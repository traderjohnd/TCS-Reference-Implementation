"""
tests/test_rbac.py
==================

Phase 3 Step 7 — RBAC and Identity Module tests.

Tests verify:
    1. All eight roles are defined with correct permissions
    2. Role access checks work correctly
    3. RBAC middleware enforces authorization when enabled
    4. RBAC middleware is transparent when disabled
    5. Dual-control workflow enforces two-approver rule
    6. Self-approval is blocked
    7. Session management works
    8. Unauthorized requests return 403
    9. Missing auth returns 401
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from tcs.persistence import CertificateStore
from tcs.api.app import create_app
from tcs.identity.roles import (
    ROLES,
    PLATFORM_ADMIN,
    GOVERNANCE_ADMIN,
    POLICY_EDITOR,
    COMPLIANCE_OFFICER,
    WORKFLOW_OWNER,
    AUDITOR,
    EXECUTIVE_VIEWER,
    EXCEPTION_APPROVER,
    get_role,
)
from tcs.identity.session import (
    create_session,
    get_session,
    revoke_session,
    clear_sessions,
)
from tcs.identity.dual_control import (
    submit_change,
    first_approve,
    second_approve,
    reject_change,
    get_request,
    list_pending,
    clear_requests,
)


# --------------------------------------------------------------------------- #
# Fixtures                                                                     #
# --------------------------------------------------------------------------- #

@pytest.fixture(autouse=True)
def _clean_sessions():
    """Clear sessions and dual-control requests between tests."""
    clear_sessions()
    clear_requests()
    yield
    clear_sessions()
    clear_requests()


@pytest.fixture
def rbac_app():
    """FastAPI app with RBAC enabled."""
    store = CertificateStore(":memory:")
    app = create_app(store=store)
    app.state.rbac_enabled = True
    yield app
    store.close()


@pytest.fixture
def rbac_client(rbac_app):
    return TestClient(rbac_app)


@pytest.fixture
def no_rbac_client():
    """Client with RBAC disabled (default)."""
    store = CertificateStore(":memory:")
    app = create_app(store=store)
    client = TestClient(app)
    yield client
    store.close()


def _make_token(user_id: str, username: str, roles: list) -> str:
    token = f"token-{user_id}"
    create_session(user_id, username, roles, token)
    return token


# --------------------------------------------------------------------------- #
# Role definition tests                                                        #
# --------------------------------------------------------------------------- #

class TestRoleDefinitions:
    def test_eight_roles_defined(self):
        assert len(ROLES) == 8

    def test_all_role_names(self):
        expected = {
            "platform_admin", "governance_admin", "policy_editor",
            "compliance_officer", "workflow_owner", "auditor",
            "executive_viewer", "exception_approver",
        }
        assert set(ROLES.keys()) == expected

    def test_get_role_valid(self):
        role = get_role("platform_admin")
        assert role.name == "platform_admin"

    def test_get_role_invalid(self):
        with pytest.raises(KeyError):
            get_role("nonexistent")


# --------------------------------------------------------------------------- #
# Role access tests                                                            #
# --------------------------------------------------------------------------- #

class TestRoleAccess:
    def test_platform_admin_can_access_everything(self):
        assert PLATFORM_ADMIN.can_access("GET", "/v2/govern")
        assert PLATFORM_ADMIN.can_access("POST", "/v2/dynamics/pll/approve/x")
        assert PLATFORM_ADMIN.can_access("GET", "/v2/certificates/abc")

    def test_governance_admin_can_govern(self):
        assert GOVERNANCE_ADMIN.can_access("POST", "/v2/govern")
        assert GOVERNANCE_ADMIN.can_access("GET", "/v2/certificates/abc")
        assert GOVERNANCE_ADMIN.can_access("GET", "/v2/dynamics/drift")

    def test_governance_admin_blocked_from_admin(self):
        assert not GOVERNANCE_ADMIN.can_access("POST", "/v2/admin/users")
        assert not GOVERNANCE_ADMIN.can_access("POST", "/v2/admin/modules")

    def test_policy_editor_blocked_from_pll_approve(self):
        assert not POLICY_EDITOR.can_access("POST", "/v2/dynamics/pll/approve/x")
        assert not POLICY_EDITOR.can_access("POST", "/v2/dynamics/recovery/activate")

    def test_policy_editor_can_simulate(self):
        assert POLICY_EDITOR.can_access("POST", "/v2/simulation/replay")

    def test_auditor_read_only(self):
        assert AUDITOR.can_access("GET", "/v2/certificates/abc")
        assert not AUDITOR.can_access("POST", "/v2/govern")
        assert not AUDITOR.can_access("GET", "/v2/dynamics/drift")

    def test_executive_viewer_metrics_only(self):
        assert EXECUTIVE_VIEWER.can_access("GET", "/v2/metrics/live")
        assert not EXECUTIVE_VIEWER.can_access("GET", "/v2/certificates/abc")
        assert not EXECUTIVE_VIEWER.can_access("GET", "/v2/dynamics/drift")

    def test_workflow_owner_blocked_from_dynamics(self):
        assert not WORKFLOW_OWNER.can_access("GET", "/v2/dynamics/drift")
        assert not WORKFLOW_OWNER.can_access("POST", "/v2/simulation/replay")

    def test_exception_approver_can_approve_pll(self):
        assert EXCEPTION_APPROVER.can_access("POST", "/v2/dynamics/pll/approve/x")


# --------------------------------------------------------------------------- #
# Session management tests                                                     #
# --------------------------------------------------------------------------- #

class TestSessionManagement:
    def test_create_and_get_session(self):
        session = create_session("u1", "alice", ["governance_admin"], "tok1")
        assert session.user_id == "u1"
        assert session.has_role("governance_admin")
        assert get_session("tok1") is session

    def test_revoke_session(self):
        create_session("u1", "alice", ["governance_admin"], "tok1")
        assert revoke_session("tok1")
        assert get_session("tok1") is None

    def test_revoke_nonexistent(self):
        assert not revoke_session("nonexistent")

    def test_session_can_access(self):
        session = create_session("u1", "alice", ["governance_admin"], "tok1")
        assert session.can_access("GET", "/v2/govern")
        assert not session.can_access("POST", "/v2/admin/users")

    def test_multi_role_session(self):
        session = create_session("u1", "alice",
                                 ["policy_editor", "exception_approver"], "tok1")
        # policy_editor can simulate
        assert session.can_access("POST", "/v2/simulation/replay")
        # exception_approver can approve PLL
        assert session.can_access("POST", "/v2/dynamics/pll/approve/x")


# --------------------------------------------------------------------------- #
# RBAC middleware tests                                                        #
# --------------------------------------------------------------------------- #

class TestRBACMiddleware:
    def test_rbac_disabled_allows_all(self, no_rbac_client):
        """With RBAC disabled, all endpoints are accessible."""
        resp = no_rbac_client.get("/v2/health")
        assert resp.status_code == 200

    def test_rbac_enabled_no_auth_returns_401(self, rbac_client):
        resp = rbac_client.get("/v2/health")
        assert resp.status_code == 401

    def test_rbac_enabled_invalid_token_returns_401(self, rbac_client):
        resp = rbac_client.get(
            "/v2/health",
            headers={"Authorization": "Bearer invalid-token"},
        )
        assert resp.status_code == 401

    def test_rbac_enabled_valid_token_allows(self, rbac_client):
        token = _make_token("u1", "alice", ["governance_admin"])
        resp = rbac_client.get(
            "/v2/health",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200

    def test_rbac_enabled_unauthorized_returns_403(self, rbac_client):
        token = _make_token("u1", "alice", ["executive_viewer"])
        resp = rbac_client.get(
            "/v2/certificates",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 403

    def test_platform_admin_accesses_all(self, rbac_client):
        token = _make_token("u1", "admin", ["platform_admin"])
        resp = rbac_client.get(
            "/v2/metrics/live",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200

    def test_auditor_can_read_certificates(self, rbac_client):
        token = _make_token("u1", "auditor", ["auditor"])
        resp = rbac_client.get(
            "/v2/certificates",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200


# --------------------------------------------------------------------------- #
# Dual-control workflow tests                                                  #
# --------------------------------------------------------------------------- #

class TestDualControl:
    def test_submit_creates_pending(self):
        req = submit_change("dc-1", "editor1", "risk_tolerance_profile",
                            "fin-r3", "Lower theta_allow", {"theta_allow": 0.80})
        assert req.status == "pending_first_approval"
        assert req.submitted_by == "editor1"

    def test_first_approval(self):
        submit_change("dc-1", "editor1", "risk_tolerance_profile",
                       "fin-r3", "Lower theta_allow", {})
        req = first_approve("dc-1", "admin1", "Justified by drift data")
        assert req.status == "pending_second_approval"
        assert req.first_approver == "admin1"

    def test_second_approval_completes(self):
        submit_change("dc-1", "editor1", "risk_tolerance_profile",
                       "fin-r3", "Lower theta_allow", {})
        first_approve("dc-1", "admin1")
        req = second_approve("dc-1", "admin2")
        assert req.status == "approved"
        assert req.second_approver == "admin2"

    def test_self_approval_blocked_first(self):
        submit_change("dc-1", "editor1", "risk_tolerance_profile",
                       "fin-r3", "test", {})
        with pytest.raises(ValueError, match="Cannot approve own request"):
            first_approve("dc-1", "editor1")

    def test_self_approval_blocked_second(self):
        submit_change("dc-1", "editor1", "risk_tolerance_profile",
                       "fin-r3", "test", {})
        first_approve("dc-1", "admin1")
        with pytest.raises(ValueError, match="Cannot approve own request"):
            second_approve("dc-1", "editor1")

    def test_same_approver_blocked(self):
        submit_change("dc-1", "editor1", "risk_tolerance_profile",
                       "fin-r3", "test", {})
        first_approve("dc-1", "admin1")
        with pytest.raises(ValueError, match="Cannot be both first and second"):
            second_approve("dc-1", "admin1")

    def test_reject_at_any_stage(self):
        submit_change("dc-1", "editor1", "risk_tolerance_profile",
                       "fin-r3", "test", {})
        req = reject_change("dc-1", "admin1")
        assert req.status == "rejected"

    def test_list_pending(self):
        submit_change("dc-1", "editor1", "rtp", "fin-r3", "test1", {})
        submit_change("dc-2", "editor1", "rtp", "fin-r3", "test2", {})
        first_approve("dc-1", "admin1")
        pending = list_pending()
        assert len(pending) == 2  # both still pending (one at stage 2)

    def test_nonexistent_request(self):
        assert first_approve("nope", "admin1") is None
        assert second_approve("nope", "admin1") is None

    def test_to_dict(self):
        req = submit_change("dc-1", "editor1", "rtp", "fin-r3", "test", {})
        d = req.to_dict()
        assert "request_id" in d
        assert "status" in d
        assert d["submitted_by"] == "editor1"
