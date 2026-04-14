"""
tcs.sidecar
===========

Runtime control system. Intercepts adapted workflow outputs, runs the
full governance pipeline (assemble -> TIS -> decision -> certificate ->
persist), and returns a ``GovernedResponse`` the calling application
treats as authoritative.

This is where TCS stops being a scoring model and starts being a
runtime control system — the Phase 2 goal.

Two components:

    enforcement_controller  — decision -> GovernedResponse mapping,
                              fail-safe -> GovernedResponse mapping.
                              Pure logic, no computation, no I/O.

    request_interceptor     — orchestrates the full pipeline:
                              InterceptedRequest -> assemble_context_v2
                              -> dimension scoring -> compute_tis
                              -> map_decision -> generate_certificate
                              -> store.issue -> enforce
                              -> GovernedResponse
"""

from tcs.sidecar.enforcement_controller import (
    EnforcementController,
    GovernedResponse,
    enforce,
    enforce_fail_safe,
)
from tcs.sidecar.request_interceptor import (
    RequestInterceptor,
    ScoringPolicy,
    default_scoring_policy,
)

__all__ = [
    "EnforcementController",
    "GovernedResponse",
    "enforce",
    "enforce_fail_safe",
    "RequestInterceptor",
    "ScoringPolicy",
    "default_scoring_policy",
]
