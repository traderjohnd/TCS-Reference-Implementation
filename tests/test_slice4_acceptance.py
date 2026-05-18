"""
Slice 4 acceptance tests — Standards Library + Composer + Active Pack +
Governance Rule audit linkage.

These pin the contract the user requested for Slice 4. Many of the 10
acceptance criteria are also asserted in test_phase4_acceptance.py at a
higher level; this file targets the specific gaps that need their own
focused assertion:

  - C3: standards carry control_interpretation notes (and the language
        frames them as interpretation, not regulatory prescription)
  - C4: preview surfaces per-standard contributions (which standard
        contributed which adjustment)
  - C7: TC carries active pack's composer_metadata
  - C8: governance_rule_matches record the ACTIVE policy profile id,
        not just any base profile
  - C9: changing the active pack changes thresholds/gates/decisions

The "interpretation, not regulatory truth" framing is verified by both
the library payload (UI source) and the standalone interpretation
strings on each standard.
"""

from __future__ import annotations

import os
from typing import Any, Dict

import pytest
from fastapi.testclient import TestClient


# --------------------------------------------------------------------------- #
# Fixtures                                                                     #
# --------------------------------------------------------------------------- #

@pytest.fixture
def client():
    """Fresh TestClient per test with the workflow-trace path enabled."""
    os.environ["TCS_WORKFLOW_TRACE_ENABLED"] = "true"
    from tcs.api.app import create_app
    from tcs.packs.pack_manager import (
        PACKS, clear_active_pack, unregister_composed_pack,
    )
    pre = set(PACKS.keys())
    c = TestClient(create_app())
    with c:
        yield c
    # Teardown: remove anything this test added so the next test starts
    # clean. Critical because PACKS is a process-global registry.
    for pid in (set(PACKS.keys()) - pre):
        try:
            unregister_composed_pack(pid)
        except Exception:
            pass
    clear_active_pack()
    os.environ.pop("TCS_WORKFLOW_TRACE_ENABLED", None)


def _deploy_meddev(client) -> Dict[str, Any]:
    return client.post("/v2/standards/deploy", json={
        "industry": "life_sciences",
        "sub_industry": "medical_devices",
        "use_case": "clinical_decision_support",
        "standard_ids": ["iso_13485", "iso_14971", "iec_62304"],
        "risk_tier": "r3", "action_class": "a4",
        "pack_name": "Slice4 MedDev CDS",
    }).json()


def _deploy_finance(client) -> Dict[str, Any]:
    return client.post("/v2/standards/deploy", json={
        "industry": "financial_services",
        "sub_industry": "retail_advisory",
        "use_case": "investment_recommendation",
        "standard_ids": ["sec_reg_bi", "finra_2111"],
        "risk_tier": "r3", "action_class": "a4",
        "pack_name": "Slice4 Retail Advisory",
    }).json()


# --------------------------------------------------------------------------- #
# C3 — every standard carries a control_interpretation note that frames the   #
# adjustment as governance interpretation, NOT a regulatory mathematical      #
# prescription. The library endpoint surfaces it so the UI can display it.    #
# --------------------------------------------------------------------------- #

class TestC3_ControlInterpretation:
    def test_every_standard_in_library_carries_control_interpretation(self, client):
        r = client.get("/v2/standards/library").json()
        assert r["total"] >= 11, f"expected >= 11 standards, got {r['total']}"
        for s in r["standards"]:
            ci = s.get("control_interpretation")
            assert isinstance(ci, str) and ci.strip(), (
                f"standard {s['id']} missing control_interpretation"
            )

    def test_every_standard_uses_interpretive_language_not_regulatory_mandate(self, client):
        # Reviewer-facing strings must not claim regulatory mathematical
        # prescription. The composer UI already carries this disclaimer
        # (see PolicyControls.jsx). Every standard must use interpretive
        # language ("interpret", "emphasi...", "under this profile",
        # "this implementation", "governance") rather than mandatory
        # prescription. Catches the regression where someone writes
        # "ISO 13485 requires B threshold >= 0.95" instead of
        # "Under this profile, ISO 13485 is interpreted as emphasizing
        # boundedness controls."
        interpretive_tokens = (
            "interpret", "emphasi", "under this", "this implementation",
            "governance",
        )
        r = client.get("/v2/standards/library").json()
        offenders = []
        for s in r["standards"]:
            ci = (s.get("control_interpretation") or "").lower()
            if not any(tok in ci for tok in interpretive_tokens):
                offenders.append((s["id"], ci))
        assert not offenders, (
            "standards lack interpretive framing — these read as "
            f"regulatory mandates: {offenders}"
        )


