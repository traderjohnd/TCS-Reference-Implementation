// ModeSwitcher — DEMO/LIVE indicator + deliberate-switch tests
// (demo-live branch, Commit 1).

import { describe, expect, it, vi, beforeEach } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

const h = vi.hoisted(() => ({
  apiFetch: vi.fn(),
  apiPost: vi.fn(),
}));
vi.mock('../../hooks/useApi', async () => {
  const actual = await vi.importActual('../../hooks/useApi');
  return {
    ...actual,
    apiFetch: (...a) => h.apiFetch(...a),
    apiPost: (...a) => h.apiPost(...a),
  };
});

import { OperatingModeProvider } from '../../hooks/useOperatingMode';
import ModeSwitcher from '../ModeSwitcher';

function renderSwitcher() {
  return render(
    <OperatingModeProvider>
      <ModeSwitcher />
    </OperatingModeProvider>,
  );
}

beforeEach(() => {
  h.apiFetch.mockReset();
  h.apiPost.mockReset();
  h.apiFetch.mockResolvedValue({
    mode: 'demo', default_mode: 'demo', external_calls_allowed: false,
    labels: { demo: 'DEMO MODE', live: 'LIVE MODE' },
  });
});

describe('ModeSwitcher', () => {
  it('shows the persistent DEMO MODE indicator by default', async () => {
    renderSwitcher();
    expect(await screen.findByText('DEMO MODE')).toBeInTheDocument();
  });

  it('switching to Live requires the explicit warning dialog', async () => {
    const user = userEvent.setup();
    renderSwitcher();
    await user.click(await screen.findByText('DEMO MODE'));

    // No API call yet — the dialog gates the switch.
    expect(h.apiPost).not.toHaveBeenCalled();
    const dialog = screen.getByRole('dialog', { name: /switch to live mode/i });
    expect(dialog).toHaveTextContent('real external provider calls');
    expect(dialog).toHaveTextContent('live_provider');

    h.apiPost.mockResolvedValueOnce({
      mode: 'live', default_mode: 'demo', external_calls_allowed: true,
      labels: { demo: 'DEMO MODE', live: 'LIVE MODE' },
    });
    await user.click(screen.getByRole('button', { name: /enable live mode/i }));
    expect(h.apiPost).toHaveBeenCalledWith('/mode', {
      mode: 'live', confirm: true,
    });
    expect(await screen.findByText('LIVE MODE')).toBeInTheDocument();
  });

  it('declining the dialog stays in Demo Mode with no API call', async () => {
    const user = userEvent.setup();
    renderSwitcher();
    await user.click(await screen.findByText('DEMO MODE'));
    await user.click(screen.getByRole('button', { name: /stay in demo mode/i }));
    expect(h.apiPost).not.toHaveBeenCalled();
    expect(screen.getByText('DEMO MODE')).toBeInTheDocument();
  });

  it('returning to Demo from Live needs no confirmation', async () => {
    h.apiFetch.mockResolvedValue({
      mode: 'live', default_mode: 'demo', external_calls_allowed: true,
      labels: { demo: 'DEMO MODE', live: 'LIVE MODE' },
    });
    h.apiPost.mockResolvedValueOnce({
      mode: 'demo', default_mode: 'demo', external_calls_allowed: false,
      labels: { demo: 'DEMO MODE', live: 'LIVE MODE' },
    });
    const user = userEvent.setup();
    renderSwitcher();
    await user.click(await screen.findByText('LIVE MODE'));
    expect(h.apiPost).toHaveBeenCalledWith('/mode', {
      mode: 'demo', confirm: false,
    });
    expect(await screen.findByText('DEMO MODE')).toBeInTheDocument();
  });
});
