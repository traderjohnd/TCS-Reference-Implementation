import { useState } from 'react';
import { usePolling } from '../hooks/useApi';
import {
  LineChart, Line, AreaChart, Area, BarChart, Bar,
  ScatterChart, Scatter,
  XAxis, YAxis, Tooltip, ResponsiveContainer, Legend,
  ReferenceLine, CartesianGrid,
} from 'recharts';

const DIM_COLORS = {
  B: '#3b82f6', // blue - Boundedness
  A: '#8b5cf6', // purple - Attribution
  C: '#f59e0b', // amber - Compliance
  K: '#10b981', // emerald - Known (calibration)
};

const DECISION_COLORS = {
  Allow: '#22c55e', Hold: '#eab308', Stop: '#ef4444',
  Escalate: '#f97316', Observe: '#3b82f6',
};

const PENALTY_COLORS = {
  P_cb: '#ef4444', P_d: '#f97316', P_n: '#eab308',
  P_h: '#a855f7', P_ps: '#ec4899',
};

function fmt(v) {
  return v ? new Date(v).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' }) : '';
}

function KCalibrationCard({ summary }) {
  const k = summary?.k_calibration || {};
  const belowThreshold = k.below_threshold || 0;
  const calibratedPct = ((k.calibrated_pct || 0) * 100).toFixed(0);

  const statusColor = belowThreshold === 0 ? 'text-green-400' :
    belowThreshold <= 2 ? 'text-yellow-400' : 'text-red-400';
  const statusLabel = belowThreshold === 0 ? 'Calibrated' :
    belowThreshold <= 2 ? 'Attention' : 'Uncalibrated';

  return (
    <div className="bg-gray-900 rounded-lg border border-gray-800 p-4">
      <div className="flex items-center justify-between mb-3">
        <h3 className="text-sm font-medium text-gray-400">K (Known) Calibration Status</h3>
        <span className={`text-xs font-bold ${statusColor} bg-gray-800 px-2 py-1 rounded`}>
          {statusLabel}
        </span>
      </div>
      <div className="grid grid-cols-4 gap-3 text-center">
        <div>
          <div className="text-xs text-gray-500">Mean K</div>
          <div className="text-lg font-mono text-emerald-400">{(k.mean || 0).toFixed(4)}</div>
        </div>
        <div>
          <div className="text-xs text-gray-500">Min / Max</div>
          <div className="text-sm font-mono text-gray-300">
            {(k.min || 0).toFixed(2)} / {(k.max || 0).toFixed(2)}
          </div>
        </div>
        <div>
          <div className="text-xs text-gray-500">Calibrated</div>
          <div className="text-lg font-mono text-gray-300">{calibratedPct}%</div>
        </div>
        <div>
          <div className="text-xs text-gray-500">Below 0.80</div>
          <div className={`text-lg font-mono ${belowThreshold > 0 ? 'text-red-400' : 'text-green-400'}`}>
            {belowThreshold}
          </div>
        </div>
      </div>
      <p className="text-xs text-gray-600 mt-3">
        K measures whether expressed confidence is justified by evidence quality,
        recency, relevance, and source adequacy. Below 0.80 = unsupported confidence.
      </p>
    </div>
  );
}

