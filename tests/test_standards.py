"""
Phase 4 / Slice 4 — standards library + composer tests.

Validates:
    1. Library data integrity (11 standards, taxonomy coverage)
    2. Control interpretation framing (every standard explicitly labels
       its adjustments as governance interpretation, not regulatory truth)
    3. Composer rules (hybrid / strictest-control):
       - thresholds take the strictest (max)
       - gate_set is the union
       - required_controls / hard_prohibitions are unions
       - weights + penalty_weights additive, re-normalized to Σ=1.0
       - profile_hash deterministic
    4. End-to-end: a composed profile is a valid PolicyProfile
"""

from __future__ import annotations

import pytest

from tcs.standards import (
    STANDARDS, TAXONOMY,
    ComposedProfile, StandardContribution,
    compose_profile, composed_pack_id,
    get_standard, list_standards, standards_for_use_case,
)
from tcs.standards.composer import COMPOSITION_RULES_VERSION


# --------------------------------------------------------------------------- #
# Library data integrity                                                       #
# --------------------------------------------------------------------------- #

class TestLibraryIntegrity:
    def test_11_standards_present(self):
        assert len(STANDARDS) == 11

    def test_each_standard_has_required_fields(self):
        required = {
            "id", "name", "regulatory_reference", "industry", "sub_industry",
            "applies_to_use_cases", "control_interpretation", "profile_adjustments",
        }
        for sid, s in STANDARDS.items():
            assert sid == s["id"], f"id mismatch in {sid}"
            assert required.issubset(s.keys()), f"missing fields in {sid}: {required - s.keys()}"

    def test_taxonomy_covers_all_standards_industries(self):
        for s in STANDARDS.values():
            assert s["industry"] in TAXONOMY, f"{s['id']} industry {s['industry']} not in taxonomy"
            sub = TAXONOMY[s["industry"]]["sub_industries"]
            assert s["sub_industry"] in sub, f"{s['id']} sub_industry not in taxonomy"

    def test_each_use_case_references_taxonomy(self):
        all_use_cases = set()
        for ind in TAXONOMY.values():
            for sub in ind["sub_industries"].values():
                all_use_cases.update(sub["use_cases"].keys())
        for s in STANDARDS.values():
            for uc in s["applies_to_use_cases"]:
                assert uc in all_use_cases, f"{s['id']} references unknown use case {uc}"

    def test_control_interpretation_is_explicit_about_framing(self):
        """
        Each control_interpretation must include language that frames the
        mapping as TCS's interpretation, not a regulatory mathematical
        requirement. Catches future entries that drift into implying
        the standard literally requires specific TCS thresholds.
        """
        for s in STANDARDS.values():
            ci = s["control_interpretation"].lower()
            # Look for any of the explicit framing phrases.
            has_framing = any(phrase in ci for phrase in (
                "this implementation",
                "interprets",
                "interpretation",
                "editorial",
                "does not specify",
                "does not literally",
            ))
            assert has_framing, (
                f"{s['id']} control_interpretation lacks framing language: {ci[:120]}..."
            )


# --------------------------------------------------------------------------- #
# Accessor functions                                                           #
# --------------------------------------------------------------------------- #

class TestAccessors:
    def test_get_standard_returns_full_entry(self):
        s = get_standard("iso_13485")
        assert s is not None
        assert s["id"] == "iso_13485"
        assert "profile_adjustments" in s

    def test_get_standard_returns_none_for_unknown(self):
        assert get_standard("nope") is None

    def test_list_standards_omits_profile_adjustments(self):
        summaries = list_standards()
        assert len(summaries) == 11
        # Summaries should NOT include the heavy profile_adjustments block.
        assert all("profile_adjustments" not in s for s in summaries)
        # But should include the interpretation note for the UI.
        assert all(s.get("control_interpretation") for s in summaries)

    def test_standards_for_use_case_filters(self):
        results = standards_for_use_case("device_software")
        ids = {s["id"] for s in results}
        # ISO 13485, ISO 14971, IEC 62304 all apply to device_software
        assert {"iso_13485", "iso_14971", "iec_62304"}.issubset(ids)
        # SEC Reg BI does NOT
        assert "sec_reg_bi" not in ids


