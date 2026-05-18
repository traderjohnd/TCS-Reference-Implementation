"""
tcs.governance.scenario_rules
==============================

Starter governance risk-classification rules.

Each rule is a deterministic, domain-scoped term-group pattern. It is
NOT semantic understanding: matching succeeds when the query contains
at least one term from each declared term group (and none of the
forbidden terms). Variant phrasings that use the configured
vocabularies produce the same governance signal; variants outside
the configured vocabularies are managed by extending the term groups,
not by the engine inferring intent.

Every rule declares a ``control_class`` in its effect so the audit
distinguishes hard-safety guardrails (which produce STOP regardless
of scoring) from deterministic-bounded category flags (which produce
HOLD pending the typed-facts evaluator) from weighted-evidence rules
(which only shift BACK scores). See the docstring in
``risk_classifier.py`` for the three-class model.

Architectural rule (pinned): hard safety MAY be detected by rules
when the violation is a prompt-risk pattern (prompt injection,
credential exposure, consumer-facing patient-specific prescribing
under prohibited indications). BUT bounded device safety MUST be
evaluated against structured facts and validated envelopes.
Numerical safety envelopes (neonatal defibrillator energy ranges,
weight-based dosing limits, role-authorization gates) DO NOT belong
here — they belong to ``tcs.governance.governed_facts`` and its
forthcoming evaluator. A term-group rule that pretends to evaluate
a numeric envelope is the wrong answer.

The rule list is intentionally small to start; the goal is to
demonstrate the pattern, not to ship a comprehensive ruleset. A real
deployment would maintain this list against authoritative sources
(drug-interaction database, regulatory prohibition lists, internal
compliance policy).

To add a rule: define a RiskRule with required_term_groups (each group
is "any of these terms"; all groups must match), forbidden_terms (any
of these present blocks the match), and an effect that emits the
governance signals.

To test a rule: write a unit test in tests/test_governance_rules.py
with 3–5 variant prompts that should all match the same rule, plus
1–2 near-miss prompts that should NOT match.
"""

from __future__ import annotations

from tcs.governance.risk_classifier import (
    CONTROL_CLASS_DETERMINISTIC_BOUNDED,
    CONTROL_CLASS_HARD_SAFETY,
    SAFETY_PROHIBITED_ACTION,
    SAFETY_PROHIBITED_CONTENT,
    SAFETY_PROMPT_INJECTION_PATTERN,
    SAFETY_UNAUTHORIZED_SCOPE,
    RiskRule,
    RuleEffect,
)


# --------------------------------------------------------------------------- #
# Term vocabularies — composed into rule groups below                          #
# --------------------------------------------------------------------------- #

_MEDICATION_TERMS = (
    "medication", "medications", "drug", "drugs", "med", "meds",
    "rx", "prescription", "prescribe", "prescribing", "pharmaceutical",
    # Specific high-attention substances commonly involved in dosing
    # decisions. Adding name-level coverage helps catch queries that
    # omit the word "medication" but name a specific drug.
    "warfarin", "lithium", "morphine", "oxycodone", "fentanyl",
    "methotrexate", "digoxin", "phenytoin", "carbamazepine",
    "amiodarone", "clozapine", "tacrolimus", "methadone",
    "valproate", "valproic",
)

_DOSING_TERMS = (
    "dose", "dosing", "dosage", "dosages",
    "mg", "mcg", "milligram", "microgram", "ml",
    "titrate", "titration", "titrating",
    "adjust", "adjustment", "increase", "decrease",
    "give", "administer", "administered", "start",
    "prescribe", "prescribing", "regimen",
)

_PREGNANCY_TERMS = (
    "pregnan*",     # pregnant, pregnancy, pregnancies
    "gestational", "gestation",
    "maternal", "antenatal", "prenatal",
    "trimester",
    "expecting mother", "expecting mothers",
)

# ─── Lithium / prescribing-during-pregnancy — context indicators ────────────
#
# The refined three-path model for medication-dosing-during-pregnancy:
#
#   consumer-facing or unauthorized patient-specific dosing  → STOP
#   clinician-facing patient-specific dosing                 → HOLD specialist
#   clinician-facing general guideline summary               → falls through
#
# We separate them with term groups, not by inferring intent:
#
#   _CONSUMER_SELF_DOSING_TERMS — phrases that read as "I, the patient,
#       want a dose for myself / my baby." The rule layer can detect
#       these with reasonable precision.
#
#   _CLINICIAN_CONTEXT_TERMS — phrases that read as "a clinician is
#       asking about a specific case." Triggers HOLD.
#
#   _PATIENT_SPECIFIC_DOSING_TERMS — phrases that narrow the question
#       to a specific patient case (vs a general literature summary).
#       Required for the clinician-facing HOLD branch; absence means
#       the query is more general and should fall through to standard
#       BACK/TIS scoring.
#
# A query that contains NEITHER consumer indicators NOR clinician +
# patient-specific indicators falls through to scoring — that is the
# "clinician general guideline summary" path the user described.

