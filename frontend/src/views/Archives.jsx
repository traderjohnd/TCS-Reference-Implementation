import { useState } from 'react';
import { useApi, apiFetch, apiPost } from '../hooks/useApi';

// ─── Decision badge ─────────────────────────────────────────────────────────

function DecisionBadge({ decision, count }) {
  const colors = {
    Allow: 'bg-green-900/40 text-green-400',
    Observe: 'bg-blue-900/40 text-blue-400',
    Hold: 'bg-yellow-900/40 text-yellow-400',
    Escalate: 'bg-orange-900/40 text-orange-400',
    Stop: 'bg-red-900/40 text-red-400',
  };
  return (
    <span className={`inline-flex items-center gap-1 px-2 py-0.5 rounded text-[10px] font-medium ${colors[decision] || 'bg-gray-800 text-gray-400'}`}>
      {decision} <span className="font-mono">{count}</span>
    </span>
  );
}

// ─── Archive Card ───────────────────────────────────────────────────────────

function ArchiveCard({ archive, onExpand, expanded }) {
  const [certs, setCerts] = useState(null);
  const [loadingCerts, setLoadingCerts] = useState(false);

  const created = new Date(archive.created_at);
  const dateStr = created.toLocaleDateString('en-US', {
    month: 'short', day: 'numeric', year: 'numeric',
  });
  const timeStr = created.toLocaleTimeString('en-US', {
    hour: 'numeric', minute: '2-digit',
  });

  const handleExpand = async () => {
    if (expanded) {
      onExpand(null);
      return;
    }
    onExpand(archive.id);
    if (!certs) {
      setLoadingCerts(true);
      try {
        const data = await apiFetch(`/archives/${archive.id}/certificates?limit=20`);
        setCerts(data.certificates);
      } catch {
        setCerts([]);
      }
      setLoadingCerts(false);
    }
  };

  return (
    <div className="bg-gray-900 rounded-lg border border-gray-800 overflow-hidden">
      {/* Summary row */}
      <button
        onClick={handleExpand}
        className="w-full px-5 py-4 text-left hover:bg-gray-800/50 transition-colors"
      >
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-4">
            <div className="w-10 h-10 bg-gray-800 rounded-lg flex items-center justify-center shrink-0">
              <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 20 20" fill="currentColor" className="w-5 h-5 text-gray-500">
                <path d="M2 3a1 1 0 00-1 1v1a1 1 0 001 1h16a1 1 0 001-1V4a1 1 0 00-1-1H2z" />
                <path fillRule="evenodd" d="M2 7.5h16l-.811 7.71a2 2 0 01-1.99 1.79H4.802a2 2 0 01-1.99-1.79L2 7.5zM7 11a1 1 0 011-1h4a1 1 0 110 2H8a1 1 0 01-1-1z" clipRule="evenodd" />
              </svg>
            </div>
            <div>
              <h4 className="text-sm font-medium text-white">{archive.label}</h4>
              <p className="text-[11px] text-gray-500 mt-0.5">
                {dateStr} at {timeStr}
                <span className="mx-2 text-gray-700">|</span>
                {archive.file_size_kb} KB
              </p>
            </div>
          </div>

          <div className="flex items-center gap-4">
            {/* Stats */}
            <div className="text-right hidden sm:block">
              <p className="text-sm font-mono text-white">{archive.certificate_count}</p>
              <p className="text-[10px] text-gray-500">certificates</p>
            </div>
            <div className="text-right hidden sm:block">
              <p className="text-sm font-mono text-white">{archive.chain_count}</p>
              <p className="text-[10px] text-gray-500">chains</p>
            </div>

            {/* Expand chevron */}
            <svg
              xmlns="http://www.w3.org/2000/svg"
              viewBox="0 0 20 20"
              fill="currentColor"
              className={`w-5 h-5 text-gray-500 transition-transform ${expanded ? 'rotate-180' : ''}`}
            >
              <path fillRule="evenodd" d="M5.23 7.21a.75.75 0 011.06.02L10 11.168l3.71-3.938a.75.75 0 111.08 1.04l-4.25 4.5a.75.75 0 01-1.08 0l-4.25-4.5a.75.75 0 01.02-1.06z" clipRule="evenodd" />
            </svg>
          </div>
        </div>

        {/* Decision breakdown */}
        {Object.keys(archive.decision_counts).length > 0 && (
          <div className="flex gap-2 mt-2 ml-14">
            {Object.entries(archive.decision_counts)
              .sort(([a], [b]) => a.localeCompare(b))
              .map(([decision, count]) => (
                <DecisionBadge key={decision} decision={decision} count={count} />
              ))}
          </div>
        )}

        {/* Time span */}
        {archive.time_span && (
          <p className="text-[10px] text-gray-600 mt-1.5 ml-14">
            Span: {new Date(archive.time_span.earliest).toLocaleDateString()} — {new Date(archive.time_span.latest).toLocaleDateString()}
          </p>
        )}
      </button>

      {/* Expanded certificate list */}
      {expanded && (
        <div className="border-t border-gray-800 px-5 py-3">
          {loadingCerts ? (
            <p className="text-xs text-gray-500 py-4 text-center">Loading certificates...</p>
          ) : certs && certs.length > 0 ? (
            <div className="overflow-x-auto">
              <table className="w-full text-xs">
                <thead>
                  <tr className="text-gray-500 text-left">
                    <th className="pb-2 pr-3 font-medium">Time</th>
                    <th className="pb-2 pr-3 font-medium">Subject</th>
                    <th className="pb-2 pr-3 font-medium">Decision</th>
                    <th className="pb-2 pr-3 font-medium">TIS</th>
                    <th className="pb-2 pr-3 font-medium">Domain</th>
                    <th className="pb-2 font-medium">Policy</th>
                  </tr>
                </thead>
                <tbody>
                  {certs.map((cert) => (
                    <tr key={cert.certificate_id} className="border-t border-gray-800/50">
                      <td className="py-1.5 pr-3 text-gray-400 whitespace-nowrap">
                        {new Date(cert.evaluation_timestamp).toLocaleTimeString()}
                      </td>
                      <td className="py-1.5 pr-3 text-gray-300 font-mono truncate max-w-[150px]">
                        {cert.subject_id}
                      </td>
                      <td className="py-1.5 pr-3">
                        <DecisionBadge decision={cert.decision} count="" />
                      </td>
                      <td className="py-1.5 pr-3 text-gray-300 font-mono">
                        {cert.tis_current?.toFixed(4)}
                      </td>
                      <td className="py-1.5 pr-3 text-gray-500">
                        {cert.domain} {cert.risk_tier}/{cert.action_class}
                      </td>
                      <td className="py-1.5 text-gray-600 truncate max-w-[120px]">
                        {cert.policy_set_id}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : (
            <p className="text-xs text-gray-500 py-4 text-center">No certificates in this archive.</p>
          )}
        </div>
      )}
    </div>
  );
}

// ─── Main Archives View ─────────────────────────────────────────────────────

export default function Archives() {
  const { data, loading, error, refetch } = useApi('/archives');
  const [archiving, setArchiving] = useState(false);
  const [archiveLabel, setArchiveLabel] = useState('');
  const [showForm, setShowForm] = useState(false);
  const [expandedId, setExpandedId] = useState(null);
  const [message, setMessage] = useState(null);

  const handleArchive = async () => {
    setArchiving(true);
    setMessage(null);
    try {
      const result = await apiPost('/archives', {
        label: archiveLabel.trim() || null,
      });
      setMessage({
        type: 'success',
        text: `Archived ${result.certificate_count} certificates as "${result.label}"`,
      });
      setShowForm(false);
      setArchiveLabel('');
      refetch();
    } catch (err) {
      setMessage({ type: 'error', text: err.message });
    }
    setArchiving(false);
  };

  const archives = data?.archives || [];

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex items-start justify-between">
        <div>
          <h2 className="text-lg font-semibold text-white">Archives</h2>
          <p className="text-xs text-gray-500 mt-1">
            Snapshot and preserve governance data. Each archive captures all certificates, chains, and audit records.
          </p>
        </div>
        {!showForm && (
          <button
            onClick={() => setShowForm(true)}
            className="text-xs px-4 py-2 rounded bg-blue-600 hover:bg-blue-700 text-white font-medium transition-colors shrink-0"
          >
            Archive &amp; Start Fresh
          </button>
        )}
      </div>

      {/* Message banner */}
      {message && (
        <div className={`rounded-lg px-4 py-3 text-sm ${
          message.type === 'success'
            ? 'bg-green-900/20 text-green-400 border border-green-800'
            : 'bg-red-900/20 text-red-400 border border-red-800'
        }`}>
          {message.text}
        </div>
      )}

      {/* Archive form */}
      {showForm && (
        <div className="bg-gray-900 rounded-lg border border-blue-800/50 border-dashed p-5">
          <h3 className="text-sm font-medium text-blue-400 mb-1">Archive Current Data</h3>
          <p className="text-[11px] text-gray-500 mb-4">
            This will snapshot all current certificates, decisions, and audit records into a timestamped archive, then reset the system to a clean state. Nothing is deleted.
          </p>
          <div className="flex gap-3 items-end">
            <div className="flex-1">
              <label className="block text-[11px] text-gray-500 mb-1">Archive Label (optional)</label>
              <input
                type="text"
                value={archiveLabel}
                onChange={(e) => setArchiveLabel(e.target.value)}
                placeholder={`Archive ${new Date().toLocaleDateString()}`}
                className="w-full bg-gray-800 border border-gray-700 rounded px-3 py-2 text-sm text-white placeholder-gray-600 focus:outline-none focus:border-blue-500"
              />
            </div>
            <button
              onClick={handleArchive}
              disabled={archiving}
              className="text-xs px-4 py-2 rounded bg-blue-600 hover:bg-blue-700 disabled:opacity-50 text-white font-medium transition-colors whitespace-nowrap"
            >
              {archiving ? 'Archiving...' : 'Archive Now'}
            </button>
            <button
              onClick={() => { setShowForm(false); setArchiveLabel(''); }}
              className="text-xs px-4 py-2 rounded border border-gray-700 text-gray-400 hover:text-white transition-colors"
            >
              Cancel
            </button>
          </div>
        </div>
      )}

      {/* Archives list */}
      {loading ? (
        <p className="text-sm text-gray-500 text-center py-8">Loading archives...</p>
      ) : error ? (
        <p className="text-sm text-red-400 text-center py-8">{error}</p>
      ) : archives.length === 0 ? (
        <div className="bg-gray-900/50 rounded-lg border border-gray-800 border-dashed p-8 text-center">
          <div className="w-14 h-14 bg-gray-800 rounded-xl flex items-center justify-center mx-auto mb-3">
            <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 20 20" fill="currentColor" className="w-6 h-6 text-gray-500">
              <path d="M2 3a1 1 0 00-1 1v1a1 1 0 001 1h16a1 1 0 001-1V4a1 1 0 00-1-1H2z" />
              <path fillRule="evenodd" d="M2 7.5h16l-.811 7.71a2 2 0 01-1.99 1.79H4.802a2 2 0 01-1.99-1.79L2 7.5zM7 11a1 1 0 011-1h4a1 1 0 110 2H8a1 1 0 01-1-1z" clipRule="evenodd" />
            </svg>
          </div>
          <p className="text-sm text-gray-400 mb-1">No archives yet</p>
          <p className="text-xs text-gray-600">
            Use "Archive &amp; Start Fresh" to snapshot your current data before a demo or walkthrough.
          </p>
        </div>
      ) : (
        <div className="space-y-3">
          {archives.map((archive) => (
            <ArchiveCard
              key={archive.id}
              archive={archive}
              expanded={expandedId === archive.id}
              onExpand={setExpandedId}
            />
          ))}
        </div>
      )}
    </div>
  );
}
