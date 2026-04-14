"""
Phase 2 Step 1 — Persistence layer tests.

Focus: append-only enforcement, chain sequencing, and verify_chain()
against the database. The headline gate is three sequential TCs
verifying correctly end-to-end.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime

import pytest

from tcs.tis_engine import compute_tis
from tcs.decision_engine import map_decision
from tcs.trust_certificate import (
    generate_certificate,
    compute_tc_hash,
)
from tcs.persistence import (
    AppendOnlyViolation,
    CertificateNotFoundError,
    CertificateStore,
    ChainSequenceError,
    init_db,
    open_connection,
)

from tests.conftest import make_tis_input


# --------------------------------------------------------------------------- #
# Test helpers                                                                 #
# --------------------------------------------------------------------------- #

def _make_tc(scores=None, *, chain_id=None, **kwargs):
    """
    Build a Phase-1 TC suitable for persistence tests. Override chain_id
    via context_metadata so multiple tests can share the same store
    without bleeding into each other.
    """
    scores = scores or {"B": 0.94, "A": 0.90, "C": 0.92, "K": 0.83}

    ctx_overrides = {}
    if chain_id is not None:
        ctx_overrides["chain_id"] = chain_id

    meta = kwargs.pop("context_metadata", {})
    meta = {**meta, **ctx_overrides}

    inp = make_tis_input(
        profile_id=kwargs.pop("profile_id", "fin-high-risk-suitability-v3"),
        dimension_scores=scores,
        context_metadata=meta,
        **kwargs,
    )
    r = compute_tis(inp)
    d, review = map_decision(inp, r)
    return generate_certificate(inp, r, d, review)


@pytest.fixture
def store():
    """Fresh in-memory store per test."""
    s = CertificateStore(":memory:")
    yield s
    s.close()


# --------------------------------------------------------------------------- #
# Schema and init                                                              #
# --------------------------------------------------------------------------- #

class TestSchemaInit:
    def test_init_db_creates_expected_tables(self):
        conn = init_db(":memory:")
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
        names = {r["name"] for r in rows}
        assert "trust_certificates" in names
        assert "lifecycle_events" in names
        assert "trust_metrics" in names
        assert "request_audit" in names
        conn.close()

    def test_init_db_is_idempotent(self):
        conn = init_db(":memory:")
        init_db(conn=conn)   # should not raise
        init_db(conn=conn)   # again
        conn.close()

    def test_open_connection_default_row_factory(self):
        conn = open_connection(":memory:")
        conn.execute("CREATE TABLE t (x TEXT)")
        conn.execute("INSERT INTO t VALUES ('a')")
        row = conn.execute("SELECT x FROM t").fetchone()
        # Row factory is sqlite3.Row -> dict-like access
        assert row["x"] == "a"
        conn.close()


# --------------------------------------------------------------------------- #
# Append-only enforcement                                                      #
# --------------------------------------------------------------------------- #

class TestAppendOnlyEnforcement:
    """
    The append-only constraint is enforced at the SQLite trigger layer,
    which raises ``sqlite3.IntegrityError`` with the text "append-only"
    in the message. The :class:`AppendOnlyViolation` subclass exists so
    that the higher-level store API paths can translate these errors
    into a typed exception, but raw connection calls surface the base
    ``IntegrityError`` directly. These tests probe the trigger layer
    and therefore match on the message.
    """

    def _assert_append_only(self, fn):
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            fn()

    def test_update_trust_certificates_blocked(self, store):
        tc = _make_tc(chain_id="chain-append-test-1")
        issued = store.issue(tc)
        self._assert_append_only(lambda: store._conn.execute(
            "UPDATE trust_certificates SET decision=? WHERE certificate_id=?",
            ("Hold", issued.certificate_id),
        ))

    def test_delete_trust_certificates_blocked(self, store):
        tc = _make_tc(chain_id="chain-append-test-2")
        issued = store.issue(tc)
        self._assert_append_only(lambda: store._conn.execute(
            "DELETE FROM trust_certificates WHERE certificate_id=?",
            (issued.certificate_id,),
        ))

    def test_update_lifecycle_events_blocked(self, store):
        tc = _make_tc(chain_id="chain-append-test-3")
        store.issue(tc)
        self._assert_append_only(lambda: store._conn.execute(
            "UPDATE lifecycle_events SET reason='tampered'"
        ))

    def test_delete_lifecycle_events_blocked(self, store):
        tc = _make_tc(chain_id="chain-append-test-4")
        store.issue(tc)
        self._assert_append_only(
            lambda: store._conn.execute("DELETE FROM lifecycle_events")
        )

    def test_metrics_and_audit_are_append_only(self, store):
        c = store._conn
        c.execute(
            "INSERT INTO trust_metrics (metric_name, metric_value) VALUES (?, ?)",
            ("tis_raw_mean", 0.87),
        )
        c.execute(
            "INSERT INTO request_audit "
            "(request_id, subject_id, decision, fail_safe_applied, received_at) "
            "VALUES (?, ?, ?, ?, ?)",
            ("req-1", "subj-1", "Allow", 0, "2026-04-08T12:00:00Z"),
        )
        self._assert_append_only(
            lambda: c.execute("UPDATE trust_metrics SET metric_value=0.0")
        )
        self._assert_append_only(
            lambda: c.execute("DELETE FROM trust_metrics")
        )
        self._assert_append_only(
            lambda: c.execute("UPDATE request_audit SET decision='Stop'")
        )
        self._assert_append_only(
            lambda: c.execute("DELETE FROM request_audit")
        )


# --------------------------------------------------------------------------- #
# Issue and get round-trip                                                     #
# --------------------------------------------------------------------------- #

class TestIssueAndGet:
    def test_issue_returns_new_tc_with_chain_linkage(self, store):
        tc = _make_tc(chain_id="chain-iag-1")
        issued = store.issue(tc)
        assert issued is not tc  # not the same instance
        assert issued.audit_integrity.chain_sequence == 1
        assert issued.audit_integrity.previous_tc_hash is None
        assert issued.audit_integrity.chain_id == "chain-iag-1"

    def test_issue_does_not_mutate_caller(self, store):
        tc = _make_tc(chain_id="chain-iag-2")
        orig_hash = tc.audit_integrity.tc_hash
        orig_seq = tc.audit_integrity.chain_sequence
        store.issue(tc)
        # Caller's TC unchanged
        assert tc.audit_integrity.tc_hash == orig_hash
        assert tc.audit_integrity.chain_sequence == orig_seq

    def test_get_round_trips(self, store):
        tc = _make_tc(chain_id="chain-iag-3")
        issued = store.issue(tc)
        loaded = store.get(issued.certificate_id)
        assert loaded.certificate_id == issued.certificate_id
        assert loaded.decision == issued.decision
        assert loaded.audit_integrity.tc_hash == issued.audit_integrity.tc_hash
        # Hash recompute is stable after round-trip
        assert compute_tc_hash(loaded.to_dict()) == loaded.audit_integrity.tc_hash

    def test_get_missing_raises(self, store):
        with pytest.raises(CertificateNotFoundError):
            store.get("does-not-exist")

    def test_count_tracks_inserts(self, store):
        assert store.count() == 0
        store.issue(_make_tc(chain_id="chain-ct-1"))
        store.issue(_make_tc(chain_id="chain-ct-2"))
        assert store.count() == 2


# --------------------------------------------------------------------------- #
# Chain sequencing                                                             #
# --------------------------------------------------------------------------- #

class TestChainSequencing:
    def test_sequential_issue_assigns_monotonic_sequence(self, store):
        chain = "chain-seq-1"
        first = store.issue(_make_tc(chain_id=chain))
        second = store.issue(_make_tc(chain_id=chain))
        third = store.issue(_make_tc(chain_id=chain))

        assert first.audit_integrity.chain_sequence == 1
        assert second.audit_integrity.chain_sequence == 2
        assert third.audit_integrity.chain_sequence == 3

    def test_sequential_issue_links_previous_hash(self, store):
        chain = "chain-seq-2"
        first = store.issue(_make_tc(chain_id=chain))
        second = store.issue(_make_tc(chain_id=chain))
        third = store.issue(_make_tc(chain_id=chain))

        assert second.audit_integrity.previous_tc_hash == first.audit_integrity.tc_hash
        assert third.audit_integrity.previous_tc_hash == second.audit_integrity.tc_hash

    def test_independent_chains_have_independent_sequences(self, store):
        a1 = store.issue(_make_tc(chain_id="chain-indep-A"))
        b1 = store.issue(_make_tc(chain_id="chain-indep-B"))
        a2 = store.issue(_make_tc(chain_id="chain-indep-A"))
        b2 = store.issue(_make_tc(chain_id="chain-indep-B"))
        assert a1.audit_integrity.chain_sequence == 1
        assert a2.audit_integrity.chain_sequence == 2
        assert b1.audit_integrity.chain_sequence == 1
        assert b2.audit_integrity.chain_sequence == 2


# --------------------------------------------------------------------------- #
# verify_chain() — the Step 1 headline gate                                    #
# --------------------------------------------------------------------------- #

class TestVerifyChain:
    def test_step1_gate_three_sequential_tcs_verify(self, store):
        """
        Phase 2 Step 1 acceptance gate:
        3 sequential TCs write and verify_chain() returns True.
        """
        chain = "chain-step1-gate"
        store.issue(_make_tc(chain_id=chain))
        store.issue(_make_tc(chain_id=chain))
        store.issue(_make_tc(chain_id=chain))

        assert store.verify_chain(chain) is True

    def test_verify_chain_empty_is_true(self, store):
        # Vacuous: no TCs means nothing to invalidate.
        assert store.verify_chain("nonexistent-chain") is True

    def test_verify_chain_single_tc(self, store):
        store.issue(_make_tc(chain_id="chain-single"))
        assert store.verify_chain("chain-single") is True

    def test_verify_chain_detects_tampered_content(self, store):
        """
        Insert a fresh TC row whose stored tc_hash does NOT match the
        content_json. verify_chain() must detect the mismatch.

        We cannot update an existing row (append-only triggers), so the
        attack model here is "a storage-layer write-through path that
        installed a row with a wrong hash". We synthesize that by
        directly INSERTing a row into a new chain with a tampered
        content_json and a fabricated tc_hash that references the
        untampered content — so the stored hash no longer matches what
        compute_tc_hash() would produce from the stored content.
        """
        tc = _make_tc(chain_id="chain-tamper")
        store.issue(tc)

        # Build a tampered content_json: take the first row's JSON and
        # flip the decision. Its stored tc_hash is the UNTAMPERED hash,
        # which now does not match the tampered content.
        raw = store._conn.execute(
            "SELECT content_json FROM trust_certificates "
            "WHERE chain_id = ?",
            ("chain-tamper",),
        ).fetchone()

        d = json.loads(raw["content_json"])
        untampered_hash = d["audit_integrity"]["tc_hash"]
        # Mutate content (including certificate_id to avoid UNIQUE clash)
        d["certificate_id"] = "cert-tampered-001"
        d["decision"] = "Stop"
        # Leave audit_integrity.tc_hash as the untampered value. That
        # is the "lie" the attacker installs. It will not match a
        # fresh compute_tc_hash() over the mutated content.
        d["audit_integrity"]["chain_id"] = "chain-tampered"
        d["audit_integrity"]["chain_sequence"] = 1
        d["audit_integrity"]["previous_tc_hash"] = None
        tampered_json = json.dumps(d)

        # Fabricate a column-unique hash (just prepend a nonce) so the
        # INSERT does not hit the UNIQUE(tc_hash) constraint. The point
        # of this test is that verify_chain() recomputes the hash from
        # stored content and compares to the stored hash — not that
        # the fabricated hash collides with the original.
        fabricated_hash = "deadbeef" + untampered_hash[8:]
        d["audit_integrity"]["tc_hash"] = fabricated_hash
        tampered_json = json.dumps(d)

        store._conn.execute("BEGIN")
        try:
            store._conn.execute(
                """
                INSERT INTO trust_certificates (
                    certificate_id, subject_id, subject_type, domain,
                    risk_tier, action_class, policy_set_id,
                    decision, lifecycle_state, invalidation_status,
                    tis_raw, tis_adjusted, tis_current,
                    evaluation_timestamp, valid_until,
                    tc_hash, previous_tc_hash,
                    chain_id, chain_sequence, hash_algorithm,
                    amended_tc_id, content_json
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    "cert-tampered-001",
                    d["subject_id"], d["subject_type"], d["domain"],
                    d["risk_tier"], d["action_class"], d["policy_set_id"],
                    "Stop", d["lifecycle_state"], d["invalidation_status"],
                    d["tis_raw"], d["tis_adjusted"], d["tis_current"],
                    d["evaluation_timestamp"], d["valid_until"],
                    fabricated_hash,
                    None,
                    "chain-tampered", 1, "sha256",
                    None, tampered_json,
                ),
            )
            store._conn.execute("COMMIT")
        except Exception:
            store._conn.execute("ROLLBACK")
            raise

        # Original chain still verifies (we never mutated it).
        assert store.verify_chain("chain-tamper") is True
        # Tampered chain does not verify — fresh hash does not match.
        assert store.verify_chain("chain-tampered") is False

    def test_verify_chain_detects_sequence_gap(self, store):
        """
        Insert chain positions 1 and 3 (skipping 2) directly, then
        verify fails. We bypass issue() here because issue() would
        assign sequence=2, not 3.
        """
        tc = _make_tc(chain_id="chain-gap")
        store.issue(tc)  # gets sequence=1

        # Now manually insert a TC at chain_sequence=3
        second = _make_tc(chain_id="chain-gap")
        d = second.to_dict()
        d_json = json.dumps(d)

        store._conn.execute("BEGIN")
        try:
            store._conn.execute(
                """
                INSERT INTO trust_certificates (
                    certificate_id, subject_id, subject_type, domain,
                    risk_tier, action_class, policy_set_id,
                    decision, lifecycle_state, invalidation_status,
                    tis_raw, tis_adjusted, tis_current,
                    evaluation_timestamp, valid_until,
                    tc_hash, previous_tc_hash,
                    chain_id, chain_sequence, hash_algorithm,
                    amended_tc_id, content_json
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    "cert-sequence-gap",
                    d["subject_id"], d["subject_type"], d["domain"],
                    d["risk_tier"], d["action_class"], d["policy_set_id"],
                    d["decision"], d["lifecycle_state"], d["invalidation_status"],
                    d["tis_raw"], d["tis_adjusted"], d["tis_current"],
                    d["evaluation_timestamp"], d["valid_until"],
                    d["audit_integrity"]["tc_hash"],
                    d["audit_integrity"]["tc_hash"],   # pretend previous=self (wrong)
                    "chain-gap", 3, "sha256",
                    None, d_json,
                ),
            )
            store._conn.execute("COMMIT")
        except Exception:
            store._conn.execute("ROLLBACK")
            raise

        # Now the chain has positions 1 and 3 — gap at 2.
        assert store.verify_chain("chain-gap") is False


# --------------------------------------------------------------------------- #
# list_chain + integration                                                     #
# --------------------------------------------------------------------------- #

class TestListChain:
    def test_list_chain_returns_in_sequence_order(self, store):
        chain = "chain-list-order"
        store.issue(_make_tc(chain_id=chain))
        store.issue(_make_tc(chain_id=chain))
        store.issue(_make_tc(chain_id=chain))

        rows = store.list_chain(chain)
        assert len(rows) == 3
        assert [r.audit_integrity.chain_sequence for r in rows] == [1, 2, 3]

    def test_list_chain_empty(self, store):
        assert store.list_chain("nope") == []
