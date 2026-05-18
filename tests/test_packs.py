"""
tests/test_packs.py
===================

Phase 3 Step 8 — Regulatory Pack Module tests.

Tests verify:
    1. All five packs are loadable and validatable
    2. Financial services pack sets correct r3/a4 baseline
    3. Pack deployment creates correct profile
    4. PLL cannot adapt below pack-defined floors
    5. Audit export generates correctly formatted output
    6. Pack API endpoints return correct data
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from tcs.persistence import CertificateStore
from tcs.api.app import create_app
from tcs.policy_profiles import PolicyProfile
from tcs.packs.pack_manager import (
    PACKS,
    list_packs,
    get_pack,
    deploy_pack,
    get_active_pack,
    get_active_pack_id,
    clear_active_pack,
    validate_against_pack,
    build_profile_from_pack,
    generate_audit_export,
)


# --------------------------------------------------------------------------- #
# Fixtures                                                                     #
# --------------------------------------------------------------------------- #

@pytest.fixture(autouse=True)
def _clean_active():
    clear_active_pack()
    yield
    clear_active_pack()


@pytest.fixture
def mem_store():
    store = CertificateStore(":memory:")
    yield store
    store.close()


@pytest.fixture
def client():
    store = CertificateStore(":memory:")
    app = create_app(store=store)
    c = TestClient(app)
    yield c
    store.close()


# --------------------------------------------------------------------------- #
# Pack registry tests                                                          #
# --------------------------------------------------------------------------- #

class TestPackRegistry:
    def test_five_packs_defined(self):
        assert len(PACKS) == 5

    def test_all_pack_ids(self):
        expected = {
            "financial_services", "healthcare_clinical",
            "gmp_quality", "enterprise_ops", "federal_public",
        }
        assert set(PACKS.keys()) == expected

    def test_list_packs_returns_summaries(self):
        packs = list_packs()
        assert len(packs) == 5
        for p in packs:
            assert "pack_id" in p
            assert "name" in p
            assert "version" in p
            assert "regulatory_references" in p

    def test_get_pack_valid(self):
        pack = get_pack("financial_services")
        assert pack is not None
        assert pack["pack_id"] == "financial_services"

    def test_get_pack_invalid(self):
        assert get_pack("nonexistent") is None


# --------------------------------------------------------------------------- #
# Financial services pack tests                                                #
# --------------------------------------------------------------------------- #

class TestFinancialServicesPack:
    def test_r3_a4_baseline(self):
        pack = get_pack("financial_services")
        cfg = pack["profile_config"]
        assert cfg["risk_tier"] == "r3"
        assert cfg["action_class"] == "a4"

    def test_all_four_gates(self):
        pack = get_pack("financial_services")
        cfg = pack["profile_config"]
        assert set(cfg["gate_set"]) == {"B", "A", "C", "K"}

    def test_theta_allow_at_085(self):
        pack = get_pack("financial_services")
        cfg = pack["profile_config"]
        assert cfg["decision_thresholds"]["theta_allow"] == 0.85

    def test_adaptation_floor(self):
        pack = get_pack("financial_services")
        floors = pack["adaptation_floors"]
        assert floors["theta_allow"] == 0.80

    def test_fail_closed(self):
        pack = get_pack("financial_services")
        assert pack["fail_behavior"] == "fail_closed"

    def test_tc_required_fields(self):
        pack = get_pack("financial_services")
        assert "suitability_determination" in pack["tc_required_fields"]

    def test_ct4_attribution_threshold(self):
        pack = get_pack("financial_services")
        assert pack["ct4_attribution_threshold"] == 0.93


# --------------------------------------------------------------------------- #
# Pack deployment tests                                                        #
# --------------------------------------------------------------------------- #

class TestDeployment:
    def test_deploy_pack(self):
        result = deploy_pack("financial_services")
        assert result["status"] == "deployed"
        assert result["pack_id"] == "financial_services"

    def test_active_pack_after_deploy(self):
        deploy_pack("financial_services")
        active = get_active_pack()
        assert active is not None
        assert active["pack_id"] == "financial_services"

    def test_no_active_pack_initially(self):
        assert get_active_pack() is None
        assert get_active_pack_id() is None

    def test_deploy_nonexistent_raises(self):
        with pytest.raises(KeyError):
            deploy_pack("nonexistent")

    def test_deploy_switches_active(self):
        deploy_pack("financial_services")
        deploy_pack("healthcare_clinical")
        assert get_active_pack_id() == "healthcare_clinical"


# --------------------------------------------------------------------------- #
# Profile building tests                                                       #
# --------------------------------------------------------------------------- #

class TestBuildProfile:
    def test_build_from_financial_pack(self):
        profile = build_profile_from_pack("financial_services")
        assert isinstance(profile, PolicyProfile)
        assert profile.risk_tier == "r3"
        assert profile.action_class == "a4"
        assert profile.decision_thresholds["theta_allow"] == 0.85

    def test_build_from_all_packs(self):
        for pack_id in PACKS:
            profile = build_profile_from_pack(pack_id)
            assert isinstance(profile, PolicyProfile)

    def test_build_nonexistent_raises(self):
        with pytest.raises(KeyError):
            build_profile_from_pack("nonexistent")


# --------------------------------------------------------------------------- #
# Validation tests                                                             #
# --------------------------------------------------------------------------- #

class TestValidation:
    def test_valid_thresholds(self):
        result = validate_against_pack("financial_services", {
            "theta_allow": 0.85,
            "theta_hold": 0.80,
            "theta_escalate": 0.70,
        })
        assert result["valid"] is True
        assert result["violations"] == []

    def test_below_floor_rejected(self):
        result = validate_against_pack("financial_services", {
            "theta_allow": 0.75,  # below 0.80 floor
        })
        assert result["valid"] is False
        assert len(result["violations"]) == 1

    def test_at_floor_accepted(self):
        result = validate_against_pack("financial_services", {
            "theta_allow": 0.80,  # exactly at floor
        })
        assert result["valid"] is True

    def test_nonexistent_pack(self):
        result = validate_against_pack("nonexistent", {"theta_allow": 0.85})
        assert result["valid"] is False


# --------------------------------------------------------------------------- #
# Audit export tests                                                           #
# --------------------------------------------------------------------------- #

class TestAuditExport:
    def test_export_financial(self, mem_store):
        export = generate_audit_export(mem_store, "financial_services", window_hours=48)
        assert export["export_format"] == "finra_examination_v2"
        assert export["pack_id"] == "financial_services"
        assert "summary" in export
        assert "compliance_status" in export

    def test_export_all_packs(self, mem_store):
        for pack_id in PACKS:
            export = generate_audit_export(mem_store, pack_id, window_hours=48)
            assert "export_format" in export
            assert export["pack_id"] == pack_id

    def test_export_nonexistent_raises(self, mem_store):
        with pytest.raises(KeyError):
            generate_audit_export(mem_store, "nonexistent")

    def test_export_serializable(self, mem_store):
        export = generate_audit_export(mem_store, "financial_services", window_hours=48)
        json.dumps(export)  # must not raise


# --------------------------------------------------------------------------- #
# API endpoint tests                                                           #
# --------------------------------------------------------------------------- #

class TestPackAPI:
    def test_list_packs(self, client):
        resp = client.get("/v2/packs")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 5

    def test_get_pack(self, client):
        resp = client.get("/v2/packs/financial_services")
        assert resp.status_code == 200
        data = resp.json()
        assert data["pack_id"] == "financial_services"

    def test_get_pack_not_found(self, client):
        resp = client.get("/v2/packs/nonexistent")
        assert resp.status_code == 404

    def test_deploy_pack(self, client):
        resp = client.post("/v2/packs/financial_services/deploy")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "deployed"

    def test_active_pack_none(self, client):
        resp = client.get("/v2/packs/active")
        assert resp.status_code == 200
        data = resp.json()
        assert data["active"] is False

    def test_active_pack_after_deploy(self, client):
        client.post("/v2/packs/financial_services/deploy")
        resp = client.get("/v2/packs/active")
        data = resp.json()
        assert data["active"] is True
        assert data["pack_id"] == "financial_services"

    def test_export_endpoint(self, client):
        resp = client.get("/v2/packs/financial_services/export?window_hours=48")
        assert resp.status_code == 200
        data = resp.json()
        assert data["export_format"] == "finra_examination_v2"
