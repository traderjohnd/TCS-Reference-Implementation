"""
Commit 5/6 — governed Live Web (/v2/query/web): mode enforcement,
explicit retrieval modes, OpenAI Responses / Anthropic Messages web
mapping, truthful retrieval statuses, certificate binding + tamper
detection, replay digest verification, secret non-exposure.
"""

from __future__ import annotations

import os
import sys
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from tcs.api.app import create_app
from tcs.providers.anthropic_web import (
    ANTHROPIC_WEB_SEARCH_TOOL_VERSION,
    AnthropicWebProvider,
)
from tcs.providers.base import ProviderError
from tcs.providers.openai_web import OpenAIWebProvider


# --------------------------------------------------------------------------- #
# Fixtures + fakes                                                             #
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
    assert r.status_code == 200


def _openai_web_response(searches=1, sources=1, cite=True,
                         text="Grounded answer from the web.",
                         search_status="completed"):
    output = []
    for i in range(searches):
        srcs = [SimpleNamespace(
            url=f"https://example.com/doc{j}",
            title=f"Doc {j}", id=f"s{j}", type="url",
        ) for j in range(sources)]
        output.append(SimpleNamespace(
            type="web_search_call", id=f"ws_{i}", status=search_status,
            action=SimpleNamespace(type="search", query=f"query {i}",
                                   sources=srcs),
            error=None,
        ))
    annotations = []
    if cite and text:
        annotations = [SimpleNamespace(
            type="url_citation", url="https://example.com/doc0",
            title="Doc 0", start_index=0, end_index=10,
        )]
    content = [] if text is None else [SimpleNamespace(
        type="output_text", text=text, annotations=annotations)]
    output.append(SimpleNamespace(type="message", content=content))
    return SimpleNamespace(
        id="resp_1", output=output, status="completed",
        usage=SimpleNamespace(input_tokens=100, output_tokens=40,
                              total_tokens=140),
    )


def _fake_openai_web(monkeypatch, response=None, raise_exc=None):
    class _Responses:
        def __init__(self):
            self.last_kwargs = None

        def create(self, **kwargs):
            self.last_kwargs = kwargs
            if raise_exc is not None:
                raise raise_exc
            return response

    responses = _Responses()
    fake = SimpleNamespace(OpenAI=lambda api_key=None: SimpleNamespace(
        responses=responses))
    monkeypatch.setitem(sys.modules, "openai", fake)
    return responses


def _anthropic_web_response(searches=1, sources=1, cite=True,
                            text="Claude web-grounded answer.",
                            stop_reason="end_turn", search_error=None):
    content = []
    for i in range(searches):
        content.append(SimpleNamespace(
            type="server_tool_use", id=f"tu_{i}", name="web_search",
            input={"query": f"query {i}", "extra_arg": "never-retained"},
        ))
        if search_error is not None:
            content.append(SimpleNamespace(
                type="web_search_tool_result", tool_use_id=f"tu_{i}",
                content=SimpleNamespace(
                    type="web_search_tool_result_error",
                    error_code=search_error),
            ))
        else:
            content.append(SimpleNamespace(
                type="web_search_tool_result", tool_use_id=f"tu_{i}",
                content=[SimpleNamespace(
                    type="web_search_result",
                    url=f"https://example.org/page{j}",
                    title=f"Page {j}", page_age="January 2, 2026",
                    encrypted_content="OPAQUE-ENCRYPTED-BLOB",
                ) for j in range(sources)],
            ))
    if text is not None:
        citations = []
        if cite:
            citations = [SimpleNamespace(
                type="web_search_result_location",
                url="https://example.org/page0", title="Page 0",
                cited_text="the cited excerpt",
            )]
        content.append(SimpleNamespace(type="text", text=text,
                                       citations=citations))
    return SimpleNamespace(
        id="msg_web_1", stop_reason=stop_reason, content=content,
        usage=SimpleNamespace(
            input_tokens=90, output_tokens=30,
            server_tool_use=SimpleNamespace(web_search_requests=1),
        ),
    )


