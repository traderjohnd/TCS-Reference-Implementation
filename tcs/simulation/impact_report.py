"""
tcs.simulation.impact_report
=============================

Generates governance impact reports from simulation results.

An impact report summarizes the business and governance implications
of deploying a proposed Risk Tolerance Profile change.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List

from tcs.simulation.historical_replay import SimulationResult


@dataclass
class ImpactReport:
    """Governance impact report for a proposed profile change."""
    simulation_id: str
    proposed_profile_id: str
    total_replayed: int
    flipped_count: int
    automation_rate_change: float
    gate_failure_rate_change: float
    risk_assessment: str         # "low" | "medium" | "high"
    risk_factors: List[str]
    recommendation: str          # "proceed" | "review" | "reject"
    computed_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "simulation_id": self.simulation_id,
            "proposed_profile_id": self.proposed_profile_id,
            "total_replayed": self.total_replayed,
            "flipped_count": self.flipped_count,
            "automation_rate_change": round(self.automation_rate_change, 4),
            "gate_failure_rate_change": round(self.gate_failure_rate_change, 4),
            "risk_assessment": self.risk_assessment,
            "risk_factors": self.risk_factors,
            "recommendation": self.recommendation,
            "computed_at": self.computed_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
        }


def generate_impact_report(result: SimulationResult) -> ImpactReport:
    """
    Generate an impact report from a simulation result.

    Risk assessment logic:
        - HIGH: >10% of decisions flip OR gate failures increase >5%
        - MEDIUM: >5% flip OR gate failures increase >2%
        - LOW: otherwise
    """
    flip_pct = (
        len(result.flipped_decisions) / result.total_replayed
        if result.total_replayed > 0 else 0.0
    )
    gf_change = result.gate_failure_rate_change

    risk_factors: List[str] = []

    # Assess risk
    if flip_pct > 0.10 or gf_change > 0.05:
        risk = "high"
    elif flip_pct > 0.05 or gf_change > 0.02:
        risk = "medium"
    else:
        risk = "low"

    # Identify risk factors
    if flip_pct > 0.10:
        risk_factors.append(f"{flip_pct:.0%} of decisions would flip (>10% threshold)")
    elif flip_pct > 0.05:
        risk_factors.append(f"{flip_pct:.0%} of decisions would flip (>5% threshold)")

    if gf_change > 0.05:
        risk_factors.append(f"Gate failure rate increases by {gf_change:.1%} (>5% threshold)")
    elif gf_change > 0.02:
        risk_factors.append(f"Gate failure rate increases by {gf_change:.1%} (>2% threshold)")

    if result.automation_rate_change < -0.05:
        risk_factors.append(f"Automation rate decreases by {abs(result.automation_rate_change):.1%}")

    # Count flips from Allow to Stop (most severe)
    allow_to_stop = sum(
        1 for f in result.flipped_decisions
        if f.before == "Allow" and f.after == "Stop"
    )
    if allow_to_stop > 0:
        risk_factors.append(f"{allow_to_stop} Allow->Stop flips detected")

    if not risk_factors:
        risk_factors.append("No significant risk factors identified")

    # Recommendation
    if risk == "high":
        recommendation = "reject"
    elif risk == "medium":
        recommendation = "review"
    else:
        recommendation = "proceed"

    return ImpactReport(
        simulation_id=result.simulation_id,
        proposed_profile_id=result.proposed_profile_id,
        total_replayed=result.total_replayed,
        flipped_count=len(result.flipped_decisions),
        automation_rate_change=result.automation_rate_change,
        gate_failure_rate_change=result.gate_failure_rate_change,
        risk_assessment=risk,
        risk_factors=risk_factors,
        recommendation=recommendation,
    )
