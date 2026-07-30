"""
tcs.api.routes_govern
=====================

POST /v2/govern — the runtime governance entry point.

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

import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field


# Override reason format is "{decision}: {justification} (by {actor})".
# The actor marker is anchored to the END of the string so that a
# justification that itself contains "(by " (e.g. a quoted user note)
# cannot be mistaken for the actor segment. Decision is everything up
# to the first ": "; justification is everything between that and the
# final actor marker; actor is the contents of the trailing "(by ...)".
_OVERRIDE_REASON_RE = re.compile(
    r"^(?P<decision>[^:]+):\s*(?P<justification>.*?)\s*\(by\s+(?P<actor>[^)]+)\)\s*$",
    re.DOTALL,
)

from pydantic import field_validator

from tcs.adapters.rag_adapter import RAGAdapter, RAGChunk, RAGOutput
from tcs.api.models_numeric import (
    GOVERNED_DECIMAL_PATTERN,
    GovernedDecimalError,
    parse_unit_interval_decimal,
)
from tcs.governed_context import CT_WEIGHT_MODIFIERS
from tcs.governed_metadata import find_protected_keys
from tcs.trust_certificate import gate_result_from_serialized


router = APIRouter()


# Allowed values for the typed evaluation-typing fields (tis-v2 Commit
# 5a.1). These are the SDK convenience parameters that historically
# travelled inside extra_metadata; that channel is protected, so the
# only public path is these validated typed fields. The route — never
# the caller — places the validated values into the trusted governed
# metadata channel.
_VALID_RISK_TIERS = frozenset({"r1", "r2", "r3"})
_VALID_ACTION_CLASSES = frozenset({"a1", "a2", "a3", "a4"})
#: CT-1..CT-13 from the canonical modifier table. CT-12 is deliberately
#: allowed: declaring credentials produces the deterministic CT-12
#: credential Stop — a conservative outcome, never an override surface.
_VALID_CONNECTION_TYPES = frozenset(CT_WEIGHT_MODIFIERS.keys())


# --------------------------------------------------------------------------- #
# Request / response models                                                    #
# --------------------------------------------------------------------------- #
#
# Governed decimal fields are JSON STRINGS on the wire (tis-v2 Commit
# 5a, owner correction 1): a JSON number token may pass through binary
# floating point before validation sees it, so lexical decimal fidelity
# requires a string. OpenAPI documents these as strings with the
# governed-decimal pattern — never ``number | string``.

class ChunkBody(BaseModel):
    """A single retrieved chunk as sent over the wire."""
    chunk_id: str
    similarity_score: str = Field(
        pattern=GOVERNED_DECIMAL_PATTERN,
        description=(
            "Governed decimal string in [0, 1], variable scale "
            "(e.g. \"0.923456\"). JSON numbers are rejected to "
            "preserve lexical decimal fidelity."
        ),
        json_schema_extra={"example": "0.9234"},
    )
    source_doc: Optional[str] = None
    version: Optional[str] = None
    content: str = ""
    tags: List[str] = Field(default_factory=list)

    @field_validator("similarity_score", mode="before")
    @classmethod
    def _validate_similarity(cls, v: object) -> str:
        try:
            parse_unit_interval_decimal(v, "similarity_score")
        except GovernedDecimalError as exc:
            raise ValueError(str(exc)) from exc
        return v  # keep the exact lexical string; Decimal at the adapter


class GovernRequestBody(BaseModel):
    """
    JSON body for POST /v2/govern.

    Fields mirror ``RAGOutput`` so the API surface is a thin
    translation layer. Optional ``base_profile_id`` lets the caller
    override the adapter default (``fin-r3-a4-ct4``) for demos that
    need a different CT resolution.

    ``extra_metadata`` is free-form DISPLAY metadata only. Keys that
    could influence scoring, gating, C3, validity, decisions, identity
    trust, provenance, or enforcement are PROTECTED and rejected with
    HTTP 422 — at any nesting depth (see tcs.governed_metadata).
    Identity attestations travel exclusively through the typed fields
    below.
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

    # Evaluation typing (tis-v2 Commit 5a.1) — the ONLY public channel
    # for risk tier, action class, and connection type. The same names
    # inside extra_metadata are protected and rejected with 422, so a
    # conflicting duplicate submission fails rather than choosing one
    # value. Omitted fields preserve the existing default behaviour
    # (adapter/interceptor defaults and CT auto-detection).
    risk_tier: Optional[str] = None
    action_class: Optional[str] = None
    connection_type: Optional[str] = None

    # Identity passthroughs — the ONLY public channel for identity
    # attestations. identity_confidence is a governed decimal string.
    requesting_identity: Optional[str] = None
    identity_verified: Optional[bool] = None
    identity_confidence: Optional[str] = Field(
        default=None,
        pattern=GOVERNED_DECIMAL_PATTERN,
        description=(
            "Governed decimal string in [0, 1] (e.g. \"0.899996\"). "
            "JSON numbers are rejected."
        ),
        json_schema_extra={"example": "0.95"},
    )
    authorization_tier: Optional[str] = None
    sensitivity_tier: Optional[str] = None
    mcp_server_id: Optional[str] = None

    # Free-form extras merged into context_metadata (display only —
    # protected keys rejected, see class docstring).
    extra_metadata: Dict[str, Any] = Field(default_factory=dict)

    @field_validator("identity_confidence", mode="before")
    @classmethod
    def _validate_identity_confidence(cls, v: object) -> object:
        if v is None:
            return v
        try:
            parse_unit_interval_decimal(v, "identity_confidence")
        except GovernedDecimalError as exc:
            raise ValueError(str(exc)) from exc
        return v

    @field_validator("risk_tier")
    @classmethod
    def _validate_risk_tier(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and v not in _VALID_RISK_TIERS:
            raise ValueError(
                f"risk_tier must be one of {sorted(_VALID_RISK_TIERS)}"
            )
        return v

    @field_validator("action_class")
    @classmethod
    def _validate_action_class(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and v not in _VALID_ACTION_CLASSES:
            raise ValueError(
                f"action_class must be one of {sorted(_VALID_ACTION_CLASSES)}"
            )
        return v

    @field_validator("connection_type")
    @classmethod
    def _validate_connection_type(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and v not in _VALID_CONNECTION_TYPES:
            raise ValueError(
                "connection_type must be one of "
                f"{sorted(_VALID_CONNECTION_TYPES)}"
            )
        return v


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

    Protected metadata (tis-v2 Commit 5a): governed keys inside
    ``extra_metadata`` — at any nesting depth, case- and
    separator-insensitively — are rejected with HTTP 422. The error
    names every rejected key path; values are never echoed. Nothing
    is silently dropped.
    """
    from decimal import Decimal

    rejected = find_protected_keys(body.extra_metadata)
    if rejected:
        raise HTTPException(
            status_code=422,
            detail={
                "error": "protected_metadata_keys",
                "message": (
                    "extra_metadata may not supply governed scoring, "
                    "gating, C3, validity, decision, identity, "
                    "provenance, or enforcement keys. Identity "
                    "attestations and evaluation typing (risk_tier, "
                    "action_class, connection_type) use the typed "
                    "request fields."
                ),
                "rejected_keys": sorted(rejected),
            },
        )

    # Evaluation typing (tis-v2 Commit 5a.1): the ROUTE — never the
    # public caller — constructs the trusted governed metadata, from
    # the validated typed fields only. A duplicate of these names in
    # extra_metadata was already rejected above (protected keys), so
    # there is exactly one value in play. Omitted fields are simply
    # absent, preserving default behaviour downstream.
    governed_metadata: Dict[str, Any] = {}
    if body.risk_tier is not None:
        governed_metadata["risk_tier"] = body.risk_tier
    if body.action_class is not None:
        governed_metadata["action_class"] = body.action_class
    if body.connection_type is not None:
        governed_metadata["connection_type"] = body.connection_type

    # Translate wire chunks -> adapter chunks. Governed decimal strings
    # become Decimal HERE — no binary float ever exists on this path.
    chunks = [
        RAGChunk(
            chunk_id=c.chunk_id,
            similarity_score=Decimal(c.similarity_score),
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
        identity_confidence=(
            Decimal(body.identity_confidence)
            if body.identity_confidence is not None else None
        ),
        authorization_tier=body.authorization_tier,
        sensitivity_tier=body.sensitivity_tier,
        mcp_server_id=body.mcp_server_id,
        extra_metadata=dict(body.extra_metadata),
        governed_metadata=governed_metadata,
    )

    adapter = RAGAdapter(base_profile_id=body.base_profile_id)
    intercepted = adapter.adapt(rag_output)

    interceptor = request.app.state.interceptor
    response = interceptor.govern(intercepted)
    return response.to_dict()


# --------------------------------------------------------------------------- #
# /v2/govern/decisions/stream — recent decisions feed                          #
# --------------------------------------------------------------------------- #

def _override_records_by_tc(store, tc_ids: list) -> Dict[str, Dict[str, Any]]:
    """
    Bulk-fetch the most-recent override_applied lifecycle_events row
    per certificate_id. Returns {tc_id: {decision, actor, at, reason}}.

    Used by /govern/decisions/stream to surface override badges
    inline without N+1 round-trips. The override metadata is parsed
    from the lifecycle_events.reason string written by the
    override endpoint: "{decision}: {justification} (by {actor})".
    """
    if not tc_ids:
        return {}
    placeholders = ",".join("?" * len(tc_ids))
    rows = store._conn.execute(
        f"""
        SELECT certificate_id, reason, occurred_at
        FROM lifecycle_events
        WHERE to_state = 'override_applied'
          AND certificate_id IN ({placeholders})
        ORDER BY occurred_at DESC
        """,
        tc_ids,
    ).fetchall()
    out: Dict[str, Dict[str, Any]] = {}
    for r in rows:
        cid = r["certificate_id"]
        if cid in out:
            continue  # keep the most recent only
        out[cid] = _parse_override_reason(r["reason"] or "", r["occurred_at"])
    return out


def _parse_override_reason(reason_str: str, occurred_at: str) -> Dict[str, Any]:
    """
    Parse the lifecycle_events.reason string into the override dict
    shape consumed by /govern/decisions/stream rows and the
    override-history endpoint.

    The reason format written by the override endpoints is:
        "{decision}: {justification} (by {actor})"

    The parser anchors the actor marker to the END of the string,
    which makes it robust to a justification that itself contains
    the substring "(by " — e.g. a quoted note like
    "Approved (by Compliance memo 2026-Q2)." The end-anchored
    regex finds only the trailing "(by ...)" and treats everything
    before it as the justification.
    """
    m = _OVERRIDE_REASON_RE.match(reason_str)
    if m:
        decision = m.group("decision").strip() or None
        justification = m.group("justification")
        actor = m.group("actor").strip() or None
    else:
        # Fall back to a permissive shape so the audit row still
        # round-trips even if a future writer produces a non-standard
        # reason. Callers expect the keys to be present.
        decision = None
        justification = reason_str
        actor = None
    return {
        "override_decision": decision,
        "override_actor": actor,
        "override_at": occurred_at,
        "override_reason_text": justification,
        "raw_reason": reason_str,
    }


@router.get("/govern/decisions/{tc_id}/override-history")
def override_history(tc_id: str, request: Request) -> Dict[str, Any]:
    """
    Return every ``override_applied`` lifecycle event for the given TC,
    most recent first.

    The decisions-stream endpoint surfaces only the latest override
    per TC (cheap and bounded for the live feed). This endpoint is the
    audit-grade view: the override badge expands into it so a reviewer
    can see every event recorded against the certificate.

    Returns 200 with ``count=0`` and an empty list when the TC has no
    override events (or does not exist). We do not 404 on unknown
    certificate_id because the UI already shows the override badge
    only when a corresponding event exists; conflating "TC unknown"
    with "no overrides" would not improve the audit story.
    """
    store = request.app.state.store
    rows = store._conn.execute(
        "SELECT reason, occurred_at FROM lifecycle_events "
        "WHERE certificate_id = ? AND to_state = 'override_applied' "
        "ORDER BY occurred_at DESC",
        (tc_id,),
    ).fetchall()
    events = [
        _parse_override_reason(r["reason"] or "", r["occurred_at"])
        for r in rows
    ]
    return {
        "certificate_id": tc_id,
        "count": len(events),
        "events": events,
    }


@router.get("/govern/decisions/stream")
def decisions_stream(
    request: Request,
    limit: int = Query(50, ge=1, le=200),
) -> Dict[str, Any]:
    """
    Return recent governance decisions for the live decisions feed.

    Polling endpoint (not SSE) — returns the most recent TCs with
    decision, scores, and timestamps.

    Each row also carries an ``override`` field. When the TC has had
    a Hold or Escalate override applied, ``override`` is a dict with
    decision / actor / at / reason. When no override has been
    applied, ``override`` is None. The original TC content
    (``decision``, ``blocking_reason``, etc.) is preserved verbatim —
    the override is a separate annotation, never a mutation.
    """
    store = request.app.state.store
    tcs = store.list_recent(limit=limit)
    overrides = _override_records_by_tc(store, [tc.certificate_id for tc in tcs])
    decisions = []
    for tc in tcs:
        d = tc.to_dict()
        decisions.append({
            "certificate_id": d["certificate_id"],
            "subject_id": d["subject_id"],
            "decision": d["decision"],
            "tis_current": d["tis_current"],
            "component_scores": d["component_scores"],
            "gate_result": gate_result_from_serialized(d),
            "blocking_reason": d.get("blocking_reason"),
            "requires_human_review": d["requires_human_review"],
            "evaluation_timestamp": d["evaluation_timestamp"],
            "domain": d["domain"],
            "risk_tier": d["risk_tier"],
            "override": overrides.get(d["certificate_id"]),
        })
    return {"count": len(decisions), "decisions": decisions}


# --------------------------------------------------------------------------- #
# /v2/govern/hold-queue — open Hold decisions                                  #
# --------------------------------------------------------------------------- #

def _overridden_tc_ids(store) -> set:
    """
    Return the set of certificate_ids that have an ``override_applied``
    lifecycle event. Used by the Hold Queue endpoint to filter out
    TCs that have already been overridden.
    """
    rows = store._conn.execute(
        "SELECT DISTINCT certificate_id FROM lifecycle_events "
        "WHERE to_state = 'override_applied'"
    ).fetchall()
    return {r["certificate_id"] for r in rows}


@router.get("/govern/hold-queue")
def hold_queue(
    request: Request,
    limit: int = Query(50, ge=1, le=200),
) -> Dict[str, Any]:
    """
    Return Hold decisions awaiting review.

    Filters recent TCs to only those with decision == "Hold" AND
    no recorded override_applied lifecycle event (the override
    endpoint writes that event; once it's present, the TC drops
    out of the queue).
    """
    store = request.app.state.store
    tcs = store.list_recent(limit=limit * 3)  # over-fetch to find holds
    overridden = _overridden_tc_ids(store)
    holds = []
    for tc in tcs:
        if tc.decision != "Hold":
            continue
        if tc.certificate_id in overridden:
            continue
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
# /v2/govern/hold-queue/{tc_id}/override — submit override                     #
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

    occurred_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    # Persist the override as a lifecycle_events row. The TC itself is
    # append-only (C-R.18 / C-P.14) — corrections never mutate the
    # original certificate. lifecycle_events IS append-only too, but
    # it accepts new rows recording state transitions, which is
    # exactly what an override is: a transition from the TC's
    # original decision to an override_applied state.
    #
    # The Hold Queue's filter (_overridden_tc_ids) checks for
    # to_state='override_applied' rows; this insert is what makes
    # the held TC drop out of the queue on next poll.
    reason = (
        f"{body.override_decision}: {body.justification} "
        f"(by {body.override_by})"
    )
    try:
        store._conn.execute(
            "INSERT INTO lifecycle_events "
            "(certificate_id, from_state, to_state, reason, occurred_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (tc_id, tc.lifecycle_state, "override_applied", reason, occurred_at),
        )
    except Exception as e:  # noqa: BLE001 — surface to caller
        raise HTTPException(
            status_code=500,
            detail=f"failed to persist override: {e}",
        )

    override_record = {
        "certificate_id": tc_id,
        "original_decision": tc.decision,
        "override_decision": body.override_decision,
        "justification": body.justification,
        "override_by": body.override_by,
        "override_at": occurred_at,
        "status": "applied",
    }
    return override_record


# --------------------------------------------------------------------------- #
# /v2/govern/escalation-queue — open Escalate decisions                        #
# --------------------------------------------------------------------------- #
#
# Mirrors hold_queue but filters decision == "Escalate". The two are
# structurally similar in the TC schema (both have requires_human_review,
# both share lifecycle_state="computed") but operationally distinct:
#
#   HOLD       — remediable review; reviewer can typically Allow or
#                Escalate (push it up) the original decision.
#   ESCALATE   — already pushed up; senior reviewer chooses Allow,
#                Stop, or return to Hold for more information.
#
# Escalation TCs carry an ``escalation_routed_to`` list (roles) populated
# at TC generation time. The queue surfaces that list so a reviewer
# knows whether this case was meant for them.

@router.get("/govern/escalation-queue")
def escalation_queue(
    request: Request,
    limit: int = Query(50, ge=1, le=200),
) -> Dict[str, Any]:
    """
    Return Escalate decisions awaiting senior review.

    Filters recent TCs where decision == "Escalate" and excludes any
    TC that already has an ``override_applied`` lifecycle event
    (same exclusion mechanism the Hold Queue uses).
    """
    store = request.app.state.store
    tcs = store.list_recent(limit=limit * 3)
    overridden = _overridden_tc_ids(store)
    escalations = []
    for tc in tcs:
        if tc.decision != "Escalate":
            continue
        if tc.certificate_id in overridden:
            continue
        d = tc.to_dict()
        escalations.append({
            "certificate_id":      d["certificate_id"],
            "subject_id":          d["subject_id"],
            "tis_current":         d["tis_current"],
            "s_base":              d.get("s_base"),
            "component_scores":    d["component_scores"],
            "gate_results":        d.get("gate_results"),
            "blocking_reason":     d.get("blocking_reason"),
            "evaluation_timestamp": d["evaluation_timestamp"],
            "domain":              d["domain"],
            "risk_tier":           d["risk_tier"],
            "policy_set_id":       d.get("policy_set_id"),
            # The reviewer-routing list. Empty list means the
            # generator didn't populate it; non-empty names the
            # roles the escalation was routed to.
            "escalation_routed_to": d.get("escalation_routed_to", []),
            # Identity context if available so a reviewer knows
            # whose action this was.
            "identity_binding":    d.get("identity_binding"),
            "override_status":     "pending",
        })
        if len(escalations) >= limit:
            break
    return {"count": len(escalations), "escalations": escalations}


class EscalationOverrideBody(BaseModel):
    override_decision: str = Field(
        ...,
        description="Reviewer's decision: 'Allow' | 'Stop' | 'Hold'.",
    )
    justification: str = Field(
        ..., min_length=10, description="Reason for the escalation decision",
    )
    override_by: str = Field(
        ..., description="Identity of the senior reviewer",
    )


@router.post("/govern/escalation-queue/{tc_id}/override")
def submit_escalation_override(
    tc_id: str,
    body: EscalationOverrideBody,
    request: Request,
) -> Dict[str, Any]:
    """
    Submit a senior-reviewer decision for an Escalate TC.

    Allowed override_decision values:
      - Allow : reviewer approves the higher-risk action
      - Stop  : reviewer rejects it outright
      - Hold  : reviewer returns to Hold pending more information

    Persistence is identical to the Hold-queue override path: writes
    a single ``lifecycle_events`` row with ``to_state='override_applied'``
    and a reason string capturing the decision, justification, and
    actor. The original TC is never mutated (append-only invariant).
    Both queues use the same override exclusion mechanism, so once
    written, the TC drops out of /govern/escalation-queue on the
    next poll.
    """
    from tcs.persistence import CertificateNotFoundError

    store = request.app.state.store
    try:
        tc = store.get(tc_id)
    except CertificateNotFoundError:
        raise HTTPException(status_code=404, detail=f"TC {tc_id!r} not found")

    if tc.decision != "Escalate":
        raise HTTPException(
            status_code=400,
            detail=(
                f"TC {tc_id!r} decision is {tc.decision!r}, not Escalate "
                "(use /v2/govern/hold-queue/{tc_id}/override for Hold TCs)"
            ),
        )

    if body.override_decision not in ("Allow", "Stop", "Hold"):
        raise HTTPException(
            status_code=400,
            detail=(
                "override_decision must be 'Allow', 'Stop', or 'Hold' "
                "for escalation-queue overrides"
            ),
        )

    occurred_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    reason = (
        f"{body.override_decision}: {body.justification} "
        f"(by {body.override_by})"
    )
    try:
        store._conn.execute(
            "INSERT INTO lifecycle_events "
            "(certificate_id, from_state, to_state, reason, occurred_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (tc_id, tc.lifecycle_state, "override_applied", reason, occurred_at),
        )
    except Exception as e:  # noqa: BLE001
        raise HTTPException(
            status_code=500,
            detail=f"failed to persist escalation override: {e}",
        )

    return {
        "certificate_id":    tc_id,
        "original_decision": tc.decision,
        "override_decision": body.override_decision,
        "justification":     body.justification,
        "override_by":       body.override_by,
        "override_at":       occurred_at,
        "status":            "applied",
    }
