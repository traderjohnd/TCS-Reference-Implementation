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

# tis-v2 (Commit 2) — canonical numerical system. The v1 float path above
# and below is frozen; everything Decimal lives in the *_v2 functions at
# the bottom of this module and in tcs.canonical.
from decimal import Decimal, localcontext

from tcs.canonical import (
    AdjustmentApplied,
    CALCULATION_VERSION_V2,
    CertificateInvariantError,
    TIS_DECIMAL_CONTEXT,
    UnsupportedCalculationVersion,
    canonical_nonnegative_parameter,
    canonical_score,
    compute_weighted_score,
    require_canonical_score,
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

    # ----- tis-v2 additions (defaulted, appended — Commit 2) ----- #
    #
    # On a v2 result the numerical fields above hold canonical Decimal
    # values; on a v1 result they hold floats exactly as before. The
    # discriminator is ``calculation_version``, whose default stays
    # "tis-v1" permanently — every site that produces a v2 result
    # assigns "tis-v2" explicitly, never via this default.
    effective_dimension_scores: Dict[str, Decimal] = field(default_factory=dict)
    observed_dimension_scores: Dict[str, Decimal] = field(default_factory=dict)
    adjustments_applied: List[AdjustmentApplied] = field(default_factory=list)
    calculation_version: str = "tis-v1"


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


# =========================================================================== #
# tis-v2 — canonical Decimal engine path (Commit 2 of the landing sequence)   #
# =========================================================================== #
#
# Everything below is ADDITIVE. The v1 path above is frozen and remains
# the production issuance path until Commit 5 atomically switches the
# orchestration call sites. ``compute_tis_v2`` is DORMANT in this commit:
# reachable only from tests, wired to no production caller.
#
# Numerical contract (TCS-BRIEF-v20):
#   * All arithmetic runs inside TIS_DECIMAL_CONTEXT (prec=28, HALF_UP).
#   * Every decision intermediate is canonicalized to 4dp BEFORE the next
#     stage consumes it (score_precision_policy
#     "decimal-4dp-half-up-each-decision-stage-context28-v1").
#   * Both operands of every gate comparison are canonical.
#   * No v2 function references a v1 float constant or the ``math``
#     module — enforced by a source-inspection test.
#
# Transport-boundary honesty: this commit converts TISInput values via
# ``Decimal(str(value))`` at the top of the v2 path. That is acceptable
# for dormant testing only; production v2 issuance stays disabled until
# Commit 5 lands Decimal-aware transport parsing at the wire edge.


# --------------------------------------------------------------------------- #
# v2 constants — Decimal, added alongside the v1 floats (never replacing)      #
# --------------------------------------------------------------------------- #

DELTA_CB_DECIMAL = Decimal("0.04")
DELTA_D_MAX_DECIMAL = Decimal("0.06")
DELTA_H_MAX_DECIMAL = Decimal("0.05")
TAU_FRESH_HOURS_DECIMAL = Decimal("1.0")
TAU_STALE_HOURS_DECIMAL = Decimal("1.0")

# p_cb clamp — an explicit tis-v2 policy change (v1 leaves p_cb unbounded).
# It bounds the INDIVIDUAL context-gap component so unbounded gap counts
# cannot disproportionately dominate the weighted aggregate below the
# aggregate cap. It is NOT needed to prevent a negative s_adjusted — the
# retained 0.5000 aggregate cap already guarantees s_adjusted >= 0.5 * s_base.
P_CB_MAX_V2 = Decimal("1.0000")

# Aggregate penalty cap — existing TCS_SPEC §9 semantics, retained in v2
# as part of the authoritative calculation semantics: engine issuance,
# Commit 4 pre-seal replay, and independent replay all apply it, and
# pre-seal validation enforces penalty_aggregate <= 0.5000.
PENALTY_AGGREGATE_CAP_V2 = Decimal("0.5000")

W_NOVELTY_BY_TIER_DECIMAL: Dict[str, Decimal] = {
    "r1": Decimal("0.03"),
    "r2": Decimal("0.05"),
    "r3": Decimal("0.08"),
}
TAU_REVIEW_DAYS_BY_TIER_DECIMAL: Dict[str, Decimal] = {
    "r1": Decimal("30"),
    "r2": Decimal("14"),
    "r3": Decimal("7"),
}
_W_PS_SPECIAL_DECIMAL: Dict[Tuple[str, str], Decimal] = {
    ("r3", "a4"): Decimal("0.08"),
    ("r3", "a3"): Decimal("0.05"),
}
_W_PS_DEFAULT_DECIMAL: Decimal = Decimal("0.03")

# Identity-adjustment constants (TCS_SPEC §19) as Decimal — the rules are
# unchanged; only the representation becomes explicit and consistent.
IDENTITY_LOW_CONFIDENCE_CAP = Decimal("0.3000")
IDENTITY_UNVERIFIED_T3_VALUE = Decimal("0.0000")
IDENTITY_CONFIDENCE_FLOOR = Decimal("0.30")

CANONICAL_PENALTY_KEYS: FrozenSet[str] = frozenset({"cb", "d", "n", "h", "ps"})

_BACK_DIMENSIONS: FrozenSet[str] = frozenset({"B", "A", "C", "K"})


# --------------------------------------------------------------------------- #
# Legacy float view (tis-v2 Commit 5a — removed by the 5b activation)          #
# --------------------------------------------------------------------------- #

def legacy_float_input_view(inp: TISInput) -> TISInput:
    """A float view of a Decimal-native TISInput for the LEGACY v1
    pipeline (v1 engine + v1 wire cannot carry Decimals).

    Producers became Decimal-native in Commit 5a while production
    issuance remains on v1; each v1 issuance call site applies this
    view. The Commit 5b activation diff removes these calls so the
    Decimal originals flow through and become component_scores_raw.
    """
    from dataclasses import replace as _replace
    return _replace(
        inp,
        dimension_scores={
            k: float(v) for k, v in inp.dimension_scores.items()
        },
        sub_factor_scores={
            dim: {sf: float(v) for sf, v in subs.items()}
            for dim, subs in (inp.sub_factor_scores or {}).items()
        },
        elapsed_hours=float(inp.elapsed_hours),
    )


# --------------------------------------------------------------------------- #
# v2 identity adjustments                                                      #
# --------------------------------------------------------------------------- #

def _apply_identity_adjustments_v2(
    observed: Dict[str, Decimal],
    meta: Dict[str, object],
) -> Tuple[Dict[str, Decimal], List[AdjustmentApplied]]:
    """Apply the TCS_SPEC §19 identity rules on canonical Decimal scores.

    Same rules as the v1 ``_apply_identity_adjustments`` — do not change
    them — but every value the rules touch is canonical, and every rule
    application that actually changes a score is recorded, in order, as
    an :class:`AdjustmentApplied`. A rule that fires but leaves the value
    unchanged records nothing.
    """
    # Canonicalize identity_confidence into the score domain BEFORE the
    # < 0.30 comparison. The value that fires the rule is exactly the
    # value that would later appear in the certificate as attested
    # identity confidence. (0.29995 canonicalizes to 0.3000 under
    # ROUND_HALF_UP and therefore does NOT fire; 0.29994 -> 0.2999 does.)
    identity_confidence = canonical_score(
        meta.get("identity_confidence", "1.0000")
    )

    # Strict Boolean requirement on v2. bool("false") is True in Python
    # (any non-empty string coerces truthy) — a v2 issuance must not
    # depend on that ambiguity. v1's loose coercion stays untouched.
    if "identity_verified" in meta:
        raw_verified = meta["identity_verified"]
        if not isinstance(raw_verified, bool):
            raise CertificateInvariantError(
                "v2 requires identity_verified to be a bool, "
                f"got {type(raw_verified).__name__}"
            )
        identity_verified = raw_verified
    else:
        identity_verified = True

    sensitivity_tier = str(meta.get("sensitivity_tier", "T1"))

    scores = dict(observed)
    adjustments: List[AdjustmentApplied] = []

    # Rule 1 (TCS_SPEC §19.1): low-confidence identity on T2/T3 data.
    if (
        identity_confidence < IDENTITY_CONFIDENCE_FLOOR
        and sensitivity_tier in ("T2", "T3")
    ):
        before = scores["B"]
        after = canonical_score(min(before, IDENTITY_LOW_CONFIDENCE_CAP))
        if after != before:
            scores["B"] = after
            adjustments.append(AdjustmentApplied(
                rule_id="TCS_SPEC_19_1", dimension="B",
                value_before=before, value_after=after,
                reason="identity_confidence_below_0_30",
            ))

    # Rule 2 (TCS_SPEC §19.2): unverified identity on T3 data.
    if (not identity_verified) and sensitivity_tier == "T3":
        before = scores["B"]
        after = IDENTITY_UNVERIFIED_T3_VALUE
        if after != before:
            scores["B"] = after
            adjustments.append(AdjustmentApplied(
                rule_id="TCS_SPEC_19_2", dimension="B",
                value_before=before, value_after=after,
                reason="unverified_identity_on_T3_data",
            ))

    return scores, adjustments


# --------------------------------------------------------------------------- #
# v2 penalties                                                                 #
# --------------------------------------------------------------------------- #

def _resolve_penalty_maxima_v2(profile: PolicyProfile) -> Dict[str, Decimal]:
    """Per-component maxima for the bounded-parameter penalty domain.

    Only Decimal constants — no float re-entry into the v2 path. ``n``
    and ``ps`` maxima are tier- and (tier, action)-dependent and are
    derived from the resolved profile, never hardcoded to r3 values.
    """
    return {
        "cb": P_CB_MAX_V2,
        "d": canonical_score(DELTA_D_MAX_DECIMAL),
        "n": canonical_score(W_NOVELTY_BY_TIER_DECIMAL[profile.risk_tier]),
        "h": canonical_score(DELTA_H_MAX_DECIMAL),
        "ps": canonical_score(_W_PS_SPECIAL_DECIMAL.get(
            (profile.risk_tier, profile.action_class),
            _W_PS_DEFAULT_DECIMAL,
        )),
    }


def _compute_penalty_components_v2(
    meta: Dict[str, object],
    profile: PolicyProfile,
) -> Dict[str, Decimal]:
    """Compute the five penalty components (TCS_SPEC §9) canonically.

    Returns a lowercase-keyed dict ``{'cb','d','n','h','ps'}``. Each
    value is created with its declared bounded-parameter canonicalizer
    (``canonical_nonnegative_parameter`` with the resolved per-component
    maximum) — the components are parameters, not scores, and are
    constructed as such.

    The COMPLETE numerical body runs inside ``TIS_DECIMAL_CONTEXT``:
    Decimal division and multiplication use the ambient context, and an
    unrelated global-context mutation must not be able to alter a
    certificate value through this path.
    """
    risk = profile.risk_tier
    action = profile.action_class
    maxima = _resolve_penalty_maxima_v2(profile)

    # n_gaps: strict non-negative integer. bool is an int subclass and is
    # rejected explicitly; fractional values are rejected rather than
    # silently truncated through int(...).
    raw_gaps = meta.get("n_gaps", 0)
    if isinstance(raw_gaps, bool) or not isinstance(raw_gaps, int):
        raise CertificateInvariantError(
            "v2 requires n_gaps to be an int, "
            f"got {type(raw_gaps).__name__}"
        )
    if raw_gaps < 0:
        raise CertificateInvariantError(
            f"v2 requires n_gaps >= 0, got {raw_gaps}"
        )

    # is_policy_sensitive: strict bool, same rationale as identity_verified.
    raw_ps = meta.get("is_policy_sensitive", False)
    if not isinstance(raw_ps, bool):
        raise CertificateInvariantError(
            "v2 requires is_policy_sensitive to be a bool, "
            f"got {type(raw_ps).__name__}"
        )

    with localcontext(TIS_DECIMAL_CONTEXT):
        # --- p_cb: cross-boundary, clamped at P_CB_MAX_V2 (tis-v2 only) --- #
        p_cb_raw = Decimal(raw_gaps) * DELTA_CB_DECIMAL
        p_cb = canonical_nonnegative_parameter(
            min(p_cb_raw, maxima["cb"]),
            field_name="penalty_breakdown.cb",
            maximum=maxima["cb"],
        )

        # --- p_d: context staleness -------------------------------------- #
        context_age = canonical_nonnegative_parameter(
            meta.get("context_age_hours", 0),
            field_name="context_age_hours",
        )
        if context_age <= TAU_FRESH_HOURS_DECIMAL:
            p_d_raw = Decimal("0")
        else:
            overshoot = context_age - TAU_FRESH_HOURS_DECIMAL
            ratio = min(Decimal("1"), overshoot / TAU_STALE_HOURS_DECIMAL)
            p_d_raw = ratio * DELTA_D_MAX_DECIMAL
        p_d = canonical_nonnegative_parameter(
            p_d_raw,
            field_name="penalty_breakdown.d",
            maximum=maxima["d"],
        )

        # --- p_n: novelty ------------------------------------------------- #
        novelty = canonical_score(meta.get("novelty_score", 0))
        p_n = canonical_nonnegative_parameter(
            novelty * W_NOVELTY_BY_TIER_DECIMAL[risk],
            field_name="penalty_breakdown.n",
            maximum=maxima["n"],
        )

        # --- p_h: human-review lag ---------------------------------------- #
        days_since_review = canonical_nonnegative_parameter(
            meta.get("days_since_review", 0),
            field_name="days_since_review",
        )
        tau_review = TAU_REVIEW_DAYS_BY_TIER_DECIMAL[risk]
        if days_since_review <= tau_review:
            p_h_raw = Decimal("0")
        else:
            lag = days_since_review - tau_review
            ratio = min(Decimal("1"), lag / tau_review)
            p_h_raw = ratio * DELTA_H_MAX_DECIMAL
        p_h = canonical_nonnegative_parameter(
            p_h_raw,
            field_name="penalty_breakdown.h",
            maximum=maxima["h"],
        )

        # --- p_ps: policy-sensitive content ------------------------------- #
        w_ps = _W_PS_SPECIAL_DECIMAL.get((risk, action), _W_PS_DEFAULT_DECIMAL)
        p_ps = canonical_nonnegative_parameter(
            (Decimal("1") if raw_ps else Decimal("0")) * w_ps,
            field_name="penalty_breakdown.ps",
            maximum=maxima["ps"],
        )

    return {"cb": p_cb, "d": p_d, "n": p_n, "h": p_h, "ps": p_ps}


def _aggregate_penalty_v2(
    components: Dict[str, Decimal],
    weights: Dict[str, Decimal],
) -> Decimal:
    """The authoritative tis-v2 penalty-aggregate formula.

        penalty_aggregate = canonical_score(
            min(PENALTY_AGGREGATE_CAP_V2, Σ weights[k] * components[k])
        )   iterated in sorted canonical key order

    The 0.5000 cap is existing TCS_SPEC §9 policy semantics, retained as
    part of tis-v2 calculation semantics. This exact formula is used by
    engine issuance, Commit 4 pre-seal replay, and independent replay
    tests. All arithmetic — including the weight-sum validation — runs
    inside the pinned context.
    """
    if set(components) != CANONICAL_PENALTY_KEYS:
        raise CertificateInvariantError(
            f"penalty components key set must be "
            f"{sorted(CANONICAL_PENALTY_KEYS)}, got {sorted(components)}"
        )
    if set(weights) != CANONICAL_PENALTY_KEYS:
        raise CertificateInvariantError(
            f"penalty weights key set must be "
            f"{sorted(CANONICAL_PENALTY_KEYS)}, got {sorted(weights)}"
        )
    for k, v in weights.items():
        require_canonical_score(v, f"penalty_weights.{k}")

    with localcontext(TIS_DECIMAL_CONTEXT):
        weight_sum = sum(weights.values(), Decimal("0"))
        if weight_sum != Decimal("1.0000"):
            raise CertificateInvariantError(
                f"penalty weights sum {weight_sum} != Decimal('1.0000')"
            )
        weighted_sum = sum(
            (weights[k] * components[k] for k in sorted(components)),
            Decimal("0"),
        )
        capped = min(PENALTY_AGGREGATE_CAP_V2, weighted_sum)
    return canonical_score(capped)


# --------------------------------------------------------------------------- #
# v2 gate                                                                      #
# --------------------------------------------------------------------------- #

def _evaluate_gate_v2(
    scores: Dict[str, Decimal],
    thresholds: Dict[str, Decimal],
    gate_set: FrozenSet[str],
) -> Tuple[int, Dict[str, str], List[str]]:
    """Gate function on canonical operands.

    Same semantics as the v1 ``_evaluate_gate`` (TCS_SPEC §5): results
    are recorded for ALL FOUR dimensions; a dimension outside gate_set
    is always "not_applicable", never "pass". Both operands of every
    comparison are canonical Decimals — the score and threshold used to
    produce a verdict are numerically identical to the values that will
    be recorded in the certificate.
    """
    gate_results_by_dim: Dict[str, str] = {}
    failing: List[str] = []
    gate_result = 1

    for dim in ("B", "A", "C", "K"):
        if dim not in gate_set:
            gate_results_by_dim[dim] = "not_applicable"
            continue
        if scores[dim] >= thresholds[dim]:
            gate_results_by_dim[dim] = "pass"
        else:
            gate_results_by_dim[dim] = "fail"
            failing.append(dim)
            gate_result = 0

    return gate_result, gate_results_by_dim, failing


# --------------------------------------------------------------------------- #
# v2 decay                                                                     #
# --------------------------------------------------------------------------- #

def _compute_decay_factor_v2(
    decay_rate: Decimal,
    elapsed_hours: Decimal,
    calculation_version: str,
) -> Decimal:
    """Decimal in, Decimal out — no binary float on this path.

    Two-stage rounding contract (decay_algorithm_version
    "decimal-exp-context28-half-even-then-4dp-half-up-v1"):
    ``Decimal.exp()`` is correctly rounded using ROUND_HALF_EVEN at
    prec=28 regardless of the context rounding mode, then the result is
    quantized to 4dp under ROUND_HALF_UP by ``canonical_score``.
    """
    if calculation_version != CALCULATION_VERSION_V2:
        raise UnsupportedCalculationVersion(calculation_version)
    with localcontext(TIS_DECIMAL_CONTEXT):
        factor = (-decay_rate * elapsed_hours).exp()
    if not factor.is_finite() or factor < Decimal("0") or factor > Decimal("1"):
        raise CertificateInvariantError(
            f"decay_factor out of range [0, 1]: {factor}"
        )
    return canonical_score(factor)


def _compute_valid_until_v2(
    evaluation_time: datetime,
    decay_rate: Decimal,
) -> datetime:
    """valid_until = evaluation_time + (ln(2) / μ) hours, via Decimal.ln().

    The final ``float()`` conversion exists solely because
    ``datetime.timedelta`` takes a float; ``valid_until`` is temporal
    metadata, not a canonical numerical certificate field, so it is not
    part of the 4dp score-domain contract.
    """
    if decay_rate <= 0:
        raise CertificateInvariantError(
            f"resolved_decay_rate must be positive for half-life "
            f"computation, got {decay_rate}"
        )
    with localcontext(TIS_DECIMAL_CONTEXT):
        half_life_hours = Decimal(2).ln() / decay_rate
    return evaluation_time + timedelta(hours=float(half_life_hours))


# --------------------------------------------------------------------------- #
# v2 public entry point — DORMANT until Commit 5 switches orchestration        #
# --------------------------------------------------------------------------- #

def compute_tis_v2(inp: TISInput) -> TISResult:
    """Run the full TIS pipeline on the canonical Decimal path.

    Fixed order of operations (TCS-BRIEF-v20 §1):

        canonicalize observed -> apply adjustments (recorded, ordered)
        -> canonicalize thresholds and weights -> s_base
        -> penalty components -> penalty_aggregate -> s_adj
        -> gate on canonical operands -> tis_raw / tis_adj
        -> decay_factor (Decimal.exp) -> tis_current

    Every decision intermediate is canonicalized before the next stage
    consumes it. The returned TISResult carries canonical Decimal values
    in the numerical fields, ``calculation_version="tis-v2"`` assigned
    explicitly, and the observed/effective score tiers plus the ordered
    adjustment record that Commit 4's certificate construction requires.

    DORMANT: no production caller invokes this function in Commit 2.
    Production orchestration stays on ``compute_tis`` until Commit 5
    completes every downstream persistence and API surface and switches
    all issuance call sites atomically.
    """
    _validate_inputs(inp)
    profile = inp.policy_profile

    # Observed tier: canonicalized pre-adjustment values entering the
    # policy adjustment rules.
    observed = {
        dim: canonical_score(inp.dimension_scores[dim])
        for dim in ("B", "A", "C", "K")
    }

    # Weights: canonical, and the resolved sum MUST be exactly 1.0000 —
    # fail closed, never normalize silently (TCS-BRIEF-v20 §5).
    weights = {
        dim: canonical_score(profile.weights[dim])
        for dim in ("B", "A", "C", "K")
    }
    with localcontext(TIS_DECIMAL_CONTEXT):
        weight_sum = sum(weights.values(), Decimal("0"))
    if weight_sum != Decimal("1.0000"):
        raise CertificateInvariantError(
            f"resolved dimension weights sum {weight_sum} != "
            f"Decimal('1.0000') for profile {profile.profile_id}"
        )

    # Thresholds: canonicalize only what the profile carries, then
    # validate structure — thresholds a subset of BACK, gate_set a
    # subset of BACK, and every gated dimension must have a threshold.
    # Profiles legitimately carry thresholds for non-gated dimensions.
    thresholds = {
        dim: canonical_score(value)
        for dim, value in profile.thresholds.items()
    }
    if not set(thresholds).issubset(_BACK_DIMENSIONS):
        raise CertificateInvariantError(
            f"unknown threshold dimension: "
            f"{sorted(set(thresholds) - _BACK_DIMENSIONS)}"
        )
    gate_set = frozenset(profile.gate_set)
    if not gate_set.issubset(_BACK_DIMENSIONS):
        raise CertificateInvariantError(
            f"unknown gated dimension: {sorted(gate_set - _BACK_DIMENSIONS)}"
        )
    missing = gate_set - set(thresholds)
    if missing:
        raise CertificateInvariantError(
            f"gated dimensions missing thresholds: {sorted(missing)}"
        )

    # Effective tier: ordered identity adjustments on canonical values.
    effective, adjustments = _apply_identity_adjustments_v2(
        observed, inp.context_metadata
    )

    # s_base: pinned weighted sum, quantized once.
    s_base = compute_weighted_score(effective, weights)

    # Penalties: canonical components, authoritative aggregate.
    penalty_components = _compute_penalty_components_v2(
        inp.context_metadata, profile
    )
    penalty_weights = {
        k: canonical_score(v) for k, v in profile.penalty_weights.items()
    }
    penalty_aggregate = _aggregate_penalty_v2(
        penalty_components, penalty_weights
    )

    # s_adj: canonicalized before anything consumes it.
    with localcontext(TIS_DECIMAL_CONTEXT):
        s_adj = canonical_score(
            s_base * (Decimal("1.0000") - penalty_aggregate)
        )

    # Gate on canonical effective scores and canonical thresholds.
    gate_result, gate_results_by_dim, failing = _evaluate_gate_v2(
        effective, thresholds, gate_set
    )

    # Gated quantities, canonicalized stage by stage.
    with localcontext(TIS_DECIMAL_CONTEXT):
        tis_raw = canonical_score(Decimal(gate_result) * s_base)
        tis_adj = canonical_score(Decimal(gate_result) * s_adj)

    # Decay: canonical parameters, Decimal.exp, versioned.
    elapsed_hours = canonical_nonnegative_parameter(
        inp.elapsed_hours, field_name="elapsed_hours"
    )
    resolved_decay_rate = canonical_nonnegative_parameter(
        profile.decay_rate, field_name="resolved_decay_rate"
    )
    decay_factor = _compute_decay_factor_v2(
        resolved_decay_rate, elapsed_hours, CALCULATION_VERSION_V2
    )

    # Invalidation: pure set-membership logic, shared with v1.
    effective_is_valid = _apply_invalidation(
        inp.is_valid, inp.invalidation_event
    )

    # tis_current: the canonical value map_decision will consume.
    with localcontext(TIS_DECIMAL_CONTEXT):
        tis_current = canonical_score(
            tis_adj * decay_factor * Decimal(effective_is_valid)
        )

    valid_until = _compute_valid_until_v2(
        inp.evaluation_time, resolved_decay_rate
    )
    c3_score = canonical_score(_extract_c3(inp))

    return TISResult(
        s_base=s_base,
        tis_raw=tis_raw,
        penalty_breakdown=dict(penalty_components),
        penalty_aggregate=penalty_aggregate,
        s_adj=s_adj,
        tis_adj=tis_adj,
        gate_result=int(gate_result),
        gate_results_by_dim=gate_results_by_dim,
        failing_dimensions=list(failing),
        C3_score=c3_score,
        decay_factor=decay_factor,
        tis_current=tis_current,
        valid_until=valid_until,
        is_valid=int(effective_is_valid),
        invalidation_event=inp.invalidation_event,
        effective_dimension_scores=dict(effective),
        observed_dimension_scores=dict(observed),
        adjustments_applied=list(adjustments),
        calculation_version=CALCULATION_VERSION_V2,
    )
