import { useEffect, useMemo, useState } from 'react';
import { useApi, usePolling, apiPost } from '../hooks/useApi';
import { useAuth } from '../hooks/useAuth';

// ─── Standards Library (read-only catalog) ──────────────────────────────────
//
// Browse every standard the composer can pull from. Read-only: no deploy
// actions here. The composer below is where standards get assembled into a
// deployable pack. Important framing: TCS parameter adjustments are this
// implementation's GOVERNANCE INTERPRETATION of the named standard, not a
// claim that the regulation itself mathematically prescribes specific
// parameter values. That language is enforced both in the standard's
// control_interpretation field and in the section header below.

function StandardsLibrary() {
  const { data, error } = useApi('/standards/library');
  const [expandedId, setExpandedId] = useState(null);
  const [open, setOpen] = useState(false);

  const standards = data?.standards || [];
  const total = data?.total ?? standards.length;

  // Group standards by industry for the read-only browse — same taxonomy
  // the composer uses so reviewers can correlate easily.
  const grouped = useMemo(() => {
    const out = {};
    for (const s of standards) {
      const k = s.industry || 'other';
      if (!out[k]) out[k] = [];
      out[k].push(s);
    }
    return out;
  }, [standards]);

  return (
    <div className="bg-gray-900 rounded-lg border border-gray-800 p-4 space-y-3">
      <button
        onClick={() => setOpen((v) => !v)}
        className="w-full text-left flex items-center justify-between"
      >
        <div>
          <h3 className="text-sm font-medium text-gray-300">
            Standards Library{' '}
            <span className="text-gray-500 font-normal">({total})</span>
          </h3>
          <p className="text-xs text-gray-500 mt-1">
            Read-only catalog. Under this TCS policy profile, each standard
            is interpreted as emphasizing the named controls — this is a
            governance interpretation, not a regulatory claim about
            mathematical prescription.
          </p>
        </div>
        <span className="text-gray-500 text-xs">
          {open ? 'Hide ▴' : 'Show ▾'}
        </span>
      </button>

      {open && error && (
        <p className="text-xs text-red-400">Failed to load: {error.message}</p>
      )}

      {open && !error && standards.length === 0 && (
        <p className="text-xs text-gray-500">Loading standards…</p>
      )}

      {open && Object.entries(grouped).map(([industry, items]) => (
        <div key={industry}>
          <div className="text-[10px] uppercase tracking-wide text-gray-500 mb-1.5">
            {industry.replace(/_/g, ' ')} ({items.length})
          </div>
          <div className="space-y-1.5">
            {items.map((s) => {
              const expanded = expandedId === s.id;
              return (
                <div
                  key={s.id}
                  className="bg-gray-800/40 border border-gray-800 rounded"
                >
                  <button
                    onClick={() => setExpandedId(expanded ? null : s.id)}
                    className="w-full text-left px-3 py-2 hover:bg-gray-800/60 transition-colors"
                  >
                    <div className="flex items-start justify-between gap-3">
                      <div className="flex-1 min-w-0">
                        <div className="text-sm text-gray-200">{s.name}</div>
                        <div className="text-[10px] font-mono text-gray-500 mt-0.5">
                          {s.regulatory_reference}{' '}
                          <span className="text-gray-600">
                            · {s.sub_industry}
                          </span>
                        </div>
                      </div>
                      <span className="text-[10px] text-gray-500 mt-0.5">
                        {expanded ? '▴' : '▾'}
                      </span>
                    </div>
                  </button>
                  {expanded && (
                    <div className="border-t border-gray-800 px-3 py-2 space-y-2">
                      <div>
                        <div className="text-[10px] uppercase tracking-wide text-gray-500 mb-1">
                          Control interpretation under this profile
                        </div>
                        <p className="text-[11px] text-gray-300 leading-snug">
                          {s.control_interpretation ||
                            '(no interpretation note recorded)'}
                        </p>
                      </div>
                      {s.applies_to_use_cases?.length > 0 && (
                        <div>
                          <div className="text-[10px] uppercase tracking-wide text-gray-500 mb-1">
                            Applies to use cases
                          </div>
                          <div className="flex flex-wrap gap-1">
                            {s.applies_to_use_cases.map((u) => (
                              <span
                                key={u}
                                className="text-[10px] bg-gray-900/60 text-gray-400 border border-gray-800 rounded px-1.5 py-0.5 font-mono"
                              >
                                {u}
                              </span>
                            ))}
                          </div>
                        </div>
                      )}
                      <div className="text-[10px] text-gray-600 font-mono">
                        id: {s.id}
                      </div>
                    </div>
                  )}
                </div>
              );
            })}
          </div>
        </div>
      ))}
    </div>
  );
}

