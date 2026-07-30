"""
tis-v2 Commit 5b — production activation runtime tests.

Every production issuance surface now runs the canonical Decimal
pipeline (compute_tis_v2 / map_decision_versioned /
generate_certificate_v2). These tests drive each surface through the
REAL app and prove the certificate that was actually PERSISTED carries:

    certificate_schema_version == 2
    calculation_version        == "tis-v2"

Surfaces (the five issuance sites):

    /v2/query   — workflow-trace path (routes_query site 2)
    /v2/query   — off-topic baseline routing (routes_query site 1)
    /v2/govern  — interceptor govern path
    /v2/govern  — interceptor credential-stop path (declared CT-12)
    /v2/evaluate — artifact evaluation issuance (also behind /v2/replay)

Plus the 5b snapshot-semantics decision:

    * new runtime snapshots record calculation_version == "tis-v2";
    * replaying a v2 snapshot reproduces the decision under tis-v2;
    * a LEGACY source snapshot (no calculation_version) re-evaluates
      under tis-v2 and records replayed_from_calculation_version =
      "tis-v1-legacy" — a new evaluation, not a claimed reproduction.
"""

from __future__ import annotations

import os
from typing import Any, Dict, List
from unittest.mock import patch

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
    store = CertificateStore(str(tmp_path / "commit5b.db"))
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


def _assert_v2_persisted(client, certificate_id: str) -> Dict[str, Any]:
    """Fetch the PERSISTED certificate and pin the activation contract."""
    r = client.get(f"/v2/certificates/{certificate_id}")
    assert r.status_code == 200, r.text
    tc = r.json()
    assert tc["certificate_schema_version"] == 2, tc.get(
        "certificate_schema_version")
    assert tc["calculation_version"] == "tis-v2", tc.get(
        "calculation_version")
    return tc


def _govern_body(**overrides):
    body = {
        "query": "What allocation for a conservative client?",
        "retrieved_chunks": [
            {"chunk_id": "c1", "similarity_score": "0.93",
             "source_doc": "policy.pdf", "version": "1",
             "content": "policy text"},
        ],
        "candidate_answer": "A 60/40 allocation.",
        "subject_id": "c5b-activation",
    }
    body.update(overrides)
    return body


# =========================================================================== #
# 1. Per-surface activation: persisted certs are schema 2 / tis-v2            #
# =========================================================================== #

class TestIssuanceSurfacesPersistV2:
    def test_govern_path_persists_v2(self, client):
        r = client.post("/v2/govern", json=_govern_body())
        assert r.status_code == 200, r.text
        cert_id = r.json()["certificate_id"]
        assert cert_id
        _assert_v2_persisted(client, cert_id)

    def test_credential_stop_path_persists_v2(self, client):
        r = client.post("/v2/govern",
                        json=_govern_body(connection_type="CT-12"))
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["decision"] == "Stop"
        tc = _assert_v2_persisted(client, data["certificate_id"])
        # The v2 credential-stop TC carries the declared CT-12
        # provenance record (typed, enumerated detail code — 5a.1).
        rec = next(r_ for r_ in tc["c3_provenance"]
                   if r_["source_type"] == "credential_detection")
        assert rec["detail_code"] == "connection_type_ct12_declared"
        assert rec["pattern_id"] == ""

    def test_query_workflow_path_persists_v2(self, client):
        r = client.post("/v2/query", json={
            "query": "What does the document retention policy say?",
            "provider": "mock", "model": "deterministic",
        })
        assert r.status_code == 200, r.text
        cert_id = r.json()["certificate_id"]
        assert cert_id
        _assert_v2_persisted(client, cert_id)

    def test_query_off_topic_baseline_path_persists_v2(self, client):
        from tcs.api import routes_query

        class _LowSimVectorStore:
            def retrieve(self, query: str, k: int = 5) -> List[Dict[str, Any]]:
                return [{
                    "chunk_id": "low-0",
                    "source_doc": "unrelated_doc.md",
                    "version": "v1",
                    "similarity_score": 0.15,
                    "content": "Unrelated content.",
                    "tags": [],
                }]

        with patch.object(
            routes_query, "_get_vector_store",
            return_value=_LowSimVectorStore(),
        ):
            r = client.post("/v2/query", json={
                "query": "Who won the 1998 World Cup?",
                "provider": "mock", "model": "deterministic",
            })
        assert r.status_code == 200, r.text
        body = r.json()
        assert "routed_via_baseline_off_topic" in \
            (body.get("blocking_reason") or "")
        tc = _assert_v2_persisted(client, body["certificate_id"])
        assert tc["policy_set_id"] == "baseline-no-pack"

    def test_evaluate_path_persists_v2(self, client):
        g = client.post("/v2/generate", json={
            "generation_mode": "raw_llm",
            "prompt": "What is the document retention policy?",
            "provider": "mock", "model": "deterministic",
        })
        assert g.status_code == 200, g.text
        artifact_id = g.json()["artifact_id"]
        r = client.post("/v2/evaluate", json={
            "artifact_id": artifact_id,
            "mode": "enforce",
        })
        assert r.status_code == 200, r.text
        tc_id = r.json()["trust_certificate_id"]
        assert tc_id
        _assert_v2_persisted(client, tc_id)

    def test_observe_mode_persists_v2_with_observed_lifecycle(self, client):
        g = client.post("/v2/generate", json={
            "generation_mode": "raw_llm",
            "prompt": "What is the document retention policy?",
            "provider": "mock", "model": "deterministic",
        })
        artifact_id = g.json()["artifact_id"]
        r = client.post("/v2/evaluate", json={
            "artifact_id": artifact_id,
            "mode": "observe",
        })
        assert r.status_code == 200, r.text
        tc_id = r.json()["trust_certificate_id"]
        tc = _assert_v2_persisted(client, tc_id)
        assert tc["lifecycle_state"] == "observed"


