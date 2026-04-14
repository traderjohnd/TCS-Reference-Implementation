import { useState } from 'react';
import { useApi, apiFetch } from '../hooks/useApi';
import StatusBadge from '../components/StatusBadge';

export default function AuditCertificates() {
  const [limit, setLimit] = useState(20);
  const { data, refetch } = useApi(`/certificates?limit=${limit}`);
  const [selectedTc, setSelectedTc] = useState(null);
  const [tcDetail, setTcDetail] = useState(null);
  const [verifyResult, setVerifyResult] = useState(null);
  const [verifying, setVerifying] = useState(false);
  const [searchId, setSearchId] = useState('');

  const certs = data?.certificates || [];

  const viewDetail = async (id) => {
    try {
      const tc = await apiFetch(`/certificates/${id}`);
      setTcDetail(tc);
      setSelectedTc(id);
    } catch {
      setTcDetail(null);
    }
  };

  const verifyChain = async () => {
    setVerifying(true);
    try {
      const result = await apiFetch('/certificates/verify-chain');
      setVerifyResult(result);
    } catch (e) {
      setVerifyResult({ error: e.message });
    }
    setVerifying(false);
  };

  const searchCert = async () => {
    if (!searchId.trim()) return;
    try {
      const tc = await apiFetch(`/certificates/${searchId.trim()}`);
      setTcDetail(tc);
      setSelectedTc(searchId.trim());
    } catch {
      alert('Certificate not found');
    }
  };

  const TC_LAYERS = [
    { title: 'Layer I: Identity', fields: ['certificate_id', 'subject_id', 'subject_type', 'domain', 'risk_tier', 'action_class', 'policy_severity', 'checkpoint_id', 'gca_context_id', 'policy_set_id'] },
    { title: 'Layer S: Score', fields: ['tis_raw', 'tis_adjusted', 'tis_current', 'penalty_aggregate'] },
    { title: 'Component Scores', key: 'component_scores' },
    { title: 'Component Weights', key: 'component_weights' },
    { title: 'Penalty Breakdown', key: 'penalty_breakdown' },
    { title: 'Layer G: Gate', fields: ['gate_passed', 'blocking_reason', 'failure_mode'] },
    { title: 'Gate Results', key: 'gate_results' },
    { title: 'Thresholds', key: 'thresholds' },
    { title: 'Decision', fields: ['decision', 'requires_human_review', 'escalation_routed_to'] },
    { title: 'Layer Prov: Provenance', fields: ['source_references', 'retrieval_ids', 'chain_of_custody_id', 'integration_boundary_gaps'] },
    { title: 'Layer T: Temporal', fields: ['evaluation_timestamp', 'valid_until', 'decay_rate', 'recompute_required', 'invalidation_status'] },
    { title: 'Layer E: Explanation', fields: ['explanation_summary', 'key_factors', 'key_concerns', 'regulatory_mapping'] },
    { title: 'Layer L: Lifecycle', fields: ['lifecycle_state'] },
    { title: 'MCP Extensions', key: 'scope_attestation' },
    { title: 'CT Audit Fields', fields: ['connection_type', 'connection_type_modifier_id'] },
    { title: 'Audit Integrity', key: 'audit_integrity' },
  ];

  return (
    <div className="space-y-6">
      <div className="flex gap-3 items-end">
        <div className="flex-1">
          <label className="text-xs text-gray-500">Search by Certificate ID</label>
          <div className="flex gap-2 mt-1">
            <input
              value={searchId}
              onChange={(e) => setSearchId(e.target.value)}
              placeholder="Enter certificate UUID"
              className="flex-1 bg-gray-800 border border-gray-700 rounded px-3 py-2 text-white text-sm"
            />
            <button onClick={searchCert}
              className="bg-blue-700 hover:bg-blue-600 text-white px-4 py-2 rounded text-sm">
              Search
            </button>
          </div>
        </div>
        <button
          onClick={verifyChain}
          disabled={verifying}
          className="bg-green-700 hover:bg-green-600 disabled:opacity-50 text-white px-4 py-2 rounded text-sm whitespace-nowrap"
        >
          {verifying ? 'Verifying...' : 'Verify Chain'}
        </button>
      </div>

      {verifyResult && (
        <div className={`rounded-lg p-3 border text-sm ${
          verifyResult.chain_intact
            ? 'bg-green-900/30 border-green-700 text-green-400'
            : 'bg-red-900/30 border-red-700 text-red-400'
        }`}>
          Chain Integrity: <strong>{verifyResult.chain_intact ? 'VERIFIED' : 'BROKEN'}</strong>
          {' '} | {verifyResult.tc_count} TCs | {verifyResult.chain_count} chains
          {verifyResult.broken_chains?.length > 0 && (
            <span> | Broken: {verifyResult.broken_chains.join(', ')}</span>
          )}
        </div>
      )}

      <div className="flex gap-6">
        <div className={`${selectedTc ? 'w-1/2' : 'w-full'} transition-all`}>
          <div className="bg-gray-900 rounded-lg border border-gray-800 p-4">
            <div className="flex justify-between items-center mb-3">
              <h3 className="text-sm font-medium text-gray-400">TC Archive ({data?.count || 0})</h3>
              <select
                value={limit}
                onChange={(e) => { setLimit(Number(e.target.value)); }}
                className="bg-gray-800 border border-gray-700 rounded px-2 py-1 text-xs text-white"
              >
                <option value={20}>20</option>
                <option value={50}>50</option>
                <option value={100}>100</option>
                <option value={200}>200</option>
              </select>
            </div>
            <div className="space-y-1">
              {certs.map((tc) => (
                <div
                  key={tc.certificate_id}
                  onClick={() => viewDetail(tc.certificate_id)}
                  className={`rounded p-2 cursor-pointer flex items-center justify-between text-sm ${
                    selectedTc === tc.certificate_id ? 'bg-blue-900/30 border border-blue-800' : 'bg-gray-800/30 hover:bg-gray-800/60'
                  }`}
                >
                  <div className="flex items-center gap-2">
                    <StatusBadge decision={tc.decision} />
                    <span className="text-gray-400 font-mono text-xs">{tc.subject_id}</span>
                  </div>
                  <span className="text-gray-500 font-mono text-xs">{tc.tis_current?.toFixed(4)}</span>
                </div>
              ))}
            </div>
          </div>
        </div>

        {selectedTc && tcDetail && (
          <div className="w-1/2">
            <div className="bg-gray-900 rounded-lg border border-gray-800 p-4 sticky top-4">
              <div className="flex justify-between items-center mb-3">
                <h3 className="text-sm font-medium text-white">TC Detail — All 11 Layers</h3>
                <button onClick={() => { setSelectedTc(null); setTcDetail(null); }}
                  className="text-gray-400 hover:text-white">&times;</button>
              </div>
              <div className="space-y-3 max-h-[75vh] overflow-y-auto">
                {TC_LAYERS.map(({ title, fields, key }) => (
                  <div key={title} className="border border-gray-800 rounded p-2">
                    <h4 className="text-xs font-medium text-blue-400 uppercase tracking-wider mb-1">{title}</h4>
                    {fields ? (
                      <dl className="space-y-0.5">
                        {fields.map((f) => (
                          <div key={f} className="flex justify-between text-xs">
                            <dt className="text-gray-500">{f}</dt>
                            <dd className="text-gray-300 font-mono max-w-[55%] text-right truncate">
                              {typeof tcDetail[f] === 'object' ? JSON.stringify(tcDetail[f]) : String(tcDetail[f] ?? '')}
                            </dd>
                          </div>
                        ))}
                      </dl>
                    ) : key && tcDetail[key] ? (
                      <pre className="text-xs text-gray-400 font-mono overflow-x-auto">
                        {JSON.stringify(tcDetail[key], null, 2)}
                      </pre>
                    ) : (
                      <span className="text-xs text-gray-600">N/A</span>
                    )}
                  </div>
                ))}
              </div>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
