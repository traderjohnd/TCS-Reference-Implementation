"""
tcs.provenance
==============

Typed, versioned, privacy-safe C3 provenance for tis-v2 certificates
(Commit 4 of the landing sequence).

Two layered models:

    GovernanceRuleMatch
        A deterministic typed representation of a rule-engine match,
        preserving the rule-specific evidence that already exists —
        rule identity, group/term POSITIONS (never lexical terms),
        fact KEYS (never fact values), control classification, and the
        static rule-library effect text.

    C3ProvenanceRecord
        A broader record describing what produced a C3 result. One
        record is MANDATORY on any v2 certificate whose c3_score is
        0.0000; the list must be empty when c3_score is nonzero.

Privacy contract (owner decision, Commit 4):

    * No fact VALUES — age groups, clinical states, device settings, or
      any typed binding — enter a portable certificate. Fact KEYS only.
    * No lexical matched terms — ``(rule_id, rule_version, group_index,
      term_index)`` identifies the vocabulary entry without repeating
      sensitive concepts from the governed interaction.
    * No raw regex source strings on the wire — stable pattern IDs plus
      an append-only versioned mapping.
    * ``blocking_reason`` / ``explanation`` are static rule-definition
      text, verified against the registered rule AT ISSUANCE ONLY.

Registry independence (owner guardrail, Commit 4):

    Issuance-time construction may consult the live rule registries to
    resolve term indices and verify static text. After construction the
    certificate stands alone: deserialization, hash-payload
    construction, raw verification, rehydration, and replay validate
    ONLY schema version, canonical shape and ordering, field domains,
    nonempty required identifiers, and internal references. They never
    compare stored content against whatever the current rule registry
    contains — otherwise editing a rule would make an authentic
    historical certificate unverifiable.

Pattern-set mappings are APPEND-ONLY and version-dispatched: a future
pattern-table change adds a new version entry; it never rewrites a
frozen historical mapping, so historical provenance records remain
verifiable permanently.

This module is a leaf: it imports only ``tcs.canonical``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Dict, FrozenSet, Tuple

from tcs.canonical import (
    CertificateInvariantError,
    require_canonical_parameter,
)


# --------------------------------------------------------------------------- #
# Record schema versions                                                       #
# --------------------------------------------------------------------------- #
#
# Every serialized record carries its own schema_version; deserialization
# and validation dispatch strictly and reject unsupported values. The
# certificate-level provenance_schema_version does not replace these.

GOVERNANCE_RULE_MATCH_SCHEMA_VERSION = 1
C3_PROVENANCE_SCHEMA_VERSION = 1


# --------------------------------------------------------------------------- #
# Pattern-set versions and APPEND-ONLY ID mappings                             #
# --------------------------------------------------------------------------- #
#
# The injection and credential regex tables in tcs.governed_context were
# historically versionless. These constants name them, and the mappings
# below bind every pattern to a stable, content-free identifier.
#
# APPEND-ONLY: when a pattern is added, removed, or semantically changed,
# a NEW version key is added with a complete new mapping, and the ACTIVE_*
# constant moves forward. Frozen historical mappings are never edited —
# provenance records dispatch on their recorded pattern_set_version, so
# every previously issued version must remain resolvable permanently.
# The conformance test in tests/test_certificate_v2_core.py proves the
# active mapping exactly covers the live regex tables; a table change
# fails that test until a new version is deliberately added here.

ACTIVE_INJECTION_PATTERN_SET_VERSION = "tcs-injection-patterns-v1"
ACTIVE_CREDENTIAL_PATTERN_SET_VERSION = "tcs-credential-patterns-v1"

INJECTION_PATTERN_IDS_BY_VERSION: Dict[str, Dict[str, str]] = {
    "tcs-injection-patterns-v1": {
        r"(?i)ignore (?:the )?(?:previous|policy|all) (?:instructions|constraints|rules)":
            "inj-001-ignore-instructions",
        r"(?i)disregard (?:the )?(?:previous|policy|all) (?:instructions|rules)":
            "inj-002-disregard-instructions",
        r"(?i)override (?:the )?(?:policy|safety|guardrails?)":
            "inj-003-override-guardrails",
        r"(?i)you are now (?:a |an )?(?:unrestricted|uncensored|jailbroken)":
            "inj-004-jailbreak-persona",
        r"(?i)recommend all (?:equities|securities|assets)":
            "inj-005-recommend-all-assets",
        r"(?i)bypass (?:the )?(?:compliance|kyc|aml|safety)":
            "inj-006-bypass-compliance",
        r"(?i)system\s*:\s*you are":
            "inj-007-fake-system-prompt",
    },
}

CREDENTIAL_PATTERN_IDS_BY_VERSION: Dict[str, Dict[str, str]] = {
    "tcs-credential-patterns-v1": {
        r"(?i)\b(api[_-]?key|secret|password|token|bearer)\s*[:=]":
            "cred-001-key-value-label",
        r"\bsk-[A-Za-z0-9]{16,}\b":
            "cred-002-openai-style-key",
        r"\b[A-Fa-f0-9]{32,}\b":
            "cred-003-long-hex",
        r"\bAKIA[0-9A-Z]{12,}\b":
            "cred-004-aws-access-key",
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----":
            "cred-005-private-key-block",
    },
}


def _pattern_ids_for(source_type: str, pattern_set_version: str) -> Dict[str, str]:
    """Resolve the frozen ID mapping for a recorded pattern-set version.

    Dispatches on the RECORDED version (append-only registry, permitted
    during verification — it is not a mutable rule registry). Unknown
    versions fail closed.
    """
    registry = (
        INJECTION_PATTERN_IDS_BY_VERSION
        if source_type == "injection_scan"
        else CREDENTIAL_PATTERN_IDS_BY_VERSION
    )
    mapping = registry.get(pattern_set_version)
    if mapping is None:
        raise CertificateInvariantError(
            f"unknown pattern_set_version {pattern_set_version!r} "
            f"for source_type {source_type!r}"
        )
    return mapping


# --------------------------------------------------------------------------- #
# Enumerations                                                                 #
# --------------------------------------------------------------------------- #

C3_SOURCE_TYPES: FrozenSet[str] = frozenset({
    "rule",
    "injection_scan",
    "connector_event",
    "credential_detection",
    "caller_supplied",
})

RULE_EVALUATORS: FrozenSet[str] = frozenset({"term_group", "typed_context"})


# --------------------------------------------------------------------------- #
# Typed records                                                                #
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class MatchedTermGroup:
    """Position of a matched vocabulary entry inside a rule.

    ``(rule_id, rule_version, group_index, term_index)`` identifies the
    exact term without serializing its lexical content.
    """
    group_index: int
    term_index: int


@dataclass(frozen=True)
class RuleMatchRef:
    """Internal reference from a C3ProvenanceRecord to one of the
    certificate's own GovernanceRuleMatch records."""
    rule_id: str
    rule_version: str


