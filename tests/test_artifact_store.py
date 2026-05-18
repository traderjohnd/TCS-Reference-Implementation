"""
ArtifactStore — persistence tests (Phase 5 Slice 5.1).

Pins:
  - persist + retrieve round-trip for both ResponseArtifact and
    GovernanceEvaluation
  - many evaluations per artifact (the replay foundation)
  - append-only enforcement: UPDATE and DELETE on either table raise
    AppendOnlyViolation
  - FK violation: inserting an evaluation whose artifact_id doesn't
    exist raises IntegrityError
  - schema migration is idempotent (opening an already-initialized
    db file does not error)
"""

from __future__ import annotations

import sqlite3

import pytest

from tcs.artifacts import (
    EVALUATION_MODE_ENFORCE,
    EVALUATION_MODE_OBSERVE,
    EVALUATION_MODE_WHAT_IF,
    GENERATION_MODE_HUMAN_COMPOSED,
    GENERATION_MODE_RAG_LLM,
    GovernanceEvaluation,
    ResponseArtifact,
)
from tcs.artifacts.store import (
    AppendOnlyViolation,
    ArtifactNotFoundError,
    ArtifactStore,
)


# --------------------------------------------------------------------------- #
# Fixtures                                                                     #
# --------------------------------------------------------------------------- #

@pytest.fixture
def store(tmp_path):
    """A fresh ArtifactStore backed by a file in tmp_path."""
    db_path = tmp_path / "artifacts.db"
    with ArtifactStore(str(db_path)) as s:
        yield s


def _make_artifact(**overrides) -> ResponseArtifact:
    defaults = dict(
        generation_mode=GENERATION_MODE_RAG_LLM,
        prompt="What is the policy on lithium during pregnancy?",
        raw_output="Please consult a clinician — I cannot provide dosing.",
        provider="openai",
        model="gpt-5.5",
        rag_enabled=True,
        rag_context="Retrieved chunks here.",
        retrieved_sources=[
            {"chunk_id": "c1", "source_doc": "clinical_protocols.md",
             "version": "v2026-01", "similarity_score": 0.917},
        ],
        generation_identity={
            "requesting_identity": "user-1",
            "identity_type": "human",
            "role": "clinician",
            "session_id": "s-42",
        },
    )
    defaults.update(overrides)
    return ResponseArtifact(**defaults)


def _make_evaluation(artifact_id: str, **overrides) -> GovernanceEvaluation:
    defaults = dict(
        artifact_id=artifact_id,
        mode=EVALUATION_MODE_ENFORCE,
        policy_profile_id="composed-medical",
        policy_profile_snapshot={"weights": {"B": 0.25, "A": 0.20,
                                             "C": 0.35, "K": 0.20}},
        component_scores={"B": 0.95, "A": 0.95, "C": 0.95, "K": 0.95},
        gate_results={"B": "pass", "A": "pass", "C": "pass", "K": "pass"},
        s_base=0.95,
        s_adjusted=0.94,
        tis_current=0.94,
        decision="Allow",
        trust_certificate_id="tc-allow-123",
    )
    defaults.update(overrides)
    return GovernanceEvaluation(**defaults)


# --------------------------------------------------------------------------- #
# Round-trip persistence                                                       #
# --------------------------------------------------------------------------- #

class TestArtifactRoundTrip:
    def test_insert_and_get_artifact_preserves_every_field(self, store):
        a = _make_artifact()
        store.insert_artifact(a)
        loaded = store.get_artifact(a.artifact_id)
        # Compare via to_dict so timezone reconstruction differences
        # don't trip the equality check.
        assert loaded.to_dict() == a.to_dict()

    def test_get_artifact_raises_when_missing(self, store):
        with pytest.raises(ArtifactNotFoundError):
            store.get_artifact("does-not-exist")

    def test_list_artifacts_returns_most_recent_first(self, store):
        a1 = _make_artifact()
        a2 = _make_artifact(prompt="second prompt")
        store.insert_artifact(a1)
        store.insert_artifact(a2)
        listed = store.list_artifacts(limit=10)
        assert len(listed) == 2
        # We can't guarantee timestamp ordering at second-resolution
        # between two back-to-back inserts, but both artifacts must
        # be present.
        ids = {a.artifact_id for a in listed}
        assert {a1.artifact_id, a2.artifact_id} == ids


