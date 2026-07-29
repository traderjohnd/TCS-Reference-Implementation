"""
tcs.adapters.rag_adapter
========================

Translate a RAG pipeline's output into an :class:`InterceptedRequest`
that the enforcement controller can govern.

A RAG pipeline typically emits:

    * the user's query
    * a list of retrieved chunks (with source / version / similarity)
    * a candidate answer generated from those chunks
    * optional metadata: model id, policy set id, session / identity, etc.

This adapter maps that output onto the CT-4 context shape expected by
``tcs.governed_context.assemble_context_v2``:

    retrieved_chunks -> [
        {
            chunk_id, source_doc, version, similarity_score, content, tags
        }
    ]

Plus the derived signals:

    * ``n_gaps``            count of chunks missing source_doc or version
    * ``chunk_min_similarity`` / ``chunk_mean_similarity``
    * ``low_similarity_flag``  True if any chunk is below SIMILARITY_FLOOR
    * ``k_subfactor_penalty``  scalar in [0, 0.5] reflecting how far the
                               worst similarity is below the floor;
                               used by downstream dimension scorers to
                               degrade the K score accordingly

The adapter is **pure**. It never:

    * computes TIS
    * makes decisions
    * writes to persistence
    * calls the TIS engine

It only reshapes data into the structured request the governance
pipeline consumes.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, localcontext
from typing import Any, Dict, List, Optional, Union

from tcs.canonical import TIS_DECIMAL_CONTEXT
from tcs.governed_metadata import find_protected_keys


# --------------------------------------------------------------------------- #
# Constants                                                                    #
# --------------------------------------------------------------------------- #

#: Minimum acceptable similarity score for a retrieved chunk.
#: Chunks below this contribute to U sub-factor degradation.
#: Phase 2 spec: "Similarity scores below 0.80 -> elevated U sub-factor".
SIMILARITY_FLOOR: float = 0.80

#: Maximum K sub-factor penalty the adapter will recommend. Keeps the
#: governance layer in control of the actual TIS effect.
MAX_K_SUBFACTOR_PENALTY: float = 0.50

# tis-v2 Commit 5a — the adapter computes similarity-derived signals in
# Decimal. Similarity is DEFINED as a decimal measurement at the
# retrieval/wire boundary; no binary float participates in the governed
# arithmetic downstream of that boundary.
SIMILARITY_FLOOR_DECIMAL = Decimal("0.80")
MAX_K_SUBFACTOR_PENALTY_DECIMAL = Decimal("0.5000")
_SIMILARITY_QUANTUM = Decimal("0.000001")
_PENALTY_QUANTUM = Decimal("0.0001")


def _as_governed_decimal(value: Any, field_name: str) -> Decimal:
    """Coerce an internal similarity/confidence input to Decimal.

    Accepts Decimal, int, or decimal string. Binary floats are REJECTED
    — an internal producer that still holds a float must declare its
    measurement as a decimal string at the point of measurement rather
    than laundering the float here (owner correction 1).
    """
    if isinstance(value, bool) or isinstance(value, float):
        raise ValueError(
            f"{field_name}: governed decimal inputs must be Decimal or "
            f"decimal strings, got {type(value).__name__} — declare the "
            f"measurement as a decimal at its source"
        )
    if isinstance(value, Decimal):
        return value
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, str):
        return Decimal(value)
    raise ValueError(
        f"{field_name}: cannot interpret {type(value).__name__} as a "
        f"governed decimal"
    )


# --------------------------------------------------------------------------- #
# Input / output dataclasses                                                   #
# --------------------------------------------------------------------------- #

@dataclass
class RAGChunk:
    """
    A single retrieved chunk as emitted by a RAG pipeline.

    All fields except ``chunk_id`` and ``similarity_score`` are optional —
    missing metadata drives the ``n_gaps`` counter upward and ultimately
    degrades the Attribution dimension in the TIS engine.

    ``content`` is the raw chunk text. Response-injection scanning runs
    over it in :mod:`tcs.governed_context`; the adapter does not scan.
    """
    chunk_id: str
    similarity_score: Union[Decimal, str, int]   # Decimal-native (5a); floats rejected in adapt()
    source_doc: Optional[str] = None
    version: Optional[str] = None
    content: str = ""
    tags: List[str] = field(default_factory=list)


@dataclass
class RAGOutput:
    """
    Output of a RAG pipeline prior to enforcement.

    ``candidate_answer`` is what the pipeline would return to the user
    if no governance were in place. The enforcement controller decides
    whether that answer actually reaches the user.
    """
    query: str
    retrieved_chunks: List[RAGChunk]
    candidate_answer: str
    model_id: str = "rag-demo-model"
    pipeline_id: str = "finance-rag-v1"
    subject_type: str = "recommendation"
    subject_id: Optional[str] = None
    request_id: Optional[str] = None

    # Optional passthroughs into context_metadata
    requesting_identity: Optional[str] = None
    identity_verified: Optional[bool] = None
    identity_confidence: Optional[Decimal] = None   # Decimal-native (5a)
    authorization_tier: Optional[str] = None
    sensitivity_tier: Optional[str] = None
    mcp_server_id: Optional[str] = None

    #: Free-form DISPLAY metadata. The public /v2/govern route rejects
    #: protected keys before this is populated, and adapt() enforces the
    #: same contract for direct internal callers — governed keys here
    #: raise rather than silently overriding adapter-computed signals.
    extra_metadata: Dict[str, Any] = field(default_factory=dict)

    #: Governed metadata for TRUSTED INTERNAL producers only (tests,
    #: demos, in-process pipelines). Never populated from the public
    #: HTTP surface. Keys here may override adapter-computed signals —
    #: that is the point: an internal, typed, deliberate channel,
    #: instead of a public free-form one (owner decision 2).
    governed_metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class InterceptedRequest:
    """
    The shared contract between adapters and the enforcement controller.

    This is the shape the sidecar consumes. Any adapter (Phase 2 RAG,
    Phase 3 agent chains, Phase 3 tool calls) must produce one of
    these. The enforcement controller never reads adapter-specific
    types — only InterceptedRequest.

    Fields:

        request_id          — unique id for audit (UUID4 if unset)
        received_at         — ISO-8601 UTC timestamp of interception
        subject_id          — identifier of the output under evaluation
        subject_type        — "recommendation" | "model_output" | ...
        candidate_output    — the raw string the pipeline would return
        base_profile_id     — which policy profile to load
                              (e.g. "fin-r3-a4-ct4")
        context_bundle      — the shape consumed by assemble_context_v2
                              (retrieved_chunks + derived signals)
        raw_output_metadata — pipeline diagnostics (model id, etc.)
                              for the TC explanation layer

    The context_bundle is deliberately a dict (not another dataclass)
    because it flows directly into assemble_context_v2, which is
    dict-native. The dataclass layer is for the adapter↔sidecar
    boundary, not for GCA internals.
    """
    request_id: str
    received_at: str
    subject_id: str
    subject_type: str
    candidate_output: str
    base_profile_id: str
    context_bundle: Dict[str, Any]
    raw_output_metadata: Dict[str, Any] = field(default_factory=dict)


# --------------------------------------------------------------------------- #
# Adapter                                                                      #
# --------------------------------------------------------------------------- #

class RAGAdapter:
    """
    Translate :class:`RAGOutput` into :class:`InterceptedRequest`.

    Instantiate with a ``base_profile_id`` (the policy profile the
    sidecar will resolve against — typically ``"fin-r3-a4-ct4"`` for
    the Phase 2 demo). The adapter is stateless beyond that config.
    """

    def __init__(
        self,
        base_profile_id: str = "fin-r3-a4-ct4",
        *,
        similarity_floor: float = SIMILARITY_FLOOR,
    ) -> None:
        self.base_profile_id = base_profile_id
        self.similarity_floor = similarity_floor

    # ---- Public API ----------------------------------------------------- #

    def adapt(self, rag_output: RAGOutput) -> InterceptedRequest:
        """
        Produce an :class:`InterceptedRequest` from a RAG pipeline result.

        The context_bundle carries:

            retrieved_chunks        — list of dicts in the shape
                                      assemble_context_v2 expects
            n_gaps                  — count of chunks missing source_doc
                                      or version
            chunk_min_similarity    — minimum similarity across chunks
            chunk_mean_similarity   — mean similarity across chunks
            low_similarity_flag     — True if any chunk < similarity_floor
            k_subfactor_penalty     — how far below floor the worst chunk
                                      is, bounded by MAX_K_SUBFACTOR_PENALTY
            prompt                  — the original user query (so the
                                      injection scanner sees it)
            pipeline_id / model_id  — carried through for the TC
        """
        request_id = rag_output.request_id or f"req-{uuid.uuid4().hex[:12]}"
        subject_id = (
            rag_output.subject_id
            or f"{rag_output.pipeline_id}-output-{uuid.uuid4().hex[:8]}"
        )

        chunks = [self._chunk_to_dict(c) for c in rag_output.retrieved_chunks]

        n_gaps = sum(
            1 for c in chunks
            if not c.get("source_doc") or not c.get("version")
        )

        # Decimal-native similarity signals (tis-v2 Commit 5a): the wire
        # boundary delivers decimal strings, the route converts them to
        # Decimal, and every derived value below is Decimal arithmetic
        # inside the pinned context. No binary float exists on this path.
        similarities = [
            c["similarity_score"] for c in chunks
            if c.get("similarity_score") is not None
        ]
        with localcontext(TIS_DECIMAL_CONTEXT):
            if similarities:
                chunk_min = min(similarities)
                chunk_mean = (
                    sum(similarities, Decimal("0")) / len(similarities)
                ).quantize(_SIMILARITY_QUANTUM)
            else:
                chunk_min = Decimal("1")
                chunk_mean = Decimal("1")

        low_similarity_flag = chunk_min < SIMILARITY_FLOOR_DECIMAL
        k_penalty = self._compute_k_penalty_decimal(chunk_min)

        context_bundle: Dict[str, Any] = {
            "retrieved_chunks": chunks,
            "prompt": rag_output.query,
            "n_gaps": n_gaps,
            "chunk_min_similarity": chunk_min,
            "chunk_mean_similarity": chunk_mean,
            "low_similarity_flag": low_similarity_flag,
            "k_subfactor_penalty": k_penalty,
            "pipeline_id": rag_output.pipeline_id,
            "model_id": rag_output.model_id,
        }

        # Identity passthrough — only set keys the caller actually
        # supplied; otherwise the TC generator falls back to the
        # Phase-1 optimistic defaults.
        for field_name in (
            "requesting_identity",
            "identity_verified",
            "identity_confidence",
            "authorization_tier",
            "sensitivity_tier",
            "mcp_server_id",
        ):
            value = getattr(rag_output, field_name, None)
            if value is not None:
                context_bundle[field_name] = value

        # extra_metadata is DISPLAY metadata only (tis-v2 Commit 5a).
        # Protected governed keys are rejected — the public route already
        # blocked them with HTTP 422; enforcing again here means a direct
        # internal caller cannot smuggle them through this channel either.
        protected = find_protected_keys(rag_output.extra_metadata)
        if protected:
            raise ValueError(
                "extra_metadata may not carry governed keys "
                f"{sorted(protected)}; internal producers use "
                "RAGOutput.governed_metadata"
            )
        for k, v in rag_output.extra_metadata.items():
            context_bundle[k] = v

        # governed_metadata is the TYPED INTERNAL channel for trusted
        # producers (tests, demos, in-process pipelines). It may override
        # adapter-computed signals deliberately. The public HTTP surface
        # never populates it.
        for k, v in rag_output.governed_metadata.items():
            context_bundle[k] = v

        return InterceptedRequest(
            request_id=request_id,
            received_at=datetime.now(timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            ),
            subject_id=subject_id,
            subject_type=rag_output.subject_type,
            candidate_output=rag_output.candidate_answer,
            base_profile_id=self.base_profile_id,
            context_bundle=context_bundle,
            raw_output_metadata={
                "pipeline_id": rag_output.pipeline_id,
                "model_id": rag_output.model_id,
                "n_chunks": len(chunks),
                "chunk_min_similarity": chunk_min,
                "chunk_mean_similarity": chunk_mean,
                "low_similarity_flag": low_similarity_flag,
            },
        )

    # ---- Internal helpers ---------------------------------------------- #

    @staticmethod
    def _chunk_to_dict(c: RAGChunk) -> Dict[str, Any]:
        """Serialize a RAGChunk to the dict shape assemble_context_v2 expects."""
        return {
            "chunk_id": c.chunk_id,
            "source_doc": c.source_doc,
            "version": c.version,
            "similarity_score": _as_governed_decimal(
                c.similarity_score, f"chunk {c.chunk_id} similarity_score"
            ),
            "content": c.content or "",
            "tags": list(c.tags),
        }

    def _compute_k_penalty_decimal(self, chunk_min: Decimal) -> Decimal:
        """Decimal-native K sub-factor penalty (same contract as
        :meth:`_compute_k_penalty`, which is retained for legacy
        callers): 0 at/above the floor, MAX at similarity 0, linear
        between, quantized to 4dp."""
        if chunk_min >= SIMILARITY_FLOOR_DECIMAL:
            return Decimal("0.0000")
        with localcontext(TIS_DECIMAL_CONTEXT):
            shortfall = SIMILARITY_FLOOR_DECIMAL - chunk_min
            scaled = shortfall / SIMILARITY_FLOOR_DECIMAL
            penalty = min(
                MAX_K_SUBFACTOR_PENALTY_DECIMAL,
                scaled * MAX_K_SUBFACTOR_PENALTY_DECIMAL,
            )
            return penalty.quantize(_PENALTY_QUANTUM)

    def _compute_k_penalty(self, chunk_min: float) -> float:
        """
        Map the worst-chunk similarity to a K sub-factor penalty.

        Contract:
            * chunk_min >= similarity_floor -> 0.0 (no penalty)
            * chunk_min = 0                -> MAX_K_SUBFACTOR_PENALTY
            * linear between those points

        The penalty is advisory — the governance layer decides how to
        translate it into an actual K score. The adapter merely surfaces
        the signal.
        """
        if chunk_min >= self.similarity_floor:
            return 0.0
        if self.similarity_floor <= 0:
            return MAX_K_SUBFACTOR_PENALTY
        # Linear decay: 0 penalty at floor, max penalty at 0.
        shortfall = self.similarity_floor - chunk_min
        scaled = shortfall / self.similarity_floor
        return round(min(MAX_K_SUBFACTOR_PENALTY, scaled * MAX_K_SUBFACTOR_PENALTY), 4)


# --------------------------------------------------------------------------- #
# Module-level convenience                                                     #
# --------------------------------------------------------------------------- #

_DEFAULT_ADAPTER: Optional[RAGAdapter] = None


def adapt(
    rag_output: RAGOutput,
    *,
    base_profile_id: str = "fin-r3-a4-ct4",
) -> InterceptedRequest:
    """
    Module-level convenience wrapper around :meth:`RAGAdapter.adapt`.

    Uses a cached adapter instance when the ``base_profile_id`` matches
    the default so callers do not need to construct one.
    """
    global _DEFAULT_ADAPTER
    if _DEFAULT_ADAPTER is None or _DEFAULT_ADAPTER.base_profile_id != base_profile_id:
        _DEFAULT_ADAPTER = RAGAdapter(base_profile_id=base_profile_id)
    return _DEFAULT_ADAPTER.adapt(rag_output)