@dataclass(frozen=True)
class GovernanceRuleMatch:
    """Deterministic typed representation of one rule-engine match.

    All string fields are ``""``-normalized (never None). Penalty
    fields are canonical 4dp non-negative parameters. Collections carry
    their declared canonical order (see validate_governance_rule_match).
    """
    schema_version: int
    rule_id: str
    rule_version: str
    evaluator: str                                # term_group | typed_context
    applies_to_domains: Tuple[str, ...]           # sorted, deduplicated
    matched_domain: str
    matched_term_groups: Tuple[MatchedTermGroup, ...]   # sorted, no duplicates
    matched_fact_keys: Tuple[str, ...]            # sorted, deduplicated; KEYS only
    control_class: str
    safety_category: str
    c3_violation: bool
    blocking_reason: str                          # static rule-library text
    decision_pressure: str                        # audit-only
    requires_human_review: bool
    boundedness_penalty: Decimal
    attribution_penalty: Decimal
    known_calibration_penalty: Decimal
    novelty_lift: Decimal
    explanation: str                              # static rule-library text
    active_policy_profile_id: str


@dataclass(frozen=True)
class C3ProvenanceRecord:
    """What produced the C3 result. See module docstring for the
    source-type contract."""
    schema_version: int
    source_type: str                              # C3_SOURCE_TYPES
    pattern_id: str                               # stable ID, never regex source
    pattern_set_version: str
    location_tag: str                             # e.g. "chunk_id=c1", "prompt"
    connector_type: str
    detail_code: str                              # non-sensitive evidence code
    producer_id: str                              # required for caller_supplied
    rule_match_refs: Tuple[RuleMatchRef, ...] = field(default=())