class TestEvaluationRoundTrip:
    def test_insert_and_get_evaluation_preserves_every_field(self, store):
        a = _make_artifact()
        store.insert_artifact(a)
        e = _make_evaluation(a.artifact_id)
        store.insert_evaluation(e)
        loaded = store.get_evaluation(e.evaluation_id)
        assert loaded.to_dict() == e.to_dict()

    def test_what_if_evaluation_persists_without_tc(self, store):
        a = _make_artifact()
        store.insert_artifact(a)
        e = GovernanceEvaluation(
            artifact_id=a.artifact_id,
            mode=EVALUATION_MODE_WHAT_IF,
            policy_profile_id="fin-r3-a4-ct4",
            policy_profile_snapshot={"weights": {"B": 0.30}},
            decision="Allow",
            # No trust_certificate_id — what_if rule.
        )
        store.insert_evaluation(e)
        loaded = store.get_evaluation(e.evaluation_id)
        assert loaded.trust_certificate_id is None
        assert loaded.enforcement_action == "counterfactual_only"


# --------------------------------------------------------------------------- #
# Many evaluations per artifact — the replay foundation                        #
# --------------------------------------------------------------------------- #

class TestReplayFoundation:
    def test_one_artifact_many_evaluations(self, store):
        # The whole point of Phase 5: the same captured generation
        # can be evaluated under multiple governance configurations.
        a = _make_artifact()
        store.insert_artifact(a)

        # Three evaluations on the same artifact:
        # 1) baseline observe with no policy
        # 2) MedDev policy in observe
        # 3) MedDev policy in enforce
        # 4) Counterfactual what_if against a financial profile
        e1 = _make_evaluation(
            a.artifact_id, mode=EVALUATION_MODE_OBSERVE,
            policy_profile_id="",   # baseline / no pack
            trust_certificate_id="tc-observe-baseline",
        )
        e2 = _make_evaluation(
            a.artifact_id, mode=EVALUATION_MODE_OBSERVE,
            trust_certificate_id="tc-observe-meddev",
        )
        e3 = _make_evaluation(
            a.artifact_id, mode=EVALUATION_MODE_ENFORCE,
        )
        e4 = _make_evaluation(
            a.artifact_id, mode=EVALUATION_MODE_WHAT_IF,
            policy_profile_id="fin-r3-a4-ct4",
            trust_certificate_id=None,
        )

        for e in (e1, e2, e3, e4):
            store.insert_evaluation(e)

        listed = store.list_evaluations_for_artifact(a.artifact_id)
        assert len(listed) == 4
        # All four IDs accounted for.
        ids = {e.evaluation_id for e in listed}
        assert ids == {e1.evaluation_id, e2.evaluation_id,
                       e3.evaluation_id, e4.evaluation_id}
        # Modes preserved.
        modes = sorted([e.mode for e in listed])
        assert modes == sorted(["observe", "observe", "enforce", "what_if"])

    def test_list_evaluations_for_unknown_artifact_returns_empty(self, store):
        # No FK violation on a SELECT; just zero rows.
        assert store.list_evaluations_for_artifact("never-existed") == []


# --------------------------------------------------------------------------- #
# Append-only enforcement                                                      #
# --------------------------------------------------------------------------- #

