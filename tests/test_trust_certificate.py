"""
Unit tests for tcs.trust_certificate.

Focus: schema compliance (TC_SCHEMA.md), lifecycle state derivation,
blocking_reason format, and JSON serialization round-tripping.
"""

from __future__ import annotations

import json
import re
import uuid

import pytest

from tcs.tis_engine import compute_tis
from tcs.decision_engine import map_decision
from tcs.trust_certificate import (
    generate_certificate,
    compute_tc_hash,
    DECISION_TO_LIFECYCLE,
)

from tests.conftest import make_tis_input


UUID4_PATTERN = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)


def _make_tc(scores, **overrides):
    """Helper: build a TC end-to-end from dimension scores."""
    inp = make_tis_input(
        profile_id=overrides.pop("profile_id", "fin-high-risk-suitability-v3"),
        dimension_scores=scores,
        **overrides,
    )
    r = compute_tis(inp)
    d, review = map_decision(inp, r)
    return generate_certificate(inp, r, d, review), r, d


# --------------------------------------------------------------------------- #
# Schema compliance                                                            #
# --------------------------------------------------------------------------- #

class TestSchemaCompliance:
    def test_certificate_id_is_uuid4(self):
        tc, _, _ = _make_tc({"B": 0.94, "A": 0.90, "C": 0.92, "K": 0.83})
        assert UUID4_PATTERN.match(tc.certificate_id)
        # Also verify parseable by uuid module
        parsed = uuid.UUID(tc.certificate_id)
        assert parsed.version == 4

    def test_three_distinct_tis_fields(self):
        """C-R.10: tis_raw, tis_adjusted, tis_current recorded separately."""
        tc, r, _ = _make_tc({"B": 0.94, "A": 0.90, "C": 0.92, "K": 0.83})
        assert tc.tis_raw == r.tis_raw
        assert tc.tis_adjusted == r.tis_adj
        assert tc.tis_current == r.tis_current

    def test_all_four_component_scores(self):
        tc, _, _ = _make_tc({"B": 0.94, "A": 0.90, "C": 0.92, "K": 0.83})
        assert set(tc.component_scores.keys()) == {"B", "A", "C", "K"}

    def test_all_four_component_weights_sum_to_one(self):
        tc, _, _ = _make_tc({"B": 0.94, "A": 0.90, "C": 0.92, "K": 0.83})
        assert set(tc.component_weights.keys()) == {"B", "A", "C", "K"}
        assert abs(sum(tc.component_weights.values()) - 1.0) < 1e-9

    def test_all_five_penalty_components_always_present(self):
        """C-R.5: all five penalty components present regardless of value."""
        tc, _, _ = _make_tc({"B": 0.94, "A": 0.90, "C": 0.92, "K": 0.83})
        assert set(tc.penalty_breakdown.keys()) == {
            "P_cb", "P_d", "P_n", "P_h", "P_ps"
        }

    def test_all_four_gate_results_always_present(self):
        """Gate results must cover all four dimensions, including non-gated."""
        tc, _, _ = _make_tc(
            {"B": 0.88, "A": 0.82, "C": 0.85, "K": 0.45},
            profile_id="enterprise-info-standard-v1",  # U not gated
        )
        assert set(tc.gate_results.keys()) == {"B", "A", "C", "K"}
        assert tc.gate_results["K"] == "not_applicable"

    def test_thresholds_for_all_four_dimensions(self):
        tc, _, _ = _make_tc(
            {"B": 0.88, "A": 0.82, "C": 0.85, "K": 0.45},
            profile_id="enterprise-info-standard-v1",
        )
        assert set(tc.thresholds.keys()) == {"B", "A", "C", "K"}

    def test_valid_until_present(self):
        """C-P.6: valid_until must be present."""
        tc, _, _ = _make_tc({"B": 0.94, "A": 0.90, "C": 0.92, "K": 0.83})
        assert tc.valid_until is not None

    def test_state_transition_history_has_initial_entry(self):
        tc, _, _ = _make_tc({"B": 0.94, "A": 0.90, "C": 0.92, "K": 0.83})
        assert len(tc.state_transition_history) == 1
        entry = tc.state_transition_history[0]
        assert entry["from"] == "computed"
        assert entry["to"] == tc.lifecycle_state
        assert "reason" in entry
        assert "timestamp" in entry


# --------------------------------------------------------------------------- #
# Lifecycle state derivation                                                   #
# --------------------------------------------------------------------------- #