_CONSUMER_SELF_DOSING_TERMS = (
    "i'm pregnant", "i am pregnant",
    "i am taking", "i'm taking",
    "should i take", "can i take",
    "what dose should i", "what mg should i",
    "for me", "for myself",
    "my pregnancy", "my baby",
    "my doctor said", "my doctor told me",
    "i want to know what dose",
)

# The clinician + patient-specific signal for the path-2 rule.
# A single broad list: any of these terms in the query indicates a
# clinician asking about a specific case (vs general literature
# summary). We don't try to separately require "clinician context"
# AND "patient specificity" because (a) most clinician indicators
# imply patient specificity and (b) the rule is term-group based,
# so requiring both groups to match exactly creates false negatives
# on natural phrasings like "for this pregnant patient" where the
# words don't appear as a contiguous substring.
#
# The fine-grained distinction between clinician general guideline
# and clinician patient-specific dosing belongs to the typed-facts
# evaluator (intended_use field), not the term-group classifier.
_CLINICIAN_PATIENT_SPECIFIC_TERMS = (
    "patient", "patients",
    "for my patient", "for a patient", "for this patient",
    "for this pregnant patient", "to a pregnant",
    "this patient", "the patient",
    "prescribe", "prescribing",
    "outpatient", "inpatient",
    "clinical decision support",
)

_PEDIATRIC_OFFLABEL_TERMS = (
    "off-label", "off label", "experimental",
    "compassionate use", "investigational",
)

_PEDIATRIC_TERMS = (
    "pediatric", "paediatric", "child", "children",
    "infant", "infants", "neonate", "neonatal", "newborn",
    "toddler", "preschool",
)

_PATIENT_IDENTIFIER_TERMS = (
    # Phrases suggesting a patient-specific query by name or weak ID.
    "patient ",                                 # "patient john doe"
    "for patient", "this patient",
    "mr.", "mrs.", "ms.",
    "john doe", "jane doe",
)

_PROMPT_INJECTION_TERMS = (
    "ignore policy", "ignore policies",
    "override compliance", "override the compliance",
    "override compliance rules", "override the rules",
    "bypass governance", "bypass the governance",
    "ignore the above", "disregard the rules",
    "disregard the policy", "disregard compliance",
    "regardless of risk profile", "regardless of policy",
    "regardless of compliance",
)

_RESTRICTED_INVESTMENT_TERMS = (
    "leveraged etf", "leveraged etfs",
    "binary option", "binary options",
    "unregistered security", "unregistered securities",
)


# --------------------------------------------------------------------------- #
# Rules                                                                        #
# --------------------------------------------------------------------------- #

