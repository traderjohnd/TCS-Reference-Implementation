import { useState, useEffect } from 'react';
import { usePolling, apiFetch, apiPost } from '../hooks/useApi';
import StatusBadge from '../components/StatusBadge';

function TCDetailPanel({ certificateId, onClose }) {
  const [tcData, setTcData] = useState(null);
  const [loadingTc, setLoadingTc] = useState(true);

  useEffect(() => {
    setLoadingTc(true);
    apiFetch(`/certificates/${certificateId}`)
      .then(setTcData)
      .catch(() => setTcData(null))
      .finally(() => setLoadingTc(false));
  }, [certificateId]);

  if (loadingTc || !tcData) return <div className="text-gray-500 p-4">Loading TC...</div>;

  const sections = [
    { title: 'Identity', fields: ['certificate_id', 'subject_id', 'subject_type', 'domain', 'risk_tier', 'action_class', 'policy_set_id'] },
    { title: 'Score', fields: ['s_base', 's_adjusted', 'tis_raw', 'tis_adjusted', 'tis_current', 'penalty_aggregate'] },
    { title: 'Components', data: tcData.component_scores },
    { title: 'Gate Results', data: tcData.gate_results },
    { title: 'Decision', fields: ['decision', 'requires_human_review', 'blocking_reason'] },
    { title: 'Provenance', fields: ['source_references', 'integration_boundary_gaps'] },
    { title: 'Temporal', fields: ['evaluation_timestamp', 'valid_until', 'decay_rate', 'invalidation_status'] },
    { title: 'Explanation', fields: ['explanation_summary', 'key_factors', 'key_concerns'] },
    { title: 'Lifecycle', fields: ['lifecycle_state'] },
    { title: 'MCP Extensions', data: tcData.scope_attestation },
    { title: 'Audit Integrity', data: tcData.audit_integrity },
  ];

  return (
    <div className="fixed inset-y-0 right-0 w-full max-w-lg bg-gray-900 border-l border-gray-800 overflow-y-auto z-50 shadow-2xl">
      <div className="sticky top-0 bg-gray-900 border-b border-gray-800 p-4 flex justify-between items-center">
        <h3 className="text-lg font-semibold text-white">Trust Certificate Detail</h3>
        <button onClick={onClose} className="text-gray-400 hover:text-white text-xl">&times;</button>
      </div>
      <div className="p-4 space-y-4">
        {sections.map(({ title, fields, data }) => (
          <div key={title} className="border border-gray-800 rounded-lg p-3">
            <h4 className="text-xs font-medium text-blue-400 uppercase tracking-wider mb-2">{title}</h4>
            {fields ? (
              <dl className="space-y-1">
                {fields.map((f) => (
                  <div key={f} className="flex justify-between text-sm">
                    <dt className="text-gray-500">{f}</dt>
                    <dd className="text-gray-300 font-mono text-xs max-w-[60%] text-right truncate">
                      {JSON.stringify(tcData[f]) ?? 'null'}
                    </dd>
                  </div>
                ))}
              </dl>
            ) : data ? (
              <pre className="text-xs text-gray-400 font-mono overflow-x-auto">
                {JSON.stringify(data, null, 2)}
              </pre>
            ) : null}
          </div>
        ))}
      </div>
    </div>
  );
}