def _fake_anthropic_web(monkeypatch, response=None, raise_exc=None):
    class _Messages:
        def __init__(self):
            self.last_kwargs = None

        def create(self, **kwargs):
            self.last_kwargs = kwargs
            if raise_exc is not None:
                raise raise_exc
            return response

    messages = _Messages()
    fake = SimpleNamespace(Anthropic=lambda api_key=None: SimpleNamespace(
        messages=messages))
    monkeypatch.setitem(sys.modules, "anthropic", fake)
    return messages


def _web(client, **overrides):
    payload = {
        "query": "What changed in the 2026 retention rules?",
        "provider": "openai", "model": "gpt-4o", "api_key": "sk-web",
        "retrieval_mode": "live_web",
    }
    payload.update(overrides)
    return client.post("/v2/query/web", json=payload)


# --------------------------------------------------------------------------- #
# Validation + mode enforcement                                                #
# --------------------------------------------------------------------------- #

class TestValidationAndModes:
    def test_demo_blocks_all_web_calls_before_provider_construction(
            self, client, monkeypatch):
        # Poison provider construction: the block must happen first.
        monkeypatch.setitem(sys.modules, "openai", None)
        monkeypatch.setitem(sys.modules, "anthropic", None)
        for provider in ("openai", "anthropic"):
            r = _web(client, provider=provider,
                     model="m", api_key="sk-x")
            assert r.status_code == 403
            assert r.json()["detail"]["error"] == "demo_mode_enforced"

    def test_retrieval_mode_must_be_explicit(self, client):
        _go_live(client)
        r = client.post("/v2/query/web", json={
            "query": "q", "provider": "openai", "model": "m",
            "api_key": "sk",
        })
        assert r.status_code == 422
        assert r.json()["detail"]["error"] == "retrieval_mode_required"

    def test_unknown_retrieval_mode_rejected(self, client):
        _go_live(client)
        r = _web(client, retrieval_mode="web_maybe")
        assert r.status_code == 422
        assert r.json()["detail"]["error"] == "unknown_retrieval_mode"

    def test_local_only_not_silently_upgraded(self, client):
        _go_live(client)
        r = _web(client, retrieval_mode="local_only")
        assert r.status_code == 422
        assert r.json()["detail"]["error"] == \
            "retrieval_mode_not_supported_here"

    def test_unsupported_provider_rejected(self, client):
        _go_live(client)
        r = _web(client, provider="mock")
        assert r.status_code == 422
        assert r.json()["detail"]["error"] == "provider_not_supported"

    def test_missing_credential_rejected(self, client):
        _go_live(client)
        r = _web(client, api_key=None)
        assert r.status_code == 422
        assert r.json()["detail"]["error"] == "missing_credential"

    def test_precise_coordinates_rejected(self, client):
        _go_live(client)
        r = _web(client, user_location={"latitude": "48.85",
                                        "longitude": "2.35"})
        assert r.status_code == 422
        assert r.json()["detail"]["error"] == "invalid_location"


# --------------------------------------------------------------------------- #
# OpenAI mapping                                                               #
# --------------------------------------------------------------------------- #

