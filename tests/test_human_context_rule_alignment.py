"""
Phase 5 Slice 5.5a — Human Context Risk Rule Alignment tests.

Pins the corrective fix to Slice 5.5: the lithium / pregnant-patient
outbound message scenario should be intervened on for the RIGHT
primary reason (a typed-context rule recognizing the actual risk),
not as a side-effect of attribution gate failure.

Acceptance criteria pinned (1-6 from the slice spec):

  1. "Lithium is fine in small doses" to a pregnant patient
     produces a rule match.
  2. The same draft without pregnant=true does NOT trigger that
     rule (predicate failure is the gate).
  3. The same draft as an internal clinician-to-clinician note
     does NOT trigger that rule either (role predicate).
  4. matched_facts includes the relevant recipient_context bindings
     (pregnant, role, channel — whatever the rule's predicates
     required).
  5. The Trust Certificate shows the human-composed source AND the
     rule evidence (rule fires, governance_rule_matches non-empty,
     TC.blocking_reason names the rule's reason).
  6. Replay from the captured snapshot is deterministic — the rule's
     evidence survives the round-trip and the second evaluation
     reproduces the same decision/scores via runtime_snapshot.

Plus unit-level tests of the typed-context evaluator's match logic.
"""

from __future__ import annotations

import os
from typing import Any, Dict

import pytest
from fastapi.testclient import TestClient


# --------------------------------------------------------------------------- #
# Fixtures                                                                     #
# --------------------------------------------------------------------------- #

@pytest.fixture
def client(tmp_path):
    os.environ["TCS_WORKFLOW_TRACE_ENABLED"] = "true"
    from tcs.api.app import create_app
    from tcs.packs.pack_manager import (
        PACKS, clear_active_pack, unregister_composed_pack,
    )
    from tcs.persistence.certificate_store import CertificateStore

    pre = set(PACKS.keys())
    store = CertificateStore(str(tmp_path / "slice55a.db"))
    app = create_app(store=store)
    c = TestClient(app)
    with c:
        yield c
    for pid in (set(PACKS.keys()) - pre):
        try:
            unregister_composed_pack(pid)
        except Exception:
            pass
    clear_active_pack()
    store.close()
    os.environ.pop("TCS_WORKFLOW_TRACE_ENABLED", None)


def _deploy_meddev(client) -> Dict[str, Any]:
    return client.post("/v2/standards/deploy", json={
        "industry": "life_sciences",
        "sub_industry": "medical_devices",
        "use_case": "clinical_decision_support",
        "standard_ids": ["iso_13485", "iso_14971", "iec_62304"],
        "risk_tier": "r3", "action_class": "a4",
    }).json()


def _make_human_draft(client, **overrides) -> str:
    """Generate a human_composed artifact. Overrides merge into recipient_context."""
    recipient_context = {
        "pregnant": True,
        "role": "patient",
        "channel": "outbound_message",
        "medication_topic": "lithium",
    }
    recipient_context.update(overrides)
    r = client.post("/v2/generate", json={
        "generation_mode": "human_composed",
        "draft": "Lithium is fine in small doses.",
        "recipient_context": recipient_context,
        "generation_identity": {
            "requesting_identity": "rep-007",
            "identity_type": "human",
            "role": "patient_support_rep",
        },
    })
    assert r.status_code == 200, r.text
    return r.json()["artifact_id"]


# --------------------------------------------------------------------------- #
# Acceptance #1 — rule fires on the flagship scenario                          #
# --------------------------------------------------------------------------- #

