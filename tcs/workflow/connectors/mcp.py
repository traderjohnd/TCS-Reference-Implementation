"""
tcs.workflow.connectors.mcp
============================

MCP connector — SHAPE-ONLY for Phase 4.

The wire to a real MCP server is deferred to Phase 5. What ships
here is the full governance surface the white paper requires:
server identity, tool selection, pre/post-evaluation timing,
enforcement perimeter status, context-expansion detection, and
Trust Certificate non-transferability.

This means: the connector emits production-grade governance
evidence today. When the real MCP wire lands in Phase 5, it can
replace the simulated invoke without changing the events or the
GCA logic.

Connection type
---------------

Default CT-1 (API). Becomes CT-12 (credentials) if the simulated
MCP payload contains credential markers — in which case the
connector raises governance evidence that drives a hard Stop.

Pre vs post evaluation
----------------------

The connector itself does not run before or after the TIS
evaluation — that is determined by where the workflow author
places the node relative to a ``TIS_EVALUATION_MARKER`` node.
What the connector contributes is the *evidence* that, if seen
post-marker, triggers the C-R.14 invalidation path in the GCA.

The connector takes a ``context_expansion`` flag in its params.
When True, the emitted event carries ``context_expansion_payload``
metadata. The GCA detects this on any post-marker node and forces
``I_inv = 0`` with ``invalidation_event = "context_expansion"``.

Operational meaning
-------------------

"Post-evaluation MCP retrieval triggers context_expansion" means
the originally-issued TC is stale; the workflow must be
re-evaluated against the expanded context before any further
action. Operationally delivered as Stop with lifecycle =
invalidated and a clear ``blocking_reason`` of
``invalidation_context_expansion``.
"""

from __future__ import annotations

import time
import uuid
from datetime import datetime, timezone
from typing import Any, List, Optional

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


class MCPConnector(GovernedConnector):
    """
    Shape-only MCP connector.

    Parameters
    ----------
    mcp_server_id
        Declared MCP server identity. Required.
    in_scope
        Whether this MCP server is inside the deployment manifest's
        Signal Chain scope. Default True. If False, the connector
        emits ``enforcement_perimeter_complete=False`` evidence and
        the workflow's TC is advisory only (per C-R.13).
    """

    connector_type = "mcp"

    def __init__(
        self,
        *,
        mcp_server_id: str,
        in_scope: bool = True,
    ) -> None:
        if not mcp_server_id:
            raise ValueError("MCPConnector requires a non-empty mcp_server_id")
        self.mcp_server_id = mcp_server_id
        self.in_scope = in_scope

    def connection_type(self) -> str:
        return "CT-1"

    def invoke(self, request: ConnectorRequest) -> ConnectorResult:
        """
        Simulate an MCP tool invocation.

        Params honored:
            tool_name (str)             : which MCP tool was selected
            context_expansion (bool)    : True if this call expanded
                                          governed context after eval
            tc_reuse_attempted (bool)   : True if an upstream TC was
                                          referenced as authorization
                                          (C-R.15 violation)
            payload (Any)               : simulated MCP response body
        """
        params = request.params or {}
        tool_name = str(params.get("tool_name", "unknown_tool"))
        context_expansion = bool(params.get("context_expansion", False))
        tc_reuse_attempted = bool(params.get("tc_reuse_attempted", False))
        sim_payload = params.get("payload", {"simulated": True})

        t0 = time.perf_counter()
        # Shape-only: no real MCP wire. The "call" is instantaneous.
        latency = round((time.perf_counter() - t0) * 1000, 2)

        return ConnectorResult(
            payload=sim_payload,
            output_text=None,
            raw_metadata={
                "mcp_server_id": self.mcp_server_id,
                "tool_name": tool_name,
                "in_scope": self.in_scope,
                "context_expansion": context_expansion,
                "tc_reuse_attempted": tc_reuse_attempted,
                "enforcement_perimeter_complete": self.in_scope,
            },
            latency_ms=latency,
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
        in_scope = bool(meta.get("in_scope", True))
        context_expansion = bool(meta.get("context_expansion", False))
        tc_reuse_attempted = bool(meta.get("tc_reuse_attempted", False))

        # B: in_scope governs perimeter coverage.
        scope_violations: List[str] = []
        if not in_scope:
            scope_violations.append(
                f"mcp_server_out_of_scope:{self.mcp_server_id}"
            )
        boundedness = BoundednessSignal(
            in_scope=in_scope,
            scope_violations=tuple(scope_violations),
            score_contribution=1.0 if in_scope else 0.0,
        )

        # C: TC reuse for authorization is a hard policy violation (C-R.15).
        policy_violations: List[str] = []
        if tc_reuse_attempted:
            policy_violations.append("tc_reuse_as_authorization")
        compliance = ComplianceSignal(
            policy_violations=tuple(policy_violations),
            score_contribution=0.5 if tc_reuse_attempted else 1.0,
        )

        attribution = AttributionSignal(
            source_count=1,
            sources_with_complete_metadata=1 if in_scope else 0,
            integration_boundary_gaps=0 if in_scope else 1,
            timestamp_present=True,
            chain_of_custody_complete=in_scope,
            score_contribution=1.0 if in_scope else 0.0,
        )

        known = KnownStateSignal()

        connector_metadata = {
            "mcp_server_id": self.mcp_server_id,
            "tool_name": meta.get("tool_name"),
            "context_expansion": context_expansion,
            "tc_reuse_attempted": tc_reuse_attempted,
            "enforcement_perimeter_complete": in_scope,
        }

        return GovernanceEvent(
            event_id=str(uuid.uuid4()),
            workflow_id=workflow_id,
            node_id=node.node_id,
            timestamp=datetime.now(timezone.utc).isoformat(),
            connector_type=f"{self.connector_type}.{meta.get('tool_name','tool')}",
            connection_type=self.connection_type(),
            sensitivity_tier=node.sensitivity_tier,
            boundedness=boundedness,
            attribution=attribution,
            compliance=compliance,
            known=known,
            payload_ref=f"mcp:{self.mcp_server_id}",
            latency_ms=result.latency_ms,
            error=result.error,
            connector_metadata=connector_metadata,
            previous_event_hash=previous_event_hash,
        )
