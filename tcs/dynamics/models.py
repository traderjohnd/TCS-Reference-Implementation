"""
tcs.dynamics.models
===================

Dataclasses for the adaptive governance modules: Trust Loss Function
results, Drift signals, and Adaptation recommendations.

These are the data contracts between the dynamics modules and the API
layer. Every module returns one of these; every API endpoint serializes
one of these.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional


@dataclass
class TrustLossResult:
    """
    Result of a Trust Loss Function computation over a sliding window.

    L_t = alpha*U + beta*P + gamma*D + delta*E + epsilon*G

    Each component is in [0, 1]. L_t is the weighted sum and represents
    governance effectiveness degradation — higher means worse.
    """
    L_t: float                         # aggregate trust loss
    components: Dict[str, float]       # {U, P, D, E, G} each in [0,1]
    weights: Dict[str, float]          # {alpha, beta, gamma, delta, epsilon}
    dominant_component: str            # key with highest weighted contribution
    window_hours: float
    window_evaluations: int
    domain: str
    computed_at: datetime

    def to_dict(self) -> Dict[str, Any]:
        return {
            "L_t": round(self.L_t, 4),
            "components": {k: round(v, 4) for k, v in self.components.items()},
            "weights": {k: round(v, 4) for k, v in self.weights.items()},
            "dominant_component": self.dominant_component,
            "window_hours": self.window_hours,
            "window_evaluations": self.window_evaluations,
            "domain": self.domain,
            "computed_at": self.computed_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
        }


@dataclass
class DriftSignal:
    """
    Drift measurement for a single governance context (risk_tier,
    action_class, connection_type).
    """
    context: str                       # e.g. "fin-r3-a4-ct4"
    D_trust: float                     # aggregate drift metric
    components: Dict[str, float]       # {level, variance, failure}
    threshold_breached: Optional[str]  # D_warn | D_alert | D_crit | None
    trend: str                         # increasing | stable | decreasing
    recommendation: Optional[str]      # pll_review | recovery_activate | None
    window_hours: float
    window_evaluations: int
    computed_at: datetime

    def to_dict(self) -> Dict[str, Any]:
        return {
            "context": self.context,
            "D_trust": round(self.D_trust, 4),
            "components": {k: round(v, 4) for k, v in self.components.items()},
            "threshold_breached": self.threshold_breached,
            "trend": self.trend,
            "recommendation": self.recommendation,
            "window_hours": self.window_hours,
            "window_evaluations": self.window_evaluations,
            "computed_at": self.computed_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
        }


@dataclass
class AdaptationRecommendation:
    """
    A recommended Risk Tolerance Profile parameter change generated
    by the Policy Learning Layer.
    """
    record_id: str
    triggered_by: str                  # drift_alert | manual
    risk_tolerance_profile_id: str
    parameter_changes: Dict[str, Dict[str, float]]  # {param: {before, after, delta}}
    evidence: Dict[str, Any]           # {D_trust, L_trust, window_evaluations}
    approval_status: str               # pending | approved | rejected
    approver_identity: Optional[str] = None
    approval_timestamp: Optional[datetime] = None
    applied_at: Optional[datetime] = None
