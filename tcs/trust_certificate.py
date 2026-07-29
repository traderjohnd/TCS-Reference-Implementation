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
from tcs.tis_engine import (
    TISInput,
    TISResult,
    # tis-v2 Commit 4 — Decimal penalty maxima for pre-seal validation.
    DELTA_D_MAX_DECIMAL,
    DELTA_H_MAX_DECIMAL,
    P_CB_MAX_V2,
    W_NOVELTY_BY_TIER_DECIMAL,
    _W_PS_DEFAULT_DECIMAL,
    _W_PS_SPECIAL_DECIMAL,
)

# tis-v2 Commit 3 — schema-version dispatch exceptions live in the
# canonical numerical module (added in Commit 1).
from tcs.canonical import (
    CertificateInvariantError,
    UnsupportedCertificateSchemaVersion,
)

# tis-v2 Commit 4 — the v2 certificate core. Decimal-native fields,
# validating serializers, versioned hash payload, typed C3 provenance.
import re as _re
from dataclasses import replace as _dataclass_replace
from decimal import Decimal, localcontext

from tcs.canonical import (
    AdjustmentApplied,
    CALCULATION_VERSION_V2,
    DECAY_ALGORITHM_VERSION,
    SCORE_PRECISION_POLICY,
    SCORE_QUANTUM,
    SCORE_ROUNDING,
    TIS_DECIMAL_CONTEXT,
    UnsupportedCalculationVersion,
    canonical_nonnegative_parameter,
    canonical_score,
    require_canonical_parameter,
    require_canonical_score,
    serialize_canonical_score,
    serialize_raw_decimal,
)
from tcs.provenance import (
    ACTIVE_CREDENTIAL_PATTERN_SET_VERSION,
    ACTIVE_INJECTION_PATTERN_SET_VERSION,
    C3_PROVENANCE_SCHEMA_VERSION,
    C3ProvenanceRecord,
    CREDENTIAL_PATTERN_IDS_BY_VERSION,
    GOVERNANCE_RULE_MATCH_SCHEMA_VERSION,
    GovernanceRuleMatch,
    INJECTION_PATTERN_IDS_BY_VERSION,
    MatchedTermGroup,
    RuleMatchRef,
    c3_provenance_record_from_dict,
    c3_record_sort_key,
    governance_rule_match_from_dict,
    rule_match_sort_key,
    serialize_c3_provenance_record,
    serialize_governance_rule_match,
    validate_c3_provenance_record,
    validate_governance_rule_match,
)


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
# Hash chain helpers — frozen v1 contract + schema-version dispatch            #
# (tis-v2 Commit 3)                                                            #
# --------------------------------------------------------------------------- #
#
# Two DISTINCT version-1 behaviors, deliberately not conflated:
#
#   Raw stored-v1 verification
#       A stored legacy record is hash-verified from its original
#       persisted dictionary BEFORE rehydration or conversion, via
#       ``build_legacy_raw_hash_payload``. Absence of
#       ``certificate_schema_version`` IS the historical wire contract;
#       a raw dictionary carrying the key is rejected as not matching
#       the historical v1 wire shape, and any other injected field
#       changes the payload and fails verification. Nothing is silently
#       ignored, and no second accepted stored-v1 wire representation
#       exists.
#
#   Post-rehydration v1 reconstruction
#       A rehydrated / in-memory v1 certificate is serialized and hashed
#       via ``build_v1_hash_payload``, which projects the dict onto the
#       FROZEN historical field set below. Explicit integer
#       ``certificate_schema_version = 1`` is permitted on internal
#       representations as dispatch metadata and is excluded from the
#       projection — it never becomes a newly hashed historical field.
#       Commit 4 may add model and serialization fields; the projection
#       guarantees they cannot leak into the historical v1 payload, so
#       neither the allowlist nor its tests ever need amendment.

#: The frozen historical v1 top-level field set. Captured from the
#: 249 stored certificates across data/tcs.db and the 2026-06-25
#: archive — every stored record carries EXACTLY these 75 keys plus
#: ``audit_integrity`` (which the hash excludes). This is a permanent
#: historical contract: it is written as a literal, never computed from
#: the live dataclass, and MUST NOT change when Commit 4+ adds fields.
V1_REQUIRED_HASH_FIELDS = frozenset({
    "action_class", "archived", "audit_log_id", "blocking_reason",
    "certificate_id", "chain_depth", "chain_of_custody_id",
    "chain_u_scores", "checkpoint_id", "compensation_scope",
    "component_scores", "component_weights", "composer_metadata",
    "connection_type", "connection_type_modifier_id", "decay_rate",
    "decision", "domain", "enhanced_logging", "escalation_routed_to",
    "evaluation_timestamp", "explanation_summary",
    "failing_dimension_subfactors", "failure_mode", "gate_passed",
    "gate_results", "gate_set", "gca_context_id",
    "governance_rule_matches", "governance_status", "identity_binding",
    "incident_id", "integration_boundary_gaps", "invalidation_status",
    "invalidation_triggers", "key_concerns", "key_factors",
    "last_invalidation_event", "lifecycle_state", "mcp_server_id",
    "override_record", "penalty_aggregate", "penalty_breakdown",
    "policy_set_id", "policy_severity", "proximity_to_threshold",
    "qualified_decision", "reason_code", "recompute_required",
    "recomputed_from_certificate_id", "recovery_mode_activated",
    "redacted_fields", "redaction_applied", "redaction_scope",
    "regulatory_explanation_level", "regulatory_mapping",
    "requires_human_review", "resolved_policy_profile_id",
    "retrieval_ids", "risk_tier", "s_adjusted", "s_base",
    "scope_attestation", "source_references",
    "state_transition_history", "step_up_completed",
    "step_up_required", "subject_id", "subject_type",
    "superseded_by_certificate_id", "thresholds", "tis_adjusted",
    "tis_current", "tis_raw", "valid_until",
})

#: Historically, no stored v1 record omits any top-level key — the
#: optional set is empty and stays empty permanently. It exists as a
#: named contract slot so the required/optional split is explicit.
V1_OPTIONAL_HASH_FIELDS = frozenset()

V1_HASH_FIELD_SET = V1_REQUIRED_HASH_FIELDS | V1_OPTIONAL_HASH_FIELDS


