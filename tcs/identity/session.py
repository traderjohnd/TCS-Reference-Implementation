"""
tcs.identity.session
=====================

Session management and token validation for TCS API.

For the reference implementation, this uses a simple token-to-user
mapping stored in memory. Production deployments would integrate
with an identity provider (OAuth2, SAML, etc.).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional

from tcs.identity.roles import Role, get_role


@dataclass
class UserSession:
    """An authenticated user session."""
    user_id: str
    username: str
    roles: List[Role]
    token: str

    def has_role(self, role_name: str) -> bool:
        return any(r.name == role_name for r in self.roles)

    def can_access(self, method: str, path: str) -> bool:
        """Check if any of the user's roles permit access."""
        return any(r.can_access(method, path) for r in self.roles)

    @property
    def role_names(self) -> List[str]:
        return [r.name for r in self.roles]


# --------------------------------------------------------------------------- #
# In-memory session store (reference implementation)                           #
# --------------------------------------------------------------------------- #

_sessions: Dict[str, UserSession] = {}


def create_session(
    user_id: str,
    username: str,
    role_names: List[str],
    token: str,
) -> UserSession:
    """Create and register a new user session."""
    roles = [get_role(name) for name in role_names]
    session = UserSession(
        user_id=user_id,
        username=username,
        roles=roles,
        token=token,
    )
    _sessions[token] = session
    return session


def get_session(token: str) -> Optional[UserSession]:
    """Look up a session by token."""
    return _sessions.get(token)


def revoke_session(token: str) -> bool:
    """Revoke a session. Returns True if it existed."""
    return _sessions.pop(token, None) is not None


def clear_sessions() -> None:
    """Clear all sessions (for testing)."""
    _sessions.clear()
