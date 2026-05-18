"""
tcs.api.routes_pll
==================

Phase 3 Step 4 — Policy Learning Layer API endpoints.

GET  /v2/dynamics/pll/recommendations
POST /v2/dynamics/pll/approve/{record_id}
POST /v2/dynamics/pll/reject/{record_id}
POST /v2/dynamics/pll/rollback/{record_id}
GET  /v2/dynamics/pll/history
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query, Request

from tcs.dynamics.pll import (
    approve_recommendation,
    get_recommendations,
    reject_recommendation,
    rollback_recommendation,
)


router = APIRouter()


@router.get("/dynamics/pll/recommendations")
def list_recommendations(
    request: Request,
    profile_id: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
) -> List[Dict[str, Any]]:
    """List pending or filtered adaptation recommendations."""
    store = request.app.state.store
    return get_recommendations(store, profile_id=profile_id, status=status)


@router.post("/dynamics/pll/approve/{record_id}")
def approve(
    request: Request,
    record_id: str,
    approver: str = Query("system"),
) -> Dict[str, Any]:
    """Approve a pending adaptation recommendation."""
    store = request.app.state.store
    result = approve_recommendation(store, record_id, approver=approver)
    if result is None:
        raise HTTPException(status_code=404, detail="Record not found")
    return result


@router.post("/dynamics/pll/reject/{record_id}")
def reject(
    request: Request,
    record_id: str,
    approver: str = Query("system"),
) -> Dict[str, Any]:
    """Reject a pending adaptation recommendation."""
    store = request.app.state.store
    result = reject_recommendation(store, record_id, approver=approver)
    if result is None:
        raise HTTPException(status_code=404, detail="Record not found")
    return result


@router.post("/dynamics/pll/rollback/{record_id}")
def rollback(
    request: Request,
    record_id: str,
    approver: str = Query("system"),
) -> Dict[str, Any]:
    """Rollback an approved adaptation (within rollback window)."""
    store = request.app.state.store
    result = rollback_recommendation(store, record_id, approver=approver)
    if result is None:
        raise HTTPException(
            status_code=404,
            detail="Record not found, not approved, or rollback window expired",
        )
    return result


@router.get("/dynamics/pll/history")
def adaptation_history(
    request: Request,
    profile_id: Optional[str] = Query(None),
) -> List[Dict[str, Any]]:
    """Full adaptation history, optionally filtered by profile."""
    store = request.app.state.store
    return get_recommendations(store, profile_id=profile_id)
