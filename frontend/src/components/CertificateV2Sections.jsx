// =============================================================================
// CertificateV2Sections — shared rendering blocks for tis-v2 certificates
// (Commit 6). Used by AuditCertificates and the LiveDecisions TC drawer.
//
// Everything here renders AUTHORITATIVE v2 values via displayGoverned
// (canonical decimal strings verbatim; v1 numbers formatted) — never
// Number()/parseFloat()/toFixed() on governed fields.
// =============================================================================

import { displayGoverned } from '../lib/governedDecimal';

const BACK = ['B', 'A', 'C', 'K'];
const DIMENSION_NAME = {
  B: 'Boundedness', A: 'Attribution', C: 'Compliance', K: 'Known',
};

// ── Integrity / unsupported states ─────────────────────────────────────────

export function UnsupportedCertificate({ version }) {
  return (
    <div
      role="alert"
      className="bg-red-900/30 border border-red-800 rounded-lg p-3 text-sm text-red-300"
    >
      <div className="font-semibold mb-1">
        Unsupported or invalid certificate data
      </div>
      <p className="text-xs text-red-200/80">
        This record declares certificate schema version{' '}
        <span className="font-mono">{String(version)}</span>, which this
        operator surface does not support. It is not rendered best-effort
        and no actions are available for it.
      </p>
    </div>
  );
}

export function IntegrityWarning({ problems }) {
  if (!problems || problems.length === 0) return null;
  return (
    <div
      role="alert"
      className="bg-red-900/30 border border-red-800 rounded-lg p-3 text-sm text-red-300"
    >
      <div className="font-semibold mb-1">
        Certificate data failed integrity validation
      </div>
      <p className="text-xs text-red-200/80 mb-1.5">
        The following authoritative fields are missing or not in canonical
        form. Values are not substituted or repaired; consequential
        actions are disabled for this record.
      </p>
      <div className="flex flex-wrap gap-1">
        {problems.map((p) => (
          <span
            key={p}
            className="text-[10px] font-mono bg-red-900/40 border border-red-800 rounded px-1.5 py-0.5"
          >
            {p}
          </span>
        ))}
      </div>
    </div>
  );
}

// ── Version identifiers ────────────────────────────────────────────────────

export function VersionsPanel({ tc, version }) {
  const rows = version === 2
    ? [
        ['certificate_schema_version', tc.certificate_schema_version],
        ['calculation_version', tc.calculation_version],
        ['score_precision_policy', tc.score_precision_policy],
        ['decay_algorithm_version', tc.decay_algorithm_version],
      ]
    : [
        ['certificate_schema_version', '1 (legacy — field absent on wire)'],
        ['calculation_version', tc.calculation_version || 'tis-v1-legacy'],
      ];
  return (
    <div className="border border-gray-800 rounded p-2">
      <h4 className="text-xs font-medium text-blue-400 uppercase tracking-wider mb-1">
        Schema &amp; Calculation Versions
      </h4>
      <dl className="space-y-0.5">
        {rows.map(([k, v]) => (
          <div key={k} className="flex justify-between text-xs">
            <dt className="text-gray-500">{k}</dt>
            <dd className="text-gray-300 font-mono max-w-[60%] text-right truncate">
              {String(v ?? '—')}
            </dd>
          </div>
        ))}
      </dl>
    </div>
  );
}

// ── Gate summary (single vocabulary) ───────────────────────────────────────

export function GateSummary({ gateResult }) {
  if (gateResult !== 0 && gateResult !== 1) {
    return (
      <span className="text-xs font-mono text-gray-500">gate: —</span>
    );
  }
  return (
    <span
      className={`text-xs font-mono ${
        gateResult === 1 ? 'text-green-400' : 'text-red-400 font-semibold'
      }`}
    >
      gate: {gateResult === 1 ? 'PASS' : 'FAIL'} ({gateResult})
    </span>
  );
}

// ── Score tiers: raw → observed → effective ────────────────────────────────