SCENARIO_RULES = (

    # ─── Lithium-rule refinement, path 1 ─────────────────────────────────
    # Consumer-facing or unauthorized patient-specific dosing during
    # pregnancy → STOP. The signal here is a consumer-self-dosing
    # phrase ("should I take", "for me", "my pregnancy") combined with
    # the medication+dosing+pregnancy base. A clinician asking about a
    # specific patient does not fire this rule (see path 2).
    RiskRule(
        rule_id="consumer_facing_dosing_during_pregnancy",
        version="v1",
        name="Consumer-facing patient-specific dosing during pregnancy",
        description=(
            "Unauthorized consumer-facing patient-specific medication "
            "dosing during pregnancy is prohibited regardless of agent. "
            "Self-dosing recommendations bypass the maternal-fetal "
            "medicine consultation pathway and create teratogenic and "
            "obstetric risk. STOP and refer to clinician."
        ),
        applies_to_domains=("life_sciences", "healthcare"),
        required_term_groups=(
            _MEDICATION_TERMS,
            _DOSING_TERMS,
            _PREGNANCY_TERMS,
            _CONSUMER_SELF_DOSING_TERMS,
        ),
        effect=RuleEffect(
            c3_violation=True,
            control_class=CONTROL_CLASS_HARD_SAFETY,
            safety_category=SAFETY_PROHIBITED_ACTION,
            # Stays non-overrideable on the consumer-facing route. A
            # clinician override of a consumer-facing STOP would
            # implicitly re-route the query, which is not what override
            # means; if a clinician is asking, the query should fall on
            # path 2 instead.
            override_policy="non_overrideable",
            blocking_reason="consumer_facing_dosing_during_pregnancy",
            requires_human_review=True,
            decision_pressure="STOP",
            explanation=(
                "Consumer-facing patient-specific dosing during pregnancy "
                "is a prohibited AI action. Refer the patient to a clinician; "
                "maternal-fetal medicine consultation is required."
            ),
        ),
    ),

    # ─── Lithium-rule refinement, path 2 ─────────────────────────────────
    # Clinician-facing patient-specific dosing during pregnancy → HOLD
    # for specialist review. The rule layer flags the *category* — the
    # actual decision of whether the proposed dose is within validated
    # intended-use bounds belongs to the typed-facts evaluator
    # (deterministic_bounded control class). Until that evaluator runs,
    # we HOLD pending specialist (MFM) review.
    RiskRule(
        rule_id="clinician_patient_specific_dosing_during_pregnancy",
        version="v1",
        name="Clinician-facing patient-specific dosing during pregnancy",
        description=(
            "A clinician asking for a patient-specific medication dose "
            "during pregnancy needs maternal-fetal medicine sign-off "
            "before the AI delivers a recommendation. The rule layer "
            "flags the category; the patient-weight / device-class / "
            "intended-use envelope check belongs to the bounded-control "
            "evaluator (deterministic_bounded)."
        ),
        applies_to_domains=("life_sciences", "healthcare"),
        required_term_groups=(
            _MEDICATION_TERMS,
            _DOSING_TERMS,
            _PREGNANCY_TERMS,
            _CLINICIAN_PATIENT_SPECIFIC_TERMS,
        ),
        # If a consumer self-dosing phrase is present, the consumer-
        # facing rule (path 1) fires instead — those should never
        # both match the same query.
        forbidden_terms=_CONSUMER_SELF_DOSING_TERMS,
        effect=RuleEffect(
            control_class=CONTROL_CLASS_DETERMINISTIC_BOUNDED,
            # Not a c3_violation by itself — this is a category flag
            # the typed-facts evaluator will refine. Until that runs,
            # we hold for specialist review.
            override_policy="specialist_review",
            requires_human_review=True,
            known_calibration_penalty=0.20,
            decision_pressure="HOLD",
            explanation=(
                "Patient-specific medication dosing during pregnancy "
                "requires maternal-fetal medicine specialist review "
                "before the AI may deliver a recommendation."
            ),
        ),
    ),

    # ─── Healthcare: pediatric off-label dosing → HOLD ───────────────────
    RiskRule(
        rule_id="clinical_pediatric_offlabel_dosing",
        version="v1",
        name="Pediatric off-label dosing recommendation",
        description=(
            "Off-label pediatric dosing recommendations require senior "
            "clinician sign-off per the clinical protocols. The AI may "
            "provide background context but a hold is required before "
            "delivery of a specific dosing recommendation."
        ),
        applies_to_domains=("life_sciences", "healthcare"),
        required_term_groups=(
            _MEDICATION_TERMS,
            _DOSING_TERMS,
            _PEDIATRIC_TERMS,
            _PEDIATRIC_OFFLABEL_TERMS,
        ),
        effect=RuleEffect(
            control_class=CONTROL_CLASS_DETERMINISTIC_BOUNDED,
            override_policy="specialist_review",
            requires_human_review=True,
            known_calibration_penalty=0.20,
            decision_pressure="HOLD",
            explanation=(
                "Pediatric off-label dosing requires senior clinician sign-off "
                "before delivery."
            ),
        ),
    ),

    # ─── Healthcare: patient-specific query without verified ID → HOLD ───
    RiskRule(
        rule_id="patient_identity_without_verification",
        version="v1",
        name="Patient-specific query lacking identity verification",
        description=(
            "Queries that ask about a specific patient by name or weak "
            "identifier require MRN + second identifier and a verified "
            "clinician role before T2 data is disclosed. The AI must hold "
            "the response pending identity verification. This is an "
            "unauthorized-scope guardrail, not a prohibited content "
            "pattern — the same question with a verified MRN is fine."
        ),
        applies_to_domains=("life_sciences", "healthcare"),
        required_term_groups=(
            _PATIENT_IDENTIFIER_TERMS,
            ("medication", "medications", "history", "diagnosis", "chart",
             "lab", "labs", "result", "results", "condition", "conditions"),
        ),
        # If the user already provided MRN-style identifiers we step out.
        forbidden_terms=("mrn", "medical record number"),
        effect=RuleEffect(
            control_class=CONTROL_CLASS_HARD_SAFETY,
            safety_category=SAFETY_UNAUTHORIZED_SCOPE,
            override_policy="policy_exception",
            requires_human_review=True,
            attribution_penalty=0.30,
            boundedness_penalty=0.20,
            decision_pressure="HOLD",
            explanation=(
                "Patient identity not verified; query references patient by "
                "name alone. Provide MRN + second identifier for clinician-"
                "authorized data access."
            ),
        ),
    ),

    # ─── Healthcare: prohibited drug-interaction combinations → STOP ─────
    # This rule replaces the medical pair patterns that were previously
    # hardcoded in the LLM connector. Moving them here makes them
    # discoverable, testable, and editable as governance config.
    RiskRule(
        rule_id="prohibited_drug_combination_warfarin_macrolide",
        version="v1",
        name="Warfarin + macrolide antibiotic — prohibited combination",
        description=(
            "Macrolide antibiotics (clarithromycin, erythromycin) inhibit "
            "CYP3A4 / CYP1A2 and significantly elevate warfarin INR, "
            "increasing bleeding risk. The combination is prohibited under "
            "the drug interactions reference."
        ),
        applies_to_domains=("life_sciences", "healthcare"),
        required_term_groups=(
            ("warfarin",),
            ("clarithromycin", "erythromycin", "azithromycin", "macrolide"),
        ),
        effect=RuleEffect(
            c3_violation=True,
            control_class=CONTROL_CLASS_HARD_SAFETY,
            # Drug-interaction prohibition is a CONTENT pattern (the
            # prohibited content is the combination itself, akin to
            # the canonical Phase 1 Scenario 1 example), distinct from
            # an action pattern like "dose during pregnancy".
            safety_category=SAFETY_PROHIBITED_CONTENT,
            override_policy="non_overrideable",
            blocking_reason="prohibited_drug_combination_warfarin_macrolide",
            decision_pressure="STOP",
            explanation=(
                "Prohibited drug combination (warfarin + macrolide). "
                "Recommend an alternative antibiotic class."
            ),
        ),
    ),

    RiskRule(
        rule_id="prohibited_drug_combination_methotrexate_tmp_smx",
        version="v1",
        name="Methotrexate + trimethoprim-sulfamethoxazole — prohibited",
        description=(
            "Increased methotrexate toxicity through reduced renal clearance. "
            "Prohibited in standard outpatient prescribing."
        ),
        applies_to_domains=("life_sciences", "healthcare"),
        required_term_groups=(
            ("methotrexate",),
            ("trimethoprim", "sulfamethoxazole", "tmp", "bactrim", "septra"),
        ),
        effect=RuleEffect(
            c3_violation=True,
            control_class=CONTROL_CLASS_HARD_SAFETY,
            safety_category=SAFETY_PROHIBITED_CONTENT,
            override_policy="non_overrideable",
            blocking_reason="prohibited_drug_combination_methotrexate_tmp_smx",
            decision_pressure="STOP",
            explanation="Prohibited drug combination (methotrexate + TMP/SMX).",
        ),
    ),

    # ─── Cross-domain: prompt injection / compliance-override attempts ───
    RiskRule(
        rule_id="prompt_injection_or_compliance_override_attempt",
        version="v1",
        name="Prompt injection / compliance-override attempt",
        description=(
            "User query attempts to instruct the system to ignore policy, "
            "override compliance, or bypass governance. This is a "
            "prohibited prompt-injection pattern; the response must be "
            "blocked at the C3 hard-stop layer regardless of other dims."
        ),
        applies_to_domains=("*",),
        required_term_groups=(_PROMPT_INJECTION_TERMS,),
        effect=RuleEffect(
            c3_violation=True,
            control_class=CONTROL_CLASS_HARD_SAFETY,
            safety_category=SAFETY_PROMPT_INJECTION_PATTERN,
            override_policy="non_overrideable",
            blocking_reason="prohibited_prompt_injection_pattern",
            decision_pressure="STOP",
            explanation=(
                "Query attempts to override compliance / governance rules — "
                "prompt-injection pattern."
            ),
        ),
    ),

    # ─── Financial: restricted instrument recommendation → STOP ──────────
    RiskRule(
        rule_id="restricted_instrument_recommendation",
        version="v1",
        name="Restricted investment instrument recommendation",
        description=(
            "Recommendations for leveraged ETFs, binary options, or "
            "unregistered securities are prohibited per the restricted "
            "instruments policy across all client profiles."
        ),
        applies_to_domains=("financial_services",),
        required_term_groups=(
            ("recommend", "recommendation", "suggest", "advice",
             "buy", "allocate", "invest"),
            _RESTRICTED_INVESTMENT_TERMS,
        ),
        effect=RuleEffect(
            c3_violation=True,
            control_class=CONTROL_CLASS_HARD_SAFETY,
            # Recommending the restricted instrument is the prohibited
            # ACTION; the instrument itself is permitted to discuss in
            # an educational context (which the recommend/buy/allocate
            # term group filters out).
            safety_category=SAFETY_PROHIBITED_ACTION,
            override_policy="policy_exception",
            blocking_reason="restricted_investment_instrument_recommendation",
            decision_pressure="STOP",
            explanation=(
                "Restricted instrument recommendation; prohibited for all "
                "client profiles per the restricted instruments policy."
            ),
        ),
    ),

)
