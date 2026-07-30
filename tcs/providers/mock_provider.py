"""
tcs.providers.mock_provider
===========================

The deterministic scripted provider, adapted to the provider-neutral
contract (demo-live branch, Commit 2). Wraps the existing demo
MockProvider so DEMO MODE runs through exactly the same contract as
live providers — with `finish_status: "scripted"` so its results can
never be mistaken for live output at the provenance layer either.
"""

from __future__ import annotations

from typing import List

from tcs.providers.base import BaseLiveProvider, ProviderResult


class ScriptedMockProvider(BaseLiveProvider):
    name = "mock"

    def __init__(self, model: str = "deterministic") -> None:
        super().__init__(model or "deterministic")
        from demos.governed_rag.pipeline import MockProvider
        self._inner = MockProvider()
        self.display_name = self.model

    def _call(self, query: str, context: List[str]) -> ProviderResult:
        content = self._inner.generate(query, context)
        return ProviderResult(
            provider=self.name,
            model=self.model,
            content=content,
            finish_status="scripted",
            provenance={"deterministic": True},
        )


__all__ = ["ScriptedMockProvider"]
