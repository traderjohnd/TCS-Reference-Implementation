"""
tcs.governed_metadata
=====================

The protected-metadata contract for public API surfaces (tis-v2
Commit 5a, owner decision 2).

Externally supplied metadata must never be able to override or supply
authoritative scoring, validity, gating, C3, decision, threshold,
penalty, identity-trust, authorization, provenance, or enforcement
inputs. This module is the single source of truth for:

    * PROTECTED_METADATA_KEYS   — exact keys, traced from every
      production consumption site (``.get``/subscript/``pop``/
      ``setdefault``/model construction) across ``tcs/``;
    * PROTECTED_METADATA_PREFIXES — prefix families, so a NEWLY
      consumed governance-sensitive key in an existing family is
      rejected before anyone remembers to add it to the exact list;
    * :func:`find_protected_keys` — a recursive scanner that inspects
      nested dicts and lists at any depth, with case- and
      separator-insensitive matching, so nested or alias-shaped
      smuggling is rejected without rejecting unrelated structured
      metadata.

Identity attestations (requesting_identity, identity_confidence,
sensitivity_tier, ...) have exactly one public channel: the TYPED
request fields on the route body. The same names inside free-form
``extra_metadata`` are rejected — two channels for one authoritative
value is how override surfaces are born.

Internal trusted producers (demos, tests, adapters above the HTTP
boundary) supply governed metadata through ``RAGOutput.governed_
metadata`` — a channel the public route never populates.
"""

from __future__ import annotations

from typing import Any, FrozenSet, List, Tuple

#: Exact protected keys, by influence class. Traced from the
#: consumption inventory across tcs/ (see tests/test_protected_metadata
#: for the guard that pins this against the live consumption sites).
PROTECTED_METADATA_KEYS: FrozenSet[str] = frozenset({
    # --- BACK / sub-factor / C3 / gate inputs ---------------------------- #
    "b_score", "a_score", "c_score", "k_score",
    "k_subfactor_penalty", "chain_u_scores", "chain_depth",
    "sub_factor_scores",
    "c3_score_computed", "c3_signals",
    "injection_detected", "injection_reason",
    "credential_detected", "credential_reason", "credential_pattern",
    "blocking_context",
    # --- validity / decision --------------------------------------------- #
    "is_valid", "invalidation_event", "elapsed_hours",
    # execution-mode provenance is set server-side from the operating
    # mode (demo-live branch) — callers must not forge it
    "execution_mode",
    "action_partially_executed", "compensation_scope", "incident_id",
    "redaction_required", "redacted_fields", "redaction_scope",
    # --- penalty inputs --------------------------------------------------- #
    "n_gaps", "novelty_score", "context_age_hours",
    "days_since_review", "is_policy_sensitive",
    # --- identity / authorization (typed request fields are the only
    #     public channel; the same names in extra_metadata are rejected) -- #
    "requesting_identity", "identity_type", "identity_verified",
    "identity_confidence", "role", "authorization_tier",
    "sensitivity_tier", "authentication_method", "requesting_session_id",
    # --- evaluation typing (tis-v2 Commit 5a.1: typed request fields on
    #     /v2/govern are the only public channel; the route validates
    #     them and constructs the trusted governed metadata itself) ----- #
    "risk_tier", "action_class",
    # --- provenance / audit ----------------------------------------------- #
    "governance_rule_matches",
    "checkpoint_id", "gca_context_id", "chain_of_custody_id",
    "audit_log_id", "retrieval_ids", "source_references",
    "connection_type", "connection_type_modifier_id",
    "resolved_policy_profile_id", "composer_metadata", "mcp_server_id",
    # connection-type detection inputs — they steer CT resolution and
    # therefore the resolved policy (thresholds, weights, gate set)
    "api_endpoint", "document_ids", "sensor_id", "web_url",
    # --- chain linkage / TEL layers --------------------------------------- #
    "previous_tc_hash", "chain_sequence", "chain_id", "issued_by",
    "original_decision", "policy_exception_id", "regulatory_basis",
    "co_authorizer",
    "governance_status", "evaluation_completeness_score",
    "components_evaluated", "components_skipped", "skip_reasons",
    "governance_integrity_score",
    # --- scope attestation ------------------------------------------------ #
    "mcp_servers_in_scope", "mcp_servers_out_of_scope",
    "downstream_agents_in_scope", "downstream_agents_out_of_scope",
    "enforcement_perimeter_complete", "attestation_basis",
    "context_expanded_after_evaluation", "context_expansion_events",
    "upstream_tc_references",
})

#: Prefix families. Any key beginning with one of these (after
#: normalization) is protected even if not in the exact list — this is
#: the drift guard for newly added keys in an existing family.
PROTECTED_METADATA_PREFIXES: Tuple[str, ...] = (
    "governance_", "override_", "identity_", "mcp_", "chain_",
    "fail_safe_", "c3_", "injection_", "credential_",
    "post_override_", "context_expansion", "step_up_",
)


def _normalize_key(key: str) -> str:
    """Case- and separator-insensitive normalization: ``C-Score`` and
    ``c_score`` both normalize to ``c_score``."""
    return key.strip().lower().replace("-", "_")


def is_protected_key(key: str) -> bool:
    norm = _normalize_key(key)
    if norm in PROTECTED_METADATA_KEYS:
        return True
    return any(norm.startswith(p) for p in PROTECTED_METADATA_PREFIXES)


def find_protected_keys(obj: Any, _path: str = "") -> List[str]:
    """Recursively scan a metadata object for protected keys.

    Inspects dict keys at every depth (lists are traversed). Returns
    the dotted paths of every protected key found — names only, never
    values, so the error surface cannot echo sensitive content.
    Unrelated structured metadata passes untouched.
    """
    found: List[str] = []
    if isinstance(obj, dict):
        for key, value in obj.items():
            key_str = str(key)
            path = f"{_path}.{key_str}" if _path else key_str
            if is_protected_key(key_str):
                found.append(path)
            found.extend(find_protected_keys(value, path))
    elif isinstance(obj, (list, tuple)):
        for i, item in enumerate(obj):
            found.extend(find_protected_keys(item, f"{_path}[{i}]"))
    return found


__all__ = [
    "PROTECTED_METADATA_KEYS",
    "PROTECTED_METADATA_PREFIXES",
    "is_protected_key",
    "find_protected_keys",
]