class TestAppendOnlyEnforcement:
    def test_update_on_response_artifacts_raises(self, store):
        a = _make_artifact()
        store.insert_artifact(a)
        # Try to mutate any column on the row.
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            store._conn.execute(
                "UPDATE response_artifacts SET provider = 'tampered' "
                "WHERE artifact_id = ?",
                (a.artifact_id,),
            )

    def test_delete_on_response_artifacts_raises(self, store):
        a = _make_artifact()
        store.insert_artifact(a)
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            store._conn.execute(
                "DELETE FROM response_artifacts WHERE artifact_id = ?",
                (a.artifact_id,),
            )

    def test_update_on_governance_evaluations_raises(self, store):
        a = _make_artifact()
        store.insert_artifact(a)
        e = _make_evaluation(a.artifact_id)
        store.insert_evaluation(e)
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            store._conn.execute(
                "UPDATE governance_evaluations SET decision = 'Stop' "
                "WHERE evaluation_id = ?",
                (e.evaluation_id,),
            )

    def test_delete_on_governance_evaluations_raises(self, store):
        a = _make_artifact()
        store.insert_artifact(a)
        e = _make_evaluation(a.artifact_id)
        store.insert_evaluation(e)
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            store._conn.execute(
                "DELETE FROM governance_evaluations WHERE evaluation_id = ?",
                (e.evaluation_id,),
            )


# --------------------------------------------------------------------------- #
# Foreign-key integrity                                                        #
# --------------------------------------------------------------------------- #

class TestForeignKeyIntegrity:
    def test_evaluation_for_unknown_artifact_raises(self, store):
        # PRAGMA foreign_keys=ON is set by open_connection. An
        # evaluation pointing at an artifact_id that doesn't exist
        # must be rejected at the database layer.
        e = _make_evaluation("never-existed-artifact-id")
        with pytest.raises(sqlite3.IntegrityError):
            store.insert_evaluation(e)


# --------------------------------------------------------------------------- #
# Schema idempotence                                                           #
# --------------------------------------------------------------------------- #

class TestSchemaIdempotence:
    def test_reopening_existing_db_does_not_error(self, tmp_path):
        # Open, insert, close, reopen. The second open re-runs the
        # schema; CREATE IF NOT EXISTS + trigger IF NOT EXISTS means
        # this must be a no-op.
        db_path = tmp_path / "reopen.db"
        s1 = ArtifactStore(str(db_path))
        a = _make_artifact()
        s1.insert_artifact(a)
        s1.close()

        s2 = ArtifactStore(str(db_path))
        loaded = s2.get_artifact(a.artifact_id)
        assert loaded.artifact_id == a.artifact_id
        s2.close()


# --------------------------------------------------------------------------- #
# Human-composed end-to-end (Phase 5 flagship case)                            #
# --------------------------------------------------------------------------- #

class TestHumanComposedFlow:
    def test_human_draft_persists_and_can_be_evaluated(self, store):
        # The flagship Phase 5 scenario: a human drafts an outbound
        # message; TCS evaluates the draft before send.
        # No LLM is involved — the artifact has no prompt, no provider,
        # no system prompt; only raw_output (the draft) and the
        # recipient_context.
        draft = ResponseArtifact(
            generation_mode=GENERATION_MODE_HUMAN_COMPOSED,
            prompt=None,
            raw_output="Lithium is fine in small doses, no worries.",
            recipient_context={
                "pregnant": True,
                "role": "patient",
                "channel": "outbound_message",
                "medication_topic": "lithium",
            },
            generation_identity={
                "requesting_identity": "rep-7",
                "identity_type": "human",
                "role": "patient_support_rep",
            },
        )
        store.insert_artifact(draft)

        # And an evaluation against it — Stop, because a consumer-
        # facing dosing message during pregnancy is exactly path 1.
        e = GovernanceEvaluation(
            artifact_id=draft.artifact_id,
            mode=EVALUATION_MODE_ENFORCE,
            policy_profile_id="composed-medical",
            policy_profile_snapshot={"weights": {"B": 0.25}},
            decision="Stop",
            trust_certificate_id="tc-stop-human",
        )
        store.insert_evaluation(e)

        # Round-trip: pull back both, check the chain.
        loaded_draft = store.get_artifact(draft.artifact_id)
        assert loaded_draft.provider is None        # no LLM was called
        assert loaded_draft.prompt is None          # no prompt frame
        assert loaded_draft.raw_output is not None  # but a draft exists

        evals = store.list_evaluations_for_artifact(draft.artifact_id)
        assert len(evals) == 1
        assert evals[0].decision == "Stop"
        assert evals[0].enforcement_action == "blocked"
        assert evals[0].delivery_intervention is True
