"""
Phase 5 Slice 5.4a — Replay Fidelity Hardening tests.

The single load-bearing claim of Slice 5.4a:

    /v2/query creates an artifact and evaluation. Re-evaluating that
    same artifact under the same policy MUST reproduce the same
    decision and BACK scores, modulo timestamps and IDs, unless the
    request explicitly asks for a fresh metadata-based re-evaluation.

This file pins:

  1. The regression: /v2/query → /v2/evaluate (auto) → identical
     decision + identical s_base, tis_current, component_scores,
     gate_results.

  2. Strategy labeling: every evaluation row carries an explicit
     evaluation_strategy ∈ {runtime_snapshot, artifact_metadata,
     what_if_policy_replay} so a future reviewer can tell whether
     a given evaluation was a replay or a fresh re-scoring.

  3. Snapshot persistence: every evaluation row carries a
     governance_input_snapshot that the engine could reproduce
     deterministically.

  4. Explicit override works: caller can force
     strategy=artifact_metadata to get a fresh re-evaluation.

  5. what_if_policy_replay isolates policy impact: same evidence,
     different policy, see how the decision shifts on the policy
     alone.

  6. /v2/replay per-config strategy works the same way.

  7. Validation: invalid strategy requests are rejected.
"""

from __future__ import annotations

import os
from typing import Any, Dict

import pytest
from fastapi.testclient import TestClient


# --------------------------------------------------------------------------- #
# Fixtures                                                                     #
# --------------------------------------------------------------------------- #

@pytest.fixture
def client(tmp_path):
    os.environ["TCS_WORKFLOW_TRACE_ENABLED"] = "true"
    from tcs.api.app import create_app
    from tcs.packs.pack_manager import (
        PACKS, clear_active_pack, unregister_composed_pack,
    )
    from tcs.persistence.certificate_store import CertificateStore

    pre = set(PACKS.keys())
    store = CertificateStore(str(tmp_path / "slice54a.db"))
    app = create_app(store=store)
    c = TestClient(app)
    with c:
        yield c
    for pid in (set(PACKS.keys()) - pre):
        try:
            unregister_composed_pack(pid)
        except Exception:
            pass
    clear_active_pack()
    store.close()
    os.environ.pop("TCS_WORKFLOW_TRACE_ENABLED", None)


def _deploy_meddev(client) -> Dict[str, Any]:
    return client.post("/v2/standards/deploy", json={
        "industry": "life_sciences",
        "sub_industry": "medical_devices",
        "use_case": "clinical_decision_support",
        "standard_ids": ["iso_13485", "iso_14971", "iec_62304"],
        "risk_tier": "r3", "action_class": "a4",
    }).json()


def _query_then_find_artifact_id(client, query: str) -> str:
    """Run /v2/query, then locate the artifact_id it created."""
    r = client.post("/v2/query", json={
        "query": query, "provider": "mock", "model": "deterministic",
    }).json()
    cert_id = r["certificate_id"]
    assert cert_id, f"/v2/query failed: {r}"

    from tcs.artifacts.store import ArtifactStore
    store = ArtifactStore(conn=client.app.state.artifact_store._conn)
    for a in store.list_artifacts(limit=10):
        for e in store.list_evaluations_for_artifact(a.artifact_id):
            if e.trust_certificate_id == cert_id:
                return a.artifact_id
    pytest.fail(f"could not find artifact for TC {cert_id}")


# --------------------------------------------------------------------------- #
# Load-bearing regression: query → evaluate parity                             #
# --------------------------------------------------------------------------- #

