"""
tcs.governance.risk_classifier
==============================

Deterministic, domain-scoped rule engine for query-level risk
classification. **Not** an NLP / semantic understanding layer — this
is structured term-group matching. Variants that use the rule's
configured term vocabularies will match; variants that use unfamiliar
phrasing, abbreviations, or domain-specific synonyms outside the
configured groups will not. False negatives are expected and managed
by adding terms to the appropriate groups in scenario_rules.py.

THREE-CLASS CONTROL MODEL
-------------------------

Every rule declares a ``control_class`` so the audit (and the decision
ladder) can distinguish what kind of guardrail just fired:

  hard_safety
      A non-negotiable safety or policy boundary. Violation forces
      STOP. May be non-overrideable or restricted-override depending
      on the rule's ``override_policy``. Detectable by the rule layer
      because the violation is expressible as a term-group pattern
      (prompt injection, credential exposure, consumer-facing
      patient-specific prescribing under prohibited indications).

  deterministic_bounded
      A formulaic but context-specific control: patient weight, age
      group, device class, role authorization, validated operating
      range. These belong in ``tcs.governance.governed_facts`` and
      its (forthcoming) bounded-control evaluator. They CANNOT be
      correctly judged from keywords. The rule layer may flag a
      *category* (e.g. "patient-specific clinician dosing during
      pregnancy" → HOLD for specialist review) but the actual
      envelope check belongs to the typed-facts evaluator.

  weighted_evidence
      An evidence-quality signal that feeds the BACK/TIS composite:
      attribution gaps, calibration concerns, policy completeness,
      novelty. These never short-circuit the decision; they shift
      dimension scores or penalty inputs and let the engine
      arbitrate.

The principle the user pinned: **hard safety overrides scoring; TIS
governs evidence quality and policy sufficiency; the Trust
Certificate records both.**

Each rule declares:
  - rule_id + version (so audit trails can identify exactly which
    rule and which version of its term groups fired)
  - which domains it applies to (or "*" for all)
  - one or more "term groups" — the query must contain at least one
    term from EACH group for the rule to fire (case-insensitive,
    whole-token aware via lightweight word-boundary matching)
  - optional forbidden terms — if any are present, the rule does NOT
    fire even if the required groups all match (used to suppress
    false positives, e.g. an educational meta-question about a
    prohibited topic)
  - a RuleEffect describing the governance signals to emit, including
    control_class, safety_category, override_policy, and (for
    backward compat) the legacy c3_category

The classifier returns RuleMatch objects carrying enough information
for the GCA to record full audit evidence on the Trust Certificate.

This module has zero dependencies on the trace, GCA, or engine layers.
It is exercised both by unit tests (with hand-written queries) and by
the GCA (which feeds the live query at trace-assembly time).

What this layer IS:
    A deterministic, domain-scoped governance rule engine that
    generalizes across prompt variants through structured term
    groups. Strong and defensible because every match is explainable
    via the rule_id, rule_version, and matched terms.

What this layer is NOT:
    A semantic-understanding / NLP layer. It does not infer intent
    beyond the configured vocabularies. False negatives on unfamiliar
    phrasings are expected and surfaced by adding to term groups.

    A device-safety envelope evaluator. Numerical safety envelopes
    (neonatal defibrillator energy ranges, weight-based dosing
    limits, role-authorization gates) belong to the typed-facts
    evaluator in tcs.governance.governed_facts, NOT to this term-
    group classifier.

    The architectural rule, stated precisely:

        Hard safety MAY be detected by rules when the violation is
        expressible as a prompt-risk pattern (prompt injection,
        credential exposure, consumer-facing patient-specific
        prescribing under prohibited indications). BUT bounded
        device safety MUST be evaluated against structured facts
        and validated envelopes. A term-group rule that pretends
        to evaluate a numeric envelope is the wrong answer, and
        tests/test_three_class_controls.py fails the build if one
        sneaks in.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple


# --------------------------------------------------------------------------- #
# Data classes                                                                 #
# --------------------------------------------------------------------------- #

# --------------------------------------------------------------------------- #
# Control-class constants (three-class model)                                  #
# --------------------------------------------------------------------------- #

CONTROL_CLASS_HARD_SAFETY           = "hard_safety"
CONTROL_CLASS_DETERMINISTIC_BOUNDED = "deterministic_bounded"
CONTROL_CLASS_WEIGHTED_EVIDENCE     = "weighted_evidence"

_CONTROL_CLASS_PRIORITY = {
    CONTROL_CLASS_HARD_SAFETY:           3,
    CONTROL_CLASS_DETERMINISTIC_BOUNDED: 2,
    CONTROL_CLASS_WEIGHTED_EVIDENCE:     1,
}


# --------------------------------------------------------------------------- #
# Safety category constants — the new authoritative taxonomy                   #
# --------------------------------------------------------------------------- #
#
# Naming rule: the safety_category is the bare canonical name. The legacy
# c3_category (kept for back-compat in the audit dict) is "C3_" + the
# safety_category, sometimes with a "_pattern" suffix for the values that
# pre-dated this split.
#
# A rule's safety_category names the prohibition or guardrail class that
# fired. Adding new categories is cheap: declare the string here and
# document its semantics.

SAFETY_PROMPT_INJECTION_PATTERN  = "prompt_injection_pattern"
SAFETY_CREDENTIAL_PATTERN        = "credential_pattern"
SAFETY_PROHIBITED_ACTION         = "prohibited_action"
SAFETY_PROHIBITED_CONTENT        = "prohibited_content"
SAFETY_UNAUTHORIZED_SCOPE        = "unauthorized_scope"
SAFETY_ENVELOPE_VIOLATION        = "safety_envelope_violation"

# Priority for picking the dominant safety_category when multiple
# fired in one evaluation. Most safety-relevant first. Credential
# exposure is irrecoverable, so it ranks highest; safety_envelope
# violation is a real harm-prevention boundary, so it ranks above
# generic prohibited_action.
_SAFETY_CATEGORY_PRIORITY = {
    SAFETY_CREDENTIAL_PATTERN:        6,
    SAFETY_PROMPT_INJECTION_PATTERN:  5,
    SAFETY_ENVELOPE_VIOLATION:        4,
    SAFETY_UNAUTHORIZED_SCOPE:        3,
    SAFETY_PROHIBITED_ACTION:         2,
    SAFETY_PROHIBITED_CONTENT:        1,
}


def _c3_category_from_safety_category(safety_category: Optional[str]) -> Optional[str]:
    """
    Compute the legacy c3_category string from a new safety_category.

    The legacy strings carry a ``C3_`` prefix and the older values
    (prompt_injection / credential / prohibited_action /
    prohibited_content) also carry a ``_pattern`` suffix. The newer
    values (unauthorized_scope, safety_envelope_violation) do not
    take the suffix — there is no legacy precedent to mirror.
    """
    if not safety_category:
        return None
    legacy_with_pattern_suffix = {
        SAFETY_PROMPT_INJECTION_PATTERN,
        SAFETY_CREDENTIAL_PATTERN,
        SAFETY_PROHIBITED_ACTION,
        SAFETY_PROHIBITED_CONTENT,
    }
    if safety_category in legacy_with_pattern_suffix:
        # SAFETY_PROHIBITED_ACTION is already "prohibited_action"; the
        # legacy form is "C3_prohibited_action_pattern". For the values
        # that already end in "_pattern" (prompt_injection_pattern,
        # credential_pattern), we just prefix.
        if safety_category.endswith("_pattern"):
            return f"C3_{safety_category}"
        return f"C3_{safety_category}_pattern"
    return f"C3_{safety_category}"


# ---- Legacy aliases (deprecated — use the SAFETY_* constants above) ------ #
# Kept so older importers continue to work. New code should use the
# SAFETY_* constants directly. The string VALUES are still the old
# "C3_..._pattern" form so any code that compares the legacy
# c3_category field still gets the value it expects.
C3_PROMPT_INJECTION_PATTERN   = "C3_prompt_injection_pattern"
C3_CREDENTIAL_PATTERN         = "C3_credential_pattern"
C3_PROHIBITED_ACTION_PATTERN  = "C3_prohibited_action_pattern"
C3_PROHIBITED_CONTENT_PATTERN = "C3_prohibited_content_pattern"


@dataclass(frozen=True)
class RuleEffect:
    """
    Governance signals a matched rule emits into the GCA.

    All fields are optional. The GCA aggregates effects across all
    matched rules (most restrictive wins for c3_violation; numeric
    penalties sum and clamp at 1.0).

    Three-class model fields:

      control_class
          Which guardrail class this rule represents.
          Defaults to CONTROL_CLASS_WEIGHTED_EVIDENCE — i.e. the rule
          is treated as evidence shaping the BACK/TIS composite, not
          as a hard guardrail. Rules that should force STOP set
          CONTROL_CLASS_HARD_SAFETY (and typically c3_violation=True).
          Rules that flag a category the typed-facts evaluator should
          confirm set CONTROL_CLASS_DETERMINISTIC_BOUNDED (and
          typically a HOLD decision_pressure).

      safety_category
          The new authoritative taxonomy of guardrail categories
          (prompt_injection_pattern, credential_pattern,
          prohibited_action, prohibited_content, unauthorized_scope,
          safety_envelope_violation). Required when control_class is
          hard_safety. Set by a rule directly, or auto-derived from
          the legacy c3_category if only the latter is provided.

      override_policy
          How a human is permitted to override this rule, if at all.
          Free-form string for now; documented values include
          "non_overrideable", "co_authorizer_required",
          "specialist_review", "policy_exception", "standard".
          Recorded on the TC audit so reviewers see exactly which
          override rule applies to a given rule firing.

    Legacy:
      c3_category
          The pre-split name. Still emitted in the audit dict for
          back-compat. New rules should set safety_category instead;
          this field is derived automatically.
    """
    c3_violation: bool = False
    c3_category: Optional[str] = None  # legacy; prefer safety_category
    safety_category: Optional[str] = None
    control_class: Optional[str] = None  # one of CONTROL_CLASS_*
    override_policy: Optional[str] = None
    blocking_reason: Optional[str] = None
    requires_human_review: bool = False
    # Numeric penalties (0..1). The GCA reduces the corresponding BACK
    # dimension's score_contribution by this amount.
    boundedness_penalty: float = 0.0
    attribution_penalty: float = 0.0
    known_calibration_penalty: float = 0.0
    novelty_lift: float = 0.0
    # Hint to the decision narrative — not a hard override of the
    # decision ladder. The engine's gates + thresholds remain
    # authoritative.
    decision_pressure: Optional[str] = None  # "STOP" | "HOLD" | "ESCALATE"
    explanation: str = ""

    def resolved_safety_category(self) -> Optional[str]:
        """
        Return the effective safety_category for audit / merge use.

        Priority:
          1. ``safety_category`` if the rule sets it.
          2. Derive from the legacy ``c3_category`` if set
             ("C3_prohibited_action_pattern" → "prohibited_action").
          3. None.
        """
        if self.safety_category:
            return self.safety_category
        if self.c3_category:
            base = self.c3_category
            if base.startswith("C3_"):
                base = base[3:]
            if base.endswith("_pattern") and base not in (
                "prompt_injection_pattern", "credential_pattern",
            ):
                base = base[: -len("_pattern")]
            return base
        return None

    def resolved_control_class(self) -> str:
        """
        Return the effective control_class for audit / merge use.

        Default rule: if a rule sets c3_violation but no
        control_class, treat it as hard_safety (the historical
        behavior — c3_violation = hard stop).
        """
        if self.control_class:
            return self.control_class
        if self.c3_violation:
            return CONTROL_CLASS_HARD_SAFETY
        return CONTROL_CLASS_WEIGHTED_EVIDENCE


@dataclass(frozen=True)
class RiskRule:
    """
    A deterministic governance rule.

    ``version`` is mandatory. A future Trust Certificate must be able
    to identify which version of a rule was active when it fired —
    rule term groups will evolve over time (terms added to catch
    additional variants, terms removed for false positives).
    """
    rule_id: str
    version: str                                              # e.g. "v1"
    name: str
    description: str
    applies_to_domains: Tuple[str, ...]                       # ("life_sciences",) or ("*",)
    required_term_groups: Tuple[Tuple[str, ...], ...]         # every group must match
    forbidden_terms: Tuple[str, ...] = ()
    effect: RuleEffect = field(default_factory=RuleEffect)


@dataclass(frozen=True)
class RuleMatch:
    """
    A single rule's match against a query.

    Carries the full audit evidence the GCA records on the Trust
    Certificate: which rule (id + version), which terms matched
    inside each required group, the effect that was applied, and
    (where the typed-facts evaluator participated) the structured
    facts that contributed to the match.

    matched_facts
        Structured fact bindings that supported the match. For pure
        term-group rules this is an empty dict. The
        deterministic_bounded evaluator
        (tcs.governance.governed_facts.evaluate_bounded_controls,
        forthcoming) populates this dict with the typed values it
        evaluated against — e.g. ``{"patient_age_group": "neonate",
        "device_class": "external_defibrillator", "setting_requested":
        50, "setting_units": "J", "validated_range": [1, 10]}``. The
        TC audit surfaces it under the same key.
    """
    rule_id: str
    rule_version: str
    applies_to_domains: Tuple[str, ...]
    matched_domain: Optional[str]                 # the active domain at match time
    # matched_terms is one matched term per required group, in group order.
    matched_terms: Tuple[str, ...]
    effect: RuleEffect
    matched_facts: Dict[str, Any] = field(default_factory=dict)

    def to_audit_dict(self) -> Dict[str, Any]:
        """Stable serializable shape for the TC's governance_rule_matches list."""
        eff = self.effect
        resolved_safety = eff.resolved_safety_category()
        # Legacy c3_category: keep whatever the rule set, or derive
        # from safety_category for back-compat with older readers.
        legacy_c3 = (
            eff.c3_category
            or (_c3_category_from_safety_category(resolved_safety)
                if eff.c3_violation else None)
        )
        return {
            "rule_id": self.rule_id,
            "rule_version": self.rule_version,
            "applies_to_domains": list(self.applies_to_domains),
            "matched_domain": self.matched_domain,
            "matched_term_groups": [
                {"group_index": i, "matched_term": t}
                for i, t in enumerate(self.matched_terms)
            ],
            "matched_facts": dict(self.matched_facts),
            "effect": {
                "c3_violation": eff.c3_violation,
                # New three-class fields (authoritative).
                "control_class": eff.resolved_control_class(),
                "safety_category": resolved_safety,
                "override_policy": eff.override_policy,
                # Legacy mirror (deprecated; readers should switch to
                # safety_category but this stays for stable audit shape).
                "c3_category": legacy_c3,
                "blocking_reason": eff.blocking_reason,
                "decision_pressure": eff.decision_pressure,
                "requires_human_review": eff.requires_human_review,
                "boundedness_penalty": eff.boundedness_penalty,
                "attribution_penalty": eff.attribution_penalty,
                "known_calibration_penalty": eff.known_calibration_penalty,
                "novelty_lift": eff.novelty_lift,
                "explanation": eff.explanation,
            },
        }


