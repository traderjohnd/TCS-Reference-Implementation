"""
tcs.sdk.models
==============

Data models for the TCS SDK. These are plain dataclasses that mirror
the API request/response shapes. No HTTP logic here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class GovernRequest:
    """
    Structured request for the ``POST /v2/govern`` endpoint.

    Fields mirror :class:`tcs.api.routes_govern.GovernRequestBody`.
    """

    query: str
    retrieved_chunks: List[Dict[str, Any]]
    candidate_answer: str
    model_id: str = "default"
    pipeline_id: str = "default"
    subject_type: str = "recommendation"
    subject_id: Optional[str] = None
    request_id: Optional[str] = None
    base_profile_id: str = "fin-r3-a4-ct4"

    # Identity passthroughs
    requesting_identity: Optional[str] = None
    identity_verified: Optional[bool] = None
    identity_confidence: Optional[float] = None
    authorization_tier: Optional[str] = None
    sensitivity_tier: Optional[str] = None
    mcp_server_id: Optional[str] = None

    extra_metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        """Serialize to the JSON body shape the API expects."""
        d: Dict[str, Any] = {
            "query": self.query,
            "retrieved_chunks": self.retrieved_chunks,
            "candidate_answer": self.candidate_answer,
            "model_id": self.model_id,
            "pipeline_id": self.pipeline_id,
            "subject_type": self.subject_type,
            "base_profile_id": self.base_profile_id,
            "extra_metadata": self.extra_metadata,
        }
        # Include optional fields only when set.
        if self.subject_id is not None:
            d["subject_id"] = self.subject_id
        if self.request_id is not None:
            d["request_id"] = self.request_id
        if self.requesting_identity is not None:
            d["requesting_identity"] = self.requesting_identity
        if self.identity_verified is not None:
            d["identity_verified"] = self.identity_verified
        if self.identity_confidence is not None:
            d["identity_confidence"] = self.identity_confidence
        if self.authorization_tier is not None:
            d["authorization_tier"] = self.authorization_tier
        if self.sensitivity_tier is not None:
            d["sensitivity_tier"] = self.sensitivity_tier
        if self.mcp_server_id is not None:
            d["mcp_server_id"] = self.mcp_server_id
        return d


@dataclass
class GovernResult:
    """
    Structured response from the ``POST /v2/govern`` endpoint.

    Wraps the raw JSON dict returned by the API, exposing the most
    commonly needed fields as typed attributes.
    """

    request_id: Optional[str]
    decision: str
    output: Optional[str]
    blocked: bool
    certificate_id: Optional[str]
    monitoring: bool
    requires_human_review: bool
    governance_degraded: bool
    fail_safe_applied: bool
    message: str
    blocking_reason: Optional[str]
    tis_current: Optional[float]
    tis_raw: Optional[float]
    s_base: Optional[float] = None
    gate_passed: Optional[bool] = None

    # Internal: base URL for building certificate_url.
    _base_url: str = ""

    # The raw API response dict, for anything not surfaced above.
    _raw: Dict[str, Any] = field(default_factory=dict)

    @property
    def allowed(self) -> bool:
        """True when the governance decision permits output delivery."""
        return self.decision in ("Allow", "Observe")

    @property
    def certificate_url(self) -> str:
        """Convenience URL for retrieving the full Trust Certificate."""
        return f"{self._base_url}/v2/certificates/{self.certificate_id}"

    @classmethod
    def from_api_response(
        cls,
        data: Dict[str, Any],
        base_url: str = "",
    ) -> "GovernResult":
        """
        Construct a ``GovernResult`` from the raw API JSON response.

        Extracts known fields; unknown fields are preserved in ``_raw``.
        """
        return cls(
            request_id=data.get("request_id"),
            decision=data.get("decision", "Stop"),
            output=data.get("output"),
            blocked=data.get("blocked", True),
            certificate_id=data.get("certificate_id"),
            monitoring=data.get("monitoring", False),
            requires_human_review=data.get("requires_human_review", False),
            governance_degraded=data.get("governance_degraded", False),
            fail_safe_applied=data.get("fail_safe_applied", False),
            message=data.get("message", ""),
            blocking_reason=data.get("blocking_reason"),
            tis_current=data.get("tis_current"),
            tis_raw=data.get("tis_raw"),
            s_base=data.get("s_base"),
            gate_passed=data.get("gate_passed"),
            _base_url=base_url,
            _raw=data,
        )