# --------------------------------------------------------------------------- #
# Validation                                                                   #
# --------------------------------------------------------------------------- #

def _require_str(value: Any, name: str) -> None:
    if not isinstance(value, str):
        raise CertificateInvariantError(
            f"{name} must be str, got {type(value).__name__}"
        )


def _require_bool(value: Any, name: str) -> None:
    if not isinstance(value, bool):
        raise CertificateInvariantError(
            f"{name} must be bool, got {type(value).__name__}"
        )


def validate_governance_rule_match(m: GovernanceRuleMatch) -> None:
    """Registry-independent validation of one typed rule match.

    Checks schema version, field domains, and canonical collection
    order. Deliberately does NOT consult any live rule registry.
    """
    if m.schema_version != GOVERNANCE_RULE_MATCH_SCHEMA_VERSION:
        raise CertificateInvariantError(
            f"unsupported GovernanceRuleMatch schema_version "
            f"{m.schema_version!r}"
        )
    _require_str(m.rule_id, "rule_id")
    if not m.rule_id:
        raise CertificateInvariantError("rule_id must be nonempty")
    _require_str(m.rule_version, "rule_version")
    if not m.rule_version:
        raise CertificateInvariantError("rule_version must be nonempty")
    if m.evaluator not in RULE_EVALUATORS:
        raise CertificateInvariantError(
            f"unknown evaluator {m.evaluator!r}"
        )
    for name in ("matched_domain", "control_class", "safety_category",
                 "blocking_reason", "decision_pressure", "explanation",
                 "active_policy_profile_id"):
        _require_str(getattr(m, name), name)
    _require_bool(m.c3_violation, "c3_violation")
    _require_bool(m.requires_human_review, "requires_human_review")

    # Identifier sets: sorted and deduplicated (declared normalization
    # rule for identifier collections).
    domains = list(m.applies_to_domains)
    if domains != sorted(set(domains)):
        raise CertificateInvariantError(
            "applies_to_domains must be sorted and deduplicated"
        )
    for d in domains:
        _require_str(d, "applies_to_domains entry")
    fact_keys = list(m.matched_fact_keys)
    if fact_keys != sorted(set(fact_keys)):
        raise CertificateInvariantError(
            "matched_fact_keys must be sorted and deduplicated"
        )
    for k in fact_keys:
        _require_str(k, "matched_fact_keys entry")

    # Positional structure: sorted by (group_index, term_index);
    # duplicates are REJECTED (declared rule for positional structures).
    groups = list(m.matched_term_groups)
    keys = [(g.group_index, g.term_index) for g in groups]
    if keys != sorted(keys):
        raise CertificateInvariantError(
            "matched_term_groups must be sorted by (group_index, term_index)"
        )
    if len(keys) != len(set(keys)):
        raise CertificateInvariantError(
            "matched_term_groups must not contain duplicates"
        )
    for g in groups:
        if (isinstance(g.group_index, bool) or isinstance(g.term_index, bool)
                or not isinstance(g.group_index, int)
                or not isinstance(g.term_index, int)
                or g.group_index < 0 or g.term_index < 0):
            raise CertificateInvariantError(
                "matched_term_groups indices must be non-negative ints"
            )

    for name in ("boundedness_penalty", "attribution_penalty",
                 "known_calibration_penalty", "novelty_lift"):
        require_canonical_parameter(getattr(m, name), name)


