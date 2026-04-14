"""
tcs.packs.financial_services
=============================

SEC Reg BI / FINRA 2111 regulatory pack.

Sets r3/a4 baseline with suitability gate, elevated attribution
threshold for CT-4, and fail-closed behavior.
"""

PACK = {
    "pack_id": "financial_services",
    "name": "Financial Services — SEC Reg BI / FINRA 2111",
    "version": "1.0.0",
    "description": (
        "Pre-configured Risk Tolerance Profile for SEC Regulation Best Interest "
        "and FINRA Rule 2111 suitability requirements. All four gates mandatory, "
        "fail-closed enforcement, elevated attribution threshold for CT-4."
    ),
    "regulatory_references": [
        "SEC Regulation Best Interest (Reg BI)",
        "FINRA Rule 2111 (Suitability)",
        "FINRA Rule 3110 (Supervision)",
        "SEC Form ADV Part 2A",
    ],
    "profile_config": {
        "profile_id": "pack-financial-services-v1",
        "domain": "financial_services",
        "risk_tier": "r3",
        "action_class": "a4",
        "gate_set": ["B", "A", "C", "K"],
        "thresholds": {"B": 0.90, "A": 0.90, "C": 0.90, "K": 0.80},
        "weights": {"B": 0.30, "A": 0.25, "C": 0.30, "K": 0.15},
        "penalty_weights": {"cb": 0.25, "d": 0.10, "n": 0.20, "h": 0.10, "ps": 0.35},
        "decay_rate": 0.050,
        "soft_hold_ceiling": 0.90,
        "decision_thresholds": {
            "theta_allow": 0.85,
            "theta_hold": 0.85,
            "theta_escalate": 0.70,
        },
        "invalidation_triggers": [
            "model_version_change", "policy_update",
            "data_distribution_drift", "environmental_change",
        ],
        "regulatory_mapping": [
            "SEC Regulation Best Interest",
            "FINRA Rule 2111",
            "FINRA Rule 3110",
        ],
    },
    "adaptation_floors": {
        "theta_allow": 0.80,
        "theta_hold": 0.75,
        "theta_escalate": 0.60,
    },
    "tc_required_fields": [
        "suitability_determination",
        "supervisory_review_eligible",
        "reg_bi_disclosure_required",
        "model_risk_classification",
    ],
    "audit_export_format": "finra_examination_v2",
    "fail_behavior": "fail_closed",
    "ct4_attribution_threshold": 0.93,
}
