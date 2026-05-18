# TCS Architecture Overview

A single-file orientation to the codebase. Detailed per-module specs live in `TCS_Claude_Code_Context_Package/tcs_context_package/`; this document is the map.

---

## The thesis

**TCS governs the path, not just the prompt.**

Every enterprise AI workflow is a graph of nodes — LLM calls, RAG retrievals, API/tool invocations, MCP server interactions, agent handoffs. Each node emits normalized governance evidence (BACK signals + connector metadata). The Governed Context Architecture (GCA) compiles that evidence into a single `TISInput`. A pure-function TIS engine scores it against a policy profile composed from regulatory standards. The decision engine maps the score to one of five outcomes. The Trust Certificate records everything: the path, the standards, the scores, the gates, the decision, and the audit chain.

The architecture is layered so each layer is independently testable and replaceable:

```
┌────────────────────────────────────────────────────────────────┐
│  PRESENTATION   React frontend: Connections, Policy Controls, │
│                 Governed Chat, Live Decisions, Audit, Drift   │
├────────────────────────────────────────────────────────────────┤
│  HTTP API       FastAPI: /v2/{query,standards,packs,...}      │
├────────────────────────────────────────────────────────────────┤
│  STANDARDS      Library + hybrid/strictest-control composer   │
│                 → composed pack with composer_metadata        │
├────────────────────────────────────────────────────────────────┤
│  WORKFLOW       Declarative orchestrator + 5 connectors       │
│                 → GovernedWorkflowTrace                       │
├────────────────────────────────────────────────────────────────┤
│  GCA            assemble_context_from_trace(trace) →          │
│                 TISInput (BACK scores, sub-factors, context)  │
├────────────────────────────────────────────────────────────────┤
│  ENGINE (pure)  compute_tis → TISResult (s_base, tis_raw,     │
│                 gate, decay, tis_current, invalidation)       │
├────────────────────────────────────────────────────────────────┤
│  DECISION       map_decision → 5-outcome ladder               │
│                 (paper-aligned: κ as remediability floor)     │
├────────────────────────────────────────────────────────────────┤
│  CERTIFICATE    generate_certificate → 11-layer TC with       │
│                 composer_metadata + hash chain                │
├────────────────────────────────────────────────────────────────┤
│  PERSISTENCE    SQLite (append-only triggers) | Postgres      │
└────────────────────────────────────────────────────────────────┘
```

---

## Invariants that hold across the codebase

These are not aspirations — they are tested and enforced:

1. **The TIS engine is a pure function.** No I/O, no module state, deterministic. The same `TISInput` always produces the same `TISResult`. It does not know whether its input came from a unit-test fixture or from five live MCP server calls.
2. **No connector constructs `TISInput` directly.** Connectors emit `GovernanceEvent`s with normalized BACK signals. The GCA is the only layer that compiles events into a `TISInput`. Verified by grep in `tests/test_phase4_acceptance.py` and the workflow-trace tests.
3. **The decision ladder is paper-aligned.**
   - `S_base = Σᵢ wᵢ · dimᵢ` is the gate-INDEPENDENT composite (white paper terminology)
   - `tis_raw = gate × S_base` collapses to 0 on gate failure (white paper definition)
   - Priority 3/4 discriminate on `S_base`, not `tis_raw`
   - `κ` is a **remediability floor**: `S_base ≥ κ → HOLD`, `S_base < κ → STOP`
   - Four explicit regression tests in `tests/test_compound_trust.py::TestPaperAlignmentSBaseVsTisRaw` pin this.
4. **BACK, not BACU.** The four primary governance dimensions are Boundedness, Attribution, Compliance, and Known. `U` exists only as a derived uncertainty quantity (`U_chain`, `U_t`, `uncertainty_mass`), never as a primary dimension. The framing assertion in `tests/test_standards.py::test_control_interpretation_is_explicit_about_framing` and the BACK migration are enforced end-to-end.
5. **Standards adjustments are governance interpretations, not regulatory truth.** Every entry in `tcs/standards/library.py` carries a `control_interpretation` note that explicitly frames the TCS parameter mapping as editorial, not a claim that the underlying regulation requires specific numerical values. The library-integrity test asserts this framing is present in every standard.
6. **Composition is strictest-control, not additive stacking.** Selecting multiple standards does not arbitrarily ratchet thresholds upward. Thresholds take the strictest (max), gates are unioned, required controls and hard prohibitions are unioned, penalty weights are additive with caps + re-normalization, dimension weights are additive deltas + re-normalization. Verified in `tests/test_standards.py::TestStrictestControlComposition`.
7. **Composed packs are first-class deployable packs.** They register through the existing Pack system, deploy through the existing flow, and carry full `composer_metadata` (industry, sub-industry, use case, standards, risk tier, action class, composition rules version, composed_at, profile_hash). The TC's `composer_metadata` block makes the standards trail self-documenting — no cross-table joins needed for audit.
8. **The trace is the source of truth for governance decisions.** UI consumers may read `node.payload` for raw artifacts (RAG chunks, API responses) but governance decisions never depend on payloads — only on the immutable `GovernanceEvent` log.

