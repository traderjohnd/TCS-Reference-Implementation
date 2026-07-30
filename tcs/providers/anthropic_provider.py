"""
tcs.providers.anthropic_provider
================================

Anthropic Claude through the provider-neutral contract (demo-live
branch, Commit 3). Replaces the inline RequestScopedAnthropic class
that lived in routes_query._build_provider.

Preserved live behavior:
    * max_tokens=2000 (the inline implementation's budget).
    * No temperature or other sampling parameters — the inline code
      never passed them, and current Claude models reject non-default
      sampling parameters, so omitting them is the only always-valid
      choice.
    * Bounded empty-content diagnostic in the response text.

Documented normalizations (required by the neutral contract):
    * The neutral system instruction from build_messages() is mapped to
      the Messages API's dedicated ``system`` field instead of being
      prepended to the user message (the inline code predates the
      shared prompt framing).
    * Response text is the concatenation of all ``text`` content
      blocks (the inline code read only content[0]).
    * ``stop_reason`` maps deterministically to the contract's
      ``finish_status`` vocabulary (table below); the raw value is
      preserved in provenance as ``anthropic_stop_reason``.
    * ``usage`` maps to the normalized token keys established by the
      OpenAI adapter: input_tokens -> prompt_tokens, output_tokens ->
      completion_tokens, total = input + output.
    * The default model is ``claude-sonnet-5`` — the inline default
      (claude-sonnet-4-20250514) is deprecated/retired upstream and
      would fail live calls. An explicitly selected model is always
      passed through verbatim and NEVER substituted.

The API key is request-scoped: held on the provider object for the
call, never logged, never serialized, never part of any result.
Provider exceptions are sanitized so credential material can never
surface in an error message, trace, artifact, or certificate.
"""

from __future__ import annotations

from typing import List

from tcs.providers.base import (
    BaseLiveProvider,
    ProviderError,
    ProviderResult,
    build_messages,
)

DEFAULT_ANTHROPIC_MODEL = "claude-sonnet-5"

# Deterministic stop_reason -> finish_status mapping. Values not in the
# table pass through verbatim (still deterministic; nothing is dropped).
STOP_REASON_TO_FINISH_STATUS = {
    "end_turn": "stop",
    "stop_sequence": "stop",
    "max_tokens": "length",
    "tool_use": "tool_use",
    "pause_turn": "pause",
    "refusal": "refusal",
}


class AnthropicProvider(BaseLiveProvider):
    name = "anthropic"

    def __init__(self, api_key: str, model: str) -> None:
        if not api_key:
            raise ProviderError("anthropic", "Anthropic API key is required")
        selected = (model or DEFAULT_ANTHROPIC_MODEL).strip()
        super().__init__(selected)  # verbatim — never substituted
        self.display_name = selected
        self._api_key = api_key  # memory-only; used for error sanitization
        import anthropic
        self._client = anthropic.Anthropic(api_key=api_key)

    def _sanitize(self, text: str) -> str:
        """Strip credential material from provider error text. Upstream
        errors can echo request headers; the key must never surface in
        results, provenance, traces, artifacts, or certificates."""
        if self._api_key and self._api_key in text:
            text = text.replace(self._api_key, "[redacted]")
        return text

    def _call(self, query: str, context: List[str]) -> ProviderResult:
        system_msg, user_msg = build_messages(query, context)
        try:
            response = self._client.messages.create(
                model=self.model,           # selected model id, verbatim
                max_tokens=2000,
                system=system_msg,
                messages=[{"role": "user", "content": user_msg}],
            )
        except Exception as exc:  # noqa: BLE001 — auth/model/rate/network
            # Never substitute a different model: the provider's own
            # error (including model-not-found) surfaces verbatim,
            # minus any credential material.
            raise ProviderError(
                "anthropic",
                self._sanitize(f"{type(exc).__name__}: {exc}"),
            )

        blocks = getattr(response, "content", None) or []
        content = "".join(
            b.text for b in blocks if getattr(b, "type", None) == "text"
        )

        # Bounded tool-action provenance: names/ids only — tool inputs
        # are never treated as (or folded into) a final answer.
        tool_actions = [
            {
                "type": "tool_use",
                "id": getattr(b, "id", None),
                "name": getattr(b, "name", None),
            }
            for b in blocks if getattr(b, "type", None) == "tool_use"
        ]

        raw_stop = getattr(response, "stop_reason", None)
        finish = STOP_REASON_TO_FINISH_STATUS.get(
            raw_stop, raw_stop or "unknown"
        )

        usage = {}
        u = getattr(response, "usage", None)
        if u is not None:
            input_tokens = getattr(u, "input_tokens", None)
            output_tokens = getattr(u, "output_tokens", None)
            total = (
                input_tokens + output_tokens
                if input_tokens is not None and output_tokens is not None
                else None
            )
            usage = {
                "prompt_tokens": input_tokens,
                "completion_tokens": output_tokens,
                "total_tokens": total,
            }

        if not content:
            # No usable model-generated text — whether the response was
            # entirely empty or carried only non-text blocks (tool_use,
            # etc.). An adapter-authored explanation is a system
            # diagnostic, never model output: it must not enter content,
            # TIS evaluation, or a Trust Certificate. Raise through the
            # established provider-failure path, preserving the
            # provider's truthful telemetry (request id, usage, finish
            # status, bounded tool actions) for trace provenance.
            diag = self._sanitize(
                f"{self.model} returned no usable text "
                f"(stop_reason={raw_stop}"
                + (f", {len(tool_actions)} tool action(s) without a "
                   "final answer" if tool_actions else "")
                + ")."
            )
            raise ProviderError(
                "anthropic", diag, category="empty_content",
                result=ProviderResult(
                    provider=self.name,
                    model=self.model,
                    content="",                     # established empty repr
                    request_id=getattr(response, "id", None),
                    usage=usage,
                    tool_actions=tool_actions,
                    error=diag,
                    error_category="empty_content",
                    finish_status=finish,           # provider's own status
                    provenance={
                        "display_model": self.display_name,
                        "anthropic_stop_reason": raw_stop,
                    },
                ),
            )

        return ProviderResult(
            provider=self.name,
            model=self.model,
            content=content,
            request_id=getattr(response, "id", None),
            usage=usage,
            tool_actions=tool_actions,
            finish_status=finish,
            provenance={
                "display_model": self.display_name,
                "anthropic_stop_reason": raw_stop,
            },
        )


__all__ = ["AnthropicProvider", "DEFAULT_ANTHROPIC_MODEL",
           "STOP_REASON_TO_FINISH_STATUS"]
