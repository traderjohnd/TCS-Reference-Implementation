import { useState } from 'react';
import { useApi, apiFetch, usePolling } from '../hooks/useApi';
import StatusBadge from '../components/StatusBadge';

// ─── GovernanceRuleMatches panel ────────────────────────────────────────────
//
// Surfaces the governance_rule_matches block from the Trust Certificate so
// reviewers can see exactly which deterministic rules fired during this
// evaluation, under which version of each rule, and what category of
// guardrail each one represented.
//
// Three states the panel handles distinctly:
//
//   governance_rule_matches === null       — classifier did not run for
//                                            this evaluation (or this is a
//                                            legacy certificate predating
//                                            the rule audit field).
//   governance_rule_matches.length === 0   — classifier ran; no rule
//                                            matched. Decision came from
//                                            BACK/TIS scoring alone.
//   non-empty list                          — one card per matched rule.
//
// Visual distinction by control_class:
//
//   hard_safety            — red accent. These are the rules that produce
//                            STOP (or restricted-override) regardless of
//                            scoring. Prompt-injection, credential exposure,
//                            consumer-facing patient-specific prescribing
//                            under prohibited indications, etc.
//   deterministic_bounded  — amber accent. The rule layer flagged a
//                            category that the typed-facts evaluator
//                            (forthcoming) will refine.
//   weighted_evidence      — gray accent. Shapes BACK/TIS scoring; never
//                            short-circuits the decision.
//
// Framing pinned at the bottom of the panel: hard safety MAY be detected
// by rules when the violation is a prompt-risk pattern, but bounded
// device safety MUST be evaluated against structured facts and validated
// envelopes — exactly what the future bounded-control evaluator covers.

const CONTROL_CLASS_PRESENTATION = {
  hard_safety: {
    label: 'Hard Safety',
    accent: 'border-red-700 bg-red-900/15',
    badge: 'bg-red-900/40 text-red-300 border border-red-800',
    description:
      'Non-overrideable or restricted-override guardrail. Firing forces STOP regardless of scoring.',
  },
  deterministic_bounded: {
    label: 'Deterministic Bounded',
    accent: 'border-amber-700 bg-amber-900/10',
    badge: 'bg-amber-900/40 text-amber-300 border border-amber-800',
    description:
      'Category flagged by rule; full envelope check belongs to the typed-facts evaluator.',
  },
  weighted_evidence: {
    label: 'Weighted Evidence',
    accent: 'border-gray-700 bg-gray-800/30',
    badge: 'bg-gray-800 text-gray-300 border border-gray-700',
    description:
      'Shapes BACK/TIS scoring; never short-circuits the decision.',
  },
};

function _presentationFor(controlClass) {
  return (
    CONTROL_CLASS_PRESENTATION[controlClass] || {
      label: controlClass || 'Unknown',
      accent: 'border-gray-700 bg-gray-800/30',
      badge: 'bg-gray-800 text-gray-300 border border-gray-700',
      description: '',
    }
  );
}

function KV({ label, value, mono = true, danger = false }) {
  // Tight key/value row used inside each rule card.
  if (value === undefined || value === null || value === '') return null;
  return (
    <div className="flex justify-between gap-2 text-xs">
      <dt className="text-gray-500 whitespace-nowrap">{label}</dt>
      <dd
        className={`text-right truncate ${mono ? 'font-mono' : ''} ${
          danger ? 'text-red-300' : 'text-gray-300'
        }`}
      >
        {String(value)}
      </dd>
    </div>
  );
}

