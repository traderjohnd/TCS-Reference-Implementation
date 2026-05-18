"""
Phase 5 Slice 5.5 — Human-Composed Workflow Polish.

Promotes `human_composed` from a generation-mode entry to a fully
first-class runtime sidecar use case. The flagship scenario is a
human rep drafting an outbound message to a pregnant client about
lithium; TCS evaluates the draft BEFORE send and either holds or
stops it.

Acceptance criteria pinned (1-10 from the slice spec):

  1. human_composed is treated as a first-class runtime sidecar
     use case (no LLM involvement at any stage).
  2. No LLM call occurs in generation OR evaluation OR replay.
  3. Human draft becomes a persisted ResponseArtifact.
  4. recipient_context is captured on the artifact verbatim.
  5. /v2/evaluate evaluates the human draft through the same
     governance pipeline as AI-generated artifacts.
  6. observe / enforce / what_if modes all work.
  7. Lithium/pregnancy outbound-message scenario pinned end-to-end.
  8. Artifact, evaluation, TC, and governance rule matches all link.
  9. The TC clearly shows this was human_composed, not LLM-generated.
 10. Full suite remains green.

Boundary the user pinned: numeric device-envelope evaluation is
DEFERRED to the bounded-control evaluator slice. This slice uses
the existing term-group classifier + BACK/TIS logic. The lithium
draft may decide HOLD via attribution gate failure (raw human
content has no automated retrieval, so A defaults to ~0.70 and
fails MedDev's A >= 0.85 gate) — that is correct and intended
behavior under the current rule set.
"""

from __future__ import annotations

import os
from typing import Any, Dict
from unittest.mock import patch

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
    store = CertificateStore(str(tmp_path / "slice55.db"))
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


def _draft_lithium_outbound(client) -> str:
    """Generate the flagship lithium/pregnancy human-composed artifact."""
    r = client.post("/v2/generate", json={
        "generation_mode": "human_composed",
        "draft": "Lithium is fine in small doses.",
        "recipient_context": {
            "pregnant": True,
            "role": "patient",
            "channel": "outbound_message",
            "medication_topic": "lithium",
        },
        "generation_identity": {
            "requesting_identity": "rep-007",
            "identity_type": "human",
            "role": "patient_support_rep",
            "session_id": "support-session-42",
        },
    })
    assert r.status_code == 200, r.text
    return r.json()["artifact_id"]


# --------------------------------------------------------------------------- #
# 1 + 2 + 3 + 4 — generation captures the draft + context, no LLM             #
# --------------------------------------------------------------------------- #

class TestHumanComposedGeneration:
    def test_generation_creates_artifact_without_calling_llm(self, client):
        # No provider clients may be invoked for human_composed.
        with patch(
            "tcs.artifacts.generation._call_openai",
            side_effect=AssertionError("must not call OpenAI"),
        ), patch(
            "tcs.artifacts.generation._call_anthropic",
            side_effect=AssertionError("must not call Anthropic"),
        ), patch(
            "tcs.artifacts.generation._call_mock",
            side_effect=AssertionError("must not call Mock"),
        ):
            artifact_id = _draft_lithium_outbound(client)
        assert artifact_id

    def test_generation_records_no_llm_fields(self, client):
        artifact_id = _draft_lithium_outbound(client)
        full = client.get(f"/v2/artifacts/{artifact_id}").json()
        # No provider, model, system prompt, RAG context, retrieved sources.
        assert full["provider"] is None
        assert full["model"] is None
        assert full["system_prompt_used"] is None
        assert full["rag_enabled"] is False
        assert full["rag_context"] is None
        assert full["retrieved_sources"] == []
        # Draft becomes the raw_output.
        assert full["raw_output"] == "Lithium is fine in small doses."

    def test_generation_records_recipient_context_verbatim(self, client):
        artifact_id = _draft_lithium_outbound(client)
        full = client.get(f"/v2/artifacts/{artifact_id}").json()
        rc = full["recipient_context"]
        assert rc["pregnant"] is True
        assert rc["role"] == "patient"
        assert rc["channel"] == "outbound_message"
        assert rc["medication_topic"] == "lithium"

    def test_generation_records_human_identity(self, client):
        artifact_id = _draft_lithium_outbound(client)
        full = client.get(f"/v2/artifacts/{artifact_id}").json()
        identity = full["generation_identity"]
        assert identity["identity_type"] == "human"
        assert identity["requesting_identity"] == "rep-007"


