"""
tcs.sdk.middleware
==================

Governance enforcement middleware for Python applications.

Two integration patterns:

1. **Decorator** — wrap any function that returns AI output::

       from tcs.sdk import TCSClient
       from tcs.sdk.middleware import governed

       client = TCSClient(base_url="http://localhost:8000")

       @governed(client=client, base_profile_id="fin-r3-a4-ct4")
       def answer_question(query: str, context: list[dict]) -> str:
           return llm.generate(query, context)

2. **ASGI Middleware** — intercept HTTP responses on governed routes::

       from tcs.sdk.middleware import TCSMiddleware

       app = FastAPI()
       app.add_middleware(
           TCSMiddleware,
           tcs_base_url="http://localhost:8000",
           governed_routes=["/api/chat", "/api/recommend"],
           base_profile_id="fin-r3-a4-ct4",
       )
"""

from __future__ import annotations

import functools
import inspect
import json
import threading
from typing import Any, Callable, Dict, List, Optional, Sequence, Union

from tcs.sdk.client import TCSClient
from tcs.sdk.models import GovernResult


# ------------------------------------------------------------------ #
# Exception hierarchy                                                  #
# ------------------------------------------------------------------ #

class GovernanceError(Exception):
    """Base exception for governance enforcement."""

    def __init__(self, message: str, result: GovernResult) -> None:
        super().__init__(message)
        self.result = result


class GovernanceHoldError(GovernanceError):
    """Output held pending human review."""


class GovernanceStopError(GovernanceError):
    """Output blocked by governance policy."""


# ------------------------------------------------------------------ #
# Thread-local storage for last GovernResult                           #
# ------------------------------------------------------------------ #

_thread_local = threading.local()


def get_last_govern_result() -> Optional[GovernResult]:
    """Retrieve the GovernResult from the most recent governed call
    in the current thread. Returns None if no governed call has
    been made yet."""
    return getattr(_thread_local, "last_result", None)


# ------------------------------------------------------------------ #
# @governed decorator                                                  #
# ------------------------------------------------------------------ #

# Sentinel for "extract from function args by name"
_AUTO = object()


def governed(
    client: TCSClient,
    *,
    base_profile_id: str = "fin-r3-a4-ct4",
    query_param: str = "query",
    context_param: str = "context",
    on_hold: Union[str, Callable[[GovernResult], Any]] = "raise",
    on_stop: Union[str, Callable[[GovernResult], Any]] = "raise",
    subject_type: str = "recommendation",
    model_id: str = "default",
    pipeline_id: str = "default",
    extra_metadata: Optional[Dict[str, Any]] = None,
) -> Callable:
    """
    Decorator that interposes TCS governance on a function's return value.

    The decorated function runs normally. Its return value becomes the
    ``candidate_answer`` sent to ``client.govern()``. The ``query`` and
    ``retrieved_chunks`` (context) are extracted from the function's
    arguments by name.

    Parameters
    ----------
    client
        A :class:`TCSClient` instance (real or test-backed).
    base_profile_id
        Policy profile to evaluate against.
    query_param
        Name of the function parameter that carries the user query.
    context_param
        Name of the function parameter that carries the retrieved chunks /
        context list. If the function has no such parameter, an empty list
        is used.
    on_hold
        What to do when the decision is Hold:
        - ``"raise"`` — raise :class:`GovernanceHoldError`
        - ``"return_none"`` — return ``None``
        - a callable — called with the :class:`GovernResult`; its return
          value becomes the decorated function's return value.
    on_stop
        Same options as *on_hold*, but for Stop decisions.
    subject_type
        Subject type recorded in the Trust Certificate.
    model_id
        Model identifier passed to the governance API.
    pipeline_id
        Pipeline identifier passed to the governance API.
    extra_metadata
        Optional dict merged into the governance request metadata.
    """

    def decorator(fn: Callable) -> Callable:
        sig = inspect.signature(fn)

        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            # --- Run the wrapped function ---
            output = fn(*args, **kwargs)

            # --- Extract query and context from call arguments ---
            bound = sig.bind(*args, **kwargs)
            bound.apply_defaults()

            query = bound.arguments.get(query_param, "")
            context = bound.arguments.get(context_param, [])

            # Normalise context to list of dicts for the API.
            if not isinstance(context, list):
                context = []

            candidate_answer = str(output) if output is not None else ""

            # --- Call governance ---
            result = client.govern(
                query=str(query),
                retrieved_chunks=context,
                candidate_answer=candidate_answer,
                base_profile_id=base_profile_id,
                subject_type=subject_type,
                model_id=model_id,
                pipeline_id=pipeline_id,
                extra_metadata=extra_metadata or {},
            )

            # Store for thread-local retrieval.
            _thread_local.last_result = result

            # --- Enforce decision ---
            if result.allowed:
                # Allow or Observe — pass through.
                return output

            if result.decision == "Hold":
                return _handle_blocked(on_hold, result, GovernanceHoldError)

            # Stop, Escalate, or any other blocking decision.
            return _handle_blocked(on_stop, result, GovernanceStopError)

        return wrapper
    return decorator


