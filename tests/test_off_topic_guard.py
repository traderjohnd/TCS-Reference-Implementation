"""
tests/test_off_topic_guard.py
=============================

Coverage for /v2/query scope-aware routing (off-topic guard).

When the top-K retrieval from the active corpus produces no chunk
above ``OFF_TOPIC_SIMILARITY_THRESHOLD``, the request is treated as
out-of-scope for the active pack and routed through
``baseline-no-pack`` instead:

  - The LLM is invoked normally (no RAG context — the active corpus
    is irrelevant by definition).
  - The response is scored against baseline-no-pack (permissive
    thresholds, K not in gate set).
  - A TC is issued under ``policy_set_id = "baseline-no-pack"`` with
    ``blocking_reason`` containing the
    ``routed_via_baseline_off_topic_for_pack:...`` marker the frontend
    paraphrases.
  - The LLM answer flows back to the caller (response field
    populated, blocked = False for the typical Allow decision).

These tests stub the vector store so the off-topic guard fires
deterministically; the mock LLM provider returns its canned answer
which is enough to drive the scoring through.
"""

from __future__ import annotations

from typing import Any, Dict, List
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from tcs.api.app import create_app
from tcs.persistence import CertificateStore


# --------------------------------------------------------------------------- #
# Fixtures                                                                     #
# --------------------------------------------------------------------------- #

class _LowSimVectorStore:
    """Stub: returns three chunks all well below the default 0.50 threshold."""

    def __init__(self, max_similarity: float = 0.20):
        self._max_similarity = max_similarity

    def retrieve(self, query: str, k: int = 5) -> List[Dict[str, Any]]:
        return [
            {
                "chunk_id": f"low-{i}",
                "source_doc": "unrelated_doc.md",
                "version": "v1",
                "similarity_score": self._max_similarity - (i * 0.05),
                "content": "Unrelated content.",
                "tags": [],
            }
            for i in range(3)
        ]


class _OnTopicVectorStore:
    """Stub: returns one chunk above the default threshold (guard does NOT fire)."""

    def retrieve(self, query: str, k: int = 5) -> List[Dict[str, Any]]:
        return [
            {
                "chunk_id": "ok-0",
                "source_doc": "relevant_doc.md",
                "version": "v1",
                "similarity_score": 0.92,
                "content": "Relevant content.",
                "tags": [],
            }
        ]


@pytest.fixture
def app_with_store(tmp_path):
    db_path = tmp_path / "off_topic_guard.db"
    store = CertificateStore(str(db_path))
    app = create_app(store=store)
    yield app
    store.close()


@pytest.fixture
def client(app_with_store):
    with TestClient(app_with_store) as c:
        yield c


# --------------------------------------------------------------------------- #
# Tests                                                                        #
# --------------------------------------------------------------------------- #

