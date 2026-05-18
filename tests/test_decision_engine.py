"""
Unit tests for tcs.decision_engine.

Focus: priority ladder branch coverage under the Option A threshold
values. Every branch in _apply_priority_ladder must have at least one
test that fires it.
"""

from __future__ import annotations

import pytest

from tcs.tis_engine import compute_tis
from tcs.decision_engine import map_decision, NEAR_BOUNDARY_ALLOW_BAND

from tests.conftest import make_tis_input


# --------------------------------------------------------------------------- #
# Priority ladder branch coverage                                              #
# --------------------------------------------------------------------------- #

class TestPriorityLadder:
    """Every priority in the ladder must have at least one firing test."""

    def test_priority_1_invalidation_stops(self):
        """Invalidation fires BEFORE gate/C3 checks, even with perfect scores."""
        inp = make_tis_input(
            "fin-high-risk-suitability-v3",
            {"B": 1.0, "A": 1.0, "C": 1.0, "K": 1.0},  # perfect gates
            is_valid=0,
            invalidation_event="model_version_change",
        )
        r = compute_tis(inp)
        d, _ = map_decision(inp, r)
        assert d == "Stop"

    def test_priority_2_c3_zero_hard_stop(self):
        """C3=0 produces Stop even when TIS_raw is within kappa."""
        inp = make_tis_input(
            "clinical-cds-samed-v2",
            {"B": 0.90, "A": 0.90, "C": 0.50, "K": 0.85},
            sub_factor_scores={"C": {"C3": 0.0}},
        )
        r = compute_tis(inp)
        d, _ = map_decision(inp, r)
        assert d == "Stop"

    def test_priority_3_gate_fail_below_kappa_stops(self):
        """Gate=0 and S_base < kappa -> Stop (too degraded to remediate)."""
        # fin profile: kappa=0.90.
        # B=0.94, A=0.76, C=0.92, K=0.88; A fails the 0.90 gate.
        # S_base = 0.30*0.94 + 0.25*0.76 + 0.30*0.92 + 0.15*0.88 = 0.880
        # S_base < 0.90 -> STOP (Priority 3, paper-aligned direction).
        inp = make_tis_input(
            "fin-high-risk-suitability-v3",
            {"B": 0.94, "A": 0.76, "C": 0.92, "K": 0.88},
        )
        r = compute_tis(inp)
        assert r.gate_result == 0
        assert r.s_base < 0.90
        d, _ = map_decision(inp, r)
        assert d == "Stop"

    def test_priority_4_gate_fail_at_or_above_kappa_holds(self):
        """
        Gate=0, S_base >= kappa, C3 != 0 -> Hold (remediable via review).

        clinical profile: kappa=0.90, K threshold=0.80.
        B=1.0, A=1.0, C=1.0, K=0.55: K fails the 0.80 gate.
        S_base = 0.25 + 0.20 + 0.35 + 0.20*0.55 = 0.910 >= 0.90 -> HOLD.
        """
        inp = make_tis_input(
            "clinical-cds-samed-v2",
            {"B": 1.0, "A": 1.0, "C": 1.0, "K": 0.55},
        )
        r = compute_tis(inp)
        assert r.gate_result == 0
        assert r.s_base >= 0.90
        d, _ = map_decision(inp, r)
        assert d == "Hold"

    def test_priority_5_below_escalate_escalates(self):
        """Gate=1 and TIS_current < theta_escalate -> Escalate."""
        # At r3 fin: theta_escalate=0.70. Need all gates pass but low total.
        # Hardest: B,A,C,K all at their thresholds, big decay.
        inp = make_tis_input(
            "fin-high-risk-suitability-v3",
            {"B": 0.90, "A": 0.90, "C": 0.90, "K": 0.80},
            elapsed_hours=20.0,   # big decay
        )
        r = compute_tis(inp)
        assert r.gate_result == 1
        assert r.tis_current < 0.70
        d, _ = map_decision(inp, r)
        assert d == "Escalate"

    def test_priority_6_score_path_hold_r2(self):
        """At r2, P6 fires for gate=1 and theta_escalate <= TIS < theta_allow."""
        inp = make_tis_input(
            "enterprise-ops-standard-v1",   # r2/a3
            {"B": 0.80, "A": 0.75, "C": 0.80, "K": 0.70},
            elapsed_hours=10.0,
        )
        r = compute_tis(inp)
        assert r.gate_result == 1
        # r2 theta_escalate=0.65, theta_allow=0.80
        assert 0.65 <= r.tis_current < 0.80
        d, _ = map_decision(inp, r)
        assert d == "Hold"

    def test_priority_7_observe_r1_only(self):
        """At r1, [theta_hold, theta_allow) maps to Observe."""
        inp = make_tis_input(
            "enterprise-info-standard-v1",  # r1/a1
            {"B": 0.72, "A": 0.72, "C": 0.76, "K": 0.50},
        )
        r = compute_tis(inp)
        # Expected raw ~ 0.686, within [0.65, 0.75)
        assert r.gate_result == 1
        assert 0.65 <= r.tis_current < 0.75
        d, _ = map_decision(inp, r)
        assert d == "Observe"

    def test_priority_7_score_path_hold_r1_lower_band(self):
        """At r1, [theta_escalate, theta_hold) maps to Hold, not Observe."""
        inp = make_tis_input(
            "enterprise-info-standard-v1",
            {"B": 0.72, "A": 0.72, "C": 0.76, "K": 0.0},
        )
        r = compute_tis(inp)
        # Expected raw ~ 0.586, within [0.55, 0.65)
        assert r.gate_result == 1
        assert 0.55 <= r.tis_current < 0.65
        d, _ = map_decision(inp, r)
        assert d == "Hold"

    def test_priority_8_allow(self):
        """Gate=1 and TIS_current >= theta_allow -> Allow."""
        inp = make_tis_input(
            "fin-high-risk-suitability-v3",
            {"B": 0.95, "A": 0.95, "C": 0.95, "K": 0.90},
        )
        r = compute_tis(inp)
        assert r.gate_result == 1
        d, _ = map_decision(inp, r)
        assert d == "Allow"


