"""
tcs.packs.gmp_quality
======================

21 CFR Part 11 / ISO 13485 regulatory pack.
"""

PACK = {
    "pack_id": "gmp_quality",
    "name": "GMP Quality — 21 CFR Part 11 / ISO 13485",
    "version": "1.0.0",
    "description": (
        "Pre-configured Risk Tolerance Profile for GMP quality systems "
        "under 21 CFR Part 11 electronic records requirements and "
        "ISO 13485 quality management."
    ),
    "regulatory_references": [
        "21 CFR Part 11 (Electronic Records/Signatures)",
        "ISO 13485:2016 (Medical Device QMS)",
        "EU Annex 11 (Computerised Systems)",
        "ICH Q10 (Pharmaceutical Quality System)",
    ],
    "profile_config": {
        "profile_id": "pack-gmp-quality-v1",
        "domain": "healthcare",
        "risk_tier": "r3",
        "action_class": "a3",
        "gate_set": ["B", "A", "C", "K"],
        "thresholds": {"B": 0.85, "A": 0.85, "C": 0.90, "K": 0.80},
        "weights": {"B": 0.25, "A": 0.25, "C": 0.30, "K": 0.20},
        "penalty_weights": {"cb": 0.15, "d": 0.20, "n": 0.25, "h": 0.15, "ps": 0.25},
        "decay_rate": 0.040,
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
            "21 CFR Part 11",
            "ISO 13485:2016",
            "EU Annex 11",
        ],
    },
    "adaptation_floors": {
        "theta_allow": 0.82,
        "theta_hold": 0.78,
        "theta_escalate": 0.65,
    },
    "tc_required_fields": [
        "electronic_signature_status",
        "audit_trail_complete",
        "validation_status",
        "change_control_reference",
    ],
    "audit_export_format": "cfr_part11_audit_v1",
    "fail_behavior": "fail_closed",
}
