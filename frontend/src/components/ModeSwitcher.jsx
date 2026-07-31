// ModeSwitcher — persistent DEMO MODE / LIVE MODE indicator + control
// (demo-live branch, Commit 1). Lives in the top toolbar so the active
// mode is visible from every view.
//
// Switching INTO Live Mode opens an explicit warning dialog; the
// backend additionally rejects unconfirmed switches and blocks
// external calls in Demo Mode regardless of what this control shows.
// Returning to Demo Mode is always a single click — the safe direction
// never needs ceremony.

import { useEffect, useState } from 'react';
import {
  DEMO_MODE, LIVE_MODE, useOperatingMode,
} from '../hooks/useOperatingMode';
import { useConnections } from '../hooks/useConnections';

export default function ModeSwitcher() {
  const { isDemo, loaded, switchMode, refresh } = useOperatingMode();
  const { clearTransientKeys } = useConnections();
  const [confirming, setConfirming] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);

  // Commit 6 hardening: the backend is the mode authority. Re-sync the
  // displayed mode whenever the window regains focus so a frontend/
  // backend disagreement (server restart, second tab) recovers to the
  // backend truth instead of persisting a stale badge.
  useEffect(() => {
    const onFocus = () => refresh();
    window.addEventListener('focus', onFocus);
    return () => window.removeEventListener('focus', onFocus);
  }, [refresh]);

  if (!loaded) {
    return (
      <span className="text-[10px] px-2.5 py-1 rounded-full border border-gray-700 text-gray-500">
        MODE…
      </span>
    );
  }

  const onIndicatorClick = () => {
    setError(null);
    if (isDemo) {
      setConfirming(true);          // entering LIVE requires the dialog
    } else {
      setBusy(true);
      switchMode(DEMO_MODE)
        .then(() => {
          // Commit 6 hardening: returning to Demo immediately clears
          // transient in-memory credentials and live execution state.
          // Saved non-secret connection metadata is untouched, and
          // already-rendered live results keep their truthful labels.
          if (typeof clearTransientKeys === 'function') clearTransientKeys();
        })
        .catch((e) => setError(e.message))
        .finally(() => setBusy(false));
    }
  };

  const onConfirmLive = () => {
    setBusy(true);
    switchMode(LIVE_MODE, { confirm: true })
      .then(() => setConfirming(false))
      .catch((e) => setError(e.message))
      .finally(() => setBusy(false));
  };

  return (
    <div className="relative">
      <button
        onClick={onIndicatorClick}
        disabled={busy}
        data-testid="mode-indicator"
        title={isDemo
          ? 'DEMO MODE — external LLM and web calls are blocked. Click to switch to LIVE MODE.'
          : 'LIVE MODE — real external provider calls are enabled. Click to return to DEMO MODE.'}
        className={`text-[11px] font-bold tracking-wider px-3 py-1.5 rounded-full border transition-colors ${
          isDemo
            ? 'bg-amber-900/40 text-amber-300 border-amber-700 hover:border-amber-500'
            : 'bg-red-900/40 text-red-300 border-red-600 hover:border-red-400'
        }`}
      >
        {isDemo ? 'DEMO MODE' : 'LIVE MODE'}
      </button>

      {confirming && (
        <div
          role="dialog"
          aria-label="Switch to Live Mode"
          className="absolute right-0 mt-2 w-80 z-50 bg-gray-900 border border-red-800 rounded-lg p-4 shadow-2xl"
        >
          <div className="text-sm font-semibold text-red-300 mb-2">
            Switch to LIVE MODE?
          </div>
          <p className="text-xs text-gray-300 leading-snug mb-3">
            Live Mode enables <span className="font-semibold">real external
            AI provider calls</span> (Live LLM and, where configured,
            Live Web). Responses may be unpredictable, provider charges
            may apply, and <span className="font-semibold">submitted
            content may leave the local environment</span>. Nothing is
            submitted or tested automatically by switching. Every
            response still passes through TIS v2 governance and is
            recorded with execution mode <span className="font-mono">live_provider</span>.
          </p>
          {error && (
            <p className="text-xs text-red-400 mb-2">{error}</p>
          )}
          <div className="flex gap-2 justify-end">
            <button
              onClick={() => { setConfirming(false); setError(null); }}
              className="text-xs px-3 py-1.5 rounded bg-gray-800 hover:bg-gray-700 text-gray-300"
            >
              Stay in Demo Mode
            </button>
            <button
              onClick={onConfirmLive}
              disabled={busy}
              className="text-xs px-3 py-1.5 rounded bg-red-700 hover:bg-red-600 text-white font-semibold"
            >
              {busy ? 'Switching…' : 'Enable LIVE MODE'}
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