# --------------------------------------------------------------------------- #
# 5 + 6 — evaluation in all three modes                                        #
# --------------------------------------------------------------------------- #

class TestHumanComposedEvaluationModes:
    def test_observe_mode_evaluates_and_issues_observed_tc(self, client):
        deployed = _deploy_meddev(client)
        artifact_id = _draft_lithium_outbound(client)
        r = client.post("/v2/evaluate", json={
            "artifact_id": artifact_id,
            "mode": "observe",
            "policy_profile_id": deployed["pack_id"],
        })
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["enforcement_action"] == "logged_only"
        assert body["delivery_intervention"] is False
        assert body["trust_certificate_id"]
        # TC is marked observed (Slice 5.3 lifecycle override).
        tc = client.get(f"/v2/certificates/{body['trust_certificate_id']}").json()
        assert tc["lifecycle_state"] == "observed"

    def test_enforce_mode_drives_action_from_decision(self, client):
        deployed = _deploy_meddev(client)
        artifact_id = _draft_lithium_outbound(client)
        r = client.post("/v2/evaluate", json={
            "artifact_id": artifact_id,
            "mode": "enforce",
            "policy_profile_id": deployed["pack_id"],
        }).json()
        # The action derives from the decision via the standard table.
        # For a human_composed draft about lithium dosing under MedDev,
        # attribution defaults are low (no automated retrieval) so the
        # A gate is likely to fail → HOLD or STOP. Either is acceptable
        # per the slice spec; we pin the action shape rather than the
        # exact decision value.
        assert r["decision"] in ("Hold", "Escalate", "Stop")
        assert r["enforcement_action"] in ("held", "escalated", "blocked")
        assert r["delivery_intervention"] is True

    def test_what_if_mode_creates_evaluation_no_tc(self, client):
        deployed = _deploy_meddev(client)
        artifact_id = _draft_lithium_outbound(client)
        r = client.post("/v2/evaluate", json={
            "artifact_id": artifact_id,
            "mode": "what_if",
            "policy_profile_id": deployed["pack_id"],
        }).json()
        assert r["enforcement_action"] == "counterfactual_only"
        assert r["trust_certificate_id"] is None


# --------------------------------------------------------------------------- #
# Evaluation NEVER calls an LLM (the load-bearing test)                        #
# --------------------------------------------------------------------------- #

class TestHumanComposedEvaluationNeverCallsLLM:
    def test_evaluation_never_invokes_provider_clients(self, client):
        deployed = _deploy_meddev(client)
        artifact_id = _draft_lithium_outbound(client)
        with patch(
            "tcs.artifacts.generation._call_openai",
            side_effect=AssertionError("must not call OpenAI"),
        ), patch(
            "tcs.artifacts.generation._call_anthropic",
            side_effect=AssertionError("must not call Anthropic"),
        ), patch(
            "tcs.artifacts.generation._call_mock",
            side_effect=AssertionError("must not call Mock"),
        ):
            for mode in ("observe", "enforce", "what_if"):
                r = client.post("/v2/evaluate", json={
                    "artifact_id": artifact_id,
                    "mode": mode,
                    "policy_profile_id": deployed["pack_id"],
                })
                assert r.status_code == 200, f"{mode}: {r.text}"


# --------------------------------------------------------------------------- #
# Replay includes human_composed artifacts                                     #
# --------------------------------------------------------------------------- #

class TestHumanComposedReplay:
    def test_replay_works_for_human_composed_artifact(self, client):
        deployed = _deploy_meddev(client)
        artifact_id = _draft_lithium_outbound(client)
        r = client.post("/v2/replay", json={
            "artifact_id": artifact_id,
            "configurations": [
                {"mode": "observe", "policy_profile_id": "baseline-no-pack"},
                {"mode": "observe", "policy_profile_id": deployed["pack_id"]},
                {"mode": "enforce", "policy_profile_id": deployed["pack_id"]},
                {"mode": "what_if", "policy_profile_id": "fin-r3-a4-ct4"},
            ],
        })
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["count"] == 4
        # Every replay evaluation tagged origin="replay" — same audit
        # discipline as AI-artifact replays.
        for e in body["evaluations"]:
            assert e["evaluation_origin"] == "replay"

    def test_replay_never_calls_llm_for_human_composed(self, client):
        deployed = _deploy_meddev(client)
        artifact_id = _draft_lithium_outbound(client)
        with patch(
            "tcs.artifacts.generation._call_openai",
            side_effect=AssertionError("must not call OpenAI"),
        ), patch(
            "tcs.artifacts.generation._call_anthropic",
            side_effect=AssertionError("must not call Anthropic"),
        ), patch(
            "tcs.artifacts.generation._call_mock",
            side_effect=AssertionError("must not call Mock"),
        ):
            r = client.post("/v2/replay", json={
                "artifact_id": artifact_id,
                "configurations": [
                    {"mode": "observe", "policy_profile_id": deployed["pack_id"]},
                    {"mode": "enforce", "policy_profile_id": deployed["pack_id"]},
                ],
            })
            assert r.status_code == 200, r.text


