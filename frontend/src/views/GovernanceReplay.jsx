import { useEffect, useMemo, useState } from 'react';
import { useApi, apiFetch, apiPost } from '../hooks/useApi';
import StatusBadge from '../components/StatusBadge';

// =============================================================================
// Phase 5 Slice 5.6 — Governance Replay + Role-Based Views
// =============================================================================
//
// The point of this view is explanation, not new enforcement. The backend
// is the source of truth. The UI here makes the four-tier sidecar
// architecture readable to a human:
//
//     What was generated?
//     What context produced it?
//     What governance evaluated it?
//     What policy changed the result?
//     What evidence was recorded?
//
// Three display modes ("roles") modulate detail density:
//
//   general    — response/decision/plain-English reason + "show details"
//   admin      — adds BACK scores, gate results, rule matches, replay UI
//   auditor    — adds artifact/eval/TC IDs, snapshot availability,
//                policy snapshot, evaluation_origin + evaluation_strategy
//
// Roles here are display modes only (not security boundaries). Real
// authorization comes from useAuth + backend RBAC; this slice
// deliberately keeps the role picker simple so a reviewer can switch
// between density levels without re-authing.

// ── Role helpers ─────────────────────────────────────────────────────────────

const ROLES = [
  { id: 'general',  label: 'General User',
    blurb: 'Decision + reason + next action.' },
  { id: 'admin',    label: 'Governance Admin',
    blurb: 'Scores, gates, policy, rules, replay deltas.' },
  { id: 'auditor',  label: 'Auditor',
    blurb: 'Artifact/eval/TC linkage, snapshot, evidence.' },
];

function useDisplayRole() {
  const [role, setRole] = useState(() => {
    return localStorage.getItem('tcs_replay_role') || 'general';
  });
  useEffect(() => { localStorage.setItem('tcs_replay_role', role); }, [role]);
  return [role, setRole];
}

// ── Small helpers ────────────────────────────────────────────────────────────

const DECISION_TONE = {
  Allow:                'text-green-400',
  Observe:              'text-green-400',
  Hold:                 'text-amber-400',
  Escalate:             'text-amber-400',
  Stop:                 'text-red-400',
  Allow_with_logging:   'text-green-400',
  Allow_with_redaction: 'text-green-400',
  Allow_with_step_up:   'text-amber-400',
  Rollback:             'text-red-400',
};

const STRATEGY_LABEL = {
  runtime_snapshot:      'Replay (runtime snapshot)',
  artifact_metadata:     'Fresh re-evaluation',
  what_if_policy_replay: 'What-if (snapshot evidence, new policy)',
};

function plainEnglishReason(decision, blockingReason, ruleMatches) {
  // Prefer a rule's explanation if one fired; otherwise paraphrase the
  // blocking_reason; otherwise fall back to a decision-summary line.
  if (Array.isArray(ruleMatches)) {
    for (const m of ruleMatches) {
      const e = m?.effect || {};
      if (e.explanation) return e.explanation;
    }
  }
  if (blockingReason) {
    return `Decision driver: ${blockingReason}.`;
  }
  switch (decision) {
    case 'Allow':    return 'Response cleared all governance checks.';
    case 'Observe':  return 'Response delivered with monitoring.';
    case 'Hold':     return 'Response held for review.';
    case 'Escalate': return 'Response escalated for review.';
    case 'Stop':     return 'Response blocked by governance.';
    default:         return `Decision: ${decision}.`;
  }
}

function fmtNumber(n, digits = 4) {
  if (n === null || n === undefined || Number.isNaN(n)) return '—';
  return Number(n).toFixed(digits);
}

// ── Subview: Role picker ─────────────────────────────────────────────────────

function RolePicker({ role, setRole }) {
  return (
    <div className="bg-gray-900 border border-gray-800 rounded-lg p-3">
      <div className="text-xs uppercase tracking-wide text-gray-500 mb-2">
        Display mode
      </div>
      <div className="flex flex-wrap gap-2">
        {ROLES.map((r) => (
          <button
            key={r.id}
            onClick={() => setRole(r.id)}
            className={`text-left rounded border px-3 py-2 text-xs transition-colors ${
              role === r.id
                ? 'border-blue-600 bg-blue-900/20 text-white'
                : 'border-gray-800 bg-gray-800/30 text-gray-300 hover:border-gray-700'
            }`}
          >
            <div className="font-semibold text-sm">{r.label}</div>
            <div className="text-[10px] text-gray-500 mt-0.5">{r.blurb}</div>
          </button>
        ))}
      </div>
      <p className="text-[10px] text-gray-600 mt-2 leading-snug">
        Display modes here change information density only. Real
        authorization comes from your account role (configured separately).
      </p>
    </div>
  );
}

