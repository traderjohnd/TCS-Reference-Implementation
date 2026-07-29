"""
tis-v2 Commit 4 — the v2 certificate core.

Covers: construction (Decimal-only raw evidence), the exact v2 wire
schema and alias removal, the sealing boundary (wrong-but-canonical
values refused BEFORE any hash exists), post-hash tamper evidence,
layered C3 provenance with per-source seal rules, append-only pattern
mappings, static rule-text conformance, the deterministic 10,000-case
serialized-certificate replay, store round-trips over mixed chains,
frozen-v1 wire preservation, and full-tree production dormancy.
"""

from __future__ import annotations

import ast
import json
import random
from datetime import datetime
from decimal import Decimal, localcontext
from pathlib import Path

import pytest

import tcs
from tcs.canonical import (
    CertificateInvariantError,
    SCORE_QUANTUM,
    SCORE_ROUNDING,
    TIS_DECIMAL_CONTEXT,
    UnsupportedCalculationVersion,
)
from tcs.decision_engine import map_decision_versioned
from tcs.governance import SCENARIO_RULES, TYPED_CONTEXT_RULES, classify_query_risk
from tcs.governed_context import _CREDENTIAL_PATTERNS, _INJECTION_PATTERNS
from tcs.persistence import CertificateStore
from tcs.policy_profiles import PROFILES, load_profile
from tcs.provenance import (
    ACTIVE_CREDENTIAL_PATTERN_SET_VERSION,
    ACTIVE_INJECTION_PATTERN_SET_VERSION,
    C3_PROVENANCE_SCHEMA_VERSION,
    C3ProvenanceRecord,
    CREDENTIAL_PATTERN_IDS_BY_VERSION,
    INJECTION_PATTERN_IDS_BY_VERSION,
    RuleMatchRef,
    validate_c3_provenance_record,
)
from tcs.tis_engine import TISInput, compute_tis_v2
from tcs.trust_certificate import (
    V1_HASH_FIELD_SET,
    V2_FIELD_SET,
    V2_OPTIONAL_FIELDS,
    V2_REQUIRED_FIELDS,
    compute_raw_stored_tc_hash,
    compute_tc_hash,
    generate_certificate,
    generate_certificate_v2,
    tc_from_dict_v2,
    validate_v2_certificate_for_sealing,
)
from tests.conftest import make_tis_input

EVAL_TIME = datetime(2026, 7, 28, 12, 0, 0)
BACK = ("B", "A", "C", "K")

#: The declared v2 wire field set, embedded as an independent literal so
#: a drift in either the production constant or the serializer breaks
#: this suite. 93 keys.
EXPECTED_V2_KEYS = sorted([
    'action_class', 'adjustments_applied', 'archived', 'audit_integrity',
    'audit_log_id', 'blocking_reason', 'c3_provenance', 'c3_score',
    'calculation_version', 'certificate_id', 'certificate_schema_version',
    'chain_depth', 'chain_of_custody_id', 'chain_u_scores', 'checkpoint_id',
    'compensation_scope', 'component_scores', 'component_scores_observed',
    'component_scores_raw', 'component_weights', 'composer_metadata',
    'connection_type', 'connection_type_modifier_id',
    'decay_algorithm_version', 'decay_factor', 'decision', 'domain',
    'elapsed_hours', 'enhanced_logging', 'escalation_routed_to',
    'evaluation_timestamp', 'explanation_summary', 'failure_mode',
    'gate_result', 'gate_results', 'gate_set', 'gca_context_id',
    'governance_rule_matches', 'governance_status', 'identity_binding',
    'incident_id', 'integration_boundary_gaps', 'invalidation_status',
    'invalidation_triggers', 'is_valid', 'key_concerns', 'key_factors',
    'last_invalidation_event', 'lifecycle_state', 'mcp_server_id',
    'override_record', 'penalty_aggregate', 'penalty_breakdown',
    'policy_set_id', 'policy_severity', 'provenance_schema_version',
    'proximity_to_threshold', 'qualified_decision', 'reason_code',
    'recompute_required', 'recomputed_from_certificate_id',
    'recovery_mode_activated', 'redacted_fields', 'redaction_applied',
    'redaction_scope', 'regulatory_explanation_level', 'regulatory_mapping',
    'requires_human_review', 'resolved_decay_rate', 'resolved_kappa',
    'resolved_penalty_weights', 'resolved_policy_profile_id',
    'resolved_theta_allow', 'resolved_theta_escalate',
    'resolved_theta_hold', 'retrieval_ids', 'risk_tier', 's_adjusted',
    's_base', 'scope_attestation', 'score_precision_policy',
    'source_references', 'state_transition_history', 'step_up_completed',
    'step_up_required', 'subject_id', 'subject_type',
    'superseded_by_certificate_id', 'thresholds', 'tis_adjusted',
    'tis_current', 'tis_raw', 'valid_until',
])

