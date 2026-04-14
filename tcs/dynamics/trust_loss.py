"""
tcs.dynamics.trust_loss
=======================

Trust Loss Function (Appendix D of TCS whitepaper).

Measures governance effectiveness degradation across five components
over a sliding window of TC data:

    L_t = alpha*K + beta*P + gamma*D + delta*E + epsilon*G

    K = known dimension increase (mean TIS decline — lower mean = higher K)
    P = policy deviation rate (gate failure rate over window)
    D = data/context drift (source quality degradation signal)
    E = environmental volatility (regime shift indicator — future hook)
    G = governance infrastructure degradation (1 - governance_integrity_score)

Each component is in [0, 1]. L_t is the weighted sum — higher means
worse governance effectiveness. A perfectly functioning system has
L_t close to 0; a system in crisis approaches 1.

The function reads TC data from the persistent store via
``store.query_window(window_hours)`` and computes all five components
from real evaluation records. Component E is stubbed at 0.0 (Phase 3
future hook for regime shift detection).

Domain-specific weights control how much each failure mode contributes
to the aggregate. Financial services weights policy deviation (beta)
heavily because regulatory compliance failure is existential.
Healthcare weights uncertainty (alpha) more because diagnostic
confidence is the primary safety signal.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from tcs.dynamics.models import TrustLossResult
from tcs.persistence import CertificateStore


# --------------------------------------------------------------------------- #
# Domain weight configurations                                                 #
# --------------------------------------------------------------------------- #

DOMAIN_WEIGHTS: Dict[str, Dict[str, float]] = {
    "financial_services": {
        "alpha": 0.15,   # K — known
        "beta":  0.35,   # P — policy deviation
        "gamma": 0.20,   # D — data drift
        "delta": 0.10,   # E — environmental volatility
        "epsilon": 0.20, # G — governance degradation
    },
    "healthcare": {
        "alpha": 0.20,
        "beta":  0.30,
        "gamma": 0.20,
        "delta": 0.10,
        "epsilon": 0.20,
    },
    "enterprise": {
        "alpha": 0.20,
        "beta":  0.30,
        "gamma": 0.25,
        "delta": 0.10,
        "epsilon": 0.15,
    },
}

#: Default weights used when domain is not in DOMAIN_WEIGHTS.
DEFAULT_WEIGHTS: Dict[str, float] = DOMAIN_WEIGHTS["enterprise"]

#: Ideal TIS mean for computing U component. A perfect system has
#: mean TIS near this value; degradation is measured as distance from it.
IDEAL_TIS_MEAN: float = 0.90


# --------------------------------------------------------------------------- #
# Component computations                                                       #
# --------------------------------------------------------------------------- #

def _compute_K(tis_values: List[float]) -> float:
    """
    Known dimension increase component.

    K = 1 - (mean_tis / ideal_mean), clamped to [0, 1].

    When mean TIS equals the ideal (0.90), K = 0.
    When mean TIS drops to 0, K = 1.
    """
    if not tis_values:
        return 0.0
    mean_tis = sum(tis_values) / len(tis_values)
    k = 1.0 - (mean_tis / IDEAL_TIS_MEAN) if IDEAL_TIS_MEAN > 0 else 0.0
    return max(0.0, min(1.0, k))


def _compute_P(decisions: List[str]) -> float:
    """
    Policy deviation rate component.

    P = (count of Stop + Hold + Escalate) / total_decisions.

    Gate failures (decisions other than Allow/Observe) indicate policy
    deviation — the system is producing outcomes that deviate from the
    desired governance state.
    """
    if not decisions:
        return 0.0
    failures = sum(
        1 for d in decisions if d in ("Stop", "Hold", "Escalate", "Rollback")
    )
    return failures / len(decisions)


def _compute_D(tc_rows: List[Dict[str, Any]]) -> float:
    """
    Data/context drift component.

    Measures source quality degradation by examining integration
    boundary gaps and attribution scores from TC content_json.

    D = mean(integration_boundary_gaps > 0) across window TCs.
    Higher means more TCs have data quality issues.
    """
    if not tc_rows:
        return 0.0
    gap_count = 0
    for row in tc_rows:
        try:
            tc_data = json.loads(row["content_json"])
            gaps = tc_data.get("integration_boundary_gaps", 0)
            if gaps > 0:
                gap_count += 1
        except (json.JSONDecodeError, KeyError):
            pass
    return gap_count / len(tc_rows)


def _compute_E() -> float:
    """
    Environmental volatility component.

    Stubbed at 0.0 for Phase 3. Future hook for regime shift
    detection (market volatility, regulatory change, etc.).
    """
    return 0.0


def _compute_G(store: CertificateStore) -> float:
    """
    Governance infrastructure degradation component.

    G = 1 - pct_clean, where pct_clean = (Allow + Observe) / total.

    A healthy system where most evaluations pass has G near 0.
    A degraded system with mostly failures has G near 1.

    Uses decision_counts() (denormalized column) rather than the full
    governance_integrity_score() to avoid expensive chain verification
    and full TC deserialization.
    """
    counts = store.decision_counts()
    total = sum(counts.values())
    if total == 0:
        return 0.0
    clean = counts.get("Allow", 0) + counts.get("Observe", 0)
    pct_clean = clean / total
    return max(0.0, min(1.0, 1.0 - pct_clean))


# --------------------------------------------------------------------------- #
# Main computation                                                             #
# --------------------------------------------------------------------------- #

def compute_trust_loss(
    store: CertificateStore,
    *,
    domain: str = "financial_services",
    window_hours: float = 24.0,
) -> TrustLossResult:
    """
    Compute the Trust Loss Function over a sliding window.

    Reads TC data from the store for the specified window and computes
    all five components. Returns a :class:`TrustLossResult` with the
    aggregate L_t, individual component values, and the dominant
    (highest weighted contribution) component.

    Parameters
    ----------
    store
        CertificateStore to read TC data from.
    domain
        Domain identifier for weight selection. One of
        ``financial_services``, ``healthcare``, ``enterprise``.
    window_hours
        Sliding window size in hours. Default 24.

    Returns
    -------
    TrustLossResult
        Complete trust loss computation result.
    """
    weights = DOMAIN_WEIGHTS.get(domain, DEFAULT_WEIGHTS)

    # Query windowed TC data
    tc_rows = store.query_window(window_hours)
    tis_values = [float(r["tis_current"]) for r in tc_rows]
    decisions = [str(r["decision"]) for r in tc_rows]

    # Compute each component
    k_val = _compute_K(tis_values)
    p_val = _compute_P(decisions)
    d_val = _compute_D(tc_rows)
    e_val = _compute_E()
    g_val = _compute_G(store)

    components = {
        "K": k_val,
        "P": p_val,
        "D": d_val,
        "E": e_val,
        "G": g_val,
    }

    # Weighted sum
    L_t = (
        weights["alpha"]   * k_val
        + weights["beta"]  * p_val
        + weights["gamma"] * d_val
        + weights["delta"] * e_val
        + weights["epsilon"] * g_val
    )

    # Identify dominant component (highest weighted contribution)
    weighted_contributions = {
        "K": weights["alpha"]   * k_val,
        "P": weights["beta"]    * p_val,
        "D": weights["gamma"]   * d_val,
        "E": weights["delta"]   * e_val,
        "G": weights["epsilon"] * g_val,
    }
    dominant = max(weighted_contributions, key=weighted_contributions.get)  # type: ignore[arg-type]

    return TrustLossResult(
        L_t=L_t,
        components=components,
        weights=weights,
        dominant_component=dominant,
        window_hours=window_hours,
        window_evaluations=len(tc_rows),
        domain=domain,
        computed_at=datetime.now(timezone.utc),
    )
