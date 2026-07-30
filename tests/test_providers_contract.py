"""
Commit 2/6 (demo-live branch) — provider-neutral live interface.

Covers:
  * ProviderResult contract + provenance_summary shape (no content,
    no secrets).
  * ScriptedMockProvider through the neutral contract (finish_status
    "scripted" so scripted output can never read as live).
  * build_provider construction/error semantics (mock default, missing
    key, unknown provider is an error — no silent mock fallback).
  * routes_query._build_provider rewire (all providers via the neutral
    layer as of Commit 3).
  * Monkeypatched OpenAI mapping: message framing, token-budget kwargs,
    result normalization, empty-content diagnostic.
  * Model-not-found surfaces verbatim; custom model IDs pass through
    untouched (catalog is a convenience, not a hard dependency).
  * LLM workflow connector lifts provider provenance into raw_metadata
    on both success and error paths.
"""

from types import SimpleNamespace

import pytest

from tcs.providers import build_provider
from tcs.providers.base import BaseLiveProvider, ProviderError, ProviderResult
from tcs.providers.mock_provider import ScriptedMockProvider
from tcs.providers.openai_provider import OpenAIProvider, parse_openai_model


# --------------------------------------------------------------------------- #
# Fake OpenAI SDK                                                              #
# --------------------------------------------------------------------------- #

class _FakeCompletions:
    def __init__(self, response=None, raise_exc=None):
        self.response = response
        self.raise_exc = raise_exc
        self.last_kwargs = None

    def create(self, **kwargs):
        self.last_kwargs = kwargs
        if self.raise_exc is not None:
            raise self.raise_exc
        return self.response


class _FakeOpenAIClient:
    def __init__(self, api_key=None, completions=None):
        self.api_key = api_key
        self.chat = SimpleNamespace(completions=completions)


def _fake_openai_module(monkeypatch, response=None, raise_exc=None):
    """Install a fake ``openai`` module; returns the completions stub so
    tests can inspect the kwargs actually sent."""
    import sys

    completions = _FakeCompletions(response=response, raise_exc=raise_exc)
    fake = SimpleNamespace(
        OpenAI=lambda api_key=None: _FakeOpenAIClient(api_key, completions),
    )
    monkeypatch.setitem(sys.modules, "openai", fake)
    return completions


def _openai_response(
    content="The answer.",
    finish_reason="stop",
    request_id="req-123",
    usage=(10, 20, 30),
):
    usage_obj = None
    if usage is not None:
        usage_obj = SimpleNamespace(
            prompt_tokens=usage[0],
            completion_tokens=usage[1],
            total_tokens=usage[2],
        )
    return SimpleNamespace(
        id=request_id,
        usage=usage_obj,
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(content=content),
                finish_reason=finish_reason,
            )
        ],
    )


# --------------------------------------------------------------------------- #
# ProviderResult contract                                                      #
# --------------------------------------------------------------------------- #

class TestProviderResult:
    def test_provenance_summary_minimum_fields(self):
        r = ProviderResult(
            provider="openai",
            model="gpt-5.5",
            content="hello",
            request_id="req-1",
            usage={"total_tokens": 5},
            tool_actions=[{"type": "none"}],
            latency_ms=12.5,
            finish_status="stop",
            provenance={"display_model": "gpt-5.5 (Instant)"},
        )
        s = r.provenance_summary()
        assert s["provider"] == "openai"
        assert s["model"] == "gpt-5.5"
        assert s["request_id"] == "req-1"
        assert s["usage"] == {"total_tokens": 5}
        assert s["tool_actions"] == [{"type": "none"}]
        assert s["finish_status"] == "stop"
        assert s["provider_latency_ms"] == 12.5
        assert s["error"] is None
        assert s["display_model"] == "gpt-5.5 (Instant)"

    def test_provenance_summary_excludes_content(self):
        r = ProviderResult(provider="p", model="m", content="SECRET-CONTENT")
        assert "SECRET-CONTENT" not in str(r.provenance_summary())

    def test_error_result_shape(self):
        r = ProviderResult(
            provider="openai", model="m", content="",
            error="model not found", finish_status="error",
        )
        assert r.provenance_summary()["error"] == "model not found"
        assert r.provenance_summary()["finish_status"] == "error"


