import { useState } from 'react';
import { useAuth } from '../hooks/useAuth';

const ROLES = [
  'platform_admin',
  'governance_admin',
  'compliance_officer',
  'policy_editor',
  'workflow_owner',
  'auditor',
  'executive_viewer',
  'exception_approver',
];

export default function Login() {
  const { login } = useAuth();
  const [username, setUsername] = useState('');
  const [role, setRole] = useState('governance_admin');
  const [error, setError] = useState('');

  const handleSubmit = async (e) => {
    e.preventDefault();
    setError('');
    try {
      await login(username || 'admin', role);
    } catch (err) {
      setError(err.message);
    }
  };

  return (
    <div className="min-h-screen bg-gray-950 flex items-center justify-center">
      <div className="bg-gray-900 rounded-xl p-8 w-full max-w-md border border-gray-800">
        <div className="flex items-center gap-3 mb-6">
          <div className="w-10 h-10 bg-blue-600 rounded-lg flex items-center justify-center font-bold">
            TCS
          </div>
          <div>
            <h1 className="text-xl font-bold text-white">Control Plane</h1>
            <p className="text-xs text-gray-500">Trust Computation System v0.2.0</p>
          </div>
        </div>

        <form onSubmit={handleSubmit} className="space-y-4">
          <div>
            <label className="block text-sm text-gray-400 mb-1">Username</label>
            <input
              type="text"
              value={username}
              onChange={(e) => setUsername(e.target.value)}
              placeholder="admin"
              className="w-full bg-gray-800 border border-gray-700 rounded px-3 py-2 text-white text-sm focus:outline-none focus:border-blue-500"
            />
          </div>
          <div>
            <label className="block text-sm text-gray-400 mb-1">Role</label>
            <select
              value={role}
              onChange={(e) => setRole(e.target.value)}
              className="w-full bg-gray-800 border border-gray-700 rounded px-3 py-2 text-white text-sm focus:outline-none focus:border-blue-500"
            >
              {ROLES.map((r) => (
                <option key={r} value={r}>{r.replace(/_/g, ' ')}</option>
              ))}
            </select>
          </div>
          {error && <p className="text-red-400 text-sm">{error}</p>}
          <button
            type="submit"
            className="w-full bg-blue-600 hover:bg-blue-700 text-white py-2 rounded text-sm font-medium transition-colors"
          >
            Sign In
          </button>
        </form>
      </div>
    </div>
  );
}
