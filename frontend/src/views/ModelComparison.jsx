import { Component, useMemo, useState } from 'react';
import { Link } from 'react-router-dom';
import { useConnections } from '../hooks/useConnections';
import { useOperatingMode } from '../hooks/useOperatingMode';
import { apiPost } from '../hooks/useApi';
import { displayGoverned } from '../lib/governedDecimal';

// Commit 4 (demo-live branch): Model Comparison — one identical governed
// request against 2-4 explicitly selected connections. Same prompt, same
// retrieved local-corpus context, same policy; each output is governed
// independently (own TIS scores, own decision, own Trust Certificate).
// No web retrieval. API keys stay in frontend memory and are sent
// request-scoped per member — never rendered, never persisted here.

const MIN_TARGETS = 2;
const MAX_TARGETS = 4;

const DECISION_STYLES = {
  Allow:    'bg-green-900/30 text-green-400 border-green-800',
  Observe:  'bg-blue-900/30 text-blue-400 border-blue-800',
  Hold:     'bg-yellow-900/30 text-yellow-400 border-yellow-800',
  Escalate: 'bg-orange-900/30 text-orange-400 border-orange-800',
  Stop:     'bg-red-900/30 text-red-400 border-red-800',
};

function DecisionBadge({ decision }) {
  const style = DECISION_STYLES[decision] ||
    'bg-gray-800 text-gray-400 border-gray-700';
  return (
    <span className={`text-xs font-semibold px-2 py-0.5 rounded border ${style}`}>
      {decision}
    </span>
  );
}

function shortHash(h) {
  return h ? `${h.slice(0, 12)}…` : '—';
}

// Malformed-member isolation: one bad member must never blank the whole
// comparison grid, mirroring the D2 list-isolation pattern. A real error
// boundary is required — render-phase throws cannot be caught by a
// try/catch around JSX creation.
class SafeMemberCard extends Component {
  constructor(props) {
    super(props);
    this.state = { failed: false };
  }

  static getDerivedStateFromError() {
    return { failed: true };
  }

  render() {
    if (this.state.failed) {
      return (
        <div className="bg-gray-900 border border-red-900 rounded-lg p-4 text-xs text-red-400">
          This comparison member could not be rendered. Other members are unaffected.
        </div>
      );
    }
    return <MemberCard member={this.props.member} />;
  }
}

