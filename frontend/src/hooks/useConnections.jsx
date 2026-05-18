import { createContext, useContext, useState, useEffect, useCallback, useMemo } from 'react';
import { apiFetch, apiPost } from './useApi';

const ConnectionsContext = createContext(null);

// TCS Connection Type mapping
const CT_TYPES = {
  openai: 'CT-1',
  anthropic: 'CT-1',
  mock: 'CT-1',
  pinecone: 'CT-4',
  weaviate: 'CT-4',
  chroma: 'CT-4',
  local_rag: 'CT-4',
  bank_api: 'CT-1',
  compliance_api: 'CT-1',
  web_source: 'CT-6',
};

const DEFAULT_MOCK = {
  id: 'mock-default',
  category: 'llm',
  type: 'mock',
  ctType: 'CT-1',
  name: 'Mock Provider',
  config: { model: 'deterministic', apiKey: '' },
  status: 'connected',
  lastTested: null,
  errorMessage: null,
};

function stripKeys(connections) {
  return connections.map((c) => ({
    ...c,
    config: { ...c.config, apiKey: undefined },
  }));
}

function loadPersistedState() {
  try {
    const raw = localStorage.getItem('tcs_connections');
    if (raw) {
      const parsed = JSON.parse(raw);
      return {
        connections: parsed.connections || [DEFAULT_MOCK],
        activeLlmId: parsed.activeLlmId || 'mock-default',
        activeRagId: parsed.activeRagId || null,
        activeApiId: parsed.activeApiId || null,
      };
    }
  } catch { /* ignore */ }
  return {
    connections: [DEFAULT_MOCK],
    activeLlmId: 'mock-default',
    activeRagId: null,
    activeApiId: null,
  };
}

export function ConnectionsProvider({ children }) {
  const initial = loadPersistedState();
  const [connections, setConnections] = useState(initial.connections);
  const [activeLlmId, setActiveLlmId] = useState(initial.activeLlmId);
  const [activeRagId, setActiveRagId] = useState(initial.activeRagId);
  const [activeApiId, setActiveApiId] = useState(initial.activeApiId);
  const [providerCatalog, setProviderCatalog] = useState([]);

  // Fetch available providers on mount
  useEffect(() => {
    apiFetch('/query/status')
      .then((data) => setProviderCatalog(data.providers || []))
      .catch(() => {});
  }, []);

  // Persist to localStorage (strip API keys)
  useEffect(() => {
    localStorage.setItem(
      'tcs_connections',
      JSON.stringify({
        connections: stripKeys(connections),
        activeLlmId,
        activeRagId,
        activeApiId,
      })
    );
  }, [connections, activeLlmId, activeRagId, activeApiId]);

  const addConnection = useCallback((conn) => {
    const id = conn.id || `${conn.type}-${Date.now()}`;
    const ctType = CT_TYPES[conn.type] || 'CT-1';
    setConnections((prev) => [
      ...prev,
      { ...conn, id, ctType, status: 'disconnected', lastTested: null, errorMessage: null },
    ]);
    return id;
  }, []);

  const updateConnection = useCallback((id, partial) => {
    setConnections((prev) =>
      prev.map((c) => (c.id === id ? { ...c, ...partial, config: { ...c.config, ...partial.config } } : c))
    );
  }, []);

  const removeConnection = useCallback(
    (id) => {
      setConnections((prev) => prev.filter((c) => c.id !== id));
      if (activeLlmId === id) setActiveLlmId(null);
      if (activeRagId === id) setActiveRagId(null);
      if (activeApiId === id) setActiveApiId(null);
    },
    [activeLlmId, activeRagId, activeApiId]
  );

  const testConnection = useCallback(async (id) => {
    setConnections((prev) =>
      prev.map((c) => (c.id === id ? { ...c, status: 'testing', errorMessage: null } : c))
    );
    const conn = connections.find((c) => c.id === id);
    if (!conn) return;

    try {
      const result = await apiPost('/connections/test', {
        category: conn.category,
        provider: conn.type,
        api_key: conn.config.apiKey || null,
        model: conn.config.model || null,
        endpoint: conn.config.endpoint || null,
      });
      setConnections((prev) =>
        prev.map((c) =>
          c.id === id
            ? {
                ...c,
                status: result.success ? 'connected' : 'error',
                lastTested: new Date().toISOString(),
                errorMessage: result.error || null,
              }
            : c
        )
      );
      return result;
    } catch (err) {
      setConnections((prev) =>
        prev.map((c) =>
          c.id === id
            ? { ...c, status: 'error', lastTested: new Date().toISOString(), errorMessage: err.message }
            : c
        )
      );
      return { success: false, error: err.message };
    }
  }, [connections]);

  const setActiveLlm = useCallback((id) => setActiveLlmId(id), []);
  const setActiveRag = useCallback((id) => setActiveRagId(id), []);
  const setActiveApi = useCallback((id) => setActiveApiId(id), []);

  const activeLlm = useMemo(
    () => connections.find((c) => c.id === activeLlmId) || null,
    [connections, activeLlmId]
  );
  const activeRag = useMemo(
    () => connections.find((c) => c.id === activeRagId) || null,
    [connections, activeRagId]
  );
  const activeApi = useMemo(
    () => connections.find((c) => c.id === activeApiId) || null,
    [connections, activeApiId]
  );

  const llmConnections = useMemo(() => connections.filter((c) => c.category === 'llm'), [connections]);
  const ragConnections = useMemo(() => connections.filter((c) => c.category === 'rag'), [connections]);
  const apiConnections = useMemo(() => connections.filter((c) => c.category === 'external_api'), [connections]);

  return (
    <ConnectionsContext.Provider
      value={{
        connections,
        llmConnections,
        ragConnections,
        apiConnections,
        activeLlm,
        activeRag,
        activeApi,
        activeLlmId,
        activeRagId,
        activeApiId,
        providerCatalog,
        addConnection,
        updateConnection,
        removeConnection,
        testConnection,
        setActiveLlm,
        setActiveRag,
        setActiveApi,
      }}
    >
      {children}
    </ConnectionsContext.Provider>
  );
}

export function useConnections() {
  const ctx = useContext(ConnectionsContext);
  if (!ctx) throw new Error('useConnections must be inside ConnectionsProvider');
  return ctx;
}
