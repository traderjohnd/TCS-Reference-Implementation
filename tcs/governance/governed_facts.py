"""
tcs.governance.governed_facts
==============================

Typed-facts schema for the **deterministic bounded controls**
evaluator. Schema lives here now; the full evaluator is staged as a
follow-up slice.

Why this file exists today (even though the evaluator does not):

  The three-class control model splits guardrails into hard_safety
  (rule-detectable), deterministic_bounded (structured-fact
  evaluable), and weighted_evidence (BACK/TIS). Hard safety can be
  detected by rules; bounded device safety must be evaluated against
  structured facts. The term-group classifier is fundamentally the
  wrong tool for "is this neonatal defibrillator setting within the
  validated 1-10 J range" — that's a typed numeric range check
  against typed inputs, not a keyword match.

  By landing the GovernedFacts schema now, we:
    (a) make the boundary between rule-detectable and fact-evaluable
        controls visible in code,
    (b) let connectors / GCA start populating these fields
        opportunistically, and
    (c) ensure the future bounded-control evaluator plugs into a
        stable shape rather than becoming a bolt-on retrofit.

  The placeholder ``evaluate_bounded_controls`` returns no matches.
  It does NOT raise: a deployment that never populates GovernedFacts
  (because no connector emits them yet) is valid and unaffected.

Architectural rule (from the user, pinned in
risk_classifier.py docstring): hard safety MAY be detected by rules
when the violation is a prompt-risk pattern. BUT bounded device
safety MUST be evaluated against structured facts and validated
envelopes. The lithium consumer-self-dosing rule is fine in the
term-group classifier (prompt-risk pattern). A neonatal
defibrillator energy-envelope check is not — it requires
patient_age_group + device_class + setting_requested + setting_units
+ a validated range, and that's exactly what this module exists
for. Never write a term-group rule that pretends to evaluate a
numeric envelope. If a control needs typed inputs, it goes here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from tcs.governance.risk_classifier import RuleMatch


# --------------------------------------------------------------------------- #
# Typed-fact vocabulary                                                        #
# --------------------------------------------------------------------------- #
#
# These constants name allowed values for the categorical GovernedFacts
# fields. They are guidance, not enforcement — the dataclass accepts
# any string so a deployment can extend the taxonomy. Constants here
# pin the values the placeholder + future evaluator and the rule
# library understand without ambiguity.

# patient_age_group
AGE_GROUP_NEONATE       = "neonate"        # 0–28 days
AGE_GROUP_INFANT        = "infant"         # 1–12 months
AGE_GROUP_TODDLER       = "toddler"        # 1–3 years
AGE_GROUP_CHILD         = "child"          # 4–11 years
AGE_GROUP_ADOLESCENT    = "adolescent"     # 12–17 years
AGE_GROUP_ADULT         = "adult"          # 18–64 years
AGE_GROUP_GERIATRIC     = "geriatric"      # 65+

# device_class — illustrative; deployments extend as needed.
DEVICE_CLASS_EXTERNAL_DEFIBRILLATOR = "external_defibrillator"
DEVICE_CLASS_INFUSION_PUMP          = "infusion_pump"
DEVICE_CLASS_VENTILATOR             = "ventilator"
DEVICE_CLASS_INSULIN_PUMP           = "insulin_pump"

# intended_use — coarse categorization of what the AI output is for.
# Distinguishes the lithium-rule paths (general guideline vs
# patient-specific clinician dosing vs consumer-facing self-dosing).
INTENDED_USE_CONSUMER_SELF_GUIDANCE         = "consumer_self_guidance"
INTENDED_USE_CLINICIAN_GENERAL_GUIDELINE    = "clinician_general_guideline"
INTENDED_USE_CLINICIAN_PATIENT_SPECIFIC     = "clinician_patient_specific"
INTENDED_USE_DEVICE_PARAMETER_SETTING       = "device_parameter_setting"

# requester_role — authorization context of who is asking.
REQUESTER_ROLE_PATIENT                  = "patient"
REQUESTER_ROLE_CAREGIVER                = "caregiver"
REQUESTER_ROLE_LICENSED_CLINICIAN       = "licensed_clinician"
REQUESTER_ROLE_SPECIALIST_CLINICIAN     = "specialist_clinician"
REQUESTER_ROLE_DEVICE_OPERATOR          = "device_operator"
REQUESTER_ROLE_UNAUTHENTICATED          = "unauthenticated"

# action_type — what the AI is being asked to produce.
ACTION_TYPE_INFORMATIONAL               = "informational"
ACTION_TYPE_RECOMMENDATION              = "recommendation"
ACTION_TYPE_PARAMETER_SET               = "parameter_set"
ACTION_TYPE_AUTHORIZATION               = "authorization"


# --------------------------------------------------------------------------- #
# GovernedFacts                                                                #
# --------------------------------------------------------------------------- #

@dataclass
class GovernedFacts:
    """
    Structured facts the bounded-control evaluator will use to judge a
    safety envelope or operating-range constraint.

    All fields default to None. A connector / GCA that has confident
    typed evidence for a field populates it; everything left None
    represents "the system does not (yet) know." The evaluator MUST
    treat None as missing evidence, not as a permissive default.

    Fields:
      patient_age_group   — categorical (AGE_GROUP_*); used by
                            pediatric/neonatal envelopes.
      patient_weight_kg   — numeric kg; used by weight-based dosing
                            and energy-per-kg envelopes.
      device_class        — categorical (DEVICE_CLASS_*); pins which
                            validated envelope table to consult.
      intended_use        — categorical (INTENDED_USE_*); separates
                            consumer self-guidance, clinician general
                            guideline, clinician patient-specific
                            recommendation, and device parameter
                            setting (the four paths the lithium and
                            defibrillator examples share).
      requester_role      — categorical (REQUESTER_ROLE_*); used by
                            authorization-scope controls.
      action_type         — categorical (ACTION_TYPE_*); what the AI
                            is being asked to produce.
      setting_requested   — numeric value the AI is being asked to
                            recommend or set (e.g. 50 for "50 joules";
                            5.0 for "5.0 mg/kg/hr"). Combined with
                            ``setting_units`` to form a typed quantity
                            the evaluator can range-check.
      setting_units       — free-form unit label ("J", "mg", "mg/kg",
                            "mL/hr"). Evaluator matches against
                            envelope-table units; mismatch → reject as
                            incomparable rather than coerce.
    """
    patient_age_group: Optional[str] = None
    patient_weight_kg: Optional[float] = None
    device_class: Optional[str] = None
    intended_use: Optional[str] = None
    requester_role: Optional[str] = None
    action_type: Optional[str] = None
    setting_requested: Optional[float] = None
    setting_units: Optional[str] = None

    def is_empty(self) -> bool:
        """True iff no field is populated. The evaluator skips empty facts."""
        return all(
            getattr(self, f) is None
            for f in (
                "patient_age_group", "patient_weight_kg", "device_class",
                "intended_use", "requester_role", "action_type",
                "setting_requested", "setting_units",
            )
        )

    def to_dict(self) -> Dict[str, Any]:
        """Stable JSON-serializable shape for audit emission."""
        return {
            "patient_age_group": self.patient_age_group,
            "patient_weight_kg": self.patient_weight_kg,
            "device_class": self.device_class,
            "intended_use": self.intended_use,
            "requester_role": self.requester_role,
            "action_type": self.action_type,
            "setting_requested": self.setting_requested,
            "setting_units": self.setting_units,
        }


# --------------------------------------------------------------------------- #
# Placeholder evaluator                                                        #
# --------------------------------------------------------------------------- #

def evaluate_bounded_controls(
    facts: GovernedFacts,
    *,
    domain: Optional[str] = None,
) -> List[RuleMatch]:
    """
    Placeholder for the deterministic bounded-controls evaluator.

    Returns an empty list. The real evaluator (follow-up slice) will
    compare ``facts`` against a validated-envelope table keyed on
    (device_class, patient_age_group, intended_use) and emit
    RuleMatch entries with:

        control_class   = CONTROL_CLASS_DETERMINISTIC_BOUNDED
        safety_category = SAFETY_ENVELOPE_VIOLATION (or
                          SAFETY_UNAUTHORIZED_SCOPE, etc.)
        matched_facts   = {field_name: value, ...,
                           "validated_range": [lo, hi],
                           "envelope_id": "..."}

    Today the function is a no-op so the GCA can call it without
    branching: when typed facts aren't populated, no envelope matches
    are emitted, and the rule classifier handles everything it can on
    its own (which is the lithium-rule case, not the defibrillator
    case).

    The signature is locked here so the future evaluator drops in
    without changing call sites.
    """
    _ = (facts, domain)  # placeholder body — accepts the API, returns []
    return []
