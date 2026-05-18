"""
End-to-end scenario tests — the Phase 1 verification contract.

Each test runs the full pipeline (compute_tis -> map_decision ->
generate_certificate) for one scenario from TEST_SCENARIOS.md and asserts
the exact expected intermediate values and decision outcome.

When all 8 tests in this file pass, Phase 1 is complete.

=============================================================================
Reconciliation notes (captured in PHASE_2_PLAN.md for doc updates)
=============================================================================

1. TEST_SCENARIOS.md scenario 2 expected TIS_adj = 0.8935, but the exact
   product 0.9065 * 0.9856 = 0.89344640 rounds to 0.8934 (not 0.8935).
   These tests assert 0.8934 — the mathematically correct value.

2. TEST_SCENARIOS.md scenario 6 expected TIS_adj = 0.8532, but the exact
   product 0.8635 * 0.9880 = 0.85313800 rounds to 0.8531. Tests assert 0.8531.

3. TEST_SCENARIOS.md scenario 3 expected requires_human_review = false.
   Under the Option A full-OR review rule, TIS_current=0.8806 with
   theta_allow=0.85 is inside the 0.05 near-boundary band, so the rule
   fires True. These tests assert True and document the correction.

4. TEST_SCENARIOS.md scenario 5 did not specify requires_human_review.
   Under Option A, TIS_current=0.7710 with r1 theta_allow=0.75 is inside
   the near-boundary band, so review=True. Tests assert True.

5. TEST_SCENARIOS.md scenario 8 decay expected values at elapsed=13.86
   show TIS_current ~0.4645, but the exact computation at the stated
   half-life produces ~0.4642. Tests use full-precision computation and
   assert the exact value.

All decision outcomes match TEST_SCENARIOS.md exactly.
"""

from __future__ import annotations

from tcs.tis_engine import compute_tis
from tcs.decision_engine import map_decision
from tcs.trust_certificate import generate_certificate

from tests.conftest import make_tis_input


# =========================================================================== #
# Scenario 1 — Healthcare STOP (C3 = 0.00 Hard Gate)                          #
# =========================================================================== #

def test_healthcare_stop():
    """
    C3 = 0.00 triggers hard Stop regardless of other scores.
    Soft-hold ceiling kappa does NOT apply. TIS_current = 0.0000.
    """
    tis_input = make_tis_input(
        profile_id="clinical-cds-samed-v2",
        dimension_scores={"B": 0.92, "A": 0.88, "C": 0.31, "K": 0.84},
        sub_factor_scores={
            "C": {"C1": 0.90, "C2": 0.85, "C3": 0.00, "C4": 0.88, "C5": 0.00}
        },
        context_metadata={
            "n_gaps": 0,
            "context_age_hours": 0.1,
            "novelty_score": 0.05,
            "days_since_review": 2,
            "is_policy_sensitive": False,
            "blocking_context": "warfarin_clarithromycin_GI_bleed",
        },
        subject_id="cds-warfarin-001",
    )
    result = compute_tis(tis_input)

    # Exact intermediate values from TEST_SCENARIOS.md
    # Under the white-paper-aligned naming: s_base is the gate-INDEPENDENT
    # weighted composite; tis_raw = gate * s_base collapses to 0 on gate fail.
    assert result.s_base == 0.6825
    assert result.tis_raw == 0.0      # gate=0 -> tis_raw collapses
    assert result.penalty_breakdown == {
        "P_cb": 0.0000, "P_d": 0.0000, "P_n": 0.0040,
        "P_h": 0.0000, "P_ps": 0.0000,
    }
    assert result.penalty_aggregate == 0.0012
    assert result.s_adj == 0.6817
    assert result.tis_adj == 0.0      # gate=0 -> tis_adj collapses
    assert result.gate_result == 0
    assert result.gate_results_by_dim == {
        "B": "pass", "A": "pass", "C": "fail", "K": "pass"
    }
    assert result.C3_score == 0.0000
    assert result.decay_factor == 1.0000
    assert result.tis_current == 0.0000

    # Decision + review
    decision, review = map_decision(tis_input, result)
    assert decision == "Stop"
    assert review is False   # hard stops are not reviewable

    # Trust Certificate
    tc = generate_certificate(tis_input, result, decision, review)
    assert tc.decision == "Stop"
    assert tc.lifecycle_state == "blocked"
    assert tc.invalidation_status == "valid"
    assert tc.gate_passed is False
    assert tc.blocking_reason == (
        "C3_prohibited_pattern_warfarin_clarithromycin_GI_bleed"
    )
    assert tc.failure_mode == "C3_prohibited_pattern"
    assert tc.failing_dimension_subfactors == {"C": {"C3": 0.0}}


