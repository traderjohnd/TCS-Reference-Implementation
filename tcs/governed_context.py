"""
tcs.governed_context
====================

Governed Context Architecture (GCA) stub.

In production, the GCA is the data plane for TCS: it assembles a governed
context object ``ρ`` (rho) from live sources (EHR, ERP, LIMS, market data, policy
stores, model registries, etc.) with end-to-end provenance, timestamped
boundary crossings, and policy-governed access. It is where the subject
under evaluation and its surrounding authoritative context are brought
together into a single auditable snapshot.

For Phase 1 v0.1, we do not connect to any real data plane. This module
is a thin wrapper that takes a scenario's ``context_metadata`` dict and
produces a normalized governed-context object with:

    - every penalty-input field present (with safe defaults where omitted)
    - stub provenance identifiers when the scenario doesn't provide real ones
    - a human-readable ``gca_snapshot_id`` for cross-reference in TCs
    - a captured-at timestamp (UTC)

The resulting dict is shaped so that ``tcs.tis_engine.compute_tis`` can
consume it directly as ``TISInput.context_metadata``. No computation
happens here — this is purely a context assembly and normalization step.

Phase 2 expands this module into a real data-plane adapter with pluggable
sources, provenance verification, and governed retrieval.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, FrozenSet, List, Optional, Tuple

from tcs.policy_profiles import DIMENSIONS, PolicyProfile, load_profile


# --------------------------------------------------------------------------- #
# Required context keys + safe defaults                                        #
# --------------------------------------------------------------------------- #
#
# These are the fields the penalty function in tcs.tis_engine expects to
# find in context_metadata. Keeping the default set here means every
# scenario that forgets a field gets a sensible, conservative value rather
# than a KeyError deep inside the penalty computation.

_REQUIRED_PENALTY_KEYS: Dict[str, Any] = {
    # P_cb input — count of integration-boundary provenance gaps.
    "n_gaps": 0,

    # P_d input — age of the governed context snapshot in hours.
    "context_age_hours": 0.0,

    # P_n input — subject novelty score in [0,1].
    "novelty_score": 0.0,

    # P_h input — days since the most recent human review of the subject.
    "days_since_review": 0,

    # P_ps input — whether the subject is policy-sensitive content.
    "is_policy_sensitive": False,
}


# Optional provenance fields. If the scenario provides them, they are passed
# through; otherwise the TC generator will create Phase-1 stub IDs.
_OPTIONAL_PROVENANCE_KEYS = (
    "checkpoint_id",
    "gca_context_id",
    "chain_of_custody_id",
    "audit_log_id",
    "source_references",
    "retrieval_ids",
    "blocking_context",
)


# --------------------------------------------------------------------------- #
# Fail-Safe Rules (TCS-TEL-001 §19 C-R.17)                                     #
# --------------------------------------------------------------------------- #
#
# When an evaluation component cannot run, the governance layer must choose
# a deterministic fail-safe outcome rather than raising an unhandled
# exception. Each failure type has a tier-indexed response (r1/r2/r3).
#
# The table below is copied verbatim from TCS_SPEC.md §19. Do not change
# the values without updating the spec first.
#
# Outcome vocabulary:
#     "allow_with_flag"       — pass through with a governance flag
#     "canonical_defaults"    — use canonical policy defaults
#     "allow_queue"           — allow now, queue for audit retry
#     "degraded_allow"        — allow with a "degraded" marker
#     "allow_max_flag"        — allow with the strongest governance flag
#     "hold"                  — pause for remediation / human review
#     "stop"                  — hard stop (no authorization)
#
# C-R.17 says: "No silent failure permitted. Governance failure must
# produce a TC." So the calling code turns an apply_fail_safe() result
# into a GovernanceStatus block (fail_safe_applied=True, fail_safe_type=
# <outcome>) and still issues a TC, rather than bubbling an exception.

FAIL_SAFE_RULES: Dict[str, Dict[str, str]] = {
    "dimension_missing":      {"r1": "allow_with_flag",     "r2": "hold", "r3": "stop"},
    "policy_unavailable":     {"r1": "canonical_defaults",  "r2": "hold", "r3": "stop"},
    "gca_failure":            {"r1": "allow_with_flag",     "r2": "hold", "r3": "stop"},
    "tc_write_failure":       {"r1": "allow_queue",         "r2": "hold", "r3": "stop"},
    "identity_provider_down": {"r1": "degraded_allow",      "r2": "hold", "r3": "stop"},
    "tcs_offline":            {"r1": "allow_max_flag",      "r2": "hold", "r3": "stop"},
}


class FailSafeLookupError(ValueError):
    """Raised when apply_fail_safe is called with an unknown failure/tier."""


def apply_fail_safe(failure_type: str, risk_tier: str) -> str:
    """
    Return the fail-safe outcome for a given failure type + risk tier.

    Parameters
    ----------
    failure_type
        One of the keys in ``FAIL_SAFE_RULES`` (``dimension_missing``,
        ``policy_unavailable``, ``gca_failure``, ``tc_write_failure``,
        ``identity_provider_down``, ``tcs_offline``).
    risk_tier
        ``"r1"``, ``"r2"``, or ``"r3"``.

    Returns
    -------
    str
        The fail-safe outcome string from ``FAIL_SAFE_RULES``.

    Raises
    ------
    FailSafeLookupError
        If ``failure_type`` or ``risk_tier`` is not in the table. This
        is itself a governance violation — an unknown failure type
        must be surfaced loudly, not silently mapped to a default.

    Notes
    -----
    This function is deliberately pure and side-effect-free. It does
    not emit a TC itself. The caller (typically the TC issuance path
    in a Phase 2 integration layer) is responsible for translating the
    returned outcome string into a GovernanceStatus block and attaching
    it to a TC per C-R.17 and C-R.20.

    In Phase 1 this function is defined and tested but not yet wired
    into the TC issuance pipeline, because none of the eight Phase 1
    scenarios exercise a fail-safe path. Phase 2 scenario 15 (degraded
    evaluation) will be the first caller.
    """
    rules = FAIL_SAFE_RULES.get(failure_type)
    if rules is None:
        raise FailSafeLookupError(
            f"Unknown failure_type {failure_type!r}. "
            f"Known types: {sorted(FAIL_SAFE_RULES.keys())}"
        )
    outcome = rules.get(risk_tier)
    if outcome is None:
        raise FailSafeLookupError(
            f"Unknown risk_tier {risk_tier!r} for failure_type "
            f"{failure_type!r}. Known tiers: {sorted(rules.keys())}"
        )
    return outcome


# --------------------------------------------------------------------------- #
# Public API                                                                   #
# --------------------------------------------------------------------------- #

def assemble_context(
    metadata: Optional[Dict[str, Any]] = None,
    *,
    captured_at: Optional[datetime] = None,
) -> Dict[str, Any]:
    """
    Assemble a governed context object ``ρ`` (rho) from scenario metadata.

    Parameters
    ----------
    metadata
        Raw context metadata from a test scenario. May be ``None`` or an
        empty dict — defaults are applied for every required penalty key.
    captured_at
        Timestamp at which the context was assembled. Defaults to
        ``datetime.now(UTC)``. In production this would be the actual
        retrieval time from the upstream data plane.

    Returns
    -------
    dict
        A normalized context dict with every penalty-input key present,
        provenance fields passed through (or stubbed), and two audit
        fields added by this module:

            - ``gca_snapshot_id``: UUID for cross-reference from TCs
            - ``captured_at``: ISO-8601 UTC timestamp

        The returned dict is a new object — the caller's ``metadata`` is
        not mutated.

    Notes
    -----
    This function does not validate the values it receives (e.g. it does
    not check that ``novelty_score`` is in [0,1]). Validation is the
    responsibility of ``tcs.tis_engine._validate_inputs``, which runs at
    compute time. Keeping validation in one place avoids duplicate rules
    drifting apart.
    """
    meta = dict(metadata or {})

    # Fill in any missing penalty input with its safe default. An explicit
    # ``None`` is treated the same as a missing key.
    for key, default in _REQUIRED_PENALTY_KEYS.items():
        if meta.get(key) is None:
            meta[key] = default

    # Pass through any optional provenance fields as-is.
    # (They are already in meta if present; this loop is defensive — it
    # ensures the dict has them as explicit keys when they are passed in
    # so downstream code can rely on presence checks.)
    for key in _OPTIONAL_PROVENANCE_KEYS:
        if key in meta and meta[key] is None:
            del meta[key]

    # Stamp the snapshot with its assembly audit fields.
    meta.setdefault("gca_snapshot_id", f"gca-{uuid.uuid4().hex[:12]}")
    if "captured_at" not in meta:
        ts = captured_at if captured_at is not None else datetime.now(timezone.utc)
        meta["captured_at"] = ts.strftime("%Y-%m-%dT%H:%M:%SZ")

    return meta


# --------------------------------------------------------------------------- #
# Safe assembly wrapper (TCS-TEL-001 §19 C-R.17)                               #
# --------------------------------------------------------------------------- #

def safe_assemble_context(
    metadata: Optional[Dict[str, Any]],
    risk_tier: str,
    *,
    captured_at: Optional[datetime] = None,
) -> Dict[str, Any]:
    """
    Assemble a governed context with C-R.17 fail-safe behavior.

    This is the Phase-2-ready entry point for callers that want a
    deterministic fail-safe outcome rather than a raised exception
    when an evaluation component fails.

    Behavior:
        - On success: returns the same dict as ``assemble_context()``
          with two extra keys set to neutral values (fail_safe_applied
          = False, fail_safe_outcome = None).

        - On failure: returns a context dict that still has every
          required penalty key present (using safe defaults), PLUS:
            * ``fail_safe_applied``:  True
            * ``fail_safe_type``:     the failure category
            * ``fail_safe_outcome``:  the string from FAIL_SAFE_RULES
                                      for (failure_type, risk_tier)
            * ``fail_safe_reason``:   the underlying exception repr
          Downstream code (the TC issuance path) reads these fields
          and sets the GovernanceStatus layer to ``degraded`` (or
          ``failed``, depending on outcome) per C-R.17 and C-R.20.

    Parameters
    ----------
    metadata
        Raw scenario metadata, same as ``assemble_context()``.
    risk_tier
        ``"r1"``, ``"r2"``, or ``"r3"``. Determines the fail-safe
        outcome row used from ``FAIL_SAFE_RULES``.
    captured_at
        Optional capture timestamp, same as ``assemble_context()``.

    Notes
    -----
    In Phase 1 this wrapper is defined but not called by the eight
    passing scenarios — they all produce successful assembly and run
    through ``assemble_context()`` directly. Phase 2 scenario 15
    (degraded evaluation) is the first caller. Keeping the wrapper
    here in Phase 1 means the fail-safe code path is fully implemented
    and reviewable, not just stubbed out.

    C-R.17: "No silent failure permitted. Governance failure must
    produce a TC." This wrapper satisfies the no-silent-failure half
    of the rule by always returning a structured context — even on
    failure. The TC-producing half is handled by the caller that
    consumes the returned dict.
    """
    try:
        ctx = assemble_context(metadata, captured_at=captured_at)
        ctx["fail_safe_applied"] = False
        ctx["fail_safe_type"] = None
        ctx["fail_safe_outcome"] = None
        ctx["fail_safe_reason"] = None
        return ctx
    except Exception as exc:
        # Context assembly raised. Classify as gca_failure (the most
        # general bucket — Phase 2 will add finer categorization based
        # on exception type). Apply the tier-indexed fail-safe rule.
        failure_type = "gca_failure"
        outcome = apply_fail_safe(failure_type, risk_tier)

        # Produce a minimally-valid context dict so that downstream
        # code does not itself crash on missing penalty keys.
        fallback: Dict[str, Any] = dict(_REQUIRED_PENALTY_KEYS)
        fallback["gca_snapshot_id"] = f"gca-failsafe-{uuid.uuid4().hex[:12]}"
        fallback["captured_at"] = (
            (captured_at or datetime.now(timezone.utc))
            .strftime("%Y-%m-%dT%H:%M:%SZ")
        )
        fallback["fail_safe_applied"] = True
        fallback["fail_safe_type"] = failure_type
        fallback["fail_safe_outcome"] = outcome
        fallback["fail_safe_reason"] = repr(exc)
        return fallback


# =========================================================================== #
# Phase 2 — Connection-Aware Trust Computation (TCS-CATC-001, TCS-PHASE2-001)  #
# =========================================================================== #
#
# Everything below this line is additive Phase-2 scaffolding. The Phase-1
# entry point ``assemble_context()`` and ``safe_assemble_context()`` above
# are unchanged and continue to power the 108 Phase-1 tests via their
# existing context_metadata shape.
#
# The Phase-2 pipeline (assemble_context_v2) is fired when the metadata
# describes a RAG-style retrieval (``retrieved_chunks`` present) or when
# the caller asks for CT-aware assembly explicitly via
# ``assemble_context_v2()``. It performs the full TCS-CATC-001 sequence:
#
#     1. detect_connection_type(metadata)          -> "CT-1".."CT-13"
#     2. if CT-12: raise CredentialDetectedError   (never reaches TIS)
#     3. validate_mcp_server_identity(metadata)    (stub in Phase 2)
#     4. classify_sensitivity_tier(metadata)       -> "T1".."T3"
#     5. check_response_injection(metadata)        -> C3 signal
#     6. count_attribution_gaps(metadata)          -> n_gaps -> P_cb
#     7. freeze_context(rho)                       -> rho' (C-R.14)
#     8. scope_attestation populated in the returned dict
#     9. resolve_policy_profile(base, ct)          -> ResolvedTISProfile
#
# The result is a context_metadata dict compatible with TISInput plus a
# ResolvedTISProfile the caller can pass to the TIS engine as the
# policy_profile. Both are returned from assemble_context_v2() as a
# tuple so the caller decides which half to consume.


# --------------------------------------------------------------------------- #
# CT_WEIGHT_MODIFIERS (TCS_SPEC.md §18)                                        #
# --------------------------------------------------------------------------- #
#
# Each CT row is a dimension-weight delta applied to the base profile.
# Every row sums to 0.00 — redistribution, not addition — so the
# Sigma-w = 1.0 invariant is preserved after resolution.
#
# CT-12 ("Credentials") is deliberately None. If credentials are
# detected in the governed context, the resolution layer raises
# CredentialDetectedError and the evaluation never reaches the TIS
# engine. C3 is forced to 0.00 in the decision path.

CT_WEIGHT_MODIFIERS: Dict[str, Optional[Dict[str, float]]] = {
    "CT-1":  {"B": +0.08, "A": +0.02, "C":  0.00, "K": -0.10},  # API
    "CT-2":  {"B": -0.05, "A":  0.00, "C": +0.10, "K": -0.05},  # Database
    "CT-3":  {"B": -0.05, "A": +0.10, "C":  0.00, "K": -0.05},  # Documents
    "CT-4":  {"B": -0.05, "A": +0.05, "C": -0.05, "K": +0.05},  # Vector DB / RAG
    "CT-5":  {"B":  0.00, "A": +0.05, "C": -0.05, "K":  0.00},  # Streaming
    "CT-6":  {"B": -0.10, "A": +0.05, "C": +0.05, "K":  0.00},  # Web
    "CT-7":  {"B":  0.00, "A": +0.05, "C": -0.05, "K":  0.00},  # Human
    "CT-8":  {"B":  0.00, "A":  0.00, "C":  0.00, "K":  0.00},  # Agent chain
    "CT-9":  {"B": -0.05, "A":  0.00, "C": -0.05, "K": +0.10},  # Sensor
    "CT-10": {"B": +0.08, "A": -0.03, "C": -0.03, "K": -0.02},  # Memory
    "CT-11": {"B": -0.05, "A": +0.08, "C": -0.03, "K":  0.00},  # AI-generated
    "CT-12": None,                                                # Credentials: STOP
    "CT-13": {"B": -0.05, "A": +0.08, "C": -0.03, "K":  0.00},  # Multimodal
}


#: Human-readable description of each CT for diagnostics and TC audit.
CT_DESCRIPTIONS: Dict[str, str] = {
    "CT-1":  "API",
    "CT-2":  "Database",
    "CT-3":  "Documents",
    "CT-4":  "Vector DB / RAG retrieval",
    "CT-5":  "Streaming data",
    "CT-6":  "Web source",
    "CT-7":  "Human input",
    "CT-8":  "Agent chain",
    "CT-9":  "Sensor / telemetry",
    "CT-10": "Memory / cached context",
    "CT-11": "AI-generated content",
    "CT-12": "Credentials (STOP)",
    "CT-13": "Multimodal",
}


# --------------------------------------------------------------------------- #
# Credential detection                                                         #
# --------------------------------------------------------------------------- #

class CredentialDetectedError(RuntimeError):
    """
    Raised by context assembly when a credential-like token is detected
    in the governed context (CT-12). Credentials must never reach the
    TIS engine — the caller catches this and emits a hard Stop TC with
    C3=0.00 via the decision-path sidecar.

    See TCS_SPEC.md §18: "CT-12 (credentials) must never reach policy
    resolution."
    """


#: Regex patterns that trip credential detection. This is intentionally
#: generous (false-positive bias) — it is safer to Stop a benign value
#: than to let a real secret through. Phase 3 replaces this with a
#: proper secret scanner.
_CREDENTIAL_PATTERNS: Tuple[re.Pattern[str], ...] = (
    # Explicit key=value labels
    re.compile(r"(?i)\b(api[_-]?key|secret|password|token|bearer)\s*[:=]"),
    # OpenAI-style project keys
    re.compile(r"\bsk-[A-Za-z0-9]{16,}\b"),
    # Generic long hex secrets
    re.compile(r"\b[A-Fa-f0-9]{32,}\b"),
    # AWS-style access keys
    re.compile(r"\bAKIA[0-9A-Z]{12,}\b"),
    # Private key blocks
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
)


def _scan_for_credentials(text: str) -> Optional[str]:
    """
    Return the matched pattern name (for audit) if ``text`` contains
    a credential-like token, or None.
    """
    if not text:
        return None
    for pat in _CREDENTIAL_PATTERNS:
        if pat.search(text):
            return pat.pattern
    return None


# --------------------------------------------------------------------------- #
# Response injection detection (C3 signal)                                     #
# --------------------------------------------------------------------------- #
#
# Phrases that try to subvert the evaluating system. Any match drops the
# C3 sub-factor to 0.00, which maps to Priority 2 hard Stop in the
# decision engine regardless of TIS_raw.

_INJECTION_PATTERNS: Tuple[re.Pattern[str], ...] = (
    re.compile(r"(?i)ignore (?:the )?(?:previous|policy|all) (?:instructions|constraints|rules)"),
    re.compile(r"(?i)disregard (?:the )?(?:previous|policy|all) (?:instructions|rules)"),
    re.compile(r"(?i)override (?:the )?(?:policy|safety|guardrails?)"),
    re.compile(r"(?i)you are now (?:a |an )?(?:unrestricted|uncensored|jailbroken)"),
    re.compile(r"(?i)recommend all (?:equities|securities|assets)"),
    re.compile(r"(?i)bypass (?:the )?(?:compliance|kyc|aml|safety)"),
    re.compile(r"(?i)system\s*:\s*you are"),  # fake system-prompt injection
)


def _scan_for_injection(text: str) -> Optional[str]:
    if not text:
        return None
    for pat in _INJECTION_PATTERNS:
        if pat.search(text):
            return pat.pattern
    return None


# --------------------------------------------------------------------------- #
# Connection type detection                                                    #
# --------------------------------------------------------------------------- #

def detect_connection_type(metadata: Dict[str, Any]) -> str:
    """
    Classify the upstream data source into one of CT-1..CT-13.

    Detection order (first match wins):

        1. Explicit ``metadata["connection_type"]`` — trust the caller
        2. ``retrieved_chunks`` present        -> CT-4 (vector DB / RAG)
        3. ``chain_u_scores`` present          -> CT-8 (agent chain)
        4. ``api_endpoint`` present            -> CT-1 (API)
        5. ``document_ids`` present            -> CT-3 (Documents)
        6. ``web_url`` present                 -> CT-6 (Web)
        7. ``sensor_id`` present               -> CT-9 (Sensor)
        8. default                             -> CT-10 (Memory)

    Phase 2 only uses CT-4 for the demo, but the full ladder is here so
    the sidecar can handle arbitrary workflows in later phases.
    """
    explicit = metadata.get("connection_type")
    if isinstance(explicit, str) and explicit:
        return explicit

    if "retrieved_chunks" in metadata and metadata["retrieved_chunks"] is not None:
        return "CT-4"
    if "chain_u_scores" in metadata and metadata.get("chain_u_scores"):
        return "CT-8"
    if "api_endpoint" in metadata and metadata["api_endpoint"]:
        return "CT-1"
    if "document_ids" in metadata and metadata["document_ids"]:
        return "CT-3"
    if "web_url" in metadata and metadata["web_url"]:
        return "CT-6"
    if "sensor_id" in metadata and metadata["sensor_id"]:
        return "CT-9"

    return "CT-10"


# --------------------------------------------------------------------------- #
# Sensitivity tier classification                                              #
# --------------------------------------------------------------------------- #

def classify_sensitivity_tier(metadata: Dict[str, Any]) -> str:
    """
    Classify the governed context's sensitivity as T1 / T2 / T3.

    Phase 2 heuristic (in precedence order):

        * metadata["sensitivity_tier"] wins if set
        * any chunk tagged "regulated" or "pii" -> T3
        * any chunk tagged "internal"           -> T2
        * else                                  -> T1

    Phase 3 replaces this with a proper classifier driven by the
    deployment data-sensitivity manifest.
    """
    explicit = metadata.get("sensitivity_tier")
    if explicit in ("T1", "T2", "T3"):
        return explicit

    chunks = metadata.get("retrieved_chunks") or []
    highest = "T1"
    for c in chunks:
        tags = {t.lower() for t in (c.get("tags") or [])}
        if {"regulated", "pii", "hipaa", "pci", "phi"} & tags:
            return "T3"
        if "internal" in tags or "confidential" in tags:
            highest = "T2"
    return highest


# --------------------------------------------------------------------------- #
# Attribution-gap counting (feeds n_gaps -> P_cb)                              #
# --------------------------------------------------------------------------- #

def count_attribution_gaps(metadata: Dict[str, Any]) -> int:
    """
    Count chunks missing attribution metadata. Each missing-metadata
    chunk counts as one integration boundary gap, which feeds the P_cb
    penalty in the TIS engine.

    A chunk has a "gap" if any of the following are missing or None:
        - source_doc
        - version

    Chunks that set both to real values (even "unknown") contribute 0.
    """
    chunks = metadata.get("retrieved_chunks") or []
    gaps = 0
    for c in chunks:
        if not c.get("source_doc"):
            gaps += 1
            continue
        if not c.get("version"):
            gaps += 1
    return gaps


# --------------------------------------------------------------------------- #
# Response-injection / credential scan                                         #
# --------------------------------------------------------------------------- #

def check_response_injection(metadata: Dict[str, Any]) -> Dict[str, Any]:
    """
    Scan every retrieved chunk's content for injection or credential
    patterns. Returns a diagnostic dict consumed by ``assemble_context_v2``.

    Returned keys:
        c3_score            : 1.0 clean, 0.0 if any pattern hit
        injection_detected  : bool
        credential_detected : bool
        injection_reason    : str or None
        credential_reason   : str or None

    Caller is responsible for raising CredentialDetectedError on a
    credential hit — this function is pure classification.
    """
    chunks = metadata.get("retrieved_chunks") or []
    injection_reason: Optional[str] = None
    credential_reason: Optional[str] = None

    for c in chunks:
        text = str(c.get("content") or c.get("text") or "")
        if injection_reason is None:
            hit = _scan_for_injection(text)
            if hit:
                injection_reason = (
                    f"chunk_id={c.get('chunk_id')}: injection pattern {hit!r}"
                )
        if credential_reason is None:
            hit = _scan_for_credentials(text)
            if hit:
                credential_reason = (
                    f"chunk_id={c.get('chunk_id')}: credential pattern {hit!r}"
                )

    # Also scan top-level free text if present (e.g. a combined prompt).
    for key in ("prompt", "user_query", "free_text"):
        text = str(metadata.get(key) or "")
        if injection_reason is None:
            hit = _scan_for_injection(text)
            if hit:
                injection_reason = f"{key}: injection pattern {hit!r}"
        if credential_reason is None:
            hit = _scan_for_credentials(text)
            if hit:
                credential_reason = f"{key}: credential pattern {hit!r}"

    any_hit = bool(injection_reason or credential_reason)
    return {
        "c3_score": 0.00 if any_hit else 1.00,
        "injection_detected": bool(injection_reason),
        "credential_detected": bool(credential_reason),
        "injection_reason": injection_reason,
        "credential_reason": credential_reason,
    }


# --------------------------------------------------------------------------- #
# MCP server identity (stub)                                                   #
# --------------------------------------------------------------------------- #

def validate_mcp_server_identity(metadata: Dict[str, Any]) -> Dict[str, Any]:
    """
    Phase 2 stub for MCP server identity validation.

    In a real deployment this would verify a signed attestation from the
    MCP server against a deployment manifest. For Phase 2 we accept
    whatever identity the caller provides and return a small report
    that downstream code writes into the scope_attestation block.

    Returns:
        {
            "mcp_server_id":    str,
            "verified":         bool,
            "verification_note": str,
        }
    """
    server_id = metadata.get("mcp_server_id") or "mcp-phase2-stub"
    return {
        "mcp_server_id": server_id,
        "verified": True,
        "verification_note": (
            "Phase 2 stub: server identity not cryptographically verified"
        ),
    }


# --------------------------------------------------------------------------- #
# Context freeze (C-R.14)                                                      #
# --------------------------------------------------------------------------- #

def freeze_context(
    rho: Dict[str, Any],
    *,
    frozen_at: Optional[datetime] = None,
) -> Dict[str, Any]:
    """
    Return a frozen copy of the governed context ``ρ`` (rho) with a
    ``context_frozen_at`` stamp.

    After this call, any MCP retrieval that adds to or mutates the
    context is a C-R.14 violation (``context_expansion`` invalidation
    event). This function itself does not enforce that — it just
    records the freeze point. Enforcement happens at the sidecar layer
    when it compares subsequent retrievals against the frozen_at stamp.
    """
    ts = frozen_at if frozen_at is not None else datetime.now(timezone.utc)
    frozen = dict(rho)
    frozen["context_frozen_at"] = ts.strftime("%Y-%m-%dT%H:%M:%SZ")
    frozen["context_is_frozen"] = True
    return frozen


# --------------------------------------------------------------------------- #
# ResolvedTISProfile and resolve_policy_profile                                #
# --------------------------------------------------------------------------- #

@dataclass
class ResolvedTISProfile:
    """
    Output of the GCA policy resolution step. Mirrors TCS_SPEC.md §18
    ``ResolvedTISProfile`` with the Phase-2 field set.

    A ResolvedTISProfile quacks like a PolicyProfile from the TIS
    engine's perspective — it carries the same ``weights``,
    ``thresholds``, ``gate_set``, ``penalty_weights``, ``decay_rate``,
    ``soft_hold_ceiling`` and ``decision_thresholds`` fields. The
    engine can consume it directly without knowing it was resolved
    from a base profile + CT modifier.

    Extra fields vs PolicyProfile:
        connection_type         — "CT-1".."CT-13" identifier
        modifier_id             — versioned CT modifier set ID
        resolved_profile_id     — composite audit ID for the TC
        use_chain_uncertainty   — True for CT-8 / CT-11
        chain_depth             — hops in agent/AI chain (default 0)
        chain_u_scores          — per-hop U_i list (default [])
        base_profile_id         — original base profile id for audit
        domain / risk_tier / action_class / invalidation_triggers /
        regulatory_mapping / profile_id — straight passthroughs
    """
    profile_id: str
    base_profile_id: str
    connection_type: str
    modifier_id: str
    resolved_profile_id: str

    domain: str
    risk_tier: str
    action_class: str

    gate_set: FrozenSet[str]
    thresholds: Dict[str, float]
    weights: Dict[str, float]
    penalty_weights: Dict[str, float]
    decay_rate: float
    soft_hold_ceiling: float
    decision_thresholds: Dict[str, float]

    invalidation_triggers: List[str]
    regulatory_mapping: List[str]
    description: str = ""

    use_chain_uncertainty: bool = False
    chain_depth: int = 0
    chain_u_scores: List[float] = field(default_factory=list)

    # Convenience accessors (match PolicyProfile API)
    @property
    def theta_allow(self) -> float:
        return self.decision_thresholds["theta_allow"]

    @property
    def theta_hold(self) -> float:
        return self.decision_thresholds["theta_hold"]

    @property
    def theta_escalate(self) -> float:
        return self.decision_thresholds["theta_escalate"]

    def validate(self) -> None:
        assert abs(sum(self.weights.values()) - 1.0) < 1e-9, \
            f"weights sum != 1.0: {self.weights}"
        assert abs(sum(self.penalty_weights.values()) - 1.0) < 1e-9, \
            f"penalty_weights sum != 1.0: {self.penalty_weights}"
        assert self.connection_type != "CT-12", \
            "CT-12 (credentials) must never reach policy resolution"


#: Version tag for the CT modifier table. Bumped whenever CT_WEIGHT_MODIFIERS
#: changes. Written into every ResolvedTISProfile and surfaces in the TC.
CT_MODIFIER_ID = "ct-modifiers-v1-2026-04"


def resolve_policy_profile(
    base_profile: PolicyProfile,
    connection_type: str,
    *,
    chain_u_scores: Optional[List[float]] = None,
) -> ResolvedTISProfile:
    """
    Apply CT-specific modifiers to a base PolicyProfile and return a
    ResolvedTISProfile.

    CT-12 (credentials) raises CredentialDetectedError immediately —
    credentials never reach the TIS engine under any circumstances.

    The resolved profile preserves every non-weight field from the
    base profile. Weights are modified by ``CT_WEIGHT_MODIFIERS[ct]``
    entry-wise, and the Sigma=1.0 invariant is validated before
    returning. CT-8 and CT-11 additionally set
    ``use_chain_uncertainty=True`` and carry the provided
    ``chain_u_scores``.

    Parameters
    ----------
    base_profile
        A fully-validated PolicyProfile loaded from policy_profiles.
    connection_type
        One of "CT-1".."CT-13" (or "CT-12" to trigger stop).
    chain_u_scores
        Per-hop uncertainty scores for CT-8 / CT-11 chains. Ignored
        for other connection types.

    Raises
    ------
    CredentialDetectedError
        If ``connection_type == "CT-12"``.
    ValueError
        If ``connection_type`` is not a known CT identifier.
    """
    if connection_type == "CT-12":
        raise CredentialDetectedError(
            "CT-12 credential detected — must Stop before TIS resolution"
        )
    if connection_type not in CT_WEIGHT_MODIFIERS:
        raise ValueError(
            f"Unknown connection_type {connection_type!r}. "
            f"Known: {sorted(CT_WEIGHT_MODIFIERS.keys())}"
        )

    modifiers = CT_WEIGHT_MODIFIERS[connection_type] or {}

    resolved_weights: Dict[str, float] = {
        dim: base_profile.weights[dim] + float(modifiers.get(dim, 0.0))
        for dim in DIMENSIONS
    }

    # Numerical sanity — modifiers sum to 0.00 by construction, but we
    # enforce the post-condition so a future typo can never silently
    # produce Sigma != 1.0.
    total = sum(resolved_weights.values())
    if abs(total - 1.0) > 1e-9:
        raise ValueError(
            f"Resolved weights sum to {total}, not 1.0. "
            f"base={base_profile.weights} modifiers={modifiers}"
        )

    resolved_profile_id = (
        f"{base_profile.profile_id}::{connection_type}::{CT_MODIFIER_ID}"
    )

    use_chain = connection_type in ("CT-8", "CT-11")
    chain_scores = list(chain_u_scores or [])

    resolved = ResolvedTISProfile(
        profile_id=base_profile.profile_id,
        base_profile_id=base_profile.profile_id,
        connection_type=connection_type,
        modifier_id=CT_MODIFIER_ID,
        resolved_profile_id=resolved_profile_id,

        domain=base_profile.domain,
        risk_tier=base_profile.risk_tier,
        action_class=base_profile.action_class,

        gate_set=base_profile.gate_set,
        thresholds=dict(base_profile.thresholds),
        weights=resolved_weights,
        penalty_weights=dict(base_profile.penalty_weights),
        decay_rate=base_profile.decay_rate,
        soft_hold_ceiling=base_profile.soft_hold_ceiling,
        decision_thresholds=dict(base_profile.decision_thresholds),

        invalidation_triggers=list(base_profile.invalidation_triggers),
        regulatory_mapping=list(base_profile.regulatory_mapping),
        description=base_profile.description,

        use_chain_uncertainty=use_chain,
        chain_depth=len(chain_scores),
        chain_u_scores=chain_scores,
    )
    resolved.validate()
    return resolved


def compute_chain_uncertainty(u_scores: List[float]) -> float:
    """
    Chain uncertainty formula for CT-8 and CT-11 (TCS_SPEC.md §18).

        U_chain = 1 - prod(U_i)

    Treats each U_i as a per-hop reliability score; chain reliability
    is the product; chain uncertainty is 1 minus that.

    Returns 0.0 on an empty list (no chain = no chain uncertainty).
    """
    if not u_scores:
        return 0.0
    product = 1.0
    for u in u_scores:
        product *= float(u)
    return 1.0 - product


# --------------------------------------------------------------------------- #
# assemble_context_v2 — the Phase-2 CT-aware pipeline                          #
# --------------------------------------------------------------------------- #

def assemble_context_v2(
    metadata: Optional[Dict[str, Any]] = None,
    *,
    base_profile: Optional[PolicyProfile] = None,
    base_profile_id: Optional[str] = None,
    captured_at: Optional[datetime] = None,
) -> Tuple[Dict[str, Any], ResolvedTISProfile]:
    """
    Full Phase 2 CT-aware context assembly.

    Runs the TCS-CATC-001 sequence and returns:
        (context_metadata_dict, resolved_policy_profile)

    Parameters
    ----------
    metadata
        Raw metadata from the request_interceptor / RAG adapter.
        Expected shape (all optional except as noted):
            {
                "retrieved_chunks": [{"chunk_id","source_doc","version",
                                       "similarity_score","content","tags"}, ...],
                "prompt":           str,
                "mcp_server_id":    str,
                "sensitivity_tier": "T1"|"T2"|"T3",
                "connection_type":  "CT-4" (override detection),
                ... plus any Phase-1 context_metadata keys
            }
    base_profile / base_profile_id
        Either a pre-loaded PolicyProfile or a profile_id to load. One
        is required.
    captured_at
        Assembly timestamp (default: now UTC).

    Returns
    -------
    (dict, ResolvedTISProfile)
        The dict is suitable as TISInput.context_metadata. It contains:
            * every Phase-1 required penalty key (with computed values)
            * c3_score                 (post-injection-scan)
            * sensitivity_tier         (post-classification)
            * context_frozen_at / context_is_frozen
            * scope_attestation fields pre-populated for the TC
            * gca_snapshot_id + captured_at

    Raises
    ------
    CredentialDetectedError
        If any retrieved chunk matches a credential pattern, or if
        the detected connection_type is CT-12.
    """
    if base_profile is None:
        if base_profile_id is None:
            raise ValueError(
                "assemble_context_v2 requires base_profile or base_profile_id"
            )
        base_profile = load_profile(base_profile_id)

    meta_in = dict(metadata or {})

    # --- Step 1: detect connection type --------------------------------- #
    connection_type = detect_connection_type(meta_in)

    # --- Step 2: credential check / CT-12 short circuit ----------------- #
    # We run the chunk-content scan BEFORE resolve_policy_profile so a
    # credential in the chunk body short-circuits even when the caller
    # set connection_type to CT-4.
    injection_report = check_response_injection(meta_in)
    if injection_report["credential_detected"]:
        raise CredentialDetectedError(
            f"Credential detected in context: "
            f"{injection_report['credential_reason']}"
        )
    if connection_type == "CT-12":
        raise CredentialDetectedError(
            "connection_type=CT-12 (credentials) — hard Stop required"
        )

    # --- Step 3: MCP server identity ------------------------------------ #
    mcp_identity = validate_mcp_server_identity(meta_in)

    # --- Step 4: sensitivity tier classification ------------------------ #
    sensitivity_tier = classify_sensitivity_tier(meta_in)

    # --- Step 5: attribution-gap counting ------------------------------- #
    n_gaps = count_attribution_gaps(meta_in)

    # --- Step 6: policy resolution (CT modifiers applied) --------------- #
    chain_scores = list(meta_in.get("chain_u_scores") or [])
    resolved = resolve_policy_profile(
        base_profile,
        connection_type,
        chain_u_scores=chain_scores,
    )

    # --- Step 7: build the context dict --------------------------------- #
    # Start from whatever the caller supplied, then override the Phase-1
    # penalty-input keys with computed values. Values the caller set
    # explicitly for penalty inputs are respected only if we did not
    # compute a real value here. (n_gaps is always computed if chunks
    # are present; context_age_hours is a pass-through.)
    ctx: Dict[str, Any] = dict(meta_in)

    # Required penalty keys with defaults
    for key, default in _REQUIRED_PENALTY_KEYS.items():
        if ctx.get(key) is None:
            ctx[key] = default

    # Computed overrides
    if "retrieved_chunks" in meta_in:
        ctx["n_gaps"] = n_gaps

    # The c3_score here is consumed by the decision path via the TC
    # failing_dimension_subfactors block. We store it in context_metadata
    # under the conventional sub_factor shape so the caller can lift it
    # into TISInput.sub_factor_scores without re-classifying.
    ctx["c3_score_computed"] = injection_report["c3_score"]
    ctx["injection_detected"] = injection_report["injection_detected"]
    ctx["injection_reason"] = injection_report["injection_reason"]
    ctx["sensitivity_tier"] = sensitivity_tier

    # Connection-type audit trail for the TC
    ctx["connection_type"] = connection_type
    ctx["connection_type_modifier_id"] = resolved.modifier_id
    ctx["resolved_policy_profile_id"] = resolved.resolved_profile_id
    if chain_scores:
        ctx["chain_depth"] = len(chain_scores)
        ctx["chain_u_scores"] = chain_scores

    # MCP server identity
    ctx["mcp_server_id"] = mcp_identity["mcp_server_id"]
    ctx.setdefault(
        "mcp_servers_in_scope", [mcp_identity["mcp_server_id"]]
    )
    ctx.setdefault("mcp_servers_out_of_scope", [])
    ctx.setdefault("enforcement_perimeter_complete", True)
    ctx.setdefault("attestation_basis", "phase2-rag-manifest-v1")

    # --- Step 8: freeze the context (C-R.14) ---------------------------- #
    frozen_at = captured_at if captured_at is not None else datetime.now(timezone.utc)
    ctx = freeze_context(ctx, frozen_at=frozen_at)
    ctx["captured_at"] = frozen_at.strftime("%Y-%m-%dT%H:%M:%SZ")
    ctx.setdefault("gca_snapshot_id", f"gca-{uuid.uuid4().hex[:12]}")

    # --- Step 9: sub_factor_scores convenience -------------------------- #
    # Caller may also want sub_factor_scores ready-made for TISInput.
    # We only set C3 — other sub-factors are the caller's problem.
    existing_sub = ctx.get("sub_factor_scores") or {}
    c_sub = dict(existing_sub.get("C") or {})
    c_sub.setdefault("C3", injection_report["c3_score"])
    existing_sub = dict(existing_sub)
    existing_sub["C"] = c_sub
    ctx["sub_factor_scores"] = existing_sub

    return ctx, resolved

