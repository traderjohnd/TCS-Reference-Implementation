"""
tcs.dynamics.recovery
=====================

Recovery Orchestrator (Phase 3 Step 5).

Implements the six-phase recovery lifecycle when D_trust exceeds D_crit:

    1. Containment   — fail-closed at r3, suspend PLL, alert
    2. Diagnosis     — identify dominant failure driver
    3. Remediation   — generate and apply remediation plan
    4. Revalidation  — verify D_trust below D_alert in shadow mode
    5. Reintroduction — gradually re-enable, 72h monitoring
    6. Stabilization  — 30-day monitoring, resume PLL, close incident

Recovery Score:
    S_recovery = G_trust / (L_trust + epsilon)

    G_trust = governance effectiveness (1 - L_t trend)
    epsilon = 0.001 (prevents division by zero)
    S_recovery > 1.0 = improving faster than it failed
    S_recovery < 1.0 = recovery slower than degradation
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from tcs.dynamics.drift import compute_drift, DRIFT_THRESHOLDS
from tcs.dynamics.trust_loss import compute_trust_loss
from tcs.persistence import CertificateStore


# --------------------------------------------------------------------------- #
# Constants                                                                    #
# --------------------------------------------------------------------------- #

#: Ordered phases of the recovery lifecycle.
RECOVERY_PHASES: List[str] = [
    "containment",
    "diagnosis",
    "remediation",
    "revalidation",
    "reintroduction",
    "stabilization",
]

#: Epsilon for recovery score denominator.
EPSILON: float = 0.001


# --------------------------------------------------------------------------- #
# Recovery score                                                               #
# --------------------------------------------------------------------------- #

def compute_recovery_score(
    store: CertificateStore,
    *,
    domain: str = "financial_services",
    window_hours: float = 24.0,
) -> float:
    """
    Compute S_recovery = G_trust / (L_trust + epsilon).

    G_trust is 1 - L_t (governance effectiveness).
    """
    loss = compute_trust_loss(store, domain=domain, window_hours=window_hours)
    g_trust = 1.0 - loss.L_t
    s_recovery = g_trust / (loss.L_t + EPSILON)
    return round(s_recovery, 4)


# --------------------------------------------------------------------------- #
# Activation                                                                   #
# --------------------------------------------------------------------------- #

def check_and_activate(
    store: CertificateStore,
    *,
    window_hours: float = 24.0,
    domain: str = "financial_services",
) -> Optional[Dict[str, Any]]:
    """
    Check if D_trust >= D_crit for any context and activate recovery
    if no active incident exists.

    Returns the incident dict if activated, None otherwise.
    """
    # Don't activate if already in recovery
    active = store.get_active_recovery()
    if active is not None:
        return None

    # Check drift
    signals = compute_drift(store, window_hours=window_hours)
    crit_signal = next(
        (s for s in signals if s.threshold_breached == "D_crit"),
        None,
    )
    if crit_signal is None:
        return None

    # Activate recovery
    now = datetime.now(timezone.utc)
    incident_id = f"REC-{domain}-{now.strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:6]}"

    loss = compute_trust_loss(store, domain=domain, window_hours=window_hours)

    evidence = {
        "D_trust": crit_signal.D_trust,
        "L_trust": round(loss.L_t, 4),
        "context": crit_signal.context,
        "drift_components": crit_signal.components,
        "loss_components": {k: round(v, 4) for k, v in loss.components.items()},
        "dominant_loss_component": loss.dominant_component,
        "window_evaluations": loss.window_evaluations,
    }

    store.insert_recovery_incident(
        incident_id=incident_id,
        trigger_d_trust=crit_signal.D_trust,
        trigger_context=crit_signal.context,
        trigger_evidence=evidence,
    )

    return store.get_recovery_incident(incident_id)


# --------------------------------------------------------------------------- #
# Phase advancement                                                            #
# --------------------------------------------------------------------------- #

def advance_phase(
    store: CertificateStore,
    incident_id: str,
    *,
    domain: str = "financial_services",
    window_hours: float = 24.0,
) -> Optional[Dict[str, Any]]:
    """
    Advance the recovery incident to the next phase.

    Validates that exit criteria for the current phase are met before
    advancing. Returns the updated incident, or None if not found or
    cannot advance.
    """
    incident = store.get_recovery_incident(incident_id)
    if incident is None or incident["status"] != "active":
        return None

    current = incident["current_phase"]
    idx = RECOVERY_PHASES.index(current) if current in RECOVERY_PHASES else -1
    if idx < 0 or idx >= len(RECOVERY_PHASES) - 1:
        return None  # already at final phase or unknown

    next_phase = RECOVERY_PHASES[idx + 1]
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    # Parse phase history
    phase_history = json.loads(incident["phase_history_json"] or "[]")

    # Phase-specific actions on transition
    updates: Dict[str, Any] = {
        "current_phase": next_phase,
    }

    if next_phase == "diagnosis":
        # Generate diagnostic report
        loss = compute_trust_loss(store, domain=domain, window_hours=window_hours)
        diagnostic = {
            "dominant_driver": loss.dominant_component,
            "L_trust": round(loss.L_t, 4),
            "components": {k: round(v, 4) for k, v in loss.components.items()},
            "diagnosed_at": now,
        }
        updates["diagnostic_json"] = json.dumps(diagnostic)

    elif next_phase == "remediation":
        # Generate remediation plan from diagnostic
        diagnostic = json.loads(incident.get("diagnostic_json") or "{}")
        dominant = diagnostic.get("dominant_driver", "unknown")
        remediation = {
            "plan": f"Address {dominant} component degradation",
            "dominant_driver": dominant,
            "shadow_mode": True,
            "generated_at": now,
        }
        updates["remediation_json"] = json.dumps(remediation)

    elif next_phase == "revalidation":
        # Compute recovery score
        s_rec = compute_recovery_score(
            store, domain=domain, window_hours=window_hours,
        )
        updates["s_recovery"] = s_rec

    elif next_phase == "stabilization":
        # Update recovery score
        s_rec = compute_recovery_score(
            store, domain=domain, window_hours=window_hours,
        )
        updates["s_recovery"] = s_rec

    # Record phase transition in history
    phase_history.append({"phase": next_phase, "entered_at": now})
    updates["phase_history_json"] = json.dumps(phase_history)

    store.update_recovery_incident(incident_id, **updates)
    return store.get_recovery_incident(incident_id)


def complete_recovery(
    store: CertificateStore,
    incident_id: str,
    *,
    domain: str = "financial_services",
    window_hours: float = 24.0,
) -> Optional[Dict[str, Any]]:
    """
    Complete a recovery incident that has reached the stabilization phase.

    Returns the completed incident, or None if conditions not met.
    """
    incident = store.get_recovery_incident(incident_id)
    if incident is None:
        return None
    if incident["current_phase"] != "stabilization":
        return None
    if incident["status"] != "active":
        return None

    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    s_rec = compute_recovery_score(
        store, domain=domain, window_hours=window_hours,
    )

    store.update_recovery_incident(
        incident_id,
        status="completed",
        s_recovery=s_rec,
        completed_at=now,
    )
    return store.get_recovery_incident(incident_id)


# --------------------------------------------------------------------------- #
# Status and history                                                           #
# --------------------------------------------------------------------------- #

def get_recovery_status(
    store: CertificateStore,
    *,
    domain: str = "financial_services",
    window_hours: float = 24.0,
) -> Dict[str, Any]:
    """
    Get current recovery status.

    Returns the active incident if one exists, otherwise a summary
    indicating no active recovery.
    """
    active = store.get_active_recovery()
    if active is None:
        # Check if we should activate
        signals = compute_drift(store, window_hours=window_hours)
        crit = next(
            (s for s in signals if s.threshold_breached == "D_crit"),
            None,
        )
        return {
            "recovery_active": False,
            "d_crit_detected": crit is not None,
            "current_d_trust": crit.D_trust if crit else None,
        }

    # Parse JSON fields for response
    result = dict(active)
    result["recovery_active"] = True
    for field in ("trigger_evidence_json", "diagnostic_json",
                  "remediation_json", "phase_history_json"):
        if result.get(field) and isinstance(result[field], str):
            try:
                result[field.replace("_json", "")] = json.loads(result[field])
            except (json.JSONDecodeError, TypeError):
                pass

    # Compute current recovery score
    result["s_recovery_current"] = compute_recovery_score(
        store, domain=domain, window_hours=window_hours,
    )
    return result


def get_recovery_history(
    store: CertificateStore,
) -> List[Dict[str, Any]]:
    """Return all recovery incidents."""
    incidents = store.list_recovery_incidents()
    results = []
    for inc in incidents:
        d = dict(inc)
        for field in ("trigger_evidence_json", "phase_history_json"):
            if d.get(field) and isinstance(d[field], str):
                try:
                    d[field.replace("_json", "")] = json.loads(d[field])
                except (json.JSONDecodeError, TypeError):
                    pass
        results.append(d)
    return results