FIXED_4DP = __import__("re").compile(r"^(?:0\.\d{4}|1\.0000)$")


def make_v2_input(
    profile_id="fin-r3-a4-ct4",
    scores=None,
    meta=None,
    **overrides,
):
    """A v2 TISInput with Decimal raw scores (Commit 4 fidelity rule)."""
    raw = scores or {"B": "0.94", "A": "0.95", "C": "0.95", "K": "0.85"}
    defaults = dict(
        subject_id="v2-core", subject_type="model_output",
        policy_profile=load_profile(profile_id),
        dimension_scores={k: Decimal(v) for k, v in raw.items()},
        context_metadata=meta if meta is not None else {
            "chain_id": "chain-v2-core",
        },
        elapsed_hours=0.0, is_valid=1, invalidation_event=None,
        evaluation_time=EVAL_TIME,
    )
    defaults.update(overrides)
    return TISInput(**defaults)


def build_v2_certificate(inp=None, *, c3_provenance=None):
    inp = inp or make_v2_input()
    res = compute_tis_v2(inp)
    decision, review = map_decision_versioned(inp, res)
    return generate_certificate_v2(
        inp, res, decision, review, c3_provenance=c3_provenance,
    ), inp, res


def caller_record(producer="tests.test_certificate_v2_core::synthetic"):
    return C3ProvenanceRecord(
        schema_version=C3_PROVENANCE_SCHEMA_VERSION,
        source_type="caller_supplied",
        pattern_id="", pattern_set_version="", location_tag="",
        connector_type="", detail_code="synthetic_test_zero",
        producer_id=producer,
    )


def c3_zero_input(meta_extra=None, **overrides):
    """An input whose C3 sub-factor is zero WITH a collapsed C dimension
    (gate fails on C) — the coupled shape every producer emits today."""
    meta = {"chain_id": "chain-v2-c3", "blocking_context": "test_zero"}
    meta.update(meta_extra or {})
    return make_v2_input(
        scores={"B": "0.94", "A": "0.95", "C": "0.31", "K": "0.85"},
        meta=meta,
        sub_factor_scores={"C": {"C3": 0.0}},
        **overrides,
    )


# =========================================================================== #
# Construction                                                                 #
# =========================================================================== #

class TestConstruction:
    def test_full_pipeline_builds_and_seals(self):
        tc, inp, res = build_v2_certificate()
        assert tc.certificate_schema_version == 2
        assert tc.calculation_version == "tis-v2"
        assert tc.gate_result == 1
        assert tc.audit_integrity is not None
        assert compute_tc_hash(tc.to_dict()) == tc.audit_integrity.tc_hash

    def test_float_raw_scores_rejected(self):
        # Delta 2: a binary float cannot be recorded as exact source
        # evidence. compute_tis_v2 tolerates it (dormant-test input),
        # certificate construction refuses it.
        inp = make_v2_input()
        inp.dimension_scores = {"B": 0.94, "A": 0.95, "C": 0.95, "K": 0.85}
        res = compute_tis_v2(inp)
        decision, review = map_decision_versioned(inp, res)
        with pytest.raises(CertificateInvariantError, match="fidelity"):
            generate_certificate_v2(inp, res, decision, review)

    def test_v1_result_rejected(self):
        inp = make_tis_input(
            "fin-high-risk-suitability-v3",
            {"B": 0.94, "A": 0.90, "C": 0.92, "K": 0.83},
        )
        from tcs.tis_engine import compute_tis
        res = compute_tis(inp)
        with pytest.raises(CertificateInvariantError):
            generate_certificate_v2(inp, res, "Allow", False)

    def test_missing_effective_scores_rejected(self):
        from dataclasses import replace
        inp = make_v2_input()
        res = compute_tis_v2(inp)
        res = replace(res, effective_dimension_scores={})
        with pytest.raises(CertificateInvariantError, match="effective"):
            generate_certificate_v2(inp, res, "Allow", False)

    def test_nonzero_c3_rejects_provenance(self):
        inp = make_v2_input()
        res = compute_tis_v2(inp)
        decision, review = map_decision_versioned(inp, res)
        with pytest.raises(CertificateInvariantError, match="empty"):
            generate_certificate_v2(
                inp, res, decision, review,
                c3_provenance=[caller_record()],
            )


