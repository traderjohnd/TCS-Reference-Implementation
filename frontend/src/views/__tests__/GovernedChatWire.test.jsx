// D1 regression tests — GovernedChat against the REAL v2 /v2/query wire.
//
// The defining fixture property: retrieval_chunks[].similarity_score is
// a decimal STRING (captured from the live merged wire), which
// previously crashed ProvenancePanel via .toFixed() and blanked the
// entire application. These tests expand the governance panel for every
// decision outcome (including the automatic expansion for non-Allow)
// and prove the panel renders, the similarity displays, no render
// exception escapes, and the rest of the application stays visible.

import { describe, expect, it, vi, beforeEach } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';

const h = vi.hoisted(() => ({ apiPost: vi.fn() }));
vi.mock('../../hooks/useApi', async () => {
  const actual = await vi.importActual('../../hooks/useApi');
  return {
    ...actual,
    apiPost: (...args) => h.apiPost(...args),
    useApi: () => ({ data: null, refetch: vi.fn() }),
    usePolling: () => ({ data: null, refetch: vi.fn() }),
  };
});
vi.mock('../../hooks/useConnections', () => ({
  useConnections: () => ({
    activeLlm: { type: 'mock', name: 'Mock', config: { model: 'det' } },
  }),
}));

import GovernedChat from '../GovernedChat';

// Real v2-shaped /v2/query response (values captured from the live
// merged wire): score fields are floats (QueryResponse display tier),
// gate_result is the 0|1 int, and chunk similarity is a decimal STRING.
function queryFixture(overrides = {}) {
  return {
    query: 'Is this client suitable for municipal bond allocation?',
    response: 'Based on the policy, municipal bonds are suitable.',
    blocked: false,
    decision: 'Allow',
    certificate_id: 'q-fixture-0001',
    tis_current: 0.9702,
    tis_raw: 0.9719,
    s_base: 0.9719,
    gate_result: 1,
    blocking_reason: null,
    requires_human_review: false,
    retrieval_chunks: [
      { chunk_id: 'c1', source_doc: 'financial_policy.md', version: 'v1',
        content: 'policy text', similarity_score: '0.9698', tags: [] },
      { chunk_id: 'c2', source_doc: null, version: null,
        content: 'x', similarity_score: '0.874999', tags: [] },
    ],
    latency_ms: { workflow_ms: 5.2, governance_ms: 12.1, total_ms: 35.0 },
    component_scores: { B: 1.0, A: 1.0, C: 1.0, K: 0.8874 },
    component_weights: { B: 0.25, A: 0.3, C: 0.25, K: 0.2 },
    gate_results: { B: 'pass', A: 'pass', C: 'pass', K: 'pass' },
    thresholds: { B: 0.9, A: 0.93, C: 0.9, K: 0.8 },
    workflow_trace: null,
    policy_profile_id: 'fin-r3-a4-ct4',
    connection_type: 'CT-4',
    ...overrides,
  };
}

const HOLD = queryFixture({
  decision: 'Hold', blocked: true, response: null,
  gate_result: 0, gate_results: { B: 'pass', A: 'fail', C: 'pass', K: 'pass' },
  blocking_reason: 'attribution_gate_fail_A=0.9_threshold=0.93',
  requires_human_review: true,
  component_scores: { B: 0.95, A: 0.9, C: 0.95, K: 0.88 },
  tis_current: 0.0, tis_raw: 0.0,
});
const STOP = queryFixture({
  decision: 'Stop', blocked: true, response: null,
  gate_result: 0, gate_results: { B: 'pass', A: 'pass', C: 'fail', K: 'pass' },
  blocking_reason: 'C3_prohibited_pattern_prompt_injection',
  component_scores: { B: 1.0, A: 0.85, C: 0.0, K: 0.95 },
  tis_current: 0.0, tis_raw: 0.0,
});
const ESCALATE = queryFixture({
  decision: 'Escalate', blocked: true, response: null,
  gate_result: 1,
  blocking_reason: null,
  requires_human_review: true,
  tis_current: 0.3421,
});

function renderChat() {
  return render(
    <MemoryRouter>
      <GovernedChat />
    </MemoryRouter>,
  );
}

async function send(user, text) {
  await user.type(screen.getByPlaceholderText(/question/i), text);
  await user.click(screen.getByRole('button', { name: /send/i }));
}

beforeEach(() => {
  h.apiPost.mockReset();
  localStorage.clear();
});

describe('GovernedChat — D1 real-wire governance panel', () => {
  it('Allow: expanding the panel renders string similarities without crashing', async () => {
    const user = userEvent.setup();
    h.apiPost.mockResolvedValueOnce(queryFixture());
    renderChat();
    await send(user, 'suitability?');

    // Collapsed by default for Allow.
    expect(await screen.findByText('Show governance')).toBeInTheDocument();
    await user.click(screen.getByText('Show governance'));

    // Panel renders; the STRING similarity displays verbatim through
    // the governed display boundary — full precision, no reformatting.
    expect(screen.getByText(/sim 0\.9698/)).toBeInTheDocument();
    expect(screen.getByText(/sim 0\.874999/)).toBeInTheDocument();
    expect(screen.getByText('Sources (2)')).toBeInTheDocument();

    // The rest of the application remains visible and usable.
    expect(screen.getByText('Governed Chat')).toBeInTheDocument();
    expect(screen.getByPlaceholderText(/question/i)).toBeEnabled();
  });

  it.each([
    ['Hold', HOLD, 'Response held for review'],
    ['Stop', STOP, 'Response blocked by governance'],
    ['Escalate', ESCALATE, 'Response escalated for review'],
  ])('%s: auto-expanded governance panel renders without blanking the app',
    async (decision, fixture, bubbleText) => {
      const user = userEvent.setup();
      h.apiPost.mockResolvedValueOnce(fixture);
      renderChat();
      await send(user, 'query');

      // Non-Allow: panel is expanded automatically — this is exactly
      // the path that previously blanked the whole application.
      expect(await screen.findByText(bubbleText)).toBeInTheDocument();
      expect(screen.getByText('Hide governance')).toBeInTheDocument();
      expect(screen.getByText(/sim 0\.9698/)).toBeInTheDocument();
      // Decision badge follows the recorded decision.
      expect(screen.getAllByText(decision).length).toBeGreaterThan(0);
      // Application shell intact.
      expect(screen.getByText('Governed Chat')).toBeInTheDocument();
      expect(screen.getByPlaceholderText(/question/i)).toBeEnabled();
    });

  it('BACK scores and certificate summary render for the non-Allow path', async () => {
    const user = userEvent.setup();
    h.apiPost.mockResolvedValueOnce(HOLD);
    renderChat();
    await send(user, 'query');

    expect(await screen.findByText('BACK scores')).toBeInTheDocument();
    // Effective A score displayed via governed boundary, FAIL marked.
    expect(screen.getByText('0.900')).toBeInTheDocument();
    expect(screen.getAllByText('FAIL').length).toBeGreaterThan(0);
    expect(screen.getByText('Trust Certificate')).toBeInTheDocument();
  });
});
