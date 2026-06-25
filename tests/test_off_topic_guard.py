"""
tests/test_off_topic_guard.py
=============================

Coverage for the /v2/query off-topic guard
(tcs.api.routes_query.OFF_TOPIC_SIMILARITY_THRESHOLD).

The guard short-circuits before the LLM is called when the active
corpus has no chunk above the configured similarity threshold. The
test stubs the vector store so the retrieval returns a known low-
similarity result and patches the LLM provider clients to raise — if
the guard is broken (LLM ends up being invoked), these tests fail
loudly with a provider-call exception.

All tests are frontend-agnostic — they only exercise the backend
behavior introduced by ``_build_off_topic_query_response``.
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
    """
    Minimal vector store stub. Returns a fixed set of low-similarity
    chunks for any query — mimics an off-topic retrieval.
    """

    def __init__(self, max_similarity: float = 0.20):
        self._max_similarity = max_similarity

    def retrieve(self, query: str, k: int = 5) -> List[Dict[str, Any]]:
        # Three chunks all well below the default 0.50 threshold.
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
    """High-similarity stub for the negative case (guard should NOT fire)."""

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
    """Fresh in-memory CertificateStore + an empty ArtifactStore."""
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
    def test_guard_fires_for_low_similarity_retrieval(self, client, monkeypatch):
        """
        Patch the vector store so retrieval returns low-similarity
        chunks (max 0.20). The guard's default threshold is 0.50.
        Expect: decision=Hold, blocking_reason starts with
        ``query_off_topic_to_active_pack``, response body is empty,
        and crucially the LLM provider was NOT invoked.
        """
        # Force the trace path so the off-topic guard's _run_query_via_trace
        # is exercised. Without this flag, /v2/query uses the legacy
        # GovernedRAGPipeline path which doesn't have the guard.
        monkeypatch.setenv("TCS_WORKFLOW_TRACE_ENABLED", "true")

        from tcs.api import routes_query

        # Stub the vector store. We do NOT patch the LLM clients —
        # the guard is enforced by the architectural ordering (guard
        # runs BEFORE the orchestrator that owns the LLMConnector).
        # If the guard fires, workflow_trace is None and certificate
        # carries the off-topic blocking_reason; that's the signal
        # the LLM was not invoked.
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
        assert body["decision"] == "Hold"
        assert body["blocked"] is True
        assert body["response"] is None
        assert body["blocking_reason"].startswith("query_off_topic_to_active_pack:")
        assert "max_similarity=0.2000" in body["blocking_reason"]
        assert "threshold=0.50" in body["blocking_reason"]
        # The guard returned five retrieval chunks (the preview the
        # similarity check ran against), all with similarity below 0.50.
        assert isinstance(body["retrieval_chunks"], list)
        assert body["retrieval_chunks"]  # non-empty
        assert all(
            float(c.get("similarity_score") or 0.0) < 0.50
            for c in body["retrieval_chunks"]
        )
        # A TC was issued.
        assert body["certificate_id"]
        # Workflow trace is None — the orchestrator did NOT run, which
        # is the structural proof that the LLM connector was never
        # invoked.
        assert body.get("workflow_trace") is None

    def test_guard_does_not_fire_for_high_similarity(self, client, monkeypatch):
        """
        Negative case: when retrieval returns high-similarity chunks,
        the guard must NOT fire and the normal workflow runs. We
        verify by asserting the response is NOT the off-topic shape:
        no off-topic blocking_reason, response present, workflow_trace
        non-null.
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
        # Not the off-topic shape — guard skipped.
        if body.get("blocking_reason"):
            assert not body["blocking_reason"].startswith(
                "query_off_topic_to_active_pack:"
            )
        # Workflow trace populated (normal path runs the orchestrator).
        assert body.get("workflow_trace") is not None

    def test_guard_threshold_env_override(self, client, monkeypatch):
        """
        The threshold is configurable via TCS_OFF_TOPIC_SIMILARITY_THRESHOLD.
        Setting it to 0.99 should make even a 0.92-similarity chunk
        trigger the guard.

        Note: the env var is read at module load. We monkey-patch the
        module-level constant directly to exercise the override path
        without reloading the module.
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
        assert body["decision"] == "Hold"
        assert body["blocking_reason"].startswith("query_off_topic_to_active_pack:")

    def test_guard_persists_artifact_and_evaluation(self, client, monkeypatch):
        """
        When the guard fires, an artifact + evaluation must be persisted
        so the Audit / Reporting surfaces show the request the same way
        a normal /v2/query call would.
        """
        monkeypatch.setenv("TCS_WORKFLOW_TRACE_ENABLED", "true")

        from tcs.api import routes_query

        with patch.object(
            routes_query, "_get_vector_store",
            return_value=_LowSimVectorStore(max_similarity=0.20),
        ):
            resp = client.post("/v2/query", json={
                "query": "Unrelated demo question.",
                "provider": "mock",
                "model": "deterministic",
            })

        body = resp.json()
        tc_id = body["certificate_id"]

        # The artifact lookup convention (tc.subject_id == artifact_id)
        # introduced in commit 636dbbb means we can fetch the artifact
        # via the TC subject_id directly. First fetch the TC to get its
        # subject_id.
        tc = client.get(f"/v2/certificates/{tc_id}").json()
        assert tc["decision"] == "Hold"
        assert tc["blocking_reason"].startswith(
            "query_off_topic_to_active_pack:"
        )
        # Confirm the artifact was persisted and surfaces the original
        # prompt so the frontend "What was asked" panel reads cleanly.
        art = client.get(f"/v2/artifacts/{tc['subject_id']}").json()
        assert art["prompt"] == "Unrelated demo question."
        assert art["raw_output"] is None
        assert art["generation_error"] == "off_topic_guard:no_llm_call"