# =========================================================================== #
# Exact v2 wire schema, alias removal, frozen v1 wire                          #
# =========================================================================== #

class TestWireSchema:
    def test_v2_serialization_has_exactly_the_declared_field_set(self):
        tc, _, _ = build_v2_certificate()
        d = tc.to_dict()
        assert sorted(d.keys()) == EXPECTED_V2_KEYS
        assert sorted(V2_FIELD_SET) == EXPECTED_V2_KEYS
        assert V2_OPTIONAL_FIELDS == frozenset()
        assert V2_FIELD_SET == V2_REQUIRED_FIELDS

    def test_no_legacy_alias_on_v2_wire(self):
        tc, _, _ = build_v2_certificate()
        d = tc.to_dict()
        for alias in ("gate_passed", "decay_rate",
                      "failing_dimension_subfactors"):
            assert alias not in d

    def test_unexpected_top_level_field_rejected(self):
        tc, _, _ = build_v2_certificate()
        d = tc.to_dict()
        d["injected_field"] = "x"
        with pytest.raises(CertificateInvariantError, match="unexpected"):
            validate_v2_certificate_for_sealing(d)

    def test_unexpected_nested_record_key_rejected(self):
        tc, _, _ = build_v2_certificate(
            inp=c3_zero_input(), c3_provenance=[caller_record()],
        )
        d = tc.to_dict()
        d["c3_provenance"][0]["surprise"] = 1
        with pytest.raises(CertificateInvariantError, match="key mismatch"):
            validate_v2_certificate_for_sealing(d)

    def test_score_fields_are_fixed_4dp_strings(self):
        tc, _, _ = build_v2_certificate()
        d = tc.to_dict()
        for name in ("s_base", "s_adjusted", "tis_raw", "tis_adjusted",
                     "tis_current", "penalty_aggregate", "decay_factor",
                     "c3_score", "resolved_theta_allow",
                     "resolved_theta_hold", "resolved_theta_escalate",
                     "resolved_kappa"):
            assert FIXED_4DP.fullmatch(d[name]), (name, d[name])
        for group in ("component_scores", "component_scores_observed",
                      "component_weights", "thresholds",
                      "resolved_penalty_weights"):
            for k, v in d[group].items():
                assert FIXED_4DP.fullmatch(v), (group, k, v)

    def test_raw_scores_are_variable_scale_and_lossless(self):
        tc, inp, _ = build_v2_certificate(
            inp=make_v2_input(scores={
                "B": "0.899996", "A": "0.95", "C": "0.9500", "K": "0.85",
            }),
        )
        d = tc.to_dict()
        assert d["component_scores_raw"]["B"] == "0.899996"
        assert d["component_scores_raw"]["C"] == "0.95"   # trailing zeros
        assert Decimal(d["component_scores_raw"]["B"]) == \
            inp.dimension_scores["B"]

    def test_fresh_v1_issuance_keeps_historical_76_key_wire(self):
        # The wire-shape invariant that keeps newly issued v1
        # certificates verifying through raw + reconstruction.
        from tcs.decision_engine import map_decision
        from tcs.tis_engine import compute_tis
        inp = make_tis_input(
            "fin-high-risk-suitability-v3",
            {"B": 0.94, "A": 0.90, "C": 0.92, "K": 0.83},
        )
        res = compute_tis(inp)
        decision, review = map_decision(inp, res)
        tc = generate_certificate(inp, res, decision, review)
        d = tc.to_dict()
        assert set(d.keys()) == V1_HASH_FIELD_SET | {"audit_integrity"}
        assert "certificate_schema_version" not in d
        assert compute_raw_stored_tc_hash(json.loads(json.dumps(d))) == \
            tc.audit_integrity.tc_hash


# =========================================================================== #
# Sealing boundary — wrong-but-canonical values refused pre-hash               #
# =========================================================================== #

