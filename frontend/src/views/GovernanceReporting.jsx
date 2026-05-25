import { useState } from 'react';
import {
  Area, AreaChart, Bar, BarChart, CartesianGrid, Cell, Legend, Line,
  LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis,
} from 'recharts';
import { useApi } from '../hooks/useApi';

// =============================================================================
// Phase 5 — Runtime governance reporting and trust telemetry.
//
// This view is the sibling of Live (now), Audit (one decision) and
// Replay (cross-policy). It answers "how is governance behaving over
// time?" using read-only aggregates over the existing TC + lifecycle_event
// store. Eight panels:
//
//   1. Decisions over time             (stacked area)
//   2. Score averages over time        (two-line: S_base, TIS_current)
//   3. Non-allow trends                (line: Hold / Escalate / Stop)
//   4. Failed BACK dimensions          (horizontal bars: B / A / C / K)
//   5. Decisions by policy / pack      (table)
//   6. Override rate by policy         (table)
//   7. Top triggered governance rules  (table)
//   8. Recent override activity        (table)
//
// No mutation. No drill-down editing. No charts beyond what `recharts`
// already provides.
// =============================================================================

const WINDOW_OPTIONS = [
  { id: '24h', label: '24h' },
  { id: '7d',  label: '7d'  },
  { id: '30d', label: '30d' },
];

const DECISION_COLORS = {
  Allow:    '#22c55e',
  Observe:  '#10b981',
  Hold:     '#f59e0b',
  Escalate: '#fb923c',
  Stop:     '#ef4444',
  Allow_with_logging:   '#22c55e',
  Allow_with_redaction: '#22c55e',
  Allow_with_step_up:   '#f59e0b',
  Rollback: '#ef4444',
};

const BACK_COLOR = '#60a5fa';

// ── Window selector ─────────────────────────────────────────────────────────

function WindowSelector({ window, onChange }) {
  return (
    <div className="flex items-center gap-2">
      <span className="text-xs text-gray-500 uppercase tracking-wide">Window</span>
      <div className="inline-flex rounded border border-gray-700 overflow-hidden">
        {WINDOW_OPTIONS.map((opt) => (
          <button
            key={opt.id}
            onClick={() => onChange(opt.id)}
            className={`px-3 py-1 text-xs font-mono transition-colors ${
              window === opt.id
                ? 'bg-blue-900/40 text-white'
                : 'bg-gray-900 text-gray-400 hover:text-white'
            }`}
          >
            {opt.label}
          </button>
        ))}
      </div>
    </div>
  );
}

// ── Panel shell + states ────────────────────────────────────────────────────

function PanelShell({ title, subtitle, children }) {
  return (
    <div className="bg-gray-900 border border-gray-800 rounded-lg p-4 flex flex-col min-h-[18rem]">
      <div className="mb-3">
        <h3 className="text-sm font-medium text-white">{title}</h3>
        {subtitle && (
          <p className="text-[11px] text-gray-500 mt-0.5">{subtitle}</p>
        )}
      </div>
      <div className="flex-1 min-h-0">{children}</div>
    </div>
  );
}

function EmptyState({ message }) {
  return (
    <div className="h-full flex items-center justify-center text-xs text-gray-500 italic">
      {message || 'No governance activity in the selected window.'}
    </div>
  );
}

function LoadingState() {
  return (
    <div className="h-full flex items-center justify-center text-xs text-gray-500 italic">
      Loading…
    </div>
  );
}

function ErrorState({ error }) {
  return (
    <div className="h-full flex items-center justify-center text-xs text-red-400">
      {String(error)}
    </div>
  );
}

// ── Helpers ─────────────────────────────────────────────────────────────────

function shortTime(iso) {
  // Display ISO timestamps compactly. For hour-bucketed data we want
  // "MM-DD HH:00"; for day-bucketed data "MM-DD".
  if (!iso) return '';
  const datePart = iso.slice(5, 10).replace('-', '/');
  const hour = iso.slice(11, 13);
  return hour === '00' ? datePart : `${datePart} ${hour}:00`;
}

function fmtPct(x) {
  if (x == null) return '—';
  return `${(Number(x) * 100).toFixed(1)}%`;
}

function fmtScore(x) {
  if (x == null) return '—';
  return Number(x).toFixed(4);
}

