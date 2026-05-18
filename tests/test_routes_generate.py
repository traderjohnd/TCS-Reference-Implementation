"""
Phase 5 Slice 5.2 — /v2/generate + /v2/artifacts/{id} endpoint tests.

Acceptance criteria pinned:

  1. /v2/generate creates a ResponseArtifact.
  2. All four generation modes work end-to-end.
  3. raw_llm is truly raw: no RAG, no retrieved_sources, no hidden
     system prompt unless explicitly overridden.
  4. rag_llm records the exact derived system prompt + retrieved
     sources.
  5. human_composed creates an artifact WITHOUT calling any LLM
     provider (proven by patching the provider clients to raise).
  6. API key is request-scoped; never persisted on the artifact.
  7. The hardcoded "financial advisory" leftover is gone — the rag_llm
     prompt under a Medical Devices active pack is the clinical
     decision support framing.
  8. GET /v2/artifacts/{id} retrieves the artifact exactly as stored.
  9. GET /v2/artifacts/{id} does not call an LLM.
"""

from __future__ import annotations

import os
from typing import Any, Dict
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient


# --------------------------------------------------------------------------- #
# Fixtures                                                                     #
# --------------------------------------------------------------------------- #

@pytest.fixture
def client(tmp_path):
    """
    Per-test client backed by a tmp_path SQLite store. Each test
    gets a fresh DB so artifact IDs / decisions don't leak across
    tests, and so the active pack global is wiped at teardown.
    """
    os.environ["TCS_WORKFLOW_TRACE_ENABLED"] = "true"
    from tcs.api.app import create_app
    from tcs.persistence.certificate_store import CertificateStore
    from tcs.packs.pack_manager import (
        PACKS, clear_active_pack, unregister_composed_pack,
    )

    pre_existing_packs = set(PACKS.keys())
    db_path = tmp_path / "phase5_slice2.db"
    store = CertificateStore(str(db_path))
    app = create_app(store=store)
    c = TestClient(app)
    with c:
        yield c
    # Teardown: drop any composed packs this test created.
    for pid in (set(PACKS.keys()) - pre_existing_packs):
        try:
            unregister_composed_pack(pid)
        except Exception:
            pass
    clear_active_pack()
    store.close()
    os.environ.pop("TCS_WORKFLOW_TRACE_ENABLED", None)


# --------------------------------------------------------------------------- #
# Mode 1 — raw_llm transparency                                                #
# --------------------------------------------------------------------------- #

class TestRawLLMTransparency:
    def test_raw_llm_with_mock_provider_creates_artifact(self, client):
        r = client.post("/v2/generate", json={
            "generation_mode": "raw_llm",
            "prompt": "What is 2 + 2?",
            "provider": "mock",
            "model": "deterministic",
        })
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["generation_mode"] == "raw_llm"
        assert body["raw_output"]                          # non-empty
        assert body["raw_output"].startswith("[MOCK PROVIDER]")
        assert body["raw_output_hash"]                     # auto-derived
        assert body["artifact_id"]                         # UUID assigned

    def test_raw_llm_has_no_rag_signals(self, client):
        # The transparency invariant pinned by the user: raw_llm
        # must surface exactly that no retrieval happened and no
        # context was injected.
        r = client.post("/v2/generate", json={
            "generation_mode": "raw_llm",
            "prompt": "Tell me about lithium.",
            "provider": "mock",
        }).json()
        assert r["rag_enabled"] is False
        assert r["retrieved_sources"] == []

    def test_raw_llm_default_system_prompt_is_none(self, client):
        # No system prompt sent by default. The artifact records
        # system_prompt_used as None, not as some hidden domain frame.
        r = client.post("/v2/generate", json={
            "generation_mode": "raw_llm",
            "prompt": "Anything.",
            "provider": "mock",
        }).json()
        assert r["system_prompt_used"] is None

    def test_raw_llm_records_caller_supplied_system_prompt_verbatim(self, client):
        # If a caller WANTS a system prompt, it gets recorded verbatim
        # on the artifact. No hidden framing on top.
        custom = "You answer only in haiku."
        r = client.post("/v2/generate", json={
            "generation_mode": "raw_llm",
            "prompt": "What is 2 + 2?",
            "provider": "mock",
            "system_prompt_override": custom,
        }).json()
        # The mock client ignores system messages but the artifact
        # must record what the user passed for transparency.
        assert r["system_prompt_used"] == custom