function MemberCard({ member: m }) {
  const isProviderFailure = m.status !== 'ok';
  return (
    <div className="bg-gray-900 border border-gray-800 rounded-lg p-4 flex flex-col gap-3 min-w-0">
      {/* Identity */}
      <div className="flex items-start justify-between gap-2">
        <div className="min-w-0">
          <div className="text-sm font-medium text-white truncate">
            {m.connection_name || `${m.provider} / ${m.model}`}
          </div>
          <div className="text-[11px] text-gray-500 font-mono truncate">
            {m.provider} · {m.model}
          </div>
          {m.label && (
            <span className="inline-block mt-1 text-[10px] text-gray-400 bg-gray-800 border border-gray-700 rounded-full px-2 py-0.5">
              {m.label}
            </span>
          )}
        </div>
        <span className={`text-[10px] font-mono px-1.5 py-0.5 rounded ${
          m.execution_mode === 'live_provider'
            ? 'bg-red-900/30 text-red-300'
            : 'bg-gray-800 text-gray-400'
        }`}>
          {m.execution_mode === 'live_provider' ? 'LIVE' : 'SCRIPTED'}
        </span>
      </div>

      {/* Provider-layer failure — explicitly NOT a governance decision.
          empty_output means the provider answered without any usable
          model-generated text; the message shown is a system
          diagnostic, never provider content. */}
      {isProviderFailure && (
        <div className="bg-red-900/15 border border-red-900/60 rounded p-2">
          <div className="text-[11px] font-semibold text-red-400 uppercase tracking-wide">
            {m.status === 'timeout' ? 'Provider timeout'
              : m.status === 'empty_output' ? 'Provider returned no usable output'
              : 'Provider error'}
          </div>
          <p className="text-xs text-red-300 mt-1 break-words">
            <span className="text-gray-500">System diagnostic (not model output): </span>
            {m.error}
          </p>
          <p className="text-[10px] text-gray-500 mt-1">
            No model output was produced, so no governance decision or
            Trust Certificate exists for this member.
          </p>
          {(m.provider_request_id || m.usage?.total_tokens != null) && (
            <p className="text-[10px] text-gray-600 font-mono mt-1">
              {m.provider_request_id && `req ${String(m.provider_request_id).slice(0, 18)} `}
              {m.usage?.total_tokens != null && `· ${m.usage.total_tokens} tokens`}
            </p>
          )}
        </div>
      )}

      {!isProviderFailure && (
        <>
          {/* Response */}
          <div className="bg-gray-950 border border-gray-800 rounded p-2 text-xs text-gray-300 whitespace-pre-wrap max-h-48 overflow-y-auto">
            {m.blocked
              ? <span className="text-yellow-400">Response withheld by governance ({m.decision}).</span>
              : (m.response || <span className="text-gray-600">(empty response)</span>)}
          </div>

          {/* Governance */}
          <div className="flex items-center gap-2">
            <DecisionBadge decision={m.decision} />
            {m.tis_current != null && (
              <span className="text-[11px] font-mono text-gray-400">
                TIS {displayGoverned(m.tis_current, 3)}
              </span>
            )}
            {(m.gate_result === 0 || m.gate_result === 1) && (
              <span className={`text-[11px] ${m.gate_result === 1 ? 'text-green-500' : 'text-red-400'}`}>
                gate {m.gate_result === 1 ? 'pass' : 'fail'}
              </span>
            )}
            {m.requires_human_review && (
              <span className="text-[10px] text-yellow-500">review</span>
            )}
          </div>

          {m.component_scores && (
            <div className="grid grid-cols-4 gap-1">
              {['B', 'A', 'C', 'K'].map((d) => (
                <div key={d} className="bg-gray-800/60 rounded px-1.5 py-1 text-center">
                  <div className="text-[10px] text-gray-500">{d}</div>
                  <div className="text-[11px] font-mono text-gray-300">
                    {displayGoverned(m.component_scores[d], 2)}
                  </div>
                  {m.gate_results && (
                    <div className={`text-[9px] ${
                      m.gate_results[d] === 'fail' ? 'text-red-400'
                        : m.gate_results[d] === 'pass' ? 'text-green-600'
                        : 'text-gray-600'
                    }`}>
                      {m.gate_results[d] === 'not_applicable' ? 'n/a' : m.gate_results[d]}
                    </div>
                  )}
                </div>
              ))}
            </div>
          )}

          {m.explanation && (
            <p className="text-[11px] text-gray-400 leading-snug">{m.explanation}</p>
          )}

          {/* Telemetry */}
          <div className="flex flex-wrap gap-x-3 gap-y-0.5 text-[10px] text-gray-500 font-mono">
            {m.latency_ms != null && <span>{Number(m.latency_ms).toFixed(0)}ms</span>}
            {m.usage?.total_tokens != null && <span>{m.usage.total_tokens} tokens</span>}
            {m.provider_request_id && (
              <span className="truncate" title={m.provider_request_id}>
                req {String(m.provider_request_id).slice(0, 18)}
              </span>
            )}
          </div>

          {/* Audit actions */}
          <div className="flex items-center gap-2 pt-1 border-t border-gray-800">
            {m.certificate_id && (
              <>
                <span className="text-[10px] font-mono text-gray-500 truncate" title={m.certificate_id}>
                  TC {m.certificate_id.slice(0, 8)}…
                </span>
                <Link to="/audit" className="text-[11px] text-blue-400 hover:text-blue-300">
                  Certificate
                </Link>
              </>
            )}
            {m.artifact_id && (
              <Link to="/replay" className="text-[11px] text-blue-400 hover:text-blue-300">
                Replay
              </Link>
            )}
          </div>
        </>
      )}
    </div>
  );
}

