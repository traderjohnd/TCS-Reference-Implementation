# Phase 4 Acceptance Gate

**Status: PASSED**
**Date: 2026-05-17**
**Test count: 667 passed, 1 skipped (8 new acceptance tests on top of 659).**
**Frontend build: clean.**

---

## What Phase 4 proves

> **TCS governs the path, not just the prompt.**
>
> The enterprise governance loop is complete and demonstrable end-to-end:
>
> ```
> Connections        define the path
> Policy Controls    define the governance regime
> Governed Chat      runs the workflow
> TCS                evaluates the path
> Trust Certificate  records the evidence
> Audit view         exposes the record
> ```

## The 10 acceptance criteria

| # | Criterion | Evidence (test / artifact) | Status |
|---|-----------|---------------------------|--------|
| 1 | User selects industry, standards, risk tier, and action class | Standards Composer UI (`PolicyControls.jsx`) + `POST /v2/standards/compose` | ✅ |
| 2 | System resolves a locked TCS policy profile | `compose_profile()` produces deterministic `composed-<hash16>` pack id; `register_composed_pack()` writes pack with full `composer_metadata`; `tests/test_phase4_acceptance.py::TestCriterion1And2_ComposeAndLock` | ✅ |
| 3 | Governed Chat uses that active profile | Route resolves `body.profile_id` from `get_active_pack()` when omitted; `tests/test_phase4_acceptance.py::TestCriterion3_ChatUsesActiveProfile` | ✅ |
| 4 | Workflow trace captures LLM / RAG / API / MCP / agent-chain nodes | `tests/test_phase4_acceptance.py::TestCriterion4_WorkflowTraceFiveConnectors` builds a single workflow with all 5 connector types + a TIS evaluation marker under a composed MedDev pack and asserts every node type appears in the trace, the engine scores the compound workflow, and `resolved_policy_profile_id` traces back to the composed pack | ✅ |
| 5 | BACK scores respond to the selected profile | Composed thresholds (e.g. K≥0.85, C≥0.90, A≥0.90 for ISO 13485 + ISO 14971 + IEC 62304) flow into `/v2/query` response `thresholds`; `tests/test_phase4_acceptance.py::TestCriterion5_BackScoresReflectProfile` | ✅ |
| 6 | HOLD / STOP / ESCALATE reasons are visible | `blocking_reason` populated on the response and TC; `DecisionReason` component in `GovernedChat.jsx` renders color-coded reason for non-Allow decisions; `tests/test_phase4_acceptance.py::TestCriterion6_DecisionReasonsVisible` | ✅ |
| 7 | Trust Certificate records the profile, path, score, gates, and decision | TC carries `policy_set_id`, `s_base` / `tis_current`, `gate_passed` / `gate_results`, `decision`, **plus the new `composer_metadata` block** (industry, sub_industry, use_case, standards, risk_tier, action_class, composition_rules_version, composed_at) for self-documenting audit; `tests/test_phase4_acceptance.py::TestCriterion7_TCSelfDocuments` | ✅ |
| 8 | Audit page can retrieve and display the certificate | `GET /v2/certificates/{id}` and `GET /v2/certificates` resolve composed-pack TCs; `AuditCertificates.jsx` Layer S list and Standards Composer Audit panel render all fields including `composer_metadata`; `tests/test_phase4_acceptance.py::TestCriterion8_AuditEndpointResolves` | ✅ |
| 9 | All tests pass | `pytest tests/ -q` → 667 passed, 1 skipped, 20 warnings | ✅ |
| 10 | Scenarios A-G still work under selected policy profiles | Scenarios in `tests/test_compound_trust.py` declare their own `profile_id` and are invariant to the active pack; `tests/test_phase4_acceptance.py::TestCriterion10_ScenariosWorkUnderActiveProfile` exercises Scenario A while a composed pack is deployed and confirms behavior is unchanged | ✅ |

## What Phase 4 ships

### Connectors (Slice 2)
- `LLMConnector` — CT-1 — wraps OpenAI / Anthropic / Mock providers
- `RAGConnector` — CT-4 — wraps the demo vector store; counts attribution gaps; detects credentials → C3
- `APIConnector` — CT-1 — HTTP tool with allowlist enforcement; unauthorized endpoint → C3 prohibited action pattern → STOP
- `MCPConnector` — CT-1 shape-only — emits `context_expansion`, `tc_reuse_attempted`, `enforcement_perimeter_complete` for real governance semantics
- `AgentChainConnector` — CT-8 — pre-computed per-agent K scores feed K_chain math
- `TISEvaluationMarkerConnector` — sentinel for the eval boundary; nodes after the marker can trigger C-R.14 `context_expansion` invalidation

