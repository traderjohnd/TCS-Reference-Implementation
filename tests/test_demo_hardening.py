"""
Commit 6/6 — investor-demo hardening: backend enforcement matrix
across every external-call surface, preflight status, deterministic
scripted scenarios, and truthful scripted labeling.
"""

from __future__ import annotations

import os
import sys

import pytest
from fastapi.testclient import TestClient

from tcs.api.app import create_app


@pytest.fixture()
def client(monkeypatch):
    # Poison both SDKs: in Demo Mode no surface may even construct a
    # provider client, let alone use a credential or the network.
    monkeypatch.setitem(sys.modules, "openai", None)
    monkeypatch.setitem(sys.modules, "anthropic", None)
    os.environ["TCS_WORKFLOW_TRACE_ENABLED"] = "true"
    app = create_app()
    with TestClient(app) as c:
        yield c
    os.environ.pop("TCS_WORKFLOW_TRACE_ENABLED", None)


# --------------------------------------------------------------------------- #
# Demo Mode enforcement matrix — all seven external-call surfaces              #
# --------------------------------------------------------------------------- #

class TestDemoModeEnforcementMatrix:
    """Every surface rejects BEFORE SDK construction (sys.modules is
    poisoned — reaching construction would raise, not reject), before
    credential use, network, artifacts, evaluation, or TC issuance."""

    def _assert_no_new_artifacts(self, client, run):
        store = client.app.state.artifact_store
        n_before = len(store.list_artifacts(limit=1000))
        run()
        assert len(store.list_artifacts(limit=1000)) == n_before

    @pytest.mark.parametrize("provider", ["openai", "anthropic"])
    def test_live_llm_blocked(self, client, provider):
        def run():
            r = client.post("/v2/query", json={
                "query": "q", "provider": provider,
                "api_key": "sk-x", "model": "m",
            })
            body = r.json()
            assert body["blocked"] is True
            assert "demo_mode_enforced" in body["blocking_reason"]
            assert body["certificate_id"] is None
        self._assert_no_new_artifacts(client, run)

    @pytest.mark.parametrize("provider", ["openai", "anthropic"])
    def test_connection_test_blocked(self, client, provider):
        r = client.post("/v2/connections/test", json={
            "category": "llm", "provider": provider,
            "api_key": "sk-x", "model": "m",
        })
        body = r.json()
        assert body["success"] is False
        assert "demo_mode_enforced" in body["error"]

    def test_comparison_blocked(self, client):
        r = client.post("/v2/query/compare", json={
            "query": "q",
            "targets": [
                {"provider": "mock", "model": "deterministic"},
                {"provider": "openai", "model": "m", "api_key": "sk-x"},
            ],
        })
        assert r.status_code == 403
        assert r.json()["detail"]["error"] == "demo_mode_enforced"

    @pytest.mark.parametrize("provider", ["openai", "anthropic"])
    def test_live_web_blocked(self, client, provider):
        r = client.post("/v2/query/web", json={
            "query": "q", "provider": provider, "model": "m",
            "api_key": "sk-x", "retrieval_mode": "live_web",
        })
        assert r.status_code == 403
        assert r.json()["detail"]["error"] == "demo_mode_enforced"


# --------------------------------------------------------------------------- #
# Truthful scripted labeling                                                   #
# --------------------------------------------------------------------------- #

class TestScriptedLabeling:
    def test_mock_result_never_labeled_live(self, client):
        r = client.post("/v2/query", json={
            "query": "What is the document retention policy?",
            "provider": "mock", "model": "deterministic",
        }).json()
        meta = (r.get("workflow_trace") or {}).get("metadata") or {}
        assert meta.get("execution_mode") == "scripted_demo"
        assert meta.get("execution_mode") != "live_provider"
        tc = client.get(f"/v2/certificates/{r['certificate_id']}").json()
        sa = tc["scope_attestation"]
        assert sa["execution_mode"] == "scripted_demo"
        # No web claim, no real-provider request id on a mock result.
        assert "web_retrieval" not in sa
        assert "live_web" not in str(sa)

    def test_demo_run_carries_scripted_label(self, client):
        r = client.post("/v2/demo/run",
                        json={"scenario_id": "allow-retention-policy"}).json()
        assert r["label"] == "SCRIPTED DEMO OUTPUT"
        assert r["scripted"] is True
        assert r["execution_mode"] == "scripted_demo"


# --------------------------------------------------------------------------- #
# Preflight                                                                    #
# --------------------------------------------------------------------------- #

