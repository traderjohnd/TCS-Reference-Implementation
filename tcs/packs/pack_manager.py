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
# Composed-pack registration (Slice 4 — standards composer integration)        #
# --------------------------------------------------------------------------- #

def register_composed_pack(composed: Any, *, name: Optional[str] = None) -> Dict[str, Any]:
    """
    Register a ComposedProfile as a deployable pack.

    The composed pack:
      - Uses ``composed-<hash16>`` as its pack_id (deterministic
        across same inputs — same composition always lands on the
        same id regardless of display name)
      - Carries the composer_metadata so the deployment can be
        reconstructed for audit (industry, sub_industry, use_case,
        standards, risk_tier, action_class, composition_rules_version,
        composed_at, profile_hash)
      - Honors the existing Pack contract (pack_id, name, version,
        description, regulatory_references, profile_config,
        fail_behavior, etc.) so the existing list_packs / deploy_pack
        flow works unchanged.

    Calling register_composed_pack with the same inputs twice yields
    the same pack_id (the hash is deterministic) and updates the
    in-memory entry idempotently. The display ``name`` MAY differ
    between calls (last-write-wins).

    Parameters
    ----------
    composed
        A ``tcs.standards.composer.ComposedProfile`` instance.
    name
        Optional human-readable display name. Used in the Available
        Packs list and Active Profile section. Defaults to an
        auto-generated descriptive label when None.

    Returns
    -------
    dict
        The registered pack dict.
    """
    from tcs.standards.composer import ComposedProfile, composed_pack_id

    if not isinstance(composed, ComposedProfile):
        raise TypeError(
            "register_composed_pack expects a ComposedProfile instance"
        )

    pack_id = composed_pack_id(composed.profile_hash)
    meta = composed.composer_metadata
    standards_list = ", ".join(meta.get("standards", []))

    if name and str(name).strip():
        display_name = str(name).strip()
    else:
        display_name = (
            f"Composed: {meta.get('industry')} · {meta.get('sub_industry')} · "
            f"{meta.get('use_case')} ({standards_list or 'no standards'})"
        )

    pack: Dict[str, Any] = {
        "pack_id": pack_id,
        "name": display_name,
        "version": "1.0.0",
        "description": (
            f"Standards-composed pack. Industry={meta.get('industry')}, "
            f"sub_industry={meta.get('sub_industry')}, use_case={meta.get('use_case')}, "
            f"risk_tier={meta.get('risk_tier')}, action_class={meta.get('action_class')}, "
            f"composition_rules={meta.get('composition_rules_version')}. "
            "Adjustments per standard are governance interpretations, not "
            "claims that the underlying regulations mathematically require "
            "the specific TCS parameter values."
        ),
        "regulatory_references": list(composed.regulatory_references),
        "profile_config": dict(composed.profile_config),
        "adaptation_floors": {},
        "tc_required_fields": [],
        "audit_export_format": "standards_composed_v1",
        "fail_behavior": "fail_closed",
        # Composer metadata — required for full audit reconstruction.
        "composer_metadata": dict(meta),
        "composer_contributions": [c.to_dict() for c in composed.contributions],
        "required_controls": list(composed.required_controls),
        "hard_prohibitions": list(composed.hard_prohibitions),
        "profile_hash": composed.profile_hash,
        "is_composed_pack": True,
    }
    PACKS[pack_id] = pack
    return pack


def unregister_composed_pack(pack_id: str) -> bool:
    """
    Remove a composed pack from the registry. Returns True if removed.

    Safety: refuses to remove non-composed (built-in) packs.
    """
    global _active_pack
    pack = PACKS.get(pack_id)
    if pack is None:
        return False
    if not pack.get("is_composed_pack"):
        raise ValueError(f"refusing to unregister built-in pack {pack_id!r}")
    if _active_pack == pack_id:
        _active_pack = None
    del PACKS[pack_id]
    return True


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
