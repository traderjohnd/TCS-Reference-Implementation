"""
tcs.api.routes_mode
===================

GET/POST /v2/mode — the global DEMO / LIVE operating-mode control
(demo-live operating-modes branch, Commit 1).

The BACKEND is the authority on the active mode: every external
provider call site enforces it via tcs.operating_mode regardless of
frontend state. Switching into LIVE MODE requires an explicit
confirmation flag — an unconfirmed request is rejected so an operator
cannot leave the investor-safe default by accident.
"""

from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from tcs.operating_mode import (
    DEFAULT_MODE,
    DEMO_MODE,
    LIVE_MODE,
    VALID_MODES,
    get_mode,
    set_mode,
)

router = APIRouter()


def _mode_payload(mode: str) -> Dict[str, Any]:
    return {
        "mode": mode,
        "default_mode": DEFAULT_MODE,
        "external_calls_allowed": mode == LIVE_MODE,
        "labels": {
            DEMO_MODE: "DEMO MODE",
            LIVE_MODE: "LIVE MODE",
        },
    }


@router.get("/mode")
def get_operating_mode(request: Request) -> Dict[str, Any]:
    """Current operating mode. The application starts in the
    deliberate, documented default (DEMO MODE — investor-safe)."""
    return _mode_payload(get_mode(request.app.state))


class ModeChangeBody(BaseModel):
    mode: str = Field(..., description="Target mode: 'demo' | 'live'.")
    confirm: bool = Field(
        False,
        description=(
            "Required (true) when switching INTO LIVE MODE — real "
            "external provider calls become possible. Ignored when "
            "returning to DEMO MODE."
        ),
    )


@router.post("/mode")
def set_operating_mode(body: ModeChangeBody, request: Request) -> Dict[str, Any]:
    """Switch the operating mode.

    Entering LIVE MODE without ``confirm: true`` is rejected with a
    structured 422 so the switch is always a deliberate operator
    action. Returning to DEMO MODE never requires confirmation —
    the safe direction is always one click.
    """
    if body.mode not in VALID_MODES:
        raise HTTPException(
            status_code=422,
            detail={
                "error": "unknown_mode",
                "message": f"mode must be one of {sorted(VALID_MODES)}",
            },
        )
    if body.mode == LIVE_MODE and not body.confirm:
        raise HTTPException(
            status_code=422,
            detail={
                "error": "confirmation_required",
                "message": (
                    "Switching to LIVE MODE enables real external "
                    "provider calls. Repeat the request with "
                    "confirm: true to proceed."
                ),
            },
        )
    new_mode = set_mode(request.app.state, body.mode)
    return _mode_payload(new_mode)