# --------------------------------------------------------------------------- #
# Matching primitives                                                          #
# --------------------------------------------------------------------------- #

def _contains_term(text_lower: str, term: str) -> bool:
    """
    Whole-word (or whole-prefix) match against a lowercase haystack.

    A term containing only word characters is matched as a whole word.
    A term ending in `*` is matched as a prefix (`pregnan*` matches
    `pregnant`, `pregnancy`, `pregnancies`). Terms containing spaces
    are matched as substrings (e.g. "drug interaction").
    """
    if not term:
        return False
    if " " in term:
        return term in text_lower
    if term.endswith("*"):
        prefix = term[:-1]
        # match prefix at a word boundary
        return bool(re.search(r"\b" + re.escape(prefix), text_lower))
    return bool(re.search(r"\b" + re.escape(term) + r"\b", text_lower))


def _first_matching_term(text_lower: str, terms: Tuple[str, ...]) -> Optional[str]:
    for term in terms:
        if _contains_term(text_lower, term):
            return term
    return None


def _rule_applies_to_domain(rule: RiskRule, domain: Optional[str]) -> bool:
    """
    Determine whether a rule's declared domain scope covers the active
    domain string.

    Domain strings come from multiple sources and have multiple shapes:
      - composed pack:    "composed:life_sciences:medical_devices"
      - built-in profile: "life_sciences" or "financial_services"
      - resolved profile: same as the base profile's domain
      - GCA hint:         "life_sciences" (from composer_metadata.industry)

    A rule with applies_to_domains=("life_sciences",) must match all of
    these forms. We do this by:
      1. exact equality
      2. prefix match (domain starts with "{d}:" or "{d}_")
      3. segment match (any colon-separated segment of the domain
         equals one of the rule's domains) — this catches the
         composed-pack form
    """
    if "*" in rule.applies_to_domains:
        return True
    if domain is None:
        return False
    segments = set(domain.split(":"))
    for d in rule.applies_to_domains:
        if domain == d:
            return True
        if domain.startswith(d + ":") or domain.startswith(d + "_"):
            return True
        if d in segments:
            return True
    return False


