// =============================================================================
// governedDecimal — the frontend's single numerical + version boundary for
// Trust Certificate data (tis-v2, Commit 6).
//
// The /v2 wire carries AUTHORITATIVE certificate values as canonical
// fixed-scale decimal STRINGS (4dp, [0,1]) — plus the deliberate
// variable-scale exception `component_scores_raw`. JavaScript's binary
// Number cannot represent these exactly, so the invariant is:
//
//     Authoritative v2 decimal string
//         → governedDecimal parser (scaled integers / BigInt) for comparison
//         → verbatim display for rendering
//         → NEVER Number() / parseFloat() / toFixed()
//
// v1 records (historical) carry JSON numbers and keep their float display
// formatting. Version dispatch is STRICT: absent-or-1 → v1, 2 → v2,
// anything else → unsupported (never best-effort rendered).
//
// A source-level guard test (conversionGuard.test.js) enforces the
// invariant across frontend/src.
// =============================================================================

// Canonical score-domain string: exactly 4dp, in [0, 1].
export const FIXED_SCORE_4DP = /^(?:0\.\d{4}|1\.0000)$/;

// Canonical non-negative parameter string: exactly 4dp, may exceed 1
// (elapsed_hours, resolved_decay_rate).
export const FIXED_PARAM_4DP = /^(?:0|[1-9]\d*)\.\d{4}$/;

// Variable-scale raw-evidence string (component_scores_raw): a plain
// non-negative decimal in [0, 1] with any number of fractional digits,
// no exponent, no signs, no whitespace. "1" and "0" are valid.
export const RAW_DECIMAL = /^(?:0|1|0\.\d+|1\.0+)$/;

/**
 * Error for non-canonical governed values. Deliberately carries the FIELD
 * NAME ONLY — never the offending value — so integrity warnings cannot
 * echo sensitive or malformed content.
 */
export class GovernedDecimalError extends Error {
  constructor(field, reason) {
    super(`${field}: ${reason}`);
    this.name = 'GovernedDecimalError';
    this.field = field;
    this.reason = reason;
  }
}

/**
 * Parse a canonical 4dp score string into a scaled integer (basis
 * points ×100): "0.9000" → 9000, "1.0000" → 10000.
 *
 * String-strip, not float-multiply: removing the decimal point avoids a
 * binary floating-point round trip entirely. The regex is REQUIRED —
 * without it "0.9" would strip to 9, not 9000 (the real failure mode
 * of the strip method).
 */
export function parseScore4(value, field) {
  if (typeof value !== 'string' || !FIXED_SCORE_4DP.test(value)) {
    throw new GovernedDecimalError(field, 'not a canonical 4dp score string');
  }
  return globalThis.parseInt(value.replace('.', ''), 10);
}

/**
 * Parse a canonical 4dp non-negative parameter string (may exceed 1)
 * into a BigInt of 1/10000 units: "20.0000" → 200000n.
 */
export function parseFixed4(value, field) {
  if (typeof value !== 'string' || !FIXED_PARAM_4DP.test(value)) {
    throw new GovernedDecimalError(
      field, 'not a canonical 4dp parameter string');
  }
  return BigInt(value.replace('.', ''));
}

/** Compare two canonical score strings via scaled integers: -1 | 0 | 1. */
export function compareScore(a, b, field = 'score') {
  const ia = parseScore4(a, field);
  const ib = parseScore4(b, `${field} (rhs)`);
  return ia < ib ? -1 : ia > ib ? 1 : 0;
}

/**
 * Display formatter for governed values of EITHER wire generation.
 *
 *   canonical/raw decimal STRING (v2)  → rendered verbatim (no precision
 *                                        loss, no reformatting)
 *   number (v1 historical / display-tier metric) → fixed-digit display
 *   null / undefined                   → em dash
 *
 * Anything else renders as '—' rather than coercing.
 */
export function displayGoverned(value, digits = 4) {
  if (value === null || value === undefined) return '—';
  if (typeof value === 'string') return value;
  if (typeof value === 'number' && Number.isFinite(value)) {
    return value.toFixed(digits);
  }
  return '—';
}

