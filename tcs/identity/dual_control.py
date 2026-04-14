"""
tcs.identity.dual_control
==========================

Two-approver workflow for high-risk (r3) parameter changes.

Every request to change r3 Risk Tolerance Profile parameters must:
    1. Be submitted by a Policy Editor (creates pending change record)
    2. Be approved by a Governance Admin (first approver)
    3. Be countersigned by a second Governance Admin or Compliance Officer
    4. Record both approver identities, timestamps, and justification
    5. Apply only after both approvals are present
    6. Cannot be approved by the submitter
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional


@dataclass
class DualControlRequest:
    """A change request requiring two approvals."""
    request_id: str
    submitted_by: str
    resource_type: str          # e.g. "risk_tolerance_profile"
    resource_id: str            # e.g. profile_id
    change_description: str
    change_data: Dict[str, Any]
    status: str = "pending_first_approval"
    first_approver: Optional[str] = None
    first_approval_at: Optional[str] = None
    first_justification: Optional[str] = None
    second_approver: Optional[str] = None
    second_approval_at: Optional[str] = None
    second_justification: Optional[str] = None
    created_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "request_id": self.request_id,
            "submitted_by": self.submitted_by,
            "resource_type": self.resource_type,
            "resource_id": self.resource_id,
            "change_description": self.change_description,
            "status": self.status,
            "first_approver": self.first_approver,
            "first_approval_at": self.first_approval_at,
            "second_approver": self.second_approver,
            "second_approval_at": self.second_approval_at,
            "created_at": self.created_at,
        }


# --------------------------------------------------------------------------- #
# In-memory dual control store (reference implementation)                      #
# --------------------------------------------------------------------------- #

_requests: Dict[str, DualControlRequest] = {}


def submit_change(
    request_id: str,
    submitted_by: str,
    resource_type: str,
    resource_id: str,
    change_description: str,
    change_data: Dict[str, Any],
) -> DualControlRequest:
    """Submit a change request requiring dual-control approval."""
    req = DualControlRequest(
        request_id=request_id,
        submitted_by=submitted_by,
        resource_type=resource_type,
        resource_id=resource_id,
        change_description=change_description,
        change_data=change_data,
    )
    _requests[request_id] = req
    return req


def first_approve(
    request_id: str,
    approver: str,
    justification: str = "",
) -> Optional[DualControlRequest]:
    """
    First approval of a dual-control request.

    Returns None if request not found. Raises ValueError if the
    approver is the submitter (cannot self-approve).
    """
    req = _requests.get(request_id)
    if req is None:
        return None
    if req.status != "pending_first_approval":
        return req
    if approver == req.submitted_by:
        raise ValueError("Cannot approve own request (dual-control violation)")

    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    req.first_approver = approver
    req.first_approval_at = now
    req.first_justification = justification
    req.status = "pending_second_approval"
    return req


def second_approve(
    request_id: str,
    approver: str,
    justification: str = "",
) -> Optional[DualControlRequest]:
    """
    Second (counter-sign) approval of a dual-control request.

    Returns None if request not found. Raises ValueError if the
    approver is the submitter or the first approver.
    """
    req = _requests.get(request_id)
    if req is None:
        return None
    if req.status != "pending_second_approval":
        return req
    if approver == req.submitted_by:
        raise ValueError("Cannot approve own request (dual-control violation)")
    if approver == req.first_approver:
        raise ValueError("Cannot be both first and second approver")

    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    req.second_approver = approver
    req.second_approval_at = now
    req.second_justification = justification
    req.status = "approved"
    return req


def reject_change(
    request_id: str,
    rejector: str,
) -> Optional[DualControlRequest]:
    """Reject a pending dual-control request at any stage."""
    req = _requests.get(request_id)
    if req is None:
        return None
    if req.status in ("approved", "rejected"):
        return req
    req.status = "rejected"
    return req


def get_request(request_id: str) -> Optional[DualControlRequest]:
    """Look up a dual-control request."""
    return _requests.get(request_id)


def list_pending() -> List[DualControlRequest]:
    """List all pending dual-control requests."""
    return [
        r for r in _requests.values()
        if r.status.startswith("pending")
    ]


def clear_requests() -> None:
    """Clear all requests (for testing)."""
    _requests.clear()
