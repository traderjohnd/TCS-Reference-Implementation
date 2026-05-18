"""
GovernanceEvaluation — dataclass tests (Phase 5 Slice 5.1).

Pins the load-bearing contract for the runtime sidecar architecture:

  - enforcement_action is DERIVED from (mode, decision); a caller
    cannot construct an observe-but-blocked or what_if-but-delivered
    evaluation
  - delivery_intervention is True iff mode=enforce AND action ≠ delivered
  - what_if evaluations MUST NOT carry a trust_certificate_id
    (counterfactual only — locked clarification)
  - observe and enforce MAY carry a TC; the lifecycle_state distinction
    happens at TC issuance time, not here
  - policy_profile_snapshot carries the full audit-grade snapshot
    (per locked decision D4)
  - artifact_id is required
"""

from __future__ import annotations

import pytest

from tcs.artifacts import (
    ENFORCEMENT_BLOCKED,
    ENFORCEMENT_COUNTERFACTUAL_ONLY,
    ENFORCEMENT_DELIVERED,
    ENFORCEMENT_ESCALATED,
    ENFORCEMENT_HELD,
    ENFORCEMENT_LOGGED_ONLY,
    EVALUATION_MODE_ENFORCE,
    EVALUATION_MODE_OBSERVE,
    EVALUATION_MODE_WHAT_IF,
    GovernanceEvaluation,
    derive_enforcement_action,
)


# --------------------------------------------------------------------------- #
# derive_enforcement_action — pure function table                              #
# --------------------------------------------------------------------------- #

class TestDeriveEnforcementAction:
    def test_observe_always_logged_only_regardless_of_decision(self):
        for decision in ("Allow", "Hold", "Stop", "Escalate", "Observe"):
            assert (
                derive_enforcement_action(EVALUATION_MODE_OBSERVE, decision)
                == ENFORCEMENT_LOGGED_ONLY
            )

    def test_what_if_always_counterfactual_only_regardless_of_decision(self):
        for decision in ("Allow", "Hold", "Stop", "Escalate", "Observe"):
            assert (
                derive_enforcement_action(EVALUATION_MODE_WHAT_IF, decision)
                == ENFORCEMENT_COUNTERFACTUAL_ONLY
            )

    @pytest.mark.parametrize("decision,expected", [
        ("Allow",              ENFORCEMENT_DELIVERED),
        ("Observe",            ENFORCEMENT_DELIVERED),  # r1 ships with caveat
        ("Hold",               ENFORCEMENT_HELD),
        ("Escalate",           ENFORCEMENT_ESCALATED),
        ("Stop",               ENFORCEMENT_BLOCKED),
        ("Allow_with_logging", ENFORCEMENT_DELIVERED),
        ("Rollback",           ENFORCEMENT_BLOCKED),
    ])
    def test_enforce_routes_per_decision(self, decision, expected):
        assert (
            derive_enforcement_action(EVALUATION_MODE_ENFORCE, decision)
            == expected
        )

    def test_unknown_mode_rejected(self):
        with pytest.raises(ValueError, match="unknown evaluation mode"):
            derive_enforcement_action("audit_only", "Allow")

    def test_unknown_decision_in_enforce_mode_rejected(self):
        # Catches the regression where someone adds a new decision
        # outcome (e.g. Phase-N expansion) but forgets to update the
        # action table.
        with pytest.raises(ValueError, match="unknown decision"):
            derive_enforcement_action(EVALUATION_MODE_ENFORCE, "Quarantine")


# --------------------------------------------------------------------------- #
# Construction-time guardrails                                                 #
# --------------------------------------------------------------------------- #

class TestConstructionGuardrails:
    def test_artifact_id_required(self):
        with pytest.raises(ValueError, match="artifact_id is required"):
            GovernanceEvaluation(artifact_id="")

    def test_unknown_mode_rejected(self):
        with pytest.raises(ValueError, match="unknown evaluation mode"):
            GovernanceEvaluation(artifact_id="a1", mode="audit_only")

    def test_caller_supplied_enforcement_action_must_match_derivation(self):
        # If a caller is brave enough to set enforcement_action
        # explicitly, the value MUST match what derivation produces.
        # This is the architectural guardrail that prevents
        # mode=observe but enforcement_action=blocked.
        with pytest.raises(ValueError, match="enforcement_action mismatch"):
            GovernanceEvaluation(
                artifact_id="a1",
                mode=EVALUATION_MODE_OBSERVE,
                decision="Stop",
                enforcement_action=ENFORCEMENT_BLOCKED,  # wrong: observe → logged_only
            )

    def test_what_if_must_not_carry_tc(self):
        with pytest.raises(ValueError, match="MUST NOT carry"):
            GovernanceEvaluation(
                artifact_id="a1",
                mode=EVALUATION_MODE_WHAT_IF,
                decision="Stop",
                trust_certificate_id="tc-123",
            )


# --------------------------------------------------------------------------- #
# Auto-derivation                                                              #
# --------------------------------------------------------------------------- #