class TestSealingBoundary:
    """Distinct from post-hash tamper evidence: these certificates are
    mutated BEFORE the store assigns their hash, and the store must
    refuse to persist them. A wrong but internally self-consistent
    value cannot become sealable."""

    def _bump(self, value: Decimal) -> Decimal:
        return value + Decimal("0.0001")

    @pytest.mark.parametrize("field", [
        "s_base", "penalty_aggregate", "tis_current",
    ])
    def test_wrong_derived_value_refused_at_store(self, field):
        tc, _, _ = build_v2_certificate()
        setattr(tc, field, self._bump(getattr(tc, field)))
        with CertificateStore(":memory:") as store:
            with pytest.raises(CertificateInvariantError):
                store.issue(tc)
            assert store.count() == 0

    def test_wrong_gate_result_refused_at_store(self):
        tc, _, _ = build_v2_certificate()
        tc.gate_result = 0     # canonical value, wrong computation
        with CertificateStore(":memory:") as store:
            with pytest.raises(CertificateInvariantError):
                store.issue(tc)
            assert store.count() == 0

    def test_wrong_decision_refused_at_store(self):
        tc, _, _ = build_v2_certificate()
        tc.decision = "Hold"   # valid enum, wrong replay
        with CertificateStore(":memory:") as store:
            with pytest.raises(CertificateInvariantError):
                store.issue(tc)
            assert store.count() == 0

    def test_missing_c3_provenance_refused_at_store(self):
        tc, _, _ = build_v2_certificate(
            inp=c3_zero_input(), c3_provenance=[caller_record()],
        )
        assert tc.c3_score == Decimal("0.0000")
        tc.c3_provenance = []
        with CertificateStore(":memory:") as store:
            with pytest.raises(CertificateInvariantError):
                store.issue(tc)
            assert store.count() == 0

    def test_builder_itself_refuses_wrong_decision(self):
        # The hash path inside generate_certificate_v2 runs the same
        # contract, so even the builder cannot produce a wrong-decision
        # certificate.
        inp = c3_zero_input()
        res = compute_tis_v2(inp)
        assert res.C3_score == Decimal("0.0000")
        with pytest.raises(CertificateInvariantError):
            generate_certificate_v2(inp, res, "Allow", False)


# =========================================================================== #
# C3-zero is an unconditional tis-v2 Stop (gate open)                          #
# =========================================================================== #

class TestC3ZeroUnconditionalStop:
    def _decoupled_result(self):
        """C3 = 0.0000 while every gate PASSES — the decoupled shape
        the v1 conjunctive ladder mishandled."""
        from dataclasses import replace
        inp = make_v2_input(meta={
            "chain_id": "chain-c3-decoupled",
        })
        res = compute_tis_v2(inp)
        res = replace(res, C3_score=Decimal("0.0000"))
        assert res.gate_result == 1
        return inp, res

    def test_decision_is_stop_even_with_gate_open(self):
        inp, res = self._decoupled_result()
        decision, review = map_decision_versioned(inp, res)
        assert decision == "Stop"
        assert review is False

    def test_seals_with_stop_and_explicit_provenance(self):
        inp, res = self._decoupled_result()
        decision, review = map_decision_versioned(inp, res)
        tc = generate_certificate_v2(
            inp, res, decision, review,
            c3_provenance=[caller_record()],
        )
        d = tc.to_dict()
        assert d["decision"] == "Stop"
        assert d["gate_result"] == 1          # the gate genuinely passed
        assert d["c3_score"] == "0.0000"
        with CertificateStore(":memory:") as store:
            issued = store.issue(tc)
            assert store.verify_chain(
                issued.audit_integrity.chain_id) is True

    def test_non_stop_decision_with_c3_zero_cannot_seal(self):
        inp, res = self._decoupled_result()
        with pytest.raises(CertificateInvariantError):
            generate_certificate_v2(
                inp, res, "Allow", False,
                c3_provenance=[caller_record()],
            )


# =========================================================================== #
# Layered C3 provenance — per-source seal rules                                #
# =========================================================================== #

def _injection_pattern_repr():
    return repr(_INJECTION_PATTERNS[0].pattern)


def _credential_pattern_repr():
    return repr(_CREDENTIAL_PATTERNS[1].pattern)   # sk- style key