function TISSparkline({ records }) {
  if (!records?.length) return <div className="h-[200px] flex items-center justify-center text-gray-600">No data</div>;

  return (
    <ResponsiveContainer width="100%" height={200}>
      <AreaChart data={records}>
        <defs>
          <linearGradient id="tisGrad" x1="0" y1="0" x2="0" y2="1">
            <stop offset="5%" stopColor="#3b82f6" stopOpacity={0.4} />
            <stop offset="95%" stopColor="#3b82f6" stopOpacity={0.05} />
          </linearGradient>
        </defs>
        <CartesianGrid strokeDasharray="3 3" stroke="#1f2937" />
        <XAxis dataKey="t" tick={{ fill: '#6b7280', fontSize: 10 }} tickFormatter={fmt} />
        <YAxis domain={[0, 1]} tick={{ fill: '#6b7280', fontSize: 10 }} />
        <Tooltip
          contentStyle={{ background: '#111827', border: '1px solid #374151', borderRadius: 8, fontSize: 12 }}
          labelFormatter={fmt}
          formatter={(v, name) => [v?.toFixed(4), name]}
        />
        <ReferenceLine y={0.85} stroke="#22c55e" strokeDasharray="3 3" label={{ value: 'Allow', fill: '#22c55e', fontSize: 10 }} />
        <ReferenceLine y={0.70} stroke="#eab308" strokeDasharray="3 3" label={{ value: 'Hold', fill: '#eab308', fontSize: 10 }} />
        <Area type="monotone" dataKey="tis_current" stroke="#3b82f6" fill="url(#tisGrad)" strokeWidth={2} dot={false} name="TIS Current" />
        <Line type="monotone" dataKey="s_base" stroke="#9ca3af" strokeWidth={1} strokeDasharray="4 2" dot={false} name="S_base (pre-gate)" />
      </AreaChart>
    </ResponsiveContainer>
  );
}

function DimensionTrends({ records }) {
  if (!records?.length) return <div className="h-[220px] flex items-center justify-center text-gray-600">No data</div>;

  return (
    <ResponsiveContainer width="100%" height={220}>
      <LineChart data={records}>
        <CartesianGrid strokeDasharray="3 3" stroke="#1f2937" />
        <XAxis dataKey="t" tick={{ fill: '#6b7280', fontSize: 10 }} tickFormatter={fmt} />
        <YAxis domain={[0, 1]} tick={{ fill: '#6b7280', fontSize: 10 }} />
        <Tooltip
          contentStyle={{ background: '#111827', border: '1px solid #374151', borderRadius: 8, fontSize: 12 }}
          labelFormatter={fmt}
          formatter={(v, name) => [v?.toFixed(4), name]}
        />
        <Legend content={() => (
          <div style={{ display: 'flex', justifyContent: 'center', gap: 16, fontSize: 11, marginTop: 4 }}>
            {[
              { label: 'B (Bounded)', color: DIM_COLORS.B },
              { label: 'A (Attribution)', color: DIM_COLORS.A },
              { label: 'C (Compliance)', color: DIM_COLORS.C },
              { label: 'K (Known)', color: DIM_COLORS.K },
            ].map(({ label, color }) => (
              <span key={label} style={{ display: 'flex', alignItems: 'center', gap: 4, color: '#9ca3af' }}>
                <span style={{ width: 14, height: 3, backgroundColor: color, display: 'inline-block', borderRadius: 2 }} />
                <span style={{ color }}>{label}</span>
              </span>
            ))}
          </div>
        )} />
        <ReferenceLine y={0.80} stroke="#374151" strokeDasharray="2 4" />
        <Line type="monotone" dataKey="B" stroke={DIM_COLORS.B} strokeWidth={2} dot={false} name="B (Bounded)" />
        <Line type="monotone" dataKey="A" stroke={DIM_COLORS.A} strokeWidth={2} dot={false} name="A (Attribution)" />
        <Line type="monotone" dataKey="C" stroke={DIM_COLORS.C} strokeWidth={2} dot={false} name="C (Compliance)" />
        <Line type="monotone" dataKey="K" stroke={DIM_COLORS.K} strokeWidth={2} dot={false} name="K (Known)" />
      </LineChart>
    </ResponsiveContainer>
  );
}

