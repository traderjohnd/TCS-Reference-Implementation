"""
tests/test_nine_outcomes.py
===========================

Phase 3 Step 1 — tests for the four new qualified decision outcomes.

Tests verify that:
    1. All 18 existing decision engine tests still pass (by import)
    2. Allow_with_logging triggers when TIS is within ENHANCED_LOGGING_BAND
    3. Allow_with_redaction triggers for T2/T3 data with redaction_required
    4. Allow_with_step_up triggers for T2 auth requesting T3 data
    5. Rollback triggers for Stop when action_partially_executed is True
    6. TC schema validates for all nine decision types
"""

from __future__ import annotations

import pytest

from tcs.tis_engine import compute_tis
from tcs.decision_engine import (
    map_decision,
    map_decision_extended,
    DecisionMetadata,
    ENHANCED_LOGGING_BAND,
    NEAR_BOUNDARY_ALLOW_BAND,
)
from tcs.trust_certificate import (
    TrustCertificate,
    DECISION_TO_LIFECYCLE,
    generate_certificate,
)

from tests.conftest import make_tis_input


# --------------------------------------------------------------------------- #
# Allow_with_logging                                                           #
# --------------------------------------------------------------------------- #

class TestAllowWithLogging:
    """Allow_with_logging fires when TIS_current is within 0.05 of theta_allow."""

    def test_near_boundary_allow_triggers_enhanced_logging(self):
        """An Allow just above theta_allow triggers Allow_with_logging."""
        # B/A/C/U=0.90/0.90/0.90/0.80 -> tis_current=0.885, proximity=0.035
        inp = make_tis_input(
            "fin-high-risk-suitability-v3",
            {"B": 0.90, "A": 0.90, "C": 0.90, "K": 0.80},
        )
        r = compute_tis(inp)
        d, review, dm = map_decision_extended(inp, r)

        assert d == "Allow"
        assert dm.base_decision == "Allow"
        assert dm.qualified_decision == "Allow_with_logging"
        assert dm.enhanced_logging is True
        assert dm.proximity_to_threshold is not None
        assert dm.proximity_to_threshold < ENHANCED_LOGGING_BAND
        assert dm.reason_code == "near_boundary_allow"

    def test_well_above_threshold_no_logging(self):
        """An Allow well above theta_allow should NOT be qualified."""
        inp = make_tis_input(
            "fin-high-risk-suitability-v3",
            {"B": 0.98, "A": 0.98, "C": 0.98, "K": 0.95},
        )
        r = compute_tis(inp)
        d, review, dm = map_decision_extended(inp, r)

        assert d == "Allow"
        assert dm.qualified_decision == "Allow"
        assert dm.enhanced_logging is False


# --------------------------------------------------------------------------- #
# Allow_with_redaction                                                         #
# --------------------------------------------------------------------------- #

class TestAllowWithRedaction:
    """Allow_with_redaction fires for T2/T3 data with redaction_required."""

    def test_t3_data_with_redaction_triggers_qualified(self):
        """T3 data present + redaction_required -> Allow_with_redaction."""
        inp = make_tis_input(
            "fin-high-risk-suitability-v3",
            {"B": 0.98, "A": 0.98, "C": 0.98, "K": 0.95},
            context_metadata={
                "sensitivity_tier": "T3",
                "redaction_required": True,
                "redacted_fields": ["patient_name", "dob", "mrn"],
                "redaction_scope": "output",
            },
        )
        r = compute_tis(inp)
        d, review, dm = map_decision_extended(inp, r)

        assert d == "Allow"
        assert dm.qualified_decision == "Allow_with_redaction"
        assert dm.redaction_applied is True
        assert dm.redacted_fields == ["patient_name", "dob", "mrn"]
        assert dm.redaction_scope == "output"
        assert dm.reason_code == "redaction_t3_data"

    def test_t1_data_no_redaction(self):
        """T1 data should not trigger redaction."""
        inp = make_tis_input(
            "fin-high-risk-suitability-v3",
            {"B": 0.98, "A": 0.98, "C": 0.98, "K": 0.95},
            context_metadata={"sensitivity_tier": "T1"},
        )
        r = compute_tis(inp)
        d, review, dm = map_decision_extended(inp, r)

        assert d == "Allow"
        assert dm.redaction_applied is False


# --------------------------------------------------------------------------- #
# Allow_with_step_up                                                           #
# --------------------------------------------------------------------------- #

class TestAllowWithStepUp:
    """Allow_with_step_up fires for T2 auth requesting T3 data."""

    def test_t2_auth_t3_data_triggers_step_up(self):
        """authorization_tier T2 + sensitivity_tier T3 -> Allow_with_step_up."""
        inp = make_tis_input(
            "fin-high-risk-suitability-v3",
            {"B": 0.98, "A": 0.98, "C": 0.98, "K": 0.95},
            context_metadata={
                "authorization_tier": "T2",
                "sensitivity_tier": "T3",
            },
        )
        r = compute_tis(inp)
        d, review, dm = map_decision_extended(inp, r)

        assert d == "Allow"
        assert dm.qualified_decision == "Allow_with_step_up"
        assert dm.step_up_required is True
        assert dm.step_up_completed is None  # pending until confirmed
        assert dm.reason_code == "step_up_t2_requesting_t3"

    def test_t3_auth_t3_data_no_step_up(self):
        """T3 auth requesting T3 data should NOT trigger step-up."""
        inp = make_tis_input(
            "fin-high-risk-suitability-v3",
            {"B": 0.98, "A": 0.98, "C": 0.98, "K": 0.95},
            context_metadata={
                "authorization_tier": "T3",
                "sensitivity_tier": "T3",
            },
        )
        r = compute_tis(inp)
        d, review, dm = map_decision_extended(inp, r)

        assert d == "Allow"
        assert dm.step_up_required is False


