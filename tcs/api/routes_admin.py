"""
tcs.api.routes_admin
=====================

Phase 3 Step 10 — Admin endpoints for Platform Admin.

GET  /v1/admin/users     — list users/sessions
POST /v1/admin/users     — create user with role
GET  /v1/admin/modules   — module configuration status
"""

from __future__ import annotations

import uuid
from typing import Any, Dict, List

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from tcs.identity.roles import ROLES, get_role
from tcs.identity.session import create_session, _sessions


router = APIRouter()


class CreateUserBody(BaseModel):
    """Create a user session with specified roles."""
    user_id: str
    username: str
    roles: List[str] = Field(..., min_length=1)


@router.get("/admin/users")
def list_users() -> Dict[str, Any]:
    """List active user sessions."""
    users = []
    for token, session in _sessions.items():
        users.append({
            "user_id": session.user_id,
            "username": session.username,
            "roles": session.role_names,
            "token": token,
        })
    return {"count": len(users), "users": users}


@router.post("/admin/users")
def create_user(body: CreateUserBody) -> Dict[str, Any]:
    """Create a new user session with assigned roles."""
    for role_name in body.roles:
        if role_name not in ROLES:
            raise HTTPException(
                status_code=400,
                detail=f"Unknown role: {role_name!r}. Valid: {sorted(ROLES.keys())}",
            )

    token = f"tok-{uuid.uuid4().hex[:16]}"
    session = create_session(
        user_id=body.user_id,
        username=body.username,
        role_names=body.roles,
        token=token,
    )
    return {
        "user_id": session.user_id,
        "username": session.username,
        "roles": session.role_names,
        "token": session.token,
    }


@router.get("/admin/modules")
def module_status() -> Dict[str, Any]:
    """Return module configuration status."""
    modules = {
        "tis_engine": {"status": "active", "version": "1.0.0"},
        "decision_engine": {"status": "active", "version": "1.0.0"},
        "governed_context": {"status": "active", "version": "2.0.0"},
        "persistence": {"status": "active", "version": "1.0.0"},
        "trust_loss": {"status": "active", "version": "1.0.0"},
        "drift_detection": {"status": "active", "version": "1.0.0"},
        "policy_learning_layer": {"status": "active", "version": "1.0.0"},
        "recovery_orchestrator": {"status": "active", "version": "1.0.0"},
        "simulation": {"status": "active", "version": "1.0.0"},
        "rbac": {"status": "active", "version": "1.0.0"},
        "regulatory_packs": {"status": "active", "version": "1.0.0"},
    }
    return {"count": len(modules), "modules": modules}
