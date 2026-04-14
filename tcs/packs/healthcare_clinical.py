"""
tcs.packs.healthcare_clinical
==============================

FDA SaMD / HIPAA regulatory pack.
"""

PACK = {
    "pack_id": "healthcare_clinical",
    "name": "Healthcare Clinical — FDA SaMD / HIPAA",
    "version": "1.0.0",
    "description": (
        "Pre-configured Risk Tolerance Profile for FDA Software as a Medical "
        "Device (SaMD) and HIPAA compliance. Elevated compliance gate threshold, "
        "uncertainty gate mandatory, fail-closed enforcement."
    ),
    "regulatory_references": [
        "FDA 21st Century Cures Act — SaMD",
        "HIPAA Privacy Rule (45 CFR 164)",
        "HIPAA Security Rule (45 CFR 164.312)",
        "IEC 62304 (Medical Device Software Lifecycle)",
    ],
    "profile_config": {
        "profile_id": "pack-healthcare-clinical-v1",
        "domain": "healthcare",
        "risk_tier": "r3",
        "action_class": "a4",
        "gate_set": ["B", "A", "C", "K"],
        "thresholds": {"B": 0.85, "A": 0.85, "C": 0.90, "K": 0.80},
        "weights": {"B": 0.25, "A": 0.20, "C": 0.35, "K": 0.20},
        "penalty_weights": {"cb": 0.10, "d": 0.25, "n": 0.30, "h": 0.20, "ps": 0.15},
        "decay_rate": 0.060,
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
            "FDA SaMD Pre-Cert",
            "HIPAA Privacy Rule",
            "IEC 62304",
        ],
    },
    "adaptation_floors": {
        "theta_allow": 0.80,
        "theta_hold": 0.75,
        "theta_escalate": 0.65,
    },
    "tc_required_fields": [
        "clinical_risk_classification",
        "hipaa_phi_assessment",
        "samd_risk_category",
        "clinician_review_required",
    ],
    "audit_export_format": "fda_samd_audit_v1",
    "fail_behavior": "fail_closed",
}
