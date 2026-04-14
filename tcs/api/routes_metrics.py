"""
tcs.api.routes_metrics
======================

GET /v1/metrics/live  — runtime governance telemetry
GET /v1/health        — liveness and integrity check

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

from fastapi import APIRouter, Request


router = APIRouter()


# --------------------------------------------------------------------------- #
# /v1/metrics/live                                                             #
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
# /v1/health                                                                   #
# --------------------------------------------------------------------------- #

# --------------------------------------------------------------------------- #
# /v1/metrics/summary — aggregate metrics for Executive View                   #
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
