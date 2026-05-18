"""
Phase 4 — Compound Trust Validation Harness.

This is the math-and-behavior validation harness the white paper
calls for: prove that trust changes correctly as a workflow becomes
more complex. Each scenario builds a different workflow shape and
asserts the BACK aggregation, policy resolution, and decision
outcome tied to the FORMAL DECISION LADDER (not just "gate failed").

Decision ladder (TCS_SPEC.md §12, restated for clarity):

    Priority 1: I_inv == 0                       -> Stop  (invalidated)
    Priority 2: gate == 0 AND C3 == 0.00         -> Stop  (hard prohibition)
    Priority 3: gate == 0 AND S_base > kappa     -> Stop  (above ceiling)
    Priority 4: gate == 0 AND S_base <= kappa    -> Hold  (within ceiling)
    Priority 5: gate == 1 AND TIS_current < theta_escalate -> Escalate
    Priority 6: gate == 1 AND TIS_current < theta_hold     -> Hold
    Priority 7: gate == 1 AND TIS_current < theta_allow
                AND risk_tier == 'r1'                       -> Observe
    Priority 8: gate == 1 AND TIS_current >= theta_allow    -> Allow

ESCALATE only applies when G=1. If a gate dimension fails, the path
is always either Stop (priorities 2-3) or Hold (priority 4) — never
Escalate. This is the correction the user emphasized for Scenario F.

Naming note: ``S_base`` is the gate-independent weighted composite
``Σᵢ wᵢ · dimᵢ``. The TIS engine returns it as ``TISResult.tis_raw``
(see tis_engine._compute_tis_raw and decision_engine module docstring
for the equivalence). The white paper sometimes uses ``TIS_raw`` for
the *gated* form ``G · S_base``; that form collapses to 0 on gate
failure and cannot discriminate HOLD from STOP. The discriminator
must be the gate-independent quantity, which is what this code uses.

kappa is a CEILING: high S_base + gate failure -> STOP (non-remediable);
low S_base + gate failure -> HOLD (remediable).

Scenarios:

    A) LLM only, safe answer                       -> ALLOW    (priority 8)
    B) LLM + RAG with complete source metadata     -> ALLOW    (priority 8)
    C) LLM + RAG with missing chunk provenance     -> HOLD     (priority 4, A gate)
    D) LLM + RAG + unauthorized API endpoint       -> STOP     (priority 2, C3 action)
    E) LLM + MARKER + post-eval MCP expansion      -> STOP     (priority 1, invalidation)
    F) Multi-agent chain with compounding K        -> HOLD     (priority 4, K gate + TIS_raw <= kappa)
    G) Credentials in retrieved chunks             -> STOP     (priority 2, C3 credentials)

Scenario E is labeled Stop operationally; the governing mechanism is
context_expansion invalidation (I_inv=0, TIS_current=0, lifecycle
"invalidated"). Operationally: delivery blocked, re-evaluation
required against the expanded context.
"""

from __future__ import annotations

from typing import Any, Dict, List

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tcs.decision_engine import map_decision
from tcs.governed_context import assemble_context_from_trace
from tcs.tis_engine import compute_tis
from tcs.workflow import (
    ConnectorRequest,
    GovernanceEvent,
    GovernedNode,
    NodeType,
    WorkflowOrchestrator,
)
from tcs.workflow.connectors import (
    AgentChainConnector,
    APIConnector,
    LLMConnector,
    MCPConnector,
    RAGConnector,
    TISEvaluationMarkerConnector,
    make_marker_node,
)
from tcs.workflow.orchestrator import WorkflowStep


# --------------------------------------------------------------------------- #
# Shared fixtures                                                              #
# --------------------------------------------------------------------------- #

class _StubVectorStore:
    def __init__(self, chunks: List[Dict[str, Any]]) -> None:
        self._chunks = chunks

    def retrieve(self, query: str, k: int = 5) -> List[Dict[str, Any]]:
        return list(self._chunks[:k])


class _StubProvider:
    def __init__(self, response: str = "Per the policy documentation, X.") -> None:
        self.response = response

    def generate(self, query: str, context: List[str]) -> str:
        return self.response


