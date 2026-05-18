"""
tcs.api.routes_connections
==========================

POST /v2/connections/test — Test an LLM, RAG, or API connection.

Accepts provider credentials and makes a minimal API call to verify
the connection is live. Keys are used in-memory only — never stored.
"""

from __future__ import annotations

import time
from typing import Optional

from fastapi import APIRouter
from pydantic import BaseModel


router = APIRouter()


# --------------------------------------------------------------------------- #
# Request / Response models                                                    #
# --------------------------------------------------------------------------- #

class TestConnectionRequest(BaseModel):
    category: str = "llm"           # llm | rag | external_api
    provider: str = "mock"          # openai | anthropic | mock
    api_key: Optional[str] = None
    model: Optional[str] = None
    endpoint: Optional[str] = None  # for custom RAG / API endpoints


class TestConnectionResponse(BaseModel):
    success: bool
    provider: str
    model: str
    latency_ms: float
    error: Optional[str] = None


# --------------------------------------------------------------------------- #
# POST /v2/connections/test                                                    #
# --------------------------------------------------------------------------- #

@router.post("/connections/test")
def test_connection(body: TestConnectionRequest) -> TestConnectionResponse:
    """
    Test a connection by making a minimal API call.

    - Mock: always succeeds instantly
    - OpenAI: sends a 1-token completion to verify auth
    - Anthropic: sends a 1-token message to verify auth
    - RAG / External API: stubbed — returns success for now
    """
    t0 = time.perf_counter()

    if body.category != "llm":
        # Stubbed — RAG and external API testing comes later
        return TestConnectionResponse(
            success=True,
            provider=body.provider,
            model=body.model or "n/a",
            latency_ms=round((time.perf_counter() - t0) * 1000, 1),
        )

    if body.provider == "mock":
        return TestConnectionResponse(
            success=True,
            provider="mock",
            model="deterministic",
            latency_ms=round((time.perf_counter() - t0) * 1000, 1),
        )

    if body.provider == "openai":
        if not body.api_key:
            return TestConnectionResponse(
                success=False, provider="openai",
                model=body.model or "unknown",
                latency_ms=0, error="API key is required",
            )
        try:
            import openai
            client = openai.OpenAI(api_key=body.api_key)
            raw_model = body.model or "gpt-5.5 (Instant)"
            api_model = raw_model.replace(" (Instant)", "").replace(" (Thinking)", "").strip()
            is_reasoning = api_model.startswith("o3") or api_model.startswith("o4") or "(Thinking)" in (body.model or "")
            is_new_model = api_model.startswith("gpt-5") or api_model.startswith("gpt-4.1")
            kwargs = {
                "model": api_model,
                "messages": [{"role": "user", "content": "ping"}],
            }
            if is_reasoning:
                kwargs["max_completion_tokens"] = 20
            elif is_new_model:
                kwargs["max_completion_tokens"] = 10
            else:
                kwargs["max_tokens"] = 1
            model_name = raw_model
            client.chat.completions.create(**kwargs)
            return TestConnectionResponse(
                success=True, provider="openai", model=model_name,
                latency_ms=round((time.perf_counter() - t0) * 1000, 1),
            )
        except Exception as e:
            return TestConnectionResponse(
                success=False, provider="openai",
                model=body.model or "unknown",
                latency_ms=round((time.perf_counter() - t0) * 1000, 1),
                error=str(e),
            )

    if body.provider == "anthropic":
        if not body.api_key:
            return TestConnectionResponse(
                success=False, provider="anthropic",
                model=body.model or "unknown",
                latency_ms=0, error="API key is required",
            )
        try:
            import anthropic
            client = anthropic.Anthropic(api_key=body.api_key)
            model_name = body.model or "claude-sonnet-4-20250514"
            client.messages.create(
                model=model_name, max_tokens=1,
                messages=[{"role": "user", "content": "ping"}],
            )
            return TestConnectionResponse(
                success=True, provider="anthropic", model=model_name,
                latency_ms=round((time.perf_counter() - t0) * 1000, 1),
            )
        except Exception as e:
            return TestConnectionResponse(
                success=False, provider="anthropic",
                model=body.model or "unknown",
                latency_ms=round((time.perf_counter() - t0) * 1000, 1),
                error=str(e),
            )

    return TestConnectionResponse(
        success=False, provider=body.provider,
        model=body.model or "unknown",
        latency_ms=0, error=f"Unknown provider: {body.provider}",
    )
