"""
Unit tests for tcs.tis_engine.

Focus: boundary conditions, validation, rounding discipline, and the
isolated behavior of each pipeline step. End-to-end pipeline coverage
lives in tests/test_scenarios.py.
"""

from __future__ import annotations

import math

import pytest

from tcs.tis_engine import (
    compute_tis,
    _aggregate_penalty,
    _compute_tis_raw,
    _evaluate_gate,
    _compute_valid_until,
    _extract_c3,
    _apply_invalidation,
    TAU_FRESH_HOURS,
)
from tcs.policy_profiles import load_profile

from tests.conftest import make_tis_input, FIXED_EVAL_TIME


# --------------------------------------------------------------------------- #
# Helper computations                                                          #
# --------------------------------------------------------------------------- #

class TestTisRaw:
    def test_weighted_sum_equal_weights(self):
        weights = {"B": 0.25, "A": 0.25, "C": 0.25, "K": 0.25}
        scores  = {"B": 0.80, "A": 0.80, "C": 0.80, "K": 0.80}
        assert _compute_tis_raw(scores, weights) == pytest.approx(0.80)

    def test_weighted_sum_unequal_weights(self):
        weights = {"B": 0.30, "A": 0.20, "C": 0.35, "K": 0.15}
        scores  = {"B": 0.90, "A": 0.70, "C": 0.80, "K": 0.60}
        expected = 0.30*0.90 + 0.20*0.70 + 0.35*0.80 + 0.15*0.60
        assert _compute_tis_raw(scores, weights) == pytest.approx(expected)


class TestPenaltyAggregate:
    def test_cap_at_half(self):
        """Aggregate must be capped at 0.50 regardless of component sum."""
        components = {"P_cb": 1.0, "P_d": 1.0, "P_n": 1.0, "P_h": 1.0, "P_ps": 1.0}
        lambdas    = {"cb": 0.20, "d": 0.20, "n": 0.20, "h": 0.20, "ps": 0.20}
        # Unweighted would be 1.0; cap brings it to 0.50.
        assert _aggregate_penalty(components, lambdas) == 0.50

    def test_no_penalty(self):
        components = {"P_cb": 0.0, "P_d": 0.0, "P_n": 0.0, "P_h": 0.0, "P_ps": 0.0}
        lambdas    = {"cb": 0.20, "d": 0.20, "n": 0.20, "h": 0.20, "ps": 0.20}
        assert _aggregate_penalty(components, lambdas) == 0.0


class TestGateEvaluation:
    def test_all_pass(self):
        scores = {"B": 0.80, "A": 0.80, "C": 0.90, "K": 0.80}
        thresh = {"B": 0.70, "A": 0.70, "C": 0.85, "K": 0.75}
        gate_set = frozenset({"B", "A", "C", "K"})
        gate, results, failing = _evaluate_gate(scores, thresh, gate_set)
        assert gate == 1
        assert all(v == "pass" for v in results.values())
        assert failing == []

    def test_single_fail(self):
        scores = {"B": 0.80, "A": 0.60, "C": 0.90, "K": 0.80}
        thresh = {"B": 0.70, "A": 0.70, "C": 0.85, "K": 0.75}
        gate_set = frozenset({"B", "A", "C", "K"})
        gate, results, failing = _evaluate_gate(scores, thresh, gate_set)
        assert gate == 0
        assert results["A"] == "fail"
        assert failing == ["A"]

    def test_not_applicable_records_even_without_gating(self):
        """Dim outside gate_set must be 'not_applicable', NOT 'pass'."""
        scores = {"B": 0.80, "A": 0.80, "C": 0.90, "K": 0.20}
        thresh = {"B": 0.70, "A": 0.70, "C": 0.85, "K": 0.75}
        gate_set = frozenset({"B", "A", "C"})  # U not gated
        gate, results, failing = _evaluate_gate(scores, thresh, gate_set)
        assert gate == 1
        assert results["K"] == "not_applicable"
        assert results["B"] == "pass"
        assert failing == []

    def test_boundary_exact(self):
        """Score exactly at threshold should pass (>=)."""
        scores = {"B": 0.70, "A": 0.70, "C": 0.85, "K": 0.80}
        thresh = {"B": 0.70, "A": 0.70, "C": 0.85, "K": 0.80}
        gate_set = frozenset({"B", "A", "C", "K"})
        gate, _, _ = _evaluate_gate(scores, thresh, gate_set)
        assert gate == 1


