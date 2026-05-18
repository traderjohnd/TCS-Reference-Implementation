"""
tcs.workflow.connectors.rag
============================

RAG retrieval connector. Wraps a vector store behind the
GovernedConnector contract.

Connection type
---------------

CT-4 (Vector DB / RAG). Per TCS_SPEC.md §18, RAG carries elevated
attribution risk because retrieved chunks often arrive without
complete provenance (missing source_doc, version, timestamp). The
CT-4 modifier in CT_WEIGHT_MODIFIERS reflects this by raising A
weight and tightening the A threshold.

Evidence emitted
----------------

The RAG connector contributes the bulk of A and K signals for any
workflow that uses retrieval:

    A: source_count, sources_with_complete_metadata,
       integration_boundary_gaps (n_gaps), chain_of_custody_complete
    K: novelty heuristic — if mean similarity is low, the retrieval
       is "novel" to the corpus and K is reduced
    B: in_scope (defaults to True; no scope claim made)
    C: defaults; the C3 injection scan happens at the LLM connector
       which sees the model's response text

The vector store object is injected at construction. The standard
demo store is ``demos.governed_rag.vector_store.SimpleVectorStore``
but any object exposing ``retrieve(query, k=N) -> list[dict]`` works.
"""

from __future__ import annotations

import time
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from tcs.workflow.connector import (
    ConnectorRequest,
    ConnectorResult,
    GovernedConnector,
)
from tcs.workflow.events import (
    AttributionSignal,
    BoundednessSignal,
    ComplianceSignal,
    GovernanceEvent,
    KnownStateSignal,
)
from tcs.workflow.trace import GovernedNode


# Below this mean similarity the K dimension is treated as novel.
_NOVELTY_SIMILARITY_THRESHOLD = 0.80

# Lightweight credential patterns. Mirrors the spirit of the existing
# governed_context.check_response_injection logic. Detection in retrieved
# chunks triggers a C3 hard stop per the C-R rules: credentials in the
# governed context surface must never reach the model.
_CREDENTIAL_PATTERNS = (
    "api_key=",
    "apikey=",
    "secret_key=",
    "secretkey=",
    "private_key=",
    "bearer ",
    "sk-proj-",
    "ssn:",
    "password=",
)


def _detect_credential(text: str) -> Optional[str]:
    if not text:
        return None
    lower = text.lower()
    for pat in _CREDENTIAL_PATTERNS:
        if pat in lower:
            return pat
    return None