// Hover text combining pack name + raw policy id, so a reader sees both.
function packLabel(row) {
  if (row.pack_name) return row.pack_name;
  return row.policy_set_id || '—';
}
function packTitle(row) {
  if (row.pack_name && row.policy_set_id) {
    return `${row.pack_name} (${row.policy_set_id})`;
  }
  return row.policy_set_id || '';
}

// Flatten { buckets: [{t, counts:{Allow:12, Hold:1}}] } into the
// shape recharts expects: [{t, Allow:12, Hold:1, ...}]. Series list is
// derived from the union of all keys so chart layout adapts to the
// decisions actually present in the window.
function flattenStacked(buckets) {
  if (!Array.isArray(buckets)) return { data: [], series: [] };
  const seriesSet = new Set();
  const data = buckets.map((b) => {
    const out = { t: b.t };
    Object.entries(b.counts || {}).forEach(([k, v]) => {
      out[k] = v;
      seriesSet.add(k);
    });
    return out;
  });
  return { data, series: Array.from(seriesSet) };
}

// ── 1. Decisions over time ──────────────────────────────────────────────────

function DecisionsOverTimePanel({ window }) {
  const { data, loading, error } = useApi(
    `/reporting/decisions-over-time?window=${window}`,
    [window],
  );
  if (loading) return <LoadingState />;
  if (error)   return <ErrorState error={error} />;
  const { data: flat, series } = flattenStacked(data?.buckets);
  if (flat.length === 0) return <EmptyState />;
  return (
    <ResponsiveContainer width="100%" height={240}>
      <AreaChart data={flat}>
        <CartesianGrid strokeDasharray="3 3" stroke="#1f2937" />
        <XAxis dataKey="t" tickFormatter={shortTime} stroke="#6b7280" fontSize={10} />
        <YAxis allowDecimals={false} stroke="#6b7280" fontSize={10} />
        <Tooltip
          contentStyle={{ background: '#111827', border: '1px solid #374151', fontSize: '12px' }}
          labelFormatter={shortTime}
        />
        <Legend wrapperStyle={{ fontSize: '11px' }} />
        {series.map((s) => (
          <Area
            key={s}
            type="monotone"
            dataKey={s}
            stackId="1"
            stroke={DECISION_COLORS[s] || '#6b7280'}
            fill={DECISION_COLORS[s] || '#6b7280'}
            fillOpacity={0.65}
          />
        ))}
      </AreaChart>
    </ResponsiveContainer>
  );
}

// ── 2. Score averages over time ─────────────────────────────────────────────

function ScoreAveragesPanel({ window }) {
  const { data, loading, error } = useApi(
    `/reporting/score-averages?window=${window}`,
    [window],
  );
  if (loading) return <LoadingState />;
  if (error)   return <ErrorState error={error} />;
  const buckets = data?.buckets || [];
  if (buckets.length === 0) return <EmptyState />;
  return (
    <ResponsiveContainer width="100%" height={240}>
      <LineChart data={buckets}>
        <CartesianGrid strokeDasharray="3 3" stroke="#1f2937" />
        <XAxis dataKey="t" tickFormatter={shortTime} stroke="#6b7280" fontSize={10} />
        <YAxis
          domain={[0, 1]} stroke="#6b7280" fontSize={10}
          tickFormatter={(v) => v.toFixed(2)}
        />
        <Tooltip
          contentStyle={{ background: '#111827', border: '1px solid #374151', fontSize: '12px' }}
          labelFormatter={shortTime}
          formatter={(v) => Number(v).toFixed(4)}
        />
        <Legend wrapperStyle={{ fontSize: '11px' }} />
        <Line type="monotone" dataKey="avg_s_base"      stroke={BACK_COLOR} dot={false} name="S_base" />
        <Line type="monotone" dataKey="avg_tis_current" stroke="#22d3ee"     dot={false} name="TIS_current" />
      </LineChart>
    </ResponsiveContainer>
  );
}

// ── 3. Non-allow trends ─────────────────────────────────────────────────────

