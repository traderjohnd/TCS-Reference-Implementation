"""
tcs.workflow.trace
===================

GovernedWorkflowTrace, GovernedNode, GovernedEdge.

A workflow trace is the complete record of a single AI-mediated
action: which connectors ran, in what order, with what evidence.
The trace is the input to the GCA, which compiles it into a TISInput
for engine scoring.

The trace is the core Phase 4 object. The TIS engine still receives
a TISInput; it does not know workflows exist. This preserves the
clean separation enforced since Phase 1: the engine is pure math,
domain-agnostic, and free of workflow concerns.

Design principles
-----------------

1. **Declarative now, emergent-ready later.** A trace is built from
   a declared workflow definition in Slice 1. The data shape (nodes,
   edges, append-only events) supports emergent capture in Phase 5
   without re-architecture.

2. **Append-only event log.** Once an event is added to a node, it
   is not mutated. The orchestrator computes hash chain pointers
   between events at append time.

3. **Schema versioning.** ``TRACE_SCHEMA_VERSION`` is written into
   every trace for forward compatibility.

4. **Connector type vs node type.** ``NodeType`` is the workflow
   role (LLM, RAG, API, MCP, AGENT, DATABASE, HUMAN_INPUT). The
   per-event ``connection_type`` (CT-1..CT-13) is what drives
   policy resolution. Both are recorded because they answer
   different audit questions.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional

from tcs.workflow.events import GovernanceEvent

TRACE_SCHEMA_VERSION = "1.0"


# --------------------------------------------------------------------------- #
# Node types — the workflow role of each step                                  #
# --------------------------------------------------------------------------- #

class NodeType(str, Enum):
    """
    The role a node plays in a workflow.

    This is distinct from ``connection_type`` (CT-1..CT-13) which
    drives policy resolution. For example, an "LLM" node typically
    has ``connection_type="CT-1"`` (API) but could be CT-11 if the
    workflow treats it as AI-generated attribution.

    TIS_EVALUATION_MARKER is a sentinel node type that declares the
    conceptual point at which TIS was evaluated in the workflow.
    Nodes AFTER a marker are "post-evaluation"; if any post-marker
    node carries context-expansion evidence (MCP retrieval after
    eval, additional context fetch, etc.), the GCA must invalidate
    the workflow per C-R.14 and require re-evaluation.
    """
    LLM = "llm"
    RAG = "rag"
    API = "api"
    MCP = "mcp"
    AGENT = "agent"
    DATABASE = "database"
    HUMAN_INPUT = "human_input"
    TIS_EVALUATION_MARKER = "tis_evaluation_marker"


# --------------------------------------------------------------------------- #
# GovernedNode — a single step in the workflow                                 #
# --------------------------------------------------------------------------- #

@dataclass
class GovernedNode:
    """
    One step in a governed workflow.

    A node carries its declarative configuration (type, connection
    type, sensitivity tier, name) and after execution, both a
    populated ``event`` field with the emitted GovernanceEvent and
    an optional ``payload`` field with the raw connector output.

    Authority split (important):
        - ``event`` is the AUTHORITATIVE governance record. The GCA
          reads only events when compiling a TISInput. Events are
          immutable once attached.
        - ``payload`` is a convenience for UI / inspection / debug.
          It carries whatever raw shape the connector produced
          (retrieved chunks, API response, etc.). It is NEVER read
          by the GCA for governance scoring — only by route layers
          that need to surface raw artifacts to the caller.

    This split keeps the audit story clean: governance decisions are
    derived only from the immutable event log, while operational
    consumers can still access the raw artifacts via the trace
    without re-running expensive operations.

    Mutability note: ``event`` is set exactly once by the orchestrator
    after the connector returns. Re-setting it is a programming error.
    """
    node_id: str
    name: str                       # human-readable label
    node_type: NodeType
    connection_type: str            # "CT-1".."CT-13"
    sensitivity_tier: str           # T0-T3
    config: Dict[str, Any] = field(default_factory=dict)
    event: Optional[GovernanceEvent] = None
    payload: Any = None             # raw connector output for inspection only

    def to_dict(self) -> dict:
        # payload deliberately not serialized — may contain large or
        # binary blobs. Callers that need it should read node.payload
        # directly.
        return {
            "node_id": self.node_id,
            "name": self.name,
            "node_type": self.node_type.value,
            "connection_type": self.connection_type,
            "sensitivity_tier": self.sensitivity_tier,
            "config": self.config,
            "event": self.event.to_dict() if self.event else None,
        }


# --------------------------------------------------------------------------- #
# GovernedEdge — a connection between two nodes                                #
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class GovernedEdge:
    """
    A directional dependency from one node to another.

    ``edge_type`` describes what flows along the edge:
        - "data_flow"   — output of from_node feeds into to_node
        - "handoff"     — agent handoff (CT-8 chain)
        - "tool_call"   — to_node is invoked by from_node as a tool
        - "reference"   — from_node refers to to_node for context
    """
    from_node_id: str
    to_node_id: str
    edge_type: str = "data_flow"


# --------------------------------------------------------------------------- #
# GovernedWorkflowTrace — the complete record of a workflow execution         #
# --------------------------------------------------------------------------- #

@dataclass
class GovernedWorkflowTrace:
    """
    Complete record of a single workflow execution.

    A trace is created at workflow start, has nodes added in
    declared order, has events attached as each connector runs,
    and is finalized when ``final_output`` is set.

    The GCA consumes a fully-executed trace and produces a TISInput.

    Fields
    ------
    workflow_id
        UUID4 — unique per execution.
    user_identity
        Authenticated principal who initiated the workflow.
        Stub for Slice 1 (real identity in later slices).
    base_profile_id
        Policy profile ID to use for governance (e.g.
        ``fin-r3-a4-ct4``). Resolved against connection types
        observed in the trace by the GCA.
    nodes
        Ordered list of GovernedNode. Append-only after creation.
    edges
        Set of GovernedEdge describing dependencies. Optional in
        Slice 1 (the orchestrator runs nodes in list order); used
        in Slice 2+ when workflows fan out / fan in.
    final_output
        The user-visible string output (e.g. LLM response). None
        until the workflow completes.
    created_at, completed_at
        ISO-8601 timestamps.
    metadata
        Free-form bag for non-governance context (request_id,
        feature flags active, etc.).
    schema_version
        Pinned to ``TRACE_SCHEMA_VERSION`` at construction.
    """
    workflow_id: str
    user_identity: Dict[str, Any]
    base_profile_id: str
    nodes: List[GovernedNode] = field(default_factory=list)
    edges: List[GovernedEdge] = field(default_factory=list)
    final_output: Optional[str] = None
    created_at: str = ""
    completed_at: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)
    schema_version: str = TRACE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if not self.created_at:
            self.created_at = datetime.now(timezone.utc).isoformat()

    @classmethod
    def new(
        cls,
        *,
        base_profile_id: str,
        user_identity: Optional[Dict[str, Any]] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> "GovernedWorkflowTrace":
        """Construct a fresh trace with a UUID4 workflow_id."""
        return cls(
            workflow_id=str(uuid.uuid4()),
            user_identity=user_identity or {},
            base_profile_id=base_profile_id,
            metadata=metadata or {},
        )

    def add_node(self, node: GovernedNode) -> None:
        if node.event is not None:
            raise ValueError(
                f"Cannot add node {node.node_id!r} with a pre-attached "
                "event. Events are populated by the orchestrator after "
                "the connector runs."
            )
        self.nodes.append(node)

    def add_edge(self, edge: GovernedEdge) -> None:
        self.edges.append(edge)

    def attach_event(self, node_id: str, event: GovernanceEvent) -> None:
        """
        Attach the connector's emitted event to its node.

        Called by the orchestrator. Idempotent failure: setting an
        event twice raises, since events are append-only.
        """
        node = self.get_node(node_id)
        if node.event is not None:
            raise ValueError(
                f"Node {node_id!r} already has an attached event. "
                "Events are append-only."
            )
        node.event = event

    def get_node(self, node_id: str) -> GovernedNode:
        for n in self.nodes:
            if n.node_id == node_id:
                return n
        raise KeyError(f"No node with id {node_id!r} in workflow {self.workflow_id}")

    def events(self) -> List[GovernanceEvent]:
        """All emitted events in node order. Excludes nodes that have not run."""
        return [n.event for n in self.nodes if n.event is not None]

    def finalize(self, final_output: Optional[str]) -> None:
        self.final_output = final_output
        self.completed_at = datetime.now(timezone.utc).isoformat()

    def to_dict(self) -> dict:
        return {
            "workflow_id": self.workflow_id,
            "user_identity": self.user_identity,
            "base_profile_id": self.base_profile_id,
            "nodes": [n.to_dict() for n in self.nodes],
            "edges": [
                {
                    "from_node_id": e.from_node_id,
                    "to_node_id": e.to_node_id,
                    "edge_type": e.edge_type,
                }
                for e in self.edges
            ],
            "final_output": self.final_output,
            "created_at": self.created_at,
            "completed_at": self.completed_at,
            "metadata": self.metadata,
            "schema_version": self.schema_version,
        }
