"""
tcs.api.routes_compare
======================

POST /v2/query/compare — comparative multi-model demonstration
(demo-live branch, Commit 4).

One identical governed request runs against two to four explicitly
selected provider/model targets. The governed input is FROZEN once
before provider fan-out — same question, same system governance
instruction, same retrieved local-corpus context in the same order,
same policy profile — so output differences are attributable to the
models, never to retrieval variance. Each successful model output then
independently traverses the standard path:

    ProviderResult -> LLM connector evidence -> TIS v2 ->
    authoritative decision -> generate_certificate_v2 ->
    persistence -> hash chain -> audit -> replay

There is NO aggregate comparison score: every member gets its own
component scores, gates, decision, explanation, Trust Certificate,
ResponseArtifact, and GovernanceEvaluation. Members are linked only
by bounded correlation metadata (comparison_id, member ids, common
prompt-package / context-snapshot hashes). That correlation metadata
is carried on the comparison response and on each member's
ResponseArtifact (recipient_context) — it is deliberately kept OUT of
Trust Certificate content so certificate hashing is untouched.

Commit 4 performs NO web retrieval: context comes from the frozen
local corpus only. Provider re-execution is not part of ordinary
replay — /v2/replay on a member's artifact reuses the stored output.

API credentials are request-scoped per member, never persisted, never
serialized into results, correlation metadata, hashes, artifacts,
evaluations, certificates, traces, logs, or errors (provider errors
are sanitized by the neutral provider layer).
"""

from __future__ import annotations

import hashlib
import json
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

router = APIRouter()

KNOWN_PROVIDERS = ("mock", "openai", "anthropic")
MIN_TARGETS = 2
MAX_TARGETS = 4
DEFAULT_MEMBER_TIMEOUT_S = 45.0
MIN_MEMBER_TIMEOUT_S = 5.0
MAX_MEMBER_TIMEOUT_S = 120.0
RETRIEVAL_K = 5
RETRIEVAL_ORDERING = "similarity_desc"


# --------------------------------------------------------------------------- #
# Schemas                                                                      #
# --------------------------------------------------------------------------- #

class ComparisonTarget(BaseModel):
    provider: str                       # mock | openai | anthropic
    model: str                          # model id — passed through verbatim
    api_key: Optional[str] = None       # request-scoped; never persisted
    connection_name: Optional[str] = None
    label: Optional[str] = None         # neutral operator label; display only


class ComparisonRequest(BaseModel):
    query: str
    profile_id: Optional[str] = None
    targets: List[ComparisonTarget] = Field(default_factory=list)
    # Operator's declared execution mode; when present it must match
    # the server's actual mode — the backend is always the authority.
    execution_mode: Optional[str] = None
    timeout_seconds: Optional[float] = None


class ComparisonMember(BaseModel):
    comparison_member_id: str
    ordinal: int
    provider: str
    model: str
    connection_name: Optional[str] = None
    label: Optional[str] = None
    execution_mode: str                 # truthful per member
    # ok            — usable model output, independently governed
    # provider_error — provider-layer failure; no decision, no TC
    # empty_output  — provider returned no usable model-generated text
    #                 (a system diagnostic is NOT model output); no
    #                 decision, no TC, safe provenance preserved
    # timeout       — bounded timeout hit; no decision, no TC
    status: str
    response: Optional[str] = None
    blocked: bool = False
    latency_ms: Optional[float] = None
    usage: Optional[Dict[str, Any]] = None
    provider_request_id: Optional[str] = None
    error: Optional[str] = None         # provider error — NOT a decision
    # Governance results — populated only for status == "ok".
    decision: Optional[str] = None
    tis_current: Optional[float] = None
    s_base: Optional[float] = None
    gate_result: Optional[int] = None
    component_scores: Optional[Dict[str, float]] = None
    component_weights: Optional[Dict[str, float]] = None
    gate_results: Optional[Dict[str, str]] = None
    thresholds: Optional[Dict[str, float]] = None
    blocking_reason: Optional[str] = None
    requires_human_review: bool = False
    explanation: Optional[str] = None
    certificate_id: Optional[str] = None
    artifact_id: Optional[str] = None


class ComparisonResponse(BaseModel):
    comparison_id: str
    execution_mode: str                 # server operating mode at run time
    question: str
    prompt_package_hash: str
    context_snapshot_id: str
    context_snapshot_hash: str
    policy_profile_id: str
    policy_profile_version: str
    retrieval_config: Dict[str, Any]
    executed_at: str
    target_count: int
    members: List[ComparisonMember]
    note: str = (
        "Same prompt, same retrieved context, same policy. "
        "Local corpus context only — no web retrieval."
    )


# --------------------------------------------------------------------------- #
# Input freezing helpers                                                       #
# --------------------------------------------------------------------------- #