---

## Where the math lives

The mathematical specification is `TCS_Claude_Code_Context_Package/tcs_context_package/TCS_SPEC.md`. The implementation modules trace 1:1 to the spec sections:

| Spec section | Implementation |
|--------------|----------------|
| §1 Canonical TIS formula | `tcs/tis_engine.py::compute_tis` |
| §3 Derived scores (S_base, S_adj, TIS_raw, TIS_adj, TIS_current) | `tcs/tis_engine.py::TISResult` |
| §4 BACK sub-factor decomposition | distributed across connector evidence + GCA aggregation |
| §5 Gate function | `tcs/tis_engine.py::_evaluate_gate` |
| §8 Soft-hold floor κ | `tcs/decision_engine.py::_apply_priority_ladder` |
| §9 Penalty function | `tcs/tis_engine.py::_compute_penalty_components` + `_aggregate_penalty` |
| §10 Temporal decay | `tcs/tis_engine.py` (`math.exp(-decay_rate * elapsed_hours)`) |
| §11 Active invalidation | `tcs/tis_engine.py::_apply_invalidation` + GCA post-marker detection |
| §12 Decision function | `tcs/decision_engine.py::_apply_priority_ladder` |
| §18 Connection-aware policy resolution | `tcs/governed_context.py::resolve_policy_profile` |
| §19 Trust Enforcement Layer (Identity, Governance Status, Audit Integrity, Override) | `tcs/trust_certificate.py` dataclass blocks |

---

## Where the architecture lives

### `tcs/workflow/` — the Phase 4 backbone

The graph layer that makes "governing the path" structural rather than ad-hoc:

- `trace.py` — `GovernedWorkflowTrace`, `GovernedNode`, `GovernedEdge`, `NodeType`. Append-only event attachment.
- `events.py` — `GovernanceEvent` + the four BACK signal types (`BoundednessSignal`, `AttributionSignal`, `ComplianceSignal`, `KnownStateSignal`) + `SensitivityTier` + SHA-256 event hashing.
- `connector.py` — `GovernedConnector` ABC with `invoke()` and `to_governance_event()`. Connectors emit *evidence*; the GCA does the *scoring*. This keeps connectors policy-free and the math centralized.
- `orchestrator.py` — `WorkflowOrchestrator` runs a declared sequence of `WorkflowStep`s, attaches events, seals the hash chain across the trace.
- `connectors/` — concrete adapters: `llm.py` (CT-1, wraps OpenAI / Anthropic / Mock), `rag.py` (CT-4, attribution gaps + similarity-based K), `api.py` (CT-1, allowlist + C3 prohibited action pattern on unauthorized endpoints), `mcp.py` (CT-1 shape-only, real governance semantics for `context_expansion` / `tc_reuse_attempted`), `agent_chain.py` (CT-8, pre-computed K_chain), `marker.py` (TIS Evaluation Marker for post-eval invalidation).

### `tcs/standards/` — the Phase 4 governance layer

- `library.py` — 11 starter standards across 4 industries + Industry/Sub-industry/Use-case taxonomy. Every standard carries a plain-English `control_interpretation` note framing the TCS parameter mapping as governance interpretation, not regulatory mathematical truth.
- `composer.py` — hybrid / strictest-control composition. Inputs: base risk tier + action class + selected standards. Outputs: a `ComposedProfile` with `profile_config` (PolicyProfile shape), per-standard `contributions` (which standard contributed which adjustment, which were overridden), deterministic `profile_hash`, and `composer_metadata` for full audit reconstruction.

### `tcs/governed_context.py` — the GCA

The compilation point. Three entry points:

- `assemble_context(metadata)` — Phase 1 stub (test fixture-driven).
- `assemble_context_v2(metadata, base_profile_id)` — Phase 2 CT-aware (RAG adapter path).
- `assemble_context_from_trace(trace)` — Phase 4 graph-aware. Walks the trace, aggregates BACK signals across events (min for B/A/K, C3 propagation for C), detects the dominant connection type, resolves the policy profile against it, and produces a `TISInput`. Post-marker MCP `context_expansion` events trigger `I_inv = 0` invalidation per C-R.14.

### `tcs/api/` — the HTTP surface

Every route prefixed `/v2`. Key surface area for Phase 4:

