"""
tests/test_healthcare_demo.py
==============================

Phase 3 Step 9 — Healthcare Clinical AI Demo tests.

Tests verify:
    1. All eight scenarios produce the expected decision
    2. Aggregation detection fires correctly on combined T1+T2+T2 context
    3. PHI redaction applies correctly
    4. Step-up auth TC field populated correctly
    5. Healthcare pack thresholds correctly applied throughout
    6. Hash chain verifies at end
    7. Demo exits with code 0
"""

from __future__ import annotations

import pytest

from tcs.persistence import CertificateStore
from tcs.sidecar import RequestInterceptor

# Import demo components
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from demos.healthcare.clinical_ai_demo import (
    DEMO_CHAIN_ID,
    DEMO_BASE_PROFILE,
    build_scenarios,
    run_one,
    main,
)


# --------------------------------------------------------------------------- #
# Fixtures                                                                     #
# --------------------------------------------------------------------------- #

@pytest.fixture
def store():
    s = CertificateStore(":memory:")
    yield s
    s.close()


@pytest.fixture
def interceptor(store):
    return RequestInterceptor(store)


@pytest.fixture
def scenarios():
    return build_scenarios()


@pytest.fixture
def all_results(store, interceptor, scenarios):
    return [run_one(s, interceptor, store) for s in scenarios]


# --------------------------------------------------------------------------- #
# Scenario decision tests                                                      #
# --------------------------------------------------------------------------- #

class TestScenarioDecisions:
    """All eight scenarios produce the expected decision."""

    def test_01_clean_clinical_recommendation(self, all_results):
        r = all_results[0]
        assert r.response.decision == "Allow"
        assert r.response.blocked is False
        assert r.matched_expectation is True

    def test_02_aggregation_t2_t2_t3(self, all_results):
        r = all_results[1]
        # Paper-aligned ladder: gate=0 + S_base < kappa -> Stop.
        assert r.response.decision == "Stop"
        assert r.response.blocked is True
        assert r.matched_expectation is True

    def test_03_missing_clinical_provenance(self, all_results):
        r = all_results[2]
        # Paper-aligned ladder: gate=0 + S_base < kappa -> Stop.
        assert r.response.decision == "Stop"
        assert r.response.blocked is True
        assert r.matched_expectation is True

    def test_04_treatment_before_confirmation(self, all_results):
        r = all_results[3]
        assert r.response.decision == "Stop"
        assert r.response.blocked is True
        assert r.matched_expectation is True

    def test_05_phi_in_output(self, all_results):
        r = all_results[4]
        assert r.response.decision == "Allow"
        assert r.response.blocked is False
        assert r.matched_expectation is True

    def test_06_low_confidence_differential(self, all_results):
        r = all_results[5]
        # Paper-aligned ladder: gate=0 + S_base < kappa -> Stop.
        assert r.response.decision == "Stop"
        assert r.response.blocked is True
        assert r.matched_expectation is True

    def test_07_physician_step_up(self, all_results):
        r = all_results[6]
        assert r.response.decision == "Allow"
        assert r.response.blocked is False
        assert r.matched_expectation is True

    def test_08_governance_degraded_failsafe(self, all_results):
        r = all_results[7]
        assert r.response.decision == "Stop"
        assert r.response.blocked is True
        assert r.response.fail_safe_applied is True
        assert r.matched_expectation is True

    def test_all_scenarios_match(self, all_results):
        for r in all_results:
            assert r.matched_expectation is True, (
                f"Scenario {r.scenario.scenario_id} ({r.scenario.name}): "
                f"expected {r.scenario.expected_decision}, "
                f"got {r.response.decision}"
            )


# --------------------------------------------------------------------------- #
# Aggregation detection tests                                                  #
# --------------------------------------------------------------------------- #

class TestAggregation:
    def test_aggregation_detected_in_context(self, all_results):
        r = all_results[1]  # aggregation_t2_t2_t3
        ctx = r.request.context_bundle
        assert ctx.get("aggregation_detected") is True

    def test_aggregation_components(self, all_results):
        r = all_results[1]
        ctx = r.request.context_bundle
        components = ctx.get("aggregation_components", [])
        assert "T1_chief_complaint" in components
        assert "T2_lab_values" in components
        assert "T2_imaging" in components

    def test_aggregation_sensitivity_tier(self, all_results):
        r = all_results[1]
        ctx = r.request.context_bundle
        assert ctx.get("sensitivity_tier") == "T3"

    def test_b_gate_fails_on_aggregation(self, all_results):
        r = all_results[1]
        assert r.tc is not None
        assert r.tc.component_scores["B"] < r.tc.thresholds["B"]