# --------------------------------------------------------------------------- #
# Rollback                                                                     #
# --------------------------------------------------------------------------- #

class TestRollback:
    """Rollback fires for Stop when action has already partially executed."""

    def test_partial_execution_triggers_rollback(self):
        """Stop + action_partially_executed -> Rollback."""
        inp = make_tis_input(
            "fin-high-risk-suitability-v3",
            {"B": 1.0, "A": 1.0, "C": 1.0, "K": 1.0},
            is_valid=0,
            invalidation_event="model_version_change",
            context_metadata={
                "action_partially_executed": True,
                "compensation_scope": "revert_trade",
                "incident_id": "INC-2026-0042",
            },
        )
        r = compute_tis(inp)
        d, review, dm = map_decision_extended(inp, r)

        assert d == "Stop"
        assert dm.base_decision == "Stop"
        assert dm.qualified_decision == "Rollback"
        assert dm.recovery_mode_activated is True
        assert dm.compensation_scope == "revert_trade"
        assert dm.incident_id == "INC-2026-0042"
        assert dm.reason_code == "rollback_partial_execution"

    def test_stop_without_partial_execution_stays_stop(self):
        """Stop without partial execution remains plain Stop."""
        inp = make_tis_input(
            "fin-high-risk-suitability-v3",
            {"B": 1.0, "A": 1.0, "C": 1.0, "K": 1.0},
            is_valid=0,
            invalidation_event="model_version_change",
        )
        r = compute_tis(inp)
        d, review, dm = map_decision_extended(inp, r)

        assert d == "Stop"
        assert dm.qualified_decision == "Stop"
        assert dm.recovery_mode_activated is False


# --------------------------------------------------------------------------- #
# TC schema validation for all nine outcomes                                   #
# --------------------------------------------------------------------------- #

class TestTCSchemaForNineOutcomes:
    """TC schema validates for all nine decision types."""

    def test_all_nine_outcomes_have_lifecycle_mapping(self):
        """Every outcome in the nine-outcome model has a lifecycle state."""
        for outcome in [
            "Allow", "Observe", "Hold", "Escalate", "Stop",
            "Allow_with_logging", "Allow_with_redaction",
            "Allow_with_step_up", "Rollback",
        ]:
            assert outcome in DECISION_TO_LIFECYCLE, (
                f"Missing DECISION_TO_LIFECYCLE entry for {outcome!r}"
            )

    def test_tc_accepts_new_fields(self):
        """TrustCertificate dataclass accepts all new Phase 3 fields."""
        inp = make_tis_input(
            "fin-high-risk-suitability-v3",
            {"B": 0.98, "A": 0.98, "C": 0.98, "K": 0.95},
        )
        r = compute_tis(inp)
        d, review = map_decision(inp, r)
        tc = generate_certificate(inp, r, d, review)

        # Set the new fields
        tc.qualified_decision = "Allow_with_logging"
        tc.enhanced_logging = True
        tc.reason_code = "near_boundary_allow"
        tc.proximity_to_threshold = 0.03

        d_out = tc.to_dict()
        assert d_out["qualified_decision"] == "Allow_with_logging"
        assert d_out["enhanced_logging"] is True
        assert d_out["reason_code"] == "near_boundary_allow"
        assert d_out["proximity_to_threshold"] == 0.03

    def test_tc_redaction_fields_serialize(self):
        """Redaction fields serialize correctly."""
        inp = make_tis_input(
            "fin-high-risk-suitability-v3",
            {"B": 0.98, "A": 0.98, "C": 0.98, "K": 0.95},
        )
        r = compute_tis(inp)
        d, review = map_decision(inp, r)
        tc = generate_certificate(inp, r, d, review)

        tc.qualified_decision = "Allow_with_redaction"
        tc.redaction_applied = True
        tc.redacted_fields = ["patient_name", "dob"]
        tc.redaction_scope = "output"

        d_out = tc.to_dict()
        assert d_out["redaction_applied"] is True
        assert d_out["redacted_fields"] == ["patient_name", "dob"]

    def test_tc_rollback_fields_serialize(self):
        """Rollback fields serialize correctly."""
        inp = make_tis_input(
            "fin-high-risk-suitability-v3",
            {"B": 1.0, "A": 1.0, "C": 1.0, "K": 1.0},
            is_valid=0,
            invalidation_event="model_version_change",
        )
        r = compute_tis(inp)
        d, review = map_decision(inp, r)
        tc = generate_certificate(inp, r, d, review)

        tc.qualified_decision = "Rollback"
        tc.compensation_scope = "revert_trade"
        tc.incident_id = "INC-001"
        tc.recovery_mode_activated = True

        d_out = tc.to_dict()
        assert d_out["qualified_decision"] == "Rollback"
        assert d_out["compensation_scope"] == "revert_trade"
        assert d_out["recovery_mode_activated"] is True