class TestOffTopicGuard:
    def test_guard_routes_low_similarity_via_baseline(self, client, monkeypatch):
        """
        Stub the vector store to return low-similarity chunks
        (max 0.20). Guard fires; request is routed through
        baseline-no-pack; LLM is still called (mock provider returns
        canned output); the response flows back to the caller as
        Allow because baseline-no-pack's thresholds (K = 0.60 and
        K not in gate set) are permissive enough.
        """
        monkeypatch.setenv("TCS_WORKFLOW_TRACE_ENABLED", "true")

        from tcs.api import routes_query

        with patch.object(
            routes_query, "_get_vector_store",
            return_value=_LowSimVectorStore(max_similarity=0.20),
        ):
            resp = client.post("/v2/query", json={
                "query": "What is Paris at night like?",
                "provider": "mock",
                "model": "deterministic",
            })

        assert resp.status_code == 200, resp.text
        body = resp.json()
        # Routed via baseline — the policy_set_id on the issued TC
        # must be the baseline profile, NOT the active pack.
        assert body["policy_profile_id"] == "baseline-no-pack"
        # Decision is Allow under baseline-no-pack's permissive thresholds.
        assert body["decision"] == "Allow"
        assert body["blocked"] is False
        # Response is populated — the LLM was called and the answer
        # flowed back through.
        assert body["response"] is not None
        # blocking_reason carries the routing marker so the frontend
        # can render "routed via baseline" prose.
        assert body["blocking_reason"].startswith(
            "routed_via_baseline_off_topic_for_pack:"
        )
        assert "max_similarity=0.2000" in body["blocking_reason"]
        # No RAG chunks are surfaced to the UI (baseline routing
        # skips RAG entirely; the active corpus was irrelevant).
        assert body["retrieval_chunks"] == []
        # workflow_trace is None — the orchestrator was bypassed.
        assert body["workflow_trace"] is None

    def test_guard_does_not_fire_for_high_similarity(self, client, monkeypatch):
        """
        Negative case: high-similarity retrieval keeps the request on
        the normal workflow path. policy_set_id reflects the active
        pack (or fallback), not baseline-no-pack.
        """
        monkeypatch.setenv("TCS_WORKFLOW_TRACE_ENABLED", "true")

        from tcs.api import routes_query

        with patch.object(
            routes_query, "_get_vector_store",
            return_value=_OnTopicVectorStore(),
        ):
            resp = client.post("/v2/query", json={
                "query": "On-topic question with strong relevance.",
                "provider": "mock",
                "model": "deterministic",
            })

        assert resp.status_code == 200, resp.text
        body = resp.json()
        # Did not get the off-topic routing marker.
        if body.get("blocking_reason"):
            assert not body["blocking_reason"].startswith(
                "routed_via_baseline_off_topic_for_pack:"
            )
        # Normal workflow ran (orchestrator populated the trace).
        assert body.get("workflow_trace") is not None

    def test_guard_threshold_env_override(self, client, monkeypatch):
        """
        Setting TCS_OFF_TOPIC_SIMILARITY_THRESHOLD higher than the
        stub's 0.92 should cause even an on-topic-looking retrieval
        to be treated as off-topic. We monkey-patch the constant
        directly rather than reload the module.
        """
        monkeypatch.setenv("TCS_WORKFLOW_TRACE_ENABLED", "true")

        from tcs.api import routes_query

        with patch.object(
            routes_query, "OFF_TOPIC_SIMILARITY_THRESHOLD", 0.99,
        ), patch.object(
            routes_query, "_get_vector_store",
            return_value=_OnTopicVectorStore(),  # max sim 0.92
        ):
            resp = client.post("/v2/query", json={
                "query": "What is Paris at night like?",
                "provider": "mock",
                "model": "deterministic",
            })

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["policy_profile_id"] == "baseline-no-pack"
        assert body["blocking_reason"].startswith(
            "routed_via_baseline_off_topic_for_pack:"
        )

    def test_routed_request_persists_artifact_with_llm_answer(self, client, monkeypatch):
        """
        The routed request must persist a ResponseArtifact carrying
        the LLM's actual answer so the Audit / Reporting surfaces
        show the request + response uniformly.
        """
        monkeypatch.setenv("TCS_WORKFLOW_TRACE_ENABLED", "true")

        from tcs.api import routes_query

        with patch.object(
            routes_query, "_get_vector_store",
            return_value=_LowSimVectorStore(max_similarity=0.20),
        ):
            resp = client.post("/v2/query", json={
                "query": "Off-topic demo question.",
                "provider": "mock",
                "model": "deterministic",
            })

        body = resp.json()
        tc_id = body["certificate_id"]

        tc = client.get(f"/v2/certificates/{tc_id}").json()
        assert tc["policy_set_id"] == "baseline-no-pack"

        # Artifact lookup convention (tc.subject_id == artifact_id).
        art = client.get(f"/v2/artifacts/{tc['subject_id']}").json()
        assert art["prompt"] == "Off-topic demo question."
        # raw_output carries the mock LLM's answer (not None).
        assert art["raw_output"] is not None
        assert art["generation_mode"] == "agent_workflow"
        assert art["rag_enabled"] is False
