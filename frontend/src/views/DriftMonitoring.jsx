import { usePolling, useApi } from '../hooks/useApi';
import { apiPost } from '../hooks/useApi';
import { useState } from 'react';
import {
  BarChart, Bar, XAxis, YAxis, Tooltip, ResponsiveContainer, CartesianGrid,
} from 'recharts';

const SEVERITY_COLORS = {
  nominal: 'text-green-400',
  warn: 'text-yellow-400',
  alert: 'text-orange-400',
  critical: 'text-red-400',
};

export default function DriftMonitoring() {
  const { data: drift } = usePolling('/dynamics/drift?window_hours=48', 10000);
  const { data: trustLoss } = usePolling('/dynamics/trust-loss?domain=financial_services&window_hours=48', 15000);
  const { data: pllRecs } = usePolling('/dynamics/pll/recommendations', 10000);
  const [approving, setApproving] = useState(null);

  const signals = drift?.signals || [];
  const loss = trustLoss || {};
  const recs = pllRecs?.recommendations || [];

  const lossComponents = loss.components
    ? Object.entries(loss.components).map(([name, val]) => ({ name, value: Math.round(val * 10000) / 10000 }))
    : [];

  const handleApprove = async (recordId) => {
    setApproving(recordId);
    try {
      await apiPost(`/dynamics/pll/approve/${recordId}`, {});
    } catch { /* ignore */ }
    setApproving(null);
  };

  const handleReject = async (recordId) => {
    setApproving(recordId);
    try {
      await apiPost(`/dynamics/pll/reject/${recordId}`, {});
    } catch { /* ignore */ }
    setApproving(null);
  };

  return (
    <div className="space-y-6">
      <div className="bg-gray-900 rounded-lg border border-gray-800 p-4">
        <h3 className="text-sm font-medium text-gray-400 mb-3">Drift Signals by Context</h3>
        {signals.length === 0 ? (
          <p className="text-gray-600 text-sm">No drift signals detected. Insufficient data or all contexts nominal.</p>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="text-left text-gray-500 border-b border-gray-800">
                  <th className="pb-2 pr-3">Context</th>
                  <th className="pb-2 pr-3">D_trust</th>
                  <th className="pb-2 pr-3">Severity</th>
                  <th className="pb-2 pr-3">Trend</th>
                  <th className="pb-2 pr-3">Evaluations</th>
                  <th className="pb-2">Recommendations</th>
                </tr>
              </thead>
              <tbody>
                {signals.map((s, i) => (
                  <tr key={i} className="border-b border-gray-800/50">
                    <td className="py-2 pr-3 font-mono text-xs text-gray-300">{s.context_id}</td>
                    <td className="py-2 pr-3 font-mono text-xs">{s.d_trust?.toFixed(4)}</td>
                    <td className={`py-2 pr-3 text-xs font-medium ${SEVERITY_COLORS[s.severity] || ''}`}>
                      {s.severity}
                    </td>
                    <td className="py-2 pr-3 text-xs text-gray-400">{s.trend}</td>
                    <td className="py-2 pr-3 text-xs text-gray-500">{s.total_evaluations}</td>
                    <td className="py-2 text-xs text-gray-500">{(s.recommendations || []).join(', ') || 'none'}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
        <div className="bg-gray-900 rounded-lg border border-gray-800 p-4">
          <h3 className="text-sm font-medium text-gray-400 mb-3">Trust Loss Components (L_trust)</h3>
          {lossComponents.length > 0 ? (
            <div>
              <div className="text-lg font-bold text-white mb-2">
                L_trust = {loss.L_trust?.toFixed(4) || 'N/A'}
              </div>
              <ResponsiveContainer width="100%" height={200}>
                <BarChart data={lossComponents} layout="vertical">
                  <CartesianGrid strokeDasharray="3 3" stroke="#374151" />
                  <XAxis type="number" tick={{ fill: '#9ca3af', fontSize: 11 }} domain={[0, 'auto']} />
                  <YAxis type="category" dataKey="name" tick={{ fill: '#9ca3af', fontSize: 11 }} width={100} />
                  <Tooltip contentStyle={{ background: '#1f2937', border: '1px solid #374151', borderRadius: 8 }} />
                  <Bar dataKey="value" fill="#8b5cf6" radius={[0, 4, 4, 0]} />
                </BarChart>
              </ResponsiveContainer>
            </div>
          ) : (
            <p className="text-gray-600 text-sm">Insufficient data to compute trust loss.</p>
          )}
        </div>

        <div className="bg-gray-900 rounded-lg border border-gray-800 p-4">
          <h3 className="text-sm font-medium text-gray-400 mb-3">PLL Recommendations</h3>
          {recs.length === 0 ? (
            <p className="text-gray-600 text-sm">No pending recommendations.</p>
          ) : (
            <div className="space-y-3">
              {recs.map((r) => (
                <div key={r.record_id} className="bg-gray-800/50 rounded p-3 border border-gray-700">
                  <div className="flex justify-between items-start">
                    <div>
                      <span className="text-xs text-blue-400 font-mono">{r.record_id}</span>
                      <span className="ml-2 text-xs text-gray-500">{r.status}</span>
                    </div>
                    {r.status === 'pending' && (
                      <div className="flex gap-2">
                        <button
                          onClick={() => handleApprove(r.record_id)}
                          disabled={approving === r.record_id}
                          className="text-xs bg-green-700 hover:bg-green-600 text-white px-2 py-1 rounded"
                        >
                          Approve
                        </button>
                        <button
                          onClick={() => handleReject(r.record_id)}
                          disabled={approving === r.record_id}
                          className="text-xs bg-red-700 hover:bg-red-600 text-white px-2 py-1 rounded"
                        >
                          Reject
                        </button>
                      </div>
                    )}
                  </div>
                  {r.parameter_changes && (
                    <pre className="text-xs text-gray-400 font-mono mt-2 overflow-x-auto">
                      {JSON.stringify(r.parameter_changes, null, 2)}
                    </pre>
                  )}
                </div>
              ))}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
