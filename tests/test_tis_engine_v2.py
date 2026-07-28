"""
Tests for the tis-v2 canonical Decimal engine path (Commit 2).

Suites (per the amended Commit 2 restate and its four final corrections):

    A. Reproduction case — the brief's replayability defect, fixed.
    B. Ambient-context isolation — hostile global Decimal contexts
       (including prec=4 and prec=3) cannot alter any engine output,
       penalty components included.
    C. Threshold boundaries — explicit cases at every gate threshold the
       profiles actually carry, ±0.000049/50/51 and the threshold itself.
    D. Adjustment ordering and identity-confidence boundaries — including
       the exact half at 0.29995, which canonicalizes UP to 0.3000 and
       therefore does NOT fire the < 0.30 rule.
    E. Decay — Decimal.exp two-stage rounding, version fail-closed.
    F. p_cb clamp (v2-only) and strict n_gaps validation; v1 unclamped
       behaviour preserved.
    G. Property suite — 10,000 seeded deterministic cases replaying every
       derived intermediate with independent pinned-context arithmetic.
       (Seeded ``random.Random`` rather than Hypothesis: hypothesis is
       not a project dependency and this commit's file scope excludes
       requirements.txt. Derandomized by construction.)
    H. Source inspection — no v2 function references a v1 float constant
       or the ``math`` module.
    I. Penalty-aggregate parity — cap, key sets, weight sum, sorted-key
       determinism.

The v1 path is exercised only to prove it is untouched.
"""

from __future__ import annotations

import ast
import random
from datetime import datetime
from decimal import (
    Context,
    Decimal,
    ROUND_CEILING,
    ROUND_FLOOR,
    getcontext,
    localcontext,
    setcontext,
)
from pathlib import Path

import pytest

import tcs.tis_engine
from tcs.canonical import (
    CertificateInvariantError,
    SCORE_QUANTUM,
    SCORE_ROUNDING,
    TIS_DECIMAL_CONTEXT,
    UnsupportedCalculationVersion,
    canonical_score,
    compute_weighted_score,
)
from tcs.policy_profiles import PROFILES, load_profile
from tcs.tis_engine import (
    TISInput,
    _aggregate_penalty_v2,
    _compute_decay_factor_v2,
    _compute_penalty_components,
    _compute_penalty_components_v2,
    compute_tis,
    compute_tis_v2,
)

EVAL_TIME = datetime(2026, 7, 28, 12, 0, 0)

BACK = ("B", "A", "C", "K")


def make_input(
    profile_id: str = "fin-r3-a4-ct4",
    scores: dict | None = None,
    meta: dict | None = None,
    **overrides,
) -> TISInput:
    defaults = dict(
        subject_id="tis-v2-test",
        subject_type="model_output",
        policy_profile=load_profile(profile_id),
        dimension_scores=scores or {"B": 0.90, "A": 0.95, "C": 0.95, "K": 0.85},
        context_metadata=meta if meta is not None else {},
        elapsed_hours=0.0,
        is_valid=1,
        invalidation_event=None,
        evaluation_time=EVAL_TIME,
    )
    defaults.update(overrides)
    return TISInput(**defaults)


def canonical_weights(profile) -> dict:
    return {dim: canonical_score(profile.weights[dim]) for dim in BACK}


def canonical_thresholds(profile) -> dict:
    return {
        dim: canonical_score(v) for dim, v in profile.thresholds.items()
    }


def quantize_pinned(value: Decimal) -> Decimal:
    """Independent final quantization using the declared rounding rule."""
    with localcontext(TIS_DECIMAL_CONTEXT):
        result = value.quantize(SCORE_QUANTUM, rounding=SCORE_ROUNDING)
    return Decimal("0.0000") if result == 0 else result


# =========================================================================== #
# A. Reproduction case                                                         #
# =========================================================================== #