# --------------------------------------------------------------------------- #
# Composer — basic mechanics                                                   #
# --------------------------------------------------------------------------- #

class TestComposerBasics:
    def test_compose_with_single_standard_produces_valid_profile(self):
        composed = compose_profile(
            industry="life_sciences",
            sub_industry="medical_devices",
            use_case="clinical_decision_support",
            standard_ids=["iso_13485"],
            risk_tier="r3",
            action_class="a4",
        )
        assert isinstance(composed, ComposedProfile)
        pc = composed.profile_config
        # Σ weights = 1.0 within rounding tolerance
        assert abs(sum(pc["weights"].values()) - 1.0) < 1e-3
        # Σ penalty_weights = 1.0
        assert abs(sum(pc["penalty_weights"].values()) - 1.0) < 1e-3
        # All four dimensions present in weights/thresholds
        assert set(pc["weights"].keys()) == {"B", "A", "C", "K"}

    def test_compose_with_unknown_standard_raises(self):
        with pytest.raises(ValueError, match="unknown standard"):
            compose_profile(
                industry="life_sciences", sub_industry="medical_devices",
                use_case="clinical_decision_support",
                standard_ids=["iso_13485", "nope"],
                risk_tier="r3", action_class="a4",
            )

    def test_compose_with_invalid_risk_tier_raises(self):
        with pytest.raises(ValueError, match="risk_tier"):
            compose_profile(
                industry="life_sciences", sub_industry="medical_devices",
                use_case="clinical_decision_support",
                standard_ids=["iso_13485"],
                risk_tier="r9", action_class="a4",
            )

    def test_compose_no_standards_returns_base_profile(self):
        composed = compose_profile(
            industry="general_ai_governance", sub_industry="cross_industry",
            use_case="any_ai_workflow",
            standard_ids=[],
            risk_tier="r2", action_class="a3",
        )
        assert composed.contributions == []
        assert composed.required_controls == []
        assert composed.hard_prohibitions == []

    def test_composer_metadata_recorded(self):
        composed = compose_profile(
            industry="life_sciences", sub_industry="medical_devices",
            use_case="clinical_decision_support",
            standard_ids=["iso_13485", "iso_14971"],
            risk_tier="r3", action_class="a4",
        )
        meta = composed.composer_metadata
        assert meta["industry"] == "life_sciences"
        assert meta["sub_industry"] == "medical_devices"
        assert meta["use_case"] == "clinical_decision_support"
        assert set(meta["standards"]) == {"iso_13485", "iso_14971"}
        assert meta["risk_tier"] == "r3"
        assert meta["action_class"] == "a4"
        assert meta["composition_rules_version"] == COMPOSITION_RULES_VERSION
        assert "composed_at" in meta


# --------------------------------------------------------------------------- #
# Composer — STRICTEST-CONTROL discipline (the locked composition rules)       #
# --------------------------------------------------------------------------- #

