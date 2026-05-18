# TCS — Trust Computation System

A reference implementation of **Computable Trust Architecture (CTA)** — runtime AI governance that scores trust as a computable, policy-enforceable, attributable property of AI-mediated action, and records every evaluation as a tamper-evident Trust Certificate.

> **TCS governs the path, not just the prompt.**
>
> Every AI workflow (LLM, RAG, API/tool, MCP, agent chain) is modeled as a governed workflow graph. Every connector emits normalized BACK evidence (**B**oundedness, **A**ttribution, **C**ompliance, **K**nown). The engine scores the compound workflow against a policy profile composed from regulatory and industry standards. Every decision (**Allow / Observe / Hold / Escalate / Stop**) produces a Trust Certificate that self-documents the standards, the path, the scores, the gates, and the outcome.

Companion whitepaper: *Computable Trust Architecture: A Formal Framework for Runtime AI Governance* (DeRudder, 2026).

---

## Quick start

### Install

```bash
pip install -r requirements.txt
cd frontend && npm install && cd ..
```

### Run the server

```bash
# Linux / macOS
TCS_WORKFLOW_TRACE_ENABLED=true \
  python -B -m uvicorn tcs.api.app:app --host 0.0.0.0 --port 8000 --reload

# Windows PowerShell
$env:TCS_WORKFLOW_TRACE_ENABLED="true"
python -B -m uvicorn tcs.api.app:app --host 0.0.0.0 --port 8000 --reload
```

Then open `http://localhost:8000` for the React control plane.

### Run the demo pack

Three workflows that prove the enterprise loop (Allow / Hold / Stop):

```bash
python demos/phase4_demo.py
```

See [`docs/PHASE_4_DEMO.md`](docs/PHASE_4_DEMO.md) for details.

### Run the test suite

```bash
python -m pytest tests/ -q
# expected: 667 passed, 1 skipped
```

---

## The enterprise governance loop

```
Connections        define the path           (which LLMs, RAG sources, APIs, MCP servers)
Policy Controls    define the governance     (Standards Composer → composed pack)
Governed Chat      runs the workflow         (declarative workflow trace)
TCS                evaluates the path        (BACK signals → S_base → decision ladder)
Trust Certificate  records the evidence      (composer_metadata, contributions, hash chain)
Audit view         exposes the record        (queryable, replayable, tamper-evident)
```

Each layer is independently testable. The TIS engine is a pure function; the workflow orchestrator is connector-agnostic; the Standards Composer produces deterministic packs; the Trust Certificate self-documents every governance decision without requiring cross-references.

---

## What this implementation ships

### Phase 1 — Specification-to-code

The four core engines, the policy profile loader, and the 8 deterministic test scenarios that pin the TIS math to the whitepaper specification.

### Phase 2 — Sidecar runtime

The RAG adapter, request interceptor, enforcement controller, persistence layer, sidecar middleware, and the 9 Phase 2 scenarios (CT-4 attribution gate, CT-8 chain uncertainty, response injection, CT-12 credential detection, fail-safe paths, hash chain integrity).

### Phase 3 — Control plane

The five regulatory packs (financial services, healthcare, GMP, enterprise ops, federal/public), the Policy Learning Layer (drift detection, adaptation history, recovery), the calibration validation harness, and the React frontend.

### Phase 4 — Enterprise integration

* **Slice 1** — `GovernedWorkflowTrace`, `GovernedConnector` contract, graph-aware GCA assembly
* **Slice 2** — Five enterprise connectors: LLM, RAG, API (with allowlist + C3 action prohibition), MCP (shape-only with real governance semantics), Agent Chain (CT-8 K_chain), plus the TIS Evaluation Marker for context-expansion invalidation. Seven compound-trust scenarios (A–G).
* **Slice 3** — Governed Chat UI as a workflow surface: clean chat by default, expandable governance evidence on demand, surfaced automatically for non-Allow decisions, with explicit invalidation messaging.
* **Slice 4** — Standards Composer: drill-down through Industry → Sub-industry → Use case → Standards → Risk tier → Action class, composing a deployable pack under hybrid / strictest-control rules. 11-standard starter library across medical devices, pharma, financial services, and general AI governance. Custom pack names, per-standard control_interpretation notes, per-standard contribution tracking.

