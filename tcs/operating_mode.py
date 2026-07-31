"""
tcs.operating_mode
==================

Global DEMO / LIVE operating mode (demo-live operating-modes branch,
Commit 1).

Two top-level application states:

    DEMO MODE ("demo")
        Deterministic, investor-safe presentation environment. External
        LLM and web calls are BLOCKED at the backend regardless of what
        the frontend shows; only the deterministic mock provider may
        execute. Governed executions record execution_mode
        "scripted_demo" so scripted output can never masquerade as live
        provider output.

    LIVE MODE ("live")
        Real-provider environment. External provider calls are
        permitted; governed executions record execution_mode
        "live_provider" together with provider/model identity in the
        workflow trace.

The BACKEND enforces the active mode — the frontend indicator is
presentation only. Every provider-construction site calls
:func:`enforce_external_call` before any external client is built.

Default mode: **DEMO** — deliberate and documented. A freshly started
application is investor-safe by construction; entering LIVE MODE is an
explicit, confirmed operator action (see tcs.api.routes_mode).

The mode lives on ``app.state`` (process lifetime). A restart returns
to the documented default rather than silently resuming LIVE.
"""

from __future__ import annotations

from typing import Any

DEMO_MODE = "demo"
LIVE_MODE = "live"
VALID_MODES = (DEMO_MODE, LIVE_MODE)

#: The deliberate, documented startup default (investor-safe).
DEFAULT_MODE = DEMO_MODE

#: Providers that are deterministic and permitted in DEMO MODE.
#: Everything else is an external call and requires LIVE MODE.
DEMO_SAFE_PROVIDERS = frozenset({None, "", "mock"})

#: Execution-mode labels recorded in workflow traces, evaluation
#: snapshots, and Trust Certificate scope attestations. Scripted output
#: must never be labeled as live provider output, and vice versa.
EXECUTION_MODE_SCRIPTED = "scripted_demo"
EXECUTION_MODE_LIVE = "live_provider"


class ExternalCallBlockedError(RuntimeError):
    """Raised when an external provider or web call is attempted while
    DEMO MODE is active. Routes convert this to a structured HTTP 403."""

    def __init__(self, provider: str) -> None:
        super().__init__(
            f"DEMO MODE is active: external call to provider "
            f"{provider!r} is blocked. Switch to LIVE MODE to use real "
            f"providers."
        )
        self.provider = provider


def get_mode(app_state: Any) -> str:
    """Current operating mode ('demo' | 'live'); the documented default
    when unset."""
    mode = getattr(app_state, "operating_mode", DEFAULT_MODE)
    return mode if mode in VALID_MODES else DEFAULT_MODE


def set_mode(app_state: Any, mode: str) -> str:
    if mode not in VALID_MODES:
        raise ValueError(f"unknown operating mode {mode!r}")
    app_state.operating_mode = mode
    return mode


def is_external_provider(provider_name: Any) -> bool:
    name = (provider_name or "").strip().lower() if isinstance(
        provider_name, str) else provider_name
    return name not in DEMO_SAFE_PROVIDERS


def enforce_external_call(app_state: Any, provider_name: Any) -> None:
    """Backend enforcement chokepoint — call BEFORE constructing any
    external provider client or performing any external retrieval.

    Deterministic (mock / absent) providers pass in either mode.
    External providers raise :class:`ExternalCallBlockedError` while
    DEMO MODE is active.
    """
    if not is_external_provider(provider_name):
        return
    if get_mode(app_state) != LIVE_MODE:
        raise ExternalCallBlockedError(str(provider_name))


def execution_mode_for(provider_name: Any) -> str:
    """The truthful execution-mode label for a governed run."""
    return (EXECUTION_MODE_LIVE if is_external_provider(provider_name)
            else EXECUTION_MODE_SCRIPTED)


__all__ = [
    "DEMO_MODE", "LIVE_MODE", "VALID_MODES", "DEFAULT_MODE",
    "DEMO_SAFE_PROVIDERS",
    "EXECUTION_MODE_SCRIPTED", "EXECUTION_MODE_LIVE",
    "ExternalCallBlockedError",
    "get_mode", "set_mode", "is_external_provider",
    "enforce_external_call", "execution_mode_for",
]