// Single OverrideForm used for both Hold and Escalate queues. The
// endpoint path and the allowed decision options differ; the rest is
// identical. Override goes to the lifecycle_events table on the
// backend; the original TC is never mutated.
function OverrideForm({ tcId, endpoint, options, defaultOption, onDone }) {
  const [decision, setDecision] = useState(defaultOption || options[0]);
  const [justification, setJustification] = useState('');
  const [submitting, setSubmitting] = useState(false);

  const handleSubmit = async (e) => {
    e.preventDefault();
    setSubmitting(true);
    try {
      await apiPost(`${endpoint}/${tcId}/override`, {
        override_decision: decision,
        justification,
        override_by: localStorage.getItem('tcs_user')
          ? JSON.parse(localStorage.getItem('tcs_user')).username
          : 'unknown',
      });
      onDone();
    } catch {
      alert('Override failed');
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <form onSubmit={handleSubmit} className="flex gap-2 items-end mt-2">
      <select value={decision} onChange={(e) => setDecision(e.target.value)}
        className="bg-gray-800 border border-gray-700 rounded px-2 py-1 text-xs text-white">
        {options.map((o) => (
          <option key={o} value={o}>{o}</option>
        ))}
      </select>
      <input value={justification} onChange={(e) => setJustification(e.target.value)}
        placeholder="Justification (min 10 chars)"
        className="bg-gray-800 border border-gray-700 rounded px-2 py-1 text-xs text-white flex-1" />
      <button type="submit" disabled={submitting || justification.length < 10}
        className="bg-blue-600 hover:bg-blue-700 disabled:opacity-50 text-white px-3 py-1 rounded text-xs">
        Submit
      </button>
    </form>
  );
}

// Small inline badge that surfaces override info on a Recent Decisions
// row. The original TC content stays unchanged; this badge reads from
// the new `override` field on the decisions-stream payload and shows
// the reviewer who overrode and to what.
function OverrideBadge({ override }) {
  const [expanded, setExpanded] = useState(false);
  if (!override) return null;
  const tone = override.override_decision === 'Allow'
    ? 'border-green-700 text-green-300 bg-green-900/30'
    : override.override_decision === 'Stop'
    ? 'border-red-700 text-red-300 bg-red-900/30'
    : 'border-blue-700 text-blue-300 bg-blue-900/30';
  return (
    <div className="inline-block">
      <button
        onClick={(e) => { e.stopPropagation(); setExpanded((v) => !v); }}
        className={`text-[10px] rounded border px-1.5 py-0.5 font-mono ${tone}`}
        title="Click to expand override details"
      >
        Overridden → {override.override_decision || '?'}
      </button>
      {expanded && (
        <div className="mt-1 text-[10px] text-gray-400 bg-gray-900/80 border border-gray-700 rounded p-2 max-w-md">
          <div><span className="text-gray-500">actor:</span> <span className="text-gray-300">{override.override_actor || '—'}</span></div>
          <div><span className="text-gray-500">at:</span> <span className="text-gray-300 font-mono">{override.override_at}</span></div>
          {override.override_reason_text && (
            <div className="mt-1">
              <span className="text-gray-500">reason:</span>{' '}
              <span className="text-gray-300">{override.override_reason_text}</span>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

export default function LiveDecisions() {
  const { data, refetch } = usePolling('/govern/decisions/stream?limit=50', 3000);
  const { data: holdData, refetch: refetchHolds } = usePolling('/govern/hold-queue?limit=20', 5000);
  const { data: escData, refetch: refetchEsc } = usePolling('/govern/escalation-queue?limit=20', 5000);
  const [selectedTc, setSelectedTc] = useState(null);
  const [overrideId, setOverrideId] = useState(null);

  const decisions = data?.decisions || [];
  const holds = holdData?.holds || [];
  const escalations = escData?.escalations || [];

  // Common "refresh everything" after an override — the override
  // affects the stream (badge appears), the source queue (TC drops
  // out), and any cross-references downstream.
  const onOverrideDone = () => {
    setOverrideId(null);
    refetch();
    refetchHolds();
    refetchEsc();
  };

  return (
    <div className="space-y-6">
      {selectedTc && <TCDetailPanel certificateId={selectedTc} onClose={() => setSelectedTc(null)} />}

      {holds.length > 0 && (
        <div className="bg-gray-900 rounded-lg border border-yellow-800 p-4">
          <h3 className="text-sm font-medium text-yellow-400 mb-1">Hold Queue ({holds.length})</h3>
          <p className="text-[11px] text-gray-500 mb-3">
            Remediable review. Reviewer can Allow (release for delivery)
            or Escalate (push to senior review).
          </p>
          <div className="space-y-2">
            {holds.map((h) => (
              <div key={h.certificate_id} className="bg-gray-800/50 rounded p-3">
                <div className="flex items-center justify-between">
                  <div className="flex items-center gap-3">
                    <StatusBadge decision="Hold" />
                    <span className="text-sm text-gray-300 font-mono">{h.subject_id}</span>
                  </div>
                  <div className="flex items-center gap-2">
                    <button onClick={() => setSelectedTc(h.certificate_id)}
                      className="text-xs text-blue-400 hover:text-blue-300">View TC</button>
                    <button onClick={() => setOverrideId(overrideId === h.certificate_id ? null : h.certificate_id)}
                      className="text-xs text-yellow-400 hover:text-yellow-300">Override</button>
                  </div>
                </div>
                <div className="text-xs text-gray-500 mt-1">{h.blocking_reason}</div>
                {overrideId === h.certificate_id && (
                  <OverrideForm
                    tcId={h.certificate_id}
                    endpoint="/govern/hold-queue"
                    options={['Allow', 'Escalate']}
                    defaultOption="Allow"
                    onDone={onOverrideDone}
                  />
                )}
              </div>
            ))}
          </div>
        </div>
      )}

      {escalations.length > 0 && (
        <div className="bg-gray-900 rounded-lg border border-orange-800 p-4">
          <h3 className="text-sm font-medium text-orange-400 mb-1">
            Escalation Queue ({escalations.length})
          </h3>
          <p className="text-[11px] text-gray-500 mb-3">
            Senior-reviewer queue. Different from Hold: the score or
            policy condition warranted higher-authority review. Reviewer
            chooses Allow (approve the higher-risk action), Stop (reject
            outright), or Hold (return for more information).
          </p>
          <div className="space-y-2">
            {escalations.map((e) => (
              <div key={e.certificate_id} className="bg-gray-800/50 rounded p-3">
                <div className="flex items-center justify-between">
                  <div className="flex items-center gap-3">
                    <StatusBadge decision="Escalate" />
                    <span className="text-sm text-gray-300 font-mono">{e.subject_id}</span>
                  </div>
                  <div className="flex items-center gap-2">
                    <button onClick={() => setSelectedTc(e.certificate_id)}
                      className="text-xs text-blue-400 hover:text-blue-300">View TC</button>
                    <button onClick={() => setOverrideId(overrideId === e.certificate_id ? null : e.certificate_id)}
                      className="text-xs text-orange-400 hover:text-orange-300">Review</button>
                  </div>
                </div>
                <div className="text-xs text-gray-500 mt-1">{e.blocking_reason}</div>
                <div className="text-[10px] text-gray-400 mt-2 grid grid-cols-2 md:grid-cols-4 gap-2">
                  {e.s_base != null && (
                    <div><span className="text-gray-500">s_base:</span>{' '}
                      <span className="font-mono">{Number(e.s_base).toFixed(4)}</span></div>
                  )}
                  {e.tis_current != null && (
                    <div><span className="text-gray-500">TIS_current:</span>{' '}
                      <span className="font-mono">{Number(e.tis_current).toFixed(4)}</span></div>
                  )}
                  {e.policy_set_id && (
                    <div className="col-span-2"><span className="text-gray-500">profile:</span>{' '}
                      <span className="font-mono text-gray-300">{e.policy_set_id}</span></div>
                  )}
                </div>
                {e.escalation_routed_to && e.escalation_routed_to.length > 0 && (
                  <div className="text-[10px] text-gray-400 mt-2">
                    <span className="text-gray-500">routed to:</span>{' '}
                    {e.escalation_routed_to.map((role) => (
                      <span key={role} className="inline-block ml-1 px-1.5 py-0.5 rounded bg-orange-900/40 border border-orange-800 text-orange-200 font-mono">
                        {role}
                      </span>
                    ))}
                  </div>
                )}
                {e.identity_binding?.requesting_identity && (
                  <div className="text-[10px] text-gray-400 mt-1">
                    <span className="text-gray-500">requester:</span>{' '}
                    <span className="font-mono text-gray-300">
                      {e.identity_binding.requesting_identity}
                    </span>
                    {e.identity_binding.role && (
                      <span className="text-gray-500"> ({e.identity_binding.role})</span>
                    )}
                  </div>
                )}
                {overrideId === e.certificate_id && (
                  <OverrideForm
                    tcId={e.certificate_id}
                    endpoint="/govern/escalation-queue"
                    options={['Allow', 'Stop', 'Hold']}
                    defaultOption="Allow"
                    onDone={onOverrideDone}
                  />
                )}
              </div>
            ))}
          </div>
        </div>
      )}

      <div className="bg-gray-900 rounded-lg border border-gray-800 p-4">
        <h3 className="text-sm font-medium text-gray-400 mb-3">Recent Decisions ({decisions.length})</h3>
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="text-left text-gray-500 border-b border-gray-800">
                <th className="pb-2 pr-3">Decision</th>
                <th className="pb-2 pr-3">Subject</th>
                <th className="pb-2 pr-3">TIS</th>
                <th className="pb-2 pr-3">B</th>
                <th className="pb-2 pr-3">A</th>
                <th className="pb-2 pr-3">C</th>
                <th className="pb-2 pr-3">K</th>
                <th className="pb-2 pr-3">Domain</th>
                <th className="pb-2 pr-3">Latency</th>
                <th className="pb-2"></th>
              </tr>
            </thead>
            <tbody>
              {decisions.map((d) => {
                const rowBg = d.decision === 'Allow' ? 'bg-green-900/10'
                  : d.decision === 'Hold' ? 'bg-yellow-900/10'
                  : d.decision === 'Stop' ? 'bg-red-900/10'
                  : d.decision === 'Escalate' ? 'bg-orange-900/10'
                  : '';
                return (
                <tr key={d.certificate_id} className={`border-b border-gray-800/50 hover:bg-gray-800/30 ${rowBg}`}>
                  <td className="py-2 pr-3">
                    <div className="flex items-center gap-1.5">
                      <StatusBadge decision={d.decision} />
                      <OverrideBadge override={d.override} />
                    </div>
                  </td>
                  <td className="py-2 pr-3 font-mono text-xs text-gray-400">{d.subject_id}</td>
                  <td className="py-2 pr-3 font-mono text-xs">{d.tis_current?.toFixed(4)}</td>
                  <td className="py-2 pr-3 font-mono text-xs">{d.component_scores?.B?.toFixed(2)}</td>
                  <td className="py-2 pr-3 font-mono text-xs">{d.component_scores?.A?.toFixed(2)}</td>
                  <td className="py-2 pr-3 font-mono text-xs">{d.component_scores?.C?.toFixed(2)}</td>
                  <td className="py-2 pr-3 font-mono text-xs">{d.component_scores?.K?.toFixed(2)}</td>
                  <td className="py-2 pr-3 text-xs text-gray-500">{d.domain}</td>
                  <td className="py-2 pr-3 font-mono text-xs text-gray-500">{d.governance_ms != null ? `${d.governance_ms}ms` : '--'}</td>
                  <td className="py-2">
                    <button onClick={() => setSelectedTc(d.certificate_id)}
                      className="text-xs text-blue-400 hover:text-blue-300">Detail</button>
                  </td>
                </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  );
}
