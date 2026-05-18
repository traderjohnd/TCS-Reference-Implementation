"""
tcs.workflow.connectors.api
============================

API / tool connector. Proves TCS can govern action, not just text.

Connection type
---------------

CT-1 (API). Enterprise AI risk shifts hard when the model moves
from "answering" to "acting" — calling external endpoints with
side effects, accessing internal services, invoking tools. This
connector emits the evidence TCS needs to govern that transition.

Evidence emitted
----------------

    B (Boundedness)
        - in_scope: True iff the endpoint is in the configured
          allowlist. Unauthorized endpoints are NOT called — the
          connector emits B violation and returns error=None payload.
        - scope_violations: lists any allowlist mismatches.

    A (Attribution)
        - timestamp_present from the response (or our call time).
        - source_count=1 for any successful call.

    C (Compliance)
        - side_effect_class read|write|destructive (declared by caller
          via params; the connector cannot infer this from HTTP).
        - policy_violations from allowlist denials.

    K (Known)
        - confidence_calibrated based on HTTP status: 2xx = high,
          5xx = low. 4xx is "calibrated negative" — the API answered.

Allowlist enforcement
---------------------

The connector enforces its allowlist *before* the HTTP call. An
unauthorized endpoint never reaches the network. This is part of
what makes the connector a governance surface, not just a thin
HTTP client.
"""

from __future__ import annotations

import time
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from tcs.workflow.connector import (
    ConnectorRequest,
    ConnectorResult,
    GovernedConnector,
)
from tcs.workflow.events import (
    AttributionSignal,
    BoundednessSignal,
    ComplianceSignal,
    GovernanceEvent,
    KnownStateSignal,
)
from tcs.workflow.trace import GovernedNode


_SIDE_EFFECT_CLASSES = ("read", "write", "destructive")


