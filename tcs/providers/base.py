"""
tcs.providers.base
==================

Provider-neutral live-LLM contract (demo-live branch, Commit 2).

Every live provider (OpenAI, Anthropic Claude, lower-capability or
future cloud/local models) and the deterministic mock implement the
same result shape, so provider-specific payloads are NEVER forced into
the governance engine: the engine and connectors consume normalized
fields only.

Contract:

    provider.generate(query, context) -> str          # legacy connector API
    provider.generate_result(query, context) -> ProviderResult
    provider.last_result                              # ProviderResult of the
                                                      # most recent call —
                                                      # the workflow LLM
                                                      # connector lifts its
                                                      # normalized provenance
                                                      # into the governed
                                                      # trace event.

API keys are constructor arguments held only on the request-scoped
provider object — never persisted, never logged, never serialized into
results or provenance.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class ProviderResult:
    """Normalized result of one live-provider call.

    Only non-secret, provider-neutral fields. ``provenance`` carries
    normalized metadata (never raw provider payloads, never keys).
    """
    provider: str                       # provider identifier ("openai", ...)
    model: str                          # model identifier actually requested
    content: str                        # response text ("" on error)
    request_id: Optional[str] = None    # provider request id when available
    usage: Dict[str, Any] = field(default_factory=dict)
    tool_actions: List[Dict[str, Any]] = field(default_factory=list)
    error: Optional[str] = None         # provider error detail (no secrets)
    error_category: Optional[str] = None  # e.g. "empty_content"
    latency_ms: Optional[float] = None
    finish_status: str = "unknown"      # stop | length | error | ...
    provenance: Dict[str, Any] = field(default_factory=dict)

    def provenance_summary(self) -> Dict[str, Any]:
        """Bounded normalized provenance for the governed trace —
        content is deliberately excluded (it is the governed output
        itself, recorded elsewhere)."""
        return {
            "provider": self.provider,
            "model": self.model,
            "request_id": self.request_id,
            "usage": dict(self.usage),
            "tool_actions": list(self.tool_actions),
            "finish_status": self.finish_status,
            "provider_latency_ms": self.latency_ms,
            "error": self.error,
            "error_category": self.error_category,
            **{k: v for k, v in self.provenance.items()},
        }


class ProviderError(RuntimeError):
    """A provider-side failure (auth, model availability, network,
    content policy, or a technically-successful response with no usable
    model-generated text). ALWAYS distinct from a governance outcome —
    no Trust Certificate is issued for a provider failure. The message
    must never contain credential material.

    ``category`` classifies the failure ("provider_error" default;
    "empty_content" when the provider returned no usable text — an
    adapter diagnostic is a SYSTEM message, never model output, so it
    rides ``detail``/``result.error`` and is never placed in
    ``ProviderResult.content``). ``result`` optionally carries a
    pre-built error-shaped ProviderResult preserving truthful provider
    telemetry (request id, usage, finish status, bounded tool actions)
    for trace provenance."""

    def __init__(self, provider: str, detail: str,
                 category: str = "provider_error",
                 result: "Optional[ProviderResult]" = None) -> None:
        super().__init__(f"{provider}: {detail}")
        self.provider = provider
        self.detail = detail
        self.category = category
        self.result = result


class BaseLiveProvider:
    """Shared plumbing for live providers: legacy string API +
    ``last_result`` capture. Subclasses implement ``_call``."""

    name: str = "base"

    def __init__(self, model: str) -> None:
        self.model = model
        self.last_result: Optional[ProviderResult] = None

    # -- neutral API ------------------------------------------------------ #

    def generate_result(self, query: str, context: List[str]) -> ProviderResult:
        t0 = time.perf_counter()
        try:
            result = self._call(query, context)
        except ProviderError as exc:
            if exc.result is not None:
                # Adapter supplied an error-shaped result carrying the
                # provider's truthful telemetry — keep it, stamped with
                # latency, as the trace-provenance source.
                exc.result.latency_ms = round(
                    (time.perf_counter() - t0) * 1000, 1)
                self.last_result = exc.result
            else:
                self._record_error(exc, t0)
            raise
        except Exception as exc:  # noqa: BLE001 — normalize to ProviderError
            err = ProviderError(self.name, f"{type(exc).__name__}: {exc}")
            self._record_error(err, t0)
            raise err
        result.latency_ms = round((time.perf_counter() - t0) * 1000, 1)
        self.last_result = result
        return result

    def _record_error(self, exc: ProviderError, t0: float) -> None:
        # Provider failures stay distinct from governance outcomes, but
        # they still produce a normalized last_result so the governed
        # trace can lift truthful error provenance (never a stale
        # success record from an earlier call).
        self.last_result = ProviderResult(
            provider=self.name,
            model=self.model,
            content="",
            error=exc.detail,
            error_category=exc.category,
            latency_ms=round((time.perf_counter() - t0) * 1000, 1),
            finish_status="error",
        )

    # -- legacy connector API (string in/out) ----------------------------- #

    def generate(self, query: str, context: List[str]) -> str:
        return self.generate_result(query, context).content

    # -- subclass hook ----------------------------------------------------- #

    def _call(self, query: str, context: List[str]) -> ProviderResult:
        raise NotImplementedError


def build_messages(query: str, context: List[str]):
    """Shared neutral prompt framing (domain framing belongs in policy
    packs, not connectors). Returns (system_msg, user_msg)."""
    context_text = "\n\n".join(context) if context else ""
    if context_text:
        system_msg = (
            "You are a careful, domain-aware assistant. Answer the "
            "user's question based on the provided context. Cite sources "
            "when possible. If the context does not cover the question, "
            "answer from general knowledge while making clear that the "
            "answer is not grounded in the supplied sources."
        )
        user_msg = f"Context:\n{context_text}\n\nQuestion: {query}"
    else:
        system_msg = (
            "You are a careful, helpful assistant. Answer the user's "
            "question directly and concisely."
        )
        user_msg = query
    return system_msg, user_msg


__all__ = [
    "ProviderResult", "ProviderError", "BaseLiveProvider", "build_messages",
]