class TestReproductionCase:
    """The brief's defect: B=0.94 observed, identity collapses it to
    0.0000, and the certificate must carry values that replay."""

    def _run(self):
        return compute_tis_v2(make_input(
            scores={"B": 0.94, "A": 0.95, "C": 0.95, "K": 0.85},
            meta={
                "identity_confidence": 0.20,
                "identity_verified": False,
                "sensitivity_tier": "T3",
            },
        ))

    def test_observed_tier_preserves_pre_adjustment_value(self):
        res = self._run()
        assert res.observed_dimension_scores["B"] == Decimal("0.9400")
        assert res.observed_dimension_scores["B"].same_quantum(SCORE_QUANTUM)

    def test_both_rules_recorded_in_order(self):
        res = self._run()
        assert len(res.adjustments_applied) == 2
        first, second = res.adjustments_applied
        assert first.rule_id == "TCS_SPEC_19_1"
        assert first.dimension == "B"
        assert first.value_before == Decimal("0.9400")
        assert first.value_after == Decimal("0.3000")
        assert second.rule_id == "TCS_SPEC_19_2"
        assert second.value_before == Decimal("0.3000")
        assert second.value_after == Decimal("0.0000")

    def test_effective_tier_is_post_adjustment(self):
        res = self._run()
        assert res.effective_dimension_scores["B"] == Decimal("0.0000")

    def test_s_base_computed_from_effective_scores(self):
        res = self._run()
        # 0.25*0 + 0.30*0.95 + 0.25*0.95 + 0.20*0.85 = 0.6925
        assert res.s_base == Decimal("0.6925")

    def test_s_base_replays_exactly_from_result_contents(self):
        res = self._run()
        profile = load_profile("fin-r3-a4-ct4")
        weights = canonical_weights(profile)
        # Production helper replay
        assert compute_weighted_score(
            res.effective_dimension_scores, weights
        ) == res.s_base
        # Independent inline replay under the pinned context
        with localcontext(TIS_DECIMAL_CONTEXT):
            recomputed = sum(
                (weights[k] * res.effective_dimension_scores[k]
                 for k in weights),
                Decimal("0"),
            ).quantize(SCORE_QUANTUM, rounding=SCORE_ROUNDING)
        assert recomputed == res.s_base

    def test_gate_verdict_produced_by_effective_score(self):
        res = self._run()
        assert res.gate_results_by_dim["B"] == "fail"
        assert res.gate_result == 0
        assert "B" in res.failing_dimensions
        # The verdict replays from effective score and canonical threshold.
        threshold = canonical_thresholds(load_profile("fin-r3-a4-ct4"))["B"]
        assert res.effective_dimension_scores["B"] < threshold

    def test_gated_quantities_collapse(self):
        res = self._run()
        assert res.tis_raw == Decimal("0.0000")
        assert res.tis_adj == Decimal("0.0000")
        assert res.tis_current == Decimal("0.0000")

    def test_calculation_version_assigned_explicitly(self):
        res = self._run()
        assert res.calculation_version == "tis-v2"


# =========================================================================== #
# B. Ambient-context isolation                                                 #
# =========================================================================== #

HOSTILE_META = {
    "n_gaps": 3,
    "context_age_hours": 1.5,        # exercises overshoot / TAU_STALE division
    "novelty_score": 0.37,
    "days_since_review": 10,         # exercises lag / tau_review division (r3)
    "is_policy_sensitive": True,
    "identity_confidence": 0.25,     # fires rule 1 on T2
    "identity_verified": True,
    "sensitivity_tier": "T2",
}


class TestAmbientContextIsolation:
    """Hostile ambient Decimal contexts — including prec=4 and prec=3 —
    must not change any engine output. Not limited to the weighted sum
    or decay: the penalty path's divisions and multiplications are
    covered explicitly (final correction #2)."""

    HOSTILE_CONTEXTS = [
        Context(prec=4, rounding=ROUND_FLOOR),
        Context(prec=3, rounding=ROUND_CEILING),
        Context(prec=6, rounding=ROUND_FLOOR),
    ]

    def _inp(self):
        return make_input(
            scores={"B": 0.913371, "A": 0.947773, "C": 0.921113, "K": 0.837779},
            meta=dict(HOSTILE_META),
            elapsed_hours=7.3,
        )

    def test_full_result_identical_under_hostile_ambient_contexts(self):
        baseline = compute_tis_v2(self._inp())
        original = getcontext()
        for hostile in self.HOSTILE_CONTEXTS:
            try:
                setcontext(hostile)
                res = compute_tis_v2(self._inp())
            finally:
                setcontext(original)
            assert res == baseline, f"result differs under ambient {hostile}"
            # Decimal __eq__ is numerical; also pin the representation.
            for name in ("s_base", "penalty_aggregate", "s_adj", "tis_raw",
                         "tis_adj", "decay_factor", "tis_current", "C3_score"):
                assert str(getattr(res, name)) == str(getattr(baseline, name))

    def test_penalty_components_identical_under_hostile_ambient_contexts(self):
        profile = load_profile("fin-r3-a4-ct4")
        baseline = _compute_penalty_components_v2(dict(HOSTILE_META), profile)
        original = getcontext()
        for hostile in self.HOSTILE_CONTEXTS:
            try:
                setcontext(hostile)
                components = _compute_penalty_components_v2(
                    dict(HOSTILE_META), profile
                )
            finally:
                setcontext(original)
            assert components == baseline
            for k in components:
                assert str(components[k]) == str(baseline[k])