class TestC3ProvenanceSources:
    def test_rule_source_derived_from_real_classifier_matches(self):
        query = "What lithium dose should I take while pregnant?"
        matches = classify_query_risk(
            query=query, domain="life_sciences", rules=list(SCENARIO_RULES),
        )
        assert matches and any(m.effect.c3_violation for m in matches)
        audit = [
            {**m.to_audit_dict(), "active_policy_profile_id": "test-prof"}
            for m in matches
        ]
        inp = c3_zero_input(meta_extra={"governance_rule_matches": audit})
        tc, _, _ = build_v2_certificate(inp=inp)
        d = tc.to_dict()
        assert d["decision"] == "Stop"
        sources = {r["source_type"] for r in d["c3_provenance"]}
        assert "rule" in sources
        rule_rec = next(
            r for r in d["c3_provenance"] if r["source_type"] == "rule")
        assert rule_rec["rule_match_refs"]
        # Privacy reduction: positions and keys only — the MATCHED
        # lexical terms from the governed interaction never appear in
        # the typed match records. (Rule identifiers and static
        # rule-library text legitimately name the risk category —
        # "consumer_facing_dosing_during_pregnancy" is a rule_id, not
        # interaction content.)
        for m in d["governance_rule_matches"]:
            for g in m["matched_term_groups"]:
                assert set(g) == {"group_index", "term_index"}
        blob = json.dumps(d["governance_rule_matches"])
        assert "matched_term" not in blob.replace("matched_term_groups", "")
        assert "lithium" not in blob        # the matched vocabulary term
        assert "should i take" not in blob  # ditto

    def test_injection_scan_source_seals_without_rule_match(self):
        # Structured signal, as the production scanner emits it (5a) —
        # reason strings are explanatory only and never parsed.
        inp = c3_zero_input(meta_extra={
            "injection_detected": True,
            "injection_reason": "chunk_id=c1: injection pattern (explanatory)",
            "c3_signals": [{
                "source_type": "injection_scan",
                "pattern_id": "inj-001-ignore-instructions",
                "pattern_set_version": ACTIVE_INJECTION_PATTERN_SET_VERSION,
                "location_tag": "chunk_id=c1",
                "connector_type": "",
                "detail_code": "",
            }],
        })
        tc, _, _ = build_v2_certificate(inp=inp)
        d = tc.to_dict()
        assert d["governance_rule_matches"] == []
        rec = next(r for r in d["c3_provenance"]
                   if r["source_type"] == "injection_scan")
        assert rec["pattern_id"] == "inj-001-ignore-instructions"
        assert rec["pattern_set_version"] == \
            ACTIVE_INJECTION_PATTERN_SET_VERSION
        assert rec["location_tag"] == "chunk_id=c1"
        with CertificateStore(":memory:") as store:
            store.issue(tc)
            assert store.count() == 1

    def test_credential_detection_source_seals_without_rule_match(self):
        inp = c3_zero_input(meta_extra={
            "credential_detected": True,
            "credential_reason": "chunk_id=c9: credential pattern (explanatory)",
            "c3_signals": [{
                "source_type": "credential_detection",
                "pattern_id": "cred-002-openai-style-key",
                "pattern_set_version": ACTIVE_CREDENTIAL_PATTERN_SET_VERSION,
                "location_tag": "chunk_id=c9",
                "connector_type": "",
                "detail_code": "",
            }],
        })
        tc, _, _ = build_v2_certificate(inp=inp)
        d = tc.to_dict()
        rec = next(r for r in d["c3_provenance"]
                   if r["source_type"] == "credential_detection")
        assert rec["pattern_id"] == "cred-002-openai-style-key"
        assert rec["pattern_set_version"] == \
            ACTIVE_CREDENTIAL_PATTERN_SET_VERSION

    def test_connector_event_source_seals_via_explicit_record(self):
        rec = C3ProvenanceRecord(
            schema_version=C3_PROVENANCE_SCHEMA_VERSION,
            source_type="connector_event",
            pattern_id="", pattern_set_version="",
            location_tag="", connector_type="api.generic_rest",
            detail_code="unauthorized_endpoint", producer_id="",
        )
        tc, _, _ = build_v2_certificate(
            inp=c3_zero_input(), c3_provenance=[rec],
        )
        d = tc.to_dict()
        assert d["c3_provenance"][0]["source_type"] == "connector_event"

    def test_caller_supplied_requires_producer_id(self):
        with pytest.raises(CertificateInvariantError, match="producer_id"):
            validate_c3_provenance_record(caller_record(producer=""))

    def test_rule_source_requires_resolvable_refs(self):
        rec = C3ProvenanceRecord(
            schema_version=C3_PROVENANCE_SCHEMA_VERSION,
            source_type="rule",
            pattern_id="", pattern_set_version="", location_tag="",
            connector_type="", detail_code="", producer_id="",
            rule_match_refs=(RuleMatchRef("ghost-rule", "v9"),),
        )
        with pytest.raises(CertificateInvariantError, match="resolve"):
            build_v2_certificate(inp=c3_zero_input(), c3_provenance=[rec])

    def test_unexplained_zero_fails_closed(self):
        with pytest.raises(CertificateInvariantError, match="explains"):
            build_v2_certificate(inp=c3_zero_input())

    def test_unknown_pattern_set_version_fails_closed(self):
        rec = C3ProvenanceRecord(
            schema_version=C3_PROVENANCE_SCHEMA_VERSION,
            source_type="injection_scan",
            pattern_id="inj-001-ignore-instructions",
            pattern_set_version="tcs-injection-patterns-v99",
            location_tag="prompt",
            connector_type="", detail_code="", producer_id="",
        )
        with pytest.raises(CertificateInvariantError, match="unknown"):
            validate_c3_provenance_record(rec)

    def test_wrong_record_schema_version_fails_closed(self):
        rec = C3ProvenanceRecord(
            schema_version=99, source_type="caller_supplied",
            pattern_id="", pattern_set_version="", location_tag="",
            connector_type="", detail_code="", producer_id="x",
        )
        with pytest.raises(CertificateInvariantError, match="schema_version"):
            validate_c3_provenance_record(rec)


