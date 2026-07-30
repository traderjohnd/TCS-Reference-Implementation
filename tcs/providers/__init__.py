"""
tcs.providers
=============

Provider-neutral live-LLM layer (demo-live branch).

``build_provider(provider_name, api_key, model)`` is the single
construction point for the governed chat/query path. It returns
``(provider, display_name)`` where ``provider`` implements the
neutral contract in :mod:`tcs.providers.base`:

    generate(query, context) -> str            # legacy connector API
    generate_result(query, context) -> ProviderResult
    last_result -> ProviderResult | None       # trace provenance source

Operating-mode enforcement happens at the ROUTE layer before this is
called — construction of an external provider is only reachable in
LIVE MODE.
"""

from __future__ import annotations

from typing import Optional, Tuple

from tcs.providers.base import (
    BaseLiveProvider,
    ProviderError,
    ProviderResult,
)
from tcs.providers.mock_provider import ScriptedMockProvider


def build_provider(
    provider_name: str,
    api_key: Optional[str],
    model: Optional[str],
) -> Tuple[BaseLiveProvider, str]:
    """Construct the named provider. Raises ValueError for unknown
    provider names and ProviderError for missing credentials, matching
    the route layer's established error handling."""
    name = (provider_name or "mock").strip().lower()

    if name == "mock":
        p = ScriptedMockProvider(model or "deterministic")
        return p, p.display_name

    if name == "openai":
        from tcs.providers.openai_provider import OpenAIProvider
        try:
            p = OpenAIProvider(api_key=api_key or "", model=model or "")
        except ProviderError as exc:
            raise ValueError(exc.detail)
        return p, p.display_name

    if name == "anthropic":
        from tcs.providers.anthropic_provider import AnthropicProvider
        try:
            p = AnthropicProvider(api_key=api_key or "", model=model or "")
        except ProviderError as exc:
            raise ValueError(exc.detail)
        return p, p.display_name

    raise ValueError(f"Unknown provider: {provider_name}")


__all__ = [
    "BaseLiveProvider", "ProviderError", "ProviderResult",
    "ScriptedMockProvider", "build_provider",
]