class TestStrictestControlComposition:
    def test_thresholds_take_max_across_standards(self):
        """ISO 13485 has A floor 0.90. 21 CFR Part 11 has A floor 0.92. Composed must be 0.92."""
        composed = compose_profile(
            industry="life_sciences", sub_industry="pharma",
            use_case="manufacturing_qc",
            standard_ids=["iso_13485", "fda_21_cfr_part_11"],
            risk_tier="r3", action_class="a4",
        )
        assert composed.profile_config["thresholds"]["A"] == 0.92

    def test_overridden_threshold_recorded_for_non_strictest(self):
        """The non-strictest standard's threshold is recorded as overridden so the UI can show it."""
        composed = compose_profile(
            industry="life_sciences", sub_industry="pharma",
            use_case="manufacturing_qc",
            standard_ids=["iso_13485", "fda_21_cfr_part_11"],
            risk_tier="r3", action_class="a4",
        )
        # ISO 13485 wanted A=0.90, 21 CFR wanted A=0.92 — 21 CFR wins.
        iso_contrib = next(c for c in composed.contributions if c.standard_id == "iso_13485")
        cfr_contrib = next(c for c in composed.contributions if c.standard_id == "fda_21_cfr_part_11")
        assert iso_contrib.threshold_floors_overridden.get("A") == 0.90
        assert cfr_contrib.threshold_floors_applied.get("A") == 0.92

    def test_gate_set_is_union(self):
        """ISO 14971 adds K; IEC 62304 adds C; composed gate_set should include both."""
        composed = compose_profile(
            industry="life_sciences", sub_industry="medical_devices",
            use_case="device_software",
            standard_ids=["iso_14971", "iec_62304"],
            risk_tier="r2", action_class="a3",
        )
        gate = set(composed.profile_config["gate_set"])
        # Base r2/a3 gate is {B,A,C}; standards add K (iso_14971); already has C
        assert "K" in gate
        assert "C" in gate

    def test_required_controls_union(self):
        composed = compose_profile(
            industry="life_sciences", sub_industry="medical_devices",
            use_case="device_software",
            standard_ids=["iso_13485", "iso_14971"],
            risk_tier="r3", action_class="a4",
        )
        # Each standard contributes its own controls; union covers all.
        assert "documentation_traceability" in composed.required_controls
        assert "risk_assessment_record" in composed.required_controls

    def test_hard_prohibitions_union(self):
        composed = compose_profile(
            industry="general_ai_governance", sub_industry="cross_industry",
            use_case="high_risk_ai_system",
            standard_ids=["eu_ai_act_high_risk", "nist_ai_rmf"],
            risk_tier="r3", action_class="a4",
        )
        # EU AI Act contributes prohibitions; NIST does not.
        assert "subliminal_manipulation" in composed.hard_prohibitions
        assert "social_scoring" in composed.hard_prohibitions

    def test_weight_deltas_additive_then_renormalized(self):
        """Two standards both pushing weight to A should compound, then re-normalize."""
        composed_single = compose_profile(
            industry="life_sciences", sub_industry="pharma",
            use_case="clinical_trials",
            standard_ids=["ich_e6"],
            risk_tier="r3", action_class="a4",
        )
        composed_double = compose_profile(
            industry="life_sciences", sub_industry="pharma",
            use_case="clinical_trials",
            standard_ids=["ich_e6", "fda_21_cfr_part_11"],
            risk_tier="r3", action_class="a4",
        )
        # Both standards add positive A weight; composed_double's A
        # should be at least as large as composed_single's A.
        assert composed_double.profile_config["weights"]["A"] >= composed_single.profile_config["weights"]["A"]
        # Σ weights still 1.0 (re-normalized)
        assert abs(sum(composed_double.profile_config["weights"].values()) - 1.0) < 1e-3

    def test_dimension_weight_never_collapses_to_zero(self):
        """Even with many negative deltas, no dimension should drop below the 0.05 floor."""
        composed = compose_profile(
            industry="general_ai_governance", sub_industry="cross_industry",
            use_case="high_risk_ai_system",
            standard_ids=["eu_ai_act_high_risk", "iso_iec_42001", "nist_ai_rmf"],
            risk_tier="r3", action_class="a4",
        )
        for dim, w in composed.profile_config["weights"].items():
            assert w >= 0.04, f"dimension {dim} collapsed to {w}"


# --------------------------------------------------------------------------- #
# Composer — deterministic hash + pack id                                      #
# --------------------------------------------------------------------------- #

class TestComposerDeterminism:
    def test_same_inputs_produce_same_hash(self):
        a = compose_profile(
            industry="life_sciences", sub_industry="medical_devices",
            use_case="clinical_decision_support",
            standard_ids=["iso_13485", "iso_14971"],
            risk_tier="r3", action_class="a4",
        )
        b = compose_profile(
            industry="life_sciences", sub_industry="medical_devices",
            use_case="clinical_decision_support",
            # Same standards in different order
            standard_ids=["iso_14971", "iso_13485"],
            risk_tier="r3", action_class="a4",
        )
        # composed_at differs by definition (timestamp). For the hash to
        # be deterministic against the user's intent (the spec inputs),
        # we strip composed_at before comparing.
        assert a.profile_config == b.profile_config
        # Pack ids derive from hash; equal profile_config means same id
        # only if composer_metadata also matches except composed_at.
        # Strip composed_at and re-hash to assert input determinism.
        from tcs.standards.composer import _compute_profile_hash
        meta_a = dict(a.composer_metadata); meta_a.pop("composed_at", None)
        meta_b = dict(b.composer_metadata); meta_b.pop("composed_at", None)
        assert meta_a == meta_b
        assert _compute_profile_hash(a.profile_config, meta_a) == _compute_profile_hash(b.profile_config, meta_b)

    def test_pack_id_hash_prefix(self):
        composed = compose_profile(
            industry="life_sciences", sub_industry="medical_devices",
            use_case="clinical_decision_support",
            standard_ids=["iso_13485"],
            risk_tier="r3", action_class="a4",
        )
        pack_id = composed_pack_id(composed.profile_hash)
        assert pack_id.startswith("composed-")
        assert len(pack_id) == len("composed-") + 16