# =========================================================================== #
# Scenario 2 — Healthcare ALLOW with Uncertainty Disclosure                    #
# =========================================================================== #

def test_healthcare_allow_uncertainty():
    """
    All gates pass at r3/a4 with the corrected C=0.94 value from the scenario.
    TIS_current >= theta_allow -> Allow. Novelty=0.60 triggers review=True.
    """
    tis_input = make_tis_input(
        profile_id="clinical-cds-samed-v2",
        dimension_scores={"B": 0.91, "A": 0.93, "C": 0.94, "K": 0.82},
        sub_factor_scores={
            "C": {"C1": 0.92, "C2": 0.90, "C3": 1.00, "C4": 0.88, "C5": 0.76}
        },
        context_metadata={
            "n_gaps": 0,
            "context_age_hours": 0.2,
            "novelty_score": 0.60,
            "days_since_review": 3,
            "is_policy_sensitive": False,
        },
        subject_id="cds-warfarin-routine-001",
    )
    result = compute_tis(tis_input)

    assert result.tis_raw == 0.9065
    assert result.penalty_breakdown["P_n"] == 0.0480
    assert result.penalty_aggregate == 0.0144
    # Doc arithmetic slip: doc says 0.8935, exact product is 0.89344640 -> 0.8934
    assert result.tis_adj == 0.8934
    assert result.gate_result == 1
    assert result.gate_results_by_dim == {
        "B": "pass", "A": "pass", "C": "pass", "K": "pass"
    }
    assert result.tis_current == 0.8934

    decision, review = map_decision(tis_input, result)
    assert decision == "Allow"
    assert review is True   # both novelty > 0.50 and near-boundary trigger

    tc = generate_certificate(tis_input, result, decision, review)
    assert tc.lifecycle_state == "admissible"
    assert tc.invalidation_status == "valid"
    assert tc.blocking_reason is None


# =========================================================================== #
# Scenario 3 — Financial Services ALLOW (Clean)                                #
# =========================================================================== #

def test_finance_allow():
    """
    All four gates pass at r3/a4. Weighted composite correct. Near-boundary
    trigger fires (margin 0.0306 < 0.05 band) -> review=True under Option A.
    """
    tis_input = make_tis_input(
        profile_id="fin-high-risk-suitability-v3",
        dimension_scores={"B": 0.94, "A": 0.90, "C": 0.92, "K": 0.83},
        context_metadata={
            "n_gaps": 0,
            "context_age_hours": 0.5,
            "novelty_score": 0.10,
            "days_since_review": 1,
            "is_policy_sensitive": True,
        },
        subject_id="recommendation-7781",
    )
    result = compute_tis(tis_input)

    assert result.tis_raw == 0.9075
    assert result.penalty_aggregate == 0.0296
    assert result.tis_adj == 0.8806
    assert result.gate_result == 1
    assert result.tis_current == 0.8806

    decision, review = map_decision(tis_input, result)
    assert decision == "Allow"
    # Option A correction: scenario originally said False, but 0.8806 < 0.85+0.05=0.90
    # triggers the near-boundary Allow clause in the full-OR review rule.
    assert review is True

    tc = generate_certificate(tis_input, result, decision, review)
    assert tc.lifecycle_state == "admissible"
    assert tc.blocking_reason is None
    # r3 regulated decisions carry regulatory mapping
    assert len(tc.regulatory_mapping) > 0
    assert tc.recompute_required is True  # r3