// ─── Standards Composer ─────────────────────────────────────────────────────
//
// Drill-down: Industry > Sub-industry > Use case > Standards > Risk > Action
// Live preview of the composed profile and per-standard contributions.
// Deploy registers + activates the composed pack via the existing Pack system.

function StandardsComposer({ onDeployed, hasEditAccess }) {
  const { data: taxonomyData } = useApi('/standards/taxonomy');
  const taxonomy = taxonomyData?.taxonomy || {};

  const [industry, setIndustry] = useState('');
  const [subIndustry, setSubIndustry] = useState('');
  const [useCase, setUseCase] = useState('');
  const [selectedStandards, setSelectedStandards] = useState([]);
  const [riskTier, setRiskTier] = useState('r3');
  const [actionClass, setActionClass] = useState('a4');
  const [eligibleStandards, setEligibleStandards] = useState([]);
  const [preview, setPreview] = useState(null);
  const [previewError, setPreviewError] = useState(null);
  const [previewLoading, setPreviewLoading] = useState(false);
  const [deploying, setDeploying] = useState(false);
  const [deployMsg, setDeployMsg] = useState(null);
  const [showAdjustments, setShowAdjustments] = useState(false);
  // Custom pack name. User-typed overrides; otherwise we suggest one
  // built from the selections so the user can edit or accept it.
  const [packName, setPackName] = useState('');
  const [packNameTouched, setPackNameTouched] = useState(false);

  const industryOptions = Object.entries(taxonomy);
  const subIndustryOptions = industry
    ? Object.entries(taxonomy[industry]?.sub_industries || {})
    : [];
  const useCaseOptions = subIndustry
    ? Object.entries(taxonomy[industry]?.sub_industries?.[subIndustry]?.use_cases || {})
    : [];

  // Cascading reset
  useEffect(() => { setSubIndustry(''); }, [industry]);
  useEffect(() => { setUseCase(''); }, [subIndustry]);
  useEffect(() => { setSelectedStandards([]); setEligibleStandards([]); setPreview(null); }, [useCase]);

  // Load eligible standards when a use case is picked
  useEffect(() => {
    if (!useCase) return;
    let cancelled = false;
    (async () => {
      try {
        const params = new URLSearchParams({ use_case: useCase });
        const r = await fetch(`/v2/standards/library?${params.toString()}`, { credentials: 'include' });
        const j = await r.json();
        if (!cancelled) setEligibleStandards(j.standards || []);
      } catch (e) {
        if (!cancelled) setEligibleStandards([]);
      }
    })();
    return () => { cancelled = true; };
  }, [useCase]);

  // Live preview as selections change
  useEffect(() => {
    if (!industry || !subIndustry || !useCase) { setPreview(null); return; }
    let cancelled = false;
    setPreviewLoading(true);
    setPreviewError(null);
    (async () => {
      try {
        const res = await apiPost('/standards/compose', {
          industry, sub_industry: subIndustry, use_case: useCase,
          standard_ids: selectedStandards,
          risk_tier: riskTier, action_class: actionClass,
        });
        if (!cancelled) setPreview(res.composed);
      } catch (e) {
        if (!cancelled) { setPreviewError(e.message); setPreview(null); }
      } finally {
        if (!cancelled) setPreviewLoading(false);
      }
    })();
    return () => { cancelled = true; };
  }, [industry, subIndustry, useCase, selectedStandards, riskTier, actionClass]);

  const toggleStandard = (id) => {
    setSelectedStandards((prev) =>
      prev.includes(id) ? prev.filter((x) => x !== id) : [...prev, id]
    );
  };

  // Suggested name from the current selections. Used as a placeholder
  // and pre-filled value the user can edit. Once the user types into
  // the name field we stop overwriting it.
  const subIndustryName = subIndustry
    ? taxonomy[industry]?.sub_industries?.[subIndustry]?.name
    : '';
  const useCaseName = useCase
    ? taxonomy[industry]?.sub_industries?.[subIndustry]?.use_cases?.[useCase]
    : '';
  const stdAbbrevs = selectedStandards
    .map((sid) => {
      const m = eligibleStandards.find((s) => s.id === sid);
      if (!m) return sid;
      // Try to extract a short token before the em-dash, e.g. "ISO 13485"
      const dashIdx = m.name.indexOf('—');
      return dashIdx > 0 ? m.name.slice(0, dashIdx).trim() : m.name;
    })
    .join(' + ');
  const suggestedName = useCase
    ? [
        subIndustryName,
        useCaseName,
        stdAbbrevs || 'no standards',
        `${riskTier}/${actionClass}`,
      ].filter(Boolean).join(' — ')
    : '';

  useEffect(() => {
    if (!packNameTouched) setPackName(suggestedName);
  }, [suggestedName, packNameTouched]);

  const deploy = async () => {
    if (!industry || !subIndustry || !useCase) return;
    setDeploying(true);
    setDeployMsg(null);
    try {
      const r = await apiPost('/standards/deploy', {
        industry, sub_industry: subIndustry, use_case: useCase,
        standard_ids: selectedStandards,
        risk_tier: riskTier, action_class: actionClass,
        pack_name: (packName || '').trim() || null,
      });
      setDeployMsg({ type: 'success', text: `Deployed "${r.pack_name}" (${r.pack_id})` });
      onDeployed?.();
    } catch (e) {
      setDeployMsg({ type: 'error', text: `Deploy failed: ${e.message}` });
    } finally {
      setDeploying(false);
    }
  };

  const canDeploy = hasEditAccess && industry && subIndustry && useCase && !deploying;
  const pc = preview?.profile_config;

  return (
    <div className="bg-gray-900 rounded-lg border border-gray-800 p-4 space-y-4">
      <div>
        <h3 className="text-sm font-medium text-gray-300">Standards Composer</h3>
        <p className="text-xs text-gray-500 mt-1">
          Compose a deployable policy profile from regulatory and industry standards.
          Standard adjustments are this implementation's governance interpretation
          (not a claim that the underlying regulations mathematically require specific
          TCS parameter values).
        </p>
      </div>

      {/* Drill-down cascade */}
      <div className="grid grid-cols-1 md:grid-cols-3 gap-3">
        <div>
          <label className="text-[11px] uppercase tracking-wide text-gray-500">Industry</label>
          <select value={industry} onChange={(e) => setIndustry(e.target.value)}
            className="w-full mt-1 bg-gray-800 border border-gray-700 rounded px-2 py-1.5 text-sm text-white">
            <option value="">— select —</option>
            {industryOptions.map(([k, v]) => (
              <option key={k} value={k}>{v.name}</option>
            ))}
          </select>
        </div>
        <div>
          <label className="text-[11px] uppercase tracking-wide text-gray-500">Sub-industry</label>
          <select value={subIndustry} onChange={(e) => setSubIndustry(e.target.value)}
            disabled={!industry}
            className="w-full mt-1 bg-gray-800 border border-gray-700 rounded px-2 py-1.5 text-sm text-white disabled:opacity-50">
            <option value="">— select —</option>
            {subIndustryOptions.map(([k, v]) => (
              <option key={k} value={k}>{v.name}</option>
            ))}
          </select>
        </div>
        <div>
          <label className="text-[11px] uppercase tracking-wide text-gray-500">Use case</label>
          <select value={useCase} onChange={(e) => setUseCase(e.target.value)}
            disabled={!subIndustry}
            className="w-full mt-1 bg-gray-800 border border-gray-700 rounded px-2 py-1.5 text-sm text-white disabled:opacity-50">
            <option value="">— select —</option>
            {useCaseOptions.map(([k, v]) => (
              <option key={k} value={k}>{v}</option>
            ))}
          </select>
        </div>
      </div>

      {/* Standards multi-select */}
      {useCase && (
        <div>
          <label className="text-[11px] uppercase tracking-wide text-gray-500">
            Applicable standards ({eligibleStandards.length})
          </label>
          {eligibleStandards.length === 0 ? (
            <p className="text-xs text-gray-600 mt-1">No standards found for this use case.</p>
          ) : (
            <div className="mt-2 space-y-1.5 max-h-72 overflow-y-auto pr-2">
              {eligibleStandards.map((s) => {
                const selected = selectedStandards.includes(s.id);
                return (
                  <button
                    key={s.id}
                    onClick={() => toggleStandard(s.id)}
                    className={`w-full text-left rounded border px-3 py-2 transition-colors ${
                      selected
                        ? 'border-blue-600 bg-blue-900/20'
                        : 'border-gray-800 bg-gray-800/40 hover:border-gray-700'
                    }`}
                  >
                    <div className="flex items-start gap-2">
                      <span className={`mt-0.5 w-3.5 h-3.5 rounded border flex items-center justify-center text-[10px] ${
                        selected ? 'bg-blue-600 border-blue-500 text-white' : 'border-gray-600'
                      }`}>{selected ? '✓' : ''}</span>
                      <div className="flex-1 min-w-0">
                        <div className="text-sm text-gray-200">{s.name}</div>
                        <div className="text-[10px] font-mono text-gray-500 mt-0.5">{s.regulatory_reference}</div>
                        <div className="text-[11px] text-gray-400 mt-1 leading-snug">
                          {s.control_interpretation}
                        </div>
                      </div>
                    </div>
                  </button>
                );
              })}
            </div>
          )}
        </div>
      )}

      {/* Risk tier + action class */}
      {useCase && (
        <div className="grid grid-cols-2 gap-3">
          <div>
            <label className="text-[11px] uppercase tracking-wide text-gray-500">Risk tier</label>
            <select value={riskTier} onChange={(e) => setRiskTier(e.target.value)}
              className="w-full mt-1 bg-gray-800 border border-gray-700 rounded px-2 py-1.5 text-sm text-white">
              <option value="r1">r1 — Low</option>
              <option value="r2">r2 — Medium</option>
              <option value="r3">r3 — High</option>
            </select>
          </div>
          <div>
            <label className="text-[11px] uppercase tracking-wide text-gray-500">Action class</label>
            <select value={actionClass} onChange={(e) => setActionClass(e.target.value)}
              className="w-full mt-1 bg-gray-800 border border-gray-700 rounded px-2 py-1.5 text-sm text-white">
              <option value="a1">a1 — Informational</option>
              <option value="a2">a2 — Advisory</option>
              <option value="a3">a3 — Operational</option>
              <option value="a4">a4 — Regulated decision</option>
            </select>
          </div>
        </div>
      )}

      {/* Composed profile preview */}
      {previewLoading && <p className="text-xs text-gray-500">Composing preview…</p>}
      {previewError && (
        <div className="text-xs bg-red-900/30 border border-red-800 rounded p-2 text-red-300">
          {previewError}
        </div>
      )}
      {pc && (
        <div className="bg-gray-800/50 border border-gray-700 rounded p-3 space-y-3">
          <div className="flex items-baseline justify-between">
            <div>
              <div className="text-[11px] uppercase tracking-wide text-gray-500">Composed profile preview</div>
              <div className="text-sm text-gray-200 mt-0.5 font-mono">{pc.profile_id}</div>
            </div>
            <div className="text-[10px] font-mono text-gray-500" title={preview.profile_hash}>
              hash {preview.profile_hash.slice(0, 12)}…
            </div>
          </div>

          {/* Dimension weights + thresholds + gate */}
          <div className="grid grid-cols-4 gap-2 text-xs">
            {['B', 'A', 'C', 'K'].map((dim) => {
              const w = pc.weights[dim];
              const t = pc.thresholds[dim];
              const inGate = pc.gate_set.includes(dim);
              return (
                <div key={dim} className="bg-gray-900/50 rounded p-2">
                  <div className="flex items-baseline justify-between mb-1">
                    <span className="font-mono font-semibold text-gray-200">{dim}</span>
                    {inGate && (
                      <span className="text-[10px] bg-blue-900/40 text-blue-300 border border-blue-800 rounded px-1.5">gate</span>
                    )}
                  </div>
                  <div className="text-[10px] text-gray-500">weight</div>
                  <div className="font-mono text-gray-300">{(w ?? 0).toFixed(4)}</div>
                  <div className="text-[10px] text-gray-500 mt-1">threshold</div>
                  <div className={`font-mono ${inGate ? 'text-gray-300' : 'text-gray-500'}`}>{(t ?? 0).toFixed(4)}</div>
                </div>
              );
            })}
          </div>

          {/* Penalty weights */}
          <div>
            <div className="text-[10px] uppercase tracking-wide text-gray-500 mb-1">Penalty weights (Σ=1.0)</div>
            <div className="flex gap-2 text-[11px]">
              {Object.entries(pc.penalty_weights).map(([k, v]) => (
                <span key={k} className="bg-gray-900/50 rounded px-2 py-0.5">
                  <span className="font-mono text-gray-300">{k}</span>
                  <span className="text-gray-500 ml-1">{(v ?? 0).toFixed(3)}</span>
                </span>
              ))}
            </div>
          </div>

          {/* Required controls + hard prohibitions (union) */}
          {(preview.required_controls?.length || preview.hard_prohibitions?.length) ? (
            <div className="grid grid-cols-2 gap-3 text-xs">
              {preview.required_controls?.length > 0 && (
                <div>
                  <div className="text-[10px] uppercase tracking-wide text-gray-500 mb-1">Required controls (OR-union)</div>
                  <ul className="space-y-0.5 text-gray-400">
                    {preview.required_controls.map((c) => <li key={c} className="font-mono">• {c}</li>)}
                  </ul>
                </div>
              )}
              {preview.hard_prohibitions?.length > 0 && (
                <div>
                  <div className="text-[10px] uppercase tracking-wide text-gray-500 mb-1">Hard prohibitions (union)</div>
                  <ul className="space-y-0.5 text-red-400">
                    {preview.hard_prohibitions.map((c) => <li key={c} className="font-mono">⛔ {c}</li>)}
                  </ul>
                </div>
              )}
            </div>
          ) : null}

          {/* View adjustments per standard (read-only) */}
          {preview.contributions?.length > 0 && (
            <div className="border-t border-gray-800 pt-2">
              <button onClick={() => setShowAdjustments((v) => !v)}
                className="text-xs text-gray-400 hover:text-white flex items-center gap-1.5">
                <span className="text-gray-600">{showAdjustments ? '▾' : '▸'}</span>
                <span>View adjustments per standard ({preview.contributions.length})</span>
              </button>
              {showAdjustments && (
                <div className="mt-2 space-y-2">
                  {preview.contributions.map((c) => {
                    const hasContent = (
                      Object.keys(c.threshold_floors_applied || {}).length ||
                      Object.keys(c.threshold_floors_overridden || {}).length ||
                      c.gate_dimensions_added?.length ||
                      Object.keys(c.weight_deltas_applied || {}).length ||
                      Object.keys(c.penalty_weight_deltas_applied || {}).length ||
                      c.required_controls_added?.length ||
                      c.hard_prohibitions_added?.length
                    );
                    if (!hasContent) return null;
                    return (
                      <div key={c.standard_id} className="bg-gray-900/50 border border-gray-800 rounded p-2 text-[11px]">
                        <div className="font-semibold text-gray-200 mb-1">{c.standard_name}</div>
                        {Object.keys(c.threshold_floors_applied).length > 0 && (
                          <div className="text-gray-400">
                            <span className="text-gray-500">strictest threshold floors applied:</span>{' '}
                            <span className="font-mono">{JSON.stringify(c.threshold_floors_applied)}</span>
                          </div>
                        )}
                        {Object.keys(c.threshold_floors_overridden).length > 0 && (
                          <div className="text-gray-500">
                            <span>thresholds requested but a stricter standard won:</span>{' '}
                            <span className="font-mono">{JSON.stringify(c.threshold_floors_overridden)}</span>
                          </div>
                        )}
                        {c.gate_dimensions_added?.length > 0 && (
                          <div className="text-gray-400">
                            <span className="text-gray-500">added to gate:</span>{' '}
                            <span className="font-mono">{c.gate_dimensions_added.join(', ')}</span>
                          </div>
                        )}
                        {Object.keys(c.weight_deltas_applied).length > 0 && (
                          <div className="text-gray-400">
                            <span className="text-gray-500">weight deltas (re-normalized):</span>{' '}
                            <span className="font-mono">{JSON.stringify(c.weight_deltas_applied)}</span>
                          </div>
                        )}
                        {Object.keys(c.penalty_weight_deltas_applied).length > 0 && (
                          <div className="text-gray-400">
                            <span className="text-gray-500">penalty deltas:</span>{' '}
                            <span className="font-mono">{JSON.stringify(c.penalty_weight_deltas_applied)}</span>
                          </div>
                        )}
                        {c.required_controls_added?.length > 0 && (
                          <div className="text-gray-400">
                            <span className="text-gray-500">controls added:</span>{' '}
                            <span className="font-mono">{c.required_controls_added.join(', ')}</span>
                          </div>
                        )}
                        {c.hard_prohibitions_added?.length > 0 && (
                          <div className="text-red-400">
                            <span className="text-gray-500">prohibitions added:</span>{' '}
                            <span className="font-mono">{c.hard_prohibitions_added.join(', ')}</span>
                          </div>
                        )}
                      </div>
                    );
                  })}
                </div>
              )}
            </div>
          )}
        </div>
      )}

      {/* Pack name + Deploy */}
      {useCase && (
        <div className="pt-1 space-y-2">
          <div>
            <label className="text-[11px] uppercase tracking-wide text-gray-500">
              Custom pack name
              {!packNameTouched && packName && (
                <span className="ml-2 text-gray-600 normal-case tracking-normal">
                  (suggested from your selections — edit to customize)
                </span>
              )}
            </label>
            <input
              value={packName}
              onChange={(e) => { setPackName(e.target.value); setPackNameTouched(true); }}
              onFocus={() => setPackNameTouched(true)}
              placeholder={suggestedName}
              className="w-full mt-1 bg-gray-800 border border-gray-700 rounded px-3 py-2 text-sm text-white placeholder-gray-600"
            />
            <p className="text-[10px] text-gray-600 mt-1">
              Shown in Active Profile and Available Packs. The pack_id (hash-based)
              is deterministic across same compositions; the name is yours to edit.
            </p>
          </div>
          <div className="flex items-center gap-3">
            <button
              onClick={deploy}
              disabled={!canDeploy}
              className="bg-blue-700 hover:bg-blue-600 disabled:opacity-40 text-white px-4 py-2 rounded text-sm font-medium transition-colors"
            >
              {deploying ? 'Deploying…' : 'Compose & deploy as active pack'}
            </button>
            {deployMsg && (
              <span className={`text-xs ${deployMsg.type === 'success' ? 'text-green-400' : 'text-red-400'}`}>
                {deployMsg.text}
              </span>
            )}
            {!hasEditAccess && (
              <span className="text-xs text-gray-500">read-only — your role cannot deploy</span>
            )}
          </div>
        </div>
      )}
    </div>
  );
}

