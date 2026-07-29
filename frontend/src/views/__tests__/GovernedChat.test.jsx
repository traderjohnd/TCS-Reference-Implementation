// GovernedChat — protected-metadata 422 envelope handling and the
// govern-request typed-field contract (owner guardrails 3 and 5).

import { describe, expect, it, vi, beforeEach } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import {
  ORDINARY_422_BODY,
  PROTECTED_METADATA_422_BODY,
} from '../../test/fixtures';

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

import { ApiError } from '../../hooks/useApi';
import GovernedChat, { ProtectedMetadataNotice } from '../GovernedChat';

function renderChat() {
  return render(
    <MemoryRouter>
      <GovernedChat />
    </MemoryRouter>,
  );
}

beforeEach(() => {
  h.apiPost.mockReset();
  localStorage.clear();
});

describe('GovernedChat — protected-metadata violation', () => {
  it('renders message + rejected key NAMES, never values, and stays usable', async () => {
    const user = userEvent.setup();
    h.apiPost.mockRejectedValueOnce(new ApiError(
      PROTECTED_METADATA_422_BODY.detail.message,
      422,
      PROTECTED_METADATA_422_BODY.detail,
    ));
    renderChat();

    const input = screen.getByPlaceholderText(/question/i);
    await user.type(input, 'What allocation?');
    await user.click(screen.getByRole('button', { name: /send/i }));

    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent(
      'Request rejected — protected governance metadata');
    expect(alert).toHaveTextContent('typed request fields');
    expect(alert).toHaveTextContent('nested.C_score');
    expect(alert).toHaveTextContent('risk_tier');

    // Usable after the error: input enabled, a second send goes out.
    expect(input).toBeEnabled();
    h.apiPost.mockResolvedValueOnce({
      response: 'ok', blocked: false, decision: 'Allow',
      certificate_id: null,
    });
    await user.type(input, 'Second question');
    await user.click(screen.getByRole('button', { name: /send/i }));
    expect(await screen.findByText('ok')).toBeInTheDocument();
  });

  it('handles an ordinary validation 422 separately (no protected notice)', async () => {
    const user = userEvent.setup();
    h.apiPost.mockRejectedValueOnce(new ApiError(
      'body.candidate_answer: Field required', 422, ORDINARY_422_BODY.detail,
    ));
    renderChat();

    await user.type(screen.getByPlaceholderText(/question/i), 'q1');
    await user.click(screen.getByRole('button', { name: /send/i }));

    expect(await screen.findByText(/Field required/)).toBeInTheDocument();
    expect(screen.queryByText(
      'Request rejected — protected governance metadata',
    )).not.toBeInTheDocument();
  });
});

describe('ProtectedMetadataNotice', () => {
  it('lists key names only', () => {
    render(<ProtectedMetadataNotice violation={{
      message: 'msg', rejectedKeys: ['a', 'b.c'],
    }} />);
    expect(screen.getByText('a')).toBeInTheDocument();
    expect(screen.getByText('b.c')).toBeInTheDocument();
  });
});
