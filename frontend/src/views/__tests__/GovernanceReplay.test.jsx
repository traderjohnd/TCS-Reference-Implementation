// GovernanceReplay — historical-verification vs tis-v2 reevaluation
// terminology (owner ruling 1) and cross-version outcome framing.

import { describe, expect, it, vi } from 'vitest';
import { render, screen } from '@testing-library/react';

vi.mock('../../hooks/useApi', () => ({
  useApi: () => ({ data: null, refetch: vi.fn(), loading: false, error: null }),
  usePolling: () => ({ data: null, refetch: vi.fn() }),
  apiFetch: vi.fn(),
  apiPost: vi.fn(),
}));

import GovernanceReplay from '../GovernanceReplay';

const BASE_EVAL = {
  evaluation_id: 'ev-1',
  artifact_id: 'art-1',
  mode: 'what_if',
  decision: 'Hold',
  enforcement_action: 'logged_only',
  delivery_intervention: false,
  policy_profile_id: 'fin-r3-a4-ct4',
  evaluation_origin: 'replay',
  component_scores: { B: 0.95, A: 0.9, C: 0.95, K: 0.88 },
  gate_results: { B: 'pass', A: 'fail', C: 'pass', K: 'pass' },
  s_base: 0.911,
  s_adjusted: 0.901,
  tis_current: 0.0,
  rule_matches: null,
};

// The EvaluationRow is not exported; render the view is heavy. Instead
// test the row through the EvaluationsPanel path by importing the view
// module's internals indirectly — simplest robust route: render the
// whole view is not needed; we re-create rows via the exported default
// with mocked data is complex. So we test the terminology through a
// minimal harness component that mirrors production usage: the view
// file exports only the default; we therefore mount the view's
// EvaluationRow via a dedicated export below.
import { __testables } from '../GovernanceReplay';

const { EvaluationRow } = __testables;

describe('GovernanceReplay — calculation-semantics terminology', () => {
  it('labels a same-semantics v2 snapshot replay as Replay', () => {
    render(<EvaluationRow role="admin" ev={{
      ...BASE_EVAL,
      evaluation_strategy: 'runtime_snapshot',
      governance_input_snapshot: {
        calculation_version: 'tis-v2',
        replayed_from_calculation_version: 'tis-v2',
      },
    }} />);
    expect(
      screen.getByText('Replay (runtime snapshot · tis-v2)'),
    ).toBeInTheDocument();
    expect(
      screen.queryByText(/reevaluation/i),
    ).not.toBeInTheDocument();
  });

  it('labels a legacy-source evaluation as a counterfactual tis-v2 REEVALUATION, never a replay/reproduction', () => {
    render(<EvaluationRow role="admin" ev={{
      ...BASE_EVAL,
      evaluation_strategy: 'runtime_snapshot',
      governance_input_snapshot: {
        calculation_version: 'tis-v2',
        replayed_from_calculation_version: 'tis-v1-legacy',
      },
    }} />);
    expect(screen.getByText(
      'tis-v2 reevaluation (counterfactual · legacy v1 source)',
    )).toBeInTheDocument();
    // Changed outcomes are framed as semantics differences — not proof
    // the original decision was wrong.
    expect(screen.getByText(
      /difference in calculation semantics, not an error in the original/,
    )).toBeInTheDocument();
    expect(screen.getByText(
      /not a reproduction of the original decision/i,
    )).toBeInTheDocument();
    // Historical verification is named as the separate path.
    expect(screen.getByText(
      /frozen v1 certificate-replay path/i,
    )).toBeInTheDocument();
  });

  it('renders flat typed v2 rule matches with fact keys only', () => {
    render(<EvaluationRow role="admin" ev={{
      ...BASE_EVAL,
      rule_matches: [{
        schema_version: 1,
        rule_id: 'typed_rule',
        rule_version: 'v1',
        control_class: 'deterministic_bounded',
        safety_category: 'prohibited_action',
        decision_pressure: 'HOLD',
        matched_fact_keys: ['channel', 'pregnant', 'role'],
      }],
    }} />);
    expect(screen.getByText(/matched_fact_keys: channel, pregnant, role/))
      .toBeInTheDocument();
    expect(screen.getByText(/\(keys only\)/)).toBeInTheDocument();
  });
});
