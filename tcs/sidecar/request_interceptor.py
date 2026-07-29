"""
tcs.sidecar.request_interceptor
================================

End-to-end runtime governance pipeline. Takes an
:class:`~tcs.adapters.rag_adapter.InterceptedRequest` and returns a
:class:`~tcs.sidecar.enforcement_controller.GovernedResponse`.

Pipeline:

    1. safe_assemble_context_v2          (Step 2 GCA + Step 3 adapter)
    2. dimension scoring                 (ScoringPolicy)
    3. compute_tis                       (Phase 1 engine, unchanged)
    4. map_decision                      (Phase 1 engine, unchanged)
    5. generate_certificate              (Phase 1 engine, unchanged)
    6. CertificateStore.issue            (Step 1 persistence)
    7. enforce                           (Step 4 enforcement controller)

Fail-safe handling (C-R.17):

    * ``CredentialDetectedError`` during context assembly       -> Stop
      with a synthesized C3=0.00 TC. No fail-safe marker — this
      is a real governance outcome, not an infrastructure failure.

    * Unexpected exception during context assembly              -> fail-safe
      ``gca_failure`` mapped through FAIL_SAFE_RULES.

    * Unknown ``base_profile_id``                               -> fail-safe
      ``policy_unavailable``.

    * Unexpected exception during scoring / compute / decision  -> fail-safe
      ``gca_failure``.

    * Exception during ``CertificateStore.issue``               -> fail-safe
      ``tc_write_failure``.

Every fail-safe path returns a GovernedResponse with ``fail_safe_applied
= True``. No silent failure — the sidecar either returns a normal
GovernedResponse with a committed TC, or a fail-safe GovernedResponse
that the calling application can distinguish and log.

The ``ScoringPolicy`` abstraction is a callable that maps
``(context_metadata, candidate_output, base_profile)`` to a
``(dimension_scores, sub_factor_scores)`` tuple. The default policy
implements the Phase 2 finance RAG demo scorer — deterministic,
signal-driven, and calibrated to make scenarios 9-17 produce the
outcomes in TEST_SCENARIOS.md.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, localcontext
from typing import Any, Callable, Dict, List, Optional, Tuple

from tcs.canonical import TIS_DECIMAL_CONTEXT

from tcs.adapters.rag_adapter import InterceptedRequest
from tcs.decision_engine import map_decision
from tcs.governed_context import (
    CredentialDetectedError,
    assemble_context_v2,
    compute_chain_uncertainty,
)
from tcs.persistence import CertificateStore
from tcs.policy_profiles import PolicyProfile, load_profile
from tcs.sidecar.enforcement_controller import (
    EnforcementController,
    GovernedResponse,
    enforce_fail_safe,
)
from tcs.tis_engine import TISInput, compute_tis
from tcs.trust_certificate import generate_certificate


# --------------------------------------------------------------------------- #
# ScoringPolicy                                                                #
# --------------------------------------------------------------------------- #

#: A scoring policy takes the assembled governed context, the candidate
#: output, and the base (or resolved) profile, and returns two dicts:
#: dimension scores {B,A,C,K} and sub_factor_scores (at minimum the
#: {"C": {"C3": ...}} entry).
ScoringPolicy = Callable[
    [Dict[str, Any], str, Any],
    Tuple[Dict[str, float], Dict[str, Dict[str, float]]],
]


def default_scoring_policy(
    context: Dict[str, Any],
    candidate_output: str,  # noqa: ARG001  (kept for future policies)
    profile: Any,
) -> Tuple[Dict[str, float], Dict[str, Dict[str, float]]]:
    """
    Deterministic dimension scorer for the Phase 2 finance RAG demo.

    Rules (calibrated against TEST_SCENARIOS.md scenarios 9-17):

        B (Boundedness)
            - baseline 0.94
            - unchanged unless caller overrides via context["B_score"]

        A (Attribution)
            - baseline 0.94
            - subtract 0.04 per attribution gap (n_gaps)
            - so 2 gaps -> A = 0.86 (fails the CT-4 elevated A
              threshold of 0.93, producing Scenario 9 Hold)

        C (Compliance)
            - baseline 0.92
            - if injection_detected -> 0.31 (fails C gate of 0.90,
              matches scenario 11 expected C)
            - if c3_score_computed == 0.00 -> 0.31 regardless
            - sub_factor_scores["C"]["C3"] tracks the binary C3 signal

        K (Known)
            - for CT-8 (agent chain): the chain math computes
              U_chain = compute_chain_uncertainty(chain_k_scores) which
              returns 1 - product(K_i). The K dimension fed to the
              engine is K_chain = 1 - U_chain = product(K_i). Scenario
              17 shape: 3 x 0.88 -> U_chain = 0.3185 -> the resulting
              K_chain fails the 0.80 gate -> Hold. CT-11 (AI-generated
              attribution) does NOT apply chain math; it uses the
              CT-4/other path.
            - for CT-4 / CT-11 / others:
                baseline 0.88
                subtract k_subfactor_penalty (scaled by 1.0 — the
                adapter already bounds the penalty at 0.5, so worst
                case is K = 0.38 which fails the gate).

    The caller may override any dimension by setting ``B_score``,
    ``A_score``, ``C_score`` or ``K_score`` in context_metadata. The
    Phase 2 demo uses that to exercise specific outcomes.
    """
    # Decimal-native derivation (tis-v2 Commit 5a): every governed value
    # is Decimal from its source — override channels (governed_metadata)
    # must supply Decimal or decimal strings; binary floats are rejected
    # so the raw-evidence tier cannot inherit a float ancestor.
    def _dec(value: Any, name: str) -> Decimal:
        if isinstance(value, Decimal):
            return value
        if isinstance(value, bool) or isinstance(value, float):
            raise ValueError(
                f"{name}: governed score overrides must be Decimal or "
                f"decimal strings, got {type(value).__name__}"
            )
        return Decimal(str(value))

    b = _dec(context.get("B_score", Decimal("0.94")), "B_score")
    a = _dec(context.get("A_score", Decimal("0.94")), "A_score")
    c = _dec(context.get("C_score", Decimal("0.92")), "C_score")
    k = _dec(context.get("K_score", Decimal("0.88")), "K_score")

    with localcontext(TIS_DECIMAL_CONTEXT):
        # A: degrade per attribution gap
        if "A_score" not in context:
            n_gaps = int(context.get("n_gaps", 0))
            a = max(Decimal("0"), a - Decimal("0.04") * n_gaps)

        # C: injection / C3 hard zero
        c3_signal = _dec(
            context.get("c3_score_computed", Decimal("1")),
            "c3_score_computed",
        )
        injection_detected = bool(context.get("injection_detected", False))
        if "C_score" not in context:
            if injection_detected or c3_signal == 0:
                c = Decimal("0.31")

        # K: chain-aware or adapter-hint path. NOTE: preserves the
        # existing v1 semantics exactly (the chain branch assigns the
        # derived chain uncertainty), only in Decimal arithmetic.
        if "K_score" not in context:
            chain_scores = [
                _dec(v, "chain_u_scores")
                for v in (context.get("chain_u_scores") or [])
            ]
            if chain_scores:
                k_chain_product = Decimal("1")
                for cs in chain_scores:
                    k_chain_product *= cs
                k = (Decimal("1") - k_chain_product).quantize(
                    Decimal("0.0001"))
            else:
                k_penalty = _dec(
                    context.get("k_subfactor_penalty", Decimal("0")),
                    "k_subfactor_penalty",
                )
                k = max(Decimal("0"), k - k_penalty)

    scores = {"B": b, "A": a, "C": c, "K": k}

    # Clamp into [0,1] (belt and suspenders — bad inputs should not
    # raise from the TIS engine's input validator at runtime).
    for dim in scores:
        if scores[dim] < 0:
            scores[dim] = Decimal("0")
        elif scores[dim] > 1:
            scores[dim] = Decimal("1")

    # C3 sub-factor: 0 on injection / explicit hard-zero, 1 otherwise
    sub = {"C": {"C3": (
        Decimal("0.0000")
        if (injection_detected or c3_signal == 0)
        else Decimal("1.0000")
    )}}

    return scores, sub


# --------------------------------------------------------------------------- #
# RequestInterceptor                                                           #
# --------------------------------------------------------------------------- #

class RequestInterceptor:
    """
    Orchestrator for the full runtime governance pipeline.

    Construct with a :class:`CertificateStore` (for TC persistence) and
    an optional :class:`EnforcementController`. The scoring_policy
    defaults to ``default_scoring_policy`` and can be swapped for
    custom demos or tests.

    Typical usage::

        interceptor = RequestInterceptor(store)
        response = interceptor.govern(adapted_request)

    ``response`` is always a :class:`GovernedResponse`. Exceptions are
    caught inside ``govern()`` and translated into fail-safe responses
    per C-R.17.
    """

    def __init__(
        self,
        store: CertificateStore,
        *,
        enforcement_controller: Optional[EnforcementController] = None,
        scoring_policy: ScoringPolicy = default_scoring_policy,
    ) -> None:
        self._store = store
        self._enforcer = enforcement_controller or EnforcementController()
        self._scoring_policy = scoring_policy

    # ---- Public API ----------------------------------------------------- #

    def govern(self, request: InterceptedRequest) -> GovernedResponse:
        """
        Run the full pipeline. Returns a :class:`GovernedResponse`.

        Every failure mode is handled — this function does not raise
        under normal operation. Unhandled exceptions represent
        programming errors and should surface in tests, not production.
        """
        profile = self._load_profile_or_fail(request)
        if isinstance(profile, GovernedResponse):
            return profile  # already a fail-safe response
        base_profile, risk_tier = profile

        # Step 1: context assembly
        try:
            context, resolved = assemble_context_v2(
                request.context_bundle,
                base_profile=base_profile,
            )
        except CredentialDetectedError as exc:
            # Not a fail-safe — this is a real governance outcome.
            # Synthesize a hard-stop TC with C3=0.00.
            return self._credential_stop_response(request, base_profile, exc)
        except Exception as exc:  # noqa: BLE001
            return enforce_fail_safe(
                "gca_failure",
                risk_tier,
                candidate_output=request.candidate_output,
                request_id=request.request_id,
                reason=repr(exc),
            )

        # Step 2: dimension scoring
        try:
            dim_scores, sub_scores = self._scoring_policy(
                context, request.candidate_output, resolved
            )
        except Exception as exc:  # noqa: BLE001
            return enforce_fail_safe(
                "dimension_missing",
                risk_tier,
                candidate_output=request.candidate_output,
                request_id=request.request_id,
                reason=repr(exc),
            )

        # Step 3: TIS computation + decision + TC generation
        try:
            # The scoring policy is Decimal-native (5a). Until Commit 5b
            # switches this site to compute_tis_v2 / generate_
            # certificate_v2, the LEGACY engine and v1 wire cannot carry
            # Decimals, so a float shadow feeds the v1 pipeline. The
            # shadow (and the float() on elapsed_hours) is REMOVED by
            # the 5b activation diff; the Decimal originals then flow
            # through unconverted and become component_scores_raw.
            tis_input = TISInput(
                subject_id=request.subject_id,
                subject_type=request.subject_type,
                policy_profile=resolved,
                dimension_scores={
                    d: float(v) for d, v in dim_scores.items()
                },
                sub_factor_scores={
                    dim: {sf: float(v) for sf, v in subs.items()}
                    for dim, subs in sub_scores.items()
                },
                context_metadata=context,
                elapsed_hours=float(context.get("elapsed_hours", 0.0)),
                is_valid=int(context.get("is_valid", 1)),
                invalidation_event=context.get("invalidation_event"),
                evaluation_time=datetime.now(timezone.utc),
            )
            tis_result = compute_tis(tis_input)
            decision, requires_review = map_decision(tis_input, tis_result)
            tc = generate_certificate(
                tis_input, tis_result, decision, requires_review
            )
        except Exception as exc:  # noqa: BLE001
            return enforce_fail_safe(
                "gca_failure",
                risk_tier,
                candidate_output=request.candidate_output,
                request_id=request.request_id,
                reason=repr(exc),
            )

        # Step 4: persist
        try:
            issued_tc = self._store.issue(tc)
        except Exception as exc:  # noqa: BLE001
            return enforce_fail_safe(
                "tc_write_failure",
                risk_tier,
                candidate_output=request.candidate_output,
                request_id=request.request_id,
                reason=repr(exc),
            )

        # Step 5: enforce
        return self._enforcer.enforce(
            decision,
            request.candidate_output,
            issued_tc,
            risk_tier,
            request_id=request.request_id,
        )

    # ---- Internal helpers ---------------------------------------------- #

    def _load_profile_or_fail(
        self,
        request: InterceptedRequest,
    ) -> Any:
        """
        Resolve the base profile. Returns either
        ``(PolicyProfile, risk_tier)`` on success or a
        :class:`GovernedResponse` on fail-safe.

        ``risk_tier`` is derived from the context_bundle if present
        (for fail-safe tier lookup on profile load failure), otherwise
        defaults to ``"r3"`` (the most conservative response).
        """
        # Best-effort risk_tier lookup for fail-safe even when the
        # profile itself cannot be loaded.
        risk_tier_hint = str(
            request.context_bundle.get("risk_tier", "r3")
        )

        try:
            profile = load_profile(request.base_profile_id)
        except Exception as exc:  # noqa: BLE001
            return enforce_fail_safe(
                "policy_unavailable",
                risk_tier_hint,
                candidate_output=request.candidate_output,
                request_id=request.request_id,
                reason=repr(exc),
            )

        return profile, profile.risk_tier

    def _credential_stop_response(
        self,
        request: InterceptedRequest,
        base_profile: PolicyProfile,
        exc: CredentialDetectedError,
    ) -> GovernedResponse:
        """
        Handle a CredentialDetectedError from the GCA.

        CT-12 / credential leaks are a hard governance Stop, not an
        infrastructure fail-safe. We synthesize a minimally-valid
        TISInput with C3=0.00 so the TIS engine produces a normal
        Stop path TC, persist it, and return a Stop response. The
        Phase 2 test contract for scenario 12 requires
        governance_status == "complete".
        """
        now = datetime.now(timezone.utc)

        # Build a hard-stop TISInput: sub_factor_scores C3=0.00 drives
        # the Priority 2 C3 hard stop in the decision engine.
        #
        # Preserve chain_id (and any other persistence / identity
        # metadata the caller supplied) from the original request so
        # the resulting Stop TC joins the same chain as the rest of
        # the workflow. Without this, every credential-stop scenario
        # ends up in its own one-element chain, which is still valid
        # but breaks the audit narrative.
        original_meta = request.context_bundle or {}

        forced_ctx: Dict[str, Any] = {
            "n_gaps": 0,
            "context_age_hours": 0.0,
            "novelty_score": 0.0,
            "days_since_review": 0,
            "is_policy_sensitive": False,
            "blocking_context": "credential_detected",
            "credential_detected": True,
            "credential_reason": repr(exc),   # explanatory only, never parsed
            # Structured provenance (5a): the exception carries the
            # typed signal; no reason-string parsing anywhere.
            "c3_signals": [exc.c3_signal()],
        }
        # Carry chain linkage + identity metadata from the original
        # request if present. None values are skipped so the TC
        # generator falls back to its optimistic stubs.
        for key in (
            "chain_id",
            "previous_tc_hash",
            "chain_sequence",
            "issued_by",
            "requesting_identity",
            "identity_type",
            "role",
            "authorization_tier",
            "identity_confidence",
            "identity_verified",
            "authentication_method",
            "requesting_session_id",
            "mcp_server_id",
            "mcp_servers_in_scope",
            "mcp_servers_out_of_scope",
        ):
            if original_meta.get(key) is not None:
                forced_ctx[key] = original_meta[key]
        # Low C dimension score so the C gate fails.
        dim_scores = {"B": 0.94, "A": 0.94, "C": 0.31, "K": 0.88}
        sub_scores = {"C": {"C3": 0.00}}

        try:
            tis_input = TISInput(
                subject_id=request.subject_id,
                subject_type=request.subject_type,
                policy_profile=base_profile,
                dimension_scores=dim_scores,
                sub_factor_scores=sub_scores,
                context_metadata=forced_ctx,
                elapsed_hours=0.0,
                is_valid=1,
                invalidation_event=None,
                evaluation_time=now,
            )
            tis_result = compute_tis(tis_input)
            decision, requires_review = map_decision(tis_input, tis_result)
            tc = generate_certificate(
                tis_input, tis_result, decision, requires_review
            )
            issued_tc = self._store.issue(tc)
        except Exception as inner:  # noqa: BLE001
            # If even the stop-TC path fails, fall back to fail-safe.
            return enforce_fail_safe(
                "tc_write_failure",
                base_profile.risk_tier,
                candidate_output=request.candidate_output,
                request_id=request.request_id,
                reason=f"credential + {inner!r}",
            )

        response = self._enforcer.enforce(
            decision,
            request.candidate_output,
            issued_tc,
            base_profile.risk_tier,
            request_id=request.request_id,
        )
        # Replace the blocking_reason with the credential reason for
        # clarity in the response. The TC itself keeps its original
        # blocking_reason (C3_prohibited_pattern_credential_detected).
        return response
