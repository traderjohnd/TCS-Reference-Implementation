"""
tcs.api.routes_standards
=========================

Phase 4 / Slice 4 — Standards library + composer API.

Routes (all under /v2):

    GET  /standards/taxonomy        Taxonomy: Industry > Sub-industry > Use case
    GET  /standards/library         List of all standards (summaries)
    GET  /standards/{id}            Single standard (full detail incl. adjustments)
    POST /standards/compose         Preview a composed profile (does not register)
    POST /standards/deploy          Compose + register as pack + deploy

The compose endpoint is read-only (preview); the deploy endpoint
registers the composed pack and activates it via the existing Pack
deployment mechanism. This keeps Packs the single source of truth for
"what's currently governing" — composed profiles enter that system as
first-class packs with composer_metadata attached for full audit.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field


router = APIRouter()


# --------------------------------------------------------------------------- #
# Request / response shapes                                                    #
# --------------------------------------------------------------------------- #

class ComposeRequest(BaseModel):
    industry: str
    sub_industry: str
    use_case: str
    standard_ids: List[str] = Field(default_factory=list)
    risk_tier: str
    action_class: str
    # Optional human-readable name. When deploying, this becomes the
    # pack's display name in Available Packs / Active Profile. When
    # composing for preview only, ignored. None falls back to an
    # auto-generated descriptive label.
    pack_name: Optional[str] = None


# --------------------------------------------------------------------------- #
# Routes                                                                       #
# --------------------------------------------------------------------------- #

@router.get("/standards/taxonomy")
def get_taxonomy() -> Dict[str, Any]:
    """
    Return the Industry > Sub-industry > Use case taxonomy.

    The frontend drill-down uses this to populate the cascading
    select boxes.
    """
    from tcs.standards import TAXONOMY
    return {"taxonomy": TAXONOMY}


@router.get("/standards/library")
def get_library(
    industry: Optional[str] = None,
    sub_industry: Optional[str] = None,
    use_case: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Return standards (summary form), optionally filtered by taxonomy.

    Summaries omit the heavy ``profile_adjustments`` block; clients
    that need the full adjustment detail should fetch the specific
    standard via /standards/{id}.
    """
    from tcs.standards import list_standards

    results = list_standards()
    if industry:
        results = [s for s in results if s["industry"] == industry]
    if sub_industry:
        results = [s for s in results if s["sub_industry"] == sub_industry]
    if use_case:
        results = [s for s in results if use_case in s["applies_to_use_cases"]]

    return {"standards": results, "total": len(results)}


@router.get("/standards/{standard_id}")
def get_standard_detail(standard_id: str) -> Dict[str, Any]:
    """Return a single standard with its full adjustment block + control_interpretation."""
    from tcs.standards import get_standard

    s = get_standard(standard_id)
    if s is None:
        raise HTTPException(404, f"Standard {standard_id!r} not found")
    return {"standard": s}


@router.post("/standards/compose")
def compose_preview(body: ComposeRequest) -> Dict[str, Any]:
    """
    Compose a profile from the selected standards and return the
    preview without registering or deploying.

    The UI calls this on every drill-down change to render the live
    composed-profile preview panel + per-standard contributions.
    """
    from tcs.standards import compose_profile

    try:
        composed = compose_profile(
            industry=body.industry,
            sub_industry=body.sub_industry,
            use_case=body.use_case,
            standard_ids=body.standard_ids,
            risk_tier=body.risk_tier,
            action_class=body.action_class,
        )
    except ValueError as e:
        raise HTTPException(400, str(e))

    return {
        "composed": composed.to_dict(),
        "preview_only": True,
    }


@router.post("/standards/deploy")
def deploy_composed(body: ComposeRequest) -> Dict[str, Any]:
    """
    Compose, register as a pack, and deploy as the active pack.

    The composed profile enters the existing Pack system as a
    first-class pack (pack_id = ``composed-<hash16>``). Deployment
    activates it through the standard ``deploy_pack`` flow, so all
    downstream consumers (chat, scoring, TC, dashboards) see it as
    the active governance regime.
    """
    from tcs.packs.pack_manager import deploy_pack, register_composed_pack
    from tcs.standards import compose_profile

    try:
        composed = compose_profile(
            industry=body.industry,
            sub_industry=body.sub_industry,
            use_case=body.use_case,
            standard_ids=body.standard_ids,
            risk_tier=body.risk_tier,
            action_class=body.action_class,
        )
    except ValueError as e:
        raise HTTPException(400, str(e))

    pack = register_composed_pack(composed, name=body.pack_name)
    deployment = deploy_pack(pack["pack_id"])

    return {
        "deployment": deployment,
        "pack_id": pack["pack_id"],
        "pack_name": pack["name"],
        "profile_hash": composed.profile_hash,
        "composer_metadata": composed.composer_metadata,
        "regulatory_references": composed.regulatory_references,
        "required_controls": composed.required_controls,
        "hard_prohibitions": composed.hard_prohibitions,
    }
