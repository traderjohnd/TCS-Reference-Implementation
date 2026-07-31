import { Component, useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import { apiFetch, apiPost } from '../hooks/useApi';
import { useConnections } from '../hooks/useConnections';
import { useOperatingMode } from '../hooks/useOperatingMode';
import { displayGoverned } from '../lib/governedDecimal';

// Commit 6 (demo-live branch): the guided investor demonstration.
// Deterministic scripted scenarios (Allow / Hold / Stop / Escalate +
// guide steps), a preflight status panel, and the concise explanation
// layer. Every executed output here is SCRIPTED DEMO OUTPUT from the
// deterministic mock/engineered path — never presented as OpenAI,
// Anthropic, or web-derived output.

const DECISION_STYLES = {
  Allow:    'bg-green-900/30 text-green-400 border-green-800',
  Hold:     'bg-yellow-900/30 text-yellow-400 border-yellow-800',
  Stop:     'bg-red-900/30 text-red-400 border-red-800',
  Escalate: 'bg-orange-900/30 text-orange-400 border-orange-800',
};

function PreflightRow({ label, ok, value }) {
  return (
    <div className="flex items-center justify-between text-xs py-0.5">
      <span className="text-gray-400">{label}</span>
      <span className={`font-mono ${
        ok === true ? 'text-green-400' : ok === false ? 'text-red-400' : 'text-gray-300'
      }`}>
        {value}
      </span>
    </div>
  );
}

class SafeScenarioCard extends Component {
  constructor(props) { super(props); this.state = { failed: false }; }
  static getDerivedStateFromError() { return { failed: true }; }
  render() {
    if (this.state.failed) {
      return (
        <div className="bg-gray-900 border border-red-900 rounded-lg p-3 text-xs text-red-400">
          This scenario card could not be rendered; the rest of the demo is unaffected.
        </div>
      );
    }
    return this.props.children;
  }
}

function ScenarioCard({ scenario, result, running, onRun }) {
  const runnable = scenario.kind !== 'guide';
  return (
    <div className="bg-gray-900 border border-gray-800 rounded-lg p-4 space-y-2">
      <div className="flex items-start justify-between gap-2">
        <div>
          <h4 className="text-sm font-medium text-white">{scenario.title}</h4>
          <p className="text-[11px] text-gray-500 mt-0.5">{scenario.demonstrates}</p>
        </div>
        {runnable ? (
          <button
            onClick={() => onRun(scenario.scenario_id)}
            disabled={running}
            className="text-xs px-3 py-1.5 rounded bg-blue-600 hover:bg-blue-700 disabled:opacity-40 text-white font-medium shrink-0"
          >
            {running ? 'Running…' : 'Run'}
          </button>
        ) : (
          <span className="text-[10px] text-gray-500 bg-gray-800 border border-gray-700 rounded-full px-2 py-0.5 shrink-0">
            walkthrough
          </span>
        )}
      </div>
      {scenario.prompt && (
        <p className="text-[11px] text-gray-400 font-mono bg-gray-950 border border-gray-800 rounded px-2 py-1">
          {scenario.prompt}
        </p>
      )}
      <p className="text-[11px] text-gray-500">
        <span className="text-gray-400">Operator action: </span>
        {scenario.operator_action}
      </p>
      {scenario.expected_decision && (
        <p className="text-[11px] text-gray-500">
          Expected decision:{' '}
          <span className="font-semibold text-gray-300">{scenario.expected_decision}</span>
        </p>
      )}

      {result && (
        <div className="border-t border-gray-800 pt-2 space-y-2">
          <span className="inline-block text-[10px] font-bold tracking-wider px-2 py-0.5 rounded bg-amber-900/40 text-amber-300 border border-amber-700">
            SCRIPTED DEMO OUTPUT
          </span>
          <div className="flex items-center gap-2">
            <span className={`text-xs font-semibold px-2 py-0.5 rounded border ${
              DECISION_STYLES[result.decision] || 'bg-gray-800 text-gray-300 border-gray-700'
            }`}>
              {result.decision}
            </span>
            <span className={`text-[11px] ${result.matches_expected ? 'text-green-500' : 'text-red-400'}`}>
              {result.matches_expected ? 'matches expected' : 'DID NOT MATCH EXPECTED'}
            </span>
            {result.tis_current != null && (
              <span className="text-[11px] font-mono text-gray-400">
                TIS {displayGoverned(result.tis_current, 3)}
              </span>
            )}
          </div>
          {result.response && (
            <p className="text-xs text-gray-300 bg-gray-950 border border-gray-800 rounded px-2 py-1 whitespace-pre-wrap max-h-32 overflow-y-auto">
              {result.response}
            </p>
          )}
          {result.blocking_reason && (
            <p className="text-[11px] text-yellow-400 break-words">
              {result.blocking_reason}
            </p>
          )}
          {result.certificate_id && (
            <p className="text-[10px] font-mono text-gray-500">
              TC {result.certificate_id.slice(0, 8)}…{' '}
              <Link to="/audit" className="text-blue-400 hover:text-blue-300 font-sans">Certificate</Link>
            </p>
          )}
        </div>
      )}
    </div>
  );
}

export default function InvestorDemo() {
  const { isDemo } = useOperatingMode();
  const { llmConnections } = useConnections();
  const [started, setStarted] = useState(false);
  const [preflight, setPreflight] = useState(null);
  const [preflightError, setPreflightError] = useState(null);
  const [scenarios, setScenarios] = useState([]);
  const [results, setResults] = useState({});
  const [runningId, setRunningId] = useState(null);

  const loadPreflight = () => {
    apiFetch('/demo/preflight')
      .then((d) => { setPreflight(d); setPreflightError(null); })
      .catch((e) => setPreflightError(e?.message || 'backend unreachable'));
  };
  useEffect(() => {
    loadPreflight();
    apiFetch('/demo/scenarios')
      .then((d) => setScenarios(d.scenarios || []))
      .catch(() => {});
  }, []);

  const run = (scenarioId) => {
    setRunningId(scenarioId);
    apiPost('/demo/run', { scenario_id: scenarioId })
      .then((r) => setResults((prev) => ({ ...prev, [scenarioId]: r })))
      .catch((e) => setResults((prev) => ({
        ...prev,
        [scenarioId]: { decision: 'Error', matches_expected: false,
                        blocking_reason: e?.message || 'run failed' },
      })))
      .finally(() => setRunningId(null));
  };

  // Live-connection summary for preflight — names/models only, and a
  // credential-PRESENCE boolean. Keys themselves are never rendered,
  // and presence is derived from in-memory state only (a refresh
  // clears it — nothing about credentials is persisted).
  const liveConnections = llmConnections.filter((c) => c.type !== 'mock');

  return (
    <div className="space-y-6">
      <div>
        <h2 className="text-lg font-semibold text-white">Investor Demo</h2>
        <p className="text-xs text-gray-500 mt-1">
          A controlled, deterministic, repeatable demonstration. In DEMO
          MODE no external AI is called — every output below is scripted
          and labeled as such. Live LLM, Model Comparison, and Live Web
          are the deliberate second act, in LIVE MODE.
        </p>
        {!isDemo && (
          <p role="alert" className="mt-2 text-xs text-yellow-400 bg-yellow-900/20 border border-yellow-800 rounded px-2 py-1">
            LIVE MODE is active. The scripted investor narrative is
            designed for DEMO MODE — return to Demo Mode from the
            toolbar for the deterministic presentation.
          </p>
        )}
      </div>

      {!started ? (
        <button
          onClick={() => setStarted(true)}
          className="text-sm px-5 py-2.5 rounded bg-blue-600 hover:bg-blue-700 text-white font-semibold"
        >
          Start Investor Demo
        </button>
      ) : (
        <>
          {/* Preflight */}
          <section className="bg-gray-900 border border-gray-800 rounded-lg p-4">
            <div className="flex items-center justify-between mb-2">
              <h3 className="text-sm font-medium text-white">Preflight</h3>
              <button onClick={loadPreflight}
                className="text-[11px] text-blue-400 hover:text-blue-300">
                Refresh
              </button>
            </div>
            {preflightError && (
              <p className="text-xs text-red-400">Backend unreachable: {preflightError}</p>
            )}
            {preflight && (
              <div className="grid grid-cols-1 sm:grid-cols-2 gap-x-6">
                <PreflightRow label="Backend" ok={true} value="reachable" />
                <PreflightRow label="Build" value={preflight.build_id} />
                <PreflightRow label="Operating mode"
                  ok={preflight.operating_mode === 'demo'}
                  value={preflight.operating_mode.toUpperCase()} />
                <PreflightRow label="Certificate store"
                  ok={preflight.certificate_store_available}
                  value={preflight.certificate_store_available ? 'available' : 'missing'} />
                <PreflightRow label="Hash-chain health"
                  ok={preflight.chain_intact}
                  value={preflight.chain_intact === null ? 'n/a'
                    : preflight.chain_intact ? 'all chains verify' : 'FAILED'} />
                <PreflightRow label="Scripted scenarios"
                  ok={preflight.scripted_scenarios_available > 0}
                  value={String(preflight.scripted_scenarios_available)} />
                <PreflightRow label="Live Web capability"
                  value={preflight.live_web_available ? 'built in' : 'unavailable'} />
                <PreflightRow label="Live connections configured"
                  value={String(liveConnections.length)} />
                <PreflightRow label="Credentials in memory"
                  value={String(liveConnections.filter((c) => c.config?.apiKey).length)} />
              </div>
            )}
            <p className="text-[10px] text-gray-600 mt-2">
              Credential presence reflects this browser session's memory
              only — it is never persisted and resets on refresh. Use
              Archives to archive current demonstration data and start
              from a clean dataset before a session (archival, not
              deletion). <Link to="/archives" className="text-blue-400">Archives</Link>
            </p>
          </section>

          {/* Explanation layer */}
          <section className="grid grid-cols-1 md:grid-cols-2 gap-2 text-[11px] text-gray-400">
            <div className="bg-gray-900/60 border border-gray-800 rounded p-2">
              <span className="font-semibold text-gray-200">Demo Mode</span> — controlled,
              deterministic, repeatable; no external AI calls; the planned
              investor narrative.
            </div>
            <div className="bg-gray-900/60 border border-gray-800 rounded p-2">
              <span className="font-semibold text-gray-200">Live LLM</span> — a real,
              nondeterministic provider response, governed after generation;
              local corpus only, no web search.
            </div>
            <div className="bg-gray-900/60 border border-gray-800 rounded p-2">
              <span className="font-semibold text-gray-200">Model Comparison</span> — same
              prompt, same retrieved context, same policy; independent outputs and
              independent TIS evaluations. Capability is not governability.
            </div>
            <div className="bg-gray-900/60 border border-gray-800 rounded p-2">
              <span className="font-semibold text-gray-200">Live Web</span> — provider-hosted
              web retrieval with explicit source evidence, citations, and separate
              retrieval vs governance states.
            </div>
            <div className="bg-gray-900/60 border border-gray-800 rounded p-2 md:col-span-2">
              A Trust Certificate attests to the governed execution, evidence,
              scoring, decision, and recorded provenance — it does not by itself
              prove the model's statement is factually true.
            </div>
          </section>

          {/* Scenarios */}
          <section className="space-y-3">
            <h3 className="text-sm font-medium text-white">
              Approved deterministic scenarios
            </h3>
            {scenarios.map((s, i) => (
              // Null-safe key/prop access here; ScenarioCard reads the
              // scenario fields inside its own render so the boundary
              // can isolate a malformed catalog entry.
              <SafeScenarioCard key={s?.scenario_id || `scenario-${i}`}>
                <ScenarioCard
                  scenario={s}
                  result={results[s?.scenario_id]}
                  running={runningId === s?.scenario_id}
                  onRun={run}
                />
              </SafeScenarioCard>
            ))}
          </section>
        </>
      )}
    </div>
  );
}
