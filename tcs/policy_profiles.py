"""
tcs.policy_profiles
===================

Load, validate, and provide access to domain policy configurations.

No computation happens here — only configuration management. Every numeric
value in this file is traceable to POLICY_PROFILES.md (domain overrides) or
TCS_SPEC.md (canonical defaults). Do not invent numbers.

Validation rules (enforced on every PolicyProfile instantiation):
    - Σ weights              == 1.0   (dimension weights sum to 1)
    - Σ penalty_weights      == 1.0   (penalty component weights sum to 1)
    - weights keys           == {B, A, C, K}
    - penalty_weights keys   == {cb, d, n, h, ps}
    - every gate dimension has a threshold
    - decay_rate             >  0
    - 0 < soft_hold_ceiling <= 1.0

See ARCHITECTURE.md §"Module: tcs/policy_profiles.py" for the class contract.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import FrozenSet, List, Dict


# --------------------------------------------------------------------------- #
# Canonical constants from TCS_SPEC.md                                        #
# --------------------------------------------------------------------------- #

DIMENSIONS: FrozenSet[str] = frozenset({"B", "A", "C", "K"})
PENALTY_COMPONENTS: FrozenSet[str] = frozenset({"cb", "d", "n", "h", "ps"})
RISK_TIERS: FrozenSet[str] = frozenset({"r1", "r2", "r3"})
ACTION_CLASSES: FrozenSet[str] = frozenset({"a1", "a2", "a3", "a4"})

# Canonical defaults (TCS_SPEC.md §7, §8, §10, §12; POLICY_PROFILES.md).
CANONICAL_DEFAULTS: dict = {
    "weights": {"B": 0.25, "A": 0.25, "C": 0.30, "K": 0.20},
    "penalty_weights": {"cb": 0.20, "d": 0.20, "n": 0.20, "h": 0.20, "ps": 0.20},
    "decay_rates": {"r1": 0.008, "r2": 0.014, "r3": 0.019},
    "thresholds": {
        "r1": {"B": 0.70, "A": 0.70, "C": 0.75, "K": None},
        "r2": {"B": 0.75, "A": 0.75, "C": 0.80, "K": None},
        "r3": {"B": 0.80, "A": 0.80, "C": 0.85, "K": 0.80},
    },
    "soft_hold_ceilings": {"r1": 0.85, "r2": 0.88, "r3": 0.90},
    # Decision thresholds — Option A values (J. DeRudder, April 2026).
    #
    # The three thresholds define four zones. theta_hold is the UPPER bound of
    # the score-driven Hold band (not the lower bound — that role belongs to
    # theta_escalate). At r2 and r3, theta_hold == theta_allow intentionally:
    # the score-driven Hold state collapses to zero width and Hold at those
    # tiers comes exclusively from the gate-failure path (G=0, TIS_raw <= κ).
    # This is correct governance: you do not auto-route a regulated decision
    # to a standard review queue on score alone; a sub-allow score at r2/r3
    # means the gates caught something and a specific gap needs remediation.
    #
    # Decision zones:
    #   r1:  Escalate < 0.55 | Hold [0.55,0.65) | Observe [0.65,0.75) | Allow ≥ 0.75
    #   r2:  Escalate < 0.65 | Hold (gate path only) [0.65,0.80)      | Allow ≥ 0.80
    #   r3:  Escalate < 0.70 | Hold (gate path only) [0.70,0.85)      | Allow ≥ 0.85
    #
    # See decision_engine.py for the priority ladder that consumes these
    # values and the semantic distinction between Hold and Escalate.
    "decision_thresholds": {
        "r1": {"theta_allow": 0.75, "theta_hold": 0.65, "theta_escalate": 0.55},
        "r2": {"theta_allow": 0.80, "theta_hold": 0.80, "theta_escalate": 0.65},
        "r3": {"theta_allow": 0.85, "theta_hold": 0.85, "theta_escalate": 0.70},
    },
    "gate_sets": {
        ("r1", "a1"): ["B", "A", "C"], ("r1", "a2"): ["B", "A", "C"],
        ("r1", "a3"): ["B", "A", "C"], ("r1", "a4"): ["B", "A", "C", "K"],
        ("r2", "a1"): ["B", "A", "C"], ("r2", "a2"): ["B", "A", "C"],
        ("r2", "a3"): ["B", "A", "C"], ("r2", "a4"): ["B", "A", "C", "K"],
        ("r3", "a1"): ["B", "A", "C"], ("r3", "a2"): ["B", "A", "C", "K"],
        ("r3", "a3"): ["B", "A", "C", "K"], ("r3", "a4"): ["B", "A", "C", "K"],
    },
}

# Canonical set of invalidation event types.
#
# Original four from TCS_SPEC.md §11. ``context_expansion`` added per
# TCS-MCP-001 §11 C-R.14 (Context Freeze): any MCP retrieval after TIS
# evaluation expands the governed context and must invalidate the TC
# immediately. Imported by ``tcs.tis_engine`` via the existing import;
# no changes to tis_engine.py needed for the engine to honor it.
INVALIDATION_EVENTS: FrozenSet[str] = frozenset({
    "model_version_change",
    "data_distribution_drift",
    "policy_update",
    "environmental_change",
    "context_expansion",
})

_WEIGHT_SUM_TOLERANCE: float = 1e-9


# --------------------------------------------------------------------------- #
# PolicyProfile                                                                #
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class PolicyProfile:
    """
    Immutable domain policy configuration.

    Instantiate via :func:`load_profile` (preferred) or directly from a raw
    dict via :meth:`from_dict`. Validation runs automatically in __post_init__
    and raises :class:`ValueError` on any violation.

    All numeric values come from POLICY_PROFILES.md. Threshold overrides above
    the canonical defaults in TCS_SPEC.md §7 are allowed; the domain floor is
    set by the spec and domains may go stricter, never looser.
    """

    profile_id: str
    domain: str
    risk_tier: str                      # "r1" | "r2" | "r3"
    action_class: str                   # "a1" | "a2" | "a3" | "a4"
    gate_set: FrozenSet[str]            # subset of {B,A,C,K}
    thresholds: Dict[str, float]        # all four dimensions
    weights: Dict[str, float]           # all four; Σ = 1
    penalty_weights: Dict[str, float]   # all five; Σ = 1
    decay_rate: float                   # μᵣ,ₐ per hour
    soft_hold_ceiling: float            # κ
    decision_thresholds: Dict[str, float]  # theta_allow / _hold / _escalate
    invalidation_triggers: List[str] = field(default_factory=list)
    regulatory_mapping: List[str] = field(default_factory=list)
    description: str = ""

    # ---- Construction helpers -------------------------------------------- #

    @classmethod
    def from_dict(cls, raw: dict) -> "PolicyProfile":
        """
        Build a PolicyProfile from the raw dict format used in
        POLICY_PROFILES.md. Unknown keys are ignored; missing required keys
        raise KeyError at dataclass construction time.
        """
        return cls(
            profile_id=raw["profile_id"],
            domain=raw["domain"],
            risk_tier=raw["risk_tier"],
            action_class=raw["action_class"],
            gate_set=frozenset(raw["gate_set"]),
            thresholds=dict(raw["thresholds"]),
            weights=dict(raw["weights"]),
            penalty_weights=dict(raw["penalty_weights"]),
            decay_rate=float(raw["decay_rate"]),
            soft_hold_ceiling=float(raw["soft_hold_ceiling"]),
            decision_thresholds=dict(raw["decision_thresholds"]),
            invalidation_triggers=list(raw.get("invalidation_triggers", [])),
            regulatory_mapping=list(raw.get("regulatory_mapping", [])),
            description=raw.get("description", ""),
        )

    # ---- Validation ------------------------------------------------------ #

    def __post_init__(self) -> None:
        self._validate()

    def _validate(self) -> None:
        # Risk tier / action class enum checks.
        if self.risk_tier not in RISK_TIERS:
            raise ValueError(
                f"profile '{self.profile_id}': risk_tier must be one of "
                f"{sorted(RISK_TIERS)}, got {self.risk_tier!r}"
            )
        if self.action_class not in ACTION_CLASSES:
            raise ValueError(
                f"profile '{self.profile_id}': action_class must be one of "
                f"{sorted(ACTION_CLASSES)}, got {self.action_class!r}"
            )

        # Dimension weights: complete and sum to 1.0.
        if set(self.weights.keys()) != set(DIMENSIONS):
            raise ValueError(
                f"profile '{self.profile_id}': weights must define all four "
                f"dimensions {sorted(DIMENSIONS)}, got {sorted(self.weights.keys())}"
            )
        w_sum = sum(self.weights.values())
        if abs(w_sum - 1.0) > _WEIGHT_SUM_TOLERANCE:
            raise ValueError(
                f"profile '{self.profile_id}': dimension weights must sum to "
                f"1.0, got {w_sum}"
            )

        # Penalty weights: complete and sum to 1.0.
        if set(self.penalty_weights.keys()) != set(PENALTY_COMPONENTS):
            raise ValueError(
                f"profile '{self.profile_id}': penalty_weights must define all "
                f"five components {sorted(PENALTY_COMPONENTS)}, got "
                f"{sorted(self.penalty_weights.keys())}"
            )
        p_sum = sum(self.penalty_weights.values())
        if abs(p_sum - 1.0) > _WEIGHT_SUM_TOLERANCE:
            raise ValueError(
                f"profile '{self.profile_id}': penalty weights must sum to "
                f"1.0, got {p_sum}"
            )

        # Thresholds: every dimension in the gate set must have a threshold.
        # TC_SCHEMA.md also requires thresholds for all four dimensions (even
        # when not gating), so we enforce the stronger rule here.
        if set(self.thresholds.keys()) != set(DIMENSIONS):
            raise ValueError(
                f"profile '{self.profile_id}': thresholds must be defined for "
                f"all four dimensions, got {sorted(self.thresholds.keys())}"
            )
        for dim in self.gate_set:
            if dim not in DIMENSIONS:
                raise ValueError(
                    f"profile '{self.profile_id}': gate_set contains unknown "
                    f"dimension {dim!r}"
                )
            if self.thresholds.get(dim) is None:
                raise ValueError(
                    f"profile '{self.profile_id}': gate dimension {dim!r} "
                    f"has no threshold defined"
                )

        # Numeric bounds.
        if self.decay_rate <= 0:
            raise ValueError(
                f"profile '{self.profile_id}': decay_rate must be positive, "
                f"got {self.decay_rate}"
            )
        if not (0 < self.soft_hold_ceiling <= 1.0):
            raise ValueError(
                f"profile '{self.profile_id}': soft_hold_ceiling (κ) must be "
                f"in (0, 1], got {self.soft_hold_ceiling}"
            )

        # Decision thresholds: all three present.
        required_decision_keys = {"theta_allow", "theta_hold", "theta_escalate"}
        if not required_decision_keys.issubset(self.decision_thresholds.keys()):
            missing = required_decision_keys - set(self.decision_thresholds.keys())
            raise ValueError(
                f"profile '{self.profile_id}': decision_thresholds missing "
                f"{sorted(missing)}"
            )

        # Invalidation triggers must be a subset of the canonical event set.
        for trigger in self.invalidation_triggers:
            if trigger not in INVALIDATION_EVENTS:
                raise ValueError(
                    f"profile '{self.profile_id}': invalidation trigger "
                    f"{trigger!r} not in canonical set "
                    f"{sorted(INVALIDATION_EVENTS)}"
                )

    # ---- Convenience accessors ------------------------------------------- #

    @property
    def theta_allow(self) -> float:
        return self.decision_thresholds["theta_allow"]

    @property
    def theta_hold(self) -> float:
        return self.decision_thresholds["theta_hold"]

    @property
    def theta_escalate(self) -> float:
        return self.decision_thresholds["theta_escalate"]


# --------------------------------------------------------------------------- #
# Hardcoded domain profiles (POLICY_PROFILES.md)                               #
# --------------------------------------------------------------------------- #
#
# For v0.1 we keep profiles in-process — no external file loading. Every value
# below is taken verbatim from POLICY_PROFILES.md. Do not adjust a number here
# without updating POLICY_PROFILES.md first.

_RAW_PROFILES: Dict[str, dict] = {

    # Profile 0: Baseline — no standards pack deployed.
    #
    # Phase 5 amendment (post Slice 5.3). The formal fallback profile
    # the /v2/evaluate resolver uses when:
    #   1. the caller did not pass a policy_profile_id, AND
    #   2. no active pack is currently deployed.
    #
    # The architectural rule the user pinned: "policy_profile_id=null"
    # MUST NOT mean "no policy math". TIS always needs a resolved
    # configuration. baseline-no-pack is that resolved configuration
    # for the "nothing else specified" case — permissive r1/a1
    # canonical defaults, empty regulatory_mapping, and a description
    # that makes its purpose explicit in any audit record.
    #
    # Replay narrative this enables:
    #   Raw artifact
    #     → baseline-no-pack observe   ← formal baseline, not "null"
    #     → MedDev observe
    #     → MedDev enforce
    #     → Financial what-if
    "baseline-no-pack": {
        "profile_id": "baseline-no-pack",
        "domain": "baseline",
        "risk_tier": "r1",
        "action_class": "a1",
        "gate_set": ["B", "A", "C"],
        "thresholds": {"B": 0.70, "A": 0.70, "C": 0.75, "K": 0.60},
        "weights": {"B": 0.25, "A": 0.25, "C": 0.30, "K": 0.20},
        "penalty_weights": {
            "cb": 0.20, "d": 0.20, "n": 0.20, "h": 0.20, "ps": 0.20
        },
        "decay_rate": 0.008,
        "soft_hold_ceiling": 0.85,
        "decision_thresholds": {
            "theta_allow": 0.75, "theta_hold": 0.65, "theta_escalate": 0.55
        },
        "invalidation_triggers": [
            "model_version_change", "policy_update",
            "data_distribution_drift", "environmental_change",
        ],
        "regulatory_mapping": [],
        "description": (
            "Baseline profile used when no standards pack is deployed "
            "and no profile is explicitly specified. TIS still computes "
            "BACK/gates/decision against canonical r1/a1 defaults, so "
            "/v2/evaluate always runs full governance math rather than "
            "short-circuiting on null policy. Replace by deploying a "
            "standards pack (Policy Controls) or by passing an explicit "
            "policy_profile_id."
        ),
    },

    # Profile 1: Financial Services — High Risk Regulated Decision
    "fin-high-risk-suitability-v3": {
        "profile_id": "fin-high-risk-suitability-v3",
        "domain": "financial_services",
        "risk_tier": "r3",
        "action_class": "a4",
        "gate_set": ["B", "A", "C", "K"],
        "thresholds": {"B": 0.90, "A": 0.90, "C": 0.90, "K": 0.80},
        "weights": {"B": 0.30, "A": 0.25, "C": 0.30, "K": 0.15},
        "penalty_weights": {
            "cb": 0.25, "d": 0.10, "n": 0.20, "h": 0.10, "ps": 0.35
        },
        "decay_rate": 0.050,
        "soft_hold_ceiling": 0.90,
        "decision_thresholds": {
            "theta_allow": 0.85, "theta_hold": 0.85, "theta_escalate": 0.70
        },
        "invalidation_triggers": [
            "model_version_change", "policy_update",
            "data_distribution_drift", "environmental_change",
        ],
        "regulatory_mapping": [
            "SEC Regulation Best Interest",
            "SEC Form ADV",
            "FINRA Rule 3110",
            "FINRA Rule 2111",
        ],
        "description": (
            "Financial services AI-assisted investment recommendation or "
            "suitability support. Thresholds represent domain policy override "
            "above canonical defaults (B=0.80, A=0.80, C=0.85)."
        ),
    },

    # Profile 2: Healthcare — High Risk Regulated Decision
    "clinical-cds-samed-v2": {
        "profile_id": "clinical-cds-samed-v2",
        "domain": "healthcare",
        "risk_tier": "r3",
        "action_class": "a4",
        "gate_set": ["B", "A", "C", "K"],
        "thresholds": {"B": 0.85, "A": 0.85, "C": 0.90, "K": 0.80},
        "weights": {"B": 0.25, "A": 0.20, "C": 0.35, "K": 0.20},
        "penalty_weights": {
            "cb": 0.10, "d": 0.25, "n": 0.30, "h": 0.20, "ps": 0.15
        },
        "decay_rate": 0.060,
        "soft_hold_ceiling": 0.90,
        "decision_thresholds": {
            "theta_allow": 0.85, "theta_hold": 0.85, "theta_escalate": 0.70
        },
        "invalidation_triggers": [
            "model_version_change", "policy_update",
            "data_distribution_drift", "environmental_change",
        ],
        "regulatory_mapping": [
            "FDA 21 CFR Part 820 (SaMD)",
            "HIPAA Section 164.312(b)",
            "CMS Conditions of Participation Section 482.24",
            "Joint Commission IM.02.02.01",
        ],
        "description": (
            "Clinical AI-assisted decision support. C carries highest weight "
            "— patient safety primacy. C3=0.00 is always a hard Stop."
        ),
    },

    # Profile 3: Enterprise Informational — Low Risk
    "enterprise-info-standard-v1": {
        "profile_id": "enterprise-info-standard-v1",
        "domain": "enterprise",
        "risk_tier": "r1",
        "action_class": "a1",
        "gate_set": ["B", "A", "C"],
        "thresholds": {"B": 0.70, "A": 0.70, "C": 0.75, "K": 0.60},
        "weights": {"B": 0.30, "A": 0.25, "C": 0.25, "K": 0.20},
        "penalty_weights": {
            "cb": 0.30, "d": 0.25, "n": 0.20, "h": 0.10, "ps": 0.15
        },
        "decay_rate": 0.008,
        "soft_hold_ceiling": 0.85,
        "decision_thresholds": {
            "theta_allow": 0.75, "theta_hold": 0.65, "theta_escalate": 0.55
        },
        "invalidation_triggers": [
            "model_version_change", "policy_update",
            "data_distribution_drift", "environmental_change",
        ],
        "regulatory_mapping": [],
        "description": (
            "Internal enterprise summarization, drafting, Q&A, informational "
            "workflows. K is scored but does not gate."
        ),
    },

    # Profile 4: Enterprise Operational — Medium Risk
    "enterprise-ops-standard-v1": {
        "profile_id": "enterprise-ops-standard-v1",
        "domain": "enterprise",
        "risk_tier": "r2",
        "action_class": "a3",
        "gate_set": ["B", "A", "C"],
        "thresholds": {"B": 0.80, "A": 0.75, "C": 0.80, "K": 0.70},
        "weights": {"B": 0.30, "A": 0.20, "C": 0.30, "K": 0.20},
        "penalty_weights": {
            "cb": 0.30, "d": 0.25, "n": 0.20, "h": 0.10, "ps": 0.15
        },
        "decay_rate": 0.014,
        "soft_hold_ceiling": 0.88,
        "decision_thresholds": {
            "theta_allow": 0.80, "theta_hold": 0.80, "theta_escalate": 0.65
        },
        "invalidation_triggers": [
            "model_version_change", "policy_update",
            "data_distribution_drift", "environmental_change",
        ],
        "regulatory_mapping": [],
        "description": (
            "Enterprise workflow-triggering actions, automated routing, "
            "internal operational recommendations."
        ),
    },

    # Profile 5: Pharma / Life Sciences — Signal Detection
    "pharma-pv-signal-v1": {
        "profile_id": "pharma-pv-signal-v1",
        "domain": "pharma_life_sciences",
        "risk_tier": "r3",
        "action_class": "a4",
        "gate_set": ["B", "A", "C", "K"],
        "thresholds": {"B": 0.88, "A": 0.92, "C": 0.90, "K": 0.82},
        "weights": {"B": 0.20, "A": 0.35, "C": 0.30, "K": 0.15},
        "penalty_weights": {
            "cb": 0.35, "d": 0.25, "n": 0.20, "h": 0.15, "ps": 0.05
        },
        "decay_rate": 0.070,
        "soft_hold_ceiling": 0.90,
        "decision_thresholds": {
            "theta_allow": 0.85, "theta_hold": 0.85, "theta_escalate": 0.70
        },
        "invalidation_triggers": [
            "model_version_change", "policy_update",
            "data_distribution_drift", "environmental_change",
        ],
        "regulatory_mapping": [
            "FDA 21 CFR Part 11",
            "ICH E2E Pharmacovigilance",
            "EMA AI Strategy",
            "EU GMP Annex 11",
            "ICH Q10",
        ],
        "description": (
            "Pharmacovigilance signal detection. Attribution (A) carries "
            "highest weight — GxP focus is data integrity and provenance."
        ),
    },

    # Profile 6 (Phase 2): Financial Services r3/a4 with CT-4 modifiers
    # pre-applied (TCS-PHASE2-001, CLAUDE.md §"New policy profile for
    # Phase 2 demo").
    #
    # Base profile: fin-high-risk-suitability-v3
    # CT-4 modifiers applied at profile load time (not at
    # resolve_policy_profile time) so the demo can load a single
    # ready-to-use profile. The resolve_policy_profile() function in
    # governed_context.py still exists for general-purpose runtime
    # resolution of an arbitrary (base, ct) pair.
    #
    # Weight delta from base: B 0.30 -> 0.25 (-0.05),
    #                         A 0.25 -> 0.30 (+0.05),
    #                         C 0.30 -> 0.25 (-0.05),
    #                         K 0.15 -> 0.20 (+0.05)
    # Sigma still equals 1.0.
    #
    # A threshold elevated from 0.90 (base) to 0.93 for missing-metadata
    # retrieval contexts — ct4_a_threshold in CLAUDE.md.
    "fin-r3-a4-ct4": {
        "profile_id": "fin-r3-a4-ct4",
        "domain": "financial_services",
        "risk_tier": "r3",
        "action_class": "a4",
        "gate_set": ["B", "A", "C", "K"],
        "thresholds": {"B": 0.90, "A": 0.93, "C": 0.90, "K": 0.80},
        "weights": {"B": 0.25, "A": 0.30, "C": 0.25, "K": 0.20},
        "penalty_weights": {
            "cb": 0.25, "d": 0.10, "n": 0.20, "h": 0.10, "ps": 0.35
        },
        "decay_rate": 0.050,
        "soft_hold_ceiling": 0.90,
        "decision_thresholds": {
            "theta_allow": 0.85, "theta_hold": 0.85, "theta_escalate": 0.70
        },
        "invalidation_triggers": [
            "model_version_change", "policy_update",
            "data_distribution_drift", "environmental_change",
            "context_expansion",
        ],
        "regulatory_mapping": [
            "SEC Regulation Best Interest",
            "SEC Form ADV",
            "FINRA Rule 3110",
            "FINRA Rule 2111",
        ],
        "description": (
            "Financial services r3/a4 with CT-4 (vector DB / RAG) "
            "modifiers pre-applied. Phase 2 demo profile. Attribution "
            "threshold elevated to 0.93 to catch missing-metadata chunks."
        ),
    },

    # Profile 7 (Phase 3): Healthcare Clinical r3/a4 with CT-4 modifiers
    # pre-applied. Phase 3 healthcare demo profile. Uses healthcare pack
    # thresholds with elevated C weight (patient safety primacy) and
    # faster decay rate (clinical context ages faster).
    "healthcare-r3-a4-ct4": {
        "profile_id": "healthcare-r3-a4-ct4",
        "domain": "healthcare",
        "risk_tier": "r3",
        "action_class": "a4",
        "gate_set": ["B", "A", "C", "K"],
        "thresholds": {"B": 0.85, "A": 0.85, "C": 0.90, "K": 0.80},
        "weights": {"B": 0.25, "A": 0.20, "C": 0.35, "K": 0.20},
        "penalty_weights": {
            "cb": 0.10, "d": 0.25, "n": 0.30, "h": 0.20, "ps": 0.15
        },
        "decay_rate": 0.060,
        "soft_hold_ceiling": 0.90,
        "decision_thresholds": {
            "theta_allow": 0.85, "theta_hold": 0.85, "theta_escalate": 0.70
        },
        "invalidation_triggers": [
            "model_version_change", "policy_update",
            "data_distribution_drift", "environmental_change",
            "context_expansion",
        ],
        "regulatory_mapping": [
            "FDA 21 CFR Part 820 (SaMD)",
            "HIPAA Section 164.312(b)",
            "CMS Conditions of Participation Section 482.24",
            "Joint Commission IM.02.02.01",
        ],
        "description": (
            "Healthcare clinical r3/a4 with CT-4 (vector DB / RAG) "
            "modifiers pre-applied. Phase 3 healthcare demo profile. "
            "C carries highest weight — patient safety primacy."
        ),
    },
}


# Eagerly construct and validate every profile at import time. Any invalid
# profile raises immediately — you cannot import this module with a broken
# configuration.
PROFILES: Dict[str, PolicyProfile] = {
    pid: PolicyProfile.from_dict(raw) for pid, raw in _RAW_PROFILES.items()
}


# --------------------------------------------------------------------------- #
# Public API                                                                   #
# --------------------------------------------------------------------------- #

def load_profile(profile_id: str) -> PolicyProfile:
    """
    Load a PolicyProfile by its profile_id.

    Falls back to the Packs registry if the id is not in the built-in
    PROFILES dict. This lets Slice 4 composed packs (pack_id =
    ``composed-<hash16>``) be loaded as profiles transparently — any
    consumer that calls ``load_profile(active_pack.profile_id)``
    receives a usable PolicyProfile regardless of whether the source
    was a built-in profile, a built-in pack, or a standards-composer
    pack.

    Raises
    ------
    ValueError
        If no profile / pack with the given id is registered.
    """
    if profile_id in PROFILES:
        return PROFILES[profile_id]

    # Pack-registry fallback (lazy import to avoid cycle).
    try:
        from tcs.packs.pack_manager import PACKS as _PACKS
    except Exception:
        _PACKS = {}

    for pack in _PACKS.values():
        pc = pack.get("profile_config") or {}
        if pc.get("profile_id") == profile_id:
            return PolicyProfile(
                profile_id=pc["profile_id"],
                domain=pc.get("domain", "unknown"),
                risk_tier=pc["risk_tier"],
                action_class=pc["action_class"],
                gate_set=frozenset(pc["gate_set"]),
                thresholds=dict(pc["thresholds"]),
                weights=dict(pc["weights"]),
                penalty_weights=dict(pc["penalty_weights"]),
                decay_rate=float(pc["decay_rate"]),
                soft_hold_ceiling=float(pc["soft_hold_ceiling"]),
                decision_thresholds=dict(pc["decision_thresholds"]),
                invalidation_triggers=list(pc.get("invalidation_triggers") or []),
                regulatory_mapping=list(pc.get("regulatory_mapping") or []),
                description=pack.get("description", ""),
            )

    raise ValueError(
        f"Unknown policy profile_id {profile_id!r}. "
        f"Registered profiles: {sorted(PROFILES.keys())}"
    )


def list_profiles() -> List[str]:
    """Return all registered profile_ids (sorted)."""
    return sorted(PROFILES.keys())