def _canonical_json_bytes(content: Dict[str, Any]) -> bytes:
    """The historical canonical JSON encoding — frozen.

    ``sort_keys=True`` so key order does not affect the hash;
    ``separators=(",", ":")`` to eliminate whitespace variance;
    UTF-8 encoding. Non-JSON-serializable values raise TypeError,
    which is correct — such content would fail round-trip anyway.
    """
    return json.dumps(
        content, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def build_legacy_raw_hash_payload(raw_dict: Dict[str, Any]) -> bytes:
    """Raw stored-v1 verification payload — FROZEN historical behavior.

    Input is the dictionary parsed directly from persisted
    ``content_json``, before any rehydration or conversion. Removes only
    ``audit_integrity`` and hashes everything else exactly as stored —
    original keys, values, representations, and omission state. A field
    injected into raw persisted content changes the payload and fails
    verification; it is never silently ignored.

    A raw dictionary carrying ``certificate_schema_version`` does not
    match the historical v1 wire shape (no stored legacy record has the
    key) and is rejected. Explicit version 1 is an internal dispatch
    classification only — see ``build_v1_hash_payload``.
    """
    if "certificate_schema_version" in raw_dict:
        raise CertificateInvariantError(
            "raw legacy v1 content must not carry "
            "certificate_schema_version; absence is the historical "
            "wire contract"
        )
    content = {k: v for k, v in raw_dict.items() if k != "audit_integrity"}
    return _canonical_json_bytes(content)


def compute_legacy_raw_tc_hash(raw_dict: Dict[str, Any]) -> str:
    """SHA-256 hex digest of the raw stored-v1 payload."""
    return hashlib.sha256(build_legacy_raw_hash_payload(raw_dict)).hexdigest()


def build_v1_hash_payload(serialized: Dict[str, Any]) -> bytes:
    """Post-rehydration v1 reconstruction payload — FROZEN projection.

    Projects a serialized model dict onto ``V1_HASH_FIELD_SET``,
    preserving presence/absence (a key absent from the input stays
    absent — no defaults are injected). ``audit_integrity``,
    ``certificate_schema_version``, and every v2-only field added by
    Commit 4 and later are excluded by construction, without depending
    on any ``to_dict()`` caller remembering to omit them.
    """
    content = {
        k: v for k, v in serialized.items() if k in V1_HASH_FIELD_SET
    }
    return _canonical_json_bytes(content)


def classify_certificate_schema_version(tc_dict: Dict[str, Any]) -> int:
    """Classify a serialized certificate's schema version.

    Absence of ``certificate_schema_version`` means v1 — NEVER a model
    default. When present, the value must be exactly the integer 1 or 2:
    bool is rejected (it is an int subclass), and no ``int(...)``
    coercion is applied. Anything else fails closed.
    """
    version = tc_dict.get("certificate_schema_version", 1)
    if isinstance(version, bool) or not isinstance(version, int):
        raise UnsupportedCertificateSchemaVersion(
            f"certificate_schema_version must be int 1 or 2, "
            f"got {version!r}"
        )
    if version not in (1, 2):
        raise UnsupportedCertificateSchemaVersion(version)
    return version


def build_hash_payload(tc_dict: Dict[str, Any]) -> bytes:
    """Version-dispatched hash payload for serialized model content.

    v1 (absent key, or explicit internal 1) routes to the frozen v1
    reconstruction projection. v2 routes to the validating-and-replaying
    v2 payload builder (Commit 4) — a v2 certificate that does not
    reproduce its own computation cannot acquire a hash at all.
    """
    version = classify_certificate_schema_version(tc_dict)
    if version == 1:
        return build_v1_hash_payload(tc_dict)
    return build_v2_hash_payload(tc_dict)


def compute_raw_stored_tc_hash(raw_dict: Dict[str, Any]) -> str:
    """Version-dispatched hash of RAW persisted content (pre-rehydration).

    Absence of ``certificate_schema_version`` → the frozen legacy raw
    path. Explicit 2 → the v2 payload builder, whose exact-schema
    validation makes any injected field a hard failure. Explicit 1 (or
    anything else) in RAW stored content does not match either wire
    contract and is rejected (Commit 3 rule: version 1 is an internal
    dispatch classification, never a stored wire representation).
    """
    if "certificate_schema_version" not in raw_dict:
        return compute_legacy_raw_tc_hash(raw_dict)
    version = raw_dict["certificate_schema_version"]
    if isinstance(version, bool) or version != 2:
        raise CertificateInvariantError(
            f"raw stored content carries unsupported "
            f"certificate_schema_version {version!r}"
        )
    return hashlib.sha256(build_v2_hash_payload(raw_dict)).hexdigest()


def compute_tc_hash(tc_dict: Dict[str, Any]) -> str:
    """
    Compute the SHA-256 hash of a TC's serialized content.

    The payload is version-dispatched via ``build_hash_payload``: for
    every v1 certificate this is byte-identical to the historical
    algorithm (drop ``audit_integrity``, canonical JSON, SHA-256) —
    proven by the stored-legacy fixture tests — and a v2-marked dict
    fails closed until Commit 4 lands the v2 payload builder.

    The hash excludes ``audit_integrity`` because that layer would
    otherwise have to reference its own hash, and excluding it lets the
    hash carry forward unchanged through chain bookkeeping.

    NOTE: verification of RAW PERSISTED legacy content must use
    ``compute_legacy_raw_tc_hash`` (before rehydration), not this
    function — the reconstruction projection would silently drop a
    field injected into stored content, whereas the raw path detects it.
    """
    return hashlib.sha256(build_hash_payload(tc_dict)).hexdigest()


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
# v2 serialization helpers (tis-v2 Commit 4)                                   #
# --------------------------------------------------------------------------- #

def _iso_z(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _round_floats_v2(obj: Any) -> Any:
    """Same float-normalization the v1 serializer applies to free-form
    nested audit blocks (scope_attestation, TEL layers)."""
    if isinstance(obj, float):
        return _r(obj)
    if isinstance(obj, dict):
        return {k: _round_floats_v2(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_round_floats_v2(v) for v in obj]
    return obj


def _tel_layer_to_dict(layer: Any) -> Optional[Dict[str, Any]]:
    if layer is None:
        return None
    return _round_floats_v2({k: v for k, v in layer.__dict__.items()})


def _serialize_parameter_4dp(value: Decimal, name: str) -> str:
    """Validating 4dp serializer for the non-negative parameter domain."""
    require_canonical_parameter(value, name)
    return format(value, ".4f")


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

    # ---- tis-v2 additions (Commit 4) — defaulted, appended -------------- #
    #
    # Model defaults describe LEGACY (v1) certificates: every v2
    # identifier below is assigned explicitly by generate_certificate_v2
    # and validated at sealing — never populated by these defaults.
    # On a v2 instance the pre-existing numeric fields above hold
    # canonical Decimal values; on v1 they hold floats exactly as before
    # (same version-discriminated-content pattern as TISResult).
    # ``governance_rule_matches`` holds legacy audit dicts on v1 and
    # List[GovernanceRuleMatch] on v2.
    component_scores_raw: Dict[str, Decimal] = field(default_factory=dict)
    component_scores_observed: Dict[str, Decimal] = field(default_factory=dict)
    adjustments_applied: List[AdjustmentApplied] = field(default_factory=list)
    c3_provenance: List[C3ProvenanceRecord] = field(default_factory=list)
    gate_result: Optional[int] = None            # v2 authoritative gate aggregate
    resolved_penalty_weights: Dict[str, Decimal] = field(default_factory=dict)
    resolved_decay_rate: Optional[Decimal] = None
    elapsed_hours: Optional[Decimal] = None
    decay_factor: Optional[Decimal] = None
    resolved_theta_allow: Optional[Decimal] = None
    resolved_theta_hold: Optional[Decimal] = None
    resolved_theta_escalate: Optional[Decimal] = None
    resolved_kappa: Optional[Decimal] = None
    c3_score: Optional[Decimal] = None
    is_valid: Optional[int] = None
    certificate_schema_version: int = 1          # v2 builder assigns 2 EXPLICITLY
    calculation_version: str = "tis-v1-legacy"   # v2 builder assigns "tis-v2" EXPLICITLY
    score_precision_policy: str = ""             # v2 builder assigns the named constant
    decay_algorithm_version: str = ""            # v2 builder assigns the named constant
    provenance_schema_version: int = 0           # v2 builder assigns 1

    # ---- Serialization -------------------------------------------------- #

    def to_dict(self) -> Dict[str, Any]:
        """
        Return the TC as a JSON-serializable dict.

        Version-aware (tis-v2 Commit 4): a v1 certificate serializes
        EXACTLY the historical 76-key wire shape — none of the v2 fields
        appear, so newly issued v1 certificates keep verifying through
        both the frozen projection and the raw stored-content path. A
        v2 certificate serializes the exact V2_FIELD_SET via validating
        (never repairing) serializers.
        """
        if self.certificate_schema_version == 2:
            return self._to_dict_v2()
        return self._to_dict_v1()

    def _to_dict_v1(self) -> Dict[str, Any]:
        """
        The FROZEN historical v1 serialization.

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

    def _to_dict_v2(self) -> Dict[str, Any]:
        """
        The tis-v2 serialization: exactly V2_FIELD_SET, one authoritative
        field per concept, canonical fixed-scale strings for every
        score-domain value, and validating serializers that REFUSE to
        repair a non-canonical internal value.

        Omitted legacy aliases (single-vocabulary rule): ``gate_passed``
        (superseded by ``gate_result``), ``decay_rate`` (superseded by
        ``resolved_decay_rate``), and ``failing_dimension_subfactors``
        (superseded by ``c3_score``; sub-factor detail beyond C3 is not
        part of the v2 computation contract).
        """
        def _score(name: str) -> str:
            return serialize_canonical_score(getattr(self, name), name)

        def _score_dict(name: str) -> Dict[str, str]:
            return {
                k: serialize_canonical_score(v, f"{name}.{k}")
                for k, v in getattr(self, name).items()
            }

        def _param(name: str) -> str:
            value = getattr(self, name)
            require_canonical_parameter(value, name)
            return format(value, ".4f")

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

            # Score — canonical fixed-scale strings
            "s_base": _score("s_base"),
            "s_adjusted": _score("s_adjusted"),
            "tis_raw": _score("tis_raw"),
            "tis_adjusted": _score("tis_adjusted"),
            "tis_current": _score("tis_current"),
            "component_scores": _score_dict("component_scores"),
            "component_scores_observed": _score_dict("component_scores_observed"),
            "component_scores_raw": {
                k: serialize_raw_decimal(v)
                for k, v in self.component_scores_raw.items()
            },
            "component_weights": _score_dict("component_weights"),
            "penalty_aggregate": _score("penalty_aggregate"),
            "penalty_breakdown": {
                k: _serialize_parameter_4dp(v, f"penalty_breakdown.{k}")
                for k, v in self.penalty_breakdown.items()
            },
            "resolved_penalty_weights": _score_dict("resolved_penalty_weights"),
            "adjustments_applied": [
                {
                    "rule_id": a.rule_id,
                    "dimension": a.dimension,
                    "value_before": serialize_canonical_score(
                        a.value_before, "adjustments_applied.value_before"),
                    "value_after": serialize_canonical_score(
                        a.value_after, "adjustments_applied.value_after"),
                    "reason": a.reason,
                }
                for a in self.adjustments_applied
            ],

            # Gate — one authoritative aggregate
            "gate_set": list(self.gate_set),
            "thresholds": _score_dict("thresholds"),
            "gate_results": dict(self.gate_results),
            "gate_result": self.gate_result,
            "blocking_reason": self.blocking_reason,
            "failure_mode": self.failure_mode,

            # Decision inputs and outcome
            "decision": self.decision,
            "requires_human_review": bool(self.requires_human_review),
            "escalation_routed_to": list(self.escalation_routed_to),
            "c3_score": _score("c3_score"),
            "is_valid": self.is_valid,
            "resolved_theta_allow": _score("resolved_theta_allow"),
            "resolved_theta_hold": _score("resolved_theta_hold"),
            "resolved_theta_escalate": _score("resolved_theta_escalate"),
            "resolved_kappa": _score("resolved_kappa"),

            # Decay
            "resolved_decay_rate": _param("resolved_decay_rate"),
            "elapsed_hours": _param("elapsed_hours"),
            "decay_factor": _score("decay_factor"),

            # Provenance layers
            "source_references": list(self.source_references),
            "retrieval_ids": list(self.retrieval_ids),
            "chain_of_custody_id": self.chain_of_custody_id,
            "audit_log_id": self.audit_log_id,
            "integration_boundary_gaps": int(self.integration_boundary_gaps),
            "governance_rule_matches": [
                serialize_governance_rule_match(m)
                for m in sorted(
                    self.governance_rule_matches or [],
                    key=rule_match_sort_key,
                )
            ],
            "c3_provenance": [
                serialize_c3_provenance_record(r)
                for r in sorted(self.c3_provenance, key=c3_record_sort_key)
            ],

            # Temporal
            "evaluation_timestamp": _iso_z(self.evaluation_timestamp),
            "valid_until": _iso_z(self.valid_until),
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

            # MCP / CT / composer audit
            "mcp_server_id": self.mcp_server_id,
            "scope_attestation": _round_floats_v2(dict(self.scope_attestation)),
            "connection_type": self.connection_type,
            "connection_type_modifier_id": self.connection_type_modifier_id,
            "resolved_policy_profile_id": self.resolved_policy_profile_id,
            "chain_depth": int(self.chain_depth),
            "chain_u_scores": [
                round(float(v), 4) for v in self.chain_u_scores
            ],
            "composer_metadata": (
                dict(self.composer_metadata) if self.composer_metadata else None
            ),

            # Trust Enforcement Layer
            "identity_binding": _tel_layer_to_dict(self.identity_binding),
            "governance_status": _tel_layer_to_dict(self.governance_status),
            "override_record": _tel_layer_to_dict(self.override_record),

            # Nine-outcome decision metadata
            "qualified_decision": self.qualified_decision,
            "enhanced_logging": bool(self.enhanced_logging),
            "reason_code": self.reason_code,
            "proximity_to_threshold": (
                round(float(self.proximity_to_threshold), 4)
                if self.proximity_to_threshold is not None else None
            ),
            "redaction_applied": bool(self.redaction_applied),
            "redacted_fields": list(self.redacted_fields),
            "redaction_scope": self.redaction_scope,
            "step_up_required": bool(self.step_up_required),
            "step_up_completed": self.step_up_completed,
            "compensation_scope": self.compensation_scope,
            "incident_id": self.incident_id,
            "recovery_mode_activated": bool(self.recovery_mode_activated),

            # Version identifiers — from explicit construction, validated
            # at sealing; never dataclass defaults.
            "certificate_schema_version": int(self.certificate_schema_version),
            "calculation_version": self.calculation_version,
            "score_precision_policy": self.score_precision_policy,
            "decay_algorithm_version": self.decay_algorithm_version,
            "provenance_schema_version": int(self.provenance_schema_version),

            # audit_integrity last; excluded from the hash payload.
            "audit_integrity": _tel_layer_to_dict(self.audit_integrity),
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


# =========================================================================== #
# tis-v2 — v2 certificate core (Commit 4 of the landing sequence)             #
# =========================================================================== #
#
# Everything below is ADDITIVE. Production issuance remains on the v1
# path (generate_certificate + map_decision) until Commit 5 atomically
# switches the orchestration call sites; generate_certificate_v2 is
# reachable only from tests in this commit.
#
# The sealing guarantee does NOT depend on callers remembering to
# validate: build_v2_hash_payload — reached exclusively through
# compute_tc_hash's version dispatch, the only way any certificate hash
# is produced — executes the complete validation-and-replay contract
# before returning payload bytes. A v2 certificate that does not
# reproduce its own computation cannot acquire a hash at all. The
# certificate store additionally invokes the same validator explicitly
# at the persistence boundary.


# --------------------------------------------------------------------------- #
# The exact v2 wire field set                                                  #
# --------------------------------------------------------------------------- #
#
# Single-vocabulary rule: one authoritative field per concept. The
# legacy aliases gate_passed (superseded by gate_result), decay_rate
# (superseded by resolved_decay_rate), and failing_dimension_subfactors
# (superseded by c3_score) are OMITTED from the v2 wire; they survive
# only in the frozen v1 representation.
#
# V2_OPTIONAL_FIELDS is empty by design: the v2 wire always emits every
# field; nullability is a per-field type rule, never key omission.

_V2_REMOVED_LEGACY_ALIASES = frozenset({
    "gate_passed", "decay_rate", "failing_dimension_subfactors",
})

_V2_NEW_FIELDS = frozenset({
    "component_scores_raw", "component_scores_observed",
    "adjustments_applied", "c3_provenance", "gate_result",
    "resolved_penalty_weights", "resolved_decay_rate", "elapsed_hours",
    "decay_factor", "resolved_theta_allow", "resolved_theta_hold",
    "resolved_theta_escalate", "resolved_kappa", "c3_score", "is_valid",
    "certificate_schema_version", "calculation_version",
    "score_precision_policy", "decay_algorithm_version",
    "provenance_schema_version",
})

V2_REQUIRED_FIELDS = frozenset(
    (V1_HASH_FIELD_SET - _V2_REMOVED_LEGACY_ALIASES)
    | _V2_NEW_FIELDS
    | {"audit_integrity"}
)
V2_OPTIONAL_FIELDS = frozenset()
V2_FIELD_SET = V2_REQUIRED_FIELDS | V2_OPTIONAL_FIELDS

_BACK_DIMS = frozenset({"B", "A", "C", "K"})
_ALLOWED_GATE_RESULTS = frozenset({"pass", "fail", "not_applicable"})
_ALLOWED_DECISIONS = frozenset({"Allow", "Observe", "Hold", "Escalate", "Stop"})
_CANONICAL_PENALTY_KEYS = frozenset({"cb", "d", "n", "h", "ps"})

_FIXED_4DP_SCORE = _re.compile(r"^(?:0\.\d{4}|1\.0000)$")
_FIXED_4DP_PARAM = _re.compile(r"^(?:0|[1-9]\d*)\.\d{4}$")


# --------------------------------------------------------------------------- #
# Strict wire parsers (validate, never repair)                                 #
# --------------------------------------------------------------------------- #

def _parse_score_string(value: Any, name: str) -> Decimal:
    if not isinstance(value, str) or not _FIXED_4DP_SCORE.fullmatch(value):
        raise CertificateInvariantError(
            f"{name} is not a canonical fixed-scale 4dp score string: "
            f"{value!r}"
        )
    return Decimal(value)


def _parse_param_string(value: Any, name: str) -> Decimal:
    if not isinstance(value, str) or not _FIXED_4DP_PARAM.fullmatch(value):
        raise CertificateInvariantError(
            f"{name} is not a canonical 4dp parameter string: {value!r}"
        )
    return Decimal(value)


def _parse_raw_string(value: Any, name: str) -> Decimal:
    """Variable-scale raw-evidence string: lossless, deterministic form."""
    if not isinstance(value, str):
        raise CertificateInvariantError(
            f"{name} must be a decimal string, got {type(value).__name__}"
        )
    try:
        d = Decimal(value)
    except Exception as exc:  # noqa: BLE001
        raise CertificateInvariantError(
            f"{name} is not a parseable decimal: {value!r}"
        ) from exc
    if not d.is_finite() or d < 0 or d > 1:
        raise CertificateInvariantError(
            f"{name} outside [0, 1]: {value!r}"
        )
    # The deterministic raw form is exactly what serialize_raw_decimal
    # emits; anything else (trailing zeros, exponent notation) is not
    # the canonical raw representation.
    if serialize_raw_decimal(d) != value:
        raise CertificateInvariantError(
            f"{name} is not in deterministic raw form: {value!r}"
        )
    return d


def _require_int01(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) \
            or value not in (0, 1):
        raise CertificateInvariantError(
            f"{name} must be int 0 or 1, got {value!r}"
        )
    return value


# --------------------------------------------------------------------------- #
# v2 sealing validation — the complete contract                                #
# --------------------------------------------------------------------------- #

def _resolve_penalty_maxima_for_seal(
    risk_tier: str, action_class: str,
) -> Dict[str, Decimal]:
    """Per-component penalty maxima from the recorded (r, a) axes.

    These are tis-v2 CALCULATION CONSTANTS (versioned by
    calculation_version), not a mutable rule registry — consulting them
    during verification is permitted.
    """
    if risk_tier not in W_NOVELTY_BY_TIER_DECIMAL:
        raise CertificateInvariantError(
            f"unknown risk_tier {risk_tier!r} for penalty maxima"
        )
    return {
        "cb": P_CB_MAX_V2,
        "d": canonical_score(DELTA_D_MAX_DECIMAL),
        "n": canonical_score(W_NOVELTY_BY_TIER_DECIMAL[risk_tier]),
        "h": canonical_score(DELTA_H_MAX_DECIMAL),
        "ps": canonical_score(_W_PS_SPECIAL_DECIMAL.get(
            (risk_tier, action_class), _W_PS_DEFAULT_DECIMAL,
        )),
    }


def _quantize_pinned(value: Decimal) -> Decimal:
    with localcontext(TIS_DECIMAL_CONTEXT):
        result = value.quantize(SCORE_QUANTUM, rounding=SCORE_ROUNDING)
    return Decimal("0.0000") if result == 0 else result


def _replay_decision_v2_from_values(
    *,
    is_valid: int,
    c3_score: Decimal,
    gate: int,
    s_base: Decimal,
    tis_current: Decimal,
    kappa: Decimal,
    theta_allow: Decimal,
    theta_hold: Decimal,
    theta_escalate: Decimal,
    risk_tier: str,
) -> str:
    """Replay the tis-v2 decision from hash-protected certificate
    contents only — no live or mutable lookups."""
    # Lazy import avoids any module-order coupling; decision_engine does
    # not import trust_certificate, so there is no cycle either way.
    from tcs.decision_engine import _apply_priority_ladder_v2
    return _apply_priority_ladder_v2(
        is_valid=is_valid, c3_score=c3_score, gate=gate, s_base=s_base,
        tis_current=tis_current, kappa=kappa, theta_allow=theta_allow,
        theta_hold=theta_hold, theta_escalate=theta_escalate,
        risk_tier=risk_tier,
    )


def validate_v2_certificate_for_sealing(serialized: Dict[str, Any]) -> None:
    """The complete tis-v2 sealing contract, over SERIALIZED content.

    Runs immediately before any v2 hash is produced (via
    build_v2_hash_payload) and again explicitly at the certificate
    store's persistence boundary. Registry-independent: consults only
    the serialized content, tis-v2 calculation constants, and the
    append-only pattern-version registry.

    Order: exact schema -> canonical form -> independent replay of every
    derived value and the decision -> provenance validation.
    """
    # ---- 1. Exact versioned schema ------------------------------------- #
    version = classify_certificate_schema_version(serialized)
    if version != 2:
        raise CertificateInvariantError(
            f"validate_v2_certificate_for_sealing requires schema "
            f"version 2, got {version}"
        )
    keys = set(serialized)
    if keys != V2_FIELD_SET:
        unexpected = sorted(keys - V2_FIELD_SET)
        missing = sorted(V2_FIELD_SET - keys)
        raise CertificateInvariantError(
            f"v2 wire schema mismatch: unexpected={unexpected} "
            f"missing={missing}"
        )

    calc_version = serialized["calculation_version"]
    if calc_version != CALCULATION_VERSION_V2:
        raise UnsupportedCalculationVersion(calc_version)
    if serialized["score_precision_policy"] != SCORE_PRECISION_POLICY:
        raise CertificateInvariantError(
            f"unsupported score_precision_policy "
            f"{serialized['score_precision_policy']!r}"
        )
    if serialized["decay_algorithm_version"] != DECAY_ALGORITHM_VERSION:
        raise CertificateInvariantError(
            f"unsupported decay_algorithm_version "
            f"{serialized['decay_algorithm_version']!r}"
        )
    if serialized["provenance_schema_version"] != C3_PROVENANCE_SCHEMA_VERSION:
        raise CertificateInvariantError(
            f"unsupported provenance_schema_version "
            f"{serialized['provenance_schema_version']!r}"
        )

    # ---- 2. Canonical form of every numerical field --------------------- #
    effective = {
        k: _parse_score_string(v, f"component_scores.{k}")
        for k, v in serialized["component_scores"].items()
    }
    raw = {
        k: _parse_raw_string(v, f"component_scores_raw.{k}")
        for k, v in serialized["component_scores_raw"].items()
    }
    del raw  # validated for form and range; raw evidence is attested
    observed = {
        k: _parse_score_string(v, f"component_scores_observed.{k}")
        for k, v in serialized["component_scores_observed"].items()
    }
    del observed  # validated; observed tier is attested, not recomputed
    weights = {
        k: _parse_score_string(v, f"component_weights.{k}")
        for k, v in serialized["component_weights"].items()
    }
    thresholds = {
        k: _parse_score_string(v, f"thresholds.{k}")
        for k, v in serialized["thresholds"].items()
    }
    for name in ("component_scores", "component_scores_observed",
                 "component_scores_raw", "component_weights"):
        if set(serialized[name]) != _BACK_DIMS:
            raise CertificateInvariantError(f"{name} key set incomplete")
    if not set(thresholds).issubset(_BACK_DIMS):
        raise CertificateInvariantError("unknown threshold dimension")

    s_base = _parse_score_string(serialized["s_base"], "s_base")
    s_adjusted = _parse_score_string(serialized["s_adjusted"], "s_adjusted")
    tis_raw = _parse_score_string(serialized["tis_raw"], "tis_raw")
    tis_adjusted = _parse_score_string(
        serialized["tis_adjusted"], "tis_adjusted")
    tis_current = _parse_score_string(
        serialized["tis_current"], "tis_current")
    penalty_aggregate = _parse_score_string(
        serialized["penalty_aggregate"], "penalty_aggregate")
    decay_factor = _parse_score_string(
        serialized["decay_factor"], "decay_factor")
    c3_score = _parse_score_string(serialized["c3_score"], "c3_score")
    theta_allow = _parse_score_string(
        serialized["resolved_theta_allow"], "resolved_theta_allow")
    theta_hold = _parse_score_string(
        serialized["resolved_theta_hold"], "resolved_theta_hold")
    theta_escalate = _parse_score_string(
        serialized["resolved_theta_escalate"], "resolved_theta_escalate")
    kappa = _parse_score_string(serialized["resolved_kappa"], "resolved_kappa")

    risk_tier = serialized["risk_tier"]
    action_class = serialized["action_class"]
    maxima = _resolve_penalty_maxima_for_seal(risk_tier, action_class)
    penalty_breakdown = {}
    for k, v in serialized["penalty_breakdown"].items():
        parsed = _parse_param_string(v, f"penalty_breakdown.{k}")
        if k not in maxima:
            raise CertificateInvariantError(
                f"unknown penalty component {k!r}"
            )
        if parsed > maxima[k]:
            raise CertificateInvariantError(
                f"penalty_breakdown.{k} exceeds maximum {maxima[k]}"
            )
        penalty_breakdown[k] = parsed
    penalty_weights = {
        k: _parse_score_string(v, f"resolved_penalty_weights.{k}")
        for k, v in serialized["resolved_penalty_weights"].items()
    }
    if set(penalty_breakdown) != _CANONICAL_PENALTY_KEYS:
        raise CertificateInvariantError("penalty_breakdown key set invalid")
    if set(penalty_weights) != _CANONICAL_PENALTY_KEYS:
        raise CertificateInvariantError(
            "resolved_penalty_weights key set invalid")

    resolved_decay_rate = _parse_param_string(
        serialized["resolved_decay_rate"], "resolved_decay_rate")
    elapsed_hours = _parse_param_string(
        serialized["elapsed_hours"], "elapsed_hours")

    gate_result = _require_int01(serialized["gate_result"], "gate_result")
    is_valid = _require_int01(serialized["is_valid"], "is_valid")

    gate_set = serialized["gate_set"]
    if (not isinstance(gate_set, list)
            or gate_set != sorted(set(gate_set))
            or not set(gate_set).issubset(_BACK_DIMS)):
        raise CertificateInvariantError(
            f"gate_set must be a sorted, deduplicated subset of BACK: "
            f"{gate_set!r}"
        )
    missing_thresholds = set(gate_set) - set(thresholds)
    if missing_thresholds:
        raise CertificateInvariantError(
            f"gated dimensions missing thresholds: "
            f"{sorted(missing_thresholds)}"
        )
    gate_results = serialized["gate_results"]
    if set(gate_results) != _BACK_DIMS:
        raise CertificateInvariantError(
            "gate_results must cover all four dimensions")
    if any(v not in _ALLOWED_GATE_RESULTS for v in gate_results.values()):
        raise CertificateInvariantError(
            "gate_results contains an unknown result")

    decision = serialized["decision"]
    if decision not in _ALLOWED_DECISIONS:
        raise CertificateInvariantError(f"unknown decision {decision!r}")

    # Adjustments: canonical score strings, valid shape.
    for a in serialized["adjustments_applied"]:
        if not isinstance(a, dict) or set(a) != {
            "rule_id", "dimension", "value_before", "value_after", "reason",
        }:
            raise CertificateInvariantError(
                "adjustments_applied entry has invalid shape")
        if a["dimension"] not in _BACK_DIMS:
            raise CertificateInvariantError(
                f"adjustments_applied dimension {a['dimension']!r} unknown")
        _parse_score_string(a["value_before"], "adjustments.value_before")
        _parse_score_string(a["value_after"], "adjustments.value_after")

    # ---- 3. Structural invariants --------------------------------------- #
    with localcontext(TIS_DECIMAL_CONTEXT):
        weight_sum = sum(weights.values(), Decimal("0"))
        pweight_sum = sum(penalty_weights.values(), Decimal("0"))
    if weight_sum != Decimal("1.0000"):
        raise CertificateInvariantError(
            f"component_weights sum {weight_sum} != 1.0000")
    if pweight_sum != Decimal("1.0000"):
        raise CertificateInvariantError(
            f"resolved_penalty_weights sum {pweight_sum} != 1.0000")
    if not (theta_escalate <= theta_hold <= theta_allow):
        raise CertificateInvariantError(
            f"threshold ordering violated: escalate={theta_escalate} "
            f"hold={theta_hold} allow={theta_allow}"
        )
    if penalty_aggregate > Decimal("0.5000"):
        raise CertificateInvariantError(
            f"penalty_aggregate {penalty_aggregate} exceeds the 0.5000 cap")

    # ---- 4. Independent replay of every derived value -------------------- #
    with localcontext(TIS_DECIMAL_CONTEXT):
        recomputed_s_base = sum(
            (weights[k] * _parse_score_string(
                serialized["component_scores"][k], f"component_scores.{k}")
             for k in weights),
            Decimal("0"),
        ).quantize(SCORE_QUANTUM, rounding=SCORE_ROUNDING)
    if recomputed_s_base == 0:
        recomputed_s_base = Decimal("0.0000")
    if recomputed_s_base != s_base:
        raise CertificateInvariantError(
            "component scores do not reproduce s_base; refusing to seal")

    recomputed_gates = {}
    for dim in ("B", "A", "C", "K"):
        if dim not in gate_set:
            recomputed_gates[dim] = "not_applicable"
        elif effective[dim] >= thresholds[dim]:
            recomputed_gates[dim] = "pass"
        else:
            recomputed_gates[dim] = "fail"
    if recomputed_gates != gate_results:
        raise CertificateInvariantError(
            "recorded gate results are not reproduced by recorded "
            "effective scores and thresholds")
    recomputed_gate = 0 if any(
        recomputed_gates[d] == "fail" for d in gate_set
    ) else 1
    if recomputed_gate != gate_result:
        raise CertificateInvariantError(
            "aggregate gate result is not reproduced by recorded gates")

    with localcontext(TIS_DECIMAL_CONTEXT):
        weighted_penalty = sum(
            (penalty_weights[k] * penalty_breakdown[k]
             for k in sorted(penalty_breakdown)),
            Decimal("0"),
        )
        recomputed_penalty = min(Decimal("0.5000"), weighted_penalty)
    recomputed_penalty = _quantize_pinned(recomputed_penalty)
    if recomputed_penalty != penalty_aggregate:
        raise CertificateInvariantError(
            "penalty_breakdown does not reproduce penalty_aggregate")

    with localcontext(TIS_DECIMAL_CONTEXT):
        recomputed_s_adj = _quantize_pinned(
            s_base * (Decimal("1.0000") - penalty_aggregate))
        recomputed_tis_raw = _quantize_pinned(
            Decimal(gate_result) * s_base)
        recomputed_tis_adj = _quantize_pinned(
            Decimal(gate_result) * s_adjusted)
        decay_input = (-resolved_decay_rate * elapsed_hours).exp()
    if recomputed_s_adj != s_adjusted:
        raise CertificateInvariantError(
            "recorded s_adjusted is not reproduced")
    if recomputed_tis_raw != tis_raw:
        raise CertificateInvariantError("recorded tis_raw is not reproduced")
    if recomputed_tis_adj != tis_adjusted:
        raise CertificateInvariantError(
            "recorded tis_adjusted is not reproduced")
    if not decay_input.is_finite() or decay_input < 0 or decay_input > 1:
        raise CertificateInvariantError(
            f"replayed decay factor out of range: {decay_input}")
    recomputed_decay = _quantize_pinned(decay_input)
    if recomputed_decay != decay_factor:
        raise CertificateInvariantError(
            "recorded decay_factor is not reproduced")
    with localcontext(TIS_DECIMAL_CONTEXT):
        recomputed_current = _quantize_pinned(
            tis_adjusted * decay_factor * Decimal(is_valid))
    if recomputed_current != tis_current:
        raise CertificateInvariantError(
            "recorded tis_current is not reproduced")

    # ---- 5. Decision replay (versioned; unconditional C3-zero Stop) ----- #
    recomputed_decision = _replay_decision_v2_from_values(
        is_valid=is_valid, c3_score=c3_score, gate=gate_result,
        s_base=s_base, tis_current=tis_current, kappa=kappa,
        theta_allow=theta_allow, theta_hold=theta_hold,
        theta_escalate=theta_escalate, risk_tier=risk_tier,
    )
    if recomputed_decision != decision:
        raise CertificateInvariantError(
            f"recorded decision {decision!r} is not reproduced by "
            f"certificate contents (replay: {recomputed_decision!r})")
    if c3_score == Decimal("0.0000") and decision != "Stop":
        raise CertificateInvariantError(
            "tis-v2 invariant violated: c3_score == 0.0000 requires "
            "decision == 'Stop' independently of gate_result")

    # ---- 6. Provenance --------------------------------------------------- #
    typed_matches = [
        governance_rule_match_from_dict(m)
        for m in serialized["governance_rule_matches"]
    ]
    match_sort = [rule_match_sort_key(m) for m in typed_matches]
    if match_sort != sorted(match_sort):
        raise CertificateInvariantError(
            "governance_rule_matches not in canonical order")
    records = [
        c3_provenance_record_from_dict(r)
        for r in serialized["c3_provenance"]
    ]
    record_sort = [c3_record_sort_key(r) for r in records]
    if record_sort != sorted(record_sort):
        raise CertificateInvariantError(
            "c3_provenance not in canonical order")

    if c3_score == Decimal("0.0000"):
        if not records:
            raise CertificateInvariantError(
                "c3_score == 0.0000 requires at least one "
                "C3ProvenanceRecord")
    else:
        if records:
            raise CertificateInvariantError(
                "c3_provenance must be empty when c3_score != 0.0000")

    # Internal references only — never a live registry lookup.
    match_keys = {(m.rule_id, m.rule_version) for m in typed_matches}
    for r in records:
        if r.source_type == "rule":
            for ref in r.rule_match_refs:
                if (ref.rule_id, ref.rule_version) not in match_keys:
                    raise CertificateInvariantError(
                        f"c3_provenance rule ref "
                        f"({ref.rule_id!r}, {ref.rule_version!r}) does "
                        f"not resolve to a recorded governance rule match")


def build_v2_hash_payload(serialized: Dict[str, Any]) -> bytes:
    """The tis-v2 hash payload: validate-and-replay, then hash as-given.

    Because v2 owns its wire shape from birth, the payload is the
    canonical JSON of the serialized dict minus ``audit_integrity`` —
    after the COMPLETE sealing contract has passed. The exact-schema
    check makes any injected field a hard failure, giving stored v2
    content the same tamper-detection property the raw v1 path has.
    """
    validate_v2_certificate_for_sealing(serialized)
    content = {
        k: v for k, v in serialized.items() if k != "audit_integrity"
    }
    return _canonical_json_bytes(content)


# --------------------------------------------------------------------------- #
# v2 deserialization (strict, never repairing)                                 #
# --------------------------------------------------------------------------- #

_IDENTITY_BINDING_KEYS = frozenset({
    "requesting_identity", "identity_type", "role", "authorization_tier",
    "identity_confidence", "identity_verified", "authentication_method",
    "requesting_session_id",
})
_GOVERNANCE_STATUS_KEYS = frozenset({
    "governance_status", "evaluation_completeness_score",
    "components_evaluated", "components_skipped", "skip_reasons",
    "fail_safe_applied", "fail_safe_type", "governance_integrity_score",
})
_OVERRIDE_RECORD_KEYS = frozenset({
    "override_invoked", "original_decision", "override_decision",
    "override_actor", "override_actor_role", "override_reason",
    "override_type", "policy_exception_id", "regulatory_basis",
    "co_authorizer", "post_override_review_required",
    "post_override_review_deadline", "post_override_review_completed",
    "override_creates_tc_amendment",
})
_AUDIT_INTEGRITY_KEYS = frozenset({
    "tc_hash", "previous_tc_hash", "chain_sequence", "chain_id",
    "hash_algorithm", "integrity_verified", "issued_by",
})


def _strict_layer(d: Optional[Dict[str, Any]], keys: frozenset,
                  cls: Any, name: str) -> Any:
    if d is None:
        return None
    if not isinstance(d, dict) or set(d) != keys:
        raise CertificateInvariantError(
            f"{name} layer has invalid key set: "
            f"{sorted(d) if isinstance(d, dict) else type(d).__name__}"
        )
    return cls(**d)


def _parse_iso_z(value: str, name: str) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise CertificateInvariantError(
            f"{name} must be an ISO-8601 Z string, got {value!r}"
        )
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")


def tc_from_dict_v2(d: Dict[str, Any]) -> "TrustCertificate":
    """Rebuild a v2 TrustCertificate from its serialized dict.

    Strict: runs the complete sealing validation first (which also
    proves replayability), then parses. Registry-independent. The
    model fields that exist only for v1 compatibility (gate_passed,
    decay_rate, failing_dimension_subfactors) are derived
    deterministically from their v2 authoritative counterparts — they
    are never serialized on v2.
    """
    validate_v2_certificate_for_sealing(d)

    adjustments = [
        AdjustmentApplied(
            rule_id=a["rule_id"], dimension=a["dimension"],
            value_before=Decimal(a["value_before"]),
            value_after=Decimal(a["value_after"]),
            reason=a["reason"],
        )
        for a in d["adjustments_applied"]
    ]
    typed_matches = [
        governance_rule_match_from_dict(m)
        for m in d["governance_rule_matches"]
    ]
    records = [
        c3_provenance_record_from_dict(r) for r in d["c3_provenance"]
    ]

    resolved_decay_rate = Decimal(d["resolved_decay_rate"])

    return TrustCertificate(
        certificate_id=d["certificate_id"],
        subject_id=d["subject_id"],
        subject_type=d["subject_type"],
        domain=d["domain"],
        risk_tier=d["risk_tier"],
        action_class=d["action_class"],
        policy_severity=d["policy_severity"],
        checkpoint_id=d["checkpoint_id"],
        gca_context_id=d["gca_context_id"],
        policy_set_id=d["policy_set_id"],
        s_base=Decimal(d["s_base"]),
        s_adjusted=Decimal(d["s_adjusted"]),
        tis_raw=Decimal(d["tis_raw"]),
        tis_adjusted=Decimal(d["tis_adjusted"]),
        tis_current=Decimal(d["tis_current"]),
        component_scores={
            k: Decimal(v) for k, v in d["component_scores"].items()
        },
        component_weights={
            k: Decimal(v) for k, v in d["component_weights"].items()
        },
        penalty_aggregate=Decimal(d["penalty_aggregate"]),
        penalty_breakdown={
            k: Decimal(v) for k, v in d["penalty_breakdown"].items()
        },
        failing_dimension_subfactors={},   # v1-only alias; not on v2 wire
        gate_set=list(d["gate_set"]),
        thresholds={k: Decimal(v) for k, v in d["thresholds"].items()},
        gate_results=dict(d["gate_results"]),
        gate_passed=(d["gate_result"] == 1),   # derived; not serialized on v2
        blocking_reason=d["blocking_reason"],
        failure_mode=d["failure_mode"],
        decision=d["decision"],
        requires_human_review=d["requires_human_review"],
        escalation_routed_to=list(d["escalation_routed_to"]),
        source_references=list(d["source_references"]),
        retrieval_ids=list(d["retrieval_ids"]),
        chain_of_custody_id=d["chain_of_custody_id"],
        audit_log_id=d["audit_log_id"],
        integration_boundary_gaps=d["integration_boundary_gaps"],
        evaluation_timestamp=_parse_iso_z(
            d["evaluation_timestamp"], "evaluation_timestamp"),
        valid_until=_parse_iso_z(d["valid_until"], "valid_until"),
        decay_rate=float(resolved_decay_rate),   # derived; not serialized on v2
        recompute_required=d["recompute_required"],
        invalidation_triggers=list(d["invalidation_triggers"]),
        last_invalidation_event=dict(d["last_invalidation_event"]),
        invalidation_status=d["invalidation_status"],
        explanation_summary=d["explanation_summary"],
        key_factors=list(d["key_factors"]),
        key_concerns=list(d["key_concerns"]),
        regulatory_explanation_level=d["regulatory_explanation_level"],
        regulatory_mapping=list(d["regulatory_mapping"]),
        lifecycle_state=d["lifecycle_state"],
        state_transition_history=[
            dict(e) for e in d["state_transition_history"]
        ],
        recomputed_from_certificate_id=d["recomputed_from_certificate_id"],
        superseded_by_certificate_id=d["superseded_by_certificate_id"],
        archived=d["archived"],
        mcp_server_id=d["mcp_server_id"],
        scope_attestation=dict(d["scope_attestation"]),
        connection_type=d["connection_type"],
        connection_type_modifier_id=d["connection_type_modifier_id"],
        resolved_policy_profile_id=d["resolved_policy_profile_id"],
        chain_depth=d["chain_depth"],
        chain_u_scores=list(d["chain_u_scores"]),
        composer_metadata=(
            dict(d["composer_metadata"])
            if d["composer_metadata"] is not None else None
        ),
        governance_rule_matches=typed_matches,
        identity_binding=_strict_layer(
            d["identity_binding"], _IDENTITY_BINDING_KEYS,
            IdentityBinding, "identity_binding"),
        governance_status=_strict_layer(
            d["governance_status"], _GOVERNANCE_STATUS_KEYS,
            GovernanceStatus, "governance_status"),
        audit_integrity=_strict_layer(
            d["audit_integrity"], _AUDIT_INTEGRITY_KEYS,
            AuditIntegrity, "audit_integrity"),
        override_record=_strict_layer(
            d["override_record"], _OVERRIDE_RECORD_KEYS,
            OverrideRecord, "override_record"),
        qualified_decision=d["qualified_decision"],
        enhanced_logging=d["enhanced_logging"],
        reason_code=d["reason_code"],
        proximity_to_threshold=d["proximity_to_threshold"],
        redaction_applied=d["redaction_applied"],
        redacted_fields=list(d["redacted_fields"]),
        redaction_scope=d["redaction_scope"],
        step_up_required=d["step_up_required"],
        step_up_completed=d["step_up_completed"],
        compensation_scope=d["compensation_scope"],
        incident_id=d["incident_id"],
        recovery_mode_activated=d["recovery_mode_activated"],
        component_scores_raw={
            k: Decimal(v) for k, v in d["component_scores_raw"].items()
        },
        component_scores_observed={
            k: Decimal(v)
            for k, v in d["component_scores_observed"].items()
        },
        adjustments_applied=adjustments,
        c3_provenance=records,
        gate_result=d["gate_result"],
        resolved_penalty_weights={
            k: Decimal(v)
            for k, v in d["resolved_penalty_weights"].items()
        },
        resolved_decay_rate=resolved_decay_rate,
        elapsed_hours=Decimal(d["elapsed_hours"]),
        decay_factor=Decimal(d["decay_factor"]),
        resolved_theta_allow=Decimal(d["resolved_theta_allow"]),
        resolved_theta_hold=Decimal(d["resolved_theta_hold"]),
        resolved_theta_escalate=Decimal(d["resolved_theta_escalate"]),
        resolved_kappa=Decimal(d["resolved_kappa"]),
        c3_score=Decimal(d["c3_score"]),
        is_valid=d["is_valid"],
        certificate_schema_version=2,
        calculation_version=d["calculation_version"],
        score_precision_policy=d["score_precision_policy"],
        decay_algorithm_version=d["decay_algorithm_version"],
        provenance_schema_version=d["provenance_schema_version"],
    )


TrustCertificate.from_dict_v2 = staticmethod(tc_from_dict_v2)


# --------------------------------------------------------------------------- #
# Issuance-time provenance construction                                        #
# --------------------------------------------------------------------------- #
#
# These helpers MAY consult the live rule registries — issuance is the
# one moment the registered rule is authoritative. After construction
# the certificate stands alone; nothing in validation or replay above
# consults a registry.

_INJECTION_REASON_RE = _re.compile(
    r"^(?P<loc>.+?): injection pattern (?P<pat>.+)$"
)
_CREDENTIAL_REASON_RE = _re.compile(
    r"^(?P<loc>.+?): credential pattern (?P<pat>.+)$"
)


def _registered_rule_for(rule_id: str, rule_version: str, evaluator: str):
    """Look up the registered rule at ISSUANCE time (never at replay)."""
    from tcs.governance import SCENARIO_RULES, TYPED_CONTEXT_RULES
    registry = (
        TYPED_CONTEXT_RULES if evaluator == "typed_context"
        else SCENARIO_RULES
    )
    for rule in registry:
        version = getattr(rule, "version", None)
        if rule.rule_id == rule_id and version == rule_version:
            return rule
    raise CertificateInvariantError(
        f"rule ({rule_id!r}, {rule_version!r}, {evaluator!r}) is not "
        f"registered; cannot lift typed provenance at issuance"
    )


def lift_governance_rule_matches(
    audit_dicts: List[Dict[str, Any]],
) -> List[GovernanceRuleMatch]:
    """Lift GCA audit dicts into typed, privacy-reduced records.

    Resolves matched lexical terms to (group_index, term_index)
    positions against the registered rule, reduces matched_facts to
    sorted KEYS, and verifies blocking_reason / explanation are the
    rule's static definition text. Fails closed on anything it cannot
    resolve — it never records lexical content as a fallback.
    """
    lifted: List[GovernanceRuleMatch] = []
    for d in audit_dicts:
        if not isinstance(d, dict):
            raise CertificateInvariantError(
                "governance_rule_matches audit entry is not a dict")
        evaluator = (
            "typed_context"
            if d.get("rule_evaluator") == "typed_context" else "term_group"
        )
        rule_id = str(d.get("rule_id") or "")
        rule_version = str(d.get("rule_version") or "")
        rule = _registered_rule_for(rule_id, rule_version, evaluator)

        term_groups_src = (
            rule.draft_term_groups if evaluator == "typed_context"
            else rule.required_term_groups
        )
        groups: List[MatchedTermGroup] = []
        for g in d.get("matched_term_groups") or []:
            gi = int(g["group_index"])
            term = str(g["matched_term"])
            if gi < 0 or gi >= len(term_groups_src):
                raise CertificateInvariantError(
                    f"rule {rule_id!r}: matched group_index {gi} out of "
                    f"range")
            try:
                ti = list(term_groups_src[gi]).index(term)
            except ValueError as exc:
                raise CertificateInvariantError(
                    f"rule {rule_id!r}: matched term for group {gi} is "
                    f"not in the registered vocabulary"
                ) from exc
            groups.append(MatchedTermGroup(group_index=gi, term_index=ti))
        groups.sort(key=lambda g: (g.group_index, g.term_index))

        effect = d.get("effect") or {}
        registered_effect = rule.effect
        recorded_blocking = str(effect.get("blocking_reason") or "")
        registered_blocking = str(registered_effect.blocking_reason or "")
        if recorded_blocking != registered_blocking:
            raise CertificateInvariantError(
                f"rule {rule_id!r}: blocking_reason is not the static "
                f"rule-definition text")
        recorded_explanation = str(effect.get("explanation") or "")
        registered_explanation = str(registered_effect.explanation or "")
        if recorded_explanation != registered_explanation:
            raise CertificateInvariantError(
                f"rule {rule_id!r}: explanation is not the static "
                f"rule-definition text")

        fact_keys = tuple(sorted(set(
            str(k) for k in (d.get("matched_facts") or {}).keys()
        )))

        def _param(value: Any, name: str) -> Decimal:
            return canonical_nonnegative_parameter(
                value if value is not None else 0,
                field_name=name,
            )

        m = GovernanceRuleMatch(
            schema_version=GOVERNANCE_RULE_MATCH_SCHEMA_VERSION,
            rule_id=rule_id,
            rule_version=rule_version,
            evaluator=evaluator,
            applies_to_domains=tuple(sorted(set(
                str(x) for x in (d.get("applies_to_domains") or [])
            ))),
            matched_domain=str(d.get("matched_domain") or ""),
            matched_term_groups=tuple(groups),
            matched_fact_keys=fact_keys,
            control_class=str(effect.get("control_class") or ""),
            safety_category=str(effect.get("safety_category") or ""),
            c3_violation=bool(effect.get("c3_violation", False)),
            blocking_reason=recorded_blocking,
            decision_pressure=str(effect.get("decision_pressure") or ""),
            requires_human_review=bool(
                effect.get("requires_human_review", False)),
            boundedness_penalty=_param(
                effect.get("boundedness_penalty"), "boundedness_penalty"),
            attribution_penalty=_param(
                effect.get("attribution_penalty"), "attribution_penalty"),
            known_calibration_penalty=_param(
                effect.get("known_calibration_penalty"),
                "known_calibration_penalty"),
            novelty_lift=_param(effect.get("novelty_lift"), "novelty_lift"),
            explanation=recorded_explanation,
            active_policy_profile_id=str(
                d.get("active_policy_profile_id") or ""),
        )
        validate_governance_rule_match(m)
        lifted.append(m)
    lifted.sort(key=rule_match_sort_key)
    return lifted


def _parse_pattern_repr(pat_repr: str) -> str:
    """Recover the regex source from its recorded repr() form."""
    import ast as _ast
    try:
        value = _ast.literal_eval(pat_repr)
    except Exception as exc:  # noqa: BLE001
        raise CertificateInvariantError(
            f"unparseable pattern repr in reason string: {pat_repr!r}"
        ) from exc
    if not isinstance(value, str):
        raise CertificateInvariantError(
            f"pattern repr did not decode to a string: {pat_repr!r}")
    return value


def derive_c3_provenance(
    meta: Dict[str, Any],
    typed_matches: List[GovernanceRuleMatch],
) -> List[C3ProvenanceRecord]:
    """Derive C3 provenance from the discovered structured context
    signals. Called only when C3 == 0.0000 and no explicit records were
    supplied. Fails closed when nothing explains the zero — callers
    with out-of-band producers must supply an explicit caller_supplied
    record with a nonempty producer_id.
    """
    records: List[C3ProvenanceRecord] = []

    c3_matches = [m for m in typed_matches if m.c3_violation]
    if c3_matches:
        refs = tuple(sorted(
            {RuleMatchRef(rule_id=m.rule_id, rule_version=m.rule_version)
             for m in c3_matches},
            key=lambda x: (x.rule_id, x.rule_version),
        ))
        records.append(C3ProvenanceRecord(
            schema_version=C3_PROVENANCE_SCHEMA_VERSION,
            source_type="rule",
            pattern_id="", pattern_set_version="", location_tag="",
            connector_type="",
            detail_code="governance_rule_c3_violation",
            producer_id="",
            rule_match_refs=refs,
        ))

    reason = meta.get("injection_reason")
    if isinstance(reason, str):
        m = _INJECTION_REASON_RE.match(reason)
        if m:
            pattern_source = _parse_pattern_repr(m.group("pat"))
            mapping = INJECTION_PATTERN_IDS_BY_VERSION[
                ACTIVE_INJECTION_PATTERN_SET_VERSION]
            pattern_id = mapping.get(pattern_source)
            if pattern_id is None:
                raise CertificateInvariantError(
                    "injection pattern is not in the active versioned "
                    "mapping; update tcs/provenance.py deliberately")
            records.append(C3ProvenanceRecord(
                schema_version=C3_PROVENANCE_SCHEMA_VERSION,
                source_type="injection_scan",
                pattern_id=pattern_id,
                pattern_set_version=ACTIVE_INJECTION_PATTERN_SET_VERSION,
                location_tag=m.group("loc"),
                connector_type="", detail_code="", producer_id="",
            ))

    cred_reason = meta.get("credential_reason")
    if meta.get("credential_detected") and isinstance(cred_reason, str):
        m = _CREDENTIAL_REASON_RE.match(cred_reason)
        if m:
            pattern_source = _parse_pattern_repr(m.group("pat"))
            mapping = CREDENTIAL_PATTERN_IDS_BY_VERSION[
                ACTIVE_CREDENTIAL_PATTERN_SET_VERSION]
            pattern_id = mapping.get(pattern_source)
            if pattern_id is None:
                raise CertificateInvariantError(
                    "credential pattern is not in the active versioned "
                    "mapping; update tcs/provenance.py deliberately")
            records.append(C3ProvenanceRecord(
                schema_version=C3_PROVENANCE_SCHEMA_VERSION,
                source_type="credential_detection",
                pattern_id=pattern_id,
                pattern_set_version=ACTIVE_CREDENTIAL_PATTERN_SET_VERSION,
                location_tag=m.group("loc"),
                connector_type="", detail_code="", producer_id="",
            ))

    if not records:
        raise CertificateInvariantError(
            "c3_score == 0.0000 but no structured signal explains it; "
            "supply explicit c3_provenance (e.g. a caller_supplied "
            "record with a nonempty producer_id)")
    records.sort(key=c3_record_sort_key)
    return records


# --------------------------------------------------------------------------- #
# v2 certificate construction — DORMANT until Commit 5                         #
# --------------------------------------------------------------------------- #

def generate_certificate_v2(
    tis_input: TISInput,
    tis_result: TISResult,
    decision: str,
    requires_human_review: bool,
    *,
    c3_provenance: Optional[List[C3ProvenanceRecord]] = None,
) -> TrustCertificate:
    """Generate a tis-v2 Trust Certificate.

    Accepts an EXPLICITLY SUPPLIED v2 TISResult — it never re-invokes
    the engine. Refuses to seal anything whose calculation_version is
    not "tis-v2". The non-numeric layers (identity, governance status,
    explanation, scope attestation, chain metadata) are built by the
    frozen v1 builder over float shadows; every numeric, gate, decision,
    provenance, and version field is then assigned EXPLICITLY from the
    v2 sources below, and the hash is recomputed through the validating
    v2 payload path — which independently replays the entire
    computation before any hash exists.

    Raw-evidence fidelity (owner delta 2): every TISInput dimension
    score must already be a finite Decimal in [0, 1]. A float-originated
    score is rejected rather than silently recorded as exact source
    evidence; Commit 5's Decimal-aware transport boundary removes this
    restriction for production API issuance.
    """
    if tis_result.calculation_version != CALCULATION_VERSION_V2:
        raise CertificateInvariantError(
            f"generate_certificate_v2 requires calculation_version "
            f"'tis-v2', got {tis_result.calculation_version!r}; "
            f"refusing to seal"
        )
    effective = tis_result.effective_dimension_scores
    if not effective:
        raise CertificateInvariantError(
            "TISResult missing effective_dimension_scores; refusing "
            "to seal")
    if set(effective) != _BACK_DIMS:
        raise CertificateInvariantError(
            "effective dimension scores incomplete")
    observed = tis_result.observed_dimension_scores
    if set(observed) != _BACK_DIMS:
        raise CertificateInvariantError(
            "observed dimension scores incomplete")

    if set(tis_input.dimension_scores) != _BACK_DIMS:
        raise CertificateInvariantError("raw dimension scores incomplete")
    for dim, value in tis_input.dimension_scores.items():
        if not isinstance(value, Decimal):
            raise CertificateInvariantError(
                f"component_scores_raw fidelity requires Decimal input "
                f"scores; dimension {dim!r} was supplied as "
                f"{type(value).__name__} — a binary float cannot be "
                f"recorded as exact source evidence"
            )
        if not value.is_finite() or value < 0 or value > 1:
            raise CertificateInvariantError(
                f"raw dimension score {dim!r} outside [0, 1]: {value}")

    profile = tis_input.policy_profile
    meta = tis_input.context_metadata

    # ---- Base construction via the frozen v1 builder (float shadows) ---- #
    shadow_input = _dataclass_replace(
        tis_input,
        dimension_scores={
            k: float(v) for k, v in tis_input.dimension_scores.items()
        },
    )
    shadow_result = _dataclass_replace(
        tis_result,
        s_base=float(tis_result.s_base),
        tis_raw=float(tis_result.tis_raw),
        penalty_breakdown={
            k: float(v) for k, v in tis_result.penalty_breakdown.items()
        },
        penalty_aggregate=float(tis_result.penalty_aggregate),
        s_adj=float(tis_result.s_adj),
        tis_adj=float(tis_result.tis_adj),
        C3_score=float(tis_result.C3_score),
        decay_factor=float(tis_result.decay_factor),
        tis_current=float(tis_result.tis_current),
        effective_dimension_scores={},
        observed_dimension_scores={},
        adjustments_applied=[],
        calculation_version="tis-v1",
    )
    tc = generate_certificate(
        shadow_input, shadow_result, decision, requires_human_review,
    )

    # ---- Typed provenance (issuance-time registry consultation) --------- #
    typed_matches = lift_governance_rule_matches(
        list(meta.get("governance_rule_matches") or [])
    )
    c3 = tis_result.C3_score
    if c3 == Decimal("0.0000"):
        if c3_provenance is not None:
            records = list(c3_provenance)
            for r in records:
                validate_c3_provenance_record(r)
            if not records:
                raise CertificateInvariantError(
                    "c3_score == 0.0000 requires at least one "
                    "C3ProvenanceRecord")
        else:
            records = derive_c3_provenance(meta, typed_matches)
    else:
        if c3_provenance:
            raise CertificateInvariantError(
                "c3_provenance must be empty when c3_score != 0.0000")
        records = []
    records.sort(key=c3_record_sort_key)

    # ---- Explicit v2 construction mapping ------------------------------- #
    tc.component_scores_raw = dict(tis_input.dimension_scores)
    tc.component_scores_observed = dict(observed)
    tc.component_scores = dict(effective)
    tc.component_weights = {
        dim: canonical_score(profile.weights[dim]) for dim in _BACK_DIMS
    }
    tc.thresholds = {
        dim: canonical_score(v) for dim, v in profile.thresholds.items()
    }
    tc.s_base = tis_result.s_base
    tc.s_adjusted = tis_result.s_adj          # name maps across the boundary
    tc.tis_raw = tis_result.tis_raw
    tc.tis_adjusted = tis_result.tis_adj      # name maps across the boundary
    tc.tis_current = tis_result.tis_current
    tc.penalty_aggregate = tis_result.penalty_aggregate
    tc.penalty_breakdown = dict(tis_result.penalty_breakdown)
    tc.resolved_penalty_weights = {
        k: canonical_score(v) for k, v in profile.penalty_weights.items()
    }
    tc.adjustments_applied = list(tis_result.adjustments_applied)
    tc.gate_result = int(tis_result.gate_result)
    tc.resolved_decay_rate = canonical_nonnegative_parameter(
        profile.decay_rate, field_name="resolved_decay_rate")
    tc.elapsed_hours = canonical_nonnegative_parameter(
        tis_input.elapsed_hours, field_name="elapsed_hours")
    tc.decay_factor = tis_result.decay_factor
    tc.resolved_theta_allow = canonical_score(profile.theta_allow)
    tc.resolved_theta_hold = canonical_score(profile.theta_hold)
    tc.resolved_theta_escalate = canonical_score(profile.theta_escalate)
    tc.resolved_kappa = canonical_score(profile.soft_hold_ceiling)
    tc.c3_score = c3
    tc.is_valid = int(tis_result.is_valid)
    tc.governance_rule_matches = typed_matches
    tc.c3_provenance = records

    # Version identifiers — explicit, from named constants; never
    # dataclass defaults.
    tc.certificate_schema_version = 2
    tc.calculation_version = tis_result.calculation_version
    tc.score_precision_policy = SCORE_PRECISION_POLICY
    tc.decay_algorithm_version = DECAY_ALGORITHM_VERSION
    tc.provenance_schema_version = C3_PROVENANCE_SCHEMA_VERSION

    # ---- Recompute the hash through the VALIDATING v2 path -------------- #
    prev_ai = tc.audit_integrity
    tc.audit_integrity = None
    tc_hash = compute_tc_hash(tc.to_dict())   # validates + replays first
    tc.audit_integrity = AuditIntegrity(
        tc_hash=tc_hash,
        previous_tc_hash=prev_ai.previous_tc_hash,
        chain_sequence=prev_ai.chain_sequence,
        chain_id=prev_ai.chain_id,
        hash_algorithm="sha256",
        integrity_verified=True,
        issued_by=prev_ai.issued_by,
    )
    return tc
