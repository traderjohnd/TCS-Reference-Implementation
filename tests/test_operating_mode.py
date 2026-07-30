"""
Demo/Live operating modes — Commit 1 (mode controller + backend
enforcement).

The backend is the authority: DEMO MODE blocks every external provider
call at the call sites themselves, the switch into LIVE MODE demands
explicit confirmation, and governed executions record a truthful
execution mode ("scripted_demo" | "live_provider") in the workflow
trace and on the Trust Certificate's scope attestation.
"""

from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

from tcs.api.app import create_app
from tcs.operating_mode import (
    DEFAULT_MODE,
    DEMO_MODE,
    LIVE_MODE,
    EXECUTION_MODE_LIVE,
    EXECUTION_MODE_SCRIPTED,
    ExternalCallBlockedError,
    enforce_external_call,
    execution_mode_for,
)


@pytest.fixture()
def client():
    # The workflow-trace path is the production configuration (see the
    # quick-start); execution-mode recording rides the trace metadata.
    os.environ["TCS_WORKFLOW_TRACE_ENABLED"] = "true"
    app = create_app()
    with TestClient(app) as c:
        yield c
    os.environ.pop("TCS_WORKFLOW_TRACE_ENABLED", None)


def _go_live(client):
    r = client.post("/v2/mode", json={"mode": "live", "confirm": True})
    assert r.status_code == 200 and r.json()["mode"] == "live"


# --------------------------------------------------------------------------- #
# Mode state machine                                                           #
# --------------------------------------------------------------------------- #

class TestModeStateMachine:
    def test_documented_default_is_demo(self, client):
        assert DEFAULT_MODE == DEMO_MODE
        body = client.get("/v2/mode").json()
        assert body["mode"] == "demo"
        assert body["default_mode"] == "demo"
        assert body["external_calls_allowed"] is False
        assert body["labels"]["demo"] == "DEMO MODE"
        assert body["labels"]["live"] == "LIVE MODE"

    def test_live_switch_requires_confirmation(self, client):
        r = client.post("/v2/mode", json={"mode": "live"})
        assert r.status_code == 422
        assert r.json()["detail"]["error"] == "confirmation_required"
        # Mode unchanged.
        assert client.get("/v2/mode").json()["mode"] == "demo"

    def test_confirmed_live_switch_and_unconfirmed_return(self, client):
        _go_live(client)
        assert client.get("/v2/mode").json()["external_calls_allowed"] is True
        # Returning to demo never needs confirmation — safe direction.
        r = client.post("/v2/mode", json={"mode": "demo"})
        assert r.status_code == 200 and r.json()["mode"] == "demo"

    def test_unknown_mode_rejected(self, client):
        r = client.post("/v2/mode", json={"mode": "chaos", "confirm": True})
        assert r.status_code == 422
        assert r.json()["detail"]["error"] == "unknown_mode"

    def test_fresh_app_returns_to_default(self):
        # A restart returns to the documented default, never resumes live.
        app1 = create_app()
        with TestClient(app1) as c1:
            c1.post("/v2/mode", json={"mode": "live", "confirm": True})
        app2 = create_app()
        with TestClient(app2) as c2:
            assert c2.get("/v2/mode").json()["mode"] == "demo"


# --------------------------------------------------------------------------- #
# Enforcement chokepoint                                                       #
# --------------------------------------------------------------------------- #