class TestInvalidation:
    def test_canonical_event_forces_zero(self):
        assert _apply_invalidation(1, "model_version_change") == 0
        assert _apply_invalidation(1, "policy_update") == 0
        assert _apply_invalidation(1, "data_distribution_drift") == 0
        assert _apply_invalidation(1, "environmental_change") == 0

    def test_no_event_preserves_valid(self):
        assert _apply_invalidation(1, None) == 1

    def test_unknown_event_preserves_valid(self):
        """Only canonical events invalidate; unknown strings are ignored."""
        assert _apply_invalidation(1, "made_up_event") == 1


class TestValidUntil:
    def test_half_life_matches_ln2(self):
        """valid_until must equal eval_time + ln(2)/decay_rate hours."""
        decay_rate = 0.050
        valid_until = _compute_valid_until(FIXED_EVAL_TIME, decay_rate)
        delta_hours = (valid_until - FIXED_EVAL_TIME).total_seconds() / 3600
        expected = math.log(2.0) / decay_rate
        assert delta_hours == pytest.approx(expected, abs=1e-9)


class TestC3Extraction:
    def test_provided_c3(self):
        inp = make_tis_input(
            "clinical-cds-samed-v2",
            {"B": 0.9, "A": 0.9, "C": 0.5, "K": 0.9},
            sub_factor_scores={"C": {"C3": 0.0}},
        )
        assert _extract_c3(inp) == 0.0

    def test_default_c3_when_missing(self):
        """Missing sub_factor_scores defaults to C3=1.0 (no prohibited pattern)."""
        inp = make_tis_input(
            "clinical-cds-samed-v2",
            {"B": 0.9, "A": 0.9, "C": 0.5, "K": 0.9},
        )
        assert _extract_c3(inp) == 1.0


# --------------------------------------------------------------------------- #
# Input validation                                                             #
# --------------------------------------------------------------------------- #

class TestInputValidation:
    def test_missing_dimension_raises(self):
        inp = make_tis_input(
            "clinical-cds-samed-v2",
            {"B": 0.9, "A": 0.9, "C": 0.9},  # missing U
        )
        with pytest.raises(ValueError, match="dimension_scores"):
            compute_tis(inp)

    def test_out_of_range_dimension_raises(self):
        inp = make_tis_input(
            "clinical-cds-samed-v2",
            {"B": 1.5, "A": 0.9, "C": 0.9, "K": 0.9},
        )
        with pytest.raises(ValueError, match="out of range"):
            compute_tis(inp)

    def test_negative_elapsed_hours_raises(self):
        inp = make_tis_input(
            "clinical-cds-samed-v2",
            {"B": 0.9, "A": 0.9, "C": 0.9, "K": 0.9},
            elapsed_hours=-1.0,
        )
        with pytest.raises(ValueError, match="elapsed_hours"):
            compute_tis(inp)

    def test_invalid_is_valid_raises(self):
        inp = make_tis_input(
            "clinical-cds-samed-v2",
            {"B": 0.9, "A": 0.9, "C": 0.9, "K": 0.9},
            is_valid=2,
        )
        with pytest.raises(ValueError, match="is_valid"):
            compute_tis(inp)

    def test_out_of_range_novelty_raises(self):
        inp = make_tis_input(
            "clinical-cds-samed-v2",
            {"B": 0.9, "A": 0.9, "C": 0.9, "K": 0.9},
            context_metadata={"novelty_score": 1.5},
        )
        with pytest.raises(ValueError, match="novelty_score"):
            compute_tis(inp)


