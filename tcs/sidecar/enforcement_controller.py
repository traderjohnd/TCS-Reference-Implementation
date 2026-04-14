"""
tcs.sidecar.enforcement_controller
==================================

Map a governance outcome (decision + TC) to a ``GovernedResponse`` the
calling application consumes as authoritative. Pure logic, no
computation, no persistence.

Two entry points:

    enforce(decision, candidate_output, tc, risk_tier) -> GovernedResponse
        Standard happy path. Converts a completed evaluation into a
        runtime response — allowing, observing, holding, escalating,
        or stopping the candidate output per the decision.

    enforce_fail_safe(failure_type, risk_tier, *, candidate_output,
                      request_id, reason) -> GovernedResponse
        Governance infrastructure failed (couldn't assemble context,
        couldn't load policy, couldn't write a TC, etc.). Maps the
        (failure_type, risk_tier) pair through ``FAIL_SAFE_RULES``
        and produces a ``GovernedResponse`` carrying the fail-safe
        outcome — C-R.17 forbids silent failure.

Decision semantics:

    +-----------+--------+----------+--------------+-----------+
    | Decision  | output | blocked  | requires_rev | monitoring|
    +-----------+--------+----------+--------------+-----------+
    | Allow     | passed | False    | per TC       | False     |
    | Observe   | passed | False    | per TC       | True      |
    | Hold      | None   | True     | True         | False     |
    | Escalate  | None   | True     | True         | False     |
    | Stop      | None   | True     | False        | False     |
    +-----------+--------+----------+--------------+-----------+

Fail-safe semantics (6 outcomes mapped to 3 response shapes):

    stop                      -> blocked=True, output=None
    hold                      -> blocked=True, output=None
    allow_with_flag           -> blocked=False, governance_degraded=True
    allow_queue               -> blocked=False, governance_degraded=True
    allow_max_flag            -> blocked=False, governance_degraded=True
    canonical_defaults        -> blocked=False, governance_degraded=True
    degraded_allow            -> blocked=False, governance_degraded=True
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from tcs.governed_context import FAIL_SAFE_RULES, apply_fail_safe
from tcs.trust_certificate import TrustCertificate


# --------------------------------------------------------------------------- #
# GovernedResponse                                                             #
# --------------------------------------------------------------------------- #

@dataclass
class GovernedResponse:
    """
    The structured response the calling application receives from the
    sidecar. Every field is set deterministically by the enforcement
    controller — no optional semantics, no "sometimes present".

    Fields:

        request_id              originating request ID for audit
        decision                Allow | Observe | Hold | Escalate | Stop |
                                (fail_safe variant strings, see below)
        output                  the text to return to the user, or None
                                if blocked
        blocked                 True iff the candidate output is withheld
        certificate_id          issued TC (None on fail-safe paths where
                                no TC was committed)
        monitoring              True for Observe — app should run
                                lightweight monitoring on the passed
                                output
        requires_human_review   True for Hold / Escalate / near-boundary
                                Allow / novelty > 0.50 — drives the
                                review queue
        escalation_routed_to    list of review destinations (empty
                                unless Escalate)
        governance_degraded     True on any fail-safe path; the app
                                should show a governance-degraded
                                banner and log elevated telemetry
        fail_safe_applied       True iff this response came from
                                enforce_fail_safe()
        fail_safe_type          behavior category per TCS_SPEC.md §19:
                                "fail_closed" | "fail_open_with_flag" |
                                "degraded_allow" | "degraded_hold" | None
        fail_safe_trigger       the trigger that fired the fail-safe —
                                one of FAIL_SAFE_RULES keys
                                (dimension_missing / policy_unavailable /
                                gca_failure / tc_write_failure /
                                identity_provider_down / tcs_offline) or
                                None
        fail_safe_outcome       the raw outcome string from
                                FAIL_SAFE_RULES (stop / hold /
                                allow_with_flag / ...) or None
        message                 plain-language message for the user /
                                downstream consumer
        blocking_reason         short machine-readable reason when
                                blocked=True (from TC or fail-safe)
        issued_at               ISO-8601 UTC when the response was built
    """
    request_id: str
    decision: str
    output: Optional[str]
    blocked: bool
    certificate_id: Optional[str]
    monitoring: bool = False
    requires_human_review: bool = False
    escalation_routed_to: List[str] = field(default_factory=list)
    governance_degraded: bool = False
    fail_safe_applied: bool = False
    fail_safe_type: Optional[str] = None
    fail_safe_trigger: Optional[str] = None
    fail_safe_outcome: Optional[str] = None
    message: str = ""
    blocking_reason: Optional[str] = None
    issued_at: str = ""

    def to_dict(self) -> Dict[str, Any]:
        """Serialize for the FastAPI route layer."""
        return {
            "request_id": self.request_id,
            "decision": self.decision,
            "output": self.output,
            "blocked": self.blocked,
            "certificate_id": self.certificate_id,
            "monitoring": self.monitoring,
            "requires_human_review": self.requires_human_review,
            "escalation_routed_to": list(self.escalation_routed_to),
            "governance_degraded": self.governance_degraded,
            "fail_safe_applied": self.fail_safe_applied,
            "fail_safe_type": self.fail_safe_type,
            "fail_safe_trigger": self.fail_safe_trigger,
            "fail_safe_outcome": self.fail_safe_outcome,
            "message": self.message,
            "blocking_reason": self.blocking_reason,
            "issued_at": self.issued_at,
        }


# --------------------------------------------------------------------------- #
# Constants                                                                    #
# --------------------------------------------------------------------------- #

#: Outcomes that block the candidate output. Everything else passes it
#: through, possibly with a degraded-governance flag.
_BLOCKING_FAIL_SAFE_OUTCOMES = frozenset({"stop", "hold"})

#: All non-blocking fail-safe outcomes. Used for defensive validation
#: against typos in the FAIL_SAFE_RULES table.
_PASSTHROUGH_FAIL_SAFE_OUTCOMES = frozenset({
    "allow_with_flag",
    "canonical_defaults",
    "allow_queue",
    "degraded_allow",
    "allow_max_flag",
})

#: Map from the raw outcome string in FAIL_SAFE_RULES to the behavior
#: category defined in TCS_SPEC.md §19. Category vocabulary:
#:
#:     fail_closed         — hard stop
#:     fail_open_with_flag — pass output with a governance flag
#:     degraded_allow      — allow with a "degraded" marker
#:     degraded_hold       — hold with a "degraded" marker
#:
#: This is the value GovernedResponse.fail_safe_type carries.
_FAIL_SAFE_CATEGORY: Dict[str, str] = {
    "stop":               "fail_closed",
    "hold":               "degraded_hold",
    "allow_with_flag":    "fail_open_with_flag",
    "allow_queue":        "fail_open_with_flag",
    "allow_max_flag":     "fail_open_with_flag",
    "canonical_defaults": "degraded_allow",
    "degraded_allow":     "degraded_allow",
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --------------------------------------------------------------------------- #
# EnforcementController                                                        #
# --------------------------------------------------------------------------- #

class EnforcementController:
    """
    Stateless mapper from (decision, TC) to GovernedResponse. The class
    wrapper exists so Phase 3 can inject policy-specific overrides
    (e.g. alternate messaging per domain) without touching call sites.

    In Phase 2 the two module-level functions ``enforce`` and
    ``enforce_fail_safe`` delegate to a default instance.
    """

    # ---- Standard decision path ---------------------------------------- #

    def enforce(
        self,
        decision: str,
        candidate_output: str,
        tc: TrustCertificate,
        risk_tier: str,
        *,
        request_id: Optional[str] = None,
    ) -> GovernedResponse:
        """
        Map a completed evaluation to a GovernedResponse.

        ``decision`` must match ``tc.decision``. ``risk_tier`` is taken
        as an explicit argument (rather than read from the TC) to keep
        the call signature compatible with CLAUDE.md Step 4 spec:
        "enforce(decision, output, tc, risk_tier) -> GovernedResponse".
        """
        if decision != tc.decision:
            raise ValueError(
                f"decision mismatch: argument={decision!r} "
                f"tc.decision={tc.decision!r}"
            )
        if decision not in ("Allow", "Observe", "Hold", "Escalate", "Stop"):
            raise ValueError(f"Unknown decision {decision!r}")

        req_id = request_id or tc.certificate_id
        iso_now = _now_iso()

        if decision == "Allow":
            return GovernedResponse(
                request_id=req_id,
                decision="Allow",
                output=candidate_output,
                blocked=False,
                certificate_id=tc.certificate_id,
                monitoring=False,
                requires_human_review=tc.requires_human_review,
                escalation_routed_to=list(tc.escalation_routed_to),
                governance_degraded=False,
                message=self._allow_message(tc),
                blocking_reason=None,
                issued_at=iso_now,
            )

        if decision == "Observe":
            return GovernedResponse(
                request_id=req_id,
                decision="Observe",
                output=candidate_output,
                blocked=False,
                certificate_id=tc.certificate_id,
                monitoring=True,
                requires_human_review=tc.requires_human_review,
                escalation_routed_to=list(tc.escalation_routed_to),
                governance_degraded=False,
                message=self._observe_message(tc),
                blocking_reason=None,
                issued_at=iso_now,
            )

        if decision == "Hold":
            return GovernedResponse(
                request_id=req_id,
                decision="Hold",
                output=None,
                blocked=True,
                certificate_id=tc.certificate_id,
                monitoring=False,
                requires_human_review=True,
                escalation_routed_to=list(tc.escalation_routed_to),
                governance_degraded=False,
                message=self._hold_message(tc),
                blocking_reason=tc.blocking_reason,
                issued_at=iso_now,
            )

        if decision == "Escalate":
            return GovernedResponse(
                request_id=req_id,
                decision="Escalate",
                output=None,
                blocked=True,
                certificate_id=tc.certificate_id,
                monitoring=False,
                requires_human_review=True,
                escalation_routed_to=list(tc.escalation_routed_to),
                governance_degraded=False,
                message=self._escalate_message(tc),
                blocking_reason=tc.blocking_reason,
                issued_at=iso_now,
            )

        # Stop
        return GovernedResponse(
            request_id=req_id,
            decision="Stop",
            output=None,
            blocked=True,
            certificate_id=tc.certificate_id,
            monitoring=False,
            requires_human_review=False,  # hard stops are not reviewable
            escalation_routed_to=list(tc.escalation_routed_to),
            governance_degraded=False,
            message=self._stop_message(tc),
            blocking_reason=tc.blocking_reason,
            issued_at=iso_now,
        )

    # ---- Fail-safe path ------------------------------------------------- #

    def enforce_fail_safe(
        self,
        failure_type: str,
        risk_tier: str,
        *,
        candidate_output: Optional[str] = None,
        request_id: Optional[str] = None,
        reason: Optional[str] = None,
    ) -> GovernedResponse:
        """
        Produce a GovernedResponse when governance infrastructure failed.

        Steps:
            1. Look up FAIL_SAFE_RULES[failure_type][risk_tier] via
               apply_fail_safe() — this raises on unknown failure or
               tier, which is itself a governance error.
            2. Map the outcome string to response fields.
            3. Return a GovernedResponse with fail_safe_applied=True
               and no certificate_id (no TC was committed).

        Blocking outcomes (stop / hold) withhold the candidate_output.
        Pass-through outcomes (allow_with_flag / canonical_defaults /
        allow_queue / degraded_allow / allow_max_flag) return the
        candidate_output with governance_degraded=True so the calling
        application can display a governance banner.

        Raises:
            FailSafeLookupError — on unknown (failure_type, risk_tier)
                pairs. Propagated from apply_fail_safe.
        """
        outcome = apply_fail_safe(failure_type, risk_tier)
        category = _FAIL_SAFE_CATEGORY.get(outcome)
        if category is None:
            # Defensive: an outcome not in the category map means
            # FAIL_SAFE_RULES gained a new value and this mapper was
            # not updated.
            raise ValueError(
                f"Unmapped fail-safe outcome {outcome!r} for "
                f"({failure_type}, {risk_tier}). "
                f"Known categories: {sorted(_FAIL_SAFE_CATEGORY.keys())}."
            )

        req_id = request_id or f"failsafe-{failure_type}-{risk_tier}"
        iso_now = _now_iso()

        # Map outcome to response shape.
        if outcome == "stop":
            return GovernedResponse(
                request_id=req_id,
                decision="Stop",
                output=None,
                blocked=True,
                certificate_id=None,
                monitoring=False,
                requires_human_review=False,
                escalation_routed_to=[],
                governance_degraded=True,
                fail_safe_applied=True,
                fail_safe_type=category,            # "fail_closed"
                fail_safe_trigger=failure_type,     # e.g. "policy_unavailable"
                fail_safe_outcome=outcome,          # "stop"
                message=(
                    f"Governance infrastructure failure ({failure_type}) "
                    f"at {risk_tier}. Fail-safe category: {category}. "
                    f"Candidate output withheld."
                ),
                blocking_reason=f"fail_safe_stop_{failure_type}",
                issued_at=iso_now,
            )

        if outcome == "hold":
            return GovernedResponse(
                request_id=req_id,
                decision="Hold",
                output=None,
                blocked=True,
                certificate_id=None,
                monitoring=False,
                requires_human_review=True,
                escalation_routed_to=[],
                governance_degraded=True,
                fail_safe_applied=True,
                fail_safe_type=category,            # "degraded_hold"
                fail_safe_trigger=failure_type,
                fail_safe_outcome=outcome,
                message=(
                    f"Governance infrastructure failure ({failure_type}) "
                    f"at {risk_tier}. Fail-safe category: {category}. "
                    f"Output queued for human review."
                ),
                blocking_reason=f"fail_safe_hold_{failure_type}",
                issued_at=iso_now,
            )

        # Pass-through outcomes (allow_with_flag / canonical_defaults /
        # allow_queue / degraded_allow / allow_max_flag).
        label = outcome.replace("_", " ")
        return GovernedResponse(
            request_id=req_id,
            decision="Allow",
            output=candidate_output,
            blocked=False,
            certificate_id=None,
            monitoring=False,
            requires_human_review=False,
            escalation_routed_to=[],
            governance_degraded=True,
            fail_safe_applied=True,
            fail_safe_type=category,                # fail_open_with_flag or degraded_allow
            fail_safe_trigger=failure_type,
            fail_safe_outcome=outcome,
            message=(
                f"Governance degraded ({failure_type}) at {risk_tier}. "
                f"Fail-safe category: {category}. Fail-safe outcome: "
                f"{label}. Output passed with governance flag."
            ),
            blocking_reason=None,
            issued_at=iso_now,
        )

    # ---- Message helpers ------------------------------------------------ #

    @staticmethod
    def _allow_message(tc: TrustCertificate) -> str:
        return (
            f"Output approved. TIS_current={tc.tis_current:.4f} "
            f"(theta_allow satisfied). Governance record: {tc.certificate_id}."
        )

    @staticmethod
    def _observe_message(tc: TrustCertificate) -> str:
        return (
            f"Output approved with monitoring. "
            f"TIS_current={tc.tis_current:.4f}. "
            f"Active lifecycle monitoring engaged. "
            f"Governance record: {tc.certificate_id}."
        )

    @staticmethod
    def _hold_message(tc: TrustCertificate) -> str:
        return (
            f"Output withheld pending governance review. "
            f"Reason: {tc.blocking_reason or 'score below allow threshold'}. "
            f"Governance record: {tc.certificate_id}."
        )

    @staticmethod
    def _escalate_message(tc: TrustCertificate) -> str:
        routed = ", ".join(tc.escalation_routed_to) or "governance review"
        return (
            f"Output escalated for urgent human review. "
            f"Routed to: {routed}. "
            f"TIS_current={tc.tis_current:.4f}. "
            f"Governance record: {tc.certificate_id}."
        )

    @staticmethod
    def _stop_message(tc: TrustCertificate) -> str:
        return (
            f"Output blocked — hard stop. "
            f"Reason: {tc.blocking_reason or tc.failure_mode or 'governance violation'}. "
            f"No human override available. "
            f"Governance record: {tc.certificate_id}."
        )


# --------------------------------------------------------------------------- #
# Module-level convenience                                                     #
# --------------------------------------------------------------------------- #

_DEFAULT_CONTROLLER = EnforcementController()


def enforce(
    decision: str,
    candidate_output: str,
    tc: TrustCertificate,
    risk_tier: str,
    *,
    request_id: Optional[str] = None,
) -> GovernedResponse:
    """Module-level ``EnforcementController.enforce``."""
    return _DEFAULT_CONTROLLER.enforce(
        decision, candidate_output, tc, risk_tier, request_id=request_id
    )


def enforce_fail_safe(
    failure_type: str,
    risk_tier: str,
    *,
    candidate_output: Optional[str] = None,
    request_id: Optional[str] = None,
    reason: Optional[str] = None,
) -> GovernedResponse:
    """Module-level ``EnforcementController.enforce_fail_safe``."""
    return _DEFAULT_CONTROLLER.enforce_fail_safe(
        failure_type,
        risk_tier,
        candidate_output=candidate_output,
        request_id=request_id,
        reason=reason,
    )
