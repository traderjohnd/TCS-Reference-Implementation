"""
tcs.api.routes_govern
=====================

POST /v1/govern — the runtime governance entry point.

Accepts a RAG-shaped request (query, retrieved chunks, candidate
answer, plus optional identity / tier / extras), runs it through the
Step 3 adapter, the Step 4 request interceptor, and returns the
resulting GovernedResponse as JSON.

Request body is validated with Pydantic. The shape mirrors
:class:`tcs.adapters.rag_adapter.RAGOutput` so the API boundary and the
internal dataclass stay in sync — a field added to RAGOutput just needs
to be mirrored here.

The route never raises. Every failure mode (including fail-safe) is
converted to a :class:`GovernedResponse` by the interceptor and
returned with HTTP 200. The calling application reads
``blocked`` / ``fail_safe_applied`` to classify the outcome.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from tcs.adapters.rag_adapter import RAGAdapter, RAGChunk, RAGOutput


router = APIRouter()


# --------------------------------------------------------------------------- #
# Request / response models                                                    #
# --------------------------------------------------------------------------- #

class ChunkBody(BaseModel):
    """A single retrieved chunk as sent over the wire."""
    chunk_id: str
    similarity_score: float = Field(ge=0.0, le=1.0)
    source_doc: Optional[str] = None
    version: Optional[str] = None
    content: str = ""
    tags: List[str] = Field(default_factory=list)


class GovernRequestBody(BaseModel):
    """
    JSON body for POST /v1/govern.

    Fields mirror ``RAGOutput`` so the API surface is a thin
    translation layer. Optional ``base_profile_id`` lets the caller
    override the adapter default (``fin-r3-a4-ct4``) for demos that
    need a different CT resolution.
    """
    query: str
    retrieved_chunks: List[ChunkBody] = Field(default_factory=list)
    candidate_answer: str
    model_id: str = "rag-demo-model"
    pipeline_id: str = "finance-rag-v1"
    subject_type: str = "recommendation"
    subject_id: Optional[str] = None
    request_id: Optional[str] = None

    base_profile_id: str = "fin-r3-a4-ct4"

    # Identity passthroughs
    requesting_identity: Optional[str] = None
    identity_verified: Optional[bool] = None
    identity_confidence: Optional[float] = None
    authorization_tier: Optional[str] = None
    sensitivity_tier: Optional[str] = None
    mcp_server_id: Optional[str] = None

    # Free-form extras merged into context_metadata
    extra_metadata: Dict[str, Any] = Field(default_factory=dict)


# --------------------------------------------------------------------------- #
# Route                                                                        #
# --------------------------------------------------------------------------- #

@router.post("/govern")
def post_govern(body: GovernRequestBody, request: Request) -> Dict[str, Any]:
    """
    Run a single governed evaluation.

    Pipeline:
        1. Translate the JSON body into a :class:`RAGOutput`
        2. Adapt via :class:`RAGAdapter` to an :class:`InterceptedRequest`
        3. Hand off to the request interceptor stored on app.state
        4. Return ``GovernedResponse.to_dict()`` as JSON

    The caller inspects the returned JSON. ``blocked`` tells them
    whether the candidate output may be released;
    ``fail_safe_applied`` tells them whether the response came from
    a governance infrastructure failure.
    """
    # Translate wire chunks -> adapter chunks
    chunks = [
        RAGChunk(
            chunk_id=c.chunk_id,
            similarity_score=c.similarity_score,
            source_doc=c.source_doc,
            version=c.version,
            content=c.content,
            tags=list(c.tags),
        )
        for c in body.retrieved_chunks
    ]

    rag_output = RAGOutput(
        query=body.query,
        retrieved_chunks=chunks,
        candidate_answer=body.candidate_answer,
        model_id=body.model_id,
        pipeline_id=body.pipeline_id,
        subject_type=body.subject_type,
        subject_id=body.subject_id,
        request_id=body.request_id,
        requesting_identity=body.requesting_identity,
        identity_verified=body.identity_verified,
        identity_confidence=body.identity_confidence,
        authorization_tier=body.authorization_tier,
        sensitivity_tier=body.sensitivity_tier,
        mcp_server_id=body.mcp_server_id,
        extra_metadata=dict(body.extra_metadata),
    )

    adapter = RAGAdapter(base_profile_id=body.base_profile_id)
    intercepted = adapter.adapt(rag_output)

    interceptor = request.app.state.interceptor
    response = interceptor.govern(intercepted)
    return response.to_dict()


# --------------------------------------------------------------------------- #
# /v1/govern/decisions/stream — recent decisions feed                          #
# --------------------------------------------------------------------------- #

@router.get("/govern/decisions/stream")
def decisions_stream(
    request: Request,
    limit: int = Query(50, ge=1, le=200),
) -> Dict[str, Any]:
    """
    Return recent governance decisions for the live decisions feed.

    Polling endpoint (not SSE) — returns the most recent TCs with
    decision, scores, and timestamps.
    """
    store = request.app.state.store
    tcs = store.list_recent(limit=limit)
    decisions = []
    for tc in tcs:
        d = tc.to_dict()
        decisions.append({
            "certificate_id": d["certificate_id"],
            "subject_id": d["subject_id"],
            "decision": d["decision"],
            "tis_current": d["tis_current"],
            "component_scores": d["component_scores"],
            "gate_passed": d["gate_passed"],
            "blocking_reason": d.get("blocking_reason"),
            "requires_human_review": d["requires_human_review"],
            "evaluation_timestamp": d["evaluation_timestamp"],
            "domain": d["domain"],
            "risk_tier": d["risk_tier"],
        })
    return {"count": len(decisions), "decisions": decisions}


# --------------------------------------------------------------------------- #
# /v1/govern/hold-queue — open Hold decisions                                  #
# --------------------------------------------------------------------------- #

@router.get("/govern/hold-queue")
def hold_queue(
    request: Request,
    limit: int = Query(50, ge=1, le=200),
) -> Dict[str, Any]:
    """
    Return Hold decisions awaiting review.

    Filters recent TCs to only those with decision == "Hold".
    """
    store = request.app.state.store
    tcs = store.list_recent(limit=limit * 3)  # over-fetch to find holds
    holds = []
    for tc in tcs:
        if tc.decision == "Hold":
            d = tc.to_dict()
            holds.append({
                "certificate_id": d["certificate_id"],
                "subject_id": d["subject_id"],
                "tis_current": d["tis_current"],
                "component_scores": d["component_scores"],
                "blocking_reason": d.get("blocking_reason"),
                "evaluation_timestamp": d["evaluation_timestamp"],
                "domain": d["domain"],
                "override_status": "pending",
            })
            if len(holds) >= limit:
                break
    return {"count": len(holds), "holds": holds}


# --------------------------------------------------------------------------- #
# /v1/govern/hold-queue/{tc_id}/override — submit override                     #
# --------------------------------------------------------------------------- #

class OverrideBody(BaseModel):
    """Override request for a Hold decision."""
    override_decision: str = Field(
        ..., description="New decision: Allow or Escalate"
    )
    justification: str = Field(
        ..., min_length=10, description="Reason for override"
    )
    override_by: str = Field(
        ..., description="User ID submitting override"
    )


@router.post("/govern/hold-queue/{tc_id}/override")
def submit_override(
    tc_id: str,
    body: OverrideBody,
    request: Request,
) -> Dict[str, Any]:
    """
    Submit an override for a Hold decision.

    Creates an override record linked to the TC. Does not modify
    the original TC (append-only archive).
    """
    from tcs.persistence import CertificateNotFoundError

    store = request.app.state.store
    try:
        tc = store.get(tc_id)
    except CertificateNotFoundError:
        raise HTTPException(status_code=404, detail=f"TC {tc_id!r} not found")

    if tc.decision != "Hold":
        raise HTTPException(
            status_code=400,
            detail=f"TC {tc_id!r} decision is {tc.decision!r}, not Hold",
        )

    if body.override_decision not in ("Allow", "Escalate"):
        raise HTTPException(
            status_code=400,
            detail="override_decision must be 'Allow' or 'Escalate'",
        )

    override_record = {
        "certificate_id": tc_id,
        "original_decision": tc.decision,
        "override_decision": body.override_decision,
        "justification": body.justification,
        "override_by": body.override_by,
        "override_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "status": "applied",
    }
    return override_record
