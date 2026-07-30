"""
tis-v2 Commit 3 — frozen v1 hash behavior + schema-version dispatch.

Two distinct version-1 contracts, tested separately and never conflated:

    Raw stored-v1 verification
        A stored legacy record is verified from its ORIGINAL persisted
        dictionary before rehydration. Absence of
        ``certificate_schema_version`` is the historical wire contract;
        an injected version key is rejected, and any other injected
        field changes the payload and fails verification.

    Post-rehydration v1 reconstruction
        A rehydrated / in-memory v1 representation may carry explicit
        integer ``certificate_schema_version = 1`` as dispatch metadata.
        The frozen projection excludes it — and every v2-only field —
        from the reconstructed historical payload.

The fixture records in ``tests/fixtures/legacy_tc_v1.json`` are ACTUAL
stored certificates captured verbatim from the development archive
(synthetic Phase 4 demo data, inspected for sensitive content before
commit). They prove the frozen serializer agrees with history, not with
itself. If any test in this module fails after Commit 3, treat it as
the legacy-corruption signal: STOP and investigate before proceeding.

The v1 allowlist tests are PERMANENT. Commit 4 adds model and
serialization fields, but it must not require modification of the
frozen v1 field contract or these tests.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from tcs.canonical import (
    CertificateInvariantError,
    UnsupportedCertificateSchemaVersion,
)
from tcs.decision_engine import map_decision
from tcs.persistence import CertificateStore
from tcs.persistence.certificate_store import _tc_from_json
from tcs.tis_engine import compute_tis
from tcs.trust_certificate import (
    V1_HASH_FIELD_SET,
    V1_OPTIONAL_HASH_FIELDS,
    V1_REQUIRED_HASH_FIELDS,
    build_hash_payload,
    build_legacy_raw_hash_payload,
    build_v1_hash_payload,
    classify_certificate_schema_version,
    compute_legacy_raw_tc_hash,
    compute_tc_hash,
    generate_certificate,
)

from tests.conftest import make_tis_input

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "legacy_tc_v1.json"


def load_fixture_records():
    with open(FIXTURE_PATH, encoding="utf-8") as f:
        return json.load(f)["records"]


FIXTURE_RECORDS = load_fixture_records()
FIXTURE_IDS = [r["certificate_id"][:8] for r in FIXTURE_RECORDS]

# Representative Commit 4 v2-only fields. Injecting these into an
# in-memory representation must NOT alter the reconstructed v1 payload.
REPRESENTATIVE_V2_FIELDS = {
    "component_scores_observed": {"B": "0.9400"},
    "component_scores_raw": {"B": "0.94"},
    "adjustments_applied": [{"rule_id": "TCS_SPEC_19_1"}],
    "calculation_version": "tis-v2",
    "score_precision_policy": "decimal-4dp-half-up-each-decision-stage-context28-v1",
    "decay_algorithm_version": "decimal-exp-context28-half-even-then-4dp-half-up-v1",
    "resolved_theta_hold": "0.8500",
    "resolved_penalty_weights": {"cb": "0.2500"},
}


def _fresh_tc(chain_id="chain-freeze-test", subject_id="freeze-001"):
    inp = make_tis_input(
        "fin-high-risk-suitability-v3",
        {"B": 0.94, "A": 0.90, "C": 0.92, "K": 0.83},
        subject_id=subject_id,
        context_metadata={"chain_id": chain_id},
    )
    r = compute_tis(inp)
    d, review = map_decision(inp, r)
    return generate_certificate(inp, r, d, review)


# =========================================================================== #
# Raw stored-v1 verification                                                   #
# =========================================================================== #

class TestRawLegacyVerification:
    @pytest.mark.parametrize("record", FIXTURE_RECORDS, ids=FIXTURE_IDS)
    def test_stored_fixture_verifies_from_untouched_raw_dict(self, record):
        raw = json.loads(record["content_json"])
        assert compute_legacy_raw_tc_hash(raw) == record["tc_hash"]

    @pytest.mark.parametrize("record", FIXTURE_RECORDS, ids=FIXTURE_IDS)
    def test_independent_algorithm_replication(self, record):
        # Deliberately does NOT use the production helpers: replicate
        # the frozen historical algorithm inline and compare.
        raw = json.loads(record["content_json"])
        content = {k: v for k, v in raw.items() if k != "audit_integrity"}
        canonical = json.dumps(
            content, sort_keys=True, separators=(",", ":")
        )
        assert hashlib.sha256(
            canonical.encode("utf-8")
        ).hexdigest() == record["tc_hash"]

    @pytest.mark.parametrize("record", FIXTURE_RECORDS, ids=FIXTURE_IDS)
    def test_injected_field_in_raw_content_changes_payload(self, record):
        raw = json.loads(record["content_json"])
        raw["injected_field"] = "tampered"
        # Not silently ignored: the payload changes, verification fails.
        assert compute_legacy_raw_tc_hash(raw) != record["tc_hash"]

    @pytest.mark.parametrize("record", FIXTURE_RECORDS, ids=FIXTURE_IDS)
    def test_injected_explicit_version_1_rejected_in_raw_content(
        self, record,
    ):
        # Absence is the historical wire contract. A stored raw dict
        # modified to add explicit version 1 must NOT verify
        # byte-identically to the original stored record — it is
        # rejected as not matching the historical v1 wire shape.
        raw = json.loads(record["content_json"])
        raw["certificate_schema_version"] = 1
        with pytest.raises(CertificateInvariantError):
            build_legacy_raw_hash_payload(raw)

    def test_injected_version_2_also_rejected_in_raw_content(self):
        raw = json.loads(FIXTURE_RECORDS[0]["content_json"])
        raw["certificate_schema_version"] = 2
        with pytest.raises(CertificateInvariantError):
            build_legacy_raw_hash_payload(raw)


# =========================================================================== #
# Schema-version dispatch                                                      #
# =========================================================================== #

class TestSchemaVersionDispatch:
    def test_absence_selects_v1(self):
        raw = json.loads(FIXTURE_RECORDS[0]["content_json"])
        assert build_hash_payload(raw) == build_v1_hash_payload(raw)
        assert compute_tc_hash(raw) == FIXTURE_RECORDS[0]["tc_hash"]

    def test_internal_explicit_1_selects_v1_and_is_not_hashed(self):
        # An internal reconstructed dict with explicit integer 1
        # dispatches to v1 and produces the SAME reconstructed
        # historical payload as the equivalent dict without the key.
        base = json.loads(FIXTURE_RECORDS[0]["content_json"])
        with_key = dict(base)
        with_key["certificate_schema_version"] = 1
        assert build_hash_payload(with_key) == build_hash_payload(base)
        assert compute_tc_hash(with_key) == FIXTURE_RECORDS[0]["tc_hash"]

    def test_version_2_marked_v1_content_fails_closed(self):
        # Since Commit 4 the dispatcher routes version 2 to the real v2
        # payload builder, whose exact-schema validation rejects
        # v1-shaped content outright — still fail-closed, now with the
        # schema-mismatch invariant error. A v1 record can never
        # masquerade as v2.
        d = json.loads(FIXTURE_RECORDS[0]["content_json"])
        d["certificate_schema_version"] = 2
        with pytest.raises(CertificateInvariantError):
            build_hash_payload(d)
        with pytest.raises(CertificateInvariantError):
            compute_tc_hash(d)

    @pytest.mark.parametrize(
        "bad", [0, 3, -1, True, False, "1", "2", None, 1.0],
    )
    def test_unsupported_versions_raise_without_coercion(self, bad):
        d = {"certificate_schema_version": bad}
        with pytest.raises(UnsupportedCertificateSchemaVersion):
            classify_certificate_schema_version(d)

    def test_classification_values(self):
        assert classify_certificate_schema_version({}) == 1
        assert classify_certificate_schema_version(
            {"certificate_schema_version": 1}
        ) == 1
        assert classify_certificate_schema_version(
            {"certificate_schema_version": 2}
        ) == 2


# =========================================================================== #
# Permanent frozen v1 field contract                                           #
# =========================================================================== #

class TestV1FieldContract:
    """PERMANENT tests. Commit 4 must not require amending them."""

    def test_contract_set_relationships(self):
        assert V1_HASH_FIELD_SET == (
            V1_REQUIRED_HASH_FIELDS | V1_OPTIONAL_HASH_FIELDS
        )
        assert V1_OPTIONAL_HASH_FIELDS == frozenset()
        assert "audit_integrity" not in V1_HASH_FIELD_SET
        assert "certificate_schema_version" not in V1_HASH_FIELD_SET

    @pytest.mark.parametrize("record", FIXTURE_RECORDS, ids=FIXTURE_IDS)
    def test_every_historical_field_recognized(self, record):
        raw = json.loads(record["content_json"])
        historical_keys = set(raw) - {"audit_integrity"}
        # Every stored field (excluding the audit layer) is recognized
        # by the frozen contract, and every required field is present.
        assert historical_keys <= V1_HASH_FIELD_SET
        assert V1_REQUIRED_HASH_FIELDS <= set(raw)

    @pytest.mark.parametrize("record", FIXTURE_RECORDS, ids=FIXTURE_IDS)
    def test_projection_emits_no_key_outside_contract(self, record):
        raw = json.loads(record["content_json"])
        raw.update(REPRESENTATIVE_V2_FIELDS)   # would leak if unprojected
        payload_keys = set(json.loads(build_v1_hash_payload(raw)))
        assert payload_keys <= V1_HASH_FIELD_SET

    def test_optional_field_omission_preserved_not_defaulted(self):
        # The projection preserves presence/absence: a key absent from
        # the input stays absent — no default is injected to "complete"
        # the payload. (Whether an incomplete v1 payload should SEAL is
        # a separate concern; the projection itself must not repair.)
        raw = json.loads(FIXTURE_RECORDS[0]["content_json"])
        del raw["composer_metadata"]
        payload = json.loads(build_v1_hash_payload(raw))
        assert "composer_metadata" not in payload

    @pytest.mark.parametrize("record", FIXTURE_RECORDS, ids=FIXTURE_IDS)
    def test_v2_fields_do_not_alter_reconstructed_v1_payload(self, record):
        base = json.loads(record["content_json"])
        augmented = dict(base)
        augmented.update(REPRESENTATIVE_V2_FIELDS)
        assert build_v1_hash_payload(augmented) == \
            build_v1_hash_payload(base)
        # And the reconstructed hash still matches the stored one.
        assert hashlib.sha256(
            build_v1_hash_payload(augmented)
        ).hexdigest() == record["tc_hash"]


# =========================================================================== #
# Rehydration + reconstruction                                                 #
# =========================================================================== #

class TestRehydrationReconstruction:
    @pytest.mark.parametrize("record", FIXTURE_RECORDS, ids=FIXTURE_IDS)
    def test_rehydrated_fixture_reproduces_stored_hash(self, record):
        tc = _tc_from_json(record["content_json"])
        assert compute_tc_hash(tc.to_dict()) == record["tc_hash"]

    @pytest.mark.parametrize("record", FIXTURE_RECORDS, ids=FIXTURE_IDS)
    def test_explicit_version_1_completes_rehydration(self, record):
        # Explicit integer 1 is accepted as internal dispatch metadata
        # after the raw-integrity boundary: _tc_from_json strips it
        # before the legacy constructor, and the reconstructed hash
        # still matches the stored one.
        d = json.loads(record["content_json"])
        d["certificate_schema_version"] = 1
        tc = _tc_from_json(json.dumps(d))
        assert compute_tc_hash(tc.to_dict()) == record["tc_hash"]

    def test_version_2_marked_v1_content_fails_rehydration(self):
        # Version 2 now routes to the strict v2 deserializer, whose
        # exact-schema validation rejects v1-shaped content.
        d = json.loads(FIXTURE_RECORDS[0]["content_json"])
        d["certificate_schema_version"] = 2
        with pytest.raises(CertificateInvariantError):
            _tc_from_json(json.dumps(d))

    @pytest.mark.parametrize("bad", [0, 3, True, "1", "2", None])
    def test_malformed_version_fails_rehydration(self, bad):
        d = json.loads(FIXTURE_RECORDS[0]["content_json"])
        d["certificate_schema_version"] = bad
        with pytest.raises(UnsupportedCertificateSchemaVersion):
            _tc_from_json(json.dumps(d))


# =========================================================================== #
# Store-level raw verification                                                 #
# =========================================================================== #

class TestStoreRawVerification:
    def test_fresh_issuance_verifies_raw_and_reconstructed(self):
        chain_id = "chain-freeze-e2e"
        with CertificateStore(":memory:") as store:
            for i in range(3):
                tc = _fresh_tc(
                    chain_id=chain_id, subject_id=f"freeze-{i:03d}",
                )
                store.issue(tc)

            assert store.verify_chain(chain_id) is True

            raws = store._list_chain_raw(chain_id)
            assert len(raws) == 3
            for raw in raws:
                assert "certificate_schema_version" not in raw
                assert compute_legacy_raw_tc_hash(raw) == \
                    raw["audit_integrity"]["tc_hash"]

    def test_injected_field_fails_store_verification(self, monkeypatch):
        with CertificateStore(":memory:") as store:
            tc = _fresh_tc(subject_id="freeze-tamper")
            issued = store.issue(tc)
            chain_id = issued.audit_integrity.chain_id
            assert store.verify_chain(chain_id) is True

            # The append-only triggers prevent tampering with stored
            # rows via SQL, so simulate a tampered read instead: the
            # verification logic must fail on an injected field.
            real_raws = store._list_chain_raw(chain_id)
            tampered = [dict(real_raws[0], injected_field="x")]
            monkeypatch.setattr(
                store, "_list_chain_raw", lambda cid: tampered,
            )
            assert store.verify_chain(chain_id) is False

    def test_version_marked_content_fails_store_verification(
        self, monkeypatch,
    ):
        with CertificateStore(":memory:") as store:
            tc = _fresh_tc(subject_id="freeze-marked")
            issued = store.issue(tc)
            chain_id = issued.audit_integrity.chain_id

            real_raws = store._list_chain_raw(chain_id)
            marked = [dict(real_raws[0], certificate_schema_version=1)]
            monkeypatch.setattr(
                store, "_list_chain_raw", lambda cid: marked,
            )
            # Raw stored content carrying the version key does not match
            # the historical v1 wire shape — verification fails rather
            # than silently accepting a second stored-v1 representation.
            assert store.verify_chain(chain_id) is False