class TestRuleFiresOnLithiumToPregnantPatient:
    def test_rule_match_present_in_governance_evidence(self, client):
        deployed = _deploy_meddev(client)
        artifact_id = _make_human_draft(client)
        r = client.post("/v2/evaluate", json={
            "artifact_id": artifact_id,
            "mode": "enforce",
            "policy_profile_id": deployed["pack_id"],
        }).json()
        tc = client.get(f"/v2/certificates/{r['trust_certificate_id']}").json()

        matches = tc.get("governance_rule_matches") or []
        rule_ids = {m["rule_id"] for m in matches}
        assert (
            "human_composed_patient_specific_medication_in_pregnancy"
            in rule_ids
        ), (
            "typed-context rule should fire on lithium/pregnant/outbound "
            f"scenario; matched rule_ids: {rule_ids}"
        )

    def test_rule_match_carries_all_required_audit_fields(self, client):
        deployed = _deploy_meddev(client)
        artifact_id = _make_human_draft(client)
        r = client.post("/v2/evaluate", json={
            "artifact_id": artifact_id,
            "mode": "enforce",
            "policy_profile_id": deployed["pack_id"],
        }).json()
        tc = client.get(f"/v2/certificates/{r['trust_certificate_id']}").json()
        m = next(
            x for x in tc["governance_rule_matches"]
            if x["rule_id"]
            == "human_composed_patient_specific_medication_in_pregnancy"
        )
        # All audit fields the user pinned must be present.
        eff = m["effect"]
        assert eff["control_class"] == "deterministic_bounded"
        assert eff["safety_category"] == "prohibited_action"
        assert eff["override_policy"] == "specialist_review"
        assert eff["blocking_reason"] == (
            "patient_specific_medication_guidance_during_pregnancy"
        )
        assert eff["requires_human_review"] is True
        assert eff["decision_pressure"] == "HOLD"
        # And the rule audit links back to the active policy profile.
        assert m["active_policy_profile_id"] == deployed["pack_id"]


# --------------------------------------------------------------------------- #
# Acceptance #2 — same draft without pregnant=true does NOT trigger            #
# --------------------------------------------------------------------------- #

class TestRuleDoesNotFireWithoutPregnantFact:
    def test_no_match_when_pregnant_false(self, client):
        deployed = _deploy_meddev(client)
        artifact_id = _make_human_draft(client, pregnant=False)
        r = client.post("/v2/evaluate", json={
            "artifact_id": artifact_id,
            "mode": "enforce",
            "policy_profile_id": deployed["pack_id"],
        }).json()
        tc = client.get(f"/v2/certificates/{r['trust_certificate_id']}").json()
        rule_ids = {
            m["rule_id"] for m in (tc.get("governance_rule_matches") or [])
        }
        assert (
            "human_composed_patient_specific_medication_in_pregnancy"
            not in rule_ids
        ), (
            "rule should not fire when pregnant=False; matched: "
            f"{rule_ids}"
        )

    def test_no_match_when_pregnant_absent(self, client):
        # Predicate failure when the key is missing entirely
        # (None values mean "fact not provided" — required facts
        # must be explicitly present).
        deployed = _deploy_meddev(client)
        r = client.post("/v2/generate", json={
            "generation_mode": "human_composed",
            "draft": "Lithium is fine in small doses.",
            "recipient_context": {
                # no pregnant key
                "role": "patient",
                "channel": "outbound_message",
                "medication_topic": "lithium",
            },
        })
        artifact_id = r.json()["artifact_id"]
        eval_r = client.post("/v2/evaluate", json={
            "artifact_id": artifact_id,
            "mode": "enforce",
            "policy_profile_id": deployed["pack_id"],
        }).json()
        tc = client.get(f"/v2/certificates/{eval_r['trust_certificate_id']}").json()
        rule_ids = {
            m["rule_id"] for m in (tc.get("governance_rule_matches") or [])
        }
        assert (
            "human_composed_patient_specific_medication_in_pregnancy"
            not in rule_ids
        )


# --------------------------------------------------------------------------- #
# Acceptance #3 — same draft as clinician-to-clinician does NOT trigger        #
# --------------------------------------------------------------------------- #

class TestRuleDoesNotFireForClinicianToClinicianNote:
    def test_clinician_role_does_not_match_rule(self, client):
        # An internal clinician-to-clinician note has the same draft
        # text but role=clinician — outside the rule's allowed roles
        # (patient/client/consumer). The rule predicates fail; no
        # match. The decision may still HOLD via the existing gate
        # behavior (attribution under MedDev), but the audit no
        # longer claims it's a consumer-facing risk.
        deployed = _deploy_meddev(client)
        artifact_id = _make_human_draft(client, role="clinician")
        r = client.post("/v2/evaluate", json={
            "artifact_id": artifact_id,
            "mode": "enforce",
            "policy_profile_id": deployed["pack_id"],
        }).json()
        tc = client.get(f"/v2/certificates/{r['trust_certificate_id']}").json()
        rule_ids = {
            m["rule_id"] for m in (tc.get("governance_rule_matches") or [])
        }
        assert (
            "human_composed_patient_specific_medication_in_pregnancy"
            not in rule_ids
        ), (
            "rule must not treat clinician-to-clinician traffic like "
            "consumer-facing outbound advice"
        )


