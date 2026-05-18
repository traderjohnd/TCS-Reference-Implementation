import { NavLink, Outlet } from 'react-router-dom';
import { useAuth } from '../hooks/useAuth';
import { useTheme } from '../hooks/useTheme';

const NAV_ITEMS = [
  { path: '/connections', label: 'Connections', view: 'connections' },
  { path: '/policy', label: 'Policy Controls', view: 'policy' },
  { path: '/chat', label: 'Governed Chat', view: 'chat' },
  { path: '/overview', label: 'Trust Overview', view: 'overview' },
  { path: '/decisions', label: 'Live Decisions', view: 'decisions' },
  { path: '/drift', label: 'Drift Monitoring', view: 'drift' },
  { path: '/audit', label: 'Audit & Certificates', view: 'audit' },
  { path: '/economic', label: 'Economic View', view: 'economic' },
  { path: '/telemetry', label: 'Telemetry', view: 'telemetry' },
  { path: '/archives', label: 'Archives', view: 'archives' },
  { path: '/admin', label: 'Admin', view: 'admin' },
];

export default function Layout() {
  const { user, logout, canAccessView } = useAuth();
  const { theme, toggle } = useTheme();

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
              <button
                onClick={toggle}
                className="flex items-center gap-1.5 text-xs px-2.5 py-1.5 rounded-full border border-gray-700 hover:border-gray-500 transition-colors"
                title={`Switch to ${theme === 'dark' ? 'light' : 'dark'} mode`}
              >
                {theme === 'dark' ? (
                  <>
                    <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 20 20" fill="currentColor" className="w-3.5 h-3.5 text-yellow-400">
                      <path d="M10 2a.75.75 0 01.75.75v1.5a.75.75 0 01-1.5 0v-1.5A.75.75 0 0110 2zM10 15a.75.75 0 01.75.75v1.5a.75.75 0 01-1.5 0v-1.5A.75.75 0 0110 15zM10 7a3 3 0 100 6 3 3 0 000-6zM15.657 5.404a.75.75 0 10-1.06-1.06l-1.061 1.06a.75.75 0 001.06 1.06l1.06-1.06zM6.464 14.596a.75.75 0 10-1.06-1.06l-1.06 1.06a.75.75 0 001.06 1.06l1.06-1.06zM18 10a.75.75 0 01-.75.75h-1.5a.75.75 0 010-1.5h1.5A.75.75 0 0118 10zM5 10a.75.75 0 01-.75.75h-1.5a.75.75 0 010-1.5h1.5A.75.75 0 015 10zM14.596 15.657a.75.75 0 001.06-1.06l-1.06-1.061a.75.75 0 10-1.06 1.06l1.06 1.06zM5.404 6.464a.75.75 0 001.06-1.06l-1.06-1.06a.75.75 0 10-1.06 1.06l1.06 1.06z" />
                    </svg>
                    <span className="text-gray-400">Light</span>
                  </>
                ) : (
                  <>
                    <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 20 20" fill="currentColor" className="w-3.5 h-3.5 text-blue-400">
                      <path fillRule="evenodd" d="M7.455 2.004a.75.75 0 01.26.77 7 7 0 009.958 7.967.75.75 0 011.067.853A8.5 8.5 0 116.647 1.921a.75.75 0 01.808.083z" clipRule="evenodd" />
                    </svg>
                    <span className="text-gray-400">Dark</span>
                  </>
                )}
              </button>
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
