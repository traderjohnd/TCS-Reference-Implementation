# Phase 5 Handoff

**Status:** Phase 5 feature work complete; demo-hardening pass underway.
**Last verified:** 2026-05-20 on branch `claude/fervent-hofstadter-2817b5`.
**Latest commit:** `baffeb8` (Demo-hardening: Escalation Queue + override badges).
**Test suite:** 928 passed, 1 skipped.
**Frontend build:** clean (only the chunk-size warning, same as Phase 4).

This document is a working handoff, scaffolded from the repo itself. It is not
the formal Phase 5 acceptance gate — when the demo-hardening pass settles,
promote the verified subset into a `PHASE_5_ACCEPTANCE.md` mirroring the
[Phase 4 acceptance doc](PHASE_4_ACCEPTANCE.md).

**Companion doc:** durable project rules, invariants, and process
expectations live in [CLAUDE_PROJECT_CONTEXT.md](CLAUDE_PROJECT_CONTEXT.md).
Any new session should read that file alongside this one — this file
describes *where the codebase is*; that file describes *what must remain
true as it changes*.

---

## 1. What Phase 5 changes about the architecture

Phase 4 proved the enterprise loop: *Connections → Policy → Governed Chat →
TCS → Trust Certificate → Audit*. Phase 5 splits the runtime sidecar so the
generation tier and the governance tier are visibly separate and
independently replayable:

```
  Generation tier   POST /v2/generate    →  ResponseArtifact (immutable)
  Evaluation tier   POST /v2/evaluate    →  GovernanceEvaluation (+ TC for observe/enforce)
  Replay tier       POST /v2/replay      →  N GovernanceEvaluations against one artifact
  Convenience       POST /v2/query       →  generate + evaluate, persists both
```

The product narrative the codebase now supports:

> *Raw LLM or human draft is captured. Governance evaluates the same
> artifact. Policies change the decision, not the generated content.
> Replay proves the difference. Trust Certificates preserve the evidence.*

---

## 2. Phase 5 slice summary

| Slice | Commit | What it shipped |
|-------|--------|-----------------|
| 5.1 | `5e59c12` | `ResponseArtifact` + `GovernanceEvaluation` dataclasses; `ArtifactStore` (SQLite, append-only); enforcement-action derivation. |
| 5.2 | `7e7c917` | `POST /v2/generate` (4 generation modes), `GET /v2/artifacts/{id}`, `GET /v2/artifacts`. In-memory API keys; `human_composed` never touches the LLM. |
| 5.3 | `cde8bc7` | `POST /v2/evaluate`, `GET /v2/evaluations/{id}`, `GET /v2/artifacts/{id}/evaluations`. Baseline-no-pack fallback for policy resolution. |
| 5.4 | `2116680` | `POST /v2/replay`; `/v2/query` persists artifact + evaluation; `evaluation_origin` ∈ {direct, replay, query}. |
| 5.4a | `37a304f` | Replay fidelity: `evaluation_strategy` ∈ {runtime_snapshot, artifact_metadata, what_if_policy_replay} + `governance_input_snapshot` field; same artifact + same policy reproduces the runtime decision. |
| 5.5 | `5710b9e` | Human-composed workflow polish. |
| 5.5a | `48fcf9c` | Typed-context rules (`tcs/governance/typed_context_rules.py`) — e.g. lithium-to-pregnant-patient outbound message — merged into the existing rule audit pipeline. |
| 5.6 | `77b1e49` | Frontend `GovernanceReplay.jsx` view + role-based display modes (general / admin / auditor). |
| 5.6 follow-up | `8fe57b3` | `lifecycle_state` regression tests. |
| Demo-hardening | `38f24f0`..`baffeb8` | Collapsible left-side nav, Policy Controls section reorder, Hold Queue override persist+filter, Escalation Queue panel, override badges in Recent Decisions. |

---

## 3. Key new concepts

### Generation modes
`raw_llm` · `rag_llm` · `agent_workflow` · `human_composed`. `raw_llm` is
truly raw — no hidden RAG, no hidden system prompt, no hidden policy framing.
The `system_prompt_used` field on the artifact is exactly what the model saw
(`None` when nothing was sent).

### Evaluation modes
`observe` (TC issued, `lifecycle_state="observed"`, no delivery change) ·
`enforce` (TC issued, decision drives action) · `what_if` (NO TC,
counterfactual only).

### Evaluation strategies (Slice 5.4a)
- `runtime_snapshot` — replay a captured `TISInput` verbatim, including the
  effective policy as the engine actually saw it (CT modifiers included).
- `artifact_metadata` — fresh metadata-based scoring derived from artifact
  provenance.
