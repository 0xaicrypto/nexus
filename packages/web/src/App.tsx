import { useEffect } from 'react';
import { Navigate, Route, Routes, useLocation, useNavigate } from 'react-router-dom';
import { api } from '@/lib/api-client';
import { ChatPage } from '@/routes/chat';
import { LandingPage } from '@/routes/landing';
import { LoginPage } from '@/routes/login';
import { useAuthStore } from '@/stores/auth';

function RequireAuth({ children }: { children: React.ReactNode }) {
  const { isAuthenticated } = useAuthStore();
  const location = useLocation();
  if (!isAuthenticated) {
    return <Navigate to="/login" state={{ from: location }} replace />;
  }
  return <>{children}</>;
}

function AuthEvents() {
  const navigate = useNavigate();
  const { clearSession } = useAuthStore();

  useEffect(() => {
    const handler = () => {
      api.logout();
      clearSession();
      navigate('/login', { replace: true });
    };
    window.addEventListener('nexus:auth-expired', handler);
    return () => window.removeEventListener('nexus:auth-expired', handler);
  }, [clearSession, navigate]);

  return null;
}

export default function App() {
  return (
    <>
      <AuthEvents />
      <Routes>
        <Route path="/" element={<LandingPage />} />
        <Route path="/login" element={<LoginPage />} />
        <Route
          path="/app"
          element={
            <RequireAuth>
              <Navigate to="/app/chat" replace />
            </RequireAuth>
          }
        />
        <Route
          path="/app/chat"
          element={
            <RequireAuth>
              <ChatPage />
            </RequireAuth>
          }
        />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Routes>
    </>
  );
}
