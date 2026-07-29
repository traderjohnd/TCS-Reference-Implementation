import { useState } from 'react';
import { useApi, apiFetch, usePolling } from '../hooks/useApi';
import StatusBadge from '../components/StatusBadge';
import PlainLanguageExplanation from '../components/PlainLanguageExplanation';
import {
  AdjustmentsPanel,
  C3ProvenancePanel,
  GateSummary,
  IntegrityWarning,
  RuleMatchesV2,
  ScoreTiersPanel,
  UnsupportedCertificate,
  VersionsPanel,
} from '../components/CertificateV2Sections';
import {
  displayGoverned,
  isFlatRuleMatch,
  normalizeCertificate,
} from '../lib/governedDecimal';

// Canonical BACK dimension order. Any dimension-keyed dict surfaced in
// the Decision Summary technical sections is reordered to this sequence
// so the display matches the spec (Boundedness, Attribution, Compliance,
// Known) regardless of the backend's JSON serialization order.
const BACK_ORDER = ['B', 'A', 'C', 'K'];
function reorderBack(dict) {
  if (!dict || typeof dict !== 'object') return dict;
  const out = {};
  for (const k of BACK_ORDER) {
    if (k in dict) out[k] = dict[k];
  }
  // Preserve any non-BACK keys at the end (defensive — current TCs
  // don't include extra keys but the dataclass schema doesn't forbid
  // them).
  for (const k of Object.keys(dict)) {
    if (!BACK_ORDER.includes(k)) out[k] = dict[k];
  }
  return out;
}
// Keys in the TC dict whose values are BACK-dimension dicts and should
// render in canonical BACK order.
const BACK_KEYED_TC_FIELDS = new Set([
  'component_scores',
  'component_weights',
  'gate_results',
  'thresholds',
]);

