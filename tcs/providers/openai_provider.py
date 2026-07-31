"""
tcs.providers.openai_provider
=============================

OpenAI Live LLM through the provider-neutral contract (demo-live
branch, Commit 2). Preserves the existing behavior of the inline
routes_query implementation exactly — neutral prompt framing, GPT-5.x /
reasoning-model token budgets, empty-response diagnostics — while
returning a normalized :class:`ProviderResult`.

Model handling:
    * Catalog display names like "gpt-5.5 (Thinking)" parse into the
      API model id + thinking flag.
    * ANY other string is passed through verbatim as a custom model id —
      the catalog is a convenience, not a hard dependency.
    * An unavailable model is NEVER silently replaced: the provider's
      model-not-found error surfaces verbatim as a ProviderError.

The API key is request-scoped: held on the provider object for the
call, never logged, never serialized, never part of any result.
"""

from __future__ import annotations

from typing import List

from tcs.providers.base import (
    BaseLiveProvider,
    ProviderError,
    ProviderResult,
    build_messages,
)


def parse_openai_model(raw_model: str):
    """'gpt-5.5 (Thinking)' -> ('gpt-5.5', thinking=True). Custom ids
    pass through untouched."""
    raw = (raw_model or "gpt-5.5 (Instant)").strip()
    is_thinking = "(Thinking)" in raw
    api_model = raw.replace(" (Instant)", "").replace(" (Thinking)", "").strip()
    return api_model, is_thinking


class OpenAIProvider(BaseLiveProvider):
    name = "openai"

    def __init__(self, api_key: str, model: str) -> None:
        if not api_key:
            raise ProviderError("openai", "OpenAI API key is required")
        display = (model or "gpt-5.5 (Instant)").strip()
        api_model, is_thinking = parse_openai_model(display)
        super().__init__(api_model)
        self.display_name = display
        self._thinking = is_thinking
        import openai
        self._client = openai.OpenAI(api_key=api_key)

    def _call(self, query: str, context: List[str]) -> ProviderResult:
        system_msg, user_msg = build_messages(query, context)
        messages = [
            {"role": "system", "content": system_msg},
            {"role": "user", "content": user_msg},
        ]
        api_model = self.model
        is_reasoning = api_model.startswith("o3") or api_model.startswith("o4")
        is_new_model = (
            api_model.startswith("gpt-5") or api_model.startswith("gpt-4.1")
        )
        kwargs = {"model": api_model, "messages": messages}
        if is_reasoning or self._thinking or is_new_model:
            # Reasoning/new models silently spend tokens on internal
            # reasoning before visible output — generous budgets so
            # complex questions do not return empty content.
            kwargs["max_completion_tokens"] = (
                4000 if (is_reasoning or self._thinking) else 2000
            )
        else:
            kwargs["max_tokens"] = 1000
            kwargs["temperature"] = 0.3

        try:
            response = self._client.chat.completions.create(**kwargs)
        except Exception as exc:  # noqa: BLE001 — auth/model/network
            # Never substitute a different model: the provider's own
            # error (including model-not-found) surfaces verbatim.
            raise ProviderError("openai", f"{type(exc).__name__}: {exc}")

        choice = response.choices[0]
        content = choice.message.content or ""
        finish = getattr(choice, "finish_reason", None) or "unknown"
        usage = {}
        if getattr(response, "usage", None) is not None:
            u = response.usage
            usage = {
                "prompt_tokens": getattr(u, "prompt_tokens", None),
                "completion_tokens": getattr(u, "completion_tokens", None),
                "total_tokens": getattr(u, "total_tokens", None),
            }

        # Bounded tool-action provenance: names/ids only — tool
        # arguments are never treated as (or folded into) an answer.
        tool_actions = []
        for tc in getattr(choice.message, "tool_calls", None) or []:
            fn = getattr(tc, "function", None)
            tool_actions.append({
                "type": "tool_call",
                "id": getattr(tc, "id", None),
                "name": getattr(fn, "name", None) if fn is not None else None,
            })

        if not content:
            # No usable model-generated text exists. An explanatory
            # sentence from THIS adapter is a system diagnostic, not
            # model output — it must never enter content, TIS
            # evaluation, or a Trust Certificate. Raise through the
            # established provider-failure path with the provider's
            # truthful telemetry preserved for trace provenance.
            diag = (
                f"{api_model} returned no usable text "
                f"(finish_reason={finish}). This usually means the model "
                "spent its token budget on internal reasoning before "
                "producing output, or a content policy intervened."
            )
            raise ProviderError(
                "openai", diag, category="empty_content",
                result=ProviderResult(
                    provider=self.name,
                    model=api_model,
                    content="",                     # established empty repr
                    request_id=getattr(response, "id", None),
                    usage=usage,
                    tool_actions=tool_actions,
                    error=diag,
                    error_category="empty_content",
                    finish_status=finish,           # provider's own status
                    provenance={"display_model": self.display_name},
                ),
            )

        return ProviderResult(
            provider=self.name,
            model=api_model,
            content=content,
            request_id=getattr(response, "id", None),
            usage=usage,
            tool_actions=tool_actions,
            finish_status=finish,
            provenance={"display_model": self.display_name},
        )


__all__ = ["OpenAIProvider", "parse_openai_model"]
