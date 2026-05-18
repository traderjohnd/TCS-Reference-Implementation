"""
tcs.standards.composer
=======================

Compose a base policy profile with a set of selected standards using
hybrid / strictest-control rules.

Composition rules (LOCKED — see tcs/standards/__init__.py docstring):

    thresholds         : take the strictest (max) applicable value
    gate_set           : union of required gates
    required_controls  : OR logic (union)
    hard_prohibitions  : union
    penalty_weights    : additive with caps, then re-normalized
    dimension_weights  : additive deltas, then re-normalized

The composer returns a ``ComposedProfile`` that includes:

    - The composed ``profile_config`` (PolicyProfile shape)
    - Per-standard ``contributions`` showing which standard
      contributed which adjustment (for the UI's "View adjustments"
      panel)
    - A deterministic ``profile_hash`` for the pack metadata
    - ``composer_metadata`` recording the inputs (industry,
      sub_industry, use_case, standards, risk_tier, action_class,
      composition_rules) for full audit reconstruction
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from tcs.policy_profiles import (
    DIMENSIONS, PENALTY_COMPONENTS, CANONICAL_DEFAULTS, load_profile,
)
from tcs.standards.library import STANDARDS


COMPOSITION_RULES_VERSION = "hybrid-strictest-control-v1"


@dataclass
class StandardContribution:
    """
    What a single standard contributed to the composed profile.

    Surfaced in the UI's "View adjustments" panel so users can see
    which standard caused which threshold elevation, gate addition,
    weight shift, or penalty change.
    """
    standard_id: str
    standard_name: str
    threshold_floors_applied: Dict[str, float] = field(default_factory=dict)
    threshold_floors_overridden: Dict[str, float] = field(default_factory=dict)  # ignored as not strictest
    gate_dimensions_added: List[str] = field(default_factory=list)
    weight_deltas_applied: Dict[str, float] = field(default_factory=dict)
    penalty_weight_deltas_applied: Dict[str, float] = field(default_factory=dict)
    required_controls_added: List[str] = field(default_factory=list)
    hard_prohibitions_added: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "standard_id": self.standard_id,
            "standard_name": self.standard_name,
            "threshold_floors_applied": dict(self.threshold_floors_applied),
            "threshold_floors_overridden": dict(self.threshold_floors_overridden),
            "gate_dimensions_added": list(self.gate_dimensions_added),
            "weight_deltas_applied": dict(self.weight_deltas_applied),
            "penalty_weight_deltas_applied": dict(self.penalty_weight_deltas_applied),
            "required_controls_added": list(self.required_controls_added),
            "hard_prohibitions_added": list(self.hard_prohibitions_added),
        }


@dataclass
class ComposedProfile:
    """
    Output of compose_profile().

    Carries the composed profile_config, per-standard contributions,
    aggregate audit fields, and the composer_metadata needed to
    persist a pack entry that reconstructs the composition fully.
    """
    profile_config: Dict[str, Any]
    contributions: List[StandardContribution]
    profile_hash: str
    composer_metadata: Dict[str, Any]
    required_controls: List[str]
    hard_prohibitions: List[str]
    regulatory_references: List[str]

    def to_dict(self) -> dict:
        return {
            "profile_config": self.profile_config,
            "contributions": [c.to_dict() for c in self.contributions],
            "profile_hash": self.profile_hash,
            "composer_metadata": self.composer_metadata,
            "required_controls": list(self.required_controls),
            "hard_prohibitions": list(self.hard_prohibitions),
            "regulatory_references": list(self.regulatory_references),
        }


# --------------------------------------------------------------------------- #
# Helpers                                                                      #
# --------------------------------------------------------------------------- #

def _base_profile_config(risk_tier: str, action_class: str) -> Dict[str, Any]:
    """
    Build a starting profile_config from canonical defaults for (r, a).

    Slice 4 uses the canonical defaults rather than picking an existing
    domain profile, so the standards composition is the SOLE source of
    domain-specific tightening. This keeps the composer's contribution
    visible: the user sees exactly what each standard added.
    """
    defaults = CANONICAL_DEFAULTS

    thresholds_dict = defaults["thresholds"][risk_tier]
    # None entries (K at r1/r2) become 0.0 baseline; standards can elevate.
    thresholds = {k: (v if v is not None else 0.0) for k, v in thresholds_dict.items()}

    gate_set = sorted(defaults["gate_sets"][(risk_tier, action_class)])

    return {
        "profile_id": f"composed-base-{risk_tier}-{action_class}",
        "domain": "composed",
        "risk_tier": risk_tier,
        "action_class": action_class,
        "weights": dict(defaults["weights"]),
        "penalty_weights": dict(defaults["penalty_weights"]),
        "thresholds": thresholds,
        "gate_set": gate_set,
        "decay_rate": defaults["decay_rates"][risk_tier],
        "soft_hold_ceiling": defaults["soft_hold_ceilings"][risk_tier],
        "decision_thresholds": dict(defaults["decision_thresholds"][risk_tier]),
        "invalidation_triggers": [
            "model_version_change", "policy_update",
            "data_distribution_drift", "environmental_change",
            "context_expansion",
        ],
        "regulatory_mapping": [],  # filled in by composer from standards
    }


def _renormalize(d: Dict[str, float]) -> Dict[str, float]:
    """Scale values so the sum is exactly 1.0; round to 4 decimal places."""
    total = sum(d.values())
    if total <= 0:
        # Fall back to equal weights for the keys.
        n = max(len(d), 1)
        return {k: round(1.0 / n, 4) for k in d}
    scaled = {k: v / total for k, v in d.items()}
    # Round and fix any residual from rounding by giving the slack to the
    # largest entry, so the final sum is exactly 1.0.
    rounded = {k: round(v, 4) for k, v in scaled.items()}
    residual = round(1.0 - sum(rounded.values()), 4)
    if abs(residual) > 0:
        max_key = max(rounded, key=rounded.get)
        rounded[max_key] = round(rounded[max_key] + residual, 4)
    return rounded


def _compute_profile_hash(profile_config: Dict[str, Any], composer_metadata: Dict[str, Any]) -> str:
    """
    Deterministic SHA-256 over the composed profile + composer inputs.

    Used as the pack's content fingerprint for audit / reproducibility.
    Same inputs always produce the same hash — the pack_id is what the
    deploy/lookup contract is built on, so it must NOT depend on the
    wall-clock moment of composition.

    ``composed_at`` is excluded from the hash payload for that reason:
    it carries audit value (when was this composition first produced)
    but is not part of the composition's identity. Two deploys with
    the same industry / sub_industry / use_case / standards /
    risk_tier / action_class / composition_rules_version produce the
    same pack_id regardless of when they happen.
    """
    composer_md_for_hash = {
        k: v for k, v in composer_metadata.items() if k != "composed_at"
    }
    payload = {
        "profile_config": profile_config,
        "composer_metadata": composer_md_for_hash,
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def composed_pack_id(profile_hash: str) -> str:
    """Stable, hash-rooted pack id for a composed profile."""
    return f"composed-{profile_hash[:16]}"


# --------------------------------------------------------------------------- #
# compose_profile                                                              #
# --------------------------------------------------------------------------- #

def compose_profile(
    *,
    industry: str,
    sub_industry: str,
    use_case: str,
    standard_ids: List[str],
    risk_tier: str,
    action_class: str,
) -> ComposedProfile:
    """
    Build a composed profile from base + selected standards.

    Parameters validated; unknown standard ids raise ValueError.

    Raises
    ------
    ValueError
        If any standard id is unknown, if risk_tier/action_class are
        invalid, or if the composed profile fails its invariants
        (Σ weights != 1.0, Σ penalty_weights != 1.0, threshold out of
        [0, 1], etc.).
    """
    # Validate inputs.
    if risk_tier not in ("r1", "r2", "r3"):
        raise ValueError(f"invalid risk_tier: {risk_tier}")
    if action_class not in ("a1", "a2", "a3", "a4"):
        raise ValueError(f"invalid action_class: {action_class}")

    unknown = [sid for sid in standard_ids if sid not in STANDARDS]
    if unknown:
        raise ValueError(f"unknown standard ids: {unknown}")

    # Deterministic ordering so the hash is reproducible.
    standard_ids = sorted(standard_ids)

    # Start from canonical defaults so the composer's contribution is
    # the only domain-specific tightening.
    profile = _base_profile_config(risk_tier, action_class)

    contributions: List[StandardContribution] = []
    required_controls: List[str] = []
    hard_prohibitions: List[str] = []
    regulatory_references: List[str] = []

    for sid in standard_ids:
        std = STANDARDS[sid]
        adj = std.get("profile_adjustments", {}) or {}
        contrib = StandardContribution(
            standard_id=sid,
            standard_name=std["name"],
        )

        # --- Thresholds: strictest (max) wins ----------------------------- #
        for dim, floor in (adj.get("threshold_floors") or {}).items():
            if dim not in DIMENSIONS:
                continue
            current = float(profile["thresholds"].get(dim, 0.0))
            floor_f = float(floor)
            if floor_f > current:
                profile["thresholds"][dim] = round(floor_f, 4)
                contrib.threshold_floors_applied[dim] = round(floor_f, 4)
            else:
                # Standard's floor is not the strictest — record it as
                # overridden so the UI can show which standards were
                # not the binding constraint.
                contrib.threshold_floors_overridden[dim] = round(floor_f, 4)

        # --- Gate set: union --------------------------------------------- #
        gate_set = set(profile.get("gate_set", []))
        for dim in (adj.get("gate_set_required") or []):
            if dim not in DIMENSIONS:
                continue
            if dim not in gate_set:
                gate_set.add(dim)
                contrib.gate_dimensions_added.append(dim)
        profile["gate_set"] = sorted(gate_set)

        # --- Dimension weights: additive deltas (re-normalized later) ---- #
        for dim, delta in (adj.get("weight_deltas") or {}).items():
            if dim not in DIMENSIONS:
                continue
            profile["weights"][dim] = profile["weights"].get(dim, 0.0) + float(delta)
            contrib.weight_deltas_applied[dim] = round(float(delta), 4)

        # --- Penalty weights: additive with caps (re-normalized later) --- #
        for comp, delta in (adj.get("penalty_weight_deltas") or {}).items():
            if comp not in PENALTY_COMPONENTS:
                continue
            profile["penalty_weights"][comp] = (
                profile["penalty_weights"].get(comp, 0.0) + float(delta)
            )
            contrib.penalty_weight_deltas_applied[comp] = round(float(delta), 4)

        # --- Required controls: OR (union) ------------------------------- #
        for ctrl in (adj.get("required_controls") or []):
            if ctrl not in required_controls:
                required_controls.append(ctrl)
                contrib.required_controls_added.append(ctrl)

        # --- Hard prohibitions: union ----------------------------------- #
        for prh in (adj.get("hard_prohibitions") or []):
            if prh not in hard_prohibitions:
                hard_prohibitions.append(prh)
                contrib.hard_prohibitions_added.append(prh)

        # --- Regulatory references (union) ------------------------------ #
        ref = std.get("regulatory_reference")
        if ref and ref not in regulatory_references:
            regulatory_references.append(ref)

        contributions.append(contrib)

    # Clamp any per-dimension weight that went negative from accumulated
    # negative deltas. The composer never lets a dimension drop to 0
    # because that would collapse it entirely from the score.
    for dim in DIMENSIONS:
        if profile["weights"][dim] < 0.05:
            profile["weights"][dim] = 0.05

    # Re-normalize weights and penalty weights.
    profile["weights"] = _renormalize(profile["weights"])
    profile["penalty_weights"] = _renormalize(profile["penalty_weights"])

    # Clamp thresholds into [0, 0.99].
    for dim in profile["thresholds"]:
        profile["thresholds"][dim] = max(0.0, min(0.99, float(profile["thresholds"][dim])))

    # Regulatory mapping records the standards on the composed profile.
    profile["regulatory_mapping"] = list(regulatory_references)

    # Composer-aware profile id and domain string.
    composer_metadata = {
        "industry": industry,
        "sub_industry": sub_industry,
        "use_case": use_case,
        "standards": list(standard_ids),
        "risk_tier": risk_tier,
        "action_class": action_class,
        "composition_rules_version": COMPOSITION_RULES_VERSION,
        "composed_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }

    profile_hash = _compute_profile_hash(profile, composer_metadata)
    profile["profile_id"] = composed_pack_id(profile_hash)
    profile["domain"] = f"composed:{industry}:{sub_industry}"

    # Final invariant check.
    w_sum = sum(profile["weights"].values())
    p_sum = sum(profile["penalty_weights"].values())
    if abs(w_sum - 1.0) > 1e-3:
        raise ValueError(f"composed weights sum to {w_sum}, not 1.0: {profile['weights']}")
    if abs(p_sum - 1.0) > 1e-3:
        raise ValueError(f"composed penalty_weights sum to {p_sum}, not 1.0: {profile['penalty_weights']}")

    return ComposedProfile(
        profile_config=profile,
        contributions=contributions,
        profile_hash=profile_hash,
        composer_metadata=composer_metadata,
        required_controls=required_controls,
        hard_prohibitions=hard_prohibitions,
        regulatory_references=regulatory_references,
    )
