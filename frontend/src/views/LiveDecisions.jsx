import { useState, useEffect } from 'react';
import { usePolling, apiFetch, apiPost } from '../hooks/useApi';
import StatusBadge from '../components/StatusBadge';
import PlainLanguageExplanation from '../components/PlainLanguageExplanation';
import {
  AdjustmentsPanel,
  C3ProvenancePanel,
  GateSummary,
  IntegrityWarning,
  ScoreTiersPanel,
  UnsupportedCertificate,
  VersionsPanel,
} from '../components/CertificateV2Sections';
import {
  displayGoverned,
  normalizeCertificate,
  validateStreamRow,
} from '../lib/governedDecimal';

// Canonical BACK dimension order — used to reorder dim-keyed dicts
// (component_scores, component_weights, gate_results, thresholds) so
// they render in BACK sequence regardless of backend JSON serialization.
const BACK_ORDER = ['B', 'A', 'C', 'K'];
function reorderBack(dict) {
  if (!dict || typeof dict !== 'object') return dict;
  const out = {};
  for (const k of BACK_ORDER) {
    if (k in dict) out[k] = dict[k];
  }
  for (const k of Object.keys(dict)) {
    if (!BACK_ORDER.includes(k)) out[k] = dict[k];
  }
  return out;
}

function TCDetailPanel({ certificateId, onClose }) {
  const [tcData, setTcData] = useState(null);
  const [loadingTc, setLoadingTc] = useState(true);
  // Phase 5 artifact linked to this TC (when present). For TCs issued
  // via the artifact tier, tc.subject_id == artifact.artifact_id. For
  // older legacy TCs, the artifact endpoint will 404 and we render a
  // "Prompt not available" placeholder.
  const [artifact, setArtifact] = useState(null);
  // Override events for the TC, from the existing
  // /v2/govern/decisions/{tc_id}/override-history endpoint. Drives the
  // "Human override" block inside PlainLanguageExplanation.
  const [overrides, setOverrides] = useState([]);
  // Collapsible technical 11-layer section. Default closed so the
  // casual reader sees only the prompt + plain-language summary.
  const [technicalOpen, setTechnicalOpen] = useState(false);

  useEffect(() => {
    let cancelled = false;
    setLoadingTc(true);
    setArtifact(null);
    setOverrides([]);
    setTechnicalOpen(false);
    (async () => {
      try {
        const tc = await apiFetch(`/certificates/${certificateId}`);
        if (cancelled) return;
        setTcData(tc);
        if (tc?.subject_id) {
          try {
            const art = await apiFetch(`/artifacts/${tc.subject_id}`);
            if (!cancelled) setArtifact(art);
          } catch {
            if (!cancelled) setArtifact(null);
          }
        }
        try {
          const hist = await apiFetch(`/govern/decisions/${certificateId}/override-history`);
          if (!cancelled) setOverrides(hist?.events || []);
        } catch {
          if (!cancelled) setOverrides([]);
        }
      } catch {
        if (!cancelled) setTcData(null);
      } finally {
        if (!cancelled) setLoadingTc(false);
      }
    })();
    return () => { cancelled = true; };
  }, [certificateId]);

  if (loadingTc || !tcData) return <div className="text-gray-500 p-4">Loading TC...</div>;

  // Record-boundary normalization: strict v1/v2/unsupported dispatch.
  // Normalization failures produce the integrity state — never a crash.
  let norm;
  try {
    norm = normalizeCertificate(tcData);
  } catch {
    norm = {
      version: 'unsupported',
      gateResult: null,
      integrity: { ok: false, problems: ['normalization_failed'] },
    };
  }

  const sections = [
    { title: 'Identity', fields: ['certificate_id', 'subject_id', 'subject_type', 'domain', 'risk_tier', 'action_class', 'policy_set_id'] },
    { title: 'Score', fields: ['s_base', 's_adjusted', 'tis_raw', 'tis_adjusted', 'tis_current', 'penalty_aggregate'] },
    { title: 'Components', data: reorderBack(tcData.component_scores) },
    { title: 'Gate Results', data: reorderBack(tcData.gate_results) },
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
        <div className="flex items-center gap-2">
          <StatusBadge decision={tcData.decision} />
          <h3 className="text-lg font-semibold text-white">Decision Summary</h3>
        </div>
        <button onClick={onClose} className="text-gray-400 hover:text-white text-xl">&times;</button>
      </div>
      <div className="p-4 space-y-4">

        {/* ── ⓪ Strict version dispatch / integrity state ──────────── */}
        {norm.version === 'unsupported' && (
          <UnsupportedCertificate
            version={tcData.certificate_schema_version}
          />
        )}
        {norm.version !== 'unsupported' && !norm.integrity.ok && (
          <IntegrityWarning problems={norm.integrity.problems} />
        )}
        {norm.version !== 'unsupported' && (
          <div className="flex items-center justify-between text-xs px-1">
            <GateSummary gateResult={norm.gateResult} />
            <span className="text-gray-500 font-mono">
              schema v{norm.version}
              {norm.version === 2
                ? ` · ${tcData.calculation_version || '—'}`
                : ' · legacy'}
            </span>
          </div>
        )}

        {/* ── ① What was asked ─────────────────────────────────────── */}
        <div className="bg-gray-900 border border-gray-700 rounded-lg p-3">
          <div className="text-[11px] uppercase tracking-wide text-gray-500 mb-1.5">
            What was asked
          </div>
          {artifact?.prompt ? (
            <blockquote className="text-sm text-gray-200 italic border-l-2 border-gray-700 pl-3 whitespace-pre-wrap leading-relaxed">
              “{artifact.prompt}”
            </blockquote>
          ) : artifact?.raw_output && artifact?.generation_mode === 'human_composed' ? (
            <div>
              <div className="text-[10px] text-gray-500 mb-1">Human-composed draft (no prompt)</div>
              <blockquote className="text-sm text-gray-200 italic border-l-2 border-gray-700 pl-3 whitespace-pre-wrap leading-relaxed">
                “{artifact.raw_output}”
              </blockquote>
            </div>
          ) : (
            <p className="text-xs text-gray-500 italic">
              Prompt not available — this certificate predates the
              artifact tier or was issued through a non-artifact path.
            </p>
          )}
        </div>

        {/* ── ② Plain-language summary (with override block if any) ── */}
        <PlainLanguageExplanation
          tc={tcData}
          prompt={artifact?.prompt || (artifact?.generation_mode === 'human_composed' ? artifact?.raw_output : null)}
          overrides={overrides}
        />

        {/* ── ③ Collapsible technical sections ─────────────────────── */}
        <div className="border border-gray-800 rounded-lg">
          <button
            onClick={() => setTechnicalOpen((v) => !v)}
            className="w-full flex items-center justify-between px-3 py-2 text-xs uppercase tracking-wider text-blue-400 hover:bg-gray-800/60 transition-colors"
            aria-expanded={technicalOpen}
          >
            <span>Technical Detail (all 11 layers)</span>
            <svg
              xmlns="http://www.w3.org/2000/svg"
              viewBox="0 0 20 20"
              fill="currentColor"
              className={`w-3 h-3 transition-transform ${technicalOpen ? '' : '-rotate-90'}`}
            >
              <path
                fillRule="evenodd"
                d="M5.293 7.293a1 1 0 011.414 0L10 10.586l3.293-3.293a1 1 0 111.414 1.414l-4 4a1 1 0 01-1.414 0l-4-4a1 1 0 010-1.414z"
                clipRule="evenodd"
              />
            </svg>
          </button>
          {technicalOpen && (
            <div className="space-y-3 p-3 border-t border-gray-800">
              {norm.version !== 'unsupported' && (
                <VersionsPanel tc={tcData} version={norm.version} />
              )}
              {norm.version === 2 && (
                <>
                  <ScoreTiersPanel tc={tcData} />
                  <AdjustmentsPanel adjustments={tcData.adjustments_applied} />
                  <C3ProvenancePanel tc={tcData} />
                </>
              )}
              {sections.map(({ title, fields, data }) => (
                <div key={title} className="border border-gray-800 rounded p-2">
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
          )}
        </div>
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
function OverrideBadge({ override, certificateId }) {
  const [expanded, setExpanded] = useState(false);
  // History is fetched lazily on first expand. The badge in
  // /govern/decisions/stream carries only the most recent override;
  // the full audit list comes from the dedicated history endpoint.
  const [history, setHistory] = useState(null);
  const [historyError, setHistoryError] = useState(null);
  const [loadingHistory, setLoadingHistory] = useState(false);

  if (!override) return null;
  const tone = override.override_decision === 'Allow'
    ? 'border-green-700 text-green-300 bg-green-900/30'
    : override.override_decision === 'Stop'
    ? 'border-red-700 text-red-300 bg-red-900/30'
    : 'border-blue-700 text-blue-300 bg-blue-900/30';

  const onToggle = (e) => {
    e.stopPropagation();
    const nextExpanded = !expanded;
    setExpanded(nextExpanded);
    if (nextExpanded && history === null && !loadingHistory && certificateId) {
      setLoadingHistory(true);
      setHistoryError(null);
      apiFetch(`/govern/decisions/${certificateId}/override-history`)
        .then((data) => setHistory(data?.events || []))
        .catch((err) => setHistoryError(String(err)))
        .finally(() => setLoadingHistory(false));
    }
  };

  // What we render in the expanded panel: the history list when we
  // have it, otherwise the single override the row already carries.
  // This keeps the panel populated immediately on expand even before
  // the history fetch completes.
  const eventsToRender = history && history.length > 0 ? history : [override];
  const showingHistory = history !== null && history.length > 1;

  return (
    <div className="inline-block">
      <button
        onClick={onToggle}
        className={`text-[10px] rounded border px-1.5 py-0.5 font-mono ${tone}`}
        title="Click to expand override details"
      >
        Overridden → {override.override_decision || '?'}
      </button>
      {expanded && (
        <div className="mt-1 text-[10px] text-gray-400 bg-gray-900/80 border border-gray-700 rounded p-2 max-w-md space-y-2">
          {loadingHistory && (
            <div className="text-gray-500 italic">Loading history…</div>
          )}
          {historyError && (
            <div className="text-red-400">Failed to load history: {historyError}</div>
          )}
          {showingHistory && (
            <div className="text-[10px] text-gray-500 uppercase tracking-wide">
              Override history ({history.length}) — newest first
            </div>
          )}
          {eventsToRender.map((ev, i) => (
            <div
              key={`${ev.override_at || 'evt'}-${i}`}
              className={
                showingHistory
                  ? 'border-l-2 border-gray-700 pl-2'
                  : ''
              }
            >
              <div>
                <span className="text-gray-500">decision:</span>{' '}
                <span className="text-gray-300 font-mono">
                  {ev.override_decision || '—'}
                </span>
              </div>
              <div>
                <span className="text-gray-500">actor:</span>{' '}
                <span className="text-gray-300">{ev.override_actor || '—'}</span>
              </div>
              <div>
                <span className="text-gray-500">at:</span>{' '}
                <span className="text-gray-300 font-mono">{ev.override_at}</span>
              </div>
              {ev.override_reason_text && (
                <div>
                  <span className="text-gray-500">reason:</span>{' '}
                  <span className="text-gray-300">{ev.override_reason_text}</span>
                </div>
              )}
            </div>
          ))}
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
  // Malformed-record exclusions reported by the tolerant list
  // boundaries (D2): valid rows stay displayed; excluded rows are
  // surfaced as a bounded, names-only integrity notice. The same
  // malformed record is reported by each feed that would have carried
  // it, so warnings are DEDUPED by certificate id before counting —
  // one damaged record is one damaged record, not three.
  const allWarnings = (data?.integrity_warnings || [])
    .concat(holdData?.integrity_warnings || [])
    .concat(escData?.integrity_warnings || []);
  const seenIds = new Set();
  const integrityWarnings = allWarnings.filter((w) => {
    const key = w?.certificate_id;
    if (!key) return true;              // unidentifiable rows can't dedupe
    if (seenIds.has(key)) return false;
    seenIds.add(key);
    return true;
  }).slice(0, 10);
  const excludedTotal = Math.max(
    integrityWarnings.length,
    data?.excluded_malformed_count || 0,
    holdData?.excluded_malformed_count || 0,
    escData?.excluded_malformed_count || 0,
  );

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

      {excludedTotal > 0 && (
        <div
          role="alert"
          className="bg-red-900/20 border border-red-800 rounded-lg p-3 text-xs text-red-200"
        >
          <span className="font-semibold">
            {excludedTotal} stored record{excludedTotal === 1 ? '' : 's'} failed
            integrity validation and {excludedTotal === 1 ? 'was' : 'were'} excluded
            from these feeds.
          </span>{' '}
          Valid records remain displayed; chain verification reports the
          underlying problem.
          {integrityWarnings.length > 0 && (
            <span className="block mt-1 font-mono text-red-300/80">
              {integrityWarnings.map((w, i) => (
                <span key={`${w.certificate_id || 'unknown'}-${i}`} className="mr-2">
                  {(w.certificate_id || '(unknown id)').slice(0, 14)}…
                  [{(w.invalid_fields || []).join(', ') || w.failure_category}]
                </span>
              ))}
            </span>
          )}
        </div>
      )}

      <div className="bg-gray-900 rounded-lg border border-yellow-800 p-4">
        <h3 className="text-sm font-medium text-yellow-400 mb-1">Hold Queue ({holds.length})</h3>
        <p className="text-[11px] text-gray-500 mb-3">
          Remediable review. Reviewer can Allow (release for delivery)
          or Escalate (push to senior review).
        </p>
        {holds.length === 0 ? (
          <div className="text-xs text-gray-500 italic px-2 py-3">
            No items pending review.
          </div>
        ) : (
          <div className="space-y-2">
            {holds.map((h) => {
              // One malformed record must never blank the queue: it
              // renders with an integrity warning and loses its
              // consequential (Override) action; neighbors render.
              const rowCheck = validateStreamRow(h);
              return (
              <div key={h.certificate_id} className="bg-gray-800/50 rounded p-3">
                <div className="flex items-center justify-between">
                  <div className="flex items-center gap-3">
                    <StatusBadge decision="Hold" />
                    <span className="text-sm text-gray-300 font-mono">{h.subject_id}</span>
                  </div>
                  <div className="flex items-center gap-2">
                    <button onClick={() => setSelectedTc(h.certificate_id)}
                      className="text-xs text-blue-400 hover:text-blue-300">View TC</button>
                    {rowCheck.ok ? (
                      <button onClick={() => setOverrideId(overrideId === h.certificate_id ? null : h.certificate_id)}
                        className="text-xs text-yellow-400 hover:text-yellow-300">Override</button>
                    ) : (
                      <span className="text-[10px] text-red-400 border border-red-800 rounded px-1.5 py-0.5"
                        title={`Invalid fields: ${rowCheck.problems.join(', ')}`}>
                        data integrity — override disabled
                      </span>
                    )}
                  </div>
                </div>
                <div className="text-xs text-gray-500 mt-1">{h.blocking_reason}</div>
                {rowCheck.ok && overrideId === h.certificate_id && (
                  <OverrideForm
                    tcId={h.certificate_id}
                    endpoint="/govern/hold-queue"
                    options={['Allow', 'Escalate']}
                    defaultOption="Allow"
                    onDone={onOverrideDone}
                  />
                )}
              </div>
              );
            })}
          </div>
        )}
      </div>

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
        {escalations.length === 0 ? (
          <div className="text-xs text-gray-500 italic px-2 py-3">
            No escalations pending senior review.
          </div>
        ) : (
          <div className="space-y-2">
            {escalations.map((e) => {
              const rowCheck = validateStreamRow(e);
              return (
              <div key={e.certificate_id} className="bg-gray-800/50 rounded p-3">
                <div className="flex items-center justify-between">
                  <div className="flex items-center gap-3">
                    <StatusBadge decision="Escalate" />
                    <span className="text-sm text-gray-300 font-mono">{e.subject_id}</span>
                  </div>
                  <div className="flex items-center gap-2">
                    <button onClick={() => setSelectedTc(e.certificate_id)}
                      className="text-xs text-blue-400 hover:text-blue-300">View TC</button>
                    {rowCheck.ok ? (
                      <button onClick={() => setOverrideId(overrideId === e.certificate_id ? null : e.certificate_id)}
                        className="text-xs text-orange-400 hover:text-orange-300">Review</button>
                    ) : (
                      <span className="text-[10px] text-red-400 border border-red-800 rounded px-1.5 py-0.5"
                        title={`Invalid fields: ${rowCheck.problems.join(', ')}`}>
                        data integrity — review disabled
                      </span>
                    )}
                  </div>
                </div>
                <div className="text-xs text-gray-500 mt-1">{e.blocking_reason}</div>
                <div className="text-[10px] text-gray-400 mt-2 grid grid-cols-2 md:grid-cols-4 gap-2">
                  {e.s_base != null && (
                    <div><span className="text-gray-500">s_base:</span>{' '}
                      <span className="font-mono">{displayGoverned(e.s_base)}</span></div>
                  )}
                  {e.tis_current != null && (
                    <div><span className="text-gray-500">TIS_current:</span>{' '}
                      <span className="font-mono">{displayGoverned(e.tis_current)}</span></div>
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
                {rowCheck.ok && overrideId === e.certificate_id && (
                  <OverrideForm
                    tcId={e.certificate_id}
                    endpoint="/govern/escalation-queue"
                    options={['Allow', 'Stop', 'Hold']}
                    defaultOption="Allow"
                    onDone={onOverrideDone}
                  />
                )}
              </div>
              );
            })}
          </div>
        )}
      </div>

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
                      <OverrideBadge
                        override={d.override}
                        certificateId={d.certificate_id}
                      />
                    </div>
                  </td>
                  <td className="py-2 pr-3 font-mono text-xs text-gray-400">{d.subject_id}</td>
                  <td className="py-2 pr-3 font-mono text-xs">{displayGoverned(d.tis_current)}</td>
                  <td className="py-2 pr-3 font-mono text-xs">{displayGoverned(d.component_scores?.B, 2)}</td>
                  <td className="py-2 pr-3 font-mono text-xs">{displayGoverned(d.component_scores?.A, 2)}</td>
                  <td className="py-2 pr-3 font-mono text-xs">{displayGoverned(d.component_scores?.C, 2)}</td>
                  <td className="py-2 pr-3 font-mono text-xs">{displayGoverned(d.component_scores?.K, 2)}</td>
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