# =========================================================================== #
# Append-only pattern mappings                                                 #
# =========================================================================== #

class TestPatternMappingConformance:
    def test_active_injection_mapping_covers_live_table_exactly(self):
        mapping = INJECTION_PATTERN_IDS_BY_VERSION[
            ACTIVE_INJECTION_PATTERN_SET_VERSION]
        live = {p.pattern for p in _INJECTION_PATTERNS}
        assert set(mapping.keys()) == live, (
            "Injection pattern table changed: add a NEW pattern-set "
            "version with a complete mapping — never rewrite the frozen "
            "historical mapping."
        )
        assert len(set(mapping.values())) == len(mapping)

    def test_active_credential_mapping_covers_live_table_exactly(self):
        mapping = CREDENTIAL_PATTERN_IDS_BY_VERSION[
            ACTIVE_CREDENTIAL_PATTERN_SET_VERSION]
        live = {p.pattern for p in _CREDENTIAL_PATTERNS}
        assert set(mapping.keys()) == live
        assert len(set(mapping.values())) == len(mapping)

    def test_registries_are_version_keyed_and_contain_active(self):
        assert ACTIVE_INJECTION_PATTERN_SET_VERSION in \
            INJECTION_PATTERN_IDS_BY_VERSION
        assert ACTIVE_CREDENTIAL_PATTERN_SET_VERSION in \
            CREDENTIAL_PATTERN_IDS_BY_VERSION
        for registry in (INJECTION_PATTERN_IDS_BY_VERSION,
                         CREDENTIAL_PATTERN_IDS_BY_VERSION):
            for version, mapping in registry.items():
                assert version and isinstance(mapping, dict) and mapping


class TestStaticRuleText:
    """blocking_reason / explanation stay only because they are static
    rule-definition text: no interpolation placeholders anywhere in the
    shipped libraries."""

    @pytest.mark.parametrize("rule", list(SCENARIO_RULES)
                             + list(TYPED_CONTEXT_RULES),
                             ids=lambda r: r.rule_id)
    def test_effect_text_is_static(self, rule):
        for text in (rule.effect.blocking_reason, rule.effect.explanation):
            if text is None:
                continue
            assert "{" not in text and "}" not in text
            assert "%s" not in text and "%d" not in text


# =========================================================================== #
# Post-hash tamper evidence                                                    #
# =========================================================================== #

class TestTamperEvidence:
    def test_payload_field_mutation_breaks_verification(self):
        tc, _, _ = build_v2_certificate()
        original_hash = tc.audit_integrity.tc_hash
        d = tc.to_dict()
        # Same-bucket serialized mutation: still canonical form, wrong
        # value — replay catches it.
        d["s_base"] = "0.9276" if d["s_base"] != "0.9276" else "0.9275"
        with pytest.raises(CertificateInvariantError):
            compute_tc_hash(d)
        del original_hash

    def test_provenance_mutation_changes_hash_or_fails(self):
        tc, _, _ = build_v2_certificate(
            inp=c3_zero_input(), c3_provenance=[caller_record()],
        )
        d = tc.to_dict()
        d["c3_provenance"][0]["producer_id"] = "someone-else"
        # Shape is still valid — the payload bytes change, so the
        # recorded hash no longer verifies.
        assert compute_tc_hash(d) != tc.audit_integrity.tc_hash

    def test_calculation_version_flip_fails_verification(self):
        tc, _, _ = build_v2_certificate()
        d = tc.to_dict()
        d["calculation_version"] = "tis-v1"
        with pytest.raises(UnsupportedCalculationVersion):
            compute_tc_hash(d)

    def test_same_bucket_internal_mutation_refused_by_serializer(self):
        tc, _, _ = build_v2_certificate()
        tc.s_base = Decimal("0.92749999")   # same 4dp bucket, not canonical
        with pytest.raises(CertificateInvariantError):
            tc.to_dict()
        tc2, _, _ = build_v2_certificate()
        tc2.component_scores["B"] = Decimal("0.899996")
        with pytest.raises(CertificateInvariantError):
            tc2.to_dict()

    def test_adjustment_tamper_changes_payload(self):
        from dataclasses import replace as dc_replace
        inp = make_v2_input(meta={
            "chain_id": "chain-adj",
            "identity_confidence": 0.20,
            "identity_verified": True,
            "sensitivity_tier": "T2",
        })
        tc, _, _ = build_v2_certificate(inp=inp)
        assert tc.adjustments_applied           # rule 19.1 fired
        d = tc.to_dict()
        d["adjustments_applied"][0]["rule_id"] = "TCS_SPEC_19_9"
        assert compute_tc_hash(d) != tc.audit_integrity.tc_hash
        del dc_replace

    def test_stored_v2_raw_injection_detected(self):
        tc, _, _ = build_v2_certificate()
        with CertificateStore(":memory:") as store:
            issued = store.issue(tc)
            chain = issued.audit_integrity.chain_id
            assert store.verify_chain(chain) is True
            raws = store._list_chain_raw(chain)
            tampered = [dict(raws[0], injected="x")]
            import unittest.mock as mock
            with mock.patch.object(
                store, "_list_chain_raw", lambda cid: tampered,
            ):
                assert store.verify_chain(chain) is False