- `routes_query.py` — `POST /v2/query`. Resolves `profile_id` from the active deployed pack when omitted. Runs the legacy `GovernedRAGPipeline` path by default; runs the Phase 4 workflow-trace path when `TCS_WORKFLOW_TRACE_ENABLED=true`. Returns the workflow trace, BACK scores, gate results, thresholds, S_base, composer_metadata, blocking_reason — everything the UI needs to render the governance panel.
- `routes_standards.py` — `GET /v2/standards/{taxonomy,library,{id}}`, `POST /v2/standards/{compose,deploy}`. Compose is a read-only preview; deploy registers a composed pack and activates it.
- `routes_packs.py` — list / get / deploy / export / active. Composed packs flow through this surface as first-class packs.
- `routes_certificates.py` — fetch + list TCs, used by the Audit view.

---

## Data flow at runtime

```
1. User picks Industry → Standards → Risk → Action in the Standards Composer
2. POST /v2/standards/deploy:
     compose_profile(...) → ComposedProfile
     register_composed_pack(composed, name=custom)  → pack with composer_metadata
     deploy_pack(pack_id) → flips _active_pack
3. User submits a query in Governed Chat
4. POST /v2/query (profile_id omitted):
     route reads active pack → uses its profile_id and composer_metadata
     orchestrator runs workflow [rag node, llm node]
       each connector invokes, returns ConnectorResult
       orchestrator extracts GovernanceEvent, seals hash chain on the trace
5. assemble_context_from_trace(trace):
     walks events, aggregates BACK signals
     resolves policy profile against dominant CT
     produces TISInput with composer_metadata in context_metadata
6. compute_tis(tis_input) → TISResult (s_base, tis_raw, gate_result, tis_current, ...)
7. map_decision(tis_input, tis_result) → ("Allow"|"Hold"|"Stop"|..., requires_human_review)
8. generate_certificate(tis_input, tis_result, decision, ...) → TrustCertificate
     carries composer_metadata, regulatory_mapping, hash chain pointer, etc.
9. store.issue(tc) → persists with append-only triggers
10. Route returns QueryResponse with response text, decision, all governance fields,
    full workflow trace dict, certificate_id
11. Frontend renders:
      clean chat bubble for Allow (governance collapsed)
      expanded GovernancePanel for Hold/Stop/Escalate showing:
        DecisionReason (with invalidation-specific framing if applicable)
        WorkflowTracePanel (node sequence + latencies)
        BackScoresPanel (4 bars + thresholds + FAIL labels)
        ProvenancePanel (sources with similarity)
        CertificateSummary (TC id + standards governed + link to Audit)
```

---

## What's intentionally NOT in the architecture (yet)

- **Real MCP wire** — MCP connector is shape-only; governance semantics are production-quality but the actual MCP protocol wire is Phase 5.
- **Real multi-LLM agent chain execution** — `AgentChainConnector` accepts pre-computed per-agent K scores; orchestrating actual multi-LLM agent runs is Phase 5.
- **Emergent workflow tracing** — orchestrator is declarative (workflow defined upfront). Capturing arbitrary connector calls into a session-bound emergent trace is Phase 5; the event model already supports it.
- **Editable per-deployment adjustments UI** — read-only in Slice 4. Power users edit the library config file directly. Advanced editing UI is a later iteration.
- **Standards library expansion beyond 11 starters** — additional regulatory frameworks plug into the existing schema.
- **Distributed pack deployment** — `_active_pack` is in-memory; multi-replica coordination is Phase 5+.
- **Persistent hash-chain anchoring** — TCs persist locally; external ledger anchoring is Phase 5+.

---

## How to read the spec

If you're new to TCS, this is the recommended reading order:

1. **This document** — the architectural map.
2. **[`README.md`](README.md)** — install, run, demo pack.
3. **[`docs/PHASE_4_ACCEPTANCE.md`](docs/PHASE_4_ACCEPTANCE.md)** — what the system proves.
4. **[`docs/PHASE_4_DEMO.md`](docs/PHASE_4_DEMO.md)** — how the demos are constructed.
5. **[`TCS_Claude_Code_Context_Package/tcs_context_package/TCS_SPEC.md`](TCS_Claude_Code_Context_Package/tcs_context_package/TCS_SPEC.md)** — the formal mathematical specification.
6. **[`TCS_Claude_Code_Context_Package/tcs_context_package/TC_SCHEMA.md`](TCS_Claude_Code_Context_Package/tcs_context_package/TC_SCHEMA.md)** — Trust Certificate schema.
7. **[`TCS_Claude_Code_Context_Package/tcs_context_package/POLICY_PROFILES.md`](TCS_Claude_Code_Context_Package/tcs_context_package/POLICY_PROFILES.md)** — policy profile definitions.
8. **[`TCS_Claude_Code_Context_Package/tcs_context_package/TEST_SCENARIOS.md`](TCS_Claude_Code_Context_Package/tcs_context_package/TEST_SCENARIOS.md)** — the 8 canonical scenarios + Phase 2 scenarios.

The companion whitepaper *Computable Trust Architecture: A Formal Framework for Runtime AI Governance* (DeRudder, 2026) is the formal foundation; this repository is the verification-grade implementation.
