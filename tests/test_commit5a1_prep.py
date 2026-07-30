"""
tis-v2 Commit 5a.1 — preparatory transport-integration corrections.

Two owner-directed corrections before production activation (5b):

1.  SDK govern contract repair — ``risk_tier`` / ``action_class`` /
    ``connection_type`` travel as dedicated TYPED top-level request
    fields on ``POST /v2/govern`` and in ``TCSClient.govern()``. They
    never pass through public ``extra_metadata`` (the same names there
    are protected keys and 422). The ROUTE — not the public caller —
    constructs the trusted governed metadata from the validated values.

2.  Source-specific credential provenance — ``credential_detection``
    records seal in exactly one of two valid evidence forms:
    pattern-detected (pattern_id + nonempty supported
    pattern_set_version, detail_code optional from an enumerated
    pattern-form set) or declared CT-12 (enumerated declared
    detail_code, pattern fields empty). Missing, unknown,
    contradictory, and unsupported combinations fail closed.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from tcs.api.app import create_app
from tcs.canonical import CertificateInvariantError
from tcs.governed_metadata import is_protected_key
from tcs.provenance import (
    ACTIVE_CREDENTIAL_PATTERN_SET_VERSION,
    C3_PROVENANCE_SCHEMA_VERSION,
    C3ProvenanceRecord,
    DECLARED_CREDENTIAL_DETAIL_CODES,
    PATTERN_CREDENTIAL_DETAIL_CODES,
    validate_c3_provenance_record,
)
from tcs.sdk import TCSClient


# --------------------------------------------------------------------------- #
# Shared fixtures                                                              #
# --------------------------------------------------------------------------- #

@pytest.fixture()
def app():
    return create_app()


@pytest.fixture()
def client(app):
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def spy(app, client):
    """Wrap app.state.interceptor so tests can observe the exact
    InterceptedRequest the route hands to the production pipeline —
    the real interceptor still runs (no behaviour change)."""
    real = app.state.interceptor
    captured = {}

    class _Spy:
        def govern(self, intercepted):
            captured["request"] = intercepted
            return real.govern(intercepted)

        def __getattr__(self, name):
            return getattr(real, name)

    app.state.interceptor = _Spy()
    yield captured
    app.state.interceptor = real


def _govern_body(**overrides):
    body = {
        "query": "What allocation for a conservative client?",
        "retrieved_chunks": [
            {"chunk_id": "c1", "similarity_score": "0.93",
             "source_doc": "policy.pdf", "version": "1",
             "content": "policy text"},
        ],
        "candidate_answer": "A 60/40 allocation.",
        "subject_id": "c5a1-transport",
    }
    body.update(overrides)
    return body


# =========================================================================== #
# 1. Typed evaluation-typing fields on /v2/govern                              #
# =========================================================================== #

class TestTypedGovernFields:
    def test_risk_tier_typed_field_reaches_trusted_context(self, client, spy):
        r = client.post("/v2/govern", json=_govern_body(risk_tier="r2"))
        assert r.status_code == 200, r.text
        bundle = spy["request"].context_bundle
        assert bundle["risk_tier"] == "r2"

    def test_action_class_typed_field_reaches_trusted_context(
            self, client, spy):
        r = client.post("/v2/govern", json=_govern_body(action_class="a3"))
        assert r.status_code == 200, r.text
        assert spy["request"].context_bundle["action_class"] == "a3"

    def test_connection_type_typed_field_reaches_trusted_context(
            self, client, spy):
        r = client.post("/v2/govern",
                        json=_govern_body(connection_type="CT-3"))
        assert r.status_code == 200, r.text
        assert spy["request"].context_bundle["connection_type"] == "CT-3"

    def test_all_three_typed_fields_together(self, client, spy):
        r = client.post("/v2/govern", json=_govern_body(
            risk_tier="r3", action_class="a4", connection_type="CT-4",
        ))
        assert r.status_code == 200, r.text
        bundle = spy["request"].context_bundle
        assert bundle["risk_tier"] == "r3"
        assert bundle["action_class"] == "a4"
        assert bundle["connection_type"] == "CT-4"

    def test_omitted_fields_preserve_default_behavior(self, client, spy):
        r = client.post("/v2/govern", json=_govern_body())
        assert r.status_code == 200, r.text
        bundle = spy["request"].context_bundle
        # No typed field supplied -> nothing injected; downstream
        # defaults (fail-safe tier hint "r3", CT auto-detection from
        # retrieved_chunks) stay in force.
        assert "risk_tier" not in bundle
        assert "action_class" not in bundle
        assert "connection_type" not in bundle

    @pytest.mark.parametrize("field,bad", [
        ("risk_tier", "r9"), ("risk_tier", "R1"), ("risk_tier", ""),
        ("action_class", "a7"), ("action_class", "advisory"),
        ("connection_type", "CT-99"), ("connection_type", "vector_db"),
        ("connection_type", ""),
    ])
    def test_invalid_typed_values_are_422(self, client, field, bad):
        r = client.post("/v2/govern", json=_govern_body(**{field: bad}))
        assert r.status_code == 422, (field, bad, r.status_code)

    def test_declared_ct12_over_typed_field_produces_credential_stop(
            self, client):
        """connection_type=CT-12 through the typed field drives the
        deterministic declared-credential Stop — proof the typed value
        actually steers the evaluation, not merely transport."""
        r = client.post("/v2/govern",
                        json=_govern_body(connection_type="CT-12"))
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["decision"] == "Stop"
        assert data["blocked"] is True
        assert "credential" in (data.get("blocking_reason") or "").lower()
        assert data["certificate_id"]

    @pytest.mark.parametrize("key,value", [
        ("risk_tier", "r1"),
        ("action_class", "a2"),
        ("connection_type", "CT-4"),
    ])
    def test_duplicate_in_extra_metadata_is_rejected(self, client, key, value):
        r = client.post("/v2/govern", json=_govern_body(
            extra_metadata={key: value},
        ))
        assert r.status_code == 422
        detail = r.json()["detail"]
        assert detail["error"] == "protected_metadata_keys"
        assert key in detail["rejected_keys"]
        # Values are never echoed.
        assert value not in json.dumps(detail["rejected_keys"])

    def test_conflicting_typed_field_plus_metadata_duplicate_is_422(
            self, client):
        """A conflicting duplicate submission fails outright — the API
        never chooses between the typed field and the metadata copy."""
        r = client.post("/v2/govern", json=_govern_body(
            risk_tier="r2",
            extra_metadata={"risk_tier": "r1"},
        ))
        assert r.status_code == 422
        assert "risk_tier" in r.json()["detail"]["rejected_keys"]

    def test_the_three_names_are_protected_keys(self):
        assert is_protected_key("risk_tier")
        assert is_protected_key("action_class")
        assert is_protected_key("connection_type")
        # Case / separator variants too.
        assert is_protected_key("Risk-Tier")
        assert is_protected_key("ACTION_CLASS")

    def test_openapi_documents_typed_fields(self, client):
        schema = client.get("/openapi.json").json()
        props = schema["components"]["schemas"]["GovernRequestBody"][
            "properties"]
        for name in ("risk_tier", "action_class", "connection_type"):
            assert name in props, name


# =========================================================================== #
# 2. SDK govern() serialization contract                                       #
# =========================================================================== #

class _RecordingTestClient:
    """Captures the exact JSON body TCSClient.govern() sends."""

    def __init__(self):
        self.posts = []

    def post(self, path, json=None):
        self.posts.append((path, json))
        return SimpleNamespace(
            status_code=200,
            text="",
            json=lambda: {
                "request_id": "req-1", "decision": "Allow",
                "output": "ok", "blocked": False,
                "certificate_id": None, "monitoring": False,
                "requires_human_review": False,
                "governance_degraded": False,
                "fail_safe_applied": False, "message": "",
            },
        )

    def get(self, path):
        return SimpleNamespace(status_code=200, text="", json=lambda: {})


class TestSDKGovernContract:
    def _chunks(self):
        return [{"chunk_id": "c1", "similarity_score": "0.93",
                 "source_doc": "d.pdf", "version": "1", "content": "x"}]

    @pytest.mark.parametrize("param,value", [
        ("risk_tier", "r2"),
        ("action_class", "a3"),
        ("connection_type", "CT-4"),
    ])
    def test_each_convenience_param_serializes_top_level(self, param, value):
        rec = _RecordingTestClient()
        sdk = TCSClient.from_test_client(rec)
        sdk.govern(
            query="q", retrieved_chunks=self._chunks(),
            candidate_answer="a", **{param: value},
        )
        _, body = rec.posts[0]
        assert body[param] == value
        assert param not in body["extra_metadata"]

    def test_all_three_together_serialize_top_level(self):
        rec = _RecordingTestClient()
        sdk = TCSClient.from_test_client(rec)
        sdk.govern(
            query="q", retrieved_chunks=self._chunks(),
            candidate_answer="a",
            risk_tier="r3", action_class="a4", connection_type="CT-4",
            extra_metadata={"note": "display-only"},
        )
        _, body = rec.posts[0]
        assert body["risk_tier"] == "r3"
        assert body["action_class"] == "a4"
        assert body["connection_type"] == "CT-4"
        for name in ("risk_tier", "action_class", "connection_type"):
            assert name not in body["extra_metadata"]
        assert body["extra_metadata"] == {"note": "display-only"}

    def test_omitted_params_absent_from_body(self):
        rec = _RecordingTestClient()
        sdk = TCSClient.from_test_client(rec)
        sdk.govern(query="q", retrieved_chunks=self._chunks(),
                   candidate_answer="a")
        _, body = rec.posts[0]
        for name in ("risk_tier", "action_class", "connection_type"):
            assert name not in body
            assert name not in body["extra_metadata"]

    def test_sdk_convenience_params_work_end_to_end(self, client):
        """The documented SDK capability against the real app: no 422,
        a certificate is issued."""
        sdk = TCSClient.from_test_client(client)
        result = sdk.govern(
            query="What allocation for a conservative client?",
            retrieved_chunks=self._chunks(),
            candidate_answer="A 60/40 allocation.",
            risk_tier="r3", action_class="a4", connection_type="CT-4",
        )
        assert result.decision
        assert result.certificate_id

    def test_sdk_caller_supplied_metadata_duplicate_is_rejected(self, client):
        from tcs.sdk.client import TCSClientError
        sdk = TCSClient.from_test_client(client)
        with pytest.raises(TCSClientError) as ei:
            sdk.govern(
                query="q", retrieved_chunks=self._chunks(),
                candidate_answer="a",
                extra_metadata={"risk_tier": "r1"},
            )
        assert ei.value.status_code == 422


# =========================================================================== #
# 3. Source-specific credential provenance validation                          #
# =========================================================================== #

def _cred_record(**overrides):
    fields = dict(
        schema_version=C3_PROVENANCE_SCHEMA_VERSION,
        source_type="credential_detection",
        pattern_id="", pattern_set_version="", location_tag="",
        connector_type="", detail_code="", producer_id="",
    )
    fields.update(overrides)
    return C3ProvenanceRecord(**fields)


class TestCredentialProvenanceForms:
    # ---- valid forms ---------------------------------------------------- #

    def test_pattern_detected_form_valid(self):
        validate_c3_provenance_record(_cred_record(
            pattern_id="cred-002-openai-style-key",
            pattern_set_version=ACTIVE_CREDENTIAL_PATTERN_SET_VERSION,
            location_tag="chunk_id=c9",
        ))

    def test_declared_ct12_form_valid(self):
        validate_c3_provenance_record(_cred_record(
            detail_code="connection_type_ct12_declared",
        ))

    # ---- missing evidence ----------------------------------------------- #

    def test_both_evidence_forms_absent_rejected(self):
        with pytest.raises(CertificateInvariantError, match="absent"):
            validate_c3_provenance_record(_cred_record())

    # ---- unknown / arbitrary values ------------------------------------- #

    def test_arbitrary_detail_code_rejected(self):
        with pytest.raises(CertificateInvariantError, match="enumerated"):
            validate_c3_provenance_record(_cred_record(
                detail_code="a free-form reason string is not provenance",
            ))

    def test_unknown_pattern_id_rejected(self):
        with pytest.raises(CertificateInvariantError, match="not in pattern"):
            validate_c3_provenance_record(_cred_record(
                pattern_id="cred-999-ghost",
                pattern_set_version=ACTIVE_CREDENTIAL_PATTERN_SET_VERSION,
            ))

    # ---- unsupported / missing pattern-set versions --------------------- #

    def test_unsupported_pattern_set_version_fails_closed(self):
        with pytest.raises(CertificateInvariantError, match="unknown"):
            validate_c3_provenance_record(_cred_record(
                pattern_id="cred-002-openai-style-key",
                pattern_set_version="tcs-credential-patterns-v99",
            ))

    def test_empty_pattern_set_version_with_pattern_id_rejected(self):
        with pytest.raises(CertificateInvariantError,
                           match="pattern_set_version"):
            validate_c3_provenance_record(_cred_record(
                pattern_id="cred-002-openai-style-key",
            ))

    # ---- contradictory combinations ------------------------------------- #

    def test_declared_code_alongside_pattern_id_is_contradictory(self):
        with pytest.raises(CertificateInvariantError, match="contradictory"):
            validate_c3_provenance_record(_cred_record(
                pattern_id="cred-002-openai-style-key",
                pattern_set_version=ACTIVE_CREDENTIAL_PATTERN_SET_VERSION,
                detail_code="connection_type_ct12_declared",
            ))

    def test_declared_form_with_pattern_set_version_is_contradictory(self):
        with pytest.raises(CertificateInvariantError, match="contradictory"):
            validate_c3_provenance_record(_cred_record(
                detail_code="connection_type_ct12_declared",
                pattern_set_version=ACTIVE_CREDENTIAL_PATTERN_SET_VERSION,
            ))

    # ---- code-set contracts --------------------------------------------- #

    def test_declared_code_set_contains_the_wired_ct12_code(self):
        assert "connection_type_ct12_declared" in \
            DECLARED_CREDENTIAL_DETAIL_CODES

    def test_pattern_form_code_set_is_declared_and_currently_empty(self):
        # Append-only contract: adding a pattern-form supplementary code
        # is an append here, never a validator rewrite. Today none exist,
        # so any nonempty detail_code on the pattern form is rejected.
        assert PATTERN_CREDENTIAL_DETAIL_CODES == frozenset()
