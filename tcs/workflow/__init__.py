"""
tcs.workflow
============

Phase 4 — Governed workflow graph and connector contract.

This package implements the foundation of the Phase 4 architecture:
TCS governs the *path*, not just the prompt. Every enterprise AI
workflow is modeled as a graph of nodes (LLM, RAG, API, MCP, agent
handoffs) connected by edges. Each connector emits normalized
governance evidence; the GCA compiles that evidence into a single
TISInput; the TIS engine scores it; the decision engine routes it.

This package is additive to Phase 1/2/3. The existing engine,
policy profiles, persistence, and routes are untouched. The new
workflow path is gated behind the ``TCS_WORKFLOW_TRACE_ENABLED``
environment variable and is opt-in until the validation harness
proves parity with the legacy path.

Schema versions are exposed at the package root so they can be
written into every Trust Certificate for audit reconstruction.
"""

from __future__ import annotations

from tcs.workflow.events import (
    EVENT_SCHEMA_VERSION,
    AttributionSignal,
    BoundednessSignal,
    ComplianceSignal,
    GovernanceEvent,
    KnownStateSignal,
    SensitivityTier,
)
from tcs.workflow.trace import (
    TRACE_SCHEMA_VERSION,
    GovernedEdge,
    GovernedNode,
    GovernedWorkflowTrace,
    NodeType,
)
from tcs.workflow.connector import (
    CONNECTOR_CONTRACT_VERSION,
    ConnectorRequest,
    ConnectorResult,
    GovernedConnector,
)
from tcs.workflow.orchestrator import WorkflowOrchestrator

__all__ = [
    # Schema versions
    "TRACE_SCHEMA_VERSION",
    "EVENT_SCHEMA_VERSION",
    "CONNECTOR_CONTRACT_VERSION",
    # Trace model
    "GovernedWorkflowTrace",
    "GovernedNode",
    "GovernedEdge",
    "NodeType",
    # Event model
    "GovernanceEvent",
    "BoundednessSignal",
    "AttributionSignal",
    "ComplianceSignal",
    "KnownStateSignal",
    "SensitivityTier",
    # Connector contract
    "GovernedConnector",
    "ConnectorRequest",
    "ConnectorResult",
    # Orchestrator
    "WorkflowOrchestrator",
]
