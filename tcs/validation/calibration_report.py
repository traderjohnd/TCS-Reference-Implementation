"""
tcs.validation.calibration_report
==================================

Analyze Trust Certificates for calibration signals.

The :class:`CalibrationReport` consumes TCs from the certificate store
-- it does not modify them. It answers seven validation questions about
whether the governance system is properly calibrated for the deployed
workflow:

    Q1: Are the BACK dimension weights producing meaningful differentiation?
    Q2: Are CT-4 penalties firing at the right frequency?
    Q3: Is the decision distribution healthy (not rubber-stamping or blocking everything)?
    Q4: Are gate failures concentrated in one dimension?
    Q5: Are kappa and theta thresholds reachable and meaningful?
    Q6: Is temporal decay relevant to the workflow timing?
    Q7: Are drift signals and trust-loss components balanced?

Usage::

    from tcs.persistence import CertificateStore
    from tcs.validation import CalibrationReport

    store = CertificateStore("data/tcs.db")
    report = CalibrationReport(store)
    result = report.generate(chain_id="demo-chain")
    print(result.summary())
"""

from __future__ import annotations

import json
import math
import statistics
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from tcs.persistence.certificate_store import CertificateStore


# --------------------------------------------------------------------------- #
# CalibrationResult                                                            #
# --------------------------------------------------------------------------- #

@dataclass
class CalibrationResult:
    """
    Complete calibration analysis result.

    All fields are populated by :meth:`CalibrationReport.generate`.
    """

    # Weight Behavior (Q1)
    dimension_score_distribution: Dict[str, Dict[str, float]] = field(
        default_factory=dict
    )
    weight_contribution_analysis: Dict[str, float] = field(default_factory=dict)
    dimension_correlation: Dict[str, float] = field(default_factory=dict)

    # Penalty Behavior (Q2)
    penalty_frequency: Dict[str, float] = field(default_factory=dict)
    penalty_magnitude: Dict[str, Dict[str, float]] = field(default_factory=dict)
    penalty_impact_on_decisions: Dict[str, Any] = field(default_factory=dict)

    # Gate Behavior (Q4)
    gate_failure_rate: float = 0.0
    gate_failure_by_dimension: Dict[str, int] = field(default_factory=dict)
    gate_near_misses: Dict[str, int] = field(default_factory=dict)
    kappa_utilization: Dict[str, Any] = field(default_factory=dict)

    # Threshold Behavior (Q3)
    decision_distribution: Dict[str, int] = field(default_factory=dict)
    theta_boundary_clustering: Dict[str, Any] = field(default_factory=dict)
    decision_stability: Dict[str, Any] = field(default_factory=dict)

    # Decay Behavior (Q6)
    decay_relevance: Dict[str, Any] = field(default_factory=dict)
    half_life_vs_workflow_duration: Dict[str, Any] = field(default_factory=dict)

    # Drift and Trust-Loss (Q7)
    drift_signal_quality: Dict[str, Any] = field(default_factory=dict)
    trust_loss_component_balance: Dict[str, Any] = field(default_factory=dict)

    # Human Judgment
    human_review_candidates: List[Dict[str, Any]] = field(default_factory=list)

    # Assessments for Q1-Q7
    assessments: Dict[str, Dict[str, str]] = field(default_factory=dict)

    # Metadata
    tc_count: int = 0
    chain_id: Optional[str] = None
    generated_at: str = ""

    def to_dict(self) -> dict:
        """Return a JSON-serializable dict of the full result."""
        d: Dict[str, Any] = {
            "dimension_score_distribution": self.dimension_score_distribution,
            "weight_contribution_analysis": self.weight_contribution_analysis,
            "dimension_correlation": self.dimension_correlation,
            "penalty_frequency": self.penalty_frequency,
            "penalty_magnitude": self.penalty_magnitude,
            "penalty_impact_on_decisions": self.penalty_impact_on_decisions,
            "gate_failure_rate": self.gate_failure_rate,
            "gate_failure_by_dimension": self.gate_failure_by_dimension,
            "gate_near_misses": self.gate_near_misses,
            "kappa_utilization": self.kappa_utilization,
            "decision_distribution": self.decision_distribution,
            "theta_boundary_clustering": self.theta_boundary_clustering,
            "decision_stability": self.decision_stability,
            "decay_relevance": self.decay_relevance,
            "half_life_vs_workflow_duration": self.half_life_vs_workflow_duration,
            "drift_signal_quality": self.drift_signal_quality,
            "trust_loss_component_balance": self.trust_loss_component_balance,
            "human_review_candidates": self.human_review_candidates,
            "assessments": self.assessments,
            "tc_count": self.tc_count,
            "chain_id": self.chain_id,
            "generated_at": self.generated_at,
        }
        return d

    def summary(self) -> str:
        """
        Return a plain-language summary of all seven calibration
        assessments.
        """
        if self.tc_count == 0:
            return (
                "Calibration Report: No Trust Certificates found. "
                "Run governed evaluations before generating a calibration report."
            )

        lines = [
            f"Calibration Report ({self.tc_count} Trust Certificates analyzed)",
            f"Chain: {self.chain_id or 'all chains'}",
            f"Generated: {self.generated_at}",
            "",
        ]

        for qid in ("Q1", "Q2", "Q3", "Q4", "Q5", "Q6", "Q7"):
            assessment = self.assessments.get(qid, {})
            status = assessment.get("status", "unknown")
            signal = assessment.get("signal", "no data")
            recommendation = assessment.get("recommendation", "")
            lines.append(f"{qid}: [{status.upper()}] {signal}")
            if recommendation:
                lines.append(f"    Recommendation: {recommendation}")
            lines.append("")

        return "\n".join(lines)


