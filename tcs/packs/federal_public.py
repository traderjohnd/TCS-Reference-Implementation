"""
tcs.packs.federal_public
=========================

FedRAMP / NIST SP 800 regulatory pack.
"""

PACK = {
    "pack_id": "federal_public",
    "name": "Federal Public Sector — FedRAMP / NIST SP 800",
    "version": "1.0.0",
    "description": (
        "Pre-configured Risk Tolerance Profile for federal government "
        "AI deployments under FedRAMP authorization and NIST SP 800 series "
        "security controls."
    ),
    "regulatory_references": [
        "FedRAMP Authorization (Moderate Baseline)",
        "NIST SP 800-53 Rev 5 (Security Controls)",
        "NIST SP 800-171 Rev 2 (CUI Protection)",
        "Executive Order 14110 (Safe AI)",
        "OMB M-24-10 (AI Governance)",
    ],
    "profile_config": {
        "profile_id": "pack-federal-public-v1",
        "domain": "enterprise",
        "risk_tier": "r3",
        "action_class": "a4",
        "gate_set": ["B", "A", "C", "K"],
        "thresholds": {"B": 0.85, "A": 0.85, "C": 0.90, "K": 0.80},
        "weights": {"B": 0.25, "A": 0.25, "C": 0.30, "K": 0.20},
        "penalty_weights": {"cb": 0.20, "d": 0.15, "n": 0.20, "h": 0.15, "ps": 0.30},
        "decay_rate": 0.030,
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
            "FedRAMP Moderate",
            "NIST SP 800-53 Rev 5",
            "EO 14110",
        ],
    },
    "adaptation_floors": {
        "theta_allow": 0.80,
        "theta_hold": 0.75,
        "theta_escalate": 0.65,
    },
    "tc_required_fields": [
        "fedramp_authorization_level",
        "cui_classification",
        "security_control_mapping",
        "ato_reference",
    ],
    "audit_export_format": "fedramp_sar_v1",
    "fail_behavior": "fail_closed",
}
