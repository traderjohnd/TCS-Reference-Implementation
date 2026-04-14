"""
tests/test_pll.py
=================

Phase 3 Step 4 — Policy Learning Layer tests.

Tests verify:
    1. PLL does not fire before W_MIN evaluations
    2. PLL generates recommendation when drift exceeds D_alert
    3. Parameter changes respect epsilon_max bounds
    4. Approval workflow: pending -> approved -> applied
    5. Rejection workflow: pending -> rejected
    6. Rollback workflow: approved -> rolled_back
    7. Adaptation records persist and are queryable
    8. Gradient computation produces sensible direction
"""

from __future__ import annotations

import hashlib
import json
import uuid

import pytest

from tcs.persistence import CertificateStore
from tcs.policy_profiles import load_profile
from tcs.dynamics.pll import (
    generate_recommendation,
    approve_recommendation,
    reject_recommendation,
    rollback_recommendation,
    get_recommendations,
    _compute_gradient,
    _clamp_delta,
    EPSILON_MAX,
    W_MIN,
)
from tcs.dynamics.models import AdaptationRecommendation


# --------------------------------------------------------------------------- #
# Fixtures                                                                     #
# --------------------------------------------------------------------------- #

@pytest.fixture
def mem_store():
    store = CertificateStore(":memory:")
    yield store
    store.close()


@pytest.fixture
def fin_profile():
    return load_profile("fin-r3-a4-ct4")


def _seed_n_tcs(store, n, *, tis=0.85, decision="Allow", policy="fin-r3-a4-ct4"):
    """Seed n TCs with given parameters."""
    for i in range(n):
        cert_id = str(uuid.uuid4())
        ts = f"2026-04-08T{(i % 24):02d}:{(i // 24) % 60:02d}:00Z"
        content = {
            "certificate_id": cert_id,
            "tis_current": tis,
            "decision": decision,
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
             "r3", "a4", policy,
             decision, "computed", "valid",
             tis, tis, tis,
             ts, "2026-04-09T10:00:00Z",
             "test-chain", i + 1, "sha256",
             tc_hash, json.dumps(content)),
        )


def _seed_drifting_tcs(store, *, policy="fin-r3-a4-ct4"):
    """
    Seed TCs that produce drift: first half healthy, second half degraded.
    Total count > W_MIN to allow PLL to fire.
    """
    n = W_MIN + 20
    half = n // 2
    for i in range(n):
        cert_id = str(uuid.uuid4())
        ts = f"2026-04-08T{(i % 24):02d}:{(i // 24) % 60:02d}:00Z"

        if i < half:
            tis = 0.90
            decision = "Allow"
        else:
            tis = 0.30
            decision = "Stop"

        content = {
            "certificate_id": cert_id,
            "tis_current": tis,
            "decision": decision,
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
             "r3", "a4", policy,
             decision, "computed", "valid",
             tis, tis, tis,
             ts, "2026-04-09T10:00:00Z",
             "test-chain", i + 1, "sha256",
             tc_hash, json.dumps(content)),
        )


# --------------------------------------------------------------------------- #
# Gradient and clamping tests                                                  #
# --------------------------------------------------------------------------- #

class TestGradient:
    def test_high_u_pushes_gradient_positive(self):
        """High uncertainty should push thresholds up (tighten)."""
        grad = _compute_gradient(
            {"K": 0.8, "P": 0.0, "D": 0.0, "E": 0.0, "G": 0.0},
            {"level": 0.0, "variance": 0.0, "failure": 0.0},
        )
        assert grad["theta_allow"] > 0

    def test_high_p_pushes_gradient_negative(self):
        """High policy deviation should push thresholds down (loosen)."""
        grad = _compute_gradient(
            {"K": 0.0, "P": 0.8, "D": 0.0, "E": 0.0, "G": 0.0},
            {"level": 0.0, "variance": 0.0, "failure": 0.0},
        )
        assert grad["theta_allow"] < 0

    def test_hold_escalate_damped(self):
        """Hold and escalate gradients should be smaller than allow."""
        grad = _compute_gradient(
            {"K": 0.5, "P": 0.1, "D": 0.0, "E": 0.0, "G": 0.0},
            {"level": 0.1, "variance": 0.0, "failure": 0.0},
        )
        assert abs(grad["theta_hold"]) < abs(grad["theta_allow"])
        assert abs(grad["theta_escalate"]) < abs(grad["theta_hold"])


class TestClamping:
    def test_clamp_within_bounds(self):
        assert _clamp_delta(0.03) == 0.03

    def test_clamp_exceeds_max(self):
        assert _clamp_delta(0.10) == EPSILON_MAX

    def test_clamp_exceeds_min(self):
        assert _clamp_delta(-0.10) == -EPSILON_MAX


# --------------------------------------------------------------------------- #
# W_MIN guard tests                                                            #
# --------------------------------------------------------------------------- #

class TestWMinGuard:
    def test_below_wmin_returns_none(self, mem_store, fin_profile):
        """PLL should not fire with fewer than W_MIN evaluations."""
        _seed_n_tcs(mem_store, W_MIN - 1, tis=0.30, decision="Stop")
        result = generate_recommendation(
            mem_store, fin_profile, window_hours=48,
        )
        assert result is None

    def test_at_wmin_with_no_drift_returns_none(self, mem_store, fin_profile):
        """At W_MIN but no drift -> no recommendation."""
        _seed_n_tcs(mem_store, W_MIN + 10, tis=0.90, decision="Allow")
        result = generate_recommendation(
            mem_store, fin_profile, window_hours=48,
        )
        assert result is None


