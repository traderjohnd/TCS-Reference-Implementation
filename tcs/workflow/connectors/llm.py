"""
tcs.workflow.connectors.llm
============================

LLM provider connector. Wraps OpenAI, Anthropic, or the Mock
provider behind the GovernedConnector contract.

Connection type
---------------

CT-1 (API). Per TCS_SPEC.md §18 and the white paper, an LLM call
is an API connection. If a future workflow treats an LLM output as
AI-generated attribution (e.g. citing a model's prior output as
ground truth), that node should use ``connection_type="CT-11"``
explicitly. The LLM connector itself does not assume CT-11.

Evidence emitted
----------------

The LLM connector knows the least about the upstream data. Its
job is to call the model and report what came back. Most BACK
signals come from upstream RAG / API connectors that handled the
real provenance. The LLM connector contributes:

    B: in_scope = True (no scope claim is made)
    A: timestamp_present = True (own call timestamp)
    C: c3_violation detection on the response text (response
       injection scan against a small phrase list — same patterns
       the existing pipeline.py InjectionScanner uses)
    K: confidence_calibrated = True by default; if the response
       is empty or errored, K is reduced. Real calibration scoring
       comes from the GCA which sees the full trace.

This is intentionally light. Connectors emit *evidence they can
observe*; they do not invent dimension scores.
"""

from __future__ import annotations

import time
import uuid
from datetime import datetime, timezone
from typing import Any, List, Optional, Tuple

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


# Prompt-injection / boundary-violation phrases the LLM connector
# scans for in the model's response text. These are kept LOCAL to the
# connector because they describe what the LLM said — they would
# trigger even if the user's query is benign but the LLM emits one
# of these phrases (a model misbehavior signal, not a user-risk
# signal).
#
# Domain-level risk patterns (drug interactions, clinical dosing
# during pregnancy, restricted instrument recommendations, prompt-
# injection ATTEMPTS in the user's query, etc.) live in
# tcs/governance/scenario_rules.py — they are evaluated at the GCA
# layer against the query + active policy, and apply uniformly across
# all connectors and providers. That keeps governance rules
# centralized, discoverable, and testable without sprinkling pattern
# lists across every connector file.
_INJECTION_RESPONSE_PATTERNS: Tuple[str, ...] = (
    "ignore policy",
    "override compliance",
    "bypass governance",
    "ignore the above",
    "disregard the rules",
)


class LLMConnector(GovernedConnector):
    """
    Adapter for a request-scoped LLM provider.

    The provider is any object exposing ``generate(query, context_chunks) -> str``,
    matching the convention used by ``demos.governed_rag.pipeline``.
    The provider is injected at construction so the connector stays
    decoupled from the OpenAI/Anthropic SDK details.

    Parameters
    ----------
    provider
        Object with a ``generate(query, context) -> str`` method.
    provider_name
        Free-form label, e.g. "openai", "anthropic", "mock".
    model
        Display name of the model, e.g. "gpt-5.5 (Instant)".
    context_key
        Which key in ``workflow_context`` carries the retrieved
        chunks. Defaults to ``"rag"``; the orchestrator populates
        this key when a RAG step precedes the LLM step.
    """

    connector_type = "llm"

    def __init__(
        self,
        *,
        provider: Any,
        provider_name: str,
        model: str,
        context_key: str = "rag",
    ) -> None:
        self.provider = provider
        self.provider_name = provider_name
        self.model = model
        self.context_key = context_key

    def connection_type(self) -> str:
        return "CT-1"

    def invoke(self, request: ConnectorRequest) -> ConnectorResult:
        t0 = time.perf_counter()
        context_chunks: List[str] = []
        ctx = request.workflow_context.get(self.context_key)
        if isinstance(ctx, dict):
            payload = ctx.get("payload")
            if isinstance(payload, list):
                for c in payload:
                    if isinstance(c, dict) and "content" in c:
                        context_chunks.append(str(c["content"]))

        try:
            response_text = self.provider.generate(request.query, context_chunks)
        except Exception as exc:
            return ConnectorResult(
                payload=None,
                output_text=None,
                raw_metadata={
                    "provider": self.provider_name,
                    "model": self.model,
                    "exception_type": type(exc).__name__,
                },
                latency_ms=round((time.perf_counter() - t0) * 1000, 2),
                error=str(exc),
            )

        return ConnectorResult(
            payload={"response_text": response_text},
            output_text=response_text,
            raw_metadata={
                "provider": self.provider_name,
                "model": self.model,
                "context_chunk_count": len(context_chunks),
                # Carry the query into raw_metadata so to_governance_event
                # can scan it for prohibited-action / drug-interaction
                # patterns (C3 detection covers BOTH the user's request
                # and the model's response).
                "query_text": request.query,
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
        response_text = result.output_text or ""
        response_lower = response_text.lower()

        # The connector scans the MODEL RESPONSE only for prompt-
        # injection / boundary-violation phrases the LLM emitted. This
        # is a model-misbehavior signal. Query-level and domain-level
        # risk patterns (drug interactions, dosing during pregnancy,
        # restricted-instrument recommendations, prompt-injection
        # ATTEMPTS in the user query, etc.) are handled by the GCA
        # risk classifier in tcs/governance/, not by the connector.
        c3_pattern: Optional[str] = None
        for pat in _INJECTION_RESPONSE_PATTERNS:
            if pat in response_lower:
                c3_pattern = pat
                break
        c3_violation = c3_pattern is not None

        compliance = ComplianceSignal(
            c3_violation=c3_violation,
            c3_pattern=c3_pattern,
            score_contribution=0.0 if c3_violation else 1.0,
        )

        # K: full calibration if the response came through; reduced
        # if the call errored. The GCA will apply policy-aware scoring;
        # the connector only reports what it directly observed.
        if result.error:
            known = KnownStateSignal(
                confidence_calibrated=False,
                score_contribution=0.0,
            )
        else:
            known = KnownStateSignal(
                confidence_calibrated=True,
                score_contribution=1.0,
            )

        # B: LLM does not claim scope authority. Defaults are fine.
        boundedness = BoundednessSignal()

        # A: own-call timestamp present; no external attribution
        # claims (those belong to RAG / API connectors upstream).
        attribution = AttributionSignal(
            source_count=int(result.raw_metadata.get("context_chunk_count", 0) or 0),
            sources_with_complete_metadata=int(
                result.raw_metadata.get("context_chunk_count", 0) or 0
            ),
            timestamp_present=True,
        )

        return GovernanceEvent(
            event_id=str(uuid.uuid4()),
            workflow_id=workflow_id,
            node_id=node.node_id,
            timestamp=datetime.now(timezone.utc).isoformat(),
            connector_type=f"{self.connector_type}.{self.provider_name}",
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
