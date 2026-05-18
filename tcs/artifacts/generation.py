"""
tcs.artifacts.generation
========================

Generation tier for Phase 5 Slice 5.2.

Produces a ``ResponseArtifact`` from one of four generation modes,
**without performing any governance evaluation**. The artifact captures
everything that produced the output so a later /v2/evaluate call can
score it against any policy without re-calling the LLM.

Mode semantics (load-bearing):

  raw_llm
      Direct provider call with the user's prompt and no other framing.
      No retrieval. No domain system prompt. No policy/standards
      grounding injected. ``system_prompt_used`` may be either None
      (the default — purest "raw" reading) or a caller-supplied
      ``system_prompt_override`` (recorded verbatim for audit). This
      mode exists so a reviewer can compare *what the model would
      have said unprompted* against *what TCS observed/enforced on
      the same output*.

  rag_llm
      Retrieve N chunks from the active corpus, build a grounding
      system prompt derived from the active pack's industry (NOT
      the leftover "financial advisory" hardcode), call the LLM,
      record (rag_enabled=True, rag_context, retrieved_sources,
      system_prompt_used).

  agent_workflow
      Route through the WorkflowOrchestrator (RAG node → LLM node
      today; extensible to multi-connector chains). The full
      ``workflow_trace`` is captured on the artifact.

  human_composed
      No LLM call. The caller supplies the draft text and
      recipient_context; the function constructs the artifact and
      returns. Important architectural test: ``test_human_composed_
      does_not_call_llm`` asserts the provider client is never
      instantiated for this path.

API key discipline: every generator accepts ``api_key`` in-memory
only. No persistence, no logging. The key is passed into the
provider constructor per call and goes out of scope when this
function returns.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from tcs.artifacts.models import (
    GENERATION_MODE_AGENT_WORKFLOW,
    GENERATION_MODE_HUMAN_COMPOSED,
    GENERATION_MODE_RAG_LLM,
    GENERATION_MODE_RAW_LLM,
    ResponseArtifact,
)


# --------------------------------------------------------------------------- #
# System prompt derivation                                                     #
# --------------------------------------------------------------------------- #
#
# Replaces the hardcoded "You are a financial advisory AI" leftover that
# was bleeding through into medical contexts. The system prompt for a
# rag_llm or agent_workflow call now derives from the active pack's
# industry, so a Medical Devices pack produces a clinical decision
# support framing and a Financial Services pack produces a financial
# advisory framing. The exact string used is recorded verbatim in
# ``ResponseArtifact.system_prompt_used`` for audit reproducibility.

_RAG_SYSTEM_PROMPT_BY_INDUSTRY: Dict[str, str] = {
    "life_sciences": (
        "You are a clinical decision support AI assistant. "
        "Answer based strictly on the provided context. "
        "If the context does not contain the answer, say so explicitly "
        "and recommend the appropriate clinician consultation."
    ),
    "financial_services": (
        "You are a financial advisory AI assistant. "
        "Answer based strictly on the provided context. "
        "If the context does not contain the answer, say so explicitly "
        "and recommend speaking with a licensed advisor."
    ),
    "pharma_life_sciences": (
        "You are a pharmacovigilance / regulated life-sciences AI "
        "assistant. Answer based strictly on the provided context. "
        "If the context does not contain the answer, say so explicitly."
    ),
    "manufacturing": (
        "You are a manufacturing operations AI assistant. "
        "Answer based strictly on the provided context."
    ),
    "enterprise": (
        "You are an enterprise knowledge AI assistant. "
        "Answer based strictly on the provided context."
    ),
}


def _derive_rag_system_prompt(industry: Optional[str]) -> str:
    """
    Return the grounding system prompt for a rag_llm / agent_workflow
    call given the active pack's industry. Falls back to a neutral
    generic prompt when the industry is unknown — never to the
    hardcoded "financial advisory" string that previously leaked
    into medical contexts.
    """
    if industry and industry in _RAG_SYSTEM_PROMPT_BY_INDUSTRY:
        return _RAG_SYSTEM_PROMPT_BY_INDUSTRY[industry]
    return (
        "You are an AI assistant. Answer based strictly on the "
        "provided context. If the context does not contain the "
        "answer, say so explicitly."
    )


# --------------------------------------------------------------------------- #
# Provider clients                                                             #
# --------------------------------------------------------------------------- #
#
# Phase-5 generation has its own minimal provider clients so raw_llm can
# bypass the existing demo pipeline's RAG-grounded signature (which
# hardcoded the financial advisory system prompt). These clients take
# an explicit message list and return a string. They do NOT inject any
# default system prompt — if the caller wants one, they pass it.
#
# Errors propagate up; the calling generator wraps them into
# ResponseArtifact.generation_error so the artifact still gets persisted
# for audit even on failure.

class _GenerationError(Exception):
    """Wrapper for provider/auth errors surfaced from generation."""


def _call_openai(
    *, api_key: Optional[str], model: str,
    messages: List[Dict[str, str]],
) -> str:
    """Direct OpenAI completion call. No hidden framing."""
    if not api_key:
        raise _GenerationError("OpenAI provider requires an api_key")
    try:
        import openai  # lazy
    except ImportError as e:
        raise _GenerationError(
            "openai package not installed; pip install -r requirements-llm.txt"
        ) from e
    client = openai.OpenAI(api_key=api_key)
    resp = client.chat.completions.create(
        model=model,
        messages=messages,
        max_tokens=500,
    )
    return resp.choices[0].message.content or ""


def _call_anthropic(
    *, api_key: Optional[str], model: str,
    system: Optional[str], user_text: str,
) -> str:
    """Direct Anthropic messages call. system is optional and explicit."""
    if not api_key:
        raise _GenerationError("Anthropic provider requires an api_key")
    try:
        import anthropic  # lazy
    except ImportError as e:
        raise _GenerationError(
            "anthropic package not installed; pip install -r requirements-llm.txt"
        ) from e
    client = anthropic.Anthropic(api_key=api_key)
    kwargs: Dict[str, Any] = {
        "model": model,
        "max_tokens": 500,
        "messages": [{"role": "user", "content": user_text}],
    }
    if system:
        kwargs["system"] = system
    resp = client.messages.create(**kwargs)
    return resp.content[0].text


def _call_mock(*, user_text: str, context_chunks: Optional[List[str]] = None) -> str:
    """
    Deterministic mock provider. Echoes the input shape so tests can
    assert "raw mock saw only the prompt, no context" or "rag mock saw
    N chunks." Does NOT pull in the demo pipeline's MockProvider —
    keeping this client local lets the test isolation be tight.
    """
    if context_chunks:
        return (
            f"[MOCK PROVIDER] saw prompt of {len(user_text)} chars + "
            f"{len(context_chunks)} retrieved chunk(s). "
            f"First chunk preview: "
            f"{(context_chunks[0] or '')[:140]!r}"
        )
    return (
        f"[MOCK PROVIDER] saw prompt of {len(user_text)} chars only. "
        f"No retrieval was performed. Echo: {user_text[:140]!r}"
    )


# --------------------------------------------------------------------------- #
# Retrieval helper                                                             #
# --------------------------------------------------------------------------- #

# Industry → corpus directory name. Mirrors the mapping used by
# routes_query._get_vector_store so rag_llm pulls from the same files
# the legacy /v2/query path used. Centralized here so future industries
# can be added in one place.
_CORPUS_DIRS: Dict[str, str] = {
    "financial_services": "documents",
    "life_sciences":      "medical_documents",
}
_DEFAULT_CORPUS = "documents"

_vector_store_cache: Dict[str, Any] = {}


def _get_vector_store(industry: Optional[str]):
    """
    Cache-friendly vector store accessor. Mirrors the behavior of
    routes_query._get_vector_store so rag_llm and the legacy /v2/query
    path read from the same corpus files.
    """
    corpus = _CORPUS_DIRS.get(industry or "", _DEFAULT_CORPUS)
    key = f"store::{corpus}"
    if key in _vector_store_cache:
        return _vector_store_cache[key]
    from demos.governed_rag.vector_store import SimpleVectorStore
    vs = SimpleVectorStore()
    docs_dir = str(
        Path(__file__).resolve().parent.parent.parent
        / "demos" / "governed_rag" / corpus
    )
    if Path(docs_dir).is_dir():
        vs.ingest_directory(docs_dir)
    _vector_store_cache[key] = vs
    return vs


# --------------------------------------------------------------------------- #
# Mode-specific generators                                                     #
# --------------------------------------------------------------------------- #

def _generate_raw_llm(
    *,
    prompt: str,
    provider: str,
    model: str,
    api_key: Optional[str],
    system_prompt_override: Optional[str],
    generation_identity: Dict[str, Any],
) -> ResponseArtifact:
    """
    Truly raw: no RAG, no domain system prompt unless the caller
    explicitly passes one. system_prompt_used is recorded verbatim
    (including None if no system prompt was sent).

    This is the path that lets reviewers compare "what the model said
    unprompted" against "what TCS observed/enforced on the same
    output." Don't add hidden defaults here — if a default ever
    becomes necessary, surface it explicitly via
    system_prompt_override at the caller.
    """
    raw_output: Optional[str] = None
    error: Optional[str] = None
    try:
        if provider == "mock":
            raw_output = _call_mock(user_text=prompt)
        elif provider == "openai":
            messages: List[Dict[str, str]] = []
            if system_prompt_override:
                messages.append({"role": "system", "content": system_prompt_override})
            messages.append({"role": "user", "content": prompt})
            raw_output = _call_openai(api_key=api_key, model=model, messages=messages)
        elif provider == "anthropic":
            raw_output = _call_anthropic(
                api_key=api_key, model=model,
                system=system_prompt_override, user_text=prompt,
            )
        else:
            raise _GenerationError(f"unknown provider {provider!r}")
    except _GenerationError as e:
        error = str(e)
    except Exception as e:  # noqa: BLE001 — capture provider errors verbatim
        error = f"{type(e).__name__}: {e}"

    return ResponseArtifact(
        generation_mode=GENERATION_MODE_RAW_LLM,
        prompt=prompt,
        raw_output=raw_output,
        provider=provider,
        model=model,
        # Recorded verbatim. None when no system prompt was sent.
        # The transparency invariant the user pinned: raw_llm must
        # surface exactly what framing (if any) the model saw.
        system_prompt_used=system_prompt_override,
        rag_enabled=False,
        rag_context=None,
        retrieved_sources=[],
        recipient_context={},
        generation_identity=dict(generation_identity),
        generation_error=error,
    )


def _generate_rag_llm(
    *,
    prompt: str,
    provider: str,
    model: str,
    api_key: Optional[str],
    industry_hint: Optional[str],
    retrieval_k: int,
    generation_identity: Dict[str, Any],
) -> ResponseArtifact:
    """
    Retrieve + ground + generate. The system prompt is derived from
    the active pack's industry (not hardcoded). Retrieved sources are
    captured verbatim on the artifact.
    """
    raw_output: Optional[str] = None
    error: Optional[str] = None
    retrieved_sources: List[Dict[str, Any]] = []
    rag_context: Optional[str] = None
    system_prompt = _derive_rag_system_prompt(industry_hint)

    try:
        vs = _get_vector_store(industry_hint)
        # SimpleVectorStore exposes .retrieve(query, k=...). Result
        # items are dicts with at least chunk_id / source_doc /
        # version / similarity_score / content.
        chunks = vs.retrieve(prompt, k=retrieval_k)
        for c in chunks:
            retrieved_sources.append({
                "chunk_id": c.get("chunk_id"),
                "source_doc": c.get("source_doc"),
                "version": c.get("version"),
                "similarity_score": c.get("similarity_score"),
            })
        bodies = [c.get("content", "") for c in chunks if c.get("content")]
        rag_context = "\n\n".join(bodies) if bodies else None

        # Build the prompt the LLM actually sees.
        user_text = (
            f"Context:\n{rag_context or '(no documents retrieved)'}\n\n"
            f"Question: {prompt}"
        )

        if provider == "mock":
            raw_output = _call_mock(user_text=user_text, context_chunks=bodies)
        elif provider == "openai":
            messages = [
                {"role": "system", "content": system_prompt},
                {"role": "user",   "content": user_text},
            ]
            raw_output = _call_openai(api_key=api_key, model=model, messages=messages)
        elif provider == "anthropic":
            raw_output = _call_anthropic(
                api_key=api_key, model=model,
                system=system_prompt, user_text=user_text,
            )
        else:
            raise _GenerationError(f"unknown provider {provider!r}")
    except _GenerationError as e:
        error = str(e)
    except Exception as e:  # noqa: BLE001
        error = f"{type(e).__name__}: {e}"

    return ResponseArtifact(
        generation_mode=GENERATION_MODE_RAG_LLM,
        prompt=prompt,
        raw_output=raw_output,
        provider=provider,
        model=model,
        system_prompt_used=system_prompt,
        rag_enabled=True,
        rag_context=rag_context,
        retrieved_sources=retrieved_sources,
        recipient_context={"industry_hint": industry_hint} if industry_hint else {},
        generation_identity=dict(generation_identity),
        generation_error=error,
    )


def _generate_agent_workflow(
    *,
    prompt: str,
    provider: str,
    model: str,
    api_key: Optional[str],
    industry_hint: Optional[str],
    retrieval_k: int,
    generation_identity: Dict[str, Any],
) -> ResponseArtifact:
    """
    Workflow-orchestrator path. RAG node + LLM node today; the multi-
    connector capability is the slot for future expansion. The full
    workflow trace is captured on the artifact so downstream
    evaluations have the per-node BACK signals available.

    The system prompt and retrieval rules are the same as rag_llm —
    the difference is the orchestrator + trace.
    """
    raw_output: Optional[str] = None
    error: Optional[str] = None
    retrieved_sources: List[Dict[str, Any]] = []
    rag_context: Optional[str] = None
    workflow_trace_dict: Optional[Dict[str, Any]] = None
    workflow_trace_id: Optional[str] = None
    system_prompt = _derive_rag_system_prompt(industry_hint)

    # Adapter: the workflow's LLMConnector calls provider.generate(query, context).
    # We wrap our raw provider clients to satisfy that signature without
    # leaking the financial-advisory hardcode. Each call gets the
    # industry-derived system prompt.
    class _WorkflowProvider:
        def generate(self_, query: str, context: List[str]) -> str:  # noqa: N805
            user_text = (
                f"Context:\n{(chr(10) + chr(10)).join(context) if context else '(no documents)'}\n\n"
                f"Question: {query}"
            )
            if provider == "mock":
                return _call_mock(user_text=user_text, context_chunks=context)
            if provider == "openai":
                return _call_openai(
                    api_key=api_key, model=model,
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user",   "content": user_text},
                    ],
                )
            if provider == "anthropic":
                return _call_anthropic(
                    api_key=api_key, model=model,
                    system=system_prompt, user_text=user_text,
                )
            raise _GenerationError(f"unknown provider {provider!r}")

    try:
        from tcs.workflow import (
            GovernedNode,
            NodeType,
            WorkflowOrchestrator,
        )
        from tcs.workflow.connectors import LLMConnector, RAGConnector
        from tcs.workflow.orchestrator import WorkflowStep

        vs = _get_vector_store(industry_hint)
        rag_connector = RAGConnector(store=vs, retrieval_k=retrieval_k)
        llm_connector = LLMConnector(
            provider=_WorkflowProvider(),
            provider_name=provider,
            model=model,
            context_key="rag",
        )
        rag_node = GovernedNode(
            node_id="rag-retrieve", name="RAG retrieval",
            node_type=NodeType.RAG,
            connection_type=rag_connector.connection_type(),
            sensitivity_tier="T2",
        )
        llm_node = GovernedNode(
            node_id="llm-generate", name="LLM generation",
            node_type=NodeType.LLM,
            connection_type=llm_connector.connection_type(),
            sensitivity_tier="T2",
        )
        orch = WorkflowOrchestrator()
        trace = orch.execute(
            steps=[
                WorkflowStep(node=rag_node, connector=rag_connector, context_key="rag"),
                WorkflowStep(node=llm_node, connector=llm_connector, context_key="llm"),
            ],
            query=prompt,
            # Generation tier does NOT bind to a policy profile —
            # that's the evaluate tier's job. The trace requires a
            # non-empty base_profile_id, so we use a sentinel that
            # downstream code can recognize and replace at /v2/evaluate.
            base_profile_id="_artifact_capture_only",
            user_identity={"provider": provider, "model": model},
            metadata={"source": "artifacts.generation.agent_workflow"},
        )
        workflow_trace_dict = trace.to_dict()
        workflow_trace_id = trace.workflow_id

        # Surface connector-level errors via the artifact's error field
        # rather than raising — the artifact must still persist for audit.
        llm_event = trace.get_node("llm-generate").event
        if llm_event and llm_event.error:
            error = f"LLM connector error: {llm_event.error}"
        else:
            raw_output = trace.final_output

        rag_node_obj = trace.get_node("rag-retrieve")
        if isinstance(rag_node_obj.payload, list):
            chunks = rag_node_obj.payload
            for c in chunks[:retrieval_k]:
                if isinstance(c, dict):
                    retrieved_sources.append({
                        "chunk_id": c.get("chunk_id"),
                        "source_doc": c.get("source_doc"),
                        "version": c.get("version"),
                        "similarity_score": c.get("similarity_score"),
                    })
            bodies = [
                c.get("content", "") for c in chunks
                if isinstance(c, dict) and c.get("content")
            ]
            rag_context = "\n\n".join(bodies) if bodies else None
    except _GenerationError as e:
        error = str(e)
    except Exception as e:  # noqa: BLE001
        error = f"{type(e).__name__}: {e}"

    return ResponseArtifact(
        generation_mode=GENERATION_MODE_AGENT_WORKFLOW,
        prompt=prompt,
        raw_output=raw_output,
        provider=provider,
        model=model,
        system_prompt_used=system_prompt,
        rag_enabled=True,
        rag_context=rag_context,
        retrieved_sources=retrieved_sources,
        workflow_trace_id=workflow_trace_id,
        workflow_trace=workflow_trace_dict,
        recipient_context={"industry_hint": industry_hint} if industry_hint else {},
        generation_identity=dict(generation_identity),
        generation_error=error,
    )


def _generate_human_composed(
    *,
    draft: str,
    recipient_context: Dict[str, Any],
    generation_identity: Dict[str, Any],
    prompt: Optional[str] = None,
) -> ResponseArtifact:
    """
    No LLM. A human drafted the text; we just package it for
    governance evaluation.

    Architectural invariant (tested): this function must NEVER call
    a provider. The flagship Phase-5 use case (a human writing to a
    pregnant client about lithium) depends on this guarantee.
    """
    if not draft or not draft.strip():
        raise _GenerationError(
            "human_composed requires a non-empty draft"
        )
    return ResponseArtifact(
        generation_mode=GENERATION_MODE_HUMAN_COMPOSED,
        prompt=prompt,                # optional context frame; may be None
        raw_output=draft,
        provider=None,                # no LLM
        model=None,
        system_prompt_used=None,
        rag_enabled=False,
        rag_context=None,
        retrieved_sources=[],
        recipient_context=dict(recipient_context or {}),
        generation_identity=dict(generation_identity),
        generation_error=None,
    )


# --------------------------------------------------------------------------- #
# Public dispatch                                                              #
# --------------------------------------------------------------------------- #

def generate(
    *,
    generation_mode: str,
    prompt: Optional[str] = None,
    draft: Optional[str] = None,
    provider: Optional[str] = None,
    model: Optional[str] = None,
    api_key: Optional[str] = None,
    industry_hint: Optional[str] = None,
    retrieval_k: int = 5,
    system_prompt_override: Optional[str] = None,
    recipient_context: Optional[Dict[str, Any]] = None,
    generation_identity: Optional[Dict[str, Any]] = None,
) -> ResponseArtifact:
    """
    Dispatch by generation_mode. Returns the constructed artifact.
    Does NOT persist — that's the caller's responsibility (the route
    handler wraps this with ArtifactStore.insert_artifact).

    API key is in-memory only and goes out of scope when this function
    returns. Never logged, never persisted.
    """
    identity = generation_identity or {}

    if generation_mode == GENERATION_MODE_HUMAN_COMPOSED:
        return _generate_human_composed(
            draft=draft or "",
            prompt=prompt,
            recipient_context=recipient_context or {},
            generation_identity=identity,
        )

    if not prompt:
        raise _GenerationError(
            f"{generation_mode} requires a prompt"
        )
    provider = provider or "mock"
    model = model or "deterministic"

    if generation_mode == GENERATION_MODE_RAW_LLM:
        return _generate_raw_llm(
            prompt=prompt, provider=provider, model=model, api_key=api_key,
            system_prompt_override=system_prompt_override,
            generation_identity=identity,
        )
    if generation_mode == GENERATION_MODE_RAG_LLM:
        return _generate_rag_llm(
            prompt=prompt, provider=provider, model=model, api_key=api_key,
            industry_hint=industry_hint, retrieval_k=retrieval_k,
            generation_identity=identity,
        )
    if generation_mode == GENERATION_MODE_AGENT_WORKFLOW:
        return _generate_agent_workflow(
            prompt=prompt, provider=provider, model=model, api_key=api_key,
            industry_hint=industry_hint, retrieval_k=retrieval_k,
            generation_identity=identity,
        )

    raise _GenerationError(f"unknown generation_mode {generation_mode!r}")


__all__ = [
    "generate",
    "_derive_rag_system_prompt",   # exported for testing
    "_GenerationError",
]