# --------------------------------------------------------------------------- #
# requires_human_review rule coverage                                          #
# --------------------------------------------------------------------------- #

class TestRequiresHumanReview:
    def test_hold_always_requires_review(self):
        """Hold decisions always require review."""
        # Need an actual HOLD: gate=0, C3 != 0, S_base >= kappa.
        # clinical profile: B=1.0,A=1.0,C=1.0,K=0.55 -> K gate fails,
        # S_base=0.91 >= kappa=0.90 -> HOLD via Priority 4.
        inp = make_tis_input(
            "clinical-cds-samed-v2",
            {"B": 1.0, "A": 1.0, "C": 1.0, "K": 0.55},
        )
        r = compute_tis(inp)
        d, review = map_decision(inp, r)
        assert d == "Hold"
        assert review is True

    def test_escalate_always_requires_review(self):
        inp = make_tis_input(
            "fin-high-risk-suitability-v3",
            {"B": 0.90, "A": 0.90, "C": 0.90, "K": 0.80},
            elapsed_hours=20.0,
        )
        r = compute_tis(inp)
        d, review = map_decision(inp, r)
        assert d == "Escalate"
        assert review is True

    def test_stop_never_requires_review(self):
        """Hard stops are not reviewable — remediate upstream."""
        inp = make_tis_input(
            "clinical-cds-samed-v2",
            {"B": 0.90, "A": 0.90, "C": 0.50, "K": 0.85},
            sub_factor_scores={"C": {"C3": 0.0}},
        )
        r = compute_tis(inp)
        _, review = map_decision(inp, r)
        assert review is False

    def test_stop_by_invalidation_no_review(self):
        inp = make_tis_input(
            "fin-high-risk-suitability-v3",
            {"B": 0.95, "A": 0.95, "C": 0.95, "K": 0.95},
            is_valid=0,
            invalidation_event="policy_update",
        )
        r = compute_tis(inp)
        _, review = map_decision(inp, r)
        assert review is False

    def test_allow_clear_no_review(self):
        """Clean Allow well above the near-boundary band -> no review."""
        inp = make_tis_input(
            "fin-high-risk-suitability-v3",
            {"B": 0.98, "A": 0.98, "C": 0.98, "K": 0.95},  # very high
        )
        r = compute_tis(inp)
        d, review = map_decision(inp, r)
        assert d == "Allow"
        # TIS_current well above theta_allow + 0.05 = 0.90
        assert r.tis_current >= 0.90
        assert review is False

    def test_allow_near_boundary_requires_review(self):
        """Allow just above theta_allow (margin < 0.05) must trigger review."""
        inp = make_tis_input(
            "fin-high-risk-suitability-v3",
            {"B": 0.94, "A": 0.90, "C": 0.92, "K": 0.83},
            context_metadata={"is_policy_sensitive": True},
        )
        r = compute_tis(inp)
        d, review = map_decision(inp, r)
        assert d == "Allow"
        assert r.tis_current < 0.85 + NEAR_BOUNDARY_ALLOW_BAND
        assert review is True

    def test_allow_novelty_triggers_review(self):
        """Allow with novelty > 0.50 requires review even if not near-boundary."""
        inp = make_tis_input(
            "fin-high-risk-suitability-v3",
            {"B": 0.98, "A": 0.98, "C": 0.98, "K": 0.95},  # well clear
            context_metadata={"novelty_score": 0.80},
        )
        r = compute_tis(inp)
        d, review = map_decision(inp, r)
        assert d == "Allow"
        # tis_current should be well above the near-boundary band
        assert r.tis_current >= 0.85 + NEAR_BOUNDARY_ALLOW_BAND
        assert review is True