class TestLifecycleState:
    def test_allow_maps_to_admissible(self):
        tc, _, _ = _make_tc({"B": 0.95, "A": 0.95, "C": 0.95, "K": 0.90})
        assert tc.decision == "Allow"
        assert tc.lifecycle_state == "admissible"

    def test_hold_maps_to_computed(self):
        # Inputs chosen so gate fails (A<0.90) but S_base >= kappa=0.90 -> HOLD
        # under paper-aligned ladder (kappa as remediability floor).
        tc, _, _ = _make_tc({"B": 1.00, "A": 0.62, "C": 1.00, "K": 1.00})
        assert tc.decision == "Hold"
        assert tc.lifecycle_state == "computed"

    def test_escalate_maps_to_computed(self):
        tc, _, _ = _make_tc(
            {"B": 0.90, "A": 0.90, "C": 0.90, "K": 0.80},
            elapsed_hours=20.0,
        )
        assert tc.decision == "Escalate"
        assert tc.lifecycle_state == "computed"

    def test_stop_by_gate_maps_to_blocked(self):
        tc, _, _ = _make_tc(
            {"B": 0.90, "A": 0.90, "C": 0.50, "K": 0.85},
            profile_id="clinical-cds-samed-v2",
            sub_factor_scores={"C": {"C3": 0.0}},
        )
        assert tc.decision == "Stop"
        assert tc.lifecycle_state == "blocked"
        assert tc.invalidation_status == "valid"

    def test_stop_by_invalidation_maps_to_invalidated_not_blocked(self):
        """Invalidation wins over decision-based lifecycle mapping."""
        tc, _, _ = _make_tc(
            {"B": 0.95, "A": 0.95, "C": 0.95, "K": 0.95},
            is_valid=0,
            invalidation_event="model_version_change",
        )
        assert tc.decision == "Stop"
        assert tc.lifecycle_state == "invalidated"
        assert tc.invalidation_status == "invalidated"

    def test_decision_to_lifecycle_mapping_complete(self):
        """Every decision type must have a lifecycle mapping."""
        for decision in ("Allow", "Observe", "Hold", "Escalate", "Stop"):
            assert decision in DECISION_TO_LIFECYCLE


# --------------------------------------------------------------------------- #
# blocking_reason and failure_mode derivation                                  #
# --------------------------------------------------------------------------- #

class TestBlockingReasonDerivation:
    def test_allow_no_blocking_reason(self):
        tc, _, _ = _make_tc({"B": 0.95, "A": 0.95, "C": 0.95, "K": 0.90})
        assert tc.blocking_reason is None
        assert tc.failure_mode is None

    def test_c3_hard_stop_reason(self):
        tc, _, _ = _make_tc(
            {"B": 0.90, "A": 0.90, "C": 0.50, "K": 0.85},
            profile_id="clinical-cds-samed-v2",
            sub_factor_scores={"C": {"C3": 0.0}},
        )
        assert tc.blocking_reason == "C3_prohibited_pattern"
        assert tc.failure_mode == "C3_prohibited_pattern"

    def test_c3_hard_stop_with_blocking_context_suffix(self):
        tc, _, _ = _make_tc(
            {"B": 0.90, "A": 0.90, "C": 0.50, "K": 0.85},
            profile_id="clinical-cds-samed-v2",
            sub_factor_scores={"C": {"C3": 0.0}},
            context_metadata={"blocking_context": "test_prohibited_combo"},
        )
        assert tc.blocking_reason == "C3_prohibited_pattern_test_prohibited_combo"

    def test_gate_fail_reason_includes_dim_and_threshold(self):
        tc, _, _ = _make_tc({"B": 0.94, "A": 0.76, "C": 0.92, "K": 0.88})
        assert tc.blocking_reason is not None
        assert "attribution_gate_fail" in tc.blocking_reason
        assert "A=0.76" in tc.blocking_reason
        assert "threshold=0.9" in tc.blocking_reason
        assert tc.failure_mode == "A_gate_fail"

    def test_invalidation_reason(self):
        tc, _, _ = _make_tc(
            {"B": 0.95, "A": 0.95, "C": 0.95, "K": 0.95},
            is_valid=0,
            invalidation_event="policy_update",
        )
        assert tc.blocking_reason == "invalidation_policy_update"
        assert tc.failure_mode == "invalidated"


# --------------------------------------------------------------------------- #
# Explanation layer                                                            #
# --------------------------------------------------------------------------- #

