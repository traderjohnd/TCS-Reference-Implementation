"""
Three-class control model — acceptance tests.

Pins the contract for the taxonomy split:

  Hard deterministic constraints (CONTROL_CLASS_HARD_SAFETY)
    - rule-detectable; firing forces STOP; non-overrideable or
      restricted-override per the rule's override_policy
    - safety_category names the prohibition class (prompt_injection,
      credential, prohibited_action, prohibited_content,
      unauthorized_scope, safety_envelope_violation)

  Deterministic bounded controls (CONTROL_CLASS_DETERMINISTIC_BOUNDED)
    - rule layer flags the *category*; the typed-facts evaluator
      (governed_facts) does the actual envelope check. Today the
      evaluator is a placeholder — the schema is what we pin.
    - typical decision_pressure: HOLD with specialist_review

  Weighted evidence controls (CONTROL_CLASS_WEIGHTED_EVIDENCE)
    - default for rules that only shape BACK/TIS scores

The architectural rule the user pinned: hard safety can be detected
by rules, but bounded device safety must be evaluated against
structured facts. These tests assert the boundary exists in code.
"""

from __future__ import annotations

import pytest

from tcs.governance import (
    CONTROL_CLASS_DETERMINISTIC_BOUNDED,
    CONTROL_CLASS_HARD_SAFETY,
    CONTROL_CLASS_WEIGHTED_EVIDENCE,
    GovernedFacts,
    RiskRule,
    RuleEffect,
    RuleMatch,
    SAFETY_CREDENTIAL_PATTERN,
    SAFETY_ENVELOPE_VIOLATION,
    SAFETY_PROHIBITED_ACTION,
    SAFETY_PROHIBITED_CONTENT,
    SAFETY_PROMPT_INJECTION_PATTERN,
    SAFETY_UNAUTHORIZED_SCOPE,
    SCENARIO_RULES,
    classify_query_risk,
    evaluate_bounded_controls,
    merge_effects,
)


# --------------------------------------------------------------------------- #
# Constants — every constant the user named must exist and be a string        #
# --------------------------------------------------------------------------- #

class TestTaxonomyConstants:
    def test_control_class_constants_present_and_distinct(self):
        values = {
            CONTROL_CLASS_HARD_SAFETY,
            CONTROL_CLASS_DETERMINISTIC_BOUNDED,
            CONTROL_CLASS_WEIGHTED_EVIDENCE,
        }
        assert values == {"hard_safety", "deterministic_bounded", "weighted_evidence"}

    def test_safety_category_constants_present_and_distinct(self):
        values = {
            SAFETY_PROMPT_INJECTION_PATTERN,
            SAFETY_CREDENTIAL_PATTERN,
            SAFETY_PROHIBITED_ACTION,
            SAFETY_PROHIBITED_CONTENT,
            SAFETY_UNAUTHORIZED_SCOPE,
            SAFETY_ENVELOPE_VIOLATION,
        }
        assert values == {
            "prompt_injection_pattern",
            "credential_pattern",
            "prohibited_action",
            "prohibited_content",
            "unauthorized_scope",
            "safety_envelope_violation",
        }


# --------------------------------------------------------------------------- #
# RuleEffect: resolved_* helpers + audit emission                              #
# --------------------------------------------------------------------------- #

class TestRuleEffectResolution:
    def test_safety_category_takes_precedence_over_c3_category(self):
        eff = RuleEffect(
            c3_violation=True,
            control_class=CONTROL_CLASS_HARD_SAFETY,
            safety_category=SAFETY_UNAUTHORIZED_SCOPE,
            c3_category="C3_prohibited_action_pattern",  # legacy, should lose
        )
        assert eff.resolved_safety_category() == "unauthorized_scope"

    def test_legacy_c3_category_is_back_derived_when_safety_unset(self):
        eff = RuleEffect(
            c3_violation=True,
            c3_category="C3_prohibited_action_pattern",
        )
        # Strips the C3_ prefix AND the _pattern suffix.
        assert eff.resolved_safety_category() == "prohibited_action"

    def test_legacy_pattern_suffix_preserved_for_prompt_injection(self):
        # prompt_injection_pattern is one of the values whose canonical
        # form retains "_pattern" — keep that exact mapping.
        eff = RuleEffect(
            c3_violation=True,
            c3_category="C3_prompt_injection_pattern",
        )
        assert eff.resolved_safety_category() == "prompt_injection_pattern"

    def test_control_class_defaults_to_weighted_evidence(self):
        eff = RuleEffect()
        assert eff.resolved_control_class() == CONTROL_CLASS_WEIGHTED_EVIDENCE

    def test_control_class_implied_hard_safety_when_c3_violation_only(self):
        # Back-compat: a rule that sets only c3_violation=True (no
        # control_class) should resolve as hard_safety so legacy
        # rules don't silently slip into weighted_evidence.
        eff = RuleEffect(
            c3_violation=True, c3_category="C3_prohibited_action_pattern",
        )
        assert eff.resolved_control_class() == CONTROL_CLASS_HARD_SAFETY