def _good_chunk(chunk_id: str, sim: float = 0.95) -> Dict[str, Any]:
    return {
        "chunk_id": chunk_id,
        "source_doc": "policy.md",
        "version": "2026-01",
        "content": f"Content for {chunk_id}.",
        "similarity_score": sim,
        "tags": [],
    }


def _orphan_chunk(chunk_id: str, sim: float = 0.95) -> Dict[str, Any]:
    """Chunk missing source_doc and version — attribution gap."""
    return {
        "chunk_id": chunk_id,
        "source_doc": None,
        "version": None,
        "content": f"Orphan content for {chunk_id}.",
        "similarity_score": sim,
        "tags": [],
    }


def _credential_chunk(chunk_id: str) -> Dict[str, Any]:
    """Chunk that smuggles a credential into the governed context."""
    return {
        "chunk_id": chunk_id,
        "source_doc": "leaked_config.md",
        "version": "2026-01",
        "content": "Use this: API_KEY=sk-proj-abc123def456ghi789 for the call.",
        "similarity_score": 0.95,
        "tags": [],
    }


def _llm_node(node_id: str = "llm") -> GovernedNode:
    return GovernedNode(
        node_id=node_id, name="LLM",
        node_type=NodeType.LLM, connection_type="CT-1", sensitivity_tier="T2",
    )


def _rag_node(node_id: str = "rag") -> GovernedNode:
    return GovernedNode(
        node_id=node_id, name="RAG",
        node_type=NodeType.RAG, connection_type="CT-4", sensitivity_tier="T2",
    )


def _api_node(node_id: str = "api") -> GovernedNode:
    return GovernedNode(
        node_id=node_id, name="API",
        node_type=NodeType.API, connection_type="CT-1", sensitivity_tier="T2",
    )


def _mcp_node(node_id: str = "mcp") -> GovernedNode:
    return GovernedNode(
        node_id=node_id, name="MCP",
        node_type=NodeType.MCP, connection_type="CT-1", sensitivity_tier="T2",
    )


def _agent_node(node_id: str = "chain") -> GovernedNode:
    return GovernedNode(
        node_id=node_id, name="Agent Chain",
        node_type=NodeType.AGENT, connection_type="CT-8", sensitivity_tier="T2",
    )


def _run(steps, query: str = "test", profile_id: str = "fin-high-risk-suitability-v3"):
    """Run an orchestrated workflow and return (trace, tis_input, tis_result, decision, requires_review)."""
    orch = WorkflowOrchestrator()
    trace = orch.execute(steps=steps, query=query, base_profile_id=profile_id)
    tis_input, _resolved = assemble_context_from_trace(trace)
    tis_result = compute_tis(tis_input)
    decision, requires_review = map_decision(tis_input, tis_result)
    return trace, tis_input, tis_result, decision, requires_review


# --------------------------------------------------------------------------- #
# In-process FastAPI sub-app for the API connector validation harness         #
# --------------------------------------------------------------------------- #

@pytest.fixture(scope="module")
def api_test_client():
    app = FastAPI()

    @app.get("/finance/quote")
    def quote():
        return {"symbol": "ACME", "price": 123.45}

    @app.post("/finance/order")
    def order():
        return {"order_id": "ord-1", "status": "filled"}

    @app.get("/admin/users")
    def admin_users():
        return {"users": ["a", "b"]}

    with TestClient(app) as client:
        yield client


# --------------------------------------------------------------------------- #
# Scenario A — LLM only, safe answer -> ALLOW (priority 8)                     #
# --------------------------------------------------------------------------- #

class TestScenarioA_LLMOnly:
    """
    Single LLM node, clean response. No RAG, no API, no chain.
    Workflow connection_type is CT-1 (API). All BACK dimensions land
    at 1.0 (no evidence reducing any dimension). G=1, TIS_current >=
    theta_allow -> ALLOW.
    """

    def test_clean_llm_only_allows(self):
        llm = LLMConnector(
            provider=_StubProvider("The policy answer is X."),
            provider_name="stub", model="m",
        )
        steps = [WorkflowStep(node=_llm_node(), connector=llm)]
        trace, tis_input, tis_result, decision, _ = _run(steps)

        assert tis_input.context_metadata["connection_type"] == "CT-1"
        assert tis_input.dimension_scores["B"] == 1.0
        assert tis_input.dimension_scores["C"] == 1.0
        assert decision == "Allow"
        assert tis_result.gate_result == 1


