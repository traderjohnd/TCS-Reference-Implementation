"""
tcs.api.routes_web
==================

POST /v2/query/web — governed Live Web retrieval (demo-live branch,
Commit 5).

Truthful governed path:

    User Query
      -> Optional Local Corpus Retrieval   (local_corpus_retrieval node)
      -> Provider-Hosted Web Retrieval     (provider_hosted_web_retrieval
                                            node — the PROVIDER executed
                                            the search; TCS never fetched
                                            pages itself)
      -> Source Evidence                   (web-evidence-v1, canonical)
      -> LLM Final Response
      -> TIS v2 -> Authoritative Decision -> Trust Certificate
      -> Persistence / Hash Chain / Audit / Replay

Provider-hosted search happens inside ONE external API request, but
the workflow trace exposes retrieval and generation as distinct
logical nodes. Retrieval modes are explicit (local_only | live_web);
unknown modes are rejected; Live Web is never silently downgraded to
Live LLM. Only retrieval statuses in GOVERNABLE_STATUSES (success,
partial) are governed and certified — a partial result records
"partial" on its certificate, and every retrieval error is disclosed
in evidence and response. Web-search failures, provider errors, and
governance decisions remain three separate concepts.
"""

from __future__ import annotations

import time
import uuid
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from tcs.operating_mode import (
    ExternalCallBlockedError,
    enforce_external_call,
    execution_mode_for,
    get_mode,
)
from tcs.providers.web_evidence import (
    GOVERNABLE_STATUSES,
    bounded_summary,
    evidence_digest,
)

router = APIRouter()

WEB_PROVIDERS = ("openai", "anthropic")
RETRIEVAL_MODES = ("local_only", "live_web")


class WebQueryRequest(BaseModel):
    query: str
    provider: str
    model: str
    api_key: Optional[str] = None       # request-scoped; never persisted
    connection_name: Optional[str] = None
    profile_id: Optional[str] = None
    execution_mode: Optional[str] = None   # declared; backend authority
    retrieval_mode: Optional[str] = None   # MUST be explicit: live_web
    include_local_corpus: bool = True
    allowed_domains: Optional[List[str]] = None
    blocked_domains: Optional[List[str]] = None
    # Approximate location only (city/region/country strings). Precise
    # coordinates are rejected — never stored in Commit 5.
    user_location: Optional[Dict[str, str]] = None
    max_searches: Optional[int] = Field(default=None, ge=1, le=10)


class WebQueryResponse(BaseModel):
    query: str
    response: Optional[str]
    blocked: bool
    decision: Optional[str]              # None on retrieval failure
    certificate_id: Optional[str]
    artifact_id: Optional[str] = None
    retrieval_mode: str = "live_web"
    retrieval_status: str
    execution_mode: str
    llm_provider: str = ""
    llm_model: str = ""
    connection_name: Optional[str] = None
    error: Optional[str] = None          # provider/retrieval layer only
    tis_current: Optional[float] = None
    tis_raw: Optional[float] = None
    s_base: Optional[float] = None
    gate_result: Optional[int] = None
    blocking_reason: Optional[str] = None
    requires_human_review: bool = False
    component_scores: Optional[Dict[str, float]] = None
    component_weights: Optional[Dict[str, float]] = None
    gate_results: Optional[Dict[str, str]] = None
    thresholds: Optional[Dict[str, float]] = None
    workflow_trace: Optional[Dict[str, Any]] = None
    policy_profile_id: Optional[str] = None
    latency_ms: Dict[str, float] = {}
    web_evidence: Optional[Dict[str, Any]] = None   # normalized, bounded
    web_evidence_digest: Optional[str] = None
    local_corpus_used: bool = False


def _reject(code: str, message: str, status: int = 422) -> HTTPException:
    return HTTPException(status_code=status,
                         detail={"error": code, "message": message})