def validate_c3_provenance_record(r: C3ProvenanceRecord) -> None:
    """Registry-independent validation of one provenance record.

    Source-specific requirements (owner decision 4):
        rule                 -> >= 1 rule_match_refs (resolved against
                                the certificate's own typed matches by
                                the certificate-level validator)
        injection_scan       -> pattern_id + known pattern_set_version
                                + location_tag
        connector_event      -> pattern_id or detail_code, + connector_type
        credential_detection -> pattern_id + known pattern_set_version
        caller_supplied      -> nonempty producer_id
    """
    if r.schema_version != C3_PROVENANCE_SCHEMA_VERSION:
        raise CertificateInvariantError(
            f"unsupported C3ProvenanceRecord schema_version "
            f"{r.schema_version!r}"
        )
    if r.source_type not in C3_SOURCE_TYPES:
        raise CertificateInvariantError(
            f"unknown C3 source_type {r.source_type!r}"
        )
    for name in ("pattern_id", "pattern_set_version", "location_tag",
                 "connector_type", "detail_code", "producer_id"):
        _require_str(getattr(r, name), name)

    refs = list(r.rule_match_refs)
    ref_keys = [(x.rule_id, x.rule_version) for x in refs]
    if ref_keys != sorted(set(ref_keys)):
        raise CertificateInvariantError(
            "rule_match_refs must be sorted and deduplicated"
        )
    for x in refs:
        _require_str(x.rule_id, "rule_match_refs.rule_id")
        _require_str(x.rule_version, "rule_match_refs.rule_version")
        if not x.rule_id or not x.rule_version:
            raise CertificateInvariantError(
                "rule_match_refs entries must be nonempty"
            )

    if r.source_type == "rule":
        if not refs:
            raise CertificateInvariantError(
                "source_type 'rule' requires at least one rule_match_ref"
            )
    else:
        if refs:
            raise CertificateInvariantError(
                f"source_type {r.source_type!r} must not carry "
                "rule_match_refs"
            )

    if r.source_type == "injection_scan":
        if not r.pattern_id or not r.location_tag:
            raise CertificateInvariantError(
                "injection_scan requires pattern_id and location_tag"
            )
        mapping = _pattern_ids_for("injection_scan", r.pattern_set_version)
        if r.pattern_id not in mapping.values():
            raise CertificateInvariantError(
                f"pattern_id {r.pattern_id!r} not in pattern set "
                f"{r.pattern_set_version!r}"
            )

    if r.source_type == "credential_detection":
        if not r.pattern_id:
            raise CertificateInvariantError(
                "credential_detection requires pattern_id"
            )
        mapping = _pattern_ids_for(
            "credential_detection", r.pattern_set_version
        )
        if r.pattern_id not in mapping.values():
            raise CertificateInvariantError(
                f"pattern_id {r.pattern_id!r} not in pattern set "
                f"{r.pattern_set_version!r}"
            )

    if r.source_type == "connector_event":
        if not r.connector_type:
            raise CertificateInvariantError(
                "connector_event requires connector_type"
            )
        if not (r.pattern_id or r.detail_code):
            raise CertificateInvariantError(
                "connector_event requires pattern_id or detail_code"
            )

    if r.source_type == "caller_supplied":
        if not r.producer_id:
            raise CertificateInvariantError(
                "caller_supplied requires a nonempty producer_id"
            )


# --------------------------------------------------------------------------- #
# Total sort orders                                                            #
# --------------------------------------------------------------------------- #

def rule_match_sort_key(m: GovernanceRuleMatch) -> Tuple:
    """Declared total order for GovernanceRuleMatch lists."""
    return (
        m.rule_id,
        m.evaluator,
        m.rule_version,
        m.matched_domain,
        tuple((g.group_index, g.term_index) for g in m.matched_term_groups),
        m.matched_fact_keys,
        m.safety_category,
        m.blocking_reason,
    )


def c3_record_sort_key(r: C3ProvenanceRecord) -> Tuple:
    """Declared total order for C3ProvenanceRecord lists."""
    return (
        r.source_type,
        r.pattern_id,
        r.location_tag,
        r.connector_type,
        r.producer_id,
        r.detail_code,
        tuple((x.rule_id, x.rule_version) for x in r.rule_match_refs),
    )


# --------------------------------------------------------------------------- #
# Canonical serialization (validating, never repairing)                        #
# --------------------------------------------------------------------------- #