class RAGConnector(GovernedConnector):
    """
    Adapter for a vector store retrieval step.

    Parameters
    ----------
    store
        Object with a ``retrieve(query, k=int) -> list[dict]`` method.
        Each returned chunk should have at minimum: ``chunk_id``,
        ``content``, ``similarity_score``. Optional fields used for
        attribution scoring: ``source_doc``, ``version``,
        ``timestamp``, ``tags``.
    retrieval_k
        How many chunks to retrieve per query. Default 5.
    """

    connector_type = "rag"

    def __init__(self, *, store: Any, retrieval_k: int = 5) -> None:
        self.store = store
        self.retrieval_k = retrieval_k

    def connection_type(self) -> str:
        return "CT-4"

    def invoke(self, request: ConnectorRequest) -> ConnectorResult:
        t0 = time.perf_counter()
        k = int(request.params.get("retrieval_k", self.retrieval_k))
        try:
            chunks = self.store.retrieve(request.query, k=k)
        except Exception as exc:
            return ConnectorResult(
                payload=[],
                output_text=None,
                raw_metadata={"exception_type": type(exc).__name__},
                latency_ms=round((time.perf_counter() - t0) * 1000, 2),
                error=str(exc),
            )

        if not isinstance(chunks, list):
            chunks = list(chunks)

        # Compute metadata completeness counts for downstream scoring,
        # and scan chunk content for credentials (C-R credential rule).
        complete_count = 0
        gaps = 0
        sim_scores: List[float] = []
        credential_pattern: Optional[str] = None
        credential_chunk_id: Optional[str] = None
        for c in chunks:
            has_source = bool(c.get("source_doc"))
            has_version = bool(c.get("version"))
            if has_source and has_version:
                complete_count += 1
            else:
                gaps += 1
            try:
                sim = float(c.get("similarity_score", 0.0))
            except (TypeError, ValueError):
                sim = 0.0
            sim_scores.append(sim)
            if credential_pattern is None:
                hit = _detect_credential(str(c.get("content", "")))
                if hit:
                    credential_pattern = hit
                    credential_chunk_id = c.get("chunk_id")

        mean_sim = sum(sim_scores) / len(sim_scores) if sim_scores else 0.0

        return ConnectorResult(
            payload=chunks,
            output_text=None,
            raw_metadata={
                "chunk_count": len(chunks),
                "complete_metadata_count": complete_count,
                "n_gaps": gaps,
                "mean_similarity": round(mean_sim, 4),
                "min_similarity": round(min(sim_scores), 4) if sim_scores else 0.0,
                "novelty_flagged": mean_sim < _NOVELTY_SIMILARITY_THRESHOLD,
                "credential_detected": credential_pattern is not None,
                "credential_pattern": credential_pattern,
                "credential_chunk_id": credential_chunk_id,
            },
            latency_ms=round((time.perf_counter() - t0) * 1000, 2),
        )

    def to_governance_event(
        self,
        result: ConnectorResult,
        node: GovernedNode,
        *,
        workflow_id: str,
        previous_event_hash: Optional[str] = None,
    ) -> GovernanceEvent:
        meta = result.raw_metadata
        chunk_count = int(meta.get("chunk_count", 0) or 0)
        complete = int(meta.get("complete_metadata_count", 0) or 0)
        gaps = int(meta.get("n_gaps", 0) or 0)

        # Attribution: fraction of chunks with complete metadata.
        a_score = 1.0 if chunk_count == 0 else complete / chunk_count

        attribution = AttributionSignal(
            source_count=chunk_count,
            sources_with_complete_metadata=complete,
            integration_boundary_gaps=gaps,
            timestamp_present=True,
            chain_of_custody_complete=(gaps == 0),
            score_contribution=a_score,
        )

        # Known: similarity drives a novelty heuristic. Mean similarity
        # well above the threshold means familiar territory (K high);
        # below means out-of-distribution / novel (K low).
        mean_sim = float(meta.get("mean_similarity", 0.0) or 0.0)
        novelty_flagged = bool(meta.get("novelty_flagged", False))
        # Map mean_similarity in [0, 1] to a K contribution in [0, 1].
        # Above 0.95 -> ~1.0; at 0.80 (threshold) -> ~0.80; below -> falls off.
        k_score = max(0.0, min(1.0, mean_sim))
        known = KnownStateSignal(
            confidence_calibrated=not novelty_flagged,
            novelty_score=round(1.0 - mean_sim, 4),
            score_contribution=k_score,
        )

        # Boundedness: RAG retrieval makes no scope claim.
        boundedness = BoundednessSignal()

        # Compliance: credential detection in chunks is a C3 hard-stop
        # signal (action-layer prohibited pattern: credentials must
        # never reach the model). The decision engine will Stop the
        # workflow via Priority 2 regardless of other dimension scores.
        credential_detected = bool(meta.get("credential_detected", False))
        cred_pattern = meta.get("credential_pattern")
        if credential_detected:
            compliance = ComplianceSignal(
                c3_violation=True,
                c3_pattern=f"credential_detected:{cred_pattern}",
                policy_violations=("credential_in_governed_context",),
                score_contribution=0.0,
            )
        else:
            compliance = ComplianceSignal()

        return GovernanceEvent(
            event_id=str(uuid.uuid4()),
            workflow_id=workflow_id,
            node_id=node.node_id,
            timestamp=datetime.now(timezone.utc).isoformat(),
            connector_type=self.connector_type,
            connection_type=self.connection_type(),
            sensitivity_tier=node.sensitivity_tier,
            boundedness=boundedness,
            attribution=attribution,
            compliance=compliance,
            known=known,
            payload_ref=None,
            latency_ms=result.latency_ms,
            error=result.error,
            previous_event_hash=previous_event_hash,
        )
