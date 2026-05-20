import { useEffect, useState } from 'react';
import { NavLink, Outlet } from 'react-router-dom';
import { useAuth } from '../hooks/useAuth';
import { useTheme } from '../hooks/useTheme';

// =============================================================================
// Phase 5 demo-hardening — left-side collapsible nav with grouped sections.
// =============================================================================
//
// Previously a 12-tab horizontal bar that exceeded legibility. The new
// layout splits the nav into a persistent left column grouped into four
// sections (RUN / MONITOR / AUDIT / SETUP) with a collapse-to-initials
// toggle. The top bar keeps branding + user controls + theme.
//
// Section order and item order are user-pinned. Renames are intentional:
// "Governed Chat" -> "Chat", "Trust Overview" -> "Overview", etc. The
// whole product is about governance; we don't need to repeat the word in
// every label.

const NAV_SECTIONS = [
  {
    id: 'run',
    label: 'RUN',
    items: [
      { path: '/chat',      label: 'Chat',      short: 'Ch', view: 'chat' },
      { path: '/decisions', label: 'Live',      short: 'Li', view: 'decisions' },
    ],
  },
  {
    id: 'monitor',
    label: 'MONITOR',
    items: [
      { path: '/overview',  label: 'Overview',       short: 'Ov', view: 'overview' },
      { path: '/telemetry', label: 'Telemetry',      short: 'Te', view: 'telemetry' },
      { path: '/drift',     label: 'Drift',          short: 'Dr', view: 'drift' },
      { path: '/economic',  label: 'Economic View',  short: 'Ec', view: 'economic' },
    ],
  },
  {
    id: 'audit',
    label: 'AUDIT',
    items: [
      { path: '/audit',     label: 'Certificates',        short: 'Ce', view: 'audit' },
      { path: '/replay',    label: 'Governance Replay',   short: 'GR', view: 'replay' },
      { path: '/archives',  label: 'Archives',            short: 'Ar', view: 'archives' },
    ],
  },
  {
    id: 'setup',
    label: 'SETUP',
    items: [
      { path: '/connections', label: 'Connections',     short: 'Cn', view: 'connections' },
      { path: '/policy',      label: 'Policy Controls', short: 'Po', view: 'policy' },
      { path: '/admin',       label: 'Admin',           short: 'Ad', view: 'admin' },
    ],
  },
];

function useCollapsedNav() {
  const [collapsed, setCollapsed] = useState(() => {
    return localStorage.getItem('tcs_nav_collapsed') === '1';
  });
  useEffect(() => {
    localStorage.setItem('tcs_nav_collapsed', collapsed ? '1' : '0');
  }, [collapsed]);
  return [collapsed, () => setCollapsed((v) => !v)];
}

// Per-section expand/collapse. Stores the set of section ids that
// are currently COLLAPSED (default = all expanded). Persisted as a
// comma-separated list in localStorage.
function useSectionCollapse() {
  const [collapsedIds, setCollapsedIds] = useState(() => {
    const raw = localStorage.getItem('tcs_nav_collapsed_sections') || '';
    return new Set(raw.split(',').filter(Boolean));
  });
  useEffect(() => {
    localStorage.setItem(
      'tcs_nav_collapsed_sections',
      Array.from(collapsedIds).join(','),
    );
  }, [collapsedIds]);
  const toggle = (id) => setCollapsedIds((prev) => {
    const next = new Set(prev);
    if (next.has(id)) next.delete(id);
    else next.add(id);
    return next;
  });
  return [collapsedIds, toggle];
}

function NavItem({ item, collapsed }) {
  return (
    <NavLink
      to={item.path}
      title={collapsed ? item.label : undefined}
      className={({ isActive }) => {
        const base = collapsed
          ? 'flex items-center justify-center w-10 h-10 mx-auto rounded text-xs font-mono transition-colors'
          : 'flex items-center gap-2 px-3 py-1.5 rounded text-sm transition-colors';
        const active = isActive
          ? 'bg-blue-900/30 text-white border-l-2 border-blue-500'
          : 'text-gray-400 hover:text-white hover:bg-gray-800';
        return `${base} ${active}`;
      }}
    >
      {collapsed ? (
        <span className="font-semibold">{item.short}</span>
      ) : (
        <span>{item.label}</span>
      )}
    </NavLink>
  );
}