function GovernanceRuleMatches({ matches }) {
  // State 1 — classifier did not run / legacy TC.
  if (matches === null || matches === undefined) {
    return (
      <div className="border border-gray-800 rounded p-2">
        <h4 className="text-xs font-medium text-blue-400 uppercase tracking-wider mb-1">
          Governance Rule Matches
        </h4>
        <p className="text-xs text-gray-500">
          Classifier did not run for this evaluation (or this is a legacy
          certificate predating the rule-audit field).
        </p>
      </div>
    );
  }

  // State 2 — classifier ran, no rules matched.
  if (Array.isArray(matches) && matches.length === 0) {
    return (
      <div className="border border-gray-800 rounded p-2">
        <h4 className="text-xs font-medium text-blue-400 uppercase tracking-wider mb-1">
          Governance Rule Matches
        </h4>
        <p className="text-xs text-gray-500">
          Classifier ran; no governance rules matched. Decision came from
          BACK/TIS scoring alone.
        </p>
      </div>
    );
  }

  // State 3 — one card per matched rule.
  return (
    <div className="border border-gray-800 rounded p-2">
      <div className="flex items-baseline justify-between mb-2">
        <h4 className="text-xs font-medium text-blue-400 uppercase tracking-wider">
          Governance Rule Matches ({matches.length})
        </h4>
      </div>
      <div className="space-y-2">
        {matches.map((m, idx) => {
          const eff = m.effect || {};
          const ctrl = eff.control_class || 'weighted_evidence';
          const pres = _presentationFor(ctrl);
          const facts = m.matched_facts || {};
          const factEntries = Object.entries(facts).filter(
            ([, v]) => v !== null && v !== undefined && v !== ''
          );
          const termGroups = Array.isArray(m.matched_term_groups)
            ? m.matched_term_groups
            : [];
          return (
            <div
              key={`${m.rule_id}-${idx}`}
              className={`rounded border p-2 ${pres.accent}`}
            >
              <div className="flex items-baseline justify-between gap-2 mb-1">
                <div className="min-w-0">
                  <div className="text-sm text-gray-200 font-mono truncate">
                    {m.rule_id}
                  </div>
                  <div className="text-[10px] text-gray-500 mt-0.5">
                    version {m.rule_version || '—'}
                    {m.matched_domain && (
                      <>
                        {' '}· matched domain{' '}
                        <span className="text-gray-400">{m.matched_domain}</span>
                      </>
                    )}
                  </div>
                </div>
                <span className={`text-[10px] rounded px-1.5 py-0.5 whitespace-nowrap ${pres.badge}`}>
                  {pres.label}
                </span>
              </div>

              <dl className="space-y-0.5 mt-2">
                <KV
                  label="active policy profile"
                  value={m.active_policy_profile_id}
                />
                <KV label="safety category" value={eff.safety_category} />
                <KV label="override policy" value={eff.override_policy} />
                <KV
                  label="decision pressure"
                  value={eff.decision_pressure}
                  danger={eff.decision_pressure === 'STOP'}
                />
                <KV
                  label="blocking reason"
                  value={eff.blocking_reason}
                  danger={!!eff.blocking_reason}
                />
                <KV
                  label="requires human review"
                  value={eff.requires_human_review ? 'yes' : null}
                  mono={false}
                />
                {/* Legacy c3_category mirror — useful for older audit tooling. */}
                <KV label="c3_category (legacy)" value={eff.c3_category} />
              </dl>

              {termGroups.length > 0 && (
                <div className="mt-2">
                  <div className="text-[10px] uppercase tracking-wide text-gray-500 mb-0.5">
                    matched term groups
                  </div>
                  <div className="flex flex-wrap gap-1">
                    {termGroups.map((g, i) => (
                      <span
                        key={i}
                        className="text-[10px] bg-gray-900/60 text-gray-300 border border-gray-800 rounded px-1.5 py-0.5 font-mono"
                        title={`group ${g.group_index}`}
                      >
                        [{g.group_index}] {g.matched_term}
                      </span>
                    ))}
                  </div>
                </div>
              )}

              <div className="mt-2">
                <div className="text-[10px] uppercase tracking-wide text-gray-500 mb-0.5">
                  matched facts
                </div>
                {factEntries.length === 0 ? (
                  <p className="text-[10px] text-gray-600 italic">
                    none — term-group rule (no typed facts bound). The
                    forthcoming bounded-control evaluator will populate
                    this dict for device-safety envelope checks.
                  </p>
                ) : (
                  <div className="space-y-0.5">
                    {factEntries.map(([k, v]) => (
                      <div key={k} className="flex justify-between text-[11px]">
                        <span className="text-gray-500 font-mono">{k}</span>
                        <span className="text-gray-300 font-mono">{String(v)}</span>
                      </div>
                    ))}
                  </div>
                )}
              </div>

              {eff.explanation && (
                <div className="mt-2 border-t border-gray-800 pt-1.5">
                  <div className="text-[10px] uppercase tracking-wide text-gray-500 mb-0.5">
                    effect summary
                  </div>
                  <p className="text-[11px] text-gray-400 leading-snug">
                    {eff.explanation}
                  </p>
                </div>
              )}

              <div className="mt-1.5 text-[10px] text-gray-600 italic">
                {pres.description}
              </div>
            </div>
          );
        })}
      </div>

      <p className="text-[10px] text-gray-600 mt-3 leading-snug border-t border-gray-800 pt-2">
        Hard safety MAY be detected by rules when the violation is a
        prompt-risk pattern (prompt injection, credential exposure,
        consumer-facing patient-specific prescribing). BUT bounded device
        safety MUST be evaluated against structured facts and validated
        envelopes — that work belongs to the deterministic bounded-control
        evaluator, not this term-group rule layer.
      </p>
    </div>
  );
}

