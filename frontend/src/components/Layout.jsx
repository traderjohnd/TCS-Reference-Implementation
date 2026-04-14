import { NavLink, Outlet } from 'react-router-dom';
import { useAuth } from '../hooks/useAuth';

const NAV_ITEMS = [
  { path: '/', label: 'Trust Overview', view: 'overview' },
  { path: '/decisions', label: 'Live Decisions', view: 'decisions' },
  { path: '/drift', label: 'Drift Monitoring', view: 'drift' },
  { path: '/policy', label: 'Policy Controls', view: 'policy' },
  { path: '/audit', label: 'Audit & Certificates', view: 'audit' },
  { path: '/economic', label: 'Economic View', view: 'economic' },
  { path: '/admin', label: 'Admin', view: 'admin' },
];

export default function Layout() {
  const { user, logout, canAccessView } = useAuth();

  return (
    <div className="min-h-screen bg-gray-950 text-gray-100">
      <header className="bg-gray-900 border-b border-gray-800">
        <div className="max-w-7xl mx-auto px-4 py-3 flex items-center justify-between">
          <div className="flex items-center gap-3">
            <div className="w-8 h-8 bg-blue-600 rounded flex items-center justify-center font-bold text-sm">
              TCS
            </div>
            <h1 className="text-lg font-semibold text-white">Trust Computation System</h1>
            <span className="text-xs text-gray-500 bg-gray-800 px-2 py-0.5 rounded">v0.2.0</span>
          </div>
          {user && (
            <div className="flex items-center gap-4">
              <span className="text-sm text-gray-400">
                {user.username} <span className="text-gray-600">({user.roles.join(', ')})</span>
              </span>
              <button
                onClick={logout}
                className="text-sm text-gray-400 hover:text-white transition-colors"
              >
                Logout
              </button>
            </div>
          )}
        </div>
        <nav className="max-w-7xl mx-auto px-4 flex gap-1 overflow-x-auto">
          {NAV_ITEMS.filter(({ view }) => canAccessView(view)).map(({ path, label }) => (
            <NavLink
              key={path}
              to={path}
              className={({ isActive }) =>
                `px-3 py-2 text-sm font-medium rounded-t transition-colors whitespace-nowrap ${
                  isActive
                    ? 'bg-gray-800 text-white border-b-2 border-blue-500'
                    : 'text-gray-400 hover:text-white hover:bg-gray-800/50'
                }`
              }
            >
              {label}
            </NavLink>
          ))}
        </nav>
      </header>
      <main className="max-w-7xl mx-auto px-4 py-6">
        <Outlet />
      </main>
    </div>
  );
}
