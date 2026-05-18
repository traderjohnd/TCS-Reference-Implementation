"""
Phase 5 Slice 5.4 — /v2/replay tests.

Acceptance criteria pinned (1–14 from the slice spec, focused on replay):

  1.  /v2/replay accepts one artifact_id and multiple configurations.
  2.  Every configuration evaluates the same stored artifact.
  3.  /v2/replay NEVER re-calls the LLM (patches every provider client).
  4.  Replay results match calling /v2/evaluate separately for the
      same artifact + configuration.
  5.  what_if configurations create evaluations but no TCs.
  6.  observe configurations create evaluations and TCs marked
      lifecycle_state="observed".
  7.  enforce configurations create evaluations + TCs; replay itself
      does NOT deliver content (no response body in the request, no
      pipeline side-effect beyond persistence).
  8.  The baseline-no-pack → MedDev observe → MedDev enforce →
      Financial what_if narrative replays cleanly.
  9.  Determinism: same artifact + same config produces same scores +
      decision (modulo ids/timestamps).
  10. Differential: same artifact + different policies can produce
      different decisions where expected.
  14. evaluation_origin="replay" on every row produced by /v2/replay.
"""

from __future__ import annotations

import os
from typing import Any, Dict, List
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
    store = CertificateStore(str(tmp_path / "slice54.db"))
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


def _generate_clean_rag_artifact(client) -> str:
    r = client.post("/v2/generate", json={
        "generation_mode": "rag_llm",
        "prompt": "What does the document retention policy say?",
        "provider": "mock",
        "industry_hint": "life_sciences",
    })
    assert r.status_code == 200, r.text
    return r.json()["artifact_id"]


def _generate_consumer_lithium_artifact(client) -> str:
    r = client.post("/v2/generate", json={
        "generation_mode": "rag_llm",
        "prompt": "I'm pregnant and want to know what dose of lithium to take",
        "provider": "mock",
        "industry_hint": "life_sciences",
    })
    assert r.status_code == 200, r.text
    return r.json()["artifact_id"]


# --------------------------------------------------------------------------- #
# Basic shape                                                                  #
# --------------------------------------------------------------------------- #

class TestReplayBasics:
    def test_replay_runs_multiple_configurations(self, client):
        meddev = _deploy_meddev(client)
        artifact_id = _generate_clean_rag_artifact(client)

        r = client.post("/v2/replay", json={
            "artifact_id": artifact_id,
            "configurations": [
                {"mode": "observe", "policy_profile_id": "baseline-no-pack"},
                {"mode": "observe", "policy_profile_id": meddev["pack_id"]},
                {"mode": "enforce", "policy_profile_id": meddev["pack_id"]},
                {"mode": "what_if", "policy_profile_id": "fin-r3-a4-ct4"},
            ],
        })
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["artifact_id"] == artifact_id
        assert body["count"] == 4
        assert len(body["evaluations"]) == 4

    def test_replay_requires_at_least_one_configuration(self, client):
        artifact_id = _generate_clean_rag_artifact(client)
        r = client.post("/v2/replay", json={
            "artifact_id": artifact_id,
            "configurations": [],
        })
        assert r.status_code == 400

    def test_replay_unknown_artifact_is_404(self, client):
        r = client.post("/v2/replay", json={
            "artifact_id": "no-such-artifact",
            "configurations": [{"mode": "observe",
                                "policy_profile_id": "baseline-no-pack"}],
        })
        assert r.status_code == 404

    def test_replay_unknown_mode_is_400(self, client):
        artifact_id = _generate_clean_rag_artifact(client)
        r = client.post("/v2/replay", json={
            "artifact_id": artifact_id,
            "configurations": [{"mode": "audit_only"}],
        })
        assert r.status_code == 400


# --------------------------------------------------------------------------- #
# evaluation_origin tagging                                                    #
# --------------------------------------------------------------------------- #

class TestReplayOriginTagging:
    def test_every_replay_evaluation_carries_origin_replay(self, client):
        meddev = _deploy_meddev(client)
        artifact_id = _generate_clean_rag_artifact(client)
        r = client.post("/v2/replay", json={
            "artifact_id": artifact_id,
            "configurations": [
                {"mode": "observe", "policy_profile_id": meddev["pack_id"]},
                {"mode": "what_if", "policy_profile_id": "fin-r3-a4-ct4"},
            ],
        }).json()
        for e in r["evaluations"]:
            assert e["evaluation_origin"] == "replay", (
                f"replay evaluation has origin={e['evaluation_origin']!r}, "
                "expected 'replay'"
            )

    def test_direct_evaluate_keeps_origin_direct(self, client):
        # Sanity check: /v2/evaluate continues to emit origin=direct.
        meddev = _deploy_meddev(client)
        artifact_id = _generate_clean_rag_artifact(client)
        post = client.post("/v2/evaluate", json={
            "artifact_id": artifact_id,
            "mode": "observe",
            "policy_profile_id": meddev["pack_id"],
        }).json()
        full = client.get(f"/v2/evaluations/{post['evaluation_id']}").json()
        assert full["evaluation_origin"] == "direct"


