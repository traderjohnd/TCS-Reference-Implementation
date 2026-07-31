"""
Commit 3/6 (demo-live branch) — Anthropic Claude behind the
provider-neutral contract.

Covers the mandate's test matrix:
  * build_provider construction, no aliases, unknown names still fail.
  * Selected and custom Claude model IDs pass through verbatim — the
    adapter never substitutes another model.
  * System/user message mapping onto the Messages API.
  * Normalized content, usage, request-id, stop-reason mapping,
    latency, empty-content diagnostic.
  * Auth / model-not-found / rate-limit failures surface as
    ProviderError with an error-shaped last_result.
  * API-key non-exposure (results, provenance, sanitized errors).
  * LLM connector provenance lift on success and error.
  * Demo Mode blocks the anthropic external call at the backend;
    Live Mode permits it only after deliberate confirmed activation,
    and the governed run travels the same TIS v2 -> certificate ->
    hash-chain path as every other provider.
"""

from __future__ import annotations

import os
import sys
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from tcs.api.app import create_app
from tcs.operating_mode import EXECUTION_MODE_LIVE
from tcs.providers import build_provider
from tcs.providers.anthropic_provider import (
    DEFAULT_ANTHROPIC_MODEL,
    AnthropicProvider,
    STOP_REASON_TO_FINISH_STATUS,
)
from tcs.providers.base import ProviderError


# --------------------------------------------------------------------------- #
# Fake Anthropic SDK                                                           #
# --------------------------------------------------------------------------- #

class _FakeMessages:
    def __init__(self, response=None, raise_exc=None):
        self.response = response
        self.raise_exc = raise_exc
        self.last_kwargs = None

    def create(self, **kwargs):
        self.last_kwargs = kwargs
        if self.raise_exc is not None:
            raise self.raise_exc
        return self.response


def _fake_anthropic_module(monkeypatch, response=None, raise_exc=None):
    """Install a fake ``anthropic`` module; returns the messages stub so
    tests can inspect the kwargs actually sent."""
    messages = _FakeMessages(response=response, raise_exc=raise_exc)
    fake = SimpleNamespace(
        Anthropic=lambda api_key=None: SimpleNamespace(
            api_key=api_key, messages=messages,
        ),
    )
    monkeypatch.setitem(sys.modules, "anthropic", fake)
    return messages


def _anthropic_response(
    text="The answer.",
    stop_reason="end_turn",
    request_id="msg_abc123",
    usage=(12, 34),
    extra_blocks=(),
):
    usage_obj = None
    if usage is not None:
        usage_obj = SimpleNamespace(
            input_tokens=usage[0], output_tokens=usage[1],
        )
    blocks = []
    if text is not None:
        blocks.append(SimpleNamespace(type="text", text=text))
    blocks.extend(extra_blocks)
    return SimpleNamespace(
        id=request_id,
        stop_reason=stop_reason,
        usage=usage_obj,
        content=blocks,
    )


# --------------------------------------------------------------------------- #
# Construction via build_provider                                              #
# --------------------------------------------------------------------------- #

class TestBuildProviderAnthropic:
    def test_constructs_with_key(self, monkeypatch):
        _fake_anthropic_module(monkeypatch)
        provider, display = build_provider(
            "anthropic", "sk-ant-test", "claude-opus-5",
        )
        assert isinstance(provider, AnthropicProvider)
        assert provider.name == "anthropic"
        assert provider.model == "claude-opus-5"
        assert display == "claude-opus-5"

    def test_missing_key_raises_valueerror(self):
        with pytest.raises(ValueError, match="Anthropic API key is required"):
            build_provider("anthropic", "", "claude-opus-5")

    def test_default_model_when_unspecified(self, monkeypatch):
        _fake_anthropic_module(monkeypatch)
        provider, display = build_provider("anthropic", "sk-ant-test", None)
        assert provider.model == DEFAULT_ANTHROPIC_MODEL
        assert display == DEFAULT_ANTHROPIC_MODEL

    @pytest.mark.parametrize("name", ["claude", "Claude", "anthropic-claude"])
    def test_no_fuzzy_aliases(self, name):
        # Only the repository's canonical identifier "anthropic" is
        # recognized — no broad matching, no silent mock fallback.
        with pytest.raises(ValueError, match="Unknown provider"):
            build_provider(name, "key", "model")


