"""
tcs.api.routes_simulation
=========================

Phase 3 Step 6 — Shadow Testing and Simulation API endpoints.

POST /v1/simulation/replay
POST /v1/simulation/shadow-mode/start
POST /v1/simulation/shadow-mode/stop
GET  /v1/simulation/shadow-mode/status
POST /v1/simulation/ab-compare
"""

from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, HTTPException, Query, Request

from tcs.policy_profiles import load_profile
from tcs.simulation.historical_replay import replay
from tcs.simulation.impact_report import generate_impact_report
from tcs.simulation.shadow_mode import (
    get_shadow_status,
    start_shadow_mode,
    stop_shadow_mode,
)
from tcs.simulation.ab_comparison import compare_profiles


router = APIRouter()


@router.post("/simulation/replay")
def run_replay(
    request: Request,
    profile_id: str = Query(...),
    window_hours: float = Query(168.0, ge=1.0, le=8760.0),
    max_records: int = Query(1000, ge=1, le=10000),
) -> Dict[str, Any]:
    """
    Replay historical TC data against a proposed profile.

    Returns decision distribution comparison and flipped decisions.
    """
    store = request.app.state.store
    try:
        profile = load_profile(profile_id)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Profile '{profile_id}' not found")

    result = replay(
        store, profile,
        window_hours=window_hours,
        max_records=max_records,
    )

    # Also generate impact report
    report = generate_impact_report(result)
    output = result.to_dict()
    output["impact_report"] = report.to_dict()
    return output


@router.post("/simulation/shadow-mode/start")
def shadow_start(
    profile_id: str = Query(...),
) -> Dict[str, Any]:
    """Start shadow mode for a profile."""
    return start_shadow_mode(profile_id)


@router.post("/simulation/shadow-mode/stop")
def shadow_stop() -> Dict[str, Any]:
    """Stop shadow mode."""
    return stop_shadow_mode()


@router.get("/simulation/shadow-mode/status")
def shadow_status() -> Dict[str, Any]:
    """Get current shadow mode status."""
    return get_shadow_status()


@router.post("/simulation/ab-compare")
def ab_compare(
    request: Request,
    profile_a_id: str = Query(...),
    profile_b_id: str = Query(...),
    window_hours: float = Query(168.0, ge=1.0, le=8760.0),
) -> Dict[str, Any]:
    """Compare two profiles against the same historical data."""
    store = request.app.state.store
    try:
        profile_a = load_profile(profile_a_id)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Profile '{profile_a_id}' not found")
    try:
        profile_b = load_profile(profile_b_id)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Profile '{profile_b_id}' not found")

    result = compare_profiles(
        store, profile_a, profile_b, window_hours=window_hours,
    )
    return result.to_dict()