### Paper alignment

* **BACK model end-to-end** — Boundedness, Attribution, Compliance, Known. `U` exists only as a derived uncertainty quantity (`U_chain`, `U_t`), never as a primary dimension.
* **Decision ladder paper-aligned** — `S_base` is the gate-independent composite; `tis_raw = gate × S_base` collapses to 0 on gate failure; the discriminator for Priority 3/4 is `S_base`, not `tis_raw`. `κ` is a **remediability floor**: `S_base ≥ κ → HOLD`, `S_base < κ → STOP`.

---

## Phase 4 acceptance

The 10-criterion Phase 4 acceptance gate is documented in [`docs/PHASE_4_ACCEPTANCE.md`](docs/PHASE_4_ACCEPTANCE.md). It is verified by `tests/test_phase4_acceptance.py` (8 tests) and proves the enterprise loop is closed end-to-end:

| # | Criterion |
|---|-----------|
| 1 | User selects industry, standards, risk tier, action class |
| 2 | System resolves a locked TCS policy profile |
| 3 | Governed Chat uses that active profile |
| 4 | Workflow trace captures LLM / RAG / API / MCP / agent-chain nodes |
| 5 | BACK scores respond to the selected profile |
| 6 | HOLD / STOP / ESCALATE reasons are visible |
| 7 | Trust Certificate records the profile, path, score, gates, decision |
| 8 | Audit page can retrieve and display the certificate |
| 9 | All tests pass |
| 10 | Scenarios A–G still work under selected policy profiles |

All ten criteria are met. See the acceptance doc for evidence per row.

---

## Repository layout

```
tcs/
  tis_engine.py             pure TIS computation (validated against canonical scenarios)
  decision_engine.py        priority-ordered decision ladder (paper-aligned)
  trust_certificate.py      11-layer TC schema + hash chain + composer_metadata
  policy_profiles.py        canonical defaults + 5 demo profiles + pack-registry fallback
  governed_context.py       GCA: assemble_context_from_trace + CT resolution
  workflow/                 Phase 4 graph: trace + events + connector contract + orchestrator
    connectors/             LLM, RAG, API, MCP, Agent Chain, TIS Evaluation Marker
  standards/                Phase 4: 11-standard library + hybrid/strictest-control composer
  packs/                    5 regulatory packs + composed-pack registration
  sidecar/                  enforcement controller + request interceptor (Phase 2 legacy)
  persistence/              SQLite append-only TC store (+ Postgres backend)
  dynamics/                 drift detection, policy learning, recovery orchestrator
  api/                      FastAPI service (POST /v2/govern, /v2/query, /v2/standards/*, ...)
  identity/                 RBAC roles + sessions
  validation/               calibration harness

tests/                      667 tests across all phases + Phase 4 acceptance
demos/
  phase4_demo.py            three demos: Allow / Hold / Stop
  governed_rag/             Phase 2 financial-services RAG demo
  healthcare/               Phase 2 clinical AI governance demo
docs/
  PHASE_4_ACCEPTANCE.md     Phase 4 acceptance gate
  PHASE_4_DEMO.md           Phase 4 demo pack: how to run + interpret
frontend/                   React 18 control plane: Connections, Policy Controls, Chat, Audit
canonical/                  whitepaper reference artifacts (test vectors, chain example)
```

---

## Architecture overview

See [`ARCHITECTURE.md`](ARCHITECTURE.md) for the layered architecture overview and the design invariants that hold across all phases.

---

## License + Author

John DeRudder | Trust Computation System Reference Implementation | 2026
