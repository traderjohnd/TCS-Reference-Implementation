"""
Shared test fixtures and helpers.

Centralizes the pattern for building TISInput objects in tests so every
test file reads consistently and small schema changes require only one
edit. The helper matches ARCHITECTURE.md §"Testing Strategy".
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Optional

import pytest

from tcs.policy_profiles import load_profile
from tcs.tis_engine import TISInput


#: Fixed evaluation time used across all tests. Deterministic timestamps
#: mean valid_until, state_transition_history, and last_invalidation_event
#: all produce stable values across test runs.
FIXED_EVAL_TIME: datetime = datetime(2026, 4, 7, 15, 0, 0)


def make_tis_input(
    profile_id: str,
    dimension_scores: Dict[str, float],
    *,
    context_metadata: Optional[Dict[str, Any]] = None,
    sub_factor_scores: Optional[Dict[str, Dict[str, float]]] = None,
    elapsed_hours: float = 0.0,
    is_valid: int = 1,
    invalidation_event: Optional[str] = None,
    subject_id: str = "test-subject",
    subject_type: str = "recommendation",
    evaluation_time: datetime = FIXED_EVAL_TIME,
) -> TISInput:
    """
    Build a TISInput with sensible defaults for tests.

    Only ``profile_id`` and ``dimension_scores`` are required. Anything
    else can be overridden via keyword arguments. The returned object is
    safe to pass directly to ``tcs.tis_engine.compute_tis``.
    """
    # Default context metadata covers the five penalty inputs with
    # "clean" values — no gaps, fresh context, no novelty, recent review,
    # not policy-sensitive. Override any of these per test as needed.
    default_meta: Dict[str, Any] = {
        "n_gaps": 0,
        "context_age_hours": 0.1,
        "novelty_score": 0.0,
        "days_since_review": 1,
        "is_policy_sensitive": False,
    }
    if context_metadata:
        default_meta.update(context_metadata)

    return TISInput(
        subject_id=subject_id,
        subject_type=subject_type,
        policy_profile=load_profile(profile_id),
        dimension_scores=dict(dimension_scores),
        sub_factor_scores=dict(sub_factor_scores) if sub_factor_scores else {},
        context_metadata=default_meta,
        elapsed_hours=elapsed_hours,
        is_valid=is_valid,
        invalidation_event=invalidation_event,
        evaluation_time=evaluation_time,
    )


@pytest.fixture
def fixed_eval_time() -> datetime:
    """Fixture wrapper around FIXED_EVAL_TIME for tests that prefer fixtures."""
    return FIXED_EVAL_TIME
