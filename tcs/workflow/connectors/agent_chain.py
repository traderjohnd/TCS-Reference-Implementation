"""
tcs.workflow.connectors.agent_chain
====================================

Agent chain connector — CT-8.

The compound-trust connector. Proves that trust degrades or
compounds across a multi-agent pipeline. Each agent contributes a
per-hop calibration score K_i; the chain reliability is the
product, and the engine scores against ``K_chain`` (not
``U_chain`` — U_chain is a derived uncertainty intermediate per
the BACK model).

Slice 2 scope
-------------

Accepts pre-computed per-agent K scores. Real multi-LLM execution
(where each agent actually runs and produces its own K_i) is
deferred to Phase 5. The goal here is to validate compound trust
behavior, not build full multi-agent orchestration.

Connection type
---------------

CT-8 (agent chain). Triggers the chain math in the GCA (added in
Slice 1): when the dominant CT is CT-8, the GCA derives
``K_chain = product(K_i)`` and feeds it as the K dimension.
``U_chain = 1 - K_chain`` surfaces only in audit metadata.

CT-11 (AI-generated attribution) is explicitly NOT chained — see
Slice 1 / the BACK migration. If AI-generated content appears
inside an agent chain workflow, the workflow graph captures both
contexts; the chain math belongs to the CT-8 node, not to the
CT-11 nodes.
"""

from __future__ import annotations

import time
import uuid
from datetime import datetime, timezone
from typing import List, Optional, Sequence

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


class AgentChainConnector(GovernedConnector):
    """
    CT-8 agent chain connector.

    Parameters
    ----------
    per_agent_K_scores
        Per-hop K_i calibration scores (in [0, 1]). Order is the
        handoff sequence: first element = first agent in the chain.
    agent_roles
        Optional human-readable labels for each agent (same length
        as ``per_agent_K_scores``).
    downstream_tc_references
        Optional list of upstream TC IDs to record for audit. These
        are AUDIT references only — C-R.15 prohibits treating them
        as authorization. The connector records them in
        connector_metadata; the GCA / TC writer must label
        reference_type="audit_reference", never "authorization".
    """

    connector_type = "agent_chain"

    def __init__(
        self,
        *,
        per_agent_K_scores: Sequence[float],
        agent_roles: Optional[Sequence[str]] = None,
        downstream_tc_references: Optional[Sequence[str]] = None,
    ) -> None:
        if not per_agent_K_scores:
            raise ValueError(
                "AgentChainConnector requires at least one per-agent K score"
            )
        for k in per_agent_K_scores:
            if not 0.0 <= float(k) <= 1.0:
                raise ValueError(
                    f"per_agent_K_scores must be in [0, 1]; got {k}"
                )
        self.per_agent_K_scores: List[float] = [float(k) for k in per_agent_K_scores]
        if agent_roles is None:
            self.agent_roles = [f"agent_{i+1}" for i in range(len(self.per_agent_K_scores))]
        else:
            if len(agent_roles) != len(self.per_agent_K_scores):
                raise ValueError(
                    "agent_roles must have same length as per_agent_K_scores"
                )
            self.agent_roles = list(agent_roles)
        self.downstream_tc_references = list(downstream_tc_references or [])

    def connection_type(self) -> str:
        return "CT-8"

    def invoke(self, request: ConnectorRequest) -> ConnectorResult:
        # Slice 2 is pre-computed: no real agent execution.
        t0 = time.perf_counter()
        product = 1.0
        for k in self.per_agent_K_scores:
            product *= k
        k_chain = round(product, 4)
        u_chain = round(1.0 - product, 4)
        latency = round((time.perf_counter() - t0) * 1000, 2)

        return ConnectorResult(
            payload={
                "per_agent_K_scores": list(self.per_agent_K_scores),
                "K_chain": k_chain,
                "U_chain_derived": u_chain,
                "agent_roles": list(self.agent_roles),
                "handoff_sequence": list(zip(self.agent_roles[:-1], self.agent_roles[1:])),
            },
            output_text=None,
            raw_metadata={
                "agent_count": len(self.per_agent_K_scores),
                "K_chain": k_chain,
                "U_chain_derived": u_chain,
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
        # The KnownStateSignal carries the per-hop K scores in
        # chain_k_scores. The GCA detects CT-8 and uses these to
        # compute K_chain = product(K_i) — that derived value becomes
        # the K dimension fed to the engine.
        k_min = min(self.per_agent_K_scores)
        known = KnownStateSignal(
            confidence_calibrated=all(k >= 0.80 for k in self.per_agent_K_scores),
            score_contribution=k_min,  # min contribution if the GCA used min-agg
            chain_k_scores=tuple(self.per_agent_K_scores),
        )

        # B / A / C are nominally clean — the chain by itself does not
        # introduce scope / attribution / compliance violations. Other
        # nodes in the workflow (LLM, RAG, API) handle those.
        boundedness = BoundednessSignal()
        attribution = AttributionSignal(
            source_count=len(self.per_agent_K_scores),
            sources_with_complete_metadata=len(self.per_agent_K_scores),
            timestamp_present=True,
            chain_of_custody_complete=True,
        )
        compliance = ComplianceSignal()

        connector_metadata = {
            "agent_count": len(self.per_agent_K_scores),
            "agent_roles": list(self.agent_roles),
            "K_chain": result.raw_metadata.get("K_chain"),
            "U_chain_derived": result.raw_metadata.get("U_chain_derived"),
            "downstream_tc_references": [
                {"certificate_id": tc_id, "reference_type": "audit_reference"}
                for tc_id in self.downstream_tc_references
            ],
        }

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
            payload_ref=f"chain:{len(self.per_agent_K_scores)}-hops",
            latency_ms=result.latency_ms,
            connector_metadata=connector_metadata,
            previous_event_hash=previous_event_hash,
        )