# =========================================================================== #
# C. Threshold boundaries                                                      #
# =========================================================================== #

class TestThresholdBoundaries:
    """Explicit boundary cases; randomized tests will not reliably hit
    these. The expected canonical value is derived from the declared
    rounding rule (SCORE_ROUNDING), not hardcoded blindly — plus a
    hardcoded VERIFIED anchor table at the 0.9000 gate."""

    # VERIFIED anchors at threshold 0.9000 under ROUND_HALF_UP
    # (fin-r3-a4-ct4, dimension B).
    ANCHORS = [
        ("0.899949", "0.8999", "fail"),
        ("0.899950", "0.9000", "pass"),   # exact half rounds UP into passing
        ("0.899951", "0.9000", "pass"),
        ("0.900049", "0.9000", "pass"),
        ("0.900050", "0.9001", "pass"),   # recorded value transitions
        ("0.900051", "0.9001", "pass"),
    ]

    @pytest.mark.parametrize("raw,expected_canonical,expected_verdict", ANCHORS)
    def test_anchor_table_at_0_9000(self, raw, expected_canonical,
                                    expected_verdict):
        res = compute_tis_v2(make_input(
            scores={"B": raw, "A": 1.0, "C": 1.0, "K": 1.0},
        ))
        assert res.effective_dimension_scores["B"] == Decimal(expected_canonical)
        assert str(res.effective_dimension_scores["B"]) == expected_canonical
        assert res.gate_results_by_dim["B"] == expected_verdict

    OFFSETS = [
        Decimal("-0.000051"), Decimal("-0.000050"), Decimal("-0.000049"),
        Decimal("0"),
        Decimal("0.000049"), Decimal("0.000050"), Decimal("0.000051"),
    ]

    @pytest.mark.parametrize("profile_id", sorted(PROFILES.keys()))
    def test_every_gated_threshold_boundary(self, profile_id):
        profile = load_profile(profile_id)
        thresholds = canonical_thresholds(profile)
        for dim in sorted(profile.gate_set):
            threshold = thresholds[dim]
            for offset in self.OFFSETS:
                raw = threshold + offset
                if raw < 0 or raw > 1:
                    continue
                expected_canonical = quantize_pinned(raw)
                expected_pass = expected_canonical >= threshold
                scores = {d: 1.0 for d in BACK}
                scores[dim] = str(raw)
                res = compute_tis_v2(make_input(
                    profile_id=profile_id, scores=scores,
                ))
                assert res.effective_dimension_scores[dim] == \
                    expected_canonical, (
                        f"{profile_id}/{dim}: raw {raw} canonicalized to "
                        f"{res.effective_dimension_scores[dim]}, expected "
                        f"{expected_canonical}"
                    )
                verdict = "pass" if expected_pass else "fail"
                assert res.gate_results_by_dim[dim] == verdict, (
                    f"{profile_id}/{dim}: raw {raw} (canonical "
                    f"{expected_canonical} vs threshold {threshold}) — "
                    f"expected {verdict}"
                )


# =========================================================================== #
# D. Adjustment ordering and identity boundaries                               #
# =========================================================================== #

