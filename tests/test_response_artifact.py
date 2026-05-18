"""
ResponseArtifact — dataclass tests (Phase 5 Slice 5.1).

Pins:
  - the four generation modes and their required-field rules
  - deterministic hashing (normalize_prompt + hash_text)
  - to_dict / from_dict round-trip preserves every field
  - immutability (frozen=True)
  - rag_context requires rag_enabled
  - human_composed requires a draft (raw_output) even without a prompt
  - all other modes require a prompt
"""

from __future__ import annotations

import pytest

from tcs.artifacts import (
    GENERATION_MODE_AGENT_WORKFLOW,
    GENERATION_MODE_HUMAN_COMPOSED,
    GENERATION_MODE_RAG_LLM,
    GENERATION_MODE_RAW_LLM,
    ResponseArtifact,
    hash_text,
    normalize_prompt,
)


# --------------------------------------------------------------------------- #
# Hashing primitives                                                           #
# --------------------------------------------------------------------------- #

class TestHashing:
    def test_normalize_collapses_whitespace_and_preserves_case(self):
        # Multiple kinds of whitespace, including tabs and newlines,
        # collapse to single spaces. Case + punctuation preserved
        # (those are part of semantic content).
        assert normalize_prompt("Hello\t\n  World!") == "Hello World!"
        assert normalize_prompt("  trim me  ") == "trim me"

    def test_hash_text_deterministic_across_whitespace_variants(self):
        # The whole point of normalization: trivial whitespace
        # differences must not change the hash.
        h1 = hash_text("Lithium dose in pregnancy?")
        h2 = hash_text("Lithium  dose  in  pregnancy?")
        h3 = hash_text("\tLithium dose in pregnancy?\n")
        assert h1 == h2 == h3

    def test_hash_text_distinguishes_semantic_content(self):
        # Case matters; word changes matter.
        assert hash_text("lithium dose") != hash_text("Lithium dose")
        assert hash_text("dose of lithium") != hash_text("dose of warfarin")

    def test_hash_text_rejects_none(self):
        with pytest.raises(TypeError):
            hash_text(None)


# --------------------------------------------------------------------------- #
# Generation mode required-field rules                                         #
# --------------------------------------------------------------------------- #

class TestGenerationModeRules:
    def test_raw_llm_requires_prompt(self):
        with pytest.raises(ValueError, match="require a prompt"):
            ResponseArtifact(generation_mode=GENERATION_MODE_RAW_LLM)

    def test_rag_llm_requires_prompt(self):
        with pytest.raises(ValueError, match="require a prompt"):
            ResponseArtifact(generation_mode=GENERATION_MODE_RAG_LLM)

    def test_agent_workflow_requires_prompt(self):
        with pytest.raises(ValueError, match="require a prompt"):
            ResponseArtifact(generation_mode=GENERATION_MODE_AGENT_WORKFLOW)

    def test_human_composed_requires_draft_or_error_not_prompt(self):
        # human_composed with neither raw_output nor a generation_error
        # is meaningless — there's nothing to govern.
        with pytest.raises(ValueError, match="raw_output"):
            ResponseArtifact(
                generation_mode=GENERATION_MODE_HUMAN_COMPOSED,
                prompt=None,
                raw_output=None,
            )

    def test_human_composed_with_only_draft_is_valid(self):
        # The flagship Phase 5 case: a human typed a draft message;
        # there is no prompt frame, just outbound content.
        a = ResponseArtifact(
            generation_mode=GENERATION_MODE_HUMAN_COMPOSED,
            prompt=None,
            raw_output="Lithium is fine in small doses, no worries.",
            recipient_context={
                "pregnant": True,
                "role": "patient",
                "channel": "outbound_message",
                "medication_topic": "lithium",
            },
        )
        assert a.raw_output is not None
        assert a.prompt is None
        # Hash auto-derived for raw_output even when prompt is None.
        assert a.raw_output_hash is not None
        assert a.prompt_hash is None

    def test_raw_llm_with_prompt_and_output_is_valid(self):
        a = ResponseArtifact(
            generation_mode=GENERATION_MODE_RAW_LLM,
            prompt="What is the capital of France?",
            raw_output="Paris.",
            provider="openai",
            model="gpt-5.5",
        )
        assert a.prompt_hash == hash_text("What is the capital of France?")
        assert a.raw_output_hash == hash_text("Paris.")

    def test_unknown_generation_mode_rejected(self):
        with pytest.raises(ValueError, match="unknown generation_mode"):
            ResponseArtifact(
                generation_mode="psychic_llm",
                prompt="ignored",
            )


# --------------------------------------------------------------------------- #
# Sanity checks on field interplay                                             #
# --------------------------------------------------------------------------- #