class TestOpenAIWebMapping:
    def test_request_configuration(self, monkeypatch):
        responses = _fake_openai_web(monkeypatch,
                                     response=_openai_web_response())
        p = OpenAIWebProvider(api_key="sk-w", model="gpt-4o")
        p.run_web_query("q", [], allowed_domains=["example.com"],
                        user_location={"city": "Boston",
                                       "country": "US"})
        k = responses.last_kwargs
        # Current hosted tool, never the legacy preview tool.
        assert k["tools"][0]["type"] == "web_search"
        assert "preview" not in str(k["tools"])
        # Live Web REQUIRES the search tool.
        assert k["tool_choice"] == {"type": "web_search"}
        # Complete source list requested via include.
        assert k["include"] == ["web_search_call.action.sources"]
        assert k["tools"][0]["filters"]["allowed_domains"] == ["example.com"]
        assert k["tools"][0]["user_location"]["type"] == "approximate"
        assert k["model"] == "gpt-4o"  # never substituted

    def test_success_parsing(self, monkeypatch):
        _fake_openai_web(monkeypatch,
                         response=_openai_web_response(sources=2))
        p = OpenAIWebProvider(api_key="sk-w", model="gpt-4o")
        text, ev = p.run_web_query("q", [])
        assert text == "Grounded answer from the web."
        assert ev.retrieval_status == "success"
        assert ev.search_call_count == 1
        assert ev.search_actions[0].query == "query 0"
        assert ev.consulted_source_count == 2
        assert ev.cited_source_count == 1
        assert ev.answer_used_web_evidence is True
        assert ev.live_access_confirmed is True
        assert ev.provider_request_id == "resp_1"
        cit = ev.citations[0]
        assert cit.provider_annotation_type == "url_citation"
        assert cit.start_offset == 0 and cit.end_offset == 10

    def test_no_search_action_is_retrieval_not_performed(
            self, client, monkeypatch):
        _go_live(client)
        _fake_openai_web(monkeypatch,
                         response=_openai_web_response(searches=0,
                                                       cite=False))
        r = _web(client)
        assert r.status_code == 200
        body = r.json()
        assert body["retrieval_status"] == "retrieval_not_performed"
        assert body["decision"] is None
        assert body["certificate_id"] is None

    def test_provider_retrieval_error_surfaces(self, client, monkeypatch):
        _go_live(client)
        _fake_openai_web(monkeypatch,
                         raise_exc=RuntimeError("web_search not supported "
                                                "for this model"))
        r = _web(client)
        body = r.json()
        assert body["retrieval_status"] == "provider_error"
        assert "not supported" in body["error"]
        assert body["certificate_id"] is None


# --------------------------------------------------------------------------- #
# Anthropic mapping                                                            #
# --------------------------------------------------------------------------- #

class TestAnthropicWebMapping:
    def test_direct_search_configuration_no_dynamic_filtering(
            self, monkeypatch):
        messages = _fake_anthropic_web(
            monkeypatch, response=_anthropic_web_response())
        p = AnthropicWebProvider(api_key="sk-a", model="claude-opus-5")
        p.run_web_query("q", [], max_searches=3)
        k = messages.last_kwargs
        tool = k["tools"][0]
        # Centralized direct-execution version — explicitly NOT the
        # dynamic-filtering (code-execution) 20260209 variant.
        assert tool["type"] == ANTHROPIC_WEB_SEARCH_TOOL_VERSION
        assert tool["type"] == "web_search_20250305"
        assert "20260209" not in str(k)
        assert tool["name"] == "web_search"
        assert tool["max_uses"] == 3          # bounded search use
        assert k["model"] == "claude-opus-5"  # never substituted

    def test_max_searches_bounded(self, monkeypatch):
        messages = _fake_anthropic_web(
            monkeypatch, response=_anthropic_web_response())
        AnthropicWebProvider(api_key="sk-a", model="claude-opus-5") \
            .run_web_query("q", [], max_searches=999)
        assert messages.last_kwargs["tools"][0]["max_uses"] == 10

    def test_success_parsing(self, monkeypatch):
        _fake_anthropic_web(monkeypatch,
                            response=_anthropic_web_response(sources=2))
        p = AnthropicWebProvider(api_key="sk-a", model="claude-opus-5")
        text, ev = p.run_web_query("q", [])
        assert text == "Claude web-grounded answer."
        assert ev.retrieval_status == "success"
        assert ev.search_actions[0].query == "query 0"
        assert ev.consulted_source_count == 2
        assert ev.cited_source_count == 1
        src = ev.consulted_sources[0]
        assert src.page_age == "January 2, 2026"   # verbatim string
        cit = ev.citations[0]
        # Block-level relationship retained; offsets never invented.
        assert cit.text_block_ordinal == 0
        assert cit.start_offset is None and cit.end_offset is None
        assert cit.cited_text == "the cited excerpt"
        assert ev.provider_request_id == "msg_web_1"

    def test_encrypted_content_and_tool_args_never_retained(
            self, monkeypatch):
        _fake_anthropic_web(monkeypatch,
                            response=_anthropic_web_response())
        p = AnthropicWebProvider(api_key="sk-a", model="claude-opus-5")
        _text, ev = p.run_web_query("q", [])
        blob = str(ev.to_dict())
        assert "OPAQUE-ENCRYPTED-BLOB" not in blob
        assert "never-retained" not in blob

    def test_in_body_search_error_is_retrieval_error(self, monkeypatch):
        _fake_anthropic_web(
            monkeypatch,
            response=_anthropic_web_response(search_error="max_uses_exceeded",
                                             cite=False,
                                             text="ungrounded text"),
        )
        p = AnthropicWebProvider(api_key="sk-a", model="claude-opus-5")
        _text, ev = p.run_web_query("q", [])
        assert ev.retrieval_status == "retrieval_error"
        assert ev.search_actions[0].status == "error"
        assert ev.search_actions[0].error_code == "max_uses_exceeded"
        assert "max_uses_exceeded" in ev.error_summary

    def test_no_results_response(self, monkeypatch):
        _fake_anthropic_web(
            monkeypatch,
            response=_anthropic_web_response(sources=0, cite=False,
                                             text="nothing found"),
        )
        p = AnthropicWebProvider(api_key="sk-a", model="claude-opus-5")
        _text, ev = p.run_web_query("q", [])
        assert ev.retrieval_status == "no_results"

    def test_pause_turn_never_certified(self, client, monkeypatch):
        _go_live(client)
        _fake_anthropic_web(
            monkeypatch,
            response=_anthropic_web_response(stop_reason="pause_turn",
                                             text=None, cite=False),
        )
        r = _web(client, provider="anthropic", model="claude-opus-5",
                 api_key="sk-a")
        body = r.json()
        assert body["retrieval_status"] == "paused"
        assert body["certificate_id"] is None
        assert body["decision"] is None