# --------------------------------------------------------------------------- #
# C4 — Preview surfaces per-standard contributions. The composer's contri-    #
# bution block names which standard contributed each adjustment so a          #
# reviewer can see "ISO 13485 added control X; IEC 62304 added gate K".      #
# --------------------------------------------------------------------------- #

class TestC4_PerStandardPreview:
    def test_compose_returns_contributions_per_standard(self, client):
        r = client.post("/v2/standards/compose", json={
            "industry": "life_sciences",
            "sub_industry": "medical_devices",
            "use_case": "clinical_decision_support",
            "standard_ids": ["iso_13485", "iso_14971", "iec_62304"],
            "risk_tier": "r3", "action_class": "a4",
        }).json()

        composed = r["composed"]
        assert "contributions" in composed, "preview missing contributions block"
        contribs = {c["standard_id"]: c for c in composed["contributions"]}
        for sid in ("iso_13485", "iso_14971", "iec_62304"):
            assert sid in contribs, f"contribution for {sid} missing from preview"
        # At least one standard must materially contribute something
        # (threshold floor, gate dim, weight delta, control, or
        # prohibition). If a standard contributes nothing, the preview
        # is misleading and the rule should be re-checked.
        any_material = any(
            (c.get("threshold_floors_applied")
             or c.get("gate_dimensions_added")
             or c.get("weight_deltas_applied")
             or c.get("required_controls_added")
             or c.get("hard_prohibitions_added"))
            for c in composed["contributions"]
        )
        assert any_material, (
            "no standard in this composition contributed any adjustment — "
            "preview would have nothing to show"
        )


# --------------------------------------------------------------------------- #
# C7 — A TC issued under the active pack carries the pack's composer_metadata #
# (industry/sub_industry/use_case/standards/risk_tier/action_class/version).  #
# --------------------------------------------------------------------------- #

class TestC7_TCCarriesComposerMetadata:
    def test_tc_self_documents_active_standards_pack(self, client):
        deployed = _deploy_meddev(client)
        q = client.post("/v2/query", json={
            "query": "Routine policy lookup question.",
            "provider": "mock", "model": "deterministic",
        }).json()
        tc = client.get(f"/v2/certificates/{q['certificate_id']}").json()

        cm = tc.get("composer_metadata")
        assert cm is not None, "TC missing composer_metadata from active pack"
        # All Slice-4 fields are present and match what was deployed.
        assert cm["industry"] == "life_sciences"
        assert cm["sub_industry"] == "medical_devices"
        assert cm["use_case"] == "clinical_decision_support"
        assert set(cm["standards"]) == {"iso_13485", "iso_14971", "iec_62304"}
        assert cm["risk_tier"] == "r3"
        assert cm["action_class"] == "a4"
        assert cm.get("composition_rules_version"), (
            "composer_metadata missing composition_rules_version"
        )
        # And the TC's policy_set_id resolves to the deployed pack id.
        assert tc["policy_set_id"] == deployed["pack_id"]


# --------------------------------------------------------------------------- #
# C8 — governance_rule_matches record the ACTIVE policy profile id.           #
# A risk rule firing must reference whichever profile was active at eval      #
# time, not just any baseline profile. Tied to active pack flow.              #
# --------------------------------------------------------------------------- #

