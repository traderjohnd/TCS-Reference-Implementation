"""
tests/test_drift.py
===================

Phase 3 Step 3 — Trust Drift Detection tests.

Tests verify:
    1. Each drift component computes correctly in isolation
    2. D_trust aggregates correctly with default weights
    3. All three threshold levels trigger correctly
    4. Trend detection works (increasing, stable, decreasing)
    5. Insufficient data returns zero drift
    6. Multiple contexts are computed independently
    7. API endpoint returns correct shape
"""

from __future__ import annotations

import json
import uuid

import pytest

from tcs.persistence import CertificateStore
from tcs.dynamics.drift import (
    compute_drift,
    _compute_drift_for_context,
    _mean,
    _stddev,
    _failure_rate,
    DEFAULT_DRIFT_WEIGHTS,
    DRIFT_THRESHOLDS,
    MIN_HALF_WINDOW,
)
from tcs.dynamics.models import DriftSignal


# --------------------------------------------------------------------------- #
# Fixtures                                                                     #
# --------------------------------------------------------------------------- #

@pytest.fixture
def mem_store():
    """In-memory CertificateStore for isolated testing."""
    store = CertificateStore(":memory:")
    yield store
    store.close()


def _seed_tcs(store, scenarios):
    """
    Seed the store with TC data via direct SQL for test isolation.

    Each scenario is a dict with: tis_current, decision,
    policy_set_id (optional), timestamp (optional),
    integration_boundary_gaps (optional).
    """
    import hashlib

    for i, s in enumerate(scenarios):
        cert_id = str(uuid.uuid4())
        ts = s.get("timestamp", f"2026-04-08T{10+i:02d}:00:00Z")
        gaps = s.get("integration_boundary_gaps", 0)
        policy = s.get("policy_set_id", "fin-r3-a4-ct4")
        chain_id = "test-chain"
        seq = i + 1

        content = {
            "certificate_id": cert_id,
            "tis_current": s["tis_current"],
            "decision": s["decision"],
            "integration_boundary_gaps": gaps,
            "component_scores": s.get("component_scores", {"B": 0.9, "A": 0.9, "C": 0.9, "K": 0.9}),
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
             s["decision"], "computed", "valid",
             s["tis_current"], s["tis_current"], s["tis_current"],
             ts, "2026-04-09T10:00:00Z",
             chain_id, seq, "sha256",
             tc_hash, json.dumps(content)),
        )


# --------------------------------------------------------------------------- #
# Statistical helper tests                                                     #
# --------------------------------------------------------------------------- #

class TestStatHelpers:
    def test_mean(self):
        assert _mean([1.0, 2.0, 3.0]) == pytest.approx(2.0)

    def test_stddev_uniform(self):
        assert _stddev([5.0, 5.0, 5.0]) == pytest.approx(0.0)

    def test_stddev_spread(self):
        # stddev of [0, 10] = 5.0 (population)
        assert _stddev([0.0, 10.0]) == pytest.approx(5.0)

    def test_stddev_single_value(self):
        assert _stddev([42.0]) == pytest.approx(0.0)

    def test_failure_rate_all_allow(self):
        assert _failure_rate(["Allow", "Allow"]) == pytest.approx(0.0)

    def test_failure_rate_all_stop(self):
        assert _failure_rate(["Stop", "Stop"]) == pytest.approx(1.0)

    def test_failure_rate_mixed(self):
        assert _failure_rate(["Allow", "Stop", "Observe", "Hold"]) == pytest.approx(0.5)


# --------------------------------------------------------------------------- #
# Drift component tests                                                        #
# --------------------------------------------------------------------------- #