# --------------------------------------------------------------------------- #
# PHI redaction tests                                                          #
# --------------------------------------------------------------------------- #

class TestPHIRedaction:
    def test_phi_detected(self, all_results):
        r = all_results[4]  # phi_in_output
        ctx = r.request.context_bundle
        assert ctx.get("phi_detected") is True

    def test_redaction_applied(self, all_results):
        r = all_results[4]
        ctx = r.request.context_bundle
        assert ctx.get("redaction_applied") is True

    def test_redacted_fields(self, all_results):
        r = all_results[4]
        ctx = r.request.context_bundle
        fields = ctx.get("redacted_fields", [])
        assert "patient_name" in fields
        assert "dob" in fields
        assert "mrn" in fields

    def test_phi_scenario_still_allows(self, all_results):
        r = all_results[4]
        assert r.response.decision == "Allow"


# --------------------------------------------------------------------------- #
# Step-up authorization tests                                                  #
# --------------------------------------------------------------------------- #

class TestStepUpAuth:
    def test_step_up_required(self, all_results):
        r = all_results[6]  # physician_step_up
        ctx = r.request.context_bundle
        assert ctx.get("step_up_required") is True

    def test_step_up_authorization(self, all_results):
        r = all_results[6]
        ctx = r.request.context_bundle
        assert ctx.get("step_up_authorization") == "physician_override"

    def test_authorization_tier_mismatch(self, all_results):
        r = all_results[6]
        ctx = r.request.context_bundle
        assert ctx.get("authorization_tier") == "T2"
        assert ctx.get("requested_action_tier") == "T3"


# --------------------------------------------------------------------------- #
# Healthcare pack threshold tests                                              #
# --------------------------------------------------------------------------- #

class TestHealthcareThresholds:
    def test_uses_healthcare_profile(self, all_results):
        r = all_results[0]  # clean_clinical_recommendation
        assert r.tc is not None
        assert r.tc.domain == "healthcare"

    def test_risk_tier_r3(self, all_results):
        r = all_results[0]
        assert r.tc is not None
        assert r.tc.risk_tier == "r3"

    def test_c_gate_threshold_090(self, all_results):
        r = all_results[0]
        assert r.tc is not None
        assert r.tc.thresholds["C"] == 0.90

    def test_all_gates_evaluated(self, all_results):
        r = all_results[0]
        assert r.tc is not None
        assert set(r.tc.gate_set) == {"B", "A", "C", "K"}


# --------------------------------------------------------------------------- #
# Chain and integrity tests                                                    #
# --------------------------------------------------------------------------- #

class TestChainIntegrity:
    def test_chain_verifies(self, store, all_results):
        assert store.verify_chain(DEMO_CHAIN_ID)

    def test_committed_tc_count(self, store, all_results):
        # 7 TCs committed (scenario 08 is fail-safe, no TC)
        assert store.count() == 7

    def test_chain_sequence_monotonic(self, store, all_results):
        tcs = store.list_chain(DEMO_CHAIN_ID)
        seqs = [tc.audit_integrity.chain_sequence for tc in tcs]
        assert seqs == sorted(seqs)
        assert seqs == list(range(1, len(seqs) + 1))


# --------------------------------------------------------------------------- #
# Demo exit code test                                                          #
# --------------------------------------------------------------------------- #

class TestDemoMain:
    def test_demo_exits_zero(self):
        exit_code = main(["--db", ":memory:"])
        assert exit_code == 0

    def test_eight_scenarios(self):
        scenarios = build_scenarios()
        assert len(scenarios) == 8

    def test_scenario_names(self):
        scenarios = build_scenarios()
        names = [s.name for s in scenarios]
        assert "clean_clinical_recommendation" in names
        assert "aggregation_t2_t2_t3" in names
        assert "missing_clinical_provenance" in names
        assert "treatment_before_confirmation" in names
        assert "phi_in_output" in names
        assert "low_confidence_differential" in names
        assert "physician_step_up" in names
        assert "governance_degraded_failsafe" in names