# --------------------------------------------------------------------------- #
# Scenario B — LLM + RAG complete metadata -> ALLOW (priority 8)               #
# --------------------------------------------------------------------------- #

class TestScenarioB_LLMPlusRAGComplete:
    """
    LLM + RAG where every retrieved chunk has source_doc + version.
    n_gaps = 0, A signal = 1.0. K from similarity = 0.95.
    Dominant CT is CT-4 (RAG dominates LLM). G=1 -> ALLOW.
    """

    def test_allows(self):
        chunks = [_good_chunk("c1"), _good_chunk("c2"), _good_chunk("c3")]
        rag = RAGConnector(store=_StubVectorStore(chunks))
        llm = LLMConnector(
            provider=_StubProvider(), provider_name="stub", model="m",
        )
        steps = [
            WorkflowStep(node=_rag_node(), connector=rag, context_key="rag"),
            WorkflowStep(node=_llm_node(), connector=llm),
        ]
        trace, tis_input, tis_result, decision, _ = _run(steps)

        assert tis_input.context_metadata["connection_type"] == "CT-4"
        assert tis_input.context_metadata["n_gaps"] == 0
        assert tis_input.dimension_scores["A"] == pytest.approx(1.0, abs=1e-9)
        assert decision == "Allow"
        assert tis_result.gate_result == 1


# --------------------------------------------------------------------------- #
# Scenario C — LLM + RAG missing provenance -> HOLD (priority 4)               #
# --------------------------------------------------------------------------- #

class TestScenarioC_LLMPlusRAGMissingProvenance:
    """
    LLM + RAG where 1 of 4 chunks lacks source_doc/version.
    A signal = 3/4 = 0.75; fails A gate (threshold 0.90).
    Other dimensions remain clean so S_base stays at or above
    kappa = 0.90 -> HOLD via Priority 4 (remediable via review).

    This is the paper-aligned formulation: HOLD requires a strong
    baseline composite (S_base >= kappa) combined with a specific
    gate failure. If too many chunks lacked metadata the workflow
    would degrade past the remediability floor and STOP instead.
    """

    def test_holds(self):
        chunks = [
            _good_chunk("c1"), _good_chunk("c2"),
            _good_chunk("c3"), _orphan_chunk("c4"),
        ]
        rag = RAGConnector(store=_StubVectorStore(chunks))
        llm = LLMConnector(
            provider=_StubProvider(), provider_name="stub", model="m",
        )
        steps = [
            WorkflowStep(node=_rag_node(), connector=rag, context_key="rag"),
            WorkflowStep(node=_llm_node(), connector=llm),
        ]
        trace, tis_input, tis_result, decision, _ = _run(steps)

        assert tis_input.context_metadata["n_gaps"] == 1
        assert tis_input.dimension_scores["A"] == pytest.approx(0.75, abs=1e-3)
        assert tis_result.gate_result == 0
        # S_base must be at or above kappa for HOLD (paper alignment).
        assert tis_result.s_base >= tis_input.policy_profile.soft_hold_ceiling
        # Priority 4: gate=0, S_base >= kappa -> Hold (remediable).
        assert decision == "Hold"
        # And the C3 path was NOT triggered (so it's not a hard Stop).
        assert tis_result.C3_score != 0.0


# --------------------------------------------------------------------------- #
# Scenario D — LLM + RAG + unauthorized API -> STOP (priority 2, C3 action)    #
# --------------------------------------------------------------------------- #