export default function AuditCertificates() {
  const [limit, setLimit] = useState(20);
  const { data, refetch } = useApi(`/certificates?limit=${limit}`);
  const [selectedTc, setSelectedTc] = useState(null);
  const [tcDetail, setTcDetail] = useState(null);
  const [verifyResult, setVerifyResult] = useState(null);
  const [verifying, setVerifying] = useState(false);
  const [searchId, setSearchId] = useState('');
  const [selectedChain, setSelectedChain] = useState('');
  const [chainSummary, setChainSummary] = useState(null);
  const [chainVerifyResult, setChainVerifyResult] = useState(null);
  const [loadingChain, setLoadingChain] = useState(false);
  const { data: liveMetrics } = useApi('/metrics/live');

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

  const chainIds = liveMetrics?.chain_ids || [];

  const loadChainSummary = async (chainId) => {
    if (!chainId) { setChainSummary(null); return; }
    setLoadingChain(true);
    setChainVerifyResult(null);
    try {
      const summary = await apiFetch(`/certificates/chain/${chainId}/summary`);
      setChainSummary(summary);
    } catch {
      setChainSummary(null);
    }
    setLoadingChain(false);
  };

  const verifySelectedChain = async () => {
    if (!selectedChain) return;
    try {
      const result = await apiFetch(`/certificates/verify-chain?chain_id=${selectedChain}`);
      setChainVerifyResult(result);
    } catch (e) {
      setChainVerifyResult({ error: e.message });
    }
  };

  const TC_LAYERS = [
    { title: 'Layer I: Identity', fields: ['certificate_id', 'subject_id', 'subject_type', 'domain', 'risk_tier', 'action_class', 'policy_severity', 'checkpoint_id', 'gca_context_id', 'policy_set_id'] },
    { title: 'Layer S: Score', fields: ['s_base', 's_adjusted', 'tis_raw', 'tis_adjusted', 'tis_current', 'penalty_aggregate'] },
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
    { title: 'Standards Composer Audit', key: 'composer_metadata' },
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

      <div className="bg-gray-900 rounded-lg border border-gray-800 p-4">
        <h3 className="text-sm font-medium text-gray-400 mb-3">Chain Explorer</h3>
        <div className="flex gap-3 items-end">
          <div className="flex-1">
            <label className="text-xs text-gray-500">Select Chain</label>
            <select
              value={selectedChain}
              onChange={(e) => { setSelectedChain(e.target.value); loadChainSummary(e.target.value); }}
              className="w-full mt-1 bg-gray-800 border border-gray-700 rounded px-3 py-2 text-white text-sm"
            >
              <option value="">-- Select a chain --</option>
              {chainIds.map((cid) => (
                <option key={cid} value={cid}>{cid}</option>
              ))}
            </select>
          </div>
          <button
            onClick={verifySelectedChain}
            disabled={!selectedChain}
            className="bg-green-700 hover:bg-green-600 disabled:opacity-50 text-white px-4 py-2 rounded text-sm whitespace-nowrap"
          >
            Verify This Chain
          </button>
        </div>

        {loadingChain && <p className="text-gray-500 text-sm mt-3">Loading chain summary...</p>}

        {chainSummary && !loadingChain && (
          <div className="mt-3 grid grid-cols-2 md:grid-cols-4 gap-4">
            <div>
              <div className="text-xs text-gray-500">Chain Length</div>
              <div className="text-lg font-mono text-gray-300">{chainSummary.chain_length ?? '--'}</div>
            </div>
            <div>
              <div className="text-xs text-gray-500">First TC</div>
              <div className="text-xs font-mono text-gray-400">{chainSummary.first_timestamp ?? '--'}</div>
            </div>
            <div>
              <div className="text-xs text-gray-500">Last TC</div>
              <div className="text-xs font-mono text-gray-400">{chainSummary.last_timestamp ?? '--'}</div>
            </div>
            <div>
              <div className="text-xs text-gray-500">Verified</div>
              <div className={`text-lg font-bold ${chainSummary.verified ? 'text-green-400' : 'text-red-400'}`}>{chainSummary.verified != null ? (chainSummary.verified ? 'Yes' : 'No') : '--'}</div>
            </div>
            {chainSummary.decision_distribution && (
              <div className="col-span-2 md:col-span-4">
                <div className="text-xs text-gray-500 mb-1">Decision Distribution</div>
                <div className="flex gap-3">
                  {Object.entries(chainSummary.decision_distribution).map(([dec, cnt]) => (
                    <span key={dec} className="text-xs font-mono text-gray-400">{dec}: {cnt}</span>
                  ))}
                </div>
              </div>
            )}
          </div>
        )}

        {chainVerifyResult && (
          <div className={`mt-3 rounded p-2 border text-sm ${
            chainVerifyResult.chain_intact
              ? 'bg-green-900/30 border-green-700 text-green-400'
              : chainVerifyResult.error
                ? 'bg-red-900/30 border-red-700 text-red-400'
                : 'bg-red-900/30 border-red-700 text-red-400'
          }`}>
            {chainVerifyResult.error
              ? `Error: ${chainVerifyResult.error}`
              : `Chain Integrity: ${chainVerifyResult.chain_intact ? 'VERIFIED' : 'BROKEN'} | ${chainVerifyResult.tc_count ?? 0} TCs`
            }
          </div>
        )}
      </div>

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
                {/* Governance rule evidence — surfaces the rule layer that
                    influenced this decision, alongside the standards
                    composer audit and the rest of the TC layers. */}
                <GovernanceRuleMatches
                  matches={tcDetail.governance_rule_matches}
                />
              </div>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