# --------------------------------------------------------------------------- #
# Acceptance #4 — matched_facts populated from recipient_context               #
# --------------------------------------------------------------------------- #

class TestMatchedFactsCapturedFromRecipientContext:
    def test_matched_facts_contains_pregnant_role_channel(self, client):
        deployed = _deploy_meddev(client)
        artifact_id = _make_human_draft(client)
        r = client.post("/v2/evaluate", json={
            "artifact_id": artifact_id,
            "mode": "enforce",
            "policy_profile_id": deployed["pack_id"],
        }).json()
        tc = client.get(f"/v2/certificates/{r['trust_certificate_id']}").json()
        m = next(
            x for x in tc["governance_rule_matches"]
            if x["rule_id"]
            == "human_composed_patient_specific_medication_in_pregnancy"
        )
        facts = m["matched_facts"]
        # The three recipient_context bindings that satisfied the
        # rule's fact_predicates must appear in matched_facts.
        assert facts["pregnant"] is True
        assert facts["role"] == "patient"
        assert facts["channel"] == "outbound_message"


# --------------------------------------------------------------------------- #
# Acceptance #5 — TC carries human-composed source AND rule evidence           #
# --------------------------------------------------------------------------- #

class TestTCCarriesSourceAndRuleEvidence:
    def test_tc_subject_type_human_composed_and_rule_present(self, client):
        deployed = _deploy_meddev(client)
        artifact_id = _make_human_draft(client)
        r = client.post("/v2/evaluate", json={
            "artifact_id": artifact_id,
            "mode": "enforce",
            "policy_profile_id": deployed["pack_id"],
        }).json()
        tc = client.get(f"/v2/certificates/{r['trust_certificate_id']}").json()
        # Source signal (Slice 5.5).
        assert tc["subject_type"] == "human_composed"
        # Rule evidence (Slice 5.5a).
        assert tc["governance_rule_matches"], (
            "governance_rule_matches must be non-empty so the audit "
            "shows WHY the message was held"
        )
        rule_ids = {m["rule_id"] for m in tc["governance_rule_matches"]}
        assert (
            "human_composed_patient_specific_medication_in_pregnancy"
            in rule_ids
        )

    def test_tc_blocking_reason_names_the_rule_not_just_the_gate(self, client):
        # Slice 5.5a contract: the runtime audit must intervene for
        # the RIGHT reason. The TC's blocking_reason should name the
        # rule (patient_specific_medication_guidance_during_pregnancy),
        # not just the downstream gate (attribution_gate_fail).
        deployed = _deploy_meddev(client)
        artifact_id = _make_human_draft(client)
        r = client.post("/v2/evaluate", json={
            "artifact_id": artifact_id,
            "mode": "enforce",
            "policy_profile_id": deployed["pack_id"],
        }).json()
        tc = client.get(f"/v2/certificates/{r['trust_certificate_id']}").json()
        br = tc.get("blocking_reason") or ""
        assert (
            "patient_specific_medication_guidance_during_pregnancy" in br
        ), (
            f"TC.blocking_reason should name the rule reason; got {br!r}"
        )


# --------------------------------------------------------------------------- #
# Acceptance #6 — replay determinism (typed-context match survives snapshot)   #
# --------------------------------------------------------------------------- #

class TestReplayDeterminismOnTypedContextMatch:
    def test_replay_reproduces_decision_and_keeps_rule_evidence(self, client):
        deployed = _deploy_meddev(client)
        artifact_id = _make_human_draft(client)

        first = client.post("/v2/evaluate", json={
            "artifact_id": artifact_id,
            "mode": "enforce",
            "policy_profile_id": deployed["pack_id"],
        }).json()
        # Second eval auto-resolves to runtime_snapshot strategy
        # (Slice 5.4a) and must reproduce identical scores +
        # decision. The rule match was captured at first-eval time
        # in the snapshot's context_metadata.governance_rule_matches.
        second = client.post("/v2/evaluate", json={
            "artifact_id": artifact_id,
            "mode": "enforce",
            "policy_profile_id": deployed["pack_id"],
        }).json()
        assert second["evaluation_strategy"] == "runtime_snapshot"
        assert first["decision"] == second["decision"]
        assert first["s_base"] == second["s_base"]
        assert first["tis_current"] == second["tis_current"]
        assert first["component_scores"] == second["component_scores"]

        # And the rule evidence is preserved on the replayed TC.
        tc2 = client.get(f"/v2/certificates/{second['trust_certificate_id']}").json()
        rule_ids = {
            m["rule_id"] for m in (tc2.get("governance_rule_matches") or [])
        }
        assert (
            "human_composed_patient_specific_medication_in_pregnancy"
            in rule_ids
        )