class TestIdentityAdjustments:
    """Boundary table (final correction #3) — canonicalization happens
    BEFORE the < 0.30 comparison, so at 4dp ROUND_HALF_UP the exact
    half 0.29995 rounds UP to 0.3000 and the rule does NOT fire."""

    BOUNDARY = [
        ("0.29994", "0.2999", True),
        ("0.29995", "0.3000", False),   # exact half — the critical transition
        ("0.29996", "0.3000", False),
        ("0.30000", "0.3000", False),
        ("0.30004", "0.3000", False),
        ("0.30005", "0.3001", False),
    ]

    @pytest.mark.parametrize("raw,canonical,fires", BOUNDARY)
    def test_confidence_boundary(self, raw, canonical, fires):
        assert str(canonical_score(raw)) == canonical
        res = compute_tis_v2(make_input(
            scores={"B": 0.94, "A": 0.95, "C": 0.95, "K": 0.85},
            meta={
                "identity_confidence": raw,
                "identity_verified": True,
                "sensitivity_tier": "T2",
            },
        ))
        if fires:
            assert res.effective_dimension_scores["B"] == Decimal("0.3000")
            assert len(res.adjustments_applied) == 1
            assert res.adjustments_applied[0].rule_id == "TCS_SPEC_19_1"
        else:
            assert res.effective_dimension_scores["B"] == Decimal("0.9400")
            assert res.adjustments_applied == []

    def test_exactly_0_30_does_not_fire(self):
        res = compute_tis_v2(make_input(
            scores={"B": 0.94, "A": 0.95, "C": 0.95, "K": 0.85},
            meta={"identity_confidence": 0.30, "sensitivity_tier": "T2"},
        ))
        assert res.adjustments_applied == []
        assert res.effective_dimension_scores["B"] == Decimal("0.9400")

    def test_ordered_rules_19_1_then_19_2(self):
        res = compute_tis_v2(make_input(
            scores={"B": 0.94, "A": 0.95, "C": 0.95, "K": 0.85},
            meta={
                "identity_confidence": 0.20,
                "identity_verified": False,
                "sensitivity_tier": "T3",
            },
        ))
        assert [a.rule_id for a in res.adjustments_applied] == \
            ["TCS_SPEC_19_1", "TCS_SPEC_19_2"]
        assert res.effective_dimension_scores["B"] == Decimal("0.0000")

    def test_unverified_t1_no_collapse(self):
        res = compute_tis_v2(make_input(
            meta={"identity_verified": False, "sensitivity_tier": "T1"},
        ))
        assert res.adjustments_applied == []
        assert res.effective_dimension_scores == res.observed_dimension_scores

    def test_verified_low_confidence_t2_clamps_only(self):
        res = compute_tis_v2(make_input(
            scores={"B": 0.94, "A": 0.95, "C": 0.95, "K": 0.85},
            meta={
                "identity_confidence": 0.20,
                "identity_verified": True,
                "sensitivity_tier": "T2",
            },
        ))
        assert [a.rule_id for a in res.adjustments_applied] == ["TCS_SPEC_19_1"]
        assert res.effective_dimension_scores["B"] == Decimal("0.3000")

    def test_no_identity_metadata_no_adjustments(self):
        res = compute_tis_v2(make_input())
        assert res.adjustments_applied == []
        assert res.effective_dimension_scores == res.observed_dimension_scores

    def test_rule_firing_without_change_records_nothing(self):
        # Rule 1 fires (low confidence, T2) but B is already below the cap.
        res = compute_tis_v2(make_input(
            scores={"B": 0.20, "A": 0.95, "C": 0.95, "K": 0.85},
            meta={"identity_confidence": 0.10, "sensitivity_tier": "T2"},
        ))
        assert res.adjustments_applied == []
        assert res.effective_dimension_scores["B"] == Decimal("0.2000")
        # Rule 2 fires (unverified, T3) but B is already 0.
        res = compute_tis_v2(make_input(
            scores={"B": 0.0, "A": 0.95, "C": 0.95, "K": 0.85},
            meta={"identity_verified": False, "sensitivity_tier": "T3"},
        ))
        assert res.adjustments_applied == []
        assert res.effective_dimension_scores["B"] == Decimal("0.0000")

    @pytest.mark.parametrize("bad", ["false", "true", 1, 0, "False"])
    def test_identity_verified_strict_bool(self, bad):
        with pytest.raises(CertificateInvariantError):
            compute_tis_v2(make_input(
                meta={"identity_verified": bad, "sensitivity_tier": "T3"},
            ))

    def test_identity_verified_real_bool_accepted(self):
        res = compute_tis_v2(make_input(
            meta={"identity_verified": True, "sensitivity_tier": "T3"},
        ))
        assert res.calculation_version == "tis-v2"