// ── Subview: Artifact picker ─────────────────────────────────────────────────

function ArtifactPicker({ selectedId, onSelect, refreshKey }) {
  const { data, refetch, loading, error } = useApi(
    `/artifacts?limit=50&_=${refreshKey}`,
  );
  const artifacts = data?.artifacts || [];
  const [manualId, setManualId] = useState('');

  const trySelectManual = () => {
    const id = manualId.trim();
    if (id) onSelect(id);
  };

  return (
    <div className="bg-gray-900 border border-gray-800 rounded-lg p-3">
      <div className="flex items-baseline justify-between mb-2">
        <div className="text-xs uppercase tracking-wide text-gray-500">
          Recent artifacts ({artifacts.length})
        </div>
        <button
          onClick={() => refetch()}
          className="text-[10px] text-gray-500 hover:text-gray-300"
        >
          Refresh
        </button>
      </div>
      {error && (
        <p className="text-xs text-red-400">Failed to load: {String(error.message || error)}</p>
      )}
      {loading && !data && (
        <p className="text-xs text-gray-500">Loading…</p>
      )}
      <div className="space-y-1 max-h-[420px] overflow-y-auto pr-1">
        {artifacts.length === 0 && !loading && (
          <p className="text-xs text-gray-600 italic">
            No artifacts yet. Run /v2/query, /v2/generate, or post a
            human-composed draft to create one.
          </p>
        )}
        {artifacts.map((a) => (
          <button
            key={a.artifact_id}
            onClick={() => onSelect(a.artifact_id)}
            className={`w-full text-left rounded border p-2 transition-colors ${
              selectedId === a.artifact_id
                ? 'border-blue-600 bg-blue-900/20'
                : 'border-gray-800 bg-gray-800/30 hover:border-gray-700'
            }`}
          >
            <div className="flex items-center gap-2 mb-1">
              <ModeBadge mode={a.generation_mode} />
              {a.generation_error && (
                <span className="text-[9px] bg-red-900/40 text-red-300 border border-red-800 rounded px-1.5">
                  error
                </span>
              )}
            </div>
            <div className="text-xs text-gray-300 line-clamp-2">
              {a.preview || '(no preview)'}
            </div>
            <div className="text-[9px] text-gray-600 font-mono mt-1 truncate">
              {a.artifact_id.slice(0, 14)}… · {a.created_at}
            </div>
          </button>
        ))}
      </div>
      <div className="mt-3 pt-3 border-t border-gray-800">
        <div className="text-[10px] uppercase tracking-wide text-gray-500 mb-1">
          Or open by ID
        </div>
        <div className="flex gap-1">
          <input
            value={manualId}
            onChange={(e) => setManualId(e.target.value)}
            onKeyDown={(e) => e.key === 'Enter' && trySelectManual()}
            placeholder="artifact-id"
            className="flex-1 bg-gray-800 border border-gray-700 rounded px-2 py-1 text-xs text-white font-mono"
          />
          <button
            onClick={trySelectManual}
            className="bg-gray-700 hover:bg-gray-600 text-white px-2 py-1 rounded text-xs"
          >
            Open
          </button>
        </div>
      </div>
    </div>
  );
}

function ModeBadge({ mode }) {
  const styles = {
    raw_llm:         'bg-purple-900/40 text-purple-200 border-purple-800',
    rag_llm:         'bg-blue-900/40   text-blue-200   border-blue-800',
    agent_workflow:  'bg-cyan-900/40   text-cyan-200   border-cyan-800',
    human_composed:  'bg-amber-900/40  text-amber-200  border-amber-800',
  };
  const label = {
    raw_llm: 'Raw LLM',
    rag_llm: 'RAG',
    agent_workflow: 'Workflow',
    human_composed: 'Human-composed',
  }[mode] || mode;
  return (
    <span className={`text-[10px] rounded border px-1.5 py-0.5 ${styles[mode] || 'bg-gray-800 text-gray-300 border-gray-700'}`}>
      {label}
    </span>
  );
}

