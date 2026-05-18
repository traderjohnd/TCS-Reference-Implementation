"""
tcs.workflow.orchestrator
==========================

Declarative workflow executor.

The orchestrator takes a declared workflow (a list of nodes with
their connectors) and an initial query, executes each node in order,
attaches each emitted GovernanceEvent to its node, and wires up the
hash chain pointers between events. Returns a fully-populated
GovernedWorkflowTrace ready for GCA consumption.

Slice 1 supports linear workflows only (run nodes in declared order).
Slice 2+ will support fan-out / fan-in graphs using the GovernedEdge
set on the trace.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from tcs.workflow.connector import (
    ConnectorRequest,
    ConnectorResult,
    GovernedConnector,
)
from tcs.workflow.events import GovernanceEvent
from tcs.workflow.trace import GovernedNode, GovernedWorkflowTrace


@dataclass
class WorkflowStep:
    """
    Declared workflow step: a node + the connector that produces it.

    ``params`` are passed through to the connector at invoke time.
    ``context_key`` is the name under which this step's output is
    stored in ``workflow_context`` for downstream connectors to read.
    Defaults to the node_id.
    """
    node: GovernedNode
    connector: GovernedConnector
    params: Dict[str, Any] = None
    context_key: Optional[str] = None

    def __post_init__(self) -> None:
        if self.params is None:
            self.params = {}
        if self.context_key is None:
            self.context_key = self.node.node_id


class WorkflowOrchestrator:
    """
    Declarative orchestrator for a linear workflow.

    Usage::

        steps = [
            WorkflowStep(node=rag_node, connector=rag_connector),
            WorkflowStep(node=llm_node, connector=llm_connector),
        ]
        trace = orchestrator.execute(
            steps=steps,
            query="What are Reg BI compliance requirements?",
            base_profile_id="fin-r3-a4-ct4",
        )
        # trace.final_output is the LLM response (or None on failure)
        # trace.events() yields the per-node GovernanceEvents
        # GCA consumes trace to produce TISInput
    """

    def execute(
        self,
        *,
        steps: List[WorkflowStep],
        query: str,
        base_profile_id: str,
        user_identity: Optional[Dict[str, Any]] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> GovernedWorkflowTrace:
        # Stash the query on the trace metadata so the GCA's risk
        # classifier can inspect it at assemble time. Without this the
        # classifier would have to scan each event for query_text, which
        # is fragile.
        trace_metadata = dict(metadata or {})
        trace_metadata.setdefault("query", query)
        trace = GovernedWorkflowTrace.new(
            base_profile_id=base_profile_id,
            user_identity=user_identity,
            metadata=trace_metadata,
        )
        workflow_context: Dict[str, Any] = {}
        previous_event_hash: Optional[str] = None
        final_output: Optional[str] = None

        for step in steps:
            trace.add_node(step.node)

            request = ConnectorRequest(
                query=query,
                workflow_context=dict(workflow_context),
                params=step.params,
            )

            t0 = time.perf_counter()
            try:
                result = step.connector.invoke(request)
            except Exception as exc:
                # A connector that raised instead of populating
                # error is a contract violation — but we recover by
                # synthesizing a failed result so the trace stays
                # well-formed for audit.
                result = ConnectorResult(
                    payload=None,
                    output_text=None,
                    raw_metadata={"exception_type": type(exc).__name__},
                    latency_ms=round((time.perf_counter() - t0) * 1000, 2),
                    error=str(exc),
                )

            # Stash output for downstream nodes.
            workflow_context[step.context_key] = {
                "payload": result.payload,
                "output_text": result.output_text,
            }
            # Stash payload on the node for non-governance consumers
            # (route layers, UI). The GCA never reads node.payload —
            # only events drive governance decisions.
            step.node.payload = result.payload
            if result.output_text is not None:
                final_output = result.output_text

            # Connector emits its evidence.
            event = step.connector.to_governance_event(
                result,
                step.node,
                workflow_id=trace.workflow_id,
                previous_event_hash=previous_event_hash,
            )

            # Orchestrator computes the hash and wires the chain.
            event = self._seal_event(event, previous_event_hash)
            trace.attach_event(step.node.node_id, event)
            previous_event_hash = event.event_hash

        trace.finalize(final_output)
        return trace

    def _seal_event(
        self,
        event: GovernanceEvent,
        previous_event_hash: Optional[str],
    ) -> GovernanceEvent:
        """
        Compute event_hash and pin previous_event_hash.

        Connectors are required to return events with both hash fields
        unset (or set to the carried-in previous_event_hash). The
        orchestrator authoritatively sets them so the chain stays
        well-formed regardless of connector implementation details.
        """
        # Replace previous_event_hash with our authoritative value.
        unsealed = replace(
            event,
            previous_event_hash=previous_event_hash,
            event_hash=None,
        )
        return replace(unsealed, event_hash=unsealed.compute_hash())