class TestEnforcement:
    def test_helper_semantics(self):
        class S:  # bare state object
            pass
        s = S()
        # Demo (default): mock and absent providers pass, external raise.
        enforce_external_call(s, None)
        enforce_external_call(s, "mock")
        with pytest.raises(ExternalCallBlockedError):
            enforce_external_call(s, "openai")
        with pytest.raises(ExternalCallBlockedError):
            enforce_external_call(s, "anthropic")
        s.operating_mode = LIVE_MODE
        enforce_external_call(s, "openai")   # allowed in live

    def test_execution_mode_labels(self):
        assert execution_mode_for("mock") == EXECUTION_MODE_SCRIPTED
        assert execution_mode_for(None) == EXECUTION_MODE_SCRIPTED
        assert execution_mode_for("openai") == EXECUTION_MODE_LIVE
        assert execution_mode_for("anthropic") == EXECUTION_MODE_LIVE

    def test_demo_blocks_external_query(self, client):
        r = client.post("/v2/query", json={
            "query": "What is the retention policy?",
            "provider": "openai", "api_key": "sk-should-never-be-used",
            "model": "gpt-4o-mini",
        })
        assert r.status_code == 200
        body = r.json()
        assert body["decision"] == "Error"
        assert body["blocked"] is True
        assert "demo_mode_enforced" in body["blocking_reason"]
        # No certificate for a blocked external call.
        assert body["certificate_id"] is None

    def test_demo_allows_mock_query(self, client):
        r = client.post("/v2/query", json={
            "query": "What is the retention policy?",
            "provider": "mock", "model": "deterministic",
        })
        assert r.status_code == 200
        assert r.json()["certificate_id"]

    def test_demo_blocks_external_generation(self, client):
        r = client.post("/v2/generate", json={
            "generation_mode": "raw_llm",
            "prompt": "hello",
            "provider": "openai", "api_key": "sk-should-never-be-used",
            "model": "gpt-4o-mini",
        })
        assert r.status_code == 403
        assert r.json()["detail"]["error"] == "demo_mode_enforced"

    def test_demo_allows_mock_generation(self, client):
        r = client.post("/v2/generate", json={
            "generation_mode": "raw_llm",
            "prompt": "hello",
            "provider": "mock", "model": "deterministic",
        })
        assert r.status_code == 200

    def test_demo_blocks_external_connection_test(self, client):
        r = client.post("/v2/connections/test", json={
            "category": "llm", "provider": "openai",
            "api_key": "sk-should-never-be-used", "model": "gpt-4o-mini",
        })
        assert r.status_code == 200
        body = r.json()
        assert body["success"] is False
        assert "demo_mode_enforced" in body["error"]

    def test_live_mode_reaches_the_provider_layer(self, client):
        # In LIVE mode the call passes enforcement and reaches the real
        # provider client, which rejects the invalid key — a PROVIDER
        # error, visibly distinct from a governance outcome and from
        # the demo-mode block.
        _go_live(client)
        r = client.post("/v2/query", json={
            "query": "What is the retention policy?",
            "provider": "openai", "api_key": "sk-invalid-test-key",
            "model": "gpt-4o-mini",
        })
        assert r.status_code == 200
        body = r.json()
        assert body["decision"] == "Error"
        assert "demo_mode_enforced" not in (body["blocking_reason"] or "")
        assert body["certificate_id"] is None


# --------------------------------------------------------------------------- #
# Truthful execution-mode recording                                            #
# --------------------------------------------------------------------------- #

class TestExecutionModeRecording:
    def test_scripted_demo_recorded_on_trace_and_certificate(self, client):
        r = client.post("/v2/query", json={
            "query": "What is the document retention policy?",
            "provider": "mock", "model": "deterministic",
        })
        assert r.status_code == 200
        body = r.json()
        # Workflow trace carries the execution mode + provider identity.
        meta = (body.get("workflow_trace") or {}).get("metadata") or {}
        assert meta.get("execution_mode") == EXECUTION_MODE_SCRIPTED
        assert meta.get("llm_provider") == "mock"
        # The Trust Certificate's scope attestation records it too.
        tc = client.get(f"/v2/certificates/{body['certificate_id']}").json()
        assert tc["scope_attestation"]["execution_mode"] == \
            EXECUTION_MODE_SCRIPTED
        # Never labeled as live provider output.
        assert tc["scope_attestation"]["execution_mode"] != \
            EXECUTION_MODE_LIVE

    def test_certificate_without_execution_mode_stays_valid(self, client):
        # Records that predate the mode system carry no execution_mode
        # key — absence is legitimate, not defaulted or backfilled.
        r = client.post("/v2/govern", json={
            "query": "q", "candidate_answer": "a",
            "retrieved_chunks": [{"chunk_id": "c1",
                                  "similarity_score": "0.93",
                                  "source_doc": "d.pdf", "version": "1",
                                  "content": "x"}],
        })
        assert r.status_code == 200
        tc = client.get(f"/v2/certificates/{r.json()['certificate_id']}").json()
        assert "execution_mode" not in tc["scope_attestation"]
        assert tc["calculation_version"] == "tis-v2"
