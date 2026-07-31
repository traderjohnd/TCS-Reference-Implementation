// useOperatingMode — global DEMO / LIVE operating mode (demo-live
// branch, Commit 1).
//
// The BACKEND owns the mode; this hook mirrors it and drives the
// toolbar control. Switching INTO Live Mode requires the explicit
// confirm flag (the backend rejects it otherwise), so an operator can
// never leave the investor-safe default by accident. Every view sees
// the same mode through this shared context.

import {
  createContext, useCallback, useContext, useEffect, useState,
} from 'react';
import { apiFetch, apiPost } from './useApi';

const ModeContext = createContext(null);

export const DEMO_MODE = 'demo';
export const LIVE_MODE = 'live';

export function OperatingModeProvider({ children }) {
  const [mode, setModeState] = useState(null);   // null until first fetch
  const [error, setError] = useState(null);

  const refresh = useCallback(() => {
    apiFetch('/mode')
      .then((d) => { setModeState(d.mode); setError(null); })
      .catch((e) => setError(e.message));
  }, []);

  useEffect(() => { refresh(); }, [refresh]);

  const switchMode = useCallback(async (target, { confirm = false } = {}) => {
    const d = await apiPost('/mode', { mode: target, confirm });
    setModeState(d.mode);
    return d.mode;
  }, []);

  return (
    <ModeContext.Provider value={{
      mode,
      isDemo: mode === DEMO_MODE,
      isLive: mode === LIVE_MODE,
      loaded: mode !== null,
      error,
      switchMode,
      refresh,
    }}>
      {children}
    </ModeContext.Provider>
  );
}

// eslint-disable-next-line react-refresh/only-export-components -- hook
// module by design (provider + hook), same pattern as the other hooks.
export function useOperatingMode() {
  const ctx = useContext(ModeContext);
  if (!ctx) {
    // Defensive default: treat as DEMO (the backend enforces anyway).
    return {
      mode: DEMO_MODE, isDemo: true, isLive: false, loaded: false,
      error: null, switchMode: async () => DEMO_MODE, refresh: () => {},
    };
  }
  return ctx;
}