# --------------------------------------------------------------------------- #
# CalibrationReport                                                            #
# --------------------------------------------------------------------------- #

class CalibrationReport:
    """
    Analyze Trust Certificates for calibration signals.

    Consumes TCs from the certificate store -- does not modify them.
    Answers 7 validation questions about whether the governance system
    is properly calibrated for the deployed workflow.
    """

    #: Dimensions tracked by the TCS scoring model.
    _DIMS = ("B", "A", "C", "K")

    #: Penalty component keys as stored in penalty_breakdown.
    _PENALTY_KEYS = ("P_cb", "P_d", "P_n", "P_h", "P_ps")

    def __init__(self, store: CertificateStore) -> None:
        self._store = store

    # ------------------------------------------------------------------ #
    # Public API                                                           #
    # ------------------------------------------------------------------ #

    def generate(self, chain_id: Optional[str] = None) -> CalibrationResult:
        """
        Generate a calibration report from stored TCs.

        Parameters
        ----------
        chain_id
            If provided, analyze only TCs from this chain. Otherwise
            analyze the most recent 200 TCs across all chains.

        Returns
        -------
        CalibrationResult
            Complete calibration analysis with assessments for Q1--Q7.
        """
        if chain_id is not None:
            tcs = self._store.list_chain(chain_id)
        else:
            tcs = self._store.list_recent(limit=200)

        result = CalibrationResult(
            chain_id=chain_id,
            generated_at=datetime.now(timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            ),
            tc_count=len(tcs),
        )

        if not tcs:
            result.assessments = self._empty_assessments()
            return result

        # Parse all TCs into dicts for analysis
        tc_dicts = [tc.to_dict() for tc in tcs]

        # Compute all analysis sections
        self._analyze_weight_behavior(tc_dicts, result)
        self._analyze_penalty_behavior(tc_dicts, result)
        self._analyze_gate_behavior(tc_dicts, result)
        self._analyze_threshold_behavior(tc_dicts, result)
        self._analyze_decay_behavior(tc_dicts, result)
        self._analyze_drift_and_trust_loss(tc_dicts, result)
        self._analyze_human_judgment(tc_dicts, result)

        # Produce assessments
        result.assessments = self._assess_all(result)

        return result

    # ------------------------------------------------------------------ #
    # Q1: Weight Behavior                                                  #
    # ------------------------------------------------------------------ #

    def _analyze_weight_behavior(
        self,
        tc_dicts: List[Dict[str, Any]],
        result: CalibrationResult,
    ) -> None:
        """Analyze dimension score distributions and weight contributions."""
        dim_scores: Dict[str, List[float]] = {d: [] for d in self._DIMS}
        contributions: Dict[str, List[float]] = {d: [] for d in self._DIMS}

        for tc in tc_dicts:
            scores = tc.get("component_scores", {})
            weights = tc.get("component_weights", {})
            for dim in self._DIMS:
                s = scores.get(dim)
                w = weights.get(dim)
                if s is not None:
                    dim_scores[dim].append(float(s))
                if s is not None and w is not None:
                    contributions[dim].append(float(s) * float(w))

        # Score distribution
        for dim in self._DIMS:
            vals = dim_scores[dim]
            if vals:
                result.dimension_score_distribution[dim] = {
                    "mean": round(statistics.mean(vals), 4),
                    "std": round(statistics.stdev(vals), 4) if len(vals) > 1 else 0.0,
                    "min": round(min(vals), 4),
                    "max": round(max(vals), 4),
                    "count": len(vals),
                }
            else:
                result.dimension_score_distribution[dim] = {
                    "mean": 0.0,
                    "std": 0.0,
                    "min": 0.0,
                    "max": 0.0,
                    "count": 0,
                }

        # Weight contribution analysis: mean contribution per dimension
        total_contribution = 0.0
        for dim in self._DIMS:
            vals = contributions[dim]
            mean_c = statistics.mean(vals) if vals else 0.0
            result.weight_contribution_analysis[dim] = round(mean_c, 4)
            total_contribution += mean_c

        # Dimension correlation: pairwise correlation of raw scores
        # Simplified: compute correlation between each pair
        result.dimension_correlation = {}
        dims_list = list(self._DIMS)
        for i in range(len(dims_list)):
            for j in range(i + 1, len(dims_list)):
                d1, d2 = dims_list[i], dims_list[j]
                vals1 = dim_scores[d1]
                vals2 = dim_scores[d2]
                if len(vals1) > 1 and len(vals2) > 1 and len(vals1) == len(vals2):
                    corr = self._pearson_correlation(vals1, vals2)
                    result.dimension_correlation[f"{d1}_{d2}"] = round(corr, 4)
                else:
                    result.dimension_correlation[f"{d1}_{d2}"] = 0.0

    @staticmethod
    def _pearson_correlation(x: List[float], y: List[float]) -> float:
        """Compute Pearson correlation coefficient between two lists."""
        n = len(x)
        if n < 2:
            return 0.0
        mean_x = statistics.mean(x)
        mean_y = statistics.mean(y)
        numerator = sum((xi - mean_x) * (yi - mean_y) for xi, yi in zip(x, y))
        denom_x = math.sqrt(sum((xi - mean_x) ** 2 for xi in x))
        denom_y = math.sqrt(sum((yi - mean_y) ** 2 for yi in y))
        if denom_x == 0.0 or denom_y == 0.0:
            return 0.0
        return numerator / (denom_x * denom_y)

    # ------------------------------------------------------------------ #
    # Q2: Penalty Behavior                                                 #
    # ------------------------------------------------------------------ #

    def _analyze_penalty_behavior(
        self,
        tc_dicts: List[Dict[str, Any]],
        result: CalibrationResult,
    ) -> None:
        """Analyze penalty frequency, magnitude, and decision impact."""
        n = len(tc_dicts)
        nonzero_counts: Dict[str, int] = {k: 0 for k in self._PENALTY_KEYS}
        active_values: Dict[str, List[float]] = {k: [] for k in self._PENALTY_KEYS}
        penalty_flipped = 0

        for tc in tc_dicts:
            breakdown = tc.get("penalty_breakdown", {})
            penalty_agg = float(tc.get("penalty_aggregate", 0.0))
            tis_raw = float(tc.get("tis_raw", 0.0))
            decision = tc.get("decision", "")

            for pk in self._PENALTY_KEYS:
                val = float(breakdown.get(pk, 0.0))
                if val > 0.0:
                    nonzero_counts[pk] += 1
                    active_values[pk].append(val)

            # Check if penalty could have flipped the decision:
            # If tis_raw >= theta_allow but tis_adjusted < theta_allow,
            # the penalty caused a downgrade.
            tis_adj = float(tc.get("tis_adjusted", 0.0))
            thresholds_section = tc.get("thresholds", {})
            # Use a rough theta_allow from the decision thresholds pattern
            # We check: was tis_raw above 0.85 but tis_adj below?
            if tis_raw >= 0.85 and tis_adj < 0.85 and penalty_agg > 0.0:
                penalty_flipped += 1

        # Frequency: fraction of TCs where each penalty is nonzero
        for pk in self._PENALTY_KEYS:
            result.penalty_frequency[pk] = round(nonzero_counts[pk] / n, 4) if n > 0 else 0.0

        # Magnitude: mean and max when active
        for pk in self._PENALTY_KEYS:
            vals = active_values[pk]
            if vals:
                result.penalty_magnitude[pk] = {
                    "mean": round(statistics.mean(vals), 4),
                    "max": round(max(vals), 4),
                    "count": len(vals),
                }
            else:
                result.penalty_magnitude[pk] = {
                    "mean": 0.0,
                    "max": 0.0,
                    "count": 0,
                }

        result.penalty_impact_on_decisions = {
            "penalty_flipped_count": penalty_flipped,
            "penalty_flipped_rate": round(penalty_flipped / n, 4) if n > 0 else 0.0,
        }

    # ------------------------------------------------------------------ #
    # Q4: Gate Behavior                                                    #
    # ------------------------------------------------------------------ #

    def _analyze_gate_behavior(
        self,
        tc_dicts: List[Dict[str, Any]],
        result: CalibrationResult,
    ) -> None:
        """Analyze gate failure rates, dimension breakdown, and near-misses."""
        n = len(tc_dicts)
        failures = 0
        failure_by_dim: Dict[str, int] = {d: 0 for d in self._DIMS}
        near_misses: Dict[str, int] = {d: 0 for d in self._DIMS}
        kappa_hold_count = 0
        kappa_eligible_count = 0

        for tc in tc_dicts:
            gate_passed = tc.get("gate_passed", True)
            gate_results = tc.get("gate_results", {})
            scores = tc.get("component_scores", {})
            thresholds = tc.get("thresholds", {})

            if not gate_passed:
                failures += 1
                for dim in self._DIMS:
                    if gate_results.get(dim) == "fail":
                        failure_by_dim[dim] += 1

            # Near-miss: score within 0.05 above threshold (passed but barely)
            for dim in self._DIMS:
                s = scores.get(dim)
                t = thresholds.get(dim)
                if s is not None and t is not None:
                    diff = float(s) - float(t)
                    if 0.0 <= diff <= 0.05:
                        near_misses[dim] += 1

            # Kappa utilization: when gate=0 and decision=Hold (not Stop)
            if not gate_passed:
                kappa_eligible_count += 1
                decision = tc.get("decision", "")
                if decision == "Hold":
                    kappa_hold_count += 1

        result.gate_failure_rate = round(failures / n, 4) if n > 0 else 0.0
        result.gate_failure_by_dimension = failure_by_dim
        result.gate_near_misses = near_misses
        result.kappa_utilization = {
            "kappa_hold_count": kappa_hold_count,
            "kappa_eligible_count": kappa_eligible_count,
            "kappa_utilization_rate": (
                round(kappa_hold_count / kappa_eligible_count, 4)
                if kappa_eligible_count > 0
                else 0.0
            ),
        }

    # ------------------------------------------------------------------ #
    # Q3: Threshold / Decision Behavior                                    #
    # ------------------------------------------------------------------ #

    def _analyze_threshold_behavior(
        self,
        tc_dicts: List[Dict[str, Any]],
        result: CalibrationResult,
    ) -> None:
        """Analyze decision distribution and theta boundary clustering."""
        n = len(tc_dicts)
        dist: Dict[str, int] = {}
        near_theta_allow = 0
        tis_current_values: List[float] = []

        for tc in tc_dicts:
            decision = tc.get("decision", "Unknown")
            dist[decision] = dist.get(decision, 0) + 1

            tis_c = float(tc.get("tis_current", 0.0))
            tis_current_values.append(tis_c)

            # Clustering near theta_allow: within 0.02 of 0.85
            # (canonical r3 theta_allow; good enough for calibration signal)
            if abs(tis_c - 0.85) <= 0.02 and tis_c > 0.0:
                near_theta_allow += 1

        result.decision_distribution = dist

        result.theta_boundary_clustering = {
            "near_theta_allow_count": near_theta_allow,
            "near_theta_allow_rate": round(near_theta_allow / n, 4) if n > 0 else 0.0,
        }

        # Decision stability: are decisions spread or concentrated?
        allow_count = dist.get("Allow", 0) + dist.get("Observe", 0)
        block_count = dist.get("Stop", 0) + dist.get("Hold", 0) + dist.get("Escalate", 0)
        if n > 0:
            allow_rate = round(allow_count / n, 4)
            block_rate = round(block_count / n, 4)
        else:
            allow_rate = 0.0
            block_rate = 0.0

        result.decision_stability = {
            "allow_rate": allow_rate,
            "block_rate": block_rate,
            "unique_decisions": len(dist),
        }

    # ------------------------------------------------------------------ #
    # Q6: Decay Behavior                                                   #
    # ------------------------------------------------------------------ #

    def _analyze_decay_behavior(
        self,
        tc_dicts: List[Dict[str, Any]],
        result: CalibrationResult,
    ) -> None:
        """Analyze whether temporal decay is relevant to the workflow."""
        decay_rates: List[float] = []
        tis_adj_values: List[float] = []
        tis_current_values: List[float] = []
        timestamps: List[str] = []

        for tc in tc_dicts:
            dr = tc.get("decay_rate")
            if dr is not None:
                decay_rates.append(float(dr))
            tis_adj = float(tc.get("tis_adjusted", 0.0))
            tis_cur = float(tc.get("tis_current", 0.0))
            tis_adj_values.append(tis_adj)
            tis_current_values.append(tis_cur)
            ts = tc.get("evaluation_timestamp", "")
            if ts:
                timestamps.append(str(ts))

        # Compute how often decay actually changes TIS_current from TIS_adj
        decay_active_count = 0
        for adj, cur in zip(tis_adj_values, tis_current_values):
            # Decay is active when tis_current < tis_adj (and both > 0)
            if adj > 0.0 and cur > 0.0 and cur < adj - 0.0001:
                decay_active_count += 1

        n = len(tc_dicts)
        result.decay_relevance = {
            "decay_active_count": decay_active_count,
            "decay_active_rate": round(decay_active_count / n, 4) if n > 0 else 0.0,
            "always_zero_elapsed": decay_active_count == 0,
        }

        # Half-life vs workflow duration
        if decay_rates:
            mean_rate = statistics.mean(decay_rates)
            if mean_rate > 0:
                half_life_hours = round(math.log(2) / mean_rate, 2)
            else:
                half_life_hours = float("inf")
        else:
            mean_rate = 0.0
            half_life_hours = float("inf")

        # Estimate workflow duration from timestamps
        workflow_duration_hours = 0.0
        if len(timestamps) >= 2:
            sorted_ts = sorted(timestamps)
            try:
                first = datetime.fromisoformat(
                    sorted_ts[0].replace("Z", "+00:00")
                )
                last = datetime.fromisoformat(
                    sorted_ts[-1].replace("Z", "+00:00")
                )
                workflow_duration_hours = round(
                    (last - first).total_seconds() / 3600.0, 4
                )
            except (ValueError, TypeError):
                pass

        result.half_life_vs_workflow_duration = {
            "mean_decay_rate": round(mean_rate, 4),
            "half_life_hours": half_life_hours,
            "workflow_duration_hours": workflow_duration_hours,
            "decay_meaningful": (
                workflow_duration_hours > 0.0
                and half_life_hours != float("inf")
                and workflow_duration_hours > half_life_hours * 0.1
            ),
        }

    # ------------------------------------------------------------------ #
    # Q7: Drift and Trust-Loss                                             #
    # ------------------------------------------------------------------ #

    def _analyze_drift_and_trust_loss(
        self,
        tc_dicts: List[Dict[str, Any]],
        result: CalibrationResult,
    ) -> None:
        """Analyze TIS variance and trust-loss component balance."""
        tis_values: List[float] = []
        penalty_totals: Dict[str, float] = {k: 0.0 for k in self._PENALTY_KEYS}

        for tc in tc_dicts:
            tis_c = float(tc.get("tis_current", 0.0))
            tis_values.append(tis_c)

            breakdown = tc.get("penalty_breakdown", {})
            for pk in self._PENALTY_KEYS:
                penalty_totals[pk] += float(breakdown.get(pk, 0.0))

        n = len(tc_dicts)

        # Drift signal quality: is there meaningful variance in TIS_current?
        if len(tis_values) > 1:
            tis_std = statistics.stdev(tis_values)
            tis_mean = statistics.mean(tis_values)
        else:
            tis_std = 0.0
            tis_mean = tis_values[0] if tis_values else 0.0

        result.drift_signal_quality = {
            "tis_mean": round(tis_mean, 4),
            "tis_std": round(tis_std, 4),
            "meaningful_variance": tis_std > 0.05,
        }

        # Trust-loss component balance: which penalties dominate?
        total_penalty = sum(penalty_totals.values())
        if total_penalty > 0:
            balance = {
                pk: round(v / total_penalty, 4)
                for pk, v in penalty_totals.items()
            }
        else:
            balance = {pk: 0.0 for pk in self._PENALTY_KEYS}

        result.trust_loss_component_balance = {
            "component_shares": balance,
            "dominant_component": (
                max(balance, key=balance.get)  # type: ignore[arg-type]
                if total_penalty > 0
                else None
            ),
            "total_penalty_mass": round(total_penalty, 4),
        }

    # ------------------------------------------------------------------ #
    # Human Judgment Candidates                                            #
    # ------------------------------------------------------------------ #

    def _analyze_human_judgment(
        self,
        tc_dicts: List[Dict[str, Any]],
        result: CalibrationResult,
    ) -> None:
        """Identify TCs flagged for human review."""
        candidates: List[Dict[str, Any]] = []
        for tc in tc_dicts:
            if tc.get("requires_human_review", False):
                candidates.append({
                    "certificate_id": tc.get("certificate_id", ""),
                    "decision": tc.get("decision", ""),
                    "tis_current": tc.get("tis_current"),
                    "blocking_reason": tc.get("blocking_reason"),
                })
        result.human_review_candidates = candidates

    # ------------------------------------------------------------------ #
    # Assessment Logic (Q1-Q7)                                             #
    # ------------------------------------------------------------------ #

    def _assess_all(self, result: CalibrationResult) -> Dict[str, Dict[str, str]]:
        """Produce assessment verdicts for all seven questions."""
        return {
            "Q1": self._assess_q1(result),
            "Q2": self._assess_q2(result),
            "Q3": self._assess_q3(result),
            "Q4": self._assess_q4(result),
            "Q5": self._assess_q5(result),
            "Q6": self._assess_q6(result),
            "Q7": self._assess_q7(result),
        }

    def _assess_q1(self, result: CalibrationResult) -> Dict[str, str]:
        """Q1: Are BACK weights producing meaningful differentiation?"""
        dist = result.dimension_score_distribution
        contribs = result.weight_contribution_analysis

        # Check if any dimension has very low variance (< 0.02 std)
        low_variance_dims = []
        for dim in self._DIMS:
            std = dist.get(dim, {}).get("std", 0.0)
            if std < 0.02:
                low_variance_dims.append(dim)

        # Check if any dimension dominates contribution (> 60% of total)
        total_contrib = sum(contribs.values())
        dominant_dim = None
        if total_contrib > 0:
            for dim, val in contribs.items():
                if val / total_contrib > 0.60:
                    dominant_dim = dim

        if dominant_dim:
            return {
                "status": "flagged",
                "signal": (
                    f"Dimension {dominant_dim} contributes >60% of TIS_raw variance. "
                    f"Other dimensions may not meaningfully influence outcomes."
                ),
                "recommendation": (
                    f"Review weight allocation. Consider whether {dominant_dim} "
                    f"dominance reflects true risk priority or a calibration artifact."
                ),
            }
        if low_variance_dims:
            return {
                "status": "needs_attention",
                "signal": (
                    f"Dimensions {', '.join(low_variance_dims)} show very low score "
                    f"variance (std < 0.02). They may not differentiate evaluations."
                ),
                "recommendation": (
                    "Investigate whether these dimensions receive meaningful "
                    "input variation in the deployed workflow."
                ),
            }
        return {
            "status": "calibrated",
            "signal": "Dimension weights produce meaningful differentiation across evaluations.",
            "recommendation": "",
        }

    def _assess_q2(self, result: CalibrationResult) -> Dict[str, str]:
        """Q2: Are CT-4 penalties firing at the right frequency?"""
        freq = result.penalty_frequency
        p_cb_freq = freq.get("P_cb", 0.0)

        if p_cb_freq > 0.80:
            return {
                "status": "flagged",
                "signal": (
                    f"Cross-boundary penalty P_cb fires on {p_cb_freq:.0%} of evaluations. "
                    f"This suggests pervasive attribution gaps -- too strict or data quality issue."
                ),
                "recommendation": (
                    "Review upstream data sources for missing metadata. "
                    "If gaps are expected, consider adjusting delta_cb or penalty weight."
                ),
            }
        if p_cb_freq == 0.0:
            all_zero = all(v == 0.0 for v in freq.values())
            if all_zero:
                return {
                    "status": "needs_attention",
                    "signal": "No penalties are firing. The penalty system may not be engaged.",
                    "recommendation": (
                        "Verify that context metadata (n_gaps, novelty_score, etc.) "
                        "is being populated from real workflow signals."
                    ),
                }
            return {
                "status": "calibrated",
                "signal": "P_cb never fires. Attribution gaps are absent in this workflow.",
                "recommendation": "",
            }
        return {
            "status": "calibrated",
            "signal": f"P_cb fires on {p_cb_freq:.0%} of evaluations. Penalty frequency appears reasonable.",
            "recommendation": "",
        }

    def _assess_q3(self, result: CalibrationResult) -> Dict[str, str]:
        """Q3: Is the decision distribution healthy?"""
        dist = result.decision_distribution
        n = result.tc_count
        if n == 0:
            return {
                "status": "unknown",
                "signal": "No evaluations to analyze.",
                "recommendation": "",
            }

        allow_count = dist.get("Allow", 0) + dist.get("Observe", 0)
        stop_count = dist.get("Stop", 0)
        allow_rate = allow_count / n

        if allow_rate > 0.90:
            return {
                "status": "needs_attention",
                "signal": (
                    f"Allow/Observe rate is {allow_rate:.0%}. "
                    f"Governance may not be adding value if nearly everything passes."
                ),
                "recommendation": (
                    "Consider whether thresholds are too permissive, or verify "
                    "that the workflow genuinely produces high-quality outputs."
                ),
            }
        if stop_count / n > 0.50:
            return {
                "status": "flagged",
                "signal": (
                    f"Stop rate is {stop_count / n:.0%}. "
                    f"More than half of evaluations are blocked."
                ),
                "recommendation": (
                    "Review whether scoring inputs accurately reflect output quality. "
                    "High Stop rates may indicate upstream data issues rather than "
                    "genuine compliance failures."
                ),
            }

        # Check clustering near theta_allow
        clustering = result.theta_boundary_clustering
        near_rate = clustering.get("near_theta_allow_rate", 0.0)
        if near_rate > 0.30:
            return {
                "status": "needs_attention",
                "signal": (
                    f"{near_rate:.0%} of TIS_current values cluster within 0.02 of "
                    f"theta_allow. Decisions are highly sensitive to small score changes."
                ),
                "recommendation": (
                    "Consider whether the threshold is at the right position or "
                    "if workflow outputs cluster naturally at this score range."
                ),
            }

        return {
            "status": "calibrated",
            "signal": (
                f"Decision distribution: {dict(dist)}. "
                f"Mix of outcomes suggests governance is differentiating."
            ),
            "recommendation": "",
        }

    def _assess_q4(self, result: CalibrationResult) -> Dict[str, str]:
        """Q4: Are gate failures concentrated in one dimension?"""
        failures_by_dim = result.gate_failure_by_dimension
        total_failures = sum(failures_by_dim.values())
        near_misses = result.gate_near_misses
        total_near_misses = sum(near_misses.values())

        if total_failures == 0:
            if total_near_misses > 0:
                return {
                    "status": "needs_attention",
                    "signal": (
                        f"No gate failures, but {total_near_misses} near-miss(es) detected "
                        f"(score within 0.05 of threshold)."
                    ),
                    "recommendation": (
                        "Near-misses indicate the workflow operates close to gate "
                        "boundaries. Monitor for threshold sensitivity."
                    ),
                }
            return {
                "status": "calibrated",
                "signal": "No gate failures detected.",
                "recommendation": "",
            }

        # Check concentration
        dominant_dim = max(failures_by_dim, key=failures_by_dim.get)  # type: ignore[arg-type]
        dominant_count = failures_by_dim[dominant_dim]
        if total_failures > 0 and dominant_count / total_failures > 0.80:
            return {
                "status": "flagged",
                "signal": (
                    f"Gate failures concentrated in dimension {dominant_dim} "
                    f"({dominant_count}/{total_failures} = "
                    f"{dominant_count / total_failures:.0%})."
                ),
                "recommendation": (
                    f"Investigate why {dominant_dim} consistently fails. "
                    f"This may indicate a systematic upstream issue rather "
                    f"than genuine risk variation."
                ),
            }

        return {
            "status": "calibrated",
            "signal": (
                f"Gate failures distributed across dimensions: {dict(failures_by_dim)}."
            ),
            "recommendation": "",
        }

    def _assess_q5(self, result: CalibrationResult) -> Dict[str, str]:
        """Q5: Are kappa and theta thresholds reachable and meaningful?"""
        kappa = result.kappa_utilization
        kappa_eligible = kappa.get("kappa_eligible_count", 0)
        kappa_holds = kappa.get("kappa_hold_count", 0)
        stability = result.decision_stability
        unique = stability.get("unique_decisions", 0)

        signals = []

        if kappa_eligible > 0 and kappa_holds == 0:
            signals.append(
                "Kappa never triggers Hold (all gate failures go to Stop). "
                "The soft-hold ceiling may be too low."
            )
        elif kappa_eligible > 0:
            signals.append(
                f"Kappa triggered Hold on {kappa_holds}/{kappa_eligible} gate failures."
            )

        if unique <= 2:
            signals.append(
                f"Only {unique} distinct decision type(s) observed. "
                f"Some decision paths (Escalate, Observe) may be unreachable."
            )

        if signals:
            has_flag = kappa_eligible > 0 and kappa_holds == 0
            return {
                "status": "flagged" if has_flag else "needs_attention",
                "signal": " ".join(signals),
                "recommendation": (
                    "Review kappa ceiling and theta thresholds to ensure "
                    "all decision paths are reachable in the deployed workflow."
                ),
            }

        return {
            "status": "calibrated",
            "signal": "Kappa and theta thresholds appear reachable and meaningful.",
            "recommendation": "",
        }

    def _assess_q6(self, result: CalibrationResult) -> Dict[str, str]:
        """Q6: Is temporal decay relevant to the workflow?"""
        decay = result.decay_relevance
        hl = result.half_life_vs_workflow_duration

        if decay.get("always_zero_elapsed", True):
            return {
                "status": "needs_attention",
                "signal": (
                    "Decay never affects TIS_current (elapsed_hours always 0). "
                    "Temporal decay is configured but not exercised."
                ),
                "recommendation": (
                    "If evaluations are always immediate, decay is irrelevant. "
                    "Consider whether this is intentional or if elapsed_hours "
                    "should be populated from workflow timing."
                ),
            }

        meaningful = hl.get("decay_meaningful", False)
        if not meaningful:
            return {
                "status": "needs_attention",
                "signal": (
                    f"Workflow duration ({hl.get('workflow_duration_hours', 0):.2f}h) "
                    f"is short relative to half-life ({hl.get('half_life_hours', 0):.1f}h). "
                    f"Decay has negligible effect."
                ),
                "recommendation": (
                    "Decay rate may be too slow for this workflow cadence, "
                    "or the workflow completes within a single half-life window."
                ),
            }

        return {
            "status": "calibrated",
            "signal": (
                f"Decay is active on {decay.get('decay_active_rate', 0):.0%} of evaluations. "
                f"Half-life ({hl.get('half_life_hours', 0):.1f}h) is proportionate "
                f"to workflow duration ({hl.get('workflow_duration_hours', 0):.2f}h)."
            ),
            "recommendation": "",
        }

    def _assess_q7(self, result: CalibrationResult) -> Dict[str, str]:
        """Q7: Are drift signals and trust-loss components balanced?"""
        drift = result.drift_signal_quality
        balance = result.trust_loss_component_balance

        meaningful = drift.get("meaningful_variance", False)
        dominant = balance.get("dominant_component")
        shares = balance.get("component_shares", {})

        if not meaningful:
            return {
                "status": "needs_attention",
                "signal": (
                    f"TIS_current std = {drift.get('tis_std', 0):.4f}. "
                    f"Low variance suggests the workflow produces uniform trust scores."
                ),
                "recommendation": (
                    "If all outputs are similar quality, this may be expected. "
                    "Otherwise, investigate whether scoring inputs lack variation."
                ),
            }

        if dominant and shares.get(dominant, 0.0) > 0.80:
            return {
                "status": "needs_attention",
                "signal": (
                    f"Trust-loss dominated by {dominant} "
                    f"({shares[dominant]:.0%} of total penalty mass). "
                    f"Other penalty components may not be contributing."
                ),
                "recommendation": (
                    f"Review whether {dominant} dominance reflects true risk "
                    f"or if other penalty inputs need better upstream signals."
                ),
            }

        return {
            "status": "calibrated",
            "signal": (
                f"TIS variance is meaningful (std={drift.get('tis_std', 0):.4f}). "
                f"Trust-loss components are balanced."
            ),
            "recommendation": "",
        }

    # ------------------------------------------------------------------ #
    # Helpers                                                              #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _empty_assessments() -> Dict[str, Dict[str, str]]:
        """Return default assessments when no TCs are available."""
        empty = {
            "status": "unknown",
            "signal": "No Trust Certificates available for analysis.",
            "recommendation": "Run governed evaluations before generating a calibration report.",
        }
        return {f"Q{i}": dict(empty) for i in range(1, 8)}
