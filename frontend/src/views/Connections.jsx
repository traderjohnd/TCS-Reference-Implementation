import { useState } from 'react';
import { useConnections } from '../hooks/useConnections';

// ─── Status indicator ───────────────────────────────────────────────────────

const STATUS_STYLES = {
  connected:    { dot: 'bg-green-400', text: 'text-green-400', label: 'Connected' },
  disconnected: { dot: 'bg-gray-500',  text: 'text-gray-500',  label: 'Not tested' },
  testing:      { dot: 'bg-yellow-400 animate-pulse', text: 'text-yellow-400', label: 'Testing...' },
  error:        { dot: 'bg-red-400',   text: 'text-red-400',   label: 'Error' },
};

function StatusDot({ status }) {
  const s = STATUS_STYLES[status] || STATUS_STYLES.disconnected;
  return (
    <div className="flex items-center gap-2">
      <span className={`w-2.5 h-2.5 rounded-full ${s.dot}`} />
      <span className={`text-xs font-medium ${s.text}`}>{s.label}</span>
    </div>
  );
}

// ─── Provider icons (simple SVG placeholders) ───────────────────────────────

function ProviderIcon({ type }) {
  const colors = {
    openai: 'bg-emerald-600',
    anthropic: 'bg-orange-600',
    mock: 'bg-gray-600',
  };
  const labels = { openai: 'OA', anthropic: 'An', mock: 'Mk' };
  return (
    <div className={`w-10 h-10 rounded-lg ${colors[type] || 'bg-blue-600'} flex items-center justify-center text-white text-xs font-bold shrink-0`}>
      {labels[type] || type.slice(0, 2).toUpperCase()}
    </div>
  );
}

// ─── Connection Card ────────────────────────────────────────────────────────