class TestRuleMatchAuditEmission:
    def _build_match(self, **eff_kwargs) -> RuleMatch:
        return RuleMatch(
            rule_id="test_rule",
            rule_version="v1",
            applies_to_domains=("*",),
            matched_domain="test",
            matched_terms=("term1",),
            effect=RuleEffect(**eff_kwargs),
            matched_facts={"patient_age_group": "neonate"},  # demo binding
        )

    def test_audit_dict_carries_three_class_fields_and_matched_facts(self):
        m = self._build_match(
            c3_violation=True,
            control_class=CONTROL_CLASS_HARD_SAFETY,
            safety_category=SAFETY_ENVELOPE_VIOLATION,
            override_policy="non_overrideable",
            blocking_reason="neonatal_defibrillator_outside_envelope",
        )
        d = m.to_audit_dict()
        eff = d["effect"]
        # New authoritative fields.
        assert eff["control_class"] == "hard_safety"
        assert eff["safety_category"] == "safety_envelope_violation"
        assert eff["override_policy"] == "non_overrideable"
        # matched_facts at the top level (separate from effect).
        assert d["matched_facts"] == {"patient_age_group": "neonate"}
        # Legacy c3_category mirror still emitted.
        assert eff["c3_category"] == "C3_safety_envelope_violation"

    def test_audit_dict_empty_matched_facts_for_term_only_rule(self):
        m = RuleMatch(
            rule_id="term_only",
            rule_version="v1",
            applies_to_domains=("*",),
            matched_domain="test",
            matched_terms=("x",),
            effect=RuleEffect(),
            # default matched_facts
        )
        assert m.to_audit_dict()["matched_facts"] == {}


# --------------------------------------------------------------------------- #
# merge_effects: primary_control_class, primary_safety_category, override     #
# --------------------------------------------------------------------------- #

class TestMergeThreeClassFields:
    def _mk(self, **eff_kwargs) -> RuleMatch:
        return RuleMatch(
            rule_id=f"r{id(eff_kwargs)}",
            rule_version="v1",
            applies_to_domains=("*",),
            matched_domain="test",
            matched_terms=("x",),
            effect=RuleEffect(**eff_kwargs),
        )

    def test_primary_control_class_picks_most_restrictive(self):
        agg = merge_effects([
            self._mk(control_class=CONTROL_CLASS_WEIGHTED_EVIDENCE),
            self._mk(control_class=CONTROL_CLASS_DETERMINISTIC_BOUNDED),
            self._mk(c3_violation=True, safety_category=SAFETY_PROHIBITED_ACTION,
                     control_class=CONTROL_CLASS_HARD_SAFETY),
        ])
        assert agg.primary_control_class == CONTROL_CLASS_HARD_SAFETY

    def test_primary_safety_category_uses_new_priority(self):
        # Credential beats envelope beats prompt_injection beats
        # prohibited_action beats prohibited_content.
        agg = merge_effects([
            self._mk(c3_violation=True, safety_category=SAFETY_PROHIBITED_CONTENT,
                     control_class=CONTROL_CLASS_HARD_SAFETY),
            self._mk(c3_violation=True, safety_category=SAFETY_CREDENTIAL_PATTERN,
                     control_class=CONTROL_CLASS_HARD_SAFETY),
            self._mk(c3_violation=True, safety_category=SAFETY_PROHIBITED_ACTION,
                     control_class=CONTROL_CLASS_HARD_SAFETY),
        ])
        assert agg.primary_safety_category == "credential_pattern"
        assert set(agg.safety_categories) == {
            "prohibited_content", "credential_pattern", "prohibited_action",
        }

    def test_override_policy_most_restrictive_wins(self):
        agg = merge_effects([
            self._mk(override_policy="standard"),
            self._mk(override_policy="specialist_review"),
            self._mk(override_policy="non_overrideable"),
            self._mk(override_policy="policy_exception"),
        ])
        assert agg.override_policy == "non_overrideable"

    def test_weighted_evidence_only_no_safety_categories(self):
        # A pure weighted_evidence rule should not contribute a
        # safety_category even if it accidentally sets one.
        agg = merge_effects([
            self._mk(safety_category=SAFETY_PROHIBITED_ACTION),  # no c3_violation, no hard_safety
        ])
        assert agg.safety_categories == ()
        assert agg.primary_safety_category is None