# --------------------------------------------------------------------------- #
# Scripted mock through the neutral contract                                   #
# --------------------------------------------------------------------------- #

class TestScriptedMockProvider:
    def test_finish_status_is_scripted_never_live(self):
        p = ScriptedMockProvider()
        result = p.generate_result("What is TCS?", [])
        assert result.provider == "mock"
        assert result.finish_status == "scripted"
        assert result.provenance.get("deterministic") is True

    def test_legacy_generate_returns_string_and_sets_last_result(self):
        p = ScriptedMockProvider()
        text = p.generate("What is TCS?", [])
        assert isinstance(text, str) and text
        assert p.last_result is not None
        assert p.last_result.content == text

    def test_deterministic_same_query_same_output(self):
        p = ScriptedMockProvider()
        assert p.generate("query one", []) == p.generate("query one", [])


# --------------------------------------------------------------------------- #
# build_provider construction semantics                                        #
# --------------------------------------------------------------------------- #

class TestBuildProvider:
    @pytest.mark.parametrize("name", ["mock", "", None])
    def test_mock_and_absent_names_build_scripted(self, name):
        provider, display = build_provider(name, None, None)
        assert isinstance(provider, ScriptedMockProvider)
        assert display == "deterministic"

    def test_openai_missing_key_raises_valueerror(self):
        with pytest.raises(ValueError, match="OpenAI API key is required"):
            build_provider("openai", "", "gpt-5.5 (Instant)")

    def test_unknown_provider_raises_valueerror(self):
        with pytest.raises(ValueError, match="Unknown provider"):
            build_provider("frontier-llm-x", "key", "model")

    def test_openai_constructs_with_key(self, monkeypatch):
        _fake_openai_module(monkeypatch)
        provider, display = build_provider("openai", "sk-test", "gpt-5.5 (Thinking)")
        assert provider.name == "openai"
        assert provider.model == "gpt-5.5"
        assert display == "gpt-5.5 (Thinking)"


# --------------------------------------------------------------------------- #
# routes_query._build_provider rewire                                          #
# --------------------------------------------------------------------------- #

class TestRoutesBuildProviderRewire:
    def test_mock_routes_through_neutral_layer(self):
        from tcs.api.routes_query import _build_provider
        provider, display = _build_provider("mock", None, None)
        assert isinstance(provider, ScriptedMockProvider)
        assert display == "deterministic"

    def test_openai_missing_key_error_message_preserved(self):
        from tcs.api.routes_query import _build_provider
        with pytest.raises(ValueError, match="OpenAI API key is required"):
            _build_provider("openai", None, None)

    def test_unknown_provider_no_silent_mock_fallback(self):
        # A scripted response must never masquerade as a live provider:
        # unknown names are an error, not a mock fallback.
        from tcs.api.routes_query import _build_provider
        with pytest.raises(ValueError, match="Unknown provider"):
            _build_provider("not-a-provider", "key", "model")

    def test_anthropic_missing_key_error_message_preserved(self):
        # Commit 3: anthropic now routes through the neutral layer with
        # the same error message the inline branch produced.
        from tcs.api.routes_query import _build_provider
        with pytest.raises(ValueError, match="Anthropic API key is required"):
            _build_provider("anthropic", None, None)


# --------------------------------------------------------------------------- #
# OpenAI model parsing / custom IDs                                            #
# --------------------------------------------------------------------------- #