# --------------------------------------------------------------------------- #
# Composed profile loads as a PolicyProfile (interoperability)                 #
# --------------------------------------------------------------------------- #

class TestComposedProfileIsValidPolicyProfile:
    def test_composed_profile_passes_policy_profile_validation(self):
        """The composed profile_config can be instantiated as a PolicyProfile."""
        from tcs.policy_profiles import PolicyProfile

        composed = compose_profile(
            industry="financial_services", sub_industry="investment_advisory",
            use_case="recommendation_generation",
            standard_ids=["sec_reg_bi", "finra_2111"],
            risk_tier="r3", action_class="a4",
        )
        pc = composed.profile_config
        # PolicyProfile validates on construction. Should not raise.
        profile = PolicyProfile(
            profile_id=pc["profile_id"],
            domain=pc["domain"],
            risk_tier=pc["risk_tier"],
            action_class=pc["action_class"],
            gate_set=frozenset(pc["gate_set"]),
            thresholds=pc["thresholds"],
            weights=pc["weights"],
            penalty_weights=pc["penalty_weights"],
            decay_rate=pc["decay_rate"],
            soft_hold_ceiling=pc["soft_hold_ceiling"],
            decision_thresholds=pc["decision_thresholds"],
            invalidation_triggers=pc["invalidation_triggers"],
            regulatory_mapping=pc["regulatory_mapping"],
            description=f"Composed from {composed.composer_metadata['standards']}",
        )
        assert profile is not None
        assert profile.risk_tier == "r3"


# --------------------------------------------------------------------------- #
# Composed profile feeds through the engine (smoke)                            #
# --------------------------------------------------------------------------- #

class TestComposedProfileGovernanceSmoke:
    def test_composed_profile_scores_a_clean_input(self):
        """End-to-end: compose a profile, then govern a clean input through it."""
        from datetime import datetime, timezone
        from tcs.decision_engine import map_decision
        from tcs.policy_profiles import PolicyProfile
        from tcs.tis_engine import TISInput, compute_tis

        composed = compose_profile(
            industry="financial_services", sub_industry="investment_advisory",
            use_case="recommendation_generation",
            standard_ids=["sec_reg_bi"],
            risk_tier="r3", action_class="a4",
        )
        pc = composed.profile_config
        profile = PolicyProfile(
            profile_id=pc["profile_id"], domain=pc["domain"],
            risk_tier=pc["risk_tier"], action_class=pc["action_class"],
            gate_set=frozenset(pc["gate_set"]),
            thresholds=pc["thresholds"], weights=pc["weights"],
            penalty_weights=pc["penalty_weights"],
            decay_rate=pc["decay_rate"],
            soft_hold_ceiling=pc["soft_hold_ceiling"],
            decision_thresholds=pc["decision_thresholds"],
            invalidation_triggers=pc["invalidation_triggers"],
            regulatory_mapping=pc["regulatory_mapping"],
            description="smoke",
        )
        inp = TISInput(
            subject_id="s",
            subject_type="recommendation",
            policy_profile=profile,
            dimension_scores={"B": 1.0, "A": 1.0, "C": 1.0, "K": 1.0},
            sub_factor_scores={"C": {"C3": 1.0}},
            context_metadata={
                "n_gaps": 0, "context_age_hours": 0.1, "novelty_score": 0.0,
                "days_since_review": 1, "is_policy_sensitive": False,
            },
            elapsed_hours=0.0, is_valid=1, invalidation_event=None,
            evaluation_time=datetime.now(timezone.utc),
        )
        result = compute_tis(inp)
        decision, _ = map_decision(inp, result)
        # Clean dimensions through a composed profile should Allow.
        assert decision == "Allow"
        assert result.gate_result == 1