### Compound trust scenarios (Slice 2 + Slice 4 acceptance)
A. LLM only → Allow
B. LLM + RAG (complete provenance) → Allow
C. LLM + RAG (missing provenance, S_base ≥ κ) → Hold (Priority 4)
D. LLM + RAG + unauthorized API → Stop (Priority 2, C3 action pattern)
E. LLM + Marker + post-eval MCP context expansion → Stop / invalidated (Priority 1)
F. LLM + RAG + Agent Chain (compound K_chain) → Hold (Priority 4)
G. LLM + RAG with credentials → Stop (Priority 2, C3 credentials)

### Governance UI (Slice 3)
- `GovernedChat.jsx` — clean chat surface; collapsed governance for Allow, expanded for non-Allow
- `GovernancePanel` — workflow trace (per-node CT badges + latency), BACK scores (4-column grid with threshold ticks, FAIL labels), provenance with missing-metadata warnings, TC summary, invalidation messaging that says "delivery blocked / re-evaluation required"

### Standards Composer (Slice 4)
- 11-standard starter library across 4 industries (`tcs/standards/library.py`)
- Hybrid / strictest-control composition (`tcs/standards/composer.py`):
  - thresholds: max (strictest)
  - gate set: union
  - required controls: OR-union
  - hard prohibitions: union
  - penalty weights: additive with caps and re-normalization
  - dimension weights: additive deltas, re-normalized
- Per-standard contribution tracking (which standard contributed which adjustment, which were overridden by stricter peers)
- Pack integration: composed profiles register as deployable packs (`is_composed_pack=True`) with full `composer_metadata`
- Custom pack names with smart auto-suggestion
- Read-only adjustment visibility per standard

### Paper alignment (kappa-as-floor + BACK)
- Decision ladder uses paper-aligned direction: gate=0 AND S_base ≥ κ → HOLD; gate=0 AND S_base < κ → STOP
- `s_base` and `s_adjusted` are first-class fields end-to-end (engine, TC schema, persistence, SDK, API response, frontend, spec docs)
- All four governance dimensions are BACK: Boundedness, Attribution, Compliance, Known
- `U` exists only as a derived uncertainty quantity (`U_chain`, `U_t`)

## What Phase 4 does NOT ship (deferred to Phase 5)

- Real MCP wire (currently shape-only — governance semantics are real)
- Real multi-LLM agent chain execution (currently pre-computed K_i)
- Editable per-deployment adjustment UI (currently read-only)
- Emergent workflow tracing (currently declarative)
- Standards library expansion beyond the 11 starter standards
- Standards library version locking + rollback
- Real-time pack deployment notification across multiple TCS replicas

## Phase 4 implementation map

```
tcs/
├── workflow/                 ← Slice 1: graph + connector contract
│   ├── trace.py
│   ├── events.py
│   ├── connector.py
│   ├── orchestrator.py
│   └── connectors/
│       ├── llm.py            ← Slice 1
│       ├── rag.py            ← Slice 1
│       ├── api.py            ← Slice 2
│       ├── mcp.py            ← Slice 2
│       ├── agent_chain.py    ← Slice 2
│       └── marker.py         ← Slice 2
├── standards/                ← Slice 4
│   ├── library.py            (11 standards + taxonomy)
│   └── composer.py           (hybrid/strictest-control)
├── api/
│   ├── routes_query.py       (Slice 1 trace path + Slice 4 active-pack default)
│   └── routes_standards.py   ← Slice 4
├── packs/
│   └── pack_manager.py       (Slice 4: register_composed_pack)
├── trust_certificate.py      (Slice 4: composer_metadata field)
└── policy_profiles.py        (Slice 4: load_profile pack-registry fallback)

frontend/src/views/
├── GovernedChat.jsx          ← Slice 3
├── PolicyControls.jsx        ← Slice 4
└── AuditCertificates.jsx     (Slice 3 + 4: s_base/composer fields)

tests/
├── test_workflow_trace.py        ← Slice 1
├── test_connector_contract.py    ← Slice 1+2
├── test_compound_trust.py        ← Slice 1-4 (scenarios A-G + S_base regression)
├── test_standards.py             ← Slice 4
└── test_phase4_acceptance.py     ← Phase 4 acceptance gate (this doc)
```

## Verification commands

```
cd "PROJECTS/Governance Code Build/tcs-reference-implementation"

# Full test suite
python -m pytest tests/ -q
# expected: 667 passed, 1 skipped

# Phase 4 acceptance suite only
python -m pytest tests/test_phase4_acceptance.py -v
# expected: 8 passed

# Frontend build
cd frontend && npm run build
# expected: clean (chunk-size warning only)

# Demo (next: see PHASE_4_DEMO.md)
$env:TCS_WORKFLOW_TRACE_ENABLED="true"
python -B -m uvicorn tcs.api.app:app --host 0.0.0.0 --port 8000 --reload
# then open http://localhost:8000
```

---

**Phase 4 is functionally complete.** Next: demo-hardening pass + GitHub README + Phase 5 planning.
