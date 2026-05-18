"""
tcs.trust_certificate
=====================

Generate the Trust Certificate (TC) artifact.

The TC is the governance record produced for every TIS evaluation —
including Stop decisions. Its schema is defined in ``TC_SCHEMA.md`` and
has seven mandatory layers:

    I    — Identity       (certificate_id, subject, domain, policy)
    S    — Score          (tis_raw, tis_adjusted, tis_current, penalties)
    G    — Gate           (gate_set, thresholds, per-dim results)
    Dec  — Decision       (Allow/Observe/Hold/Escalate/Stop + review flag)
    Prov — Provenance     (source refs, chain of custody, audit log)
    T    — Temporal       (issued, valid_until, decay, invalidation)
    E    — Explanation    (legible summary for regulatory examiner)
    L    — Lifecycle      (state + transition history)

No computation happens in this module. It consumes a :class:`TISInput`
and :class:`TISResult` (from ``tcs.tis_engine``) plus the decision made
by ``tcs.decision_engine`` and assembles the complete artifact.

Compliance requirements enforced here (TCS_SPEC.md §13, TC_SCHEMA.md):

    - certificate_id is a globally unique UUID4
    - tis_raw, tis_adjusted, tis_current recorded as three distinct fields
    - all four component_scores and component_weights present
    - all five penalty components in penalty_breakdown (zero-valued included)
    - gate_results present for all four dimensions (pass/fail/not_applicable)
    - valid_until computed from decay_rate
    - lifecycle_state + state_transition_history populated
    - explanation_summary legible without source code access
    - regulatory_mapping preserved from the policy profile
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

from tcs.policy_profiles import PolicyProfile
from tcs.tis_engine import TISInput, TISResult


# --------------------------------------------------------------------------- #
# Constants and mappings                                                       #
# --------------------------------------------------------------------------- #

#: Map decision → initial lifecycle state at TC issuance.
#: Overridden to "invalidated" when is_valid == 0 (TCS_SPEC.md §11, §14;
#: TEST_SCENARIOS.md scenario 7).
DECISION_TO_LIFECYCLE: Dict[str, str] = {
    "Allow":                "admissible",
    "Observe":              "admissible",
    "Hold":                 "computed",
    "Escalate":             "computed",
    "Stop":                 "blocked",
    # Phase 3 qualified outcomes (TCS-BUILD-003 Step 1)
    "Allow_with_logging":   "admissible",
    "Allow_with_redaction": "admissible",
    "Allow_with_step_up":   "admissible",
    "Rollback":             "blocked",
}

#: Human-readable labels for dimensions (for explanation text).
_DIM_LABELS: Dict[str, str] = {
    "B": "Boundedness",
    "A": "Attribution",
    "C": "Compliance",
    "K": "Known",
}

#: Default escalation routing by domain. Phase 1 uses simple defaults;
#: Phase 2 will make these profile-configurable.
_DEFAULT_ESCALATION_ROUTING: Dict[str, List[str]] = {
    "healthcare":           ["attending_physician"],
    "financial_services":   ["compliance_officer"],
    "pharma_life_sciences": ["qualified_person", "pharmacovigilance_lead"],
    "enterprise":           ["operations_lead"],
    "manufacturing":        ["process_engineer"],
    "gaming":               ["responsible_gaming_lead"],
    "real_estate":          ["broker_of_record"],
}

_FLOAT_PRECISION: int = 4


def _r(value: float) -> float:
    return round(float(value), _FLOAT_PRECISION)


# --------------------------------------------------------------------------- #
# Trust Enforcement Layer dataclasses (TCS-TEL-001, TCS_SPEC.md §19)           #
# --------------------------------------------------------------------------- #
#
# Four enforcement layers added on top of the seven canonical layers (I, S,
# G, Prov, T, E, L). The TC now has 11 layers:
#
#     Id  — IdentityBinding       (who caused the evaluation)
#     GS  — GovernanceStatus      (was governance complete or degraded)
#     AI  — AuditIntegrity        (SHA-256 hash chain)
#     Ov  — OverrideRecord        (human exception handling)
#
# All four are required for Phase 1 completion per TCS_SPEC.md §19. In
# Phase 1 these carry stub values populated by ``generate_certificate()``;
# Phase 2 wires them to real identity providers, governance health signals,
# persistent hash chains, and override workflows.


@dataclass
class IdentityBinding:
    """
    Layer Id — who caused the evaluation.

    The identity_confidence and identity_verified fields have operational
    effects on the TIS engine:
        - identity_confidence < 0.30 with a T2+ request collapses the B3
          sub-factor to 0.00, which drives the B gate to fail.
        - identity_verified = False with a T3 request sets B = 0.00
          unconditionally — immediate gate failure.

    These checks run in ``tcs.tis_engine.compute_tis`` before gate
    evaluation. The values themselves are carried here in the TC for
    audit reconstruction.
    """
    requesting_identity: str        # Authenticated principal ID
    identity_type: str              # human | system | agent | automated_pipeline
    role: str                       # Organizational role at request time
    authorization_tier: str         # T1 | T2 | T3 — highest accessible tier
    identity_confidence: float      # [0,1] — 1.0=hardware token, 0.8=OAuth+MFA, 0.5=session
    identity_verified: bool         # Positively verified against identity provider
    authentication_method: str      # oauth2_mfa | saml | api_key | certificate | session_token
    requesting_session_id: str      # Binds evaluation to authentication event


@dataclass
class GovernanceStatus:
    """
    Layer GS — was the governance evaluation complete, degraded, or failed.

    The ``governance_status`` field is the top-level signal a downstream
    consumer checks before treating the TC as authoritative:

        - complete:  every component ran, no fail-safe invoked
        - degraded:  some components skipped, fail-safe applied, TC still
                     usable with appropriate caveats
        - minimal:   skeletal TC (identity + fail-safe decision only);
                     not authoritative
        - failed:    governance infrastructure broken; TC CANNOT authorize
                     any action (C-P.18)

    ``evaluation_completeness_score`` is a continuous [0,1] measure that
    correlates with ``governance_status`` but carries finer granularity
    for dashboards and trend analysis.
    """
    governance_status: str                        # complete | degraded | minimal | failed
    evaluation_completeness_score: float          # [0,1]; 1.0 = all steps ran
    components_evaluated: List[str]               # Steps that completed
    components_skipped: List[str]                 # Steps that could not run
    skip_reasons: Dict[str, str]                  # {component: reason}
    fail_safe_applied: bool                       # Whether fail-safe behavior was used
    fail_safe_type: Optional[str]                 # fail_closed | fail_open_with_flag | degraded_allow | degraded_hold
    governance_integrity_score: float             # [0,1] — infrastructure health at eval time


@dataclass
class AuditIntegrity:
    """
    Layer AI — cryptographic integrity via SHA-256 hash chain.

    ``tc_hash`` is computed over the canonical JSON of the TC content
    *excluding* the ``audit_integrity`` layer itself (otherwise the hash
    would have to reference itself). See ``compute_tc_hash`` for the
    canonicalization rules.

    In Phase 1:
        - chain_id is scoped to the current test session
        - previous_tc_hash is None for the first TC in a chain
        - chain_sequence starts at 1 and increments monotonically
        - integrity_verified is True on issuance (the issuing path
          has not been tampered with)

    Phase 2 replaces the in-memory chain with a persistent append-only
    archive scoped to deployment + domain + date.
    """
    tc_hash: str                                  # SHA-256 of TC content (excl. audit layer)
    previous_tc_hash: Optional[str]               # None for first TC in chain
    chain_sequence: int                           # Monotonically increasing; gaps = violation
    chain_id: str                                 # Scoped to deployment+domain+date
    hash_algorithm: str                           # "sha256" for Phase 1
    integrity_verified: bool                      # Hash verified at issuance
    issued_by: str                                # Identity of TCS service that issued the TC


@dataclass
class OverrideRecord:
    """
    Layer Ov — human override handling.

    Overrides are rare, load-bearing, and heavily constrained. Hard rules
    enforced in code and tests:

        - C3 = 0.00 Stop is NEVER overrideable (C-P.17)
        - I_inv = 0 Stop is NEVER overrideable (C-P.17)
        - override_actor must have identity_type == "human" (C-P.16)
        - r3 Stop override requires a co_authorizer (two-person rule)
        - r2+ overrides require a policy_exception_id
        - r3 overrides require a regulatory_basis

    For the Phase 1 passing scenarios, override_invoked is always False
    and all other fields are None. Phase 2 scenarios 15/16/17 exercise
    the override workflow.
    """
    override_invoked: bool                        # Was a human override applied
    original_decision: Optional[str]              # Decision before override
    override_decision: Optional[str]              # Decision after override
    override_actor: Optional[str]                 # Authenticated human identity
    override_actor_role: Optional[str]            # Role satisfying authority matrix
    override_reason: Optional[str]                # Plain-language reason
    override_type: Optional[str]                  # clinical_judgment | compliance_exception | operational_exception | regulatory_variance
    policy_exception_id: Optional[str]            # Required at r2+
    regulatory_basis: Optional[str]               # Required at r3
    co_authorizer: Optional[str]                  # Second identity for r3 Stop override
    post_override_review_required: bool
    post_override_review_deadline: Optional[str]  # ISO-8601
    post_override_review_completed: bool
    override_creates_tc_amendment: bool


# --------------------------------------------------------------------------- #
# Hash chain helper                                                            #
# --------------------------------------------------------------------------- #

def compute_tc_hash(tc_dict: Dict[str, Any]) -> str:
    """
    Compute the SHA-256 hash of a TC's content, excluding the audit layer.

    The hash covers every serialized field *except* ``audit_integrity``
    itself — that layer would otherwise have to reference its own hash,
    which is a chicken-and-egg problem. Excluding it also lets the hash
    carry forward unchanged when we later add chain bookkeeping.

    Canonicalization rules (critical for reproducible hashing):
        - ``sort_keys=True`` so key order does not affect the hash
        - ``separators=(",", ":")`` to eliminate whitespace variance
        - UTF-8 encoding before hashing

    Passing any non-JSON-serializable value in ``tc_dict`` will raise
    TypeError here, which is the correct behavior — the TC would fail
    JSON round-trip anyway.
    """
    content = {k: v for k, v in tc_dict.items() if k != "audit_integrity"}
    canonical = json.dumps(content, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def verify_chain(tcs: List["TrustCertificate"]) -> bool:
    """
    Verify hash-chain integrity across a list of TCs.

    Returns True iff every TC in the list hashes consistently with its
    recorded tc_hash AND the previous_tc_hash linkage is unbroken AND
    chain_sequence is a strictly monotonic run of +1 increments.

    This is not used in the Phase 1 passing scenarios (single-TC cases),
    but it is the verification function that Phase 2 scenario 17 will
    call to validate a multi-TC chain. Keeping it here in Phase 1 means
    the hash machinery has a complete round-trip from issuance to
    verification, which is the signal a reviewer actually wants to see.
    """
    sorted_tcs = sorted(tcs, key=lambda t: t.audit_integrity.chain_sequence)
    for i, tc in enumerate(sorted_tcs):
        computed = compute_tc_hash(tc.to_dict())
        if computed != tc.audit_integrity.tc_hash:
            return False  # Content modified
        if i > 0:
            prev = sorted_tcs[i - 1]
            if tc.audit_integrity.previous_tc_hash != prev.audit_integrity.tc_hash:
                return False  # Chain broken
            if tc.audit_integrity.chain_sequence != prev.audit_integrity.chain_sequence + 1:
                return False  # TC deleted from chain
    return True


# --------------------------------------------------------------------------- #
# TrustCertificate dataclass                                                   #
# --------------------------------------------------------------------------- #

@dataclass
class TrustCertificate:
    """
    Complete Trust Certificate. All fields from TC_SCHEMA.md are mandatory.

    Serializable to a JSON-safe dict via :meth:`to_dict` and to a pretty
    JSON string via :meth:`to_json`.
    """

    # ---- Layer I: Identity ---------------------------------------------- #
    certificate_id: str          # UUID4
    subject_id: str
    subject_type: str
    domain: str
    risk_tier: str
    action_class: str
    policy_severity: str         # "standard" for v0.1
    checkpoint_id: str
    gca_context_id: str
    policy_set_id: str           # profile.profile_id

    # ---- Layer S: Score ------------------------------------------------- #
    # Score naming (white paper alignment):
    #   s_base       = Σᵢ wᵢ · dimᵢ           (gate-independent composite)
    #   s_adjusted   = s_base * (1 - P)        (gate-independent post-penalty)
    #   tis_raw      = gate * s_base           (gated; 0 on gate fail)
    #   tis_adjusted = gate * s_adjusted       (gated; 0 on gate fail)
    #   tis_current  = s_adjusted * decay * gate * is_valid
    # The decision engine uses s_base for Priority 3/4 kappa discrimination
    # because tis_raw collapses to 0 on gate failure (white paper definition).
    s_base: float
    s_adjusted: float
    tis_raw: float
    tis_adjusted: float
    tis_current: float
    component_scores: Dict[str, float]        # B,A,C,K (BACK)
    component_weights: Dict[str, float]       # B,A,C,K (BACK); Σ = 1
    penalty_aggregate: float
    penalty_breakdown: Dict[str, float]       # P_cb,P_d,P_n,P_h,P_ps (all five)
    failing_dimension_subfactors: Dict[str, Dict[str, float]]

    # ---- Layer G: Gate -------------------------------------------------- #
    gate_set: List[str]
    thresholds: Dict[str, float]              # all four dims
    gate_results: Dict[str, str]              # pass|fail|not_applicable, all four
    gate_passed: bool
    blocking_reason: Optional[str]
    failure_mode: Optional[str]

    # ---- Decision block ------------------------------------------------- #
    decision: str                             # Allow|Observe|Hold|Escalate|Stop
    requires_human_review: bool
    escalation_routed_to: List[str]

    # ---- Layer Prov: Provenance ----------------------------------------- #
    source_references: List[str]
    retrieval_ids: List[str]
    chain_of_custody_id: str
    audit_log_id: str
    integration_boundary_gaps: int

    # ---- Layer T: Temporal ---------------------------------------------- #
    evaluation_timestamp: datetime
    valid_until: datetime
    decay_rate: float
    recompute_required: bool
    invalidation_triggers: List[str]
    last_invalidation_event: Dict[str, Any]
    invalidation_status: str                  # valid|invalidated|pending_recompute

    # ---- Layer E: Explanation ------------------------------------------- #
    explanation_summary: str
    key_factors: List[str]
    key_concerns: List[str]
    regulatory_explanation_level: str         # "regulatory"
    regulatory_mapping: List[str]

    # ---- Layer L: Lifecycle --------------------------------------------- #
    lifecycle_state: str
    state_transition_history: List[Dict[str, Any]]
    recomputed_from_certificate_id: Optional[str] = None
    superseded_by_certificate_id: Optional[str] = None
    archived: bool = False

    # ---- MCP Extensions (TCS-MCP-001 §11 — downstream bypass rules) ---- #
    # Added additively to the seven canonical layers. In Phase 1 these
    # carry stub values populated by generate_certificate(); Phase 2 will
    # wire them to real MCP server identity, scope manifests, and
    # context-freeze detection.
    #
    # mcp_server_id:     provenance ID of the MCP server that produced the
    #                    governed context (stub in Phase 1).
    # scope_attestation: C-R.13/14/15 block — perimeter coverage, context
    #                    freeze state, upstream TC references.
    mcp_server_id: Optional[str] = None
    scope_attestation: Dict[str, Any] = field(default_factory=dict)

    # ---- CT Audit Fields (TCS-CATC-001 §18 — Connection-Aware TIS) ----- #
    # Added additively for Connection-Aware Trust Computation. In Phase 1
    # these carry stub values populated by generate_certificate(); Phase 2
    # will wire them to the ResolvedTISProfile returned by the GCA
    # policy-resolution step (see TCS_SPEC.md §18).
    #
    # connection_type:              ct identifier (CT-1..CT-13, or "CT-0"
    #                               stub when ct is not yet resolved).
    # connection_type_modifier_id:  versioned CT modifier set ID.
    # resolved_policy_profile_id:   composite audit ID
    #                               (base_profile + modifier_id + timestamp).
    # chain_depth:                  number of hops in an agent chain
    #                               (only meaningful for CT-8; 0 otherwise).
    #                               CT-11 is NOT a chain context.
    # chain_u_scores:               per-hop K_i values used for the
    #                               CT-8 chain math (K_chain = Π(K_i),
    #                               U_chain = 1 - K_chain). Kept under
    #                               the legacy field name "chain_u_scores"
    #                               for archive compatibility; values are
    #                               K_i (positive calibration scores).
    #                               Empty list for non-CT-8 connections.
    connection_type: Optional[str] = None
    connection_type_modifier_id: Optional[str] = None
    resolved_policy_profile_id: Optional[str] = None
    chain_depth: int = 0
    chain_u_scores: List[float] = field(default_factory=list)

    # ---- Standards Composer audit (Slice 4) ---------------------------- #
    # When the active policy profile was produced by the Standards
    # Composer, this block carries the composer inputs verbatim so the
    # TC self-documents which standards governed the decision. None when
    # the profile is built-in (not composed). The fields are:
    #   industry, sub_industry, use_case
    #   standards                     list of standard ids (sorted)
    #   risk_tier, action_class
    #   composition_rules_version
    #   composed_at                   ISO-8601 timestamp of composition
    # The audit can reconstruct the full composition by looking up the
    # standards library, the composition rules version, and the pack
    # registry — but this block makes the standards trail visible on
    # the certificate itself without requiring a join.
    composer_metadata: Optional[Dict[str, Any]] = None

    # ---- Governance Risk Rule audit (Slice 4.5) ------------------------ #
    # Records every risk rule that matched the query during this
    # evaluation. Each entry carries:
    #   rule_id, rule_version, applies_to_domains, matched_domain,
    #   matched_term_groups (group_index + matched_term per group),
    #   effect (c3_violation, c3_category, blocking_reason,
    #           decision_pressure, requires_human_review, penalties,
    #           explanation),
    #   active_policy_profile_id
    # An empty list means the classifier ran but no rule matched. None
    # means the classifier did not run (legacy path predating Slice 4.5
    # or classifier failure). Rules are versioned so a future audit can
    # tell exactly which definition of clinical_medication_dosing_pregnancy
    # (or any other rule) was in effect when the TC was issued.
    governance_rule_matches: Optional[List[Dict[str, Any]]] = None

    # ---- Trust Enforcement Layer (TCS-TEL-001 — TCS_SPEC.md §19) ------- #
    # Four new TC layers required for Phase 1 completion. The dataclass
    # wrappers live above; here they are attached to the TrustCertificate
    # as optional fields so that existing construction sites continue to
    # work unchanged. ``generate_certificate()`` populates all four with
    # Phase-1 stub values; Phase 2 wires them to real identity providers,
    # governance monitors, persistent hash chains, and override workflows.
    identity_binding: Optional[IdentityBinding] = None
    governance_status: Optional[GovernanceStatus] = None
    audit_integrity: Optional[AuditIntegrity] = None
    override_record: Optional[OverrideRecord] = None

    # ---- Phase 3 Nine-Outcome Decision Fields (TCS-BUILD-003 Step 1) ---- #
    # Additive only — no schema breaks. All default to None/False so
    # existing TC construction sites are unchanged.
    qualified_decision: Optional[str] = None     # nine-outcome refined decision
    enhanced_logging: bool = False                # Allow_with_logging flag
    reason_code: Optional[str] = None
    proximity_to_threshold: Optional[float] = None
    redaction_applied: bool = False               # Allow_with_redaction
    redacted_fields: List[str] = field(default_factory=list)
    redaction_scope: Optional[str] = None
    step_up_required: bool = False                # Allow_with_step_up
    step_up_completed: Optional[bool] = None
    compensation_scope: Optional[str] = None      # Rollback
    incident_id: Optional[str] = None
    recovery_mode_activated: bool = False

    # ---- Serialization -------------------------------------------------- #

    def to_dict(self) -> Dict[str, Any]:
        """
        Return the TC as a JSON-serializable dict.

        Datetimes become ISO-8601 strings with a 'Z' suffix, floats are
        rounded to 4 decimal places, and nested collections are copied
        (not referenced) to prevent accidental mutation of the TC after
        issuance.
        """

        def _iso(dt: datetime) -> str:
            return dt.strftime("%Y-%m-%dT%H:%M:%SZ")

        def _round_floats(obj: Any) -> Any:
            if isinstance(obj, float):
                return _r(obj)
            if isinstance(obj, dict):
                return {k: _round_floats(v) for k, v in obj.items()}
            if isinstance(obj, list):
                return [_round_floats(v) for v in obj]
            return obj

        def _layer_to_dict(layer: Any) -> Optional[Dict[str, Any]]:
            """
            Serialize a TEL layer dataclass (IdentityBinding, GovernanceStatus,
            AuditIntegrity, OverrideRecord) to a plain dict, or return None.
            """
            if layer is None:
                return None
            return _round_floats({
                k: v for k, v in layer.__dict__.items()
            })

        return {
            # Identity
            "certificate_id": self.certificate_id,
            "subject_id": self.subject_id,
            "subject_type": self.subject_type,
            "domain": self.domain,
            "risk_tier": self.risk_tier,
            "action_class": self.action_class,
            "policy_severity": self.policy_severity,
            "checkpoint_id": self.checkpoint_id,
            "gca_context_id": self.gca_context_id,
            "policy_set_id": self.policy_set_id,

            # Score (white paper alignment — see TISResult docstring)
            "s_base":          _r(self.s_base),
            "s_adjusted":      _r(self.s_adjusted),
            "tis_raw":         _r(self.tis_raw),
            "tis_adjusted":    _r(self.tis_adjusted),
            "tis_current":     _r(self.tis_current),
            "component_scores":  _round_floats(dict(self.component_scores)),
            "component_weights": _round_floats(dict(self.component_weights)),
            "penalty_aggregate": _r(self.penalty_aggregate),
            "penalty_breakdown": _round_floats(dict(self.penalty_breakdown)),
            "failing_dimension_subfactors": _round_floats(
                dict(self.failing_dimension_subfactors)
            ),

            # Gate
            "gate_set": list(self.gate_set),
            "thresholds": _round_floats(dict(self.thresholds)),
            "gate_results": dict(self.gate_results),
            "gate_passed": bool(self.gate_passed),
            "blocking_reason": self.blocking_reason,
            "failure_mode": self.failure_mode,

            # Decision
            "decision": self.decision,
            "requires_human_review": bool(self.requires_human_review),
            "escalation_routed_to": list(self.escalation_routed_to),

            # Provenance
            "source_references": list(self.source_references),
            "retrieval_ids": list(self.retrieval_ids),
            "chain_of_custody_id": self.chain_of_custody_id,
            "audit_log_id": self.audit_log_id,
            "integration_boundary_gaps": int(self.integration_boundary_gaps),

            # Temporal
            "evaluation_timestamp": _iso(self.evaluation_timestamp),
            "valid_until": _iso(self.valid_until),
            "decay_rate": _r(self.decay_rate),
            "recompute_required": bool(self.recompute_required),
            "invalidation_triggers": list(self.invalidation_triggers),
            "last_invalidation_event": dict(self.last_invalidation_event),
            "invalidation_status": self.invalidation_status,

            # Explanation
            "explanation_summary": self.explanation_summary,
            "key_factors": list(self.key_factors),
            "key_concerns": list(self.key_concerns),
            "regulatory_explanation_level": self.regulatory_explanation_level,
            "regulatory_mapping": list(self.regulatory_mapping),

            # Lifecycle
            "lifecycle_state": self.lifecycle_state,
            "state_transition_history": [
                dict(entry) for entry in self.state_transition_history
            ],
            "recomputed_from_certificate_id": self.recomputed_from_certificate_id,
            "superseded_by_certificate_id": self.superseded_by_certificate_id,
            "archived": bool(self.archived),

            # MCP Extensions (TCS-MCP-001 §11)
            "mcp_server_id": self.mcp_server_id,
            "scope_attestation": _round_floats(dict(self.scope_attestation)),

            # CT Audit Fields (TCS-CATC-001 §18)
            "connection_type": self.connection_type,
            "connection_type_modifier_id": self.connection_type_modifier_id,
            "resolved_policy_profile_id": self.resolved_policy_profile_id,
            "chain_depth": int(self.chain_depth),
            "chain_u_scores": [_r(v) for v in self.chain_u_scores],

            # Standards Composer audit (Slice 4)
            "composer_metadata": (
                dict(self.composer_metadata) if self.composer_metadata else None
            ),

            # Governance Risk Rule audit (Slice 4.5).
            # None means the classifier did not run for this evaluation.
            # An empty list means it ran and no rule matched. A non-empty
            # list carries one audit dict per triggered rule (see
            # RuleMatch.to_audit_dict for shape).
            "governance_rule_matches": (
                [dict(m) for m in self.governance_rule_matches]
                if self.governance_rule_matches is not None
                else None
            ),

            # Trust Enforcement Layer (TCS-TEL-001 §19)
            "identity_binding":   _layer_to_dict(self.identity_binding),
            "governance_status":  _layer_to_dict(self.governance_status),
            "override_record":    _layer_to_dict(self.override_record),

            # Phase 3 Nine-Outcome Decision Fields (TCS-BUILD-003 Step 1)
            "qualified_decision":       self.qualified_decision,
            "enhanced_logging":         bool(self.enhanced_logging),
            "reason_code":              self.reason_code,
            "proximity_to_threshold":   _r(self.proximity_to_threshold) if self.proximity_to_threshold is not None else None,
            "redaction_applied":        bool(self.redaction_applied),
            "redacted_fields":          list(self.redacted_fields),
            "redaction_scope":          self.redaction_scope,
            "step_up_required":         bool(self.step_up_required),
            "step_up_completed":        self.step_up_completed,
            "compensation_scope":       self.compensation_scope,
            "incident_id":              self.incident_id,
            "recovery_mode_activated":  bool(self.recovery_mode_activated),

            # audit_integrity intentionally last — its tc_hash is computed
            # over every other serialized field (compute_tc_hash skips this
            # key), so its position in the dict is irrelevant to the hash.
            "audit_integrity":    _layer_to_dict(self.audit_integrity),
        }

    def to_json(self, indent: int = 2) -> str:
        """Return a pretty-printed JSON serialization of the TC."""
        return json.dumps(self.to_dict(), indent=indent, default=str)


# --------------------------------------------------------------------------- #
# Internal helpers                                                             #
# --------------------------------------------------------------------------- #

def _derive_lifecycle_state(decision: str, is_valid: int) -> str:
    """
    Initial lifecycle state for a newly issued TC.

    Invalidation wins over every other state: an invalidated TC is
    lifecycle_state = "invalidated" regardless of the decision it carries
    (TCS_SPEC.md §11, TEST_SCENARIOS.md scenario 7).
    """
    if is_valid == 0:
        return "invalidated"
    return DECISION_TO_LIFECYCLE[decision]


def _derive_invalidation_status(is_valid: int) -> str:
    return "invalidated" if is_valid == 0 else "valid"


def _derive_blocking_reason(
    tis_result: TISResult,
    decision: str,
    inp: TISInput,
) -> Optional[str]:
    """
    Build a machine-readable blocking_reason string.

    Priority order matches the decision function:
        1. Invalidation event       → "invalidation_{event}"
        2. C₃ = 0.00                → "C3_prohibited_pattern[_<ctx>]"
        3. Gate failure             → "{dim_lower}_gate_fail_{DIM}={score}_threshold={thr}"
        4. Allow/Observe            → None

    An optional ``blocking_context`` entry in ``TISInput.context_metadata``
    may be appended to the C3 prohibited-pattern reason for richer audit
    traces (see TEST_SCENARIOS.md scenario 1).
    """
    if decision in ("Allow", "Observe"):
        return None

    # 1. Invalidation.
    if tis_result.is_valid == 0 and tis_result.invalidation_event:
        return f"invalidation_{tis_result.invalidation_event}"

    # 2. C₃ hard stop.
    if tis_result.C3_score == 0.00:
        base = "C3_prohibited_pattern"
        ctx = inp.context_metadata.get("blocking_context")
        if isinstance(ctx, str) and ctx:
            return f"{base}_{ctx}"
        return base

    # 3. Governance-rule reason (Slice 5.5a). When a rule fired and
    # provided a blocking_reason (e.g. the typed-context lithium
    # rule's "patient_specific_medication_guidance_during_pregnancy"),
    # surface that as the TC's blocking_reason rather than the
    # less-specific gate-failure string. The rule reason names the
    # actual risk; the gate failure is downstream evidence. The gate
    # info still appears in failure_mode / failing_dimensions for
    # diagnostic use.
    rule_reason = inp.context_metadata.get("governance_rule_blocking_reason")
    if isinstance(rule_reason, str) and rule_reason:
        cat = (
            inp.context_metadata.get("governance_primary_safety_category")
            or (
                # Fallback to the typed-context blocking_context if no
                # primary_safety_category was merged (typed-context
                # deterministic_bounded rules don't propagate it).
                (
                    inp.context_metadata.get("blocking_context", "").split(":")[0]
                    if ":" in str(inp.context_metadata.get("blocking_context", ""))
                    else None
                )
            )
        )
        return f"{cat}:{rule_reason}" if cat else rule_reason

    # 4. Gate failure. Use the first failing dimension to build the reason.
    if tis_result.failing_dimensions:
        dim = tis_result.failing_dimensions[0]
        score = inp.dimension_scores[dim]
        threshold = inp.policy_profile.thresholds[dim]
        dim_name = {
            "B": "boundedness",
            "A": "attribution",
            "C": "compliance",
            "K": "known",
        }[dim]
        return (
            f"{dim_name}_gate_fail_"
            f"{dim}={_r(score)}_threshold={_r(threshold)}"
        )

    return None


def _derive_failure_mode(
    tis_result: TISResult,
    decision: str,
) -> Optional[str]:
    """Short categorical label used for dashboards and alerting."""
    if decision in ("Allow", "Observe"):
        return None
    if tis_result.is_valid == 0:
        return "invalidated"
    if tis_result.C3_score == 0.00:
        return "C3_prohibited_pattern"
    if tis_result.failing_dimensions:
        dim = tis_result.failing_dimensions[0]
        return f"{dim}_gate_fail"
    return None


def _derive_last_invalidation_event(
    tis_result: TISResult,
    evaluation_time: datetime,
) -> Dict[str, Any]:
    """
    Populate the ``last_invalidation_event`` block.

    When no invalidation has occurred, all fields are null (per
    TC_SCHEMA.md Layer T).
    """
    if tis_result.invalidation_event:
        return {
            "type": tis_result.invalidation_event,
            "timestamp": evaluation_time.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "details": None,
        }
    return {"type": None, "timestamp": None, "details": None}


def _derive_failing_subfactors(
    tis_result: TISResult,
    inp: TISInput,
) -> Dict[str, Dict[str, float]]:
    """
    Return sub-factor detail for any failing dimensions.

    Currently only surfaces C₃ when the C gate has failed on a
    prohibited-pattern condition (the only sub-factor exposed in Phase 1).
    """
    out: Dict[str, Dict[str, float]] = {}
    if not tis_result.failing_dimensions:
        return out

    for dim in tis_result.failing_dimensions:
        if dim == "C":
            out["C"] = {"C3": _r(tis_result.C3_score)}
        elif dim in inp.sub_factor_scores:
            out[dim] = {
                k: _r(v) for k, v in inp.sub_factor_scores[dim].items()
            }
    return out


def _derive_escalation_routing(
    decision: str,
    domain: str,
) -> List[str]:
    """Escalation routing is populated only for Escalate decisions."""
    if decision != "Escalate":
        return []
    return list(_DEFAULT_ESCALATION_ROUTING.get(domain, ["reviewer"]))


def _generate_explanation(
    inp: TISInput,
    tis_result: TISResult,
    decision: str,
    profile: PolicyProfile,
) -> tuple[str, List[str], List[str]]:
    """
    Build the human-readable explanation triple for Layer E.

    The summary must be legible without source-code access and must name,
    per TC_SCHEMA.md:

        1. domain and action type
        2. which gates were evaluated
        3. which gates passed or failed and why
        4. the enforcement decision
        5. regulatory significance if applicable
    """
    # Assemble per-dimension gate line ("B=0.92 PASS, A=0.88 PASS, ...").
    gate_lines: List[str] = []
    for dim in ("B", "A", "C", "K"):
        score = inp.dimension_scores[dim]
        status = tis_result.gate_results_by_dim[dim]
        if status == "pass":
            gate_lines.append(f"{dim}={_r(score)} PASS")
        elif status == "fail":
            threshold = profile.thresholds[dim]
            gate_lines.append(
                f"{dim}={_r(score)} FAIL (< {_r(threshold)})"
            )
        else:
            gate_lines.append(f"{dim}={_r(score)} not_gated")

    gates_str = ", ".join(gate_lines)
    gate_set_str = "{" + ",".join(sorted(profile.gate_set)) + "}"

    # Decision-specific narrative fragment.
    if decision == "Stop" and tis_result.is_valid == 0:
        decision_fragment = (
            f"Invalidation event '{tis_result.invalidation_event}' fired "
            f"at Priority 1. TIS_current forced to 0.0000 and decision set "
            f"to Stop regardless of dimensional scores."
        )
    elif decision == "Stop" and tis_result.C3_score == 0.00:
        decision_fragment = (
            "C3 prohibited-pattern sub-factor = 0.00 -> hard Stop. "
            "Soft-hold ceiling kappa does not apply."
        )
    elif decision == "Stop":
        decision_fragment = (
            f"Gate collapsed (G=0) and S_base={_r(tis_result.s_base)} "
            f"is below remediability floor kappa={_r(profile.soft_hold_ceiling)} "
            f"-> Stop (too degraded to remediate)."
        )
    elif decision == "Hold":
        decision_fragment = (
            f"Gate collapsed (G=0) but S_base={_r(tis_result.s_base)} "
            f"remains at or above remediability floor kappa="
            f"{_r(profile.soft_hold_ceiling)} -> Hold (remediable through review)."
        )
    elif decision == "Escalate":
        decision_fragment = (
            f"TIS_current={_r(tis_result.tis_current)} is below the "
            f"escalate threshold theta_escalate={_r(profile.theta_escalate)} -> "
            f"Escalate."
        )
    elif decision == "Observe":
        decision_fragment = (
            f"TIS_current={_r(tis_result.tis_current)} is below theta_allow="
            f"{_r(profile.theta_allow)} but above theta_hold="
            f"{_r(profile.theta_hold)} at r1 -> Observe."
        )
    else:  # Allow
        decision_fragment = (
            f"All gates in {gate_set_str} passed. "
            f"TIS_current={_r(tis_result.tis_current)} >= theta_allow="
            f"{_r(profile.theta_allow)} -> Allow."
        )

    reg_fragment = ""
    if profile.regulatory_mapping:
        reg_fragment = (
            " Regulatory scope: "
            + "; ".join(profile.regulatory_mapping[:3])
            + ("; ..." if len(profile.regulatory_mapping) > 3 else "")
            + "."
        )

    # Phase 5 Slice 5.5 — make the audit explicit when the subject is a
    # human-composed draft (no LLM in the loop). Reviewers reading the
    # TC should be able to tell at a glance that this evaluation
    # governed a human-authored outbound message before delivery, not
    # an LLM completion.
    if inp.subject_type == "human_composed":
        subject_clause = (
            f"Human-composed draft message '{inp.subject_id}' (no LLM "
            f"in the loop) evaluated against"
        )
    else:
        subject_clause = (
            f"Subject '{inp.subject_id}' ({inp.subject_type}) evaluated against"
        )

    summary = (
        f"{subject_clause} "
        f"policy '{profile.profile_id}' at {profile.risk_tier}/{profile.action_class} "
        f"in domain '{profile.domain}'. "
        f"Gate set {gate_set_str} evaluated: {gates_str}. "
        f"{decision_fragment}"
        f"{reg_fragment}"
    )

    # key_factors: positive contributors. key_concerns: what reduced score.
    key_factors: List[str] = []
    key_concerns: List[str] = []

    for dim in ("B", "A", "C", "K"):
        status = tis_result.gate_results_by_dim[dim]
        score = inp.dimension_scores[dim]
        label = _DIM_LABELS[dim]
        if status == "pass":
            key_factors.append(f"{label} ({dim}) passed at {_r(score)}")
        elif status == "fail":
            key_concerns.append(
                f"{label} ({dim}) failed at {_r(score)} "
                f"(threshold {_r(profile.thresholds[dim])})"
            )

    if tis_result.C3_score == 0.00:
        key_concerns.append(
            "C3 prohibited-pattern sub-factor = 0.00 (hard stop condition)"
        )

    if tis_result.penalty_aggregate > 0:
        key_concerns.append(
            f"Aggregate penalty P = {_r(tis_result.penalty_aggregate)}"
        )

    if tis_result.is_valid == 0:
        key_concerns.append(
            f"Invalidation event: {tis_result.invalidation_event}"
        )

    if not key_factors:
        key_factors.append("No dimension passed its gate")
    if not key_concerns:
        key_concerns.append("No blocking concerns")

    return summary, key_factors, key_concerns


def _stub_id(prefix: str) -> str:
    """Short stub ID for Phase 1 placeholder provenance references."""
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


# --------------------------------------------------------------------------- #
# Public entry point                                                           #
# --------------------------------------------------------------------------- #

def generate_certificate(
    tis_input: TISInput,
    tis_result: TISResult,
    decision: str,
    requires_human_review: bool,
) -> TrustCertificate:
    """
    Assemble a complete Trust Certificate from a TIS computation.

    The decision and ``requires_human_review`` flag come from
    ``tcs.decision_engine.map_decision`` — this function does NOT compute
    them. It only packages the already-made decision into the TC artifact.

    Every required field from ``TC_SCHEMA.md`` is populated. Provenance IDs
    (checkpoint_id, gca_context_id, chain_of_custody_id, audit_log_id) are
    Phase-1 stubs generated from uuid4; Phase 2 will wire them to real
    upstream identifiers when the GCA data plane is connected.
    """
    profile = tis_input.policy_profile
    is_valid_effective = tis_result.is_valid

    lifecycle_state = _derive_lifecycle_state(decision, is_valid_effective)
    invalidation_status = _derive_invalidation_status(is_valid_effective)
    blocking_reason = _derive_blocking_reason(tis_result, decision, tis_input)
    failure_mode = _derive_failure_mode(tis_result, decision)
    escalation_routed_to = _derive_escalation_routing(decision, profile.domain)
    last_invalidation_event = _derive_last_invalidation_event(
        tis_result, tis_input.evaluation_time
    )
    failing_subfactors = _derive_failing_subfactors(tis_result, tis_input)

    explanation_summary, key_factors, key_concerns = _generate_explanation(
        tis_input, tis_result, decision, profile
    )

    # Initial state transition: every TC begins life in "computed" and then
    # settles into its assigned initial state (per TC_SCHEMA.md Layer L).
    initial_transition = {
        "from": "computed",
        "to": lifecycle_state,
        "timestamp": tis_input.evaluation_time.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "reason": f"Initial evaluation -- decision: {decision}",
    }

    # Provenance references. In Phase 1 these are generated stubs; context
    # metadata may override any of them if the scenario provides real IDs.
    meta = tis_input.context_metadata
    source_references = list(meta.get("source_references", []))
    retrieval_ids = list(meta.get("retrieval_ids", []))
    checkpoint_id = str(meta.get("checkpoint_id") or _stub_id("ckpt"))
    gca_context_id = str(meta.get("gca_context_id") or _stub_id("gca"))
    chain_of_custody_id = str(
        meta.get("chain_of_custody_id") or _stub_id("coc")
    )
    audit_log_id = str(meta.get("audit_log_id") or _stub_id("audit"))

    # recompute_required: True for r3 per TC_SCHEMA.md Layer T.
    recompute_required = (profile.risk_tier == "r3")

    # ---- MCP Extensions (TCS-MCP-001 §11) ------------------------------ #
    # Phase-1 stub values. The scope_attestation block is structurally
    # complete but carries placeholder content: no MCP servers enumerated,
    # no downstream agents declared, enforcement_perimeter_complete=True
    # on the assumption that a Phase-1 scenario has no out-of-scope
    # surfaces by construction. Phase 2 populates these from the actual
    # deployment manifest when assemble_context() becomes MCP-backed.
    #
    # Scenario metadata may override any of these by providing the same
    # keys in context_metadata — this lets Phase 2 scenarios (9/10/11)
    # exercise the bypass rules without touching generate_certificate().
    mcp_server_id = str(meta.get("mcp_server_id") or _stub_id("mcp"))

    # context_expanded flows from the invalidation_event if it's "context_expansion";
    # everything else is a stub default.
    context_expanded = bool(
        meta.get("context_expanded_after_evaluation")
        or tis_result.invalidation_event == "context_expansion"
    )
    scope_attestation: Dict[str, Any] = {
        "mcp_servers_in_scope": list(meta.get("mcp_servers_in_scope", [mcp_server_id])),
        "mcp_servers_out_of_scope": list(meta.get("mcp_servers_out_of_scope", [])),
        "downstream_agents_in_scope": list(meta.get("downstream_agents_in_scope", [])),
        "downstream_agents_out_of_scope": list(meta.get("downstream_agents_out_of_scope", [])),
        "context_frozen_at": tis_input.evaluation_time.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "context_expanded_after_evaluation": context_expanded,
        "context_expansion_events": list(meta.get("context_expansion_events", [])),
        "enforcement_perimeter_complete": bool(
            meta.get("enforcement_perimeter_complete", True)
        ),
        "attestation_basis": str(
            meta.get("attestation_basis", "deployment-manifest-stub-v1")
        ),
        "upstream_tc_references": list(meta.get("upstream_tc_references", [])),
    }

    # ---- CT Audit Fields (TCS-CATC-001 §18) ---------------------------- #
    # Phase-1 stubs. In Phase 2 these will be populated from the
    # ResolvedTISProfile returned by governed_context.resolve_policy_profile.
    # Phase-1 convention: connection_type defaults to "CT-0" (unresolved),
    # chain_depth = 0, chain_u_scores = [] — all signalling "CT resolution
    # has not yet run". Scenario metadata may override any of these keys,
    # which lets Phase 2 scenarios 12/13/14 exercise connection-aware
    # scoring without touching generate_certificate() further.
    connection_type = str(meta.get("connection_type") or "CT-0")
    connection_type_modifier_id = str(
        meta.get("connection_type_modifier_id") or "ct-modifier-stub-v0"
    )
    resolved_policy_profile_id = str(
        meta.get("resolved_policy_profile_id")
        or f"{profile.profile_id}::{connection_type}::stub"
    )
    chain_depth = int(meta.get("chain_depth", 0))
    chain_u_scores = [float(v) for v in meta.get("chain_u_scores", [])]

    # ---- Standards Composer audit (Slice 4) ---------------------------- #
    # If the active policy profile was produced by the Standards
    # Composer, the route/GCA will have stashed its composer_metadata
    # in context_metadata. Pass it through to the TC verbatim so the
    # audit trail is self-contained.
    cm_raw = meta.get("composer_metadata")
    composer_metadata: Optional[Dict[str, Any]] = (
        dict(cm_raw) if isinstance(cm_raw, dict) else None
    )

    # ---- Governance Risk Rule audit (Slice 4.5) ------------------------ #
    # The GCA stashes one audit dict per triggered rule in
    # context_metadata["governance_rule_matches"] (see
    # governed_context._apply_query_risk_classification). The shape comes
    # from RuleMatch.to_audit_dict() and already includes rule_version,
    # matched_domain, matched_term_groups, effect (with c3_category), and
    # active_policy_profile_id. We pass it through verbatim so the TC
    # self-documents which deterministic rules fired and which version
    # of each rule was in effect.
    rule_matches_raw = meta.get("governance_rule_matches")
    if rule_matches_raw is None:
        governance_rule_matches: Optional[List[Dict[str, Any]]] = None
    elif isinstance(rule_matches_raw, list):
        governance_rule_matches = [
            dict(m) for m in rule_matches_raw if isinstance(m, dict)
        ]
    else:
        governance_rule_matches = None

    # ---- Trust Enforcement Layer (TCS-TEL-001 §19) --------------------- #
    # Phase-1 stubs for the four new layers. The stubs are "optimistic":
    # identity is authenticated at high confidence, governance is
    # complete, no override is invoked. These are the values expected by
    # the Phase-1 scenarios. Phase 2 scenarios 15/16/17 override them via
    # context_metadata to exercise degraded and override workflows.
    #
    # Layer Id — IdentityBinding
    identity_binding = IdentityBinding(
        requesting_identity=str(
            meta.get("requesting_identity") or _stub_id("id")
        ),
        identity_type=str(meta.get("identity_type") or "human"),
        role=str(meta.get("role") or "evaluation_requester"),
        authorization_tier=str(meta.get("authorization_tier") or "T3"),
        identity_confidence=float(meta.get("identity_confidence", 1.0)),
        identity_verified=bool(meta.get("identity_verified", True)),
        authentication_method=str(
            meta.get("authentication_method") or "oauth2_mfa"
        ),
        requesting_session_id=str(
            meta.get("requesting_session_id") or _stub_id("sess")
        ),
    )

    # Layer GS — GovernanceStatus
    governance_status_obj = GovernanceStatus(
        governance_status=str(meta.get("governance_status") or "complete"),
        evaluation_completeness_score=float(
            meta.get("evaluation_completeness_score", 1.0)
        ),
        components_evaluated=list(
            meta.get(
                "components_evaluated",
                [
                    "context_assembly",
                    "dimension_scoring",
                    "penalty_computation",
                    "gate_evaluation",
                    "decay_application",
                    "invalidation_check",
                    "decision_mapping",
                    "certificate_generation",
                ],
            )
        ),
        components_skipped=list(meta.get("components_skipped", [])),
        skip_reasons=dict(meta.get("skip_reasons", {})),
        fail_safe_applied=bool(meta.get("fail_safe_applied", False)),
        fail_safe_type=meta.get("fail_safe_type"),  # None by default
        governance_integrity_score=float(
            meta.get("governance_integrity_score", 1.0)
        ),
    )

    # Layer Ov — OverrideRecord
    # Phase 1 scenarios do not invoke overrides — every field stays at
    # its null/False default. Phase 2 scenario 16 populates this block.
    override_record = OverrideRecord(
        override_invoked=bool(meta.get("override_invoked", False)),
        original_decision=meta.get("original_decision"),
        override_decision=meta.get("override_decision"),
        override_actor=meta.get("override_actor"),
        override_actor_role=meta.get("override_actor_role"),
        override_reason=meta.get("override_reason"),
        override_type=meta.get("override_type"),
        policy_exception_id=meta.get("policy_exception_id"),
        regulatory_basis=meta.get("regulatory_basis"),
        co_authorizer=meta.get("co_authorizer"),
        post_override_review_required=bool(
            meta.get("post_override_review_required", False)
        ),
        post_override_review_deadline=meta.get("post_override_review_deadline"),
        post_override_review_completed=bool(
            meta.get("post_override_review_completed", False)
        ),
        override_creates_tc_amendment=bool(
            meta.get("override_creates_tc_amendment", False)
        ),
    )

    tc = TrustCertificate(
        # Identity
        certificate_id=str(uuid.uuid4()),
        subject_id=tis_input.subject_id,
        subject_type=tis_input.subject_type,
        domain=profile.domain,
        risk_tier=profile.risk_tier,
        action_class=profile.action_class,
        policy_severity="standard",
        checkpoint_id=checkpoint_id,
        gca_context_id=gca_context_id,
        policy_set_id=profile.profile_id,

        # Score
        s_base=tis_result.s_base,
        s_adjusted=tis_result.s_adj,
        tis_raw=tis_result.tis_raw,
        tis_adjusted=tis_result.tis_adj,
        tis_current=tis_result.tis_current,
        component_scores=dict(tis_input.dimension_scores),
        component_weights=dict(profile.weights),
        penalty_aggregate=tis_result.penalty_aggregate,
        penalty_breakdown=dict(tis_result.penalty_breakdown),
        failing_dimension_subfactors=failing_subfactors,

        # Gate
        gate_set=sorted(profile.gate_set),
        thresholds=dict(profile.thresholds),
        gate_results=dict(tis_result.gate_results_by_dim),
        gate_passed=(tis_result.gate_result == 1),
        blocking_reason=blocking_reason,
        failure_mode=failure_mode,

        # Decision
        decision=decision,
        requires_human_review=requires_human_review,
        escalation_routed_to=escalation_routed_to,

        # Provenance
        source_references=source_references,
        retrieval_ids=retrieval_ids,
        chain_of_custody_id=chain_of_custody_id,
        audit_log_id=audit_log_id,
        integration_boundary_gaps=int(meta.get("n_gaps", 0)),

        # Temporal
        evaluation_timestamp=tis_input.evaluation_time,
        valid_until=tis_result.valid_until,
        decay_rate=profile.decay_rate,
        recompute_required=recompute_required,
        invalidation_triggers=list(profile.invalidation_triggers),
        last_invalidation_event=last_invalidation_event,
        invalidation_status=invalidation_status,

        # Explanation
        explanation_summary=explanation_summary,
        key_factors=key_factors,
        key_concerns=key_concerns,
        regulatory_explanation_level="regulatory",
        regulatory_mapping=list(profile.regulatory_mapping),

        # Lifecycle
        lifecycle_state=lifecycle_state,
        state_transition_history=[initial_transition],
        recomputed_from_certificate_id=None,
        superseded_by_certificate_id=None,
        archived=False,

        # MCP Extensions (TCS-MCP-001 §11 — downstream bypass rules)
        mcp_server_id=mcp_server_id,
        scope_attestation=scope_attestation,

        # CT Audit Fields (TCS-CATC-001 §18 — Connection-Aware TIS)
        connection_type=connection_type,
        connection_type_modifier_id=connection_type_modifier_id,
        resolved_policy_profile_id=resolved_policy_profile_id,
        chain_depth=chain_depth,
        chain_u_scores=chain_u_scores,

        # Standards Composer audit (Slice 4)
        composer_metadata=composer_metadata,

        # Governance Risk Rule audit (Slice 4.5)
        governance_rule_matches=governance_rule_matches,

        # Trust Enforcement Layer (TCS-TEL-001 §19)
        # audit_integrity is attached after construction so that its
        # tc_hash can be computed over the serialized TC content. See
        # the block immediately below.
        identity_binding=identity_binding,
        governance_status=governance_status_obj,
        override_record=override_record,
        audit_integrity=None,
    )

    # ---- AuditIntegrity: compute hash and attach after construction ---- #
    # compute_tc_hash() deliberately excludes the "audit_integrity" key,
    # so we can safely serialize the TC with audit_integrity=None, take
    # the hash, and then write the layer back onto the TC. This keeps
    # the hash reproducible on re-serialization: any caller who runs
    # compute_tc_hash(tc.to_dict()) later will get the same value.
    tc_hash = compute_tc_hash(tc.to_dict())
    tc.audit_integrity = AuditIntegrity(
        tc_hash=tc_hash,
        previous_tc_hash=meta.get("previous_tc_hash"),  # None in Phase 1
        chain_sequence=int(meta.get("chain_sequence", 1)),
        chain_id=str(meta.get("chain_id") or _stub_id("chain")),
        hash_algorithm="sha256",
        integrity_verified=True,
        issued_by=str(meta.get("issued_by") or "tcs-reference-impl-v0.1"),
    )
    return tc
