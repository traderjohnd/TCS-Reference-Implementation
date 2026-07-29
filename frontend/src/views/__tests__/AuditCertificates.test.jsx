// AuditCertificates — v1/v2 rendering, score tiers, gate-beside-effective,
// ordered adjustments, typed provenance, versions, malformed-v2 integrity.

import { describe, expect, it, vi, beforeEach } from 'vitest';
import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import {
  V1_CERT, V2_CERT, V2_CERT_MALFORMED,
} from '../../test/fixtures';

const h = vi.hoisted(() => ({
  apiFetch: vi.fn(),
  listData: { current: null },
}));
vi.mock('../../hooks/useApi', () => ({
  useApi: () => ({ data: h.listData.current, refetch: vi.fn() }),
  usePolling: () => ({ data: null, refetch: vi.fn() }),
  apiFetch: (...args) => h.apiFetch(...args),
  apiPost: vi.fn(),
}));
const mockApiFetch = h.apiFetch;

import AuditCertificates from '../AuditCertificates';

function mockCertEndpoints(cert) {
  mockApiFetch.mockImplementation(async (path) => {
    if (path.startsWith('/certificates/')) return cert;
    if (path.startsWith('/artifacts/')) throw new Error('404');
    if (path.includes('override-history')) return { events: [] };
    return {};
  });
}

async function openDetail(user, subjectId) {
  await user.click(screen.getByText(subjectId));
  await screen.findByText('Decision Summary');
}

async function openTechnical(user) {
  await user.click(screen.getByText(/Technical Detail/i));
}

beforeEach(() => {
  mockApiFetch.mockReset();
  h.listData.current = {
    count: 3,
    certificates: [V2_CERT, V2_CERT_MALFORMED, V1_CERT],
  };
});

describe('AuditCertificates — archive list', () => {
  it('renders v2 decimal strings verbatim and v1 floats formatted, and a malformed neighbor never blanks the list', () => {
    mockCertEndpoints(V2_CERT);
    render(<AuditCertificates />);
    // v2 rows — canonical string verbatim (valid + malformed rows both
    // carry tis_current "0.0000" and both must render).
    expect(screen.getAllByText('0.0000').length).toBeGreaterThanOrEqual(2);
    // v1 row — float formatted at 4dp for display.
    expect(screen.getByText('0.8806')).toBeInTheDocument();
    // Malformed v2 neighbor still renders as a row (subject visible).
    expect(screen.getByText('subject-v2-malformed')).toBeInTheDocument();
    expect(screen.getByText('subject-v1-0001')).toBeInTheDocument();
  });
});

describe('AuditCertificates — v2 certificate detail', () => {
  it('shows schema/calculation versions and the gate_result vocabulary', async () => {
    const user = userEvent.setup();
    mockCertEndpoints(V2_CERT);
    render(<AuditCertificates />);
    await openDetail(user, 'subject-v2-0001');

    expect(screen.getByText(/gate: FAIL \(0\)/)).toBeInTheDocument();
    expect(screen.getByText(/schema v2 · tis-v2/)).toBeInTheDocument();

    await openTechnical(user);
    expect(
      screen.getByText('Schema & Calculation Versions')).toBeInTheDocument();
    expect(screen.getByText(
      'decimal-4dp-half-up-each-decision-stage-context28-v1',
    )).toBeInTheDocument();
    expect(screen.getByText(
      'decimal-exp-context28-half-even-then-4dp-half-up-v1',
    )).toBeInTheDocument();
  });

  it('renders raw → observed → effective tiers with the verdict beside the EFFECTIVE score', async () => {
    const user = userEvent.setup();
    mockCertEndpoints(V2_CERT);
    render(<AuditCertificates />);
    await openDetail(user, 'subject-v2-0001');
    await openTechnical(user);

    // Raw tier preserved at full precision, verbatim.
    expect(screen.getByText('0.899996')).toBeInTheDocument();
    // The A row pairs the failing verdict with the EFFECTIVE score.
    const effectiveA = screen.getByTestId('effective-A');
    expect(effectiveA).toHaveTextContent('0.9000');
    const row = effectiveA.closest('tr');
    expect(within(row).getByText('FAIL')).toBeInTheDocument();
    // The raw value is in the same row but NOT in the effective cell —
    // the number beside the verdict is the number that produced it.
    expect(effectiveA).not.toHaveTextContent('0.899996');
  });

  it('renders adjustments in recorded order with the correct verbs', async () => {
    const user = userEvent.setup();
    mockCertEndpoints(V2_CERT);
    render(<AuditCertificates />);
    await openDetail(user, 'subject-v2-0001');
    await openTechnical(user);

    const panel = screen.getByText('Adjustments Applied (2)').parentElement;
    const items = within(panel).getAllByRole('listitem');
    expect(items).toHaveLength(2);
    expect(items[0]).toHaveTextContent('0.9400');
    expect(items[0]).toHaveTextContent('clamped to');
    expect(items[0]).toHaveTextContent('TCS_SPEC_19_1');
    expect(items[1]).toHaveTextContent('set to');
    expect(items[1]).toHaveTextContent('TCS_SPEC_19_2');
  });

  it('renders the flat typed rule match with fact KEYS only', async () => {
    const user = userEvent.setup();
    mockCertEndpoints(V2_CERT);
    render(<AuditCertificates />);
    await openDetail(user, 'subject-v2-0001');
    await openTechnical(user);

    expect(screen.getByText(
      'human_composed_patient_specific_medication_in_pregnancy',
    )).toBeInTheDocument();
    expect(screen.getByText('pregnant')).toBeInTheDocument();
    // Fact VALUES never render.
    expect(screen.queryByText(/pregnant.*true/)).not.toBeInTheDocument();
  });
});

describe('AuditCertificates — malformed v2 detail', () => {
  it('shows the integrity warning with field names, never substituted values', async () => {
    const user = userEvent.setup();
    mockCertEndpoints(V2_CERT_MALFORMED);
    render(<AuditCertificates />);
    await openDetail(user, 'subject-v2-malformed');

    const alert = screen.getByRole('alert');
    expect(alert).toHaveTextContent('failed integrity validation');
    expect(within(alert).getByText('s_base')).toBeInTheDocument();
    expect(within(alert).getByText('gate_result')).toBeInTheDocument();
    // The malformed value itself is never echoed inside the warning.
    expect(alert).not.toHaveTextContent('0.911');
    // No legacy-alias fallback: gate shows the unknown state, not a
    // verdict derived from gate_passed.
    expect(screen.getByText('gate: —')).toBeInTheDocument();
  });
});

describe('AuditCertificates — v1 certificate detail', () => {
  it('renders the legacy record with derived gate and legacy version label', async () => {
    const user = userEvent.setup();
    mockCertEndpoints(V1_CERT);
    render(<AuditCertificates />);
    await openDetail(user, 'subject-v1-0001');

    expect(screen.getByText(/gate: PASS \(1\)/)).toBeInTheDocument();
    expect(screen.getByText(/schema v1 · legacy/)).toBeInTheDocument();
  });
});
