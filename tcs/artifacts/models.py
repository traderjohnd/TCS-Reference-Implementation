"""
tcs.artifacts.models
====================

ResponseArtifact + GovernanceEvaluation dataclasses.

Both are immutable once constructed (``frozen=True``). The persistence
layer round-trips them via ``to_dict`` / ``from_dict`` with strict shape
preservation so a stored artifact can later be replayed against any
number of governance configurations.

Architectural invariants enforced here in __post_init__:

  - ResponseArtifact requires the right content for its generation_mode
    (raw_llm/rag_llm/agent_workflow need a prompt; human_composed
    requires a draft/raw_output even if prompt is None).
  - GovernanceEvaluation's enforcement_action MUST equal what
    derive_enforcement_action(mode, decision) returns. This is the
    architectural guardrail: an evaluation can never be constructed
    with mode="observe" but enforcement_action="blocked".
  - what_if evaluations MUST NOT carry a trust_certificate_id.
    observe and enforce MAY carry one (and normally do).
  - delivery_intervention is derived: True only for enforce mode with
    a non-delivered action.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from tcs.artifacts.helpers import derive_enforcement_action, hash_text


# --------------------------------------------------------------------------- #
# String constants                                                             #
# --------------------------------------------------------------------------- #

# Generation modes — what kind of producer created the artifact.
GENERATION_MODE_RAW_LLM         = "raw_llm"
GENERATION_MODE_RAG_LLM         = "rag_llm"
GENERATION_MODE_AGENT_WORKFLOW  = "agent_workflow"
GENERATION_MODE_HUMAN_COMPOSED  = "human_composed"

_GENERATION_MODES = frozenset({
    GENERATION_MODE_RAW_LLM,
    GENERATION_MODE_RAG_LLM,
    GENERATION_MODE_AGENT_WORKFLOW,
    GENERATION_MODE_HUMAN_COMPOSED,
})

# Evaluation modes — how TCS is being asked to look at the artifact.
EVALUATION_MODE_OBSERVE  = "observe"
EVALUATION_MODE_ENFORCE  = "enforce"
EVALUATION_MODE_WHAT_IF  = "what_if"

_EVALUATION_MODES = frozenset({
    EVALUATION_MODE_OBSERVE,
    EVALUATION_MODE_ENFORCE,
    EVALUATION_MODE_WHAT_IF,
})

# Enforcement actions — derived from (mode, decision). NEVER set directly
# by callers; constructed via derive_enforcement_action.
ENFORCEMENT_DELIVERED            = "delivered"
ENFORCEMENT_HELD                 = "held"
ENFORCEMENT_BLOCKED              = "blocked"
ENFORCEMENT_ESCALATED            = "escalated"
ENFORCEMENT_LOGGED_ONLY          = "logged_only"           # observe
ENFORCEMENT_COUNTERFACTUAL_ONLY  = "counterfactual_only"   # what_if


# --------------------------------------------------------------------------- #
# ResponseArtifact                                                             #
# --------------------------------------------------------------------------- #

def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def _iso(dt: datetime) -> str:
    """ISO-8601 with trailing Z (matches existing TC serialization)."""
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso(s: str) -> datetime:
    """Parse ISO-8601 with optional trailing Z."""
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    return datetime.fromisoformat(s)


@dataclass(frozen=True)
class ResponseArtifact:
    """
    Immutable capture of one generation event.

    The artifact records *everything that produced the output* so a
    later evaluation can be replayed without recreating the generation.
    That includes the system prompt, the retrieved context (for RAG),
    the workflow trace (for agent flows), the recipient context (for
    human_composed outbound messages), and the generating identity.

    Fields:

      Identity:
        artifact_id       — UUID4 string, generated if not supplied.
        created_at        — UTC datetime, set to "now" if not supplied.
        generation_mode   — one of GENERATION_MODE_*. Determines which
                            other fields are required.

      Content:
        prompt            — user-facing query (or None for
                            human_composed where there's only a draft).
        prompt_hash       — sha256 of normalized prompt. Auto-derived
                            in __post_init__ if not supplied AND
                            prompt is present.
        raw_output        — the generated text (LLM completion, draft,
                            agent final output). None on generation
                            error.
        raw_output_hash   — sha256 of normalized raw_output. Auto-
                            derived if not supplied AND raw_output is
                            present.

      Provenance:
        provider          — "openai" | "anthropic" | "mock" | None.
                            None for human_composed.
        model             — provider's model identifier.
        system_prompt_used — verbatim system prompt sent to the LLM.
                            None for human_composed.
        rag_enabled       — bool. True iff retrieval ran.
        rag_context       — concatenated retrieved chunks the LLM saw.
                            None when rag_enabled is False.
        retrieved_sources — per-chunk metadata
                            ([{chunk_id, source_doc, version,
                               similarity_score}, ...]).
        workflow_trace_id — reference to a stored trace, if any.
        workflow_trace    — full serialized GovernedWorkflowTrace dict.

      Context (critical for human_composed):
        recipient_context — typed facts about the recipient/situation.
                            For a clinician writing to a pregnant
                            client: {"pregnant": True, "role":
                            "patient", "channel": "outbound_message",
                            "medication_topic": "lithium"}.
                            Phase 5 stores these verbatim; the
                            Deterministic Bounded Control Evaluator
                            (deferred slice) will read this dict for
                            numeric envelope checks.

      Identity binding:
        generation_identity — {"requesting_identity", "identity_type",
                              "role", "session_id"} of whoever
                              triggered the generation. Required —
                              audit needs to know who produced the
                              artifact, especially for human_composed.

      Failure:
        generation_error  — non-None when generation failed; raw_output
                            is None in that case.
    """

    # Identity
    artifact_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    created_at: datetime = field(default_factory=_utcnow)
    generation_mode: str = GENERATION_MODE_RAW_LLM

    # Content
    prompt: Optional[str] = None
    prompt_hash: Optional[str] = None
    raw_output: Optional[str] = None
    raw_output_hash: Optional[str] = None

    # Provenance
    provider: Optional[str] = None
    model: Optional[str] = None
    system_prompt_used: Optional[str] = None
    rag_enabled: bool = False
    rag_context: Optional[str] = None
    retrieved_sources: List[Dict[str, Any]] = field(default_factory=list)
    workflow_trace_id: Optional[str] = None
    workflow_trace: Optional[Dict[str, Any]] = None

    # Context
    recipient_context: Dict[str, Any] = field(default_factory=dict)

    # Identity binding
    generation_identity: Dict[str, Any] = field(default_factory=dict)

    # Failure
    generation_error: Optional[str] = None

    # --- Validation + auto-derivation ----------------------------------- #

    def __post_init__(self) -> None:
        # generation_mode must be one of the four declared values.
        if self.generation_mode not in _GENERATION_MODES:
            raise ValueError(
                f"unknown generation_mode {self.generation_mode!r}; "
                f"expected one of: {sorted(_GENERATION_MODES)}"
            )

        # raw_llm / rag_llm / agent_workflow require a prompt.
        # human_composed requires raw_output (the draft text). The
        # prompt may be None for human_composed (the human typed
        # the draft directly without a query/prompt frame).
        if self.generation_mode == GENERATION_MODE_HUMAN_COMPOSED:
            if not (self.raw_output or self.generation_error):
                raise ValueError(
                    "human_composed artifacts require raw_output "
                    "(the draft text) — got None and no generation_error"
                )
        else:
            if self.prompt is None and self.generation_error is None:
                raise ValueError(
                    f"{self.generation_mode} artifacts require a prompt"
                )

        # Auto-derive hashes when content is present and hash not given.
        # Use object.__setattr__ because frozen=True blocks normal
        # attribute assignment — this is the standard escape hatch for
        # __post_init__ derivation on frozen dataclasses.
        if self.prompt is not None and self.prompt_hash is None:
            object.__setattr__(self, "prompt_hash", hash_text(self.prompt))
        if self.raw_output is not None and self.raw_output_hash is None:
            object.__setattr__(
                self, "raw_output_hash", hash_text(self.raw_output)
            )

        # RAG sanity: rag_context only meaningful when rag_enabled.
        if self.rag_context and not self.rag_enabled:
            raise ValueError(
                "rag_context is set but rag_enabled is False — "
                "either set rag_enabled=True or clear rag_context"
            )

    # --- Serialization --------------------------------------------------- #

    def to_dict(self) -> Dict[str, Any]:
        """JSON-serializable dict suitable for content_json persistence."""
        return {
            "artifact_id": self.artifact_id,
            "created_at": _iso(self.created_at),
            "generation_mode": self.generation_mode,
            "prompt": self.prompt,
            "prompt_hash": self.prompt_hash,
            "raw_output": self.raw_output,
            "raw_output_hash": self.raw_output_hash,
            "provider": self.provider,
            "model": self.model,
            "system_prompt_used": self.system_prompt_used,
            "rag_enabled": bool(self.rag_enabled),
            "rag_context": self.rag_context,
            "retrieved_sources": list(self.retrieved_sources),
            "workflow_trace_id": self.workflow_trace_id,
            "workflow_trace": (
                dict(self.workflow_trace)
                if self.workflow_trace is not None else None
            ),
            "recipient_context": dict(self.recipient_context),
            "generation_identity": dict(self.generation_identity),
            "generation_error": self.generation_error,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "ResponseArtifact":
        return cls(
            artifact_id=d["artifact_id"],
            created_at=_parse_iso(d["created_at"]),
            generation_mode=d["generation_mode"],
            prompt=d.get("prompt"),
            prompt_hash=d.get("prompt_hash"),
            raw_output=d.get("raw_output"),
            raw_output_hash=d.get("raw_output_hash"),
            provider=d.get("provider"),
            model=d.get("model"),
            system_prompt_used=d.get("system_prompt_used"),
            rag_enabled=bool(d.get("rag_enabled", False)),
            rag_context=d.get("rag_context"),
            retrieved_sources=list(d.get("retrieved_sources") or []),
            workflow_trace_id=d.get("workflow_trace_id"),
            workflow_trace=d.get("workflow_trace"),
            recipient_context=dict(d.get("recipient_context") or {}),
            generation_identity=dict(d.get("generation_identity") or {}),
            generation_error=d.get("generation_error"),
        )


# --------------------------------------------------------------------------- #
# GovernanceEvaluation                                                         #
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class GovernanceEvaluation:
    """
    One pass of TCS against one ResponseArtifact. Immutable.

    Many evaluations may target the same artifact_id — that's the
    whole point of the replay system: "evaluate the same captured
    output under different policies and modes, then compare."

    Per D4 (locked decision): the FULL policy profile config is
    snapshotted into ``policy_profile_snapshot`` at evaluation time.
    Referencing the profile_id alone is not enough — a future
    reviewer needs the exact weights, thresholds, gates, and standards
    interpretation that were active when this decision was rendered,
    even if the profile registry has since been edited.

    Fields:

      Identity:
        evaluation_id              — UUID4.
        artifact_id                — FK to ResponseArtifact.
        created_at                 — UTC datetime.

      Mode:
        mode                       — observe | enforce | what_if.

      Policy (caller-provided, defaults to active pack — D3):
        policy_profile_id          — string id (e.g. "composed-abc...").
        policy_profile_snapshot    — full profile dict at evaluation
                                     time (audit-grade reproducibility).
        selected_standards         — list of standard ids (composed packs).
        enabled_controls           — list of control identifiers that ran.

      Scoring (snapshotted at evaluation time):
        rule_matches               — list of governance_rule_matches dicts
                                     (same shape as on the TC).
        component_scores           — {"B", "A", "C", "K"}.
        gate_results               — per-dim "pass"|"fail"|"not_applicable".
        s_base, s_adjusted, tis_current — three of the five score values.

      Decision + enforcement:
        decision                   — Allow|Observe|Hold|Escalate|Stop
                                     (or one of the Phase-3 refinements).
        enforcement_action         — derived from (mode, decision) per
                                     helpers.derive_enforcement_action.
                                     Validated in __post_init__.
        delivery_intervention      — True iff this evaluation actually
                                     altered delivery (i.e. mode=enforce
                                     AND enforcement_action ≠ delivered).
                                     Always False for observe and what_if.

      Audit:
        trust_certificate_id       — TC issued for this evaluation. None
                                     for what_if (per locked clarification).
                                     Present for observe and enforce.
        evaluator_identity         — who triggered the evaluation.
        evaluation_completeness_score — from GovernanceStatus layer.
    """

    # Identity
    evaluation_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    artifact_id: str = ""
    created_at: datetime = field(default_factory=_utcnow)

    # Mode
    mode: str = EVALUATION_MODE_OBSERVE

    # Policy
    policy_profile_id: str = ""
    policy_profile_snapshot: Dict[str, Any] = field(default_factory=dict)
    selected_standards: List[str] = field(default_factory=list)
    enabled_controls: List[str] = field(default_factory=list)

    # Scoring
    rule_matches: Optional[List[Dict[str, Any]]] = None
    component_scores: Dict[str, float] = field(default_factory=dict)
    gate_results: Dict[str, str] = field(default_factory=dict)
    s_base: float = 0.0
    s_adjusted: float = 0.0
    tis_current: float = 0.0

    # Decision + enforcement
    decision: str = "Allow"
    enforcement_action: str = ""
    delivery_intervention: bool = False

    # Audit
    trust_certificate_id: Optional[str] = None
    evaluator_identity: Dict[str, Any] = field(default_factory=dict)
    evaluation_completeness_score: float = 1.0

    # --- Validation + derivation ---------------------------------------- #

    def __post_init__(self) -> None:
        # Mode must be one of the three declared values.
        if self.mode not in _EVALUATION_MODES:
            raise ValueError(
                f"unknown evaluation mode {self.mode!r}; "
                f"expected one of: {sorted(_EVALUATION_MODES)}"
            )

        # artifact_id is required (the whole point — an evaluation
        # always evaluates *something*).
        if not self.artifact_id:
            raise ValueError("artifact_id is required on GovernanceEvaluation")

        # enforcement_action is derived from (mode, decision). If the
        # caller didn't set it, derive it. If they DID set it,
        # validate it matches what derivation would produce — this
        # catches drift between callers and the derivation rules and
        # prevents accidentally constructing observe-but-blocked or
        # what_if-but-delivered objects.
        expected_action = derive_enforcement_action(self.mode, self.decision)
        if not self.enforcement_action:
            object.__setattr__(self, "enforcement_action", expected_action)
        elif self.enforcement_action != expected_action:
            raise ValueError(
                f"enforcement_action mismatch: caller set "
                f"{self.enforcement_action!r} but (mode={self.mode!r}, "
                f"decision={self.decision!r}) requires {expected_action!r}"
            )

        # delivery_intervention is derived. Observe and what_if NEVER
        # alter delivery; enforce alters delivery iff the action is
        # not "delivered".
        expected_intervention = (
            self.mode == EVALUATION_MODE_ENFORCE
            and self.enforcement_action != ENFORCEMENT_DELIVERED
        )
        # Allow callers to omit it; validate if they set it.
        if self.delivery_intervention is False:
            object.__setattr__(
                self, "delivery_intervention", expected_intervention
            )
        elif self.delivery_intervention != expected_intervention:
            raise ValueError(
                f"delivery_intervention mismatch: caller set "
                f"{self.delivery_intervention!r} but mode={self.mode!r} "
                f"with enforcement_action={self.enforcement_action!r} "
                f"requires {expected_intervention!r}"
            )

        # TC-issuance rules (locked clarification):
        #   what_if  → NEVER issue a TC. trust_certificate_id MUST be None.
        #   observe  → TC may be issued (recommended, lifecycle "observed").
        #   enforce  → TC may be issued (normally is).
        if (
            self.mode == EVALUATION_MODE_WHAT_IF
            and self.trust_certificate_id is not None
        ):
            raise ValueError(
                "what_if evaluations MUST NOT carry a trust_certificate_id "
                "(per locked design decision: counterfactual only, no TC)"
            )

    # --- Serialization --------------------------------------------------- #

    def to_dict(self) -> Dict[str, Any]:
        return {
            "evaluation_id": self.evaluation_id,
            "artifact_id": self.artifact_id,
            "created_at": _iso(self.created_at),
            "mode": self.mode,
            "policy_profile_id": self.policy_profile_id,
            "policy_profile_snapshot": dict(self.policy_profile_snapshot),
            "selected_standards": list(self.selected_standards),
            "enabled_controls": list(self.enabled_controls),
            "rule_matches": (
                [dict(m) for m in self.rule_matches]
                if self.rule_matches is not None else None
            ),
            "component_scores": dict(self.component_scores),
            "gate_results": dict(self.gate_results),
            "s_base": float(self.s_base),
            "s_adjusted": float(self.s_adjusted),
            "tis_current": float(self.tis_current),
            "decision": self.decision,
            "enforcement_action": self.enforcement_action,
            "delivery_intervention": bool(self.delivery_intervention),
            "trust_certificate_id": self.trust_certificate_id,
            "evaluator_identity": dict(self.evaluator_identity),
            "evaluation_completeness_score": float(
                self.evaluation_completeness_score
            ),
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "GovernanceEvaluation":
        return cls(
            evaluation_id=d["evaluation_id"],
            artifact_id=d["artifact_id"],
            created_at=_parse_iso(d["created_at"]),
            mode=d["mode"],
            policy_profile_id=d.get("policy_profile_id", ""),
            policy_profile_snapshot=dict(d.get("policy_profile_snapshot") or {}),
            selected_standards=list(d.get("selected_standards") or []),
            enabled_controls=list(d.get("enabled_controls") or []),
            rule_matches=(
                [dict(m) for m in d["rule_matches"]]
                if d.get("rule_matches") is not None else None
            ),
            component_scores=dict(d.get("component_scores") or {}),
            gate_results=dict(d.get("gate_results") or {}),
            s_base=float(d.get("s_base", 0.0)),
            s_adjusted=float(d.get("s_adjusted", 0.0)),
            tis_current=float(d.get("tis_current", 0.0)),
            decision=d.get("decision", "Allow"),
            enforcement_action=d.get("enforcement_action", ""),
            delivery_intervention=bool(d.get("delivery_intervention", False)),
            trust_certificate_id=d.get("trust_certificate_id"),
            evaluator_identity=dict(d.get("evaluator_identity") or {}),
            evaluation_completeness_score=float(
                d.get("evaluation_completeness_score", 1.0)
            ),
        )