// ─── Original PolicyControls (preserved below the composer) ─────────────────

export default function PolicyControls() {
  const { hasEditAccess } = useAuth();
  const { data: activePack, refetch: refetchActive } = usePolling('/packs/active', 10000);
  const { data: packs, refetch: refetchPacks } = useApi('/packs');
  const { data: pllHistory } = useApi('/dynamics/pll/history');
  const [simResult, setSimResult] = useState(null);
  const [simLoading, setSimLoading] = useState(false);
  const [simProfile, setSimProfile] = useState('');
  const [deployingId, setDeployingId] = useState(null);
  const [deployMsg, setDeployMsg] = useState(null);

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

  const handlePackDeployed = () => {
    refetchActive();
    refetchPacks();
  };

  return (
    <div className="space-y-6">
      <StandardsLibrary />
      <StandardsComposer onDeployed={handlePackDeployed} hasEditAccess={hasEditAccess()} />

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
          {deployMsg && (
            <div className={`text-xs mb-3 px-3 py-2 rounded ${deployMsg.type === 'success' ? 'bg-green-900/50 text-green-400 border border-green-800' : 'bg-red-900/50 text-red-400 border border-red-800'}`}>
              {deployMsg.text}
            </div>
          )}
          <div className="space-y-2">
            {packList.map((p) => (
              <div key={p.pack_id} className="bg-gray-800/50 rounded p-3 flex justify-between items-center">
                <div>
                  <div className="text-sm text-gray-300">{p.name}</div>
                  <div className="text-xs text-gray-500">{p.pack_id} v{p.version}</div>
                </div>
                {hasEditAccess() && (
                  <button
                    disabled={deployingId === p.pack_id}
                    onClick={async () => {
                      setDeployingId(p.pack_id);
                      setDeployMsg(null);
                      try {
                        await apiPost(`/packs/${p.pack_id}/deploy`, {});
                        setDeployMsg({ type: 'success', text: `Deployed ${p.name || p.pack_id}` });
                        refetchActive();
                      } catch (err) {
                        setDeployMsg({ type: 'error', text: `Deploy failed: ${err.message}` });
                      } finally {
                        setDeployingId(null);
                      }
                    }}
                    className="text-xs bg-blue-700 hover:bg-blue-600 disabled:opacity-50 text-white px-3 py-1 rounded"
                  >
                    {deployingId === p.pack_id ? 'Deploying...' : 'Deploy'}
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
