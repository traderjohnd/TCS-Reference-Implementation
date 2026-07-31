// Live Web (demo-live branch, Commit 5): governed web-grounded answers.
// Demo Mode can neither select nor execute Live Web; live runs show the
// LIVE WEB badge only for completed web-grounded results; citations are
// visible, clickable (http/https only, safe rel), and cited sources are
// distinguished from merely consulted sources; retrieval errors are
// provider-layer, never governance decisions; keys never render.

import { describe, expect, it, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';

const h = vi.hoisted(() => ({
  apiPost: vi.fn(),
  mode: { current: 'live' },
  connections: { current: [] },
}));
vi.mock('../../hooks/useApi', async () => {
  const actual = await vi.importActual('../../hooks/useApi');
  return {
    ...actual,
    apiPost: (...a) => h.apiPost(...a),
    useApi: () => ({ data: null, refetch: vi.fn() }),
    usePolling: () => ({ data: null, refetch: vi.fn() }),
  };
});
vi.mock('../../hooks/useConnections', () => ({
  useConnections: () => ({ llmConnections: h.connections.current }),
}));
vi.mock('../../hooks/useOperatingMode', async () => {
  const actual = await vi.importActual('../../hooks/useOperatingMode');
  return {
    ...actual,
    useOperatingMode: () => ({
      mode: h.mode.current,
      isDemo: h.mode.current === 'demo',
      isLive: h.mode.current === 'live',
      loaded: true, error: null,
      switchMode: vi.fn(), refresh: vi.fn(),
    }),
  };
});

import LiveWeb from '../LiveWeb';

const OPENAI_CONN = {
  id: 'oa-1', type: 'openai', name: 'TCS Test Key',
  category: 'llm', config: { model: 'gpt-4o', apiKey: 'sk-LIVEWEB-SECRET' },
};
const MOCK_CONN = {
  id: 'mock-1', type: 'mock', name: 'Mock Provider',
  category: 'llm', config: { model: 'deterministic' },
};

function evidence(overrides = {}) {
  return {
    schema_version: 'web-evidence-v1',
    provider: 'openai', model: 'gpt-4o',
    retrieval_mode: 'live_web', retrieval_status: 'success',
    live_access_requested: true, live_access_confirmed: true,
    search_call_count: 2, successful_search_count: 2,
    failed_search_count: 0, consulted_source_count: 2,
    cited_source_count: 1, answer_used_web_evidence: true,
    provider_request_id: 'resp_1', error_summary: null,
    search_actions: [], citations: [
      { citation_id: 'cit-1', source_id: 'src-1', ordinal: 0,
        provider_annotation_type: 'url_citation', text_block_ordinal: 0,
        start_offset: 0, end_offset: 10,
        cited_text: 'seven years', title: 'Retention rules',
        display_url: 'https://example.com/policy' },
    ],
    consulted_sources: [
      { source_id: 'src-1', first_seen_ordinal: 0,
        display_url: 'https://example.com/policy',
        canonical_url: 'https://example.com/policy',
        title: 'Retention rules', cited: true },
      { source_id: 'src-2', first_seen_ordinal: 1,
        display_url: 'https://other.example/background',
        canonical_url: 'https://other.example/background',
        title: 'Background reading', cited: false },
    ],
    ...overrides,
  };
}

function webResponse(overrides = {}) {
  return {
    query: 'What changed?',
    response: 'Retention is seven years per the 2026 update.',
    blocked: false,
    decision: 'Allow',
    certificate_id: 'tc-web-1111',
    artifact_id: 'webq-1',
    retrieval_mode: 'live_web',
    retrieval_status: 'success',
    execution_mode: 'live_provider',
    llm_provider: 'openai', llm_model: 'gpt-4o',
    error: null,
    tis_current: '0.9101',
    workflow_trace: { nodes: [
      { node_id: 'local-corpus-retrieval' },
      { node_id: 'provider-hosted-web-retrieval', event: {} },
      { node_id: 'llm-generate' },
    ] },
    policy_profile_id: 'fin-r3-a4-ct4',
    latency_ms: {},
    web_evidence: evidence(),
    web_evidence_digest: 'd'.repeat(64),
    local_corpus_used: true,
    ...overrides,
  };
}

function renderView() {
  return render(<MemoryRouter><LiveWeb /></MemoryRouter>);
}

async function setupAndRun(response) {
  renderView();
  fireEvent.change(screen.getByLabelText('Connection'),
    { target: { value: 'oa-1' } });
  fireEvent.change(
    screen.getByPlaceholderText(/question requiring live web/i),
    { target: { value: 'What changed?' } });
  fireEvent.click(screen.getByRole('button', { name: /review live web/i }));
  h.apiPost.mockResolvedValue(response);
  fireEvent.click(screen.getByRole('button', { name: /start live web query/i }));
  await waitFor(() => expect(h.apiPost).toHaveBeenCalled());
}

beforeEach(() => {
  h.apiPost.mockReset();
  h.mode.current = 'live';
  h.connections.current = [OPENAI_CONN, MOCK_CONN];
  localStorage.clear();
});

describe('LiveWeb — mode gating and setup', () => {
  it('cannot be selected or executed in Demo Mode', () => {
    h.mode.current = 'demo';
    renderView();
    const alert = screen.getByRole('alert');
    expect(alert).toHaveTextContent(/DEMO MODE/);
    expect(alert).toHaveTextContent(/requires LIVE MODE/i);
    expect(screen.queryByRole('button', { name: /review live web/i }))
      .not.toBeInTheDocument();
    expect(h.apiPost).not.toHaveBeenCalled();
  });

  it('shows the Live Web selection in Live Mode with live badge', () => {
    renderView();
    expect(screen.getByText('LIVE MODE — Live Web')).toBeInTheDocument();
    // Only live providers are selectable — mock never appears.
    expect(screen.queryByText(/Mock Provider/)).not.toBeInTheDocument();
    expect(screen.getByText(/TCS Test Key — openai \/ gpt-4o/))
      .toBeInTheDocument();
  });

  it('shows provider/model, web+corpus state, domains, location and charge warning before start', async () => {
    renderView();
    fireEvent.change(screen.getByLabelText('Connection'),
      { target: { value: 'oa-1' } });
    fireEvent.change(
      screen.getByPlaceholderText(/question requiring live web/i),
      { target: { value: 'q' } });
    fireEvent.change(screen.getByLabelText('Allowed domains'),
      { target: { value: 'example.com, sec.gov' } });
    fireEvent.change(screen.getByLabelText('Approximate city'),
      { target: { value: 'Boston' } });
    fireEvent.click(screen.getByRole('button', { name: /review live web/i }));
    expect(screen.getByText(/provider charges may apply/i)).toBeInTheDocument();
    expect(screen.getByText(/TCS Test Key: openai \/ gpt-4o/)).toBeInTheDocument();
    expect(screen.getByText(/External web access:/)).toBeInTheDocument();
    expect(screen.getByText(/Local corpus retrieval: enabled/)).toBeInTheDocument();
    expect(screen.getByText(/Allowed domains: example.com, sec.gov/)).toBeInTheDocument();
    expect(screen.getByText(/Approximate location: Boston/)).toBeInTheDocument();
    expect(screen.getByText(/Bounded search-use limit: 5/)).toBeInTheDocument();
    // Nothing sent before the deliberate start.
    expect(h.apiPost).not.toHaveBeenCalled();
    // The key never renders anywhere.
    expect(document.body.textContent).not.toContain('sk-LIVEWEB-SECRET');
  });
});

describe('LiveWeb — results', () => {
  it('shows LIVE WEB badge, retrieval summary, web node and governed answer', async () => {
    await setupAndRun(webResponse());
    expect(await screen.findByText('LIVE WEB')).toBeInTheDocument();
    expect(screen.getByText(/retrieval: success/)).toBeInTheDocument();
    expect(screen.getByText(/2 searches · 2 consulted · 1 cited/))
      .toBeInTheDocument();
    expect(screen.getByText(/Retention is seven years/)).toBeInTheDocument();
    expect(screen.getByText(/provider_hosted_web_retrieval/)).toBeInTheDocument();
    expect(screen.getByText(/TCS did not fetch pages/)).toBeInTheDocument();
    expect(screen.getByRole('link', { name: /certificate/i })).toBeInTheDocument();
    expect(screen.getByRole('link', { name: /replay/i })).toBeInTheDocument();
    // Canonical decimal rendering through the governed boundary.
    expect(screen.getByText(/TIS 0\.910/)).toBeInTheDocument();
  });

  it('renders clickable citations with safe external-link behavior', async () => {
    await setupAndRun(webResponse());
    const cited = await screen.findAllByRole('link',
      { name: /Retention rules/ });
    expect(cited.length).toBeGreaterThan(0);
    expect(cited[0]).toHaveAttribute('href', 'https://example.com/policy');
    expect(cited[0]).toHaveAttribute('target', '_blank');
    expect(cited[0].getAttribute('rel')).toContain('noopener');
    expect(cited[0].getAttribute('rel')).toContain('noreferrer');
    expect(screen.getByText(/“seven years”/)).toBeInTheDocument();
  });

  it('distinguishes cited from merely consulted sources', async () => {
    await setupAndRun(webResponse());
    expect(await screen.findByText('Cited sources')).toBeInTheDocument();
    expect(screen.getByText('Other consulted sources')).toBeInTheDocument();
    expect(screen.getByText(/not necessarily supporting the answer/i))
      .toBeInTheDocument();
  });

  it('withholds unsafe citation URLs from clickable rendering', async () => {
    const ev = evidence();
    ev.consulted_sources[0].display_url = 'javascript:alert(1)';
    await setupAndRun(webResponse({ web_evidence: ev }));
    expect(await screen.findByText(/unsafe URL withheld/i)).toBeInTheDocument();
    // Never rendered as a link.
    const links = screen.queryAllByRole('link');
    expect(links.some((l) =>
      (l.getAttribute('href') || '').startsWith('javascript:'))).toBe(false);
  });

  it('isolates a malformed source row', async () => {
    const ev = evidence();
    // A null row would crash SourceLink access — isolation keeps the rest.
    ev.consulted_sources = [null, ev.consulted_sources[1]];
    await setupAndRun(webResponse({ web_evidence: ev }));
    expect(await screen.findByText(/could not be rendered/i)).toBeInTheDocument();
    expect(screen.getByText(/Background reading/)).toBeInTheDocument();
  });

  it('shows a partial-result warning while remaining governed', async () => {
    await setupAndRun(webResponse({
      retrieval_status: 'partial',
      web_evidence: evidence({ retrieval_status: 'partial',
                               failed_search_count: 1,
                               error_summary: 'one search timed out' }),
    }));
    expect(await screen.findByText(/Partial retrieval/)).toBeInTheDocument();
    expect(screen.getByText(/certificate records "partial"/)).toBeInTheDocument();
    expect(screen.getByText(/one search timed out/)).toBeInTheDocument();
    expect(screen.getByText('LIVE WEB')).toBeInTheDocument();
  });

  it('shows retrieval failure as provider-layer with no governance badge', async () => {
    await setupAndRun(webResponse({
      response: null, blocked: true, decision: null,
      certificate_id: null, artifact_id: null,
      retrieval_status: 'retrieval_error',
      error: 'Live Web retrieval was not certifiable (status=retrieval_error).',
      web_evidence: evidence({ retrieval_status: 'retrieval_error',
                               cited_source_count: 0 }),
      web_evidence_digest: 'e'.repeat(64),
    }));
    expect(await screen.findByText('NOT WEB-GROUNDED')).toBeInTheDocument();
    expect(screen.queryByText('LIVE WEB')).not.toBeInTheDocument();
    expect(screen.getByText(/Retrieval failed — no governed answer/i))
      .toBeInTheDocument();
    expect(screen.getByText(/System diagnostic \(not model output\)/i))
      .toBeInTheDocument();
    expect(screen.getByText(/not Hold, Stop, or Escalate/i)).toBeInTheDocument();
    expect(screen.queryByText('Hold')).not.toBeInTheDocument();
    expect(screen.queryByText('Stop')).not.toBeInTheDocument();
    expect(screen.queryByText('Escalate')).not.toBeInTheDocument();
  });

  it('exposes the evidence digest in technical details', async () => {
    await setupAndRun(webResponse());
    fireEvent.click(await screen.findByText(/technical details/i));
    expect(screen.getByText(new RegExp('d'.repeat(64)))).toBeInTheDocument();
    expect(screen.getByText(/retrieval_mode: live_web/)).toBeInTheDocument();
  });
});
