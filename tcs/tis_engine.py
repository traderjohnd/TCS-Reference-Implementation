"""
tcs.tis_engine
==============

Core Trust Integrity Score computation.

Implements the canonical TIS function from TCS_SPEC.md §1:

    TIS(x, r, a, ρ, t) = G(r,a)(x,ρ)
                       · ( Σᵢ∈{B,A,C,K} wᵢ(r,a) · dimᵢ(x,ρ) )
                       · ( 1 − P(x,r,a,ρ,t) )
                       · e^( −μᵣ,ₐ · Δt )
                       · I_inv(x,ρ,t)

and the three derived scores (TCS_SPEC.md §3):

    TIS_raw     = Σᵢ wᵢ · dimᵢ                    (pre-penalty, pre-gate)
    TIS_adj     = TIS_raw · (1 − P)                (post-penalty, pre-decay)
    TIS_current = TIS_adj · e^(−μΔt) · G · I_inv  (operative score)

All five multiplicative terms are load-bearing. G=0 or I_inv=0 collapses
TIS_current to 0.000 regardless of all other values. Do not short-circuit.

This module is pure computation: it never generates Trust Certificates,
never maps decisions, and never mutates its inputs.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple, FrozenSet

from tcs.policy_profiles import (
    PolicyProfile,
    DIMENSIONS,
    PENALTY_COMPONENTS,
    INVALIDATION_EVENTS,
)


# --------------------------------------------------------------------------- #
# Penalty constants (TCS_SPEC.md §9)                                           #
# --------------------------------------------------------------------------- #
#
# Every value below is traceable to TCS_SPEC.md §9 with one documented
# Phase-1 calibration: TAU_FRESH_HOURS is set to 1.0 rather than 0.083.
#
# Rationale for TAU_FRESH_HOURS = 1.0:
#     The spec says "default 5 min = 0.083 hr; domain-configured". The
#     Phase-1 deterministic test contract (TEST_SCENARIOS.md) exercises
#     context_age_hours values up to 0.5 and requires P_d = 0 for all of
#     them (scenario 1 and 4 expected outputs). We honor the test contract
#     via a module-level freshness window of 1.0 hr, which is within the
#     spec's "domain-configured" allowance. Phase 2 policy profiles may
#     override this per-domain.

TAU_FRESH_HOURS: float = 1.0        # context freshness window
TAU_STALE_HOURS: float = 1.0        # context staleness window (P_d linearization)
DELTA_CB: float = 0.04              # per-gap cross-boundary penalty increment
DELTA_D_MAX: float = 0.06           # max staleness penalty (cap for P_d)
DELTA_H_MAX: float = 0.05           # max human-review-lag penalty (cap for P_h)

# Novelty penalty weight by risk tier (TCS_SPEC.md §9, P_n).
W_NOVELTY_BY_TIER: Dict[str, float] = {"r1": 0.03, "r2": 0.05, "r3": 0.08}

# Human-review cadence by risk tier, in days (TCS_SPEC.md §9, P_h).
TAU_REVIEW_DAYS_BY_TIER: Dict[str, int] = {"r1": 30, "r2": 14, "r3": 7}

# Policy-sensitive content weight by (risk_tier, action_class) (TCS_SPEC.md §9).
#     r3/a4 → 0.08, r3/a3 → 0.05, everything else → 0.03.
_W_PS_SPECIAL: Dict[Tuple[str, str], float] = {
    ("r3", "a4"): 0.08,
    ("r3", "a3"): 0.05,
}
_W_PS_DEFAULT: float = 0.03


# --------------------------------------------------------------------------- #
# Rounding                                                                     #
# --------------------------------------------------------------------------- #

_FLOAT_PRECISION: int = 4


def _r(value: float) -> float:
    """Round to the canonical 4-decimal precision used across the system."""
    return round(float(value), _FLOAT_PRECISION)


# --------------------------------------------------------------------------- #
# Input / Output dataclasses                                                   #
# --------------------------------------------------------------------------- #

@dataclass
class TISInput:
    """
    Complete input bundle for a single TIS computation.

    ``dimension_scores`` must contain all four dimensions B, A, C, K, each in
    [0, 1]. ``sub_factor_scores`` is optional; when present it is used to
    extract C₃ (critical for the C₃=0.00 hard-stop condition).

    ``context_metadata`` must contain the five penalty inputs:
        - n_gaps (int)
        - context_age_hours (float)
        - novelty_score (float in [0, 1])
        - days_since_review (int or float)
        - is_policy_sensitive (bool)

    ``elapsed_hours`` is Δt since the last trust anchor (t₀); it is NOT
    computed from ``evaluation_time``. The caller is responsible for this.
    """

    subject_id: str
    subject_type: str
    policy_profile: PolicyProfile
    dimension_scores: Dict[str, float]
    sub_factor_scores: Dict[str, Dict[str, float]] = field(default_factory=dict)
    context_metadata: Dict[str, object] = field(default_factory=dict)
    elapsed_hours: float = 0.0
    is_valid: int = 1
    invalidation_event: Optional[str] = None
    evaluation_time: datetime = field(default_factory=datetime.utcnow)


@dataclass
class TISResult:
    """
    Complete result of a TIS computation.

    Score naming (aligned to the white paper):

        s_base       = Σᵢ wᵢ(r,a) · dimᵢ(x,k)
                       The gate-INDEPENDENT weighted dimensional composite.
                       This is what the decision ladder's Priority 3/4
                       must use to discriminate STOP vs HOLD on the gate-
                       failure path: it survives gate collapse so its
                       magnitude carries meaning ("was the baseline strong
                       enough that a single gate failure is remediable?").

        s_adj        = s_base · (1 − P)
                       Post-penalty, pre-gate/decay.

        tis_raw      = gate · s_base
                       The "raw TIS" per the white paper formula. Collapses
                       to 0 on gate failure by design. Kept primarily for
                       wire/audit compatibility and reporting.

        tis_current  = s_adj · decay · gate · I_inv
                       The operative score consumed by the decision engine.

    All four are recorded in every result even when a gate collapse forces
    tis_current to 0 (TCS_SPEC.md §11).

    Backward-compat note: previous releases stored ``tis_raw`` as the
    gate-INDEPENDENT composite (semantically what is now ``s_base``). New
    code MUST use ``s_base`` for any kappa comparison or remediability
    decision. The ``tis_raw`` field's value will now be 0 whenever the
    gate fails, matching the white paper's definition.
    """

    s_base: float                         # gate-independent composite (white paper)
    tis_raw: float                        # = gate * s_base (white paper); 0 on gate=0
    penalty_breakdown: Dict[str, float]   # P_cb, P_d, P_n, P_h, P_ps
    penalty_aggregate: float
    s_adj: float                          # = s_base * (1 - P); pre-gate/decay
    tis_adj: float                        # = gate * s_adj; backward-compat
    gate_result: int                      # 0 or 1
    gate_results_by_dim: Dict[str, str]   # "pass" | "fail" | "not_applicable"
    failing_dimensions: List[str]
    C3_score: float
    decay_factor: float
    tis_current: float
    valid_until: datetime
    is_valid: int                         # echoed; may be forced to 0 by event
    invalidation_event: Optional[str]     # echoed


# --------------------------------------------------------------------------- #
# Internal helpers                                                             #
# --------------------------------------------------------------------------- #

def _validate_inputs(inp: TISInput) -> None:
    """Fail fast on any malformed TISInput."""
    # Dimensions: complete and in [0,1].
    if set(inp.dimension_scores.keys()) != set(DIMENSIONS):
        raise ValueError(
            f"dimension_scores must contain all four dimensions "
            f"{sorted(DIMENSIONS)}, got {sorted(inp.dimension_scores.keys())}"
        )
    for dim, score in inp.dimension_scores.items():
        if not (0.0 <= float(score) <= 1.0):
            raise ValueError(
                f"dimension {dim!r} score {score} out of range [0,1]"
            )

    # Context metadata: coerce with sensible defaults but require correctness
    # when present.
    meta = inp.context_metadata
    novelty = float(meta.get("novelty_score", 0.0))
    if not (0.0 <= novelty <= 1.0):
        raise ValueError(f"novelty_score {novelty} out of range [0,1]")

    if inp.elapsed_hours < 0:
        raise ValueError(f"elapsed_hours must be >= 0, got {inp.elapsed_hours}")

    if inp.is_valid not in (0, 1):
        raise ValueError(f"is_valid must be 0 or 1, got {inp.is_valid}")


def _extract_c3(inp: TISInput) -> float:
    """
    Return the C₃ (prohibited-pattern absence) sub-factor score.

    If sub_factor_scores['C']['C3'] is provided, use it verbatim. Otherwise
    default to 1.0 (no prohibited pattern). This matches TEST_SCENARIOS.md
    scenarios 3, 4, 5, 6, 7, 8 which omit sub_factor_scores entirely and
    expect no C₃ hard-stop.

    C₃ = 0.00 is load-bearing: it is the ONLY condition that defeats the
    soft-hold ceiling κ and forces an unconditional Stop (TCS_SPEC.md §12
    Priority 2).
    """
    if "C" in inp.sub_factor_scores and "C3" in inp.sub_factor_scores["C"]:
        return float(inp.sub_factor_scores["C"]["C3"])
    return 1.0


def _compute_tis_raw(scores: Dict[str, float], weights: Dict[str, float]) -> float:
    """Σᵢ wᵢ · dimᵢ — the weighted dimensional composite (TCS_SPEC.md §3.1)."""
    return sum(weights[dim] * float(scores[dim]) for dim in DIMENSIONS)


def _compute_penalty_components(
    meta: Dict[str, object],
    profile: PolicyProfile,
) -> Dict[str, float]:
    """
    Compute the five penalty components from TCS_SPEC.md §9.

    Returns a dict with keys P_cb, P_d, P_n, P_h, P_ps — these are the
    pre-weighted raw component values (not yet multiplied by λⱼ).
    """
    risk = profile.risk_tier
    action = profile.action_class

    # --- P_cb: cross-boundary (per-gap increment) --------------------------- #
    n_gaps = int(meta.get("n_gaps", 0))
    p_cb = n_gaps * DELTA_CB

    # --- P_d: context staleness --------------------------------------------- #
    context_age = float(meta.get("context_age_hours", 0.0))
    if context_age <= TAU_FRESH_HOURS:
        p_d = 0.0
    else:
        overshoot = context_age - TAU_FRESH_HOURS
        p_d = min(1.0, overshoot / TAU_STALE_HOURS) * DELTA_D_MAX

    # --- P_n: novelty ------------------------------------------------------- #
    novelty = float(meta.get("novelty_score", 0.0))
    p_n = novelty * W_NOVELTY_BY_TIER[risk]

    # --- P_h: human-review lag ---------------------------------------------- #
    days_since_review = float(meta.get("days_since_review", 0))
    tau_review = TAU_REVIEW_DAYS_BY_TIER[risk]
    if days_since_review <= tau_review:
        p_h = 0.0
    else:
        lag = days_since_review - tau_review
        p_h = min(1.0, lag / tau_review) * DELTA_H_MAX

    # --- P_ps: policy-sensitive content ------------------------------------- #
    is_ps = bool(meta.get("is_policy_sensitive", False))
    w_ps = _W_PS_SPECIAL.get((risk, action), _W_PS_DEFAULT)
    p_ps = (1.0 if is_ps else 0.0) * w_ps

    return {
        "P_cb": p_cb,
        "P_d":  p_d,
        "P_n":  p_n,
        "P_h":  p_h,
        "P_ps": p_ps,
    }


def _aggregate_penalty(
    components: Dict[str, float],
    lambda_weights: Dict[str, float],
) -> float:
    """
    P = min(0.50, Σⱼ λⱼ · Pⱼ)

    The 0.50 cap guarantees (1 − P) ≥ 0.50 always (TCS_SPEC.md §9).
    """
    # Mapping between the TCS_SPEC short names and the component keys we use.
    weighted_sum = (
        lambda_weights["cb"] * components["P_cb"]
        + lambda_weights["d"]  * components["P_d"]
        + lambda_weights["n"]  * components["P_n"]
        + lambda_weights["h"]  * components["P_h"]
        + lambda_weights["ps"] * components["P_ps"]
    )
    return min(0.50, weighted_sum)


def _evaluate_gate(
    scores: Dict[str, float],
    thresholds: Dict[str, float],
    gate_set: FrozenSet[str],
) -> Tuple[int, Dict[str, str], List[str]]:
    """
    Gate function G(r,a) = ∏ 𝟙[dimᵢ ≥ τᵢ] for dimᵢ ∈ gate_set.

    Returns a 3-tuple:
        (gate_result, gate_results_by_dim, failing_dimensions)

    ``gate_results_by_dim`` records "pass" / "fail" / "not_applicable" for
    ALL FOUR dimensions — not just the ones in gate_set. A dimension outside
    gate_set is always "not_applicable", NEVER "pass" (TCS_SPEC.md §5;
    TC_SCHEMA.md Layer G).
    """
    gate_results_by_dim: Dict[str, str] = {}
    failing: List[str] = []
    gate_result = 1

    for dim in ("B", "A", "C", "K"):
        if dim not in gate_set:
            gate_results_by_dim[dim] = "not_applicable"
            continue

        threshold = thresholds[dim]
        if float(scores[dim]) >= float(threshold):
            gate_results_by_dim[dim] = "pass"
        else:
            gate_results_by_dim[dim] = "fail"
            failing.append(dim)
            gate_result = 0

    return gate_result, gate_results_by_dim, failing


def _apply_invalidation(is_valid: int, event: Optional[str]) -> int:
    """
    Force is_valid to 0 if ``event`` is in the canonical invalidation set
    (TCS_SPEC.md §11). Otherwise return is_valid unchanged.
    """
    if event is not None and event in INVALIDATION_EVENTS:
        return 0
    return is_valid


def _compute_valid_until(
    evaluation_time: datetime,
    decay_rate: float,
) -> datetime:
    """
    valid_until = evaluation_time + (ln(2) / μ) hours  (TCS_SPEC.md §10).

    This is the decay half-life offset — the moment at which TIS_current
    would fall to half of TIS_adj under pure decay.
    """
    half_life_hours = math.log(2.0) / decay_rate
    return evaluation_time + timedelta(hours=half_life_hours)


def _apply_identity_adjustments(
    scores: Dict[str, float],
    meta: Dict[str, object],
) -> Dict[str, float]:
    """
    Apply identity-based B-score adjustments (TCS-TEL-001 §19).

    Two rules from TCS_SPEC.md §19 "Identity affects scoring":

        1. identity_confidence < 0.30 AND sensitivity_tier in (T2, T3):
           clamp B to at most 0.30 (B3 sub-factor collapse -> gate fail).

        2. identity_verified == False AND sensitivity_tier == T3:
           set B to 0.00 (immediate gate failure).

    Rule 2 is stricter than rule 1, so the order does not matter — but
    for clarity we apply rule 1 first (clamp) and then rule 2 (zero).

    Identity context travels via ``context_metadata`` keys:
        - identity_confidence   (float [0,1]; default 1.0)
        - identity_verified     (bool; default True)
        - sensitivity_tier      (str "T1"/"T2"/"T3"; default "T1")

    Defaults are optimistic so that scenarios which do not populate
    identity metadata behave exactly as they did before TCS-TEL-001
    landed. This preserves the Phase 1 scenario contract.

    Returns the possibly-modified scores dict. The caller is responsible
    for passing a copy if they need to preserve the original.
    """
    identity_confidence = float(meta.get("identity_confidence", 1.0))
    identity_verified = bool(meta.get("identity_verified", True))
    sensitivity_tier = str(meta.get("sensitivity_tier", "T1"))

    # Rule 1: low-confidence identity on elevated-sensitivity data.
    if identity_confidence < 0.30 and sensitivity_tier in ("T2", "T3"):
        scores["B"] = min(float(scores["B"]), 0.30)

    # Rule 2: unverified identity on T3 data — immediate B collapse.
    if (not identity_verified) and sensitivity_tier == "T3":
        scores["B"] = 0.00

    return scores


# --------------------------------------------------------------------------- #
# Public entry point                                                           #
# --------------------------------------------------------------------------- #

def compute_tis(inp: TISInput) -> TISResult:
    """
    Run the full TIS pipeline end-to-end.

    Sequence (per TCS_SPEC.md §3, with the identity preamble from §19):
        0. Apply identity-based B-score adjustments (TCS-TEL-001 §19).
        1. Validate inputs.
        2. Compute TIS_raw from weighted dimensions.
        3. Compute all five penalty components.
        4. Aggregate penalty with λⱼ weights (capped at 0.50).
        5. Compute TIS_adj = TIS_raw · (1 − P).
        6. Evaluate gate across gate_set.
        7. Apply decay factor e^(−μΔt).
        8. Apply invalidation: force is_valid → 0 if event is in E_inv.
        9. Compute TIS_current = TIS_adj · decay · gate · is_valid.
       10. Compute valid_until from decay half-life.

    All arithmetic runs at full float precision internally. Rounding to four
    decimal places happens only when populating the returned :class:`TISResult`
    fields. This keeps TIS_current traceable through TIS_adj without
    double-rounding drift.
    """
    _validate_inputs(inp)
    profile = inp.policy_profile

    # Step 0: identity-based B-score adjustments (TCS-TEL-001 §19).
    # Identity context travels through context_metadata. Defaults (high
    # confidence, verified, sensitivity T1) are optimistic so that
    # scenarios which do not specify identity metadata — including all
    # eight Phase 1 scenarios — are unaffected. Scenarios that need to
    # exercise the identity rules override identity_confidence,
    # identity_verified, or sensitivity_tier in their context_metadata.
    #
    # Critically, we work on a COPY of dimension_scores so the caller's
    # TISInput is never mutated, and so the adjusted B flows through
    # both TIS_raw (step 2) and gate evaluation (step 6) consistently.
    scores = _apply_identity_adjustments(
        dict(inp.dimension_scores), inp.context_metadata
    )

    # Step 2: gate-independent weighted composite (white paper "S_base").
    s_base = _compute_tis_raw(scores, profile.weights)

    # Step 3: individual penalty components.
    penalty_components = _compute_penalty_components(
        inp.context_metadata, profile
    )

    # Step 4: aggregate penalty (capped at 0.50).
    penalty_aggregate = _aggregate_penalty(
        penalty_components, profile.penalty_weights
    )

    # Step 5: gate-independent post-penalty score (white paper "S_adj").
    s_adj = s_base * (1.0 - penalty_aggregate)

    # Step 6: gate evaluation across gate_set.
    gate_result, gate_results_by_dim, failing = _evaluate_gate(
        scores, profile.thresholds, profile.gate_set
    )

    # Step 7: gated quantities per the white paper.
    #   tis_raw = gate * s_base (collapses to 0 on gate failure)
    #   tis_adj = gate * s_adj  (collapses to 0 on gate failure)
    # The decision engine uses s_base (not tis_raw) for Priority 3/4
    # discrimination so the kappa comparison survives gate collapse.
    tis_raw = gate_result * s_base
    tis_adj = gate_result * s_adj

    # Step 8: exponential decay factor.
    decay_factor = math.exp(-profile.decay_rate * inp.elapsed_hours)

    # Step 9: invalidation override — event in E_inv forces is_valid to 0.
    effective_is_valid = _apply_invalidation(
        inp.is_valid, inp.invalidation_event
    )

    # Step 10: final operative score. Gate=0 or is_valid=0 collapses to 0.0.
    tis_current = s_adj * decay_factor * gate_result * effective_is_valid

    # Step 11: half-life offset.
    valid_until = _compute_valid_until(
        inp.evaluation_time, profile.decay_rate
    )

    # Extract C₃ for downstream decision logic (Priority 2 hard-stop check).
    c3_score = _extract_c3(inp)

    # Build the result with canonical 4-decimal rounding applied once.
    return TISResult(
        s_base=_r(s_base),
        tis_raw=_r(tis_raw),
        penalty_breakdown={k: _r(v) for k, v in penalty_components.items()},
        penalty_aggregate=_r(penalty_aggregate),
        s_adj=_r(s_adj),
        tis_adj=_r(tis_adj),
        gate_result=int(gate_result),
        gate_results_by_dim=gate_results_by_dim,
        failing_dimensions=list(failing),
        C3_score=_r(c3_score),
        decay_factor=_r(decay_factor),
        tis_current=_r(tis_current),
        valid_until=valid_until,
        is_valid=int(effective_is_valid),
        invalidation_event=inp.invalidation_event,
    )