# --------------------------------------------------------------------------- #
# Request mapping                                                              #
# --------------------------------------------------------------------------- #

class TestAnthropicRequestMapping:
    def test_system_and_user_mapping_with_context(self, monkeypatch):
        messages = _fake_anthropic_module(
            monkeypatch, response=_anthropic_response(),
        )
        p = AnthropicProvider(api_key="sk-ant-test", model="claude-opus-5")
        p.generate_result("What is the policy?", ["chunk one", "chunk two"])
        kwargs = messages.last_kwargs
        # System instruction goes to the dedicated system field.
        assert "domain-aware" in kwargs["system"]
        # User content carries context + question, never the system text.
        assert kwargs["messages"] == [
            {"role": "user", "content": kwargs["messages"][0]["content"]},
        ]
        user_content = kwargs["messages"][0]["content"]
        assert "chunk one" in user_content
        assert "What is the policy?" in user_content
        assert "domain-aware" not in user_content

    def test_token_budget_preserved(self, monkeypatch):
        messages = _fake_anthropic_module(
            monkeypatch, response=_anthropic_response(),
        )
        AnthropicProvider(
            api_key="sk-ant-test", model="claude-opus-5",
        ).generate_result("q", [])
        assert messages.last_kwargs["max_tokens"] == 2000

    def test_no_sampling_parameters_sent(self, monkeypatch):
        # Current Claude models reject non-default sampling params —
        # the adapter passes generation parameters only when valid,
        # which here means not at all (matches the inline behavior).
        messages = _fake_anthropic_module(
            monkeypatch, response=_anthropic_response(),
        )
        AnthropicProvider(
            api_key="sk-ant-test", model="claude-opus-5",
        ).generate_result("q", [])
        for param in ("temperature", "top_p", "top_k", "thinking"):
            assert param not in messages.last_kwargs

    def test_selected_model_passes_verbatim(self, monkeypatch):
        messages = _fake_anthropic_module(
            monkeypatch, response=_anthropic_response(),
        )
        AnthropicProvider(
            api_key="sk-ant-test", model="claude-haiku-4-5",
        ).generate_result("q", [])
        assert messages.last_kwargs["model"] == "claude-haiku-4-5"

    def test_custom_model_id_passes_verbatim(self, monkeypatch):
        messages = _fake_anthropic_module(
            monkeypatch, response=_anthropic_response(),
        )
        AnthropicProvider(
            api_key="sk-ant-test", model="claude-experimental-preview-42",
        ).generate_result("q", [])
        assert messages.last_kwargs["model"] == "claude-experimental-preview-42"


# --------------------------------------------------------------------------- #
# Response normalization                                                       #
# --------------------------------------------------------------------------- #

