"""
tcs.artifacts.evaluation
========================

Evaluation tier for Phase 5 Slice 5.3.

Takes a stored ``ResponseArtifact`` and runs the existing TCS pipeline
(GCA → rule classifier → BACK/TIS engine → decision engine) against it,
producing a ``GovernanceEvaluation``. NEVER re-calls the LLM.

The flow is straightforward:

    artifact + profile + mode
        ↓
    build a TISInput from artifact provenance
        ↓
    assemble_context_v2 (GCA — runs the rule classifier as a side effect)
        ↓
    compute_tis (deterministic engine; same as Phase 1)
        ↓
    map_decision (deterministic ladder; same as Phase 1)
        ↓
    mode-dependent TC issuance:
        observe → TC with lifecycle_state="observed", logged_only
        enforce → TC with the normal lifecycle, decision drives action
        what_if → NO TC; counterfactual_only
        ↓
    GovernanceEvaluation with full policy_profile_snapshot

Why this passes through assemble_context_v2 instead of recomputing
signals from scratch: the v2 path already integrates the rule
classifier and produces the context_metadata shape the engine
consumes. Reusing it keeps the rule audit (governance_rule_matches),
the C3 hard-stop behavior, and the active_policy_profile_id binding
consistent with /v2/query.

What this layer is NOT:

  - A typed-facts evaluator. recipient_context.pregnant=True does not
    by itself fire any rule today; the typed-facts envelope evaluator
    that does that is the next slice. For Slice 5.3, human-composed
    artifacts are evaluated using the current term-group rules plus
    BACK/TIS scoring against the active policy. That is the locked
    scope.

  - A workflow-trace reassembler. agent_workflow artifacts carry a
    serialized workflow_trace dict, but Slice 5.3 evaluates them via
    the same metadata path as rag_llm. Reconstructing a live trace
    from a stored dict (so we could use _aggregate_back_scores per
    event signals) is a richer-signal future enhancement.

Default dimension scores per generation_mode (starting points; the
rule classifier and BACK adjustments inside assemble_context_v2 then
modify them):

  raw_llm
      B=1.00  A=0.65  C=1.00  K depends on provider
      A is intentionally low: no retrieval evidence, the model's
      claims are unattributed by construction. Under a high-risk
      policy with A gate ≥ 0.85, this naturally Holds — exactly what
      the user pinned as "the point of replay."

  rag_llm
      B=1.00  A=0.85→0.95  C=1.00  K depends on provider
      A is raised to 0.95 when every chunk has source_doc + version
      + similarity_score ≥ 0.85 (well-attributed retrieval).

  agent_workflow
      Same as rag_llm in this slice. Future enhancement: reuse the
      workflow trace's per-event BACK signals.

  human_composed
      B=1.00  A=0.70  C=1.00  K=0.95
      K is high because the text was authored by a human (no
      hallucination risk). A is moderate because no automated
      retrieval ran; under a high-risk policy this still drives
      a Hold or Stop, which is correct.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from tcs.artifacts.models import (
    EVALUATION_MODE_ENFORCE,
    EVALUATION_MODE_OBSERVE,
    EVALUATION_MODE_WHAT_IF,
    EVALUATION_ORIGIN_DIRECT,
    EVALUATION_STRATEGY_ARTIFACT_METADATA,
    EVALUATION_STRATEGY_RUNTIME_SNAPSHOT,
    EVALUATION_STRATEGY_WHAT_IF_POLICY_REPLAY,
    GENERATION_MODE_AGENT_WORKFLOW,
    GENERATION_MODE_HUMAN_COMPOSED,
    GENERATION_MODE_RAG_LLM,
    GENERATION_MODE_RAW_LLM,
    GovernanceEvaluation,
    ResponseArtifact,
)
from tcs.decision_engine import map_decision
from tcs.governed_context import assemble_context_v2
from tcs.policy_profiles import PolicyProfile, load_profile
from tcs.tis_engine import TISInput, compute_tis
from tcs.trust_certificate import (
    TrustCertificate,
    compute_tc_hash,
    generate_certificate,
)


# --------------------------------------------------------------------------- #
# Policy snapshot                                                              #
# --------------------------------------------------------------------------- #

_SENTINEL_NOT_PROVIDED = object()


def _capture_effective_policy(profile: Any) -> Dict[str, Any]:
    """
    Capture the policy fields the TIS engine actually reads.

    Crucially this captures the EFFECTIVE values — for a
    ResolvedTISProfile produced by assemble_context_from_trace,
    that means the CT-modified weights/thresholds the engine
    actually used. A snapshot that recorded only the base policy_id
    would not reproduce the runtime decision when replayed.

    Accepts either a PolicyProfile or a ResolvedTISProfile;
    duck-typed on attribute access.
    """
    return {
        "profile_id":          getattr(profile, "profile_id", None),
        "domain":              getattr(profile, "domain", None),
        "risk_tier":           getattr(profile, "risk_tier", None),
        "action_class":        getattr(profile, "action_class", None),
        "gate_set":            sorted(getattr(profile, "gate_set", []) or []),
        "thresholds":          dict(getattr(profile, "thresholds", {}) or {}),
        "weights":             dict(getattr(profile, "weights", {}) or {}),
        "penalty_weights":     dict(getattr(profile, "penalty_weights", {}) or {}),
        "decay_rate":          float(getattr(profile, "decay_rate", 0.0)),
        "soft_hold_ceiling":   float(getattr(profile, "soft_hold_ceiling", 0.0)),
        "decision_thresholds": dict(getattr(profile, "decision_thresholds", {}) or {}),
        "invalidation_triggers": list(
            getattr(profile, "invalidation_triggers", []) or []
        ),
        "regulatory_mapping":  list(
            getattr(profile, "regulatory_mapping", []) or []
        ),
    }


def _policy_from_capture(captured: Dict[str, Any]) -> PolicyProfile:
    """
    Rebuild a fresh PolicyProfile from a captured effective-policy dict.
    Used by tis_input_from_snapshot to replay against the EXACT
    weights/thresholds/gates the runtime scored with.
    """
    return PolicyProfile(
        profile_id=captured["profile_id"],
        domain=captured.get("domain", "unknown"),
        risk_tier=captured["risk_tier"],
        action_class=captured["action_class"],
        gate_set=frozenset(captured.get("gate_set") or []),
        thresholds=dict(captured.get("thresholds") or {}),
        weights=dict(captured.get("weights") or {}),
        penalty_weights=dict(captured.get("penalty_weights") or {}),
        decay_rate=float(captured.get("decay_rate", 0.0)),
        soft_hold_ceiling=float(captured.get("soft_hold_ceiling", 0.0)),
        decision_thresholds=dict(captured.get("decision_thresholds") or {}),
        invalidation_triggers=list(captured.get("invalidation_triggers") or []),
        regulatory_mapping=list(captured.get("regulatory_mapping") or []),
        description="(replay; rebuilt from captured effective policy)",
    )


def snapshot_tis_input(tis_input: TISInput) -> Dict[str, Any]:
    """
    Capture a TISInput as a JSON-serializable dict.

    The captured shape is everything the deterministic TIS engine
    needs to reproduce the same TISResult: dimension_scores,
    sub_factor_scores, context_metadata, temporal state, identity,
    AND the EFFECTIVE policy (weights/thresholds/gate_set/etc. as
    the engine actually saw them — including CT modifiers when the
    runtime resolved the policy through assemble_context_from_trace).

    Capturing the effective policy inline is what closes the
    replay-fidelity gap: /v2/query passes a ResolvedTISProfile to
    the engine; a replay that loaded only the base profile would
    see different weights and produce a different score. The
    snapshot now records the actual numbers the engine used.

    Returns
    -------
    dict
        JSON-serializable. Suitable for the
        ``governance_input_snapshot`` field on GovernanceEvaluation.
    """
    return {
        "subject_id":         tis_input.subject_id,
        "subject_type":       tis_input.subject_type,
        # Base profile_id for cross-reference + auto-resolver logic
        # (what_if_policy_replay decides "different policy?" by
        # comparing this against the new policy_profile_id).
        "policy_profile_id":  tis_input.policy_profile.profile_id,
        # Full effective policy — the bytes the engine actually used.
        "effective_policy":   _capture_effective_policy(tis_input.policy_profile),
        "dimension_scores":   dict(tis_input.dimension_scores),
        "sub_factor_scores":  {
            d: dict(sf) for d, sf in (tis_input.sub_factor_scores or {}).items()
        },
        "context_metadata":   dict(tis_input.context_metadata or {}),
        "elapsed_hours":      float(tis_input.elapsed_hours),
        "is_valid":           int(tis_input.is_valid),
        "invalidation_event": tis_input.invalidation_event,
        "evaluation_time":    tis_input.evaluation_time.strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        ),
    }


def tis_input_from_snapshot(
    snapshot: Dict[str, Any],
    *,
    policy: Optional[PolicyProfile] = None,
) -> TISInput:
    """
    Rebuild a TISInput from a captured snapshot.

    Policy resolution:
      - If ``policy`` is supplied (typically for what_if_policy_replay
        — caller wants a DIFFERENT policy), use it.
      - Else, rebuild the policy from the snapshot's ``effective_policy``
        field. This guarantees the engine sees the EXACT same
        weights/thresholds it scored against originally — including
        CT modifiers and any other resolution applied at runtime.
        Without this, a runtime_snapshot replay against a profile
        that had CT modifiers applied at runtime would see different
        weights and produce a different score.

    Determinism guarantee: ``compute_tis(tis_input_from_snapshot(s))``
    reproduces the original ``compute_tis(original_tis_input)`` whenever
    ``s == snapshot_tis_input(original_tis_input)``. The TIS engine is
    pure; same input gives same output.
    """
    from datetime import datetime
    ev = snapshot["evaluation_time"]
    if ev.endswith("Z"):
        ev = ev[:-1] + "+00:00"
    effective_profile = policy
    if effective_profile is None:
        captured = snapshot.get("effective_policy")
        if not captured:
            raise ValueError(
                "snapshot has no effective_policy and no replacement "
                "policy was supplied; cannot rebuild TISInput"
            )
        effective_profile = _policy_from_capture(captured)
    return TISInput(
        subject_id=snapshot["subject_id"],
        subject_type=snapshot["subject_type"],
        policy_profile=effective_profile,
        dimension_scores=dict(snapshot["dimension_scores"]),
        sub_factor_scores={
            d: dict(sf)
            for d, sf in (snapshot.get("sub_factor_scores") or {}).items()
        },
        context_metadata=dict(snapshot.get("context_metadata") or {}),
        elapsed_hours=float(snapshot.get("elapsed_hours", 0.0)),
        is_valid=int(snapshot.get("is_valid", 1)),
        invalidation_event=snapshot.get("invalidation_event"),
        evaluation_time=datetime.fromisoformat(ev),
    )


def _snapshot_profile(profile: PolicyProfile) -> Dict[str, Any]:
    """
    Snapshot a PolicyProfile into a JSON-serializable dict suitable for
    persistence on the evaluation record. Per locked decision D4 — a
    future reviewer needs the exact weights/thresholds/gates active
    when this decision was rendered, even if the live profile registry
    has since been edited.

    The snapshot intentionally captures every load-bearing field. We
    do NOT freeze ``description`` here (purely human-facing string;
    can change without affecting scoring).
    """
    return {
        "profile_id":           profile.profile_id,
        "domain":               profile.domain,
        "risk_tier":            profile.risk_tier,
        "action_class":         profile.action_class,
        "gate_set":             sorted(profile.gate_set),
        "thresholds":           dict(profile.thresholds),
        "weights":              dict(profile.weights),
        "penalty_weights":      dict(profile.penalty_weights),
        "decay_rate":           float(profile.decay_rate),
        "soft_hold_ceiling":    float(profile.soft_hold_ceiling),
        "decision_thresholds":  dict(profile.decision_thresholds),
        "invalidation_triggers": list(profile.invalidation_triggers),
        "regulatory_mapping":   list(profile.regulatory_mapping),
    }


# --------------------------------------------------------------------------- #
# Provenance → starting dimension scores                                       #
# --------------------------------------------------------------------------- #

def _default_dimension_scores(artifact: ResponseArtifact) -> Dict[str, float]:
    """
    Starting BACK scores derived from the artifact's provenance.

    Treated as defaults: assemble_context_v2 + the rule classifier
    will further modify these (zero C on rule-class C3 violations,
    apply numeric penalties from rule effects, etc.). The engine
    then applies gates and the decision ladder.

    These defaults are intentionally honest about what evidence each
    generation mode actually produces. A raw_llm artifact has no
    retrieval, so attribution is lower. A human_composed artifact
    has no LLM, so calibration risk is lower. These are not arbitrary
    fudge factors — they are the load-bearing reason replay can show
    governance kicking in under stricter policies.
    """
    provider = (artifact.provider or "").lower()
    if provider in ("mock", ""):
        # Deterministic mock is highly predictable, so K stays high.
        # Empty string is the human_composed case.
        k = 0.95
    else:
        # Real LLMs are calibrated but not perfect.
        k = 0.85

    if artifact.generation_mode == GENERATION_MODE_RAW_LLM:
        return {"B": 1.0, "A": 0.65, "C": 1.0, "K": k}

    if artifact.generation_mode == GENERATION_MODE_HUMAN_COMPOSED:
        # Human authored: low hallucination risk → K high. No
        # automated retrieval → A moderate.
        return {"B": 1.0, "A": 0.70, "C": 1.0, "K": 0.95}

    # rag_llm and agent_workflow share the retrieval-grounded default.
    base = 0.85
    sources = artifact.retrieved_sources or []
    if sources and all(
        bool(s.get("source_doc"))
        and bool(s.get("version"))
        and float(s.get("similarity_score") or 0.0) >= 0.85
        for s in sources
    ):
        base = 0.95
    return {"B": 1.0, "A": base, "C": 1.0, "K": k}


def _build_classifier_query(artifact: ResponseArtifact) -> str:
    """
    Return the text the rule classifier will examine for THIS artifact.

    For LLM modes the classifier examines the user prompt — that's
    where intent-revealing terms live ("should I take", "for my
    patient", "ignore policy").

    For human_composed there is no prompt; the outbound message
    itself is what's being sent. We classify the draft. We ALSO
    flatten ``recipient_context`` into a key/value text blob so
    out-of-band facts (e.g. ``pregnant=True``) can still influence
    term-group matching today, BEFORE the typed-facts evaluator
    lands. The proper typed-facts integration is the next slice;
    this is the bridge that keeps human_composed evaluation
    non-trivial in the meantime.
    """
    if artifact.generation_mode == GENERATION_MODE_HUMAN_COMPOSED:
        draft = artifact.raw_output or ""
        rc = artifact.recipient_context or {}
        rc_blob = " ".join(f"{k}={v}" for k, v in rc.items())
        return f"{draft} {rc_blob}".strip()
    return artifact.prompt or ""


def _build_metadata_from_artifact(
    artifact: ResponseArtifact,
    *,
    classifier_query: str,
) -> Dict[str, Any]:
    """
    Build the metadata dict that ``assemble_context_v2`` consumes.

    Captures retrieved_chunks (so attribution-gap counting runs the
    same way it does at generation time), the prompt (for the rule
    classifier — set above per mode), and a few connection-type
    hints derived from the active generation mode.
    """
    retrieved_chunks: List[Dict[str, Any]] = []
    if artifact.retrieved_sources:
        # The chunks may have been stored without their content. The
        # classifier and attribution-gap counter only need metadata
        # presence, so this is fine.
        for s in artifact.retrieved_sources:
            retrieved_chunks.append({
                "chunk_id":         s.get("chunk_id"),
                "source_doc":       s.get("source_doc"),
                "version":          s.get("version"),
                "similarity_score": s.get("similarity_score"),
                "content":          "",
            })

    # Connection-type hint: rag_llm and agent_workflow → CT-4 (vector
    # DB); raw_llm and human_composed → CT-1 (API or direct).
    if artifact.generation_mode in (
        GENERATION_MODE_RAG_LLM, GENERATION_MODE_AGENT_WORKFLOW,
    ):
        ct = "CT-4"
    else:
        ct = "CT-1"

    meta: Dict[str, Any] = {
        "prompt":           classifier_query,
        "retrieved_chunks": retrieved_chunks,
        "connection_type":  ct,
        # Phase-1 penalty defaults; assemble_context_v2 fills in
        # anything left as None with its own defaults.
        "n_gaps":               None,
        "context_age_hours":    0.1,
        "novelty_score":        0.0,
        "days_since_review":    1,
        "is_policy_sensitive":  False,
        # Composer audit (carried through to the TC for parity with
        # how /v2/query populates it).
        "generation_mode":      artifact.generation_mode,
    }
    return meta


# --------------------------------------------------------------------------- #
# Strategy-specific scoring paths                                              #
# --------------------------------------------------------------------------- #

def _score_via_artifact_metadata(
    artifact: ResponseArtifact, profile: PolicyProfile, eval_time: datetime,
) -> TISInput:
    """
    Build a fresh TISInput from the artifact's stored provenance.

    This is the metadata-driven path: rebuild signals from
    retrieved_sources, recipient_context, generation_mode defaults.
    Used when no prior runtime snapshot exists, OR when the caller
    explicitly asks for a fresh metadata-based re-evaluation.

    Phase 5 Slice 5.5a: runs the typed-context rule evaluator
    alongside the term-group classifier. Typed-context rules fire
    on the combination of recipient_context typed facts +
    draft-text matching — most importantly, the lithium-to-pregnant-
    patient outbound case the pure term-group rules cannot catch.
    Both evaluators emit RuleMatch objects with the same audit shape;
    we merge them into a single governance_rule_matches list and
    re-run effect aggregation so the merged decision_pressure /
    blocking_reason / penalties reflect the union.
    """
    classifier_query = _build_classifier_query(artifact)
    meta_in = _build_metadata_from_artifact(
        artifact, classifier_query=classifier_query,
    )
    ctx, _resolved = assemble_context_v2(meta_in, base_profile=profile)

    # ---- Slice 5.5a: typed-context rules ----------------------------- #
    # Run the typed-context evaluator over the artifact's structured
    # recipient_context + draft. Merge matching rules into the
    # existing governance_rule_matches list so the audit shape
    # stays uniform across rule sources.
    typed_matches = _apply_typed_context_rules(
        artifact=artifact, profile=profile, ctx=ctx,
    )

    dim_scores = _default_dimension_scores(artifact)
    if ctx.get("c3_score_computed", 1.0) == 0.0:
        dim_scores["C"] = 0.0

    # Apply rule-emitted numeric penalties to dim scores (includes
    # both term-group and typed-context rule matches because they
    # share the governance_rule_matches list at this point).
    if isinstance(ctx.get("governance_rule_matches"), list):
        b_pen = a_pen = k_pen = 0.0
        for m in ctx["governance_rule_matches"]:
            eff = (m or {}).get("effect") or {}
            b_pen += float(eff.get("boundedness_penalty") or 0.0)
            a_pen += float(eff.get("attribution_penalty") or 0.0)
            k_pen += float(eff.get("known_calibration_penalty") or 0.0)
        if b_pen:
            dim_scores["B"] = max(0.0, dim_scores["B"] - min(1.0, b_pen))
        if a_pen:
            dim_scores["A"] = max(0.0, dim_scores["A"] - min(1.0, a_pen))
        if k_pen:
            dim_scores["K"] = max(0.0, dim_scores["K"] - min(1.0, k_pen))

    sub_factor_scores = {"C": {"C3": ctx.get("c3_score_computed", 1.0)}}
    return TISInput(
        subject_id=artifact.artifact_id,
        subject_type=artifact.generation_mode,
        policy_profile=profile,
        dimension_scores=dim_scores,
        sub_factor_scores=sub_factor_scores,
        context_metadata=ctx,
        elapsed_hours=0.0,
        is_valid=1,
        invalidation_event=None,
        evaluation_time=eval_time,
    )


def _apply_typed_context_rules(
    *,
    artifact: ResponseArtifact,
    profile: PolicyProfile,
    ctx: Dict[str, Any],
) -> List[Any]:
    """
    Run the typed-context evaluator and merge results into the
    existing rule-audit pipeline.

    Side effects on ``ctx``:
      - extends ``ctx["governance_rule_matches"]`` with the typed-
        context rule matches (in addition to term-group matches the
        GCA already populated)
      - sets / updates ``ctx["governance_rule_blocking_reason"]``,
        ``ctx["governance_rule_decision_pressure"]``,
        ``ctx["governance_rule_requires_human_review"]``,
        ``ctx["governance_override_policy"]`` from the typed-context
        merged effect when stronger than what's already set
      - records ``active_policy_profile_id`` on every typed-context
        audit dict so the audit format stays uniform with term-group
        matches

    Returns the list of RuleMatch objects that fired (may be empty).
    """
    from tcs.governance import (
        evaluate_typed_context_rules,
        merge_effects,
    )

    # Draft text: for human_composed the artifact's raw_output IS
    # the outbound message. For other generation modes, the draft
    # is the prompt + raw_output (so rules can match against
    # either). Most typed-context rules today target human_composed
    # via applies_to_generation_modes; the broader text is just
    # defensive for future rules.
    if artifact.generation_mode == "human_composed":
        draft_text = artifact.raw_output or ""
    else:
        draft_text = f"{artifact.prompt or ''} {artifact.raw_output or ''}"

    domain = getattr(profile, "domain", None)
    matches = evaluate_typed_context_rules(
        generation_mode=artifact.generation_mode,
        recipient_context=artifact.recipient_context,
        draft_text=draft_text,
        domain=domain,
    )
    if not matches:
        return []

    # Append per-rule audit dicts to the existing list.
    existing = ctx.get("governance_rule_matches") or []
    active_profile_id = getattr(profile, "profile_id", None)
    for m in matches:
        d = m.to_audit_dict()
        d["active_policy_profile_id"] = active_profile_id
        d["rule_evaluator"] = "typed_context"  # audit hint
        existing.append(d)
    ctx["governance_rule_matches"] = existing

    # Re-aggregate the merged effect across typed-context matches
    # (the term-group merge already ran inside assemble_context_v2;
    # we apply the typed-context aggregate on top).
    agg = merge_effects(matches)

    if agg.blocking_reason:
        # Surface as the governance rule's reason. We do NOT
        # overwrite an existing blocking_context that came from a
        # term-group C3 violation (those are strictly more severe);
        # otherwise we set it so the TC machinery can pick it up.
        if not ctx.get("blocking_context"):
            cat = agg.primary_safety_category
            ctx["blocking_context"] = (
                f"{cat}:{agg.blocking_reason}" if cat else agg.blocking_reason
            )
        # Always record the typed-context-side reason for audit.
        ctx["governance_rule_blocking_reason"] = (
            ctx.get("governance_rule_blocking_reason") or agg.blocking_reason
        )

    if agg.requires_human_review:
        ctx["governance_rule_requires_human_review"] = True

    if agg.decision_pressure:
        # Stronger decision_pressure wins (STOP > HOLD > ESCALATE).
        existing_pressure = ctx.get("governance_rule_decision_pressure")
        pressure_priority = {"STOP": 3, "HOLD": 2, "ESCALATE": 1}
        if (
            pressure_priority.get(agg.decision_pressure, 0)
            > pressure_priority.get(existing_pressure or "", 0)
        ):
            ctx["governance_rule_decision_pressure"] = agg.decision_pressure

    if agg.override_policy and not ctx.get("governance_override_policy"):
        ctx["governance_override_policy"] = agg.override_policy

    return matches


def _resolve_strategy(
    caller_strategy: Optional[str],
    source_snapshot: Optional[Dict[str, Any]],
    policy: PolicyProfile,
) -> str:
    """
    Decide which strategy to actually use based on caller intent and
    snapshot availability.

    Rules:
      - Caller explicitly named a strategy → honor it.
        - runtime_snapshot requires a source_snapshot.
        - what_if_policy_replay requires a source_snapshot AND the
          policy must differ from the snapshot's original policy.
      - Caller passed None → "auto":
        - source_snapshot present + same policy → runtime_snapshot
        - source_snapshot present + different policy → what_if_policy_replay
          (caller is asking the same evidence to be re-scored under a
          different policy; that's the textbook what-if case)
        - source_snapshot absent → artifact_metadata
    """
    if caller_strategy is not None:
        if caller_strategy not in _ALL_STRATEGIES:
            raise ValueError(
                f"unknown evaluation_strategy {caller_strategy!r}; "
                f"expected one of: {sorted(_ALL_STRATEGIES)}"
            )
        if caller_strategy == EVALUATION_STRATEGY_RUNTIME_SNAPSHOT:
            if not source_snapshot:
                raise ValueError(
                    "evaluation_strategy=runtime_snapshot requires a "
                    "prior captured TISInput snapshot on the artifact "
                    "(no such snapshot found). Either run /v2/query first "
                    "or use evaluation_strategy=artifact_metadata."
                )
        if caller_strategy == EVALUATION_STRATEGY_WHAT_IF_POLICY_REPLAY:
            if not source_snapshot:
                raise ValueError(
                    "evaluation_strategy=what_if_policy_replay requires "
                    "a prior captured TISInput snapshot on the artifact"
                )
            if source_snapshot.get("policy_profile_id") == policy.profile_id:
                raise ValueError(
                    "evaluation_strategy=what_if_policy_replay requires a "
                    "DIFFERENT policy_profile_id than the snapshot's "
                    "original policy. Pass a different policy_profile_id "
                    "or use runtime_snapshot to replay verbatim."
                )
        return caller_strategy

    # Auto-resolution rules:
    #   - snapshot exists AND policy matches → runtime_snapshot.
    #     This is the load-bearing case: /v2/query writes a snapshot,
    #     a subsequent /v2/evaluate under the SAME policy must
    #     reproduce the runtime decision deterministically. The
    #     regression test pins this.
    #   - everything else → artifact_metadata.
    #     Different policy means a fresh re-evaluation under the new
    #     policy, with signals freshly derived from artifact
    #     provenance. We deliberately do NOT auto-pick
    #     what_if_policy_replay when policies differ — that strategy
    #     reuses prior evidence (which may include rule-collapsed C=0)
    #     across the policy switch, which is rarely what an unqualified
    #     replay should mean. what_if_policy_replay is opt-in only.
    if source_snapshot and source_snapshot.get("policy_profile_id") == policy.profile_id:
        return EVALUATION_STRATEGY_RUNTIME_SNAPSHOT
    return EVALUATION_STRATEGY_ARTIFACT_METADATA


_ALL_STRATEGIES = frozenset({
    EVALUATION_STRATEGY_RUNTIME_SNAPSHOT,
    EVALUATION_STRATEGY_ARTIFACT_METADATA,
    EVALUATION_STRATEGY_WHAT_IF_POLICY_REPLAY,
})


# --------------------------------------------------------------------------- #
# Main entry point                                                             #
# --------------------------------------------------------------------------- #

def evaluate_artifact(
    *,
    artifact: ResponseArtifact,
    mode: str,
    policy_profile_id: Optional[str] = None,
    evaluator_identity: Optional[Dict[str, Any]] = None,
    certificate_store: Any = None,
    origin: str = EVALUATION_ORIGIN_DIRECT,
    strategy: Optional[str] = None,
    source_snapshot: Optional[Dict[str, Any]] = None,
) -> Tuple[GovernanceEvaluation, Optional[TrustCertificate]]:
    """
    Evaluate a stored artifact under the given mode, policy, and
    replay strategy. NEVER re-calls the LLM.

    Parameters
    ----------
    artifact
        The ResponseArtifact loaded from the store.
    mode
        observe | enforce | what_if (the delivery-side mode).
    policy_profile_id
        Caller-resolved profile id (D3).
    evaluator_identity
        Who triggered the evaluation. Recorded on the evaluation row.
    certificate_store
        Optional CertificateStore for persisting the TC (observe/enforce only).
    origin
        direct | replay | query — call-path audit tag.
    strategy
        runtime_snapshot | artifact_metadata | what_if_policy_replay
        or None (auto-resolve). Auto picks runtime_snapshot when a
        source_snapshot is supplied AND its policy matches; picks
        what_if_policy_replay when a snapshot is supplied with a
        different policy; otherwise artifact_metadata.
    source_snapshot
        A captured TISInput dict (per snapshot_tis_input). When
        present and strategy is runtime_snapshot or
        what_if_policy_replay, the engine replays this exact input
        verbatim — producing deterministic, reproducible decisions.
        Route handlers are responsible for locating the right
        source_snapshot (typically the most recent runtime-origin
        evaluation for the artifact).

    Returns
    -------
    (evaluation, tc_or_none)
        ``evaluation`` is the constructed GovernanceEvaluation with
        the actually-used strategy + captured governance_input_snapshot
        set. ``tc_or_none`` is the TrustCertificate when issued.
    """
    if policy_profile_id is None:
        raise ValueError(
            "evaluate_artifact requires policy_profile_id (route handler "
            "resolves caller-default vs active-pack default)"
        )

    profile = load_profile(policy_profile_id)
    profile_snapshot = _snapshot_profile(profile)
    eval_time = datetime.now(timezone.utc).replace(microsecond=0)

    resolved_strategy = _resolve_strategy(strategy, source_snapshot, profile)

    if resolved_strategy == EVALUATION_STRATEGY_ARTIFACT_METADATA:
        # Fresh metadata-based scoring (the pre-5.4a behavior).
        tis_input = _score_via_artifact_metadata(artifact, profile, eval_time)
    elif resolved_strategy == EVALUATION_STRATEGY_RUNTIME_SNAPSHOT:
        # Replay the captured TISInput verbatim, including the
        # EFFECTIVE policy the runtime scored with (which may have
        # CT modifiers applied — those are baked into the snapshot's
        # effective_policy field). Same input → same TISResult → same
        # decision. This is the replay-fidelity guarantee Slice 5.4a
        # exists to deliver.
        #
        # We deliberately do NOT pass `policy=profile` here — that
        # would replace the captured effective weights with the
        # freshly-loaded BASE weights, breaking parity for CT-modified
        # runs. The snapshot's effective_policy is the source of truth.
        tis_input = tis_input_from_snapshot(source_snapshot)
        # Force eval_time to the current moment so the row is timestamped
        # consistently with other evaluations issued now. The captured
        # evaluation_time in the snapshot is preserved inside
        # governance_input_snapshot for audit reconstruction.
        tis_input = TISInput(
            subject_id=tis_input.subject_id,
            subject_type=tis_input.subject_type,
            policy_profile=tis_input.policy_profile,
            dimension_scores=tis_input.dimension_scores,
            sub_factor_scores=tis_input.sub_factor_scores,
            context_metadata=tis_input.context_metadata,
            elapsed_hours=tis_input.elapsed_hours,
            is_valid=tis_input.is_valid,
            invalidation_event=tis_input.invalidation_event,
            evaluation_time=eval_time,
        )
    else:  # EVALUATION_STRATEGY_WHAT_IF_POLICY_REPLAY
        # Same evidence (dim_scores + sub_factors + context_metadata)
        # under a DIFFERENT policy. Isolates policy impact from
        # evidence drift.
        tis_input = tis_input_from_snapshot(source_snapshot, policy=profile)
        tis_input = TISInput(
            subject_id=tis_input.subject_id,
            subject_type=tis_input.subject_type,
            policy_profile=profile,
            dimension_scores=tis_input.dimension_scores,
            sub_factor_scores=tis_input.sub_factor_scores,
            context_metadata=tis_input.context_metadata,
            elapsed_hours=tis_input.elapsed_hours,
            is_valid=tis_input.is_valid,
            invalidation_event=tis_input.invalidation_event,
            evaluation_time=eval_time,
        )

    tis_result = compute_tis(tis_input)
    decision, requires_review = map_decision(tis_input, tis_result)

    # Capture the actual TISInput used for THIS evaluation.
    # Every evaluation (regardless of strategy) carries a snapshot so
    # any future replay can reproduce it deterministically.
    captured_snapshot = snapshot_tis_input(tis_input)
    dim_scores = dict(tis_input.dimension_scores)
    ctx = tis_input.context_metadata

    # Trust Certificate issuance:
    #   observe → TC issued, lifecycle_state="observed" (per D1)
    #   enforce → TC issued, normal lifecycle from decision
    #   what_if → no TC (counterfactual only, per locked clarification)
    tc: Optional[TrustCertificate] = None
    tc_id: Optional[str] = None

    if mode in (EVALUATION_MODE_OBSERVE, EVALUATION_MODE_ENFORCE):
        tc = generate_certificate(
            tis_input, tis_result, decision, requires_review,
        )
        # For observe mode, override the lifecycle_state so the TC
        # is clearly marked as audit-only and never confused with an
        # enforce-mode TC that altered delivery. The TC schema's
        # lifecycle_state field accepts free-form strings; we use
        # "observed" per locked decision D1.
        if mode == EVALUATION_MODE_OBSERVE:
            object.__setattr__(tc, "lifecycle_state", "observed")
            # And record an explicit state-transition entry so the
            # audit shows the override.
            tc.state_transition_history.append({
                "from": tc.state_transition_history[-1]["to"]
                        if tc.state_transition_history else "computed",
                "to": "observed",
                "timestamp": eval_time.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "reason": "Observe-mode evaluation; no delivery intervention",
            })
            # Recompute the audit-integrity hash so it's consistent
            # with the mutated content. compute_tc_hash deliberately
            # excludes the audit layer itself, so this is safe.
            if tc.audit_integrity is not None:
                new_hash = compute_tc_hash(tc.to_dict())
                tc.audit_integrity = type(tc.audit_integrity)(
                    tc_hash=new_hash,
                    previous_tc_hash=tc.audit_integrity.previous_tc_hash,
                    chain_sequence=tc.audit_integrity.chain_sequence,
                    chain_id=tc.audit_integrity.chain_id,
                    hash_algorithm=tc.audit_integrity.hash_algorithm,
                    integrity_verified=True,
                    issued_by=tc.audit_integrity.issued_by,
                )
        if certificate_store is not None:
            tc = certificate_store.issue(tc)
        tc_id = tc.certificate_id

    # Selected_standards + enabled_controls — pulled from composer
    # metadata if the active pack is a composed pack, otherwise empty.
    selected_standards: List[str] = []
    enabled_controls: List[str] = []
    try:
        from tcs.packs.pack_manager import get_active_pack
        active = get_active_pack() or {}
        if (
            active.get("is_composed_pack")
            and active.get("profile_config", {}).get("profile_id")
                == profile.profile_id
        ):
            cm = active.get("composer_metadata") or {}
            selected_standards = list(cm.get("standards") or [])
            enabled_controls = list(active.get("required_controls") or [])
    except Exception:
        pass

    evaluation = GovernanceEvaluation(
        evaluation_id=str(uuid.uuid4()),
        artifact_id=artifact.artifact_id,
        created_at=eval_time,
        mode=mode,
        policy_profile_id=profile.profile_id,
        policy_profile_snapshot=profile_snapshot,
        selected_standards=selected_standards,
        enabled_controls=enabled_controls,
        rule_matches=(
            ctx.get("governance_rule_matches")
            if isinstance(ctx, dict) else None
        ),
        component_scores={k: round(v, 4) for k, v in dim_scores.items()},
        gate_results=dict(tis_result.gate_results_by_dim),
        s_base=round(tis_result.s_base, 4),
        s_adjusted=round(tis_result.s_adj, 4),
        tis_current=round(tis_result.tis_current, 4),
        decision=decision,
        # enforcement_action is derived from (mode, decision) — don't
        # set it explicitly; the dataclass __post_init__ derives and
        # validates.
        trust_certificate_id=tc_id,
        evaluator_identity=dict(evaluator_identity or {}),
        evaluation_completeness_score=1.0,
        evaluation_origin=origin,
        evaluation_strategy=resolved_strategy,
        governance_input_snapshot=captured_snapshot,
    )
    return evaluation, tc


__all__ = ["evaluate_artifact"]