function KCalibrationTimeline({ records }) {
  if (!records?.length) return <div className="h-[180px] flex items-center justify-center text-gray-600">No data</div>;

  // Color-code each point by calibration status
  const enriched = records.map(r => ({
    ...r,
    k_status: r.K >= 0.85 ? 'calibrated' : r.K >= 0.70 ? 'marginal' : 'uncalibrated',
    k_fill: r.K >= 0.85 ? '#22c55e' : r.K >= 0.70 ? '#eab308' : '#ef4444',
  }));

  return (
    <ResponsiveContainer width="100%" height={180}>
      <ScatterChart>
        <CartesianGrid strokeDasharray="3 3" stroke="#1f2937" />
        <XAxis dataKey="t" tick={{ fill: '#6b7280', fontSize: 10 }} tickFormatter={fmt} name="Time" />
        <YAxis domain={[0, 1]} tick={{ fill: '#6b7280', fontSize: 10 }} name="K Score" />
        <Tooltip
          contentStyle={{ background: '#111827', border: '1px solid #374151', borderRadius: 8, fontSize: 12 }}
          formatter={(v, name) => {
            if (name === 'K') return [v?.toFixed(4), 'K (Known)'];
            return [v, name];
          }}
          labelFormatter={fmt}
        />
        <ReferenceLine y={0.80} stroke="#ef4444" strokeDasharray="3 3" label={{ value: 'Gate', fill: '#ef4444', fontSize: 10 }} />
        <Scatter data={enriched} dataKey="K" fill="#10b981" name="K" >
          {enriched.map((entry, i) => (
            <circle key={i} r={4} fill={entry.k_fill} />
          ))}
        </Scatter>
      </ScatterChart>
    </ResponsiveContainer>
  );
}

function PenaltyPressure({ records }) {
  if (!records?.length) return <div className="h-[200px] flex items-center justify-center text-gray-600">No data</div>;

  return (
    <ResponsiveContainer width="100%" height={200}>
      <AreaChart data={records}>
        <CartesianGrid strokeDasharray="3 3" stroke="#1f2937" />
        <XAxis dataKey="t" tick={{ fill: '#6b7280', fontSize: 10 }} tickFormatter={fmt} />
        <YAxis domain={[0, 'auto']} tick={{ fill: '#6b7280', fontSize: 10 }} />
        <Tooltip
          contentStyle={{ background: '#111827', border: '1px solid #374151', borderRadius: 8, fontSize: 12 }}
          labelFormatter={fmt}
          formatter={(v, name) => [v?.toFixed(4), name]}
        />
        <Legend wrapperStyle={{ fontSize: 11 }} />
        <Area type="monotone" dataKey="P_cb" stackId="1" stroke={PENALTY_COLORS.P_cb} fill={PENALTY_COLORS.P_cb} fillOpacity={0.6} name="Cross-Boundary" />
        <Area type="monotone" dataKey="P_d" stackId="1" stroke={PENALTY_COLORS.P_d} fill={PENALTY_COLORS.P_d} fillOpacity={0.6} name="Staleness" />
        <Area type="monotone" dataKey="P_n" stackId="1" stroke={PENALTY_COLORS.P_n} fill={PENALTY_COLORS.P_n} fillOpacity={0.6} name="Novelty" />
        <Area type="monotone" dataKey="P_h" stackId="1" stroke={PENALTY_COLORS.P_h} fill={PENALTY_COLORS.P_h} fillOpacity={0.6} name="Review Lag" />
        <Area type="monotone" dataKey="P_ps" stackId="1" stroke={PENALTY_COLORS.P_ps} fill={PENALTY_COLORS.P_ps} fillOpacity={0.6} name="Policy-Sensitive" />
      </AreaChart>
    </ResponsiveContainer>
  );
}