class TestAnthropicResponseNormalization:
    def _provider(self, monkeypatch, **resp_kwargs):
        _fake_anthropic_module(
            monkeypatch, response=_anthropic_response(**resp_kwargs),
        )
        return AnthropicProvider(api_key="sk-ant-test", model="claude-opus-5")

    def test_normalized_result_fields(self, monkeypatch):
        p = self._provider(monkeypatch)
        result = p.generate_result("q", [])
        assert result.provider == "anthropic"
        assert result.model == "claude-opus-5"
        assert result.content == "The answer."
        assert result.request_id == "msg_abc123"
        assert result.usage == {
            "prompt_tokens": 12, "completion_tokens": 34, "total_tokens": 46,
        }
        assert result.finish_status == "stop"
        assert result.latency_ms is not None
        assert result.tool_actions == []          # explicit empty, not absent
        assert result.error is None               # explicit None, not absent
        assert result.provenance["anthropic_stop_reason"] == "end_turn"
        assert result.provenance["display_model"] == "claude-opus-5"

    def test_multiple_text_blocks_concatenated(self, monkeypatch):
        p = self._provider(
            monkeypatch,
            text="part one. ",
            extra_blocks=(
                SimpleNamespace(type="tool_use", name="x", input={}),
                SimpleNamespace(type="text", text="part two."),
            ),
        )
        assert p.generate_result("q", []).content == "part one. part two."

    @pytest.mark.parametrize("raw,expected", [
        ("end_turn", "stop"),
        ("stop_sequence", "stop"),
        ("max_tokens", "length"),
        ("tool_use", "tool_use"),
        ("pause_turn", "pause"),
        ("refusal", "refusal"),
        ("future_reason", "future_reason"),  # unknown values pass through
    ])
    def test_stop_reason_maps_deterministically(self, monkeypatch, raw, expected):
        assert STOP_REASON_TO_FINISH_STATUS.get(raw, raw) == expected
        p = self._provider(monkeypatch, stop_reason=raw)
        result = p.generate_result("q", [])
        assert result.finish_status == expected
        assert result.provenance["anthropic_stop_reason"] == raw

    def test_missing_usage_yields_empty_dict(self, monkeypatch):
        p = self._provider(monkeypatch, usage=None)
        assert p.generate_result("q", []).usage == {}

    def test_empty_content_is_provider_failure_not_model_output(self, monkeypatch):
        # Fixup after eb80246: no text blocks -> no model output. The
        # diagnostic is a SYSTEM message riding the provider-failure
        # path — never placed in content, never scored, never certified.
        p = self._provider(monkeypatch, text=None, stop_reason="refusal")
        with pytest.raises(ProviderError) as excinfo:
            p.generate_result("q", [])
        assert excinfo.value.category == "empty_content"
        assert "returned no usable text" in excinfo.value.detail
        assert len(excinfo.value.detail) < 300  # bounded diagnostic
        last = p.last_result
        assert last.content == ""              # established empty repr
        assert last.error_category == "empty_content"
        assert last.finish_status == "refusal"  # provider's own status
        assert last.request_id == "msg_abc123"  # telemetry preserved
        assert last.usage["total_tokens"] == 46
        assert last.provenance["anthropic_stop_reason"] == "refusal"

    def test_tool_actions_without_final_text_is_empty_output(self, monkeypatch):
        # Non-text blocks but no final answer: bounded tool-action
        # provenance is preserved; tool inputs are never treated as the
        # answer; no ProviderResult with content is returned.
        p = self._provider(
            monkeypatch,
            text=None,
            stop_reason="tool_use",
            extra_blocks=(
                SimpleNamespace(type="tool_use", id="tu_1",
                                name="search_corpus",
                                input={"query": "secret args"}),
            ),
        )
        with pytest.raises(ProviderError) as excinfo:
            p.generate_result("q", [])
        assert excinfo.value.category == "empty_content"
        assert "tool action" in excinfo.value.detail
        last = p.last_result
        assert last.content == ""
        assert last.tool_actions == [
            {"type": "tool_use", "id": "tu_1", "name": "search_corpus"},
        ]
        # Bounded provenance only — tool arguments never recorded.
        assert "secret args" not in str(last)


# --------------------------------------------------------------------------- #
# Provider failures                                                            #
# --------------------------------------------------------------------------- #

