"""
tcs.api.routes_replay
=====================

Phase 5 Slice 5.4 — POST /v2/replay.

Runs N governance evaluations against a single stored ResponseArtifact
and returns all of them in one shot. The intended use is comparison —
"show me what TCS would have done to this same captured output under
each of these N policies/modes."

Hard contracts the user pinned:

  - /v2/replay NEVER re-calls the LLM provider. It is an
    evaluation/comparison endpoint, not a generation endpoint.
    Architectural guardrail test in test_routes_replay.py patches
    every provider client to raise; replay must still succeed.

  - /v2/replay is NOT a delivery endpoint. It may compute
    enforcement_action and may issue TCs per the per-config mode
    rules (observe and enforce issue TCs; what_if does not), but
    replay itself never sends or delivers any content externally.
    There is no "response" body in the request, and the response
    payload returns evaluations only — no chat output or pipeline
    side-effects beyond persistence.

  - Every evaluation persisted by /v2/replay carries
    evaluation_origin="replay" so a future auditor can distinguish
    replay analyses from direct evaluations and from /v2/query's
    runtime enforcement.

  - Per-config policy_profile_id resolution follows the same rules
    as /v2/evaluate (caller > active pack > baseline-no-pack
    fallback). Each config can target a different profile — that's
    the whole point of replay.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from tcs.artifacts import (
    EVALUATION_MODE_ENFORCE,
    EVALUATION_MODE_OBSERVE,
    EVALUATION_MODE_WHAT_IF,
    EVALUATION_ORIGIN_REPLAY,
)
from tcs.artifacts.evaluation import evaluate_artifact
from tcs.artifacts.store import ArtifactNotFoundError, ArtifactStore
from tcs.api.routes_evaluate import (
    BASELINE_PROFILE_ID,
    _resolve_policy_profile_id,
)


# Mounted with prefix="/v2" by app.py.
router = APIRouter()


# --------------------------------------------------------------------------- #
# Request / response models                                                    #
# --------------------------------------------------------------------------- #

class ReplayConfiguration(BaseModel):
    """One (mode, policy_profile_id) pair within a replay batch."""

    mode: str = Field(
        ...,
        description="observe | enforce | what_if. Same rules as /v2/evaluate.",
    )
    policy_profile_id: Optional[str] = Field(
        None,
        description=(
            "Profile to evaluate this configuration under. If null, "
            "resolves the same way /v2/evaluate does: active pack first, "
            "then baseline-no-pack as the documented fallback. Different "
            "configurations in one batch may target different profiles — "
            "that's the comparison use case."
        ),
    )


class ReplayRequest(BaseModel):
    """Input shape for POST /v2/replay."""

    artifact_id: str = Field(
        ...,
        description="ID of a previously-generated ResponseArtifact.",
    )
    configurations: List[ReplayConfiguration] = Field(
        ...,
        description="One or more evaluation configurations to run.",
    )
    evaluator_identity: Optional[Dict[str, Any]] = Field(
        None,
        description=(
            "Identity attached to every evaluation in this batch. "
            "Typically a reviewer / analyst, not the original "
            "generation_identity from the artifact."
        ),
    )


class ReplayEvaluationSummary(BaseModel):
    """Slim per-evaluation summary returned by /v2/replay."""

    evaluation_id: str
    mode: str
    policy_profile_id: str
    decision: str
    enforcement_action: str
    delivery_intervention: bool
    trust_certificate_id: Optional[str]
    s_base: float
    tis_current: float
    component_scores: Dict[str, float]
    gate_results: Dict[str, str]
    evaluation_origin: str


class ReplayResponse(BaseModel):
    """Response payload for POST /v2/replay."""

    artifact_id: str
    count: int
    evaluations: List[ReplayEvaluationSummary]


# --------------------------------------------------------------------------- #
# Helpers                                                                       #
# --------------------------------------------------------------------------- #

def _artifact_store(request: Request) -> ArtifactStore:
    store = getattr(request.app.state, "artifact_store", None)
    if store is None:
        store = ArtifactStore()
        request.app.state.artifact_store = store
    return store


def _certificate_store(request: Request) -> Any:
    return getattr(request.app.state, "store", None)


# --------------------------------------------------------------------------- #
# Endpoint                                                                     #
# --------------------------------------------------------------------------- #

@router.post("/replay", response_model=ReplayResponse)
def post_replay(body: ReplayRequest, request: Request) -> ReplayResponse:
    """
    Run N evaluations against one stored artifact and return all
    summaries. Never re-calls the LLM; never delivers content.
    """
    if not body.configurations:
        raise HTTPException(
            status_code=400,
            detail="replay requires at least one configuration",
        )

    # Validate every config up front so a bad config in the middle
    # doesn't half-execute the batch.
    for i, cfg in enumerate(body.configurations):
        if cfg.mode not in (
            EVALUATION_MODE_OBSERVE,
            EVALUATION_MODE_ENFORCE,
            EVALUATION_MODE_WHAT_IF,
        ):
            raise HTTPException(
                status_code=400,
                detail=(
                    f"configuration[{i}] has unknown mode {cfg.mode!r}; "
                    f"expected observe | enforce | what_if"
                ),
            )

    artifact_store = _artifact_store(request)
    try:
        artifact = artifact_store.get_artifact(body.artifact_id)
    except ArtifactNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e

    summaries: List[ReplayEvaluationSummary] = []
    cert_store = _certificate_store(request)

    for cfg in body.configurations:
        profile_id = _resolve_policy_profile_id(request, cfg.policy_profile_id)
        # what_if does NOT issue a TC, so we suppress the certificate
        # store for those configurations to keep the dependency narrow.
        store_for_this = (
            cert_store if cfg.mode != EVALUATION_MODE_WHAT_IF else None
        )
        try:
            evaluation, _tc = evaluate_artifact(
                artifact=artifact,
                mode=cfg.mode,
                policy_profile_id=profile_id,
                evaluator_identity=body.evaluator_identity,
                certificate_store=store_for_this,
                origin=EVALUATION_ORIGIN_REPLAY,
            )
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e

        # Persist the evaluation. Append-only at the storage layer
        # like every other governance row.
        artifact_store.insert_evaluation(evaluation)

        summaries.append(ReplayEvaluationSummary(
            evaluation_id=evaluation.evaluation_id,
            mode=evaluation.mode,
            policy_profile_id=evaluation.policy_profile_id,
            decision=evaluation.decision,
            enforcement_action=evaluation.enforcement_action,
            delivery_intervention=evaluation.delivery_intervention,
            trust_certificate_id=evaluation.trust_certificate_id,
            s_base=evaluation.s_base,
            tis_current=evaluation.tis_current,
            component_scores=dict(evaluation.component_scores),
            gate_results=dict(evaluation.gate_results),
            evaluation_origin=evaluation.evaluation_origin,
        ))

    return ReplayResponse(
        artifact_id=body.artifact_id,
        count=len(summaries),
        evaluations=summaries,
    )


# Re-export for symmetry with other route modules that surface their
# constants. The baseline id is owned by routes_evaluate but useful for
# tests of replay's fallback behavior.
__all__ = ["router", "BASELINE_PROFILE_ID"]
