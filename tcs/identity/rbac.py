"""
tcs.identity.rbac
==================

Role-based access control middleware for FastAPI.

Provides a dependency that extracts the user session from the
Authorization header and enforces role-based endpoint access.

When RBAC is disabled (default for backward compatibility), all
requests are permitted. Enable RBAC by setting
``app.state.rbac_enabled = True``.
"""

from __future__ import annotations

from typing import Optional

from fastapi import Depends, HTTPException, Request

from tcs.identity.session import UserSession, get_session


def get_current_user(request: Request) -> Optional[UserSession]:
    """
    Extract the current user from the Authorization header.

    Header format: ``Authorization: Bearer <token>``

    If RBAC is not enabled on the app, returns None (all access permitted).
    If RBAC is enabled but no valid token is provided, raises 401.
    """
    rbac_enabled = getattr(request.app.state, "rbac_enabled", False)
    if not rbac_enabled:
        return None

    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(
            status_code=401,
            detail="Missing or invalid Authorization header",
        )

    token = auth_header[7:]  # strip "Bearer "
    session = get_session(token)
    if session is None:
        raise HTTPException(
            status_code=401,
            detail="Invalid or expired session token",
        )

    return session


def enforce_rbac(request: Request) -> Optional[UserSession]:
    """
    FastAPI dependency that enforces RBAC on the current request.

    If RBAC is enabled:
        - Extracts user from Authorization header
        - Checks if any of the user's roles permit the requested endpoint
        - Raises 403 if no role permits access

    If RBAC is not enabled:
        - Returns None, allowing all access (backward compatible)
    """
    user = get_current_user(request)
    if user is None:
        return None  # RBAC not enabled

    method = request.method
    path = request.url.path

    if not user.can_access(method, path):
        raise HTTPException(
            status_code=403,
            detail=f"Role(s) {user.role_names} not authorized for {method} {path}",
        )

    return user
