# CLAUDE Project Context — TCS reference implementation

Durable project instructions for any AI coding session (Claude Code or
otherwise) working in this repository. This file is the tracked
counterpart to the gitignored local `CLAUDE.md`. When the two disagree,
this file wins — `CLAUDE.md` is allowed to add per-developer preferences
on top, but must not contradict anything here.

---

## 1. Read this first, every session

- **[PHASE_5_HANDOFF.md](PHASE_5_HANDOFF.md)** — current state, Phase 5
  slice summary, endpoints, recommended next work. Re-read at the start
  of every session; it is the source of truth for "where are we right
  now."
- [../ARCHITECTURE.md](../ARCHITECTURE.md) — layered architecture map and
  cross-phase invariants.
- [../README.md](../README.md) — install, run, demo pack.
- [PHASE_4_ACCEPTANCE.md](PHASE_4_ACCEPTANCE.md) — formal acceptance gate
  Phase 5 builds on top of.

---

## 2. Non-negotiable invariants

These are pinned by the project owner and enforced by tests. Any change
that weakens any one of these must be flagged and discussed before it
lands.

### Mathematical / model invariants

1. **BACK, not BACU.** The four primary governance dimensions are
   **B**oundedness, **A**ttribution, **C**ompliance, **K**nown.
2. **`K` is the fourth primary dimension** — not `U`. `U` exists only as
   a derived uncertainty quantity (`U_chain`, `U_t`, `uncertainty_mass`).
   Never reintroduce `U` as a primary dimension.
3. **`S_base` is the gate-independent composite.** It is the white-paper
   weighted sum across dimensions, computed without applying gates.
4. **`tis_raw = G × S_base`.** The gate multiplier collapses `tis_raw` to
   0 on gate failure but does not change `S_base`.
5. **`κ` is a remediability floor**, not a generic threshold:
   `S_base ≥ κ → HOLD`, `S_base < κ → STOP`.
6. The TIS engine and decision engine remain **deterministic** and
   **generation-agnostic**. Same `TISInput` → same `TISResult`. They do
   not know whether their input came from a unit-test fixture or a live
   workflow.

### Runtime / replay invariants

7. **No connector constructs `TISInput` directly.** Connectors emit
   `GovernanceEvent`s with normalized BACK signals. The GCA is the only
   layer that compiles events into a `TISInput`.
8. **`raw_llm` mode is truly raw.** No hidden RAG, no hidden system
   prompt, no hidden policy framing. The `system_prompt_used` field
   records exactly what was sent to the model (or `None` when nothing
   was sent).
9. **Replay must not re-call the LLM.** `/v2/evaluate` and `/v2/replay`
   are pure reads against the stored `ResponseArtifact`. The
   architectural guardrail tests patch every provider client to raise;
   these endpoints must still succeed.
10. **Same artifact + same policy + runtime snapshot reproduces the
    decision** deterministically. This is the Slice 5.4a guarantee:
    `compute_tis(tis_input_from_snapshot(s))` produces the same
    `TISResult` as the original.

### TC issuance invariants

11. **`what_if` creates a `GovernanceEvaluation` but NO Trust
    Certificate.** Counterfactual only.
12. **`observe` and `enforce` create Trust Certificates** with correct
    lifecycle semantics. `observe` TCs are marked
    `lifecycle_state="observed"` so they cannot be confused with
    `enforce`-mode TCs that altered delivery.

### Security / hygiene invariants

13. **No stored API keys.** Provider keys are passed into the client
    constructor for a single call and never written to the artifact,
    the DB, or the logs.
14. **No canned LLM-style domain answers** anywhere in the code path.
    Generation is what produces text; evaluation never fabricates a
    "response."

### Process invariants

15. **No new architecture without approval.** Phase 5 feature work is
    done. Current focus is demo-hardening and operational visibility.
    Do not introduce new abstractions, services, or modules without
    explicit sign-off.
16. **Verify before recommending.** Run the test suite and rebuild the
    frontend after any non-trivial change. The Phase 4 acceptance
    pattern (10 criteria with evidence per row) is the model.
17. **Use the worktree the session opens in.** Don't switch branches
    unless `git log` shows that expected commits are missing.
18. **Investigate unfamiliar state before acting.** Don't reset,
    force-push, delete branches, or bypass hooks with `--no-verify`
    when something looks off. Resolve the underlying issue.

---

## 3. Where things live (high-level map)

```
tcs/
  tis_engine.py          pure TIS computation
  decision_engine.py     priority-ordered decision ladder (paper-aligned)
  trust_certificate.py   11-layer TC + composer_metadata + hash chain
  policy_profiles.py     canonical defaults + baseline-no-pack fallback
  governed_context.py    GCA — assemble_context_* entry points
  workflow/              trace, events, orchestrator, connectors
  standards/             library + hybrid/strictest-control composer
  artifacts/             Phase 5 — ResponseArtifact, GovernanceEvaluation,
                         evaluation engine, ArtifactStore
  governance/            governance rules (typed-context, term-group, ...)
  packs/                 regulatory packs + composed-pack registration
  persistence/           SQLite (append-only triggers); Postgres backend
  api/                   FastAPI; routes_{generate,evaluate,replay,query,...}
  identity/              RBAC + sessions
  dynamics/              drift, PLL, recovery

frontend/src/views/      React control plane (Connections, PolicyControls,
                         GovernedChat, GovernanceReplay, LiveDecisions,
                         AuditCertificates, ...)

tests/                   Tests across all phases incl. Phase 5 slices
docs/                    PHASE_4_*, PHASE_5_HANDOFF, this file
demos/                   phase4_demo.py + governed_rag + healthcare
```

---

## 4. Local-only configuration

`CLAUDE.md` at the repo root is gitignored by project policy
(see `.gitignore`). Individual sessions may keep a local `CLAUDE.md`
with developer-specific shortcuts, but those preferences:

- must not contradict anything in this file, and
- must not be relied on by other sessions or other developers.

If a rule from a local `CLAUDE.md` becomes load-bearing for the
project, promote it here and commit.
