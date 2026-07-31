# TCS Investor Demonstration Runbook (demo-live branch, Commit 6)

One page per phase. No API keys or secret values appear in this
document — live keys are operator-entered at demonstration time and
exist in browser/backend memory only.

---

## 1. Environment startup

```powershell
# Backend (from the repo root)
$env:TCS_WORKFLOW_TRACE_ENABLED = "true"
python -m uvicorn tcs.api.app:create_app --factory --port 8000

# Frontend (second terminal)
cd frontend
npm ci          # first time / clean checkout
npm run build   # production build, or `npm run dev` for a dev session
npm run preview # serves the production build
```

## 2. Health check

* `GET /v2/health` → `status: ok`, `chain_intact: true`.
* Open **Investor Demo → Start Investor Demo → Preflight**:
  backend reachable, build id, operating mode `DEMO`, certificate
  store available, chain health "all chains verify", scripted
  scenarios ≥ 4, live connections / credentials-in-memory counts.

## 3. Demo Mode confirmation

* The toolbar badge must read **DEMO MODE** (the documented default;
  a backend restart always returns to it).
* Demo Mode blocks every external call at the backend — this can be
  shown live by attempting a Live LLM send (blocked with
  `demo_mode_enforced`).

## 4. Archive / start fresh (optional, recommended)

* **Archives → Create archive** snapshots current demonstration data
  and starts a clean dataset. This is archival, not deletion —
  historical data stays retrievable under its archive id. Nothing is
  ever reset merely by switching modes.

## 5. Deterministic scripted narrative (DEMO MODE)

Run the scenarios from **Investor Demo** in order. Every output is
labeled **SCRIPTED DEMO OUTPUT**; identifiers and timestamps vary by
design, everything else repeats exactly.

| # | Scenario | Expected | Operator action |
|---|---|---|---|
| 1 | Allow — governed answer with provenance | Allow | Expand governance layer; open the TC |
| 2 | Stop — prompt-injection attempt | Stop | Show C3 hard stop; note non-overrideable |
| 3 | Hold — remediable gate failure | Hold | Hold queue → review → exercise Hold override |
| 4 | Escalate — decayed trust | Escalate | Escalation queue → walk the routed TC |
| 5 | Trust Certificate detail | — | Audit → Certificates: walk the layers |
| 6 | Hash-chain verification | — | Preflight chain health + Audit chain walk |
| 7 | Governance replay | — | Replay a stored artifact; no provider re-execution |
| 8 | Reporting & telemetry | — | Decision distribution, gate failures, integrity |
| 9 | Malformed-record resilience | — | record_integrity census; surfaces never blank |

Key message: the certificate attests to the governed execution,
evidence, scoring, decision, and provenance — not to the factual truth
of the model's statement.

## 6. Switch to LIVE MODE (deliberate second act)

* Toolbar → DEMO MODE badge → confirmation dialog (external AI
  providers may be called; responses may be unpredictable; provider
  charges may apply; submitted content may leave the local
  environment) → **Enable LIVE MODE**.
* Switching never auto-submits a prompt or tests a connection.

## 7. Operator key entry

* **Connections → + Add Provider**: create/activate **`TCS Test Key`**
  (OpenAI) and a Claude connection, entering limited, spend-capped
  test keys. Keys live in memory only; a browser refresh removes them
  and requires re-entry.

## 8. Live LLM

* **Chat** with the live connection active: banner shows provider,
  exact model, key-in-memory, local corpus on, external web retrieval
  disabled, charge warning. Send one ordinary query (real,
  nondeterministic, governed after generation) and one
  governance-triggering query (e.g. the prompt-injection phrase) to
  show a live non-Allow.

## 9. Model Comparison (stronger vs smaller)

* **Model Comparison**: select a frontier model (e.g. `claude-opus-5`
  or `gpt-4o`) and a smaller/lower-cost model (e.g. `gpt-4o-mini` or
  `claude-haiku-4-5`), optionally labeling them `frontier model` /
  `smaller model` (labels are presentation only — they never affect
  scoring, and neither model is presented as inherently unsafe).
* One prompt, one frozen retrieval, one policy — independently
  governed outputs, side by side, each with its own certificate.

## 10. Live Web

* **Live Web**: select a live connection, review the pre-submission
  summary (explicit live external access, corpus state, search-use
  limit, domain controls, optional approximate location, charge
  warning), run one query per enabled provider.
* Show the truthful retrieval status, requested/observed/confirmed
  live-access states, search/source/citation counts, clickable
  citations, cited-vs-consulted distinction, evidence digest, and the
  `provider_hosted_web_retrieval` trace node. TCS did not fetch pages;
  the provider hosted the retrieval.

## 11. Return to Demo Mode and shutdown

* Toolbar → LIVE MODE badge → one click returns to DEMO MODE: new
  external requests are blocked immediately, in-memory keys are
  cleared, live execution state resets; existing live results keep
  their truthful labels.
* Shutdown: stop the frontend preview, Ctrl-C the uvicorn backend.
* Revoke/rotate the temporary test keys at the provider consoles when
  the demonstration is finished.

## 12. Owner-executed live credential smoke (REQUIRED preflight before
any investor session that will demonstrate live functionality)

1. Enter a limited, spend-capped OpenAI key; create/activate
   `TCS Test Key`.
2. Run one ordinary Live LLM query → real answer, decision recorded.
3. Run one deterministic non-Allow governance-triggering query
   (prompt-injection phrase) → live Stop.
4. Verify the v2 Trust Certificate issued for each (Audit →
   Certificates; `calculation_version: tis-v2`).
5. Verify persistence, hashing, audit, and replay (chain health true;
   Replay works with no provider call).
6. Verify key absence from localStorage (DevTools → Application),
   backend logs, artifacts, certificates, traces, responses, errors.
7. Refresh the browser → key gone, re-entry required.
8. Repeat 1–7 with an Anthropic test key.
9. Run one Live Web query per enabled provider; verify visible
   citations and the evidence digest.
10. Revoke the temporary keys when appropriate.

## 13. Emergency procedure

1. **Return to Demo Mode** (toolbar, single click) — external calls
   stop immediately, in-memory keys are cleared.
2. **Stop the backend** (Ctrl-C on uvicorn) if a stronger stop is
   needed.
3. **Revoke/rotate the test key** at the provider console if exposure
   is suspected.
4. **Restore the paired code/database snapshot** (the archive created
   in step 4 plus the git tag/commit of this branch) if data
   integrity fails; chain health in Preflight verifies the restore.

## 14. Rollback / troubleshooting

* Mode badge disagrees with backend → the badge resyncs on window
  focus; the backend is always authoritative and blocks external
  calls in Demo Mode regardless of the badge.
* Preflight `chain_intact: false` → do not present; restore per §13.4.
* Provider errors during live segments render as provider failures —
  they are never governance decisions; switch the narrative back to
  the deterministic Demo Mode scenarios at any time.
