"""
tcs.packs.enterprise_ops
=========================

NIST AI RMF / ISO 42001 regulatory pack.
"""

PACK = {
    "pack_id": "enterprise_ops",
    "name": "Enterprise Operations — NIST AI RMF / ISO 42001",
    "version": "1.0.0",
    "description": (
        "Pre-configured Risk Tolerance Profile for enterprise AI operations "
        "aligned with NIST AI Risk Management Framework and ISO/IEC 42001 "
        "AI Management System standard."
    ),
    "regulatory_references": [
        "NIST AI RMF 1.0 (AI 100-1)",
        "ISO/IEC 42001:2023 (AI Management System)",
        "NIST SP 800-53 Rev 5 (Selected Controls)",
        "ISO/IEC 27001:2022 (Information Security)",
    ],
    "profile_config": {
        "profile_id": "pack-enterprise-ops-v1",
        "domain": "enterprise",
        "risk_tier": "r2",
        "action_class": "a3",
        "gate_set": ["B", "A", "C"],
        "thresholds": {"B": 0.75, "A": 0.75, "C": 0.80, "K": 0.70},
        "weights": {"B": 0.25, "A": 0.25, "C": 0.30, "K": 0.20},
        "penalty_weights": {"cb": 0.20, "d": 0.20, "n": 0.20, "h": 0.20, "ps": 0.20},
        "decay_rate": 0.014,
        "soft_hold_ceiling": 0.88,
        "decision_thresholds": {
            "theta_allow": 0.80,
            "theta_hold": 0.80,
            "theta_escalate": 0.65,
        },
        "invalidation_triggers": [
            "model_version_change", "policy_update",
            "data_distribution_drift",
        ],
        "regulatory_mapping": [
            "NIST AI RMF 1.0",
            "ISO/IEC 42001:2023",
        ],
    },
    "adaptation_floors": {
        "theta_allow": 0.70,
        "theta_hold": 0.65,
        "theta_escalate": 0.50,
    },
    "tc_required_fields": [
        "ai_risk_category",
        "impact_assessment_reference",
        "data_governance_status",
    ],
    "audit_export_format": "nist_ai_rmf_v1",
    "fail_behavior": "fail_open",
}
