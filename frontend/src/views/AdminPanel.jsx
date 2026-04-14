import { useState } from 'react';
import { useApi, apiPost } from '../hooks/useApi';

const AVAILABLE_ROLES = [
  'platform_admin', 'governance_admin', 'compliance_officer',
  'policy_editor', 'workflow_owner', 'auditor',
  'executive_viewer', 'exception_approver',
];

export default function AdminPanel() {
  const { data: users, refetch: refetchUsers } = useApi('/admin/users');
  const { data: modules } = useApi('/admin/modules');
  const [newUser, setNewUser] = useState({ username: '', role: 'governance_admin' });
  const [creating, setCreating] = useState(false);

  const userList = users?.users || [];
  const moduleList = modules?.modules || {};

  const createUser = async (e) => {
    e.preventDefault();
    if (!newUser.username) return;
    setCreating(true);
    try {
      await apiPost('/admin/users', {
        user_id: `user-${newUser.username}`,
        username: newUser.username,
        roles: [newUser.role],
      });
      setNewUser({ username: '', role: 'governance_admin' });
      refetchUsers();
    } catch (err) {
      alert(err.message);
    }
    setCreating(false);
  };

  return (
    <div className="space-y-6">
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
        <div className="bg-gray-900 rounded-lg border border-gray-800 p-4">
          <h3 className="text-sm font-medium text-gray-400 mb-3">User Management</h3>
          <form onSubmit={createUser} className="flex gap-2 mb-4">
            <input
              value={newUser.username}
              onChange={(e) => setNewUser({ ...newUser, username: e.target.value })}
              placeholder="Username"
              className="flex-1 bg-gray-800 border border-gray-700 rounded px-3 py-2 text-white text-sm"
            />
            <select
              value={newUser.role}
              onChange={(e) => setNewUser({ ...newUser, role: e.target.value })}
              className="bg-gray-800 border border-gray-700 rounded px-2 py-2 text-white text-sm"
            >
              {AVAILABLE_ROLES.map((r) => (
                <option key={r} value={r}>{r.replace(/_/g, ' ')}</option>
              ))}
            </select>
            <button type="submit" disabled={creating}
              className="bg-blue-700 hover:bg-blue-600 disabled:opacity-50 text-white px-4 py-2 rounded text-sm">
              Create
            </button>
          </form>
          <div className="space-y-2">
            {userList.map((u) => (
              <div key={u.token} className="bg-gray-800/50 rounded p-3 flex justify-between items-center">
                <div>
                  <span className="text-sm text-gray-300">{u.username}</span>
                  <span className="ml-2 text-xs text-gray-500">({u.roles.join(', ')})</span>
                </div>
                <span className="text-xs text-gray-600 font-mono">{u.token.substring(0, 12)}...</span>
              </div>
            ))}
            {userList.length === 0 && (
              <p className="text-gray-600 text-sm">No active sessions.</p>
            )}
          </div>
        </div>

        <div className="bg-gray-900 rounded-lg border border-gray-800 p-4">
          <h3 className="text-sm font-medium text-gray-400 mb-3">Module Configuration</h3>
          <div className="space-y-2">
            {Object.entries(moduleList).map(([name, info]) => (
              <div key={name} className="bg-gray-800/50 rounded p-3 flex justify-between items-center">
                <div>
                  <span className="text-sm text-gray-300">{name.replace(/_/g, ' ')}</span>
                  <span className="ml-2 text-xs text-gray-500">v{info.version}</span>
                </div>
                <span className={`text-xs px-2 py-0.5 rounded ${
                  info.status === 'active'
                    ? 'bg-green-900/50 text-green-400 border border-green-800'
                    : 'bg-gray-700 text-gray-400 border border-gray-600'
                }`}>
                  {info.status}
                </span>
              </div>
            ))}
          </div>
        </div>
      </div>
    </div>
  );
}