export function ScoreTiersPanel({ tc }) {
  const raw = tc.component_scores_raw || {};
  const observed = tc.component_scores_observed || {};
  const effective = tc.component_scores || {};
  const thresholds = tc.thresholds || {};
  const gateResults = tc.gate_results || {};
  return (
    <div className="border border-gray-800 rounded p-2">
      <h4 className="text-xs font-medium text-blue-400 uppercase tracking-wider mb-1">
        Dimension Score Tiers
      </h4>
      <p className="text-[10px] text-gray-500 mb-2 leading-snug">
        raw (as received, variable scale) → observed (canonical,
        pre-adjustment) → effective (post-adjustment). Gate verdicts are
        produced by the EFFECTIVE score against the canonical threshold —
        the number shown beside each verdict is the number that produced
        it.
      </p>
      <table className="w-full text-xs">
        <thead>
          <tr className="text-left text-gray-500 border-b border-gray-800">
            <th className="pb-1 pr-2">dim</th>
            <th className="pb-1 pr-2">raw</th>
            <th className="pb-1 pr-2">observed</th>
            <th className="pb-1 pr-2">effective</th>
            <th className="pb-1 pr-2">threshold</th>
            <th className="pb-1">gate</th>
          </tr>
        </thead>
        <tbody>
          {BACK.map((dim) => {
            const result = gateResults[dim];
            return (
              <tr key={dim} className="border-b border-gray-800/50">
                <td className="py-1 pr-2 font-mono text-gray-400">
                  {dim}
                  <span className="text-gray-600 ml-1 font-sans">
                    {DIMENSION_NAME[dim]}
                  </span>
                </td>
                <td className="py-1 pr-2 font-mono text-gray-500">
                  {displayGoverned(raw[dim])}
                </td>
                <td className="py-1 pr-2 font-mono text-gray-400">
                  {displayGoverned(observed[dim])}
                </td>
                <td
                  className={`py-1 pr-2 font-mono ${
                    result === 'fail'
                      ? 'text-red-300 font-semibold'
                      : 'text-gray-200'
                  }`}
                  data-testid={`effective-${dim}`}
                >
                  {displayGoverned(effective[dim])}
                </td>
                <td className="py-1 pr-2 font-mono text-gray-500">
                  {displayGoverned(thresholds[dim])}
                </td>
                <td className="py-1">
                  {result === 'fail' ? (
                    <span className="text-red-400 font-semibold">FAIL</span>
                  ) : result === 'pass' ? (
                    <span className="text-green-400">PASS</span>
                  ) : (
                    <span className="text-gray-600">
                      {result || 'not_applicable'}
                    </span>
                  )}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

// ── Ordered identity adjustments ───────────────────────────────────────────

// §19.1 is a min() → "clamped to"; §19.2 is an assignment → "set to".
const ADJUSTMENT_VERB = {
  TCS_SPEC_19_1: 'clamped to',
  TCS_SPEC_19_2: 'set to',
};

export function AdjustmentsPanel({ adjustments }) {
  const list = Array.isArray(adjustments) ? adjustments : [];
  return (
    <div className="border border-gray-800 rounded p-2">
      <h4 className="text-xs font-medium text-blue-400 uppercase tracking-wider mb-1">
        Adjustments Applied ({list.length})
      </h4>
      {list.length === 0 ? (
        <p className="text-xs text-gray-500">
          No identity adjustments changed a score — observed and effective
          values are identical.
        </p>
      ) : (
        <ol className="space-y-1 list-decimal list-inside">
          {list.map((a, i) => (
            <li key={`${a.rule_id}-${i}`} className="text-xs text-gray-300">
              <span className="font-mono text-gray-400">{a.dimension}</span>{' '}
              <span className="font-mono">{displayGoverned(a.value_before)}</span>{' '}
              {ADJUSTMENT_VERB[a.rule_id] || 'changed to'}{' '}
              <span className="font-mono">{displayGoverned(a.value_after)}</span>
              <span className="text-gray-500">
                {' '}— {a.rule_id}
                {a.reason ? ` (${a.reason})` : ''}
              </span>
            </li>
          ))}
        </ol>
      )}
    </div>
  );
}

// ── Typed C3 provenance ────────────────────────────────────────────────────

export function C3ProvenancePanel({ tc }) {
  const records = Array.isArray(tc.c3_provenance) ? tc.c3_provenance : [];
  return (
    <div className="border border-gray-800 rounded p-2">
      <h4 className="text-xs font-medium text-blue-400 uppercase tracking-wider mb-1">
        C3 Provenance
      </h4>
      <div className="flex justify-between text-xs mb-1">
        <span className="text-gray-500">c3_score</span>
        <span className="font-mono text-gray-300">
          {displayGoverned(tc.c3_score)}
        </span>
      </div>
      {records.length === 0 ? (
        <p className="text-xs text-gray-500">
          No C3 provenance records — C3 is nonzero for this evaluation.
        </p>
      ) : (
        <div className="space-y-1.5">
          {records.map((r, i) => (
            <div
              key={`${r.source_type}-${i}`}
              className="bg-gray-900/60 border border-gray-800 rounded p-1.5 text-[11px]"
            >
              <div className="flex justify-between">
                <span className="text-amber-300 font-mono">{r.source_type}</span>
                {r.detail_code ? (
                  <span className="text-gray-400 font-mono">{r.detail_code}</span>
                ) : null}
              </div>
              <div className="text-gray-500 mt-0.5 space-y-0.5">
                {r.pattern_id ? (
                  <div>
                    pattern <span className="font-mono text-gray-400">{r.pattern_id}</span>
                    {r.pattern_set_version ? (
                      <span className="text-gray-600"> @ {r.pattern_set_version}</span>
                    ) : null}
                  </div>
                ) : null}
                {r.location_tag ? (
                  <div>location <span className="font-mono text-gray-400">{r.location_tag}</span></div>
                ) : null}
                {r.connector_type ? (
                  <div>connector <span className="font-mono text-gray-400">{r.connector_type}</span></div>
                ) : null}
                {Array.isArray(r.rule_match_refs) && r.rule_match_refs.length > 0 ? (
                  <div>
                    rules{' '}
                    {r.rule_match_refs.map((x) => (
                      <span key={`${x.rule_id}@${x.rule_version}`} className="font-mono text-gray-400 mr-1">
                        {x.rule_id}@{x.rule_version}
                      </span>
                    ))}
                  </div>
                ) : null}
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

// ── Flat typed rule matches (v2) ───────────────────────────────────────────

export function RuleMatchesV2({ matches }) {
  const list = Array.isArray(matches) ? matches : [];
  if (list.length === 0) {
    return (
      <p className="text-xs text-gray-500">
        No governance rules matched. Decision came from BACK/TIS scoring
        alone.
      </p>
    );
  }
  return (
    <div className="space-y-2">
      {list.map((m, i) => (
        <div
          key={`${m.rule_id}-${i}`}
          className="bg-gray-900/60 border border-gray-800 rounded p-2 text-[11px]"
        >
          <div className="flex items-baseline justify-between gap-2">
            <span className="text-amber-300 font-mono">{m.rule_id}</span>
            <span className="text-gray-600">v{m.rule_version}</span>
          </div>
          <div className="text-gray-500 mt-0.5">
            {m.control_class} · {m.safety_category || '—'} ·{' '}
            {m.decision_pressure || '—'}
            {m.c3_violation ? ' · C3 violation' : ''}
          </div>
          {m.blocking_reason ? (
            <div className="text-red-300 mt-0.5 font-mono">
              {m.blocking_reason}
            </div>
          ) : null}
          {Array.isArray(m.matched_fact_keys) && m.matched_fact_keys.length > 0 ? (
            <div className="text-gray-400 mt-1">
              matched fact keys{' '}
              {m.matched_fact_keys.map((k) => (
                <span key={k} className="font-mono bg-gray-800 border border-gray-700 rounded px-1 mr-1">
                  {k}
                </span>
              ))}
              <span className="text-gray-600 italic ml-1">
                (keys only — fact values never enter a portable certificate)
              </span>
            </div>
          ) : null}
          {m.explanation ? (
            <div className="text-gray-400 mt-1 leading-snug">{m.explanation}</div>
          ) : null}
        </div>
      ))}
    </div>
  );
}