_RULE_MATCH_KEYS = frozenset({
    "schema_version", "rule_id", "rule_version", "evaluator",
    "applies_to_domains", "matched_domain", "matched_term_groups",
    "matched_fact_keys", "control_class", "safety_category",
    "c3_violation", "blocking_reason", "decision_pressure",
    "requires_human_review", "boundedness_penalty", "attribution_penalty",
    "known_calibration_penalty", "novelty_lift", "explanation",
    "active_policy_profile_id",
})

_C3_RECORD_KEYS = frozenset({
    "schema_version", "source_type", "pattern_id", "pattern_set_version",
    "location_tag", "connector_type", "detail_code", "producer_id",
    "rule_match_refs",
})


def _serialize_parameter_4dp(value: Decimal, name: str) -> str:
    require_canonical_parameter(value, name)
    return format(value, ".4f")


def serialize_governance_rule_match(m: GovernanceRuleMatch) -> Dict[str, Any]:
    validate_governance_rule_match(m)
    return {
        "schema_version": m.schema_version,
        "rule_id": m.rule_id,
        "rule_version": m.rule_version,
        "evaluator": m.evaluator,
        "applies_to_domains": list(m.applies_to_domains),
        "matched_domain": m.matched_domain,
        "matched_term_groups": [
            {"group_index": g.group_index, "term_index": g.term_index}
            for g in m.matched_term_groups
        ],
        "matched_fact_keys": list(m.matched_fact_keys),
        "control_class": m.control_class,
        "safety_category": m.safety_category,
        "c3_violation": m.c3_violation,
        "blocking_reason": m.blocking_reason,
        "decision_pressure": m.decision_pressure,
        "requires_human_review": m.requires_human_review,
        "boundedness_penalty": _serialize_parameter_4dp(
            m.boundedness_penalty, "boundedness_penalty"),
        "attribution_penalty": _serialize_parameter_4dp(
            m.attribution_penalty, "attribution_penalty"),
        "known_calibration_penalty": _serialize_parameter_4dp(
            m.known_calibration_penalty, "known_calibration_penalty"),
        "novelty_lift": _serialize_parameter_4dp(
            m.novelty_lift, "novelty_lift"),
        "explanation": m.explanation,
        "active_policy_profile_id": m.active_policy_profile_id,
    }


def governance_rule_match_from_dict(d: Dict[str, Any]) -> GovernanceRuleMatch:
    """Strict deserialization — unknown keys rejected, no repair."""
    if not isinstance(d, dict):
        raise CertificateInvariantError(
            f"GovernanceRuleMatch payload must be dict, got "
            f"{type(d).__name__}"
        )
    if set(d) != _RULE_MATCH_KEYS:
        unexpected = sorted(set(d) - _RULE_MATCH_KEYS)
        missing = sorted(_RULE_MATCH_KEYS - set(d))
        raise CertificateInvariantError(
            f"GovernanceRuleMatch key mismatch: unexpected={unexpected} "
            f"missing={missing}"
        )
    groups = []
    for g in d["matched_term_groups"]:
        if not isinstance(g, dict) or set(g) != {"group_index", "term_index"}:
            raise CertificateInvariantError(
                "matched_term_groups entries must have exactly "
                "group_index and term_index"
            )
        groups.append(MatchedTermGroup(
            group_index=g["group_index"], term_index=g["term_index"],
        ))
    m = GovernanceRuleMatch(
        schema_version=d["schema_version"],
        rule_id=d["rule_id"],
        rule_version=d["rule_version"],
        evaluator=d["evaluator"],
        applies_to_domains=tuple(d["applies_to_domains"]),
        matched_domain=d["matched_domain"],
        matched_term_groups=tuple(groups),
        matched_fact_keys=tuple(d["matched_fact_keys"]),
        control_class=d["control_class"],
        safety_category=d["safety_category"],
        c3_violation=d["c3_violation"],
        blocking_reason=d["blocking_reason"],
        decision_pressure=d["decision_pressure"],
        requires_human_review=d["requires_human_review"],
        boundedness_penalty=_parse_parameter_4dp(
            d["boundedness_penalty"], "boundedness_penalty"),
        attribution_penalty=_parse_parameter_4dp(
            d["attribution_penalty"], "attribution_penalty"),
        known_calibration_penalty=_parse_parameter_4dp(
            d["known_calibration_penalty"], "known_calibration_penalty"),
        novelty_lift=_parse_parameter_4dp(d["novelty_lift"], "novelty_lift"),
        explanation=d["explanation"],
        active_policy_profile_id=d["active_policy_profile_id"],
    )
    validate_governance_rule_match(m)
    return m


