import { usePolling } from '../hooks/useApi';
import MetricCard from '../components/MetricCard';
import {
  BarChart, Bar, XAxis, YAxis, Tooltip, ResponsiveContainer,
} from 'recharts';

export default function EconomicView() {
  const { data: summary, loading } = usePolling('/metrics/summary', 5000);
  const { data: metrics } = usePolling('/metrics/live', 5000);

  if (loading || !summary) {
    return <div className="text-gray-500">Loading economic metrics...</div>;
  }

  const dist = metrics?.tis_distribution || {};
  const allowedTis = dist.mean || 0;
  const cap = 1.0;
  const cAllowed = (allowedTis * cap).toFixed(4);

  const decisionData = summary.decision_counts
    ? Object.entries(summary.decision_counts).map(([name, value]) => ({ name, value }))
    : [];

  return (
    <div className="space-y-6">
      <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-4">
        <MetricCard
          label="Automation Rate"
          value={`${(summary.automation_rate * 100).toFixed(1)}%`}
          subtitle={`${summary.allow_count} auto-approved of ${summary.total_evaluations}`}
          color="green"
        />
        <MetricCard
          label="Review Hours Saved"
          value={Math.round(summary.allow_count * 0.25)}
          subtitle="@ 15 min per manual review"
          color="blue"
        />
        <MetricCard
          label="Stop Decisions"
          value={summary.stop_count}
          subtitle="Potentially harmful outputs blocked"
          color="red"
        />
        <MetricCard
          label="Liability Events Prevented"
          value={summary.stop_count}
          subtitle="High-risk outputs intercepted"
          color="purple"
        />
      </div>

      <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
        <MetricCard
          label="C_allowed (TIS x Cap)"
          value={cAllowed}
          subtitle="Mean trust capacity utilized"
          color="blue"
        />
        <MetricCard
          label="Hold Queue Depth"
          value={summary.hold_queue_depth}
          subtitle="Decisions awaiting review"
          color="yellow"
        />
        <MetricCard
          label="Escalation Rate"
          value={summary.escalate_count > 0 && summary.total_evaluations > 0
            ? `${((summary.escalate_count / summary.total_evaluations) * 100).toFixed(1)}%`
            : '0.0%'}
          subtitle={`${summary.escalate_count} escalations`}
          color="purple"
        />
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
        <div className="bg-gray-900 rounded-lg border border-gray-800 p-4">
          <h3 className="text-sm font-medium text-gray-400 mb-3">Decision Volume</h3>
          <ResponsiveContainer width="100%" height={200}>
            <BarChart data={decisionData}>
              <XAxis dataKey="name" tick={{ fill: '#9ca3af', fontSize: 11 }} />
              <YAxis tick={{ fill: '#9ca3af', fontSize: 11 }} />
              <Tooltip contentStyle={{ background: '#1f2937', border: '1px solid #374151', borderRadius: 8 }} />
              <Bar dataKey="value" radius={[4, 4, 0, 0]}>
                {decisionData.map((entry) => {
                  const colors = { Allow: '#22c55e', Observe: '#3b82f6', Hold: '#eab308', Escalate: '#f97316', Stop: '#ef4444' };
                  return <Bar key={entry.name} fill={colors[entry.name] || '#6b7280'} />;
                })}
              </Bar>
            </BarChart>
          </ResponsiveContainer>
        </div>

        <div className="bg-gray-900 rounded-lg border border-gray-800 p-4">
          <h3 className="text-sm font-medium text-gray-400 mb-3">Governance ROI Summary</h3>
          <div className="space-y-3">
            <div className="flex justify-between text-sm border-b border-gray-800 pb-2">
              <span className="text-gray-500">Total evaluations processed</span>
              <span className="text-gray-300 font-mono">{summary.total_evaluations}</span>
            </div>
            <div className="flex justify-between text-sm border-b border-gray-800 pb-2">
              <span className="text-gray-500">Governance integrity score</span>
              <span className="text-gray-300 font-mono">{(summary.governance_integrity_score * 100).toFixed(1)}%</span>
            </div>
            <div className="flex justify-between text-sm border-b border-gray-800 pb-2">
              <span className="text-gray-500">Gate failure rate</span>
              <span className="text-gray-300 font-mono">{(summary.gate_failure_rate * 100).toFixed(1)}%</span>
            </div>
            <div className="flex justify-between text-sm border-b border-gray-800 pb-2">
              <span className="text-gray-500">Mean TIS</span>
              <span className="text-gray-300 font-mono">{summary.mean_tis?.toFixed(4)}</span>
            </div>
            <div className="flex justify-between text-sm">
              <span className="text-gray-500">Review decisions pending</span>
              <span className="text-gray-300 font-mono">{summary.review_count}</span>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