function ConnectionCard({ conn, isActive, onSetActive, onTest, onDelete, onUpdateKey }) {
  const [showKey, setShowKey] = useState(false);
  const [keyValue, setKeyValue] = useState(conn.config.apiKey || '');
  const isMock = conn.type === 'mock';
  const needsKey = !isMock && !conn.config.apiKey;

  const handleKeyChange = (e) => {
    const val = e.target.value;
    setKeyValue(val);
    onUpdateKey(conn.id, val);
  };

  const borderColor = isActive
    ? 'border-l-blue-500'
    : conn.status === 'connected'
    ? 'border-l-green-600'
    : conn.status === 'error'
    ? 'border-l-red-600'
    : 'border-l-gray-700';

  return (
    <div className={`bg-gray-900 rounded-lg border border-gray-800 border-l-4 ${borderColor} p-4 transition-all`}>
      <div className="flex items-start gap-3">
        <ProviderIcon type={conn.type} />
        <div className="flex-1 min-w-0">
          {/* Header row */}
          <div className="flex items-center justify-between">
            <div>
              <h4 className="text-sm font-medium text-white">{conn.name}</h4>
              <div className="flex items-center gap-3 mt-0.5">
                <span className="text-xs text-gray-500">{conn.config.model}</span>
                <span className="text-[10px] font-mono text-gray-600 bg-gray-800 px-1.5 py-0.5 rounded">
                  {conn.ctType}
                </span>
              </div>
            </div>
            <StatusDot status={conn.status} />
          </div>

          {/* API Key input (non-mock only) */}
          {!isMock && (
            <div className="mt-3">
              <label className="block text-[11px] text-gray-500 mb-1">API Key</label>
              <div className="flex gap-2">
                <div className="relative flex-1">
                  <input
                    type={showKey ? 'text' : 'password'}
                    value={keyValue}
                    onChange={handleKeyChange}
                    placeholder={conn.type === 'openai' ? 'sk-...' : 'sk-ant-...'}
                    className="w-full bg-gray-800 border border-gray-700 rounded px-3 py-1.5 text-sm text-white placeholder-gray-600 focus:outline-none focus:border-blue-500 pr-16"
                  />
                  <button
                    type="button"
                    onClick={() => setShowKey(!showKey)}
                    className="absolute right-2 top-1/2 -translate-y-1/2 text-[10px] text-gray-500 hover:text-gray-300"
                  >
                    {showKey ? 'hide' : 'show'}
                  </button>
                </div>
              </div>
            </div>
          )}

          {/* Error message */}
          {conn.errorMessage && (
            <p className="mt-2 text-xs text-red-400 bg-red-900/20 rounded px-2 py-1">
              {conn.errorMessage}
            </p>
          )}

          {/* Last tested */}
          {conn.lastTested && (
            <p className="mt-1 text-[10px] text-gray-600">
              Last tested: {new Date(conn.lastTested).toLocaleTimeString()}
            </p>
          )}

          {/* Actions */}
          <div className="flex items-center gap-2 mt-3">
            <button
              onClick={() => onTest(conn.id)}
              disabled={conn.status === 'testing' || (!isMock && needsKey)}
              className="text-xs px-3 py-1.5 rounded border border-gray-700 text-gray-300 hover:text-white hover:border-gray-500 disabled:opacity-40 disabled:cursor-not-allowed transition-colors"
            >
              {conn.status === 'testing' ? 'Testing...' : 'Test Connection'}
            </button>
            <button
              onClick={() => onSetActive(conn.id)}
              disabled={isActive}
              className={`text-xs px-3 py-1.5 rounded transition-colors ${
                isActive
                  ? 'bg-blue-600/20 text-blue-400 border border-blue-700 cursor-default'
                  : 'border border-gray-700 text-gray-300 hover:text-white hover:border-blue-500'
              }`}
            >
              {isActive ? 'Active' : 'Set Active'}
            </button>
            {!isMock && (
              <button
                onClick={() => onDelete(conn.id)}
                className="text-xs px-3 py-1.5 rounded border border-gray-700 text-gray-500 hover:text-red-400 hover:border-red-700 transition-colors ml-auto"
              >
                Remove
              </button>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}

// ─── Add Connection Form ────────────────────────────────────────────────────

function AddConnectionForm({ providerCatalog, onAdd, onCancel }) {
  const realProviders = providerCatalog.filter((p) => p.requires_key);
  const [provider, setProvider] = useState(realProviders[0]?.id || 'openai');
  const [apiKey, setApiKey] = useState('');
  const [model, setModel] = useState('');
  const [name, setName] = useState('');

  const currentProvider = realProviders.find((p) => p.id === provider);
  const models = currentProvider?.models || [];

  // Set default model when provider changes
  const handleProviderChange = (e) => {
    const val = e.target.value;
    setProvider(val);
    const p = realProviders.find((x) => x.id === val);
    if (p?.models?.length) setModel(p.models[0]);
  };

  // Set initial model
  if (!model && models.length) setModel(models[0]);

  const handleSubmit = (e) => {
    e.preventDefault();
    if (!apiKey.trim()) return;
    onAdd({
      category: 'llm',
      type: provider,
      name: name.trim() || `${currentProvider?.name || provider} Connection`,
      config: { model, apiKey: apiKey.trim() },
    });
  };

  return (
    <form onSubmit={handleSubmit} className="bg-gray-900 rounded-lg border border-blue-800/50 border-dashed p-4 space-y-3">
      <h4 className="text-sm font-medium text-blue-400">New LLM Connection</h4>

      <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
        <div>
          <label className="block text-[11px] text-gray-500 mb-1">Provider</label>
          <select
            value={provider}
            onChange={handleProviderChange}
            className="w-full bg-gray-800 border border-gray-700 rounded px-3 py-2 text-white text-sm"
          >
            {realProviders.map((p) => (
              <option key={p.id} value={p.id}>{p.name}</option>
            ))}
          </select>
        </div>
        <div>
          <label className="block text-[11px] text-gray-500 mb-1">Model</label>
          <select
            value={model}
            onChange={(e) => setModel(e.target.value)}
            className="w-full bg-gray-800 border border-gray-700 rounded px-3 py-2 text-white text-sm"
          >
            {models.map((m) => (
              <option key={m} value={m}>{m}</option>
            ))}
          </select>
        </div>
      </div>

      <div>
        <label className="block text-[11px] text-gray-500 mb-1">API Key</label>
        <input
          type="password"
          value={apiKey}
          onChange={(e) => setApiKey(e.target.value)}
          placeholder={provider === 'openai' ? 'sk-...' : 'sk-ant-...'}
          className="w-full bg-gray-800 border border-gray-700 rounded px-3 py-2 text-white text-sm placeholder-gray-600 focus:outline-none focus:border-blue-500"
          required
        />
      </div>

      <div>
        <label className="block text-[11px] text-gray-500 mb-1">Connection Name (optional)</label>
        <input
          type="text"
          value={name}
          onChange={(e) => setName(e.target.value)}
          placeholder={`My ${currentProvider?.name || ''} Connection`}
          className="w-full bg-gray-800 border border-gray-700 rounded px-3 py-2 text-white text-sm placeholder-gray-600 focus:outline-none focus:border-blue-500"
        />
      </div>

      <div className="flex gap-2 pt-1">
        <button
          type="submit"
          disabled={!apiKey.trim()}
          className="text-xs px-4 py-2 rounded bg-blue-600 hover:bg-blue-700 disabled:opacity-40 text-white font-medium transition-colors"
        >
          Add Connection
        </button>
        <button
          type="button"
          onClick={onCancel}
          className="text-xs px-4 py-2 rounded border border-gray-700 text-gray-400 hover:text-white transition-colors"
        >
          Cancel
        </button>
      </div>
    </form>
  );
}

// ─── Stubbed Section ────────────────────────────────────────────────────────

function StubSection({ title, description, icon }) {
  return (
    <div className="bg-gray-900/50 rounded-lg border border-gray-800 border-dashed p-6 text-center">
      <div className="w-12 h-12 bg-gray-800 rounded-xl flex items-center justify-center mx-auto mb-3">
        <span className="text-xl text-gray-500">{icon}</span>
      </div>
      <h4 className="text-sm font-medium text-gray-400 mb-1">{title}</h4>
      <p className="text-xs text-gray-600 max-w-md mx-auto mb-3">{description}</p>
      <span className="inline-block text-[10px] font-medium text-gray-500 bg-gray-800 px-2 py-0.5 rounded-full border border-gray-700">
        Coming Soon
      </span>
    </div>
  );
}

// ─── Main Connections View ──────────────────────────────────────────────────

export default function Connections() {
  const {
    llmConnections,
    ragConnections,
    apiConnections,
    activeLlmId,
    providerCatalog,
    addConnection,
    updateConnection,
    removeConnection,
    testConnection,
    setActiveLlm,
  } = useConnections();

  const [showAddForm, setShowAddForm] = useState(false);

  const handleAdd = (conn) => {
    const id = addConnection(conn);
    setShowAddForm(false);
    // Auto-set as active if it's the first real provider
    if (llmConnections.filter((c) => c.type !== 'mock').length === 0) {
      setActiveLlm(id);
    }
  };

  const handleUpdateKey = (id, key) => {
    updateConnection(id, { config: { apiKey: key }, status: 'disconnected', errorMessage: null });
  };

  // Count stats
  const totalConnections = llmConnections.length + ragConnections.length + apiConnections.length;
  const connectedCount = [...llmConnections, ...ragConnections, ...apiConnections].filter(
    (c) => c.status === 'connected'
  ).length;

  return (
    <div className="space-y-8">
      {/* Header */}
      <div>
        <h2 className="text-lg font-semibold text-white">Connections</h2>
        <p className="text-xs text-gray-500 mt-1">
          Configure your data sources and AI providers. Each connection is governed by the TCS trust engine.
        </p>
        <div className="flex items-center gap-4 mt-3">
          <div className="flex items-center gap-2 text-xs">
            <span className="w-2 h-2 rounded-full bg-green-400" />
            <span className="text-gray-400">{connectedCount} connected</span>
          </div>
          <div className="flex items-center gap-2 text-xs">
            <span className="w-2 h-2 rounded-full bg-gray-500" />
            <span className="text-gray-400">{totalConnections - connectedCount} pending</span>
          </div>
        </div>
      </div>

      {/* ── LLM Providers ──────────────────────────────────────────────────── */}
      <section>
        <div className="flex items-center justify-between mb-3">
          <div>
            <h3 className="text-sm font-medium text-white">LLM Providers</h3>
            <p className="text-[11px] text-gray-500 mt-0.5">
              Language models for governed chat and AI workflows.
              <span className="ml-1 font-mono text-gray-600">CT-1 (API)</span>
            </p>
          </div>
          {!showAddForm && (
            <button
              onClick={() => setShowAddForm(true)}
              className="text-xs px-3 py-1.5 rounded bg-blue-600 hover:bg-blue-700 text-white font-medium transition-colors"
            >
              + Add Provider
            </button>
          )}
        </div>

        <div className="space-y-3">
          {llmConnections.map((conn) => (
            <ConnectionCard
              key={conn.id}
              conn={conn}
              isActive={activeLlmId === conn.id}
              onSetActive={setActiveLlm}
              onTest={testConnection}
              onDelete={removeConnection}
              onUpdateKey={handleUpdateKey}
            />
          ))}

          {showAddForm && (
            <AddConnectionForm
              providerCatalog={providerCatalog}
              onAdd={handleAdd}
              onCancel={() => setShowAddForm(false)}
            />
          )}

          {llmConnections.length === 0 && !showAddForm && (
            <div className="bg-gray-900/50 rounded-lg border border-gray-800 border-dashed p-6 text-center">
              <p className="text-sm text-gray-400 mb-2">No LLM providers configured</p>
              <button
                onClick={() => setShowAddForm(true)}
                className="text-xs px-4 py-2 rounded bg-blue-600 hover:bg-blue-700 text-white font-medium transition-colors"
              >
                Add your first provider
              </button>
            </div>
          )}
        </div>
      </section>

      {/* ── RAG Sources ────────────────────────────────────────────────────── */}
      <section>
        <div className="mb-3">
          <h3 className="text-sm font-medium text-white">RAG Sources</h3>
          <p className="text-[11px] text-gray-500 mt-0.5">
            Vector databases and document stores for retrieval-augmented generation.
            <span className="ml-1 font-mono text-gray-600">CT-4 (Vector DB)</span>
          </p>
        </div>
        <StubSection
          title="RAG Data Sources"
          description="Connect vector databases (Pinecone, Weaviate, ChromaDB) and document stores to provide context for governed AI interactions."
          icon="&#x1F50D;"
        />
      </section>

      {/* ── External APIs ──────────────────────────────────────────────────── */}
      <section>
        <div className="mb-3">
          <h3 className="text-sm font-medium text-white">External APIs</h3>
          <p className="text-[11px] text-gray-500 mt-0.5">
            Third-party data feeds, compliance services, and business APIs.
            <span className="ml-1 font-mono text-gray-600">CT-1 (API)</span>
          </p>
        </div>
        <StubSection
          title="External API Integrations"
          description="Connect bank feeds, compliance APIs, market data providers, and other external services. Each integration boundary is tracked by TCS."
          icon="&#x1F517;"
        />
      </section>
    </div>
  );
}