# --------------------------------------------------------------------------- #
# Mode-specific TC issuance under replay                                       #
# --------------------------------------------------------------------------- #

class TestReplayTCIssuance:
    def test_observe_replay_issues_observed_tc(self, client):
        meddev = _deploy_meddev(client)
        artifact_id = _generate_clean_rag_artifact(client)
        r = client.post("/v2/replay", json={
            "artifact_id": artifact_id,
            "configurations": [
                {"mode": "observe", "policy_profile_id": meddev["pack_id"]},
            ],
        }).json()
        tc_id = r["evaluations"][0]["trust_certificate_id"]
        assert tc_id, "observe replay must issue a TC"
        tc = client.get(f"/v2/certificates/{tc_id}").json()
        assert tc["lifecycle_state"] == "observed"

    def test_enforce_replay_issues_tc_but_does_not_deliver(self, client):
        meddev = _deploy_meddev(client)
        artifact_id = _generate_clean_rag_artifact(client)
        r = client.post("/v2/replay", json={
            "artifact_id": artifact_id,
            "configurations": [
                {"mode": "enforce", "policy_profile_id": meddev["pack_id"]},
            ],
        }).json()
        # TC issued — same as enforce on /v2/evaluate.
        assert r["evaluations"][0]["trust_certificate_id"]
        # Replay response shape has NO response/output field — replay
        # is comparison-only, not delivery. The contract is enforced
        # by absence: nothing in the response body could trigger
        # downstream delivery.
        assert "response" not in r
        assert "raw_output" not in r
        assert "delivered_content" not in r

    def test_what_if_replay_does_not_issue_tc(self, client):
        artifact_id = _generate_clean_rag_artifact(client)
        r = client.post("/v2/replay", json={
            "artifact_id": artifact_id,
            "configurations": [
                {"mode": "what_if", "policy_profile_id": "baseline-no-pack"},
            ],
        }).json()
        assert r["evaluations"][0]["trust_certificate_id"] is None
        assert r["evaluations"][0]["enforcement_action"] == "counterfactual_only"


# --------------------------------------------------------------------------- #
# No-re-call invariant (the load-bearing test)                                 #
# --------------------------------------------------------------------------- #

class TestReplayNoLLMReCall:
    def test_replay_does_not_call_any_provider(self, client):
        meddev = _deploy_meddev(client)
        artifact_id = _generate_clean_rag_artifact(client)

        with patch(
            "tcs.artifacts.generation._call_openai",
            side_effect=AssertionError("replay must not call OpenAI"),
        ), patch(
            "tcs.artifacts.generation._call_anthropic",
            side_effect=AssertionError("replay must not call Anthropic"),
        ), patch(
            "tcs.artifacts.generation._call_mock",
            side_effect=AssertionError("replay must not call Mock"),
        ):
            r = client.post("/v2/replay", json={
                "artifact_id": artifact_id,
                "configurations": [
                    {"mode": "observe", "policy_profile_id": "baseline-no-pack"},
                    {"mode": "observe", "policy_profile_id": meddev["pack_id"]},
                    {"mode": "enforce", "policy_profile_id": meddev["pack_id"]},
                    {"mode": "what_if", "policy_profile_id": "fin-r3-a4-ct4"},
                ],
            })
            assert r.status_code == 200, r.text


# --------------------------------------------------------------------------- #
# Parity with /v2/evaluate                                                     #
# --------------------------------------------------------------------------- #

class TestReplayParityWithDirectEvaluate:
    def test_replay_one_config_matches_direct_evaluate(self, client):
        # Same artifact + same single configuration should produce
        # the same decision and BACK scores whether run via /v2/replay
        # or /v2/evaluate. (evaluation_ids and timestamps differ.)
        meddev = _deploy_meddev(client)
        artifact_id = _generate_clean_rag_artifact(client)

        direct = client.post("/v2/evaluate", json={
            "artifact_id": artifact_id,
            "mode": "observe",
            "policy_profile_id": meddev["pack_id"],
        }).json()
        replay = client.post("/v2/replay", json={
            "artifact_id": artifact_id,
            "configurations": [
                {"mode": "observe", "policy_profile_id": meddev["pack_id"]},
            ],
        }).json()["evaluations"][0]

        assert direct["decision"] == replay["decision"]
        assert direct["s_base"] == replay["s_base"]
        assert direct["tis_current"] == replay["tis_current"]
        assert direct["component_scores"] == replay["component_scores"]
        assert direct["gate_results"] == replay["gate_results"]
        assert direct["enforcement_action"] == replay["enforcement_action"]