def _validate(body: WebQueryRequest, app_state: Any) -> None:
    # Retrieval mode must be explicit — never inferred from the model,
    # never ambiguous.
    if body.retrieval_mode is None:
        raise _reject("retrieval_mode_required",
                      "retrieval_mode must be explicit: live_web")
    if body.retrieval_mode not in RETRIEVAL_MODES:
        raise _reject("unknown_retrieval_mode",
                      f"Unknown retrieval mode {body.retrieval_mode!r}.")
    if body.retrieval_mode == "local_only":
        raise _reject(
            "retrieval_mode_not_supported_here",
            "local_only retrieval runs through /v2/query; /v2/query/web "
            "executes live_web only.",
        )
    name = (body.provider or "").strip().lower()
    if name not in WEB_PROVIDERS:
        raise _reject("provider_not_supported",
                      f"Live Web supports {WEB_PROVIDERS}; got "
                      f"{body.provider!r}.")
    if not (body.model or "").strip():
        raise _reject("missing_model", "Live Web requires an explicit model.")
    if not body.api_key:
        raise _reject("missing_credential",
                      "Live Web requires a request-scoped API key.")
    if body.user_location:
        bad = set(body.user_location) - {"city", "region", "country"}
        if bad:
            raise _reject(
                "invalid_location",
                "Approximate location accepts city/region/country only — "
                f"rejected fields: {sorted(bad)}.",
            )
    actual_mode = get_mode(app_state)
    if body.execution_mode and body.execution_mode != actual_mode:
        raise _reject(
            "execution_mode_mismatch",
            f"Declared execution mode {body.execution_mode!r} does not "
            f"match the server operating mode {actual_mode!r}.",
        )
    # Backend enforcement BEFORE provider construction or any network
    # execution: Live Web is external by definition.
    try:
        enforce_external_call(app_state, name)
    except ExternalCallBlockedError as e:
        raise _reject("demo_mode_enforced", str(e), status=403)


class _RecordedOutputProvider:
    """LLM-node provider replaying the final text the web adapter
    already received — the provider is never called twice. last_result
    carries the web adapter's normalized ProviderResult so the trace
    lifts truthful provenance."""

    def __init__(self, final_text: str, last_result) -> None:
        self._text = final_text
        self.last_result = last_result

    def generate(self, query: str, context: List[str]) -> str:  # noqa: ARG002
        return self._text


class _FrozenLocalStore:
    def __init__(self, chunks: List[Dict[str, Any]]) -> None:
        self._chunks = chunks

    def retrieve(self, query: str, k: int = 5):  # noqa: ARG002
        return list(self._chunks)