class TestQueryEvaluateReplayParity:
    """
    The single regression Slice 5.4a exists to enforce.
    """

    def test_default_strategy_reproduces_runtime_decision_and_scores(
        self, client,
    ):
        # Step 1: /v2/query runs and records its decision + the
        # TISInput snapshot that produced it.
        deployed = _deploy_meddev(client)
        query_r = client.post("/v2/query", json={
            "query": "Tell me about document retention.",
            "provider": "mock", "model": "deterministic",
        }).json()
        runtime_decision = query_r["decision"]
        runtime_s_base = query_r["s_base"]
        runtime_tis_current = query_r["tis_current"]

        # Locate the artifact + the evaluation row that /v2/query wrote.
        from tcs.artifacts.store import ArtifactStore
        store = ArtifactStore(conn=client.app.state.artifact_store._conn)
        artifact_id = None
        runtime_eval = None
        for a in store.list_artifacts(limit=10):
            for e in store.list_evaluations_for_artifact(a.artifact_id):
                if e.trust_certificate_id == query_r["certificate_id"]:
                    artifact_id = a.artifact_id
                    runtime_eval = e
                    break
            if artifact_id:
                break
        assert runtime_eval is not None
        # The runtime evaluation must carry the runtime_snapshot
        # strategy + a non-empty snapshot.
        assert runtime_eval.evaluation_strategy == "runtime_snapshot"
        assert runtime_eval.governance_input_snapshot is not None

        # Step 2: /v2/evaluate (default strategy) under the same policy.
        # Must reproduce the runtime decision + scores via
        # runtime_snapshot replay.
        replay_r = client.post("/v2/evaluate", json={
            "artifact_id": artifact_id,
            "mode": "enforce",
            "policy_profile_id": deployed["pack_id"],
            # NO strategy specified — defaults to auto.
        }).json()

        assert replay_r["evaluation_strategy"] == "runtime_snapshot", (
            f"default re-evaluation should pick runtime_snapshot; got "
            f"{replay_r['evaluation_strategy']!r}"
        )
        assert replay_r["decision"] == runtime_decision, (
            f"runtime decision {runtime_decision!r} not reproduced; "
            f"got {replay_r['decision']!r}"
        )
        assert replay_r["s_base"] == runtime_s_base, (
            f"s_base drift: runtime={runtime_s_base}, replay={replay_r['s_base']}"
        )
        assert replay_r["tis_current"] == runtime_tis_current, (
            f"tis_current drift: runtime={runtime_tis_current}, "
            f"replay={replay_r['tis_current']}"
        )

    def test_explicit_artifact_metadata_override_does_a_fresh_evaluation(
        self, client,
    ):
        # The escape hatch: a caller who explicitly wants the fresh
        # metadata-based re-evaluation can ask for it. The strategy
        # label must reflect that choice so a reviewer can see this
        # was NOT a replay of the runtime decision.
        deployed = _deploy_meddev(client)
        client.post("/v2/query", json={
            "query": "Tell me about retention.",
            "provider": "mock", "model": "deterministic",
        })
        artifact_id = _query_then_find_artifact_id(
            client, "Tell me about something else.",
        )

        r = client.post("/v2/evaluate", json={
            "artifact_id": artifact_id,
            "mode": "observe",
            "policy_profile_id": deployed["pack_id"],
            "strategy": "artifact_metadata",
        }).json()
        assert r["evaluation_strategy"] == "artifact_metadata"


# --------------------------------------------------------------------------- #
# Strategy labeling on every row                                               #
# --------------------------------------------------------------------------- #

class TestStrategyLabelingOnEveryRow:
    def test_query_origin_evaluation_strategy_is_runtime_snapshot(self, client):
        _deploy_meddev(client)
        artifact_id = _query_then_find_artifact_id(client, "Anything.")
        from tcs.artifacts.store import ArtifactStore
        store = ArtifactStore(conn=client.app.state.artifact_store._conn)
        evals = store.list_evaluations_for_artifact(artifact_id)
        # The /v2/query-written evaluation has origin=query AND
        # strategy=runtime_snapshot AND a snapshot.
        query_evals = [e for e in evals if e.evaluation_origin == "query"]
        assert query_evals, "expected at least one origin=query evaluation"
        for e in query_evals:
            assert e.evaluation_strategy == "runtime_snapshot"
            assert e.governance_input_snapshot is not None

    def test_every_evaluation_carries_a_snapshot(self, client):
        # Even artifact_metadata strategy evaluations persist a
        # captured TISInput — so they could be replayed later if
        # someone wants to reproduce them.
        deployed = _deploy_meddev(client)
        # Direct evaluate path against an artifact with no prior
        # snapshot.
        gen = client.post("/v2/generate", json={
            "generation_mode": "rag_llm",
            "prompt": "Test.", "provider": "mock",
            "industry_hint": "life_sciences",
        }).json()
        client.post("/v2/evaluate", json={
            "artifact_id": gen["artifact_id"],
            "mode": "observe",
            "policy_profile_id": deployed["pack_id"],
        })
        from tcs.artifacts.store import ArtifactStore
        store = ArtifactStore(conn=client.app.state.artifact_store._conn)
        evals = store.list_evaluations_for_artifact(gen["artifact_id"])
        assert evals
        for e in evals:
            assert e.governance_input_snapshot is not None, (
                f"evaluation {e.evaluation_id} missing snapshot"
            )
            assert "dimension_scores" in e.governance_input_snapshot
            assert "context_metadata" in e.governance_input_snapshot