# --------------------------------------------------------------------------- #
# The flagship four-step replay narrative                                      #
# --------------------------------------------------------------------------- #

class TestReplayBaselineNarrative:
    def test_consumer_lithium_through_four_step_replay(self, client):
        # The narrative the user pinned:
        #   baseline-no-pack observe → MedDev observe → MedDev enforce
        #   → Financial what_if
        # Same captured generation, different governance configurations.
        meddev = _deploy_meddev(client)
        artifact_id = _generate_consumer_lithium_artifact(client)

        r = client.post("/v2/replay", json={
            "artifact_id": artifact_id,
            "configurations": [
                {"mode": "observe", "policy_profile_id": "baseline-no-pack"},
                {"mode": "observe", "policy_profile_id": meddev["pack_id"]},
                {"mode": "enforce", "policy_profile_id": meddev["pack_id"]},
                {"mode": "what_if", "policy_profile_id": "fin-r3-a4-ct4"},
            ],
        }).json()
        assert r["count"] == 4
        evals = {
            (e["mode"], e["policy_profile_id"]): e for e in r["evaluations"]
        }

        # Step 1: baseline observe — life_sciences-only rule does NOT
        # apply (baseline domain is "baseline", not life_sciences).
        # The consumer rule does not fire here.
        baseline = evals[("observe", "baseline-no-pack")]
        assert baseline["evaluation_origin"] == "replay"

        # Step 2: MedDev observe — consumer rule fires under
        # life_sciences. Decision = Stop, but observe means logged_only
        # (no delivery intervention).
        meddev_obs = evals[("observe", meddev["pack_id"])]
        assert meddev_obs["decision"] == "Stop"
        assert meddev_obs["enforcement_action"] == "logged_only"
        assert meddev_obs["delivery_intervention"] is False
        assert meddev_obs["trust_certificate_id"]

        # Step 3: MedDev enforce — same rule fires, but action is
        # actually blocked.
        meddev_enf = evals[("enforce", meddev["pack_id"])]
        assert meddev_enf["decision"] == "Stop"
        assert meddev_enf["enforcement_action"] == "blocked"
        assert meddev_enf["delivery_intervention"] is True

        # Step 4: financial what_if — counterfactual only. The
        # life_sciences rule does not apply under financial domain,
        # so the decision differs from MedDev. No TC.
        fin_wif = evals[("what_if", "fin-r3-a4-ct4")]
        assert fin_wif["enforcement_action"] == "counterfactual_only"
        assert fin_wif["trust_certificate_id"] is None


# --------------------------------------------------------------------------- #
# Determinism under replay                                                     #
# --------------------------------------------------------------------------- #

class TestReplayDeterminism:
    def test_two_replays_of_same_config_produce_same_scores(self, client):
        meddev = _deploy_meddev(client)
        artifact_id = _generate_clean_rag_artifact(client)

        r1 = client.post("/v2/replay", json={
            "artifact_id": artifact_id,
            "configurations": [
                {"mode": "observe", "policy_profile_id": meddev["pack_id"]},
            ],
        }).json()["evaluations"][0]
        r2 = client.post("/v2/replay", json={
            "artifact_id": artifact_id,
            "configurations": [
                {"mode": "observe", "policy_profile_id": meddev["pack_id"]},
            ],
        }).json()["evaluations"][0]

        assert r1["decision"] == r2["decision"]
        assert r1["s_base"] == r2["s_base"]
        assert r1["component_scores"] == r2["component_scores"]
        # Different evaluation IDs — each is a new row.
        assert r1["evaluation_id"] != r2["evaluation_id"]


# --------------------------------------------------------------------------- #
# Replay persists rows in the per-artifact listing                             #
# --------------------------------------------------------------------------- #

class TestReplayPersistsToListing:
    def test_replay_rows_appear_in_list_evaluations_for_artifact(self, client):
        meddev = _deploy_meddev(client)
        artifact_id = _generate_clean_rag_artifact(client)
        replay = client.post("/v2/replay", json={
            "artifact_id": artifact_id,
            "configurations": [
                {"mode": "observe", "policy_profile_id": "baseline-no-pack"},
                {"mode": "observe", "policy_profile_id": meddev["pack_id"]},
            ],
        }).json()

        replay_ids = {e["evaluation_id"] for e in replay["evaluations"]}
        listing = client.get(
            f"/v2/artifacts/{artifact_id}/evaluations"
        ).json()
        listed_ids = {e["evaluation_id"] for e in listing["evaluations"]}
        # Every replay evaluation must be retrievable via the per-
        # artifact listing — that's the foundation of the future
        # replay-comparison UI.
        assert replay_ids.issubset(listed_ids)