class TestParseOpenAIModel:
    def test_catalog_instant(self):
        assert parse_openai_model("gpt-5.5 (Instant)") == ("gpt-5.5", False)

    def test_catalog_thinking(self):
        assert parse_openai_model("gpt-5.4 (Thinking)") == ("gpt-5.4", True)

    def test_plain_model_untouched(self):
        assert parse_openai_model("gpt-4o") == ("gpt-4o", False)

    def test_custom_id_passes_through_verbatim(self):
        assert parse_openai_model("my-org/custom-ft-model") == (
            "my-org/custom-ft-model", False,
        )

    def test_default_when_empty(self):
        assert parse_openai_model("") == ("gpt-5.5", False)


# --------------------------------------------------------------------------- #
# Monkeypatched OpenAI mapping                                                 #
# --------------------------------------------------------------------------- #

class TestOpenAIProviderMapping:
    def test_new_model_uses_completion_token_budget(self, monkeypatch):
        completions = _fake_openai_module(monkeypatch, response=_openai_response())
        p = OpenAIProvider(api_key="sk-test", model="gpt-5.5 (Instant)")
        result = p.generate_result("q", [])
        kwargs = completions.last_kwargs
        assert kwargs["model"] == "gpt-5.5"
        assert kwargs["max_completion_tokens"] == 2000
        assert "max_tokens" not in kwargs
        assert result.content == "The answer."
        assert result.request_id == "req-123"
        assert result.usage == {
            "prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30,
        }
        assert result.finish_status == "stop"
        assert result.provenance["display_model"] == "gpt-5.5 (Instant)"
        assert result.latency_ms is not None

    def test_thinking_mode_gets_larger_budget(self, monkeypatch):
        completions = _fake_openai_module(monkeypatch, response=_openai_response())
        OpenAIProvider(api_key="sk-test", model="gpt-5.5 (Thinking)").generate_result("q", [])
        assert completions.last_kwargs["max_completion_tokens"] == 4000

    def test_legacy_model_uses_max_tokens_and_temperature(self, monkeypatch):
        completions = _fake_openai_module(monkeypatch, response=_openai_response())
        OpenAIProvider(api_key="sk-test", model="gpt-4o").generate_result("q", [])
        kwargs = completions.last_kwargs
        assert kwargs["max_tokens"] == 1000
        assert kwargs["temperature"] == 0.3
        assert "max_completion_tokens" not in kwargs

    def test_context_framing_matches_neutral_prompt(self, monkeypatch):
        completions = _fake_openai_module(monkeypatch, response=_openai_response())
        OpenAIProvider(api_key="sk-test", model="gpt-4o").generate_result(
            "What is the policy?", ["chunk one", "chunk two"],
        )
        messages = completions.last_kwargs["messages"]
        assert messages[0]["role"] == "system"
        assert "domain-aware" in messages[0]["content"]
        assert "chunk one" in messages[1]["content"]
        assert "What is the policy?" in messages[1]["content"]

    def test_empty_content_produces_diagnostic(self, monkeypatch):
        _fake_openai_module(
            monkeypatch,
            response=_openai_response(content=None, finish_reason="length"),
        )
        result = OpenAIProvider(
            api_key="sk-test", model="gpt-5.5 (Instant)",
        ).generate_result("q", [])
        assert "returned no content" in result.content
        assert "finish_reason=length" in result.content

    def test_custom_model_id_sent_verbatim(self, monkeypatch):
        completions = _fake_openai_module(monkeypatch, response=_openai_response())
        p = OpenAIProvider(api_key="sk-test", model="my-org/custom-ft-model")
        p.generate_result("q", [])
        assert completions.last_kwargs["model"] == "my-org/custom-ft-model"

    def test_model_not_found_surfaces_verbatim(self, monkeypatch):
        detail = "The model `gpt-99-ultra` does not exist or you do not have access to it."
        _fake_openai_module(monkeypatch, raise_exc=RuntimeError(detail))
        p = OpenAIProvider(api_key="sk-test", model="gpt-99-ultra")
        with pytest.raises(ProviderError) as excinfo:
            p.generate_result("q", [])
        # The provider's own message surfaces verbatim — the model is
        # never silently replaced with a different one.
        assert detail in str(excinfo.value)
        # A normalized error result is recorded for trace provenance.
        assert p.last_result is not None
        assert p.last_result.finish_status == "error"
        assert detail in p.last_result.error

    def test_api_key_never_in_result_or_provenance(self, monkeypatch):
        _fake_openai_module(monkeypatch, response=_openai_response())
        secret = "sk-test-SECRET-KEY-VALUE"
        p = OpenAIProvider(api_key=secret, model="gpt-4o")
        result = p.generate_result("q", [])
        assert secret not in str(result)
        assert secret not in str(result.provenance_summary())

    def test_missing_key_rejected_at_construction(self):
        with pytest.raises(ProviderError, match="OpenAI API key is required"):
            OpenAIProvider(api_key="", model="gpt-4o")