class TestFieldInterplay:
    def test_rag_context_requires_rag_enabled(self):
        # Catches the regression where a refactor sets rag_context but
        # forgets to flip rag_enabled — that would break audit because
        # the artifact would say "no retrieval ran" while carrying
        # retrieved content.
        with pytest.raises(ValueError, match="rag_enabled is False"):
            ResponseArtifact(
                generation_mode=GENERATION_MODE_RAG_LLM,
                prompt="anything",
                raw_output="response",
                rag_enabled=False,
                rag_context="some retrieved text",
            )

    def test_rag_enabled_without_context_is_allowed(self):
        # rag_enabled with no context is fine — represents a retrieval
        # call that returned zero results.
        a = ResponseArtifact(
            generation_mode=GENERATION_MODE_RAG_LLM,
            prompt="anything",
            rag_enabled=True,
            rag_context=None,
        )
        assert a.rag_enabled is True

    def test_caller_supplied_hash_is_not_overwritten(self):
        # If a caller stamps a hash explicitly (e.g. from a connector
        # that already computed it), the dataclass keeps the supplied
        # value rather than re-deriving.
        a = ResponseArtifact(
            generation_mode=GENERATION_MODE_RAW_LLM,
            prompt="x",
            prompt_hash="caller_supplied_hash",
        )
        assert a.prompt_hash == "caller_supplied_hash"


# --------------------------------------------------------------------------- #
# Immutability                                                                  #
# --------------------------------------------------------------------------- #

class TestImmutability:
    def test_frozen_dataclass_blocks_attribute_assignment(self):
        a = ResponseArtifact(
            generation_mode=GENERATION_MODE_RAW_LLM,
            prompt="x",
            raw_output="y",
        )
        with pytest.raises(Exception):  # dataclasses.FrozenInstanceError
            a.raw_output = "tampered"  # type: ignore[misc]


# --------------------------------------------------------------------------- #
# Serialization                                                                #
# --------------------------------------------------------------------------- #

class TestSerialization:
    def _build_full_artifact(self) -> ResponseArtifact:
        return ResponseArtifact(
            generation_mode=GENERATION_MODE_RAG_LLM,
            prompt="What's the policy on lithium during pregnancy?",
            raw_output="I can't provide a specific dose; please consult a clinician.",
            provider="openai",
            model="gpt-5.5",
            system_prompt_used="You are a clinical decision support AI...",
            rag_enabled=True,
            rag_context="Retrieved chunks here.",
            retrieved_sources=[
                {"chunk_id": "c1", "source_doc": "clinical_protocols.md",
                 "version": "v2026-01", "similarity_score": 0.917},
                {"chunk_id": "c2", "source_doc": "drug_interactions.md",
                 "version": "v2026-01", "similarity_score": 0.893},
            ],
            workflow_trace_id="trace-abc",
            workflow_trace={"nodes": [{"id": "rag"}, {"id": "llm"}]},
            recipient_context={"role": "clinician", "session_id": "s-42"},
            generation_identity={
                "requesting_identity": "user-1",
                "identity_type": "human",
                "role": "clinician",
                "session_id": "s-42",
            },
        )

    def test_to_dict_includes_all_fields(self):
        a = self._build_full_artifact()
        d = a.to_dict()
        # Spot-check critical fields are present and serializable.
        for field_name in (
            "artifact_id", "created_at", "generation_mode",
            "prompt", "prompt_hash", "raw_output", "raw_output_hash",
            "provider", "model", "system_prompt_used", "rag_enabled",
            "rag_context", "retrieved_sources", "workflow_trace_id",
            "workflow_trace", "recipient_context", "generation_identity",
            "generation_error",
        ):
            assert field_name in d, f"missing field in to_dict: {field_name}"

    def test_round_trip_preserves_every_field(self):
        original = self._build_full_artifact()
        restored = ResponseArtifact.from_dict(original.to_dict())
        # Compare via to_dict so we don't rely on datetime equality
        # quirks across tz-aware reconstruction.
        assert original.to_dict() == restored.to_dict()

    def test_round_trip_preserves_human_composed_with_null_prompt(self):
        original = ResponseArtifact(
            generation_mode=GENERATION_MODE_HUMAN_COMPOSED,
            prompt=None,
            raw_output="Draft outbound message text.",
            recipient_context={"pregnant": True, "channel": "email"},
            generation_identity={"requesting_identity": "rep-7",
                                 "identity_type": "human"},
        )
        restored = ResponseArtifact.from_dict(original.to_dict())
        assert restored.prompt is None
        assert restored.raw_output == "Draft outbound message text."
        assert restored.recipient_context["pregnant"] is True

    def test_generation_error_recorded_with_null_output(self):
        # A failed generation still produces an artifact for audit.
        a = ResponseArtifact(
            generation_mode=GENERATION_MODE_RAW_LLM,
            prompt="something",
            raw_output=None,
            generation_error="LLM provider returned 429",
        )
        assert a.generation_error == "LLM provider returned 429"
        assert a.raw_output is None
        assert a.raw_output_hash is None
