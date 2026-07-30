// LiveDecisions — decision-driven action semantics (owner guardrail 6),
// decimal-string rendering, malformed-row resilience.

import { describe, expect, it, vi, beforeEach } from 'vitest';
import { render, screen, within } from '@testing-library/react';

const h = vi.hoisted(() => ({
  stream: { current: null },
  holds: { current: null },
  esc: { current: null },
}));
vi.mock('../../hooks/useApi', () => ({
  usePolling: (path) => {
    if (path.includes('hold-queue')) {
      return { data: h.holds.current, refetch: vi.fn() };
    }
    if (path.includes('escalation-queue')) {
      return { data: h.esc.current, refetch: vi.fn() };
    }
    return { data: h.stream.current, refetch: vi.fn() };
  },
  useApi: () => ({ data: null, refetch: vi.fn() }),
  apiFetch: vi.fn(),
  apiPost: vi.fn(),
}));

import LiveDecisions from '../LiveDecisions';

// Two records with the SAME failed aggregate gate (gate_result = 0)
// but DIFFERENT legitimate final decisions. The UI must follow
// `decision`, never reconstruct one from the gate state.
const GATE_FAIL_HOLD = {
  certificate_id: 'tc-hold-1', subject_id: 'subj-hold',
  decision: 'Hold', tis_current: '0.0000',
  component_scores: { B: '0.9500', A: '0.9000', C: '0.9500', K: '0.8800' },
  gate_result: 0, blocking_reason: 'attribution_gate_fail',
  requires_human_review: true,
  evaluation_timestamp: '2026-07-29T12:00:00Z',
  domain: 'financial_services', risk_tier: 'r3', override: null,
};
const GATE_FAIL_STOP = {
  certificate_id: 'tc-stop-1', subject_id: 'subj-stop',
  decision: 'Stop', tis_current: '0.0000',
  component_scores: { B: '0.9400', A: '0.7600', C: '0.9200', K: '0.8800' },
  gate_result: 0, blocking_reason: 'attribution_gate_fail_sbase_below_kappa',
  requires_human_review: false,
  evaluation_timestamp: '2026-07-29T12:01:00Z',
  domain: 'financial_services', risk_tier: 'r3', override: null,
};

beforeEach(() => {
  h.stream.current = { count: 2, decisions: [GATE_FAIL_HOLD, GATE_FAIL_STOP] };
  h.holds.current = {
    count: 1,
    holds: [{
      certificate_id: 'tc-hold-1', subject_id: 'subj-hold',
      tis_current: '0.0000',
      component_scores: GATE_FAIL_HOLD.component_scores,
      blocking_reason: 'attribution_gate_fail',
      evaluation_timestamp: '2026-07-29T12:00:00Z',
      domain: 'financial_services', override_status: 'pending',
    }],
  };
  h.esc.current = {
    count: 1,
    escalations: [{
      certificate_id: 'tc-esc-1', subject_id: 'subj-esc',
      tis_current: '0.3422', s_base: '0.9300',
      component_scores: { B: '0.9500', A: '0.9500', C: '0.9500', K: '0.8500' },
      gate_results: { B: 'pass', A: 'pass', C: 'pass', K: 'pass' },
      blocking_reason: null,
      evaluation_timestamp: '2026-07-29T12:02:00Z',
      domain: 'financial_services', risk_tier: 'r3',
      policy_set_id: 'fin-r3-a4-ct4',
      escalation_routed_to: ['senior_reviewer'],
      identity_binding: null, override_status: 'pending',
    }],
  };
});

describe('LiveDecisions — decision drives actions, not gate_result', () => {
  it('same failed gate, different decisions: Hold gets Hold actions, Stop gets none', async () => {
    render(<LiveDecisions />);

    // Both gate-fail rows render with their own decision badges.
    const holdRow = screen.getAllByText('subj-hold')[0].closest('tr')
      || screen.getAllByText('subj-hold')[0].closest('div');
    expect(holdRow).toBeTruthy();
    expect(screen.getAllByText('Hold').length).toBeGreaterThan(0);
    expect(screen.getAllByText('Stop').length).toBeGreaterThan(0);

    // The Hold queue exposes exactly the permitted Hold override
    // options (Allow | Escalate) — open the form to check.
    const overrideBtn = screen.getByRole('button', { name: 'Override' });
    overrideBtn.click();
    const options = await screen.findAllByRole('option');
    const holdOptions = options.map((o) => o.textContent);
    expect(holdOptions).toContain('Allow');
    expect(holdOptions).toContain('Escalate');
    expect(holdOptions).not.toContain('Stop');

    // The Stop record appears ONLY in Recent Decisions — no override
    // control exists for it anywhere.
    expect(screen.getAllByText('subj-stop').length).toBe(1);
    const stopCell = screen.getByText('subj-stop').closest('tr');
    expect(within(stopCell).queryByText('Override')).toBeNull();
    expect(within(stopCell).queryByText('Review')).toBeNull();
  });

  it('Escalate queue exposes Allow | Stop | Hold', async () => {
    render(<LiveDecisions />);
    screen.getByRole('button', { name: 'Review' }).click();
    const options = await screen.findAllByRole('option');
    const texts = options.map((o) => o.textContent);
    for (const expected of ['Allow', 'Stop', 'Hold']) {
      expect(texts).toContain(expected);
    }
  });

  it('renders decimal-string tis_current verbatim', () => {
    render(<LiveDecisions />);
    expect(screen.getAllByText('0.0000').length).toBeGreaterThanOrEqual(2);
    expect(screen.getByText('0.3422')).toBeInTheDocument();
    expect(screen.getByText('0.9300')).toBeInTheDocument();
  });
});

describe('LiveDecisions — malformed rows never blank the queue', () => {
  it('a malformed hold row loses Override but neighbors keep theirs', () => {
    h.holds.current = {
      count: 2,
      holds: [
        {
          certificate_id: 'tc-bad', subject_id: 'subj-bad',
          tis_current: '9e-1',          // malformed governed value
          component_scores: { B: '0.9500' },
          blocking_reason: 'x',
          evaluation_timestamp: '2026-07-29T12:00:00Z',
          domain: 'financial_services', override_status: 'pending',
        },
        h.holds.current.holds[0],
      ],
    };
    render(<LiveDecisions />);
    // Both rows render (subj-hold also appears in the decisions stream).
    expect(screen.getByText('subj-bad')).toBeInTheDocument();
    expect(screen.getAllByText('subj-hold').length).toBeGreaterThanOrEqual(1);
    // The malformed row shows the integrity chip instead of Override.
    expect(screen.getByText(/data integrity — override disabled/))
      .toBeInTheDocument();
    // Exactly one Override button remains (the valid neighbor's).
    expect(screen.getAllByRole('button', { name: 'Override' })).toHaveLength(1);
  });
});