# =========================================================================== #
# Store round-trip, mixed chains                                               #
# =========================================================================== #

class TestStoreRoundTrip:
    def test_v2_store_round_trip_is_byte_identical(self):
        tc, _, _ = build_v2_certificate()
        with CertificateStore(":memory:") as store:
            issued = store.issue(tc)
            restored = store.get(issued.certificate_id)
            assert restored.to_dict() == issued.to_dict()
            assert restored.certificate_schema_version == 2
            assert isinstance(restored.s_base, Decimal)

    def test_mixed_v1_v2_chain_verifies(self):
        from tcs.decision_engine import map_decision
        from tcs.tis_engine import compute_tis
        chain = "chain-mixed"
        with CertificateStore(":memory:") as store:
            v1_inp = make_tis_input(
                "fin-high-risk-suitability-v3",
                {"B": 0.94, "A": 0.90, "C": 0.92, "K": 0.83},
                context_metadata={"chain_id": chain},
            )
            v1_res = compute_tis(v1_inp)
            d1, r1 = map_decision(v1_inp, v1_res)
            store.issue(generate_certificate(v1_inp, v1_res, d1, r1))

            tc2, _, _ = build_v2_certificate(
                inp=make_v2_input(meta={"chain_id": chain}))
            store.issue(tc2)

            tc3, _, _ = build_v2_certificate(
                inp=make_v2_input(meta={"chain_id": chain},
                                  subject_id="v2-core-b"))
            store.issue(tc3)

            assert store.verify_chain(chain) is True
            assert store.count() == 3

    def test_from_dict_v2_round_trip(self):
        tc, _, _ = build_v2_certificate()
        payload = tc.to_dict()
        restored = tc_from_dict_v2(payload)
        assert restored.to_dict() == payload


# =========================================================================== #
# The deterministic >= 10,000-case serialized replay                           #
# =========================================================================== #