class TestScenarioD_UnauthorizedAPI:
    """
    LLM + RAG + API call to /admin/users which is NOT in the allowlist.
    Per the APIConnector design, attempting to call an endpoint outside
    the allowlist is a C3 prohibited ACTION pattern. C3=0.00 triggers
    Priority 2 -> hard Stop regardless of other dimension scores.

    This is the formal-ladder answer to "unauthorized action": treat
    it as a content/action prohibition (C3), not as a recoverable
    score drop. The hard stop is the correct governance outcome.
    """

    def test_stops_via_c3(self, api_test_client):
        chunks = [_good_chunk("c1")]
        rag = RAGConnector(store=_StubVectorStore(chunks))
        llm = LLMConnector(
            provider=_StubProvider(), provider_name="stub", model="m",
        )
        api = APIConnector(
            http_client=api_test_client,
            allowlist=["/finance/quote", "/finance/order"],
        )

        steps = [
            WorkflowStep(node=_rag_node(), connector=rag, context_key="rag"),
            WorkflowStep(node=_llm_node(), connector=llm),
            WorkflowStep(
                node=_api_node(),
                connector=api,
                params={"url": "/admin/users", "method": "GET"},
            ),
        ]
        trace, tis_input, tis_result, decision, _ = _run(steps)

        # API event populated and authorization denied.
        api_event = trace.get_node("api").event
        assert api_event is not None
        assert api_event.compliance.c3_violation is True
        assert "unauthorized_endpoint" in (api_event.compliance.c3_pattern or "")

        # C3 propagated to the GCA — C dimension forced to 0.0.
        assert tis_input.dimension_scores["C"] == 0.0
        assert tis_result.C3_score == 0.0
        # Priority 2: gate=0 AND C3=0.00 -> Stop (kappa does not apply).
        assert tis_result.gate_result == 0
        assert decision == "Stop"

    def test_authorized_endpoint_passes(self, api_test_client):
        """Same workflow but a permitted endpoint -> no C3 violation, Allow."""
        chunks = [_good_chunk("c1")]
        rag = RAGConnector(store=_StubVectorStore(chunks))
        llm = LLMConnector(
            provider=_StubProvider(), provider_name="stub", model="m",
        )
        api = APIConnector(
            http_client=api_test_client,
            allowlist=["/finance/quote", "/finance/order"],
        )
        steps = [
            WorkflowStep(node=_rag_node(), connector=rag, context_key="rag"),
            WorkflowStep(node=_llm_node(), connector=llm),
            WorkflowStep(
                node=_api_node(),
                connector=api,
                params={"url": "/finance/quote", "method": "GET"},
            ),
        ]
        trace, tis_input, tis_result, decision, _ = _run(steps)

        api_event = trace.get_node("api").event
        assert api_event.error is None
        assert api_event.compliance.c3_violation is False
        assert decision == "Allow"


# --------------------------------------------------------------------------- #
# Scenario E — LLM + Marker + post-eval MCP -> STOP (priority 1, invalidation) #
# --------------------------------------------------------------------------- #

class TestScenarioE_PostEvalMCPExpansion:
    """
    Workflow:
        LLM -> [TIS Evaluation Marker] -> MCP retrieval (context_expansion=True)

    The post-marker MCP node simulates a tool retrieval that pulled
    additional governed context AFTER the TIS evaluation conceptually
    occurred. Per C-R.14, this invalidates the workflow's TC:
    I_inv=0, invalidation_event="context_expansion", TIS_current=0.

    Operationally this is "delivery blocked, re-evaluation required."
    The decision engine returns Stop via Priority 1 (invalidation
    overrides everything else).
    """

    def test_post_eval_expansion_invalidates(self):
        llm = LLMConnector(
            provider=_StubProvider(), provider_name="stub", model="m",
        )
        mcp_post = MCPConnector(mcp_server_id="mcp-policy-tool")

        steps = [
            WorkflowStep(node=_llm_node("pre-llm"), connector=llm),
            WorkflowStep(
                node=make_marker_node(),
                connector=TISEvaluationMarkerConnector(),
            ),
            WorkflowStep(
                node=_mcp_node("post-mcp"),
                connector=mcp_post,
                params={
                    "tool_name": "extra_retrieve",
                    "context_expansion": True,
                },
            ),
        ]
        trace, tis_input, tis_result, decision, _ = _run(steps)

        assert tis_input.invalidation_event == "context_expansion"
        assert tis_input.is_valid == 0
        assert tis_input.context_metadata.get("context_expanded_after_evaluation") is True
        # Priority 1: invalidation -> Stop, TIS_current=0.
        assert decision == "Stop"
        assert tis_result.tis_current == 0.0

    def test_pre_eval_mcp_does_not_invalidate(self):
        """Same MCP retrieval BEFORE the marker is fine — it's part of pre-eval context."""
        llm = LLMConnector(
            provider=_StubProvider(), provider_name="stub", model="m",
        )
        mcp_pre = MCPConnector(mcp_server_id="mcp-policy-tool")

        steps = [
            WorkflowStep(
                node=_mcp_node("pre-mcp"),
                connector=mcp_pre,
                params={
                    "tool_name": "initial_retrieve",
                    "context_expansion": True,  # set, but pre-marker
                },
            ),
            WorkflowStep(node=_llm_node(), connector=llm),
            WorkflowStep(
                node=make_marker_node(),
                connector=TISEvaluationMarkerConnector(),
            ),
        ]
        trace, tis_input, tis_result, decision, _ = _run(steps)

        # No post-marker node carried expansion -> no invalidation.
        assert tis_input.invalidation_event is None
        assert tis_input.is_valid == 1
        assert decision == "Allow"


