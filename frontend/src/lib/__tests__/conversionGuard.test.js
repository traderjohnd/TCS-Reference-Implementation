// =============================================================================
// Numerical-conversion audit guard (owner guardrail 4, Commit 6).
//
// Invariant:
//
//     Authoritative v2 decimal string
//         → governedDecimal parser or verbatim display
//         → NEVER Number() / parseFloat() / toFixed()
//
// This test scans every source line under frontend/src (tests and this
// guard excluded) and fails when an authoritative governed field name
// appears on the same line as a direct Number(/parseFloat(/.toFixed(
// conversion. It is deliberately a line-level lint: coarse enough to be
// dependable, precise enough that the repository currently passes with
// ZERO allowlisted lines.
//
// Presentation-only float metrics (telemetry stream rows, evaluation
// dashboard rows, aggregate reporting averages) don't trip the guard
// because their field names differ or they render via displayGoverned.
// If a legitimately-float display line must ever mention an
// authoritative name AND convert on the same line, annotate it with
//     // governed-display-float: <field> — <why this is display tier>
// and it will be reported in the allowlist assertion below.
// =============================================================================

/* global process */
import { describe, expect, it } from 'vitest';
import { readFileSync, readdirSync, statSync } from 'node:fs';
import { join, relative } from 'node:path';

// Vitest runs with cwd = frontend/ (the package root).
const SRC_ROOT = join(process.cwd(), 'src');

// The authoritative field list pinned by the repository owner.
const AUTHORITATIVE_FIELDS = [
  'component_scores',
  'component_scores_observed',
  'component_scores_raw',
  'component_weights',
  'thresholds',
  's_base',
  's_adjusted',
  'tis_raw',
  'tis_adjusted',
  'tis_current',
  'penalty_aggregate',
  'penalty_breakdown',
  'resolved_penalty_weights',
  'resolved_decay_rate',
  'elapsed_hours',
  'decay_factor',
  'resolved_theta_allow',
  'resolved_theta_hold',
  'resolved_theta_escalate',
  'resolved_kappa',
  'c3_score',
  'identity_confidence',
];

const CONVERSION = /(?:\bNumber\s*\(|\bparseFloat\s*\(|\.toFixed\s*\()/;
const ANNOTATION = 'governed-display-float:';

function* walk(dir) {
  for (const entry of readdirSync(dir)) {
    const full = join(dir, entry);
    const st = statSync(full);
    if (st.isDirectory()) {
      if (entry === '__tests__' || entry === 'node_modules') continue;
      yield* walk(full);
    } else if (/\.(js|jsx)$/.test(entry) && !/\.test\./.test(entry)) {
      yield full;
    }
  }
}

function scan() {
  const violations = [];
  const annotated = [];
  for (const file of walk(SRC_ROOT)) {
    const rel = relative(SRC_ROOT, file).replace(/\\/g, '/');
    if (rel === 'test/setup.js' || rel === 'test/fixtures.js') continue;
    const lines = readFileSync(file, 'utf-8').split(/\r?\n/);
    lines.forEach((line, i) => {
      if (!CONVERSION.test(line)) return;
      const fields = AUTHORITATIVE_FIELDS.filter((f) =>
        new RegExp(`\\b${f}\\b`).test(line));
      if (fields.length === 0) return;
      const entry = `${rel}:${i + 1} [${fields.join(', ')}]`;
      if (line.includes(ANNOTATION)) annotated.push(entry);
      else violations.push(entry);
    });
  }
  return { violations, annotated };
}

describe('authoritative-v2 conversion guard', () => {
  it('no authoritative field is passed directly into Number/parseFloat/toFixed', () => {
    const { violations } = scan();
    expect(violations, [
      'Authoritative governed fields must flow through governedDecimal',
      'parsers or verbatim display — never Number()/parseFloat()/',
      '.toFixed(). Offending lines:',
      ...violations,
    ].join('\n')).toEqual([]);
  });

  it('the documented display-float allowlist stays empty', () => {
    // If a future change annotates a line with governed-display-float:,
    // this pin makes the exception explicit and reviewable.
    const { annotated } = scan();
    expect(annotated).toEqual([]);
  });
});