class TestAnthropicFailures:
    def _failing_provider(self, monkeypatch, exc):
        _fake_anthropic_module(monkeypatch, raise_exc=exc)
        return AnthropicProvider(api_key="sk-ant-test", model="claude-opus-5")

    def test_auth_failure_surfaces(self, monkeypatch):
        class AuthenticationError(Exception):
            pass
        p = self._failing_provider(
            monkeypatch, AuthenticationError("invalid x-api-key"),
        )
        with pytest.raises(ProviderError) as excinfo:
            p.generate_result("q", [])
        assert "AuthenticationError" in excinfo.value.detail
        assert "invalid x-api-key" in excinfo.value.detail

    def test_model_not_found_surfaces_verbatim_no_substitution(self, monkeypatch):
        detail = "model: claude-nonexistent-9 not found"

        class NotFoundError(Exception):
            pass
        _fake_anthropic_module(monkeypatch)
        p = AnthropicProvider(api_key="sk-ant-test", model="claude-nonexistent-9")
        messages = _fake_anthropic_module(
            monkeypatch, raise_exc=NotFoundError(detail),
        )
        p2 = AnthropicProvider(api_key="sk-ant-test", model="claude-nonexistent-9")
        with pytest.raises(ProviderError) as excinfo:
            p2.generate_result("q", [])
        assert detail in str(excinfo.value)
        # The requested model went to the API verbatim — never replaced.
        assert messages.last_kwargs["model"] == "claude-nonexistent-9"
        assert p.model == "claude-nonexistent-9"

    def test_rate_limit_failure_surfaces(self, monkeypatch):
        class RateLimitError(Exception):
            pass
        p = self._failing_provider(
            monkeypatch, RateLimitError("rate_limit_error: retry later"),
        )
        with pytest.raises(ProviderError, match="rate_limit_error"):
            p.generate_result("q", [])

    def test_error_shaped_last_result(self, monkeypatch):
        p = self._failing_provider(monkeypatch, RuntimeError("boom"))
        with pytest.raises(ProviderError):
            p.generate_result("q", [])
        assert p.last_result is not None
        assert p.last_result.finish_status == "error"
        assert p.last_result.content == ""
        assert "boom" in p.last_result.error
        assert p.last_result.latency_ms is not None


# --------------------------------------------------------------------------- #
# Secret handling                                                              #
# --------------------------------------------------------------------------- #

class TestAnthropicSecretHandling:
    SECRET = "sk-ant-SECRET-KEY-VALUE-123"

    def test_key_never_in_result_or_provenance(self, monkeypatch):
        _fake_anthropic_module(monkeypatch, response=_anthropic_response())
        p = AnthropicProvider(api_key=self.SECRET, model="claude-opus-5")
        result = p.generate_result("q", [])
        assert self.SECRET not in str(result)
        assert self.SECRET not in str(result.provenance_summary())

    def test_error_echoing_key_is_sanitized(self, monkeypatch):
        # Upstream errors can echo request headers; the key must be
        # scrubbed before the error is stored or exposed anywhere.
        exc = RuntimeError(
            f"401 unauthorized; header x-api-key: {self.SECRET} rejected",
        )
        _fake_anthropic_module(monkeypatch, raise_exc=exc)
        p = AnthropicProvider(api_key=self.SECRET, model="claude-opus-5")
        with pytest.raises(ProviderError) as excinfo:
            p.generate_result("q", [])
        assert self.SECRET not in str(excinfo.value)
        assert "[redacted]" in excinfo.value.detail
        # The error-shaped last_result (trace provenance source) is
        # equally clean.
        assert self.SECRET not in str(p.last_result)
        assert self.SECRET not in str(p.last_result.provenance_summary())


# --------------------------------------------------------------------------- #
# Connector provenance lift                                                    #
# --------------------------------------------------------------------------- #

class TestConnectorProvenanceWithAnthropic:
    def _connector(self, provider):
        from tcs.workflow.connectors.llm import LLMConnector
        return LLMConnector(
            provider=provider, provider_name="anthropic",
            model=provider.display_name,
        )

    def test_success_provenance_lifted(self, monkeypatch):
        from tcs.workflow.connector import ConnectorRequest
        _fake_anthropic_module(monkeypatch, response=_anthropic_response())
        p = AnthropicProvider(api_key="sk-ant-test", model="claude-opus-5")
        result = self._connector(p).invoke(ConnectorRequest(query="hi"))
        prov = result.raw_metadata["provider_provenance"]
        assert prov["provider"] == "anthropic"
        assert prov["model"] == "claude-opus-5"
        assert prov["request_id"] == "msg_abc123"
        assert prov["finish_status"] == "stop"
        assert prov["anthropic_stop_reason"] == "end_turn"
        assert prov["usage"]["total_tokens"] == 46

    def test_error_provenance_lifted(self, monkeypatch):
        from tcs.workflow.connector import ConnectorRequest
        _fake_anthropic_module(monkeypatch, raise_exc=RuntimeError("provider down"))
        p = AnthropicProvider(api_key="sk-ant-test", model="claude-opus-5")
        result = self._connector(p).invoke(ConnectorRequest(query="hi"))
        assert result.error is not None
        prov = result.raw_metadata["provider_provenance"]
        assert prov["finish_status"] == "error"
        assert "provider down" in prov["error"]


