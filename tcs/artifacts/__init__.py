"""
tcs.artifacts — Runtime Sidecar Transparency (Phase 5)
=======================================================

This module separates **generation** from **governance evaluation** so the
sidecar architecture from the TCS whitepaper is visible in code:

    Generation tier  → produces ResponseArtifact (immutable capture of
                       what was generated, by whom, from what context)
    Evaluation tier  → produces GovernanceEvaluation (one pass of TCS
                       against an artifact; many evaluations per artifact)
    Enforcement tier → derived from (mode, decision) per derive_enforcement_action
    Audit tier       → Trust Certificate hash chain (existing)

Three modes for evaluation:

    observe   — TCS evaluates, records a TC marked lifecycle_state="observed",
                does NOT change delivery. enforcement_action = "logged_only".
    enforce   — TCS evaluates, records a TC, intervenes per the decision.
                enforcement_action ∈ {delivered, held, blocked, escalated}.
    what_if   — Counterfactual evaluation. Creates a GovernanceEvaluation
                row but NO Trust Certificate. enforcement_action =
                "counterfactual_only". Used by the replay UI to compare
                "what would have happened under policy X" without
                affecting delivery.

The TIS engine remains deterministic and unaware of these modes — they
only matter at the layer that translates a TIS result into a runtime
action and an audit record.

Four generation modes:

    raw_llm           — LLM called with the user prompt, no RAG, no
                        system-prompt grounding to a corpus.
    rag_llm           — LLM called with retrieved corpus chunks injected.
                        (Today's /v2/query mode.)
    agent_workflow    — multi-node trace; the LLM may be one of several
                        connectors.
    human_composed    — no LLM. A human typed the output (e.g. an
                        outbound message to a client). TCS governs the
                        draft before send. This is one of the strongest
                        runtime examples of the sidecar pattern: TCS is
                        not a model wrapper, it is an enforcement layer
                        at the point of action.

Phase 5 scope reminder: this layer evaluates human-authored draft text
using the current rule and BACK/TIS logic. Numeric device-envelope
checks (neonatal defibrillator settings, weight-based dosing limits,
etc.) belong to the typed-facts evaluator and are deferred to the
Deterministic Bounded Control Evaluator slice. Do not let UI or docs
imply the typed-facts envelope evaluator exists here.
"""

from __future__ import annotations

from tcs.artifacts.helpers import (
    derive_enforcement_action,
    hash_text,
    normalize_prompt,
)
from tcs.artifacts.models import (
    ENFORCEMENT_BLOCKED,
    ENFORCEMENT_COUNTERFACTUAL_ONLY,
    ENFORCEMENT_DELIVERED,
    ENFORCEMENT_ESCALATED,
    ENFORCEMENT_HELD,
    ENFORCEMENT_LOGGED_ONLY,
    GENERATION_MODE_AGENT_WORKFLOW,
    GENERATION_MODE_HUMAN_COMPOSED,
    GENERATION_MODE_RAG_LLM,
    GENERATION_MODE_RAW_LLM,
    EVALUATION_MODE_ENFORCE,
    EVALUATION_MODE_OBSERVE,
    EVALUATION_MODE_WHAT_IF,
    GovernanceEvaluation,
    ResponseArtifact,
)

__all__ = [
    "ResponseArtifact",
    "GovernanceEvaluation",
    # Generation modes
    "GENERATION_MODE_RAW_LLM",
    "GENERATION_MODE_RAG_LLM",
    "GENERATION_MODE_AGENT_WORKFLOW",
    "GENERATION_MODE_HUMAN_COMPOSED",
    # Evaluation modes
    "EVALUATION_MODE_OBSERVE",
    "EVALUATION_MODE_ENFORCE",
    "EVALUATION_MODE_WHAT_IF",
    # Enforcement actions
    "ENFORCEMENT_DELIVERED",
    "ENFORCEMENT_HELD",
    "ENFORCEMENT_BLOCKED",
    "ENFORCEMENT_ESCALATED",
    "ENFORCEMENT_LOGGED_ONLY",
    "ENFORCEMENT_COUNTERFACTUAL_ONLY",
    # Helpers
    "derive_enforcement_action",
    "hash_text",
    "normalize_prompt",
]
