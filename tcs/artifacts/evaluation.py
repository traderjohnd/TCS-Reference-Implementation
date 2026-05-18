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
) -> Tuple[GovernanceEvaluation, Optional[TrustCertificate]]:
    """
    Evaluate a stored artifact under the given mode and policy.

    Parameters
    ----------
    artifact
        The ResponseArtifact loaded from the store. NEVER re-generated.
    mode
        One of "observe", "enforce", "what_if".
    policy_profile_id
        Caller-provided profile id (D3). If None, the caller is
        expected to have resolved the active pack id before calling
        — this function does NOT consult the pack manager (keeping
        the dependency surface narrow). The route handler is the
        right place to resolve "default to active pack."
    evaluator_identity
        Who triggered the evaluation. Recorded on the evaluation row
        and the TC (if one is issued).
    certificate_store
        Optional CertificateStore for persisting the TC. If None and
        mode is observe/enforce, the TC is generated but not
        persisted — caller can persist later. If mode is what_if,
        no TC is constructed regardless.

    Returns
    -------
    (evaluation, tc_or_none)
        ``evaluation`` is the constructed GovernanceEvaluation (NOT
        yet persisted by this function — caller writes to ArtifactStore).
        ``tc_or_none`` is the TrustCertificate when issued (observe
        or enforce), or None for what_if.
    """
    if policy_profile_id is None:
        raise ValueError(
            "evaluate_artifact requires policy_profile_id (route handler "
            "resolves caller-default vs active-pack default)"
        )

    profile = load_profile(policy_profile_id)
    profile_snapshot = _snapshot_profile(profile)
    classifier_query = _build_classifier_query(artifact)
    meta_in = _build_metadata_from_artifact(
        artifact, classifier_query=classifier_query,
    )

    # GCA. Runs the rule classifier as a side effect; populates
    # ctx["c3_score_computed"], ctx["governance_rule_matches"], etc.
    ctx, resolved = assemble_context_v2(
        meta_in,
        base_profile=profile,
    )

    # Starting dimension scores from provenance. The classifier may
    # have already collapsed C via ctx["c3_score_computed"]==0.0
    # (no-op here because dim_scores didn't go into assemble_context_v2,
    # but we mirror that effect on the computed scores below).
    dim_scores = _default_dimension_scores(artifact)
    if ctx.get("c3_score_computed", 1.0) == 0.0:
        # C3 violation detected by classifier — collapse C so the gate
        # fails and Priority 2 of the decision ladder fires.
        dim_scores["C"] = 0.0

    # Apply rule-emitted numeric penalties to dim scores (the
    # classifier records boundedness_penalty, attribution_penalty,
    # known_calibration_penalty on the merged effect; assemble_context_v2
    # already mutated ctx but we never passed dim_scores in, so we
    # must apply the deltas ourselves here).
    # These are stored in the per-rule audit dicts; sum and clamp.
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

    # Build the TISInput. The subject_id is the artifact_id —
    # everything in the TC and evaluation can be joined back to the
    # captured generation.
    eval_time = datetime.now(timezone.utc).replace(microsecond=0)
    sub_factor_scores = {"C": {"C3": ctx.get("c3_score_computed", 1.0)}}
    tis_input = TISInput(
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

    tis_result = compute_tis(tis_input)
    decision, requires_review = map_decision(tis_input, tis_result)

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
        rule_matches=ctx.get("governance_rule_matches"),
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
    )
    return evaluation, tc


__all__ = ["evaluate_artifact"]