// Tiny pill used by the Hash Chain Walk to show per-row integrity
// outcomes (content hash, linkage, sequence). Green check for true,
// red cross for false. Pure presentation.
function Check({ label, ok }) {
  return (
    <span className={`inline-flex items-center gap-1 rounded px-1.5 py-0.5 border ${
      ok
        ? 'border-green-800 bg-green-900/30 text-green-300'
        : 'border-red-800 bg-red-900/30 text-red-300'
    }`}>
      <span className="font-mono">{ok ? '✓' : '✗'}</span>
      <span>{label}</span>
    </span>
  );
}

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
  //
  // tis-v2 certificates serialize the FLAT typed GovernanceRuleMatch
  // record (control_class etc. at top level, fact KEYS only, term-group
  // POSITIONS only). Legacy v1 certificates carry the nested `effect`
  // shape. Never use one shape's aliases as fallbacks for the other.
  if (matches.every(isFlatRuleMatch)) {
    return (
      <div className="border border-gray-800 rounded p-2">
        <div className="flex items-baseline justify-between mb-2">
          <h4 className="text-xs font-medium text-blue-400 uppercase tracking-wider">
            Governance Rule Matches ({matches.length})
          </h4>
        </div>
        <RuleMatchesV2 matches={matches} />
      </div>
    );
  }

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
  // Record-boundary normalization result for the selected TC:
  // { version, gateResult, integrity } — strict v1/v2/unsupported
  // dispatch, field-name-only integrity problems.
  const [tcNorm, setTcNorm] = useState(null);
  // The TC's linked ResponseArtifact (Phase 5 path). May be null for
  // TCs issued through the legacy /v2/govern path or for Phase 4 demo
  // TCs that pre-date the artifact tier. Used to surface the original
  // prompt above the plain-language summary.
  const [tcArtifact, setTcArtifact] = useState(null);
  // Override events for the selected TC, pulled from
  // /v2/govern/decisions/{tc_id}/override-history. Drives the
  // "Human override" section inside PlainLanguageExplanation.
  const [tcOverrides, setTcOverrides] = useState([]);
  // Collapsible technical 11-layer dump. Default closed so casual readers
  // see only the prompt + plain-language summary; auditors expand.
  const [technicalOpen, setTechnicalOpen] = useState(false);
  const [verifyResult, setVerifyResult] = useState(null);
  const [verifying, setVerifying] = useState(false);
  const [searchId, setSearchId] = useState('');
  const [selectedChain, setSelectedChain] = useState('');
  const [chainSummary, setChainSummary] = useState(null);
  const [chainVerifyResult, setChainVerifyResult] = useState(null);
  const [loadingChain, setLoadingChain] = useState(false);
  // Chain walk-through (examiner-grade hash-chain audit view). Loaded
  // when a chain is selected in the Chain Explorer.
  const [chainWalk, setChainWalk] = useState(null);
  const [loadingChainWalk, setLoadingChainWalk] = useState(false);
  const { data: liveMetrics } = useApi('/metrics/live');

  const certs = data?.certificates || [];

  const viewDetail = async (id) => {
    try {
      const tc = await apiFetch(`/certificates/${id}`);
      setTcDetail(tc);
      // Normalization failures never crash the view — a malformed
      // record renders an explicit integrity warning instead.
      try {
        setTcNorm(normalizeCertificate(tc));
      } catch {
        setTcNorm({
          version: 'unsupported',
          gateResult: null,
          integrity: { ok: false, problems: ['normalization_failed'] },
        });
      }
      setSelectedTc(id);
      setTechnicalOpen(false);
      // Try to fetch the linked artifact. For Phase 5 TCs issued via
      // /v2/query, tc.subject_id == artifact.artifact_id. For older
      // TCs the artifact endpoint will 404 and we leave the prompt
      // panel showing a "not available" message.
      setTcArtifact(null);
      if (tc?.subject_id) {
        try {
          const art = await apiFetch(`/artifacts/${tc.subject_id}`);
          setTcArtifact(art);
        } catch {
          setTcArtifact(null);
        }
      }
      // Override history: existing endpoint returns 200 with count=0
      // when the TC has no overrides (or is unknown), so no try/catch
      // needed for the empty case.
      try {
        const hist = await apiFetch(`/govern/decisions/${id}/override-history`);
        setTcOverrides(hist?.events || []);
      } catch {
        setTcOverrides([]);
      }
    } catch {
      setTcDetail(null);
      setTcNorm(null);
      setTcArtifact(null);
      setTcOverrides([]);
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
      try {
        setTcNorm(normalizeCertificate(tc));
      } catch {
        setTcNorm({
          version: 'unsupported',
          gateResult: null,
          integrity: { ok: false, problems: ['normalization_failed'] },
        });
      }
      setSelectedTc(searchId.trim());
    } catch {
      alert('Certificate not found');
    }
  };

  const chainIds = liveMetrics?.chain_ids || [];

  const loadChainSummary = async (chainId) => {
    if (!chainId) { setChainSummary(null); setChainWalk(null); return; }
    setLoadingChain(true);
    setChainVerifyResult(null);
    try {
      const summary = await apiFetch(`/certificates/chain/${chainId}/summary`);
      setChainSummary(summary);
    } catch {
      setChainSummary(null);
    }
    setLoadingChain(false);
    // Also fetch the full walk so the examiner-grade hash-chain
    // panel populates without an extra user click.
    setLoadingChainWalk(true);
    try {
      const walk = await apiFetch(`/certificates/chain/${chainId}/walk`);
      setChainWalk(walk);
    } catch {
      setChainWalk(null);
    }
    setLoadingChainWalk(false);
  };

  const copyWalkJson = async () => {
    if (!chainWalk) return;
    try {
      await navigator.clipboard.writeText(JSON.stringify(chainWalk, null, 2));
    } catch {
      // Clipboard API may be unavailable in some browser contexts;
      // silent failure is fine — the JSON is also visible inline.
    }
  };

  const downloadWalkCsv = () => {
    if (!chainWalk?.rows?.length) return;
    const header = [
      'chain_sequence', 'certificate_id', 'decision',
      'evaluation_timestamp', 'lifecycle_state',
      'tc_hash', 'previous_tc_hash',
      'content_hash_ok', 'linkage_ok', 'sequence_ok',
    ];
    const lines = [header.join(',')];
    for (const r of chainWalk.rows) {
      const cells = header.map((k) => {
        const v = r[k];
        if (v === null || v === undefined) return '';
        const s = String(v);
        // Escape commas and quotes for CSV safety.
        if (s.includes(',') || s.includes('"') || s.includes('\n')) {
          return `"${s.replace(/"/g, '""')}"`;
        }
        return s;
      });
      lines.push(cells.join(','));
    }
    const blob = new Blob([lines.join('\n')], { type: 'text/csv' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `chain-${chainWalk.chain_id}-walk.csv`;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
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
    // Gate uses the single gate_result vocabulary on v2; the legacy
    // gate_passed boolean appears only on v1 records (never as a
    // fallback for v2). The normalized PASS/FAIL banner above the
    // layers is derived once per record.
    { title: 'Layer G: Gate', fields: ['gate_result', 'gate_passed', 'blocking_reason', 'failure_mode'] },
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
          <p className="text-[10px] text-gray-500 mt-1 font-normal">
            Historical verification — each record is verified under its
            own recorded calculation semantics (legacy certificates
            through the frozen v1 path, tis-v2 certificates through the
            v2 path). A pass here means the original decision record is
            reproduced as issued.
          </p>
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

      {/* ── Examiner-grade Hash Chain Walk ─────────────────────────────── */}
      {selectedChain && (
        <div className="bg-gray-900 rounded-lg border border-gray-800 p-4">
          <div className="flex items-baseline justify-between mb-3 gap-3 flex-wrap">
            <div>
              <h3 className="text-sm font-medium text-gray-300">
                Hash Chain Walk —{' '}
                <span className="font-mono text-xs text-gray-500">{selectedChain}</span>
              </h3>
              <p className="text-[11px] text-gray-500 mt-0.5">
                Examiner-grade walk-through. Every TC in the chain in
                sequence, with content-hash, linkage, and sequence
                integrity checks per row. Read-only.
              </p>
            </div>
            {chainWalk?.rows?.length > 0 && (
              <div className="flex items-center gap-2">
                <button
                  onClick={copyWalkJson}
                  className="text-xs bg-gray-800 hover:bg-gray-700 border border-gray-700 text-gray-300 px-3 py-1.5 rounded"
                  title="Copy the full walk payload as JSON"
                >
                  Copy JSON
                </button>
                <button
                  onClick={downloadWalkCsv}
                  className="text-xs bg-gray-800 hover:bg-gray-700 border border-gray-700 text-gray-300 px-3 py-1.5 rounded"
                  title="Download the walk as CSV for offline audit"
                >
                  Download CSV
                </button>
              </div>
            )}
          </div>

          {loadingChainWalk && (
            <p className="text-xs text-gray-500 italic">Loading chain walk…</p>
          )}

          {!loadingChainWalk && chainWalk && chainWalk.count === 0 && (
            <p className="text-xs text-gray-500 italic">
              No TCs in this chain.
            </p>
          )}

          {!loadingChainWalk && chainWalk && chainWalk.count > 0 && (
            <>
              {/* Top-level chain status banner */}
              <div className={`mb-3 rounded border px-3 py-2 text-sm flex items-center justify-between ${
                chainWalk.chain_intact
                  ? 'bg-green-900/20 border-green-800 text-green-300'
                  : 'bg-red-900/20 border-red-800 text-red-300'
              }`}>
                <span>
                  Chain integrity:{' '}
                  <span className="font-semibold">
                    {chainWalk.chain_intact ? 'VERIFIED ✓' : 'BROKEN ✗'}
                  </span>
                </span>
                <span className="text-xs text-gray-400 font-mono">
                  {chainWalk.count} TC{chainWalk.count === 1 ? '' : 's'}
                </span>
              </div>

              {/* Per-TC walk */}
              <div className="space-y-3">
                {chainWalk.rows.map((row, idx) => {
                  const rowOk = row.content_hash_ok && row.linkage_ok && row.sequence_ok;
                  const isFirst = idx === 0;
                  return (
                    <div key={row.certificate_id || idx}>
                      <div className={`border rounded p-3 ${
                        rowOk
                          ? 'bg-gray-800/40 border-gray-700'
                          : 'bg-red-900/15 border-red-800'
                      }`}>
                        <div className="flex items-baseline justify-between gap-3 flex-wrap mb-2">
                          <div className="flex items-baseline gap-2">
                            <span className="text-[11px] uppercase tracking-wide text-gray-500 font-mono">
                              # {row.chain_sequence ?? '?'}
                            </span>
                            <button
                              onClick={() => viewDetail(row.certificate_id)}
                              className="text-sm font-mono text-blue-300 hover:text-blue-200 underline-offset-2 hover:underline"
                              title="Open TC Detail"
                            >
                              {(row.certificate_id || '').slice(0, 14)}…
                            </button>
                            <StatusBadge decision={row.decision} />
                          </div>
                          <div className="text-[11px] text-gray-500 font-mono">
                            {row.evaluation_timestamp}
                            {row.lifecycle_state ? <> · {row.lifecycle_state}</> : null}
                          </div>
                        </div>

                        <div className="grid grid-cols-1 md:grid-cols-2 gap-2 text-[11px]">
                          <div>
                            <div className="text-gray-500">tc_hash</div>
                            <div className="font-mono text-gray-300 break-all">
                              {row.tc_hash || '(missing)'}
                            </div>
                          </div>
                          <div>
                            <div className="text-gray-500">previous_tc_hash</div>
                            <div className="font-mono text-gray-300 break-all">
                              {isFirst
                                ? <span className="italic text-gray-500">(none — chain start)</span>
                                : row.previous_tc_hash || '(missing)'}
                            </div>
                          </div>
                        </div>

                        <div className="mt-2 flex flex-wrap gap-2 text-[10px]">
                          <Check label="content hash"      ok={row.content_hash_ok} />
                          <Check label="linkage"           ok={row.linkage_ok} />
                          <Check label="sequence"          ok={row.sequence_ok} />
                        </div>
                      </div>

                      {/* Arrow between rows visualizing the linkage */}
                      {idx < chainWalk.rows.length - 1 && (
                        <div className="flex items-center justify-center my-1 text-gray-600 text-xs font-mono">
                          │ previous_tc_hash ▼
                        </div>
                      )}
                    </div>
                  );
                })}
              </div>
            </>
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
                  <span className="text-gray-500 font-mono text-xs">{displayGoverned(tc.tis_current)}</span>
                </div>
              ))}
            </div>
          </div>
        </div>

        {selectedTc && tcDetail && (
          <div className="w-1/2">
            <div className="bg-gray-900 rounded-lg border border-gray-800 p-4 sticky top-4">
              <div className="flex justify-between items-center mb-3">
                <div className="flex items-center gap-2">
                  <StatusBadge decision={tcDetail.decision} />
                  <h3 className="text-sm font-medium text-white">Decision Summary</h3>
                </div>
                <button onClick={() => { setSelectedTc(null); setTcDetail(null); setTcNorm(null); setTcArtifact(null); setTcOverrides([]); }}
                  className="text-gray-400 hover:text-white">&times;</button>
              </div>
              <div className="space-y-3 max-h-[75vh] overflow-y-auto">
                {/* ── ⓪ Strict version dispatch / integrity state ──────── */}
                {tcNorm?.version === 'unsupported' && (
                  <UnsupportedCertificate
                    version={tcDetail.certificate_schema_version}
                  />
                )}
                {tcNorm && tcNorm.version !== 'unsupported'
                  && !tcNorm.integrity.ok && (
                  <IntegrityWarning problems={tcNorm.integrity.problems} />
                )}
                {tcNorm && tcNorm.version !== 'unsupported' && (
                  <div className="flex items-center justify-between text-xs px-1">
                    <GateSummary gateResult={tcNorm.gateResult} />
                    <span className="text-gray-500 font-mono">
                      schema v{tcNorm.version}
                      {tcNorm.version === 2
                        ? ` · ${tcDetail.calculation_version || '—'}`
                        : ' · legacy'}
                    </span>
                  </div>
                )}

                {/* ── ① What was asked ─────────────────────────────────── */}
                <div className="bg-gray-900 border border-gray-700 rounded-lg p-3">
                  <div className="text-[11px] uppercase tracking-wide text-gray-500 mb-1.5">
                    What was asked
                  </div>
                  {tcArtifact?.prompt ? (
                    <blockquote className="text-sm text-gray-200 italic border-l-2 border-gray-700 pl-3 whitespace-pre-wrap leading-relaxed">
                      “{tcArtifact.prompt}”
                    </blockquote>
                  ) : tcArtifact?.raw_output && tcArtifact?.generation_mode === 'human_composed' ? (
                    <div>
                      <div className="text-[10px] text-gray-500 mb-1">Human-composed draft (no prompt)</div>
                      <blockquote className="text-sm text-gray-200 italic border-l-2 border-gray-700 pl-3 whitespace-pre-wrap leading-relaxed">
                        “{tcArtifact.raw_output}”
                      </blockquote>
                    </div>
                  ) : (
                    <p className="text-xs text-gray-500 italic">
                      Prompt not available — this certificate predates the
                      artifact tier or was issued through a non-artifact path.
                    </p>
                  )}
                </div>

                {/* ── ② Plain-language summary ─────────────────────────── */}
                <PlainLanguageExplanation
                  tc={tcDetail}
                  prompt={tcArtifact?.prompt || (tcArtifact?.generation_mode === 'human_composed' ? tcArtifact?.raw_output : null)}
                  overrides={tcOverrides}
                />

                {/* ── ③ Collapsible technical detail ───────────────────── */}
                <div className="border border-gray-800 rounded">
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
                    <div className="space-y-3 p-2 border-t border-gray-800">
                      {tcNorm && tcNorm.version !== 'unsupported' && (
                        <VersionsPanel tc={tcDetail} version={tcNorm.version} />
                      )}
                      {tcNorm?.version === 2 && (
                        <>
                          <ScoreTiersPanel tc={tcDetail} />
                          <AdjustmentsPanel
                            adjustments={tcDetail.adjustments_applied}
                          />
                          <C3ProvenancePanel tc={tcDetail} />
                        </>
                      )}
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
                              {JSON.stringify(
                                BACK_KEYED_TC_FIELDS.has(key)
                                  ? reorderBack(tcDetail[key])
                                  : tcDetail[key],
                                null,
                                2,
                              )}
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
                  )}
                </div>
              </div>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
