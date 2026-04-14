"""
TIS Engine — Public Function Contract
======================================

This file documents the public interface of the Trust Integrity Score
computation engine (tcs/tis_engine.py). It is extracted for reference
and does not contain executable logic.

Pure-Function Contract
----------------------

``compute_tis`` is a **pure function**:

- **No I/O**: does not read files, call APIs, or access databases.
- **No external calls**: does not invoke MCP servers, network services,
  or any system outside its own module.
- **No state**: does not read or write module-level mutable state.
  Every invocation produces output solely from its input argument.
- **Deterministic**: the same ``TISInput`` always produces the same
  ``TISResult``, regardless of call order, concurrency, or environment.

This contract is architecturally load-bearing. The TIS engine receives
a fully resolved ``TISInput`` from the Governed Context Architecture
(GCA) and returns a ``TISResult``. It never knows whether its input
came from a test fixture, a scenario JSON file, or five live MCP server
calls. This separation is what makes the engine testable, auditable,
and connection-type-agnostic.

The fourth dimension is K (Known), representing uncertainty calibration
and epistemic awareness in the formal specification.

Canonical Formula Implemented
-----------------------------

::

    TIS(x, r, a, ρ, t) = G(r,a)(x,ρ)
                        * SUM_i( w_i(r,a) * dim_i(x,ρ) )
                        * (1 - P(x,r,a,ρ,t))
                        * exp(-mu(r,a) * dt)
                        * I_inv(x,ρ,t)

Three derived scores are computed and returned:

- ``TIS_raw``:     SUM(w_i * dim_i)              — pre-penalty, pre-gate
- ``TIS_adj``:     TIS_raw * (1 - P)             — post-penalty, pre-decay
- ``TIS_current``: TIS_adj * decay * G * I_inv   — operative score
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, FrozenSet, List, Optional, Tuple

from tcs.policy_profiles import PolicyProfile


@dataclass
class TISInput:
    """Complete input bundle for a single TIS computation."""

    subject_id: str
    subject_type: str
    policy_profile: PolicyProfile
    dimension_scores: Dict[str, float]          # {"B": float, "A": float, "C": float, "K": float}
    sub_factor_scores: Dict[str, Dict[str, float]]  # e.g. {"C": {"C3": 0.00}}; optional
    context_metadata: Dict[str, object]         # n_gaps, context_age_hours, novelty_score, etc.
    elapsed_hours: float                        # delta-t since trust anchor t_0
    is_valid: int                               # 1=valid, 0=invalidated
    invalidation_event: Optional[str]           # event type if invalidated
    evaluation_time: datetime                   # UTC timestamp of evaluation


@dataclass
class TISResult:
    """Complete result of a TIS computation. All fields required."""

    tis_raw: float                              # pre-penalty weighted composite
    penalty_breakdown: Dict[str, float]         # all five: P_cb, P_d, P_n, P_h, P_ps
    penalty_aggregate: float                    # min(0.50, weighted sum of components)
    tis_adj: float                              # TIS_raw * (1 - P)
    gate_result: int                            # 0 or 1
    gate_results_by_dim: Dict[str, str]         # "pass" | "fail" | "not_applicable" for all four
    failing_dimensions: List[str]               # dimensions that failed the gate
    C3_score: float                             # C3 sub-factor; 0.00 = hard stop
    decay_factor: float                         # exp(-mu * dt)
    tis_current: float                          # operative score after all terms
    valid_until: datetime                       # evaluation_time + ln(2)/mu
    is_valid: int                               # echoed; may be forced to 0
    invalidation_event: Optional[str]           # echoed


def compute_tis(inp: TISInput) -> TISResult:
    """
    Run the full TIS pipeline end-to-end.

    Parameters
    ----------
    inp : TISInput
        Complete input bundle containing subject identity, policy profile,
        dimension scores {B, A, C, K}, context metadata for penalty
        computation, elapsed time for decay, and invalidation state.

    Returns
    -------
    TISResult
        Complete computation result with TIS_raw, TIS_adj, TIS_current,
        gate evaluation, penalty breakdown, decay factor, and validity.

    Pipeline Sequence
    -----------------
    0. Apply identity-based B-score adjustments (TCS-TEL-001 section 19).
    1. Validate inputs.
    2. Compute TIS_raw from weighted dimensions.
    3. Compute all five penalty components.
    4. Aggregate penalty with lambda weights (capped at 0.50).
    5. Compute TIS_adj = TIS_raw * (1 - P).
    6. Evaluate gate across gate_set.
    7. Apply decay factor exp(-mu * dt).
    8. Apply invalidation: force is_valid to 0 if event is in E_inv.
    9. Compute TIS_current = TIS_adj * decay * gate * is_valid.
    10. Compute valid_until from decay half-life.

    All arithmetic runs at full float precision internally. Rounding to
    four decimal places happens only when populating the returned TISResult.
    """
    ...  # Implementation in tcs/tis_engine.py
