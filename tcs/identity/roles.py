"""
tcs.identity.roles
===================

Eight role definitions with permission sets for TCS RBAC.

Each role has a set of permitted endpoint patterns (path prefixes or
exact paths) and a set of blocked patterns. Blocked patterns take
precedence over permitted patterns.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, FrozenSet, List, Set


@dataclass(frozen=True)
class Role:
    """A TCS governance role with endpoint permissions."""
    name: str
    description: str
    permitted: FrozenSet[str]   # endpoint patterns (prefix match)
    blocked: FrozenSet[str]     # blocked patterns (prefix match, override permitted)

    def can_access(self, method: str, path: str) -> bool:
        """
        Check if this role can access the given endpoint.

        Blocked patterns take precedence over permitted patterns.
        Both use prefix matching against the normalized path.
        """
        # Check blocked first (takes precedence)
        for pattern in self.blocked:
            if path.startswith(pattern):
                return False

        # Check permitted
        for pattern in self.permitted:
            if path.startswith(pattern):
                return True

        return False


# --------------------------------------------------------------------------- #
# Role definitions                                                             #
# --------------------------------------------------------------------------- #

PLATFORM_ADMIN = Role(
    name="platform_admin",
    description="Full system access. Cannot approve own dual-control requests.",
    permitted=frozenset({
        "/v1/",
    }),
    blocked=frozenset(),  # no blocks except self-approval (enforced in dual_control)
)

GOVERNANCE_ADMIN = Role(
    name="governance_admin",
    description="Governance operations: govern, certificates, metrics, dynamics, simulation, packs.",
    permitted=frozenset({
        "/v1/govern",
        "/v1/certificates",
        "/v1/metrics",
        "/v1/dynamics",
        "/v1/simulation",
        "/v1/health",
    }),
    blocked=frozenset({
        "/v1/admin/users",
        "/v1/admin/modules",
    }),
)

POLICY_EDITOR = Role(
    name="policy_editor",
    description="Read governance data, run simulations, view PLL recommendations.",
    permitted=frozenset({
        "/v1/govern",
        "/v1/simulation",
        "/v1/dynamics/pll/recommendations",
        "/v1/dynamics/pll/history",
        "/v1/dynamics/trust-loss",
        "/v1/dynamics/drift",
        "/v1/certificates",
        "/v1/metrics",
        "/v1/health",
    }),
    blocked=frozenset({
        "/v1/dynamics/pll/approve",
        "/v1/dynamics/pll/reject",
        "/v1/dynamics/recovery/activate",
    }),
)

COMPLIANCE_OFFICER = Role(
    name="compliance_officer",
    description="Audit access: certificates, metrics, drift, hold queue.",
    permitted=frozenset({
        "/v1/certificates",
        "/v1/metrics",
        "/v1/dynamics/drift",
        "/v1/dynamics/trust-loss",
        "/v1/govern",
        "/v1/health",
    }),
    blocked=frozenset({
        "/v1/dynamics/pll/approve",
        "/v1/simulation/replay",
    }),
)

WORKFLOW_OWNER = Role(
    name="workflow_owner",
    description="Own-workflow governance: hold queue and certificates for own workflow.",
    permitted=frozenset({
        "/v1/govern",
        "/v1/certificates",
        "/v1/health",
        "/v1/metrics",
    }),
    blocked=frozenset({
        "/v1/dynamics",
        "/v1/simulation",
    }),
)

AUDITOR = Role(
    name="auditor",
    description="Read-only certificate and chain verification access.",
    permitted=frozenset({
        "/v1/certificates",
        "/v1/health",
    }),
    blocked=frozenset({
        "/v1/dynamics",
        "/v1/simulation",
        "/v1/govern",
        "/v1/metrics",
    }),
)

EXECUTIVE_VIEWER = Role(
    name="executive_viewer",
    description="Dashboard-only: live metrics and summary.",
    permitted=frozenset({
        "/v1/metrics",
        "/v1/health",
    }),
    blocked=frozenset({
        "/v1/certificates",
        "/v1/dynamics",
        "/v1/simulation",
        "/v1/govern",
    }),
)

EXCEPTION_APPROVER = Role(
    name="exception_approver",
    description="Approve overrides and PLL changes. Cannot approve own requests.",
    permitted=frozenset({
        "/v1/govern",
        "/v1/dynamics/pll/approve",
        "/v1/dynamics/pll/reject",
        "/v1/dynamics/pll/recommendations",
        "/v1/dynamics/pll/history",
        "/v1/certificates",
        "/v1/metrics",
        "/v1/health",
    }),
    blocked=frozenset(),
)

# --------------------------------------------------------------------------- #
# Role registry                                                                #
# --------------------------------------------------------------------------- #

ROLES: Dict[str, Role] = {
    r.name: r for r in [
        PLATFORM_ADMIN,
        GOVERNANCE_ADMIN,
        POLICY_EDITOR,
        COMPLIANCE_OFFICER,
        WORKFLOW_OWNER,
        AUDITOR,
        EXECUTIVE_VIEWER,
        EXCEPTION_APPROVER,
    ]
}


def get_role(name: str) -> Role:
    """Look up a role by name. Raises KeyError if not found."""
    return ROLES[name]
