"""
tcs.governance.typed_context_rules
===================================

Phase 5 Slice 5.5a — Human Context Risk Rule Alignment.

A typed-context rule evaluator that runs *alongside* the term-group
``risk_classifier`` and emits the same ``RuleMatch`` shape so the
audit pipeline (governance_rule_matches, matched_facts, control_class,
safety_category, override_policy) is uniform across evaluators.

What this layer IS:

  A small evaluator that combines structured/typed evidence from
  ``recipient_context`` with simple draft-term matching. Detects
  runtime-governance risks the pure term-group classifier cannot
  see — most importantly, a human-authored outbound message giving
  patient-specific medication guidance to a pregnant client/patient.

What this layer is NOT:

  The numeric Deterministic Bounded Control Evaluator. That slice
  is still deferred. Typed-context rules do NOT evaluate numeric
  safety envelopes (defibrillator joules, weight-based dosing
  limits). They evaluate categorical typed facts ("role is patient",
  "pregnant is true", "channel is outbound_message") together with
  draft-term matching.

  An LLM-aware semantic analyzer. The draft is examined via the
  same term-group machinery as the existing classifier.

Architectural rule (pinned, again):

    Hard safety MAY be detected by rules. Bounded device safety
    MUST be evaluated against structured facts and validated
    envelopes — and that envelope evaluator is the NEXT slice.

    Typed-context rules sit between these two: they enrich the
    rule layer with typed-fact predicates so a draft + a recipient
    context together can trigger a category that pure term-group
    matching would miss. The fact predicates here are categorical,
    not numeric.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from tcs.governance.risk_classifier import (
    CONTROL_CLASS_DETERMINISTIC_BOUNDED,
    SAFETY_PROHIBITED_ACTION,
    RuleEffect,
    RuleMatch,
    _contains_term,
    _first_matching_term,
    _rule_applies_to_domain,
)


# --------------------------------------------------------------------------- #
# Schema                                                                       #
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class TypedContextRule:
    """
    A rule that combines typed-fact predicates over recipient_context
    with draft-term matching.

    Match logic (all conditions must hold):

      1. generation_mode is in ``applies_to_generation_modes`` (or
         the rule's list contains "*").
      2. domain is in ``applies_to_domains`` (segment-matched the
         same way as RiskRule.applies_to_domains).
      3. Every (key, predicate) in ``fact_predicates`` is satisfied
         by ``recipient_context[key]``. Predicate types:
           - tuple/list  → value must be in the set
           - bool/int/float/str → value must equal the predicate
      4. Every group in ``draft_term_groups`` has at least one
         term present in the draft text (same case-insensitive
         word-boundary matching as the term-group classifier).
    """
    rule_id: str
    version: str
    name: str
    description: str
    applies_to_generation_modes: Tuple[str, ...]
    applies_to_domains: Tuple[str, ...]
    fact_predicates: Dict[str, Any] = field(default_factory=dict)
    draft_term_groups: Tuple[Tuple[str, ...], ...] = field(default_factory=tuple)
    effect: RuleEffect = field(default_factory=RuleEffect)


# --------------------------------------------------------------------------- #
# Term vocabularies (shared with scenario_rules but kept local for clarity)   #
# --------------------------------------------------------------------------- #

# Mirrored from scenario_rules._MEDICATION_TERMS so the typed-context
# rule can recognize medication mentions without coupling import order.
_MEDICATION_TERMS: Tuple[str, ...] = (
    "medication", "medications", "drug", "drugs", "med", "meds",
    "rx", "prescription", "prescribe", "prescribing", "pharmaceutical",
    "warfarin", "lithium", "morphine", "oxycodone", "fentanyl",
    "methotrexate", "digoxin", "phenytoin", "carbamazepine",
    "amiodarone", "clozapine", "tacrolimus", "methadone",
    "valproate", "valproic",
)

# Dosing / reassurance / treatment / safety-advice language.
# Deliberately broader than the term-group classifier's _DOSING_TERMS
# because the typed-context rule fires on outbound-to-consumer
# advice, where the surface forms are different ("fine in small
# doses" rather than "should I take 600 mg").
_DOSING_OR_ADVICE_TERMS: Tuple[str, ...] = (
    "dose", "dosing", "dosage", "dosages",
    "mg", "mcg", "milligram", "microgram", "ml",
    "take", "taking",
    "prescribe", "prescribing", "prescription",
    "small doses", "low doses", "high doses",
    "is fine", "is safe", "is ok", "is okay",
    "no worries", "no problem",
    "treatment", "monitor",
    "adjust", "adjustment", "increase", "decrease",
    "raise", "reduce",
    "give", "administer",
)


# --------------------------------------------------------------------------- #
# Starter ruleset                                                              #
# --------------------------------------------------------------------------- #

TYPED_CONTEXT_RULES: Tuple[TypedContextRule, ...] = (

    # ─── Patient-specific medication guidance to a pregnant recipient ─────
    # The flagship Phase-5 case: a human-authored outbound message
    # giving medication dosing / reassurance / treatment advice to
    # a pregnant patient/client/consumer. Fires HOLD for specialist
    # (maternal-fetal medicine / clinician) review. Override policy
    # is specialist_review, not non_overrideable — a clinician may
    # legitimately need to send such a message after specialist
    # consultation; the rule ensures that consultation happens
    # before delivery.
    #
    # Critically, this rule fires on the COMBINATION of:
    #   recipient_context typed facts (role, channel, pregnant)
    # AND
    #   draft text mentioning a medication AND dosing/advice language.
    # Neither half alone fires it. That's what makes it precise
    # enough to be a non-overrideable signal for an audit story
    # while still being categorical (not numeric).
    TypedContextRule(
        rule_id="human_composed_patient_specific_medication_in_pregnancy",
        version="v1",
        name=(
            "Human-composed patient-specific medication guidance "
            "during pregnancy"
        ),
        description=(
            "Runtime governance rule for human-authored outbound "
            "messages that provide patient-specific medication dosing, "
            "reassurance, or treatment guidance to a pregnant client/"
            "patient. Maternal-fetal medicine (MFM) / specialist "
            "review is required before delivery, regardless of who "
            "drafted the message. The rule combines recipient_context "
            "typed facts (role, channel, pregnant) with draft-text "
            "matching on medication + dosing/advice language."
        ),
        applies_to_generation_modes=("human_composed",),
        applies_to_domains=("life_sciences", "healthcare", "*"),
        fact_predicates={
            "pregnant": True,
            "role": ("patient", "client", "consumer"),
            "channel": (
                "outbound_message", "email", "sms", "chat_message",
                "letter", "portal_message",
            ),
        },
        draft_term_groups=(
            _MEDICATION_TERMS,
            _DOSING_OR_ADVICE_TERMS,
        ),
        effect=RuleEffect(
            # control_class = deterministic_bounded — the rule
            # recognizes a category that the future numeric
            # bounded-control evaluator will refine. For now we hold
            # for specialist review rather than hard-stop.
            control_class=CONTROL_CLASS_DETERMINISTIC_BOUNDED,
            # safety_category is set on the effect for audit even
            # though the merge logic surfaces it primarily for
            # c3_violation / hard_safety rules. Reviewers reading
            # governance_rule_matches[*].effect.safety_category see
            # the prohibition class directly.
            safety_category=SAFETY_PROHIBITED_ACTION,
            override_policy="specialist_review",
            blocking_reason=(
                "patient_specific_medication_guidance_during_pregnancy"
            ),
            requires_human_review=True,
            decision_pressure="HOLD",
            explanation=(
                "Patient-specific medication guidance during pregnancy "
                "in an outbound human-authored message requires "
                "maternal-fetal medicine / specialist review before "
                "delivery. The recipient context indicates a pregnant "
                "patient/client receiving outbound communication; the "
                "draft references medication and dosing/treatment "
                "advice. This is not a numeric safety-envelope check "
                "(that evaluator is the next slice) — it is the "
                "categorical typed-context guardrail that catches the "
                "rep-to-pregnant-patient pattern the term-group "
                "classifier cannot see on its own."
            ),
        ),
    ),

)


# --------------------------------------------------------------------------- #
# Predicate matching                                                           #
# --------------------------------------------------------------------------- #

def _predicate_holds(value: Any, predicate: Any) -> bool:
    """
    Match a typed-fact value against a predicate.

    - tuple/list predicates: membership ("value in this set").
    - any other type: equality.

    Returns False when value is missing (None) — required facts must
    actually be present in recipient_context to fire the rule. A
    None value is "this fact wasn't provided", not "this fact is
    explicitly None."
    """
    if value is None:
        return False
    if isinstance(predicate, (tuple, list, set, frozenset)):
        return value in predicate
    return value == predicate


def _all_predicates_hold(
    recipient_context: Optional[Dict[str, Any]],
    predicates: Dict[str, Any],
) -> Tuple[bool, Dict[str, Any]]:
    """
    Evaluate every predicate. Returns ``(all_match, matched_facts)``.

    ``matched_facts`` is the dict of (key → value) bindings that
    satisfied the predicates. Used as ``RuleMatch.matched_facts`` so
    the audit shows exactly which typed facts contributed.
    """
    if not predicates:
        return True, {}
    rc = recipient_context or {}
    matched: Dict[str, Any] = {}
    for key, pred in predicates.items():
        v = rc.get(key)
        if not _predicate_holds(v, pred):
            return False, {}
        matched[key] = v
    return True, matched


def _generation_mode_applies(rule: TypedContextRule, mode: Optional[str]) -> bool:
    if "*" in rule.applies_to_generation_modes:
        return True
    if mode is None:
        return False
    return mode in rule.applies_to_generation_modes


# --------------------------------------------------------------------------- #
# Public entry point                                                           #
# --------------------------------------------------------------------------- #

def evaluate_typed_context_rules(
    *,
    generation_mode: Optional[str],
    recipient_context: Optional[Dict[str, Any]],
    draft_text: str,
    domain: Optional[str],
    rules: Optional[Tuple[TypedContextRule, ...]] = None,
) -> List[RuleMatch]:
    """
    Run typed-context rules and return matching RuleMatch objects.

    The returned RuleMatch objects use the same shape as the term-
    group classifier so the GCA can merge them into a single
    governance_rule_matches audit list. ``matched_facts`` is
    populated from the recipient_context bindings that satisfied
    the rule's fact_predicates; ``matched_terms`` carries the
    draft-term matches one per draft_term_group.

    A rule fires only when ALL of:
      - generation_mode applies
      - domain applies
      - every fact_predicate is satisfied
      - every draft_term_group has at least one match in draft_text
    """
    matches: List[RuleMatch] = []
    rule_set = rules if rules is not None else TYPED_CONTEXT_RULES
    text_lower = (draft_text or "").lower()

    for rule in rule_set:
        if not _generation_mode_applies(rule, generation_mode):
            continue
        # Reuse the existing domain-scope helper (handles "*",
        # composed-pack segment matching, etc.).
        if not _rule_applies_to_domain(rule, domain):  # type: ignore[arg-type]
            continue

        facts_ok, matched_facts = _all_predicates_hold(
            recipient_context, rule.fact_predicates,
        )
        if not facts_ok:
            continue

        # Draft term groups — each group must have one matching term.
        matched_per_group: List[str] = []
        all_groups_matched = True
        for group in rule.draft_term_groups:
            hit = _first_matching_term(text_lower, group)
            if hit is None:
                all_groups_matched = False
                break
            matched_per_group.append(hit)
        if not all_groups_matched:
            continue

        matches.append(RuleMatch(
            rule_id=rule.rule_id,
            rule_version=rule.version,
            applies_to_domains=rule.applies_to_domains,
            matched_domain=domain,
            matched_terms=tuple(matched_per_group),
            effect=rule.effect,
            matched_facts=dict(matched_facts),
        ))

    return matches


__all__ = [
    "TypedContextRule",
    "TYPED_CONTEXT_RULES",
    "evaluate_typed_context_rules",
]
