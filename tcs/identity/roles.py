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
        "/v2/",
    }),
    blocked=frozenset(),  # no blocks except self-approval (enforced in dual_control)
)

GOVERNANCE_ADMIN = Role(
    name="governance_admin",
    description="Governance operations: govern, certificates, metrics, dynamics, simulation, packs.",
    permitted=frozenset({
        "/v2/govern",
        "/v2/certificates",
        "/v2/metrics",
        "/v2/dynamics",
        "/v2/simulation",
        "/v2/health",
    }),
    blocked=frozenset({
        "/v2/admin/users",
        "/v2/admin/modules",
    }),
)

POLICY_EDITOR = Role(
    name="policy_editor",
    description="Read governance data, run simulations, view PLL recommendations.",
    permitted=frozenset({
        "/v2/govern",
        "/v2/simulation",
        "/v2/dynamics/pll/recommendations",
        "/v2/dynamics/pll/history",
        "/v2/dynamics/trust-loss",
        "/v2/dynamics/drift",
        "/v2/certificates",
        "/v2/metrics",
        "/v2/health",
    }),
    blocked=frozenset({
        "/v2/dynamics/pll/approve",
        "/v2/dynamics/pll/reject",
        "/v2/dynamics/recovery/activate",
    }),
)

COMPLIANCE_OFFICER = Role(
    name="compliance_officer",
    description="Audit access: certificates, metrics, drift, hold queue.",
    permitted=frozenset({
        "/v2/certificates",
        "/v2/metrics",
        "/v2/dynamics/drift",
        "/v2/dynamics/trust-loss",
        "/v2/govern",
        "/v2/health",
    }),
    blocked=frozenset({
        "/v2/dynamics/pll/approve",
        "/v2/simulation/replay",
    }),
)

WORKFLOW_OWNER = Role(
    name="workflow_owner",
    description="Own-workflow governance: hold queue and certificates for own workflow.",
    permitted=frozenset({
        "/v2/govern",
        "/v2/certificates",
        "/v2/health",
        "/v2/metrics",
    }),
    blocked=frozenset({
        "/v2/dynamics",
        "/v2/simulation",
    }),
)

AUDITOR = Role(
    name="auditor",
    description="Read-only certificate and chain verification access.",
    permitted=frozenset({
        "/v2/certificates",
        "/v2/health",
    }),
    blocked=frozenset({
        "/v2/dynamics",
        "/v2/simulation",
        "/v2/govern",
        "/v2/metrics",
    }),
)

EXECUTIVE_VIEWER = Role(
    name="executive_viewer",
    description="Dashboard-only: live metrics and summary.",
    permitted=frozenset({
        "/v2/metrics",
        "/v2/health",
    }),
    blocked=frozenset({
        "/v2/certificates",
        "/v2/dynamics",
        "/v2/simulation",
        "/v2/govern",
    }),
)

EXCEPTION_APPROVER = Role(
    name="exception_approver",
    description="Approve overrides and PLL changes. Cannot approve own requests.",
    permitted=frozenset({
        "/v2/govern",
        "/v2/dynamics/pll/approve",
        "/v2/dynamics/pll/reject",
        "/v2/dynamics/pll/recommendations",
        "/v2/dynamics/pll/history",
        "/v2/certificates",
        "/v2/metrics",
        "/v2/health",
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