# --------------------------------------------------------------------------- #
# GovernedFacts schema — foundation for the future bounded-control evaluator  #
# --------------------------------------------------------------------------- #

class TestGovernedFactsSchema:
    def test_all_eight_fields_present(self):
        # The user listed these eight fields explicitly. Pin them.
        f = GovernedFacts()
        for name in (
            "patient_age_group", "patient_weight_kg", "device_class",
            "intended_use", "requester_role", "action_type",
            "setting_requested", "setting_units",
        ):
            assert hasattr(f, name), f"GovernedFacts missing required field {name}"

    def test_default_facts_are_empty(self):
        f = GovernedFacts()
        assert f.is_empty()
        for name in (
            "patient_age_group", "patient_weight_kg", "device_class",
            "intended_use", "requester_role", "action_type",
            "setting_requested", "setting_units",
        ):
            assert getattr(f, name) is None

    def test_populated_facts_are_not_empty(self):
        f = GovernedFacts(patient_age_group="neonate", patient_weight_kg=3.5)
        assert not f.is_empty()

    def test_to_dict_round_trip_shape(self):
        f = GovernedFacts(
            patient_age_group="neonate",
            patient_weight_kg=3.5,
            device_class="external_defibrillator",
            intended_use="device_parameter_setting",
            requester_role="device_operator",
            action_type="parameter_set",
            setting_requested=50.0,
            setting_units="J",
        )
        d = f.to_dict()
        assert d["patient_age_group"] == "neonate"
        assert d["patient_weight_kg"] == 3.5
        assert d["device_class"] == "external_defibrillator"
        assert d["intended_use"] == "device_parameter_setting"
        assert d["requester_role"] == "device_operator"
        assert d["action_type"] == "parameter_set"
        assert d["setting_requested"] == 50.0
        assert d["setting_units"] == "J"


class TestGovernedFactsEvaluatorPlaceholder:
    """
    The full bounded-control evaluator is a follow-up slice. The
    placeholder MUST:
      - exist with the locked signature
      - never raise
      - return [] for any input (no false positives)
    """

    def test_placeholder_returns_no_matches_for_empty_facts(self):
        out = evaluate_bounded_controls(GovernedFacts())
        assert out == []

    def test_placeholder_returns_no_matches_even_for_populated_facts(self):
        # When the full evaluator lands, this assertion flips and a
        # new test pins what it returns. For now: no matches means
        # the rule layer alone governs.
        facts = GovernedFacts(
            patient_age_group="neonate",
            device_class="external_defibrillator",
            intended_use="device_parameter_setting",
            setting_requested=200.0,  # blatantly out of neonatal range
            setting_units="J",
        )
        out = evaluate_bounded_controls(facts, domain="life_sciences")
        assert out == []

    def test_placeholder_does_not_raise_on_partial_facts(self):
        # Robustness: partial fact populations are the common case
        # (different connectors provide different subsets). The
        # placeholder must not raise on any combination.
        for facts in [
            GovernedFacts(),
            GovernedFacts(patient_age_group="adult"),
            GovernedFacts(device_class="infusion_pump"),
            GovernedFacts(intended_use="clinician_patient_specific"),
            GovernedFacts(setting_requested=10.0, setting_units="mg"),
        ]:
            # Just calling it is the test — no exception means pass.
            evaluate_bounded_controls(facts)


# --------------------------------------------------------------------------- #
# Architectural guardrail — no defibrillator-envelope rule allowed             #
# --------------------------------------------------------------------------- #
#
# The user pinned: do NOT model neonatal defibrillator safety envelopes,
# device settings, patient weight, age group, requester role, or
# intended use as keyword-matching problems. Those belong to typed
# facts. This test fails fast if a future contributor adds a term-
# group rule that pretends to evaluate one of those envelopes —
# catching the regression before it can ship.

