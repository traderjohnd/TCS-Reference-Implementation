# TCS Reference Implementation

The **Trust Computation System (TCS)** is a formal framework for runtime AI governance that quantifies trust as a computable, policy-enforceable, and attributable property of AI outputs. TCS computes a Trust Integrity Score (TIS) across four governance dimensions -- Boundedness (B), Attribution (A), Compliance (C), and Known (K) -- evaluated against a resolved policy profile parameterized by risk tier, action class, and data acquisition connection type. Every evaluation produces a Trust Certificate: a tamper-evident, hash-chained, identity-attributed governance record suitable for regulatory examination. The formal specification, pilot results, and mathematical foundations are presented in the companion whitepaper: [Computable Trust Architecture: A Formal Framework for Runtime AI Governance](https://arxiv.org/abs/PLACEHOLDER) (DeRudder, 2026).

## Canonical Test Vectors

The file [`canonical/canonical_test_vectors.json`](canonical/canonical_test_vectors.json) contains the eight deterministic governance scenarios used to verify the TIS engine against the formal specification. Each vector specifies exact inputs (dimension scores, penalty events, elapsed time, invalidation state) and exact expected outputs (TIS\_raw, TIS\_adj, TIS\_current, decision).

The eight scenarios cover:

| # | Scenario | Decision | What It Proves |
|---|----------|----------|----------------|
| 1 | Healthcare C3 prohibited pattern | **Stop** | C3=0.00 hard stop; kappa bypass; gate collapse |
| 2 | Healthcare allow with novelty | **Allow** | All gates pass; novelty triggers human review |
| 3 | Financial services clean allow | **Allow** | Full penalty system; policy-sensitive flag |
| 4 | Financial attribution gate failure | **Hold** | Gate-path Hold via soft-hold ceiling kappa |
| 5 | Enterprise informational | **Allow** | K scored but not gated at r1; not\_applicable |
| 6 | High-risk K gate | **Hold** | K gate enforcement at r3; Hold vs Stop distinction |
| 7 | Invalidation event | **Stop** | Active invalidation overrides all terms at Priority 1 |
| 8 | Temporal decay (4 time points) | **Allow -> Hold -> Escalate** | Decay formula; decision transitions over time |

All floating-point comparisons use 4-decimal rounding. If a test fails, the code is wrong -- the expected outputs are the specification.

## Engine Contract

The TIS computation engine (`tcs/tis_engine.py`) is a **pure function** with three invariants:

1. **No I/O** -- does not read files, call APIs, or access databases
2. **No state** -- does not read or write module-level mutable state
3. **Deterministic** -- the same `TISInput` always produces the same `TISResult`

The engine receives a fully resolved `TISInput` from the Governed Context Architecture and returns a `TISResult`. It never knows whether its input came from a test fixture or five live MCP server calls. This separation is what makes the engine testable, auditable, and connection-type-agnostic.

The public interface is documented in [`canonical/engine_signature.py`](canonical/engine_signature.py):

```python
def compute_tis(inp: TISInput) -> TISResult:
    """
    Pipeline: validate -> TIS_raw -> penalties -> TIS_adj -> gate -> decay -> invalidation -> TIS_current
    Canonical formula: TIS = G * SUM(w_i * dim_i) * (1-P) * exp(-mu*dt) * I_inv
    """
```

## Running the Tests

```bash
pip install pydantic python-dateutil pytest fastapi uvicorn httpx
python -m pytest tests/ -q
```

Expected output:

```
474 passed in ~6s
```

The 474 tests include:
- 8 canonical specification scenarios (Phase 1)
- 11 Phase 2 integration scenarios (CT-4, chain uncertainty, fail-safe, enforcement)
- 455 unit and integration tests across all modules (engine, decision, certificate, persistence, API, drift, PLL, recovery, simulation, regulatory packs, RBAC, healthcare demo, control plane)

## Trust Certificate Chain

The file [`canonical/chain_verification_example.json`](canonical/chain_verification_example.json) demonstrates the hash chain integrity mechanism described in the whitepaper (Section VI, Audit Integrity layer).

Each Trust Certificate contains:
- `tc_hash` -- SHA-256 of the TC content (excluding the audit\_integrity layer itself)
- `previous_tc_hash` -- hash of the prior TC in the chain (null for the first)
- `chain_sequence` -- monotonically increasing integer (gap = integrity violation)

The example shows two consecutive TCs (an Allow followed by a Hold) with verified chain linkage:

```
TC-1: chain_sequence=1, previous_tc_hash=null
      tc_hash=4ddf09921791efb0e3ffb09bbed024de7d8483294953f7b5c076798728aece9e

TC-2: chain_sequence=2
      previous_tc_hash=4ddf09921791efb0e3ffb09bbed024de7d8483294953f7b5c076798728aece9e
      tc_hash=a4f49f181b716ac3f67413ef6fecfee5a5ed799ee8aa7eb91a9e4a920ce9d0de
```

Chain verification checks three properties:
1. **Content integrity** -- recomputed hash matches recorded tc\_hash
2. **Chain linkage** -- each TC's previous\_tc\_hash equals the prior TC's tc\_hash
3. **Sequence continuity** -- no gaps in chain\_sequence (deletion detection)

The `verify_chain()` function in `tcs/trust_certificate.py` implements this verification. Any modification, insertion, or deletion of a TC in the chain is cryptographically detectable.

## Project Structure

```
TCS-Reference-Implementation/
  README.md
  requirements.txt
  canonical/                    -- Whitepaper reference artifacts
    canonical_test_vectors.json -- 8 deterministic governance scenarios
    engine_signature.py         -- Pure-function contract documentation
    chain_verification_example.json -- Hash chain integrity proof
  tcs/
    tis_engine.py               -- TIS computation (pure function)
    decision_engine.py          -- Priority-ordered decision mapping
    trust_certificate.py        -- 11-layer TC generation + hash chain
    policy_profiles.py          -- Domain policy configurations
    governed_context.py         -- GCA with CT-4 resolution + injection detection
    persistence/                -- SQLite append-only TC store
    sidecar/                    -- Enforcement controller + request interceptor
    dynamics/                   -- Trust loss, drift detection, PLL, recovery
    api/                        -- FastAPI service (POST /v1/govern, GET /v1/certificates)
    packs/                      -- Regulatory compliance packs (5 domains)
    identity/                   -- RBAC roles + session management
  tests/                        -- 474-test verification suite
  demos/
    finance/                    -- 10-scenario financial RAG governance demo
    healthcare/                 -- 8-scenario clinical AI governance demo
  frontend/                     -- React 18 control plane SPA
```

## Author

John DeRudder | Trust Computation System v0.1 | April 2026
