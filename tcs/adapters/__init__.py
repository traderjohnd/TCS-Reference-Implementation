"""
tcs.adapters
============

Adapters translate upstream workflow outputs into ``InterceptedRequest``
objects that the enforcement controller can govern. Each adapter is a
thin, pure translator — it never computes trust, never makes decisions,
and never touches the persistence layer.

Current adapters:

    rag_adapter   — RAG pipeline output (chunks + query + candidate answer)
                    mapped to the CT-4 context shape expected by
                    ``tcs.governed_context.assemble_context_v2``.

Future Phase 3 adapters:
    agent_chain_adapter   — CT-8 multi-agent pipelines
    tool_call_adapter     — CT-1 tool / API workflows
    document_ingest_adapter — CT-3 document pipelines
"""

from tcs.adapters.rag_adapter import (
    InterceptedRequest,
    RAGAdapter,
    RAGChunk,
    RAGOutput,
    SIMILARITY_FLOOR,
    adapt,
)

__all__ = [
    "InterceptedRequest",
    "RAGAdapter",
    "RAGChunk",
    "RAGOutput",
    "SIMILARITY_FLOOR",
    "adapt",
]