# =========================================================================== #
# E. Decay                                                                     #
# =========================================================================== #

class TestDecayV2:
    """Decimal.exp two-stage rounding contract, versioned fail-closed."""

    KNOWN = [
        ("0.0500", "5.0000", "0.7788"),
        ("0.0500", "13.8600", "0.5001"),
        ("0.0500", "20.0000", "0.3679"),
        ("0.0600", "0.0000", "1.0000"),
    ]

    @pytest.mark.parametrize("rate,hours,expected", KNOWN)
    def test_known_decay_values(self, rate, hours, expected):
        factor = _compute_decay_factor_v2(
            Decimal(rate), Decimal(hours), "tis-v2"
        )
        assert str(factor) == expected
        assert factor.same_quantum(SCORE_QUANTUM)

    def test_unsupported_calculation_version_fails_closed(self):
        with pytest.raises(UnsupportedCalculationVersion):
            _compute_decay_factor_v2(
                Decimal("0.0500"), Decimal("5.0000"), "tis-v1"
            )

    def test_decay_through_engine(self):
        res = compute_tis_v2(make_input(elapsed_hours=5.0))
        assert res.decay_factor == Decimal("0.7788")

    def test_decay_matches_independent_replay(self):
        res = compute_tis_v2(make_input(elapsed_hours=7.3))
        with localcontext(TIS_DECIMAL_CONTEXT):
            factor = (-Decimal("0.0500") * Decimal("7.3000")).exp()
        assert res.decay_factor == quantize_pinned(factor)


# =========================================================================== #
# F. p_cb clamp and strict n_gaps                                              #
# =========================================================================== #

class TestPcbClampV2:
    def test_unclamped_below_25_gaps(self):
        profile = load_profile("fin-r3-a4-ct4")
        components = _compute_penalty_components_v2({"n_gaps": 3}, profile)
        assert components["cb"] == Decimal("0.1200")

    def test_exactly_25_gaps_is_1(self):
        profile = load_profile("fin-r3-a4-ct4")
        components = _compute_penalty_components_v2({"n_gaps": 25}, profile)
        assert components["cb"] == Decimal("1.0000")

    def test_30_gaps_clamped_to_1(self):
        profile = load_profile("fin-r3-a4-ct4")
        components = _compute_penalty_components_v2({"n_gaps": 30}, profile)
        assert components["cb"] == Decimal("1.0000")

    def test_v1_remains_unclamped(self):
        profile = load_profile("fin-r3-a4-ct4")
        components = _compute_penalty_components({"n_gaps": 30}, profile)
        assert components["P_cb"] == pytest.approx(1.2)

    @pytest.mark.parametrize("bad", [True, False, -1, 2.5, "3", None])
    def test_n_gaps_strict_nonnegative_int(self, bad):
        profile = load_profile("fin-r3-a4-ct4")
        with pytest.raises(CertificateInvariantError):
            _compute_penalty_components_v2({"n_gaps": bad}, profile)

    @pytest.mark.parametrize("bad", ["yes", 1, 0.0])
    def test_is_policy_sensitive_strict_bool(self, bad):
        profile = load_profile("fin-r3-a4-ct4")
        with pytest.raises(CertificateInvariantError):
            _compute_penalty_components_v2(
                {"is_policy_sensitive": bad}, profile
            )

    def test_aggregate_capped_even_with_max_gaps(self):
        res = compute_tis_v2(make_input(
            meta={"n_gaps": 40, "is_policy_sensitive": True},
        ))
        assert res.penalty_breakdown["cb"] == Decimal("1.0000")
        assert res.penalty_aggregate <= Decimal("0.5000")


# =========================================================================== #
# G. Property suite — 10,000 seeded deterministic cases                        #
# =========================================================================== #