@router.post("/query/web")
def run_web_query(body: WebQueryRequest, request: Request) -> WebQueryResponse:
    from tcs.decision_engine import map_decision_versioned
    from tcs.governed_context import assemble_context_from_trace
    from tcs.providers.base import ProviderError
    from tcs.tis_engine import compute_tis_v2
    from tcs.trust_certificate import gate_result_of, generate_certificate_v2
    from tcs.workflow import GovernedNode, NodeType, WorkflowOrchestrator
    from tcs.workflow.connectors import LLMConnector, RAGConnector
    from tcs.workflow.connectors.web_retrieval import (
        WEB_RETRIEVAL_NODE_ID,
        WebRetrievalConnector,
    )
    from tcs.workflow.orchestrator import WorkflowStep
    from tcs.api.routes_query import (
        QueryRequest,
        _get_vector_store,
        _persist_query_artifact_and_evaluation,
    )

    app_state = request.app.state
    _validate(body, app_state)
    provider_name = body.provider.strip().lower()
    exec_mode = execution_mode_for(provider_name)  # live_provider
    t_total = time.perf_counter()
    latency: Dict[str, float] = {}

    store = app_state.store
    artifact_store = getattr(app_state, "artifact_store", None)

    # Profile / industry resolution (same rules as /v2/query).
    composer_metadata: Optional[Dict[str, Any]] = None
    industry: Optional[str] = None
    profile_id = body.profile_id
    if not profile_id:
        try:
            from tcs.packs.pack_manager import get_active_pack
            active = get_active_pack()
            if active is not None:
                profile_id = (active.get("profile_config") or {}).get("profile_id")
                if active.get("is_composed_pack"):
                    composer_metadata = dict(active.get("composer_metadata") or {})
                    industry = composer_metadata.get("industry")
                else:
                    industry = (active.get("profile_config") or {}).get("domain")
        except Exception:  # noqa: BLE001
            pass
    if not profile_id:
        profile_id = "fin-r3-a4-ct4"

    # ── Optional local corpus retrieval (one retrieval, reused). ────── #
    local_chunks: List[Dict[str, Any]] = []
    if body.include_local_corpus:
        try:
            local_chunks = _get_vector_store(industry).retrieve(
                body.query, k=5) or []
        except Exception:  # noqa: BLE001
            local_chunks = []
    context_texts = [
        c.get("content") for c in local_chunks
        if isinstance(c, dict) and c.get("content")
    ]

    # ── Provider-hosted web retrieval + generation (ONE request). ───── #
    if provider_name == "openai":
        from tcs.providers.openai_web import OpenAIWebProvider
        web_cls, extra_kwargs = OpenAIWebProvider, {}
    else:
        from tcs.providers.anthropic_web import AnthropicWebProvider
        web_cls = AnthropicWebProvider
        extra_kwargs = {"max_searches": body.max_searches}

    def _failure(status: str, error: str,
                 evidence=None) -> WebQueryResponse:
        latency["total_ms"] = round(
            (time.perf_counter() - t_total) * 1000, 1)
        return WebQueryResponse(
            query=body.query, response=None, blocked=True,
            decision=None, certificate_id=None,
            retrieval_status=status,
            execution_mode=exec_mode,
            llm_provider=provider_name, llm_model=body.model,
            connection_name=body.connection_name,
            error=error, latency_ms=latency,
            policy_profile_id=profile_id,
            web_evidence=(evidence.to_dict() if evidence else None),
            web_evidence_digest=(
                evidence_digest(evidence) if evidence else None),
            local_corpus_used=bool(context_texts),
        )

    t0 = time.perf_counter()
    try:
        web_provider = web_cls(api_key=body.api_key, model=body.model)
    except ProviderError as e:
        return _failure("provider_error", e.detail)
    try:
        final_text, evidence = web_provider.run_web_query(
            body.query, context_texts,
            allowed_domains=body.allowed_domains,
            blocked_domains=body.blocked_domains,
            user_location=body.user_location,
            **extra_kwargs,
        )
    except ProviderError as e:
        ev = getattr(web_provider, "last_evidence", None)
        status = ev.retrieval_status if ev is not None else "provider_error"
        return _failure(status, e.detail, evidence=ev)
    latency["provider_ms"] = round((time.perf_counter() - t0) * 1000, 1)

    digest = evidence_digest(evidence)
    if evidence.retrieval_status not in GOVERNABLE_STATUSES:
        # Truthful non-governable outcomes: never downgraded to Live
        # LLM, never certified. retrieval_not_performed / no_results /
        # no_citations / retrieval_error / paused.
        return _failure(
            evidence.retrieval_status,
            f"Live Web retrieval was not certifiable "
            f"(status={evidence.retrieval_status}).",
            evidence=evidence,
        )

    # ── Governed trace: distinct logical nodes. ─────────────────────── #
    web_artifact_id = f"webq-{uuid.uuid4()}"
    web_connector = WebRetrievalConnector(
        evidence=evidence, evidence_digest=digest,
        evidence_artifact_id=web_artifact_id,
    )
    llm_connector = LLMConnector(
        provider=_RecordedOutputProvider(final_text,
                                         web_provider.last_result),
        provider_name=provider_name,
        model=web_provider.display_name,
        context_key="rag",
    )
    steps: List[Any] = []
    if context_texts:
        rag_connector = RAGConnector(
            store=_FrozenLocalStore(local_chunks), retrieval_k=5)
        steps.append(WorkflowStep(
            node=GovernedNode(
                node_id="local-corpus-retrieval",
                name="Local corpus retrieval",
                node_type=NodeType.RAG,
                connection_type=rag_connector.connection_type(),
                sensitivity_tier="T2",
            ),
            connector=rag_connector, context_key="rag",
        ))
    steps.append(WorkflowStep(
        node=GovernedNode(
            node_id=WEB_RETRIEVAL_NODE_ID,
            name="Provider-hosted web retrieval",
            node_type=NodeType.API,
            connection_type=web_connector.connection_type(),
            sensitivity_tier="T2",
        ),
        connector=web_connector, context_key="web",
    ))
    steps.append(WorkflowStep(
        node=GovernedNode(
            node_id="llm-generate",
            name="LLM generation (web-grounded)",
            node_type=NodeType.LLM,
            connection_type=llm_connector.connection_type(),
            sensitivity_tier="T2",
        ),
        connector=llm_connector, context_key="llm",
    ))

    orchestrator = WorkflowOrchestrator()
    t0 = time.perf_counter()
    trace = orchestrator.execute(
        steps=steps,
        query=body.query,
        base_profile_id=profile_id,
        user_identity={"provider": provider_name,
                       "model": web_provider.display_name},
        metadata={
            "source": "routes_web.live_web_path",
            "execution_mode": exec_mode,
            "retrieval_mode": "live_web",
            "llm_provider": provider_name,
            "llm_model": web_provider.display_name,
            # The LLM node's answer is bound to this evidence digest.
            "web_evidence_digest": digest,
        },
    )
    latency["workflow_ms"] = round((time.perf_counter() - t0) * 1000, 1)

    llm_event = trace.get_node("llm-generate").event
    if llm_event and llm_event.error:
        return _failure("provider_error",
                        f"LLM provider error: {llm_event.error}")

    # ── TIS v2 -> decision -> certificate. ──────────────────────────── #
    t0 = time.perf_counter()
    tis_input, _resolved = assemble_context_from_trace(trace)
    tis_input.subject_id = web_artifact_id  # artifact/TC/evidence link
    if composer_metadata:
        tis_input.context_metadata["composer_metadata"] = dict(composer_metadata)
    tis_input.context_metadata["execution_mode"] = exec_mode
    # Bounded, hash-covered web provenance summary — never the source
    # list, never citations, never keys (see trust_certificate).
    tis_input.context_metadata["web_retrieval"] = bounded_summary(
        evidence, web_artifact_id)
    tis_result = compute_tis_v2(tis_input)
    decision, requires_review = map_decision_versioned(tis_input, tis_result)
    tc = generate_certificate_v2(tis_input, tis_result, decision,
                                 requires_review)
    issued_tc = store.issue(tc)
    latency["governance_ms"] = round((time.perf_counter() - t0) * 1000, 1)
    latency["total_ms"] = round((time.perf_counter() - t_total) * 1000, 1)

    artifact_id = None
    if artifact_store is not None:
        try:
            _persist_query_artifact_and_evaluation(
                artifact_store=artifact_store,
                body=QueryRequest(query=body.query, profile_id=profile_id,
                                  provider=provider_name,
                                  model=web_provider.display_name),
                provider_name=provider_name,
                model_name=web_provider.display_name,
                industry=industry,
                trace=trace,
                tis_input=tis_input,
                tis_result=tis_result,
                decision=decision,
                issued_tc=issued_tc,
                composer_metadata=composer_metadata,
                identity_role="live_web_endpoint",
                recipient_context_extra={
                    "retrieval_mode": "live_web",
                    "web_evidence": evidence.to_dict(),
                    "web_evidence_digest": digest,
                },
            )
            artifact_id = issued_tc.subject_id
        except Exception:  # noqa: BLE001 — persistence is best-effort
            pass

    blocked = decision in ("Hold", "Escalate", "Stop")
    return WebQueryResponse(
        query=body.query,
        response=None if blocked else final_text,
        blocked=blocked,
        decision=decision,
        certificate_id=issued_tc.certificate_id,
        artifact_id=artifact_id,
        retrieval_status=evidence.retrieval_status,
        execution_mode=exec_mode,
        llm_provider=provider_name,
        llm_model=web_provider.display_name,
        connection_name=body.connection_name,
        tis_current=issued_tc.tis_current,
        tis_raw=issued_tc.tis_raw,
        s_base=issued_tc.s_base,
        gate_result=gate_result_of(issued_tc),
        blocking_reason=issued_tc.blocking_reason,
        requires_human_review=requires_review,
        component_scores=dict(issued_tc.component_scores),
        component_weights=dict(issued_tc.component_weights),
        gate_results=dict(issued_tc.gate_results),
        thresholds=dict(issued_tc.thresholds),
        workflow_trace=trace.to_dict(),
        policy_profile_id=issued_tc.policy_set_id,
        latency_ms=latency,
        web_evidence=evidence.to_dict(),
        web_evidence_digest=digest,
        local_corpus_used=bool(context_texts),
    )
