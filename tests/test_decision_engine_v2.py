"""
tis-v2 versioned decision semantics (Commit 4).

The headline behavior: under tis-v2, C3_score == 0.0000 is an
UNCONDITIONAL Stop — no ``gate == 0`` conjunction — while the frozen v1
ladder keeps its legacy conjunctive behavior byte-identical. The
dispatcher accepts both recognized legacy labels and fails closed on
anything else.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

import pytest

from tcs.canonical import UnsupportedCalculationVersion
from tcs.decision_engine import (
    LEGACY_CALCULATION_VERSIONS,
    map_decision,
    map_decision_v2,
    map_decision_versioned,
)
from tcs.policy_profiles import load_profile
from tcs.tis_engine import TISInput, TISResult

EVAL_TIME = datetime(2026, 7, 28, 12, 0, 0)


def make_input(profile_id="fin-r3-a4-ct4", meta=None):
    return TISInput(
        subject_id="dv2", subject_type="model_output",
        policy_profile=load_profile(profile_id),
        dimension_scores={"B": 0.95, "A": 0.95, "C": 0.95, "K": 0.85},
        context_metadata=meta if meta is not None else {},
        elapsed_hours=0.0, is_valid=1, invalidation_event=None,
        evaluation_time=EVAL_TIME,
    )


def make_v2_result(**overrides) -> TISResult:
    defaults = dict(
        s_base=Decimal("0.9300"),
        tis_raw=Decimal("0.9300"),
        penalty_breakdown={
            "cb": Decimal("0.0000"), "d": Decimal("0.0000"),
            "n": Decimal("0.0000"), "h": Decimal("0.0000"),
            "ps": Decimal("0.0000"),
        },
        penalty_aggregate=Decimal("0.0000"),
        s_adj=Decimal("0.9300"),
        tis_adj=Decimal("0.9300"),
        gate_result=1,
        gate_results_by_dim={"B": "pass", "A": "pass", "C": "pass",
                             "K": "pass"},
        failing_dimensions=[],
        C3_score=Decimal("1.0000"),
        decay_factor=Decimal("1.0000"),
        tis_current=Decimal("0.9300"),
        valid_until=EVAL_TIME + timedelta(hours=10),
        is_valid=1,
        invalidation_event=None,
        effective_dimension_scores={
            "B": Decimal("0.9500"), "A": Decimal("0.9500"),
            "C": Decimal("0.9500"), "K": Decimal("0.8500"),
        },
        observed_dimension_scores={
            "B": Decimal("0.9500"), "A": Decimal("0.9500"),
            "C": Decimal("0.9500"), "K": Decimal("0.8500"),
        },
        adjustments_applied=[],
        calculation_version="tis-v2",
    )
    defaults.update(overrides)
    return TISResult(**defaults)


def as_v1_floats(res: TISResult) -> TISResult:
    """The equivalent legacy result — floats, v1 label."""
    from dataclasses import replace
    return replace(
        res,
        s_base=float(res.s_base), tis_raw=float(res.tis_raw),
        penalty_breakdown={k: float(v)
                           for k, v in res.penalty_breakdown.items()},
        penalty_aggregate=float(res.penalty_aggregate),
        s_adj=float(res.s_adj), tis_adj=float(res.tis_adj),
        C3_score=float(res.C3_score),
        decay_factor=float(res.decay_factor),
        tis_current=float(res.tis_current),
        effective_dimension_scores={}, observed_dimension_scores={},
        adjustments_applied=[], calculation_version="tis-v1",
    )


class TestUnconditionalC3Stop:
    """The tis-v2 semantic correction from the C3 discovery trace."""

    def test_c3_zero_with_gate_open_stops_under_v2(self):
        # Decoupled C dimension: gate PASSES, score ladder would Allow —
        # but C3 == 0.0000 must Stop unconditionally under tis-v2.
        res = make_v2_result(C3_score=Decimal("0.0000"))
        assert res.gate_result == 1
        assert res.tis_current >= Decimal("0.8500")
        decision, review = map_decision_v2(make_input(), res)
        assert decision == "Stop"
        assert review is False        # hard stops are not reviewable

    def test_same_shape_keeps_legacy_conjunctive_behavior_under_v1(self):
        # CONTRAST: the frozen v1 ladder requires gate == 0 for the C3
        # stop; with gate = 1 it falls through the score ladder to
        # Allow. This is exactly the hole tis-v2 closes — and exactly
        # the historical behavior v1 replay must preserve.
        res_v1 = as_v1_floats(make_v2_result(C3_score=Decimal("0.0000")))
        decision, _ = map_decision(make_input(), res_v1)
        assert decision == "Allow"

    def test_c3_zero_with_gate_closed_stops_under_both(self):
        res = make_v2_result(
            C3_score=Decimal("0.0000"), gate_result=0,
            gate_results_by_dim={"B": "pass", "A": "pass", "C": "fail",
                                 "K": "pass"},
            failing_dimensions=["C"],
            tis_raw=Decimal("0.0000"), tis_adj=Decimal("0.0000"),
            tis_current=Decimal("0.0000"),
        )
        assert map_decision_v2(make_input(), res)[0] == "Stop"
        assert map_decision(make_input(), as_v1_floats(res))[0] == "Stop"

    def test_invalidation_still_fires_first(self):
        res = make_v2_result(C3_score=Decimal("0.0000"), is_valid=0,
                             tis_current=Decimal("0.0000"))
        assert map_decision_v2(make_input(), res)[0] == "Stop"


class TestVersionDispatch:
    def test_legacy_labels_route_to_v1(self):
        assert LEGACY_CALCULATION_VERSIONS == {"tis-v1", "tis-v1-legacy"}
        for label in sorted(LEGACY_CALCULATION_VERSIONS):
            res = as_v1_floats(make_v2_result())
            from dataclasses import replace
            res = replace(res, calculation_version=label)
            decision, _ = map_decision_versioned(make_input(), res)
            assert decision == "Allow"

    def test_v2_label_routes_to_v2(self):
        res = make_v2_result(C3_score=Decimal("0.0000"))
        decision, _ = map_decision_versioned(make_input(), res)
        assert decision == "Stop"     # the unconditional v2 semantics

    @pytest.mark.parametrize("bad", ["tis-v3", "", "TIS-V2", "v2", "tis_v2"])
    def test_unknown_versions_fail_closed(self, bad):
        from dataclasses import replace
        res = replace(make_v2_result(), calculation_version=bad)
        with pytest.raises(UnsupportedCalculationVersion):
            map_decision_versioned(make_input(), res)

    def test_map_decision_v2_rejects_v1_result(self):
        with pytest.raises(UnsupportedCalculationVersion):
            map_decision_v2(make_input(), as_v1_floats(make_v2_result()))


class TestLadderParityBelowPriority2:
    """Away from the C3 branch, the v2 ladder mirrors v1 semantics."""

    def test_gate_fail_below_kappa_stops(self):
        res = make_v2_result(
            gate_result=0, s_base=Decimal("0.8500"),
            gate_results_by_dim={"B": "pass", "A": "fail", "C": "pass",
                                 "K": "pass"},
            failing_dimensions=["A"],
            tis_raw=Decimal("0.0000"), tis_adj=Decimal("0.0000"),
            tis_current=Decimal("0.0000"),
        )
        assert map_decision_v2(make_input(), res)[0] == "Stop"

    def test_gate_fail_at_or_above_kappa_holds(self):
        res = make_v2_result(
            gate_result=0, s_base=Decimal("0.9200"),
            gate_results_by_dim={"B": "pass", "A": "fail", "C": "pass",
                                 "K": "pass"},
            failing_dimensions=["A"],
            tis_raw=Decimal("0.0000"), tis_adj=Decimal("0.0000"),
            tis_current=Decimal("0.0000"),
        )
        decision, review = map_decision_v2(make_input(), res)
        assert decision == "Hold" and review is True

    def test_escalate_band(self):
        res = make_v2_result(tis_current=Decimal("0.6500"))
        assert map_decision_v2(make_input(), res)[0] == "Escalate"

    def test_score_path_hold_band(self):
        res = make_v2_result(tis_current=Decimal("0.7800"))
        assert map_decision_v2(make_input(), res)[0] == "Hold"

    def test_observe_r1_only(self):
        res = make_v2_result(tis_current=Decimal("0.7000"))
        decision, _ = map_decision_v2(
            make_input(profile_id="enterprise-info-standard-v1"), res)
        assert decision == "Observe"

    def test_allow_band(self):
        res = make_v2_result(tis_current=Decimal("0.9300"))
        assert map_decision_v2(make_input(), res)[0] == "Allow"


class TestRequiresHumanReviewV2:
    def test_novelty_triggers_review_on_allow(self):
        res = make_v2_result()
        _, review = map_decision_v2(
            make_input(meta={"novelty_score": 0.60}), res)
        assert review is True

    def test_near_boundary_allow_triggers_review(self):
        res = make_v2_result(tis_current=Decimal("0.8700"))
        decision, review = map_decision_v2(make_input(), res)
        assert decision == "Allow" and review is True

    def test_comfortable_allow_no_review(self):
        res = make_v2_result(tis_current=Decimal("0.9300"))
        decision, review = map_decision_v2(make_input(), res)
        assert decision == "Allow" and review is False
