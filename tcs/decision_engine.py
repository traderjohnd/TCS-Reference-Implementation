"""
tcs.decision_engine
===================

Pure logic: map a :class:`TISResult` to an enforcement decision.

Implements the priority-ordered decision function from TCS_SPEC.md §12
with the Option A threshold reconciliation (J. DeRudder, April 2026 —
see PHASE_2_PLAN.md "Spec Reconciliation Items" for the rationale).

No computation, no mutation, no reordering. Every branch is commented
with its priority number and the spec rule it enforces.

    Priority 1:  is_valid == 0                                  -> Stop
    Priority 2:  gate == 0 and C3 == 0.00                       -> Stop
    Priority 3:  gate == 0 and tis_raw > kappa                  -> Stop
    Priority 4:  gate == 0 and tis_raw <= kappa                 -> Hold (gate path)
    Priority 5:  gate == 1 and tis_current < theta_escalate     -> Escalate
    Priority 6:  gate == 1 and tis_current < theta_hold         -> Hold (score path)
    Priority 7:  gate == 1 and tis_current < theta_allow
                 and risk_tier == 'r1'                          -> Observe
    Priority 8:  gate == 1 and tis_current >= theta_allow       -> Allow

This ordering is load-bearing. C-P.10 (TCS_SPEC.md §13) explicitly
prohibits reordering the priority list.

-------------------------------------------------------------------------
SEMANTIC DISTINCTION — Hold vs Escalate vs Observe
-------------------------------------------------------------------------

Hold and Escalate are not the same state. They map to distinct real-world
workflows at different urgency levels:

    Escalate (score path, gate=1, TIS < theta_escalate):
        "The score cleared the gate minimums but is too low for comfort —
         get a human now." Routed to immediate-attention review queue.
        Example at r3: TIS = 0.68 — gates passed, composite dangerous.

    Hold (gate path, gate=0, TIS_raw <= kappa):
        "A specific fixable gap — process team remediates and recomputes."
        Example: attribution gap at a market data vendor boundary.

    Hold (score path, gate=1, theta_escalate <= TIS < theta_hold):
        "The composite is above the concern floor but not enough to
         auto-approve — standard human review queue." Only available at r1.
        At r2/r3 this path is architecturally dead by design (see below).

    Observe (gate=1, theta_hold <= TIS < theta_allow, r1 only):
        "Low-risk output, admit with monitoring." No human action required.

-------------------------------------------------------------------------
THRESHOLD SPACING AT r1 vs r2/r3 (Option A design)
-------------------------------------------------------------------------

Option A uses three-scalar threshold spacing with tier-specific meaning:

    r1: theta_escalate=0.55, theta_hold=0.65, theta_allow=0.75
    r2: theta_escalate=0.65, theta_hold=0.80, theta_allow=0.80
    r3: theta_escalate=0.70, theta_hold=0.85, theta_allow=0.85

At r1 the three thresholds are all distinct, producing four score-driven
zones below Allow: Escalate < 0.55, Hold [0.55, 0.65), Observe
[0.65, 0.75), Allow >= 0.75. Hold and Observe are both non-blocking
states at different urgency levels — Hold is the standard review queue,
Observe is admit-with-monitoring.

At r2 and r3, theta_hold == theta_allow intentionally. This collapses
Observe to zero width (Priority 7 becomes unreachable at those tiers —
correct, because you do not "observe and monitor" a regulated decision
at high risk). Priority 6 still fires: any gate=1 subject with
tis_current in [theta_escalate, theta_allow) lands in Hold via P6.

The semantic difference between r2/r3 Hold via score path (P6) vs gate
path (P4) remains meaningful in production workflows:

    P4 Hold (gate path): a specific fixable gap — process team remediates
        the attribution/context problem and recomputes. Not a human
        review; a process remediation ticket.

    P6 Hold (score path): gates passed but composite is below the allow
        floor — standard human review queue, no specific gap to fix.

At r1, the range [theta_escalate, theta_allow) splits between P6 (Hold,
lower half) and P7 (Observe, upper half). At r2/r3, P6 owns the whole
range and P7 is structurally unreachable — intentional, because Observe
does not exist at high risk.

-------------------------------------------------------------------------
requires_human_review — the full OR rule
-------------------------------------------------------------------------

Implements ARCHITECTURE.md §"decision_engine.py" verbatim:

    requires_human_review = (
        decision in ("Hold", "Escalate")                                 # remediation
        or novelty_score > 0.50                                          # novelty trigger
        or (decision == "Allow" and tis_current < theta_allow + 0.05)    # near-boundary Allow
    )

Stop decisions never require review — hard stops are not reviewable;
they must be remediated upstream, not overridden. The near-boundary
clause means any Allow within 0.05 of theta_allow gets a human confirmation
pass, which is the correct governance story for borderline approvals.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from tcs.tis_engine import TISInput, TISResult


# --------------------------------------------------------------------------- #
# Constants                                                                    #
# --------------------------------------------------------------------------- #

#: Width of the "near-boundary Allow" band that triggers human review.
#: A subject whose TIS_current falls in [theta_allow, theta_allow + BAND)
#: is marked for review even though it cleared the allow threshold.
NEAR_BOUNDARY_ALLOW_BAND: float = 0.05

#: Width of the "near-boundary Allow" band that triggers enhanced logging.
#: An Allow within this distance of theta_allow gets enhanced_logging=True.
ENHANCED_LOGGING_BAND: float = 0.05


# --------------------------------------------------------------------------- #
# Extended decision metadata (Phase 3 — nine-outcome model)                    #
# --------------------------------------------------------------------------- #

@dataclass
class DecisionMetadata:
    """
    Extended metadata produced by the nine-outcome decision model.

    The five original outcomes (Allow, Observe, Hold, Escalate, Stop) are
    unchanged. Four new qualified outcomes refine Allow and Stop:

        Allow_with_logging    — TIS within ENHANCED_LOGGING_BAND of theta_allow
        Allow_with_redaction  — T2/T3 data present in output
        Allow_with_step_up    — authorization_tier T2 requesting T3 data
        Rollback              — Stop where action has partially executed

    The ``base_decision`` field always contains one of the five original
    outcomes. The ``qualified_decision`` field contains the refined outcome
    (or equals base_decision when no qualifier applies).

    TC fields added per Phase 3 spec Step 1:
        enhanced_logging, reason_code, proximity_to_threshold,
        redaction_applied, redacted_fields, redaction_scope,
        step_up_required, step_up_completed,
        compensation_scope, incident_id, recovery_mode_activated
    """
    base_decision: str
    qualified_decision: str

    # Allow_with_logging fields
    enhanced_logging: bool = False
    reason_code: Optional[str] = None
    proximity_to_threshold: Optional[float] = None

    # Allow_with_redaction fields
    redaction_applied: bool = False
    redacted_fields: List[str] = field(default_factory=list)
    redaction_scope: Optional[str] = None

    # Allow_with_step_up fields
    step_up_required: bool = False
    step_up_completed: Optional[bool] = None

    # Rollback fields
    compensation_scope: Optional[str] = None
    incident_id: Optional[str] = None
    recovery_mode_activated: bool = False


# --------------------------------------------------------------------------- #
# Decision mapping                                                             #
# --------------------------------------------------------------------------- #

def map_decision(
    tis_input: TISInput,
    tis_result: TISResult,
) -> Tuple[str, bool]:
    """
    Map a TIS computation to an enforcement decision.

    Returns a 2-tuple ``(decision, requires_human_review)``. The decision
    string is one of the five base outcomes: ``"Allow"``, ``"Observe"``,
    ``"Hold"``, ``"Escalate"``, ``"Stop"``.

    This function implements TCS_SPEC.md §12 verbatim under the Option A
    threshold values. Do not reorder the branches. Do not add new
    conditions. If a new outcome type is required, update TCS_SPEC.md
    first and then update this function — never the reverse.

    For the nine-outcome model, call :func:`map_decision_extended` instead.
    """
    profile = tis_input.policy_profile

    tis_current = tis_result.tis_current
    tis_raw     = tis_result.tis_raw
    gate        = tis_result.gate_result
    c3_score    = tis_result.C3_score
    is_valid    = tis_result.is_valid

    kappa            = profile.soft_hold_ceiling
    theta_allow      = profile.theta_allow
    theta_hold       = profile.theta_hold
    theta_escalate   = profile.theta_escalate
    risk_tier        = profile.risk_tier

    decision = _apply_priority_ladder(
        is_valid=is_valid,
        gate=gate,
        c3_score=c3_score,
        tis_raw=tis_raw,
        tis_current=tis_current,
        kappa=kappa,
        theta_allow=theta_allow,
        theta_hold=theta_hold,
        theta_escalate=theta_escalate,
        risk_tier=risk_tier,
    )

    novelty_score = float(tis_input.context_metadata.get("novelty_score", 0.0))
    requires_human_review = _requires_human_review(
        decision=decision,
        novelty_score=novelty_score,
        tis_current=tis_current,
        theta_allow=theta_allow,
    )

    return decision, requires_human_review


def map_decision_extended(
    tis_input: TISInput,
    tis_result: TISResult,
) -> Tuple[str, bool, DecisionMetadata]:
    """
    Nine-outcome decision model (Phase 3).

    Returns a 3-tuple ``(decision, requires_human_review, metadata)``.
    ``decision`` is the base five-outcome decision (unchanged from Phase 1/2).
    ``metadata.qualified_decision`` is one of nine outcomes:

        Allow, Observe, Hold, Escalate, Stop,
        Allow_with_logging, Allow_with_redaction,
        Allow_with_step_up, Rollback

    Qualifiers are applied *after* the priority ladder — the ladder itself
    is unchanged and all existing tests continue to pass.
    """
    decision, requires_human_review = map_decision(tis_input, tis_result)
    meta = tis_input.context_metadata

    profile = tis_input.policy_profile
    theta_allow = profile.theta_allow
    tis_current = tis_result.tis_current

    dm = DecisionMetadata(
        base_decision=decision,
        qualified_decision=decision,
    )

    # --- Qualify Allow decisions ------------------------------------------- #
    if decision == "Allow":
        proximity = tis_current - theta_allow

        # 1. Allow_with_step_up: authorization_tier T2 requesting T3 data
        auth_tier = str(meta.get("authorization_tier", "T1"))
        sensitivity_tier = str(meta.get("sensitivity_tier", "T1"))
        if auth_tier == "T2" and sensitivity_tier == "T3":
            dm.qualified_decision = "Allow_with_step_up"
            dm.step_up_required = True
            dm.step_up_completed = None  # pending until auth confirmed
            dm.reason_code = "step_up_t2_requesting_t3"

        # 2. Allow_with_redaction: T2 or T3 data present in output
        elif sensitivity_tier in ("T2", "T3") and meta.get("redaction_required"):
            redacted = list(meta.get("redacted_fields", []))
            dm.qualified_decision = "Allow_with_redaction"
            dm.redaction_applied = True
            dm.redacted_fields = redacted
            dm.redaction_scope = str(meta.get("redaction_scope", "output"))
            dm.reason_code = f"redaction_{sensitivity_tier.lower()}_data"

        # 3. Allow_with_logging: TIS within ENHANCED_LOGGING_BAND of theta_allow
        elif proximity < ENHANCED_LOGGING_BAND:
            dm.qualified_decision = "Allow_with_logging"
            dm.enhanced_logging = True
            dm.proximity_to_threshold = round(proximity, 4)
            dm.reason_code = "near_boundary_allow"

    # --- Qualify Stop decisions -------------------------------------------- #
    elif decision == "Stop":
        # 4. Rollback: action has already partially executed
        if meta.get("action_partially_executed"):
            dm.qualified_decision = "Rollback"
            dm.compensation_scope = str(meta.get("compensation_scope", "full"))
            dm.incident_id = str(meta.get("incident_id", ""))
            dm.recovery_mode_activated = True
            dm.reason_code = "rollback_partial_execution"

    return decision, requires_human_review, dm


# --------------------------------------------------------------------------- #
# Priority ladder                                                              #
# --------------------------------------------------------------------------- #

def _apply_priority_ladder(
    *,
    is_valid: int,
    gate: int,
    c3_score: float,
    tis_raw: float,
    tis_current: float,
    kappa: float,
    theta_allow: float,
    theta_hold: float,
    theta_escalate: float,
    risk_tier: str,
) -> str:
    """
    Walk the decision priority ladder from TCS_SPEC.md §12.

    All arguments are keyword-only so the caller cannot accidentally swap
    positional arguments (a subtle but dangerous bug class for a file this
    load-bearing).
    """

    # ----- Priority 1: Invalidation (absolute) --------------------------- #
    # An invalidation event strips trust irrespective of all other terms.
    # Fires before the gate check, before C3, before every threshold.
    if is_valid == 0:
        return "Stop"

    # ----- Priority 2: Hard safety violation (C3 = 0.00) ----------------- #
    # C-P.8: the soft-hold ceiling kappa does NOT apply when C3 = 0.00.
    # This is the only condition that can produce Stop with a sub-kappa
    # TIS_raw when the gate has collapsed.
    if gate == 0 and c3_score == 0.00:
        return "Stop"

    # ----- Priority 3: Gate failure above soft-hold ceiling -------------- #
    # Gate collapsed and the pre-penalty composite is too high to rehabilitate.
    if gate == 0 and tis_raw > kappa:
        return "Stop"

    # ----- Priority 4: Gate failure within soft-hold ceiling ------------- #
    # Gate path Hold: a specific fixable gap (missing attribution, stale
    # context, etc.). Routed to process remediation, not human review.
    if gate == 0 and tis_raw <= kappa:
        return "Hold"

    # ----- Priority 5: Below escalate threshold -------------------------- #
    # Gate passed but the operative score is so low that routing to an
    # urgent human review queue is the only responsible outcome.
    if gate == 1 and tis_current < theta_escalate:
        return "Escalate"

    # ----- Priority 6: Score-path Hold ----------------------------------- #
    # Gate passed, score is above Escalate territory but below theta_hold.
    # Routed to the standard human review queue (distinct from P4 gate-path
    # Hold which goes to process remediation).
    #
    # Under Option A:
    #   r1: fires for tis_current in [0.55, 0.65) — genuine Hold band.
    #   r2: fires for tis_current in [0.65, 0.80) — full score-path Hold.
    #   r3: fires for tis_current in [0.70, 0.85) — full score-path Hold.
    # See the module docstring "THRESHOLD SPACING AT r1 vs r2/r3" for why
    # this branch catches different ranges at different tiers.
    if gate == 1 and tis_current < theta_hold:
        return "Hold"

    # ----- Priority 7: Observe (r1-only) --------------------------------- #
    # Observe is an r1-only admissible-with-monitoring state, covering the
    # upper half of the r1 sub-allow zone: [theta_hold=0.65, theta_allow=0.75).
    # At r2/r3 this branch is structurally unreachable because Option A
    # sets theta_hold == theta_allow at those tiers — Priority 6 already
    # consumed everything below theta_allow. That is intentional: Observe
    # does not exist at r2/r3.
    if gate == 1 and tis_current < theta_allow and risk_tier == "r1":
        return "Observe"

    # ----- Priority 8: Allow --------------------------------------------- #
    if gate == 1 and tis_current >= theta_allow:
        return "Allow"

    # Should be unreachable. If we land here, the input space has a case
    # the spec did not cover. Raise loudly — silent fallbacks would defeat
    # the entire point of a verification-grade implementation.
    raise ValueError(
        "Decision logic exhausted without resolution. "
        f"gate={gate} is_valid={is_valid} c3={c3_score} "
        f"tis_raw={tis_raw} tis_current={tis_current} "
        f"kappa={kappa} theta_allow={theta_allow} theta_hold={theta_hold} "
        f"theta_escalate={theta_escalate} risk_tier={risk_tier}"
    )


# --------------------------------------------------------------------------- #
# Human review flag                                                            #
# --------------------------------------------------------------------------- #

def _requires_human_review(
    *,
    decision: str,
    novelty_score: float,
    tis_current: float,
    theta_allow: float,
) -> bool:
    """
    Decide whether this evaluation needs human review.

    Rule (full OR, per ARCHITECTURE.md §"decision_engine.py"):

        - Hold and Escalate          -> always True (remediation or review)
        - Allow/Observe + novelty>0.50 -> True (novelty trigger)
        - Allow + near-boundary        -> True (score just barely cleared)
        - Stop                         -> False (hard stops are not reviewable)

    The near-boundary Allow band is NEAR_BOUNDARY_ALLOW_BAND (default 0.05).
    An Allow whose TIS_current is less than theta_allow + 0.05 is flagged
    for human confirmation — the governance premise being that a score
    that just barely cleared the bar deserves a pair of eyes before the
    output reaches a consequential surface.
    """
    # Stop is never reviewable — hard stops must be remediated upstream.
    if decision == "Stop":
        return False

    if decision in ("Hold", "Escalate"):
        return True

    # Allow / Observe branch.
    if novelty_score > 0.50:
        return True

    if decision == "Allow" and tis_current < theta_allow + NEAR_BOUNDARY_ALLOW_BAND:
        return True

    return False
