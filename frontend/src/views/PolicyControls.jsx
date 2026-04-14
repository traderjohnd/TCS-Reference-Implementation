import { useState } from 'react';
import { useApi, usePolling, apiPost } from '../hooks/useApi';
import { useAuth } from '../hooks/useAuth';

export default function PolicyControls() {
  const { hasEditAccess } = useAuth();
  const { data: activePack } = usePolling('/packs/active', 10000);
  const { data: packs } = useApi('/packs');
  const { data: pllHistory } = useApi('/dynamics/pll/history');
  const [simResult, setSimResult] = useState(null);
  const [simLoading, setSimLoading] = useState(false);
  const [simProfile, setSimProfile] = useState('');

  const packList = packs || [];
  const history = pllHistory?.adaptations || [];
  const active = activePack?.active ? activePack : null;

  const runSimulation = async () => {
    if (!simProfile) return;
    setSimLoading(true);
    setSimResult(null);
    try {
      const result = await apiPost('/simulation/replay', {
        profile_id: simProfile,
        window_hours: 24,
        max_records: 100,
      });
      setSimResult(result);
    } catch (err) {
      setSimResult({ error: err.message });
    }
    setSimLoading(false);
  };

  return (
    <div className="space-y-6">
      <div className="bg-gray-900 rounded-lg border border-gray-800 p-4">
        <h3 className="text-sm font-medium text-gray-400 mb-3">Active Risk Tolerance Profile</h3>
        {active ? (
          <div className="space-y-3">
            <div className="flex items-center gap-3">
              <span className="text-lg font-bold text-white">{active.name}</span>
              <span className="text-xs bg-green-900/50 text-green-400 px-2 py-0.5 rounded border border-green-800">
                Active
              </span>
            </div>
            <div className="grid grid-cols-2 md:grid-cols-4 gap-3 text-sm">
              <div>
                <span className="text-gray-500">Pack ID:</span>
                <span className="ml-2 text-gray-300 font-mono">{active.pack_id}</span>
              </div>
              <div>
                <span className="text-gray-500">Version:</span>
                <span className="ml-2 text-gray-300">{active.version}</span>
              </div>
              <div>
                <span className="text-gray-500">Risk Tier:</span>
                <span className="ml-2 text-gray-300">{active.profile_config?.risk_tier}</span>
              </div>
              <div>
                <span className="text-gray-500">Action Class:</span>
                <span className="ml-2 text-gray-300">{active.profile_config?.action_class}</span>
              </div>
            </div>
            {active.profile_config?.decision_thresholds && (
              <div className="bg-gray-800/50 rounded p-3">
                <h4 className="text-xs text-gray-500 mb-2">Decision Thresholds</h4>
                <div className="grid grid-cols-3 gap-2 text-sm">
                  {Object.entries(active.profile_config.decision_thresholds).map(([k, v]) => (
                    <div key={k}>
                      <span className="text-gray-500">{k}:</span>
                      <span className="ml-1 text-gray-300 font-mono">{v}</span>
                    </div>
                  ))}
                </div>
              </div>
            )}
          </div>
        ) : (
          <p className="text-gray-600 text-sm">No regulatory pack deployed. Using base policy profiles.</p>
        )}
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
        <div className="bg-gray-900 rounded-lg border border-gray-800 p-4">
          <h3 className="text-sm font-medium text-gray-400 mb-3">Available Packs</h3>
          <div className="space-y-2">
            {packList.map((p) => (
              <div key={p.pack_id} className="bg-gray-800/50 rounded p-3 flex justify-between items-center">
                <div>
                  <div className="text-sm text-gray-300">{p.name}</div>
                  <div className="text-xs text-gray-500">{p.pack_id} v{p.version}</div>
                </div>
                {hasEditAccess() && (
                  <button
                    onClick={() => apiPost(`/packs/${p.pack_id}/deploy`, {})}
                    className="text-xs bg-blue-700 hover:bg-blue-600 text-white px-3 py-1 rounded"
                  >
                    Deploy
                  </button>
                )}
              </div>
            ))}
          </div>
        </div>

        <div className="bg-gray-900 rounded-lg border border-gray-800 p-4">
          <h3 className="text-sm font-medium text-gray-400 mb-3">Simulation Runner</h3>
          <div className="space-y-3">
            <div>
              <label className="text-xs text-gray-500">Profile ID for replay</label>
              <input
                value={simProfile}
                onChange={(e) => setSimProfile(e.target.value)}
                placeholder="e.g. fin-r3-a4-ct4"
                className="w-full bg-gray-800 border border-gray-700 rounded px-3 py-2 text-white text-sm mt-1"
              />
            </div>
            <button
              onClick={runSimulation}
              disabled={simLoading || !simProfile}
              className="bg-purple-700 hover:bg-purple-600 disabled:opacity-50 text-white px-4 py-2 rounded text-sm"
            >
              {simLoading ? 'Running...' : 'Run Historical Replay'}
            </button>
            {simResult && (
              <div className="bg-gray-800/50 rounded p-3 mt-2">
                {simResult.error ? (
                  <p className="text-red-400 text-sm">{simResult.error}</p>
                ) : (
                  <div className="text-sm space-y-1">
                    <div className="text-gray-300">
                      Risk: <span className={simResult.impact_report?.risk_level === 'high' ? 'text-red-400' : simResult.impact_report?.risk_level === 'medium' ? 'text-yellow-400' : 'text-green-400'}>
                        {simResult.impact_report?.risk_level || 'N/A'}
                      </span>
                    </div>
                    <div className="text-gray-400 text-xs">
                      Recommendation: {simResult.impact_report?.recommendation || 'N/A'}
                    </div>
                    <pre className="text-xs text-gray-500 font-mono mt-2 overflow-x-auto max-h-40">
                      {JSON.stringify(simResult.simulation, null, 2)}
                    </pre>
                  </div>
                )}
              </div>
            )}
          </div>
        </div>
      </div>

      <div className="bg-gray-900 rounded-lg border border-gray-800 p-4">
        <h3 className="text-sm font-medium text-gray-400 mb-3">Adaptation History</h3>
        {history.length === 0 ? (
          <p className="text-gray-600 text-sm">No policy adaptations recorded.</p>
        ) : (
          <div className="space-y-2">
            {history.map((a, i) => (
              <div key={i} className="bg-gray-800/50 rounded p-3 text-sm">
                <div className="flex justify-between">
                  <span className="text-gray-300 font-mono text-xs">{a.record_id}</span>
                  <span className={`text-xs ${a.status === 'approved' ? 'text-green-400' : a.status === 'rejected' ? 'text-red-400' : 'text-yellow-400'}`}>
                    {a.status}
                  </span>
                </div>
                <div className="text-xs text-gray-500 mt-1">{a.created_at}</div>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
