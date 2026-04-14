"""
tcs.dynamics.pll
================

Policy Learning Layer (Phase 3 Step 4).

Generates recommendations to adjust Risk Tolerance Profile parameters
when sustained drift is detected. The core update rule:

    k_(t+1) = k_t + eta * nabla_L_t

Where:
    k_t       = current Risk Tolerance Profile parameters
    eta       = learning rate (default 0.01, configurable per domain)
    nabla_L_t = gradient of trust loss w.r.t. parameters
              = direction that reduces L_trust

Stability constraints:
    1. |delta_k| < epsilon_max per cycle (max 0.05 threshold change)
    2. window >= W_min evaluations before first adaptation (default 100)
    3. r3 changes require human approval before applying
    4. rollback available for 7 days after any adaptation
    5. adaptation log immutable — every change recorded

The PLL does NOT apply changes directly. It creates a pending
AdaptationRecommendation that must be approved (or auto-approved for
r1/r2) before taking effect.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from tcs.dynamics.drift import compute_drift, DRIFT_THRESHOLDS
from tcs.dynamics.trust_loss import compute_trust_loss
from tcs.dynamics.models import AdaptationRecommendation
from tcs.persistence import CertificateStore
from tcs.policy_profiles import PolicyProfile, load_profile


# --------------------------------------------------------------------------- #
# PLL configuration                                                            #
# --------------------------------------------------------------------------- #

#: Learning rate per domain. Controls how aggressively parameters shift.
LEARNING_RATES: Dict[str, float] = {
    "financial_services": 0.01,
    "healthcare": 0.008,
    "enterprise": 0.015,
}
DEFAULT_LEARNING_RATE: float = 0.01

#: Maximum absolute threshold change per cycle.
EPSILON_MAX: float = 0.05

#: Minimum evaluations in the window before PLL can fire.
W_MIN: int = 100

#: Rollback availability window in days.
ROLLBACK_WINDOW_DAYS: int = 7


# --------------------------------------------------------------------------- #
# Gradient computation                                                         #
# --------------------------------------------------------------------------- #

def _compute_gradient(
    trust_loss_components: Dict[str, float],
    drift_components: Dict[str, float],
) -> Dict[str, float]:
    """
    Compute the gradient direction for threshold adjustments.

    The gradient points in the direction that reduces trust loss.
    High K component -> tighten theta_allow (raise it).
    High P component -> loosen theta_allow (lower it, reducing gate failures).
    High level drift -> adjust theta_allow proportionally.

    Returns a dict of {parameter: gradient_value} for theta_allow,
    theta_hold, and theta_escalate.
    """
    k_val = trust_loss_components.get("K", 0.0)
    p_val = trust_loss_components.get("P", 0.0)
    level_drift = drift_components.get("level", 0.0)

    # If policy deviation (P) is high, the system is too strict — too many
    # gate failures. The gradient should lower thresholds.
    # If known (K) is high, the system is too loose — TIS is declining.
    # The gradient should raise thresholds.
    # Net direction = K pushes up, P pushes down.
    grad_allow = k_val - p_val + level_drift * 0.5
    grad_hold = grad_allow * 0.8   # hold tracks allow with dampening
    grad_escalate = grad_allow * 0.6  # escalate tracks with more dampening

    return {
        "theta_allow": grad_allow,
        "theta_hold": grad_hold,
        "theta_escalate": grad_escalate,
    }


def _clamp_delta(delta: float) -> float:
    """Clamp a parameter delta to [-EPSILON_MAX, +EPSILON_MAX]."""
    return max(-EPSILON_MAX, min(EPSILON_MAX, delta))


def _clamp_threshold(value: float) -> float:
    """Keep thresholds in a reasonable range [0.30, 0.99]."""
    return max(0.30, min(0.99, value))


# --------------------------------------------------------------------------- #
# Recommendation generation                                                    #
# --------------------------------------------------------------------------- #

def generate_recommendation(
    store: CertificateStore,
    profile: PolicyProfile,
    *,
    window_hours: float = 24.0,
    domain: Optional[str] = None,
) -> Optional[AdaptationRecommendation]:
    """
    Evaluate whether PLL should recommend a parameter change.

    Returns an AdaptationRecommendation if drift exceeds D_alert AND
    the window has at least W_MIN evaluations. Returns None otherwise.

    Parameters
    ----------
    store
        CertificateStore to read TC data from.
    profile
        The PolicyProfile to potentially adapt.
    window_hours
        Sliding window for drift/loss computation.
    domain
        Domain for trust loss computation. Defaults to profile.domain.
    """
    dm = domain or profile.domain

    # Check minimum evaluation count
    tc_rows = store.query_window(window_hours)
    if len(tc_rows) < W_MIN:
        return None

    # Compute drift for this profile's context
    drift_signals = compute_drift(store, window_hours=window_hours)

    # Find the signal for this profile's policy_set_id
    target_signal = None
    for sig in drift_signals:
        if sig.context == profile.profile_id:
            target_signal = sig
            break

    # If no matching context or no alert-level drift, skip
    if target_signal is None:
        # Check if ANY context has alert-level drift
        target_signal = next(
            (s for s in drift_signals
             if s.threshold_breached in ("D_alert", "D_crit")),
            None,
        )
    if target_signal is None:
        return None
    if target_signal.threshold_breached not in ("D_alert", "D_crit"):
        return None

    # Compute trust loss for gradient
    loss_result = compute_trust_loss(store, domain=dm, window_hours=window_hours)

    # Compute gradient
    gradient = _compute_gradient(
        loss_result.components,
        target_signal.components,
    )

    # Apply learning rate and clamp
    eta = LEARNING_RATES.get(dm, DEFAULT_LEARNING_RATE)
    current = profile.decision_thresholds
    parameter_changes: Dict[str, Dict[str, float]] = {}

    for param in ("theta_allow", "theta_hold", "theta_escalate"):
        before = current[param]
        raw_delta = eta * gradient[param]
        delta = _clamp_delta(raw_delta)
        after = _clamp_threshold(before + delta)
        actual_delta = round(after - before, 4)
        if abs(actual_delta) > 1e-6:
            parameter_changes[param] = {
                "before": round(before, 4),
                "after": round(after, 4),
                "delta": actual_delta,
            }

    # If no meaningful changes, skip
    if not parameter_changes:
        return None

    # Build record
    now = datetime.now(timezone.utc)
    record_id = f"PAR-{dm}-{now.strftime('%Y%m%dT%H%M%S')}"
    rollback_until = now + timedelta(days=ROLLBACK_WINDOW_DAYS)

    rec = AdaptationRecommendation(
        record_id=record_id,
        triggered_by="drift_alert",
        risk_tolerance_profile_id=profile.profile_id,
        parameter_changes=parameter_changes,
        evidence={
            "D_trust": target_signal.D_trust,
            "L_trust": round(loss_result.L_t, 4),
            "window_evaluations": loss_result.window_evaluations,
            "drift_context": target_signal.context,
            "threshold_breached": target_signal.threshold_breached,
        },
        approval_status="pending",
    )

    # Persist
    store.insert_adaptation(
        record_id=rec.record_id,
        triggered_by=rec.triggered_by,
        profile_id=rec.risk_tolerance_profile_id,
        parameter_changes=rec.parameter_changes,
        evidence=rec.evidence,
        rollback_available_until=rollback_until.strftime("%Y-%m-%dT%H:%M:%SZ"),
    )

    return rec


# --------------------------------------------------------------------------- #
# Approval workflow                                                            #
# --------------------------------------------------------------------------- #

def approve_recommendation(
    store: CertificateStore,
    record_id: str,
    approver: str = "system",
) -> Optional[Dict[str, Any]]:
    """
    Approve a pending adaptation record.

    Returns the updated record dict, or None if not found.
    """
    rec = store.get_adaptation(record_id)
    if rec is None:
        return None
    if rec["approval_status"] != "pending":
        return rec  # already processed

    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    store.update_adaptation_status(
        record_id, "approved", approver=approver, applied_at=now,
    )
    return store.get_adaptation(record_id)


def reject_recommendation(
    store: CertificateStore,
    record_id: str,
    approver: str = "system",
) -> Optional[Dict[str, Any]]:
    """
    Reject a pending adaptation record.

    Returns the updated record dict, or None if not found.
    """
    rec = store.get_adaptation(record_id)
    if rec is None:
        return None
    if rec["approval_status"] != "pending":
        return rec

    store.update_adaptation_status(record_id, "rejected", approver=approver)
    return store.get_adaptation(record_id)


def rollback_recommendation(
    store: CertificateStore,
    record_id: str,
    approver: str = "system",
) -> Optional[Dict[str, Any]]:
    """
    Rollback an approved adaptation (within the rollback window).

    Returns the updated record dict, or None if not found or
    rollback window expired.
    """
    rec = store.get_adaptation(record_id)
    if rec is None:
        return None
    if rec["approval_status"] != "approved":
        return None

    # Check rollback window
    if rec.get("rollback_available_until"):
        deadline = datetime.fromisoformat(
            rec["rollback_available_until"].replace("Z", "+00:00")
        )
        if datetime.now(timezone.utc) > deadline:
            return None  # window expired

    store.update_adaptation_status(record_id, "rolled_back", approver=approver)
    return store.get_adaptation(record_id)


def get_recommendations(
    store: CertificateStore,
    profile_id: Optional[str] = None,
    status: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """
    List adaptation records with optional filters.

    Returns dicts with parameter_changes and evidence parsed from JSON.
    """
    import json
    rows = store.list_adaptations(profile_id=profile_id, status=status)
    for r in rows:
        if isinstance(r.get("parameter_changes_json"), str):
            r["parameter_changes"] = json.loads(r["parameter_changes_json"])
        if isinstance(r.get("evidence_json"), str):
            r["evidence"] = json.loads(r["evidence_json"])
    return rows