# --------------------------------------------------------------------------- #
# Public API                                                                   #
# --------------------------------------------------------------------------- #

def classify_query_risk(
    *,
    query: str,
    domain: Optional[str],
    rules: List[RiskRule],
) -> List[RuleMatch]:
    """
    Return all rules that match the given query under the given domain.

    A rule matches when:
      1. It applies to ``domain`` (or its applies_to_domains is "*")
      2. Each ``required_term_group`` has at least one term in the query
      3. No ``forbidden_terms`` are present in the query
    """
    if not query:
        return []
    text = query.lower()
    matches: List[RuleMatch] = []

    for rule in rules:
        if not _rule_applies_to_domain(rule, domain):
            continue

        # Forbidden-term short circuit.
        if rule.forbidden_terms:
            if any(_contains_term(text, t) for t in rule.forbidden_terms):
                continue

        # Each required group must contribute at least one term.
        matched_per_group: List[str] = []
        all_groups_matched = True
        for group in rule.required_term_groups:
            hit = _first_matching_term(text, group)
            if hit is None:
                all_groups_matched = False
                break
            matched_per_group.append(hit)

        if all_groups_matched:
            matches.append(RuleMatch(
                rule_id=rule.rule_id,
                rule_version=rule.version,
                applies_to_domains=rule.applies_to_domains,
                matched_domain=domain,
                matched_terms=tuple(matched_per_group),
                effect=rule.effect,
                # The term-group classifier does not bind typed facts;
                # the bounded-control evaluator in governed_facts will
                # populate this dict when it eventually runs.
                matched_facts={},
            ))

    return matches


