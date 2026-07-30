"""
tests/test_chain_walk.py
========================

Coverage for GET /v2/certificates/chain/{chain_id}/walk — the
examiner-grade chain walk-through endpoint.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from tcs.api.app import create_app
from tcs.decision_engine import map_decision
from tcs.persistence import CertificateStore
from tcs.policy_profiles import load_profile
from tcs.tis_engine import TISInput, compute_tis
from tcs.trust_certificate import generate_certificate


def _issue_tc(store, *, subject_id: str, chain_id: str):
    """Issue a TC under a specific chain_id by mutating the TC before issue."""
    profile = load_profile("fin-r3-a4-ct4")
    inp = TISInput(
        subject_id=subject_id,
        subject_type="recommendation",
        policy_profile=profile,
        dimension_scores={"B": 0.95, "A": 0.95, "C": 1.00, "K": 0.95},
        sub_factor_scores={"C": {"C3": 1.0}},
        context_metadata={
            "n_gaps": 0, "context_age_hours": 0.1,
            "novelty_score": 0.0, "days_since_review": 1,
            "is_policy_sensitive": False,
        },
        elapsed_hours=0.0,
        is_valid=1,
        invalidation_event=None,
        evaluation_time=datetime.now(timezone.utc).replace(microsecond=0),
    )
    res = compute_tis(inp)
    decision, requires = map_decision(inp, res)
    tc = generate_certificate(inp, res, decision, requires)
    # Force the chain_id so all TCs in this test share one chain.
    object.__setattr__(tc.audit_integrity, "chain_id", chain_id)
    return store.issue(tc)


@pytest.fixture
def app_with_store(tmp_path):
    db_path = tmp_path / "chain_walk.db"
    store = CertificateStore(str(db_path))
    app = create_app(store=store)
    yield app, store
    store.close()


@pytest.fixture
def client(app_with_store):
    app, _ = app_with_store
    with TestClient(app) as c:
        yield c


# --------------------------------------------------------------------------- #
# Tests                                                                        #
# --------------------------------------------------------------------------- #

class TestChainWalk:
    def test_unknown_chain_returns_empty_but_intact(self, client):
        body = client.get("/v2/certificates/chain/no-such-chain/walk").json()
        assert body["chain_id"] == "no-such-chain"
        assert body["count"] == 0
        assert body["chain_intact"] is True
        assert body["rows"] == []

    def test_single_tc_chain_walks_with_no_predecessor(self, client, app_with_store):
        _, store = app_with_store
        _issue_tc(store, subject_id="walk-1", chain_id="chain-A")

        body = client.get("/v2/certificates/chain/chain-A/walk").json()
        assert body["count"] == 1
        assert body["chain_intact"] is True

        row = body["rows"][0]
        assert row["chain_sequence"] == 1
        assert row["previous_tc_hash"] is None
        assert row["tc_hash"] is not None
        assert row["content_hash_ok"] is True
        assert row["linkage_ok"] is True
        assert row["sequence_ok"] is True
        assert row["certificate_id"]
        assert row["decision"] in ("Allow", "Hold", "Stop", "Escalate", "Observe")

    def test_three_tc_chain_walks_in_order_with_linkage(self, client, app_with_store):
        _, store = app_with_store
        tc1 = _issue_tc(store, subject_id="walk-A", chain_id="chain-B")
        tc2 = _issue_tc(store, subject_id="walk-B", chain_id="chain-B")
        tc3 = _issue_tc(store, subject_id="walk-C", chain_id="chain-B")

        body = client.get("/v2/certificates/chain/chain-B/walk").json()
        assert body["count"] == 3
        assert body["chain_intact"] is True

        # Rows in chain_sequence order
        seqs = [r["chain_sequence"] for r in body["rows"]]
        assert seqs == [1, 2, 3]

        # Each previous_tc_hash matches the prior row's tc_hash
        rows = body["rows"]
        assert rows[0]["previous_tc_hash"] is None
        assert rows[1]["previous_tc_hash"] == rows[0]["tc_hash"]
        assert rows[2]["previous_tc_hash"] == rows[1]["tc_hash"]

        # Every per-row check passes
        for r in rows:
            assert r["content_hash_ok"] is True
            assert r["linkage_ok"] is True
            assert r["sequence_ok"] is True

        # certificate_ids match what was issued (in order)
        issued_ids = [tc1.certificate_id, tc2.certificate_id, tc3.certificate_id]
        assert [r["certificate_id"] for r in rows] == issued_ids

    def test_walk_matches_verify_chain_for_intact_chains(self, client, app_with_store):
        _, store = app_with_store
        _issue_tc(store, subject_id="walk-vA", chain_id="chain-C")
        _issue_tc(store, subject_id="walk-vB", chain_id="chain-C")

        walk = client.get("/v2/certificates/chain/chain-C/walk").json()
        # Cross-check against the existing verify_chain endpoint.
        verify = client.get(
            "/v2/certificates/verify-chain?chain_id=chain-C"
        ).json()
        assert walk["chain_intact"] is True
        assert verify["chain_intact"] is True
        assert walk["count"] == verify["tc_count"]

    def test_walk_returns_decision_and_lifecycle_fields(self, client, app_with_store):
        _, store = app_with_store
        _issue_tc(store, subject_id="walk-meta", chain_id="chain-D")
        body = client.get("/v2/certificates/chain/chain-D/walk").json()
        row = body["rows"][0]
        # Auditor-facing identity fields are present.
        for k in (
            "certificate_id", "decision", "evaluation_timestamp",
            "lifecycle_state", "chain_sequence", "tc_hash",
            "previous_tc_hash", "content_hash_ok", "linkage_ok",
            "sequence_ok",
        ):
            assert k in row, f"row missing field: {k}"
        # Evaluation timestamp is ISO-Z formatted.
        assert row["evaluation_timestamp"].endswith("Z")