class TestNoEnvelopeRulesInTermGroupClassifier:
    _FORBIDDEN_ENVELOPE_TERMS = (
        # If any scenario rule uses these as required terms, we're
        # treating a numeric/categorical envelope as keyword
        # matching — which the architecture explicitly forbids.
        "joule", "joules",
        "kg/hr", "mg/kg", "mcg/kg",
        "neonatal energy", "defibrillation energy",
        "infusion rate exceeds", "weight exceeds",
    )

    def test_no_rule_attempts_to_evaluate_a_numeric_envelope_via_keywords(self):
        offenders = []
        for rule in SCENARIO_RULES:
            for group in rule.required_term_groups:
                for term in group:
                    low = term.lower()
                    for forbidden in self._FORBIDDEN_ENVELOPE_TERMS:
                        if forbidden in low:
                            offenders.append((rule.rule_id, term, forbidden))
        assert not offenders, (
            "term-group rules contain envelope-shaped keywords — these "
            "must move to the typed-facts evaluator (governed_facts.py): "
            f"{offenders}"
        )


# --------------------------------------------------------------------------- #
# Lithium three-path behavior at the rule layer                                #
# --------------------------------------------------------------------------- #

class TestLithiumThreePathBehavior:
    def _ids(self, query: str) -> set:
        return {m.rule_id for m in classify_query_risk(
            query=query, domain="life_sciences", rules=list(SCENARIO_RULES),
        )}

    def test_consumer_self_dosing_fires_path_1_with_hard_safety(self):
        ids = self._ids(
            "I'm pregnant and want to know what dose of lithium to take"
        )
        assert "consumer_facing_dosing_during_pregnancy" in ids
        assert "clinician_patient_specific_dosing_during_pregnancy" not in ids

        # Verify the rule's effect carries the correct three-class shape.
        rule = next(
            r for r in SCENARIO_RULES
            if r.rule_id == "consumer_facing_dosing_during_pregnancy"
        )
        eff = rule.effect
        assert eff.resolved_control_class() == CONTROL_CLASS_HARD_SAFETY
        assert eff.resolved_safety_category() == SAFETY_PROHIBITED_ACTION
        assert eff.override_policy == "non_overrideable"
        assert eff.decision_pressure == "STOP"

    def test_clinician_patient_specific_fires_path_2_with_deterministic_bounded(self):
        ids = self._ids(
            "What lithium dose should I prescribe for this pregnant patient?"
        )
        assert "clinician_patient_specific_dosing_during_pregnancy" in ids
        assert "consumer_facing_dosing_during_pregnancy" not in ids

        rule = next(
            r for r in SCENARIO_RULES
            if r.rule_id == "clinician_patient_specific_dosing_during_pregnancy"
        )
        eff = rule.effect
        # Path 2 is the deterministic_bounded category-flag — it
        # holds for specialist review; the typed-facts evaluator
        # (forthcoming) will refine to STOP-if-outside-intended-use.
        assert eff.resolved_control_class() == CONTROL_CLASS_DETERMINISTIC_BOUNDED
        assert eff.override_policy == "specialist_review"
        assert eff.decision_pressure == "HOLD"
        # Not a hard C3 stop — that's the whole point of path 2.
        assert eff.c3_violation is False

    def test_general_guideline_falls_through_no_rule_fires(self):
        # The lithium/general-guideline path is intentionally
        # unhandled by rules — BACK/TIS scoring with source support
        # is the right place for it.
        ids = self._ids(
            "Lithium dosing for refractory bipolar disorder during pregnancy"
        )
        assert "consumer_facing_dosing_during_pregnancy" not in ids
        assert "clinician_patient_specific_dosing_during_pregnancy" not in ids

    def test_consumer_indicator_takes_precedence_over_clinician_context(self):
        # A query with "should I take" AND "patient" should fire the
        # consumer rule (path 1) and the clinician rule must be
        # suppressed via forbidden_terms.
        ids = self._ids(
            "Should I take 600 mg of lithium during pregnancy "
            "for this patient I am treating?"
        )
        assert "consumer_facing_dosing_during_pregnancy" in ids
        assert "clinician_patient_specific_dosing_during_pregnancy" not in ids
