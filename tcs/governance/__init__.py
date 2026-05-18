"""
tcs.governance — Risk Classification + Scenario Rules
=====================================================

A deterministic, domain-scoped policy-trigger layer that classifies
user queries against governance risk patterns and emits BACK signals
(c3_violation, review_required, novelty_lift, etc.) that flow into the
standard GCA / TIS pipeline.

This is NOT a semantic-understanding layer. It is structured term-group
matching: each rule declares one or more term groups, and a query fires
the rule iff it contains at least one term from EACH group (and none
of the forbidden terms). False negatives on phrasings outside the
configured vocabularies are expected and managed by extending the
term groups in scenario_rules.py.

The principle: **sample prompts are allowed; sample answers are not.**

Governance outcomes (Allow / Hold / Stop / Escalate) come from these
structured rule patterns — not from canned LLM response text. Variant
prompts that express the same configured risk pattern produce the same
governance outcome.

For example, this rule:

    {
      "rule_id": "clinical_medication_dosing_pregnancy",
      "applies_to_domains": ["life_sciences"],
      "required_term_groups": [
        ["medication", "drug", "med", "rx", "prescription",
         "lithium", "warfarin", "morphine", ...],
        ["dose", "dosing", "dosage", "mg", "titrate",
         "prescribe", "increase", "decrease", "adjust", "give"],
        ["pregnan", "gestational", "maternal", "trimester"]
      ],
      "effect": {"c3_violation": True,
                 "blocking_reason": "prohibited_clinical_dosing_during_pregnancy"}
    }

...matches all of:

    "Lithium dosing for refractory bipolar disorder during pregnancy"
    "What lithium dose should I use for a pregnant patient?"
    "Can I increase lithium during pregnancy?"
    "How should I prescribe morphine to a pregnant patient with chronic pain?"

and produces the same governance signal in every case.

See:
    risk_classifier.py — rule schema + pure-function classifier
    scenario_rules.py  — declarative starter rules
"""

from __future__ import annotations

from tcs.governance.governed_facts import (
    GovernedFacts,
    evaluate_bounded_controls,
)
from tcs.governance.risk_classifier import (
    # Three-class control model
    CONTROL_CLASS_DETERMINISTIC_BOUNDED,
    CONTROL_CLASS_HARD_SAFETY,
    CONTROL_CLASS_WEIGHTED_EVIDENCE,
    # Safety / C3 category taxonomy — new authoritative names
    SAFETY_CREDENTIAL_PATTERN,
    SAFETY_ENVELOPE_VIOLATION,
    SAFETY_PROHIBITED_ACTION,
    SAFETY_PROHIBITED_CONTENT,
    SAFETY_PROMPT_INJECTION_PATTERN,
    SAFETY_UNAUTHORIZED_SCOPE,
    # Legacy C3 aliases (deprecated)
    C3_CREDENTIAL_PATTERN,
    C3_PROHIBITED_ACTION_PATTERN,
    C3_PROHIBITED_CONTENT_PATTERN,
    C3_PROMPT_INJECTION_PATTERN,
    MergedEffect,
    RiskRule,
    RuleEffect,
    RuleMatch,
    classify_query_risk,
    merge_effects,
)
from tcs.governance.scenario_rules import SCENARIO_RULES

__all__ = [
    # Schema
    "RiskRule",
    "RuleEffect",
    "RuleMatch",
    "MergedEffect",
    # Three-class control model
    "CONTROL_CLASS_HARD_SAFETY",
    "CONTROL_CLASS_DETERMINISTIC_BOUNDED",
    "CONTROL_CLASS_WEIGHTED_EVIDENCE",
    # Safety category taxonomy (new authoritative)
    "SAFETY_PROMPT_INJECTION_PATTERN",
    "SAFETY_CREDENTIAL_PATTERN",
    "SAFETY_PROHIBITED_ACTION",
    "SAFETY_PROHIBITED_CONTENT",
    "SAFETY_UNAUTHORIZED_SCOPE",
    "SAFETY_ENVELOPE_VIOLATION",
    # Legacy C3 category aliases (deprecated)
    "C3_PROMPT_INJECTION_PATTERN",
    "C3_CREDENTIAL_PATTERN",
    "C3_PROHIBITED_ACTION_PATTERN",
    "C3_PROHIBITED_CONTENT_PATTERN",
    # Engine
    "classify_query_risk",
    "merge_effects",
    # Rules
    "SCENARIO_RULES",
    # Typed-facts foundation (schema only; evaluator is a follow-up slice)
    "GovernedFacts",
    "evaluate_bounded_controls",
]