# --------------------------------------------------------------------------- #
# Recommendation generation                                                    #
# --------------------------------------------------------------------------- #

class TestGenerateRecommendation:
    def test_drift_alert_generates_recommendation(self, mem_store, fin_profile):
        """Sustained drift above D_alert should produce a recommendation."""
        _seed_drifting_tcs(mem_store)
        result = generate_recommendation(
            mem_store, fin_profile, window_hours=48,
        )
        assert result is not None
        assert isinstance(result, AdaptationRecommendation)
        assert result.approval_status == "pending"
        assert result.triggered_by == "drift_alert"
        assert len(result.parameter_changes) > 0

    def test_parameter_changes_within_epsilon_max(self, mem_store, fin_profile):
        """All parameter deltas must be within epsilon_max."""
        _seed_drifting_tcs(mem_store)
        result = generate_recommendation(
            mem_store, fin_profile, window_hours=48,
        )
        if result is not None:
            for param, change in result.parameter_changes.items():
                assert abs(change["delta"]) <= EPSILON_MAX + 1e-6

    def test_recommendation_persisted(self, mem_store, fin_profile):
        """Generated recommendation should be in the store."""
        _seed_drifting_tcs(mem_store)
        result = generate_recommendation(
            mem_store, fin_profile, window_hours=48,
        )
        assert result is not None
        recs = get_recommendations(mem_store, status="pending")
        assert len(recs) >= 1
        assert any(r["record_id"] == result.record_id for r in recs)

    def test_evidence_fields_populated(self, mem_store, fin_profile):
        """Evidence dict should contain D_trust, L_trust, window_evaluations."""
        _seed_drifting_tcs(mem_store)
        result = generate_recommendation(
            mem_store, fin_profile, window_hours=48,
        )
        assert result is not None
        assert "D_trust" in result.evidence
        assert "L_trust" in result.evidence
        assert "window_evaluations" in result.evidence


# --------------------------------------------------------------------------- #
# Approval workflow                                                            #
# --------------------------------------------------------------------------- #

class TestApprovalWorkflow:
    def test_approve_pending(self, mem_store, fin_profile):
        """Approve a pending recommendation."""
        _seed_drifting_tcs(mem_store)
        rec = generate_recommendation(mem_store, fin_profile, window_hours=48)
        assert rec is not None

        result = approve_recommendation(mem_store, rec.record_id, approver="admin")
        assert result is not None
        assert result["approval_status"] == "approved"
        assert result["approver_identity"] == "admin"
        assert result["applied_at"] is not None

    def test_reject_pending(self, mem_store, fin_profile):
        """Reject a pending recommendation."""
        _seed_drifting_tcs(mem_store)
        rec = generate_recommendation(mem_store, fin_profile, window_hours=48)
        assert rec is not None

        result = reject_recommendation(mem_store, rec.record_id, approver="admin")
        assert result is not None
        assert result["approval_status"] == "rejected"

    def test_approve_nonexistent_returns_none(self, mem_store):
        result = approve_recommendation(mem_store, "PAR-nonexistent")
        assert result is None

    def test_rollback_approved(self, mem_store, fin_profile):
        """Rollback an approved recommendation within the window."""
        _seed_drifting_tcs(mem_store)
        rec = generate_recommendation(mem_store, fin_profile, window_hours=48)
        assert rec is not None

        approve_recommendation(mem_store, rec.record_id)
        result = rollback_recommendation(mem_store, rec.record_id)
        assert result is not None
        assert result["approval_status"] == "rolled_back"

    def test_rollback_unapproved_returns_none(self, mem_store, fin_profile):
        """Cannot rollback a pending recommendation."""
        _seed_drifting_tcs(mem_store)
        rec = generate_recommendation(mem_store, fin_profile, window_hours=48)
        assert rec is not None

        result = rollback_recommendation(mem_store, rec.record_id)
        assert result is None  # still pending, can't rollback


# --------------------------------------------------------------------------- #
# Query / history                                                              #
# --------------------------------------------------------------------------- #

class TestHistory:
    def test_list_by_status(self, mem_store, fin_profile):
        _seed_drifting_tcs(mem_store)
        rec = generate_recommendation(mem_store, fin_profile, window_hours=48)
        assert rec is not None

        pending = get_recommendations(mem_store, status="pending")
        assert len(pending) >= 1

        approved = get_recommendations(mem_store, status="approved")
        assert len(approved) == 0

    def test_list_all(self, mem_store, fin_profile):
        _seed_drifting_tcs(mem_store)
        rec = generate_recommendation(mem_store, fin_profile, window_hours=48)
        assert rec is not None
        approve_recommendation(mem_store, rec.record_id)

        all_recs = get_recommendations(mem_store)
        assert len(all_recs) >= 1
        assert all_recs[0]["approval_status"] == "approved"

    def test_parsed_json_fields(self, mem_store, fin_profile):
        """parameter_changes and evidence should be parsed dicts."""
        _seed_drifting_tcs(mem_store)
        rec = generate_recommendation(mem_store, fin_profile, window_hours=48)
        assert rec is not None

        recs = get_recommendations(mem_store)
        assert isinstance(recs[0]["parameter_changes"], dict)
        assert isinstance(recs[0]["evidence"], dict)
