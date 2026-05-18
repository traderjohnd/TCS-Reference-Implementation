"""
tcs.api.routes_auth
====================

Authentication endpoints for the Control Plane.

POST /v2/auth/login — create session with username and role
"""

from __future__ import annotations

import uuid
from typing import Any, Dict

from fastapi import APIRouter
from pydantic import BaseModel

from tcs.identity.session import create_session


router = APIRouter()


class LoginBody(BaseModel):
    """Login request body."""
    username: str
    role: str = "governance_admin"


@router.post("/auth/login")
def login(body: LoginBody) -> Dict[str, Any]:
    """Create a user session and return the token."""
    token = f"tok-{uuid.uuid4().hex[:16]}"
    session = create_session(
        user_id=f"user-{body.username}",
        username=body.username,
        role_names=[body.role],
        token=token,
    )
    return {
        "token": session.token,
        "user_id": session.user_id,
        "username": session.username,
        "roles": session.role_names,
    }