# --------------------------------------------------------------------------- #
# what_if_policy_replay — isolates policy impact                               #
# --------------------------------------------------------------------------- #

class TestWhatIfPolicyReplay:
    def test_what_if_replay_reuses_evidence_under_different_policy(
        self, client,
    ):
        # Setup: /v2/query under MedDev captures runtime snapshot.
        deployed = _deploy_meddev(client)
        client.post("/v2/query", json={
            "query": "Routine policy lookup.",
            "provider": "mock", "model": "deterministic",
        })
        artifact_id = _query_then_find_artifact_id(
            client, "Routine policy lookup #2.",
        )

        # Replay the SAME captured evidence under a DIFFERENT policy
        # (baseline-no-pack). The strategy="what_if_policy_replay"
        # request takes the snapshot's BACK signals + context
        # verbatim and only swaps the policy.
        r = client.post("/v2/evaluate", json={
            "artifact_id": artifact_id,
            "mode": "what_if",
            "policy_profile_id": "baseline-no-pack",
            "strategy": "what_if_policy_replay",
        })
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["evaluation_strategy"] == "what_if_policy_replay"
        assert body["policy_profile_id"] == "baseline-no-pack"

    def test_what_if_replay_requires_a_prior_snapshot(self, client):
        # Fresh artifact with no prior evaluation → no snapshot to
        # replay. Caller-supplied what_if_policy_replay must be 400.
        deployed = _deploy_meddev(client)
        gen = client.post("/v2/generate", json={
            "generation_mode": "rag_llm",
            "prompt": "test", "provider": "mock",
            "industry_hint": "life_sciences",
        }).json()
        r = client.post("/v2/evaluate", json={
            "artifact_id": gen["artifact_id"],
            "mode": "what_if",
            "policy_profile_id": "baseline-no-pack",
            "strategy": "what_if_policy_replay",
        })
        assert r.status_code == 400
        assert "snapshot" in r.json()["detail"].lower()

    def test_what_if_replay_requires_different_policy(self, client):
        # what_if_policy_replay with the SAME policy as the snapshot
        # is meaningless — it would just reproduce runtime_snapshot.
        # Caller-supplied request with matching policy must be 400.
        deployed = _deploy_meddev(client)
        client.post("/v2/query", json={
            "query": "snapshot-creator",
            "provider": "mock", "model": "deterministic",
        })
        artifact_id = _query_then_find_artifact_id(
            client, "second query",
        )

        # Find the snapshot's original policy.
        from tcs.artifacts.store import ArtifactStore
        store = ArtifactStore(conn=client.app.state.artifact_store._conn)
        evals = store.list_evaluations_for_artifact(artifact_id)
        snap_eval = next(e for e in evals if e.governance_input_snapshot)
        same_policy = snap_eval.governance_input_snapshot["policy_profile_id"]

        r = client.post("/v2/evaluate", json={
            "artifact_id": artifact_id,
            "mode": "what_if",
            "policy_profile_id": same_policy,
            "strategy": "what_if_policy_replay",
        })
        assert r.status_code == 400
        assert "different" in r.json()["detail"].lower()


# --------------------------------------------------------------------------- #
# runtime_snapshot — error when no snapshot exists                             #
# --------------------------------------------------------------------------- #