# --------------------------------------------------------------------------- #
# Scenario F — Multi-agent chain with compounding K -> HOLD (priority 4)       #
# --------------------------------------------------------------------------- #

class TestScenarioF_AgentChainCompoundingK:
    """
    3-agent chain with K_i = [0.95, 0.90, 0.85] -> K_chain = 0.7268.
    The chain workflow uses clean RAG (no attribution gaps) so the
    other dimensions stay strong. CT-8 weight modifier is all-zero,
    so the base profile weights apply: a sub-threshold K_chain
    against full B/A/C drops K to 0.7268 < r3 K-gate 0.80 -> G=0.
    S_base = 0.30 + 0.25 + 0.30 + 0.15*0.7268 = 0.959 >= kappa=0.90
    -> HOLD via Priority 4 (remediable through human review).

    The corrected expectation per the formal decision ladder:
    ESCALATE only applies when G=1. K gate failure means G=0, so
    the path is either Stop (priorities 2-3) or Hold (priority 4).
    HOLD here is paper-aligned: high baseline composite + specific
    gate failure = a human-reviewable degradation.

    This is the "trust compounds across the path" demonstration:
    a 3-agent chain with reasonable per-hop scores still produces
    K_chain that fails the r3 K gate.
    """

    def test_chain_holds(self):
        # Clean RAG keeps B/A/C strong; chain pulls K down via K_chain.
        # gate=0 (K gate fails) but S_base >= kappa -> HOLD (paper-aligned).
        chunks = [_good_chunk("c1"), _good_chunk("c2"), _good_chunk("c3")]
        rag = RAGConnector(store=_StubVectorStore(chunks))
        chain = AgentChainConnector(
            per_agent_K_scores=[0.95, 0.90, 0.85],
            agent_roles=["research", "analysis", "synthesis"],
        )
        llm = LLMConnector(
            provider=_StubProvider(), provider_name="stub", model="m",
        )
        steps = [
            WorkflowStep(node=_rag_node(), connector=rag, context_key="rag"),
            WorkflowStep(node=_agent_node(), connector=chain),
            WorkflowStep(node=_llm_node(), connector=llm),
        ]
        trace, tis_input, tis_result, decision, _ = _run(steps)

        # CT-8 wins as dominant CT (chain dominates RAG).
        assert tis_input.context_metadata["connection_type"] == "CT-8"
        # K_chain = 0.95 * 0.90 * 0.85 = 0.72675 -> 0.7268
        assert tis_input.dimension_scores["K"] == pytest.approx(0.7268, abs=1e-3)
        assert tis_input.context_metadata.get("chain_depth") == 3
        # K gate fails at the r3 threshold of 0.80.
        assert tis_result.gate_result == 0
        # S_base stays above kappa because other dims are clean.
        assert tis_result.s_base >= tis_input.policy_profile.soft_hold_ceiling
        # Priority 4: gate=0, S_base >= kappa -> HOLD (NOT Escalate).
        assert decision == "Hold"

    def test_chain_with_strong_per_hop_still_allows(self):
        """K_i = [0.98, 0.98, 0.98] -> K_chain = 0.941 > 0.80, gate=1, ALLOW."""
        chunks = [_good_chunk("c1"), _good_chunk("c2")]
        rag = RAGConnector(store=_StubVectorStore(chunks))
        chain = AgentChainConnector(per_agent_K_scores=[0.98, 0.98, 0.98])
        llm = LLMConnector(
            provider=_StubProvider(), provider_name="stub", model="m",
        )
        steps = [
            WorkflowStep(node=_rag_node(), connector=rag, context_key="rag"),
            WorkflowStep(node=_agent_node(), connector=chain),
            WorkflowStep(node=_llm_node(), connector=llm),
        ]
        trace, tis_input, tis_result, decision, _ = _run(steps)

        assert tis_input.dimension_scores["K"] == pytest.approx(0.9412, abs=1e-3)
        assert tis_result.gate_result == 1
        assert decision == "Allow"


