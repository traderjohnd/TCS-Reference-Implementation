"""
tcs.api.routes_reporting
========================

Phase 5 demo-hardening — Runtime governance reporting and trust
telemetry.

This is a READ-ONLY aggregate surface, distinct from the existing
operational and audit views:

    Live       — what is happening right now
    Audit      — what happened in one specific decision
    Replay     — how the same artifact behaves under different policies
    Reporting  — how governance behaves over time     ← this module

All eight endpoints share the same shape:

    - HTTP GET only, no mutation
    - read existing tables only: ``trust_certificates`` and
      ``lifecycle_events``
    - default ``window="7d"``; accepted values: ``24h`` | ``7d`` | ``30d``
    - return 200 with empty / zeroed buckets when there is no data in
      the selected window (never 404)
    - parse ``content_json`` in Python for fields that are not on
      indexed columns (``s_base``, ``gate_results``,
      ``governance_rule_matches``, ``composer_metadata``). This is fine
      at demo scale (thousands of TCs); production scale would
      denormalize into dedicated columns.

This module makes NO schema changes, adds NO new tables, and
introduces NO new dependencies. The override-event parser is reused
verbatim from :mod:`tcs.api.routes_govern` so the override display is
identical across the Live, Audit, and Reporting surfaces.

Hard scope locks (from the Phase 5 Reporting MVP brief):

    - no LLM call from any endpoint here
    - no replay re-scoring
    - no policy mutation
    - no Trust Certificate or GovernanceEvaluation schema change
    - no bounded-control evaluator behavior

Eight panels:

    1. /reporting/decisions-over-time       — stacked area
    2. /reporting/decisions-by-policy       — table
    3. /reporting/override-rate-by-policy   — table
    4. /reporting/non-allow-trends          — line (Hold/Escalate/Stop)
    5. /reporting/top-rules                 — table
    6. /reporting/failed-back-dimensions    — bar (B/A/C/K)
    7. /reporting/score-averages            — two lines (S_base, TIS_current)
    8. /reporting/override-activity         — table
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter, HTTPException, Query, Request

# Re-use the hardened parser from routes_govern so override display is
# identical across surfaces. This is the only cross-module dependency
# in this file.
from tcs.api.routes_govern import _parse_override_reason
from tcs.persistence.certificate_store import tolerant_tc_from_json


router = APIRouter()


# --------------------------------------------------------------------------- #
# Window + bucket helpers                                                      #
# --------------------------------------------------------------------------- #

_VALID_WINDOWS = ("24h", "7d", "30d")
_VALID_BUCKETS = ("hour", "day")


def _window_to_cutoff(window: str) -> str:
    """
    Convert a window string to a UTC ISO-Z cutoff timestamp.

    Raises HTTP 400 on an unknown value so clients get a clear
    contract error rather than a silent empty result.
    """
    deltas = {
        "24h": timedelta(hours=24),
        "7d":  timedelta(days=7),
        "30d": timedelta(days=30),
    }
    if window not in deltas:
        raise HTTPException(
            status_code=400,
            detail=(
                f"unknown window {window!r}; expected one of: "
                f"{', '.join(_VALID_WINDOWS)}"
            ),
        )
    now = datetime.now(timezone.utc)
    return (now - deltas[window]).strftime("%Y-%m-%dT%H:%M:%SZ")


def _default_bucket_for(window: str) -> str:
    """
    Auto-pick the bucket granularity for a window:

        24h → hour
        7d  → day
        30d → day
    """
    return "hour" if window == "24h" else "day"


def _bucket_key(ts_iso: str, bucket: str) -> str:
    """
    Truncate an ISO-Z timestamp to the start of its bucket.

    Input shape: ``YYYY-MM-DDTHH:MM:SSZ``. Returns the same shape with
    minutes/seconds (and hour for day-bucket) zeroed. Done in Python
    rather than via SQLite ``strftime`` to keep behavior identical
    across SQLite versions.
    """
    if bucket == "hour":
        return ts_iso[:13] + ":00:00Z"
    return ts_iso[:10] + "T00:00:00Z"


def _validate_bucket(bucket: Optional[str], window: str) -> str:
    """
    Resolve and validate the bucket. ``None`` uses the auto-default
    for the window. Explicit bucket values are validated.
    """
    if bucket is None:
        return _default_bucket_for(window)
    if bucket not in _VALID_BUCKETS:
        raise HTTPException(
            status_code=400,
            detail=(
                f"unknown bucket {bucket!r}; expected one of: "
                f"{', '.join(_VALID_BUCKETS)}"
            ),
        )
    return bucket


# --------------------------------------------------------------------------- #
# Pack label lookup                                                            #
# --------------------------------------------------------------------------- #

def _pack_label_lookup() -> Dict[str, str]:
    """
    Build a map ``policy_set_id → pack name`` from the in-memory pack
    registry. Used to surface human-readable labels on by-policy panels.

    Returns an empty dict if the pack registry isn't importable for any
    reason — callers must tolerate missing labels and fall back to the
    raw ``policy_set_id`` (which is always available on every TC).
    """
    out: Dict[str, str] = {}
    try:
        from tcs.packs.pack_manager import list_packs
        for p in list_packs() or []:
            pid = (p.get("profile_config") or {}).get("profile_id")
            name = p.get("name")
            if pid and name:
                out[pid] = name
    except Exception:  # noqa: BLE001 — defensive; reporting must not error
        pass
    return out


# --------------------------------------------------------------------------- #
# Common data access                                                           #
# --------------------------------------------------------------------------- #

def _fetch_tc_rows_in_window(
    store: Any,
    cutoff_iso: str,
    *,
    columns: str = "evaluation_timestamp, decision, policy_set_id, "
                   "tis_current, content_json",
) -> List[Dict[str, Any]]:
    """
    Pull TC rows in the window. Caller selects which columns it needs
    via the ``columns`` argument; ``evaluation_timestamp`` is always
    included for ordering and bucketing.

    Rows are returned ordered ASC by ``evaluation_timestamp`` for
    deterministic aggregation.
    """
    rows = store._conn.execute(
        f"SELECT {columns} FROM trust_certificates "
        "WHERE evaluation_timestamp >= ? "
        "ORDER BY evaluation_timestamp ASC",
        (cutoff_iso,),
    ).fetchall()
    return rows


def _overridden_tc_ids_in_window(
    store: Any, cutoff_iso: str,
) -> Dict[str, str]:
    """
    Return ``{certificate_id: occurred_at}`` for every ``override_applied``
    event recorded since the cutoff. Multiple events on the same TC
    collapse to the most-recent ``occurred_at``.
    """
    rows = store._conn.execute(
        "SELECT certificate_id, occurred_at FROM lifecycle_events "
        "WHERE to_state = 'override_applied' "
        "AND occurred_at >= ? "
        "ORDER BY occurred_at DESC",
        (cutoff_iso,),
    ).fetchall()
    out: Dict[str, str] = {}
    for r in rows:
        cid = r["certificate_id"]
        if cid not in out:
            out[cid] = r["occurred_at"]
    return out


# --------------------------------------------------------------------------- #
# 1. /reporting/decisions-over-time                                            #
# --------------------------------------------------------------------------- #

@router.get("/reporting/decisions-over-time")
def decisions_over_time(
    request: Request,
    window: str = Query("7d"),
    bucket: Optional[str] = Query(None),
) -> Dict[str, Any]:
    """
    Decisions broken out by time bucket and decision outcome.

    Response shape::

        {
          "window":  "7d",
          "bucket":  "day",
          "cutoff":  "2026-05-13T00:00:00Z",
          "buckets": [
            {"t": "2026-05-13T00:00:00Z", "counts": {"Allow": 12, "Hold": 1}},
            ...
          ]
        }

    Counts include every decision label that appears in the window
    (Allow / Observe / Hold / Escalate / Stop and any Phase-3 refinements
    like ``Allow_with_logging``). Buckets with no activity are omitted —
    the frontend chart fills gaps as zero.
    """
    cutoff = _window_to_cutoff(window)
    resolved_bucket = _validate_bucket(bucket, window)
    rows = _fetch_tc_rows_in_window(
        request.app.state.store, cutoff,
        columns="evaluation_timestamp, decision",
    )

    buckets: Dict[str, Counter] = defaultdict(Counter)
    for r in rows:
        key = _bucket_key(r["evaluation_timestamp"], resolved_bucket)
        buckets[key][r["decision"]] += 1

    out = [
        {"t": k, "counts": dict(buckets[k])}
        for k in sorted(buckets)
    ]
    return {
        "window":  window,
        "bucket":  resolved_bucket,
        "cutoff":  cutoff,
        "buckets": out,
    }


# --------------------------------------------------------------------------- #
# 2. /reporting/decisions-by-policy                                            #
# --------------------------------------------------------------------------- #

@router.get("/reporting/decisions-by-policy")
def decisions_by_policy(
    request: Request,
    window: str = Query("7d"),
) -> Dict[str, Any]:
    """
    Decision counts grouped by policy profile / pack.

    Response shape::

        {
          "window": "7d",
          "rows": [
            {
              "policy_set_id": "composed-abc...",
              "pack_name":     "MedDev Composed",     # null when unknown
              "total":         42,
              "counts":        {"Allow": 30, "Hold": 8, "Stop": 4}
            },
            ...
          ]
        }

    Rows are ordered by ``total`` descending so the most-active pack
    is at the top. ``pack_name`` falls back to ``null`` when the pack
    registry doesn't have a name for the ``policy_set_id`` — the
    frontend then displays the id directly.
    """
    cutoff = _window_to_cutoff(window)
    rows = _fetch_tc_rows_in_window(
        request.app.state.store, cutoff,
        columns="policy_set_id, decision",
    )

    by_policy: Dict[str, Counter] = defaultdict(Counter)
    for r in rows:
        by_policy[r["policy_set_id"]][r["decision"]] += 1

    labels = _pack_label_lookup()
    out: List[Dict[str, Any]] = []
    for pid, counts in by_policy.items():
        out.append({
            "policy_set_id": pid,
            "pack_name":     labels.get(pid),
            "total":         sum(counts.values()),
            "counts":        dict(counts),
        })
    out.sort(key=lambda x: x["total"], reverse=True)
    return {"window": window, "rows": out}


# --------------------------------------------------------------------------- #
# 3. /reporting/override-rate-by-policy                                        #
# --------------------------------------------------------------------------- #

@router.get("/reporting/override-rate-by-policy")
def override_rate_by_policy(
    request: Request,
    window: str = Query("7d"),
) -> Dict[str, Any]:
    """
    Override rate per policy profile, in the selected window.

    Response shape::

        {
          "window": "7d",
          "rows": [
            {
              "policy_set_id": "composed-abc...",
              "pack_name":     "MedDev Composed",
              "total":         42,
              "overridden":    7,
              "rate":          0.1667
            },
            ...
          ]
        }

    Both numerator (overrides) and denominator (total TCs) are
    restricted to the window: a TC issued before the cutoff that gets
    overridden inside the window is NOT counted, because it isn't in
    the TC denominator. This keeps the rate interpretable as
    "of decisions made in this window, how many were overridden."

    Rows are ordered by total descending; rows with zero TCs in the
    window are omitted (an override-only row would have a denominator
    of zero and divide-by-zero would be meaningless).
    """
    cutoff = _window_to_cutoff(window)
    store = request.app.state.store
    rows = _fetch_tc_rows_in_window(
        store, cutoff,
        columns="certificate_id, policy_set_id",
    )

    # Pull TC ids per policy. Overrides keyed by tc_id are then crossed
    # against this set so we only count overrides whose TC is in-window.
    tcs_by_policy: Dict[str, set] = defaultdict(set)
    tc_to_policy: Dict[str, str] = {}
    for r in rows:
        cid = r["certificate_id"]
        pid = r["policy_set_id"]
        tcs_by_policy[pid].add(cid)
        tc_to_policy[cid] = pid

    overrides_in_window = _overridden_tc_ids_in_window(store, cutoff)
    overrides_per_policy: Counter = Counter()
    for cid in overrides_in_window:
        pid = tc_to_policy.get(cid)
        if pid is not None:
            overrides_per_policy[pid] += 1

    labels = _pack_label_lookup()
    out: List[Dict[str, Any]] = []
    for pid, tc_ids in tcs_by_policy.items():
        total = len(tc_ids)
        overridden = overrides_per_policy.get(pid, 0)
        rate = round(overridden / total, 4) if total else 0.0
        out.append({
            "policy_set_id": pid,
            "pack_name":     labels.get(pid),
            "total":         total,
            "overridden":    overridden,
            "rate":          rate,
        })
    out.sort(key=lambda x: x["total"], reverse=True)
    return {"window": window, "rows": out}


# --------------------------------------------------------------------------- #
# 4. /reporting/non-allow-trends                                               #
# --------------------------------------------------------------------------- #

#: Decisions that count as "non-allow" for the trends panel. We treat
#: ``Allow`` and any ``Allow_with_*`` refinement as the baseline; the
#: panel surfaces the explicitly held / escalated / stopped paths.
_NON_ALLOW_DECISIONS = ("Hold", "Escalate", "Stop")


@router.get("/reporting/non-allow-trends")
def non_allow_trends(
    request: Request,
    window: str = Query("7d"),
    bucket: Optional[str] = Query(None),
) -> Dict[str, Any]:
    """
    Time-bucketed counts of Hold, Escalate, and Stop decisions.

    Response shape::

        {
          "window":  "7d",
          "bucket":  "day",
          "cutoff":  "2026-05-13T00:00:00Z",
          "buckets": [
            {"t": "2026-05-13T00:00:00Z", "Hold": 1, "Escalate": 0, "Stop": 0},
            ...
          ]
        }

    Overlaps deliberately with /reporting/decisions-over-time but
    presents only the three non-allow lines so a reviewer can scan
    intervention pressure at a glance without filtering an
    everything-included chart.
    """
    cutoff = _window_to_cutoff(window)
    resolved_bucket = _validate_bucket(bucket, window)
    rows = _fetch_tc_rows_in_window(
        request.app.state.store, cutoff,
        columns="evaluation_timestamp, decision",
    )

    buckets: Dict[str, Counter] = defaultdict(Counter)
    for r in rows:
        if r["decision"] in _NON_ALLOW_DECISIONS:
            key = _bucket_key(r["evaluation_timestamp"], resolved_bucket)
            buckets[key][r["decision"]] += 1

    out = []
    for k in sorted(buckets):
        cnts = buckets[k]
        out.append({
            "t":        k,
            "Hold":     cnts.get("Hold", 0),
            "Escalate": cnts.get("Escalate", 0),
            "Stop":     cnts.get("Stop", 0),
        })
    return {
        "window":  window,
        "bucket":  resolved_bucket,
        "cutoff":  cutoff,
        "buckets": out,
    }


# --------------------------------------------------------------------------- #
# 5. /reporting/top-rules                                                      #
# --------------------------------------------------------------------------- #

@router.get("/reporting/top-rules")
def top_rules(
    request: Request,
    window: str = Query("7d"),
    limit: int = Query(10, ge=1, le=100),
) -> Dict[str, Any]:
    """
    Most-fired governance rules in the window, with their top
    associated decision and top associated policy.

    Response shape::

        {
          "window": "7d",
          "rules": [
            {
              "rule_id":             "lithium_pregnant_outbound",
              "rule_name":           "Lithium-to-pregnant outbound...",
              "fires":               7,
              "top_decision":        "Stop",
              "top_policy_set_id":   "composed-...",
              "top_pack_name":       "MedDev Composed"   # nullable
            },
            ...
          ]
        }

    Rule matches live inside each TC's ``content_json.governance_rule_matches``
    list, populated at TC generation. We load + parse rows in the window
    and aggregate in Python — adequate at demo scale, deliberately not a
    production analytics shape.
    """
    cutoff = _window_to_cutoff(window)
    rows = _fetch_tc_rows_in_window(
        request.app.state.store, cutoff,
        columns="content_json, decision, policy_set_id",
    )

    # rule_id → {"name": ..., "fires": int, "decisions": Counter, "policies": Counter}
    accum: Dict[str, Dict[str, Any]] = {}
    excluded_malformed = 0
    for r in rows:
        # Tolerant per-record boundary (D2): a stored row that fails
        # certificate validation must not silently contribute to
        # aggregates as though it were valid — exclude and count it.
        tc, _problem = tolerant_tc_from_json(r["content_json"])
        if tc is None:
            excluded_malformed += 1
            continue
        try:
            blob = json.loads(r["content_json"]) or {}
        except (TypeError, ValueError):
            continue
        matches = blob.get("governance_rule_matches") or []
        for m in matches:
            if not isinstance(m, dict):
                continue
            rid = m.get("rule_id")
            if not rid:
                continue
            slot = accum.setdefault(rid, {
                "rule_id":   rid,
                "rule_name": m.get("rule_name") or rid,
                "fires":     0,
                "decisions": Counter(),
                "policies":  Counter(),
            })
            slot["fires"] += 1
            slot["decisions"][r["decision"]] += 1
            slot["policies"][r["policy_set_id"]] += 1

    labels = _pack_label_lookup()
    flat: List[Dict[str, Any]] = []
    for slot in accum.values():
        top_decision = slot["decisions"].most_common(1)
        top_policy   = slot["policies"].most_common(1)
        flat.append({
            "rule_id":           slot["rule_id"],
            "rule_name":         slot["rule_name"],
            "fires":             slot["fires"],
            "top_decision":      top_decision[0][0] if top_decision else None,
            "top_policy_set_id": top_policy[0][0] if top_policy else None,
            "top_pack_name":     labels.get(top_policy[0][0]) if top_policy else None,
        })
    flat.sort(key=lambda x: x["fires"], reverse=True)
    return {
        "window": window,
        "rules": flat[:limit],
        "excluded_malformed_count": excluded_malformed,
    }


# --------------------------------------------------------------------------- #
# 6. /reporting/failed-back-dimensions                                         #
# --------------------------------------------------------------------------- #

#: Canonical dimension order. The panel always returns exactly these
#: four rows so the bar chart has a stable layout even when one or more
#: dimensions has zero failures.
_BACK_DIMS = ("B", "A", "C", "K")


@router.get("/reporting/failed-back-dimensions")
def failed_back_dimensions(
    request: Request,
    window: str = Query("7d"),
) -> Dict[str, Any]:
    """
    Per-dimension failure counts in the window.

    Response shape::

        {
          "window": "7d",
          "dims": [
            {"dim": "B", "fail_count": 0, "evaluated": 17},
            {"dim": "A", "fail_count": 3, "evaluated": 17},
            ...
          ]
        }

    A "fail" is a TC whose ``gate_results`` recorded ``"fail"`` for the
    dimension. ``not_applicable`` does NOT count toward either side of
    the ratio; ``evaluated`` is the number of TCs where the gate was
    actually run (``pass`` or ``fail``).

    Always returns all four BACK dimensions in canonical order — the
    panel exists to compare gates, and a missing row would create a
    visual gap.
    """
    cutoff = _window_to_cutoff(window)
    rows = _fetch_tc_rows_in_window(
        request.app.state.store, cutoff,
        columns="content_json",
    )

    fail = Counter()
    evaluated = Counter()
    excluded_malformed = 0
    for r in rows:
        # Tolerant per-record boundary (D2): rows failing certificate
        # validation are excluded from aggregates and counted.
        tc, _problem = tolerant_tc_from_json(r["content_json"])
        if tc is None:
            excluded_malformed += 1
            continue
        try:
            blob = json.loads(r["content_json"]) or {}
        except (TypeError, ValueError):
            continue
        gate_results = blob.get("gate_results") or {}
        for dim in _BACK_DIMS:
            res = gate_results.get(dim)
            if res in ("pass", "fail"):
                evaluated[dim] += 1
                if res == "fail":
                    fail[dim] += 1

    dims = [
        {
            "dim":        dim,
            "fail_count": fail.get(dim, 0),
            "evaluated":  evaluated.get(dim, 0),
        }
        for dim in _BACK_DIMS
    ]
    return {
        "window": window,
        "dims": dims,
        "excluded_malformed_count": excluded_malformed,
    }


# --------------------------------------------------------------------------- #
# 7. /reporting/score-averages                                                 #
# --------------------------------------------------------------------------- #

@router.get("/reporting/score-averages")
def score_averages(
    request: Request,
    window: str = Query("7d"),
    bucket: Optional[str] = Query(None),
) -> Dict[str, Any]:
    """
    Bucketed average of ``s_base`` and ``tis_current``.

    Response shape::

        {
          "window":  "7d",
          "bucket":  "day",
          "cutoff":  "...",
          "buckets": [
            {"t": "...", "avg_s_base": 0.8612, "avg_tis_current": 0.7951, "n": 12},
            ...
          ]
        }

    ``tis_current`` is a top-level TC column; ``s_base`` lives inside
    ``content_json`` so we parse it. Buckets with zero TCs are omitted
    (no defensible average to report).
    """
    cutoff = _window_to_cutoff(window)
    resolved_bucket = _validate_bucket(bucket, window)
    rows = _fetch_tc_rows_in_window(
        request.app.state.store, cutoff,
        columns="evaluation_timestamp, tis_current, content_json",
    )

    sums_s: Dict[str, float] = defaultdict(float)
    sums_t: Dict[str, float] = defaultdict(float)
    counts: Counter = Counter()

    for r in rows:
        key = _bucket_key(r["evaluation_timestamp"], resolved_bucket)
        try:
            blob = json.loads(r["content_json"]) or {}
        except (TypeError, ValueError):
            blob = {}
        s_base = float(blob.get("s_base") or 0.0)
        tis_current = float(r["tis_current"] or 0.0)
        sums_s[key] += s_base
        sums_t[key] += tis_current
        counts[key] += 1

    out = []
    for k in sorted(counts):
        n = counts[k]
        out.append({
            "t":               k,
            "avg_s_base":      round(sums_s[k] / n, 4) if n else 0.0,
            "avg_tis_current": round(sums_t[k] / n, 4) if n else 0.0,
            "n":               n,
        })
    return {
        "window":  window,
        "bucket":  resolved_bucket,
        "cutoff":  cutoff,
        "buckets": out,
    }


# --------------------------------------------------------------------------- #
# 8. /reporting/override-activity                                              #
# --------------------------------------------------------------------------- #

@router.get("/reporting/override-activity")
def override_activity(
    request: Request,
    window: str = Query("7d"),
    limit: int = Query(50, ge=1, le=500),
) -> Dict[str, Any]:
    """
    Recent override events with full audit detail.

    Response shape::

        {
          "window": "7d",
          "count":  3,
          "events": [
            {
              "certificate_id":    "...",
              "original_decision": "Hold",
              "policy_set_id":     "...",
              "pack_name":         "...",         # null when unknown
              "override_decision": "Allow",
              "override_actor":    "...",
              "override_at":       "...",
              "override_reason_text": "...",
              "raw_reason":        "...",
            },
            ...
          ]
        }

    Reuses ``_parse_override_reason`` from :mod:`tcs.api.routes_govern`
    so the parsed shape matches what the Live view's override badge
    consumes. Events are returned newest-first up to ``limit``.

    Events whose underlying TC is unknown (e.g. the TC was issued
    before the window's TC backfill horizon and we cannot resolve its
    policy / original decision) are still returned, with
    ``original_decision`` and ``policy_set_id`` left null. That keeps
    the audit trail visible even when context is partial.
    """
    cutoff = _window_to_cutoff(window)
    store = request.app.state.store

    ev_rows = store._conn.execute(
        "SELECT certificate_id, reason, occurred_at FROM lifecycle_events "
        "WHERE to_state = 'override_applied' AND occurred_at >= ? "
        "ORDER BY occurred_at DESC LIMIT ?",
        (cutoff, int(limit)),
    ).fetchall()

    if not ev_rows:
        return {"window": window, "count": 0, "events": []}

    # Bulk-fetch TC context for the certificate_ids referenced by the
    # events. One query, regardless of event count.
    tc_ids = list({r["certificate_id"] for r in ev_rows})
    placeholders = ",".join("?" * len(tc_ids))
    tc_rows = store._conn.execute(
        f"SELECT certificate_id, decision, policy_set_id "
        f"FROM trust_certificates "
        f"WHERE certificate_id IN ({placeholders})",
        tc_ids,
    ).fetchall()
    tc_context: Dict[str, Dict[str, Any]] = {
        r["certificate_id"]: {
            "original_decision": r["decision"],
            "policy_set_id":     r["policy_set_id"],
        }
        for r in tc_rows
    }

    labels = _pack_label_lookup()
    events: List[Dict[str, Any]] = []
    for r in ev_rows:
        cid = r["certificate_id"]
        parsed = _parse_override_reason(r["reason"] or "", r["occurred_at"])
        ctx = tc_context.get(cid, {})
        pid = ctx.get("policy_set_id")
        events.append({
            "certificate_id":      cid,
            "original_decision":   ctx.get("original_decision"),
            "policy_set_id":       pid,
            "pack_name":           labels.get(pid) if pid else None,
            "override_decision":   parsed["override_decision"],
            "override_actor":      parsed["override_actor"],
            "override_at":         parsed["override_at"],
            "override_reason_text": parsed["override_reason_text"],
            "raw_reason":          parsed["raw_reason"],
        })
    return {"window": window, "count": len(events), "events": events}


__all__ = ["router"]