function NonAllowTrendsPanel({ window }) {
  const { data, loading, error } = useApi(
    `/reporting/non-allow-trends?window=${window}`,
    [window],
  );
  if (loading) return <LoadingState />;
  if (error)   return <ErrorState error={error} />;
  const buckets = data?.buckets || [];
  if (buckets.length === 0) return <EmptyState />;
  return (
    <ResponsiveContainer width="100%" height={240}>
      <LineChart data={buckets}>
        <CartesianGrid strokeDasharray="3 3" stroke="#1f2937" />
        <XAxis dataKey="t" tickFormatter={shortTime} stroke="#6b7280" fontSize={10} />
        <YAxis allowDecimals={false} stroke="#6b7280" fontSize={10} />
        <Tooltip
          contentStyle={{ background: '#111827', border: '1px solid #374151', fontSize: '12px' }}
          labelFormatter={shortTime}
        />
        <Legend wrapperStyle={{ fontSize: '11px' }} />
        <Line type="monotone" dataKey="Hold"     stroke={DECISION_COLORS.Hold}     dot={false} />
        <Line type="monotone" dataKey="Escalate" stroke={DECISION_COLORS.Escalate} dot={false} />
        <Line type="monotone" dataKey="Stop"     stroke={DECISION_COLORS.Stop}     dot={false} />
      </LineChart>
    </ResponsiveContainer>
  );
}

// ── 4. Failed BACK dimensions ───────────────────────────────────────────────

function FailedBackPanel({ window }) {
  const { data, loading, error } = useApi(
    `/reporting/failed-back-dimensions?window=${window}`,
    [window],
  );
  if (loading) return <LoadingState />;
  if (error)   return <ErrorState error={error} />;
  const dims = data?.dims || [];
  const allZero = dims.every((d) => d.fail_count === 0 && d.evaluated === 0);
  if (allZero) return <EmptyState />;
  return (
    <ResponsiveContainer width="100%" height={240}>
      <BarChart data={dims} layout="vertical" margin={{ left: 20 }}>
        <CartesianGrid strokeDasharray="3 3" stroke="#1f2937" />
        <XAxis type="number" allowDecimals={false} stroke="#6b7280" fontSize={10} />
        <YAxis type="category" dataKey="dim" stroke="#6b7280" fontSize={11} width={30} />
        <Tooltip
          contentStyle={{ background: '#111827', border: '1px solid #374151', fontSize: '12px' }}
          formatter={(v, _, props) => {
            const row = props?.payload;
            return [`${v} fails / ${row?.evaluated ?? 0} evaluated`, 'gate result'];
          }}
        />
        <Bar dataKey="fail_count" fill={DECISION_COLORS.Stop}>
          {dims.map((d) => (
            <Cell key={d.dim} fill={d.fail_count > 0 ? DECISION_COLORS.Stop : '#374151'} />
          ))}
        </Bar>
      </BarChart>
    </ResponsiveContainer>
  );
}

// ── 5. Decisions by policy ──────────────────────────────────────────────────