@dataclass(frozen=True)
class MergedEffect:
    """
    Aggregate of all matched rules' effects.

    The aggregate preserves the full category sets (safety_categories,
    c3_categories for back-compat) so the audit can see every guardrail
    class that fired, not just the dominant one. ``primary_*`` fields
    give the dominant pick that the GCA / decision engine uses for
    immediate routing (blocking_reason prefix, narrative).

    New fields (three-class model):
      primary_control_class — highest priority control_class across all
        matches (hard_safety > deterministic_bounded > weighted_evidence)
      safety_categories     — union of safety_categories from all matches
      primary_safety_category — highest priority safety_category
      override_policy       — most-restrictive override_policy in play

    Legacy fields:
      c3_categories, primary_c3_category — kept for any reader that
        still wants the old-form values; auto-derived where needed.
    """
    c3_violation: bool = False
    primary_control_class: str = CONTROL_CLASS_WEIGHTED_EVIDENCE
    safety_categories: Tuple[str, ...] = ()
    primary_safety_category: Optional[str] = None
    override_policy: Optional[str] = None
    c3_categories: Tuple[str, ...] = ()              # legacy mirror
    primary_c3_category: Optional[str] = None        # legacy mirror
    blocking_reason: Optional[str] = None
    requires_human_review: bool = False
    boundedness_penalty: float = 0.0
    attribution_penalty: float = 0.0
    known_calibration_penalty: float = 0.0
    novelty_lift: float = 0.0
    decision_pressure: Optional[str] = None
    explanation: str = ""