# =========================================================================== #
# Scenario 4 — Financial Services HOLD (Attribution Gate Failure)              #
# =========================================================================== #

def test_finance_stop_attribution_low_sbase():
    """
    Scenario 4 (paper-aligned).

    Single gate failure on A. S_base = 0.88 < kappa = 0.90 -> STOP via
    Priority 3 (gate-failure path, below remediability floor). Under the
    paper's kappa-as-floor semantics, a degraded baseline + gate failure
    is not worth remediating — too far gone.

    Pre-paper-alignment this scenario asserted HOLD because the local
    code had the kappa direction inverted; flipping to STOP brings the
    test in line with the white paper.

    blocking_reason still records the specific A=0.76 detail.
    """
    tis_input = make_tis_input(
        profile_id="fin-high-risk-suitability-v3",
        dimension_scores={"B": 0.94, "A": 0.76, "C": 0.92, "K": 0.88},
        context_metadata={
            "n_gaps": 1,
            "context_age_hours": 0.3,
            "novelty_score": 0.05,
            "days_since_review": 1,
            "is_policy_sensitive": False,
        },
        subject_id="recommendation-7782",
    )
    result = compute_tis(tis_input)

    assert result.s_base == 0.8800
    assert result.tis_raw == 0.0      # gate=0 collapses tis_raw
    assert result.penalty_breakdown["P_cb"] == 0.0400
    assert result.penalty_breakdown["P_n"] == 0.0040
    assert result.penalty_aggregate == 0.0108
    assert result.s_adj == 0.8705
    assert result.tis_adj == 0.0      # gate=0 collapses tis_adj
    assert result.gate_result == 0
    assert result.gate_results_by_dim == {
        "B": "pass", "A": "fail", "C": "pass", "K": "pass"
    }
    assert result.tis_current == 0.0000
    assert result.failing_dimensions == ["A"]

    decision, review = map_decision(tis_input, result)
    assert decision == "Stop"   # Priority 3: gate=0, S_base=0.88 < kappa=0.90
    assert review is False      # Stops are not reviewable

    tc = generate_certificate(tis_input, result, decision, review)
    assert tc.lifecycle_state == "blocked"
    assert tc.blocking_reason == "attribution_gate_fail_A=0.76_threshold=0.9"
    assert tc.failure_mode == "A_gate_fail"


# =========================================================================== #
# Scenario 5 — Enterprise Informational ALLOW (U Scored but Not Gated)         #
# =========================================================================== #

def test_enterprise_info_allow():
    """
    At r1/a1, U is NOT in gate_set. U=0.45 does not block.
    gate_results shows U as 'not_applicable' (NOT 'pass').
    """
    tis_input = make_tis_input(
        profile_id="enterprise-info-standard-v1",
        dimension_scores={"B": 0.88, "A": 0.82, "C": 0.85, "K": 0.45},
        context_metadata={
            "n_gaps": 0,
            "context_age_hours": 0.5,
            "novelty_score": 0.10,
            "days_since_review": 5,
            "is_policy_sensitive": False,
        },
        subject_id="summary-draft-001",
        subject_type="model_output",
    )
    result = compute_tis(tis_input)

    assert result.tis_raw == 0.7715
    assert result.gate_result == 1
    assert result.gate_results_by_dim == {
        "B": "pass", "A": "pass", "C": "pass", "K": "not_applicable"
    }
    # tis_current is tis_adj here (no decay) — small penalty from novelty.
    # At r1 with enterprise-info profile: P_n = 0.10*0.03 = 0.003, lambda_n=0.20
    # -> P = 0.0006, tis_adj = 0.7715 * 0.9994 = 0.77103710 -> 0.7710
    assert result.tis_adj == 0.7710
    assert result.tis_current == 0.7710

    decision, review = map_decision(tis_input, result)
    assert decision == "Allow"
    # Option A: near-boundary at r1 (0.7710 < 0.75 + 0.05 = 0.80) -> True
    assert review is True

    tc = generate_certificate(tis_input, result, decision, review)
    assert tc.lifecycle_state == "admissible"
    assert tc.blocking_reason is None
    # Enterprise has no regulatory mapping
    assert tc.regulatory_mapping == []


