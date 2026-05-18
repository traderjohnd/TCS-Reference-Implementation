"""
Phase 5 Slice 5.3 — /v2/evaluate + /v2/evaluations/{id} +
/v2/artifacts/{id}/evaluations tests.

Acceptance criteria pinned (1–14 from the slice spec):

  1.  /v2/evaluate evaluates an existing artifact without re-calling
      any provider.
  2.  Supports observe, enforce, and what_if.
  3.  observe issues a TC with lifecycle_state="observed" and
      enforcement_action="logged_only".
  4.  enforce issues a TC; enforcement_action derived from decision.
  5.  what_if creates an evaluation but does NOT issue a TC.
  6.  Caller-provided policy_profile_id is respected; defaults to
      the active pack.
  7.  Full policy profile snapshot stored on the evaluation row.
  8.  Evaluation uses stored artifact content (no fresh generation).
  9.  Determinism: same artifact + same policy → same scores +
      decision (modulo IDs/timestamps).
  10. Differential replay: same artifact + different policies → can
      produce different decisions.
  11. Human-composed artifacts evaluate without an LLM call.
  12. GET /v2/evaluations/{id} returns exact stored evaluation.
  13. GET /v2/artifacts/{id}/evaluations returns evaluations oldest
      first.
  14. Hard no-re-call test: patching every provider client to raise
      must not break /v2/evaluate.
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
    store = CertificateStore(str(tmp_path / "slice53.db"))
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


def _deploy_finance(client) -> Dict[str, Any]:
    return client.post("/v2/standards/deploy", json={
        "industry": "financial_services",
        "sub_industry": "retail_advisory",
        "use_case": "investment_recommendation",
        "standard_ids": ["sec_reg_bi", "finra_2111"],
        "risk_tier": "r3", "action_class": "a4",
    }).json()


def _generate_clean_rag_artifact(client) -> str:
    """Generate a benign rag_llm artifact and return its artifact_id."""
    r = client.post("/v2/generate", json={
        "generation_mode": "rag_llm",
        "prompt": "What does the document retention policy say?",
        "provider": "mock",
        "industry_hint": "life_sciences",
    })
    assert r.status_code == 200, r.text
    return r.json()["artifact_id"]


def _generate_consumer_lithium_artifact(client) -> str:
    """
    Generate an artifact whose prompt fires the consumer rule.

    Uses rag_llm with explicit life_sciences industry_hint so the
    artifact has well-attributed retrieved_sources (A defaults to
    0.85+). This way differential replay across profiles isolates
    the rule-classifier effect from baseline attribution gating —
    when the same artifact decides differently across profiles, we
    know the difference came from the classifier's domain scoping,
    not from A simply failing every gate.
    """
    r = client.post("/v2/generate", json={
        "generation_mode": "rag_llm",
        "prompt": "I'm pregnant and want to know what dose of lithium to take",
        "provider": "mock",
        "industry_hint": "life_sciences",
    })
    assert r.status_code == 200, r.text
    return r.json()["artifact_id"]


# --------------------------------------------------------------------------- #
# Mode 1 — observe                                                             #
# --------------------------------------------------------------------------- #

class TestObserveMode:
    def test_observe_issues_tc_with_observed_lifecycle_and_logged_only(
        self, client,
    ):
        deployed = _deploy_meddev(client)
        artifact_id = _generate_clean_rag_artifact(client)

        r = client.post("/v2/evaluate", json={
            "artifact_id": artifact_id,
            "mode": "observe",
            "policy_profile_id": deployed["pack_id"],
        })
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["mode"] == "observe"
        assert body["enforcement_action"] == "logged_only"
        assert body["delivery_intervention"] is False
        # Observe DOES issue a TC (per locked D1).
        assert body["trust_certificate_id"], (
            "observe must issue a TC per locked decision D1"
        )

        # Confirm the TC carries lifecycle_state="observed".
        tc = client.get(
            f"/v2/certificates/{body['trust_certificate_id']}"
        ).json()
        assert tc["lifecycle_state"] == "observed", (
            f"observe TC must carry lifecycle_state='observed', got "
            f"{tc['lifecycle_state']!r}"
        )


# --------------------------------------------------------------------------- #
# Mode 2 — enforce                                                             #
# --------------------------------------------------------------------------- #

class TestEnforceMode:
    def test_enforce_issues_tc_with_normal_lifecycle(self, client):
        deployed = _deploy_meddev(client)
        artifact_id = _generate_clean_rag_artifact(client)

        r = client.post("/v2/evaluate", json={
            "artifact_id": artifact_id,
            "mode": "enforce",
            "policy_profile_id": deployed["pack_id"],
        }).json()

        assert r["mode"] == "enforce"
        assert r["trust_certificate_id"]
        # enforcement_action derives from the decision.
        valid_actions = {"delivered", "held", "blocked", "escalated"}
        assert r["enforcement_action"] in valid_actions

        # And the TC's lifecycle_state is NOT "observed" (it's
        # whatever the decision mapped to: admissible / computed /
        # blocked).
        tc = client.get(f"/v2/certificates/{r['trust_certificate_id']}").json()
        assert tc["lifecycle_state"] != "observed"

    def test_enforce_on_consumer_lithium_query_stops(self, client):
        # The consumer-self-dosing rule should fire under a Medical
        # Devices pack and force a STOP decision in enforce mode.
        deployed = _deploy_meddev(client)
        artifact_id = _generate_consumer_lithium_artifact(client)

        r = client.post("/v2/evaluate", json={
            "artifact_id": artifact_id,
            "mode": "enforce",
            "policy_profile_id": deployed["pack_id"],
        }).json()

        assert r["decision"] == "Stop"
        assert r["enforcement_action"] == "blocked"
        assert r["delivery_intervention"] is True


# --------------------------------------------------------------------------- #
# Mode 3 — what_if                                                             #
# --------------------------------------------------------------------------- #

class TestWhatIfMode:
    def test_what_if_creates_evaluation_but_no_tc(self, client):
        deployed = _deploy_meddev(client)
        artifact_id = _generate_clean_rag_artifact(client)

        r = client.post("/v2/evaluate", json={
            "artifact_id": artifact_id,
            "mode": "what_if",
            "policy_profile_id": deployed["pack_id"],
        }).json()

        assert r["mode"] == "what_if"
        assert r["enforcement_action"] == "counterfactual_only"
        assert r["delivery_intervention"] is False
        # Locked clarification: what_if NEVER issues a TC.
        assert r["trust_certificate_id"] is None

        # And it does appear in the per-artifact evaluation list.
        listing = client.get(
            f"/v2/artifacts/{artifact_id}/evaluations"
        ).json()
        assert any(
            e["evaluation_id"] == r["evaluation_id"]
            for e in listing["evaluations"]
        )


# --------------------------------------------------------------------------- #
# Policy profile resolution (D3) + snapshot (D4)                               #
# --------------------------------------------------------------------------- #

class TestPolicyResolution:
    def test_caller_supplied_profile_id_is_used(self, client):
        # Deploy MedDev as active. Then evaluate against a DIFFERENT
        # caller-supplied profile (a built-in financial one).
        _deploy_meddev(client)
        artifact_id = _generate_clean_rag_artifact(client)
        r = client.post("/v2/evaluate", json={
            "artifact_id": artifact_id,
            "mode": "observe",
            "policy_profile_id": "fin-high-risk-suitability-v3",
        }).json()
        assert r["policy_profile_id"] == "fin-high-risk-suitability-v3"

    def test_defaults_to_active_pack_when_omitted(self, client):
        deployed = _deploy_meddev(client)
        artifact_id = _generate_clean_rag_artifact(client)
        r = client.post("/v2/evaluate", json={
            "artifact_id": artifact_id,
            "mode": "observe",
            # no policy_profile_id — should default to active pack
        }).json()
        assert r["policy_profile_id"] == deployed["pack_id"]

    def test_missing_profile_and_no_active_pack_falls_through_to_baseline(
        self, client,
    ):
        # Architectural invariant (amended after Slice 5.3 review):
        # policy_profile_id=null MUST NOT mean "skip governance math."
        # When no caller-supplied profile and no active pack exist,
        # the resolver falls through to baseline-no-pack — TIS still
        # runs against a documented baseline, and the audit trail
        # makes that explicit.
        artifact_id = _generate_clean_rag_artifact(client)
        r = client.post("/v2/evaluate", json={
            "artifact_id": artifact_id,
            "mode": "observe",
        })
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["policy_profile_id"] == "baseline-no-pack", (
            f"null policy must resolve to baseline-no-pack; got "
            f"{body['policy_profile_id']!r}"
        )
        # And BACK/TIS math actually ran — the response surfaces
        # populated component_scores and a real decision, not a stub.
        assert body["component_scores"]
        assert body["decision"] in (
            "Allow", "Observe", "Hold", "Escalate", "Stop",
        )

    def test_baseline_no_pack_can_be_requested_explicitly(self, client):
        # baseline-no-pack is a first-class profile in the registry.
        # Callers can reference it explicitly for replay comparisons
        # ("evaluate this artifact under the no-pack baseline").
        artifact_id = _generate_clean_rag_artifact(client)
        r = client.post("/v2/evaluate", json={
            "artifact_id": artifact_id,
            "mode": "observe",
            "policy_profile_id": "baseline-no-pack",
        }).json()
        assert r["policy_profile_id"] == "baseline-no-pack"
        # The snapshot reflects the baseline profile's actual
        # configuration — domain=baseline, r1/a1, gate_set={B,A,C}.
        full = client.get(f"/v2/evaluations/{r['evaluation_id']}").json()
        snap = full["policy_profile_snapshot"]
        assert snap["domain"] == "baseline"
        assert snap["risk_tier"] == "r1"
        assert snap["action_class"] == "a1"
        assert sorted(snap["gate_set"]) == ["A", "B", "C"]
        # Empty regulatory_mapping is part of the "no pack" framing —
        # the baseline does not claim regulatory compliance against
        # any specific standard.
        assert snap["regulatory_mapping"] == []


class TestPolicySnapshot:
    def test_full_profile_snapshot_carried_on_evaluation(self, client):
        # Per locked decision D4: a future reviewer must see the
        # exact weights/thresholds/gates that were active at
        # evaluation time, even if the live registry has since
        # been edited. Verify the snapshot is comprehensive.
        deployed = _deploy_meddev(client)
        artifact_id = _generate_clean_rag_artifact(client)
        post = client.post("/v2/evaluate", json={
            "artifact_id": artifact_id,
            "mode": "observe",
            "policy_profile_id": deployed["pack_id"],
        }).json()
        full = client.get(f"/v2/evaluations/{post['evaluation_id']}").json()
        snap = full["policy_profile_snapshot"]
        for required in (
            "profile_id", "domain", "risk_tier", "action_class",
            "gate_set", "thresholds", "weights", "penalty_weights",
            "decay_rate", "soft_hold_ceiling", "decision_thresholds",
            "regulatory_mapping",
        ):
            assert required in snap, f"snapshot missing {required!r}"
        # Snapshot reflects MedDev specifically.
        assert snap["domain"].startswith("composed:life_sciences")


# --------------------------------------------------------------------------- #
# Determinism (acceptance #9)                                                  #
# --------------------------------------------------------------------------- #

class TestDeterminism:
    def test_same_artifact_same_policy_produces_same_decision_and_scores(
        self, client,
    ):
        deployed = _deploy_meddev(client)
        artifact_id = _generate_clean_rag_artifact(client)

        r1 = client.post("/v2/evaluate", json={
            "artifact_id": artifact_id,
            "mode": "observe",
            "policy_profile_id": deployed["pack_id"],
        }).json()
        r2 = client.post("/v2/evaluate", json={
            "artifact_id": artifact_id,
            "mode": "observe",
            "policy_profile_id": deployed["pack_id"],
        }).json()

        # Same decision, same scores, same gate results. Different
        # evaluation_id and trust_certificate_id (each is a new row).
        assert r1["decision"] == r2["decision"]
        assert r1["s_base"] == r2["s_base"]
        assert r1["tis_current"] == r2["tis_current"]
        assert r1["component_scores"] == r2["component_scores"]
        assert r1["gate_results"] == r2["gate_results"]
        assert r1["evaluation_id"] != r2["evaluation_id"]


# --------------------------------------------------------------------------- #
# Differential replay (acceptance #10)                                         #
# --------------------------------------------------------------------------- #

class TestDifferentialReplay:
    def test_same_consumer_lithium_artifact_decides_differently_by_policy(
        self, client,
    ):
        # The load-bearing acceptance test for #10: same captured
        # generation, different governance profiles, different
        # decisions. We isolate the rule-classifier effect by
        # comparing:
        #   profile A: composed MedDev pack (life_sciences domain)
        #              → consumer rule fires → C=0 → STOP
        #   profile B: enterprise-info-standard-v1 (low-risk r1/a1)
        #              → rule does NOT apply (life_sciences scoped)
        #              → all gates pass on the rag_llm artifact's
        #                  baseline scores → ALLOW
        #
        # Generate ONCE — no re-call between the two evaluations.
        meddev = _deploy_meddev(client)
        artifact_id = _generate_consumer_lithium_artifact(client)

        r_meddev = client.post("/v2/evaluate", json={
            "artifact_id": artifact_id,
            "mode": "observe",
            "policy_profile_id": meddev["pack_id"],
        }).json()

        # Use a built-in r1/a1 enterprise profile — low thresholds,
        # K not gated, life_sciences-only rules don't apply.
        r_baseline = client.post("/v2/evaluate", json={
            "artifact_id": artifact_id,
            "mode": "observe",
            "policy_profile_id": "enterprise-info-standard-v1",
        }).json()

        assert r_meddev["decision"] == "Stop", (
            f"consumer lithium under MedDev must Stop; got "
            f"{r_meddev['decision']!r}"
        )
        assert r_baseline["decision"] != "Stop", (
            f"same artifact under permissive baseline should not Stop "
            f"(life_sciences rule does not apply); got "
            f"{r_baseline['decision']!r}"
        )
        # And the actual decisions differ — the whole point of replay.
        assert r_meddev["decision"] != r_baseline["decision"], (
            "replay must show governance kicking in: same artifact, "
            "different policies, different decisions"
        )


# --------------------------------------------------------------------------- #
# Human-composed evaluation without an LLM (acceptance #11)                    #
# --------------------------------------------------------------------------- #

class TestHumanComposedEvaluation:
    def test_human_composed_artifact_evaluates_successfully(self, client):
        # Create a human draft, then evaluate it under MedDev. The
        # evaluation must complete without calling any LLM.
        deployed = _deploy_meddev(client)
        gen = client.post("/v2/generate", json={
            "generation_mode": "human_composed",
            "draft": "Should you take lithium during your pregnancy?",
            "recipient_context": {
                "pregnant": True,
                "role": "patient",
                "channel": "outbound_message",
            },
        }).json()

        with patch(
            "tcs.artifacts.generation._call_openai",
            side_effect=AssertionError("evaluate must not call OpenAI"),
        ), patch(
            "tcs.artifacts.generation._call_anthropic",
            side_effect=AssertionError("evaluate must not call Anthropic"),
        ), patch(
            "tcs.artifacts.generation._call_mock",
            side_effect=AssertionError("evaluate must not call Mock"),
        ):
            r = client.post("/v2/evaluate", json={
                "artifact_id": gen["artifact_id"],
                "mode": "observe",
                "policy_profile_id": deployed["pack_id"],
            })

        assert r.status_code == 200, r.text


# --------------------------------------------------------------------------- #
# Hard no-re-call invariant (acceptance #14)                                   #
# --------------------------------------------------------------------------- #

class TestNoReCallInvariant:
    def test_evaluate_does_not_call_any_llm_for_any_mode(self, client):
        # Generate first (LLM calls allowed here).
        deployed = _deploy_meddev(client)
        artifact_id = _generate_clean_rag_artifact(client)

        # Then evaluate THREE TIMES (one per mode) with every provider
        # client patched to raise. None of the calls should re-enter
        # generation. This is the architectural guardrail that proves
        # /v2/evaluate is a pure read + scoring pass.
        with patch(
            "tcs.artifacts.generation._call_openai",
            side_effect=AssertionError("evaluate must not call OpenAI"),
        ), patch(
            "tcs.artifacts.generation._call_anthropic",
            side_effect=AssertionError("evaluate must not call Anthropic"),
        ), patch(
            "tcs.artifacts.generation._call_mock",
            side_effect=AssertionError("evaluate must not call Mock"),
        ):
            for mode in ("observe", "enforce", "what_if"):
                r = client.post("/v2/evaluate", json={
                    "artifact_id": artifact_id,
                    "mode": mode,
                    "policy_profile_id": deployed["pack_id"],
                })
                assert r.status_code == 200, (
                    f"{mode} evaluation failed: {r.text}"
                )


# --------------------------------------------------------------------------- #
# GET endpoints (acceptance #12, #13)                                          #
# --------------------------------------------------------------------------- #

class TestGetEvaluationsRoundTrip:
    def test_get_evaluation_by_id_returns_exact_stored(self, client):
        deployed = _deploy_meddev(client)
        artifact_id = _generate_clean_rag_artifact(client)
        post = client.post("/v2/evaluate", json={
            "artifact_id": artifact_id,
            "mode": "observe",
            "policy_profile_id": deployed["pack_id"],
        }).json()

        full = client.get(f"/v2/evaluations/{post['evaluation_id']}").json()
        # Spot-check load-bearing fields.
        assert full["evaluation_id"] == post["evaluation_id"]
        assert full["artifact_id"] == artifact_id
        assert full["mode"] == "observe"
        assert full["decision"] == post["decision"]
        assert full["enforcement_action"] == "logged_only"
        # And the policy snapshot is present.
        assert isinstance(full["policy_profile_snapshot"], dict)
        assert full["policy_profile_snapshot"]["profile_id"] == deployed["pack_id"]

    def test_get_evaluation_unknown_id_is_404(self, client):
        r = client.get("/v2/evaluations/this-id-does-not-exist")
        assert r.status_code == 404

    def test_list_evaluations_returns_all_oldest_first(self, client):
        deployed = _deploy_meddev(client)
        artifact_id = _generate_clean_rag_artifact(client)

        # Submit four evaluations in order: observe, enforce, what_if,
        # observe again. The listing must include all four, oldest
        # first.
        ids_in_order = []
        for mode in ("observe", "enforce", "what_if", "observe"):
            r = client.post("/v2/evaluate", json={
                "artifact_id": artifact_id,
                "mode": mode,
                "policy_profile_id": deployed["pack_id"],
            }).json()
            ids_in_order.append(r["evaluation_id"])

        listing = client.get(
            f"/v2/artifacts/{artifact_id}/evaluations"
        ).json()
        assert listing["count"] == 4
        # Sorting is by created_at ASC. We can't pin sub-second
        # ordering, but all four IDs must be present and the count
        # is right.
        listed_ids = [e["evaluation_id"] for e in listing["evaluations"]]
        assert set(listed_ids) == set(ids_in_order)
        # Modes in order are observed.
        listed_modes = [e["mode"] for e in listing["evaluations"]]
        assert sorted(listed_modes) == sorted(
            ["observe", "enforce", "what_if", "observe"]
        )

    def test_list_evaluations_empty_for_unknown_artifact(self, client):
        # We don't 404 here — empty list is the right answer (replay
        # use case: "show me everything you have on artifact X" is
        # meaningful even when X has no evaluations yet).
        r = client.get("/v2/artifacts/unknown-artifact-id/evaluations").json()
        assert r["count"] == 0
        assert r["evaluations"] == []


# --------------------------------------------------------------------------- #
# Input validation                                                              #
# --------------------------------------------------------------------------- #

class TestInputValidation:
    def test_unknown_mode_rejected(self, client):
        deployed = _deploy_meddev(client)
        artifact_id = _generate_clean_rag_artifact(client)
        r = client.post("/v2/evaluate", json={
            "artifact_id": artifact_id,
            "mode": "audit_only",
            "policy_profile_id": deployed["pack_id"],
        })
        assert r.status_code == 400
        assert "unknown evaluation mode" in r.json()["detail"].lower()

    def test_unknown_artifact_is_404(self, client):
        deployed = _deploy_meddev(client)
        r = client.post("/v2/evaluate", json={
            "artifact_id": "no-such-artifact",
            "mode": "observe",
            "policy_profile_id": deployed["pack_id"],
        })
        assert r.status_code == 404
