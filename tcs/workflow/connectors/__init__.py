"""
tcs.workflow.connectors
========================

Concrete GovernedConnector implementations.

Slice 1:
    - LLMConnector   — wraps OpenAI / Anthropic / Mock providers (CT-1)
    - RAGConnector   — wraps SimpleVectorStore (CT-4)

Slice 2:
    - APIConnector                  — HTTP tools with allowlist (CT-1)
    - MCPConnector                  — shape-only MCP (CT-1, governance-real)
    - AgentChainConnector           — multi-agent K_chain (CT-8)
    - TISEvaluationMarkerConnector  — sentinel for the eval boundary
"""

from __future__ import annotations

from tcs.workflow.connectors.agent_chain import AgentChainConnector
from tcs.workflow.connectors.api import APIConnector
from tcs.workflow.connectors.llm import LLMConnector
from tcs.workflow.connectors.marker import (
    TISEvaluationMarkerConnector,
    make_marker_node,
)
from tcs.workflow.connectors.mcp import MCPConnector
from tcs.workflow.connectors.rag import RAGConnector

__all__ = [
    "LLMConnector",
    "RAGConnector",
    "APIConnector",
    "MCPConnector",
    "AgentChainConnector",
    "TISEvaluationMarkerConnector",
    "make_marker_node",
]