# --------------------------------------------------------------------------- #
# Error recording on the base contract                                         #
# --------------------------------------------------------------------------- #

class _ExplodingProvider(BaseLiveProvider):
    name = "exploding"

    def _call(self, query, context):
        raise ConnectionResetError("socket closed mid-flight")


class TestBaseErrorRecording:
    def test_unexpected_exception_normalized_and_recorded(self):
        p = _ExplodingProvider("some-model")
        with pytest.raises(ProviderError) as excinfo:
            p.generate_result("q", [])
        assert "ConnectionResetError" in excinfo.value.detail
        assert p.last_result is not None
        assert p.last_result.error == excinfo.value.detail
        assert p.last_result.finish_status == "error"
        assert p.last_result.content == ""
        assert p.last_result.latency_ms is not None


# --------------------------------------------------------------------------- #
# LLM workflow connector lifts provider provenance                             #
# --------------------------------------------------------------------------- #

class _ContractStubProvider(BaseLiveProvider):
    name = "stub"

    def __init__(self, model="stub-model", fail=False):
        super().__init__(model)
        self._fail = fail

    def _call(self, query, context):
        if self._fail:
            raise ProviderError(self.name, "stub provider down")
        return ProviderResult(
            provider=self.name,
            model=self.model,
            content=f"stub answer to {query}",
            request_id="stub-req-9",
            finish_status="stop",
        )


class _LegacyOnlyProvider:
    """Pre-contract provider: generate() only, no last_result."""

    def generate(self, query, context):
        return "legacy answer"


class TestLLMConnectorProvenanceLift:
    def _connector(self, provider):
        from tcs.workflow.connectors.llm import LLMConnector
        return LLMConnector(
            provider=provider, provider_name="stub", model="stub-model",
        )

    def test_success_path_lifts_provenance(self):
        from tcs.workflow.connector import ConnectorRequest
        provider = _ContractStubProvider()
        result = self._connector(provider).invoke(ConnectorRequest(query="hi"))
        assert result.error is None
        prov = result.raw_metadata["provider_provenance"]
        assert prov["provider"] == "stub"
        assert prov["model"] == "stub-model"
        assert prov["request_id"] == "stub-req-9"
        assert prov["finish_status"] == "stop"
        assert prov["error"] is None

    def test_error_path_lifts_error_provenance(self):
        from tcs.workflow.connector import ConnectorRequest
        provider = _ContractStubProvider(fail=True)
        result = self._connector(provider).invoke(ConnectorRequest(query="hi"))
        assert result.error is not None
        assert result.output_text is None
        prov = result.raw_metadata["provider_provenance"]
        assert prov["finish_status"] == "error"
        assert "stub provider down" in prov["error"]

    def test_legacy_provider_contributes_nothing(self):
        from tcs.workflow.connector import ConnectorRequest
        result = self._connector(_LegacyOnlyProvider()).invoke(
            ConnectorRequest(query="hi"),
        )
        assert result.output_text == "legacy answer"
        assert "provider_provenance" not in result.raw_metadata