# =========================================================================== #
# Scenario 6 — High-Risk U Gate Failure -> Hold (Not Stop)                     #
# =========================================================================== #

def test_high_risk_known_gate_stop_low_sbase():
    """
    Scenario 6 (paper-aligned).

    At r3/a4, K IS in gate_set. K=0.72 < 0.80 fails.
    S_base = 0.8635 < kappa = 0.90 -> STOP via Priority 3 (below
    remediability floor). C3 != 0 so this is not a hard C3 stop.

    Pre-paper-alignment this scenario asserted HOLD because the local
    code had the kappa direction inverted; flipping to STOP brings the
    test in line with the white paper.
    """
    tis_input = make_tis_input(
        profile_id="clinical-cds-samed-v2",
        dimension_scores={"B": 0.90, "A": 0.88, "C": 0.91, "K": 0.72},
        context_metadata={
            "n_gaps": 0,
            "context_age_hours": 0.1,
            "novelty_score": 0.50,
            "days_since_review": 4,
            "is_policy_sensitive": False,
        },
        subject_id="clinical-borderline-001",
    )
    result = compute_tis(tis_input)

    assert result.s_base == 0.8635
    assert result.tis_raw == 0.0      # gate=0 collapses tis_raw
    assert result.penalty_breakdown["P_n"] == 0.0400
    assert result.penalty_aggregate == 0.0120
    # Doc arithmetic slip: doc says 0.8532, exact product is 0.85313800 -> 0.8531
    assert result.s_adj == 0.8531
    assert result.tis_adj == 0.0      # gate=0 collapses tis_adj
    assert result.gate_result == 0
    assert result.gate_results_by_dim == {
        "B": "pass", "A": "pass", "C": "pass", "K": "fail"
    }
    assert result.failing_dimensions == ["K"]
    assert result.tis_current == 0.0000

    decision, review = map_decision(tis_input, result)
    assert decision == "Stop"   # Priority 3: gate=0, S_base=0.8635 < kappa=0.90
    assert review is False      # Stops are not reviewable

    tc = generate_certificate(tis_input, result, decision, review)
    assert tc.lifecycle_state == "blocked"
    assert tc.blocking_reason == "known_gate_fail_K=0.72_threshold=0.8"
    assert tc.failure_mode == "K_gate_fail"


# =========================================================================== #
# Scenario 7 — Invalidation Event -> Stop (I_inv = 0)                          #
# =========================================================================== #

def test_invalidation_event():
    """
    Invalidation event fires at Priority 1 BEFORE any gate or C3 check.
    TIS_raw and TIS_adj still computed for audit, but TIS_current = 0.0000.
    lifecycle_state becomes 'invalidated' (not 'blocked').
    """
    tis_input = make_tis_input(
        profile_id="fin-high-risk-suitability-v3",
        dimension_scores={"B": 0.95, "A": 0.95, "C": 0.95, "K": 0.95},
        context_metadata={
            "n_gaps": 0,
            "context_age_hours": 0.1,
            "novelty_score": 0.0,
            "days_since_review": 1,
            "is_policy_sensitive": False,
        },
        elapsed_hours=2.0,
        is_valid=0,
        invalidation_event="model_version_change",
        subject_id="recommendation-7783",
    )
    result = compute_tis(tis_input)

    # Intermediates still computed and recorded for audit.
    assert result.tis_raw == 0.9500
    assert result.tis_adj == 0.9500   # no penalty contributors
    assert result.gate_result == 1
    assert result.is_valid == 0
    # TIS_current forced to 0 by is_valid=0 * gate * decay
    assert result.tis_current == 0.0000

    decision, review = map_decision(tis_input, result)
    assert decision == "Stop"   # Priority 1 fires
    assert review is False      # hard stop via invalidation

    tc = generate_certificate(tis_input, result, decision, review)
    assert tc.lifecycle_state == "invalidated"    # not 'blocked'
    assert tc.invalidation_status == "invalidated"
    assert tc.blocking_reason == "invalidation_model_version_change"
    assert tc.failure_mode == "invalidated"
    assert tc.last_invalidation_event["type"] == "model_version_change"