class TestPropertyReplay:
    """Every derived intermediate the decision consumes is recomputed
    with independent pinned-context arithmetic and compared exactly."""

    N_CASES = 10_000

    def test_ten_thousand_case_replay(self):
        rng = random.Random(20260728)
        profile_ids = sorted(PROFILES.keys())
        profiles = {pid: load_profile(pid) for pid in profile_ids}

        for i in range(self.N_CASES):
            pid = profile_ids[rng.randrange(len(profile_ids))]
            profile = profiles[pid]

            scores = {dim: f"{rng.random():.6f}" for dim in BACK}
            meta = {
                "n_gaps": rng.randrange(0, 41),
                "context_age_hours": round(rng.uniform(0.0, 5.0), 4),
                "novelty_score": f"{rng.random():.4f}",
                "days_since_review": rng.randrange(0, 40),
                "is_policy_sensitive": rng.random() < 0.5,
                "identity_confidence": f"{rng.random():.6f}",
                "identity_verified": rng.random() < 0.8,
                "sensitivity_tier": ("T1", "T2", "T3")[rng.randrange(3)],
            }
            invalidation = (
                "model_version_change" if rng.random() < 0.1 else None
            )
            inp = make_input(
                profile_id=pid, scores=scores, meta=meta,
                elapsed_hours=round(rng.uniform(0.0, 30.0), 4),
                invalidation_event=invalidation,
            )
            res = compute_tis_v2(inp)
            ctx = f"case {i} profile {pid}"

            # Key-set completeness.
            assert set(res.observed_dimension_scores) == set(BACK), ctx
            assert set(res.effective_dimension_scores) == set(BACK), ctx
            assert set(res.penalty_breakdown) == {"cb", "d", "n", "h", "ps"}, ctx

            # Canonical form of every score-domain field.
            for name in ("s_base", "tis_raw", "penalty_aggregate", "s_adj",
                         "tis_adj", "C3_score", "decay_factor", "tis_current"):
                v = getattr(res, name)
                assert isinstance(v, Decimal), f"{ctx}: {name}"
                assert v.same_quantum(SCORE_QUANTUM), f"{ctx}: {name} = {v}"
                assert not (v.is_zero() and v.is_signed()), f"{ctx}: {name}"

            weights = canonical_weights(profile)

            # s_base replay.
            with localcontext(TIS_DECIMAL_CONTEXT):
                recomputed_s_base = sum(
                    (weights[k] * res.effective_dimension_scores[k]
                     for k in weights),
                    Decimal("0"),
                ).quantize(SCORE_QUANTUM, rounding=SCORE_ROUNDING)
            assert recomputed_s_base == res.s_base, ctx

            # penalty_aggregate replay — sorted keys, cap, quantize.
            pweights = {
                k: canonical_score(v)
                for k, v in profile.penalty_weights.items()
            }
            with localcontext(TIS_DECIMAL_CONTEXT):
                weighted = sum(
                    (pweights[k] * res.penalty_breakdown[k]
                     for k in sorted(res.penalty_breakdown)),
                    Decimal("0"),
                )
                recomputed_p = min(Decimal("0.5000"), weighted).quantize(
                    SCORE_QUANTUM, rounding=SCORE_ROUNDING
                )
            if recomputed_p == 0:
                recomputed_p = Decimal("0.0000")
            assert recomputed_p == res.penalty_aggregate, ctx
            assert res.penalty_aggregate <= Decimal("0.5000"), ctx

            # s_adj replay.
            with localcontext(TIS_DECIMAL_CONTEXT):
                recomputed_s_adj = (
                    res.s_base * (Decimal("1.0000") - res.penalty_aggregate)
                ).quantize(SCORE_QUANTUM, rounding=SCORE_ROUNDING)
            if recomputed_s_adj == 0:
                recomputed_s_adj = Decimal("0.0000")
            assert recomputed_s_adj == res.s_adj, ctx

            # Gate replay from effective scores and canonical thresholds.
            thresholds = canonical_thresholds(profile)
            recomputed_gates = {}
            for dim in BACK:
                if dim not in profile.gate_set:
                    recomputed_gates[dim] = "not_applicable"
                elif res.effective_dimension_scores[dim] >= thresholds[dim]:
                    recomputed_gates[dim] = "pass"
                else:
                    recomputed_gates[dim] = "fail"
            assert recomputed_gates == res.gate_results_by_dim, ctx
            recomputed_gate = 0 if any(
                recomputed_gates[d] == "fail" for d in profile.gate_set
            ) else 1
            assert recomputed_gate == res.gate_result, ctx

            # Gated quantities replay.
            with localcontext(TIS_DECIMAL_CONTEXT):
                recomputed_tis_raw = (
                    Decimal(res.gate_result) * res.s_base
                ).quantize(SCORE_QUANTUM, rounding=SCORE_ROUNDING)
                recomputed_tis_adj = (
                    Decimal(res.gate_result) * res.s_adj
                ).quantize(SCORE_QUANTUM, rounding=SCORE_ROUNDING)
            if recomputed_tis_raw == 0:
                recomputed_tis_raw = Decimal("0.0000")
            if recomputed_tis_adj == 0:
                recomputed_tis_adj = Decimal("0.0000")
            assert recomputed_tis_raw == res.tis_raw, ctx
            assert recomputed_tis_adj == res.tis_adj, ctx

            # tis_current replay.
            with localcontext(TIS_DECIMAL_CONTEXT):
                recomputed_current = (
                    res.tis_adj * res.decay_factor * Decimal(res.is_valid)
                ).quantize(SCORE_QUANTUM, rounding=SCORE_ROUNDING)
            if recomputed_current == 0:
                recomputed_current = Decimal("0.0000")
            assert recomputed_current == res.tis_current, ctx

            # Invalidation semantics.
            if invalidation is not None:
                assert res.is_valid == 0, ctx
                assert res.tis_current == Decimal("0.0000"), ctx

            assert res.calculation_version == "tis-v2", ctx


