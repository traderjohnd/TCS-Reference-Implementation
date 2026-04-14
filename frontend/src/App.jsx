import { BrowserRouter, Routes, Route, Navigate } from 'react-router-dom';
import { AuthProvider, useAuth } from './hooks/useAuth';
import Layout from './components/Layout';
import Login from './views/Login';
import TrustOverview from './views/TrustOverview';
import LiveDecisions from './views/LiveDecisions';
import DriftMonitoring from './views/DriftMonitoring';
import PolicyControls from './views/PolicyControls';
import AuditCertificates from './views/AuditCertificates';
import EconomicView from './views/EconomicView';
import AdminPanel from './views/AdminPanel';

function ProtectedRoute({ children, view }) {
  const { user, canAccessView } = useAuth();
  if (!user) return <Navigate to="/login" replace />;
  if (view && !canAccessView(view)) return <Navigate to="/" replace />;
  return children;
}

function AppRoutes() {
  const { user } = useAuth();

  if (!user) {
    return (
      <Routes>
        <Route path="/login" element={<Login />} />
        <Route path="*" element={<Navigate to="/login" replace />} />
      </Routes>
    );
  }

  return (
    <Routes>
      <Route path="/login" element={<Navigate to="/" replace />} />
      <Route element={<Layout />}>
        <Route path="/" element={<ProtectedRoute view="overview"><TrustOverview /></ProtectedRoute>} />
        <Route path="/decisions" element={<ProtectedRoute view="decisions"><LiveDecisions /></ProtectedRoute>} />
        <Route path="/drift" element={<ProtectedRoute view="drift"><DriftMonitoring /></ProtectedRoute>} />
        <Route path="/policy" element={<ProtectedRoute view="policy"><PolicyControls /></ProtectedRoute>} />
        <Route path="/audit" element={<ProtectedRoute view="audit"><AuditCertificates /></ProtectedRoute>} />
        <Route path="/economic" element={<ProtectedRoute view="economic"><EconomicView /></ProtectedRoute>} />
        <Route path="/admin" element={<ProtectedRoute view="admin"><AdminPanel /></ProtectedRoute>} />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Route>
    </Routes>
  );
}

export default function App() {
  return (
    <BrowserRouter>
      <AuthProvider>
        <AppRoutes />
      </AuthProvider>
    </BrowserRouter>
  );
}