# --------------------------------------------------------------------------- #
# Governance evidence: TC clearly shows source as human-composed               #
# --------------------------------------------------------------------------- #

class TestHumanComposedTCEvidence:
    def test_tc_subject_type_is_human_composed(self, client):
        deployed = _deploy_meddev(client)
        artifact_id = _draft_lithium_outbound(client)
        r = client.post("/v2/evaluate", json={
            "artifact_id": artifact_id,
            "mode": "enforce",
            "policy_profile_id": deployed["pack_id"],
        }).json()
        tc = client.get(f"/v2/certificates/{r['trust_certificate_id']}").json()
        # The TC carries the generation mode via subject_type so a
        # reviewer can tell at-a-glance the source was a human draft.
        assert tc["subject_type"] == "human_composed", (
            f"TC must mark subject_type='human_composed' for human "
            f"drafts; got {tc['subject_type']!r}"
        )

    def test_tc_explanation_calls_out_human_composed_explicitly(self, client):
        # Slice 5.5 polish: the human-readable explanation_summary
        # should say "Human-composed draft message" instead of the
        # generic "Subject 'X' (human_composed) evaluated against".
        deployed = _deploy_meddev(client)
        artifact_id = _draft_lithium_outbound(client)
        r = client.post("/v2/evaluate", json={
            "artifact_id": artifact_id,
            "mode": "enforce",
            "policy_profile_id": deployed["pack_id"],
        }).json()
        tc = client.get(f"/v2/certificates/{r['trust_certificate_id']}").json()
        summary = tc["explanation_summary"]
        assert "human-composed draft message" in summary.lower(), (
            f"explanation_summary should call out human-composed; got: "
            f"{summary!r}"
        )
        assert "no llm in the loop" in summary.lower(), (
            "explanation should make explicit that no LLM was involved"
        )

    def test_tc_subject_id_links_back_to_artifact(self, client):
        deployed = _deploy_meddev(client)
        artifact_id = _draft_lithium_outbound(client)
        r = client.post("/v2/evaluate", json={
            "artifact_id": artifact_id,
            "mode": "enforce",
            "policy_profile_id": deployed["pack_id"],
        }).json()
        tc = client.get(f"/v2/certificates/{r['trust_certificate_id']}").json()
        # TC.subject_id IS the artifact_id — the audit can walk
        # from TC -> artifact in one hop.
        assert tc["subject_id"] == artifact_id


# --------------------------------------------------------------------------- #
# Artifact + evaluation + TC + governance rule matches all link                #
# --------------------------------------------------------------------------- #

class TestHumanComposedLinkage:
    def test_full_chain_is_traversable(self, client):
        deployed = _deploy_meddev(client)
        artifact_id = _draft_lithium_outbound(client)
        eval_r = client.post("/v2/evaluate", json={
            "artifact_id": artifact_id,
            "mode": "enforce",
            "policy_profile_id": deployed["pack_id"],
        }).json()

        # 1. Artifact retrievable.
        artifact = client.get(f"/v2/artifacts/{artifact_id}").json()
        assert artifact["generation_mode"] == "human_composed"

        # 2. Evaluation retrievable + carries the artifact_id link.
        evaluation = client.get(
            f"/v2/evaluations/{eval_r['evaluation_id']}"
        ).json()
        assert evaluation["artifact_id"] == artifact_id

        # 3. TC retrievable + linked from evaluation.
        tc = client.get(f"/v2/certificates/{eval_r['trust_certificate_id']}").json()
        assert evaluation["trust_certificate_id"] == tc["certificate_id"]

        # 4. Evaluation list-for-artifact contains this evaluation.
        listing = client.get(
            f"/v2/artifacts/{artifact_id}/evaluations"
        ).json()
        eval_ids = [e["evaluation_id"] for e in listing["evaluations"]]
        assert eval_r["evaluation_id"] in eval_ids

        # 5. governance_rule_matches field exists on the TC (list may
        # be empty if no rule fired for the draft; that's fine — the
        # field's presence is what matters for the audit shape).
        assert "governance_rule_matches" in tc

        # 6. policy_profile_snapshot is on the evaluation (audit-grade
        # reproducibility per Slice 5.3 D4).
        assert evaluation["policy_profile_snapshot"]["profile_id"] == (
            deployed["pack_id"]
        )

        # 7. governance_input_snapshot is on the evaluation (Slice
        # 5.4a replay fidelity).
        assert evaluation["governance_input_snapshot"] is not None