class TestDriftComponents:
    """Test individual drift components via _compute_drift_for_context."""

    def test_no_drift_when_stable(self):
        """Identical halves produce zero drift."""
        rows = [
            {"tis_current": 0.85, "decision": "Allow", "evaluation_timestamp": f"2026-04-08T{10+i:02d}:00:00Z"}
            for i in range(6)
        ]
        signal = _compute_drift_for_context(rows, "ctx", 48.0, DEFAULT_DRIFT_WEIGHTS)
        assert signal.D_trust == pytest.approx(0.0, abs=0.001)
        assert signal.threshold_breached is None
        assert signal.trend == "stable"

    def test_level_drift_detected(self):
        """Declining TIS mean produces level drift."""
        rows = []
        # Earlier half: high TIS
        for i in range(4):
            rows.append({"tis_current": 0.90, "decision": "Allow",
                         "evaluation_timestamp": f"2026-04-08T{10+i:02d}:00:00Z"})
        # Later half: low TIS
        for i in range(4):
            rows.append({"tis_current": 0.50, "decision": "Allow",
                         "evaluation_timestamp": f"2026-04-08T{14+i:02d}:00:00Z"})
        signal = _compute_drift_for_context(rows, "ctx", 48.0, DEFAULT_DRIFT_WEIGHTS)
        assert signal.components["level"] == pytest.approx(0.40, abs=0.01)
        assert signal.D_trust > 0

    def test_variance_drift_detected(self):
        """Increasing score spread produces variance drift."""
        rows = []
        # Earlier half: tight scores
        for i in range(4):
            rows.append({"tis_current": 0.80, "decision": "Allow",
                         "evaluation_timestamp": f"2026-04-08T{10+i:02d}:00:00Z"})
        # Later half: spread scores
        vals = [0.50, 0.90, 0.40, 1.00]
        for i, v in enumerate(vals):
            rows.append({"tis_current": v, "decision": "Allow",
                         "evaluation_timestamp": f"2026-04-08T{14+i:02d}:00:00Z"})
        signal = _compute_drift_for_context(rows, "ctx", 48.0, DEFAULT_DRIFT_WEIGHTS)
        assert signal.components["variance"] > 0.1

    def test_failure_drift_detected(self):
        """Rising gate failure rate produces failure drift."""
        rows = []
        # Earlier half: all Allow
        for i in range(4):
            rows.append({"tis_current": 0.85, "decision": "Allow",
                         "evaluation_timestamp": f"2026-04-08T{10+i:02d}:00:00Z"})
        # Later half: all Stop
        for i in range(4):
            rows.append({"tis_current": 0.10, "decision": "Stop",
                         "evaluation_timestamp": f"2026-04-08T{14+i:02d}:00:00Z"})
        signal = _compute_drift_for_context(rows, "ctx", 48.0, DEFAULT_DRIFT_WEIGHTS)
        assert signal.components["failure"] == pytest.approx(1.0, abs=0.01)

    def test_failure_drift_only_counts_acceleration(self):
        """Improving failure rate (deceleration) contributes 0."""
        rows = []
        # Earlier half: all Stop
        for i in range(4):
            rows.append({"tis_current": 0.10, "decision": "Stop",
                         "evaluation_timestamp": f"2026-04-08T{10+i:02d}:00:00Z"})
        # Later half: all Allow
        for i in range(4):
            rows.append({"tis_current": 0.85, "decision": "Allow",
                         "evaluation_timestamp": f"2026-04-08T{14+i:02d}:00:00Z"})
        signal = _compute_drift_for_context(rows, "ctx", 48.0, DEFAULT_DRIFT_WEIGHTS)
        assert signal.components["failure"] == pytest.approx(0.0)


# --------------------------------------------------------------------------- #
# Threshold tests                                                              #
# --------------------------------------------------------------------------- #

class TestThresholds:
    def _make_signal(self, d_trust):
        """Helper to create a signal with a specific D_trust."""
        # Craft rows that produce the target D_trust via level drift only
        # D_trust = w1 * delta_mu => delta_mu = D_trust / w1
        delta_mu = d_trust / DEFAULT_DRIFT_WEIGHTS["w1"]
        high = 0.90
        low = high - delta_mu
        rows = []
        for i in range(4):
            rows.append({"tis_current": high, "decision": "Allow",
                         "evaluation_timestamp": f"2026-04-08T{10+i:02d}:00:00Z"})
        for i in range(4):
            rows.append({"tis_current": low, "decision": "Allow",
                         "evaluation_timestamp": f"2026-04-08T{14+i:02d}:00:00Z"})
        return _compute_drift_for_context(rows, "ctx", 48.0, DEFAULT_DRIFT_WEIGHTS)

    def test_below_warn_no_breach(self):
        signal = self._make_signal(0.015)
        assert signal.threshold_breached is None
        assert signal.recommendation is None

    def test_warn_threshold(self):
        signal = self._make_signal(0.025)
        assert signal.threshold_breached == "D_warn"
        assert signal.recommendation is None  # warn has no recommendation

    def test_alert_threshold(self):
        signal = self._make_signal(0.045)
        assert signal.threshold_breached == "D_alert"
        assert signal.recommendation == "pll_review"

    def test_crit_threshold(self):
        signal = self._make_signal(0.090)
        assert signal.threshold_breached == "D_crit"
        assert signal.recommendation == "recovery_activate"


# --------------------------------------------------------------------------- #
# Trend detection                                                              #
# --------------------------------------------------------------------------- #

class TestTrend:
    def test_declining_tis_is_increasing_drift(self):
        rows = []
        for i in range(4):
            rows.append({"tis_current": 0.90, "decision": "Allow",
                         "evaluation_timestamp": f"2026-04-08T{10+i:02d}:00:00Z"})
        for i in range(4):
            rows.append({"tis_current": 0.70, "decision": "Allow",
                         "evaluation_timestamp": f"2026-04-08T{14+i:02d}:00:00Z"})
        signal = _compute_drift_for_context(rows, "ctx", 48.0, DEFAULT_DRIFT_WEIGHTS)
        assert signal.trend == "increasing"

    def test_improving_tis_is_decreasing_drift(self):
        rows = []
        for i in range(4):
            rows.append({"tis_current": 0.70, "decision": "Allow",
                         "evaluation_timestamp": f"2026-04-08T{10+i:02d}:00:00Z"})
        for i in range(4):
            rows.append({"tis_current": 0.90, "decision": "Allow",
                         "evaluation_timestamp": f"2026-04-08T{14+i:02d}:00:00Z"})
        signal = _compute_drift_for_context(rows, "ctx", 48.0, DEFAULT_DRIFT_WEIGHTS)
        assert signal.trend == "decreasing"


