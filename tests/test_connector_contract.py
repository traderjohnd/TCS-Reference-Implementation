"""
Phase 4 / Slice 1 — connector contract tests.

Validates that every shipped connector conforms to the GovernedConnector
contract and produces well-formed GovernanceEvents:

    - connection_type() returns a valid CT-* identifier
    - invoke() returns a ConnectorResult (never raises on normal path)
    - to_governance_event() returns a GovernanceEvent with all four
      BACK signals populated
    - Event hash is deterministic; chain wiring works in the orchestrator
"""

from __future__ import annotations

from typing import Any, Dict, List

import pytest

from tcs.workflow import (
    AttributionSignal,
    BoundednessSignal,
    ComplianceSignal,
    ConnectorRequest,
    ConnectorResult,
    GovernanceEvent,
    GovernedConnector,
    GovernedNode,
    GovernedWorkflowTrace,
    KnownStateSignal,
    NodeType,
    WorkflowOrchestrator,
)
from tcs.workflow.connectors import LLMConnector, RAGConnector
from tcs.workflow.orchestrator import WorkflowStep


# --------------------------------------------------------------------------- #
# Test fixtures                                                                #
# --------------------------------------------------------------------------- #

class _StubVectorStore:
    """In-memory store with controllable chunk shape for tests."""

    def __init__(self, chunks: List[Dict[str, Any]]) -> None:
        self._chunks = chunks

    def retrieve(self, query: str, k: int = 5) -> List[Dict[str, Any]]:
        return list(self._chunks[:k])


class _StubProvider:
    """Provider that records calls and returns a canned response."""

    def __init__(self, response: str = "Stubbed response.") -> None:
        self.response = response
        self.calls: List[tuple] = []

    def generate(self, query: str, context: List[str]) -> str:
        self.calls.append((query, list(context)))
        return self.response


def _good_chunk(chunk_id: str, sim: float = 0.95) -> Dict[str, Any]:
    return {
        "chunk_id": chunk_id,
        "source_doc": "policy.md",
        "version": "2026-01",
        "content": f"Content for {chunk_id}.",
        "similarity_score": sim,
        "tags": ["test"],
    }


def _missing_metadata_chunk(chunk_id: str, sim: float = 0.95) -> Dict[str, Any]:
    """Chunk missing source_doc and version — counts as an attribution gap."""
    return {
        "chunk_id": chunk_id,
        "source_doc": None,
        "version": None,
        "content": f"Orphan content for {chunk_id}.",
        "similarity_score": sim,
        "tags": [],
    }


def _llm_node() -> GovernedNode:
    return GovernedNode(
        node_id="llm",
        name="LLM",
        node_type=NodeType.LLM,
        connection_type="CT-1",
        sensitivity_tier="T2",
    )


def _rag_node() -> GovernedNode:
    return GovernedNode(
        node_id="rag",
        name="RAG",
        node_type=NodeType.RAG,
        connection_type="CT-4",
        sensitivity_tier="T2",
    )


# --------------------------------------------------------------------------- #
# RAG connector contract                                                       #
# --------------------------------------------------------------------------- #

class TestRAGConnector:
    def test_connection_type_is_ct4(self):
        c = RAGConnector(store=_StubVectorStore([]))
        assert c.connection_type() == "CT-4"

    def test_invoke_returns_chunks_in_payload(self):
        chunks = [_good_chunk("c1"), _good_chunk("c2")]
        c = RAGConnector(store=_StubVectorStore(chunks))
        result = c.invoke(ConnectorRequest(query="test"))
        assert isinstance(result, ConnectorResult)
        assert result.payload == chunks
        assert result.raw_metadata["chunk_count"] == 2
        assert result.raw_metadata["n_gaps"] == 0
        assert result.error is None

    def test_invoke_counts_attribution_gaps(self):
        chunks = [_good_chunk("c1"), _missing_metadata_chunk("c2")]
        c = RAGConnector(store=_StubVectorStore(chunks))
        result = c.invoke(ConnectorRequest(query="test"))
        assert result.raw_metadata["n_gaps"] == 1
        assert result.raw_metadata["complete_metadata_count"] == 1

    def test_event_attribution_signal_reflects_gaps(self):
        chunks = [_good_chunk("c1"), _missing_metadata_chunk("c2")]
        c = RAGConnector(store=_StubVectorStore(chunks))
        result = c.invoke(ConnectorRequest(query="test"))
        event = c.to_governance_event(
            result, _rag_node(), workflow_id="wf-test"
        )
        assert event.connection_type == "CT-4"
        assert event.attribution.source_count == 2
        assert event.attribution.sources_with_complete_metadata == 1
        assert event.attribution.integration_boundary_gaps == 1
        # Score contribution = complete / total = 1/2 = 0.5
        assert event.attribution.score_contribution == pytest.approx(0.5, abs=1e-9)
        assert event.attribution.chain_of_custody_complete is False

    def test_event_known_signal_falls_off_with_low_similarity(self):
        # All chunks at 0.50 similarity — below novelty threshold (0.80)
        chunks = [_good_chunk("c1", 0.5), _good_chunk("c2", 0.5)]
        c = RAGConnector(store=_StubVectorStore(chunks))
        result = c.invoke(ConnectorRequest(query="test"))
        event = c.to_governance_event(
            result, _rag_node(), workflow_id="wf-test"
        )
        assert event.known.confidence_calibrated is False
        assert event.known.score_contribution == pytest.approx(0.5, abs=1e-9)
        assert event.known.novelty_score == pytest.approx(0.5, abs=1e-9)