function DecisionsByPolicyPanel({ window }) {
  const { data, loading, error } = useApi(
    `/reporting/decisions-by-policy?window=${window}`,
    [window],
  );
  if (loading) return <LoadingState />;
  if (error)   return <ErrorState error={error} />;
  const rows = data?.rows || [];
  if (rows.length === 0) return <EmptyState />;
  return (
    <div className="overflow-auto max-h-64">
      <table className="w-full text-xs">
        <thead className="text-left text-gray-500 uppercase tracking-wide">
          <tr className="border-b border-gray-800">
            <th className="pb-1 pr-2">Pack / Profile</th>
            <th className="pb-1 pr-2 text-right">Total</th>
            <th className="pb-1 pr-2 text-right">Allow</th>
            <th className="pb-1 pr-2 text-right">Hold</th>
            <th className="pb-1 pr-2 text-right">Escalate</th>
            <th className="pb-1 pr-2 text-right">Stop</th>
          </tr>
        </thead>
        <tbody className="font-mono">
          {rows.map((r) => (
            <tr key={r.policy_set_id} className="border-b border-gray-800/60">
              <td className="py-1 pr-2 truncate max-w-[14rem]" title={packTitle(r)}>
                <span className="text-gray-200">{packLabel(r)}</span>
                {r.pack_name && r.policy_set_id && (
                  <span className="text-[10px] text-gray-600 ml-1">
                    ({r.policy_set_id})
                  </span>
                )}
              </td>
              <td className="py-1 pr-2 text-right text-gray-100">{r.total}</td>
              <td className="py-1 pr-2 text-right text-green-400">{r.counts.Allow || 0}</td>
              <td className="py-1 pr-2 text-right text-amber-400">{r.counts.Hold || 0}</td>
              <td className="py-1 pr-2 text-right text-orange-400">{r.counts.Escalate || 0}</td>
              <td className="py-1 pr-2 text-right text-red-400">{r.counts.Stop || 0}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

// ── 6. Override rate by policy ──────────────────────────────────────────────

function OverrideRatePanel({ window }) {
  const { data, loading, error } = useApi(
    `/reporting/override-rate-by-policy?window=${window}`,
    [window],
  );
  if (loading) return <LoadingState />;
  if (error)   return <ErrorState error={error} />;
  const rows = data?.rows || [];
  if (rows.length === 0) return <EmptyState />;
  return (
    <div className="overflow-auto max-h-64">
      <table className="w-full text-xs">
        <thead className="text-left text-gray-500 uppercase tracking-wide">
          <tr className="border-b border-gray-800">
            <th className="pb-1 pr-2">Pack / Profile</th>
            <th className="pb-1 pr-2 text-right">Total</th>
            <th className="pb-1 pr-2 text-right">Overridden</th>
            <th className="pb-1 pr-2 text-right">Rate</th>
          </tr>
        </thead>
        <tbody className="font-mono">
          {rows.map((r) => (
            <tr key={r.policy_set_id} className="border-b border-gray-800/60">
              <td className="py-1 pr-2 truncate max-w-[14rem]" title={packTitle(r)}>
                <span className="text-gray-200">{packLabel(r)}</span>
              </td>
              <td className="py-1 pr-2 text-right text-gray-100">{r.total}</td>
              <td className="py-1 pr-2 text-right text-blue-300">{r.overridden}</td>
              <td className="py-1 pr-2 text-right text-amber-300">{fmtPct(r.rate)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

// ── 7. Top triggered rules ──────────────────────────────────────────────────

function TopRulesPanel({ window }) {
  const { data, loading, error } = useApi(
    `/reporting/top-rules?window=${window}&limit=10`,
    [window],
  );
  if (loading) return <LoadingState />;
  if (error)   return <ErrorState error={error} />;
  const rules = data?.rules || [];
  if (rules.length === 0) return <EmptyState />;
  return (
    <div className="overflow-auto max-h-64">
      <table className="w-full text-xs">
        <thead className="text-left text-gray-500 uppercase tracking-wide">
          <tr className="border-b border-gray-800">
            <th className="pb-1 pr-2">Rule</th>
            <th className="pb-1 pr-2 text-right">Fires</th>
            <th className="pb-1 pr-2">Top decision</th>
            <th className="pb-1 pr-2">Top pack</th>
          </tr>
        </thead>
        <tbody>
          {rules.map((r) => (
            <tr key={r.rule_id} className="border-b border-gray-800/60">
              <td className="py-1 pr-2 max-w-[14rem]" title={r.rule_id}>
                <div className="text-gray-200">{r.rule_name || r.rule_id}</div>
                {r.rule_name && r.rule_name !== r.rule_id && (
                  <div className="text-[10px] text-gray-600 font-mono">{r.rule_id}</div>
                )}
              </td>
              <td className="py-1 pr-2 text-right font-mono text-gray-100">{r.fires}</td>
              <td className="py-1 pr-2">
                <span
                  className="text-xs font-mono"
                  style={{ color: DECISION_COLORS[r.top_decision] || '#9ca3af' }}
                >
                  {r.top_decision || '—'}
                </span>
              </td>
              <td className="py-1 pr-2 text-gray-300 truncate max-w-[12rem]"
                  title={r.top_policy_set_id || ''}>
                {r.top_pack_name || r.top_policy_set_id || '—'}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

// ── 8. Recent override activity ─────────────────────────────────────────────

function OverrideActivityPanel({ window }) {
  const { data, loading, error } = useApi(
    `/reporting/override-activity?window=${window}&limit=50`,
    [window],
  );
  if (loading) return <LoadingState />;
  if (error)   return <ErrorState error={error} />;
  const events = data?.events || [];
  if (events.length === 0) return <EmptyState />;
  return (
    <div className="overflow-auto max-h-64">
      <table className="w-full text-xs">
        <thead className="text-left text-gray-500 uppercase tracking-wide">
          <tr className="border-b border-gray-800">
            <th className="pb-1 pr-2">When</th>
            <th className="pb-1 pr-2">Before → After</th>
            <th className="pb-1 pr-2">Actor</th>
            <th className="pb-1 pr-2">Pack</th>
            <th className="pb-1 pr-2">Reason</th>
          </tr>
        </thead>
        <tbody>
          {events.map((e) => (
            <tr key={`${e.certificate_id}-${e.override_at}`} className="border-b border-gray-800/60 align-top">
              <td className="py-1 pr-2 font-mono text-gray-400 whitespace-nowrap">
                {shortTime(e.override_at)}
              </td>
              <td className="py-1 pr-2 font-mono">
                <span style={{ color: DECISION_COLORS[e.original_decision] || '#9ca3af' }}>
                  {e.original_decision || '?'}
                </span>
                <span className="text-gray-600 mx-1">→</span>
                <span style={{ color: DECISION_COLORS[e.override_decision] || '#9ca3af' }}>
                  {e.override_decision || '?'}
                </span>
              </td>
              <td className="py-1 pr-2 text-gray-300">{e.override_actor || '—'}</td>
              <td className="py-1 pr-2 text-gray-300 truncate max-w-[10rem]"
                  title={e.policy_set_id || ''}>
                {e.pack_name || e.policy_set_id || '—'}
              </td>
              <td className="py-1 pr-2 text-gray-400 max-w-[18rem] truncate"
                  title={e.override_reason_text || ''}>
                {e.override_reason_text || '—'}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

// ── Top-level component ─────────────────────────────────────────────────────

export default function GovernanceReporting() {
  const [window, setWindow] = useState('7d');

  return (
    <div className="space-y-4">
      {/* Header bar with window selector + framing note */}
      <div className="flex items-center justify-between bg-gray-900 border border-gray-800 rounded-lg px-4 py-3">
        <div>
          <h2 className="text-base font-semibold text-white">
            Runtime governance reporting and trust telemetry
          </h2>
          <p className="text-[11px] text-gray-500 mt-0.5">
            How governance behaves over time. Aggregate, read-only views over the
            existing Trust Certificate + lifecycle archive. Distinct from Live
            (now), Audit (one decision), and Replay (cross-policy).
          </p>
        </div>
        <WindowSelector window={window} onChange={setWindow} />
      </div>

      {/* 2-column grid on lg+, 1-column on small */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        <PanelShell
          title="Decisions over time"
          subtitle="Stacked counts per decision outcome."
        >
          <DecisionsOverTimePanel window={window} />
        </PanelShell>

        <PanelShell
          title="Score averages over time"
          subtitle="S_base and TIS_current, bucket-averaged."
        >
          <ScoreAveragesPanel window={window} />
        </PanelShell>

        <PanelShell
          title="Non-allow trends"
          subtitle="Hold, Escalate, and Stop counts per bucket."
        >
          <NonAllowTrendsPanel window={window} />
        </PanelShell>

        <PanelShell
          title="Failed BACK dimensions"
          subtitle="Per-dimension gate failure counts. not_applicable rows are not counted."
        >
          <FailedBackPanel window={window} />
        </PanelShell>

        <PanelShell
          title="Decisions by policy / pack"
          subtitle="Decision distribution per active policy set."
        >
          <DecisionsByPolicyPanel window={window} />
        </PanelShell>

        <PanelShell
          title="Override rate by policy"
          subtitle="Of decisions made in this window, the share that were overridden."
        >
          <OverrideRatePanel window={window} />
        </PanelShell>

        <PanelShell
          title="Top triggered governance rules"
          subtitle="Most-fired rules with the decision and policy each is most associated with."
        >
          <TopRulesPanel window={window} />
        </PanelShell>

        <PanelShell
          title="Recent override activity"
          subtitle="Latest override events. Original TC is preserved; this is additive audit."
        >
          <OverrideActivityPanel window={window} />
        </PanelShell>
      </div>
    </div>
  );
}
