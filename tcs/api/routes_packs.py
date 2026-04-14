"""
tcs.api.routes_packs
=====================

Phase 3 Step 8 — Regulatory Pack API endpoints.

GET  /v1/packs                  — list available packs
GET  /v1/packs/active           — currently active pack
GET  /v1/packs/{pack_id}        — pack detail
POST /v1/packs/{pack_id}/deploy — deploy pack
GET  /v1/packs/{pack_id}/export — audit export
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query, Request

from tcs.packs.pack_manager import (
    deploy_pack,
    generate_audit_export,
    get_active_pack,
    get_pack,
    list_packs,
)


router = APIRouter()


@router.get("/packs")
def list_available_packs() -> List[Dict[str, Any]]:
    """List all available regulatory packs."""
    return list_packs()


@router.get("/packs/active")
def active_pack() -> Dict[str, Any]:
    """Get the currently active regulatory pack."""
    pack = get_active_pack()
    if pack is None:
        return {"active": False, "pack_id": None}
    return {"active": True, **pack}


@router.get("/packs/{pack_id}")
def pack_detail(pack_id: str) -> Dict[str, Any]:
    """Get full pack configuration."""
    pack = get_pack(pack_id)
    if pack is None:
        raise HTTPException(status_code=404, detail=f"Pack '{pack_id}' not found")
    return pack


@router.post("/packs/{pack_id}/deploy")
def deploy(pack_id: str) -> Dict[str, Any]:
    """Deploy a regulatory pack as the active configuration."""
    try:
        return deploy_pack(pack_id)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.get("/packs/{pack_id}/export")
def audit_export(
    request: Request,
    pack_id: str,
    window_hours: float = Query(720.0, ge=1.0, le=8760.0),
) -> Dict[str, Any]:
    """Generate audit export in pack-specific format."""
    store = request.app.state.store
    try:
        return generate_audit_export(store, pack_id, window_hours=window_hours)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))
