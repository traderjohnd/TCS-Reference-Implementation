"""
Phase 4 Step 3 — PostgreSQL persistence tests.

Skipped unless ``TCS_TEST_PG=1`` is set and a PostgreSQL instance is
reachable at the configured connection parameters.

Verifies:
    * issue / get round-trip
    * list_chain ordering
    * verify_chain integrity across 5 sequential TCs
    * decision_counts aggregation
    * tis_distribution histogram
    * append-only: UPDATE and DELETE are rejected (RULEs)
    * migration idempotency (run twice, no error)
    * count(), list_recent(), gate_failure_rate()
    * governance_integrity_score()

Set environment variables before running::

    TCS_TEST_PG=1
    TCS_PG_HOST=localhost
    TCS_PG_PORT=5432
    TCS_PG_DATABASE=tcs_test
    TCS_PG_USER=tcs
    TCS_PG_PASSWORD=tcs_dev

Or start the local dev database::

    docker compose up -d
    TCS_TEST_PG=1 pytest tests/test_pg_store.py -v
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone

import pytest

# Skip entire module if TCS_TEST_PG is not set.
pytestmark = pytest.mark.skipif(
    os.environ.get("TCS_TEST_PG", "0") != "1",
    reason="PostgreSQL tests disabled (set TCS_TEST_PG=1 to enable)",
)

# Import psycopg and pg_store — may not be installed.
try:
    import psycopg
    from psycopg.rows import dict_row
    from tcs.persistence.pg_store import PostgresCertificateStore
except ImportError:
    pytest.skip("psycopg not installed", allow_module_level=True)

from tcs.persistence.certificate_store import (
    ChainSequenceError,
    CertificateNotFoundError,
)
from tcs.trust_certificate import (
    TrustCertificate,
    AuditIntegrity,
    IdentityBinding,
    GovernanceStatus,
    OverrideRecord,
    compute_tc_hash,
)


# --------------------------------------------------------------------------- #
# Helpers                                                                      #
# --------------------------------------------------------------------------- #

def _make_tc(
    *,
    decision: str = "Allow",
    tis_raw: float = 0.90,
    tis_current: float = 0.88,
    chain_id: str = "test-chain",
) -> TrustCertificate:
    """Build a minimal valid TrustCertificate for persistence testing."""
    now = datetime.now(timezone.utc)
    cert_id = f"TC-{uuid.uuid4().hex[:12]}"

    tc = TrustCertificate(
        certificate_id=cert_id,
        subject_id="test-subject-001",
        subject_type="recommendation",
        domain="financial_services",
        risk_tier="r3",
        action_class="a4",
        policy_severity="standard",
        checkpoint_id="chk-001",
        gca_context_id="gca-001",
        policy_set_id="fin-r3-a4-ct4",
        tis_raw=tis_raw,
        tis_adjusted=tis_raw * 0.98,
        tis_current=tis_current,
        component_scores={"B": 0.94, "A": 0.90, "C": 0.92, "K": 0.83},
        component_weights={"B": 0.25, "A": 0.30, "C": 0.25, "K": 0.20},
        penalty_aggregate=0.02,
        penalty_breakdown={
            "P_cb": 0.0, "P_d": 0.0, "P_n": 0.01, "P_h": 0.0, "P_ps": 0.01,
        },
        failing_dimension_subfactors={},
        gate_set=["B", "A", "C", "K"],
        thresholds={"B": 0.90, "A": 0.90, "C": 0.90, "K": 0.80},
        gate_results={"B": "pass", "A": "pass", "C": "pass", "K": "pass"},
        gate_passed=True,
        blocking_reason=None,
        failure_mode=None,
        decision=decision,
        requires_human_review=False,
        escalation_routed_to=[],
        source_references=["src-001"],
        retrieval_ids=["ret-001"],
        chain_of_custody_id="coc-001",
        audit_log_id="audit-001",
        integration_boundary_gaps=0,
        evaluation_timestamp=now,
        valid_until=now,
        decay_rate=0.05,
        recompute_required=True,
        invalidation_triggers=["model_version_change"],
        last_invalidation_event={"type": None},
        invalidation_status="valid",
        explanation_summary="Test TC for PostgreSQL persistence.",
        key_factors=["all gates passed"],
        key_concerns=[],
        regulatory_explanation_level="regulatory",
        regulatory_mapping=["SEC Reg BI"],
        lifecycle_state="admissible" if decision == "Allow" else "computed",
        state_transition_history=[{
            "from": "computed",
            "to": "admissible" if decision == "Allow" else "computed",
            "timestamp": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "reason": f"Initial evaluation — decision: {decision}",
        }],
        recomputed_from_certificate_id=None,
        superseded_by_certificate_id=None,
        archived=False,
        mcp_server_id=None,
        scope_attestation={},
        connection_type="CT-4",
        connection_type_modifier_id="ct-mod-v1",
        resolved_policy_profile_id="fin-r3-a4-ct4-resolved",
        chain_depth=0,
        chain_u_scores=[],
        identity_binding=IdentityBinding(
            requesting_identity="test-user",
            identity_type="human",
            role="analyst",
            authorization_tier="T2",
            identity_confidence=0.90,
            identity_verified=True,
            authentication_method="oauth2_mfa",
            requesting_session_id="session-001",
        ),
        governance_status=GovernanceStatus(
            governance_status="complete",
            evaluation_completeness_score=1.0,
            components_evaluated=["tis", "gate", "decision", "tc"],
            components_skipped=[],
            skip_reasons={},
            fail_safe_applied=False,
            fail_safe_type=None,
            governance_integrity_score=1.0,
        ),
        audit_integrity=AuditIntegrity(
            tc_hash="placeholder",
            previous_tc_hash=None,
            chain_sequence=0,
            chain_id=chain_id,
            hash_algorithm="sha256",
            integrity_verified=True,
            issued_by="tcs-reference-impl-v0.1",
        ),
        override_record=OverrideRecord(
            override_invoked=False,
            original_decision=None,
            override_decision=None,
            override_actor=None,
            override_actor_role=None,
            override_reason=None,
            override_type=None,
            policy_exception_id=None,
            regulatory_basis=None,
            co_authorizer=None,
            post_override_review_required=False,
            post_override_review_deadline=None,
            post_override_review_completed=False,
            override_creates_tc_amendment=False,
        ),
    )

    # Compute the real hash.
    tc.audit_integrity.tc_hash = compute_tc_hash(tc.to_dict())
    return tc


# --------------------------------------------------------------------------- #
# Fixtures                                                                     #
# --------------------------------------------------------------------------- #

@pytest.fixture
def pg_store():
    """
    Create a PostgresCertificateStore connected to the test database.
    Uses a unique schema prefix or truncates tables between tests.
    """
    store = PostgresCertificateStore(
        database=os.environ.get("TCS_PG_DATABASE", "tcs"),
    )
    store.run_migrations()

    # Clean slate for each test — truncate via raw SQL bypassing rules.
    # We temporarily drop and re-create the no-delete rules.
    conn = store._conn
    for table in (
        "trust_certificates", "lifecycle_events",
        "trust_metrics", "request_audit",
    ):
        # Drop the no_delete rule, truncate, then re-create the rule.
        conn.execute(f"DROP RULE IF EXISTS {table}_no_delete ON {table}")
        conn.execute(f"TRUNCATE {table} RESTART IDENTITY CASCADE")
    # Re-apply migrations to restore rules.
    store.run_migrations()

    yield store
    store.close()


# --------------------------------------------------------------------------- #
# Tests                                                                        #
# --------------------------------------------------------------------------- #

class TestIssueAndGet:
    def test_issue_returns_tc_with_chain_linkage(self, pg_store):
        tc = _make_tc()
        issued = pg_store.issue(tc)
        assert issued.audit_integrity.chain_sequence == 1
        assert issued.audit_integrity.previous_tc_hash is None
        assert issued.certificate_id == tc.certificate_id

    def test_get_round_trip(self, pg_store):
        tc = _make_tc()
        issued = pg_store.issue(tc)
        loaded = pg_store.get(issued.certificate_id)
        assert loaded.certificate_id == issued.certificate_id
        assert loaded.decision == issued.decision
        assert loaded.tis_raw == issued.tis_raw

    def test_get_not_found(self, pg_store):
        with pytest.raises(CertificateNotFoundError):
            pg_store.get("nonexistent-id")


class TestChain:
    def test_chain_of_five(self, pg_store):
        chain_id = f"chain-{uuid.uuid4().hex[:8]}"
        issued = []
        for _ in range(5):
            tc = _make_tc(chain_id=chain_id)
            issued.append(pg_store.issue(tc))

        # Chain sequence should be 1..5.
        assert [t.audit_integrity.chain_sequence for t in issued] == [1, 2, 3, 4, 5]

        # Each previous_tc_hash links to the prior.
        assert issued[0].audit_integrity.previous_tc_hash is None
        for i in range(1, 5):
            assert issued[i].audit_integrity.previous_tc_hash == issued[i-1].audit_integrity.tc_hash

    def test_verify_chain_passes(self, pg_store):
        chain_id = f"chain-{uuid.uuid4().hex[:8]}"
        for _ in range(5):
            pg_store.issue(_make_tc(chain_id=chain_id))
        assert pg_store.verify_chain(chain_id) is True

    def test_list_chain(self, pg_store):
        chain_id = f"chain-{uuid.uuid4().hex[:8]}"
        for _ in range(3):
            pg_store.issue(_make_tc(chain_id=chain_id))
        chain = pg_store.list_chain(chain_id)
        assert len(chain) == 3
        assert chain[0].audit_integrity.chain_sequence == 1
        assert chain[2].audit_integrity.chain_sequence == 3


class TestAggregation:
    def test_count(self, pg_store):
        for _ in range(3):
            pg_store.issue(_make_tc())
        assert pg_store.count() == 3

    def test_decision_counts(self, pg_store):
        pg_store.issue(_make_tc(decision="Allow"))
        pg_store.issue(_make_tc(decision="Allow"))
        pg_store.issue(_make_tc(decision="Hold", tis_current=0.0))
        counts = pg_store.decision_counts()
        assert counts["Allow"] == 2
        assert counts["Hold"] == 1

    def test_tis_distribution(self, pg_store):
        pg_store.issue(_make_tc(tis_current=0.90))
        pg_store.issue(_make_tc(tis_current=0.75))
        pg_store.issue(_make_tc(decision="Stop", tis_current=0.0))
        dist = pg_store.tis_distribution()
        assert dist["count"] == 3
        assert dist["histogram"]["allow_zone"] == 1
        assert dist["histogram"]["review_zone"] == 1
        assert dist["histogram"]["stop_zone"] == 1

    def test_gate_failure_rate(self, pg_store):
        pg_store.issue(_make_tc(decision="Allow"))
        pg_store.issue(_make_tc(decision="Stop", tis_current=0.0))
        rate = pg_store.gate_failure_rate()
        assert rate == 0.5

    def test_list_recent(self, pg_store):
        for _ in range(5):
            pg_store.issue(_make_tc())
        recent = pg_store.list_recent(limit=3)
        assert len(recent) == 3

    def test_governance_integrity_score(self, pg_store):
        pg_store.issue(_make_tc(decision="Allow"))
        score = pg_store.governance_integrity_score()
        assert 0.0 <= score <= 1.0


class TestAppendOnly:
    def test_update_is_no_op(self, pg_store):
        tc = _make_tc()
        issued = pg_store.issue(tc)

        # Attempt UPDATE — should be silently ignored by the RULE.
        pg_store._conn.execute(
            "UPDATE trust_certificates SET decision = 'Stop' "
            "WHERE certificate_id = %s",
            (issued.certificate_id,),
        )

        # Verify the row is unchanged.
        loaded = pg_store.get(issued.certificate_id)
        assert loaded.decision == "Allow"  # not 'Stop'

    def test_delete_is_no_op(self, pg_store):
        tc = _make_tc()
        issued = pg_store.issue(tc)

        # Attempt DELETE — should be silently ignored by the RULE.
        pg_store._conn.execute(
            "DELETE FROM trust_certificates WHERE certificate_id = %s",
            (issued.certificate_id,),
        )

        # Row still exists.
        loaded = pg_store.get(issued.certificate_id)
        assert loaded.certificate_id == issued.certificate_id


class TestMigrationIdempotency:
    def test_run_migrations_twice(self, pg_store):
        """Running migrations a second time should not raise."""
        pg_store.run_migrations()
        pg_store.run_migrations()
        # If we got here, it's idempotent.
        assert pg_store.count() >= 0
