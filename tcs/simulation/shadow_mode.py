"""
tcs.simulation.shadow_mode
==========================

Shadow mode evaluation — runs TCS in parallel with a production AI
pipeline without enforcement. Shadow TCs are flagged and stored
separately from production TCs.

Shadow mode is used for:
    - Initial onboarding of new deployments
    - Testing proposed profile changes in parallel
    - Collecting baseline data before enabling enforcement
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Optional

from tcs.persistence import CertificateStore


# --------------------------------------------------------------------------- #
# Shadow mode state (in-memory for reference implementation)                   #
# --------------------------------------------------------------------------- #

_shadow_state: Dict[str, Any] = {
    "active": False,
    "profile_id": None,
    "started_at": None,
    "evaluations": 0,
}


def start_shadow_mode(profile_id: str) -> Dict[str, Any]:
    """
    Start shadow mode for the given profile.

    Returns the shadow mode status.
    """
    global _shadow_state
    _shadow_state = {
        "active": True,
        "profile_id": profile_id,
        "started_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "evaluations": 0,
    }
    return dict(_shadow_state)


def stop_shadow_mode() -> Dict[str, Any]:
    """
    Stop shadow mode.

    Returns the final shadow mode status with evaluation count.
    """
    global _shadow_state
    result = dict(_shadow_state)
    result["stopped_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    result["active"] = False
    _shadow_state = {
        "active": False,
        "profile_id": None,
        "started_at": None,
        "evaluations": 0,
    }
    return result


def get_shadow_status() -> Dict[str, Any]:
    """Return current shadow mode status."""
    return dict(_shadow_state)


def record_shadow_evaluation() -> None:
    """Increment the shadow evaluation counter."""
    global _shadow_state
    _shadow_state["evaluations"] = _shadow_state.get("evaluations", 0) + 1


def is_shadow_active() -> bool:
    """Check if shadow mode is currently active."""
    return _shadow_state.get("active", False)