# --------------------------------------------------------------------------- #
# Replay determinism for human_composed (Slice 5.4a + 5.5)                     #
# --------------------------------------------------------------------------- #

class TestHumanComposedReplayDeterminism:
    def test_same_human_artifact_same_policy_replays_identically(self, client):
        # The Slice 5.4a guarantee extended to human_composed:
        # generate once, replay against the captured snapshot, get
        # identical scores and decision.
        deployed = _deploy_meddev(client)
        artifact_id = _draft_lithium_outbound(client)

        first = client.post("/v2/evaluate", json={
            "artifact_id": artifact_id,
            "mode": "enforce",
            "policy_profile_id": deployed["pack_id"],
        }).json()

        # Second call defaults to runtime_snapshot strategy (per
        # 5.4a auto-resolver) because the first call wrote a snapshot.
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
        assert first["gate_results"] == second["gate_results"]


# --------------------------------------------------------------------------- #
# The flagship end-to-end lithium / pregnancy scenario                         #
# --------------------------------------------------------------------------- #

class TestLithiumPregnancyOutboundScenario:
    """
    End-to-end pinning of the scenario the user named:

        human draft: "Lithium is fine in small doses."
        recipient_context: pregnant client / patient / outbound message
        active MedDev policy
        expected: HOLD or STOP with clear blocking reason

    The current rule layer's term-group classifier does not have an
    "outbound-to-pregnant-patient" rule (rep-to-consumer dosing
    advice is a typed-fact problem, deferred to the bounded-control
    evaluator). What the current pipeline catches:

      - human_composed artifact has no automated retrieval, so the
        attribution default is ~0.70.
      - MedDev's A gate threshold is 0.85+ (r3/a4).
      - A=0.70 < 0.85 → A gate fails.
      - On a gate failure, the decision ladder uses S_base vs kappa.
        With the human_composed default scores (B=1.0, A=0.70, C=1.0,
        K=0.95), S_base is well above kappa → HOLD.

    So the runtime answer today is HOLD via attribution gate failure
    under MedDev's strict threshold. The TC's blocking_reason names
    the failed gate, the recipient_context is captured for audit, and
    a future bounded-control evaluator can refine to STOP via typed-
    facts (role=patient + channel=outbound_message + pregnant=True +
    medication_topic=lithium + dose-asserting language in draft).
    """

    def test_scenario_produces_hold_or_stop_under_meddev(self, client):
        deployed = _deploy_meddev(client)
        artifact_id = _draft_lithium_outbound(client)
        r = client.post("/v2/evaluate", json={
            "artifact_id": artifact_id,
            "mode": "enforce",
            "policy_profile_id": deployed["pack_id"],
        }).json()
        assert r["decision"] in ("Hold", "Escalate", "Stop"), (
            f"lithium outbound under MedDev expected HOLD/STOP/ESCALATE; "
            f"got {r['decision']!r}"
        )
        # And the action actually intervened in delivery.
        assert r["delivery_intervention"] is True

    def test_scenario_blocking_reason_is_clear(self, client):
        deployed = _deploy_meddev(client)
        artifact_id = _draft_lithium_outbound(client)
        r = client.post("/v2/evaluate", json={
            "artifact_id": artifact_id,
            "mode": "enforce",
            "policy_profile_id": deployed["pack_id"],
        }).json()
        tc = client.get(f"/v2/certificates/{r['trust_certificate_id']}").json()
        blocking = tc.get("blocking_reason") or ""
        # Should name either the failed gate (current behavior) or
        # the rule that fired (if a rule does end up matching). Both
        # are acceptable "clear blocking reasons."
        assert blocking, "blocking_reason must be present for HOLD/STOP"
        # And the artifact's full recipient_context is captured on
        # the artifact for an auditor to read.
        artifact = client.get(f"/v2/artifacts/{artifact_id}").json()
        assert artifact["recipient_context"]["pregnant"] is True
        assert artifact["recipient_context"]["role"] == "patient"
        assert artifact["recipient_context"]["channel"] == "outbound_message"