# --------------------------------------------------------------------------- #
# Edge cases                                                                   #
# --------------------------------------------------------------------------- #

class TestEdgeCases:
    def test_insufficient_data_returns_zero(self):
        """Fewer than MIN_HALF_WINDOW per half -> zero drift."""
        rows = [
            {"tis_current": 0.85, "decision": "Allow",
             "evaluation_timestamp": "2026-04-08T10:00:00Z"},
        ]
        signal = _compute_drift_for_context(rows, "ctx", 48.0, DEFAULT_DRIFT_WEIGHTS)
        assert signal.D_trust == 0.0
        assert signal.trend == "stable"
        assert signal.threshold_breached is None

    def test_empty_rows(self):
        signal = _compute_drift_for_context([], "ctx", 48.0, DEFAULT_DRIFT_WEIGHTS)
        assert signal.D_trust == 0.0


# --------------------------------------------------------------------------- #
# Integrated compute_drift tests                                               #
# --------------------------------------------------------------------------- #

class TestComputeDrift:
    def test_empty_store_returns_empty(self, mem_store):
        signals = compute_drift(mem_store, window_hours=48)
        assert signals == []

    def test_single_context(self, mem_store):
        """Store with one context produces one signal."""
        _seed_tcs(mem_store, [
            {"tis_current": 0.90, "decision": "Allow"},
            {"tis_current": 0.90, "decision": "Allow"},
            {"tis_current": 0.85, "decision": "Allow"},
            {"tis_current": 0.85, "decision": "Allow"},
        ])
        signals = compute_drift(mem_store, window_hours=48)
        assert len(signals) == 1
        assert signals[0].context == "fin-r3-a4-ct4"
        assert isinstance(signals[0], DriftSignal)

    def test_multiple_contexts(self, mem_store):
        """Different policy_set_ids produce separate signals."""
        _seed_tcs(mem_store, [
            {"tis_current": 0.90, "decision": "Allow", "policy_set_id": "ctx-a"},
            {"tis_current": 0.90, "decision": "Allow", "policy_set_id": "ctx-a"},
            {"tis_current": 0.85, "decision": "Allow", "policy_set_id": "ctx-a"},
            {"tis_current": 0.85, "decision": "Allow", "policy_set_id": "ctx-a"},
            {"tis_current": 0.90, "decision": "Allow", "policy_set_id": "ctx-b"},
            {"tis_current": 0.90, "decision": "Allow", "policy_set_id": "ctx-b"},
            {"tis_current": 0.50, "decision": "Stop", "policy_set_id": "ctx-b"},
            {"tis_current": 0.50, "decision": "Stop", "policy_set_id": "ctx-b"},
        ])
        signals = compute_drift(mem_store, window_hours=48)
        assert len(signals) == 2
        # ctx-b has worse drift, should be first (sorted desc)
        assert signals[0].context == "ctx-b"
        assert signals[0].D_trust > signals[1].D_trust

    def test_degrading_context_triggers_alert(self, mem_store):
        """A severely degrading context should breach D_alert or D_crit."""
        _seed_tcs(mem_store, [
            {"tis_current": 0.90, "decision": "Allow"},
            {"tis_current": 0.92, "decision": "Allow"},
            {"tis_current": 0.88, "decision": "Allow"},
            {"tis_current": 0.91, "decision": "Allow"},
            # Later half: severe degradation
            {"tis_current": 0.30, "decision": "Stop"},
            {"tis_current": 0.25, "decision": "Stop"},
            {"tis_current": 0.20, "decision": "Stop"},
            {"tis_current": 0.15, "decision": "Stop"},
        ])
        signals = compute_drift(mem_store, window_hours=48)
        assert len(signals) == 1
        assert signals[0].threshold_breached is not None
        assert signals[0].D_trust > DRIFT_THRESHOLDS["D_alert"]

    def test_result_serializes(self, mem_store):
        """DriftSignal.to_dict() produces JSON-serializable dict."""
        _seed_tcs(mem_store, [
            {"tis_current": 0.90, "decision": "Allow"},
            {"tis_current": 0.90, "decision": "Allow"},
            {"tis_current": 0.85, "decision": "Allow"},
            {"tis_current": 0.85, "decision": "Allow"},
        ])
        signals = compute_drift(mem_store, window_hours=48)
        d = signals[0].to_dict()
        assert "D_trust" in d
        assert "components" in d
        assert "threshold_breached" in d
        json.dumps(d)  # must not raise

    def test_all_three_components_present(self, mem_store):
        """Each signal has all three drift components."""
        _seed_tcs(mem_store, [
            {"tis_current": 0.90, "decision": "Allow"},
            {"tis_current": 0.90, "decision": "Allow"},
            {"tis_current": 0.85, "decision": "Allow"},
            {"tis_current": 0.85, "decision": "Allow"},
        ])
        signals = compute_drift(mem_store, window_hours=48)
        for key in ("level", "variance", "failure"):
            assert key in signals[0].components
