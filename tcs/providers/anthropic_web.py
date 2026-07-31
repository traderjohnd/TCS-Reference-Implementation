"""
tcs.providers.anthropic_web
===========================

Anthropic governed Live Web adapter (demo-live branch, Commit 5).

Uses Anthropic's server-side web-search tool through the Messages API
with the explicitly selected DIRECT-EXECUTION tool version below.
Dynamic filtering (the ``web_search_20260209`` variant) is DELIBERATELY
NOT used in Commit 5: it runs provider-side code execution under the
hood, which would introduce an additional code-execution node that
must be governed separately in a future change.

A bounded search-use limit (``max_uses``) is always set. Anthropic can
return a successful HTTP response whose ``web_search_tool_result``
content is an error object — that is a retrieval error, never a
successful search. ``pause_turn`` is surfaced as a truthful bounded
paused-retrieval state; an incomplete paused turn is never certified
as a final model answer. Tool arguments beyond the bounded query and
identifiers are never retained.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from tcs.providers.base import ProviderError, ProviderResult, build_messages
from tcs.providers.web_evidence import (
    Citation,
    ConsultedSource,
    SearchAction,
    WebRetrievalEvidence,
    compute_retrieval_status,
    utc_iso,
)

#: Centralized, explicitly selected tool version — the basic
#: direct-execution variant. NOT web_search_20260209 (dynamic
#: filtering / embedded code execution).
ANTHROPIC_WEB_SEARCH_TOOL_VERSION = "web_search_20250305"
ANTHROPIC_WEB_SEARCH_TOOL_NAME = "web_search"

#: Bounded search-use default (overridable per request, capped).
DEFAULT_MAX_SEARCHES = 5
MAX_MAX_SEARCHES = 10


class AnthropicWebProvider:
    """One governed Live Web execution against the Messages API."""

    name = "anthropic"

    def __init__(self, api_key: str, model: str) -> None:
        if not api_key:
            raise ProviderError("anthropic", "Anthropic API key is required")
        selected = (model or "").strip()
        if not selected:
            raise ProviderError("anthropic",
                                "Live Web requires an explicit model")
        self.model = selected               # verbatim — never substituted
        self.display_name = selected
        self._api_key = api_key
        self.last_result: Optional[ProviderResult] = None
        self.last_evidence: Optional[WebRetrievalEvidence] = None
        import anthropic
        self._client = anthropic.Anthropic(api_key=api_key)

    def _sanitize(self, text: str) -> str:
        if self._api_key and self._api_key in text:
            text = text.replace(self._api_key, "[redacted]")
        return text

    def run_web_query(
        self,
        query: str,
        context: List[str],
        *,
        allowed_domains: Optional[List[str]] = None,
        blocked_domains: Optional[List[str]] = None,
        user_location: Optional[Dict[str, str]] = None,
        max_searches: Optional[int] = None,
    ) -> tuple[str, WebRetrievalEvidence]:
        system_msg, user_msg = build_messages(query, context)

        tool_cfg: Dict[str, Any] = {
            "type": ANTHROPIC_WEB_SEARCH_TOOL_VERSION,
            "name": ANTHROPIC_WEB_SEARCH_TOOL_NAME,
            "max_uses": max(1, min(MAX_MAX_SEARCHES,
                                   max_searches or DEFAULT_MAX_SEARCHES)),
        }
        if allowed_domains:
            tool_cfg["allowed_domains"] = list(allowed_domains)
        if blocked_domains:
            tool_cfg["blocked_domains"] = list(blocked_domains)
        if user_location:
            tool_cfg["user_location"] = {
                "type": "approximate",
                **{k: v for k, v in user_location.items()
                   if k in ("city", "region", "country") and v},
            }

        started = datetime.now(timezone.utc)
        try:
            response = self._client.messages.create(
                model=self.model,
                max_tokens=2000,
                system=system_msg,
                messages=[{"role": "user", "content": user_msg}],
                tools=[tool_cfg],
            )
        except Exception as exc:  # noqa: BLE001
            raise ProviderError(
                "anthropic",
                self._sanitize(f"{type(exc).__name__}: {exc}"),
            )
        completed = datetime.now(timezone.utc)

        evidence = WebRetrievalEvidence(
            provider=self.name,
            model=self.model,
            live_access_requested=True,
            retrieval_started_at=utc_iso(started),
            retrieval_completed_at=utc_iso(completed),
            provider_request_id=getattr(response, "id", None),
        )

        raw_stop = getattr(response, "stop_reason", None)
        paused = raw_stop == "pause_turn"

        final_text = ""
        block_ordinal = 0
        action_ordinal = 0
        source_ordinal = 0
        citation_ordinal = 0
        pending_actions: Dict[str, SearchAction] = {}
        error_codes: List[str] = []

        for b in getattr(response, "content", None) or []:
            btype = getattr(b, "type", None)
            if btype == "server_tool_use" and \
                    getattr(b, "name", None) == ANTHROPIC_WEB_SEARCH_TOOL_NAME:
                # Bounded: only the query and identifiers are retained —
                # never other tool arguments.
                inp = getattr(b, "input", None) or {}
                q = inp.get("query") if isinstance(inp, dict) else None
                act = SearchAction(
                    ordinal=action_ordinal,
                    provider_call_id=getattr(b, "id", None),
                    action_type="search",
                    provider_native_type="server_tool_use.web_search",
                    query=q,
                    status="pending",
                )
                evidence.search_actions.append(act)
                if act.provider_call_id:
                    pending_actions[act.provider_call_id] = act
                action_ordinal += 1
            elif btype == "web_search_tool_result":
                tool_use_id = getattr(b, "tool_use_id", None)
                act = pending_actions.get(tool_use_id)
                content = getattr(b, "content", None)
                # A successful HTTP response can still carry an error
                # OBJECT here — that is a retrieval error, never a
                # successful search.
                if not isinstance(content, list):
                    code = str(getattr(content, "error_code", None)
                               or getattr(content, "type", None)
                               or "web_search_error")
                    error_codes.append(code)
                    if act is not None:
                        act.status = "error"
                        act.error_code = code
                    continue
                if act is not None:
                    act.status = "completed"
                for src in content:
                    if getattr(src, "type", None) != "web_search_result":
                        continue
                    evidence.consulted_sources.append(ConsultedSource(
                        first_seen_ordinal=source_ordinal,
                        display_url=getattr(src, "url", None),
                        title=getattr(src, "title", None),
                        page_age=getattr(src, "page_age", None),
                        source_type="web_search_result",
                        search_call_ids=(
                            [tool_use_id] if tool_use_id else []
                        ),
                        # encrypted_content is provider continuation
                        # material — NEVER retained in evidence.
                    ))
                    source_ordinal += 1
            elif btype == "text":
                text = getattr(b, "text", "") or ""
                for cit in getattr(b, "citations", None) or []:
                    if getattr(cit, "type", None) != \
                            "web_search_result_location":
                        continue
                    # Anthropic citations are block-level: retain the
                    # content-block relationship; offsets are NOT
                    # supplied reliably, so none are invented.
                    evidence.citations.append(Citation(
                        ordinal=citation_ordinal,
                        source_display_url=getattr(cit, "url", None),
                        provider_annotation_type=
                            "web_search_result_location",
                        text_block_ordinal=block_ordinal,
                        start_offset=None,
                        end_offset=None,
                        cited_text=getattr(cit, "cited_text", None),
                        title=getattr(cit, "title", None),
                    ))
                    citation_ordinal += 1
                final_text += text
                block_ordinal += 1

        # Any search action that never produced a result block while the
        # turn is paused stays truthfully "pending".
        for act in evidence.search_actions:
            if act.status == "pending" and not paused:
                act.status = "error"
                act.error_code = act.error_code or "no_result_block"

        usage: Dict[str, Any] = {}
        u = getattr(response, "usage", None)
        if u is not None:
            input_tokens = getattr(u, "input_tokens", None)
            output_tokens = getattr(u, "output_tokens", None)
            usage = {
                "prompt_tokens": input_tokens,
                "completion_tokens": output_tokens,
                "total_tokens": (
                    input_tokens + output_tokens
                    if input_tokens is not None and output_tokens is not None
                    else None
                ),
            }
            stu = getattr(u, "server_tool_use", None)
            if stu is not None:
                usage["web_search_requests"] = getattr(
                    stu, "web_search_requests", None)

        if error_codes:
            evidence.error_summary = self._sanitize(
                "; ".join(sorted(set(error_codes)))[:500])

        # Same documented confirmation rule as the OpenAI adapter:
        # requested AND at least one successful search. (The Anthropic
        # server-side tool is live by definition when declared; the
        # request configuration is unchanged by the 5209e0b fixup.)
        evidence.live_access_confirmed = (
            evidence.live_access_requested
            and evidence.successful_search_count > 0
        )
        evidence.answer_used_web_evidence = bool(evidence.citations)
        evidence.retrieval_status = compute_retrieval_status(
            evidence, final_text, paused=paused,
        )
        evidence.finalize()
        self.last_evidence = evidence

        finish = "pause" if paused else str(raw_stop or "unknown")
        if paused or not final_text:
            # An incomplete paused turn — or a turn with retrieval
            # provenance but no usable final text — is never certified.
            diag = self._sanitize(
                f"{self.model} produced no final Live Web answer "
                f"(stop_reason={raw_stop}, "
                f"retrieval_status={evidence.retrieval_status})."
            )
            self.last_result = ProviderResult(
                provider=self.name, model=self.model, content="",
                request_id=getattr(response, "id", None), usage=usage,
                tool_actions=[
                    {"type": "server_tool_use", "id": a.provider_call_id,
                     "name": "web_search"}
                    for a in evidence.search_actions
                ],
                error=diag, error_category="empty_content",
                finish_status=finish,
                provenance={"display_model": self.display_name,
                            "retrieval_mode": "live_web",
                            "anthropic_stop_reason": raw_stop},
            )
            raise ProviderError("anthropic", diag,
                                category="empty_content",
                                result=self.last_result)

        self.last_result = ProviderResult(
            provider=self.name,
            model=self.model,
            content=final_text,
            request_id=getattr(response, "id", None),
            usage=usage,
            tool_actions=[
                {"type": "server_tool_use", "id": a.provider_call_id,
                 "name": "web_search"}
                for a in evidence.search_actions
            ],
            finish_status=finish,
            provenance={"display_model": self.display_name,
                        "retrieval_mode": "live_web",
                        "anthropic_stop_reason": raw_stop},
        )
        return final_text, evidence


__all__ = ["AnthropicWebProvider", "ANTHROPIC_WEB_SEARCH_TOOL_VERSION",
           "ANTHROPIC_WEB_SEARCH_TOOL_NAME", "DEFAULT_MAX_SEARCHES",
           "MAX_MAX_SEARCHES"]
