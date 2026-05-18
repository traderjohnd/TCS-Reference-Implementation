"""
tcs.workflow.connectors.marker
===============================

TIS Evaluation Marker — a sentinel node declaring the point at which
TIS was (conceptually) evaluated in the workflow.

This is a built-in, no-op connector. It does no real work; it just
emits a marker GovernanceEvent so the trace records the boundary.
The GCA reads the marker and treats any post-marker nodes as
"post-evaluation" — if any such node carries context-expansion
evidence (e.g. an MCP retrieval pulling new context after the
governance gate ran), the GCA must invalidate the workflow per
C-R.14 and force re-evaluation.

Connection type
---------------

Markers are not real connections. They use CT-1 as a benign default
purely to satisfy the policy resolver's CT-aware path; the GCA
recognizes the marker node type and does not let it influence
dominant CT selection.

Operational meaning of "post-evaluation"
----------------------------------------

In the declarative Slice 2 model, the workflow author places a
marker to declare "TIS evaluation conceptually happens here." Any
node after the marker represents action or retrieval that crossed
the governance boundary. If those post-marker actions expand the
governed context (per C-R.14), the originally-issued certificate
becomes stale; the workflow must be re-evaluated against the
expanded context before any further action.

Operationally this is delivered through the standard invalidation
path: ``I_inv = 0``, ``invalidation_event = "context_expansion"``,
``TIS_current = 0``, decision = Stop, lifecycle = invalidated.
The Stop label means "delivery blocked, re-evaluation required" —
not "permanently rejected."
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Optional

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
from tcs.workflow.trace import GovernedNode, NodeType


class TISEvaluationMarkerConnector(GovernedConnector):
    """No-op connector that marks the TIS evaluation boundary."""

    connector_type = "marker.tis_evaluation"

    def connection_type(self) -> str:
        return "CT-1"

    def invoke(self, request: ConnectorRequest) -> ConnectorResult:
        return ConnectorResult(
            payload={"marker": "tis_evaluation"},
            output_text=None,
            raw_metadata={"is_marker": True},
            latency_ms=0.0,
        )

    def to_governance_event(
        self,
        result: ConnectorResult,
        node: GovernedNode,
        *,
        workflow_id: str,
        previous_event_hash: Optional[str] = None,
    ) -> GovernanceEvent:
        # Marker emits all-1.0 signals so it never reduces any
        # dimension when min-aggregated. The GCA recognizes the
        # marker by node.node_type, not by signal content.
        return GovernanceEvent(
            event_id=str(uuid.uuid4()),
            workflow_id=workflow_id,
            node_id=node.node_id,
            timestamp=datetime.now(timezone.utc).isoformat(),
            connector_type=self.connector_type,
            connection_type=self.connection_type(),
            sensitivity_tier=node.sensitivity_tier,
            boundedness=BoundednessSignal(),
            attribution=AttributionSignal(),
            compliance=ComplianceSignal(),
            known=KnownStateSignal(),
            payload_ref="tis_evaluation_marker",
            latency_ms=0.0,
            previous_event_hash=previous_event_hash,
        )


def make_marker_node(node_id: str = "tis-marker") -> GovernedNode:
    """Convenience: construct a properly-typed marker node."""
    return GovernedNode(
        node_id=node_id,
        name="TIS Evaluation Marker",
        node_type=NodeType.TIS_EVALUATION_MARKER,
        connection_type="CT-1",
        sensitivity_tier="T0",
    )