class TestExplanation:
    def test_summary_is_non_trivial(self):
        tc, _, _ = _make_tc({"B": 0.94, "A": 0.90, "C": 0.92, "K": 0.83})
        assert len(tc.explanation_summary) > 50
        # Should mention the decision
        assert "Allow" in tc.explanation_summary
        # Should mention the profile id
        assert "fin-high-risk-suitability-v3" in tc.explanation_summary

    def test_key_factors_and_concerns_populated(self):
        tc, _, _ = _make_tc({"B": 0.94, "A": 0.90, "C": 0.92, "K": 0.83})
        assert len(tc.key_factors) > 0
        assert len(tc.key_concerns) > 0

    def test_regulatory_explanation_level(self):
        tc, _, _ = _make_tc({"B": 0.94, "A": 0.90, "C": 0.92, "K": 0.83})
        assert tc.regulatory_explanation_level == "regulatory"

    def test_r3_has_regulatory_mapping(self):
        """r3/a4 subjects must carry a regulatory_mapping."""
        tc, _, _ = _make_tc({"B": 0.94, "A": 0.90, "C": 0.92, "K": 0.83})
        assert len(tc.regulatory_mapping) > 0


# --------------------------------------------------------------------------- #
# Serialization                                                                #
# --------------------------------------------------------------------------- #

class TestSerialization:
    def test_to_dict_returns_dict(self):
        tc, _, _ = _make_tc({"B": 0.94, "A": 0.90, "C": 0.92, "K": 0.83})
        d = tc.to_dict()
        assert isinstance(d, dict)

    def test_to_json_produces_valid_json(self):
        tc, _, _ = _make_tc({"B": 0.94, "A": 0.90, "C": 0.92, "K": 0.83})
        s = tc.to_json()
        parsed = json.loads(s)
        assert isinstance(parsed, dict)

    def test_to_dict_and_to_json_agree(self):
        tc, _, _ = _make_tc({"B": 0.94, "A": 0.90, "C": 0.92, "K": 0.83})
        assert tc.to_dict() == json.loads(tc.to_json())

    def test_datetimes_serialize_to_iso8601(self):
        tc, _, _ = _make_tc({"B": 0.94, "A": 0.90, "C": 0.92, "K": 0.83})
        d = tc.to_dict()
        # YYYY-MM-DDTHH:MM:SSZ
        assert re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$",
                        d["evaluation_timestamp"])
        assert re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$",
                        d["valid_until"])

    def test_floats_rounded_in_serialization(self):
        tc, _, _ = _make_tc({"B": 0.94, "A": 0.90, "C": 0.92, "K": 0.83})
        d = tc.to_dict()
        # Spot-check — any float value in the top level should be 4dp.
        for key in ("tis_raw", "tis_adjusted", "tis_current",
                    "penalty_aggregate", "decay_rate"):
            assert round(d[key], 4) == d[key]

    def test_serialization_caller_isolation(self):
        """Mutating to_dict() output should not affect the TC."""
        tc, _, _ = _make_tc({"B": 0.94, "A": 0.90, "C": 0.92, "K": 0.83})
        d = tc.to_dict()
        d["penalty_breakdown"]["P_cb"] = 999.0
        # Original TC still has its original value.
        assert tc.penalty_breakdown["P_cb"] != 999.0


# --------------------------------------------------------------------------- #
# Recompute-required flag                                                      #
# --------------------------------------------------------------------------- #

class TestRecomputeFlag:
    def test_r3_requires_recompute(self):
        tc, _, _ = _make_tc({"B": 0.94, "A": 0.90, "C": 0.92, "K": 0.83})
        assert tc.recompute_required is True

    def test_r1_does_not_require_recompute(self):
        tc, _, _ = _make_tc(
            {"B": 0.88, "A": 0.82, "C": 0.85, "K": 0.45},
            profile_id="enterprise-info-standard-v1",
        )
        assert tc.recompute_required is False


# --------------------------------------------------------------------------- #
# Trust Enforcement Layer (TCS-TEL-001 §19)                                    #
# --------------------------------------------------------------------------- #
#
# The three assertions required for Phase 1 TEL completion:
#
#   (a) tc_hash verifies correctly when recomputed
#   (b) governance_status == "complete" and
#       evaluation_completeness_score == 1.0 for every passing scenario
#   (c) override_invoked == False for every Phase 1 scenario
#
# Implemented as a parametrized test that runs each of the 8 canonical
# scenarios through the full pipeline and then checks the three
# invariants on the resulting TC. Failures here mean TEL wiring has
# drifted from the Phase 1 contract.