# --------------------------------------------------------------------------- #
# Unit tests of the evaluator (no API)                                         #
# --------------------------------------------------------------------------- #

class TestTypedContextEvaluatorUnit:
    """
    Direct tests of evaluate_typed_context_rules without the API
    surface. Pins the predicate / draft-term matching semantics.
    """

    def _run(self, **kwargs):
        from tcs.governance import evaluate_typed_context_rules
        defaults = dict(
            generation_mode="human_composed",
            recipient_context={
                "pregnant": True, "role": "patient",
                "channel": "outbound_message",
            },
            draft_text="Lithium is fine in small doses.",
            domain="life_sciences",
        )
        defaults.update(kwargs)
        return evaluate_typed_context_rules(**defaults)

    def test_fires_on_full_flagship_scenario(self):
        matches = self._run()
        assert any(
            m.rule_id
            == "human_composed_patient_specific_medication_in_pregnancy"
            for m in matches
        )

    def test_does_not_fire_on_wrong_generation_mode(self):
        # The rule applies only to human_composed today.
        matches = self._run(generation_mode="raw_llm")
        assert matches == []

    def test_does_not_fire_when_role_is_clinician(self):
        matches = self._run(
            recipient_context={
                "pregnant": True, "role": "clinician",
                "channel": "outbound_message",
            },
        )
        assert matches == []

    def test_does_not_fire_when_channel_missing(self):
        matches = self._run(
            recipient_context={
                "pregnant": True, "role": "patient",
                # no channel
            },
        )
        assert matches == []

    def test_does_not_fire_when_draft_has_no_medication(self):
        matches = self._run(
            draft_text="The weather today is nice.",
        )
        assert matches == []

    def test_does_not_fire_when_draft_has_no_dosing_advice(self):
        matches = self._run(
            draft_text="What is lithium?",
        )
        assert matches == []

    def test_matched_facts_is_a_subset_of_recipient_context(self):
        # matched_facts should contain only the keys the rule's
        # predicates actually checked, with the values that
        # satisfied them.
        matches = self._run(
            recipient_context={
                "pregnant": True, "role": "patient",
                "channel": "outbound_message",
                # extra unrelated facts the rule doesn't predicate on
                "preferred_language": "en",
                "internal_case_id": "C-1234",
            },
        )
        assert matches
        m = matches[0]
        assert set(m.matched_facts.keys()) == {"pregnant", "role", "channel"}
        assert "preferred_language" not in m.matched_facts
        assert "internal_case_id" not in m.matched_facts

    def test_alternate_role_values_in_predicate_set_all_fire(self):
        # The role predicate accepts (patient, client, consumer).
        # All three values should trigger.
        for role_value in ("patient", "client", "consumer"):
            matches = self._run(
                recipient_context={
                    "pregnant": True, "role": role_value,
                    "channel": "outbound_message",
                },
            )
            assert matches, f"role={role_value!r} should trigger the rule"


# --------------------------------------------------------------------------- #
# Architectural guardrail (the user's instinct on this slice was good)         #
# --------------------------------------------------------------------------- #

class TestArchitecturalBoundaryRespected:
    def test_typed_context_rule_uses_no_numeric_envelopes(self):
        # The user explicitly said: this slice does NOT build the
        # numeric Deterministic Bounded Control Evaluator. The
        # rule's fact_predicates should be categorical (bools, strs,
        # set memberships) — no numeric range predicates.
        from tcs.governance import TYPED_CONTEXT_RULES
        for rule in TYPED_CONTEXT_RULES:
            for key, pred in rule.fact_predicates.items():
                # Allowed: bool, str, int, tuple/list/set/frozenset.
                # Disallowed: any predicate object that would imply
                # a numeric comparison (e.g. a dict with min/max).
                assert not isinstance(pred, dict), (
                    f"rule {rule.rule_id} predicate {key!r} is a dict — "
                    "numeric range predicates belong to the bounded-"
                    "control evaluator, not the typed-context layer"
                )