def serialize_c3_provenance_record(r: C3ProvenanceRecord) -> Dict[str, Any]:
    validate_c3_provenance_record(r)
    return {
        "schema_version": r.schema_version,
        "source_type": r.source_type,
        "pattern_id": r.pattern_id,
        "pattern_set_version": r.pattern_set_version,
        "location_tag": r.location_tag,
        "connector_type": r.connector_type,
        "detail_code": r.detail_code,
        "producer_id": r.producer_id,
        "rule_match_refs": [
            {"rule_id": x.rule_id, "rule_version": x.rule_version}
            for x in r.rule_match_refs
        ],
    }


def c3_provenance_record_from_dict(d: Dict[str, Any]) -> C3ProvenanceRecord:
    """Strict deserialization — unknown keys rejected, no repair."""
    if not isinstance(d, dict):
        raise CertificateInvariantError(
            f"C3ProvenanceRecord payload must be dict, got "
            f"{type(d).__name__}"
        )
    if set(d) != _C3_RECORD_KEYS:
        unexpected = sorted(set(d) - _C3_RECORD_KEYS)
        missing = sorted(_C3_RECORD_KEYS - set(d))
        raise CertificateInvariantError(
            f"C3ProvenanceRecord key mismatch: unexpected={unexpected} "
            f"missing={missing}"
        )
    refs = []
    for x in d["rule_match_refs"]:
        if not isinstance(x, dict) or set(x) != {"rule_id", "rule_version"}:
            raise CertificateInvariantError(
                "rule_match_refs entries must have exactly rule_id and "
                "rule_version"
            )
        refs.append(RuleMatchRef(
            rule_id=x["rule_id"], rule_version=x["rule_version"],
        ))
    r = C3ProvenanceRecord(
        schema_version=d["schema_version"],
        source_type=d["source_type"],
        pattern_id=d["pattern_id"],
        pattern_set_version=d["pattern_set_version"],
        location_tag=d["location_tag"],
        connector_type=d["connector_type"],
        detail_code=d["detail_code"],
        producer_id=d["producer_id"],
        rule_match_refs=tuple(refs),
    )
    validate_c3_provenance_record(r)
    return r


def _parse_parameter_4dp(value: Any, name: str) -> Decimal:
    """Parse a serialized 4dp parameter string strictly (no repair)."""
    if not isinstance(value, str):
        raise CertificateInvariantError(
            f"{name} must be a 4dp string, got {type(value).__name__}"
        )
    import re
    if not re.fullmatch(r"(?:0|[1-9]\d*)\.\d{4}", value):
        raise CertificateInvariantError(
            f"{name} is not a canonical 4dp parameter string: {value!r}"
        )
    d = Decimal(value)
    require_canonical_parameter(d, name)
    return d


__all__ = [
    "GOVERNANCE_RULE_MATCH_SCHEMA_VERSION",
    "C3_PROVENANCE_SCHEMA_VERSION",
    "ACTIVE_INJECTION_PATTERN_SET_VERSION",
    "ACTIVE_CREDENTIAL_PATTERN_SET_VERSION",
    "INJECTION_PATTERN_IDS_BY_VERSION",
    "CREDENTIAL_PATTERN_IDS_BY_VERSION",
    "C3_SOURCE_TYPES",
    "RULE_EVALUATORS",
    "MatchedTermGroup",
    "RuleMatchRef",
    "GovernanceRuleMatch",
    "C3ProvenanceRecord",
    "validate_governance_rule_match",
    "validate_c3_provenance_record",
    "rule_match_sort_key",
    "c3_record_sort_key",
    "serialize_governance_rule_match",
    "governance_rule_match_from_dict",
    "serialize_c3_provenance_record",
    "c3_provenance_record_from_dict",
]
