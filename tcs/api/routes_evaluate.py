"""
tcs.api.routes_evaluate
========================

Phase 5 Slice 5.3 — evaluation-tier endpoints.

  POST /v2/evaluate                       Evaluate a stored ResponseArtifact
                                          under a (mode, policy_profile)
                                          configuration. Never re-calls
                                          the LLM.

  GET  /v2/evaluations/{evaluation_id}    Retrieve a stored evaluation.

  GET  /v2/artifacts/{artifact_id}/evaluations
                                          List every evaluation that has
                                          been performed against the
                                          given artifact, oldest first.
                                          Foundation for /v2/replay
                                          (next slice).

Hard contract reminders:

  - This module MUST NOT call any LLM. Evaluation is a pure read of
    the stored artifact + a stateless TCS scoring pass. The
    architectural guardrail test in
    tests/test_routes_evaluate.py patches every provider client to
    raise; /v2/evaluate must still succeed.

  - Caller-provided ``policy_profile_id`` defaults to the active
    pack's profile (per locked decision D3). Required for replay
    and what-if comparison.

  - what_if evaluations create a GovernanceEvaluation row but do NOT
    issue a Trust Certificate (per locked clarification). observe
    and enforce both issue TCs; observe TCs are marked
    lifecycle_state="observed" so they cannot be confused with
    enforce-mode TCs that altered delivery.

  - The full policy profile config is snapshotted onto the evaluation
    row (per locked decision D4). A future reviewer sees exactly which
    weights/thresholds/gates were active when the decision was made,
    even if the live profile registry has since been edited.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from tcs.artifacts import (
    EVALUATION_MODE_ENFORCE,
    EVALUATION_MODE_OBSERVE,
    EVALUATION_MODE_WHAT_IF,
    GovernanceEvaluation,
)
from tcs.artifacts.evaluation import evaluate_artifact
from tcs.artifacts.store import ArtifactNotFoundError, ArtifactStore


# Mounted with prefix="/v2" by app.py.
router = APIRouter()


# --------------------------------------------------------------------------- #
# Request / response models                                                    #
# --------------------------------------------------------------------------- #

class EvaluateRequest(BaseModel):
    """Input shape for POST /v2/evaluate."""

    artifact_id: str = Field(
        ...,
        description="ID of a previously-generated ResponseArtifact.",
    )
    mode: str = Field(
        ...,
        description="One of observe | enforce | what_if.",
    )
    policy_profile_id: Optional[str] = Field(
        None,
        description=(
            "Profile to evaluate against. If omitted, the active pack's "
            "profile_id is used. Caller can pass an alternate profile to "
            "support replay / what-if comparisons (D3)."
        ),
    )
    evaluator_identity: Optional[Dict[str, Any]] = Field(
        None,
        description=(
            "Who triggered the evaluation. Recorded on the evaluation row "
            "and on the TC (if one is issued). May differ from the "
            "artifact's generation_identity."
        ),
    )


class EvaluateResponse(BaseModel):
    """Slim response surface; full evaluation via GET /v2/evaluations/{id}."""

    evaluation_id: str
    artifact_id: str
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


# --------------------------------------------------------------------------- #
# Helpers                                                                       #
# --------------------------------------------------------------------------- #

#: The formal fallback profile when no caller-supplied profile and no
#: active pack are present. Architectural invariant pinned by the user:
#: policy_profile_id=null MUST NOT mean "skip governance math." Falling
#: through to a documented baseline keeps TIS reasoning consistent and
#: makes the audit trail explicit ("evaluated under baseline-no-pack")
#: rather than implicit ("no policy was applied"). Defined in
#: tcs/policy_profiles.py.
BASELINE_PROFILE_ID = "baseline-no-pack"


def _resolve_policy_profile_id(
    request: Request, caller_supplied: Optional[str],
) -> str:
    """
    Resolution order (D3, amended):
      1. Caller-supplied policy_profile_id.
      2. Active pack's profile_id.
      3. The formal baseline-no-pack profile (NOT a 400 error).

    The baseline fallback is intentional: TIS always needs a resolved
    configuration. "No active pack" is not "no governance math" —
    it's "evaluate against the documented baseline that represents
    'no standards selected.'" The audit trail then reads
    ``policy_profile_id="baseline-no-pack"`` explicitly, instead of
    a 400 surface masking what happened.
    """
    if caller_supplied:
        return caller_supplied
    try:
        from tcs.packs.pack_manager import get_active_pack
        active = get_active_pack() or {}
        pid = (active.get("profile_config") or {}).get("profile_id")
        if pid:
            return pid
    except Exception:
        pass
    return BASELINE_PROFILE_ID


def _artifact_store(request: Request) -> ArtifactStore:
    """Per-app ArtifactStore (registered at startup in app.py)."""
    store = getattr(request.app.state, "artifact_store", None)
    if store is None:
        store = ArtifactStore()
        request.app.state.artifact_store = store
    return store


def _certificate_store(request: Request) -> Any:
    """Per-app CertificateStore for TC issuance in observe/enforce."""
    return getattr(request.app.state, "store", None)


# --------------------------------------------------------------------------- #
# Endpoints                                                                    #
# --------------------------------------------------------------------------- #

@router.post("/evaluate", response_model=EvaluateResponse)
def post_evaluate(body: EvaluateRequest, request: Request) -> EvaluateResponse:
    """
    Evaluate a stored artifact. Never calls an LLM. Returns the slim
    evaluation surface; the full evaluation is at
    GET /v2/evaluations/{evaluation_id}.
    """
    mode = body.mode
    if mode not in (
        EVALUATION_MODE_OBSERVE,
        EVALUATION_MODE_ENFORCE,
        EVALUATION_MODE_WHAT_IF,
    ):
        raise HTTPException(
            status_code=400,
            detail=(
                f"unknown evaluation mode {mode!r}; "
                "expected observe | enforce | what_if"
            ),
        )

    profile_id = _resolve_policy_profile_id(request, body.policy_profile_id)
    artifact_store = _artifact_store(request)

    try:
        artifact = artifact_store.get_artifact(body.artifact_id)
    except ArtifactNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e

    # Only persist the TC for observe/enforce. what_if produces no TC
    # at all — the evaluator returns None for the TC and we skip
    # persistence.
    cert_store = (
        _certificate_store(request) if mode != EVALUATION_MODE_WHAT_IF else None
    )

    try:
        evaluation, _tc = evaluate_artifact(
            artifact=artifact,
            mode=mode,
            policy_profile_id=profile_id,
            evaluator_identity=body.evaluator_identity,
            certificate_store=cert_store,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    # Persist the evaluation row. Append-only at the storage layer.
    artifact_store.insert_evaluation(evaluation)

    return EvaluateResponse(
        evaluation_id=evaluation.evaluation_id,
        artifact_id=evaluation.artifact_id,
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
    )


@router.get("/evaluations/{evaluation_id}")
def get_evaluation(evaluation_id: str, request: Request) -> Dict[str, Any]:
    """Retrieve a stored evaluation by id. Pure read; no LLM call."""
    try:
        evaluation: GovernanceEvaluation = (
            _artifact_store(request).get_evaluation(evaluation_id)
        )
    except ArtifactNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    return evaluation.to_dict()


@router.get("/artifacts/{artifact_id}/evaluations")
def list_evaluations(artifact_id: str, request: Request) -> Dict[str, Any]:
    """
    Return every evaluation performed against the artifact, oldest
    first. Empty list when no evaluations exist (or when the
    artifact id is unknown — the ArtifactStore does not raise on
    list_evaluations_for_artifact for unknown ids; a 404 here would
    conflate "no evaluations" with "unknown artifact").
    """
    evaluations: List[GovernanceEvaluation] = (
        _artifact_store(request).list_evaluations_for_artifact(artifact_id)
    )
    return {
        "artifact_id": artifact_id,
        "count": len(evaluations),
        "evaluations": [e.to_dict() for e in evaluations],
    }