function DecisionScatter({ records }) {
  if (!records?.length) return <div className="h-[200px] flex items-center justify-center text-gray-600">No data</div>;

  // Group by decision for separate series
  const byDecision = {};
  records.forEach(r => {
    if (!byDecision[r.decision]) byDecision[r.decision] = [];
    byDecision[r.decision].push(r);
  });

  return (
    <ResponsiveContainer width="100%" height={200}>
      <ScatterChart>
        <CartesianGrid strokeDasharray="3 3" stroke="#1f2937" />
        <XAxis dataKey="s_base" domain={[0, 1]} tick={{ fill: '#6b7280', fontSize: 10 }} name="S_base" label={{ value: 'S_base (pre-gate)', fill: '#6b7280', fontSize: 10, position: 'insideBottom', offset: -5 }} />
        <YAxis dataKey="tis_current" domain={[0, 1]} tick={{ fill: '#6b7280', fontSize: 10 }} name="TIS Current" label={{ value: 'TIS Current', fill: '#6b7280', fontSize: 10, angle: -90, position: 'insideLeft' }} />
        <Tooltip
          contentStyle={{ background: '#111827', border: '1px solid #374151', borderRadius: 8, fontSize: 12 }}
          formatter={(v, name) => [typeof v === 'number' ? v.toFixed(4) : v, name]}
        />
        <ReferenceLine y={0.85} stroke="#22c55e" strokeDasharray="3 3" />
        <ReferenceLine y={0.70} stroke="#eab308" strokeDasharray="3 3" />
        {Object.entries(byDecision).map(([dec, data]) => (
          <Scatter key={dec} data={data} fill={DECISION_COLORS[dec] || '#6b7280'} name={dec} />
        ))}
        <Legend wrapperStyle={{ fontSize: 11 }} />
      </ScatterChart>
    </ResponsiveContainer>
  );
}

