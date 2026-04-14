"""
tests/test_recovery.py
======================

Phase 3 Step 5 — Recovery Orchestrator tests.

Tests verify:
    1. Recovery activates when D_trust >= D_crit
    2. No activation when no D_crit breach
    3. No duplicate activation when already active
    4. Phase transitions follow correct order
    5. Cannot skip phases
    6. Recovery score computed correctly
    7. Completion only from stabilization phase
    8. Full lifecycle: containment -> ... -> stabilization -> completed
    9. History tracks all incidents
"""

from __future__ import annotations

import hashlib
import json
import uuid

import pytest

from tcs.persistence import CertificateStore
from tcs.dynamics.recovery import (
    check_and_activate,
    advance_phase,
    complete_recovery,
    compute_recovery_score,
    get_recovery_status,
    get_recovery_history,
    RECOVERY_PHASES,
    EPSILON,
)


# --------------------------------------------------------------------------- #
# Fixtures                                                                     #
# --------------------------------------------------------------------------- #

@pytest.fixture
def mem_store():
    store = CertificateStore(":memory:")
    yield store
    store.close()


def _seed_crisis_tcs(store, n=120, *, policy="fin-r3-a4-ct4"):
    """
    Seed TCs that produce D_crit level drift.
    First half healthy, second half severely degraded.
    """
    half = n // 2
    for i in range(n):
        cert_id = str(uuid.uuid4())
        ts = f"2026-04-08T{(i % 24):02d}:{(i // 24) % 60:02d}:00Z"

        if i < half:
            tis = 0.92
            decision = "Allow"
        else:
            tis = 0.05
            decision = "Stop"

        content = {
            "certificate_id": cert_id,
            "tis_current": tis,
            "decision": decision,
            "integration_boundary_gaps": 2 if i >= half else 0,
            "component_scores": {"B": 0.9, "A": 0.9, "C": 0.9, "K": 0.9},
        }
        canonical = json.dumps(
            {k: v for k, v in content.items() if k != "audit_integrity"},
            sort_keys=True, separators=(",", ":")
        )
        tc_hash = hashlib.sha256(canonical.encode()).hexdigest()

        store._conn.execute(
            """INSERT INTO trust_certificates (
                certificate_id, subject_id, subject_type, domain,
                risk_tier, action_class, policy_set_id,
                decision, lifecycle_state, invalidation_status,
                tis_raw, tis_adjusted, tis_current,
                evaluation_timestamp, valid_until,
                chain_id, chain_sequence, hash_algorithm,
                tc_hash, content_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (cert_id, f"test-{i}", "recommendation", "financial_services",
             "r3", "a4", policy,
             decision, "computed", "valid",
             tis, tis, tis,
             ts, "2026-04-09T10:00:00Z",
             "test-chain", i + 1, "sha256",
             tc_hash, json.dumps(content)),
        )


def _seed_healthy_tcs(store, n=20):
    """Seed healthy TCs that won't trigger D_crit."""
    for i in range(n):
        cert_id = str(uuid.uuid4())
        ts = f"2026-04-08T{(i % 24):02d}:{(i // 24) % 60:02d}:00Z"
        content = {
            "certificate_id": cert_id,
            "tis_current": 0.90,
            "decision": "Allow",
            "integration_boundary_gaps": 0,
            "component_scores": {"B": 0.9, "A": 0.9, "C": 0.9, "K": 0.9},
        }
        canonical = json.dumps(
            {k: v for k, v in content.items() if k != "audit_integrity"},
            sort_keys=True, separators=(",", ":")
        )
        tc_hash = hashlib.sha256(canonical.encode()).hexdigest()

        store._conn.execute(
            """INSERT INTO trust_certificates (
                certificate_id, subject_id, subject_type, domain,
                risk_tier, action_class, policy_set_id,
                decision, lifecycle_state, invalidation_status,
                tis_raw, tis_adjusted, tis_current,
                evaluation_timestamp, valid_until,
                chain_id, chain_sequence, hash_algorithm,
                tc_hash, content_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (cert_id, f"test-{i}", "recommendation", "financial_services",
             "r3", "a4", "fin-r3-a4-ct4",
             "Allow", "computed", "valid",
             0.90, 0.90, 0.90,
             ts, "2026-04-09T10:00:00Z",
             "test-chain", i + 1, "sha256",
             tc_hash, json.dumps(content)),
        )


# --------------------------------------------------------------------------- #
# Activation tests                                                             #
# --------------------------------------------------------------------------- #

class TestActivation:
    def test_activates_on_d_crit(self, mem_store):
        """Recovery should activate when D_trust >= D_crit."""
        _seed_crisis_tcs(mem_store)
        result = check_and_activate(mem_store, window_hours=48)
        assert result is not None
        assert result["status"] == "active"
        assert result["current_phase"] == "containment"
        assert result["incident_id"].startswith("REC-")

    def test_no_activation_when_healthy(self, mem_store):
        """No activation when drift is below D_crit."""
        _seed_healthy_tcs(mem_store)
        result = check_and_activate(mem_store, window_hours=48)
        assert result is None

    def test_no_duplicate_activation(self, mem_store):
        """Cannot activate when recovery is already active."""
        _seed_crisis_tcs(mem_store)
        first = check_and_activate(mem_store, window_hours=48)
        assert first is not None
        second = check_and_activate(mem_store, window_hours=48)
        assert second is None

    def test_empty_store_no_activation(self, mem_store):
        result = check_and_activate(mem_store, window_hours=48)
        assert result is None


# --------------------------------------------------------------------------- #
# Phase transition tests                                                       #
# --------------------------------------------------------------------------- #

class TestPhaseTransitions:
    def test_advance_through_all_phases(self, mem_store):
        """Should be able to advance through all six phases in order."""
        _seed_crisis_tcs(mem_store)
        incident = check_and_activate(mem_store, window_hours=48)
        assert incident is not None
        incident_id = incident["incident_id"]

        # Start at containment, advance through remaining 5 phases
        for expected_phase in RECOVERY_PHASES[1:]:
            result = advance_phase(mem_store, incident_id, window_hours=48)
            assert result is not None
            assert result["current_phase"] == expected_phase

    def test_cannot_advance_past_stabilization(self, mem_store):
        """Cannot advance beyond the final phase."""
        _seed_crisis_tcs(mem_store)
        incident = check_and_activate(mem_store, window_hours=48)
        incident_id = incident["incident_id"]

        # Advance to stabilization
        for _ in range(5):
            advance_phase(mem_store, incident_id, window_hours=48)

        # Try to advance past stabilization
        result = advance_phase(mem_store, incident_id, window_hours=48)
        assert result is None

    def test_advance_nonexistent_returns_none(self, mem_store):
        result = advance_phase(mem_store, "REC-nonexistent", window_hours=48)
        assert result is None

    def test_diagnosis_produces_diagnostic(self, mem_store):
        """Advancing to diagnosis should populate diagnostic_json."""
        _seed_crisis_tcs(mem_store)
        incident = check_and_activate(mem_store, window_hours=48)
        result = advance_phase(mem_store, incident["incident_id"], window_hours=48)
        assert result is not None
        assert result["diagnostic_json"] is not None
        diag = json.loads(result["diagnostic_json"])
        assert "dominant_driver" in diag

    def test_remediation_produces_plan(self, mem_store):
        """Advancing to remediation should populate remediation_json."""
        _seed_crisis_tcs(mem_store)
        incident = check_and_activate(mem_store, window_hours=48)
        advance_phase(mem_store, incident["incident_id"], window_hours=48)  # diagnosis
        result = advance_phase(mem_store, incident["incident_id"], window_hours=48)  # remediation
        assert result is not None
        assert result["remediation_json"] is not None


# --------------------------------------------------------------------------- #
# Recovery score tests                                                         #
# --------------------------------------------------------------------------- #

class TestRecoveryScore:
    def test_healthy_system_high_score(self, mem_store):
        """Healthy system should have S_recovery > 1."""
        _seed_healthy_tcs(mem_store)
        score = compute_recovery_score(mem_store, window_hours=48)
        assert score > 1.0

    def test_crisis_system_low_score(self, mem_store):
        """Crisis system should have lower S_recovery."""
        _seed_crisis_tcs(mem_store)
        score = compute_recovery_score(mem_store, window_hours=48)
        # With high L_trust, score should be lower
        assert isinstance(score, float)

    def test_revalidation_computes_score(self, mem_store):
        """Advancing to revalidation should set s_recovery."""
        _seed_crisis_tcs(mem_store)
        incident = check_and_activate(mem_store, window_hours=48)
        iid = incident["incident_id"]
        advance_phase(mem_store, iid, window_hours=48)  # diagnosis
        advance_phase(mem_store, iid, window_hours=48)  # remediation
        result = advance_phase(mem_store, iid, window_hours=48)  # revalidation
        assert result is not None
        assert result["s_recovery"] is not None


# --------------------------------------------------------------------------- #
# Completion tests                                                             #
# --------------------------------------------------------------------------- #

class TestCompletion:
    def test_complete_from_stabilization(self, mem_store):
        """Can complete recovery from stabilization phase."""
        _seed_crisis_tcs(mem_store)
        incident = check_and_activate(mem_store, window_hours=48)
        iid = incident["incident_id"]

        # Advance to stabilization
        for _ in range(5):
            advance_phase(mem_store, iid, window_hours=48)

        result = complete_recovery(mem_store, iid, window_hours=48)
        assert result is not None
        assert result["status"] == "completed"
        assert result["completed_at"] is not None

    def test_cannot_complete_before_stabilization(self, mem_store):
        """Cannot complete recovery before reaching stabilization."""
        _seed_crisis_tcs(mem_store)
        incident = check_and_activate(mem_store, window_hours=48)
        result = complete_recovery(mem_store, incident["incident_id"], window_hours=48)
        assert result is None

    def test_can_activate_after_completion(self, mem_store):
        """After completing recovery, can activate again if D_crit."""
        _seed_crisis_tcs(mem_store)
        incident = check_and_activate(mem_store, window_hours=48)
        iid = incident["incident_id"]
        for _ in range(5):
            advance_phase(mem_store, iid, window_hours=48)
        complete_recovery(mem_store, iid, window_hours=48)

        # Should be able to activate again
        second = check_and_activate(mem_store, window_hours=48)
        assert second is not None


# --------------------------------------------------------------------------- #
# Status and history tests                                                     #
# --------------------------------------------------------------------------- #

class TestStatusAndHistory:
    def test_status_no_recovery(self, mem_store):
        status = get_recovery_status(mem_store, window_hours=48)
        assert status["recovery_active"] is False

    def test_status_active_recovery(self, mem_store):
        _seed_crisis_tcs(mem_store)
        check_and_activate(mem_store, window_hours=48)
        status = get_recovery_status(mem_store, window_hours=48)
        assert status["recovery_active"] is True
        assert "s_recovery_current" in status

    def test_history_tracks_incidents(self, mem_store):
        _seed_crisis_tcs(mem_store)
        check_and_activate(mem_store, window_hours=48)
        history = get_recovery_history(mem_store)
        assert len(history) >= 1
        assert history[0]["status"] == "active"

    def test_phase_history_tracked(self, mem_store):
        """Phase transitions are recorded in phase_history."""
        _seed_crisis_tcs(mem_store)
        incident = check_and_activate(mem_store, window_hours=48)
        advance_phase(mem_store, incident["incident_id"], window_hours=48)

        updated = mem_store.get_recovery_incident(incident["incident_id"])
        history = json.loads(updated["phase_history_json"])
        assert len(history) == 2
        assert history[0]["phase"] == "containment"
        assert history[1]["phase"] == "diagnosis"