// ── Strict certificate version dispatch ────────────────────────────────────

export const CERT_UNSUPPORTED = 'unsupported';

/**
 * Strict version discriminator (owner guardrail 1):
 *   certificate_schema_version absent or 1 → 1
 *   certificate_schema_version === 2       → 2
 *   anything else                          → 'unsupported'
 */
export function certificateVersion(record) {
  if (!record || typeof record !== 'object') return CERT_UNSUPPORTED;
  const v = record.certificate_schema_version;
  if (v === undefined || v === null || v === 1) return 1;
  if (v === 2) return 2;
  return CERT_UNSUPPORTED;
}

// Authoritative v2 scalar score-domain fields validated at the record
// boundary. (identity_confidence is an optional attested input; the
// scalar list below is what every v2 TC carries.)
const V2_SCORE_FIELDS = [
  's_base', 's_adjusted', 'tis_raw', 'tis_adjusted', 'tis_current',
  'penalty_aggregate', 'c3_score',
];
const V2_SCORE_DICTS = [
  'component_scores', 'component_scores_observed', 'component_weights',
];
const BACK = ['B', 'A', 'C', 'K'];

/**
 * Normalize one certificate record at the record boundary.
 *
 * Returns { version, gateResult, integrity: { ok, problems } } where
 * `problems` is a list of FIELD NAMES (never values). For v2 records:
 *
 *   - canonical governed values must be canonical STRINGS;
 *   - gate_result must be the integer 0 or 1 (legacy aliases like
 *     gate_passed are NEVER used as fallbacks on v2);
 *   - calculation_version must be present.
 *
 * A malformed v2 record is NOT coerced into the v1 presentation path
 * and never substitutes zero/NaN/rounded values — callers render an
 * explicit integrity warning and disable consequential actions.
 */
export function normalizeCertificate(record) {
  const version = certificateVersion(record);
  if (version === CERT_UNSUPPORTED) {
    return {
      version,
      gateResult: null,
      integrity: {
        ok: false,
        problems: ['certificate_schema_version'],
      },
    };
  }

  if (version === 1) {
    // Historical v1 record: JSON numbers, gate_passed boolean.
    const gp = record.gate_passed;
    return {
      version,
      gateResult: gp === true ? 1 : gp === false ? 0 : null,
      integrity: { ok: true, problems: [] },
    };
  }

  // version === 2 — strict validation, names only.
  const problems = [];
  for (const f of V2_SCORE_FIELDS) {
    if (f in record
        && (typeof record[f] !== 'string'
            || !FIXED_SCORE_4DP.test(record[f]))) {
      problems.push(f);
    } else if (!(f in record) && (f === 's_base' || f === 'tis_current')) {
      problems.push(f);
    }
  }
  for (const d of V2_SCORE_DICTS) {
    const dict = record[d];
    if (!dict || typeof dict !== 'object') {
      problems.push(d);
      continue;
    }
    for (const dim of BACK) {
      const v = dict[dim];
      if (typeof v !== 'string' || !FIXED_SCORE_4DP.test(v)) {
        problems.push(`${d}.${dim}`);
      }
    }
  }
  const raw = record.component_scores_raw;
  if (!raw || typeof raw !== 'object') {
    problems.push('component_scores_raw');
  } else {
    for (const dim of BACK) {
      const v = raw[dim];
      if (typeof v !== 'string' || !RAW_DECIMAL.test(v)) {
        problems.push(`component_scores_raw.${dim}`);
      }
    }
  }
  const gr = record.gate_result;
  const gateOk = gr === 0 || gr === 1;
  if (!gateOk) problems.push('gate_result');
  if (typeof record.calculation_version !== 'string'
      || !record.calculation_version) {
    problems.push('calculation_version');
  }

  return {
    version,
    gateResult: gateOk ? gr : null,
    integrity: { ok: problems.length === 0, problems },
  };
}

