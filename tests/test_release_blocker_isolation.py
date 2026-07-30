"""
Release-blocker fix 2/2 (D2) — malformed-record isolation at LIST
boundaries.

A malformed or tampered stored certificate row must be excluded and
identified at list/dashboard/stream/queue/aggregate boundaries — never
repaired, never served as an ordinary certificate, and never fatal to
the whole feed — while authoritative single-record reads, verification,
and replay keep their strict fail-closed behavior, and hash-chain
verification keeps reporting the underlying problem truthfully.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from tcs.api.app import create_app
from tcs.canonical import CertificateInvariantError
from tcs.decision_engine import map_decision_versioned
from tcs.persistence import CertificateStore
from tcs.persistence.certificate_store import tolerant_tc_from_json
from tcs.policy_profiles import load_profile
from tcs.tis_engine import TISInput, compute_tis_v2
from tcs.trust_certificate import generate_certificate_v2


# --------------------------------------------------------------------------- #
# Fixtures                                                                     #
# --------------------------------------------------------------------------- #

def _issue(store, subject, *, elapsed="0.0000", scores=None, sub=None,
           meta=None, chain="chain-iso-test"):
    D = Decimal
    m = {"n_gaps": 0, "context_age_hours": 0.1, "novelty_score": 0.0,
         "days_since_review": 1, "is_policy_sensitive": False,
         "chain_id": chain}
    m.update(meta or {})
    prof = load_profile("fin-r3-a4-ct4")
    inp = TISInput(
        subject_id=subject, subject_type="recommendation",
        policy_profile=prof,
        dimension_scores=scores or {"B": D("0.95"), "A": D("0.95"),
                                    "C": D("0.95"), "K": D("0.85")},
        sub_factor_scores=sub or {"C": {"C3": D("1.0000")}},
        context_metadata=m, elapsed_hours=D(elapsed), is_valid=1,
        invalidation_event=None,
        evaluation_time=datetime.now(timezone.utc).replace(microsecond=0),
    )
    res = compute_tis_v2(inp)
    dec, review = map_decision_versioned(inp, res)
    return store.issue(generate_certificate_v2(inp, res, dec, review))


def _plant_malformed(store, source_tc, cert_id, *, corrupt):
    """Insert a tampered copy of a valid stored row (data-level tamper —
    the sealing path itself cannot produce such a row)."""
    conn = store._conn
    cols = [r[1] for r in conn.execute("PRAGMA table_info(trust_certificates)")]
    src = conn.execute(
        "SELECT * FROM trust_certificates WHERE certificate_id = ?",
        (source_tc.certificate_id,),
    ).fetchone()
    d = json.loads(src["content_json"])
    d["certificate_id"] = cert_id
    d["subject_id"] = f"subject-{cert_id}"
    d["audit_integrity"]["chain_id"] = f"chain-{cert_id}"
    d["audit_integrity"]["chain_sequence"] = 1
    d["audit_integrity"]["previous_tc_hash"] = None
    d["audit_integrity"]["tc_hash"] = ("0" * 56) + cert_id[-8:].rjust(8, "0")
    corrupt(d)
    row = {c: src[c] for c in cols if c != "id"}
    row.update({
        "certificate_id": cert_id,
        "subject_id": f"subject-{cert_id}",
        "chain_id": f"chain-{cert_id}",
        "chain_sequence": 1,
        "tc_hash": d["audit_integrity"]["tc_hash"],
        "previous_tc_hash": None,
        "content_json": json.dumps(d),
    })
    conn.execute(
        f"INSERT INTO trust_certificates ({','.join(row)}) "
        f"VALUES ({','.join('?' * len(row))})",
        list(row.values()),
    )
    conn.commit()
    return cert_id


def _corrupt_sbase(d):
    d["s_base"] = "0.911"          # non-canonical score string


def _corrupt_gate(d):
    d["gate_result"] = True        # not integer 0|1


@pytest.fixture()
def seeded(tmp_path):
    """(app-client, store, ids) — valid Hold/Escalate/Stop/Allow rows
    with one malformed row planted BETWEEN valid neighbors."""
    store = CertificateStore(str(tmp_path / "iso.db"))
    hold = _issue(store, "iso-hold", scores={
        "B": Decimal("0.95"), "A": Decimal("0.90"),
        "C": Decimal("0.95"), "K": Decimal("0.88")})       # gate-fail Hold
    esc = _issue(store, "iso-escalate", elapsed="20.0000")   # Escalate
    stop = _issue(store, "iso-stop",
                  scores={"B": Decimal("0.94"), "A": Decimal("0.94"),
                          "C": Decimal("0.31"), "K": Decimal("0.88")},
                  sub={"C": {"C3": Decimal("0.0000")}},
                  meta={"blocking_context": "test_zero",
                        "injection_detected": True,
                        "injection_reason": "chunk_id=c1 (explanatory)",
                        "c3_signals": [{
                            "source_type": "injection_scan",
                            "pattern_id": "inj-001-ignore-instructions",
                            "pattern_set_version": "tcs-injection-patterns-v1",
                            "location_tag": "chunk_id=c1",
                            "connector_type": "", "detail_code": ""}]})
    bad = _plant_malformed(store, hold, "malformed-mid-0001",
                           corrupt=_corrupt_sbase)
    allow = _issue(store, "iso-allow")                        # newest valid
    app = create_app(store=store)
    client = TestClient(app)
    with client:
        yield client, store, {
            "hold": hold, "escalate": esc, "stop": stop,
            "allow": allow, "bad": bad,
        }
    store.close()


# --------------------------------------------------------------------------- #
# Tolerant helper contract                                                     #
# --------------------------------------------------------------------------- #

class TestTolerantHelper:
    def test_valid_row_round_trips(self, seeded):
        _, store, ids = seeded
        row = store._conn.execute(
            "SELECT content_json FROM trust_certificates "
            "WHERE certificate_id = ?", (ids["allow"].certificate_id,),
        ).fetchone()
        tc, problem = tolerant_tc_from_json(row["content_json"])
        assert problem is None
        assert tc.certificate_id == ids["allow"].certificate_id

    def test_malformed_row_excluded_with_names_not_values(self, seeded):
        _, store, ids = seeded
        row = store._conn.execute(
            "SELECT content_json FROM trust_certificates "
            "WHERE certificate_id = ?", (ids["bad"],),
        ).fetchone()
        tc, problem = tolerant_tc_from_json(row["content_json"])
        assert tc is None
        assert problem["excluded"] is True
        assert problem["certificate_id"] == ids["bad"]
        assert problem["failure_category"] == "invalid_certificate"
        assert "s_base" in problem["invalid_fields"]
        # The untrusted malformed VALUE never appears anywhere.
        assert "0.911" not in json.dumps(problem)

    def test_unreadable_json_categorized(self):
        tc, problem = tolerant_tc_from_json("{not json")
        assert tc is None
        assert problem["failure_category"] == "unreadable_json"

    def test_unsupported_schema_categorized(self, seeded):
        _, store, ids = seeded
        row = store._conn.execute(
            "SELECT content_json FROM trust_certificates "
            "WHERE certificate_id = ?", (ids["allow"].certificate_id,),
        ).fetchone()
        d = json.loads(row["content_json"])
        d["certificate_schema_version"] = 3
        tc, problem = tolerant_tc_from_json(json.dumps(d))
        assert tc is None
        assert problem["failure_category"] == "unsupported_schema"
        assert problem["invalid_fields"] == ["certificate_schema_version"]


# --------------------------------------------------------------------------- #
# List boundaries: one bad record never blanks a feed                          #
# --------------------------------------------------------------------------- #

class TestListIsolation:
    def test_certificates_list_serves_valid_neighbors(self, seeded):
        client, _, ids = seeded
        r = client.get("/v2/certificates?limit=50")
        assert r.status_code == 200, r.text
        body = r.json()
        served = {c["certificate_id"] for c in body["certificates"]}
        for k in ("hold", "escalate", "stop", "allow"):
            assert ids[k].certificate_id in served
        # The malformed record is NOT served as an ordinary certificate.
        assert ids["bad"] not in served
        assert body["excluded_malformed_count"] == 1
        w = body["integrity_warnings"][0]
        assert w["certificate_id"] == ids["bad"]
        assert "s_base" in w["invalid_fields"]
        assert "0.911" not in json.dumps(body)

    def test_decisions_stream_and_queues_stay_available(self, seeded):
        client, _, ids = seeded
        stream = client.get("/v2/govern/decisions/stream?limit=50")
        assert stream.status_code == 200
        s = stream.json()
        assert s["excluded_malformed_count"] == 1
        decisions = {d["subject_id"]: d["decision"] for d in s["decisions"]}
        # Malformed Hold-shaped record excluded; valid Hold/Stop/Escalate
        # neighbors all present with their own decisions.
        assert decisions.get("iso-hold") == "Hold"
        assert decisions.get("iso-stop") == "Stop"
        assert decisions.get("iso-escalate") == "Escalate"
        assert f"subject-{ids['bad']}" not in decisions

        holds = client.get("/v2/govern/hold-queue?limit=20")
        assert holds.status_code == 200
        h = holds.json()
        assert h["excluded_malformed_count"] == 1
        assert [x["subject_id"] for x in h["holds"]] == ["iso-hold"]
        # Valid displayed count vs excluded count, distinguished.
        assert h["count"] == 1

        esc = client.get("/v2/govern/escalation-queue?limit=20")
        assert esc.status_code == 200
        e = esc.json()
        assert e["excluded_malformed_count"] == 1
        assert [x["subject_id"] for x in e["escalations"]] == ["iso-escalate"]

    def test_multiple_and_boundary_position_malformed_records(self, tmp_path):
        store = CertificateStore(str(tmp_path / "multi.db"))
        # Malformed FIRST (oldest), middle, and LAST (newest) around
        # valid rows — including malformed Hold-, Stop-, and
        # Escalate-shaped records.
        seed_hold = _issue(store, "m-hold", scores={
            "B": Decimal("0.95"), "A": Decimal("0.90"),
            "C": Decimal("0.95"), "K": Decimal("0.88")})
        _plant_malformed(store, seed_hold, "malformed-first-hold",
                         corrupt=_corrupt_sbase)
        _issue(store, "m-valid-1")
        seed_esc = _issue(store, "m-escalate", elapsed="20.0000")
        _plant_malformed(store, seed_esc, "malformed-mid-escalate",
                         corrupt=_corrupt_gate)
        _issue(store, "m-valid-2")
        seed_stop = _issue(store, "m-stop",
                           scores={"B": Decimal("0.94"), "A": Decimal("0.94"),
                                   "C": Decimal("0.31"), "K": Decimal("0.88")},
                           sub={"C": {"C3": Decimal("0.0000")}},
                           meta={"blocking_context": "t",
                                 "injection_detected": True,
                                 "injection_reason": "x",
                                 "c3_signals": [{
                                     "source_type": "injection_scan",
                                     "pattern_id": "inj-001-ignore-instructions",
                                     "pattern_set_version": "tcs-injection-patterns-v1",
                                     "location_tag": "chunk_id=c1",
                                     "connector_type": "", "detail_code": ""}]})
        _plant_malformed(store, seed_stop, "malformed-last-stop",
                         corrupt=_corrupt_sbase)

        tcs, excluded = store.list_recent_with_integrity(limit=50)
        assert len(excluded) == 3
        assert {e["certificate_id"] for e in excluded} == {
            "malformed-first-hold", "malformed-mid-escalate",
            "malformed-last-stop"}
        served = {t.subject_id for t in tcs}
        assert {"m-hold", "m-valid-1", "m-escalate",
                "m-valid-2", "m-stop"} <= served
        gate_problem = next(e for e in excluded
                            if e["certificate_id"] == "malformed-mid-escalate")
        assert "gate_result" in gate_problem["invalid_fields"]
        store.close()


# --------------------------------------------------------------------------- #
# Metrics / reporting / telemetry                                              #
# --------------------------------------------------------------------------- #

class TestMetricsAndReporting:
    def test_metrics_live_available_with_integrity_census(self, seeded):
        client, _, ids = seeded
        r = client.get("/v2/metrics/live")
        assert r.status_code == 200, r.text
        body = r.json()
        ri = body["record_integrity"]
        assert ri["malformed_record_count"] == 1
        assert ri["excluded_record_count"] == 1
        assert ri["excluded"][0]["certificate_id"] == ids["bad"]
        assert "0.911" not in json.dumps(body)

    def test_health_separates_availability_integrity_and_counts(self, seeded):
        client, store, ids = seeded
        r = client.get("/v2/health")
        assert r.status_code == 200
        body = r.json()
        # Degraded integrity signaled truthfully...
        assert body["status"] == "degraded"
        assert body["malformed_record_count"] == 1
        assert body["excluded_record_count"] == 1
        # ...the tampered row also breaks ITS chain's verification —
        # never converted into chain_intact: true by list isolation.
        assert body["chain_intact"] is False
        assert store.verify_chain(f"chain-{ids['bad']}") is False
        # ...while every read feed stays AVAILABLE.
        for path in ("/v2/certificates?limit=50",
                     "/v2/govern/decisions/stream?limit=50",
                     "/v2/govern/hold-queue", "/v2/govern/escalation-queue",
                     "/v2/metrics/live", "/v2/metrics/telemetry"):
            assert client.get(path).status_code == 200, path

    def test_valid_chains_still_verify_individually(self, seeded):
        client, store, ids = seeded
        assert store.verify_chain("chain-iso-test") is True

    def test_telemetry_and_reporting_exclude_and_count(self, seeded):
        client, _, ids = seeded
        t = client.get("/v2/metrics/telemetry?window=24h&limit=100")
        assert t.status_code == 200
        tb = t.json()
        assert tb["excluded_malformed_count"] == 1
        subjects = {r["subject_id"] for r in tb["records"]}
        assert f"subject-{ids['bad']}" not in subjects

        rules = client.get("/v2/reporting/top-rules?window=7d")
        assert rules.status_code == 200
        assert rules.json()["excluded_malformed_count"] == 1

        dims = client.get("/v2/reporting/failed-back-dimensions?window=7d")
        assert dims.status_code == 200
        db_ = dims.json()
        assert db_["excluded_malformed_count"] == 1
        # The malformed Hold-shaped record (gate fail on A) is EXCLUDED
        # from failure aggregates — only the valid rows' gates count.
        a_row = next(d for d in db_["dims"] if d["dim"] == "A")
        assert a_row["fail_count"] == 1   # the one VALID gate-fail Hold


# --------------------------------------------------------------------------- #
# Strict fail-closed paths preserved                                           #
# --------------------------------------------------------------------------- #

class TestStrictPathsPreserved:
    def test_single_record_get_fails_closed(self, seeded):
        client, store, ids = seeded
        with pytest.raises(CertificateInvariantError):
            store.get(ids["bad"])
        # The authoritative single-record route refuses loudly — the
        # TestClient re-raises the unhandled server exception (a real
        # deployment surfaces it as HTTP 500).
        with pytest.raises(CertificateInvariantError):
            client.get(f"/v2/certificates/{ids['bad']}")

    def test_valid_single_record_get_still_works(self, seeded):
        client, _, ids = seeded
        r = client.get(f"/v2/certificates/{ids['allow'].certificate_id}")
        assert r.status_code == 200
        assert r.json()["calculation_version"] == "tis-v2"

    def test_no_silent_rewrite_or_normalization(self, seeded):
        _, store, ids = seeded
        # The stored row is untouched by every tolerant read above:
        # the raw content still carries the tampered value verbatim.
        row = store._conn.execute(
            "SELECT content_json FROM trust_certificates "
            "WHERE certificate_id = ?", (ids["bad"],),
        ).fetchone()
        assert json.loads(row["content_json"])["s_base"] == "0.911"
