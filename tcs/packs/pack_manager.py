"""
tcs.packs.pack_manager
=======================

Pack selection, validation, deployment, and audit export.

Manages the lifecycle of regulatory packs: listing available packs,
deploying a pack as the active configuration, validating that a
profile stays within pack-defined floors, and generating audit
exports in the pack's required format.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from tcs.packs.financial_services import PACK as FINANCIAL_PACK
from tcs.packs.healthcare_clinical import PACK as HEALTHCARE_PACK
from tcs.packs.gmp_quality import PACK as GMP_PACK
from tcs.packs.enterprise_ops import PACK as ENTERPRISE_PACK
from tcs.packs.federal_public import PACK as FEDERAL_PACK
from tcs.policy_profiles import PolicyProfile
from tcs.persistence import CertificateStore


# --------------------------------------------------------------------------- #
# Pack registry                                                                #
# --------------------------------------------------------------------------- #

PACKS: Dict[str, Dict[str, Any]] = {
    p["pack_id"]: p for p in [
        FINANCIAL_PACK,
        HEALTHCARE_PACK,
        GMP_PACK,
        ENTERPRISE_PACK,
        FEDERAL_PACK,
    ]
}

#: Currently active pack (in-memory for reference implementation).
_active_pack: Optional[str] = None


# --------------------------------------------------------------------------- #
# Pack operations                                                              #
# --------------------------------------------------------------------------- #

def list_packs() -> List[Dict[str, Any]]:
    """Return summary info for all available packs."""
    return [
        {
            "pack_id": p["pack_id"],
            "name": p["name"],
            "version": p["version"],
            "description": p["description"],
            "regulatory_references": p["regulatory_references"],
            "fail_behavior": p["fail_behavior"],
        }
        for p in PACKS.values()
    ]


def get_pack(pack_id: str) -> Optional[Dict[str, Any]]:
    """Return full pack configuration, or None if not found."""
    return PACKS.get(pack_id)


def deploy_pack(pack_id: str) -> Dict[str, Any]:
    """
    Deploy a pack as the active configuration.

    Returns the pack details with deployment timestamp.
    Raises KeyError if pack_id is not found.
    """
    global _active_pack
    pack = PACKS.get(pack_id)
    if pack is None:
        raise KeyError(f"Pack '{pack_id}' not found")

    _active_pack = pack_id
    return {
        "pack_id": pack_id,
        "status": "deployed",
        "deployed_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "profile_id": pack["profile_config"]["profile_id"],
    }


def get_active_pack() -> Optional[Dict[str, Any]]:
    """Return the currently active pack, or None."""
    if _active_pack is None:
        return None
    return PACKS.get(_active_pack)


def get_active_pack_id() -> Optional[str]:
    """Return the active pack ID."""
    return _active_pack


def clear_active_pack() -> None:
    """Clear the active pack (for testing)."""
    global _active_pack
    _active_pack = None


# --------------------------------------------------------------------------- #
# Pack validation                                                              #
# --------------------------------------------------------------------------- #

def validate_against_pack(
    pack_id: str,
    decision_thresholds: Dict[str, float],
) -> Dict[str, Any]:
    """
    Validate that proposed thresholds respect the pack's adaptation floors.

    Returns a dict with 'valid' (bool) and any 'violations' found.
    """
    pack = PACKS.get(pack_id)
    if pack is None:
        return {"valid": False, "violations": [f"Pack '{pack_id}' not found"]}

    floors = pack.get("adaptation_floors", {})
    violations = []

    for param, floor in floors.items():
        proposed = decision_thresholds.get(param)
        if proposed is not None and proposed < floor:
            violations.append(
                f"{param} = {proposed:.4f} below floor {floor:.4f}"
            )

    return {
        "valid": len(violations) == 0,
        "violations": violations,
    }


def build_profile_from_pack(pack_id: str) -> PolicyProfile:
    """
    Build a PolicyProfile from the pack's profile_config.

    Raises KeyError if pack not found.
    """
    pack = PACKS.get(pack_id)
    if pack is None:
        raise KeyError(f"Pack '{pack_id}' not found")
    return PolicyProfile.from_dict(pack["profile_config"])


# --------------------------------------------------------------------------- #
# Audit export                                                                 #
# --------------------------------------------------------------------------- #

def generate_audit_export(
    store: CertificateStore,
    pack_id: str,
    *,
    window_hours: float = 720.0,  # 30 days
) -> Dict[str, Any]:
    """
    Generate an audit export in the pack's required format.

    Returns a structured dict containing TC summary, decision
    distribution, and compliance-relevant metadata.
    """
    pack = PACKS.get(pack_id)
    if pack is None:
        raise KeyError(f"Pack '{pack_id}' not found")

    tc_rows = store.query_window(window_hours)
    decisions = {}
    for row in tc_rows:
        d = str(row["decision"])
        decisions[d] = decisions.get(d, 0) + 1

    total = len(tc_rows)
    allow_count = decisions.get("Allow", 0) + decisions.get("Observe", 0)
    automation_rate = allow_count / total if total > 0 else 0.0

    return {
        "export_format": pack["audit_export_format"],
        "pack_id": pack_id,
        "pack_name": pack["name"],
        "pack_version": pack["version"],
        "regulatory_references": pack["regulatory_references"],
        "export_timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "window_hours": window_hours,
        "summary": {
            "total_evaluations": total,
            "decision_distribution": decisions,
            "automation_rate": round(automation_rate, 4),
            "tc_required_fields": pack.get("tc_required_fields", []),
            "fail_behavior": pack["fail_behavior"],
        },
        "compliance_status": {
            "pack_deployed": _active_pack == pack_id,
            "adaptation_floors": pack.get("adaptation_floors", {}),
        },
    }