# --------------------------------------------------------------------------- #
# Governed path: certification, binding, tamper, replay                        #
# --------------------------------------------------------------------------- #

class TestGovernedWebPath:
    def _run_success(self, client, monkeypatch, **kw):
        _go_live(client)
        _fake_openai_web(monkeypatch, response=_openai_web_response())
        r = _web(client, **kw)
        assert r.status_code == 200
        return r

    def test_full_governed_path_with_distinct_nodes(self, client, monkeypatch):
        r = self._run_success(client, monkeypatch)
        body = r.json()
        assert body["retrieval_status"] == "success"
        assert body["decision"] is not None
        assert body["certificate_id"]
        node_ids = [n["node_id"] for n in body["workflow_trace"]["nodes"]]
        assert "local-corpus-retrieval" in node_ids
        assert "provider-hosted-web-retrieval" in node_ids
        assert "llm-generate" in node_ids
        assert body["local_corpus_used"] is True
        assert body["execution_mode"] == "live_provider"

    def test_certificate_binding_and_old_certs_unchanged(
            self, client, monkeypatch):
        r = self._run_success(client, monkeypatch)
        body = r.json()
        tc = client.get(f"/v2/certificates/{body['certificate_id']}").json()
        wr = tc["scope_attestation"]["web_retrieval"]
        assert wr["schema_version"] == "web-evidence-v1"
        assert wr["retrieval_status"] == "success"
        assert wr["web_evidence_digest"] == body["web_evidence_digest"]
        assert wr["evidence_artifact_id"] == body["artifact_id"]
        # Bounded: never the source list or citations inside the TC.
        assert "consulted_sources" not in wr and "citations" not in wr
        # A non-web certificate has no web_retrieval key at all.
        r2 = client.post("/v2/query", json={
            "query": "What is the retention policy?",
            "provider": "mock", "model": "deterministic",
        })
        tc2 = client.get(
            f"/v2/certificates/{r2.json()['certificate_id']}").json()
        assert "web_retrieval" not in tc2["scope_attestation"]

    def test_partial_recorded_on_certificate(self, client, monkeypatch):
        _go_live(client)
        resp = _openai_web_response(searches=2)
        # Second search failed; first succeeded with sources + citation.
        resp.output[1].status = "failed"
        resp.output[1].action = SimpleNamespace(type="search",
                                                query="query 1",
                                                sources=[])
        _fake_openai_web(monkeypatch, response=resp)
        r = _web(client)
        body = r.json()
        assert body["retrieval_status"] == "partial"
        assert body["certificate_id"]
        tc = client.get(f"/v2/certificates/{body['certificate_id']}").json()
        assert tc["scope_attestation"]["web_retrieval"][
            "retrieval_status"] == "partial"

    def test_evidence_persisted_and_replay_verifies_digest(
            self, client, monkeypatch):
        r = self._run_success(client, monkeypatch)
        body = r.json()
        # Poison both SDKs: replay must never re-execute retrieval.
        _fake_openai_web(monkeypatch,
                         raise_exc=AssertionError("no re-execution"))
        _fake_anthropic_web(monkeypatch,
                            raise_exc=AssertionError("no re-execution"))
        rr = client.post("/v2/replay", json={
            "artifact_id": body["artifact_id"],
            "configurations": [{"mode": "observe"}],
        })
        assert rr.status_code == 200
        assert rr.json()["count"] == 1

    def test_tampered_evidence_refuses_replay(self, client, monkeypatch):
        r = self._run_success(client, monkeypatch)
        body = r.json()
        artifact_store = client.app.state.artifact_store
        # Simulate post-issuance tampering of the stored evidence: the
        # replay route reads through get_artifact, so intercept it and
        # alter one source URL.
        real_get = artifact_store.get_artifact

        def tampered_get(artifact_id):
            art = real_get(artifact_id)
            if artifact_id == body["artifact_id"]:
                art.recipient_context["web_evidence"][
                    "consulted_sources"][0]["display_url"] = \
                    "https://evil.example/tampered"
            return art

        monkeypatch.setattr(artifact_store, "get_artifact", tampered_get)
        rr = client.post("/v2/replay", json={
            "artifact_id": body["artifact_id"],
            "configurations": [{"mode": "observe"}],
        })
        assert rr.status_code == 409
        assert rr.json()["detail"]["error"] == "evidence_digest_mismatch"

    def test_tampered_scope_summary_fails_certificate_verification(
            self, client, monkeypatch):
        from tcs.trust_certificate import compute_tc_hash
        r = self._run_success(client, monkeypatch)
        body = r.json()
        tc = client.get(f"/v2/certificates/{body['certificate_id']}").json()
        recomputed = compute_tc_hash(tc)
        assert recomputed == tc["audit_integrity"]["tc_hash"]
        tc["scope_attestation"]["web_retrieval"]["web_evidence_digest"] = \
            "0" * 64
        assert compute_tc_hash(tc) != tc["audit_integrity"]["tc_hash"]

    def test_numerical_core_untouched_for_equivalent_output(
            self, client, monkeypatch):
        # Two identical web runs produce identical decimal results —
        # the web path feeds evidence only; TIS arithmetic is unchanged.
        r1 = self._run_success(client, monkeypatch)
        r2 = self._run_success(client, monkeypatch)
        b1, b2 = r1.json(), r2.json()
        assert b1["component_scores"] == b2["component_scores"]
        assert b1["tis_current"] == b2["tis_current"]
        assert b1["decision"] == b2["decision"]

    def test_empty_web_output_keeps_empty_semantics(self, client, monkeypatch):
        _go_live(client)
        _fake_openai_web(monkeypatch,
                         response=_openai_web_response(text=None,
                                                       cite=False))
        r = _web(client)
        body = r.json()
        assert body["retrieval_status"] == "empty_output"
        assert body["certificate_id"] is None
        assert body["decision"] is None

    def test_api_key_never_in_response_evidence_or_certificate(
            self, client, monkeypatch):
        secret = "sk-web-LIVE-SECRET-99"
        r = self._run_success(client, monkeypatch, api_key=secret)
        assert secret not in r.text
        body = r.json()
        tc = client.get(f"/v2/certificates/{body['certificate_id']}")
        assert secret not in tc.text
        artifact = client.app.state.artifact_store.get_artifact(
            body["artifact_id"])
        assert secret not in str(artifact.__dict__)

    def test_web_provider_error_echoing_key_is_sanitized(
            self, client, monkeypatch):
        _go_live(client)
        secret = "sk-web-ECHOED-SECRET"
        _fake_openai_web(monkeypatch,
                         raise_exc=RuntimeError(f"401 key {secret} bad"))
        r = _web(client, api_key=secret)
        assert secret not in r.text
        assert "[redacted]" in r.json()["error"]

    def test_no_code_execution_node_in_trace(self, client, monkeypatch):
        r = self._run_success(client, monkeypatch)
        trace = r.json()["workflow_trace"]
        assert "code_execution" not in str(trace)