export default function Telemetry() {
  const [window, setWindow] = useState('1h');
  const { data, loading } = usePolling(`/metrics/telemetry?window=${window}&limit=100`, 3000);

  const records = data?.records || [];
  const summary = data?.summary || {};

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <h2 className="text-lg font-semibold text-white">Real-Time Telemetry</h2>
          <p className="text-xs text-gray-500 mt-1">
            Per-evaluation dimension scores, K calibration, and penalty pressure.
            Polling every 3 seconds.
          </p>
        </div>
        <div className="flex gap-2">
          {['15m', '30m', '1h', '6h', '24h'].map(w => (
            <button
              key={w}
              onClick={() => setWindow(w)}
              className={`text-xs px-3 py-1 rounded ${window === w
                ? 'bg-blue-700 text-white'
                : 'bg-gray-800 text-gray-400 hover:text-white'}`}
            >
              {w}
            </button>
          ))}
        </div>
      </div>

      {loading && !records.length && (
        <div className="text-gray-500 text-sm">Loading telemetry...</div>
      )}

      {/* K Calibration Status Card */}
      <KCalibrationCard summary={summary} />

      {/* TIS Timeline + Dimension Trends side by side */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
        <div className="bg-gray-900 rounded-lg p-4 border border-gray-800">
          <h3 className="text-sm font-medium text-gray-400 mb-3">TIS Score Timeline</h3>
          <p className="text-xs text-gray-600 mb-2">
            TIS_current (blue) vs S_base (gray dashed, gate-independent composite). Reference lines at Allow (0.85) and Hold (0.70) thresholds.
          </p>
          <TISSparkline records={records} />
        </div>

        <div className="bg-gray-900 rounded-lg p-4 border border-gray-800">
          <h3 className="text-sm font-medium text-gray-400 mb-3">BACK Dimension Trends</h3>
          <p className="text-xs text-gray-600 mb-2">
            Per-evaluation scores for Boundedness, Attribution, Compliance, and Known.
          </p>
          <DimensionTrends records={records} />
        </div>
      </div>

      {/* K Calibration scatter + Penalty Pressure */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
        <div className="bg-gray-900 rounded-lg p-4 border border-gray-800">
          <h3 className="text-sm font-medium text-gray-400 mb-3">K Calibration Timeline</h3>
          <p className="text-xs text-gray-600 mb-2">
            Each dot is one evaluation. Green = calibrated (&ge;0.85), yellow = marginal (&ge;0.70), red = uncalibrated.
            Red line = gate threshold (0.80).
          </p>
          <KCalibrationTimeline records={records} />
        </div>

        <div className="bg-gray-900 rounded-lg p-4 border border-gray-800">
          <h3 className="text-sm font-medium text-gray-400 mb-3">Penalty Pressure Stacked</h3>
          <p className="text-xs text-gray-600 mb-2">
            Five penalty components over time. Rising pressure signals degrading evidence quality or compliance drift.
          </p>
          <PenaltyPressure records={records} />
        </div>
      </div>

      {/* Decision scatter plot */}
      <div className="bg-gray-900 rounded-lg p-4 border border-gray-800">
        <h3 className="text-sm font-medium text-gray-400 mb-3">Decision Phase Space</h3>
        <p className="text-xs text-gray-600 mb-2">
          S_base (x-axis, pre-gate composite) vs TIS_current (y-axis). Each point colored by governance decision.
          Points at y=0 indicate gate failure or invalidation (TIS collapsed to zero).
        </p>
        <DecisionScatter records={records} />
      </div>

      {/* Raw telemetry table */}
      <div className="bg-gray-900 rounded-lg border border-gray-800 p-4">
        <h3 className="text-sm font-medium text-gray-400 mb-3">
          Recent Evaluations ({records.length})
        </h3>
        <div className="overflow-x-auto">
          <table className="w-full text-xs">
            <thead>
              <tr className="text-left text-gray-500 border-b border-gray-800">
                <th className="pb-2 pr-2">Time</th>
                <th className="pb-2 pr-2">Decision</th>
                <th className="pb-2 pr-2">Subject</th>
                <th className="pb-2 pr-2">TIS</th>
                <th className="pb-2 pr-2">B</th>
                <th className="pb-2 pr-2">A</th>
                <th className="pb-2 pr-2">C</th>
                <th className="pb-2 pr-2">K</th>
                <th className="pb-2 pr-2">Penalty</th>
                <th className="pb-2 pr-2">Gate</th>
              </tr>
            </thead>
            <tbody>
              {records.slice().reverse().map((r) => (
                <tr key={r.certificate_id} className={`border-b border-gray-800/50 ${
                  r.decision === 'Allow' ? 'bg-green-900/5' :
                  r.decision === 'Stop' ? 'bg-red-900/10' :
                  r.decision === 'Hold' ? 'bg-yellow-900/5' : ''
                }`}>
                  <td className="py-1.5 pr-2 text-gray-500 font-mono">{fmt(r.t)}</td>
                  <td className="py-1.5 pr-2">
                    <span className={`px-1.5 py-0.5 rounded text-[10px] font-bold ${
                      r.decision === 'Allow' ? 'bg-green-900/50 text-green-400' :
                      r.decision === 'Stop' ? 'bg-red-900/50 text-red-400' :
                      r.decision === 'Hold' ? 'bg-yellow-900/50 text-yellow-400' :
                      r.decision === 'Escalate' ? 'bg-orange-900/50 text-orange-400' :
                      'bg-blue-900/50 text-blue-400'
                    }`}>
                      {r.decision}
                    </span>
                  </td>
                  <td className="py-1.5 pr-2 text-gray-400 font-mono truncate max-w-[120px]">{r.subject_id}</td>
                  <td className="py-1.5 pr-2 font-mono text-gray-300">{r.tis_current?.toFixed(4)}</td>
                  <td className={`py-1.5 pr-2 font-mono ${r.B >= 0.80 ? 'text-gray-300' : 'text-red-400'}`}>{r.B?.toFixed(2)}</td>
                  <td className={`py-1.5 pr-2 font-mono ${r.A >= 0.80 ? 'text-gray-300' : 'text-red-400'}`}>{r.A?.toFixed(2)}</td>
                  <td className={`py-1.5 pr-2 font-mono ${r.C >= 0.80 ? 'text-gray-300' : 'text-red-400'}`}>{r.C?.toFixed(2)}</td>
                  <td className={`py-1.5 pr-2 font-mono font-bold ${r.K >= 0.85 ? 'text-emerald-400' : r.K >= 0.70 ? 'text-yellow-400' : 'text-red-400'}`}>
                    {r.K?.toFixed(2)}
                  </td>
                  <td className={`py-1.5 pr-2 font-mono ${r.penalty_aggregate > 0.01 ? 'text-yellow-400' : 'text-gray-500'}`}>
                    {r.penalty_aggregate?.toFixed(4)}
                  </td>
                  <td className="py-1.5 pr-2">
                    {r.gate_passed
                      ? <span className="text-green-500">PASS</span>
                      : <span className="text-red-400 font-bold">FAIL</span>
                    }
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  );
}