- `what_if_policy_replay` — same captured evidence, different policy.
  Opt-in only; auto-resolution will NOT pick this when policies differ.

The deterministic replay guarantee: `compute_tis(tis_input_from_snapshot(s))`
reproduces `compute_tis(original_tis_input)` when
`s == snapshot_tis_input(original_tis_input)`.

### Evaluation origin (call-path audit tag)
`direct` (`/v2/evaluate`) · `replay` (`/v2/replay`) · `query` (`/v2/query`).

### Enforcement actions
Derived from `(mode, decision)` — never set by callers. `delivered`,
`held`, `blocked`, `escalated`, `logged_only` (observe), `counterfactual_only`
(what_if). Validated in `GovernanceEvaluation.__post_init__`.

### `baseline-no-pack` profile
The documented fallback when no pack is active and no profile_id is
supplied. The audit trail records `policy_profile_id="baseline-no-pack"`
explicitly instead of silently skipping governance math.

---

## 4. Endpoint surface

Phase 5 additions, all under `/v2`:

| Method | Path | Purpose |
|--------|------|---------|
| POST | `/generate` | Create a `ResponseArtifact`. No governance. |
| GET | `/artifacts` | List recent artifacts (UI picker). |
| GET | `/artifacts/{id}` | Retrieve one artifact verbatim. |
| POST | `/evaluate` | Evaluate a stored artifact under (mode, profile, strategy). Never calls the LLM. |
| GET | `/evaluations/{id}` | Retrieve one evaluation. |
| GET | `/artifacts/{id}/evaluations` | All evaluations for an artifact, oldest first. |
| POST | `/replay` | N evaluations against one artifact; never delivers; persists with `evaluation_origin="replay"`. |

`/v2/query` now persists the artifact and a `query`-origin evaluation with a
captured `governance_input_snapshot`, which is what later enables
`/v2/evaluate` to reproduce the runtime decision via `runtime_snapshot`
strategy.

---

## 5. Frontend views (`frontend/src/views/`)

- `Connections.jsx` — Phase 3, unchanged surface.
- `PolicyControls.jsx` — Standards Composer + deploy.
- `GovernedChat.jsx` — chat surface with collapsed/expanded governance.
- `LiveDecisions.jsx` — Recent Decisions; demo-hardening added override
  badges.
- `GovernanceReplay.jsx` — Phase 5 Slice 5.6. Artifact picker, evaluation
  history, replay configurator, three role-based display modes (general /
  admin / auditor) persisted in localStorage.
- `AuditCertificates.jsx` — TC listing/detail, including `composer_metadata`
  and Phase 5 fields.
- `DriftMonitoring.jsx`, `Telemetry.jsx`, `Archives.jsx`, `AdminPanel.jsx`,
  `EconomicView.jsx`, `TrustOverview.jsx`, `Login.jsx` — supporting views.

---

## 6. Test coverage (new in Phase 5)

- `test_response_artifact.py` — dataclass invariants, hashing, immutability.
- `test_artifact_store.py` — SQLite shape, append-only triggers, FK.
- `test_routes_generate.py` — guardrail that `human_composed` never calls
  the LLM (provider clients patched to raise).
- `test_routes_evaluate.py` — guardrail that `/v2/evaluate` never calls the
  LLM; mode/strategy validation; baseline-no-pack fallback.
- `test_routes_replay.py` — batch evaluation; `evaluation_origin="replay"`
  on every persisted row.
- `test_replay_fidelity.py` — runtime snapshot reproduces the runtime
  decision deterministically under same policy.
- `test_governance_evaluation.py` — observe/enforce/what_if TC issuance
  rules, `lifecycle_state="observed"` for observe mode.
- `test_human_composed_workflow.py` + `test_human_context_rule_alignment.py`
  — typed-context rule evaluator (Slice 5.5a).
- `test_query_refactor_parity.py` — `/v2/query` parity after the
  artifact + evaluation rewrite.

---

## 7. Non-negotiable invariants (preserved)

These are the user-pinned invariants for the Phase 5 work. They are
asserted in code and in tests:

- **BACK only, not BACU.** Four primary dimensions: Boundedness,
  Attribution, Compliance, Known. `U` exists only as derived uncertainty
  (`U_chain`, `U_t`, `uncertainty_mass`).
- `S_base` is the gate-independent composite. `tis_raw = G × S_base`.
- `κ` is a remediability floor: `S_base ≥ κ → HOLD`, `S_base < κ → STOP`.
- `raw_llm` mode has no hidden RAG, no hidden system prompt, no hidden
  policy framing. `system_prompt_used` records exactly what was sent.
