"""
tcs.api.routes_generate
========================

Phase 5 Slice 5.2 — generation-tier endpoints.

  POST /v2/generate           Create a ResponseArtifact via one of the
                              four generation modes. NO governance
                              evaluation performed here. NO Trust
                              Certificate issued. Returns the artifact
                              metadata so the caller can hand the
                              artifact_id to /v2/evaluate (Slice 5.3).

  GET  /v2/artifacts/{id}    Retrieve a stored artifact exactly as
                              persisted. No re-generation; the
                              ArtifactStore is the source of truth.

Hard contract reminders:

  - API key is in-memory only. The request shape carries an
    ``api_key`` field which is passed straight into the provider
    client constructor for this one call and goes out of scope when
    the request returns. It is never persisted to the artifact, the
    DB, or the logs.
  - human_composed MUST NOT call any LLM. The dispatcher in
    tcs.artifacts.generation routes around the provider clients for
    this mode entirely; a test in tests/test_routes_generate.py
    asserts that the human_composed path completes when the LLM
    clients are mocked to raise on call.
  - raw_llm is truly raw. ``system_prompt_used`` is exactly what was
    sent (None when nothing was sent). ``rag_enabled=False`` and
    ``retrieved_sources=[]`` always.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from tcs.artifacts import (
    EVALUATION_MODE_OBSERVE,  # noqa: F401 — kept for API parity in later slices
    GENERATION_MODE_AGENT_WORKFLOW,
    GENERATION_MODE_HUMAN_COMPOSED,
    GENERATION_MODE_RAG_LLM,
    GENERATION_MODE_RAW_LLM,
    ResponseArtifact,
)
from tcs.artifacts.generation import _GenerationError, generate
from tcs.artifacts.store import ArtifactNotFoundError, ArtifactStore


# Mounted with prefix="/v2" by app.py — matches the other route modules.
router = APIRouter()


# --------------------------------------------------------------------------- #
# Request / response models                                                    #
# --------------------------------------------------------------------------- #

class GenerateRequest(BaseModel):
    """Input shape for POST /v2/generate."""

    generation_mode: str = Field(
        ...,
        description=(
            "One of raw_llm | rag_llm | agent_workflow | human_composed. "
            "raw_llm is truly raw (no RAG, no domain system prompt unless "
            "explicitly supplied via system_prompt_override). "
            "human_composed accepts a draft and does not call any LLM."
        ),
    )

    # Common to LLM modes
    prompt: Optional[str] = Field(
        None,
        description=(
            "User query / instruction. Required for raw_llm, rag_llm, "
            "and agent_workflow. May be None for human_composed (the "
            "draft alone is sufficient there)."
        ),
    )

    # Provider auth — in-memory only
    provider: Optional[str] = Field(
        "mock",
        description="openai | anthropic | mock. Ignored for human_composed.",
    )
    model: Optional[str] = Field(
        "deterministic",
        description="Provider's model identifier. Ignored for human_composed.",
    )
    api_key: Optional[str] = Field(
        None,
        description=(
            "API key for openai/anthropic. Used in-memory for this one "
            "call and never persisted to the artifact, the DB, or the "
            "logs. Required when provider is openai or anthropic."
        ),
    )

    # RAG-only
    industry_hint: Optional[str] = Field(
        None,
        description=(
            "Industry key used to derive the grounding system prompt and "
            "select the corpus for rag_llm / agent_workflow. If omitted, "
            "the active pack's industry is used. Replaces the leftover "
            "'financial advisory' hardcode that previously bled into "
            "all RAG contexts."
        ),
    )
    retrieval_k: int = Field(
        5,
        description="Number of chunks to retrieve for rag_llm / agent_workflow.",
    )

    # raw_llm only
    system_prompt_override: Optional[str] = Field(
        None,
        description=(
            "Caller-supplied system prompt for raw_llm. If omitted, no "
            "system message is sent to the model and "
            "system_prompt_used is recorded as None. This is the "
            "transparency invariant: raw_llm surfaces exactly what "
            "framing (if any) the model saw."
        ),
    )

    # human_composed
    draft: Optional[str] = Field(
        None,
        description="Outbound message text (required for human_composed).",
    )
    recipient_context: Optional[Dict[str, Any]] = Field(
        None,
        description=(
            "Typed facts about the recipient/situation. Strongly "
            "recommended for human_composed so future "
            "deterministic-bounded evaluation can use them."
        ),
    )

    # Identity binding (audit)
    generation_identity: Optional[Dict[str, Any]] = Field(
        None,
        description=(
            "Who triggered the generation: requesting_identity, "
            "identity_type, role, session_id. Recorded on the artifact."
        ),
    )


class GenerateResponse(BaseModel):
    """
    Slim response surface for /v2/generate. The full artifact dict is
    available via GET /v2/artifacts/{artifact_id}.

    We deliberately do NOT echo the raw_output's full body in this
    response by default — for very large outputs the artifact_id +
    a fetch round-trip is cleaner. The ``raw_output`` field IS included
    here for ergonomic chat-style consumers in Slice 5.2; future slices
    may move it behind a flag.
    """

    artifact_id: str
    generation_mode: str
    raw_output: Optional[str]
    raw_output_hash: Optional[str]
    provider: Optional[str]
    model: Optional[str]
    system_prompt_used: Optional[str]
    rag_enabled: bool
    retrieved_sources: List[Dict[str, Any]]
    workflow_trace_id: Optional[str]
    generation_error: Optional[str]


# --------------------------------------------------------------------------- #
# Helpers                                                                       #
# --------------------------------------------------------------------------- #

def _resolve_industry_hint(
    request: Request, explicit: Optional[str],
) -> Optional[str]:
    """
    Industry resolution order:
      1. Explicit ``industry_hint`` on the request — caller override.
      2. Active pack's industry (composed packs) or domain (built-in
         packs), via tcs.packs.pack_manager.get_active_pack.
      3. None — _derive_rag_system_prompt then falls back to its
         neutral generic prompt.
    """
    if explicit:
        return explicit
    try:
        from tcs.packs.pack_manager import get_active_pack
        active = get_active_pack()
    except Exception:
        return None
    if not active:
        return None
    if active.get("is_composed_pack"):
        cm = active.get("composer_metadata") or {}
        return cm.get("industry")
    return (active.get("profile_config") or {}).get("domain")


def _artifact_store(request: Request) -> ArtifactStore:
    """
    Fetch the per-app ArtifactStore. Registered in app.py at startup;
    falls back to constructing one against the default DB if not
    present (test code paths can run without app state).
    """
    store = getattr(request.app.state, "artifact_store", None)
    if store is None:
        store = ArtifactStore()
        request.app.state.artifact_store = store
    return store


# --------------------------------------------------------------------------- #
# Endpoints                                                                    #
# --------------------------------------------------------------------------- #

@router.post("/generate", response_model=GenerateResponse)
def post_generate(body: GenerateRequest, request: Request) -> GenerateResponse:
    """
    Create a ResponseArtifact. No governance is evaluated here.
    """
    # Per-mode input validation up front. Pydantic only enforces types;
    # the cross-field rules (e.g. human_composed requires draft) live
    # here so the error message is clear.
    if body.generation_mode == GENERATION_MODE_HUMAN_COMPOSED:
        if not body.draft or not body.draft.strip():
            raise HTTPException(
                status_code=400,
                detail="human_composed requires a non-empty `draft`",
            )
    elif body.generation_mode in (
        GENERATION_MODE_RAW_LLM,
        GENERATION_MODE_RAG_LLM,
        GENERATION_MODE_AGENT_WORKFLOW,
    ):
        if not body.prompt or not body.prompt.strip():
            raise HTTPException(
                status_code=400,
                detail=f"{body.generation_mode} requires a non-empty `prompt`",
            )
    else:
        raise HTTPException(
            status_code=400,
            detail=(
                f"unknown generation_mode {body.generation_mode!r}; "
                "expected raw_llm | rag_llm | agent_workflow | human_composed"
            ),
        )

    industry = _resolve_industry_hint(request, body.industry_hint)

    try:
        artifact = generate(
            generation_mode=body.generation_mode,
            prompt=body.prompt,
            draft=body.draft,
            provider=body.provider,
            model=body.model,
            api_key=body.api_key,
            industry_hint=industry,
            retrieval_k=body.retrieval_k,
            system_prompt_override=body.system_prompt_override,
            recipient_context=body.recipient_context,
            generation_identity=body.generation_identity,
        )
    except _GenerationError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    # Persist. The artifact is append-only at the storage layer.
    _artifact_store(request).insert_artifact(artifact)

    return GenerateResponse(
        artifact_id=artifact.artifact_id,
        generation_mode=artifact.generation_mode,
        raw_output=artifact.raw_output,
        raw_output_hash=artifact.raw_output_hash,
        provider=artifact.provider,
        model=artifact.model,
        system_prompt_used=artifact.system_prompt_used,
        rag_enabled=artifact.rag_enabled,
        retrieved_sources=list(artifact.retrieved_sources),
        workflow_trace_id=artifact.workflow_trace_id,
        generation_error=artifact.generation_error,
    )


@router.get("/artifacts")
def list_artifacts(
    request: Request,
    limit: int = 50,
) -> Dict[str, Any]:
    """
    List recent artifacts (most recent first).

    Returns slim summaries so the UI artifact picker stays fast
    even with large stores. Each entry carries enough metadata
    to render a row (generation_mode + prompt/draft preview +
    timestamp) and the artifact_id for drill-down.

    Phase 5 Slice 5.6 addition — the frontend Governance Replay
    view uses this to populate its artifact picker.
    """
    store = _artifact_store(request)
    if limit <= 0 or limit > 500:
        limit = 50
    artifacts = store.list_artifacts(limit=limit)

    def _iso(dt) -> str:
        return dt.strftime("%Y-%m-%dT%H:%M:%SZ")

    def _preview(a) -> str:
        # For LLM modes the prompt is the meaningful preview text.
        # For human_composed the draft (raw_output) is the message
        # being sent — that's what reviewers need to see.
        if a.generation_mode == "human_composed":
            return (a.raw_output or "")[:160]
        return (a.prompt or a.raw_output or "")[:160]

    return {
        "count": len(artifacts),
        "artifacts": [
            {
                "artifact_id": a.artifact_id,
                "created_at": _iso(a.created_at),
                "generation_mode": a.generation_mode,
                "provider": a.provider,
                "model": a.model,
                "preview": _preview(a),
                "has_workflow_trace": a.workflow_trace is not None,
                "has_retrieved_sources": bool(a.retrieved_sources),
                "generation_error": a.generation_error,
            }
            for a in artifacts
        ],
    }


@router.get("/artifacts/{artifact_id}")
def get_artifact(artifact_id: str, request: Request) -> Dict[str, Any]:
    """
    Retrieve a stored artifact exactly as persisted.

    Returns the full artifact dict (every field on ResponseArtifact).
    Does NOT re-generate anything; the ArtifactStore is the source of
    truth and an artifact, once written, is immutable.
    """
    try:
        artifact: ResponseArtifact = _artifact_store(request).get_artifact(
            artifact_id
        )
    except ArtifactNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    return artifact.to_dict()