# =========================================================================== #
# H. Source inspection                                                         #
# =========================================================================== #

FORBIDDEN_V1_NAMES = {
    "DELTA_CB", "DELTA_D_MAX", "DELTA_H_MAX",
    "W_NOVELTY_BY_TIER", "TAU_FRESH_HOURS", "TAU_STALE_HOURS",
    "TAU_REVIEW_DAYS_BY_TIER", "_W_PS_SPECIAL", "_W_PS_DEFAULT",
    "math",
}

EXPECTED_V2_FUNCTIONS = {
    "_apply_identity_adjustments_v2",
    "_resolve_penalty_maxima_v2",
    "_compute_penalty_components_v2",
    "_aggregate_penalty_v2",
    "_evaluate_gate_v2",
    "_compute_decay_factor_v2",
    "_compute_valid_until_v2",
    "compute_tis_v2",
}


class TestSourceInspection:
    """No v2 function references a v1 float constant (exact-name match —
    DELTA_CB_DECIMAL is not DELTA_CB) or the math module."""

    def _v2_functions(self):
        source = Path(tcs.tis_engine.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)
        return [
            node for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) and "_v2" in node.name
        ]

    def test_expected_v2_functions_present(self):
        names = {fn.name for fn in self._v2_functions()}
        assert EXPECTED_V2_FUNCTIONS <= names

    def test_no_v1_constant_or_math_reference_in_v2_functions(self):
        for fn in self._v2_functions():
            referenced = {
                node.id for node in ast.walk(fn)
                if isinstance(node, ast.Name)
            }
            bad = referenced & FORBIDDEN_V1_NAMES
            assert not bad, (
                f"{fn.name} references v1 float constant(s) or math: "
                f"{sorted(bad)}"
            )


# =========================================================================== #
# I. Penalty-aggregate parity                                                  #
# =========================================================================== #

def _params(**kv):
    """Canonical bounded-parameter dict from 4dp strings."""
    return {k: Decimal(v) for k, v in kv.items()}


FIN_WEIGHTS = {
    "cb": Decimal("0.2500"), "d": Decimal("0.1000"), "n": Decimal("0.2000"),
    "h": Decimal("0.1000"), "ps": Decimal("0.3500"),
}