class TestSerializedReplay10k:
    N_CASES = 10_000

    def test_ten_thousand_case_serialized_certificate_replay(self):
        rng = random.Random(20260729)
        profile_ids = sorted(PROFILES.keys())
        profiles = {pid: load_profile(pid) for pid in profile_ids}
        from tcs.decision_engine import _apply_priority_ladder_v2

        for i in range(self.N_CASES):
            pid = profile_ids[rng.randrange(len(profile_ids))]
            scores = {dim: Decimal(f"{rng.random():.6f}") for dim in BACK}
            zero_c3 = rng.random() < 0.08
            meta = {
                "chain_id": f"chain-replay-{i}",
                "n_gaps": rng.randrange(0, 30),
                "context_age_hours": round(rng.uniform(0.0, 4.0), 4),
                "novelty_score": f"{rng.random():.4f}",
                "days_since_review": rng.randrange(0, 30),
                "is_policy_sensitive": rng.random() < 0.5,
                "identity_confidence": f"{rng.random():.6f}",
                "identity_verified": rng.random() < 0.85,
                "sensitivity_tier": ("T1", "T2", "T3")[rng.randrange(3)],
            }
            inp = TISInput(
                subject_id=f"replay-{i}", subject_type="model_output",
                policy_profile=profiles[pid],
                dimension_scores=scores,
                sub_factor_scores=(
                    {"C": {"C3": 0.0}} if zero_c3 else {}
                ),
                context_metadata=meta,
                elapsed_hours=round(rng.uniform(0.0, 24.0), 4),
                is_valid=1,
                invalidation_event=(
                    "model_version_change" if rng.random() < 0.05 else None
                ),
                evaluation_time=EVAL_TIME,
            )
            res = compute_tis_v2(inp)
            decision, review = map_decision_versioned(inp, res)
            tc = generate_certificate_v2(
                inp, res, decision, review,
                c3_provenance=(
                    [caller_record(f"tests.replay::case-{i}")]
                    if res.C3_score == Decimal("0.0000") else None
                ),
            )

            # 1. Replay from the SERIALIZED payload with independent
            #    pinned-context arithmetic, including the final quantize.
            payload = tc.to_dict()
            ctx = f"case {i} profile {pid}"
            with localcontext(TIS_DECIMAL_CONTEXT):
                recomputed_s_base = sum(
                    (Decimal(payload["component_weights"][k])
                     * Decimal(payload["component_scores"][k])
                     for k in payload["component_weights"]),
                    Decimal("0"),
                ).quantize(SCORE_QUANTUM, rounding=SCORE_ROUNDING)
            if recomputed_s_base == 0:
                recomputed_s_base = Decimal("0.0000")
            assert recomputed_s_base == Decimal(payload["s_base"]), ctx

            replayed = _apply_priority_ladder_v2(
                is_valid=payload["is_valid"],
                c3_score=Decimal(payload["c3_score"]),
                gate=payload["gate_result"],
                s_base=Decimal(payload["s_base"]),
                tis_current=Decimal(payload["tis_current"]),
                kappa=Decimal(payload["resolved_kappa"]),
                theta_allow=Decimal(payload["resolved_theta_allow"]),
                theta_hold=Decimal(payload["resolved_theta_hold"]),
                theta_escalate=Decimal(payload["resolved_theta_escalate"]),
                risk_tier=payload["risk_tier"],
            )
            assert replayed == payload["decision"], ctx
            if Decimal(payload["c3_score"]) == Decimal("0.0000"):
                assert payload["decision"] == "Stop", ctx

            # 2b. Canonical serialized form.
            assert FIXED_4DP.fullmatch(payload["s_base"]), ctx
            assert FIXED_4DP.fullmatch(payload["tis_current"]), ctx
            for k, v in payload["component_scores"].items():
                assert FIXED_4DP.fullmatch(v), (ctx, k)

            # 3. Round-trip losslessness, asserted SEPARATELY, on a
            #    deterministic subsample (every 20th case) — each
            #    from_dict_v2 call re-runs the complete sealing
            #    validation, so the subsample keeps runtime sane while
            #    every certificate in the loop has already passed the
            #    full contract at construction.
            if i % 20 == 0:
                restored = tc_from_dict_v2(payload)
                assert restored.to_dict() == payload, ctx
                assert compute_tc_hash(payload) == \
                    tc.audit_integrity.tc_hash, ctx


# =========================================================================== #
# Production dormancy — full tcs/ tree, imports and aliases included           #
# =========================================================================== #

_V2_ENTRY_POINTS = {"compute_tis_v2", "generate_certificate_v2",
                    "map_decision_v2"}
_DEFINITION_MODULES = {"tis_engine.py", "trust_certificate.py",
                       "decision_engine.py"}


class TestProductionDormancy:
    def test_no_production_module_references_v2_entry_points(self):
        tcs_root = Path(tcs.__file__).parent
        offenders = []
        for py in sorted(tcs_root.rglob("*.py")):
            if py.name in _DEFINITION_MODULES:
                continue
            tree = ast.parse(py.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                referenced = set()
                if isinstance(node, ast.Name):
                    referenced.add(node.id)
                elif isinstance(node, ast.Attribute):
                    referenced.add(node.attr)
                elif isinstance(node, ast.ImportFrom):
                    # Detects renamed imports too:
                    #   from tcs.tis_engine import compute_tis_v2 as x
                    referenced.update(a.name for a in node.names)
                elif isinstance(node, ast.Import):
                    referenced.update(a.name for a in node.names)
                hits = referenced & _V2_ENTRY_POINTS
                if hits:
                    offenders.append((str(py.relative_to(tcs_root)),
                                      sorted(hits)))
        assert not offenders, (
            f"v2 entry points referenced outside their definition "
            f"modules before Commit 5: {offenders}"
        )