/**
 * Light row-level validation for stream/queue rows (which carry only a
 * decision + a few score fields, no schema version). A row is displayable
 * when its score fields are numbers (v1 rows / display metrics) or
 * canonical/raw decimal strings (v2 rows). Malformed rows keep rendering
 * as a warning row and lose consequential actions — one bad record never
 * blanks the queue.
 */
export function validateStreamRow(row) {
  const problems = [];
  const checkScore = (name) => {
    const v = row?.[name];
    if (v === null || v === undefined) return;
    if (typeof v === 'number' && Number.isFinite(v)) return;
    if (typeof v === 'string'
        && (FIXED_SCORE_4DP.test(v) || RAW_DECIMAL.test(v))) return;
    problems.push(name);
  };
  checkScore('tis_current');
  checkScore('s_base');
  for (const dim of BACK) {
    const cs = row?.component_scores;
    if (cs && dim in cs) {
      const v = cs[dim];
      if (v === null || v === undefined) continue;
      if (typeof v === 'number' && Number.isFinite(v)) continue;
      if (typeof v === 'string'
          && (FIXED_SCORE_4DP.test(v) || RAW_DECIMAL.test(v))) continue;
      problems.push(`component_scores.${dim}`);
    }
  }
  return { ok: problems.length === 0, problems };
}

/**
 * True when a rule-match record uses the FLAT typed v2 shape
 * (GovernanceRuleMatch) rather than the v1 nested `effect` shape.
 */
export function isFlatRuleMatch(m) {
  return !!m && typeof m === 'object'
    && ('schema_version' in m || 'matched_fact_keys' in m)
    && !('effect' in m);
}

// ── Govern-request construction (typed evaluation-typing fields) ───────────

const TYPED_GOVERN_FIELDS = ['risk_tier', 'action_class', 'connection_type'];

/**
 * Build a POST /v2/govern body. `risk_tier`, `action_class`, and
 * `connection_type` travel as dedicated TOP-LEVEL typed fields and are
 * NEVER placed into extra_metadata — the same names there are protected
 * keys and the API rejects the request with 422.
 */
export function buildGovernBody({
  query,
  retrievedChunks = [],
  candidateAnswer,
  riskTier = null,
  actionClass = null,
  connectionType = null,
  extraMetadata = null,
  ...rest
}) {
  const body = {
    query,
    retrieved_chunks: retrievedChunks,
    candidate_answer: candidateAnswer,
    ...rest,
  };
  if (extraMetadata) {
    const cleaned = { ...extraMetadata };
    for (const k of TYPED_GOVERN_FIELDS) {
      // Refuse silently-smuggled typed fields rather than sending a
      // request the API will 422.
      if (k in cleaned) {
        throw new GovernedDecimalError(
          k, 'must be passed as a typed field, not inside extra metadata');
      }
    }
    body.extra_metadata = cleaned;
  }
  if (riskTier !== null) body.risk_tier = riskTier;
  if (actionClass !== null) body.action_class = actionClass;
  if (connectionType !== null) body.connection_type = connectionType;
  return body;
}

// ── Protected-metadata 422 envelope ────────────────────────────────────────

/**
 * Discriminate the protected-metadata violation from an ordinary 422.
 *
 * Real backend contract (captured from the live FastAPI route):
 *
 *   protected-metadata violation:
 *     { "detail": { "error": "protected_metadata_keys",
 *                   "message": "...", "rejected_keys": ["a", "b.c"] } }
 *
 *   ordinary validation 422:
 *     { "detail": [ { "type": "missing", "loc": [...], "msg": "..." } ] }
 *
 * Returns { message, rejectedKeys } (key NAMES only) or null when the
 * error is not a protected-metadata violation.
 */
export function parseProtectedMetadataError(detail) {
  if (!detail || typeof detail !== 'object' || Array.isArray(detail)) {
    return null;
  }
  if (detail.error !== 'protected_metadata_keys') return null;
  return {
    message: typeof detail.message === 'string' ? detail.message : '',
    rejectedKeys: Array.isArray(detail.rejected_keys)
      ? detail.rejected_keys.map(String)
      : [],
  };
}