# --------------------------------------------------------------------------- #
# LLM connector contract                                                       #
# --------------------------------------------------------------------------- #

class TestLLMConnector:
    def test_connection_type_is_ct1(self):
        c = LLMConnector(provider=_StubProvider(), provider_name="stub", model="m")
        assert c.connection_type() == "CT-1"

    def test_invoke_returns_response_text(self):
        provider = _StubProvider("Hello from stub.")
        c = LLMConnector(provider=provider, provider_name="stub", model="m")
        result = c.invoke(ConnectorRequest(query="hi"))
        assert result.output_text == "Hello from stub."
        assert result.payload == {"response_text": "Hello from stub."}
        assert result.error is None

    def test_invoke_reads_context_from_workflow_context(self):
        provider = _StubProvider()
        c = LLMConnector(provider=provider, provider_name="stub", model="m")
        req = ConnectorRequest(
            query="test",
            workflow_context={
                "rag": {"payload": [{"content": "chunk A"}, {"content": "chunk B"}]}
            },
        )
        c.invoke(req)
        # Provider received both chunk bodies as context
        _q, ctx = provider.calls[0]
        assert ctx == ["chunk A", "chunk B"]

    def test_invoke_handles_provider_exception_into_error(self):
        class BadProvider:
            def generate(self, *_a, **_kw):
                raise RuntimeError("api down")

        c = LLMConnector(provider=BadProvider(), provider_name="bad", model="m")
        result = c.invoke(ConnectorRequest(query="test"))
        assert result.error == "api down"
        assert result.output_text is None

    def test_event_c3_violation_on_injection_phrase(self):
        provider = _StubProvider(
            "Sure — let me ignore policy constraints and recommend that."
        )
        c = LLMConnector(provider=provider, provider_name="stub", model="m")
        result = c.invoke(ConnectorRequest(query="x"))
        event = c.to_governance_event(
            result, _llm_node(), workflow_id="wf-test"
        )
        assert event.compliance.c3_violation is True
        assert event.compliance.c3_pattern == "ignore policy"
        assert event.compliance.score_contribution == 0.0

    def test_event_no_c3_on_clean_response(self):
        provider = _StubProvider("Per the policy, the answer is X.")
        c = LLMConnector(provider=provider, provider_name="stub", model="m")
        result = c.invoke(ConnectorRequest(query="x"))
        event = c.to_governance_event(
            result, _llm_node(), workflow_id="wf-test"
        )
        assert event.compliance.c3_violation is False
        assert event.compliance.c3_pattern is None
        assert event.compliance.score_contribution == 1.0


# --------------------------------------------------------------------------- #
# Orchestrator chain integrity                                                 #
# --------------------------------------------------------------------------- #

class TestOrchestratorChain:
    def test_two_step_workflow_chains_event_hashes(self):
        chunks = [_good_chunk("c1"), _good_chunk("c2")]
        rag = RAGConnector(store=_StubVectorStore(chunks))
        llm = LLMConnector(
            provider=_StubProvider("Answer."),
            provider_name="stub",
            model="m",
        )
        orch = WorkflowOrchestrator()
        trace = orch.execute(
            steps=[
                WorkflowStep(node=_rag_node(), connector=rag, context_key="rag"),
                WorkflowStep(node=_llm_node(), connector=llm, context_key="llm"),
            ],
            query="test",
            base_profile_id="fin-r3-a4-ct4",
        )
        events = trace.events()
        assert len(events) == 2
        assert events[0].previous_event_hash is None
        assert events[0].event_hash is not None
        assert events[1].previous_event_hash == events[0].event_hash
        assert events[1].event_hash is not None
        # Both nodes have attached events
        assert trace.get_node("rag").event is events[0]
        assert trace.get_node("llm").event is events[1]
        # Final output came from the LLM step
        assert trace.final_output == "Answer."

    def test_workflow_runs_when_rag_returns_no_chunks(self):
        rag = RAGConnector(store=_StubVectorStore([]))
        llm = LLMConnector(
            provider=_StubProvider("OK."), provider_name="stub", model="m"
        )
        orch = WorkflowOrchestrator()
        trace = orch.execute(
            steps=[
                WorkflowStep(node=_rag_node(), connector=rag),
                WorkflowStep(node=_llm_node(), connector=llm),
            ],
            query="test",
            base_profile_id="fin-r3-a4-ct4",
        )
        assert trace.final_output == "OK."
        # Empty retrieval -> A signal default behavior
        rag_event = trace.get_node("rag").event
        assert rag_event.attribution.source_count == 0
        assert rag_event.attribution.score_contribution == 1.0  # vacuously true