class _FrozenStore:
    """Replays one pre-executed retrieval to every comparison member.

    All members receive byte-identical chunks in identical order; the
    real vector store is queried exactly once, before fan-out.
    """

    def __init__(self, chunks: List[Dict[str, Any]]) -> None:
        self._chunks = chunks

    def retrieve(self, query: str, k: int = RETRIEVAL_K):  # noqa: ARG002
        return list(self._chunks)


def _canonical_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True, default=str)


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _context_snapshot(chunks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    snapshot = []
    for c in chunks:
        if not isinstance(c, dict):
            continue
        snapshot.append({
            "chunk_id": c.get("chunk_id"),
            "source_doc": c.get("source_doc"),
            "version": c.get("version"),
            "similarity_score": str(c.get("similarity_score")),
            "content": c.get("content"),
        })
    return snapshot


def _reject(code: str, message: str, status: int = 422) -> HTTPException:
    return HTTPException(status_code=status,
                         detail={"error": code, "message": message})


def _validate_targets(body: ComparisonRequest, app_state: Any) -> None:
    n = len(body.targets)
    if n < MIN_TARGETS:
        raise _reject("too_few_targets",
                      f"A comparison needs at least {MIN_TARGETS} targets.")
    if n > MAX_TARGETS:
        raise _reject("too_many_targets",
                      f"A comparison accepts at most {MAX_TARGETS} targets.")

    seen = set()
    for t in body.targets:
        name = (t.provider or "").strip().lower()
        if name not in KNOWN_PROVIDERS:
            raise _reject("unknown_provider",
                          f"Unknown provider: {t.provider!r}")
        if not (t.model or "").strip():
            raise _reject("missing_model",
                          f"Target for provider {name!r} has no model id.")
        if name != "mock" and not t.api_key:
            raise _reject(
                "missing_credential",
                f"Live provider {name!r} requires a request-scoped API key.",
            )
        # Duplicates are rejected unless the operator deliberately
        # assigned a distinct configuration (connection name or label).
        key = (name, t.model.strip(),
               (t.connection_name or "").strip(), (t.label or "").strip())
        if key in seen:
            raise _reject(
                "duplicate_target",
                f"Duplicate target {name}/{t.model} — assign a distinct "
                "connection name or label to compare the same model twice.",
            )
        seen.add(key)

    # Declared execution mode must match the backend's actual mode.
    actual_mode = get_mode(app_state)
    if body.execution_mode and body.execution_mode != actual_mode:
        raise _reject(
            "execution_mode_mismatch",
            f"Declared execution mode {body.execution_mode!r} does not "
            f"match the server operating mode {actual_mode!r}.",
        )

    # Backend demo-mode enforcement per target (never frontend-only).
    for t in body.targets:
        try:
            enforce_external_call(app_state, t.provider)
        except ExternalCallBlockedError as e:
            raise _reject("demo_mode_enforced", str(e), status=403)


def _member_timeout(body: ComparisonRequest) -> float:
    if body.timeout_seconds is None:
        return DEFAULT_MEMBER_TIMEOUT_S
    return max(MIN_MEMBER_TIMEOUT_S,
               min(MAX_MEMBER_TIMEOUT_S, float(body.timeout_seconds)))


# --------------------------------------------------------------------------- #
# POST /v2/query/compare                                                       #
# --------------------------------------------------------------------------- #

@router.post("/query/compare")
def run_comparison(body: ComparisonRequest, request: Request) -> ComparisonResponse:
    """Execute one identical governed request against 2-4 explicitly
    selected provider/model targets and govern each output
    independently. See the module docstring for the contract."""
    from concurrent.futures import ThreadPoolExecutor
    from concurrent.futures import TimeoutError as FutureTimeoutError
    from datetime import datetime, timezone

    from tcs.decision_engine import map_decision_versioned
    from tcs.governed_context import assemble_context_from_trace
    from tcs.providers import build_provider
    from tcs.providers.base import build_messages
    from tcs.tis_engine import compute_tis_v2
    from tcs.trust_certificate import gate_result_of, generate_certificate_v2
    from tcs.workflow import GovernedNode, NodeType, WorkflowOrchestrator
    from tcs.workflow.connectors import LLMConnector, RAGConnector
    from tcs.workflow.orchestrator import WorkflowStep
    from tcs.api.routes_query import (
        _get_vector_store,
        _persist_query_artifact_and_evaluation,
        QueryRequest,
    )

    app_state = request.app.state
    _validate_targets(body, app_state)

    store = app_state.store
    artifact_store = getattr(app_state, "artifact_store", None)

    # ── Resolve profile / industry from the active pack (same rules
    #    as /v2/query: explicit value wins). ─────────────────────────── #
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

    # ── Freeze the common governed input ONCE before fan-out. ────────── #
    comparison_id = f"cmp-{uuid.uuid4()}"
    executed_at = datetime.now(timezone.utc).isoformat()
    vector_store = _get_vector_store(industry)
    try:
        frozen_chunks = vector_store.retrieve(body.query, k=RETRIEVAL_K) or []
    except Exception:  # noqa: BLE001 — an empty corpus is a valid snapshot
        frozen_chunks = []

    snapshot = _context_snapshot(frozen_chunks)
    context_snapshot_hash = _sha256(_canonical_json(snapshot))
    context_snapshot_id = f"ctx-{context_snapshot_hash[:16]}"

    # The normalized common prompt package: the same neutral system
    # governance instruction + user message every provider receives
    # (provider-specific wire formatting happens inside each adapter
    # and is recorded separately via the adapter's provenance).
    context_texts = [c["content"] for c in snapshot if c.get("content")]
    system_msg, user_msg = build_messages(body.query, context_texts)
    prompt_package = {"system": system_msg, "user": user_msg,
                      "question": body.query}
    prompt_package_hash = _sha256(_canonical_json(prompt_package))

    frozen_store = _FrozenStore(frozen_chunks)
    member_timeout = _member_timeout(body)

    # ── Per-member provider construction (fast, sequential). ─────────── #
    n = len(body.targets)
    prepared: List[Dict[str, Any]] = []
    for i, t in enumerate(body.targets):
        member: Dict[str, Any] = {
            "ordinal": i,
            "member_id": f"{comparison_id}-m{i}",
            "target": t,
            "provider_obj": None,
            "model_name": t.model,
            "error": None,
        }
        try:
            provider_obj, model_name = build_provider(
                t.provider, t.api_key, t.model,
            )
            member["provider_obj"] = provider_obj
            member["model_name"] = model_name
        except ValueError as e:
            member["error"] = str(e)
        prepared.append(member)

    # ── Bounded-concurrency fan-out: only the provider-bound workflow
    #    runs in threads. Governance, certificate issuance, and
    #    persistence happen sequentially afterwards so the TC hash
    #    chain sequence stays strictly ordered. ─────────────────────── #
    def _run_member_workflow(member: Dict[str, Any]):
        t = member["target"]
        rag_connector = RAGConnector(store=frozen_store, retrieval_k=RETRIEVAL_K)
        llm_connector = LLMConnector(
            provider=member["provider_obj"],
            provider_name=t.provider,
            model=member["model_name"],
            context_key="rag",
        )
        rag_node = GovernedNode(
            node_id="rag-retrieve", name="Frozen comparison context",
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
        orchestrator = WorkflowOrchestrator()
        return orchestrator.execute(
            steps=[
                WorkflowStep(node=rag_node, connector=rag_connector,
                             context_key="rag"),
                WorkflowStep(node=llm_node, connector=llm_connector,
                             context_key="llm"),
            ],
            query=body.query,
            base_profile_id=profile_id,
            user_identity={"provider": t.provider,
                           "model": member["model_name"]},
            metadata={
                "source": "routes_compare.comparison_path",
                "execution_mode": execution_mode_for(t.provider),
                "llm_provider": t.provider,
                "llm_model": member["model_name"],
                "comparison_id": comparison_id,
                "comparison_member_id": member["member_id"],
            },
        )

    runnable = [m for m in prepared if m["provider_obj"] is not None]
    with ThreadPoolExecutor(max_workers=min(MAX_TARGETS, max(1, len(runnable)))) as pool:
        t_start = time.perf_counter()
        futures = {m["member_id"]: pool.submit(_run_member_workflow, m)
                   for m in runnable}
        for m in runnable:
            fut = futures[m["member_id"]]
            remaining = max(0.1, member_timeout -
                            (time.perf_counter() - t_start))
            try:
                m["trace"] = fut.result(timeout=remaining)
            except FutureTimeoutError:
                m["timeout"] = True
                fut.cancel()  # best-effort; a running call is abandoned
            except Exception as e:  # noqa: BLE001 — isolate member failure
                m["error"] = str(e)

    # ── Sequential governance + certification per successful member. ── #
    members_out: List[ComparisonMember] = []
    for m in prepared:
        t = m["target"]
        member_exec_mode = execution_mode_for(t.provider)
        base = dict(
            comparison_member_id=m["member_id"],
            ordinal=m["ordinal"],
            provider=t.provider,
            model=m["model_name"],
            connection_name=t.connection_name,
            label=t.label,
            execution_mode=member_exec_mode,
        )

        if m.get("timeout"):
            members_out.append(ComparisonMember(
                **base, status="timeout",
                error=f"Provider call exceeded the {member_timeout:.0f}s "
                      "comparison timeout.",
            ))
            continue

        trace = m.get("trace")
        if m.get("error") is not None or trace is None:
            members_out.append(ComparisonMember(
                **base, status="provider_error",
                error=m.get("error") or "provider construction failed",
            ))
            continue

        llm_event = trace.get_node("llm-generate").event
        if llm_event and llm_event.error:
            # Provider failure: no model output was produced, so no
            # Trust Certificate is issued — a provider error is never
            # a governance decision. When the provider technically
            # answered but returned no usable text, classify it as
            # empty_output and preserve the provider's safe telemetry
            # (request id, usage) from the error-shaped last_result;
            # the diagnostic text is a SYSTEM message, never scored.
            last = getattr(m["provider_obj"], "last_result", None)
            summary = {}
            if last is not None and hasattr(last, "provenance_summary"):
                summary = last.provenance_summary()
            is_empty = summary.get("error_category") == "empty_content"
            members_out.append(ComparisonMember(
                **base,
                status="empty_output" if is_empty else "provider_error",
                error=f"LLM provider error: {llm_event.error}",
                latency_ms=llm_event.latency_ms,
                usage=summary.get("usage") or None,
                provider_request_id=summary.get("request_id"),
            ))
            continue

        # Normalized provider telemetry from the neutral contract.
        provider_prov = {}
        llm_node_obj = trace.get_node("llm-generate")
        raw_meta = getattr(llm_node_obj, "raw_metadata", None) or {}
        if isinstance(raw_meta, dict):
            provider_prov = raw_meta.get("provider_provenance") or {}
        if not provider_prov:
            last = getattr(m["provider_obj"], "last_result", None)
            if last is not None:
                provider_prov = last.provenance_summary()

        tis_input, _resolved = assemble_context_from_trace(trace)
        if composer_metadata:
            tis_input.context_metadata["composer_metadata"] = dict(composer_metadata)
        tis_input.context_metadata["execution_mode"] = member_exec_mode
        tis_result = compute_tis_v2(tis_input)
        decision, requires_review = map_decision_versioned(tis_input, tis_result)
        tc = generate_certificate_v2(tis_input, tis_result, decision,
                                     requires_review)
        issued_tc = store.issue(tc)

        artifact_id = None
        if artifact_store is not None:
            try:
                _persist_query_artifact_and_evaluation(
                    artifact_store=artifact_store,
                    body=QueryRequest(
                        query=body.query, profile_id=profile_id,
                        provider=t.provider, model=m["model_name"],
                    ),
                    provider_name=t.provider,
                    model_name=m["model_name"],
                    industry=industry,
                    trace=trace,
                    tis_input=tis_input,
                    tis_result=tis_result,
                    decision=decision,
                    issued_tc=issued_tc,
                    composer_metadata=composer_metadata,
                    identity_role="comparison_endpoint",
                    recipient_context_extra={
                        # Bounded correlation metadata — never a key.
                        "comparison_id": comparison_id,
                        "comparison_member_id": m["member_id"],
                        "member_ordinal": m["ordinal"],
                        "provider": t.provider,
                        "model": m["model_name"],
                        "prompt_package_hash": prompt_package_hash,
                        "context_snapshot_hash": context_snapshot_hash,
                    },
                )
                artifact_id = issued_tc.subject_id
            except Exception:  # noqa: BLE001 — persistence is best-effort
                pass

        blocked = decision in ("Hold", "Escalate", "Stop")
        members_out.append(ComparisonMember(
            **base,
            status="ok",
            response=None if blocked else trace.final_output,
            blocked=blocked,
            latency_ms=llm_event.latency_ms if llm_event else None,
            usage=provider_prov.get("usage") or None,
            provider_request_id=provider_prov.get("request_id"),
            decision=decision,
            tis_current=issued_tc.tis_current,
            s_base=issued_tc.s_base,
            gate_result=gate_result_of(issued_tc),
            component_scores=dict(issued_tc.component_scores),
            component_weights=dict(issued_tc.component_weights),
            gate_results=dict(issued_tc.gate_results),
            thresholds=dict(issued_tc.thresholds),
            blocking_reason=issued_tc.blocking_reason,
            requires_human_review=requires_review,
            explanation=getattr(issued_tc, "explanation_summary", None),
            certificate_id=issued_tc.certificate_id,
            artifact_id=artifact_id,
        ))

    profile_version = "tis-v2"
    return ComparisonResponse(
        comparison_id=comparison_id,
        execution_mode=get_mode(app_state),
        question=body.query,
        prompt_package_hash=prompt_package_hash,
        context_snapshot_id=context_snapshot_id,
        context_snapshot_hash=context_snapshot_hash,
        policy_profile_id=profile_id,
        policy_profile_version=profile_version,
        retrieval_config={
            "k": RETRIEVAL_K,
            "ordering": RETRIEVAL_ORDERING,
            "source": "local_corpus",
            "web_retrieval": False,
            "retrievals_executed": 1,
        },
        executed_at=executed_at,
        target_count=n,
        members=members_out,
    )