_PHASE_1_SCENARIOS = [
    # (name, profile_id, dimension_scores, context_metadata, kwargs)
    (
        "healthcare_stop",
        "clinical-cds-samed-v2",
        {"B": 0.92, "A": 0.88, "C": 0.31, "K": 0.84},
        {
            "n_gaps": 0, "context_age_hours": 0.1, "novelty_score": 0.05,
            "days_since_review": 2, "is_policy_sensitive": False,
            "blocking_context": "warfarin_clarithromycin_GI_bleed",
        },
        {
            "sub_factor_scores": {
                "C": {"C1": 0.90, "C2": 0.85, "C3": 0.00,
                      "C4": 0.88, "C5": 0.00}
            },
        },
    ),
    (
        "healthcare_allow_uncertainty",
        "clinical-cds-samed-v2",
        {"B": 0.91, "A": 0.93, "C": 0.94, "K": 0.82},
        {
            "n_gaps": 0, "context_age_hours": 0.2, "novelty_score": 0.60,
            "days_since_review": 3, "is_policy_sensitive": False,
        },
        {
            "sub_factor_scores": {
                "C": {"C1": 0.92, "C2": 0.90, "C3": 1.00,
                      "C4": 0.88, "C5": 0.76}
            },
        },
    ),
    (
        "finance_allow",
        "fin-high-risk-suitability-v3",
        {"B": 0.94, "A": 0.90, "C": 0.92, "K": 0.83},
        {
            "n_gaps": 0, "context_age_hours": 0.5, "novelty_score": 0.10,
            "days_since_review": 1, "is_policy_sensitive": True,
        },
        {},
    ),
    (
        "finance_hold_attribution",
        "fin-high-risk-suitability-v3",
        {"B": 0.94, "A": 0.76, "C": 0.92, "K": 0.88},
        {
            "n_gaps": 1, "context_age_hours": 0.3, "novelty_score": 0.05,
            "days_since_review": 1, "is_policy_sensitive": False,
        },
        {},
    ),
    (
        "enterprise_info_allow",
        "enterprise-info-standard-v1",
        {"B": 0.88, "A": 0.82, "C": 0.85, "K": 0.45},
        {
            "n_gaps": 0, "context_age_hours": 0.5, "novelty_score": 0.10,
            "days_since_review": 5, "is_policy_sensitive": False,
        },
        {"subject_type": "model_output"},
    ),
    (
        "high_risk_uncertainty_gate",
        "clinical-cds-samed-v2",
        {"B": 0.90, "A": 0.88, "C": 0.91, "K": 0.72},
        {
            "n_gaps": 0, "context_age_hours": 0.1, "novelty_score": 0.50,
            "days_since_review": 4, "is_policy_sensitive": False,
        },
        {},
    ),
    (
        "invalidation_event",
        "fin-high-risk-suitability-v3",
        {"B": 0.95, "A": 0.95, "C": 0.95, "K": 0.95},
        {
            "n_gaps": 0, "context_age_hours": 0.1, "novelty_score": 0.0,
            "days_since_review": 1, "is_policy_sensitive": False,
        },
        {
            "elapsed_hours": 2.0,
            "is_valid": 0,
            "invalidation_event": "model_version_change",
        },
    ),
    (
        "decay_over_time",
        "fin-high-risk-suitability-v3",
        {"B": 0.95, "A": 0.92, "C": 0.94, "K": 0.88},
        {
            "n_gaps": 0, "context_age_hours": 0.1, "novelty_score": 0.05,
            "days_since_review": 1, "is_policy_sensitive": False,
        },
        {"elapsed_hours": 0.0},
    ),
]


def _run_scenario(scenario):
    """Run a Phase-1 scenario tuple through the full pipeline -> (inp, r, tc)."""
    name, profile_id, scores, meta, kwargs = scenario
    inp = make_tis_input(
        profile_id=profile_id,
        dimension_scores=scores,
        context_metadata=meta,
        **kwargs,
    )
    r = compute_tis(inp)
    d, review = map_decision(inp, r)
    tc = generate_certificate(inp, r, d, review)
    return inp, r, tc