# --------------------------------------------------------------------------- #
# Scenario G — Credentials in retrieved chunks -> STOP (priority 2, C3)        #
# --------------------------------------------------------------------------- #

class TestScenarioG_CredentialsExposed:
    """
    RAG retrieval surfaces a chunk containing a credential. The RAG
    connector's credential scan fires -> ComplianceSignal with
    c3_violation=True and c3_pattern="credential_detected:<pat>".

    GCA min-aggregation forces C dimension to 0.0 and the engine's
    failing_dimension_subfactors path records C3=0.00. Decision
    engine Priority 2 (gate=0 AND C3=0.00) -> hard Stop, non-overrideable
    by κ. lifecycle state = "blocked".
    """

    def test_credential_in_chunk_stops(self):
        chunks = [_good_chunk("c1"), _credential_chunk("c2")]
        rag = RAGConnector(store=_StubVectorStore(chunks))
        llm = LLMConnector(
            provider=_StubProvider(), provider_name="stub", model="m",
        )
        steps = [
            WorkflowStep(node=_rag_node(), connector=rag, context_key="rag"),
            WorkflowStep(node=_llm_node(), connector=llm),
        ]
        trace, tis_input, tis_result, decision, _ = _run(steps)

        rag_event = trace.get_node("rag").event
        assert rag_event.compliance.c3_violation is True
        assert (rag_event.compliance.c3_pattern or "").startswith(
            "credential_detected:"
        )
        # C3 propagates to TIS engine.
        assert tis_result.C3_score == 0.0
        # Priority 2: gate=0 AND C3=0.00 -> hard Stop.
        assert tis_result.gate_result == 0
        assert decision == "Stop"


# --------------------------------------------------------------------------- #
# Cross-cutting: connectors don't leak into the engine                         #
# --------------------------------------------------------------------------- #

