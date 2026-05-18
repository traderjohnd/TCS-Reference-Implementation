"""
tcs.api.routes_query
====================

POST /v2/query — Live governed RAG query endpoint.

Accepts a user query, runs it through the governed RAG pipeline
(retrieve -> generate -> govern), and returns the governed response
with full governance metadata.

The frontend sends the LLM provider, API key, and model with each
request. Keys are used in-memory only — never stored or logged.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Request
from pydantic import BaseModel


router = APIRouter()


# --------------------------------------------------------------------------- #
# Request / Response models                                                    #
# --------------------------------------------------------------------------- #

class QueryRequest(BaseModel):
    query: str
    # profile_id: when omitted, the route resolves it from the active
    # deployed pack (Standards Composer or any pack deployment). If no
    # pack is active, falls back to the canonical fin-r3-a4-ct4 demo
    # profile. Explicit values always win over the active pack.
    profile_id: Optional[str] = None
    provider: str = "mock"          # openai | anthropic | mock
    api_key: Optional[str] = None   # used in-memory only, never stored
    model: Optional[str] = None     # e.g. gpt-4o-mini, claude-sonnet-4-20250514


class QueryResponse(BaseModel):
    query: str
    response: Optional[str]
    blocked: bool
    decision: str
    certificate_id: Optional[str]
    tis_current: Optional[float]
    tis_raw: Optional[float]
    s_base: Optional[float] = None
    gate_passed: Optional[bool] = None
    blocking_reason: Optional[str] = None
    requires_human_review: bool = False
    retrieval_chunks: List[Dict[str, Any]] = []
    latency_ms: Dict[str, float] = {}
    llm_provider: str = ""
    llm_model: str = ""
    # Phase 4 / Slice 3: governance evidence the UI surfaces in the
    # expandable governance layer. Populated when the workflow-trace
    # path runs; None on the legacy path.
    component_scores: Optional[Dict[str, float]] = None
    component_weights: Optional[Dict[str, float]] = None
    gate_results: Optional[Dict[str, str]] = None
    thresholds: Optional[Dict[str, float]] = None
    workflow_trace: Optional[Dict[str, Any]] = None
    policy_profile_id: Optional[str] = None
    connection_type: Optional[str] = None


# --------------------------------------------------------------------------- #
# Pipeline management — keyed by (provider, model)                             #
# --------------------------------------------------------------------------- #

_pipeline_cache: Dict[str, Any] = {}
_tcs_client_cache: Dict[str, Any] = {}


def _get_tcs_client(store):
    """Get or create a TCS client that talks directly to the store."""
    if "client" in _tcs_client_cache:
        return _tcs_client_cache["client"]

    from tcs.sdk.client import TCSClient
    from fastapi.testclient import TestClient
    from tcs.api.app import create_app

    inner_app = create_app(store=store)
    test_client = TestClient(inner_app)
    test_client.__enter__()
    tcs_client = TCSClient.from_test_client(test_client)

    _tcs_client_cache["client"] = tcs_client
    _tcs_client_cache["test_client"] = test_client
    return tcs_client


# Domain-keyed RAG corpora (Phase 4.5).
#
# The active deployed pack's industry determines which corpus is loaded
# for the workflow. Each corpus is built lazily and cached. The default
# (unknown / unmatched industry) falls back to the financial demo
# corpus so existing behavior is preserved.
_CORPUS_DIRS = {
    # industry key (from active pack's composer_metadata) -> dir name
    "financial_services":     "documents",
    "life_sciences":          "medical_documents",
    "general_ai_governance":  "documents",   # no dedicated corpus yet
}
_DEFAULT_CORPUS = "documents"


def _get_vector_store(industry: Optional[str] = None):
    """
    Return a vector store appropriate for the given industry.

    Stores are cached per directory so a second request for the same
    corpus does not re-ingest. When ``industry`` is None or unknown,
    falls back to the financial demo corpus.
    """
    corpus_dirname = _CORPUS_DIRS.get(industry or "", _DEFAULT_CORPUS)
    cache_key = f"store::{corpus_dirname}"
    if cache_key in _pipeline_cache:
        return _pipeline_cache[cache_key]

    from demos.governed_rag.vector_store import SimpleVectorStore
    vs = SimpleVectorStore()

    docs_dir = str(
        Path(__file__).resolve().parent.parent.parent
        / "demos" / "governed_rag" / corpus_dirname
    )
    if Path(docs_dir).is_dir():
        chunk_count = vs.ingest_directory(docs_dir)
        print(f"[TCS Query] Ingested {chunk_count} chunks from {docs_dir}")

    _pipeline_cache[cache_key] = vs
    return vs


def _build_provider(provider_name: str, api_key: Optional[str], model: Optional[str]):
    """Build an LLM provider from the request parameters."""
    from demos.governed_rag.pipeline import MockProvider

    if provider_name == "openai":
        if not api_key:
            raise ValueError("OpenAI API key is required")
        import openai
        client = openai.OpenAI(api_key=api_key)

        # Parse model name and mode: "gpt-5.5 (Thinking)" -> model=gpt-5.5, thinking=True
        raw_model = model or "gpt-5.5 (Instant)"
        is_thinking = "(Thinking)" in raw_model
        api_model = raw_model.replace(" (Instant)", "").replace(" (Thinking)", "").strip()
        display_name = raw_model

        # Reasoning models (o3, o4-mini, etc.) always use thinking mode
        is_reasoning_model = api_model.startswith("o3") or api_model.startswith("o4")

        class RequestScopedOpenAI:
            def generate(self, query, context):
                context_text = "\n\n".join(context)
                messages = [
                    {"role": "system", "content": (
                        "You are a financial advisory AI. Answer based strictly "
                        "on the provided context. Cite sources when possible."
                    )},
                    {"role": "user", "content": f"Context:\n{context_text}\n\nQuestion: {query}"},
                ]

                kwargs = {"model": api_model, "messages": messages}

                # GPT-5.x and reasoning models require max_completion_tokens
                is_new_model = api_model.startswith("gpt-5") or api_model.startswith("gpt-4.1")

                if is_reasoning_model or is_thinking or is_new_model:
                    # GPT-5.x, GPT-4.1, and reasoning models: max_completion_tokens, no temperature
                    kwargs["max_completion_tokens"] = 2000 if (is_reasoning_model or is_thinking) else 500
                else:
                    # Legacy models (gpt-4o, etc): standard completion
                    kwargs["max_tokens"] = 500
                    kwargs["temperature"] = 0.3

                response = client.chat.completions.create(**kwargs)
                return response.choices[0].message.content or ""

        return RequestScopedOpenAI(), display_name

    elif provider_name == "anthropic":
        if not api_key:
            raise ValueError("Anthropic API key is required")
        import anthropic
        client = anthropic.Anthropic(api_key=api_key)
        model_name = model or "claude-sonnet-4-20250514"

        class RequestScopedAnthropic:
            def generate(self, query, context):
                context_text = "\n\n".join(context)
                response = client.messages.create(
                    model=model_name,
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

        return RequestScopedAnthropic(), model_name

    else:
        return MockProvider(), "deterministic"


# --------------------------------------------------------------------------- #
# GET /v2/query/status                                                         #
# --------------------------------------------------------------------------- #

@router.get("/query/status")
def query_status() -> Dict[str, Any]:
    """Return available providers and models."""
    return {
        "providers": [
            {
                "id": "openai",
                "name": "OpenAI",
                "models": [
                    "gpt-5.5 (Instant)", "gpt-5.5 (Thinking)",
                    "gpt-5.4 (Instant)", "gpt-5.4 (Thinking)",
                    "gpt-5.3 (Instant)", "gpt-5.3 (Thinking)",
                    "gpt-5.2 (Instant)", "gpt-5.2 (Thinking)",
                    "gpt-4.1", "gpt-4.1-mini", "gpt-4.1-nano",
                    "gpt-4o", "gpt-4o-mini",
                    "o3", "o3-mini", "o4-mini",
                ],
                "requires_key": True,
            },
            {
                "id": "anthropic",
                "name": "Anthropic",
                "models": ["claude-sonnet-4-20250514", "claude-opus-4-20250514", "claude-haiku-4-20250514"],
                "requires_key": True,
            },
            {
                "id": "mock",
                "name": "Mock (No API Key)",
                "models": ["deterministic"],
                "requires_key": False,
            },
        ],
    }


# --------------------------------------------------------------------------- #
# POST /v2/query — governed RAG query                                          #
# --------------------------------------------------------------------------- #

def _workflow_trace_enabled() -> bool:
    """
    Slice-1 feature flag for the workflow-graph path.

    Default is ``False`` — the legacy ``GovernedRAGPipeline`` path runs
    until the validation harness confirms parity. Set
    ``TCS_WORKFLOW_TRACE_ENABLED=true`` (or 1, yes, on) to opt in.
    Read on every request so an operator can flip the flag without
    restarting the server.
    """
    return os.getenv("TCS_WORKFLOW_TRACE_ENABLED", "false").strip().lower() in (
        "true", "1", "yes", "on",
    )


# --------------------------------------------------------------------------- #
# Phase 5 Slice 5.4 — /v2/query artifact + evaluation persistence              #
# --------------------------------------------------------------------------- #
#
# /v2/query was originally a fused generate+evaluate+deliver pipeline. The
# Phase-5 sidecar architecture splits those tiers so the same captured
# output can be replayed under different policies. Rather than rewrite
# /v2/query as two separate calls (which would risk parity drift), we
# leave its scoring path bit-for-bit identical and ADD persistence
# side-effects: every request now writes a ResponseArtifact + a
# GovernanceEvaluation (evaluation_origin="query") in addition to the
# existing TC. The artifact_id is later usable by /v2/replay or
# /v2/evaluate without re-calling the LLM.

def _persist_query_artifact_and_evaluation(
    *,
    artifact_store: Any,
    body: "QueryRequest",
    provider_name: str,
    model_name: str,
    industry: Optional[str],
    trace: Any,
    tis_input: Any,
    tis_result: Any,
    decision: str,
    issued_tc: Any,
    composer_metadata: Optional[Dict[str, Any]],
) -> None:
    """
    Best-effort persistence of artifact + evaluation rows for a query.
    Raises are caught at the call site so /v2/query semantics never
    break on a persistence failure.
    """
    from tcs.artifacts import (
        EVALUATION_MODE_ENFORCE,
        EVALUATION_ORIGIN_QUERY,
        GENERATION_MODE_AGENT_WORKFLOW,
        GovernanceEvaluation,
        ResponseArtifact,
    )
    from tcs.artifacts.evaluation import _snapshot_profile

    # Extract retrieved chunks from the trace for the artifact's
    # retrieved_sources field. Pulled from the RAG node's payload.
    retrieved_sources: List[Dict[str, Any]] = []
    rag_context: Optional[str] = None
    try:
        rag_node = trace.get_node("rag-retrieve")
        if rag_node is not None and isinstance(rag_node.payload, list):
            bodies: List[str] = []
            for c in rag_node.payload:
                if not isinstance(c, dict):
                    continue
                retrieved_sources.append({
                    "chunk_id":         c.get("chunk_id"),
                    "source_doc":       c.get("source_doc"),
                    "version":          c.get("version"),
                    "similarity_score": c.get("similarity_score"),
                })
                if c.get("content"):
                    bodies.append(c["content"])
            if bodies:
                rag_context = "\n\n".join(bodies)
    except KeyError:
        pass

    # Build the artifact. The system prompt currently used in
    # /v2/query is the leftover hardcoded "financial advisory"
    # string for openai/anthropic; we record it as None here rather
    # than reaching into the connector internals to extract it.
    # Future cleanup will route /v2/query through the same industry-
    # derived prompt logic as /v2/generate, at which point this
    # field will populate. The artifact persists faithfully whatever
    # the scoring path actually saw.
    artifact = ResponseArtifact(
        generation_mode=GENERATION_MODE_AGENT_WORKFLOW,
        prompt=body.query,
        raw_output=trace.final_output,
        provider=provider_name,
        model=model_name,
        system_prompt_used=None,
        rag_enabled=True,
        rag_context=rag_context,
        retrieved_sources=retrieved_sources,
        workflow_trace_id=trace.workflow_id,
        workflow_trace=trace.to_dict(),
        recipient_context={"industry_hint": industry} if industry else {},
        generation_identity={
            "requesting_identity": "query_endpoint",
            "identity_type": "system",
            "role": "runtime_query_path",
            "session_id": getattr(trace, "workflow_id", None),
        },
    )
    artifact_store.insert_artifact(artifact)

    # Build the evaluation. Reuses the SAME tis_input / tis_result /
    # decision the scoring path already computed — no re-scoring, no
    # divergence risk. evaluation_origin="query" tags this row as
    # runtime, not direct or replay.
    profile = tis_input.policy_profile
    snapshot = _snapshot_profile(profile)
    rule_matches = (
        tis_input.context_metadata.get("governance_rule_matches")
        if tis_input.context_metadata else None
    )
    selected_standards: List[str] = []
    if composer_metadata:
        selected_standards = list(composer_metadata.get("standards") or [])

    evaluation = GovernanceEvaluation(
        artifact_id=artifact.artifact_id,
        mode=EVALUATION_MODE_ENFORCE,
        policy_profile_id=profile.profile_id,
        policy_profile_snapshot=snapshot,
        selected_standards=selected_standards,
        enabled_controls=[],
        rule_matches=rule_matches,
        component_scores={
            k: round(v, 4) for k, v in tis_input.dimension_scores.items()
        },
        gate_results=dict(tis_result.gate_results_by_dim),
        s_base=round(tis_result.s_base, 4),
        s_adjusted=round(tis_result.s_adj, 4),
        tis_current=round(tis_result.tis_current, 4),
        decision=decision,
        trust_certificate_id=issued_tc.certificate_id,
        evaluator_identity={
            "requesting_identity": "query_endpoint",
            "identity_type": "system",
            "role": "runtime_query_path",
        },
        evaluation_completeness_score=1.0,
        evaluation_origin=EVALUATION_ORIGIN_QUERY,
    )
    artifact_store.insert_evaluation(evaluation)


def _run_query_via_trace(
    body: "QueryRequest",
    store,
    provider,
    provider_name: str,
    model_name: str,
    composer_metadata: Optional[Dict[str, Any]] = None,
    industry: Optional[str] = None,
    artifact_store: Optional[Any] = None,
) -> "QueryResponse":
    """
    Phase 4 / Slice 1: workflow-graph query path.

    Same external behavior as the legacy ``GovernedRAGPipeline`` path,
    but internally routed through:

        WorkflowOrchestrator -> GovernedWorkflowTrace
            -> assemble_context_from_trace -> TISInput
            -> compute_tis -> map_decision -> generate_certificate
            -> store.issue

    This is the foundation for Slices 2-4: every future connector
    (API, MCP, agent chain) plugs into the same orchestrator with
    no change to the engine, decision logic, or TC schema.
    """
    from tcs.decision_engine import map_decision
    from tcs.governed_context import assemble_context_from_trace
    from tcs.tis_engine import compute_tis
    from tcs.trust_certificate import generate_certificate
    from tcs.workflow import (
        GovernedNode,
        NodeType,
        WorkflowOrchestrator,
    )
    from tcs.workflow.connectors import LLMConnector, RAGConnector
    from tcs.workflow.orchestrator import WorkflowStep

    t_total = time.perf_counter()
    latency: Dict[str, float] = {}

    vector_store = _get_vector_store(industry)

    rag_connector = RAGConnector(store=vector_store, retrieval_k=5)
    llm_connector = LLMConnector(
        provider=provider,
        provider_name=provider_name,
        model=model_name,
        context_key="rag",
    )

    rag_node = GovernedNode(
        node_id="rag-retrieve",
        name="RAG retrieval",
        node_type=NodeType.RAG,
        connection_type=rag_connector.connection_type(),
        sensitivity_tier="T2",
    )
    llm_node = GovernedNode(
        node_id="llm-generate",
        name="LLM generation",
        node_type=NodeType.LLM,
        connection_type=llm_connector.connection_type(),
        sensitivity_tier="T2",
    )

    orchestrator = WorkflowOrchestrator()
    t0 = time.perf_counter()
    trace = orchestrator.execute(
        steps=[
            WorkflowStep(node=rag_node, connector=rag_connector, context_key="rag"),
            WorkflowStep(node=llm_node, connector=llm_connector, context_key="llm"),
        ],
        query=body.query,
        base_profile_id=body.profile_id,
        user_identity={"provider": provider_name, "model": model_name},
        metadata={"source": "routes_query.workflow_trace_path"},
    )
    latency["workflow_ms"] = round((time.perf_counter() - t0) * 1000, 1)

    # Surface any connector-level error directly without persisting a TC.
    llm_event = trace.get_node("llm-generate").event
    if llm_event and llm_event.error:
        latency["total_ms"] = round((time.perf_counter() - t_total) * 1000, 1)
        return QueryResponse(
            query=body.query,
            response=None,
            blocked=True,
            decision="Error",
            certificate_id=None,
            tis_current=None,
            tis_raw=None,
            gate_passed=None,
            blocking_reason=f"LLM provider error: {llm_event.error}",
            requires_human_review=False,
            retrieval_chunks=[],
            latency_ms=latency,
            llm_provider=provider_name,
            llm_model=model_name,
        )

    # Compile trace -> TISInput, score, decide, issue TC.
    t0 = time.perf_counter()
    tis_input, _resolved = assemble_context_from_trace(trace)
    # Inject Standards Composer audit trail into context_metadata so
    # generate_certificate() carries it onto the issued TC.
    if composer_metadata:
        tis_input.context_metadata["composer_metadata"] = dict(composer_metadata)
    tis_result = compute_tis(tis_input)
    decision, requires_review = map_decision(tis_input, tis_result)
    tc = generate_certificate(tis_input, tis_result, decision, requires_review)
    issued_tc = store.issue(tc)
    latency["governance_ms"] = round((time.perf_counter() - t0) * 1000, 1)
    latency["total_ms"] = round((time.perf_counter() - t_total) * 1000, 1)

    blocked = decision in ("Hold", "Escalate", "Stop")
    response_text = trace.final_output if not blocked else None

    # Phase 5 Slice 5.4 — alongside the existing scoring path, persist
    # a ResponseArtifact + GovernanceEvaluation so /v2/query
    # participates in the runtime sidecar audit trail (and is
    # replayable later via /v2/replay or /v2/evaluate). This is
    # PERSISTENCE INSTRUMENTATION: it does not change the scoring
    # path or the response shape — those remain bit-for-bit identical
    # to pre-5.4 /v2/query. The parity test in
    # tests/test_query_refactor_parity.py pins this.
    if artifact_store is not None:
        try:
            _persist_query_artifact_and_evaluation(
                artifact_store=artifact_store,
                body=body,
                provider_name=provider_name,
                model_name=model_name,
                industry=industry,
                trace=trace,
                tis_input=tis_input,
                tis_result=tis_result,
                decision=decision,
                issued_tc=issued_tc,
                composer_metadata=composer_metadata,
            )
        except Exception:  # noqa: BLE001
            # Persistence is best-effort; never break /v2/query.
            pass

    # Pull the retrieved chunks from the RAG node's payload. The
    # trace is the source of truth — no second retrieval. The GCA
    # already governed these exact chunks via the RAG node's event.
    rag_chunks_payload: List[Dict[str, Any]] = []
    try:
        rag_node = trace.get_node("rag-retrieve")
    except KeyError:
        rag_node = None
    if rag_node is not None and isinstance(rag_node.payload, list):
        for c in rag_node.payload[:5]:
            if not isinstance(c, dict):
                continue
            rag_chunks_payload.append({
                "chunk_id": c.get("chunk_id"),
                "source_doc": c.get("source_doc"),
                "version": c.get("version"),
                "content": c.get("content"),
                "similarity_score": c.get("similarity_score"),
                "tags": c.get("tags", []),
            })

    # Slice 3: surface the workflow trace + governance evidence the
    # chat UI needs to render the expandable governance layer. The
    # trace is JSON-serialized via its to_dict() helper; nodes carry
    # connector_type, sensitivity_tier, BACK signals, latency.
    workflow_trace_dict = trace.to_dict()

    return QueryResponse(
        query=body.query,
        response=response_text,
        blocked=blocked,
        decision=decision,
        certificate_id=issued_tc.certificate_id,
        tis_current=issued_tc.tis_current,
        tis_raw=issued_tc.tis_raw,
        s_base=issued_tc.s_base,
        gate_passed=issued_tc.gate_passed,
        blocking_reason=issued_tc.blocking_reason,
        requires_human_review=requires_review,
        retrieval_chunks=rag_chunks_payload,
        latency_ms=latency,
        llm_provider=provider_name,
        llm_model=model_name,
        component_scores=dict(issued_tc.component_scores),
        component_weights=dict(issued_tc.component_weights),
        gate_results=dict(issued_tc.gate_results),
        thresholds=dict(issued_tc.thresholds),
        workflow_trace=workflow_trace_dict,
        policy_profile_id=issued_tc.policy_set_id,
        connection_type=getattr(_resolved, "connection_type", None),
    )


@router.post("/query")
def run_query(body: QueryRequest, request: Request) -> QueryResponse:
    """
    Run a governed RAG query.

    1. Build LLM provider from request params (key used in-memory only)
    2. Retrieve relevant chunks from the financial policy corpus
    3. Generate an LLM response
    4. Submit to TCS governance engine
    5. Return governed result — response delivered or blocked

    When ``TCS_WORKFLOW_TRACE_ENABLED=true``, the new Phase 4
    workflow-graph path runs instead of the legacy pipeline. External
    behavior is unchanged; internally the request is routed through
    GovernedWorkflowTrace + GovernedConnector.
    """
    from demos.governed_rag.pipeline import GovernedRAGPipeline

    store = request.app.state.store
    # Phase 5 Slice 5.4 — /v2/query also writes a ResponseArtifact
    # + GovernanceEvaluation per request so the runtime sidecar audit
    # trail covers query traffic. Optional: if the app didn't register
    # an artifact_store (test paths that build a bare app), instrument
    # is silently skipped.
    artifact_store = getattr(request.app.state, "artifact_store", None)

    # Resolve profile_id. Explicit value wins; otherwise use the active
    # deployed pack (Standards Composer or any other pack); otherwise
    # fall back to the canonical demo profile. The active-pack lookup
    # is the integration point that makes Slice 4 visible end-to-end.
    # When the active pack is a Standards Composer pack, we also stash
    # its composer_metadata so it ends up on the issued Trust Certificate.
    # The active pack's industry (composed packs) or domain (built-in
    # packs) drives which RAG corpus the workflow uses.
    _active_composer_metadata: Optional[Dict[str, Any]] = None
    _active_industry: Optional[str] = None
    if not body.profile_id:
        try:
            from tcs.packs.pack_manager import get_active_pack
            active = get_active_pack()
            if active is not None:
                active_profile_id = active.get("profile_config", {}).get("profile_id")
                if active_profile_id:
                    body.profile_id = active_profile_id
                if active.get("is_composed_pack"):
                    cm = dict(active.get("composer_metadata") or {})
                    _active_composer_metadata = cm
                    _active_industry = cm.get("industry")
                else:
                    # Built-in packs use a domain string instead of
                    # composer_metadata.industry; map both to the same key.
                    _active_industry = (
                        active.get("profile_config", {}).get("domain")
                    )
        except Exception:
            pass
    if not body.profile_id:
        body.profile_id = "fin-r3-a4-ct4"
    # Stash on app state for downstream consumers / debugging.
    request.app.state._active_composer_metadata = _active_composer_metadata
    request.app.state._active_industry = _active_industry

    # Build provider from request
    try:
        provider, model_name = _build_provider(body.provider, body.api_key, body.model)
    except ValueError as e:
        return QueryResponse(
            query=body.query,
            response=None,
            blocked=True,
            decision="Error",
            certificate_id=None,
            tis_current=None,
            tis_raw=None,
            gate_passed=None,
            blocking_reason=str(e),
            requires_human_review=False,
            retrieval_chunks=[],
            latency_ms={},
            llm_provider=body.provider,
            llm_model=body.model or "unknown",
        )

    # Phase 4 / Slice 1 path — opt-in via env var.
    if _workflow_trace_enabled():
        try:
            return _run_query_via_trace(
                body=body,
                store=store,
                provider=provider,
                provider_name=body.provider,
                model_name=model_name,
                composer_metadata=_active_composer_metadata,
                industry=_active_industry,
                artifact_store=artifact_store,
            )
        except Exception as e:
            return QueryResponse(
                query=body.query,
                response=None,
                blocked=True,
                decision="Error",
                certificate_id=None,
                tis_current=None,
                tis_raw=None,
                gate_passed=None,
                blocking_reason=f"Workflow trace path error: {e}",
                requires_human_review=False,
                retrieval_chunks=[],
                latency_ms={},
                llm_provider=body.provider,
                llm_model=model_name,
            )

    # Get shared resources (legacy path also honors industry-based corpus)
    tcs_client = _get_tcs_client(store)
    vector_store = _get_vector_store(_active_industry)

    # Build pipeline with the request-scoped provider
    pipeline = GovernedRAGPipeline(
        tcs_client=tcs_client,
        provider=provider,
        vector_store=vector_store,
        base_profile_id=body.profile_id,
    )

    try:
        result = pipeline.query(body.query)
    except Exception as e:
        # LLM provider error (auth failure, rate limit, bad model, etc.)
        error_msg = str(e)
        # Extract the meaningful part from OpenAI/Anthropic error messages
        if "Error code:" in error_msg:
            # e.g. "Error code: 401 - {'error': {'message': '...'}}"
            try:
                import json as _json
                json_part = error_msg[error_msg.index("{"):]
                parsed = _json.loads(json_part.replace("'", '"'))
                error_msg = parsed.get("error", {}).get("message", error_msg)
            except Exception:
                pass
        return QueryResponse(
            query=body.query,
            response=None,
            blocked=True,
            decision="Error",
            certificate_id=None,
            tis_current=None,
            tis_raw=None,
            gate_passed=None,
            blocking_reason=f"LLM provider error: {error_msg}",
            requires_human_review=False,
            retrieval_chunks=[],
            latency_ms={},
            llm_provider=body.provider,
            llm_model=model_name,
        )

    return QueryResponse(
        query=result.query,
        response=result.governed_response,
        blocked=result.blocked,
        decision=result.governance_decision,
        certificate_id=result.certificate_id,
        tis_current=result.tis_current,
        tis_raw=result.tis_raw,
        gate_passed=result.gate_passed,
        blocking_reason=result.blocking_reason,
        requires_human_review=result.requires_human_review,
        retrieval_chunks=result.retrieval_chunks[:5],
        latency_ms=result.latency_ms,
        llm_provider=body.provider,
        llm_model=model_name,
    )
