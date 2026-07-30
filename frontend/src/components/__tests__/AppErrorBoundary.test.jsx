// AppErrorBoundary — defense-in-depth containment tests.

import { describe, expect, it, vi, beforeEach, afterEach } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

import AppErrorBoundary from '../AppErrorBoundary';

function Bomb({ message }) {
  throw new TypeError(message);
}

let consoleSpy;
beforeEach(() => {
  // React logs caught render errors; capture so test output stays clean
  // and so the sanitized-logging assertion can inspect what was logged.
  consoleSpy = vi.spyOn(console, 'error').mockImplementation(() => {});
});
afterEach(() => consoleSpy.mockRestore());

describe('AppErrorBoundary', () => {
  it('contains a component exception instead of blanking the application', () => {
    render(
      <AppErrorBoundary>
        <Bomb message="x.toFixed is not a function" />
      </AppErrorBoundary>,
    );
    const alert = screen.getByRole('alert');
    expect(alert).toHaveTextContent('Display error in the operator surface');
    expect(alert).toHaveTextContent('display failure only');
    expect(alert).toHaveTextContent('TypeError');
    expect(screen.getByRole('button', { name: /return to application/i }))
      .toBeInTheDocument();
  });

  it('renders children normally when nothing throws', () => {
    render(
      <AppErrorBoundary>
        <div>operator surface</div>
      </AppErrorBoundary>,
    );
    expect(screen.getByText('operator surface')).toBeInTheDocument();
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });

  it('caps the displayed message so oversized payloads never render in full', () => {
    const secretish = 'boom ' + 'A'.repeat(500);
    render(
      <AppErrorBoundary>
        <Bomb message={secretish} />
      </AppErrorBoundary>,
    );
    const alert = screen.getByRole('alert');
    // Display cap is 160 chars — the full 500-char tail must not appear.
    expect(alert.textContent).not.toContain('A'.repeat(200));
  });

  it('logs through the sanitized path only (name, capped message, stack)', () => {
    render(
      <AppErrorBoundary>
        <Bomb message={'M'.repeat(500)} />
      </AppErrorBoundary>,
    );
    const boundaryCall = consoleSpy.mock.calls.find(
      (c) => typeof c[0] === 'string' && c[0].includes('caught by boundary'),
    );
    expect(boundaryCall).toBeTruthy();
    // Message argument is capped at 300 chars by sanitize().
    expect(boundaryCall[2].length).toBeLessThanOrEqual(300);
  });

  it('reset returns safely to the application shell', async () => {
    const user = userEvent.setup();
    const assign = vi.fn();
    const original = window.location;
    Object.defineProperty(window, 'location', {
      configurable: true,
      value: { ...original, assign },
    });
    render(
      <AppErrorBoundary>
        <Bomb message="boom" />
      </AppErrorBoundary>,
    );
    await user.click(
      screen.getByRole('button', { name: /return to application/i }));
    expect(assign).toHaveBeenCalledWith('/');
    Object.defineProperty(window, 'location', {
      configurable: true,
      value: original,
    });
  });
});