# --------------------------------------------------------------------------- #
# Rounding                                                                     #
# --------------------------------------------------------------------------- #

class TestRounding:
    def test_all_result_floats_at_4dp(self):
        inp = make_tis_input(
            "fin-high-risk-suitability-v3",
            {"B": 0.94, "A": 0.90, "C": 0.92, "K": 0.83},
            context_metadata={"n_gaps": 0, "context_age_hours": 0.5, "novelty_score": 0.10, "days_since_review": 1, "is_policy_sensitive": True},
        )
        r = compute_tis(inp)
        # Every float in the result should have at most 4 decimal places.
        for field_value in (r.tis_raw, r.tis_adj, r.tis_current,
                            r.penalty_aggregate, r.decay_factor, r.C3_score):
            assert round(field_value, 4) == field_value
        for v in r.penalty_breakdown.values():
            assert round(v, 4) == v


# --------------------------------------------------------------------------- #
# Penalty component integration                                                #
# --------------------------------------------------------------------------- #

class TestPenaltyComponents:
    def test_fresh_context_zero_p_d(self):
        """context_age_hours <= TAU_FRESH_HOURS must produce P_d = 0."""
        inp = make_tis_input(
            "fin-high-risk-suitability-v3",
            {"B": 0.90, "A": 0.90, "C": 0.90, "K": 0.85},
            context_metadata={"context_age_hours": TAU_FRESH_HOURS},
        )
        r = compute_tis(inp)
        assert r.penalty_breakdown["P_d"] == 0.0

    def test_recent_review_zero_p_h(self):
        """days_since_review <= tau_review must produce P_h = 0."""
        inp = make_tis_input(
            "fin-high-risk-suitability-v3",  # r3, tau_review=7
            {"B": 0.90, "A": 0.90, "C": 0.90, "K": 0.85},
            context_metadata={"days_since_review": 7},
        )
        r = compute_tis(inp)
        assert r.penalty_breakdown["P_h"] == 0.0

    def test_n_gaps_scales_p_cb(self):
        """P_cb should scale linearly with n_gaps (default delta_cb=0.04)."""
        inp = make_tis_input(
            "fin-high-risk-suitability-v3",
            {"B": 0.90, "A": 0.90, "C": 0.90, "K": 0.85},
            context_metadata={"n_gaps": 3},
        )
        r = compute_tis(inp)
        assert r.penalty_breakdown["P_cb"] == 0.12   # 3 * 0.04

    def test_all_five_components_always_present(self):
        """C-R.5: all five penalty components must appear regardless of value."""
        inp = make_tis_input(
            "fin-high-risk-suitability-v3",
            {"B": 0.90, "A": 0.90, "C": 0.90, "K": 0.85},
        )
        r = compute_tis(inp)
        assert set(r.penalty_breakdown.keys()) == {
            "P_cb", "P_d", "P_n", "P_h", "P_ps"
        }


# --------------------------------------------------------------------------- #
# Decay                                                                        #
# --------------------------------------------------------------------------- #

class TestDecay:
    def test_zero_elapsed_no_decay(self):
        inp = make_tis_input(
            "fin-high-risk-suitability-v3",
            {"B": 0.90, "A": 0.90, "C": 0.90, "K": 0.85},
            elapsed_hours=0.0,
        )
        r = compute_tis(inp)
        assert r.decay_factor == 1.0

    def test_half_life_factor_is_half(self):
        """At elapsed = ln(2)/mu, the decay factor should be 0.5."""
        profile = load_profile("fin-high-risk-suitability-v3")
        half_life = math.log(2.0) / profile.decay_rate
        inp = make_tis_input(
            "fin-high-risk-suitability-v3",
            {"B": 0.90, "A": 0.90, "C": 0.90, "K": 0.85},
            elapsed_hours=half_life,
        )
        r = compute_tis(inp)
        assert r.decay_factor == 0.5
