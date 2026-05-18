"""
tcs.api.routes_metrics
======================

GET /v2/metrics/live  — runtime governance telemetry
GET /v2/health        — liveness and integrity check

The metrics endpoint reads aggregate stats directly from the store:
TIS distribution, gate failure rate, governance integrity score,
decision counts, chain count. Phase 2 computes all of these on every
request — the store is SQLite and query cost is negligible for the
demo workload. Phase 3 caches them behind a background refresher.

The health endpoint returns a minimal liveness signal plus three
fields CLAUDE.md Step 5 calls out:
    * status
    * policy_version
    * chain_intact

``chain_intact`` is True iff every chain in the archive verifies
(via :meth:`CertificateStore.all_chains_verify`).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict

import re

from fastapi import APIRouter, Query, Request


router = APIRouter()


# --------------------------------------------------------------------------- #
# Shared helpers                                                               #
# --------------------------------------------------------------------------- #

_WINDOW_RE = re.compile(r"^(\d+(?:\.\d+)?)(h|m)$")


def _parse_window(s: str) -> float:
    """
    Parse a human-friendly window string into hours.

    Examples: "1h" -> 1.0, "24h" -> 24.0, "30m" -> 0.5
    """
    m = _WINDOW_RE.match(s.strip().lower())
    if not m:
        raise ValueError(f"Invalid window format: {s!r}; expected e.g. '1h' or '30m'")
    value = float(m.group(1))
    unit = m.group(2)
    if unit == "h":
        return value
    return value / 60.0


# --------------------------------------------------------------------------- #
# /v2/metrics/live                                                             #
# --------------------------------------------------------------------------- #

@router.get("/metrics/live")
def get_metrics_live(request: Request) -> Dict[str, Any]:
    """
    Return live governance telemetry for dashboards.

    Schema:

        {
            "total_certificates":         int,
            "chain_count":                int,
            "decision_counts":            {"Allow": int, "Hold": int, ...},
            "tis_distribution":           {
                "count": int, "mean": float, "min": float, "max": float,
                "histogram": {
                    "stop_zone": int,
                    "review_zone": int,
                    "allow_zone": int,
                    "invalidated": int,
                }
            },
            "gate_failure_rate":          float in [0, 1],
            "governance_integrity_score": float in [0, 1],
            "snapshot_at":                ISO-8601 UTC
        }
    """
    store = request.app.state.store
    return {
        "total_evaluations": store.count(),
        "total_certificates": store.count(),
        "chain_count": len(store.list_chain_ids()),
        "decisions": store.decision_counts(),
        "decision_counts": store.decision_counts(),
        "tis_distribution": store.tis_distribution(),
        "gate_failure_rate": store.gate_failure_rate(),
        "governance_integrity_score": store.governance_integrity_score(),
        "chain_intact": store.all_chains_verify(),
        "dimension_means": store.dimension_means(),
        "dominant_failure_dimension": store.dominant_failure_dimension(),
        "snapshot_at": datetime.now(timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        ),
    }


# --------------------------------------------------------------------------- #
# /v2/health                                                                   #
# --------------------------------------------------------------------------- #

# --------------------------------------------------------------------------- #
# /v2/metrics/summary — aggregate metrics for Executive View                   #
# --------------------------------------------------------------------------- #

@router.get("/metrics/summary")
def get_metrics_summary(request: Request) -> Dict[str, Any]:
    """
    Aggregate metrics for the Executive / Economic View.

    Includes automation rate, review counts, stop counts,
    and economic indicators derived from the TC archive.
    """
    store = request.app.state.store
    counts = store.decision_counts()
    total = store.count()

    allow_count = counts.get("Allow", 0) + counts.get("Observe", 0)
    hold_count = counts.get("Hold", 0)
    escalate_count = counts.get("Escalate", 0)
    stop_count = counts.get("Stop", 0)

    automation_rate = allow_count / total if total > 0 else 0.0
    review_count = hold_count + escalate_count

    dist = store.tis_distribution()

    return {
        "total_evaluations": total,
        "automation_rate": round(automation_rate, 4),
        "review_count": review_count,
        "stop_count": stop_count,
        "allow_count": allow_count,
        "hold_queue_depth": hold_count,
        "escalate_count": escalate_count,
        "mean_tis": round(dist.get("mean", 0.0), 4),
        "gate_failure_rate": store.gate_failure_rate(),
        "governance_integrity_score": store.governance_integrity_score(),
        "decision_counts": counts,
        "snapshot_at": datetime.now(timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        ),
    }


# --------------------------------------------------------------------------- #
# /v2/metrics/timeseries                                                       #
# --------------------------------------------------------------------------- #

@router.get("/metrics/timeseries")
def get_timeseries(
    request: Request,
    window: str = Query("1h"),
    bucket: str = Query("1m"),
) -> Dict[str, Any]:
    """
    Time-bucketed decision counts and mean TIS for dashboard
    timeseries charts.
    """
    store = request.app.state.store
    window_hours = _parse_window(window)
    bucket_minutes = _parse_window(bucket) * 60.0  # _parse_window returns hours
    return {"buckets": store.timeseries_buckets(window_hours, bucket_minutes)}


# --------------------------------------------------------------------------- #
# /v2/metrics/gate-failures                                                    #
# --------------------------------------------------------------------------- #

@router.get("/metrics/gate-failures")
def get_gate_failures(
    request: Request,
    window: str = Query("24h"),
) -> Dict[str, Any]:
    """Gate failure breakdown by dimension and profile."""
    store = request.app.state.store
    window_hours = _parse_window(window)
    return store.gate_failure_details(window_hours)


# --------------------------------------------------------------------------- #
# /v2/metrics/attribution-gaps                                                 #
# --------------------------------------------------------------------------- #

@router.get("/metrics/attribution-gaps")
def get_attribution_gaps(
    request: Request,
    window: str = Query("24h"),
) -> Dict[str, Any]:
    """Attribution gap metrics and trend."""
    store = request.app.state.store
    window_hours = _parse_window(window)
    return store.attribution_gap_details(window_hours)


# --------------------------------------------------------------------------- #
# /v2/health                                                                   #
# --------------------------------------------------------------------------- #

# --------------------------------------------------------------------------- #
# /v2/metrics/telemetry                                                        #
# --------------------------------------------------------------------------- #

@router.get("/metrics/telemetry")
def get_telemetry(
    request: Request,
    window: str = Query("1h"),
    limit: int = Query(100),
) -> Dict[str, Any]:
    """
    Per-evaluation telemetry stream for real-time charts.

    Returns individual TC data points with dimension scores, TIS values,
    penalty breakdowns, and K calibration signals for the Telemetry view.
    """
    store = request.app.state.store
    window_hours = _parse_window(window)
    records = store.telemetry_stream(window_hours, limit)

    # Compute summary statistics for the window
    if records:
        k_scores = [r["K"] for r in records]
        tis_scores = [r["tis_current"] for r in records]
        penalties = [r["penalty_aggregate"] for r in records]
        gate_fails = sum(1 for r in records if not r["gate_passed"])

        k_mean = round(sum(k_scores) / len(k_scores), 4)
        k_min = round(min(k_scores), 4)
        k_max = round(max(k_scores), 4)
        # K calibration band: scores within 0.1 of mean
        k_calibrated = sum(1 for k in k_scores if abs(k - k_mean) < 0.10)

        summary = {
            "count": len(records),
            "k_calibration": {
                "mean": k_mean,
                "min": k_min,
                "max": k_max,
                "calibrated_pct": round(k_calibrated / len(k_scores), 4),
                "below_threshold": sum(1 for k in k_scores if k < 0.80),
            },
            "tis_summary": {
                "mean": round(sum(tis_scores) / len(tis_scores), 4),
                "min": round(min(tis_scores), 4),
                "max": round(max(tis_scores), 4),
            },
            "penalty_pressure": {
                "mean": round(sum(penalties) / len(penalties), 4),
                "max": round(max(penalties), 4),
            },
            "gate_failure_count": gate_fails,
            "gate_failure_rate": round(gate_fails / len(records), 4),
        }
    else:
        summary = {
            "count": 0,
            "k_calibration": {"mean": 0, "min": 0, "max": 0, "calibrated_pct": 0, "below_threshold": 0},
            "tis_summary": {"mean": 0, "min": 0, "max": 0},
            "penalty_pressure": {"mean": 0, "max": 0},
            "gate_failure_count": 0,
            "gate_failure_rate": 0,
        }

    return {
        "records": records,
        "summary": summary,
        "window": window,
        "snapshot_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


@router.get("/health")
def get_health(request: Request) -> Dict[str, Any]:
    """
    Liveness + integrity check.

    Returns:

        {
            "status":         "ok" | "degraded",
            "api_version":    str,
            "policy_version": str,
            "chain_intact":   bool,
            "tc_count":       int,
            "chain_count":    int,
            "uptime_seconds": float,
            "checked_at":     ISO-8601 UTC
        }

    ``status`` is "ok" iff ``chain_intact`` is True. A failing chain
    flips the service into "degraded" so upstream load balancers can
    drain traffic.
    """
    store = request.app.state.store
    chain_intact = store.all_chains_verify()
    start = getattr(
        request.app.state, "start_time", datetime.now(timezone.utc)
    )
    uptime = (datetime.now(timezone.utc) - start).total_seconds()
    return {
        "status": "ok" if chain_intact else "degraded",
        "api_version": getattr(request.app.state, "api_version", "unknown"),
        "policy_version": getattr(
            request.app.state, "policy_version", "unknown"
        ),
        "chain_intact": chain_intact,
        "tc_count": store.count(),
        "chain_count": len(store.list_chain_ids()),
        "uptime_seconds": round(uptime, 3),
        "checked_at": datetime.now(timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        ),
    }
