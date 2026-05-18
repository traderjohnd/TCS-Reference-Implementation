"""
tcs.sdk
=======

Python SDK for integrating with the TCS governance API.

Quick start::

    from tcs.sdk import TCSClient

    client = TCSClient(base_url="http://localhost:8000")
    result = client.govern(
        query="Is this client suitable for municipal bonds?",
        retrieved_chunks=[...],
        candidate_answer="Based on the client profile...",
    )

    if result.allowed:
        print(result.output)
    else:
        print(f"Blocked: {result.blocking_reason}")
"""

from tcs.sdk.client import TCSClient, TCSClientError
from tcs.sdk.models import GovernResult, GovernRequest
from tcs.sdk.middleware import (
    GovernanceError,
    GovernanceHoldError,
    GovernanceStopError,
    TCSMiddleware,
    governed,
    get_last_govern_result,
)

__all__ = [
    "TCSClient",
    "TCSClientError",
    "GovernResult",
    "GovernRequest",
    "GovernanceError",
    "GovernanceHoldError",
    "GovernanceStopError",
    "TCSMiddleware",
    "governed",
    "get_last_govern_result",
]
