"""
tcs.simulation.historical_replay
=================================

Replays historical TC data against a proposed Risk Tolerance Profile
to predict decision changes before production deployment.

Process:
    1. Load TC records from store matching query filters
    2. For each TC, extract original dimension scores and context
    3. Re-run TIS computation with the proposed profile
    4. Compare new decision to original decision
    5. Aggregate flipped decisions and impact metrics
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from tcs.decision_engine import map_decision
from tcs.policy_profiles import PolicyProfile
from tcs.persistence import CertificateStore
from tcs.tis_engine import TISInput, compute_tis


# --------------------------------------------------------------------------- #
# Result types                                                                 #
# --------------------------------------------------------------------------- #

@dataclass
class FlippedDecision:
    """A single decision that changed under the proposed profile."""
    certificate_id: str
    before: str
    after: str
    tis_before: float
    tis_after: float
    reason: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "certificate_id": self.certificate_id,
            "before": self.before,
            "after": self.after,
            "tis_before": round(self.tis_before, 4),
            "tis_after": round(self.tis_after, 4),
            "reason": self.reason,
        }


@dataclass
class SimulationResult:
    """Complete result of a historical replay simulation."""
    simulation_id: str
    total_replayed: int
    decision_distribution_before: Dict[str, int]
    decision_distribution_after: Dict[str, int]
    flipped_decisions: List[FlippedDecision]
    automation_rate_before: float
    automation_rate_after: float
    automation_rate_change: float
    gate_failure_rate_before: float
    gate_failure_rate_after: float
    gate_failure_rate_change: float
    proposed_profile_id: str
    computed_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "simulation_id": self.simulation_id,
            "total_replayed": self.total_replayed,
            "decision_distribution_before": self.decision_distribution_before,
            "decision_distribution_after": self.decision_distribution_after,
            "flipped_decisions": [f.to_dict() for f in self.flipped_decisions],
            "automation_rate_before": round(self.automation_rate_before, 4),
            "automation_rate_after": round(self.automation_rate_after, 4),
            "automation_rate_change": round(self.automation_rate_change, 4),
            "gate_failure_rate_before": round(self.gate_failure_rate_before, 4),
            "gate_failure_rate_after": round(self.gate_failure_rate_after, 4),
            "gate_failure_rate_change": round(self.gate_failure_rate_change, 4),
            "proposed_profile_id": self.proposed_profile_id,
            "computed_at": self.computed_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
        }


# --------------------------------------------------------------------------- #
# Replay engine                                                                #
# --------------------------------------------------------------------------- #

def _extract_replay_input(
    tc_data: Dict[str, Any],
    proposed_profile: PolicyProfile,
) -> Optional[TISInput]:
    """
    Extract a TISInput from stored TC content_json for replay.

    Returns None if the TC doesn't have enough data to replay.
    """
    scores = tc_data.get("component_scores", {})
    if not all(k in scores for k in ("B", "A", "C", "K")):
        return None

    # Build context_metadata with safe defaults for penalty computation
    context = {
        "n_gaps": tc_data.get("integration_boundary_gaps", 0),
        "context_age_hours": 0.0,
        "novelty_score": tc_data.get("novelty_score", 0.0),
        "days_since_review": tc_data.get("days_since_review", 0),
        "is_policy_sensitive": tc_data.get("is_policy_sensitive", False),
    }

    return TISInput(
        subject_id=tc_data.get("certificate_id", "replay"),
        subject_type="replay",
        policy_profile=proposed_profile,
        dimension_scores={k: float(v) for k, v in scores.items()},
        context_metadata=context,
        elapsed_hours=0.0,
    )


def _automation_rate(dist: Dict[str, int], total: int) -> float:
    """Fraction of decisions that are Allow or Observe (automated)."""
    if total == 0:
        return 0.0
    auto = dist.get("Allow", 0) + dist.get("Observe", 0)
    return auto / total


def _gate_failure_rate(dist: Dict[str, int], total: int) -> float:
    """Fraction of decisions that are gate failures (not Allow/Observe)."""
    if total == 0:
        return 0.0
    failures = sum(v for k, v in dist.items() if k not in ("Allow", "Observe"))
    return failures / total


def replay(
    store: CertificateStore,
    proposed_profile: PolicyProfile,
    *,
    window_hours: float = 168.0,  # default 7 days
    max_records: int = 1000,
) -> SimulationResult:
    """
    Replay historical TC data against a proposed profile.

    Parameters
    ----------
    store
        CertificateStore to read historical TCs from.
    proposed_profile
        The candidate Risk Tolerance Profile to test.
    window_hours
        How far back to look for TCs to replay.
    max_records
        Maximum number of TCs to replay (performance guard).

    Returns
    -------
    SimulationResult
        Complete comparison of original vs. proposed decisions.
    """
    tc_rows = store.query_window(window_hours)
    if len(tc_rows) > max_records:
        tc_rows = tc_rows[-max_records:]  # most recent

    dist_before: Dict[str, int] = {}
    dist_after: Dict[str, int] = {}
    flipped: List[FlippedDecision] = []

    for row in tc_rows:
        original_decision = str(row["decision"])
        original_tis = float(row["tis_current"])
        dist_before[original_decision] = dist_before.get(original_decision, 0) + 1

        # Parse content_json to get dimension scores
        try:
            tc_data = json.loads(row["content_json"])
        except (json.JSONDecodeError, KeyError):
            dist_after[original_decision] = dist_after.get(original_decision, 0) + 1
            continue

        # Build replay input
        replay_input = _extract_replay_input(tc_data, proposed_profile)
        if replay_input is None:
            dist_after[original_decision] = dist_after.get(original_decision, 0) + 1
            continue

        # Re-run TIS computation
        tis_result = compute_tis(replay_input)
        new_decision, _ = map_decision(replay_input, tis_result)
        new_tis = tis_result.tis_current

        dist_after[new_decision] = dist_after.get(new_decision, 0) + 1

        if new_decision != original_decision:
            cert_id = tc_data.get("certificate_id", "unknown")
            reason = _explain_flip(original_decision, new_decision, original_tis, new_tis)
            flipped.append(FlippedDecision(
                certificate_id=cert_id,
                before=original_decision,
                after=new_decision,
                tis_before=original_tis,
                tis_after=new_tis,
                reason=reason,
            ))

    total = len(tc_rows)
    auto_before = _automation_rate(dist_before, total)
    auto_after = _automation_rate(dist_after, total)
    gf_before = _gate_failure_rate(dist_before, total)
    gf_after = _gate_failure_rate(dist_after, total)

    return SimulationResult(
        simulation_id=f"SIM-{uuid.uuid4().hex[:8]}",
        total_replayed=total,
        decision_distribution_before=dist_before,
        decision_distribution_after=dist_after,
        flipped_decisions=flipped,
        automation_rate_before=auto_before,
        automation_rate_after=auto_after,
        automation_rate_change=auto_after - auto_before,
        gate_failure_rate_before=gf_before,
        gate_failure_rate_after=gf_after,
        gate_failure_rate_change=gf_after - gf_before,
        proposed_profile_id=proposed_profile.profile_id,
    )


def _explain_flip(before: str, after: str, tis_before: float, tis_after: float) -> str:
    """Generate a human-readable reason for a decision flip."""
    if before == "Allow" and after in ("Hold", "Escalate", "Stop"):
        return f"Tightened thresholds: TIS {tis_before:.3f}->{tis_after:.3f} now below new threshold"
    if before in ("Hold", "Escalate", "Stop") and after == "Allow":
        return f"Loosened thresholds: TIS {tis_before:.3f}->{tis_after:.3f} now above new threshold"
    return f"Decision changed: {before}->{after}, TIS {tis_before:.3f}->{tis_after:.3f}"