# =========================================================================== #
# Scenario 8 — Temporal Decay Over Time                                        #
# =========================================================================== #

def test_decay_over_time():
    """
    TIS_current decreases with elapsed time per the decay factor.
    Gate remains 1 throughout; only decay changes the decision.

    Expected outcomes under Option A r3 thresholds (theta_escalate=0.70,
    theta_hold=0.85, theta_allow=0.85):

        t=0.0   -> TIS_current ~0.9283 -> Allow
        t=5.0   -> TIS_current ~0.7229 -> Hold (P6 score path)
        t=13.86 -> TIS_current ~0.4642 -> Escalate
        t=20.0  -> TIS_current ~0.3415 -> Escalate
    """
    base_scores = {"B": 0.95, "A": 0.92, "C": 0.94, "K": 0.88}
    base_meta = {
        "n_gaps": 0,
        "context_age_hours": 0.1,
        "novelty_score": 0.05,
        "days_since_review": 1,
        "is_policy_sensitive": False,
    }

    # All four sub-runs share the same inputs except elapsed_hours.
    # TIS_raw and TIS_adj should be identical across them.
    def _run(elapsed):
        inp = make_tis_input(
            profile_id="fin-high-risk-suitability-v3",
            dimension_scores=base_scores,
            context_metadata=base_meta,
            elapsed_hours=elapsed,
            subject_id=f"recommendation-7784-t{elapsed}",
        )
        result = compute_tis(inp)
        decision, review = map_decision(inp, result)
        return result, decision, review

    # --- t = 0.0 : no decay -> Allow ----------------------------------- #
    r0, d0, rv0 = _run(0.0)
    assert r0.tis_raw == 0.9290
    assert r0.penalty_aggregate == 0.0008
    assert r0.tis_adj == 0.9283
    assert r0.decay_factor == 1.0000
    assert r0.tis_current == 0.9283
    assert d0 == "Allow"
    # 0.9283 > 0.85 + 0.05 = 0.90, NOT near-boundary, novelty 0.05 low -> False
    assert rv0 is False

    # --- t = 5.0 : moderate decay -> Hold (r3 P6 score path) ----------- #
    r5, d5, rv5 = _run(5.0)
    assert r5.tis_raw == 0.9290
    assert r5.tis_adj == 0.9283
    assert r5.tis_current == 0.7229
    assert d5 == "Hold"
    assert rv5 is True

    # --- t = 13.86 : ~half-life -> Escalate (below theta_escalate) ---- #
    r13, d13, rv13 = _run(13.86)
    assert r13.tis_raw == 0.9290
    assert r13.tis_adj == 0.9283
    # Doc says ~0.4645, exact full-precision value is 0.4642.
    assert r13.tis_current == 0.4642
    assert d13 == "Escalate"
    assert rv13 is True

    # --- t = 20.0 : strong decay -> Escalate --------------------------- #
    r20, d20, rv20 = _run(20.0)
    assert r20.tis_raw == 0.9290
    assert r20.tis_adj == 0.9283
    assert r20.tis_current == 0.3415
    assert d20 == "Escalate"
    assert rv20 is True

    # Monotonicity sanity: decay only decreases TIS_current over time.
    assert r0.tis_current > r5.tis_current > r13.tis_current > r20.tis_current
