"""
tcs.api.routes_dynamics
=======================

Phase 3 adaptive governance API endpoints.

GET /v1/dynamics/trust-loss — Trust Loss Function computation
GET /v1/dynamics/drift      — Trust Drift Detection (Step 3)
"""

from __future__ import annotations

from typing import Any, Dict, List

from fastapi import APIRouter, Query, Request

from tcs.dynamics.trust_loss import compute_trust_loss
from tcs.dynamics.drift import compute_drift


router = APIRouter()


@router.get("/dynamics/trust-loss")
def get_trust_loss(
    request: Request,
    domain: str = Query("financial_services"),
    window_hours: float = Query(24.0, ge=0.1, le=720.0),
) -> Dict[str, Any]:
    """
    Compute the Trust Loss Function over a sliding window.

    Returns L_t (aggregate trust loss), individual component values
    (U, P, D, E, G), the dominant component, and window statistics.

    Higher L_t = worse governance effectiveness.
    """
    store = request.app.state.store
    result = compute_trust_loss(
        store,
        domain=domain,
        window_hours=window_hours,
    )
    return result.to_dict()


@router.get("/dynamics/drift")
def get_drift(
    request: Request,
    window_hours: float = Query(24.0, ge=0.1, le=720.0),
) -> List[Dict[str, Any]]:
    """
    Compute Trust Drift Detection across all governance contexts.

    Returns a list of drift signals, one per context (policy_set_id),
    sorted by D_trust descending (worst drift first).

    Each signal includes D_trust, component breakdown (level, variance,
    failure), threshold breach status, trend, and recommendation.
    """
    store = request.app.state.store
    signals = compute_drift(store, window_hours=window_hours)
    return [s.to_dict() for s in signals]
