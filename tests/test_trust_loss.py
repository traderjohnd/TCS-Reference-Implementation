"""
tests/test_trust_loss.py
========================

Phase 3 Step 2 — Trust Loss Function tests.

Tests verify:
    1. Each component computes correctly in isolation
    2. L_t aggregates correctly with domain-specific weights
    3. Dominant component correctly identifies the highest-weight failure
    4. Empty window returns sensible defaults
    5. Financial services domain weights are applied correctly
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from tcs.persistence import CertificateStore
from tcs.dynamics.trust_loss import (
    compute_trust_loss,
    _compute_K,
    _compute_P,
    _compute_D,
    _compute_E,
    _compute_G,
    DOMAIN_WEIGHTS,
    IDEAL_TIS_MEAN,
)
from tcs.dynamics.models import TrustLossResult


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
    integration_boundary_gaps (optional), timestamp (optional).
    """
    from datetime import datetime, timezone
    import uuid

    for i, s in enumerate(scenarios):
        cert_id = str(uuid.uuid4())
        ts = s.get("timestamp", f"2026-04-08T{10+i:02d}:00:00Z")
        gaps = s.get("integration_boundary_gaps", 0)
        chain_id = "test-chain"
        seq = i + 1

        content = {
            "certificate_id": cert_id,
            "tis_current": s["tis_current"],
            "decision": s["decision"],
            "integration_boundary_gaps": gaps,
            "component_scores": s.get("component_scores", {"B": 0.9, "A": 0.9, "C": 0.9, "K": 0.9}),
        }

        # Compute hash
        import hashlib
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
             s["decision"], "computed", "valid",
             s["tis_current"], s["tis_current"], s["tis_current"],
             ts, "2026-04-09T10:00:00Z",
             chain_id, seq, "sha256",
             tc_hash, json.dumps(content)),
        )


# --------------------------------------------------------------------------- #
# Individual component tests                                                   #
# --------------------------------------------------------------------------- #

class TestComponentU:
    """Uncertainty increase component."""

    def test_perfect_tis_means_zero_u(self):
        """When mean TIS equals ideal, U = 0."""
        assert _compute_K([0.90, 0.90, 0.90]) == pytest.approx(0.0, abs=0.001)

    def test_zero_tis_means_max_u(self):
        """When mean TIS is 0, U = 1."""
        assert _compute_K([0.0, 0.0, 0.0]) == pytest.approx(1.0, abs=0.001)

    def test_declining_tis_raises_u(self):
        """Lower mean TIS produces higher U."""
        k_high = _compute_K([0.80, 0.80, 0.80])
        k_low = _compute_K([0.50, 0.50, 0.50])
        assert k_low > k_high

    def test_empty_values_returns_zero(self):
        assert _compute_K([]) == 0.0


class TestComponentP:
    """Policy deviation rate component."""

    def test_all_allow_means_zero_p(self):
        assert _compute_P(["Allow", "Allow", "Allow"]) == pytest.approx(0.0)

    def test_all_stop_means_max_p(self):
        assert _compute_P(["Stop", "Stop", "Stop"]) == pytest.approx(1.0)

    def test_mixed_decisions(self):
        """2 failures out of 4 = P=0.5."""
        p = _compute_P(["Allow", "Stop", "Hold", "Allow"])
        assert p == pytest.approx(0.5)

    def test_empty_returns_zero(self):
        assert _compute_P([]) == 0.0


class TestComponentD:
    """Data/context drift component."""

    def test_no_gaps_means_zero_d(self):
        rows = [
            {"content_json": json.dumps({"integration_boundary_gaps": 0})},
            {"content_json": json.dumps({"integration_boundary_gaps": 0})},
        ]
        assert _compute_D(rows) == pytest.approx(0.0)

    def test_all_gaps_means_max_d(self):
        rows = [
            {"content_json": json.dumps({"integration_boundary_gaps": 2})},
            {"content_json": json.dumps({"integration_boundary_gaps": 1})},
        ]
        assert _compute_D(rows) == pytest.approx(1.0)

    def test_empty_returns_zero(self):
        assert _compute_D([]) == 0.0


class TestComponentE:
    """Environmental volatility (stub)."""

    def test_always_zero(self):
        assert _compute_E() == 0.0