# --------------------------------------------------------------------------- #
# Mode 2 — rag_llm: retrieved sources, industry-derived prompt                 #
# --------------------------------------------------------------------------- #

class TestRagLLMRetrieval:
    def test_rag_llm_records_retrieved_sources(self, client):
        r = client.post("/v2/generate", json={
            "generation_mode": "rag_llm",
            "prompt": "What does the policy say about lithium during pregnancy?",
            "provider": "mock",
            "industry_hint": "life_sciences",
            "retrieval_k": 3,
        }).json()
        assert r["rag_enabled"] is True
        sources = r["retrieved_sources"]
        assert isinstance(sources, list)
        # We don't pin an exact count because the corpus may grow;
        # we pin that retrieval ran and returned shape-correct rows.
        assert len(sources) >= 1
        for s in sources:
            assert "chunk_id" in s
            assert "source_doc" in s
            assert "similarity_score" in s

    def test_rag_llm_under_life_sciences_uses_clinical_prompt(self, client):
        # Acceptance criterion #7: the "financial advisory" hardcode
        # must be gone. Under a life_sciences industry hint, the
        # recorded system_prompt_used must reflect clinical framing,
        # NOT financial.
        r = client.post("/v2/generate", json={
            "generation_mode": "rag_llm",
            "prompt": "Lithium dosing question.",
            "provider": "mock",
            "industry_hint": "life_sciences",
        }).json()
        sp = r["system_prompt_used"] or ""
        assert "clinical decision support" in sp.lower(), (
            f"life_sciences should use clinical framing, got: {sp!r}"
        )
        assert "financial advisory" not in sp.lower(), (
            "leftover 'financial advisory' hardcode bled into a "
            "life_sciences rag_llm call"
        )

    def test_rag_llm_under_financial_uses_financial_prompt(self, client):
        # Symmetry: the financial framing is correct *when financial
        # is what's actually selected*.
        r = client.post("/v2/generate", json={
            "generation_mode": "rag_llm",
            "prompt": "What is Reg BI?",
            "provider": "mock",
            "industry_hint": "financial_services",
        }).json()
        sp = r["system_prompt_used"] or ""
        assert "financial advisory" in sp.lower()

    def test_rag_llm_with_no_industry_uses_neutral_prompt(self, client):
        # No industry → neutral grounding prompt. Never the financial
        # leftover.
        r = client.post("/v2/generate", json={
            "generation_mode": "rag_llm",
            "prompt": "Tell me something.",
            "provider": "mock",
            # no industry_hint, no active pack
        }).json()
        sp = r["system_prompt_used"] or ""
        assert "financial advisory" not in sp.lower()
        assert "clinical decision support" not in sp.lower()


# --------------------------------------------------------------------------- #
# Mode 3 — agent_workflow: full workflow trace captured                        #
# --------------------------------------------------------------------------- #

class TestAgentWorkflowTrace:
    def test_agent_workflow_records_workflow_trace_id(self, client):
        r = client.post("/v2/generate", json={
            "generation_mode": "agent_workflow",
            "prompt": "Summarize compliance requirements.",
            "provider": "mock",
            "industry_hint": "life_sciences",
        }).json()
        assert r["generation_mode"] == "agent_workflow"
        # The workflow_id (returned as workflow_trace_id on the
        # artifact) must be present — that's the whole reason to
        # use this mode over rag_llm.
        assert r["workflow_trace_id"], (
            "agent_workflow must populate workflow_trace_id"
        )
        # And RAG flags should match rag_llm semantics (RAG is part
        # of the workflow).
        assert r["rag_enabled"] is True

    def test_agent_workflow_full_artifact_includes_trace(self, client):
        # GET the artifact and verify the full workflow_trace dict
        # is present (with nodes).
        post = client.post("/v2/generate", json={
            "generation_mode": "agent_workflow",
            "prompt": "Anything.",
            "provider": "mock",
            "industry_hint": "life_sciences",
        }).json()
        full = client.get(f"/v2/artifacts/{post['artifact_id']}").json()
        assert full["workflow_trace"] is not None
        assert "nodes" in full["workflow_trace"]
        node_ids = [n["node_id"] for n in full["workflow_trace"]["nodes"]]
        assert "rag-retrieve" in node_ids
        assert "llm-generate" in node_ids


