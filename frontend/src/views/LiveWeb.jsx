import { Component, useMemo, useState } from 'react';
import { Link } from 'react-router-dom';
import { useConnections } from '../hooks/useConnections';
import { useOperatingMode } from '../hooks/useOperatingMode';
import { apiPost } from '../hooks/useApi';
import { displayGoverned } from '../lib/governedDecimal';

// Commit 5 (demo-live branch): governed Live Web. One selected live
// provider performs hosted web retrieval + generation in a single
// governed request; TCS renders the distinct retrieval/generation
// nodes, normalized evidence, citations, and the Trust Certificate.
// Only Live Mode can execute; scripted output never gets a Live Web
// badge. Provider text renders as text; only http(s) links are
// clickable, opened with safe external-link behavior.

function isSafeUrl(url) {
  if (typeof url !== 'string') return false;
  const u = url.trim().toLowerCase();
  return u.startsWith('http://') || u.startsWith('https://');
}

function SourceLink({ url, title }) {
  const label = title || url || '(untitled source)';
  if (!isSafeUrl(url)) {
    return (
      <span className="text-gray-400" title="Non-HTTP(S) URL — not clickable">
        {label} <span className="text-yellow-500 text-[10px]">(unsafe URL withheld)</span>
      </span>
    );
  }
  return (
    <a
      href={url}
      target="_blank"
      rel="noopener noreferrer"
      className="text-blue-400 hover:text-blue-300 underline break-all"
    >
      {label}
    </a>
  );
}

// Row bodies access source/citation fields INSIDE their own render so
// the SafeRow boundary can catch a malformed entry — prop evaluation in
// the parent would crash the whole surface instead.
function SourceRow({ source }) {
  return (
    <li>
      <SourceLink url={source.display_url} title={source.title} />
    </li>
  );
}

function CitationRow({ citation }) {
  return (
    <li>
      <SourceLink url={citation.display_url} title={citation.title} />
      {citation.cited_text && (
        <span className="text-gray-500"> — “{citation.cited_text}”</span>
      )}
    </li>
  );
}

// One malformed source or citation must never blank the whole surface.
class SafeRow extends Component {
  constructor(props) { super(props); this.state = { failed: false }; }
  static getDerivedStateFromError() { return { failed: true }; }
  render() {
    if (this.state.failed) {
      return (
        <li className="text-xs text-red-400">
          This evidence row could not be rendered; other evidence is unaffected.
        </li>
      );
    }
    return this.props.children;
  }
}