export default function ModelComparison() {
  const { llmConnections } = useConnections();
  const { isLive, isDemo } = useOperatingMode();

  const [selectedIds, setSelectedIds] = useState([]);
  // Optional neutral operator labels ("smaller model", "frontier model")
  // keyed by connection id. Presentation metadata only — never scoring.
  const [labels, setLabels] = useState({});
  const [prompt, setPrompt] = useState('');
  const [confirming, setConfirming] = useState(false);
  const [running, setRunning] = useState(false);
  const [result, setResult] = useState(null);
  const [requestError, setRequestError] = useState(null);
  const [showTech, setShowTech] = useState(false);

  const selected = useMemo(
    () => selectedIds
      .map((id) => llmConnections.find((c) => c.id === id))
      .filter(Boolean),
    [selectedIds, llmConnections],
  );
  const externalCount = selected.filter((c) => c.type !== 'mock').length;
  const demoBlocked = isDemo && externalCount > 0;
  const canStart = prompt.trim() &&
    selected.length >= MIN_TARGETS && selected.length <= MAX_TARGETS &&
    !demoBlocked && !running;

  const toggle = (id) => {
    setSelectedIds((prev) => prev.includes(id)
      ? prev.filter((x) => x !== id)
      : prev.length >= MAX_TARGETS ? prev : [...prev, id]);
    setConfirming(false);
  };

  const runComparison = async () => {
    setRunning(true);
    setConfirming(false);
    setRequestError(null);
    setResult(null);
    try {
      const data = await apiPost('/query/compare', {
        query: prompt.trim(),
        execution_mode: isLive ? 'live' : 'demo',
        targets: selected.map((c) => ({
          provider: c.type,
          model: c.config?.model || '',
          api_key: c.type === 'mock' ? null : (c.config?.apiKey || null),
          connection_name: c.name,
          label: (labels[c.id] || '').trim() || null,
        })),
      });
      setResult(data);
    } catch (err) {
      setRequestError(err?.message || 'Comparison request failed');
    } finally {
      setRunning(false);
    }
  };

  const okMembers = result?.members?.filter((m) => m.status === 'ok') || [];
  const failedMembers = result?.members?.filter((m) => m.status !== 'ok') || [];

  return (
    <div className="space-y-6">
      {/* Header */}
      <div>
        <h2 className="text-lg font-semibold text-white">Model Comparison</h2>
        <p className="text-xs text-gray-500 mt-1">
          Run one identical governed request against two to four models.
          Same prompt, same retrieved context, same policy — TCS evaluates
          each output independently. Outputs differ; trust characteristics
          differ; model capability and governability are related but not
          equivalent.
        </p>
        <p className="text-[11px] text-gray-600 mt-1">
          Local corpus retrieval is active for all members (one retrieval,
          frozen and shared). No web retrieval. Policy profile: the active
          policy pack governs every member identically.
        </p>
        <div className="mt-2">
          <span className={`text-[11px] font-semibold px-2 py-0.5 rounded ${
            isLive ? 'bg-red-900/40 text-red-300' : 'bg-gray-800 text-gray-300'
          }`}>
            {isLive ? 'LIVE MODE — real provider calls' : 'DEMO MODE — scripted comparison only'}
          </span>
        </div>
      </div>

      {/* Target selection */}
      <section>
        <h3 className="text-sm font-medium text-white mb-2">
          Select {MIN_TARGETS}–{MAX_TARGETS} connections
        </h3>
        <div className="grid grid-cols-1 sm:grid-cols-2 gap-2">
          {llmConnections.map((c) => {
            const checked = selectedIds.includes(c.id);
            return (
              <label
                key={c.id}
                className={`flex items-center gap-3 rounded-lg border p-3 cursor-pointer transition-colors ${
                  checked ? 'border-blue-600 bg-blue-900/10' : 'border-gray-800 bg-gray-900 hover:border-gray-600'
                }`}
              >
                <input
                  type="checkbox"
                  checked={checked}
                  onChange={() => toggle(c.id)}
                  aria-label={`Select ${c.name}`}
                />
                <div className="min-w-0">
                  <div className="text-sm text-white truncate">{c.name}</div>
                  <div className="text-[11px] text-gray-500 font-mono truncate">
                    {c.type} · {c.config?.model || '(no model)'}
                  </div>
                </div>
              </label>
            );
          })}
        </div>
        {demoBlocked && (
          <p className="mt-2 text-xs text-yellow-400">
            DEMO MODE is active: external providers cannot be compared.
            Switch to LIVE MODE for real provider calls, or select mock
            connections for a scripted demonstration.
          </p>
        )}
      </section>

      {/* Prompt */}
      <section>
        <h3 className="text-sm font-medium text-white mb-2">Common prompt</h3>
        <textarea
          value={prompt}
          onChange={(e) => { setPrompt(e.target.value); setConfirming(false); }}
          rows={3}
          placeholder="One prompt, sent identically to every selected model…"
          className="w-full bg-gray-900 border border-gray-700 rounded-lg px-3 py-2 text-sm text-white placeholder-gray-600 focus:outline-none focus:border-blue-500"
        />
      </section>

      {/* Confirm + start */}
      <section className="space-y-2">
        {!confirming ? (
          <button
            onClick={() => setConfirming(true)}
            disabled={!canStart}
            className="text-sm px-4 py-2 rounded bg-blue-600 hover:bg-blue-700 disabled:opacity-40 disabled:cursor-not-allowed text-white font-medium"
          >
            Review comparison…
          </button>
        ) : (
          <div className="bg-gray-900 border border-blue-800/60 rounded-lg p-4 space-y-2">
            <p className="text-sm text-white">
              This comparison will make{' '}
              <span className="font-semibold">{externalCount}</span> external
              provider {externalCount === 1 ? 'request' : 'requests'}
              {externalCount > 0 && ' — provider charges may apply'}.
            </p>
            <ul className="text-xs text-gray-400 space-y-1">
              {selected.map((c) => (
                <li key={c.id} className="flex items-center gap-2">
                  <span className="font-mono">
                    {c.name}: {c.type} / {c.config?.model || '(no model)'}
                    {c.type === 'mock' && ' (scripted)'}
                  </span>
                  <input
                    type="text"
                    value={labels[c.id] || ''}
                    onChange={(e) => setLabels((prev) => ({ ...prev, [c.id]: e.target.value }))}
                    placeholder="optional label (e.g. smaller model)"
                    aria-label={`Label for ${c.name}`}
                    className="flex-1 bg-gray-800 border border-gray-700 rounded px-2 py-0.5 text-[11px] text-white placeholder-gray-600"
                  />
                </li>
              ))}
            </ul>
            <div className="flex gap-2 pt-1">
              <button
                onClick={runComparison}
                className="text-sm px-4 py-2 rounded bg-blue-600 hover:bg-blue-700 text-white font-medium"
              >
                Start comparison
              </button>
              <button
                onClick={() => setConfirming(false)}
                className="text-sm px-4 py-2 rounded border border-gray-700 text-gray-400 hover:text-white"
              >
                Cancel
              </button>
            </div>
          </div>
        )}
        {running && (
          <p className="text-xs text-gray-400">Running comparison…</p>
        )}
        {requestError && (
          <p className="text-xs text-red-400 bg-red-900/20 rounded px-2 py-1">
            {requestError}
          </p>
        )}
      </section>

      {/* Results */}
      {result && (
        <section className="space-y-3">
          <div className="flex items-center justify-between flex-wrap gap-2">
            <h3 className="text-sm font-medium text-white">
              Results — same prompt, same retrieved context, same policy
            </h3>
            <span className="text-[10px] text-gray-500">
              {okMembers.length} governed · {failedMembers.length} provider
              {failedMembers.length === 1 ? ' failure' : ' failures'}
            </span>
          </div>

          <div className={`grid gap-3 grid-cols-1 ${
            result.members.length > 2 ? 'lg:grid-cols-3 md:grid-cols-2' : 'md:grid-cols-2'
          }`}>
            {result.members.map((m) => (
              <SafeMemberCard key={m.comparison_member_id} member={m} />
            ))}
          </div>

          {/* Technical detail */}
          <div className="border border-gray-800 rounded-lg">
            <button
              onClick={() => setShowTech((v) => !v)}
              className="w-full text-left px-3 py-1.5 text-xs text-gray-400 hover:text-white"
            >
              {showTech ? '▾' : '▸'} Technical details
            </button>
            {showTech && (
              <div className="border-t border-gray-800 p-3 text-[11px] font-mono text-gray-400 space-y-1">
                <div>comparison_id: {result.comparison_id}</div>
                <div>prompt_package_hash: {shortHash(result.prompt_package_hash)}</div>
                <div>context_snapshot: {result.context_snapshot_id} ({shortHash(result.context_snapshot_hash)})</div>
                <div>policy_profile: {result.policy_profile_id} ({result.policy_profile_version})</div>
                <div>retrieval: k={result.retrieval_config?.k}, {result.retrieval_config?.ordering}, web_retrieval={String(result.retrieval_config?.web_retrieval)}</div>
                <div>execution_mode: {result.execution_mode}</div>
              </div>
            )}
          </div>
        </section>
      )}
    </div>
  );
}
