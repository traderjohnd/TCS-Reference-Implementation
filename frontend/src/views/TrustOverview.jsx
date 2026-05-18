import { usePolling } from '../hooks/useApi';
import MetricCard from '../components/MetricCard';
import {
  BarChart, Bar, XAxis, YAxis, Tooltip, ResponsiveContainer,
  PieChart, Pie, Cell, AreaChart, Area, Legend,
} from 'recharts';

const DECISION_COLORS = {
  Allow: '#22c55e', Observe: '#3b82f6', Hold: '#eab308',
  Escalate: '#f97316', Stop: '#ef4444',
};

const DIM_LABELS = { B: 'Boundedness', A: 'Attribution', C: 'Compliance', K: 'Known' };

export default function TrustOverview() {
  const { data: metrics, loading } = usePolling('/metrics/live', 5000);
  const { data: health } = usePolling('/health', 10000);
  const { data: drift } = usePolling('/dynamics/drift?window_hours=24', 15000);
  const { data: timeseries } = usePolling('/metrics/timeseries?window=1h&bucket=1m', 10000);
  const { data: gateFailures } = usePolling('/metrics/gate-failures?window=24h', 15000);

  if (loading || !metrics) {
    return <div className="text-gray-500">Loading metrics...</div>;
  }

  const dist = metrics.tis_distribution || {};
  const histogram = dist.histogram || {};
  const histData = [
    { zone: 'Stop (<0.55)', count: histogram.stop_zone || 0 },
    { zone: 'Review (0.55-0.85)', count: histogram.review_zone || 0 },
    { zone: 'Allow (>0.85)', count: histogram.allow_zone || 0 },
    { zone: 'Invalidated', count: histogram.invalidated || 0 },
  ];

  const decisions = metrics.decision_counts || {};
  const pieData = Object.entries(decisions)
    .filter(([, v]) => v > 0)
    .map(([name, value]) => ({ name, value }));

  const dimMeans = metrics.dimension_means || {};
  const dimData = Object.entries(dimMeans).map(([key, val]) => ({
    dim: DIM_LABELS[key] || key,
    score: Math.round(val * 100) / 100,
  }));

  const hasDriftAlert = drift?.signals?.some((s) => s.severity === 'alert' || s.severity === 'critical');

  return (
    <div className="space-y-6">
      {hasDriftAlert && (
        <div className="bg-yellow-900/30 border border-yellow-700 rounded-lg p-3 text-yellow-400 text-sm">
          Drift alert detected in one or more governance contexts. See Drift Monitoring for details.
        </div>
      )}

      <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-4">
        <MetricCard
          label="Total Evaluations"
          value={metrics.total_evaluations}
          subtitle={`${metrics.chain_count} chain(s)`}
          color="blue"
        />
        <MetricCard
          label="Governance Integrity"
          value={`${(metrics.governance_integrity_score * 100).toFixed(1)}%`}
          subtitle={health?.status === 'ok' ? 'System healthy' : 'Degraded'}
          color={metrics.governance_integrity_score > 0.9 ? 'green' : 'yellow'}
        />
        <MetricCard
          label="Gate Failure Rate"
          value={`${(metrics.gate_failure_rate * 100).toFixed(1)}%`}
          subtitle={metrics.dominant_failure_dimension ? `Primary: ${metrics.dominant_failure_dimension}` : 'No failures'}
          color={metrics.gate_failure_rate > 0.2 ? 'red' : 'green'}
        />
        <MetricCard
          label="Chain Integrity"
          value={metrics.chain_intact ? 'Verified' : 'BROKEN'}
          subtitle={`${metrics.total_certificates} TCs verified`}
          color={metrics.chain_intact ? 'green' : 'red'}
        />
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
        <div className="bg-gray-900 rounded-lg p-4 border border-gray-800">
          <h3 className="text-sm font-medium text-gray-400 mb-3">TIS Distribution</h3>
          <ResponsiveContainer width="100%" height={200}>
            <BarChart data={histData}>
              <XAxis dataKey="zone" tick={{ fill: '#9ca3af', fontSize: 11 }} />
              <YAxis tick={{ fill: '#9ca3af', fontSize: 11 }} />
              <Tooltip
                contentStyle={{ background: '#1f2937', border: '1px solid #374151', borderRadius: 8 }}
                labelStyle={{ color: '#fff' }}
              />
              <Bar dataKey="count" fill="#3b82f6" radius={[4, 4, 0, 0]} />
            </BarChart>
          </ResponsiveContainer>
        </div>

        <div className="bg-gray-900 rounded-lg p-4 border border-gray-800">
          <h3 className="text-sm font-medium text-gray-400 mb-3">Decision Distribution</h3>
          {pieData.length > 0 ? (
            <ResponsiveContainer width="100%" height={200}>
              <PieChart>
                <Pie data={pieData} cx="50%" cy="50%" innerRadius={50} outerRadius={80} dataKey="value" label={({ name, value }) => `${name}: ${value}`}>
                  {pieData.map((entry) => (
                    <Cell key={entry.name} fill={DECISION_COLORS[entry.name] || '#6b7280'} />
                  ))}
                </Pie>
                <Tooltip contentStyle={{ background: '#1f2937', border: '1px solid #374151', borderRadius: 8 }} />
              </PieChart>
            </ResponsiveContainer>
          ) : (
            <div className="h-[200px] flex items-center justify-center text-gray-600">No decisions yet</div>
          )}
        </div>
      </div>

      <div className="bg-gray-900 rounded-lg p-4 border border-gray-800">
        <h3 className="text-sm font-medium text-gray-400 mb-3">Dimension Score Averages</h3>
        <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
          {dimData.map(({ dim, score }) => (
            <div key={dim} className="text-center">
              <div className="text-xs text-gray-500 mb-1">{dim}</div>
              <div className="w-full bg-gray-800 rounded-full h-3">
                <div
                  className="h-3 rounded-full transition-all"
                  style={{
                    width: `${score * 100}%`,
                    backgroundColor: score >= 0.85 ? '#22c55e' : score >= 0.7 ? '#eab308' : '#ef4444',
                  }}
                />
              </div>
              <div className="text-sm font-mono text-gray-300 mt-1">{score.toFixed(4)}</div>
            </div>
          ))}
        </div>
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
        <div className="bg-gray-900 rounded-lg p-4 border border-gray-800">
          <h3 className="text-sm font-medium text-gray-400 mb-3">Decision Timeseries (1h window)</h3>
          {timeseries?.buckets?.length > 0 ? (
            <ResponsiveContainer width="100%" height={200}>
              <AreaChart data={timeseries.buckets}>
                <XAxis dataKey="timestamp" tick={{ fill: '#9ca3af', fontSize: 10 }} tickFormatter={(v) => v ? new Date(v).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }) : ''} />
                <YAxis tick={{ fill: '#9ca3af', fontSize: 11 }} />
                <Tooltip contentStyle={{ background: '#1f2937', border: '1px solid #374151', borderRadius: 8 }} labelStyle={{ color: '#fff' }} />
                <Legend wrapperStyle={{ fontSize: 11 }} />
                <Area type="monotone" dataKey="allow" stackId="1" stroke="#22c55e" fill="#22c55e" fillOpacity={0.6} />
                <Area type="monotone" dataKey="hold" stackId="1" stroke="#eab308" fill="#eab308" fillOpacity={0.6} />
                <Area type="monotone" dataKey="stop" stackId="1" stroke="#ef4444" fill="#ef4444" fillOpacity={0.6} />
              </AreaChart>
            </ResponsiveContainer>
          ) : (
            <div className="h-[200px] flex items-center justify-center text-gray-600">No timeseries data yet</div>
          )}
        </div>

        <div className="bg-gray-900 rounded-lg p-4 border border-gray-800">
          <h3 className="text-sm font-medium text-gray-400 mb-3">Gate Failure Breakdown (24h)</h3>
          {gateFailures?.dimensions && Object.keys(gateFailures.dimensions).length > 0 ? (
            <ResponsiveContainer width="100%" height={200}>
              <BarChart data={Object.entries(gateFailures.dimensions).map(([dim, count]) => ({ dim: DIM_LABELS[dim] || dim, count }))}>
                <XAxis dataKey="dim" tick={{ fill: '#9ca3af', fontSize: 11 }} />
                <YAxis tick={{ fill: '#9ca3af', fontSize: 11 }} />
                <Tooltip contentStyle={{ background: '#1f2937', border: '1px solid #374151', borderRadius: 8 }} labelStyle={{ color: '#fff' }} />
                <Bar dataKey="count" fill="#f97316" radius={[4, 4, 0, 0]} />
              </BarChart>
            </ResponsiveContainer>
          ) : (
            <div className="h-[200px] flex items-center justify-center text-gray-600">No gate failures recorded</div>
          )}
        </div>
      </div>

      <div className="bg-gray-900 rounded-lg p-4 border border-gray-800">
        <h3 className="text-sm font-medium text-gray-400 mb-3">Governance Latency Percentiles</h3>
        <div className="grid grid-cols-3 gap-4 text-center">
          <div>
            <div className="text-xs text-gray-500 mb-1">p50</div>
            <div className="text-lg font-mono text-gray-500">--</div>
          </div>
          <div>
            <div className="text-xs text-gray-500 mb-1">p95</div>
            <div className="text-lg font-mono text-gray-500">--</div>
          </div>
          <div>
            <div className="text-xs text-gray-500 mb-1">p99</div>
            <div className="text-lg font-mono text-gray-500">--</div>
          </div>
        </div>
        <p className="text-xs text-gray-600 text-center mt-2">Latency metrics available when demo is running</p>
      </div>
    </div>
  );
}
