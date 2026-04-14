"""
tcs.simulation.ab_comparison
=============================

Compare two Risk Tolerance Profiles simultaneously against historical
TC data to determine which produces better governance outcomes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict

from tcs.policy_profiles import PolicyProfile
from tcs.persistence import CertificateStore
from tcs.simulation.historical_replay import SimulationResult, replay


@dataclass
class ABComparisonResult:
    """Result of comparing two profiles against the same historical data."""
    profile_a_id: str
    profile_b_id: str
    result_a: SimulationResult
    result_b: SimulationResult
    recommendation: str  # "profile_a" | "profile_b" | "equivalent"
    reasoning: str
    computed_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "profile_a_id": self.profile_a_id,
            "profile_b_id": self.profile_b_id,
            "result_a": self.result_a.to_dict(),
            "result_b": self.result_b.to_dict(),
            "recommendation": self.recommendation,
            "reasoning": self.reasoning,
            "computed_at": self.computed_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
        }


def compare_profiles(
    store: CertificateStore,
    profile_a: PolicyProfile,
    profile_b: PolicyProfile,
    *,
    window_hours: float = 168.0,
    max_records: int = 1000,
) -> ABComparisonResult:
    """
    Run both profiles against the same historical data and recommend
    which one produces better governance outcomes.

    "Better" is defined as: higher automation rate with equal or lower
    gate failure rate. If one profile has fewer gate failures but lower
    automation, it is preferred (safety over throughput).
    """
    result_a = replay(store, profile_a, window_hours=window_hours, max_records=max_records)
    result_b = replay(store, profile_b, window_hours=window_hours, max_records=max_records)

    # Decision logic — use strict inequality for "better"
    a_strictly_better_safety = result_a.gate_failure_rate_after < result_b.gate_failure_rate_after
    b_strictly_better_safety = result_b.gate_failure_rate_after < result_a.gate_failure_rate_after
    a_strictly_better_auto = result_a.automation_rate_after > result_b.automation_rate_after
    b_strictly_better_auto = result_b.automation_rate_after > result_a.automation_rate_after

    if not a_strictly_better_safety and not b_strictly_better_safety and \
       not a_strictly_better_auto and not b_strictly_better_auto:
        recommendation = "equivalent"
        reasoning = "Both profiles produce equivalent governance outcomes"
    elif a_strictly_better_safety and (a_strictly_better_auto or not b_strictly_better_auto):
        recommendation = "profile_a"
        reasoning = f"{profile_a.profile_id} preferred for safety (lower gate failure rate)"
    elif b_strictly_better_safety and (b_strictly_better_auto or not a_strictly_better_auto):
        recommendation = "profile_b"
        reasoning = f"{profile_b.profile_id} preferred for safety (lower gate failure rate)"
    elif a_strictly_better_auto:
        recommendation = "profile_a"
        reasoning = f"{profile_a.profile_id} has better automation rate with equal safety"
    elif b_strictly_better_auto:
        recommendation = "profile_b"
        reasoning = f"{profile_b.profile_id} has better automation rate with equal safety"
    else:
        recommendation = "equivalent"
        reasoning = "Both profiles produce equivalent governance outcomes"

    return ABComparisonResult(
        profile_a_id=profile_a.profile_id,
        profile_b_id=profile_b.profile_id,
        result_a=result_a,
        result_b=result_b,
        recommendation=recommendation,
        reasoning=reasoning,
    )
