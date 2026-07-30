// GovernedChat — Demo Mode guard (demo-live branch, Commit 1): an
// operator cannot accidentally send a real provider request while
// DEMO MODE is active.

import { describe, expect, it, vi, beforeEach } from 'vitest';
import { render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';

const h = vi.hoisted(() => ({
  apiPost: vi.fn(),
  mode: { current: 'demo' },
  llm: { current: null },
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
  useConnections: () => ({ activeLlm: h.llm.current }),
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

import GovernedChat from '../GovernedChat';

function renderChat() {
  return render(<MemoryRouter><GovernedChat /></MemoryRouter>);
}

beforeEach(() => {
  h.apiPost.mockReset();
  localStorage.clear();
});

describe('GovernedChat — Demo Mode guard', () => {
  it('blocks sending to an external provider in Demo Mode with a clear notice', () => {
    h.mode.current = 'demo';
    h.llm.current = {
      type: 'openai', name: 'TCS Test Key',
      config: { model: 'gpt-4o-mini', apiKey: 'present-in-memory' },
    };
    renderChat();
    const alert = screen.getByRole('alert');
    expect(alert).toHaveTextContent('DEMO MODE');
    expect(alert).toHaveTextContent('blocked');
    expect(screen.getByPlaceholderText(/configure an llm connection|question/i))
      .toBeDisabled();
  });

  it('allows the deterministic mock provider in Demo Mode', () => {
    h.mode.current = 'demo';
    h.llm.current = {
      type: 'mock', name: 'Mock', config: { model: 'deterministic' },
    };
    renderChat();
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
    expect(screen.getByPlaceholderText(/question/i)).toBeEnabled();
    expect(screen.getByText(/deterministic scripted responses/i))
      .toBeInTheDocument();
  });

  it('allows the external provider in Live Mode and labels it Live LLM', () => {
    h.mode.current = 'live';
    h.llm.current = {
      type: 'openai', name: 'TCS Test Key',
      config: { model: 'gpt-4o-mini', apiKey: 'present-in-memory' },
    };
    renderChat();
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
    expect(screen.getByPlaceholderText(/question/i)).toBeEnabled();
    expect(screen.getByText(/Live LLM: TCS Test Key — gpt-4o-mini/))
      .toBeInTheDocument();
  });
});