# --------------------------------------------------------------------------- #
# Mode 4 — human_composed: NEVER calls an LLM (load-bearing test)              #
# --------------------------------------------------------------------------- #

class TestHumanComposedNeverCallsLLM:
    def test_human_composed_succeeds_with_provider_clients_patched_to_raise(
        self, client,
    ):
        # If human_composed accidentally routes through any provider
        # client, these patches will raise and the test will fail.
        # This is the architectural guardrail the user pinned: the
        # flagship Phase 5 case (human writing to a pregnant client)
        # must work without an LLM in the loop.
        with patch(
            "tcs.artifacts.generation._call_openai",
            side_effect=AssertionError("OpenAI client called by human_composed!"),
        ), patch(
            "tcs.artifacts.generation._call_anthropic",
            side_effect=AssertionError("Anthropic client called by human_composed!"),
        ), patch(
            "tcs.artifacts.generation._call_mock",
            side_effect=AssertionError("Mock client called by human_composed!"),
        ):
            r = client.post("/v2/generate", json={
                "generation_mode": "human_composed",
                "draft": "Lithium is fine in small doses, no worries.",
                "recipient_context": {
                    "pregnant": True,
                    "role": "patient",
                    "channel": "outbound_message",
                    "medication_topic": "lithium",
                },
                "generation_identity": {
                    "requesting_identity": "rep-7",
                    "identity_type": "human",
                    "role": "patient_support_rep",
                },
            })

        assert r.status_code == 200, r.text
        body = r.json()
        assert body["generation_mode"] == "human_composed"
        assert body["provider"] is None        # no LLM was selected
        assert body["model"] is None
        assert body["system_prompt_used"] is None
        assert body["rag_enabled"] is False
        assert body["retrieved_sources"] == []
        # The draft is the raw_output.
        assert "lithium" in body["raw_output"].lower()

    def test_human_composed_rejects_empty_draft(self, client):
        r = client.post("/v2/generate", json={
            "generation_mode": "human_composed",
            "draft": "   ",
        })
        assert r.status_code == 400
        assert "draft" in r.json()["detail"].lower()


# --------------------------------------------------------------------------- #
# API key discipline                                                           #
# --------------------------------------------------------------------------- #

class TestApiKeyDiscipline:
    def test_api_key_is_not_persisted_on_artifact(self, client):
        # The key the caller sends in the request body must NOT
        # appear anywhere on the stored artifact JSON. The artifact
        # records provider and model identifiers, not credentials.
        r = client.post("/v2/generate", json={
            "generation_mode": "raw_llm",
            "prompt": "Hello.",
            "provider": "mock",     # mock provider ignores api_key
            "api_key": "sk-do-not-persist-this-anywhere",
        }).json()
        full = client.get(f"/v2/artifacts/{r['artifact_id']}").json()
        flat = repr(full)
        assert "sk-do-not-persist-this-anywhere" not in flat, (
            "API key leaked into the persisted artifact"
        )


# --------------------------------------------------------------------------- #
# GET /v2/artifacts/{id} — round-trip + no-LLM invariant                       #
# --------------------------------------------------------------------------- #