# Legacy C3 priority — used only for back-compat derivation of
# primary_c3_category. The new code path uses _SAFETY_CATEGORY_PRIORITY.
_C3_CATEGORY_PRIORITY = {
    C3_CREDENTIAL_PATTERN:        4,
    C3_PROMPT_INJECTION_PATTERN:  3,
    C3_PROHIBITED_ACTION_PATTERN: 2,
    C3_PROHIBITED_CONTENT_PATTERN: 1,
}

# Override-policy restrictiveness ordering. Higher = more restrictive.
# When multiple rules fire with different override_policy values, the
# most-restrictive wins for the merged audit field — a reviewer
# downstream should see the strictest gate they must clear, not a
# permissive one a different rule happened to allow.
_OVERRIDE_POLICY_PRIORITY = {
    "non_overrideable":         5,
    "co_authorizer_required":   4,
    "specialist_review":        3,
    "policy_exception":         2,
    "standard":                 1,
}


def merge_effects(matches: List[RuleMatch]) -> MergedEffect:
    """
    Combine multiple rule matches into a single aggregate effect.

    Rules:
      - c3_violation: OR across rules
      - primary_control_class: highest priority across matches
        (hard_safety > deterministic_bounded > weighted_evidence)
      - safety_categories: union across matches (any control_class)
      - primary_safety_category: highest priority per
        _SAFETY_CATEGORY_PRIORITY; only considers c3_violating rules
        AND any rule with control_class == hard_safety
      - override_policy: most restrictive across matches
      - c3_categories / primary_c3_category: legacy mirrors
      - requires_human_review: OR across rules
      - blocking_reason: first non-empty wins (rules earlier in the
        list win — order rules from most-specific to least-specific)
      - decision_pressure: STOP > HOLD > ESCALATE > None
      - numeric penalties: sum, clamp to [0, 1]
      - explanation: join non-empty strings
    """
    if not matches:
        return MergedEffect()

    c3 = any(m.effect.c3_violation for m in matches)
    review = any(m.effect.requires_human_review for m in matches)
    blocking_reason = next(
        (m.effect.blocking_reason for m in matches if m.effect.blocking_reason),
        None,
    )

    # control_class: dominant across all matches.
    primary_control_class = CONTROL_CLASS_WEIGHTED_EVIDENCE
    for m in matches:
        cc = m.effect.resolved_control_class()
        if _CONTROL_CLASS_PRIORITY.get(cc, 0) > _CONTROL_CLASS_PRIORITY.get(
            primary_control_class, 0
        ):
            primary_control_class = cc

    # safety_categories: union from any rule that resolves one AND is
    # either a c3 violation or a hard_safety control. Pure
    # weighted_evidence rules don't contribute a safety category even
    # if they happen to set one — the field would be meaningless there.
    safety_cats_seen: List[str] = []
    for m in matches:
        sc = m.effect.resolved_safety_category()
        cc = m.effect.resolved_control_class()
        contributes = m.effect.c3_violation or cc == CONTROL_CLASS_HARD_SAFETY
        if contributes and sc and sc not in safety_cats_seen:
            safety_cats_seen.append(sc)
    primary_safety_category: Optional[str] = None
    if safety_cats_seen:
        primary_safety_category = max(
            safety_cats_seen,
            key=lambda c: _SAFETY_CATEGORY_PRIORITY.get(c, 0),
        )

    # Legacy c3_categories: derive from safety_categories for back-compat.
    c3_cats_seen: List[str] = []
    for sc in safety_cats_seen:
        legacy = _c3_category_from_safety_category(sc)
        if legacy and legacy not in c3_cats_seen:
            c3_cats_seen.append(legacy)
    primary_c3: Optional[str] = None
    if c3_cats_seen:
        primary_c3 = max(c3_cats_seen, key=lambda c: _C3_CATEGORY_PRIORITY.get(c, 0))

    # Override policy: most restrictive wins.
    override_policy: Optional[str] = None
    for m in matches:
        op = m.effect.override_policy
        if op and _OVERRIDE_POLICY_PRIORITY.get(op, 0) > _OVERRIDE_POLICY_PRIORITY.get(
            override_policy or "", 0
        ):
            override_policy = op

    pressure_priority = {"STOP": 3, "HOLD": 2, "ESCALATE": 1}
    pressure: Optional[str] = None
    for m in matches:
        p = m.effect.decision_pressure
        if p and pressure_priority.get(p, 0) > pressure_priority.get(pressure or "", 0):
            pressure = p

    def _clamp(v: float) -> float:
        return max(0.0, min(1.0, v))

    b = _clamp(sum(m.effect.boundedness_penalty for m in matches))
    a = _clamp(sum(m.effect.attribution_penalty for m in matches))
    k = _clamp(sum(m.effect.known_calibration_penalty for m in matches))
    n = _clamp(sum(m.effect.novelty_lift for m in matches))

    explanation = " | ".join(
        m.effect.explanation for m in matches if m.effect.explanation
    )

    return MergedEffect(
        c3_violation=c3,
        primary_control_class=primary_control_class,
        safety_categories=tuple(safety_cats_seen),
        primary_safety_category=primary_safety_category,
        override_policy=override_policy,
        c3_categories=tuple(c3_cats_seen),
        primary_c3_category=primary_c3,
        blocking_reason=blocking_reason,
        requires_human_review=review,
        boundedness_penalty=b,
        attribution_penalty=a,
        known_calibration_penalty=k,
        novelty_lift=n,
        decision_pressure=pressure,
        explanation=explanation,
    )
