// PlainLanguageExplanation — effective-beside-verdict invariant,
// observed/effective separation, ordered adjustments.

import { describe, expect, it } from 'vitest';
import { render, screen } from '@testing-library/react';

import PlainLanguageExplanation from '../PlainLanguageExplanation';
import { V2_CERT } from '../../test/fixtures';

describe('PlainLanguageExplanation — tis-v2 record', () => {
  // The numeric parenthetical renders on the gate-failure narrative
  // branch; a rule-supplied explanation takes narrative precedence, so
  // these two tests use the certificate without rule matches.
  const V2_NO_RULES = { ...V2_CERT, governance_rule_matches: [] };

  it('shows the EFFECTIVE score beside the failing gate verdict, verbatim', () => {
    const { container } = render(
      <PlainLanguageExplanation tc={V2_NO_RULES} />);
    // The failing dimension parenthetical carries the effective value
    // (component_scores.A = "0.9000") and the canonical threshold.
    expect(container.textContent)
      .toContain('A = 0.9000, required ≥ 0.9300');
  });

  it('never displays the raw/observed value beside a verdict', () => {
    const tc = {
      ...V2_NO_RULES,
      // Distinct values per tier so any confusion is detectable.
      component_scores_raw: {
        ...V2_CERT.component_scores_raw, A: '0.874999',
      },
      component_scores_observed: {
        ...V2_CERT.component_scores_observed, A: '0.8750',
      },
      component_scores: {
        ...V2_CERT.component_scores, A: '0.3000',
      },
    };
    const { container } = render(<PlainLanguageExplanation tc={tc} />);
    expect(container.textContent).toContain('A = 0.3000');
    expect(container.textContent).not.toContain('A = 0.8750');
    expect(container.textContent).not.toContain('A = 0.874999');
  });

  it('renders adjustments in recorded order with per-rule verbs', () => {
    render(<PlainLanguageExplanation tc={V2_CERT} />);
    const items = screen.getAllByRole('listitem');
    expect(items).toHaveLength(2);
    expect(items[0]).toHaveTextContent('0.9400');
    expect(items[0]).toHaveTextContent('clamped to');
    expect(items[0]).toHaveTextContent('0.3000');
    expect(items[1]).toHaveTextContent('0.3000');
    expect(items[1]).toHaveTextContent('set to');
    expect(items[1]).toHaveTextContent('0.0000');
    // The order is the recorded order: §19.1 before §19.2.
    expect(items[0]).toHaveTextContent('TCS_SPEC_19_1');
    expect(items[1]).toHaveTextContent('TCS_SPEC_19_2');
  });

  it('states that verdicts use post-adjustment (effective) scores', () => {
    render(<PlainLanguageExplanation tc={V2_CERT} />);
    expect(screen.getByText(
      /Gate verdicts and S_base use the post-adjustment \(effective\) scores/,
    )).toBeInTheDocument();
  });

  it('uses the flat typed rule explanation on v2 records', () => {
    render(<PlainLanguageExplanation tc={V2_CERT} />);
    expect(screen.getByText(/Lithium is contraindicated in pregnancy/))
      .toBeInTheDocument();
  });
});
