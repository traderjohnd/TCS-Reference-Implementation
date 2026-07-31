"""
tcs.workflow.connectors.web_retrieval
=====================================

Provider-hosted web retrieval node (demo-live branch, Commit 5).

The provider executes search and generation inside ONE external API
request, but the governed trace must expose retrieval and generation
as distinct logical nodes. This connector REPLAYS the already-recorded
:class:`WebRetrievalEvidence` produced by the web adapter — it never
performs retrieval itself, and the trace never claims TCS
independently downloaded a page (the node is explicitly named
``provider_hosted_web_retrieval``).

Evidence emitted (bounded facts through existing signal interfaces —
no new scoring, no source-quality judgments, no domain-based trust):

    A: source_count = consulted sources; sources_with_complete_metadata
       = consulted sources carrying both a URL and a title;
       timestamp_present = retrieval timestamps recorded.
    B/C/K: defaults — the LLM node and GCA own those signals.
"""

from __future__ import annotations

import time
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from tcs.providers.web_evidence import WebRetrievalEvidence
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

WEB_RETRIEVAL_NODE_ID = "provider-hosted-web-retrieval"


class WebRetrievalConnector(GovernedConnector):
    connector_type = "web_retrieval"

    def __init__(self, *, evidence: WebRetrievalEvidence,
                 evidence_digest: str,
                 evidence_artifact_id: Optional[str] = None) -> None:
        self.evidence = evidence
        self.evidence_digest = evidence_digest
        self.evidence_artifact_id = evidence_artifact_id

    def connection_type(self) -> str:
        return "CT-6"  # Web connection type per TCS_SPEC.md §18

    def invoke(self, request: ConnectorRequest) -> ConnectorResult:
        t0 = time.perf_counter()
        ev = self.evidence
        # Bounded node metadata only — never the source list, never
        # citation payloads, never page content, never credentials.
        summary: Dict[str, Any] = {
            "provider": ev.provider,
            "model": ev.model,
            "retrieval_status": ev.retrieval_status,
            "search_call_count": ev.search_call_count,
            "consulted_source_count": ev.consulted_source_count,
            "cited_source_count": ev.cited_source_count,
            "evidence_artifact_id": self.evidence_artifact_id,
            "web_evidence_digest": self.evidence_digest,
            "error_categories": (
                [ev.error_summary] if ev.error_summary else []
            ),
            "live_access_requested": ev.live_access_requested,
            "live_access_confirmed": ev.live_access_confirmed,
        }
        return ConnectorResult(
            payload=summary,
            output_text=None,
            raw_metadata=dict(summary),
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
        ev = self.evidence
        complete = sum(
            1 for s in ev.consulted_sources
            if s.display_url and s.title
        )
        attribution = AttributionSignal(
            source_count=ev.consulted_source_count,
            sources_with_complete_metadata=complete,
            timestamp_present=bool(ev.retrieval_completed_at),
        )
        return GovernanceEvent(
            event_id=str(uuid.uuid4()),
            workflow_id=workflow_id,
            node_id=node.node_id,
            timestamp=datetime.now(timezone.utc).isoformat(),
            connector_type=f"{self.connector_type}.{ev.provider}",
            connection_type=self.connection_type(),
            sensitivity_tier=node.sensitivity_tier,
            boundedness=BoundednessSignal(),
            attribution=attribution,
            compliance=ComplianceSignal(),
            known=KnownStateSignal(),
            payload_ref=None,
            latency_ms=result.latency_ms,
            error=None,
            previous_event_hash=previous_event_hash,
        )


__all__ = ["WebRetrievalConnector", "WEB_RETRIEVAL_NODE_ID"]