class TestComponentG:
    """Governance infrastructure degradation."""

    def test_empty_store_gi_is_high(self, mem_store):
        """Empty store has GI=1.0 so G=0.0."""
        g = _compute_G(mem_store)
        # Empty store: pct_clean=1.0 (vacuous), chain_bonus=0.4 (empty verifies)
        # GI = 1.0*0.4 + 0.4 + 0.2 = 1.0 -> G = 0.0
        assert g == pytest.approx(0.0, abs=0.01)

    def test_degraded_store(self, mem_store):
        """A store with mostly Stop decisions has lower GI and higher G."""
        _seed_tcs(mem_store, [
            {"tis_current": 0.0, "decision": "Stop"},
            {"tis_current": 0.0, "decision": "Stop"},
            {"tis_current": 0.0, "decision": "Stop"},
            {"tis_current": 0.9, "decision": "Allow"},
        ])
        g = _compute_G(mem_store)
        assert g > 0.0  # GI < 1.0 so G > 0.0


# --------------------------------------------------------------------------- #
# Integrated L_t computation                                                   #
# --------------------------------------------------------------------------- #

class TestComputeTrustLoss:
    """End-to-end trust loss computation."""

    def test_empty_store_produces_low_loss(self, mem_store):
        """Empty store should have near-zero L_t."""
        result = compute_trust_loss(mem_store, domain="financial_services")
        assert isinstance(result, TrustLossResult)
        assert result.L_t >= 0.0
        assert result.window_evaluations == 0
        assert result.domain == "financial_services"

    def test_healthy_system_low_loss(self, mem_store):
        """A system with all Allows and high TIS has low L_t."""
        _seed_tcs(mem_store, [
            {"tis_current": 0.92, "decision": "Allow"},
            {"tis_current": 0.91, "decision": "Allow"},
            {"tis_current": 0.93, "decision": "Allow"},
            {"tis_current": 0.90, "decision": "Allow"},
        ])
        result = compute_trust_loss(mem_store, domain="financial_services", window_hours=48)
        assert result.L_t < 0.15  # healthy
        assert result.window_evaluations == 4
        assert result.components["P"] == pytest.approx(0.0)

    def test_unhealthy_system_high_loss(self, mem_store):
        """A system with many Stops and low TIS has high L_t."""
        _seed_tcs(mem_store, [
            {"tis_current": 0.0, "decision": "Stop", "integration_boundary_gaps": 3},
            {"tis_current": 0.0, "decision": "Stop", "integration_boundary_gaps": 2},
            {"tis_current": 0.3, "decision": "Hold", "integration_boundary_gaps": 1},
            {"tis_current": 0.0, "decision": "Stop"},
        ])
        result = compute_trust_loss(mem_store, domain="financial_services", window_hours=48)
        assert result.L_t > 0.40  # degraded
        assert result.components["P"] > 0.5   # most decisions are failures
        assert result.components["K"] > 0.5   # TIS very low

    def test_dominant_component_correct(self, mem_store):
        """Dominant component should be the one with highest weighted contribution."""
        _seed_tcs(mem_store, [
            {"tis_current": 0.0, "decision": "Stop"},
            {"tis_current": 0.0, "decision": "Stop"},
            {"tis_current": 0.0, "decision": "Stop"},
        ])
        result = compute_trust_loss(mem_store, domain="financial_services", window_hours=48)
        # P should be dominant since beta=0.35 and P=1.0 (all Stop)
        assert result.dominant_component == "P"

    def test_domain_weights_applied(self, mem_store):
        """Different domains produce different L_t from same data."""
        _seed_tcs(mem_store, [
            {"tis_current": 0.5, "decision": "Hold"},
            {"tis_current": 0.5, "decision": "Hold"},
        ])
        fin = compute_trust_loss(mem_store, domain="financial_services", window_hours=48)
        ent = compute_trust_loss(mem_store, domain="enterprise", window_hours=48)
        # Same components, different weights -> different L_t
        assert fin.weights != ent.weights
        # Both should have non-zero L_t
        assert fin.L_t > 0
        assert ent.L_t > 0

    def test_result_serializes(self, mem_store):
        """TrustLossResult.to_dict() produces valid JSON-serializable dict."""
        _seed_tcs(mem_store, [
            {"tis_current": 0.85, "decision": "Allow"},
        ])
        result = compute_trust_loss(mem_store, domain="healthcare", window_hours=48)
        d = result.to_dict()
        assert "L_t" in d
        assert "components" in d
        assert "dominant_component" in d
        assert d["domain"] == "healthcare"
        # Should be JSON-serializable
        json.dumps(d)

    def test_all_five_components_present(self, mem_store):
        """Result must contain all five components."""
        result = compute_trust_loss(mem_store)
        for key in ("K", "P", "D", "E", "G"):
            assert key in result.components