export default function LiveWeb() {
  const { llmConnections } = useConnections();
  const { isLive } = useOperatingMode();

  const webConnections = useMemo(
    () => llmConnections.filter((c) => c.type === 'openai' || c.type === 'anthropic'),
    [llmConnections],
  );
  const [connId, setConnId] = useState('');
  const conn = webConnections.find((c) => c.id === connId) || null;
  const [prompt, setPrompt] = useState('');
  const [useLocalCorpus, setUseLocalCorpus] = useState(true);
  const [allowedDomains, setAllowedDomains] = useState('');
  const [blockedDomains, setBlockedDomains] = useState('');
  const [city, setCity] = useState('');
  const [country, setCountry] = useState('');
  const [maxSearches, setMaxSearches] = useState('5');
  const [confirming, setConfirming] = useState(false);
  const [running, setRunning] = useState(false);
  const [result, setResult] = useState(null);
  const [requestError, setRequestError] = useState(null);
  const [showTech, setShowTech] = useState(false);

  const parseDomains = (s) => s.split(',').map((d) => d.trim()).filter(Boolean);
  const canStart = isLive && conn && prompt.trim() && !running;

  const run = async () => {
    setRunning(true); setConfirming(false);
    setRequestError(null); setResult(null);
    try {
      const allowed = parseDomains(allowedDomains);
      const blocked = parseDomains(blockedDomains);
      const location = {};
      if (city.trim()) location.city = city.trim();
      if (country.trim()) location.country = country.trim();
      const data = await apiPost('/query/web', {
        query: prompt.trim(),
        provider: conn.type,
        model: conn.config?.model || '',
        api_key: conn.config?.apiKey || null,
        connection_name: conn.name,
        execution_mode: 'live',
        retrieval_mode: 'live_web',
        include_local_corpus: useLocalCorpus,
        allowed_domains: allowed.length ? allowed : null,
        blocked_domains: blocked.length ? blocked : null,
        user_location: Object.keys(location).length ? location : null,
        max_searches: Number(maxSearches) || 5,
      });
      setResult(data);
    } catch (err) {
      setRequestError(err?.message || 'Live Web request failed');
    } finally {
      setRunning(false);
    }
  };

  const ev = result?.web_evidence || null;
  const governed = result?.certificate_id != null;
  const retrievalOk = result?.retrieval_status === 'success' ||
    result?.retrieval_status === 'partial';
  const citedSources = (ev?.consulted_sources || []).filter((s) => s?.cited);
  const otherSources = (ev?.consulted_sources || []).filter((s) => !s?.cited);
  const webNode = result?.workflow_trace?.nodes?.find(
    (n) => n.node_id === 'provider-hosted-web-retrieval');

  if (!isLive) {
    return (
      <div className="space-y-4">
        <h2 className="text-lg font-semibold text-white">Live Web</h2>
        <div role="alert" className="bg-yellow-900/20 border border-yellow-800 rounded-lg p-4 text-sm text-yellow-300">
          DEMO MODE is active. Governed Live Web requires LIVE MODE — external
          web retrieval cannot be selected or executed in Demo Mode, and the
          backend enforces this regardless of frontend state.
        </div>
      </div>
    );
  }

  return (
    <div className="space-y-6">
      <div>
        <h2 className="text-lg font-semibold text-white">Live Web</h2>
        <p className="text-xs text-gray-500 mt-1">
          Governed web-grounded answers: the selected provider performs hosted
          web retrieval and generation in one governed request; TCS records the
          retrieval as its own workflow node, normalizes the source evidence,
          and certifies the final answer. TCS does not fetch pages itself.
        </p>
        <span className="inline-block mt-2 text-[11px] font-semibold px-2 py-0.5 rounded bg-red-900/40 text-red-300">
          LIVE MODE — Live Web
        </span>
      </div>

      {/* Setup */}
      <section className="space-y-3">
        <div>
          <label className="block text-sm text-white mb-1" htmlFor="lw-conn">Connection</label>
          <select
            id="lw-conn"
            value={connId}
            onChange={(e) => { setConnId(e.target.value); setConfirming(false); }}
            className="w-full bg-gray-900 border border-gray-700 rounded px-3 py-2 text-sm text-white"
          >
            <option value="">Select a live connection…</option>
            {webConnections.map((c) => (
              <option key={c.id} value={c.id}>
                {c.name} — {c.type} / {c.config?.model || '(no model)'}
              </option>
            ))}
          </select>
        </div>
        <textarea
          value={prompt}
          onChange={(e) => { setPrompt(e.target.value); setConfirming(false); }}
          rows={3}
          placeholder="Question requiring live web evidence…"
          className="w-full bg-gray-900 border border-gray-700 rounded-lg px-3 py-2 text-sm text-white placeholder-gray-600 focus:outline-none focus:border-blue-500"
        />
        <div className="grid grid-cols-1 sm:grid-cols-2 gap-3 text-xs">
          <label className="flex items-center gap-2 text-gray-300">
            <input type="checkbox" checked={useLocalCorpus}
              onChange={(e) => setUseLocalCorpus(e.target.checked)} />
            Include local corpus retrieval
          </label>
          <label className="flex items-center gap-2 text-gray-300">
            Max searches
            <input type="number" min="1" max="10" value={maxSearches}
              onChange={(e) => setMaxSearches(e.target.value)}
              aria-label="Max searches"
              className="w-16 bg-gray-900 border border-gray-700 rounded px-2 py-1 text-white" />
          </label>
          <input type="text" value={allowedDomains}
            onChange={(e) => setAllowedDomains(e.target.value)}
            placeholder="Allowed domains (comma-separated, optional)"
            aria-label="Allowed domains"
            className="bg-gray-900 border border-gray-700 rounded px-2 py-1 text-white placeholder-gray-600" />
          <input type="text" value={blockedDomains}
            onChange={(e) => setBlockedDomains(e.target.value)}
            placeholder="Blocked domains (optional)"
            aria-label="Blocked domains"
            className="bg-gray-900 border border-gray-700 rounded px-2 py-1 text-white placeholder-gray-600" />
          <input type="text" value={city} onChange={(e) => setCity(e.target.value)}
            placeholder="Approximate city (optional, disclosed to provider)"
            aria-label="Approximate city"
            className="bg-gray-900 border border-gray-700 rounded px-2 py-1 text-white placeholder-gray-600" />
          <input type="text" value={country} onChange={(e) => setCountry(e.target.value)}
            placeholder="Country (optional)"
            aria-label="Country"
            className="bg-gray-900 border border-gray-700 rounded px-2 py-1 text-white placeholder-gray-600" />
        </div>
      </section>

      {/* Confirm + start */}
      <section className="space-y-2">
        {!confirming ? (
          <button
            onClick={() => setConfirming(true)}
            disabled={!canStart}
            className="text-sm px-4 py-2 rounded bg-blue-600 hover:bg-blue-700 disabled:opacity-40 disabled:cursor-not-allowed text-white font-medium"
          >
            Review Live Web request…
          </button>
        ) : (
          <div className="bg-gray-900 border border-blue-800/60 rounded-lg p-4 space-y-1 text-xs text-gray-300">
            <p className="text-sm text-white">
              1 external Live Web request — provider charges may apply.
            </p>
            <p className="font-mono">{conn.name}: {conn.type} / {conn.config?.model}</p>
            <p>External web access: <span className="text-red-300">enabled</span></p>
            <p>Local corpus retrieval: {useLocalCorpus ? 'enabled' : 'disabled'}</p>
            <p>Allowed domains: {parseDomains(allowedDomains).join(', ') || 'none'}</p>
            <p>Blocked domains: {parseDomains(blockedDomains).join(', ') || 'none'}</p>
            <p>Approximate location: {(city.trim() || country.trim())
              ? [city.trim(), country.trim()].filter(Boolean).join(', ')
              : 'not shared'}</p>
            <p>Bounded search-use limit: {Number(maxSearches) || 5}</p>
            <div className="flex gap-2 pt-2">
              <button onClick={run}
                className="text-sm px-4 py-2 rounded bg-blue-600 hover:bg-blue-700 text-white font-medium">
                Start Live Web query
              </button>
              <button onClick={() => setConfirming(false)}
                className="text-sm px-4 py-2 rounded border border-gray-700 text-gray-400 hover:text-white">
                Cancel
              </button>
            </div>
          </div>
        )}
        {running && <p className="text-xs text-gray-400">Running governed Live Web query…</p>}
        {requestError && (
          <p className="text-xs text-red-400 bg-red-900/20 rounded px-2 py-1">{requestError}</p>
        )}
      </section>

      {/* Result */}
      {result && (
        <section className="space-y-3">
          <div className="flex items-center gap-2 flex-wrap">
            {retrievalOk && governed ? (
              <span className="text-[11px] font-semibold px-2 py-0.5 rounded bg-red-900/40 text-red-300">
                LIVE WEB
              </span>
            ) : (
              <span className="text-[11px] font-semibold px-2 py-0.5 rounded bg-gray-800 text-gray-300">
                NOT WEB-GROUNDED
              </span>
            )}
            <span className="text-[11px] font-mono text-gray-400">
              {result.llm_provider} / {result.llm_model}
            </span>
            <span className={`text-[11px] font-mono ${
              retrievalOk ? 'text-green-500' : 'text-yellow-400'
            }`}>
              retrieval: {result.retrieval_status}
            </span>
            {ev && (
              <span className="text-[10px] text-gray-500 font-mono">
                {ev.search_call_count} searches · {ev.consulted_source_count} consulted · {ev.cited_source_count} cited
              </span>
            )}
          </div>

          {result.retrieval_status === 'partial' && (
            <p className="text-xs text-yellow-400 bg-yellow-900/15 border border-yellow-800/50 rounded px-2 py-1">
              Partial retrieval: some searches failed. Every error is recorded
              in the evidence; the certificate records "partial".
            </p>
          )}
          {ev?.error_summary && (
            <p className="text-xs text-yellow-400">Retrieval warnings: {ev.error_summary}</p>
          )}

          {/* Retrieval-layer failure — never a governance decision */}
          {!governed && (
            <div className="bg-red-900/15 border border-red-900/60 rounded p-3">
              <div className="text-[11px] font-semibold text-red-400 uppercase tracking-wide">
                Retrieval failed — no governed answer
              </div>
              <p className="text-xs text-red-300 mt-1">
                <span className="text-gray-500">System diagnostic (not model output): </span>
                {result.error}
              </p>
              <p className="text-[10px] text-gray-500 mt-1">
                No governance decision and no Trust Certificate exist for this
                request. Retrieval errors are not Hold, Stop, or Escalate.
              </p>
            </div>
          )}

          {/* Governed answer */}
          {governed && (
            <>
              <div className="bg-gray-950 border border-gray-800 rounded p-3 text-sm text-gray-200 whitespace-pre-wrap">
                {result.blocked
                  ? <span className="text-yellow-400">Response withheld by governance ({result.decision}).</span>
                  : result.response}
              </div>
              <div className="flex items-center gap-3 text-xs">
                <span className="font-semibold">{result.decision}</span>
                {result.tis_current != null && (
                  <span className="font-mono text-gray-400">
                    TIS {displayGoverned(result.tis_current, 3)}
                  </span>
                )}
                {result.certificate_id && (
                  <>
                    <span className="font-mono text-gray-500" title={result.certificate_id}>
                      TC {result.certificate_id.slice(0, 8)}…
                    </span>
                    <Link to="/audit" className="text-blue-400 hover:text-blue-300">Certificate</Link>
                    <Link to="/replay" className="text-blue-400 hover:text-blue-300">Replay</Link>
                  </>
                )}
              </div>

              {/* Citations */}
              <div>
                <h4 className="text-xs font-medium text-white mb-1">Cited sources</h4>
                {citedSources.length === 0 ? (
                  <p className="text-xs text-gray-500">None.</p>
                ) : (
                  <ul className="space-y-1 text-xs">
                    {citedSources.map((s, i) => (
                      <SafeRow key={s?.source_id || `cited-${i}`}>
                        <SourceRow source={s} />
                      </SafeRow>
                    ))}
                  </ul>
                )}
                {otherSources.length > 0 && (
                  <>
                    <h4 className="text-xs font-medium text-gray-400 mt-2 mb-1">
                      Other consulted sources
                      <span className="text-[10px] text-gray-600 ml-2">
                        consulted during search — not necessarily supporting the answer
                      </span>
                    </h4>
                    <ul className="space-y-1 text-xs">
                      {otherSources.map((s, i) => (
                        <SafeRow key={s?.source_id || `other-${i}`}>
                          <SourceRow source={s} />
                        </SafeRow>
                      ))}
                    </ul>
                  </>
                )}
              </div>

              {/* Inline citation excerpts */}
              {(ev?.citations || []).length > 0 && (
                <div>
                  <h4 className="text-xs font-medium text-white mb-1">Citations</h4>
                  <ul className="space-y-1 text-xs text-gray-400">
                    {ev.citations.map((c, i) => (
                      <SafeRow key={c?.citation_id || `cit-${i}`}>
                        <CitationRow citation={c} />
                      </SafeRow>
                    ))}
                  </ul>
                </div>
              )}
            </>
          )}

          {/* Workflow web node */}
          {webNode && (
            <div className="text-[11px] text-gray-400 border border-gray-800 rounded p-2">
              <span className="text-gray-500">Workflow node: </span>
              provider_hosted_web_retrieval — status {webNode.event ? 'recorded' : 'n/a'};
              the provider performed the hosted retrieval; TCS did not fetch pages.
            </div>
          )}

          {/* Technical details */}
          <div className="border border-gray-800 rounded-lg">
            <button onClick={() => setShowTech((v) => !v)}
              className="w-full text-left px-3 py-1.5 text-xs text-gray-400 hover:text-white">
              {showTech ? '▾' : '▸'} Technical details
            </button>
            {showTech && (
              <div className="border-t border-gray-800 p-3 text-[11px] font-mono text-gray-400 space-y-1">
                <div>web_evidence_digest: {result.web_evidence_digest}</div>
                <div>
                  live_access: requested={String(ev?.live_access_requested)}{' '}
                  · observed={String(ev?.web_search_action_observed)}{' '}
                  · confirmed={String(ev?.live_access_confirmed)}
                </div>
                <div>retrieval_mode: {result.retrieval_mode}</div>
                <div>local_corpus_used: {String(result.local_corpus_used)}</div>
                <div>policy_profile: {result.policy_profile_id}</div>
                <div>execution_mode: {result.execution_mode}</div>
              </div>
            )}
          </div>
        </section>
      )}
    </div>
  );
}