class TestAggregatePenaltyV2:
    def test_sum_under_cap_returns_quantized_sum(self):
        components = _params(
            cb="0.1200", d="0.0000", n="0.0296", h="0.0000", ps="0.0800",
        )
        with localcontext(TIS_DECIMAL_CONTEXT):
            expected = sum(
                (FIN_WEIGHTS[k] * components[k] for k in sorted(components)),
                Decimal("0"),
            ).quantize(SCORE_QUANTUM, rounding=SCORE_ROUNDING)
        assert _aggregate_penalty_v2(components, FIN_WEIGHTS) == expected

    def test_sum_over_cap_returns_exactly_0_5000(self):
        weights = {
            "cb": Decimal("1.0000"), "d": Decimal("0.0000"),
            "n": Decimal("0.0000"), "h": Decimal("0.0000"),
            "ps": Decimal("0.0000"),
        }
        components = _params(
            cb="0.6000", d="0.0000", n="0.0000", h="0.0000", ps="0.0000",
        )
        result = _aggregate_penalty_v2(components, weights)
        assert result == Decimal("0.5000")
        assert str(result) == "0.5000"

    def test_mismatched_component_keys_raise(self):
        components = _params(cb="0.1000", d="0.0000", n="0.0000", h="0.0000")
        with pytest.raises(CertificateInvariantError):
            _aggregate_penalty_v2(components, FIN_WEIGHTS)

    def test_mismatched_weight_keys_raise(self):
        components = _params(
            cb="0.1000", d="0.0000", n="0.0000", h="0.0000", ps="0.0000",
        )
        weights = dict(FIN_WEIGHTS)
        del weights["ps"]
        with pytest.raises(CertificateInvariantError):
            _aggregate_penalty_v2(components, weights)

    def test_weights_summing_to_1_0001_raise(self):
        components = _params(
            cb="0.1000", d="0.0000", n="0.0000", h="0.0000", ps="0.0000",
        )
        weights = {
            "cb": Decimal("0.2001"), "d": Decimal("0.2000"),
            "n": Decimal("0.2000"), "h": Decimal("0.2000"),
            "ps": Decimal("0.2000"),
        }
        with pytest.raises(CertificateInvariantError):
            _aggregate_penalty_v2(components, weights)

    def test_non_canonical_weight_raises(self):
        components = _params(
            cb="0.1000", d="0.0000", n="0.0000", h="0.0000", ps="0.0000",
        )
        weights = dict(FIN_WEIGHTS)
        weights["cb"] = Decimal("0.25")     # wrong quantum
        with pytest.raises(CertificateInvariantError):
            _aggregate_penalty_v2(components, weights)

    def test_sorted_key_order_determinism(self):
        components = _params(
            cb="0.1200", d="0.0333", n="0.0296", h="0.0111", ps="0.0800",
        )
        reversed_components = dict(reversed(list(components.items())))
        assert _aggregate_penalty_v2(components, FIN_WEIGHTS) == \
            _aggregate_penalty_v2(reversed_components, FIN_WEIGHTS)


# =========================================================================== #
# Cross-cutting: all profiles run, v1 untouched                                #
# =========================================================================== #

class TestAllProfilesSmoke:
    @pytest.mark.parametrize("profile_id", sorted(PROFILES.keys()))
    def test_v2_runs_on_every_shipped_profile(self, profile_id):
        res = compute_tis_v2(make_input(profile_id=profile_id))
        assert res.calculation_version == "tis-v2"
        # Weight-sum fail-closed validation passed implicitly; verify the
        # canonical weight sum really is exactly 1.0000.
        weights = canonical_weights(load_profile(profile_id))
        with localcontext(TIS_DECIMAL_CONTEXT):
            assert sum(weights.values(), Decimal("0")) == Decimal("1.0000")


class TestV1PathUntouched:
    def test_v1_result_defaults_and_types(self):
        res = compute_tis(make_input(
            scores={"B": 0.94, "A": 0.95, "C": 0.95, "K": 0.85},
        ))
        assert res.calculation_version == "tis-v1"
        assert res.effective_dimension_scores == {}
        assert res.observed_dimension_scores == {}
        assert res.adjustments_applied == []
        assert isinstance(res.s_base, float)
        assert set(res.penalty_breakdown) == {
            "P_cb", "P_d", "P_n", "P_h", "P_ps"
        }
