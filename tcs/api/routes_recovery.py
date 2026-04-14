"""
tcs.api.routes_recovery
=======================

Phase 3 Step 5 — Recovery Orchestrator API endpoints.

GET  /v1/dynamics/recovery/status
POST /v1/dynamics/recovery/activate
POST /v1/dynamics/recovery/advance-phase
GET  /v1/dynamics/recovery/history
"""

from __future__ import annotations

from typing import Any, Dict, List

from fastapi import APIRouter, HTTPException, Query, Request

from tcs.dynamics.recovery import (
    advance_phase,
    check_and_activate,
    complete_recovery,
    get_recovery_history,
    get_recovery_status,
)


router = APIRouter()


@router.get("/dynamics/recovery/status")
def recovery_status(
    request: Request,
    domain: str = Query("financial_services"),
    window_hours: float = Query(24.0, ge=0.1, le=720.0),
) -> Dict[str, Any]:
    """Get current recovery status."""
    store = request.app.state.store
    return get_recovery_status(store, domain=domain, window_hours=window_hours)


@router.post("/dynamics/recovery/activate")
def activate_recovery(
    request: Request,
    domain: str = Query("financial_services"),
    window_hours: float = Query(24.0, ge=0.1, le=720.0),
) -> Dict[str, Any]:
    """
    Activate recovery mode if D_trust >= D_crit.

    Returns the new incident or error if already active or no crisis.
    """
    store = request.app.state.store
    result = check_and_activate(
        store, domain=domain, window_hours=window_hours,
    )
    if result is None:
        # Check why
        active = store.get_active_recovery()
        if active is not None:
            raise HTTPException(
                status_code=409,
                detail="Recovery already active",
            )
        raise HTTPException(
            status_code=400,
            detail="No D_crit threshold breach detected",
        )
    return result


@router.post("/dynamics/recovery/advance-phase")
def advance_recovery_phase(
    request: Request,
    incident_id: str = Query(...),
    domain: str = Query("financial_services"),
    window_hours: float = Query(24.0, ge=0.1, le=720.0),
) -> Dict[str, Any]:
    """
    Advance the active recovery to the next phase.

    Requires the incident_id of the active recovery.
    """
    store = request.app.state.store
    result = advance_phase(
        store, incident_id, domain=domain, window_hours=window_hours,
    )
    if result is None:
        raise HTTPException(
            status_code=400,
            detail="Cannot advance: incident not found, not active, or at final phase",
        )
    return result


@router.post("/dynamics/recovery/complete")
def complete_recovery_endpoint(
    request: Request,
    incident_id: str = Query(...),
    domain: str = Query("financial_services"),
    window_hours: float = Query(24.0, ge=0.1, le=720.0),
) -> Dict[str, Any]:
    """Complete a recovery at the stabilization phase."""
    store = request.app.state.store
    result = complete_recovery(
        store, incident_id, domain=domain, window_hours=window_hours,
    )
    if result is None:
        raise HTTPException(
            status_code=400,
            detail="Cannot complete: not in stabilization phase or not active",
        )
    return result


@router.get("/dynamics/recovery/history")
def recovery_history(request: Request) -> List[Dict[str, Any]]:
    """Full recovery incident history."""
    store = request.app.state.store
    return get_recovery_history(store)