function NavSection({
  section, collapsed, canAccessView,
  sectionCollapsed, onToggleSection,
}) {
  const visibleItems = section.items.filter((it) => canAccessView(it.view));
  if (visibleItems.length === 0) return null;

  // When the WHOLE nav is collapsed (icon-only mode), per-section
  // expand/collapse doesn't apply — all items render as icons.
  if (collapsed) {
    return (
      <div className="mb-4">
        <div className="border-t border-gray-800 my-2 mx-2" />
        <div className="space-y-1">
          {visibleItems.map((it) => (
            <NavItem key={it.path} item={it} collapsed={collapsed} />
          ))}
        </div>
      </div>
    );
  }

  // Expanded nav: section header is a clickable toggle.
  return (
    <div className="mb-3">
      <button
        onClick={() => onToggleSection(section.id)}
        className="w-full flex items-center justify-between px-3 py-1.5 rounded text-[11px] uppercase tracking-wider font-bold text-blue-300 hover:bg-gray-800/60 transition-colors"
        title={sectionCollapsed ? `Expand ${section.label}` : `Collapse ${section.label}`}
        aria-expanded={!sectionCollapsed}
      >
        <span>{section.label}</span>
        <svg
          xmlns="http://www.w3.org/2000/svg"
          viewBox="0 0 20 20"
          fill="currentColor"
          className={`w-3 h-3 transition-transform ${
            sectionCollapsed ? '-rotate-90' : ''
          }`}
        >
          <path
            fillRule="evenodd"
            d="M5.293 7.293a1 1 0 011.414 0L10 10.586l3.293-3.293a1 1 0 111.414 1.414l-4 4a1 1 0 01-1.414 0l-4-4a1 1 0 010-1.414z"
            clipRule="evenodd"
          />
        </svg>
      </button>
      {!sectionCollapsed && (
        <div className="space-y-0.5 mt-1">
          {visibleItems.map((it) => (
            <NavItem key={it.path} item={it} collapsed={collapsed} />
          ))}
        </div>
      )}
    </div>
  );
}

export default function Layout() {
  const { user, logout, canAccessView } = useAuth();
  const { theme, toggle } = useTheme();
  const [collapsed, toggleCollapsed] = useCollapsedNav();
  const [collapsedSections, toggleSection] = useSectionCollapse();

  return (
    <div className="min-h-screen bg-gray-950 text-gray-100">
      {/* ─── Top bar: branding + theme + user ───────────────────────────── */}
      <header className="bg-gray-900 border-b border-gray-800">
        <div className="max-w-full mx-auto px-4 py-3 flex items-center justify-between">
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
      </header>

      {/* ─── Body: left nav + content ───────────────────────────────────── */}
      <div className="flex">
        <aside
          className={`bg-gray-900 border-r border-gray-800 min-h-[calc(100vh-57px)] transition-[width] duration-150 ${
            collapsed ? 'w-14' : 'w-56'
          }`}
        >
          <div className="sticky top-0 py-3">
            <button
              onClick={toggleCollapsed}
              className={`mb-3 mx-auto flex items-center justify-center w-10 h-8 rounded text-xs text-gray-400 hover:text-white hover:bg-gray-800 transition-colors ${
                collapsed ? '' : 'ml-auto mr-2'
              }`}
              title={collapsed ? 'Expand navigation' : 'Collapse navigation'}
              aria-label={collapsed ? 'Expand navigation' : 'Collapse navigation'}
            >
              {collapsed ? (
                /* chevron-double-right */
                <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 20 20" fill="currentColor" className="w-4 h-4">
                  <path fillRule="evenodd" d="M3.293 4.293a1 1 0 011.414 0L9 8.586l4.293-4.293a1 1 0 111.414 1.414L10.414 10l4.293 4.293a1 1 0 01-1.414 1.414L9 11.414l-4.293 4.293a1 1 0 01-1.414-1.414L7.586 10 3.293 5.707a1 1 0 010-1.414z" clipRule="evenodd" transform="rotate(45 10 10)" />
                  <path fillRule="evenodd" d="M3 5a1 1 0 011-1h2a1 1 0 010 2H5v9h9V8a1 1 0 112 0v7a1 1 0 01-1 1H4a1 1 0 01-1-1V5z" clipRule="evenodd" />
                </svg>
              ) : (
                /* chevron-double-left */
                <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 20 20" fill="currentColor" className="w-4 h-4">
                  <path fillRule="evenodd" d="M12.707 5.293a1 1 0 010 1.414L9.414 10l3.293 3.293a1 1 0 01-1.414 1.414l-4-4a1 1 0 010-1.414l4-4a1 1 0 011.414 0z" clipRule="evenodd" />
                </svg>
              )}
            </button>
            <nav className={collapsed ? 'px-1' : 'px-2'}>
              {NAV_SECTIONS.map((section) => (
                <NavSection
                  key={section.id}
                  section={section}
                  collapsed={collapsed}
                  canAccessView={canAccessView}
                  sectionCollapsed={collapsedSections.has(section.id)}
                  onToggleSection={toggleSection}
                />
              ))}
            </nav>
          </div>
        </aside>

        <main className="flex-1 min-w-0 px-6 py-6">
          <Outlet />
        </main>
      </div>
    </div>
  );
}