class TestGetArtifact:
    def test_get_returns_artifact_exactly_as_stored(self, client):
        # Generate, then GET; the returned dict should match what
        # ResponseArtifact.to_dict() produced at persist time.
        r = client.post("/v2/generate", json={
            "generation_mode": "rag_llm",
            "prompt": "Anything.",
            "provider": "mock",
            "industry_hint": "life_sciences",
        }).json()

        full = client.get(f"/v2/artifacts/{r['artifact_id']}").json()
        # Spot-check load-bearing fields.
        assert full["artifact_id"] == r["artifact_id"]
        assert full["generation_mode"] == "rag_llm"
        assert full["rag_enabled"] is True
        assert full["raw_output"] == r["raw_output"]
        # Full ResponseArtifact has fields the slim response did not
        # surface (rag_context, recipient_context, generation_identity).
        for field in (
            "rag_context", "recipient_context", "generation_identity",
            "workflow_trace_id", "workflow_trace", "generation_error",
            "created_at",
        ):
            assert field in full, f"GET missing field {field!r}"

    def test_get_unknown_artifact_returns_404(self, client):
        r = client.get("/v2/artifacts/this-id-does-not-exist")
        assert r.status_code == 404

    def test_get_does_not_call_any_llm(self, client):
        # GET is a pure read — must not touch any provider client
        # even by accident.
        post = client.post("/v2/generate", json={
            "generation_mode": "raw_llm",
            "prompt": "x",
            "provider": "mock",
        }).json()
        with patch(
            "tcs.artifacts.generation._call_openai",
            side_effect=AssertionError("GET should not call OpenAI"),
        ), patch(
            "tcs.artifacts.generation._call_anthropic",
            side_effect=AssertionError("GET should not call Anthropic"),
        ), patch(
            "tcs.artifacts.generation._call_mock",
            side_effect=AssertionError("GET should not call Mock"),
        ):
            r = client.get(f"/v2/artifacts/{post['artifact_id']}")
            assert r.status_code == 200


# --------------------------------------------------------------------------- #
# Input validation                                                              #
# --------------------------------------------------------------------------- #

class TestInputValidation:
    def test_raw_llm_without_prompt_rejected(self, client):
        r = client.post("/v2/generate", json={
            "generation_mode": "raw_llm",
            "provider": "mock",
        })
        assert r.status_code == 400
        assert "prompt" in r.json()["detail"].lower()

    def test_unknown_mode_rejected(self, client):
        r = client.post("/v2/generate", json={
            "generation_mode": "psychic_llm",
            "prompt": "...",
        })
        assert r.status_code == 400
        assert "unknown generation_mode" in r.json()["detail"].lower()


# --------------------------------------------------------------------------- #
# Active pack integration: industry hint resolution                            #
# --------------------------------------------------------------------------- #

class TestActivePackDrivesIndustry:
    def _deploy_meddev(self, client) -> Dict[str, Any]:
        return client.post("/v2/standards/deploy", json={
            "industry": "life_sciences",
            "sub_industry": "medical_devices",
            "use_case": "clinical_decision_support",
            "standard_ids": ["iso_13485", "iso_14971", "iec_62304"],
            "risk_tier": "r3", "action_class": "a4",
        }).json()

    def test_rag_llm_uses_active_pack_industry_when_hint_omitted(self, client):
        # Deploy MedDev pack, then call /v2/generate WITHOUT
        # industry_hint. The endpoint should resolve the industry
        # from the active pack and use the clinical framing.
        self._deploy_meddev(client)
        r = client.post("/v2/generate", json={
            "generation_mode": "rag_llm",
            "prompt": "Question.",
            "provider": "mock",
            # no industry_hint
        }).json()
        assert "clinical decision support" in (
            r["system_prompt_used"] or ""
        ).lower(), (
            f"active pack should drive industry resolution; got prompt: "
            f"{r['system_prompt_used']!r}"
        )

    def test_explicit_industry_hint_overrides_active_pack(self, client):
        self._deploy_meddev(client)
        r = client.post("/v2/generate", json={
            "generation_mode": "rag_llm",
            "prompt": "Question.",
            "provider": "mock",
            "industry_hint": "financial_services",   # explicit override
        }).json()
        sp = (r["system_prompt_used"] or "").lower()
        assert "financial advisory" in sp
        assert "clinical decision support" not in sp
