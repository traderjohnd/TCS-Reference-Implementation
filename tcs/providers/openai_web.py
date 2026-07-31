"""
tcs.providers.openai_web
========================

OpenAI governed Live Web adapter (demo-live branch, Commit 5).

Uses the Responses API with the CURRENT hosted ``web_search`` tool —
never the legacy ``web_search_preview`` tool, never a silent fallback
to Chat Completions or a search-preview model. When the operator
selected Live Web:

  * the search tool is REQUIRED (``tool_choice`` pins the hosted
    tool), not optional;
  * live external access is requested EXPLICITLY via
    ``external_web_access: true`` in the tool configuration — never
    relying on the provider's default, and never a cache-only
    configuration. The Live Web path has no input that can disable
    it. The provider executes the retrieval on its infrastructure;
    TCS does not fetch pages itself;
  * the complete consulted-source list is requested through the
    provider's include mechanism
    (``include=["web_search_call.action.sources"]``).

Evidence-state semantics (fixup after 5209e0b):
  * ``live_access_requested`` is derived from the OUTBOUND tool
    configuration (external_web_access is True) — never inferred from
    an observed search action;
  * ``web_search_action_observed`` is derived from the response;
  * ``live_access_confirmed`` follows the documented rule: requested
    AND at least one search completed successfully. It does not claim
    per-page freshness proof the provider does not supply.

Truthful outcomes:
  * request completed but NO web_search action occurred ->
    ``retrieval_not_performed`` (never labeled Live Web, no TC);
  * searches ran but nothing usable came back -> classified via
    compute_retrieval_status; source evidence is never invented;
  * empty final text -> the established empty_content provider-failure
    semantics; an adapter diagnostic is never certified as output.

The API key is request-scoped and never enters evidence, hashes,
errors, or results.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from tcs.providers.base import ProviderError, ProviderResult
from tcs.providers.openai_provider import parse_openai_model
from tcs.providers.web_evidence import (
    Citation,
    ConsultedSource,
    SearchAction,
    WebRetrievalEvidence,
    compute_retrieval_status,
    utc_iso,
)

#: Current hosted web-search tool type (centralized constant).
OPENAI_WEB_SEARCH_TOOL = "web_search"

#: Include path for the complete consulted-source list.
OPENAI_SOURCES_INCLUDE = "web_search_call.action.sources"


class OpenAIWebProvider:
    """One governed Live Web execution against the Responses API."""

    name = "openai"

    def __init__(self, api_key: str, model: str) -> None:
        if not api_key:
            raise ProviderError("openai", "OpenAI API key is required")
        display = (model or "").strip()
        if not display:
            raise ProviderError("openai", "Live Web requires an explicit model")
        api_model, _thinking = parse_openai_model(display)
        self.model = api_model              # verbatim — never substituted
        self.display_name = display
        self._api_key = api_key
        self.last_result: Optional[ProviderResult] = None
        self.last_evidence: Optional[WebRetrievalEvidence] = None
        import openai
        self._client = openai.OpenAI(api_key=api_key)

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
    ) -> tuple[str, WebRetrievalEvidence]:
        """Execute one Live Web request; returns (final_text, evidence).

        Raises ProviderError for provider-layer failures (auth, model
        without web-search support, org disablement, empty output).
        Retrieval-level failures are reported truthfully on the
        returned evidence's retrieval_status instead.
        """
        from tcs.providers.base import build_messages
        system_msg, user_msg = build_messages(query, context)

        tool_cfg: Dict[str, Any] = {
            "type": OPENAI_WEB_SEARCH_TOOL,
            # Live external access is requested EXPLICITLY — never the
            # provider default, never cache-only. Hardcoded True: the
            # Live Web path exposes no way to construct it disabled.
            "external_web_access": True,
        }
        filters: Dict[str, Any] = {}
        if allowed_domains:
            filters["allowed_domains"] = list(allowed_domains)
        if blocked_domains:
            filters["blocked_domains"] = list(blocked_domains)
        if filters:
            tool_cfg["filters"] = filters
        if user_location:
            # Approximate only — city/region/country strings; never
            # precise coordinates (Commit 5 privacy rule).
            tool_cfg["user_location"] = {
                "type": "approximate",
                **{k: v for k, v in user_location.items()
                   if k in ("city", "region", "country") and v},
            }

        started = datetime.now(timezone.utc)
        try:
            response = self._client.responses.create(
                model=self.model,
                instructions=system_msg,
                input=user_msg,
                tools=[tool_cfg],
                # Live Web REQUIRES the hosted search tool — never
                # optional, never silently downgraded to Live LLM.
                tool_choice={"type": OPENAI_WEB_SEARCH_TOOL},
                include=[OPENAI_SOURCES_INCLUDE],
            )
        except Exception as exc:  # noqa: BLE001 — auth/model/org/network
            raise ProviderError(
                "openai", self._sanitize(f"{type(exc).__name__}: {exc}"),
            )
        completed = datetime.now(timezone.utc)

        evidence = WebRetrievalEvidence(
            provider=self.name,
            model=self.model,
            # Request-derived: taken from the outbound configuration,
            # never inferred from the response.
            live_access_requested=(
                tool_cfg["external_web_access"] is True
            ),
            retrieval_started_at=utc_iso(started),
            retrieval_completed_at=utc_iso(completed),
            provider_request_id=getattr(response, "id", None),
        )

        final_text = ""
        block_ordinal = 0
        action_ordinal = 0
        source_ordinal = 0
        citation_ordinal = 0

        for item in getattr(response, "output", None) or []:
            itype = getattr(item, "type", None)
            if itype == "web_search_call":
                action = getattr(item, "action", None)
                native_type = getattr(action, "type", None)
                status = getattr(item, "status", None) or "unknown"
                evidence.search_actions.append(SearchAction(
                    ordinal=action_ordinal,
                    provider_call_id=getattr(item, "id", None),
                    action_type=(
                        "search" if native_type == "search"
                        else "open_page" if native_type == "open_page"
                        else str(native_type or "unknown")
                    ),
                    provider_native_type=(
                        str(native_type) if native_type else None
                    ),
                    # Never invent a query the provider didn't return.
                    query=getattr(action, "query", None),
                    status=status,
                    error_code=(
                        str(getattr(item, "error", None))
                        if getattr(item, "error", None) else None
                    ),
                ))
                call_id = getattr(item, "id", None)
                for src in getattr(action, "sources", None) or []:
                    evidence.consulted_sources.append(ConsultedSource(
                        first_seen_ordinal=source_ordinal,
                        display_url=getattr(src, "url", None),
                        title=getattr(src, "title", None),
                        provider_source_id=getattr(src, "id", None),
                        source_type=getattr(src, "type", None) or "url",
                        search_call_ids=[call_id] if call_id else [],
                    ))
                    source_ordinal += 1
                action_ordinal += 1
            elif itype == "message":
                for content in getattr(item, "content", None) or []:
                    if getattr(content, "type", None) != "output_text":
                        continue
                    text = getattr(content, "text", "") or ""
                    for ann in getattr(content, "annotations", None) or []:
                        if getattr(ann, "type", None) != "url_citation":
                            continue
                        evidence.citations.append(Citation(
                            ordinal=citation_ordinal,
                            source_display_url=getattr(ann, "url", None),
                            provider_annotation_type="url_citation",
                            text_block_ordinal=block_ordinal,
                            start_offset=getattr(ann, "start_index", None),
                            end_offset=getattr(ann, "end_index", None),
                            title=getattr(ann, "title", None),
                        ))
                        citation_ordinal += 1
                    final_text += text
                    block_ordinal += 1

        usage: Dict[str, Any] = {}
        u = getattr(response, "usage", None)
        if u is not None:
            usage = {
                "prompt_tokens": getattr(u, "input_tokens", None),
                "completion_tokens": getattr(u, "output_tokens", None),
                "total_tokens": getattr(u, "total_tokens", None),
            }

        # Documented confirmation rule: explicitly requested AND at
        # least one search completed successfully. Not a per-page
        # freshness claim.
        evidence.live_access_confirmed = (
            evidence.live_access_requested
            and evidence.successful_search_count > 0
        )
        evidence.answer_used_web_evidence = bool(evidence.citations)
        evidence.retrieval_status = compute_retrieval_status(
            evidence, final_text,
        )
        evidence.finalize()
        self.last_evidence = evidence

        finish = getattr(response, "status", None) or "completed"
        if not final_text:
            diag = self._sanitize(
                f"{self.model} returned no usable text on a Live Web "
                f"request (status={finish})."
            )
            self.last_result = ProviderResult(
                provider=self.name, model=self.model, content="",
                request_id=getattr(response, "id", None), usage=usage,
                error=diag, error_category="empty_content",
                finish_status=str(finish),
                provenance={"display_model": self.display_name,
                            "retrieval_mode": "live_web"},
            )
            raise ProviderError("openai", diag, category="empty_content",
                                result=self.last_result)

        self.last_result = ProviderResult(
            provider=self.name,
            model=self.model,
            content=final_text,
            request_id=getattr(response, "id", None),
            usage=usage,
            tool_actions=[
                {"type": "web_search_call", "id": a.provider_call_id,
                 "name": a.action_type}
                for a in evidence.search_actions
            ],
            finish_status=str(finish),
            provenance={"display_model": self.display_name,
                        "retrieval_mode": "live_web"},
        )
        return final_text, evidence


__all__ = ["OpenAIWebProvider", "OPENAI_WEB_SEARCH_TOOL",
           "OPENAI_SOURCES_INCLUDE"]