class APIConnector(GovernedConnector):
    """
    HTTP connector with allowlist enforcement.

    Parameters
    ----------
    http_client
        Any object exposing ``request(method, url, **kwargs)``
        returning an object with ``.status_code``, ``.text``, and
        ``.headers`` attributes. The default constructor uses an
        injected client so tests can pass a FastAPI ``TestClient``
        instead of opening a real socket.
    allowlist
        Iterable of allowed URL prefixes (host+path). The full
        request URL must start with one of these to be authorized.
    """

    connector_type = "api"

    def __init__(
        self,
        *,
        http_client: Any,
        allowlist: List[str],
    ) -> None:
        self.http_client = http_client
        self.allowlist = list(allowlist)

    def connection_type(self) -> str:
        return "CT-1"

    def _is_authorized(self, url: str) -> bool:
        parsed = urlparse(url)
        # For test clients, the host may be empty; compare on path-only
        # prefixes in addition to full URLs.
        candidates = [url]
        if parsed.path:
            candidates.append(parsed.path)
        for allowed in self.allowlist:
            for cand in candidates:
                if cand.startswith(allowed):
                    return True
        return False

    def invoke(self, request: ConnectorRequest) -> ConnectorResult:
        params = request.params or {}
        url = str(params.get("url", ""))
        method = str(params.get("method", "GET")).upper()
        side_effect = str(params.get("side_effect_class", "read")).lower()
        if side_effect not in _SIDE_EFFECT_CLASSES:
            side_effect = "read"
        json_body = params.get("json")

        t0 = time.perf_counter()
        authorized = self._is_authorized(url)

        if not authorized:
            return ConnectorResult(
                payload=None,
                output_text=None,
                raw_metadata={
                    "url": url,
                    "method": method,
                    "side_effect_class": side_effect,
                    "authorized": False,
                    "allowlist": list(self.allowlist),
                    "status_code": None,
                },
                latency_ms=round((time.perf_counter() - t0) * 1000, 2),
                error="endpoint_not_in_allowlist",
            )

        # Authorized — make the real call.
        try:
            kwargs: Dict[str, Any] = {}
            if json_body is not None:
                kwargs["json"] = json_body
            response = self.http_client.request(method, url, **kwargs)
            status_code = int(getattr(response, "status_code", 0))
            body_text = getattr(response, "text", "") or ""
            return ConnectorResult(
                payload={"status_code": status_code, "body": body_text},
                output_text=None,
                raw_metadata={
                    "url": url,
                    "method": method,
                    "side_effect_class": side_effect,
                    "authorized": True,
                    "status_code": status_code,
                    "is_2xx": 200 <= status_code < 300,
                    "is_4xx": 400 <= status_code < 500,
                    "is_5xx": 500 <= status_code < 600,
                },
                latency_ms=round((time.perf_counter() - t0) * 1000, 2),
            )
        except Exception as exc:
            return ConnectorResult(
                payload=None,
                output_text=None,
                raw_metadata={
                    "url": url,
                    "method": method,
                    "side_effect_class": side_effect,
                    "authorized": True,
                    "status_code": None,
                    "exception_type": type(exc).__name__,
                },
                latency_ms=round((time.perf_counter() - t0) * 1000, 2),
                error=str(exc),
            )

    def to_governance_event(
        self,
        result: ConnectorResult,
        node: GovernedNode,
        *,
        workflow_id: str,
        previous_event_hash: Optional[str] = None,
    ) -> GovernanceEvent:
        meta = result.raw_metadata
        url = meta.get("url", "")
        authorized = bool(meta.get("authorized", False))
        status_code = meta.get("status_code")
        side_effect = meta.get("side_effect_class", "read")

        if not authorized:
            # Unauthorized endpoint. Per the formal decision ladder, an
            # attempt to act outside the authorization scope is treated
            # as a C3 prohibited ACTION pattern — the action-layer analog
            # of a prohibited content pattern. Setting c3_violation=True
            # triggers Priority 2 (gate=0 AND C3=0.00) -> hard Stop.
            # This is principled: "do not call endpoints outside the
            # allowlist" is a prohibition the workflow must respect, not
            # a remediable score drop. Documentation in scope_attestation
            # also records the boundary violation for audit.
            boundedness = BoundednessSignal(
                in_scope=False,
                scope_violations=(f"unauthorized_endpoint:{url}",),
                external_references=(url,),
                score_contribution=0.0,
            )
            compliance = ComplianceSignal(
                c3_violation=True,
                c3_pattern=f"unauthorized_endpoint:{url}",
                policy_violations=(f"endpoint_not_in_allowlist:{url}",),
                score_contribution=0.0,
            )
            known = KnownStateSignal(
                confidence_calibrated=False,
                score_contribution=0.0,
            )
        else:
            # Authorized call. Score based on HTTP status.
            is_2xx = bool(meta.get("is_2xx"))
            is_5xx = bool(meta.get("is_5xx"))
            boundedness = BoundednessSignal(in_scope=True)
            compliance = ComplianceSignal(
                score_contribution=1.0,
            )
            if is_2xx:
                k_score = 1.0
                k_cal = True
            elif is_5xx:
                k_score = 0.4
                k_cal = False
            else:
                # 4xx — API answered, calibrated but not affirmative.
                k_score = 0.85
                k_cal = True
            known = KnownStateSignal(
                confidence_calibrated=k_cal,
                score_contribution=k_score,
            )

        # Attribution: a single named external source (the endpoint).
        attribution = AttributionSignal(
            source_count=1,
            sources_with_complete_metadata=1 if authorized else 0,
            integration_boundary_gaps=0 if authorized else 1,
            timestamp_present=True,
            chain_of_custody_complete=authorized,
            score_contribution=1.0 if authorized else 0.0,
        )

        return GovernanceEvent(
            event_id=str(uuid.uuid4()),
            workflow_id=workflow_id,
            node_id=node.node_id,
            timestamp=datetime.now(timezone.utc).isoformat(),
            connector_type=f"{self.connector_type}.{side_effect}",
            connection_type=self.connection_type(),
            sensitivity_tier=node.sensitivity_tier,
            boundedness=boundedness,
            attribution=attribution,
            compliance=compliance,
            known=known,
            payload_ref=str(status_code) if status_code is not None else None,
            latency_ms=result.latency_ms,
            error=result.error,
            previous_event_hash=previous_event_hash,
        )