class TestDecisionLadderSBaseDiscriminator:
    """
    Regression for the S_base discriminator in Priority 3/4 under the
    paper-aligned kappa-as-floor semantics:

        gate=0, C3 > 0, S_base >= kappa -> HOLD (remediable via review)
        gate=0, C3 > 0, S_base <  kappa -> STOP (too degraded)

    The discriminator is S_base (the gate-INDEPENDENT composite). The
    field ``tis_raw`` per the white paper equals gate * S_base and
    collapses to 0 on gate failure; using it for the kappa comparison
    would make the test pointless.

    Constructed directly against the engine to dial S_base precisely,
    rather than working backwards through connectors. The path under
    test is the math, not the trace assembly.
    """

    def _make_input(self, *, scores, profile_id="fin-high-risk-suitability-v3"):
        from datetime import datetime, timezone
        from tcs.policy_profiles import load_profile
        from tcs.tis_engine import TISInput

        return TISInput(
            subject_id="s_base-regression",
            subject_type="model_output",
            policy_profile=load_profile(profile_id),
            dimension_scores=scores,
            sub_factor_scores={"C": {"C3": 1.0}},   # no C3 violation
            context_metadata={
                "n_gaps": 0,
                "context_age_hours": 0.1,
                "novelty_score": 0.0,
                "days_since_review": 1,
                "is_policy_sensitive": False,
            },
            elapsed_hours=0.0,
            is_valid=1,
            invalidation_event=None,
            evaluation_time=datetime.now(timezone.utc),
        )

    def test_gate_fail_with_high_s_base_holds(self):
        """
        Gate fails on A (0.62 < 0.90), but other dimensions are high
        enough that S_base stays at or above kappa=0.90 -> HOLD (Priority 4).

        Weights (fin-r3-a4): B=0.30, A=0.25, C=0.30, K=0.15
        Scores: B=1.0, A=0.62, C=1.0, K=1.0
        S_base = 0.30 + 0.155 + 0.30 + 0.15 = 0.905 >= 0.90 -> HOLD.
        A=0.62 still fails the A gate threshold of 0.90.
        """
        from tcs.decision_engine import map_decision
        from tcs.tis_engine import compute_tis

        inp = self._make_input(scores={"B": 1.0, "A": 0.62, "C": 1.0, "K": 1.0})
        res = compute_tis(inp)
        decision, _ = map_decision(inp, res)

        assert res.gate_result == 0          # A gate failed
        assert res.C3_score == 1.0           # no C3 violation
        # tis_raw is gated (= gate * S_base = 0) per the white paper; we
        # must compare S_base directly against kappa.
        assert res.tis_raw == 0.0
        assert res.s_base >= inp.policy_profile.soft_hold_ceiling
        assert decision == "Hold"            # Priority 4

    def test_gate_fail_with_low_s_base_stops(self):
        """
        Gate fails on A (0.50 < 0.90), other dimensions modest, so
        S_base drops below kappa=0.90 -> STOP (Priority 3).

        Scores: B=0.80, A=0.50, C=0.80, K=0.70
        S_base = 0.30*0.80 + 0.25*0.50 + 0.30*0.80 + 0.15*0.70
               = 0.24 + 0.125 + 0.24 + 0.105 = 0.710 < 0.90 -> STOP.
        """
        from tcs.decision_engine import map_decision
        from tcs.tis_engine import compute_tis

        inp = self._make_input(scores={"B": 0.80, "A": 0.50, "C": 0.80, "K": 0.70})
        res = compute_tis(inp)
        decision, _ = map_decision(inp, res)

        assert res.gate_result == 0          # A gate failed
        assert res.C3_score == 1.0           # no C3 violation
        assert res.tis_raw == 0.0            # tis_raw collapses on gate fail
        assert res.s_base < inp.policy_profile.soft_hold_ceiling
        assert decision == "Stop"            # Priority 3