class TestC8_RuleMatchesRecordActivePackProfile:
    def test_rule_audit_references_active_pack_profile_id(self, client):
        deployed = _deploy_meddev(client)
        # Fire a consumer-self-dosing query — under the refined rule
        # set (path 1) this is a hard STOP. The resulting TC must
        # carry an audit entry whose active_policy_profile_id equals
        # the active pack's profile id.
        q = client.post("/v2/query", json={
            "query": (
                "I'm pregnant and want to know what dose of lithium to take"
            ),
            "provider": "mock", "model": "deterministic",
        }).json()
        assert q["decision"] == "Stop", (
            f"consumer self-dosing query did not Stop: {q['decision']}"
        )

        tc = client.get(f"/v2/certificates/{q['certificate_id']}").json()
        matches = tc.get("governance_rule_matches")
        assert matches, (
            "TC has no governance_rule_matches — classifier did not run or "
            "no rule fired for consumer self-dosing variant"
        )

        # Active pack id is what every match must reference. The pack
        # carries profile_config.profile_id; that's what shows up on
        # the TC and on each rule audit dict.
        active_profile_id = (
            deployed.get("profile_config", {}).get("profile_id")
            or tc["policy_set_id"]
        )
        for m in matches:
            assert m.get("active_policy_profile_id") == active_profile_id, (
                f"rule {m['rule_id']} audit references "
                f"{m.get('active_policy_profile_id')!r} but active pack is "
                f"{active_profile_id!r}"
            )
            # Audit shape must remain complete with the new fields.
            assert m.get("rule_version"), "rule audit missing rule_version"
            eff = m["effect"]
            # New three-class authoritative fields.
            assert eff.get("control_class"), "rule audit missing control_class"
            assert eff.get("safety_category"), (
                "rule audit missing safety_category — the new taxonomy field"
            )
            # Legacy c3_category mirror still emitted for back-compat.
            assert eff.get("c3_category"), "rule audit missing c3_category (legacy mirror)"


# --------------------------------------------------------------------------- #
# C9 — Changing the active pack changes thresholds/gates/outcomes.            #
# This is the load-bearing acceptance test for Slice 4. If swapping the       #
# active pack doesn't materially change governance, the pack system isn't     #
# actually wired into the engine.                                             #
# --------------------------------------------------------------------------- #

class TestC9_ActivePackChangesOutcome:
    def test_swapping_active_pack_changes_active_profile_id_and_composer_metadata(
        self, client,
    ):
        # Deploy MedDev → query → record active profile and CM on TC.
        meddev = _deploy_meddev(client)
        q1 = client.post("/v2/query", json={
            "query": "What is our document retention policy?",
            "provider": "mock", "model": "deterministic",
        }).json()
        tc1 = client.get(f"/v2/certificates/{q1['certificate_id']}").json()

        # Deploy Finance → same query → different active profile + CM.
        finance = _deploy_finance(client)
        q2 = client.post("/v2/query", json={
            "query": "What is our document retention policy?",
            "provider": "mock", "model": "deterministic",
        }).json()
        tc2 = client.get(f"/v2/certificates/{q2['certificate_id']}").json()

        # The two TCs must reflect the two different active packs.
        assert tc1["policy_set_id"] != tc2["policy_set_id"], (
            "policy_set_id did not change across active pack swap — "
            "active pack not flowing into evaluation"
        )
        assert tc1["composer_metadata"]["industry"] == "life_sciences"
        assert tc2["composer_metadata"]["industry"] == "financial_services"
        # And the standards lists differ because they came from different packs.
        assert set(tc1["composer_metadata"]["standards"]) != set(
            tc2["composer_metadata"]["standards"]
        )

    def test_swapping_active_pack_can_change_thresholds_or_gate_set(
        self, client,
    ):
        # Build two compositions that differ only in standards selection;
        # at least one of {thresholds, gate_set, weights} must differ.
        a = client.post("/v2/standards/compose", json={
            "industry": "life_sciences",
            "sub_industry": "medical_devices",
            "use_case": "clinical_decision_support",
            "standard_ids": ["iso_13485"],
            "risk_tier": "r3", "action_class": "a4",
        }).json()["composed"]
        b = client.post("/v2/standards/compose", json={
            "industry": "life_sciences",
            "sub_industry": "medical_devices",
            "use_case": "clinical_decision_support",
            "standard_ids": ["iso_13485", "iso_14971", "iec_62304"],
            "risk_tier": "r3", "action_class": "a4",
        }).json()["composed"]

        pa, pb = a["profile_config"], b["profile_config"]
        differs = (
            pa["thresholds"] != pb["thresholds"]
            or sorted(pa["gate_set"]) != sorted(pb["gate_set"])
            or pa["weights"] != pb["weights"]
            or pa["penalty_weights"] != pb["penalty_weights"]
        )
        assert differs, (
            "two different standard selections produced identical profiles — "
            "composer is not actually applying per-standard adjustments"
        )