def _handle_blocked(
    handler: Union[str, Callable[[GovernResult], Any]],
    result: GovernResult,
    exc_cls: type,
) -> Any:
    """Dispatch a Hold/Stop decision according to the handler config."""
    if handler == "raise":
        raise exc_cls(
            f"Governance {result.decision}: {result.blocking_reason or result.message}",
            result=result,
        )
    if handler == "return_none":
        return None
    if callable(handler):
        return handler(result)
    raise ValueError(f"Invalid governance handler: {handler!r}")


# ------------------------------------------------------------------ #
# ASGI Middleware                                                      #
# ------------------------------------------------------------------ #

class TCSMiddleware:
    """
    ASGI middleware that interposes TCS governance on configured routes.

    For governed routes the middleware:

    * Buffers the response body.
    * Sends it through ``TCSClient.govern()``.
    * On Allow/Observe — forwards the response with an
      ``X-TCS-Certificate-Id`` header.
    * On Hold/Stop — replaces the response with a 403 JSON body
      containing the governance decision and certificate ID.

    Non-governed routes pass through untouched.

    Parameters
    ----------
    app
        The inner ASGI application.
    tcs_base_url
        Root URL of the TCS governance API.
    governed_routes
        URL path prefixes that should be governed.
    base_profile_id
        Policy profile to evaluate against.
    tcs_client
        Optional pre-configured :class:`TCSClient`. If not provided one
        is created from *tcs_base_url*.
    api_key
        Optional Bearer token forwarded to the TCS API.
    """

    def __init__(
        self,
        app: Any,
        *,
        tcs_base_url: str = "http://localhost:8000",
        governed_routes: Sequence[str] = (),
        base_profile_id: str = "fin-r3-a4-ct4",
        tcs_client: Optional[TCSClient] = None,
        api_key: Optional[str] = None,
    ) -> None:
        self.app = app
        self.governed_routes = list(governed_routes)
        self.base_profile_id = base_profile_id
        self.client = tcs_client or TCSClient(
            base_url=tcs_base_url, api_key=api_key,
        )

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        if not self._is_governed(path):
            await self.app(scope, receive, send)
            return

        # Buffer the response from the inner app.
        response_started = False
        status_code = 200
        response_headers: List[List[bytes]] = []
        body_parts: List[bytes] = []

        async def capture_send(message: dict) -> None:
            nonlocal response_started, status_code, response_headers

            if message["type"] == "http.response.start":
                response_started = True
                status_code = message.get("status", 200)
                response_headers = list(message.get("headers", []))
            elif message["type"] == "http.response.body":
                body_parts.append(message.get("body", b""))

        await self.app(scope, receive, capture_send)

        # Reconstruct body.
        full_body = b"".join(body_parts)
        body_text = full_body.decode("utf-8", errors="replace")

        # Extract query from request (best-effort from body or query string).
        query = self._extract_query(scope)

        # Govern the response.
        try:
            result = self.client.govern(
                query=query,
                retrieved_chunks=[],
                candidate_answer=body_text,
                base_profile_id=self.base_profile_id,
            )
        except Exception:
            # If governance fails, pass through the original response
            # (fail-open at the middleware layer; the API has its own
            # fail-safe logic).
            await self._send_response(send, status_code, response_headers, full_body)
            return

        if result.allowed:
            # Add certificate header and forward.
            if result.certificate_id:
                response_headers.append([
                    b"x-tcs-certificate-id",
                    result.certificate_id.encode("utf-8"),
                ])
            await self._send_response(send, status_code, response_headers, full_body)
        else:
            # Block — replace response with 403.
            block_body = json.dumps({
                "error": "governance_block",
                "decision": result.decision,
                "message": result.message or f"Output blocked: {result.decision}",
                "certificate_id": result.certificate_id,
                "blocking_reason": result.blocking_reason,
            }).encode("utf-8")
            block_headers = [
                [b"content-type", b"application/json"],
            ]
            if result.certificate_id:
                block_headers.append([
                    b"x-tcs-certificate-id",
                    result.certificate_id.encode("utf-8"),
                ])
            await self._send_response(send, 403, block_headers, block_body)

    def _is_governed(self, path: str) -> bool:
        """Check if the request path matches any governed route prefix."""
        return any(path.startswith(route) for route in self.governed_routes)

    @staticmethod
    def _extract_query(scope: dict) -> str:
        """Best-effort query extraction from the ASGI scope."""
        qs = scope.get("query_string", b"").decode("utf-8", errors="replace")
        for param in qs.split("&"):
            if param.startswith("query=") or param.startswith("q="):
                return param.split("=", 1)[1]
        return ""

    @staticmethod
    async def _send_response(
        send: Any,
        status: int,
        headers: List[List[bytes]],
        body: bytes,
    ) -> None:
        """Send a complete HTTP response."""
        # Ensure content-length is correct.
        filtered = [h for h in headers if h[0].lower() != b"content-length"]
        filtered.append([b"content-length", str(len(body)).encode("ascii")])

        await send({
            "type": "http.response.start",
            "status": status,
            "headers": filtered,
        })
        await send({
            "type": "http.response.body",
            "body": body,
        })
