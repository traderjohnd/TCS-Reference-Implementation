"""
tests/test_reporting.py
=======================

Coverage for the Phase 5 Reporting / Trust Telemetry surface
(``/v2/reporting/*``).

Every endpoint here is READ-ONLY. The tests therefore stand up a
fresh in-memory ``CertificateStore``, inject TCs (and a few
``lifecycle_events`` rows) with controlled timestamps, then exercise
the reporting endpoints and assert on aggregation correctness.

What we deliberately avoid:

  - any schema change
  - any LLM call
  - any policy mutation
  - any reuse of the existing populated_store fixture from
    test_control_plane.py, because reporting tests need precise
    control over evaluation_timestamp values that the demo fixture
    doesn't provide
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import pytest
from fastapi.testclient import TestClient

from tcs.api.app import create_app
from tcs.decision_engine import map_decision
from tcs.persistence import CertificateStore
from tcs.policy_profiles import load_profile
from tcs.tis_engine import TISInput, compute_tis
from tcs.trust_certificate import generate_certificate


# --------------------------------------------------------------------------- #
# TC factory                                                                   #
# --------------------------------------------------------------------------- #

def _make_tc(
    *,
    when: datetime,
    decision: str = "Allow",
    subject_id: Optional[str] = None,
    profile_id: str = "fin-r3-a4-ct4",
    s_base: Optional[float] = None,
    gate_results: Optional[Dict[str, str]] = None,
    governance_rule_matches: Optional[List[Dict[str, Any]]] = None,
):
    """
    Build a TC with a controlled ``evaluation_timestamp``, force a
    target decision, and optionally override scoring fields that the
    Reporting endpoints aggregate over.

    Returned object is unissued — call ``store.issue(tc)`` to persist.
    """
    if subject_id is None:
        subject_id = f"rep-{decision}-{when.strftime('%Y%m%d%H%M%S%f')}"

    profile = load_profile(profile_id)

    # Reasonable starting scores. Each test overrides whichever subset
    # of (decision, s_base, gate_results, rule_matches) it needs.
    inp = TISInput(
        subject_id=subject_id,
        subject_type="recommendation",
        policy_profile=profile,
        dimension_scores={"B": 0.95, "A": 0.95, "C": 1.00, "K": 0.95},
        sub_factor_scores={"C": {"C3": 1.0}},
        context_metadata={
            "n_gaps": 0,
            "context_age_hours": 0.1,
            "novelty_score": 0.0,
            "days_since_review": 1,
            "is_policy_sensitive": False,
        },
        elapsed_hours=0.0,
        is_valid=1,
        invalidation_event=None,
        evaluation_time=when,
    )
    res = compute_tis(inp)
    natural_decision, requires_review = map_decision(inp, res)
    if natural_decision != decision:
        # Force the desired decision. The TIS result is left intact;
        # reporting aggregates on the recorded decision label and (where
        # called for) on overrides of specific TC fields below.
        requires_review = decision in ("Hold", "Escalate", "Stop")
    tc = generate_certificate(inp, res, decision, requires_review)

    # Mutate decision-derived fields the engine doesn't know to recompute.
    # The TC dataclass is not frozen; mutating before issue is safe.
    if s_base is not None:
        tc.s_base = float(s_base)
    if gate_results is not None:
        tc.gate_results = dict(gate_results)
    if governance_rule_matches is not None:
        tc.governance_rule_matches = list(governance_rule_matches)
    return tc


def _issue(store, tc):
    return store.issue(tc)


# --------------------------------------------------------------------------- #
# Lifecycle-event helper                                                       #
# --------------------------------------------------------------------------- #

def _insert_override_event(store, tc_id: str, *, when: datetime,
                           decision: str = "Allow",
                           actor: str = "reviewer_test",
                           justification: str = "Test override."):
    """
    Insert an ``override_applied`` lifecycle event directly into the
    store. Mirrors the reason format the real override endpoints
    write so ``_parse_override_reason`` round-trips.
    """
    reason = f"{decision}: {justification} (by {actor})"
    store._conn.execute(
        "INSERT INTO lifecycle_events "
        "(certificate_id, from_state, to_state, reason, occurred_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (
            tc_id, "computed", "override_applied", reason,
            when.strftime("%Y-%m-%dT%H:%M:%SZ"),
        ),
    )


# --------------------------------------------------------------------------- #
# Fixtures                                                                     #
# --------------------------------------------------------------------------- #

@pytest.fixture
def store():
    s = CertificateStore(":memory:")
    yield s
    s.close()


@pytest.fixture
def client(store):
    """Empty store + app. Tests seed data themselves so they control timing."""
    app = create_app(store=store)
    with TestClient(app) as c:
        yield c


@pytest.fixture
def now():
    """Fixed 'now' used inside tests to make windows deterministic."""
    return datetime.now(timezone.utc).replace(microsecond=0)


# --------------------------------------------------------------------------- #
# Empty-DB invariants (all endpoints)                                          #
# --------------------------------------------------------------------------- #

class TestEmptyState:
    """
    Reporting must never 404 on an empty store. Every endpoint returns
    200 with an empty list or zeroed bucket payload — this is the
    "No governance activity in the selected window" surface the
    frontend renders.
    """

    EMPTY_ENDPOINTS = [
        ("/v2/reporting/decisions-over-time",     "buckets"),
        ("/v2/reporting/decisions-by-policy",     "rows"),
        ("/v2/reporting/override-rate-by-policy", "rows"),
        ("/v2/reporting/non-allow-trends",        "buckets"),
        ("/v2/reporting/top-rules",               "rules"),
        ("/v2/reporting/score-averages",          "buckets"),
        ("/v2/reporting/override-activity",       "events"),
    ]

    @pytest.mark.parametrize("path,list_key", EMPTY_ENDPOINTS)
    def test_endpoint_returns_empty_list_on_empty_store(self, client, path, list_key):
        resp = client.get(path)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert isinstance(body.get(list_key), list)
        assert body[list_key] == []

    def test_failed_back_dimensions_always_returns_all_four_dims(self, client):
        # This panel is the one exception to the "empty list" rule:
        # it always returns exactly the four BACK dimensions in
        # canonical order so the bar chart has a stable layout.
        resp = client.get("/v2/reporting/failed-back-dimensions")
        assert resp.status_code == 200
        dims = resp.json()["dims"]
        assert [d["dim"] for d in dims] == ["B", "A", "C", "K"]
        for d in dims:
            assert d["fail_count"] == 0
            assert d["evaluated"] == 0


# --------------------------------------------------------------------------- #
# Window parameter validation                                                  #
# --------------------------------------------------------------------------- #

class TestWindowValidation:
    """An unknown window value must 400 — silent empty results would
    hide a contract bug from the caller."""

    def test_unknown_window_rejected(self, client):
        resp = client.get("/v2/reporting/decisions-over-time?window=999d")
        assert resp.status_code == 400
        assert "24h" in resp.json()["detail"]

    def test_unknown_bucket_rejected(self, client):
        resp = client.get("/v2/reporting/decisions-over-time?window=7d&bucket=year")
        assert resp.status_code == 400

    @pytest.mark.parametrize("window,bucket", [
        ("24h", "hour"),
        ("7d",  "day"),
        ("30d", "day"),
    ])
    def test_default_bucket_matches_window(self, client, window, bucket):
        resp = client.get(f"/v2/reporting/decisions-over-time?window={window}")
        assert resp.status_code == 200
        assert resp.json()["bucket"] == bucket


# --------------------------------------------------------------------------- #
# 1. decisions-over-time                                                       #
# --------------------------------------------------------------------------- #

class TestDecisionsOverTime:
    def test_window_filter_excludes_older_tcs(self, client, store, now):
        # Two TCs inside 7d window, one outside it.
        _issue(store, _make_tc(when=now - timedelta(days=2), decision="Allow"))
        _issue(store, _make_tc(when=now - timedelta(days=5), decision="Hold"))
        _issue(store, _make_tc(when=now - timedelta(days=20), decision="Stop"))

        body = client.get("/v2/reporting/decisions-over-time?window=7d").json()
        total = sum(sum(b["counts"].values()) for b in body["buckets"])
        assert total == 2

        body30 = client.get("/v2/reporting/decisions-over-time?window=30d").json()
        total30 = sum(sum(b["counts"].values()) for b in body30["buckets"])
        assert total30 == 3

    def test_bucket_counts_per_day(self, client, store, now):
        day1 = now - timedelta(days=1)
        day2 = now - timedelta(days=2)
        _issue(store, _make_tc(when=day1, decision="Allow"))
        _issue(store, _make_tc(when=day1, decision="Allow"))
        _issue(store, _make_tc(when=day1, decision="Hold"))
        _issue(store, _make_tc(when=day2, decision="Stop"))

        body = client.get("/v2/reporting/decisions-over-time?window=7d").json()
        # Should land in 2 day buckets.
        assert len(body["buckets"]) == 2

        # Each bucket key truncates to YYYY-MM-DDT00:00:00Z.
        keys = {b["t"]: b["counts"] for b in body["buckets"]}
        d1key = day1.strftime("%Y-%m-%dT00:00:00Z")
        d2key = day2.strftime("%Y-%m-%dT00:00:00Z")
        assert keys[d1key]["Allow"] == 2
        assert keys[d1key]["Hold"]  == 1
        assert keys[d2key]["Stop"]  == 1

    def test_hourly_bucket_for_24h_window(self, client, store, now):
        h1 = now - timedelta(hours=1)
        h2 = now - timedelta(hours=2)
        _issue(store, _make_tc(when=h1, decision="Allow"))
        _issue(store, _make_tc(when=h2, decision="Hold"))
        body = client.get("/v2/reporting/decisions-over-time?window=24h").json()
        assert body["bucket"] == "hour"
        assert len(body["buckets"]) == 2
        # Hourly key is YYYY-MM-DDTHH:00:00Z.
        for b in body["buckets"]:
            assert b["t"].endswith(":00:00Z")


# --------------------------------------------------------------------------- #
# 2. decisions-by-policy                                                       #
# --------------------------------------------------------------------------- #

class TestDecisionsByPolicy:
    def test_groups_by_policy_set_id(self, client, store, now):
        # Two profiles, mixed decisions.
        _issue(store, _make_tc(when=now - timedelta(hours=2),
                               decision="Allow",
                               profile_id="fin-r3-a4-ct4"))
        _issue(store, _make_tc(when=now - timedelta(hours=2),
                               decision="Hold",
                               profile_id="fin-r3-a4-ct4"))
        _issue(store, _make_tc(when=now - timedelta(hours=2),
                               decision="Allow",
                               profile_id="healthcare-r3-a4-ct4"))

        body = client.get("/v2/reporting/decisions-by-policy?window=7d").json()
        rows = {r["policy_set_id"]: r for r in body["rows"]}
        assert rows["fin-r3-a4-ct4"]["total"] == 2
        assert rows["fin-r3-a4-ct4"]["counts"] == {"Allow": 1, "Hold": 1}
        assert rows["healthcare-r3-a4-ct4"]["total"] == 1
        assert rows["healthcare-r3-a4-ct4"]["counts"] == {"Allow": 1}

    def test_rows_sorted_by_total_desc(self, client, store, now):
        # 3 of one, 1 of another. Higher-total row must come first.
        for _ in range(3):
            _issue(store, _make_tc(when=now - timedelta(hours=1),
                                   decision="Allow",
                                   profile_id="fin-r3-a4-ct4"))
        _issue(store, _make_tc(when=now - timedelta(hours=1),
                               decision="Allow",
                               profile_id="healthcare-r3-a4-ct4"))
        rows = client.get("/v2/reporting/decisions-by-policy?window=7d").json()["rows"]
        assert rows[0]["policy_set_id"] == "fin-r3-a4-ct4"
        assert rows[0]["total"] == 3

    def test_pack_name_is_none_when_unknown(self, client, store, now):
        _issue(store, _make_tc(when=now - timedelta(hours=1),
                               decision="Allow",
                               profile_id="fin-r3-a4-ct4"))
        rows = client.get("/v2/reporting/decisions-by-policy?window=7d").json()["rows"]
        # fin-r3-a4-ct4 is a built-in profile, not a composed pack, so
        # pack_name is null (the frontend then displays policy_set_id).
        assert rows[0]["pack_name"] is None
        assert rows[0]["policy_set_id"] == "fin-r3-a4-ct4"


# --------------------------------------------------------------------------- #
# 3. override-rate-by-policy                                                   #
# --------------------------------------------------------------------------- #

class TestOverrideRateByPolicy:
    def test_rate_math(self, client, store, now):
        # 5 TCs under one profile, override 2 → rate = 0.4.
        when = now - timedelta(hours=1)
        tc_ids: List[str] = []
        for i in range(5):
            issued = _issue(store, _make_tc(
                when=when, decision="Hold",
                subject_id=f"rate-{i}",
                profile_id="fin-r3-a4-ct4",
            ))
            tc_ids.append(issued.certificate_id)

        _insert_override_event(store, tc_ids[0], when=when)
        _insert_override_event(store, tc_ids[1], when=when)

        rows = client.get(
            "/v2/reporting/override-rate-by-policy?window=7d"
        ).json()["rows"]
        row = next(r for r in rows if r["policy_set_id"] == "fin-r3-a4-ct4")
        assert row["total"] == 5
        assert row["overridden"] == 2
        assert row["rate"] == pytest.approx(0.4)

    def test_rate_zero_when_no_overrides(self, client, store, now):
        for i in range(3):
            _issue(store, _make_tc(
                when=now - timedelta(hours=1), decision="Allow",
                subject_id=f"no-ovr-{i}",
            ))
        rows = client.get(
            "/v2/reporting/override-rate-by-policy?window=7d"
        ).json()["rows"]
        assert rows[0]["overridden"] == 0
        assert rows[0]["rate"] == 0.0

    def test_override_outside_window_not_counted(self, client, store, now):
        # TC issued 2 days ago, overridden 20 days ago — the override
        # is outside 7d window. Override count stays 0 in 7d.
        issued = _issue(store, _make_tc(
            when=now - timedelta(days=2), decision="Hold",
            subject_id="ovr-outside",
        ))
        _insert_override_event(
            store, issued.certificate_id, when=now - timedelta(days=20),
        )
        rows = client.get(
            "/v2/reporting/override-rate-by-policy?window=7d"
        ).json()["rows"]
        row = next(r for r in rows if r["total"] == 1)
        assert row["overridden"] == 0
        assert row["rate"] == 0.0


# --------------------------------------------------------------------------- #
# 4. non-allow-trends                                                          #
# --------------------------------------------------------------------------- #

class TestNonAllowTrends:
    def test_filters_to_three_decisions(self, client, store, now):
        when = now - timedelta(hours=1)
        _issue(store, _make_tc(when=when, decision="Allow",    subject_id="t-a"))
        _issue(store, _make_tc(when=when, decision="Hold",     subject_id="t-h"))
        _issue(store, _make_tc(when=when, decision="Escalate", subject_id="t-e"))
        _issue(store, _make_tc(when=when, decision="Stop",     subject_id="t-s"))
        _issue(store, _make_tc(when=when, decision="Observe",  subject_id="t-o"))

        body = client.get("/v2/reporting/non-allow-trends?window=7d").json()
        assert len(body["buckets"]) == 1
        b = body["buckets"][0]
        # Allow + Observe are excluded from the non-allow panel.
        assert b["Hold"] == 1
        assert b["Escalate"] == 1
        assert b["Stop"] == 1


# --------------------------------------------------------------------------- #
# 5. top-rules                                                                 #
# --------------------------------------------------------------------------- #

class TestTopRules:
    def test_aggregates_across_tcs(self, client, store, now):
        rule_x = [{"rule_id": "rule_x", "rule_name": "X — outbound check"}]
        rule_y = [{"rule_id": "rule_y", "rule_name": "Y — disclosure check"}]

        # rule_x fires 3 times under Stop; rule_y fires once under Hold.
        for i in range(3):
            _issue(store, _make_tc(
                when=now - timedelta(hours=1), decision="Stop",
                subject_id=f"tr-x-{i}",
                governance_rule_matches=list(rule_x),
            ))
        _issue(store, _make_tc(
            when=now - timedelta(hours=1), decision="Hold",
            subject_id="tr-y-0",
            governance_rule_matches=list(rule_y),
        ))

        rules = client.get("/v2/reporting/top-rules?window=7d").json()["rules"]
        # Most-fired first.
        assert rules[0]["rule_id"] == "rule_x"
        assert rules[0]["fires"] == 3
        assert rules[0]["top_decision"] == "Stop"
        assert rules[1]["rule_id"] == "rule_y"
        assert rules[1]["fires"] == 1
        assert rules[1]["top_decision"] == "Hold"

    def test_respects_limit(self, client, store, now):
        for i in range(5):
            _issue(store, _make_tc(
                when=now - timedelta(hours=1), decision="Stop",
                subject_id=f"tr-l-{i}",
                governance_rule_matches=[{"rule_id": f"r_{i}", "rule_name": f"R{i}"}],
            ))
        rules = client.get("/v2/reporting/top-rules?window=7d&limit=3").json()["rules"]
        assert len(rules) == 3


# --------------------------------------------------------------------------- #
# 6. failed-back-dimensions                                                    #
# --------------------------------------------------------------------------- #

class TestFailedBackDimensions:
    def test_fail_counts_per_dim(self, client, store, now):
        when = now - timedelta(hours=1)
        # B-fail TCs (2); A-fail TCs (1); K not_applicable TCs (1).
        _issue(store, _make_tc(when=when, decision="Hold", subject_id="bd-b1",
                               gate_results={"B": "fail", "A": "pass", "C": "pass", "K": "pass"}))
        _issue(store, _make_tc(when=when, decision="Hold", subject_id="bd-b2",
                               gate_results={"B": "fail", "A": "pass", "C": "pass", "K": "pass"}))
        _issue(store, _make_tc(when=when, decision="Hold", subject_id="bd-a1",
                               gate_results={"B": "pass", "A": "fail", "C": "pass", "K": "pass"}))
        _issue(store, _make_tc(when=when, decision="Allow", subject_id="bd-na",
                               gate_results={"B": "pass", "A": "pass", "C": "pass", "K": "not_applicable"}))

        dims = {d["dim"]: d for d in
                client.get("/v2/reporting/failed-back-dimensions?window=7d").json()["dims"]}
        assert dims["B"]["fail_count"] == 2
        assert dims["A"]["fail_count"] == 1
        assert dims["C"]["fail_count"] == 0
        assert dims["K"]["fail_count"] == 0
        # K had one not_applicable row — it doesn't count toward
        # evaluated or fail.
        assert dims["K"]["evaluated"] == 3
        assert dims["B"]["evaluated"] == 4


# --------------------------------------------------------------------------- #
# 7. score-averages                                                            #
# --------------------------------------------------------------------------- #

class TestScoreAverages:
    def test_avg_per_bucket(self, client, store, now):
        day1 = now - timedelta(days=1)
        day2 = now - timedelta(days=2)
        _issue(store, _make_tc(when=day1, decision="Allow", subject_id="sa-1",
                               s_base=0.80))
        _issue(store, _make_tc(when=day1, decision="Allow", subject_id="sa-2",
                               s_base=0.90))
        _issue(store, _make_tc(when=day2, decision="Hold",  subject_id="sa-3",
                               s_base=0.70))
        body = client.get("/v2/reporting/score-averages?window=7d").json()
        per_t = {b["t"]: b for b in body["buckets"]}
        d1key = day1.strftime("%Y-%m-%dT00:00:00Z")
        d2key = day2.strftime("%Y-%m-%dT00:00:00Z")
        assert per_t[d1key]["avg_s_base"] == pytest.approx(0.85)
        assert per_t[d1key]["n"] == 2
        assert per_t[d2key]["avg_s_base"] == pytest.approx(0.70)
        assert per_t[d2key]["n"] == 1


# --------------------------------------------------------------------------- #
# 8. override-activity                                                         #
# --------------------------------------------------------------------------- #

class TestOverrideActivity:
    def test_returns_event_with_parsed_fields(self, client, store, now):
        issued = _issue(store, _make_tc(
            when=now - timedelta(hours=2), decision="Hold",
            subject_id="oa-1",
            profile_id="fin-r3-a4-ct4",
        ))
        _insert_override_event(
            store, issued.certificate_id,
            when=now - timedelta(hours=1),
            decision="Allow",
            actor="reviewer_42",
            justification="Reviewed and approved.",
        )

        body = client.get("/v2/reporting/override-activity?window=7d").json()
        assert body["count"] == 1
        ev = body["events"][0]
        assert ev["certificate_id"] == issued.certificate_id
        assert ev["original_decision"] == "Hold"
        assert ev["policy_set_id"] == "fin-r3-a4-ct4"
        assert ev["override_decision"] == "Allow"
        assert ev["override_actor"] == "reviewer_42"
        assert ev["override_reason_text"] == "Reviewed and approved."

    def test_parser_robust_to_by_in_justification(self, client, store, now):
        """
        Regression for the parser-hardening pass: a justification that
        itself contains '(by ...)' must not be confused for the actor
        marker. Reporting reuses _parse_override_reason from
        routes_govern, so the regression test surface here covers
        cross-surface consistency.
        """
        issued = _issue(store, _make_tc(
            when=now - timedelta(hours=2), decision="Hold",
            subject_id="oa-tricky",
        ))
        tricky = (
            "Approved (by Compliance memo 2026-Q2) under exception 4.3; "
            "see annotation (by reviewer note)."
        )
        _insert_override_event(
            store, issued.certificate_id,
            when=now - timedelta(hours=1),
            decision="Allow",
            actor="compliance_lead_99",
            justification=tricky,
        )
        ev = client.get("/v2/reporting/override-activity?window=7d").json()["events"][0]
        # End-anchored parse selects the trailing "(by compliance_lead_99)".
        assert ev["override_actor"] == "compliance_lead_99"
        assert ev["override_reason_text"] == tricky

    def test_newest_first(self, client, store, now):
        a = _issue(store, _make_tc(when=now - timedelta(hours=5), decision="Hold",
                                   subject_id="oa-a"))
        b = _issue(store, _make_tc(when=now - timedelta(hours=5), decision="Hold",
                                   subject_id="oa-b"))
        _insert_override_event(store, a.certificate_id, when=now - timedelta(hours=4))
        _insert_override_event(store, b.certificate_id, when=now - timedelta(hours=1))

        events = client.get("/v2/reporting/override-activity?window=7d").json()["events"]
        assert events[0]["certificate_id"] == b.certificate_id
        assert events[1]["certificate_id"] == a.certificate_id
