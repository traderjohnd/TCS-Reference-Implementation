"""
tcs.dynamics.drift
==================

Trust Drift Detection (Phase 3 Step 3).

Measures governance effectiveness degradation across three components
per governance context (risk_tier, action_class, connection_type):

    D_trust(r,a,ct) = w1 * |delta_mu| + w2 * |delta_sigma| + w3 * delta_L_prime

    delta_mu      = change in mean TIS over comparison window
    delta_sigma   = change in TIS standard deviation
    delta_L_prime = change in gate failure acceleration

Default weights: w1=0.40, w2=0.30, w3=0.30

Three thresholds trigger escalating responses:
    D_warn  = 0.020 -> early warning, increase monitoring frequency
    D_alert = 0.040 -> policy adaptation trigger, PLL recommendation
    D_crit  = 0.080 -> Recovery Orchestrator activation, fail-closed at r3

The function splits the window into two halves (earlier vs. later) and
computes drift as the change between them. This captures the *direction*
of governance degradation, not just current state.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from tcs.dynamics.models import DriftSignal
from tcs.persistence import CertificateStore


# --------------------------------------------------------------------------- #
# Default weights and thresholds                                               #
# --------------------------------------------------------------------------- #

DEFAULT_DRIFT_WEIGHTS: Dict[str, float] = {
    "w1": 0.40,   # level drift (delta_mu)
    "w2": 0.30,   # variance drift (delta_sigma)
    "w3": 0.30,   # failure drift (delta_L_prime)
}

DRIFT_THRESHOLDS: Dict[str, float] = {
    "D_warn":  0.020,
    "D_alert": 0.040,
    "D_crit":  0.080,
}

#: Minimum evaluations per half-window to produce a meaningful drift
#: signal. Below this, we report D_trust = 0.0 (insufficient data).
MIN_HALF_WINDOW: int = 2


# --------------------------------------------------------------------------- #
# Statistical helpers                                                          #
# --------------------------------------------------------------------------- #

def _mean(values: List[float]) -> float:
    """Mean of a non-empty list."""
    return sum(values) / len(values)


def _stddev(values: List[float]) -> float:
    """Population standard deviation of a non-empty list."""
    if len(values) < 2:
        return 0.0
    mu = _mean(values)
    variance = sum((x - mu) ** 2 for x in values) / len(values)
    return math.sqrt(variance)


def _failure_rate(decisions: List[str]) -> float:
    """Fraction of decisions that are gate failures (not Allow/Observe)."""
    if not decisions:
        return 0.0
    failures = sum(1 for d in decisions if d not in ("Allow", "Observe"))
    return failures / len(decisions)


# --------------------------------------------------------------------------- #
# Per-context drift computation                                                #
# --------------------------------------------------------------------------- #

def _compute_drift_for_context(
    rows: List[Dict[str, Any]],
    context: str,
    window_hours: float,
    weights: Dict[str, float],
) -> DriftSignal:
    """
    Compute drift signal for a single governance context.

    Splits the rows (already sorted by evaluation_timestamp ASC) into
    two halves and compares statistics between them.
    """
    n = len(rows)
    mid = n // 2

    # Insufficient data -> zero drift
    if mid < MIN_HALF_WINDOW or (n - mid) < MIN_HALF_WINDOW:
        return DriftSignal(
            context=context,
            D_trust=0.0,
            components={"level": 0.0, "variance": 0.0, "failure": 0.0},
            threshold_breached=None,
            trend="stable",
            recommendation=None,
            window_hours=window_hours,
            window_evaluations=n,
            computed_at=datetime.now(timezone.utc),
        )

    earlier = rows[:mid]
    later = rows[mid:]

    # Level drift: |delta_mu|
    tis_earlier = [float(r["tis_current"]) for r in earlier]
    tis_later = [float(r["tis_current"]) for r in later]
    mu_earlier = _mean(tis_earlier)
    mu_later = _mean(tis_later)
    delta_mu = abs(mu_later - mu_earlier)

    # Variance drift: |delta_sigma|
    sigma_earlier = _stddev(tis_earlier)
    sigma_later = _stddev(tis_later)
    delta_sigma = abs(sigma_later - sigma_earlier)

    # Failure drift: change in failure rate (acceleration)
    dec_earlier = [str(r["decision"]) for r in earlier]
    dec_later = [str(r["decision"]) for r in later]
    fr_earlier = _failure_rate(dec_earlier)
    fr_later = _failure_rate(dec_later)
    delta_L_prime = max(0.0, fr_later - fr_earlier)  # only count acceleration

    # Weighted aggregate
    D_trust = (
        weights["w1"] * delta_mu
        + weights["w2"] * delta_sigma
        + weights["w3"] * delta_L_prime
    )

    # Threshold classification
    threshold_breached: Optional[str] = None
    if D_trust >= DRIFT_THRESHOLDS["D_crit"]:
        threshold_breached = "D_crit"
    elif D_trust >= DRIFT_THRESHOLDS["D_alert"]:
        threshold_breached = "D_alert"
    elif D_trust >= DRIFT_THRESHOLDS["D_warn"]:
        threshold_breached = "D_warn"

    # Trend: based on TIS direction
    if mu_later < mu_earlier - 0.01:
        trend = "increasing"   # drift is increasing (TIS declining)
    elif mu_later > mu_earlier + 0.01:
        trend = "decreasing"   # drift is decreasing (TIS improving)
    else:
        trend = "stable"

    # Recommendation
    recommendation: Optional[str] = None
    if threshold_breached == "D_alert":
        recommendation = "pll_review"
    elif threshold_breached == "D_crit":
        recommendation = "recovery_activate"

    return DriftSignal(
        context=context,
        D_trust=round(D_trust, 4),
        components={
            "level": round(delta_mu, 4),
            "variance": round(delta_sigma, 4),
            "failure": round(delta_L_prime, 4),
        },
        threshold_breached=threshold_breached,
        trend=trend,
        recommendation=recommendation,
        window_hours=window_hours,
        window_evaluations=n,
        computed_at=datetime.now(timezone.utc),
    )


# --------------------------------------------------------------------------- #
# Main entry point                                                             #
# --------------------------------------------------------------------------- #

def compute_drift(
    store: CertificateStore,
    *,
    window_hours: float = 24.0,
    weights: Optional[Dict[str, float]] = None,
) -> List[DriftSignal]:
    """
    Compute drift signals for all governance contexts in the window.

    Parameters
    ----------
    store
        CertificateStore to read TC data from.
    window_hours
        Sliding window size in hours. Default 24.
    weights
        Override drift component weights. Default w1=0.40, w2=0.30, w3=0.30.

    Returns
    -------
    list[DriftSignal]
        One signal per governance context, sorted by D_trust descending.
    """
    w = weights or DEFAULT_DRIFT_WEIGHTS
    grouped = store.query_window_by_context(window_hours)

    signals = []
    for context, rows in grouped.items():
        signal = _compute_drift_for_context(rows, context, window_hours, w)
        signals.append(signal)

    # Sort by D_trust descending (worst drift first)
    signals.sort(key=lambda s: s.D_trust, reverse=True)
    return signals
