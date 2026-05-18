"""
demos.governed_rag.pipeline
============================

A RAG pipeline with TCS governance in the request path.

This is not a simulation. When configured with OpenAI or Anthropic, the
LLM generates real responses. TCS evaluates each response before it
reaches the user.

With ``MockProvider`` (the default), responses are deterministic and
no API keys are required — ideal for CI and tests.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Protocol, runtime_checkable

from tcs.sdk.client import TCSClient
from tcs.sdk.models import GovernResult
from demos.governed_rag.vector_store import SimpleVectorStore


# --------------------------------------------------------------------------- #
# GovernedQueryResult                                                          #
# --------------------------------------------------------------------------- #

@dataclass
class GovernedQueryResult:
    """Full result of a governed RAG query."""
    query: str
    retrieval_chunks: List[Dict[str, Any]]
    raw_llm_response: str
    governance_decision: str
    governed_response: Optional[str]
    certificate_id: Optional[str]
    tis_current: Optional[float]
    tis_raw: Optional[float]
    gate_passed: Optional[bool]
    blocked: bool
    blocking_reason: Optional[str]
    requires_human_review: bool = False
    latency_ms: Dict[str, float] = field(default_factory=dict)


# --------------------------------------------------------------------------- #
# LLM Provider Protocol                                                        #
# --------------------------------------------------------------------------- #

@runtime_checkable
class LLMProvider(Protocol):
    """Interface for LLM generation backends."""

    def generate(self, query: str, context: List[str]) -> str:
        """Generate a response given a query and context chunks."""
        ...


# --------------------------------------------------------------------------- #
# Mock Provider                                                                #
# --------------------------------------------------------------------------- #

class MockProvider:
    """
    Deterministic, retrieval-aware mock LLM provider.

    Returns ONE template that quotes the actual retrieved chunks. Does
    NOT produce canned domain answers — every response is explicitly
    labeled ``[MOCK PROVIDER]`` so the user knows no real LLM was
    called.

    Governance outcomes (Allow / Hold / Stop / Escalate) come from the
    governance risk classifier + policy / BACK signals — NOT from
    canned response strings. See tcs/governance/risk_classifier.py.

    The principle: sample prompts are allowed; sample answers are not.
    """

    _LABEL = "[MOCK PROVIDER]"
    _FOOTER = (
        " — Switch to OpenAI or Anthropic on the Connections tab for a "
        "real model answer. The mock is for deterministic governance "
        "testing only."
    )

    # Kept as an empty dict for backward-compat with any test that
    # imports the symbol. Intentionally empty — the mock no longer
    # produces canned domain answers.
    _RESPONSES: Dict[str, str] = {}

    @staticmethod
    def _first_sentence(text: str, max_chars: int = 220) -> str:
        text = (text or "").strip().replace("\n", " ")
        if not text:
            return ""
        # Cut at the first sentence-ending punctuation, or fall back to
        # a character budget so the mock response stays short.
        for end in (". ", "? ", "! "):
            idx = text.find(end)
            if 20 < idx < max_chars:
                return text[: idx + 1].strip()
        return text[:max_chars].rstrip() + ("…" if len(text) > max_chars else "")

    def generate(self, query: str, context: List[str]) -> str:
        """
        Return a mock response that explicitly references the actual
        retrieved chunks. No domain expertise is implied or fabricated.
        """
        # Normalize the context: each item is either a string (chunk
        # body) or a dict containing the chunk's content. We accept
        # both for forward-compat with caller variants.
        bodies: List[str] = []
        for c in (context or []):
            if isinstance(c, str):
                bodies.append(c)
            elif isinstance(c, dict):
                v = c.get("content")
                if isinstance(v, str):
                    bodies.append(v)

        n = len(bodies)
        if n == 0:
            return (
                f"{self._LABEL} No real LLM was called. No documents were "
                f"retrieved by the RAG step for this query, so there is no "
                f"context to summarize.{self._FOOTER}"
            )

        # Quote the most relevant chunk's first sentence so the mock
        # demonstrates that retrieval and corpus selection actually
        # work — without inventing a domain answer.
        excerpt = self._first_sentence(bodies[0])
        if not excerpt:
            return (
                f"{self._LABEL} No real LLM was called. {n} document(s) "
                f"were retrieved but yielded no quotable content.{self._FOOTER}"
            )
        return (
            f"{self._LABEL} No real LLM was called. The workflow retrieved "
            f"{n} chunk(s) from the active corpus. Most relevant excerpt: "
            f"“{excerpt}”{self._FOOTER}"
        )


# --------------------------------------------------------------------------- #
# Optional real providers (lazy imports)                                        #
# --------------------------------------------------------------------------- #

class OpenAIProvider:
    """Uses openai.ChatCompletion. Requires OPENAI_API_KEY env var."""

    def __init__(self, model: str = "gpt-4o-mini") -> None:
        import openai  # lazy
        self._client = openai.OpenAI()
        self._model = model

    def generate(self, query: str, context: List[str]) -> str:
        import openai
        context_text = "\n\n".join(context)
        response = self._client.chat.completions.create(
            model=self._model,
            messages=[
                {"role": "system", "content": (
                    "You are a financial advisory AI. Answer based strictly "
                    "on the provided context. Cite sources when possible."
                )},
                {"role": "user", "content": (
                    f"Context:\n{context_text}\n\nQuestion: {query}"
                )},
            ],
            max_tokens=500,
            temperature=0.3,
        )
        return response.choices[0].message.content or ""


class AnthropicProvider:
    """Uses anthropic.messages. Requires ANTHROPIC_API_KEY env var."""

    def __init__(self, model: str = "claude-sonnet-4-20250514") -> None:
        import anthropic  # lazy
        self._client = anthropic.Anthropic()
        self._model = model

    def generate(self, query: str, context: List[str]) -> str:
        import anthropic
        context_text = "\n\n".join(context)
        response = self._client.messages.create(
            model=self._model,
            max_tokens=500,
            messages=[
                {"role": "user", "content": (
                    f"You are a financial advisory AI. Answer based strictly "
                    f"on the provided context.\n\nContext:\n{context_text}\n\n"
                    f"Question: {query}"
                )},
            ],
        )
        return response.content[0].text


# --------------------------------------------------------------------------- #
# GovernedRAGPipeline                                                          #
# --------------------------------------------------------------------------- #

class GovernedRAGPipeline:
    """
    A RAG pipeline with TCS governance in the request path.

    Parameters
    ----------
    tcs_client
        Pre-configured TCSClient (real or test-backed).
    provider
        LLM generation backend.
    vector_store
        Optional pre-built vector store. If None, a new one is created.
    base_profile_id
        Policy profile for governance evaluation.
    retrieval_k
        Number of chunks to retrieve per query.
    """

    def __init__(
        self,
        *,
        tcs_client: TCSClient,
        provider: Optional[LLMProvider] = None,
        vector_store: Optional[SimpleVectorStore] = None,
        base_profile_id: str = "fin-r3-a4-ct4",
        retrieval_k: int = 5,
    ) -> None:
        self.client = tcs_client
        self.provider = provider or MockProvider()
        self.store = vector_store or SimpleVectorStore()
        self.base_profile_id = base_profile_id
        self.retrieval_k = retrieval_k

    def ingest_documents(self, doc_dir: str) -> int:
        """Embed and store documents. Returns count of chunks ingested."""
        return self.store.ingest_directory(doc_dir)

    def query(self, user_query: str) -> GovernedQueryResult:
        """
        Full governed RAG pipeline:

        1. Retrieve relevant chunks from vector store
        2. Generate LLM response from query + chunks
        3. Submit to TCS governance
        4. Return governed result (response or block notice)
        """
        latency: Dict[str, float] = {}
        t_total = time.perf_counter()

        # Step 1: Retrieve
        t0 = time.perf_counter()
        chunks = self.store.retrieve(user_query, k=self.retrieval_k)
        latency["retrieval_ms"] = round((time.perf_counter() - t0) * 1000, 1)

        # Step 2: Generate
        context_texts = [c["content"] for c in chunks]
        t0 = time.perf_counter()
        raw_response = self.provider.generate(user_query, context_texts)
        latency["generation_ms"] = round((time.perf_counter() - t0) * 1000, 1)

        # Step 3: Govern
        # Include the candidate answer as a synthetic chunk so the
        # governance layer's injection scanner examines the LLM output
        # text, not only the retrieved chunks. This is the correct
        # architecture: TCS must scan the generated response for
        # prohibited patterns (C3) before delivery.
        governed_chunks = list(chunks) + [{
            "chunk_id": "llm-output",
            "source_doc": "llm-generation",
            "version": "live",
            "content": raw_response,
            "similarity_score": 1.0,
        }]
        t0 = time.perf_counter()
        gov_result = self.client.govern(
            query=user_query,
            retrieved_chunks=governed_chunks,
            candidate_answer=raw_response,
            base_profile_id=self.base_profile_id,
        )
        latency["governance_ms"] = round((time.perf_counter() - t0) * 1000, 1)

        latency["total_ms"] = round((time.perf_counter() - t_total) * 1000, 1)

        # Step 4: Build result
        return GovernedQueryResult(
            query=user_query,
            retrieval_chunks=chunks,
            raw_llm_response=raw_response,
            governance_decision=gov_result.decision,
            governed_response=raw_response if gov_result.allowed else None,
            certificate_id=gov_result.certificate_id,
            tis_current=gov_result.tis_current,
            tis_raw=gov_result.tis_raw,
            gate_passed=gov_result.gate_passed,
            blocked=gov_result.blocked,
            blocking_reason=gov_result.blocking_reason,
            requires_human_review=gov_result.requires_human_review,
            latency_ms=latency,
        )

    def query_batch(self, queries: List[str]) -> List[GovernedQueryResult]:
        """Run multiple queries sequentially."""
        return [self.query(q) for q in queries]