// ── Subview: Artifact panel (role-aware) ─────────────────────────────────────

function ArtifactPanel({ artifact, role }) {
  if (!artifact) {
    return (
      <div className="bg-gray-900 border border-gray-800 rounded-lg p-6 text-center text-gray-500 text-sm">
        Select an artifact from the list to inspect.
      </div>
    );
  }

  const isHuman = artifact.generation_mode === 'human_composed';
  const isRaw   = artifact.generation_mode === 'raw_llm';
  const isRag   = artifact.generation_mode === 'rag_llm' || artifact.generation_mode === 'agent_workflow';

  return (
    <div className="bg-gray-900 border border-gray-800 rounded-lg p-4 space-y-3">
      <div className="flex items-center justify-between gap-3">
        <div className="flex items-center gap-2">
          <ModeBadge mode={artifact.generation_mode} />
          {isHuman && (
            <span className="text-[11px] text-amber-300">
              Human-composed draft, no LLM in the loop
            </span>
          )}
          {isRaw && (
            <span className="text-[11px] text-purple-300">
              Raw LLM — no RAG, no hidden system prompt
            </span>
          )}
        </div>
        {role !== 'general' && (
          <div className="text-[10px] text-gray-600 font-mono">
            {artifact.artifact_id}
          </div>
        )}
      </div>

      {artifact.prompt && (
        <div>
          <div className="text-[10px] uppercase tracking-wide text-gray-500 mb-1">
            {isHuman ? 'Context frame' : 'Prompt'}
          </div>
          <div className="bg-gray-800/50 rounded p-2 text-sm text-gray-200 whitespace-pre-wrap">
            {artifact.prompt}
          </div>
        </div>
      )}

      {artifact.raw_output != null && (
        <div>
          <div className="text-[10px] uppercase tracking-wide text-gray-500 mb-1">
            {isHuman ? 'Outbound draft' : 'Generated output'}
          </div>
          <div className="bg-gray-800/50 rounded p-2 text-sm text-gray-200 whitespace-pre-wrap">
            {artifact.raw_output}
          </div>
        </div>
      )}

      {artifact.generation_error && (
        <div className="bg-red-900/30 border border-red-800 rounded p-2 text-xs text-red-300">
          Generation error: {artifact.generation_error}
        </div>
      )}

      {/* RAG-specific: retrieved sources + system prompt */}
      {role !== 'general' && isRag && (
        <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
          <div>
            <div className="text-[10px] uppercase tracking-wide text-gray-500 mb-1">
              System prompt used
            </div>
            <div className="bg-gray-900/60 border border-gray-800 rounded p-2 text-[11px] text-gray-300 font-mono">
              {artifact.system_prompt_used || <span className="text-gray-600">(none recorded)</span>}
            </div>
          </div>
          <div>
            <div className="text-[10px] uppercase tracking-wide text-gray-500 mb-1">
              Retrieved sources ({(artifact.retrieved_sources || []).length})
            </div>
            <div className="space-y-1 max-h-32 overflow-y-auto">
              {(artifact.retrieved_sources || []).map((s, i) => (
                <div key={i} className="text-[10px] bg-gray-900/60 border border-gray-800 rounded px-2 py-1 flex justify-between">
                  <span className="text-gray-300 truncate">
                    {s.source_doc || s.chunk_id} <span className="text-gray-600">{s.version}</span>
                  </span>
                  <span className="text-gray-500 font-mono">
                    sim {fmtNumber(s.similarity_score, 3)}
                  </span>
                </div>
              ))}
            </div>
          </div>
        </div>
      )}

      {/* Raw LLM transparency block */}
      {role !== 'general' && isRaw && (
        <div className="grid grid-cols-2 gap-3 text-[11px]">
          <div className="bg-gray-900/60 border border-gray-800 rounded p-2">
            <div className="text-[10px] uppercase tracking-wide text-gray-500">RAG enabled</div>
            <div className="text-gray-300 font-mono">{artifact.rag_enabled ? 'yes' : 'no'}</div>
          </div>
          <div className="bg-gray-900/60 border border-gray-800 rounded p-2">
            <div className="text-[10px] uppercase tracking-wide text-gray-500">System prompt</div>
            <div className="text-gray-300 font-mono">
              {artifact.system_prompt_used ?? 'none'}
            </div>
          </div>
        </div>
      )}

      {/* Human-composed recipient context */}
      {isHuman && Object.keys(artifact.recipient_context || {}).length > 0 && (
        <div>
          <div className="text-[10px] uppercase tracking-wide text-gray-500 mb-1">
            Recipient context (typed facts)
          </div>
          <div className="bg-gray-900/60 border border-gray-800 rounded p-2 text-[11px] text-gray-300 font-mono">
            {Object.entries(artifact.recipient_context).map(([k, v]) => (
              <div key={k} className="flex justify-between gap-2">
                <span className="text-gray-500">{k}</span>
                <span>{String(v)}</span>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Auditor-only: provenance IDs */}
      {role === 'auditor' && (
        <div className="grid grid-cols-2 gap-2 text-[10px] font-mono text-gray-500 border-t border-gray-800 pt-2">
          <div>provider: <span className="text-gray-300">{artifact.provider ?? 'none'}</span></div>
          <div>model: <span className="text-gray-300">{artifact.model ?? 'none'}</span></div>
          <div>workflow_trace_id: <span className="text-gray-300">{artifact.workflow_trace_id ?? 'none'}</span></div>
          <div>created_at: <span className="text-gray-300">{artifact.created_at}</span></div>
        </div>
      )}
    </div>
  );
}

// ── Subview: Evaluation row (role-aware) ─────────────────────────────────────

function EvaluationRow({ ev, role }) {
  const ruleMatches = ev.rule_matches || [];
  const tone = DECISION_TONE[ev.decision] || 'text-gray-300';
  const reason = plainEnglishReason(ev.decision, /* blocking_reason */ null, ruleMatches);

  return (
    <div className="border border-gray-800 rounded p-2 bg-gray-800/20">
      <div className="flex items-center justify-between gap-2 flex-wrap">
        <div className="flex items-center gap-2 flex-wrap">
          <span className={`text-sm font-semibold ${tone}`}>{ev.decision}</span>
          <span className="text-[10px] bg-gray-800 text-gray-400 border border-gray-700 rounded px-1.5 py-0.5">
            {ev.mode}
          </span>
          <span className="text-[10px] bg-gray-800 text-gray-400 border border-gray-700 rounded px-1.5 py-0.5">
            origin: {ev.evaluation_origin}
          </span>
          {ev.evaluation_strategy && (
            <span className="text-[10px] bg-gray-800 text-gray-400 border border-gray-700 rounded px-1.5 py-0.5">
              {STRATEGY_LABEL[ev.evaluation_strategy] || ev.evaluation_strategy}
            </span>
          )}
          <span className="text-[10px] text-gray-500 font-mono">
            {ev.policy_profile_id}
          </span>
        </div>
        <div className="flex items-center gap-2">
          <span className="text-[10px] text-gray-500">
            {ev.enforcement_action}
            {ev.delivery_intervention ? ' · intervened' : ''}
          </span>
          {ev.trust_certificate_id && (
            <span className="text-[10px] font-mono text-gray-500" title={ev.trust_certificate_id}>
              TC: {ev.trust_certificate_id.slice(0, 8)}…
            </span>
          )}
        </div>
      </div>

      <div className="text-xs text-gray-300 mt-1.5 leading-snug">
        {reason}
      </div>

      {role !== 'general' && (
        <div className="mt-2 grid grid-cols-2 md:grid-cols-4 gap-1 text-[10px]">
          <Kv k="s_base"      v={fmtNumber(ev.s_base, 4)} />
          <Kv k="tis_current" v={fmtNumber(ev.tis_current, 4)} />
          <Kv k="B" v={fmtNumber(ev.component_scores?.B, 3)} />
          <Kv k="A" v={fmtNumber(ev.component_scores?.A, 3)} />
          <Kv k="C" v={fmtNumber(ev.component_scores?.C, 3)} />
          <Kv k="K" v={fmtNumber(ev.component_scores?.K, 3)} />
          {ev.gate_results && Object.entries(ev.gate_results).map(([dim, r]) => (
            <Kv key={dim} k={`gate ${dim}`} v={r} tone={r === 'fail' ? 'text-red-300' : ''} />
          ))}
        </div>
      )}

      {role !== 'general' && ruleMatches.length > 0 && (
        <div className="mt-2">
          <div className="text-[10px] uppercase tracking-wide text-gray-500 mb-1">
            Rule matches ({ruleMatches.length})
          </div>
          <div className="space-y-1">
            {ruleMatches.map((m, i) => (
              <div key={i} className="bg-gray-900/60 border border-gray-800 rounded p-1.5 text-[10px]">
                <div className="flex items-center justify-between gap-2">
                  <span className="text-amber-300 font-mono">{m.rule_id}</span>
                  <span className="text-gray-600">v{m.rule_version}</span>
                </div>
                <div className="text-gray-500 mt-0.5">
                  {m.effect?.control_class} · {m.effect?.safety_category || '—'} · {m.effect?.override_policy || '—'}
                </div>
                {m.matched_facts && Object.keys(m.matched_facts).length > 0 && (
                  <div className="text-gray-400 mt-1 font-mono">
                    matched_facts: {JSON.stringify(m.matched_facts)}
                  </div>
                )}
              </div>
            ))}
          </div>
        </div>
      )}

      {role === 'auditor' && (
        <div className="mt-2 pt-2 border-t border-gray-800 text-[10px] text-gray-500 font-mono">
          eval_id: {ev.evaluation_id}
          {ev.governance_input_snapshot && (
            <> · snapshot: present</>
          )}
        </div>
      )}
    </div>
  );
}

function Kv({ k, v, tone = '' }) {
  return (
    <div className="bg-gray-900/40 rounded px-1.5 py-1">
      <div className="text-gray-600">{k}</div>
      <div className={`font-mono ${tone || 'text-gray-300'}`}>{v}</div>
    </div>
  );
}

// ── Subview: Evaluations list ────────────────────────────────────────────────

function EvaluationsPanel({ artifactId, role, refreshKey }) {
  const { data, refetch, loading } = useApi(
    artifactId ? `/artifacts/${artifactId}/evaluations?_=${refreshKey}` : null,
  );
  const evals = data?.evaluations || [];
  if (!artifactId) return null;

  return (
    <div className="bg-gray-900 border border-gray-800 rounded-lg p-4">
      <div className="flex items-baseline justify-between mb-2">
        <div className="text-sm font-medium text-gray-300">
          Evaluations ({evals.length})
        </div>
        <button onClick={() => refetch()} className="text-[10px] text-gray-500 hover:text-gray-300">
          Refresh
        </button>
      </div>
      {loading && !data && <p className="text-xs text-gray-500">Loading…</p>}
      {evals.length === 0 && !loading && (
        <p className="text-xs text-gray-600 italic">
          No evaluations yet. Build a replay below to evaluate this artifact.
        </p>
      )}
      <div className="space-y-2">
        {evals.map((e) => (
          <EvaluationRow key={e.evaluation_id} ev={e} role={role} />
        ))}
      </div>
    </div>
  );
}

// ── Subview: Replay builder + results ────────────────────────────────────────

function ReplayPanel({ artifactId, onReplayComplete }) {
  // Live profile + pack listing for the dropdowns.
  const { data: packsData } = useApi('/packs');
  const builtIn = useMemo(
    () => [
      'baseline-no-pack',
      'fin-r3-a4-ct4',
      'fin-high-risk-suitability-v3',
      'clinical-cds-samed-v2',
      'enterprise-info-standard-v1',
      'enterprise-ops-standard-v1',
    ],
    [],
  );
  const composedPacks = useMemo(
    () => (packsData || []).map((p) => p.pack_id).filter(Boolean),
    [packsData],
  );
  const allProfiles = useMemo(
    () => Array.from(new Set([...builtIn, ...composedPacks])),
    [builtIn, composedPacks],
  );

  const [configs, setConfigs] = useState([
    { mode: 'observe', policy_profile_id: 'baseline-no-pack', strategy: '' },
  ]);
  const [running, setRunning] = useState(false);
  const [results, setResults] = useState(null);
  const [error, setError] = useState(null);

  const updateConfig = (i, key, value) => {
    setConfigs((prev) => prev.map((c, idx) => (idx === i ? { ...c, [key]: value } : c)));
  };
  const removeConfig = (i) => {
    setConfigs((prev) => prev.filter((_, idx) => idx !== i));
  };
  const addConfig = () => {
    setConfigs((prev) => [
      ...prev,
      { mode: 'observe', policy_profile_id: 'baseline-no-pack', strategy: '' },
    ]);
  };

  const runReplay = async () => {
    if (!artifactId || configs.length === 0) return;
    setRunning(true);
    setError(null);
    try {
      const cleaned = configs.map((c) => ({
        mode: c.mode,
        policy_profile_id: c.policy_profile_id || null,
        ...(c.strategy ? { strategy: c.strategy } : {}),
      }));
      const res = await apiPost('/replay', {
        artifact_id: artifactId,
        configurations: cleaned,
      });
      setResults(res);
      onReplayComplete?.();
    } catch (e) {
      setError(String(e.message || e));
      setResults(null);
    } finally {
      setRunning(false);
    }
  };

  if (!artifactId) return null;

  return (
    <div className="bg-gray-900 border border-gray-800 rounded-lg p-4 space-y-3">
      <div>
        <h3 className="text-sm font-medium text-gray-300">Replay panel</h3>
        <p className="text-[11px] text-gray-500 leading-snug mt-1">
          Evaluate this same captured artifact under multiple
          (mode, policy) configurations. No LLM is re-called. Use this
          to compare what governance would do under different policies
          against the same generated/drafted content.
        </p>
      </div>

      <div className="space-y-1.5">
        {configs.map((c, i) => (
          <div key={i} className="flex items-center gap-2">
            <select
              value={c.mode}
              onChange={(e) => updateConfig(i, 'mode', e.target.value)}
              className="bg-gray-800 border border-gray-700 rounded px-2 py-1 text-xs text-white"
            >
              <option value="observe">observe</option>
              <option value="enforce">enforce</option>
              <option value="what_if">what_if</option>
            </select>
            <select
              value={c.policy_profile_id}
              onChange={(e) => updateConfig(i, 'policy_profile_id', e.target.value)}
              className="flex-1 bg-gray-800 border border-gray-700 rounded px-2 py-1 text-xs text-white font-mono"
            >
              {allProfiles.map((p) => (
                <option key={p} value={p}>{p}</option>
              ))}
            </select>
            <select
              value={c.strategy}
              onChange={(e) => updateConfig(i, 'strategy', e.target.value)}
              className="bg-gray-800 border border-gray-700 rounded px-2 py-1 text-xs text-white"
              title="Evaluation strategy. 'auto' means runtime_snapshot when prior snapshot matches policy; else artifact_metadata."
            >
              <option value="">auto</option>
              <option value="runtime_snapshot">runtime_snapshot</option>
              <option value="artifact_metadata">artifact_metadata</option>
              <option value="what_if_policy_replay">what_if_policy_replay</option>
            </select>
            <button
              onClick={() => removeConfig(i)}
              className="text-gray-600 hover:text-red-400 px-2"
              disabled={configs.length <= 1}
              title="Remove this configuration"
            >
              ×
            </button>
          </div>
        ))}
      </div>

      <div className="flex items-center gap-2">
        <button
          onClick={addConfig}
          className="text-xs bg-gray-800 hover:bg-gray-700 text-gray-300 px-3 py-1.5 rounded"
        >
          + Add config
        </button>
        <button
          onClick={runReplay}
          disabled={running || !configs.length}
          className="text-xs bg-blue-700 hover:bg-blue-600 disabled:opacity-50 text-white px-4 py-1.5 rounded"
        >
          {running ? 'Running…' : `Run replay (${configs.length})`}
        </button>
      </div>

      {error && (
        <div className="bg-red-900/30 border border-red-800 rounded p-2 text-xs text-red-300">
          {error}
        </div>
      )}

      {results && (
        <div className="border-t border-gray-800 pt-3">
          <div className="text-[10px] uppercase tracking-wide text-gray-500 mb-2">
            Results ({results.count})
          </div>
          <div className="overflow-x-auto">
            <table className="w-full text-[11px]">
              <thead className="text-gray-500 uppercase">
                <tr>
                  <th className="text-left py-1">Mode</th>
                  <th className="text-left py-1">Policy</th>
                  <th className="text-left py-1">Strategy</th>
                  <th className="text-left py-1">Decision</th>
                  <th className="text-left py-1">Action</th>
                  <th className="text-right py-1">s_base</th>
                  <th className="text-right py-1">tis_current</th>
                  <th className="text-left py-1">Failed gates</th>
                  <th className="text-left py-1">TC</th>
                </tr>
              </thead>
              <tbody>
                {results.evaluations.map((e) => {
                  const failedGates = Object.entries(e.gate_results || {})
                    .filter(([, r]) => r === 'fail')
                    .map(([d]) => d);
                  return (
                    <tr key={e.evaluation_id} className="border-t border-gray-800">
                      <td className="py-1.5">{e.mode}</td>
                      <td className="py-1.5 font-mono text-gray-300">{e.policy_profile_id}</td>
                      <td className="py-1.5 text-gray-400">{e.evaluation_strategy}</td>
                      <td className={`py-1.5 font-semibold ${DECISION_TONE[e.decision] || ''}`}>
                        {e.decision}
                      </td>
                      <td className="py-1.5 text-gray-400">
                        {e.enforcement_action}
                        {e.delivery_intervention ? ' (intervened)' : ''}
                      </td>
                      <td className="py-1.5 text-right font-mono text-gray-300">{fmtNumber(e.s_base, 4)}</td>
                      <td className="py-1.5 text-right font-mono text-gray-300">{fmtNumber(e.tis_current, 4)}</td>
                      <td className="py-1.5 text-red-300">
                        {failedGates.length > 0 ? failedGates.join(', ') : '—'}
                      </td>
                      <td className="py-1.5 font-mono text-gray-500">
                        {e.trust_certificate_id ? `${e.trust_certificate_id.slice(0, 8)}…` : '—'}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
          <p className="text-[10px] text-gray-600 mt-2 leading-snug">
            Replay never re-calls the LLM. Each row evaluates the same
            captured artifact under a different (mode, policy)
            configuration; runtime_snapshot strategy replays the
            captured TISInput verbatim, artifact_metadata re-scores
            from artifact provenance, and what_if_policy_replay
            isolates policy impact by reusing prior evidence.
          </p>
        </div>
      )}
    </div>
  );
}

// ── Top-level view ───────────────────────────────────────────────────────────

export default function GovernanceReplay() {
  const [role, setRole] = useDisplayRole();
  const [selectedId, setSelectedId] = useState(null);
  const [refreshKey, setRefreshKey] = useState(0);

  const [artifact, setArtifact] = useState(null);
  const [artifactError, setArtifactError] = useState(null);

  useEffect(() => {
    setArtifact(null);
    setArtifactError(null);
    if (!selectedId) return;
    let cancelled = false;
    (async () => {
      try {
        const a = await apiFetch(`/artifacts/${selectedId}`);
        if (!cancelled) setArtifact(a);
      } catch (e) {
        if (!cancelled) setArtifactError(String(e.message || e));
      }
    })();
    return () => { cancelled = true; };
  }, [selectedId, refreshKey]);

  const onReplayComplete = () => setRefreshKey((k) => k + 1);

  return (
    <div className="space-y-4">
      <div>
        <h2 className="text-lg font-semibold text-white">Governance Replay</h2>
        <p className="text-xs text-gray-500 mt-0.5">
          Generate once → capture artifact → evaluate / replay under
          governance → enforce or observe → show audit evidence.
          The frontend explains the architecture; the backend is the
          source of truth.
        </p>
      </div>

      <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
        <div className="space-y-3">
          <RolePicker role={role} setRole={setRole} />
          <ArtifactPicker
            selectedId={selectedId}
            onSelect={setSelectedId}
            refreshKey={refreshKey}
          />
        </div>
        <div className="md:col-span-2 space-y-3">
          {artifactError && (
            <div className="bg-red-900/30 border border-red-800 rounded p-2 text-xs text-red-300">
              {artifactError}
            </div>
          )}
          <ArtifactPanel artifact={artifact} role={role} />
          <EvaluationsPanel
            artifactId={selectedId}
            role={role}
            refreshKey={refreshKey}
          />
          <ReplayPanel
            artifactId={selectedId}
            onReplayComplete={onReplayComplete}
          />
        </div>
      </div>
    </div>
  );
}