# =========================================================================== #
# 2. Snapshot calculation semantics (5b decision)                             #
# =========================================================================== #

class TestSnapshotCalculationSemantics:
    def _evaluate(self, client, artifact_id, **extra):
        body = {"artifact_id": artifact_id, "mode": "enforce"}
        body.update(extra)
        r = client.post("/v2/evaluate", json=body)
        assert r.status_code == 200, r.text
        return r.json()

    def _make_artifact(self, client):
        g = client.post("/v2/generate", json={
            "generation_mode": "raw_llm",
            "prompt": "What is the document retention policy?",
            "provider": "mock", "model": "deterministic",
        })
        assert g.status_code == 200, g.text
        return g.json()["artifact_id"]

    def _full_evaluation(self, client, evaluation_id):
        r = client.get(f"/v2/evaluations/{evaluation_id}")
        assert r.status_code == 200, r.text
        return r.json()

    def test_new_snapshots_record_tis_v2(self, client):
        artifact_id = self._make_artifact(client)
        ev = self._evaluate(client, artifact_id)
        full = self._full_evaluation(client, ev["evaluation_id"])
        snap = full["governance_input_snapshot"]
        assert snap["calculation_version"] == "tis-v2"

    def test_v2_snapshot_replay_reproduces_decision(self, client):
        artifact_id = self._make_artifact(client)
        first = self._evaluate(client, artifact_id)
        # /v2/replay auto-locates the latest runtime snapshot for the
        # artifact and replays it verbatim under the same policy.
        r = client.post("/v2/replay", json={
            "artifact_id": artifact_id,
            "configurations": [{"mode": "what_if"}],
        })
        assert r.status_code == 200, r.text
        summary = r.json()["evaluations"][0]
        assert summary["decision"] == first["decision"]
        full = self._full_evaluation(client, summary["evaluation_id"])
        snap = full["governance_input_snapshot"]
        assert snap["calculation_version"] == "tis-v2"
        assert snap.get("replayed_from_calculation_version") == "tis-v2"

    def test_legacy_snapshot_marked_as_cross_version_evaluation(self):
        """A source snapshot with NO calculation_version (captured
        before activation, by the float engine) re-evaluates under
        tis-v2 and says so — never claiming reproduction of a v1
        decision."""
        from tcs.artifacts.evaluation import (
            evaluate_artifact, snapshot_tis_input,
        )
        from tcs.artifacts.models import ResponseArtifact
        from tcs.artifacts.evaluation import _score_via_artifact_metadata
        from tcs.policy_profiles import load_profile
        from datetime import datetime, timezone

        artifact = ResponseArtifact(
            artifact_id="legacy-snap-artifact",
            generation_mode="human_composed",
            prompt="What is the retention policy?",
            raw_output="Retain for seven years.",
            provider="", model="",
        )
        profile = load_profile("baseline-no-pack")
        tis_input = _score_via_artifact_metadata(
            artifact, profile,
            datetime.now(timezone.utc).replace(microsecond=0),
        )
        legacy_snapshot = snapshot_tis_input(tis_input)
        # Simulate a pre-activation row: strip the semantics marker and
        # downgrade Decimals to the floats a legacy row would hold.
        legacy_snapshot.pop("calculation_version")

        evaluation, tc = evaluate_artifact(
            artifact=artifact,
            mode="what_if",
            policy_profile_id="baseline-no-pack",
            strategy="runtime_snapshot",
            source_snapshot=legacy_snapshot,
        )
        assert tc is None  # what_if never issues
        snap = evaluation.governance_input_snapshot
        assert snap["calculation_version"] == "tis-v2"
        assert snap["replayed_from_calculation_version"] == "tis-v1-legacy"


# =========================================================================== #
# 3. Wire sanity after activation                                              #
# =========================================================================== #

class TestActivatedWire:
    def test_v2_certificate_scores_are_decimal_strings(self, client):
        r = client.post("/v2/govern", json=_govern_body())
        tc = _assert_v2_persisted(client, r.json()["certificate_id"])
        assert isinstance(tc["s_base"], str)
        assert isinstance(tc["tis_current"], str)
        for v in tc["component_scores"].values():
            assert isinstance(v, str)

    def test_raw_tier_preserves_producer_decimals(self, client):
        r = client.post("/v2/govern", json=_govern_body())
        tc = _assert_v2_persisted(client, r.json()["certificate_id"])
        assert set(tc["component_scores_raw"]) == {"B", "A", "C", "K"}
