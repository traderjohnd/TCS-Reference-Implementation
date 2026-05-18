"""
tcs.sdk.client
==============

Thin HTTP client for the TCS governance API.

Uses only ``urllib.request`` — no external HTTP dependency (httpx,
requests) for Phase 4. Keeps requirements.txt minimal.

Usage::

    from tcs.sdk import TCSClient

    client = TCSClient(base_url="http://localhost:8000")
    result = client.govern(
        query="Is this client suitable for municipal bonds?",
        retrieved_chunks=[{"chunk_id": "c1", "similarity_score": 0.91, ...}],
        candidate_answer="Based on the client profile...",
    )
    print(result.decision)       # "Allow"
    print(result.certificate_id) # "TC-..."
"""

from __future__ import annotations

import json
import urllib.request
import urllib.error
from typing import Any, Dict, List, Optional

from tcs.sdk.models import GovernRequest, GovernResult


class TCSClientError(Exception):
    """Raised when an API call fails."""

    def __init__(
        self,
        message: str,
        status_code: Optional[int] = None,
        response_body: Optional[str] = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.response_body = response_body


class TCSClient:
    """
    Thin HTTP client for the TCS governance API.

    All methods are synchronous. The client is stateless — each call
    opens a fresh HTTP connection. Fine for Phase 4 demo workloads.

    For testing, use :meth:`from_test_client` to wrap a FastAPI
    ``TestClient`` — same public API, no real HTTP.

    Parameters
    ----------
    base_url
        Root URL of the TCS API (no trailing slash).
    timeout
        Request timeout in seconds.
    api_key
        Optional Bearer token for authenticated deployments.
    """

    def __init__(
        self,
        base_url: str = "http://localhost:8000",
        *,
        timeout: float = 30.0,
        api_key: Optional[str] = None,
        _test_client: Any = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._api_key = api_key
        self._test_client = _test_client

    @classmethod
    def from_test_client(cls, test_client: Any) -> "TCSClient":
        """
        Create a TCSClient that routes requests through a FastAPI
        ``TestClient`` instead of real HTTP. Used for testing.
        """
        return cls(base_url="http://testserver", _test_client=test_client)

    # ------------------------------------------------------------------ #
    # Internal helpers                                                     #
    # ------------------------------------------------------------------ #

    def _headers(self) -> Dict[str, str]:
        h: Dict[str, str] = {"Content-Type": "application/json"}
        if self._api_key:
            h["Authorization"] = f"Bearer {self._api_key}"
        return h

    def _request(
        self,
        method: str,
        path: str,
        body: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Send an HTTP request and return the parsed JSON response.

        If ``_test_client`` is set (via :meth:`from_test_client`), routes
        the request through the FastAPI TestClient. Otherwise uses
        ``urllib.request`` for real HTTP.

        Raises :class:`TCSClientError` on any HTTP or JSON error.
        """
        if self._test_client is not None:
            return self._request_via_test_client(method, path, body)

        url = f"{self._base_url}{path}"
        data = json.dumps(body).encode("utf-8") if body is not None else None

        req = urllib.request.Request(
            url,
            data=data,
            headers=self._headers(),
            method=method,
        )

        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                raw = resp.read().decode("utf-8")
                return json.loads(raw)
        except urllib.error.HTTPError as exc:
            body_text = ""
            try:
                body_text = exc.read().decode("utf-8")
            except Exception:
                pass
            raise TCSClientError(
                f"HTTP {exc.code} on {method} {path}: {body_text}",
                status_code=exc.code,
                response_body=body_text,
            ) from exc
        except urllib.error.URLError as exc:
            raise TCSClientError(
                f"Connection error on {method} {path}: {exc.reason}"
            ) from exc
        except json.JSONDecodeError as exc:
            raise TCSClientError(
                f"Invalid JSON response from {method} {path}: {exc}"
            ) from exc

    def _request_via_test_client(
        self,
        method: str,
        path: str,
        body: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Route a request through the FastAPI TestClient."""
        tc = self._test_client
        if method == "GET":
            resp = tc.get(path)
        elif method == "POST":
            resp = tc.post(path, json=body)
        else:
            raise TCSClientError(f"Unsupported method {method}")

        if resp.status_code >= 400:
            raise TCSClientError(
                f"HTTP {resp.status_code} on {method} {path}: {resp.text}",
                status_code=resp.status_code,
                response_body=resp.text,
            )
        return resp.json()

    def _get(self, path: str) -> Dict[str, Any]:
        return self._request("GET", path)

    def _post(self, path: str, body: Dict[str, Any]) -> Dict[str, Any]:
        return self._request("POST", path, body)

    # ------------------------------------------------------------------ #
    # Public API                                                           #
    # ------------------------------------------------------------------ #

    def govern(
        self,
        *,
        query: str,
        retrieved_chunks: List[Dict[str, Any]],
        candidate_answer: str,
        model_id: str = "default",
        pipeline_id: str = "default",
        subject_type: str = "recommendation",
        subject_id: Optional[str] = None,
        base_profile_id: str = "fin-r3-a4-ct4",
        risk_tier: Optional[str] = None,
        action_class: Optional[str] = None,
        connection_type: Optional[str] = None,
        extra_metadata: Optional[Dict[str, Any]] = None,
    ) -> GovernResult:
        """
        Submit an AI output for governance evaluation.

        Returns a :class:`GovernResult` with the decision, certificate
        ID, and extracted scores. The ``allowed`` property tells you
        whether the output may be delivered to the user.
        """
        req = GovernRequest(
            query=query,
            retrieved_chunks=retrieved_chunks,
            candidate_answer=candidate_answer,
            model_id=model_id,
            pipeline_id=pipeline_id,
            subject_type=subject_type,
            subject_id=subject_id,
            base_profile_id=base_profile_id,
            extra_metadata=extra_metadata or {},
        )

        # Fold optional overrides into extra_metadata for the API.
        if risk_tier is not None:
            req.extra_metadata["risk_tier"] = risk_tier
        if action_class is not None:
            req.extra_metadata["action_class"] = action_class
        if connection_type is not None:
            req.extra_metadata["connection_type"] = connection_type

        data = self._post("/v2/govern", req.to_dict())
        result = GovernResult.from_api_response(data, base_url=self._base_url)

        # The govern endpoint returns the GovernedResponse shape, which
        # doesn't include TIS scores directly. Fetch them from the TC
        # if a certificate was issued, so the SDK surface has all the
        # fields the Phase 4 spec promises.
        if result.certificate_id:
            try:
                tc = self.get_certificate(result.certificate_id)
                result.tis_current = tc.get("tis_current")
                result.tis_raw = tc.get("tis_raw")
                result.s_base = tc.get("s_base")
                result.gate_passed = tc.get("gate_passed")
            except TCSClientError:
                pass  # Scores stay None if the fetch fails.

        return result

    def get_certificate(self, certificate_id: str) -> Dict[str, Any]:
        """Retrieve the full Trust Certificate by ID."""
        return self._get(f"/v2/certificates/{certificate_id}")

    def health(self) -> Dict[str, Any]:
        """Check API health and chain integrity."""
        return self._get("/v2/health")

    def metrics(self) -> Dict[str, Any]:
        """Get live governance metrics."""
        return self._get("/v2/metrics/live")

    def decision_stream(self, limit: int = 50) -> List[Dict[str, Any]]:
        """Get recent governance decisions."""
        data = self._get(f"/v2/govern/decisions/stream?limit={limit}")
        return data.get("decisions", [])

    def hold_queue(self, limit: int = 20) -> List[Dict[str, Any]]:
        """Get Hold decisions awaiting review."""
        data = self._get(f"/v2/govern/hold-queue?limit={limit}")
        return data.get("holds", [])

    def override_hold(
        self,
        certificate_id: str,
        *,
        override_decision: str,
        justification: str,
        override_by: str,
    ) -> Dict[str, Any]:
        """Submit an override for a Hold decision."""
        return self._post(
            f"/v2/govern/hold-queue/{certificate_id}/override",
            {
                "override_decision": override_decision,
                "justification": justification,
                "override_by": override_by,
            },
        )

    def verify_chain(self, chain_id: Optional[str] = None) -> Dict[str, Any]:
        """Verify hash chain integrity."""
        path = "/v2/certificates/verify-chain"
        if chain_id:
            path += f"?chain_id={chain_id}"
        return self._get(path)
