"""
tcs.workflow.connector
=======================

The GovernedConnector contract.

Every enterprise connector — LLM, RAG, API, MCP, agent chain,
database — implements the same three-method contract. This is the
adapter pattern: the workflow orchestrator does not know whether
it is calling OpenAI, Pinecone, an internal REST API, or an MCP
server. It calls ``invoke()``, asks the connector to emit a
GovernanceEvent, and appends both to the trace.

Contract
--------

1. ``invoke(request) -> ConnectorResult``
   Execute the connector's real work (API call, retrieval, etc.).
   Return a payload-bearing result. Connectors are responsible for
   their own error handling; failures should populate
   ``ConnectorResult.error`` rather than raising.

2. ``to_governance_event(result, node, workflow_id, previous_event_hash) -> GovernanceEvent``
   Translate the connector's raw result into normalized BACK
   evidence. This is where attribution gaps, similarity scores,
   prohibited patterns, etc. become BoundednessSignal /
   AttributionSignal / ComplianceSignal / KnownStateSignal.

3. ``connection_type() -> str``
   The CT-1..CT-13 classifier for this connector. Drives policy
   resolution in the GCA.

Design principles
-----------------

1. **One event per invocation.** A connector emits exactly one
   GovernanceEvent per call. Multi-step operations should be
   modeled as multiple nodes, not as one event with multiple
   sub-results.

2. **No scoring in the connector.** Connectors emit *evidence*
   (counts, presence flags, raw similarity). The GCA does the
   scoring with policy context. This keeps connectors policy-free
   and the math centralized.

3. **Schema versioning.** ``CONNECTOR_CONTRACT_VERSION`` is exposed
   so the orchestrator can detect contract drift if a connector is
   loaded from a different schema generation.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

from tcs.workflow.events import GovernanceEvent
from tcs.workflow.trace import GovernedNode

CONNECTOR_CONTRACT_VERSION = "1.0"


# --------------------------------------------------------------------------- #
# ConnectorRequest / ConnectorResult — the wire shapes                         #
# --------------------------------------------------------------------------- #

@dataclass
class ConnectorRequest:
    """
    Input to a connector's ``invoke()``.

    Carries the user query, accumulated workflow context (outputs
    from prior nodes keyed by node_id), and per-connector params
    from the node config. Connectors should treat this as read-only.
    """
    query: str
    workflow_context: Dict[str, Any] = field(default_factory=dict)
    params: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ConnectorResult:
    """
    Output from a connector's ``invoke()``.

    ``payload`` is whatever raw shape the connector produces (string,
    list of chunks, dict, etc.). ``output_text`` is the user-visible
    string if this connector terminates the workflow (LLM nodes
    populate this; RAG nodes typically do not).

    ``raw_metadata`` is connector-specific bookkeeping that the
    ``to_governance_event()`` method will read to build the BACK
    signals.
    """
    payload: Any
    output_text: Optional[str] = None
    raw_metadata: Dict[str, Any] = field(default_factory=dict)
    latency_ms: float = 0.0
    error: Optional[str] = None


# --------------------------------------------------------------------------- #
# GovernedConnector — the abstract contract                                    #
# --------------------------------------------------------------------------- #

class GovernedConnector(ABC):
    """
    Abstract base for every connector in a governed workflow.

    Concrete connectors live under ``tcs/workflow/connectors/``.
    Slice 1 ships LLM and RAG connectors; Slice 2 adds API, MCP,
    and agent-chain connectors.

    Subclasses must declare:
        - ``connection_type()`` returning a CT-X identifier
        - ``connector_type`` class attribute (a short label like
          "llm.openai" used in telemetry; defaults to the class
          name lowercased)
        - ``invoke(request)`` doing the real work
        - ``to_governance_event(result, node, workflow_id, previous_event_hash)``
          translating result to evidence
    """

    connector_type: str = ""
    contract_version: str = CONNECTOR_CONTRACT_VERSION

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        if not cls.connector_type:
            cls.connector_type = cls.__name__.lower()

    @abstractmethod
    def connection_type(self) -> str:
        """
        Return the CT identifier this connector falls under.

        See TCS_SPEC.md §18 for the canonical list. Examples:
            - CT-1   API (REST, gRPC, SDK)
            - CT-4   Vector DB / RAG
            - CT-8   Agent chain
            - CT-11  AI-generated attribution
        """

    @abstractmethod
    def invoke(self, request: ConnectorRequest) -> ConnectorResult:
        """Execute the connector's real work and return a result."""

    @abstractmethod
    def to_governance_event(
        self,
        result: ConnectorResult,
        node: GovernedNode,
        *,
        workflow_id: str,
        previous_event_hash: Optional[str] = None,
    ) -> GovernanceEvent:
        """
        Translate ``result`` into a GovernanceEvent.

        The event_hash is computed by the orchestrator after this
        returns; subclasses should leave ``event_hash=None`` and
        pass ``previous_event_hash`` through unchanged for the
        orchestrator to wire up the chain.
        """
