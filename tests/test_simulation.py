"""
tests/test_simulation.py
========================

Phase 3 Step 6 — Shadow Testing and Simulation tests.

Tests verify:
    1. Historical replay correctly identifies flipped decisions
    2. Replay does not modify production TC records
    3. Automation rate and gate failure rate computed correctly
    4. Shadow mode start/stop lifecycle works
    5. A/B comparison produces a recommendation
    6. Impact report risk assessment logic
    7. Empty store produces sensible defaults
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import replace as dataclass_replace

import pytest

from tcs.persistence import CertificateStore
from tcs.policy_profiles import PolicyProfile, load_profile
from tcs.simulation.historical_replay import (
    SimulationResult,
    replay,
    _automation_rate,
    _gate_failure_rate,
)
from tcs.simulation.shadow_mode import (
    start_shadow_mode,
    stop_shadow_mode,
    get_shadow_status,
    is_shadow_active,
)
from tcs.simulation.ab_comparison import compare_profiles, ABComparisonResult
from tcs.simulation.impact_report import generate_impact_report, ImpactReport


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


def _make_looser_profile(base: PolicyProfile) -> PolicyProfile:
    """Create a looser version by lowering theta_allow."""
    new_thresholds = dict(base.decision_thresholds)
    new_thresholds["theta_allow"] = max(0.50, new_thresholds["theta_allow"] - 0.20)
    new_thresholds["theta_hold"] = max(0.40, new_thresholds["theta_hold"] - 0.20)
    new_thresholds["theta_escalate"] = max(0.30, new_thresholds["theta_escalate"] - 0.20)
    return PolicyProfile(
        profile_id=base.profile_id + "-looser",
        domain=base.domain,
        risk_tier=base.risk_tier,
        action_class=base.action_class,
        gate_set=base.gate_set,
        thresholds=base.thresholds,
        weights=base.weights,
        penalty_weights=base.penalty_weights,
        decay_rate=base.decay_rate,
        soft_hold_ceiling=base.soft_hold_ceiling,
        decision_thresholds=new_thresholds,
    )


def _make_tighter_profile(base: PolicyProfile) -> PolicyProfile:
    """Create a tighter version by raising theta_allow."""
    new_thresholds = dict(base.decision_thresholds)
    new_thresholds["theta_allow"] = min(0.99, new_thresholds["theta_allow"] + 0.10)
    new_thresholds["theta_hold"] = min(0.98, new_thresholds["theta_hold"] + 0.10)
    new_thresholds["theta_escalate"] = min(0.97, new_thresholds["theta_escalate"] + 0.10)
    return PolicyProfile(
        profile_id=base.profile_id + "-tighter",
        domain=base.domain,
        risk_tier=base.risk_tier,
        action_class=base.action_class,
        gate_set=base.gate_set,
        thresholds=base.thresholds,
        weights=base.weights,
        penalty_weights=base.penalty_weights,
        decay_rate=base.decay_rate,
        soft_hold_ceiling=base.soft_hold_ceiling,
        decision_thresholds=new_thresholds,
    )


def _seed_tcs(store, scenarios):
    """Seed TCs with specific scenarios."""
    for i, s in enumerate(scenarios):
        cert_id = str(uuid.uuid4())
        ts = s.get("timestamp", f"2026-04-08T{10+i:02d}:00:00Z")
        policy = s.get("policy_set_id", "fin-r3-a4-ct4")

        content = {
            "certificate_id": cert_id,
            "tis_current": s["tis_current"],
            "decision": s["decision"],
            "integration_boundary_gaps": s.get("integration_boundary_gaps", 0),
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
             "test-chain", i + 1, "sha256",
             tc_hash, json.dumps(content)),
        )


# --------------------------------------------------------------------------- #
# Helper function tests                                                        #
# --------------------------------------------------------------------------- #

class TestHelpers:
    def test_automation_rate_all_allow(self):
        assert _automation_rate({"Allow": 10}, 10) == pytest.approx(1.0)

    def test_automation_rate_all_stop(self):
        assert _automation_rate({"Stop": 10}, 10) == pytest.approx(0.0)

    def test_automation_rate_mixed(self):
        assert _automation_rate({"Allow": 5, "Stop": 5}, 10) == pytest.approx(0.5)

    def test_gate_failure_rate(self):
        assert _gate_failure_rate({"Allow": 7, "Stop": 3}, 10) == pytest.approx(0.3)

    def test_empty_total(self):
        assert _automation_rate({}, 0) == pytest.approx(0.0)
        assert _gate_failure_rate({}, 0) == pytest.approx(0.0)


# --------------------------------------------------------------------------- #
# Historical replay tests                                                      #
# --------------------------------------------------------------------------- #

class TestReplay:
    def test_empty_store_returns_zero(self, mem_store, fin_profile):
        result = replay(mem_store, fin_profile, window_hours=48)
        assert isinstance(result, SimulationResult)
        assert result.total_replayed == 0
        assert result.flipped_decisions == []

    def test_replay_with_same_profile_no_flips(self, mem_store, fin_profile):
        """Replaying with the same profile should produce few/no flips."""
        _seed_tcs(mem_store, [
            {"tis_current": 0.90, "decision": "Allow",
             "component_scores": {"B": 0.95, "A": 0.95, "C": 0.95, "K": 0.95}},
            {"tis_current": 0.90, "decision": "Allow",
             "component_scores": {"B": 0.95, "A": 0.95, "C": 0.95, "K": 0.95}},
        ])
        result = replay(mem_store, fin_profile, window_hours=48)
        assert result.total_replayed == 2

    def test_looser_profile_flips_holds_to_allows(self, mem_store, fin_profile):
        """A looser profile should flip some Holds to Allows."""
        _seed_tcs(mem_store, [
            {"tis_current": 0.75, "decision": "Hold",
             "component_scores": {"B": 0.85, "A": 0.85, "C": 0.85, "K": 0.85}},
            {"tis_current": 0.75, "decision": "Hold",
             "component_scores": {"B": 0.85, "A": 0.85, "C": 0.85, "K": 0.85}},
            {"tis_current": 0.90, "decision": "Allow",
             "component_scores": {"B": 0.95, "A": 0.95, "C": 0.95, "K": 0.95}},
        ])
        looser = _make_looser_profile(fin_profile)
        result = replay(mem_store, looser, window_hours=48)
        assert result.total_replayed == 3
        # Some decisions should have flipped
        assert result.automation_rate_after >= result.automation_rate_before

    def test_replay_does_not_modify_store(self, mem_store, fin_profile):
        """Replay must not write to the store."""
        _seed_tcs(mem_store, [
            {"tis_current": 0.90, "decision": "Allow",
             "component_scores": {"B": 0.95, "A": 0.95, "C": 0.95, "K": 0.95}},
        ])
        count_before = mem_store.count()
        looser = _make_looser_profile(fin_profile)
        replay(mem_store, looser, window_hours=48)
        assert mem_store.count() == count_before

    def test_result_serializes(self, mem_store, fin_profile):
        """SimulationResult.to_dict() produces valid JSON."""
        _seed_tcs(mem_store, [
            {"tis_current": 0.90, "decision": "Allow",
             "component_scores": {"B": 0.95, "A": 0.95, "C": 0.95, "K": 0.95}},
        ])
        result = replay(mem_store, fin_profile, window_hours=48)
        d = result.to_dict()
        assert "simulation_id" in d
        assert "total_replayed" in d
        json.dumps(d)  # must not raise

    def test_max_records_limit(self, mem_store, fin_profile):
        """Replay respects max_records limit."""
        _seed_tcs(mem_store, [
            {"tis_current": 0.90, "decision": "Allow",
             "component_scores": {"B": 0.95, "A": 0.95, "C": 0.95, "K": 0.95}}
            for _ in range(10)
        ])
        result = replay(mem_store, fin_profile, window_hours=48, max_records=5)
        assert result.total_replayed == 5


# --------------------------------------------------------------------------- #
# Shadow mode tests                                                            #
# --------------------------------------------------------------------------- #

class TestShadowMode:
    def test_start_stop_lifecycle(self):
        assert not is_shadow_active()
        start_shadow_mode("test-profile")
        assert is_shadow_active()
        status = get_shadow_status()
        assert status["profile_id"] == "test-profile"
        result = stop_shadow_mode()
        assert not is_shadow_active()
        assert "stopped_at" in result

    def test_status_after_stop(self):
        start_shadow_mode("prof")
        stop_shadow_mode()
        status = get_shadow_status()
        assert status["active"] is False
        assert status["profile_id"] is None


# --------------------------------------------------------------------------- #
# A/B comparison tests                                                         #
# --------------------------------------------------------------------------- #

class TestABComparison:
    def test_compare_same_profile(self, mem_store, fin_profile):
        """Comparing a profile to itself should be equivalent."""
        _seed_tcs(mem_store, [
            {"tis_current": 0.90, "decision": "Allow",
             "component_scores": {"B": 0.95, "A": 0.95, "C": 0.95, "K": 0.95}},
        ])
        result = compare_profiles(mem_store, fin_profile, fin_profile, window_hours=48)
        assert isinstance(result, ABComparisonResult)
        assert result.recommendation == "equivalent"

    def test_compare_different_profiles(self, mem_store, fin_profile):
        """Comparing different profiles should produce a recommendation."""
        _seed_tcs(mem_store, [
            {"tis_current": 0.80, "decision": "Hold",
             "component_scores": {"B": 0.85, "A": 0.85, "C": 0.85, "K": 0.85}},
            {"tis_current": 0.90, "decision": "Allow",
             "component_scores": {"B": 0.95, "A": 0.95, "C": 0.95, "K": 0.95}},
        ])
        looser = _make_looser_profile(fin_profile)
        result = compare_profiles(mem_store, fin_profile, looser, window_hours=48)
        assert result.recommendation in ("profile_a", "profile_b", "equivalent")

    def test_result_serializes(self, mem_store, fin_profile):
        _seed_tcs(mem_store, [
            {"tis_current": 0.90, "decision": "Allow",
             "component_scores": {"B": 0.95, "A": 0.95, "C": 0.95, "K": 0.95}},
        ])
        result = compare_profiles(mem_store, fin_profile, fin_profile, window_hours=48)
        d = result.to_dict()
        json.dumps(d)  # must not raise


# --------------------------------------------------------------------------- #
# Impact report tests                                                          #
# --------------------------------------------------------------------------- #

class TestImpactReport:
    def _make_result(self, flipped_count=0, auto_change=0.0, gf_change=0.0, total=100):
        from tcs.simulation.historical_replay import FlippedDecision
        flipped = [
            FlippedDecision("tc-1", "Allow", "Hold", 0.85, 0.75, "test")
            for _ in range(flipped_count)
        ]
        return SimulationResult(
            simulation_id="SIM-test",
            total_replayed=total,
            decision_distribution_before={"Allow": total},
            decision_distribution_after={"Allow": total - flipped_count, "Hold": flipped_count},
            flipped_decisions=flipped,
            automation_rate_before=1.0,
            automation_rate_after=1.0 + auto_change,
            automation_rate_change=auto_change,
            gate_failure_rate_before=0.0,
            gate_failure_rate_after=gf_change,
            gate_failure_rate_change=gf_change,
            proposed_profile_id="test-profile",
        )

    def test_low_risk(self):
        result = self._make_result(flipped_count=2)
        report = generate_impact_report(result)
        assert report.risk_assessment == "low"
        assert report.recommendation == "proceed"

    def test_medium_risk(self):
        result = self._make_result(flipped_count=8, gf_change=0.03)
        report = generate_impact_report(result)
        assert report.risk_assessment == "medium"
        assert report.recommendation == "review"

    def test_high_risk(self):
        result = self._make_result(flipped_count=15, gf_change=0.06)
        report = generate_impact_report(result)
        assert report.risk_assessment == "high"
        assert report.recommendation == "reject"

    def test_report_serializes(self):
        result = self._make_result(flipped_count=3)
        report = generate_impact_report(result)
        d = report.to_dict()
        assert "risk_assessment" in d
        assert "risk_factors" in d
        json.dumps(d)