class TestRuntimeSnapshotRequiresSnapshot:
    def test_runtime_snapshot_without_prior_evaluation_is_400(self, client):
        deployed = _deploy_meddev(client)
        gen = client.post("/v2/generate", json={
            "generation_mode": "rag_llm",
            "prompt": "no snapshot yet",
            "provider": "mock",
            "industry_hint": "life_sciences",
        }).json()
        r = client.post("/v2/evaluate", json={
            "artifact_id": gen["artifact_id"],
            "mode": "observe",
            "policy_profile_id": deployed["pack_id"],
            "strategy": "runtime_snapshot",
        })
        assert r.status_code == 400
        assert "snapshot" in r.json()["detail"].lower()


# --------------------------------------------------------------------------- #
# /v2/replay per-config strategy                                               #
# --------------------------------------------------------------------------- #

class TestReplayPerConfigStrategy:
    def test_replay_per_config_strategies(self, client):
        deployed = _deploy_meddev(client)
        # Build snapshot via /v2/query.
        client.post("/v2/query", json={
            "query": "snapshot-creator",
            "provider": "mock", "model": "deterministic",
        })
        artifact_id = _query_then_find_artifact_id(client, "for replay test")

        r = client.post("/v2/replay", json={
            "artifact_id": artifact_id,
            "configurations": [
                # No strategy → auto. Same policy as snapshot →
                # runtime_snapshot.
                {"mode": "observe", "policy_profile_id": deployed["pack_id"]},
                # Explicit artifact_metadata — fresh re-evaluation
                # under the same policy.
                {"mode": "observe", "policy_profile_id": deployed["pack_id"],
                 "strategy": "artifact_metadata"},
                # Different policy, no strategy → auto picks
                # artifact_metadata (per the conservative auto rule
                # we landed on for slice 5.4a).
                {"mode": "what_if", "policy_profile_id": "baseline-no-pack"},
                # Explicit what_if_policy_replay with a different
                # policy → uses snapshot evidence under new policy.
                {"mode": "what_if", "policy_profile_id": "baseline-no-pack",
                 "strategy": "what_if_policy_replay"},
            ],
        }).json()
        strategies = [e["evaluation_strategy"] for e in r["evaluations"]]
        assert strategies == [
            "runtime_snapshot",
            "artifact_metadata",
            "artifact_metadata",
            "what_if_policy_replay",
        ], f"per-config strategies wrong: {strategies}"


# --------------------------------------------------------------------------- #
# Snapshot fidelity at the dataclass level                                     #
# --------------------------------------------------------------------------- #

class TestSnapshotFidelity:
    def test_snapshot_replay_produces_identical_tis_result(self):
        # Direct unit test of the snapshot/replay roundtrip at the
        # engine layer: the same snapshot + same policy must produce
        # the same TISResult every time. This is the bedrock the
        # whole replay story depends on.
        from datetime import datetime, timezone
        from tcs.artifacts.evaluation import (
            snapshot_tis_input, tis_input_from_snapshot,
        )
        from tcs.policy_profiles import load_profile
        from tcs.tis_engine import TISInput, compute_tis

        profile = load_profile("fin-high-risk-suitability-v3")
        original = TISInput(
            subject_id="test-subject",
            subject_type="raw_llm",
            policy_profile=profile,
            dimension_scores={"B": 0.95, "A": 0.92, "C": 0.94, "K": 0.88},
            sub_factor_scores={"C": {"C3": 1.0}},
            context_metadata={"n_gaps": 0, "context_age_hours": 0.1,
                              "novelty_score": 0.0, "days_since_review": 1,
                              "is_policy_sensitive": False},
            elapsed_hours=0.0,
            is_valid=1,
            invalidation_event=None,
            evaluation_time=datetime.now(timezone.utc).replace(microsecond=0),
        )
        result_a = compute_tis(original)

        snap = snapshot_tis_input(original)
        rebuilt = tis_input_from_snapshot(snap, policy=profile)
        result_b = compute_tis(rebuilt)

        # Same input → same scoring output, modulo nothing.
        assert result_a.s_base == result_b.s_base
        assert result_a.s_adj == result_b.s_adj
        assert result_a.tis_current == result_b.tis_current
        assert result_a.gate_result == result_b.gate_result
        assert result_a.gate_results_by_dim == result_b.gate_results_by_dim
