import { createContext, useContext, useState, useCallback } from 'react';

const AuthContext = createContext(null);

const ROLE_VIEW_ACCESS = {
  platform_admin: ['overview', 'decisions', 'drift', 'policy', 'audit', 'economic', 'admin'],
  governance_admin: ['overview', 'decisions', 'drift', 'policy', 'audit', 'economic'],
  compliance_officer: ['overview', 'decisions', 'drift', 'policy', 'audit', 'economic'],
  policy_editor: ['overview', 'policy'],
  workflow_owner: ['decisions'],
  auditor: ['audit'],
  executive_viewer: ['overview', 'economic'],
  exception_approver: ['overview', 'decisions', 'drift', 'audit'],
};

export function AuthProvider({ children }) {
  const [user, setUser] = useState(() => {
    const stored = localStorage.getItem('tcs_user');
    return stored ? JSON.parse(stored) : null;
  });

  const login = useCallback(async (username, role) => {
    const res = await fetch('/v1/auth/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ username, role }),
    });
    if (!res.ok) throw new Error('Login failed');
    const data = await res.json();
    const userData = {
      token: data.token,
      username: data.username,
      userId: data.user_id,
      roles: data.roles,
    };
    localStorage.setItem('tcs_token', data.token);
    localStorage.setItem('tcs_user', JSON.stringify(userData));
    setUser(userData);
    return userData;
  }, []);

  const logout = useCallback(() => {
    localStorage.removeItem('tcs_token');
    localStorage.removeItem('tcs_user');
    setUser(null);
  }, []);

  const canAccessView = useCallback((view) => {
    if (!user) return false;
    return user.roles.some((role) => {
      const views = ROLE_VIEW_ACCESS[role] || [];
      return views.includes(view);
    });
  }, [user]);

  const hasEditAccess = useCallback(() => {
    if (!user) return false;
    return user.roles.some((r) =>
      ['platform_admin', 'governance_admin'].includes(r)
    );
  }, [user]);

  return (
    <AuthContext.Provider value={{ user, login, logout, canAccessView, hasEditAccess }}>
      {children}
    </AuthContext.Provider>
  );
}

export function useAuth() {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error('useAuth must be inside AuthProvider');
  return ctx;
}
