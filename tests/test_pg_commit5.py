"""
tis-v2 Commit 5a — real-PostgreSQL adapter verification (owner
correction 5).

Runs against an actual PostgreSQL instance (gated exactly like
test_pg_store.py):

    docker run -d --name tcs-pg -e POSTGRES_USER=tcs \
        -e POSTGRES_PASSWORD=tcs_dev -e POSTGRES_DB=tcs -p 5432:5432 \
        postgres:16
    TCS_TEST_PG=1 pytest tests/test_pg_commit5.py -v

Covers: v2 issuance + rehydration, mixed v1/v2 chain verification, raw
v1 and exact-schema v2 hash verification, transaction rollback after a
planted sealing failure, Decimal authoritative values preserved in
content_json, the documented lossy REAL dashboard columns, and the
monotonic issuance floor on the PostgreSQL adapter.
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from decimal import Decimal

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("TCS_TEST_PG", "0") != "1",
    reason="PostgreSQL tests disabled (set TCS_TEST_PG=1 to enable)",
)

try:
    import psycopg  # noqa: F401
    from tcs.persistence.pg_store import PostgresCertificateStore
except ImportError:
    pytest.skip("psycopg not installed", allow_module_level=True)

from tcs.decision_engine import map_decision, map_decision_versioned
from tcs.persistence.certificate_store import IssuanceVersionRegressionError
from tcs.policy_profiles import load_profile
from tcs.tis_engine import TISInput, compute_tis, compute_tis_v2
from tcs.trust_certificate import (
    compute_raw_stored_tc_hash,
    generate_certificate,
    generate_certificate_v2,
)
from tests.conftest import make_tis_input

EVAL_TIME = datetime(2026, 7, 29, 12, 0, 0)


@pytest.fixture
def pg_store():
    store = PostgresCertificateStore(
        database=os.environ.get("TCS_PG_DATABASE", "tcs"),
    )
    store.run_migrations()
    conn = store._conn
    for table in ("trust_certificates", "lifecycle_events",
                  "trust_metrics", "request_audit"):
        conn.execute(f"DROP RULE IF EXISTS {table}_no_delete ON {table}")
        conn.execute(f"TRUNCATE {table} RESTART IDENTITY CASCADE")
    store.run_migrations()
    yield store
    store.close()


def _v1_tc(chain_id="pg-chain", subject="pg-v1"):
    inp = make_tis_input(
        "fin-high-risk-suitability-v3",
        {"B": 0.94, "A": 0.90, "C": 0.92, "K": 0.83},
        subject_id=subject,
        context_metadata={"chain_id": chain_id},
    )
    res = compute_tis(inp)
    d, review = map_decision(inp, res)
    return generate_certificate(inp, res, d, review)


def _v2_tc(chain_id="pg-chain", subject="pg-v2"):
    inp = TISInput(
        subject_id=subject, subject_type="model_output",
        policy_profile=load_profile("fin-r3-a4-ct4"),
        dimension_scores={"B": Decimal("0.899996"),
                          "A": Decimal("0.95"),
                          "C": Decimal("0.95"), "K": Decimal("0.85")},
        context_metadata={"chain_id": chain_id},
        elapsed_hours=0.0, is_valid=1, invalidation_event=None,
        evaluation_time=EVAL_TIME,
    )
    res = compute_tis_v2(inp)
    d, review = map_decision_versioned(inp, res)
    return generate_certificate_v2(inp, res, d, review)


class TestPostgresV2:
    def test_v2_issue_and_rehydrate_byte_identical(self, pg_store):
        issued = pg_store.issue(_v2_tc())
        restored = pg_store.get(issued.certificate_id)
        assert restored.to_dict() == issued.to_dict()
        assert restored.certificate_schema_version == 2
        assert isinstance(restored.s_base, Decimal)

    def test_mixed_chain_raw_verification(self, pg_store):
        chain = "pg-mixed"
        pg_store.issue(_v1_tc(chain_id=chain))
        pg_store.issue(_v2_tc(chain_id=chain, subject="pg-v2-b"))
        pg_store.issue(_v2_tc(chain_id=chain, subject="pg-v2-c"))
        assert pg_store.verify_chain(chain) is True
        # Raw dispatch: v1 rows via the frozen legacy payload, v2 rows
        # via the exact-schema v2 payload — from stored dicts.
        raws = pg_store._list_chain_raw(chain)
        assert len(raws) == 3
        for raw in raws:
            assert compute_raw_stored_tc_hash(raw) == \
                raw["audit_integrity"]["tc_hash"]
        versions = [r.get("certificate_schema_version") for r in raws]
        assert versions == [None, 2, 2]   # absence IS the v1 wire

    def test_decimal_authoritative_in_content_json(self, pg_store):
        issued = pg_store.issue(_v2_tc(subject="pg-decimal"))
        row = pg_store._conn.execute(
            "SELECT content_json, tis_current FROM trust_certificates "
            "WHERE certificate_id = %s",
            (issued.certificate_id,),
        ).fetchone()
        content = json.loads(row["content_json"])
        # Authoritative: canonical decimal strings + lossless raw tier.
        assert content["s_base"] == format(issued.s_base, ".4f")
        assert content["component_scores_raw"]["B"] == "0.899996"
        # Documented lossy conversion in the denormalized REAL column.
        assert isinstance(row["tis_current"], float)
        assert abs(row["tis_current"] - float(issued.tis_current)) < 1e-9

    def test_rollback_on_planted_sealing_failure(self, pg_store):
        good = pg_store.issue(_v2_tc(subject="pg-good"))
        assert good is not None
        bad = _v2_tc(subject="pg-bad")
        bad.s_base = bad.s_base + Decimal("0.0001")   # canonical but wrong
        before = pg_store.count()
        with pytest.raises(Exception):
            pg_store.issue(bad)
        assert pg_store.count() == before   # transaction rolled back

    def test_issuance_floor_on_postgres(self, pg_store):
        pg_store.issue(_v2_tc(subject="pg-floor"))
        with pytest.raises(IssuanceVersionRegressionError):
            pg_store.issue(_v1_tc(subject="pg-floor-v1"))
        # v2 continues; historical reads unaffected.
        pg_store.issue(_v2_tc(subject="pg-floor-2", chain_id="pg-chain2"))
        assert pg_store.count() == 2