# --------------------------------------------------------------------------- #
# Operating-mode enforcement + governance-path parity (route level)             #
# --------------------------------------------------------------------------- #

@pytest.fixture()
def client():
    os.environ["TCS_WORKFLOW_TRACE_ENABLED"] = "true"
    app = create_app()
    with TestClient(app) as c:
        yield c
    os.environ.pop("TCS_WORKFLOW_TRACE_ENABLED", None)


def _go_live(client):
    r = client.post("/v2/mode", json={"mode": "live", "confirm": True})
    assert r.status_code == 200 and r.json()["mode"] == "live"


class TestAnthropicModeEnforcement:
    def test_demo_blocks_anthropic_query(self, client):
        r = client.post("/v2/query", json={
            "query": "What is the retention policy?",
            "provider": "anthropic", "api_key": "sk-ant-never-used",
            "model": "claude-opus-5",
        })
        assert r.status_code == 200
        body = r.json()
        assert body["blocked"] is True
        assert "demo_mode_enforced" in body["blocking_reason"]
        assert body["certificate_id"] is None

    def test_demo_blocks_anthropic_connection_test(self, client):
        r = client.post("/v2/connections/test", json={
            "category": "llm", "provider": "anthropic",
            "api_key": "sk-ant-never-used", "model": "claude-opus-5",
        })
        assert r.status_code == 200
        body = r.json()
        assert body["success"] is False
        assert "demo_mode_enforced" in body["error"]

    def test_live_mode_full_governed_path(self, client, monkeypatch):
        # Deliberate confirmed activation, then a Claude response must
        # travel the same downstream path as any other provider:
        # connector -> workflow evidence -> TIS v2 -> decision ->
        # certificate -> persistence -> hash chain.
        _go_live(client)
        secret = "sk-ant-live-test-SECRET"
        _fake_anthropic_module(
            monkeypatch,
            response=_anthropic_response(
                text="Retention is seven years per policy section 4.2.",
            ),
        )
        r = client.post("/v2/query", json={
            "query": "What is the document retention policy?",
            "provider": "anthropic", "api_key": secret,
            "model": "claude-opus-5",
        })
        assert r.status_code == 200
        body = r.json()
        assert body["blocking_reason"] is None or \
            "demo_mode_enforced" not in body["blocking_reason"]
        assert body["llm_provider"] == "anthropic"
        assert body["certificate_id"]  # governed evaluation issued a TC
        # Truthful execution mode on trace and certificate.
        meta = (body.get("workflow_trace") or {}).get("metadata") or {}
        assert meta.get("execution_mode") == EXECUTION_MODE_LIVE
        assert meta.get("llm_provider") == "anthropic"
        tc = client.get(f"/v2/certificates/{body['certificate_id']}")
        assert tc.status_code == 200
        tc_body = tc.json()
        assert tc_body["scope_attestation"]["execution_mode"] == \
            EXECUTION_MODE_LIVE
        # The key never appears in the response or the certificate.
        assert secret not in r.text
        assert secret not in tc.text

    def test_live_mode_anthropic_provider_error_isolated(self, client, monkeypatch):
        # A provider failure in live mode is a provider error — no
        # certificate, and clearly not a governance outcome.
        _go_live(client)
        _fake_anthropic_module(
            monkeypatch, raise_exc=RuntimeError("authentication_error"),
        )
        r = client.post("/v2/query", json={
            "query": "What is the retention policy?",
            "provider": "anthropic", "api_key": "sk-ant-bad",
            "model": "claude-opus-5",
        })
        assert r.status_code == 200
        body = r.json()
        assert body["blocked"] is True
        assert body["certificate_id"] is None
        assert "demo_mode_enforced" not in (body["blocking_reason"] or "")
