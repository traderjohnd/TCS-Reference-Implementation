"""
tcs.standards.library
======================

Starter library of 11 regulatory and industry standards across four
industries, plus a taxonomy of Industry > Sub-industry > Use case.

Every entry in ``STANDARDS`` carries:

    - id                   : stable identifier
    - name                 : human-readable name
    - regulatory_reference : the actual standard citation
    - industry             : taxonomy industry key
    - sub_industry         : taxonomy sub-industry key
    - applies_to_use_cases : list of use case keys this standard governs
    - control_interpretation : plain-English note framing the mapping
                              as a TCS governance interpretation, NOT
                              a claim that the standard mathematically
                              requires the specific TCS parameters.
    - profile_adjustments  : structured deltas the composer applies to
                              the base profile (see composer.py for the
                              hybrid / strictest-control rules)

CRITICAL — REGULATORY DISCLAIMER:

The ``profile_adjustments`` values are this implementation's
governance interpretation, not regulatory truth. ISO 13485 does not
literally specify a TCS attribution threshold of 0.92. The standard
emphasizes documentation traceability and design control; under TCS,
that principle is interpreted as elevating the attribution gate. Each
``control_interpretation`` says this in plain English so future
readers understand the mapping is editorial.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional


# --------------------------------------------------------------------------- #
# Taxonomy: Industry > Sub-industry > Use case                                 #
# --------------------------------------------------------------------------- #

TAXONOMY: Dict[str, Dict[str, Any]] = {
    "life_sciences": {
        "name": "Life Sciences",
        "sub_industries": {
            "medical_devices": {
                "name": "Medical Devices",
                "use_cases": {
                    "clinical_decision_support": "Clinical decision support",
                    "device_software": "Device software / SaMD",
                    "treatment_planning": "Treatment planning assistance",
                },
            },
            "pharma": {
                "name": "Pharmaceuticals",
                "use_cases": {
                    "drug_safety": "Drug safety / pharmacovigilance",
                    "clinical_trials": "Clinical trial operations",
                    "manufacturing_qc": "Manufacturing quality control",
                },
            },
        },
    },
    "financial_services": {
        "name": "Financial Services",
        "sub_industries": {
            "investment_advisory": {
                "name": "Investment Advisory",
                "use_cases": {
                    "recommendation_generation": "Investment recommendation generation",
                    "suitability_review": "Suitability review",
                    "trade_execution": "Trade execution decisions",
                },
            },
        },
    },
    "general_ai_governance": {
        "name": "General AI Governance",
        "sub_industries": {
            "cross_industry": {
                "name": "Cross-industry",
                "use_cases": {
                    "any_ai_workflow": "Any AI workflow (cross-industry baseline)",
                    "high_risk_ai_system": "High-risk AI system (EU AI Act Article 6)",
                },
            },
        },
    },
}


# --------------------------------------------------------------------------- #
# 11-standard starter library                                                  #
# --------------------------------------------------------------------------- #
#
# profile_adjustments schema:
#
#   threshold_floors      : Dict[dim, float]
#       Minimum threshold per dimension. Composer takes the STRICTEST
#       (max) value across selected standards for each dimension.
#
#   gate_set_required     : List[str]
#       Dimensions this standard requires in the gate set. Composer
#       takes the UNION across selected standards.
#
#   weight_deltas         : Dict[dim, float]
#       Additive deltas to apply to base weights. Composer sums them,
#       then re-normalizes so Σ weights = 1.0. Deltas are visible in
#       the per-standard contribution panel.
#
#   penalty_weight_deltas : Dict[component, float]
#       Additive deltas to apply to penalty weights. Summed, capped at
#       1.0 cumulative per component, then re-normalized.
#
#   required_controls     : List[str]
#       Named controls the standard requires the deployment to honor.
#       Composer takes the UNION (OR logic). Recorded on the composed
#       pack as control_requirements for audit / compliance reporting.
#
#   hard_prohibitions     : List[str]
#       Named prohibitions. Composer takes the UNION. Recorded on the
#       composed pack as hard_prohibitions; downstream enforcement
#       (a future slice) can wire these into C3 detection.

STANDARDS: Dict[str, Dict[str, Any]] = {

    # ---- Medical Devices ------------------------------------------------- #

    "iso_13485": {
        "id": "iso_13485",
        "name": "ISO 13485 — Medical Device QMS",
        "regulatory_reference": "ISO 13485:2016 — Medical devices: Quality management systems",
        "industry": "life_sciences",
        "sub_industry": "medical_devices",
        "applies_to_use_cases": [
            "clinical_decision_support", "device_software", "treatment_planning",
        ],
        "control_interpretation": (
            "ISO 13485 emphasizes documentation traceability, design control, "
            "and a quality management system covering the full device lifecycle. "
            "Under TCS, this implementation interprets that principle as "
            "elevating Attribution requirements (every recommendation must be "
            "traceable to a documented, version-controlled source) and adding "
            "compliance gating. This is a governance interpretation; ISO 13485 "
            "does not specify a numerical TCS attribution threshold."
        ),
        "profile_adjustments": {
            "threshold_floors": {"A": 0.90, "C": 0.85},
            "gate_set_required": ["A", "C"],
            "weight_deltas": {"A": +0.05, "B": -0.025, "C": -0.025},
            "penalty_weight_deltas": {"cb": +0.03},
            "required_controls": ["documentation_traceability", "design_control_records"],
            "hard_prohibitions": [],
        },
    },

    "iso_14971": {
        "id": "iso_14971",
        "name": "ISO 14971 — Medical Device Risk Management",
        "regulatory_reference": "ISO 14971:2019 — Medical devices: Application of risk management",
        "industry": "life_sciences",
        "sub_industry": "medical_devices",
        "applies_to_use_cases": [
            "clinical_decision_support", "device_software", "treatment_planning",
        ],
        "control_interpretation": (
            "ISO 14971 establishes a risk-management framework for medical "
            "devices spanning identification, evaluation, and control of "
            "hazards. Under TCS, this implementation interprets that as "
            "elevating Known (calibration) requirements — the system must "
            "express confidence proportional to actual reliability — and "
            "as adding a novelty penalty so out-of-distribution cases are "
            "flagged for human review. ISO 14971 does not specify a TCS "
            "K-threshold; the elevation is editorial."
        ),
        "profile_adjustments": {
            "threshold_floors": {"K": 0.85},
            "gate_set_required": ["K"],
            "weight_deltas": {"K": +0.05, "B": -0.05},
            "penalty_weight_deltas": {"n": +0.05},
            "required_controls": ["risk_assessment_record", "post_market_surveillance"],
            "hard_prohibitions": [],
        },
    },

    "iec_62304": {
        "id": "iec_62304",
        "name": "IEC 62304 — Medical Device Software Lifecycle",
        "regulatory_reference": "IEC 62304:2006/A1:2015 — Medical device software lifecycle processes",
        "industry": "life_sciences",
        "sub_industry": "medical_devices",
        "applies_to_use_cases": [
            "device_software",
            "clinical_decision_support",
            "treatment_planning",
        ],
        "control_interpretation": (
            "IEC 62304 governs the software development lifecycle for medical "
            "device software, with controls scaled by safety classification "
            "(Class A/B/C). Under TCS, this implementation interprets that as "
            "elevating Compliance requirements (the software's outputs must "
            "satisfy applicable safety-classification controls) and as "
            "tightening the Compliance gate so prohibited patterns are caught "
            "before the response is delivered. This is an interpretation, not "
            "a numerical requirement from IEC 62304 itself."
        ),
        "profile_adjustments": {
            "threshold_floors": {"C": 0.90},
            "gate_set_required": ["C"],
            "weight_deltas": {"C": +0.05, "B": -0.05},
            "penalty_weight_deltas": {"h": +0.03},
            "required_controls": ["software_classification", "verification_records"],
            "hard_prohibitions": [],
        },
    },

    # ---- Pharma ---------------------------------------------------------- #

    "fda_21_cfr_part_11": {
        "id": "fda_21_cfr_part_11",
        "name": "FDA 21 CFR Part 11 — Electronic Records & Signatures",
        "regulatory_reference": "21 CFR Part 11 — Electronic records; electronic signatures",
        "industry": "life_sciences",
        "sub_industry": "pharma",
        "applies_to_use_cases": [
            "drug_safety", "clinical_trials", "manufacturing_qc",
        ],
        "control_interpretation": (
            "21 CFR Part 11 requires electronic records and signatures to be "
            "trustworthy, reliable, and equivalent to paper records, with "
            "audit trails, access controls, and validated systems. Under TCS, "
            "this implementation interprets that as elevating Attribution "
            "requirements — every output must trace to authenticated identity "
            "and timestamped evidence — and as adding identity-binding "
            "controls. 21 CFR Part 11 does not specify TCS thresholds; the "
            "interpretation is editorial."
        ),
        "profile_adjustments": {
            "threshold_floors": {"A": 0.92, "B": 0.85},
            "gate_set_required": ["A"],
            "weight_deltas": {"A": +0.08, "K": -0.04, "B": -0.04},
            "penalty_weight_deltas": {"cb": +0.04},
            "required_controls": [
                "audit_trail_complete", "identity_binding", "system_validation",
            ],
            "hard_prohibitions": [],
        },
    },

    "gmp": {
        "id": "gmp",
        "name": "GMP — Good Manufacturing Practice",
        "regulatory_reference": "21 CFR Parts 210 & 211 / EU GMP / WHO TRS 986 Annex 2",
        "industry": "life_sciences",
        "sub_industry": "pharma",
        "applies_to_use_cases": ["manufacturing_qc"],
        "control_interpretation": (
            "GMP requires that pharmaceutical products are consistently "
            "produced and controlled according to quality standards, with "
            "complete documentation, validated processes, and deviation "
            "tracking. Under TCS, this implementation interprets that as "
            "elevating Compliance and adding a documentation-completeness "
            "penalty so missing batch-record references increase scrutiny. "
            "GMP does not specify TCS numerical thresholds."
        ),
        "profile_adjustments": {
            "threshold_floors": {"C": 0.92},
            "gate_set_required": ["C"],
            "weight_deltas": {"C": +0.05, "A": +0.03, "K": -0.04, "B": -0.04},
            "penalty_weight_deltas": {"d": +0.03, "h": +0.03},
            "required_controls": [
                "batch_record_complete", "deviation_tracked", "validated_process",
            ],
            "hard_prohibitions": [],
        },
    },

    "ich_e6": {
        "id": "ich_e6",
        "name": "ICH E6 — Good Clinical Practice",
        "regulatory_reference": "ICH E6(R2) — Good Clinical Practice for Clinical Trials",
        "industry": "life_sciences",
        "sub_industry": "pharma",
        "applies_to_use_cases": ["clinical_trials"],
        "control_interpretation": (
            "ICH E6 sets ethical and scientific quality standards for "
            "designing, conducting, and reporting clinical trials. It "
            "emphasizes informed consent, source-data verification, and "
            "trial monitoring. Under TCS, this implementation interprets "
            "that as elevating both Attribution (source verification) and "
            "Known calibration (confidence proportional to evidence). The "
            "TCS numerical adjustments are editorial."
        ),
        "profile_adjustments": {
            "threshold_floors": {"A": 0.90, "K": 0.82},
            "gate_set_required": ["A", "K"],
            "weight_deltas": {"A": +0.04, "K": +0.04, "B": -0.04, "C": -0.04},
            "penalty_weight_deltas": {"h": +0.04},
            "required_controls": [
                "informed_consent", "source_data_verification", "monitoring_plan",
            ],
            "hard_prohibitions": [],
        },
    },

    # ---- Financial Services --------------------------------------------- #

    "sec_reg_bi": {
        "id": "sec_reg_bi",
        "name": "SEC Regulation Best Interest",
        "regulatory_reference": "17 CFR § 240.15l-1 — Regulation Best Interest",
        "industry": "financial_services",
        "sub_industry": "investment_advisory",
        "applies_to_use_cases": [
            "recommendation_generation", "suitability_review",
        ],
        "control_interpretation": (
            "SEC Reg BI requires that broker-dealers act in the retail "
            "customer's best interest when making recommendations, with "
            "specific obligations for disclosure, care, conflict-of-interest "
            "management, and compliance. Under TCS, this implementation "
            "interprets that as elevating Compliance gating (the suitability "
            "determination must clear policy thresholds) and elevating the "
            "policy-sensitivity penalty so concentrated positions and "
            "restricted instruments receive higher scrutiny. Reg BI does "
            "not specify TCS parameters; the mapping is editorial."
        ),
        "profile_adjustments": {
            "threshold_floors": {"C": 0.90, "A": 0.88},
            "gate_set_required": ["B", "A", "C"],
            "weight_deltas": {"C": +0.05, "B": -0.025, "K": -0.025},
            "penalty_weight_deltas": {"ps": +0.05},
            "required_controls": [
                "suitability_determination", "best_interest_disclosure",
                "conflict_of_interest_identification",
            ],
            "hard_prohibitions": [],
        },
    },

    "finra_2111": {
        "id": "finra_2111",
        "name": "FINRA Rule 2111 — Suitability",
        "regulatory_reference": "FINRA Rule 2111 — Suitability obligations",
        "industry": "financial_services",
        "sub_industry": "investment_advisory",
        "applies_to_use_cases": [
            "recommendation_generation", "suitability_review", "trade_execution",
        ],
        "control_interpretation": (
            "FINRA 2111 requires three forms of suitability: reasonable-basis, "
            "customer-specific, and quantitative. Under TCS, this "
            "implementation interprets that as elevating Compliance "
            "(quantitative suitability test) and increasing the human-review "
            "penalty so stale reviews are caught. FINRA 2111 does not "
            "specify TCS thresholds; the mapping is editorial."
        ),
        "profile_adjustments": {
            "threshold_floors": {"C": 0.88},
            "gate_set_required": ["C"],
            "weight_deltas": {"C": +0.03, "A": +0.02, "K": -0.05},
            "penalty_weight_deltas": {"h": +0.04, "ps": +0.03},
            "required_controls": [
                "reasonable_basis_suitability", "customer_specific_suitability",
                "quantitative_suitability",
            ],
            "hard_prohibitions": [],
        },
    },

    # ---- General AI Governance ------------------------------------------ #

    "nist_ai_rmf": {
        "id": "nist_ai_rmf",
        "name": "NIST AI Risk Management Framework",
        "regulatory_reference": "NIST AI RMF 1.0 (NIST AI 100-1, January 2023)",
        "industry": "general_ai_governance",
        "sub_industry": "cross_industry",
        "applies_to_use_cases": ["any_ai_workflow", "high_risk_ai_system"],
        "control_interpretation": (
            "NIST AI RMF organizes AI risk management around four "
            "functions: Govern, Map, Measure, Manage. It emphasizes "
            "trustworthy AI characteristics including validity, reliability, "
            "safety, accountability, transparency, explainability, privacy, "
            "and fairness. Under TCS, this implementation interprets the "
            "RMF as elevating Boundedness (scope and authorized operation) "
            "and Attribution (accountability, transparency). The NIST RMF "
            "is voluntary guidance; the TCS parameter mapping is editorial."
        ),
        "profile_adjustments": {
            "threshold_floors": {"B": 0.85, "A": 0.85},
            "gate_set_required": ["B", "A"],
            "weight_deltas": {"B": +0.03, "A": +0.03, "C": -0.03, "K": -0.03},
            "penalty_weight_deltas": {"n": +0.03},
            "required_controls": [
                "govern_function", "map_function", "measure_function", "manage_function",
            ],
            "hard_prohibitions": [],
        },
    },

    "iso_iec_42001": {
        "id": "iso_iec_42001",
        "name": "ISO/IEC 42001 — AI Management System",
        "regulatory_reference": "ISO/IEC 42001:2023 — Information technology: Artificial intelligence: Management system",
        "industry": "general_ai_governance",
        "sub_industry": "cross_industry",
        "applies_to_use_cases": ["any_ai_workflow", "high_risk_ai_system"],
        "control_interpretation": (
            "ISO/IEC 42001 specifies requirements for an AI management "
            "system covering AI lifecycle governance, impact assessment, "
            "and operational controls. Under TCS, this implementation "
            "interprets it as elevating all three of Boundedness, "
            "Attribution, and Known calibration — the standard's scope "
            "spans the full BACK model. The TCS parameter mapping is "
            "editorial; ISO/IEC 42001 does not specify TCS thresholds."
        ),
        "profile_adjustments": {
            "threshold_floors": {"B": 0.85, "A": 0.85, "K": 0.80},
            "gate_set_required": ["B", "A", "K"],
            "weight_deltas": {"B": +0.03, "A": +0.03, "K": +0.03, "C": -0.09},
            "penalty_weight_deltas": {"cb": +0.02, "n": +0.02},
            "required_controls": [
                "ai_lifecycle_governance", "impact_assessment", "operational_controls",
                "internal_audit_program",
            ],
            "hard_prohibitions": [],
        },
    },

    "eu_ai_act_high_risk": {
        "id": "eu_ai_act_high_risk",
        "name": "EU AI Act — High-Risk Systems",
        "regulatory_reference": "Regulation (EU) 2024/1689 — Artificial Intelligence Act, Articles 8-15",
        "industry": "general_ai_governance",
        "sub_industry": "cross_industry",
        "applies_to_use_cases": ["high_risk_ai_system"],
        "control_interpretation": (
            "The EU AI Act high-risk regime (Articles 8-15) requires risk "
            "management systems, data governance, technical documentation, "
            "record-keeping, transparency, human oversight, accuracy, "
            "robustness, and cybersecurity for high-risk AI systems. Under "
            "TCS, this implementation interprets this as the strictest "
            "tier — elevating all four BACK dimension thresholds and "
            "requiring all gates. It also adds explicit prohibitions "
            "aligned with Article 5 (subliminal manipulation, social "
            "scoring, etc.). The TCS parameter mapping is editorial; the "
            "EU AI Act does not specify TCS thresholds."
        ),
        "profile_adjustments": {
            "threshold_floors": {"B": 0.88, "A": 0.90, "C": 0.90, "K": 0.85},
            "gate_set_required": ["B", "A", "C", "K"],
            "weight_deltas": {"B": +0.02, "A": +0.03, "C": +0.03, "K": +0.02},
            "penalty_weight_deltas": {"cb": +0.03, "n": +0.03, "h": +0.02, "ps": +0.02},
            "required_controls": [
                "risk_management_system", "data_governance", "technical_documentation",
                "record_keeping", "transparency_to_user", "human_oversight",
                "accuracy_robustness_cybersecurity",
            ],
            "hard_prohibitions": [
                "subliminal_manipulation", "social_scoring",
                "untargeted_facial_image_scraping",
            ],
        },
    },

}


# --------------------------------------------------------------------------- #
# Accessors                                                                    #
# --------------------------------------------------------------------------- #

def get_standard(standard_id: str) -> Optional[Dict[str, Any]]:
    """Return the full standard entry or None if not found."""
    return STANDARDS.get(standard_id)


def list_standards() -> List[Dict[str, Any]]:
    """Return summary info (no profile_adjustments) for every standard."""
    return [
        {
            "id": s["id"],
            "name": s["name"],
            "regulatory_reference": s["regulatory_reference"],
            "industry": s["industry"],
            "sub_industry": s["sub_industry"],
            "applies_to_use_cases": list(s["applies_to_use_cases"]),
            "control_interpretation": s["control_interpretation"],
        }
        for s in STANDARDS.values()
    ]


def standards_for_use_case(use_case: str) -> List[Dict[str, Any]]:
    """Return standards that apply to the given use case key."""
    return [
        {"id": s["id"], "name": s["name"]}
        for s in STANDARDS.values()
        if use_case in s["applies_to_use_cases"]
    ]