class TestPreflight:
    def test_preflight_shape(self, client):
        p = client.get("/v2/demo/preflight").json()
        assert p["backend_reachable"] is True
        assert p["build_id"]
        assert p["operating_mode"] == "demo"
        assert p["default_mode"] == "demo"
        assert p["certificate_store_available"] is True
        assert p["chain_intact"] in (True, None)
        assert p["scripted_scenarios_available"] >= 4
        assert p["live_web_available"] is True
        # Credentials/connections are frontend memory state — the
        # backend preflight never carries key material.
        assert "api_key" not in str(p)

    def test_preflight_reflects_mode(self, client):
        client.post("/v2/mode", json={"mode": "live", "confirm": True})
        assert client.get("/v2/demo/preflight").json()[
            "operating_mode"] == "live"


# --------------------------------------------------------------------------- #
# Deterministic scripted scenarios                                             #
# --------------------------------------------------------------------------- #

class TestScriptedScenarios:
    def test_catalog_covers_required_outcomes(self, client):
        cat = client.get("/v2/demo/scenarios").json()["scenarios"]
        expected = {s["expected_decision"] for s in cat
                    if s["expected_decision"]}
        assert {"Allow", "Hold", "Stop", "Escalate"} <= expected
        titles = " ".join(s["title"] for s in cat).lower()
        for topic in ("certificate", "hash-chain", "replay",
                      "reporting", "resilience"):
            assert topic in titles
        for s in cat:
            assert s["title"] and s["operator_action"] and s["demonstrates"]

    @pytest.mark.parametrize("scenario_id,expected", [
        ("allow-retention-policy", "Allow"),
        ("stop-prompt-injection", "Stop"),
        ("hold-remediable-gate-failure", "Hold"),
        ("escalate-decayed-trust", "Escalate"),
    ])
    def test_each_scenario_lands_expected_decision(self, client,
                                                   scenario_id, expected):
        r = client.post("/v2/demo/run", json={"scenario_id": scenario_id})
        assert r.status_code == 200
        body = r.json()
        assert body["decision"] == expected
        assert body["matches_expected"] is True
        assert body["certificate_id"]
        tc = client.get(f"/v2/certificates/{body['certificate_id']}")
        assert tc.status_code == 200
        assert tc.json()["calculation_version"] == "tis-v2"

    def test_scenario_determinism(self, client):
        runs = [
            client.post("/v2/demo/run",
                        json={"scenario_id": "allow-retention-policy"}).json()
            for _ in range(2)
        ]
        # Same response text, component values, gates, decision —
        # only identifiers/timestamps vary (documented).
        assert runs[0]["response"] == runs[1]["response"]
        assert runs[0]["component_scores"] == runs[1]["component_scores"]
        assert runs[0]["gate_results"] == runs[1]["gate_results"]
        assert runs[0]["tis_current"] == runs[1]["tis_current"]
        assert runs[0]["decision"] == runs[1]["decision"]
        assert runs[0]["certificate_id"] != runs[1]["certificate_id"]

    def test_engineered_scenarios_determinism(self, client):
        for sid in ("hold-remediable-gate-failure",
                    "escalate-decayed-trust"):
            a = client.post("/v2/demo/run", json={"scenario_id": sid}).json()
            b = client.post("/v2/demo/run", json={"scenario_id": sid}).json()
            assert a["component_scores"] == b["component_scores"]
            assert a["tis_current"] == b["tis_current"]
            assert a["decision"] == b["decision"]

    def test_hold_scenario_populates_review_queue(self, client):
        r = client.post("/v2/demo/run",
                        json={"scenario_id": "hold-remediable-gate-failure"}).json()
        assert r["requires_human_review"] is True
        assert r["gate_result"] == 0  # remediable gate failure, not a stop

    def test_unknown_scenario_rejected(self, client):
        r = client.post("/v2/demo/run", json={"scenario_id": "nope"})
        assert r.status_code == 404
        assert r.json()["detail"]["error"] == "unknown_scenario"

    def test_guide_steps_not_runnable(self, client):
        r = client.post("/v2/demo/run",
                        json={"scenario_id": "guide-certificate-detail"})
        assert r.status_code == 422
        assert r.json()["detail"]["error"] == "not_runnable"

    def test_chain_intact_after_full_scripted_sequence(self, client):
        for sid in ("allow-retention-policy", "stop-prompt-injection",
                    "hold-remediable-gate-failure", "escalate-decayed-trust"):
            assert client.post("/v2/demo/run",
                               json={"scenario_id": sid}).status_code == 200
        assert client.get("/v2/health").json()["chain_intact"] is True
