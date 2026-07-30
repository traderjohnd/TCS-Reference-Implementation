// Model Comparison (demo-live branch, Commit 4): one identical governed
// request against 2-4 selected connections; side-by-side independently
// governed results; scripted vs live clearly distinguished; provider
// failures never rendered as governance decisions; keys never displayed.

import { describe, expect, it, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';

const h = vi.hoisted(() => ({
  apiPost: vi.fn(),
  mode: { current: 'demo' },
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

import ModelComparison from '../ModelComparison';

const MOCK_CONN = {
  id: 'mock-default', type: 'mock', name: 'Mock Provider',
  category: 'llm', config: { model: 'deterministic' },
};
const MOCK_CONN_2 = {
  id: 'mock-2', type: 'mock', name: 'Mock Baseline',
  category: 'llm', config: { model: 'deterministic' },
};
const OPENAI_CONN = {
  id: 'oa-1', type: 'openai', name: 'TCS Test Key',
  category: 'llm', config: { model: 'gpt-4o', apiKey: 'sk-SECRET-KEY' },
};
const CLAUDE_CONN = {
  id: 'an-1', type: 'anthropic', name: 'Claude Connection',
  category: 'llm', config: { model: 'claude-opus-5', apiKey: 'sk-ant-SECRET' },
};

function member(overrides = {}) {
  return {
    comparison_member_id: 'cmp-1-m0',
    ordinal: 0,
    provider: 'openai',
    model: 'gpt-4o',
    connection_name: 'TCS Test Key',
    label: null,
    execution_mode: 'live_provider',
    status: 'ok',
    response: 'A governed answer.',
    blocked: false,
    latency_ms: 812.4,
    usage: { prompt_tokens: 10, completion_tokens: 20, total_tokens: 30 },
    provider_request_id: 'req-1',
    error: null,
    decision: 'Allow',
    tis_current: '0.9142',
    s_base: '0.9310',
    gate_result: 1,
    component_scores: { B: '0.95', A: '0.92', C: '0.94', K: '0.88' },
    component_weights: { B: '0.25', A: '0.30', C: '0.25', K: '0.20' },
    gate_results: { B: 'pass', A: 'pass', C: 'pass', K: 'pass' },
    thresholds: { B: '0.80', A: '0.93', C: '0.85', K: '0.80' },
    blocking_reason: null,
    requires_human_review: false,
    explanation: 'All gates passed under the active policy.',
    certificate_id: 'tc-aaaa1111-2222',
    artifact_id: 'art-1',
    ...overrides,
  };
}

function comparisonResponse(members) {
  return {
    comparison_id: 'cmp-1',
    execution_mode: 'live',
    question: 'What is the retention policy?',
    prompt_package_hash: 'a'.repeat(64),
    context_snapshot_id: 'ctx-abcdef1234567890',
    context_snapshot_hash: 'b'.repeat(64),
    policy_profile_id: 'fin-r3-a4-ct4',
    policy_profile_version: 'tis-v2',
    retrieval_config: { k: 5, ordering: 'similarity_desc', web_retrieval: false },
    executed_at: '2026-07-30T12:00:00Z',
    target_count: members.length,
    members,
  };
}

function renderView() {
  return render(<MemoryRouter><ModelComparison /></MemoryRouter>);
}

async function selectAndPrompt(names, prompt = 'What is the retention policy?') {
  for (const n of names) {
    fireEvent.click(screen.getByLabelText(`Select ${n}`));
  }
  fireEvent.change(
    screen.getByPlaceholderText(/one prompt, sent identically/i),
    { target: { value: prompt } },
  );
}

beforeEach(() => {
  h.apiPost.mockReset();
  h.mode.current = 'live';
  h.connections.current = [MOCK_CONN, MOCK_CONN_2, OPENAI_CONN, CLAUDE_CONN];
  localStorage.clear();
});

describe('ModelComparison — selection and start flow', () => {
  it('requires at least two selected connections before review', async () => {
    renderView();
    await selectAndPrompt(['TCS Test Key']);
    expect(screen.getByRole('button', { name: /review comparison/i }))
      .toBeDisabled();
    fireEvent.click(screen.getByLabelText('Select Claude Connection'));
    expect(screen.getByRole('button', { name: /review comparison/i }))
      .toBeEnabled();
  });

  it('caps selection at four connections', () => {
    h.connections.current = [
      MOCK_CONN, MOCK_CONN_2, OPENAI_CONN, CLAUDE_CONN,
      { id: 'x5', type: 'mock', name: 'Fifth', category: 'llm', config: { model: 'deterministic' } },
    ];
    renderView();
    ['Mock Provider', 'Mock Baseline', 'TCS Test Key', 'Claude Connection', 'Fifth']
      .forEach((n) => fireEvent.click(screen.getByLabelText(`Select ${n}`)));
    // The fifth selection is ignored.
    expect(screen.getByLabelText('Select Fifth')).not.toBeChecked();
  });

  it('shows provider/model, request count and charge warning before a deliberate start', async () => {
    renderView();
    await selectAndPrompt(['TCS Test Key', 'Claude Connection']);
    fireEvent.click(screen.getByRole('button', { name: /review comparison/i }));
    // Explicit request count + provider/model identification + warning.
    expect(screen.getByText(/will make/i).textContent)
      .toMatch(/2 external provider requests/);
    expect(screen.getByText(/provider charges may apply/i)).toBeInTheDocument();
    expect(screen.getByText(/TCS Test Key: openai \/ gpt-4o/)).toBeInTheDocument();
    expect(screen.getByText(/Claude Connection: anthropic \/ claude-opus-5/))
      .toBeInTheDocument();
    // Nothing sent until the deliberate start action.
    expect(h.apiPost).not.toHaveBeenCalled();
    h.apiPost.mockResolvedValue(comparisonResponse([member()]));
    fireEvent.click(screen.getByRole('button', { name: /start comparison/i }));
    await waitFor(() => expect(h.apiPost).toHaveBeenCalledTimes(1));
  });

  it('never renders API keys anywhere in the view', async () => {
    renderView();
    await selectAndPrompt(['TCS Test Key', 'Claude Connection']);
    fireEvent.click(screen.getByRole('button', { name: /review comparison/i }));
    expect(document.body.textContent).not.toContain('sk-SECRET-KEY');
    expect(document.body.textContent).not.toContain('sk-ant-SECRET');
  });
});

describe('ModelComparison — Demo Mode', () => {
  it('blocks live targets in Demo Mode with a clear notice', async () => {
    h.mode.current = 'demo';
    renderView();
    await selectAndPrompt(['TCS Test Key', 'Claude Connection']);
    expect(screen.getByText(/DEMO MODE is active: external providers cannot/i))
      .toBeInTheDocument();
    expect(screen.getByRole('button', { name: /review comparison/i }))
      .toBeDisabled();
  });

  it('permits a clearly scripted mock comparison in Demo Mode', async () => {
    h.mode.current = 'demo';
    renderView();
    await selectAndPrompt(['Mock Provider', 'Mock Baseline']);
    expect(screen.getByText(/DEMO MODE — scripted comparison only/i))
      .toBeInTheDocument();
    expect(screen.getByRole('button', { name: /review comparison/i }))
      .toBeEnabled();
  });

  it('distinguishes scripted from live members in results', async () => {
    renderView();
    await selectAndPrompt(['Mock Provider', 'TCS Test Key']);
    h.apiPost.mockResolvedValue(comparisonResponse([
      member({ comparison_member_id: 'cmp-1-m0', provider: 'mock',
               model: 'deterministic', connection_name: 'Mock Provider',
               execution_mode: 'scripted_demo' }),
      member({ comparison_member_id: 'cmp-1-m1', ordinal: 1 }),
    ]));
    fireEvent.click(screen.getByRole('button', { name: /review comparison/i }));
    fireEvent.click(screen.getByRole('button', { name: /start comparison/i }));
    await waitFor(() => {
      expect(screen.getByText('SCRIPTED')).toBeInTheDocument();
      expect(screen.getByText('LIVE')).toBeInTheDocument();
    });
  });
});

describe('ModelComparison — results', () => {
  async function runWith(members) {
    renderView();
    await selectAndPrompt(['TCS Test Key', 'Claude Connection']);
    h.apiPost.mockResolvedValue(comparisonResponse(members));
    fireEvent.click(screen.getByRole('button', { name: /review comparison/i }));
    fireEvent.click(screen.getByRole('button', { name: /start comparison/i }));
    await waitFor(() => expect(h.apiPost).toHaveBeenCalled());
  }

  it('renders side-by-side governed results with the same-input statement', async () => {
    await runWith([
      member(),
      member({ comparison_member_id: 'cmp-1-m1', ordinal: 1,
               provider: 'anthropic', model: 'claude-opus-5',
               connection_name: 'Claude Connection',
               label: 'frontier model',
               response: 'A different governed answer.',
               certificate_id: 'tc-bbbb2222-3333', artifact_id: 'art-2' }),
    ]);
    expect(await screen.findByText(/Results — same prompt, same retrieved context, same policy/i))
      .toBeInTheDocument();
    expect(screen.getByText('A governed answer.')).toBeInTheDocument();
    expect(screen.getByText('A different governed answer.')).toBeInTheDocument();
    // Provider/model identity appears in the selection list AND on the
    // member card — assert presence, tolerate both.
    expect(screen.getAllByText('openai · gpt-4o').length).toBeGreaterThan(0);
    expect(screen.getAllByText('anthropic · claude-opus-5').length)
      .toBeGreaterThan(0);
    expect(screen.getByText('frontier model')).toBeInTheDocument();
    // Certificate + replay audit actions per member.
    expect(screen.getAllByRole('link', { name: /certificate/i })).toHaveLength(2);
    expect(screen.getAllByRole('link', { name: /replay/i })).toHaveLength(2);
  });

  it('renders canonical decimal scores through the governed display boundary', async () => {
    await runWith([member()]);
    // tis_current arrives as the decimal STRING "0.9142" on the wire;
    // displayGoverned renders it at 3 places without float round-trip.
    expect(await screen.findByText(/TIS 0\.914/)).toBeInTheDocument();
  });

  it('shows mixed decisions independently', async () => {
    await runWith([
      member(),
      member({ comparison_member_id: 'cmp-1-m1', ordinal: 1,
               provider: 'anthropic', model: 'claude-opus-5',
               decision: 'Stop', blocked: true, response: null,
               gate_result: 0,
               gate_results: { B: 'pass', A: 'pass', C: 'fail', K: 'pass' },
               certificate_id: 'tc-cccc' }),
    ]);
    expect(await screen.findByText('Allow')).toBeInTheDocument();
    expect(screen.getByText('Stop')).toBeInTheDocument();
    expect(screen.getByText(/Response withheld by governance/i))
      .toBeInTheDocument();
  });

  it('isolates a provider failure without presenting it as a decision', async () => {
    await runWith([
      member(),
      member({ comparison_member_id: 'cmp-1-m1', ordinal: 1,
               provider: 'anthropic', model: 'claude-opus-5',
               status: 'provider_error',
               error: 'LLM provider error: 401 [redacted]',
               response: null, decision: null, certificate_id: null,
               artifact_id: null, component_scores: null,
               gate_results: null }),
    ]);
    expect((await screen.findAllByText(/provider error/i)).length)
      .toBeGreaterThan(0);
    expect(screen.getByText(/no governance decision or/i)).toBeInTheDocument();
    // The failed member shows no decision badge; the sibling keeps its TC.
    expect(screen.getByText('Allow')).toBeInTheDocument();
    expect(screen.getAllByRole('link', { name: /certificate/i })).toHaveLength(1);
    expect(screen.getByText(/1 governed · 1 provider failure/)).toBeInTheDocument();
  });

  it('shows empty provider output as a provider-layer failure, never a decision', async () => {
    await runWith([
      member(),
      member({ comparison_member_id: 'cmp-1-m1', ordinal: 1,
               provider: 'anthropic', model: 'claude-opus-5',
               status: 'empty_output',
               error: 'LLM provider error: anthropic: claude-opus-5 returned no usable text (stop_reason=refusal).',
               response: null, decision: null, certificate_id: null,
               artifact_id: null, component_scores: null,
               gate_results: null,
               provider_request_id: 'msg-anthropic-1',
               usage: { total_tokens: 33 } }),
    ]);
    expect(await screen.findByText(/Provider returned no usable output/i))
      .toBeInTheDocument();
    // The diagnostic is clearly a system message, not model content.
    expect(screen.getByText(/System diagnostic \(not model output\)/i))
      .toBeInTheDocument();
    // No governance decision is displayed for the empty member —
    // only the sibling's Allow badge exists, and no Hold/Stop/Escalate.
    expect(screen.getAllByText('Allow')).toHaveLength(1);
    expect(screen.queryByText('Hold')).not.toBeInTheDocument();
    expect(screen.queryByText('Stop')).not.toBeInTheDocument();
    expect(screen.queryByText('Escalate')).not.toBeInTheDocument();
    // Safe provenance remains visible; sibling stays certified.
    expect(screen.getByText(/33 tokens/)).toBeInTheDocument();
    expect(screen.getAllByRole('link', { name: /certificate/i })).toHaveLength(1);
  });

  it('remains usable when every provider fails', async () => {
    await runWith([
      member({ status: 'provider_error', error: 'openai down',
               decision: null, certificate_id: null, artifact_id: null,
               response: null, component_scores: null }),
      member({ comparison_member_id: 'cmp-1-m1', ordinal: 1,
               provider: 'anthropic', status: 'timeout',
               error: 'Provider call exceeded the 45s comparison timeout.',
               decision: null, certificate_id: null, artifact_id: null,
               response: null, component_scores: null }),
    ]);
    expect(await screen.findByText(/0 governed · 2 provider failures/))
      .toBeInTheDocument();
    expect(screen.getByText(/Provider error/i)).toBeInTheDocument();
    expect(screen.getByText(/Provider timeout/i)).toBeInTheDocument();
  });

  it('isolates a malformed member instead of blanking the grid', async () => {
    // usage.total_tokens as an object is not a valid React child — the
    // card render throws; the member error boundary keeps the sibling
    // visible (D2 list-isolation pattern).
    await runWith([
      member(),
      member({ comparison_member_id: 'cmp-1-m1', ordinal: 1,
               usage: { total_tokens: { bogus: true } } }),
    ]);
    expect(await screen.findByText('A governed answer.')).toBeInTheDocument();
    expect(screen.getByText(/could not be rendered/i)).toBeInTheDocument();
  });

  it('exposes prompt/context hashes in the technical detail section', async () => {
    await runWith([member()]);
    fireEvent.click(await screen.findByText(/technical details/i));
    expect(screen.getByText(/prompt_package_hash: aaaaaaaaaaaa…/))
      .toBeInTheDocument();
    expect(screen.getByText(/ctx-abcdef1234567890/)).toBeInTheDocument();
    expect(screen.getByText(/web_retrieval=false/)).toBeInTheDocument();
    expect(screen.getByText(/fin-r3-a4-ct4/)).toBeInTheDocument();
  });
});