class TestTrustEnforcementLayer:
    """
    Phase 1 TEL assertions on the canonical 8 scenarios.

    These tests are the Phase 1 acceptance contract for TCS-TEL-001.
    They do not replace the existing tests/test_scenarios.py contract;
    they augment it with the three TEL invariants spelled out in the
    CLAUDE.md "Trust Enforcement Layer" section.
    """

    @pytest.mark.parametrize(
        "scenario", _PHASE_1_SCENARIOS, ids=[s[0] for s in _PHASE_1_SCENARIOS]
    )
    def test_tc_hash_verifies_on_recompute(self, scenario):
        """(a) Every TC's stored tc_hash must match a fresh recompute."""
        _, _, tc = _run_scenario(scenario)
        assert tc.audit_integrity is not None, \
            "audit_integrity layer missing from TC"
        assert tc.audit_integrity.hash_algorithm == "sha256"
        recomputed = compute_tc_hash(tc.to_dict())
        assert recomputed == tc.audit_integrity.tc_hash, (
            f"tc_hash mismatch on recompute.\n"
            f"  stored:     {tc.audit_integrity.tc_hash}\n"
            f"  recomputed: {recomputed}"
        )
        # Also verify the hash is stable across a JSON round-trip —
        # if canonicalization is wrong, this is where it surfaces.
        reparsed = json.loads(tc.to_json())
        assert compute_tc_hash(reparsed) == tc.audit_integrity.tc_hash

    @pytest.mark.parametrize(
        "scenario", _PHASE_1_SCENARIOS, ids=[s[0] for s in _PHASE_1_SCENARIOS]
    )
    def test_governance_status_complete(self, scenario):
        """
        (b) Every Phase 1 scenario must produce a TC with
        governance_status='complete' and evaluation_completeness_score=1.0.
        Phase 2 scenario 15 will be the first scenario to see 'degraded'.
        """
        _, _, tc = _run_scenario(scenario)
        assert tc.governance_status is not None
        assert tc.governance_status.governance_status == "complete"
        assert tc.governance_status.evaluation_completeness_score == 1.0
        assert tc.governance_status.fail_safe_applied is False
        assert tc.governance_status.fail_safe_type is None
        # Every component must have been evaluated; none skipped.
        assert len(tc.governance_status.components_evaluated) > 0
        assert tc.governance_status.components_skipped == []

    @pytest.mark.parametrize(
        "scenario", _PHASE_1_SCENARIOS, ids=[s[0] for s in _PHASE_1_SCENARIOS]
    )
    def test_override_not_invoked(self, scenario):
        """
        (c) Phase 1 scenarios never invoke a human override. Every field
        in the override_record except override_invoked must be None/False.
        Phase 2 scenario 16 will be the first scenario to populate this.
        """
        _, _, tc = _run_scenario(scenario)
        assert tc.override_record is not None
        assert tc.override_record.override_invoked is False
        assert tc.override_record.original_decision is None
        assert tc.override_record.override_decision is None
        assert tc.override_record.override_actor is None
        assert tc.override_record.post_override_review_required is False
        assert tc.override_record.post_override_review_completed is False

    def test_identity_binding_populated_for_all_scenarios(self):
        """
        Every Phase 1 TC must carry a populated IdentityBinding with
        the Phase-1 optimistic stub defaults (verified, high-confidence,
        T3-capable human identity).
        """
        for scenario in _PHASE_1_SCENARIOS:
            _, _, tc = _run_scenario(scenario)
            assert tc.identity_binding is not None, \
                f"identity_binding missing for {scenario[0]}"
            assert tc.identity_binding.identity_type == "human"
            assert tc.identity_binding.identity_verified is True
            assert tc.identity_binding.identity_confidence == 1.0
            assert tc.identity_binding.authorization_tier == "T3"
            assert tc.identity_binding.requesting_identity is not None
            assert tc.identity_binding.requesting_session_id is not None

    def test_audit_integrity_chain_defaults(self):
        """
        Phase 1 TCs are single-TC chains: previous_tc_hash is None,
        chain_sequence is 1, chain_id is present, integrity_verified
        is True.
        """
        for scenario in _PHASE_1_SCENARIOS:
            _, _, tc = _run_scenario(scenario)
            ai = tc.audit_integrity
            assert ai is not None
            assert ai.previous_tc_hash is None
            assert ai.chain_sequence == 1
            assert ai.chain_id is not None and ai.chain_id.startswith("chain-")
            assert ai.integrity_verified is True
            assert ai.issued_by == "tcs-reference-impl-v0.1"