class TestAutoDerivation:
    def test_enforcement_action_auto_derived_when_omitted(self):
        e = GovernanceEvaluation(
            artifact_id="a1",
            mode=EVALUATION_MODE_ENFORCE,
            decision="Stop",
        )
        assert e.enforcement_action == ENFORCEMENT_BLOCKED

    def test_delivery_intervention_true_for_enforce_blocked(self):
        e = GovernanceEvaluation(
            artifact_id="a1",
            mode=EVALUATION_MODE_ENFORCE,
            decision="Stop",
        )
        assert e.delivery_intervention is True

    def test_delivery_intervention_true_for_enforce_held(self):
        e = GovernanceEvaluation(
            artifact_id="a1",
            mode=EVALUATION_MODE_ENFORCE,
            decision="Hold",
        )
        assert e.delivery_intervention is True

    def test_delivery_intervention_false_for_enforce_allow(self):
        e = GovernanceEvaluation(
            artifact_id="a1",
            mode=EVALUATION_MODE_ENFORCE,
            decision="Allow",
        )
        # Allow delivers normally — no intervention even in enforce mode.
        assert e.delivery_intervention is False

    def test_delivery_intervention_always_false_for_observe(self):
        # Observe NEVER alters delivery, regardless of decision.
        # That's the locked design choice in D1: observe is audit-only.
        for decision in ("Allow", "Hold", "Stop", "Escalate"):
            e = GovernanceEvaluation(
                artifact_id="a1",
                mode=EVALUATION_MODE_OBSERVE,
                decision=decision,
            )
            assert e.delivery_intervention is False, (
                f"observe with decision={decision} must not intervene"
            )

    def test_delivery_intervention_always_false_for_what_if(self):
        for decision in ("Allow", "Hold", "Stop", "Escalate"):
            e = GovernanceEvaluation(
                artifact_id="a1",
                mode=EVALUATION_MODE_WHAT_IF,
                decision=decision,
            )
            assert e.delivery_intervention is False, (
                f"what_if with decision={decision} must not intervene"
            )


# --------------------------------------------------------------------------- #
# Policy snapshot (D4)                                                         #
# --------------------------------------------------------------------------- #

class TestPolicySnapshot:
    def test_snapshot_carried_verbatim(self):
        # The whole point of D4: a future reviewer must be able to
        # see the exact thresholds/weights/gates active at evaluation
        # time, even if the live profile registry has since been edited.
        snapshot = {
            "weights": {"B": 0.30, "A": 0.25, "C": 0.30, "K": 0.15},
            "thresholds": {"B": 0.90, "A": 0.90, "C": 0.90, "K": 0.80},
            "gate_set": ["B", "A", "C", "K"],
            "soft_hold_ceiling": 0.90,
            "regulatory_mapping": ["ISO 13485", "IEC 62304"],
        }
        e = GovernanceEvaluation(
            artifact_id="a1",
            mode=EVALUATION_MODE_ENFORCE,
            decision="Allow",
            policy_profile_id="composed-abc123",
            policy_profile_snapshot=snapshot,
        )
        assert e.policy_profile_snapshot == snapshot
        # And the snapshot survives serialization.
        d = e.to_dict()
        assert d["policy_profile_snapshot"] == snapshot


# --------------------------------------------------------------------------- #
# Serialization                                                                #
# --------------------------------------------------------------------------- #

class TestSerialization:
    def _build_full_evaluation(self) -> GovernanceEvaluation:
        return GovernanceEvaluation(
            artifact_id="a1",
            mode=EVALUATION_MODE_ENFORCE,
            policy_profile_id="composed-medical",
            policy_profile_snapshot={"weights": {"B": 0.25}},
            selected_standards=["iso_13485", "iso_14971", "iec_62304"],
            enabled_controls=["c1", "c2"],
            rule_matches=[
                {"rule_id": "consumer_facing_dosing_during_pregnancy",
                 "rule_version": "v1",
                 "effect": {"safety_category": "prohibited_action",
                            "control_class": "hard_safety"}},
            ],
            component_scores={"B": 0.92, "A": 0.88, "C": 0.31, "K": 0.84},
            gate_results={"B": "pass", "A": "pass", "C": "fail", "K": "pass"},
            s_base=0.6825,
            s_adjusted=0.6817,
            tis_current=0.0,
            decision="Stop",
            trust_certificate_id="tc-987",
            evaluator_identity={"requesting_identity": "system",
                                "identity_type": "system"},
        )

    def test_round_trip_preserves_every_field(self):
        original = self._build_full_evaluation()
        restored = GovernanceEvaluation.from_dict(original.to_dict())
        assert original.to_dict() == restored.to_dict()

    def test_what_if_round_trip_keeps_tc_none(self):
        original = GovernanceEvaluation(
            artifact_id="a1",
            mode=EVALUATION_MODE_WHAT_IF,
            policy_profile_id="composed-other",
            decision="Stop",
        )
        restored = GovernanceEvaluation.from_dict(original.to_dict())
        assert restored.trust_certificate_id is None
        assert restored.enforcement_action == ENFORCEMENT_COUNTERFACTUAL_ONLY

    def test_observe_round_trip_keeps_logged_only(self):
        original = GovernanceEvaluation(
            artifact_id="a1",
            mode=EVALUATION_MODE_OBSERVE,
            decision="Stop",
            trust_certificate_id="tc-observed-123",  # observe may carry a TC
        )
        restored = GovernanceEvaluation.from_dict(original.to_dict())
        assert restored.enforcement_action == ENFORCEMENT_LOGGED_ONLY
        assert restored.delivery_intervention is False
        assert restored.trust_certificate_id == "tc-observed-123"


# --------------------------------------------------------------------------- #
# Immutability                                                                  #
# --------------------------------------------------------------------------- #

class TestImmutability:
    def test_frozen_dataclass_blocks_mutation(self):
        e = GovernanceEvaluation(
            artifact_id="a1",
            mode=EVALUATION_MODE_ENFORCE,
            decision="Allow",
        )
        with pytest.raises(Exception):  # FrozenInstanceError
            e.decision = "Stop"  # type: ignore[misc]