class TestPaperAlignmentSBaseVsTisRaw:
    """
    Explicit regression for the paper alignment milestone:

    1. When gate=0, tis_raw collapses to 0 (per white paper:
       tis_raw = gate * s_base).
    2. s_base remains available and is the gate-independent composite.
    3. High s_base (>= kappa) with non-C3 gate failure -> HOLD.
    4. Low s_base (< kappa) with non-C3 gate failure -> STOP.

    These four properties together pin the paper-aligned semantics
    and would catch any regression that re-inverts the kappa direction
    or accidentally reverts to using tis_raw as the discriminator.
    """

    def _make_input(self, *, scores, profile_id="fin-high-risk-suitability-v3"):
        from datetime import datetime, timezone
        from tcs.policy_profiles import load_profile
        from tcs.tis_engine import TISInput

        return TISInput(
            subject_id="paper-alignment-regression",
            subject_type="model_output",
            policy_profile=load_profile(profile_id),
            dimension_scores=scores,
            sub_factor_scores={"C": {"C3": 1.0}},   # no C3 violation
            context_metadata={
                "n_gaps": 0, "context_age_hours": 0.1, "novelty_score": 0.0,
                "days_since_review": 1, "is_policy_sensitive": False,
            },
            elapsed_hours=0.0,
            is_valid=1,
            invalidation_event=None,
            evaluation_time=datetime.now(timezone.utc),
        )

    def test_tis_raw_collapses_on_gate_failure(self):
        """Property 1: gate=0 -> tis_raw == 0 (white paper definition)."""
        from tcs.tis_engine import compute_tis

        # A fails 0.90 gate -> gate=0.
        inp = self._make_input(scores={"B": 1.0, "A": 0.50, "C": 1.0, "K": 1.0})
        res = compute_tis(inp)

        assert res.gate_result == 0
        assert res.tis_raw == 0.0
        assert res.tis_adj == 0.0  # tis_adj also collapses (= gate * s_adj)

    def test_s_base_remains_available_when_gate_fails(self):
        """Property 2: s_base survives gate collapse for ladder discrimination."""
        from tcs.tis_engine import compute_tis

        # Gate fails but s_base is still computed.
        inp = self._make_input(scores={"B": 1.0, "A": 0.50, "C": 1.0, "K": 1.0})
        res = compute_tis(inp)

        assert res.gate_result == 0
        # s_base = 0.30 + 0.125 + 0.30 + 0.15 = 0.875 (gate-independent)
        assert res.s_base == 0.875
        assert res.s_adj > 0    # s_adj also survives gate collapse

    def test_high_s_base_non_c3_gate_fail_holds(self):
        """Property 3: gate=0, C3>0, s_base >= kappa -> HOLD."""
        from tcs.decision_engine import map_decision
        from tcs.tis_engine import compute_tis

        # B=1.0, A=0.62 (fails 0.90 gate), C=1.0, K=1.0
        # s_base = 0.30 + 0.155 + 0.30 + 0.15 = 0.905 >= 0.90 -> HOLD
        inp = self._make_input(scores={"B": 1.0, "A": 0.62, "C": 1.0, "K": 1.0})
        res = compute_tis(inp)
        decision, _ = map_decision(inp, res)

        assert res.gate_result == 0
        assert res.C3_score == 1.0          # no C3 violation
        assert res.s_base >= inp.policy_profile.soft_hold_ceiling
        assert decision == "Hold"

    def test_low_s_base_non_c3_gate_fail_stops(self):
        """Property 4: gate=0, C3>0, s_base < kappa -> STOP."""
        from tcs.decision_engine import map_decision
        from tcs.tis_engine import compute_tis

        # B=0.80, A=0.50, C=0.80, K=0.70
        # s_base = 0.30*0.80 + 0.25*0.50 + 0.30*0.80 + 0.15*0.70 = 0.710
        inp = self._make_input(scores={"B": 0.80, "A": 0.50, "C": 0.80, "K": 0.70})
        res = compute_tis(inp)
        decision, _ = map_decision(inp, res)

        assert res.gate_result == 0
        assert res.C3_score == 1.0          # no C3 violation
        assert res.s_base < inp.policy_profile.soft_hold_ceiling
        assert decision == "Stop"


class TestEngineRemainsConnectorAgnostic:
    """
    The TIS engine must never know which connectors ran. It scores a
    TISInput. This test exercises every Slice 2 connector in a single
    workflow and asserts the engine produces a finite TISResult
    without touching any workflow / connector class.
    """

    def test_all_connectors_in_one_workflow(self, api_test_client):
        chunks = [_good_chunk("c1"), _good_chunk("c2")]
        rag = RAGConnector(store=_StubVectorStore(chunks))
        llm = LLMConnector(
            provider=_StubProvider("Synthesized answer."),
            provider_name="stub", model="m",
        )
        api = APIConnector(
            http_client=api_test_client,
            allowlist=["/finance/quote"],
        )
        chain = AgentChainConnector(per_agent_K_scores=[0.98, 0.98])
        mcp = MCPConnector(mcp_server_id="mcp-1")

        steps = [
            WorkflowStep(node=_rag_node(), connector=rag, context_key="rag"),
            WorkflowStep(node=_agent_node(), connector=chain),
            WorkflowStep(node=_llm_node(), connector=llm),
            WorkflowStep(
                node=_api_node(),
                connector=api,
                params={"url": "/finance/quote", "method": "GET"},
            ),
            WorkflowStep(
                node=_mcp_node(),
                connector=mcp,
                params={"tool_name": "lookup"},
            ),
            WorkflowStep(
                node=make_marker_node(),
                connector=TISEvaluationMarkerConnector(),
            ),
        ]
        trace, tis_input, tis_result, decision, _ = _run(steps)

        # Engine produced a complete TISResult — finite numbers, valid decision.
        assert 0.0 <= tis_result.tis_current <= 1.0
        assert decision in ("Allow", "Observe", "Hold", "Escalate", "Stop")
        # CT-8 dominates this mixed workflow.
        assert tis_input.context_metadata["connection_type"] == "CT-8"