- `/v2/evaluate` and `/v2/replay` never re-call the LLM. Architectural
  guardrail tests patch every provider client to raise; these endpoints
  must still succeed.
- Same artifact + same policy + captured runtime snapshot reproduces
  the decision deterministically (5.4a).
- TIS engine and decision engine remain deterministic and
  generation-agnostic. Connectors emit evidence; the GCA compiles;
  the engine scores.
- `what_if` creates a `GovernanceEvaluation` but no Trust Certificate.
- `observe` and `enforce` create Trust Certificates; `observe` TCs are
  marked `lifecycle_state="observed"` to keep them distinguishable from
  enforce-mode TCs that altered delivery.
- API keys are in-memory only — never persisted to the artifact, the DB,
  or the logs.
- No canned LLM-style domain answers anywhere in the code path.

---

## 8. How to run locally

```powershell
# From the repo root
pip install -r requirements.txt

# Install + build the frontend (required for /v2/health and SPA serving)
cd frontend
npm install
npm run build
cd ..

# Run the server with workflow tracing enabled
$env:TCS_WORKFLOW_TRACE_ENABLED="true"
python -B -m uvicorn tcs.api.app:app --host 0.0.0.0 --port 8000 --reload
```

Then open `http://localhost:8000` for the React control plane. The Phase 5
Governance Replay view lives under the left-nav "Governance" group.

Demo packs:

```powershell
python demos/phase4_demo.py
```

The Phase 4 demo pack remains the cleanest end-to-end story
(Allow / Hold / Stop). A Phase 5 demo pack that exercises
generate → evaluate → replay against the same artifact under different
policies is a natural next addition.

Full verification suite:

```powershell
python -m pytest tests/ -q
# expected: 928 passed, 1 skipped
```

---

## 9. Recent commits (newest first)

```
baffeb8  Demo-hardening: Escalation Queue + override badges in Recent Decisions
1c4e70a  Fix Hold Queue override: actually persist + filter
8734553  Policy Controls: reorder page sections
e86b665  Nav polish: collapsible sections + brighter section headers
38f24f0  Demo-hardening: left-side collapsible nav with 4 grouped sections
8fe57b3  Phase 5 Slice 5.6 follow-up: lifecycle_state regression tests
77b1e49  Phase 5 Slice 5.6: Frontend Governance Replay + Role-Based Views
48fcf9c  Phase 5 Slice 5.5a: Human Context Risk Rule Alignment
5710b9e  Phase 5 Slice 5.5: Human-Composed Workflow Polish
37a304f  Phase 5 Slice 5.4a: Replay Fidelity Hardening
2116680  Phase 5 Slice 5.4: POST /v2/replay + /v2/query persistence + evaluation_origin
cde8bc7  Phase 5 Slice 5.3: POST /v2/evaluate + GET evaluations + baseline-no-pack
7e7c917  Phase 5 Slice 5.2: POST /v2/generate + GET /v2/artifacts/{id}
5e59c12  Phase 5 Slice 5.1: ResponseArtifact + GovernanceEvaluation foundation
dec6705  Complete Phase 4 policy controls and audit rule evidence
```

---

## 10. Recommended next work

Operational visibility first, not new architecture. In order:

1. **Verify Hold Queue override clearing after restart.** `1c4e70a` fixed
   persist + filter; confirm overrides survive a server restart and the
   queue shows the right state.
2. **Confirm Escalation Queue panel covers the Phase 5 escalate-mode
   decisions** (the `baffeb8` add). Make sure it pulls from the same
   GovernanceEvaluation store the rest of the UI reads.
3. **Override-history visibility in Recent Decisions.** The override badge
   landed in `baffeb8`; consider an expandable inline history when a
   reviewer clicks an overridden row.
4. **Phase 5 demo pack** — three workflows that prove the
   *raw_llm vs rag_llm vs human_composed* difference, then replay each
   under a stricter policy to show decision change. Mirror the structure
   of `demos/phase4_demo.py`.
5. **Promote this handoff to `docs/PHASE_5_ACCEPTANCE.md`** once the
   demo-hardening polish lands and the criterion table is final.

Deferred from Phase 5 (and from Phase 4):

- Deterministic Bounded Control Evaluator (numeric envelope checks on
  typed `recipient_context`).
- Real MCP wire protocol (still shape-only).
- Real multi-LLM agent chain execution (still pre-computed K_i).
- Emergent workflow tracing.
- Persistent hash-chain anchoring beyond the local store.
- Multi-replica pack deployment coordination.
- Production retention / redaction / WORM policy on `ResponseArtifact`
  (Phase 5 stores forever; production must not).
