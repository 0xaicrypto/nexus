import { useEffect } from 'react';
import { Navigate, Route, Routes, useLocation, useNavigate } from 'react-router-dom';
import { api } from '@/lib/api-client';
import { ErrorBoundary } from '@/components/ErrorBoundary';
import { ChatPage } from '@/routes/chat';
import { LandingPage } from '@/routes/landing';
import { LoginPage } from '@/routes/login';
import { TodayPage } from '@/routes/today';
import { PatientsLayout, PatientSummaryPage, PatientChatPage } from '@/routes/patients';
import { ImagingPage } from '@/routes/imaging';
import { LabsPage } from '@/routes/labs';
import { MemoryGraphPage } from '@/routes/memory-graph';
import { ReportPage } from '@/routes/report-page';
import { MedicalRecordsPage } from '@/routes/medical-records';
import { ViewerPage } from '@/routes/viewer';
import { SettingsPage } from '@/routes/settings';
import { AdminUsersPage } from '@/routes/admin/users';
import { ResearchPage } from '@/routes/research';
import { ResearchDetailPage } from '@/routes/research-detail';
import { WritingPage } from '@/routes/writing';
import { WritingEditorPage } from '@/routes/writing-editor';
import { SkillsPage } from '@/routes/skills';
import { PluginsPage } from '@/routes/plugins';
import { KnowledgePage } from '@/routes/knowledge';
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
      <ErrorBoundary>
        <Routes>
          <Route path="/" element={<LandingPage />} />
          <Route path="/login" element={<LoginPage />} />
          <Route
            path="/app"
            element={
              <RequireAuth>
                <Navigate to="/app/today" replace />
              </RequireAuth>
            }
          />
          <Route
            path="/app/today"
            element={
              <RequireAuth>
                <TodayPage />
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
          <Route
            path="/app/patients"
            element={
              <RequireAuth>
                <PatientsLayout />
              </RequireAuth>
            }
          >
            <Route index element={<PatientSummaryPage />} />
            <Route path=":hash" element={<PatientSummaryPage />} />
            <Route path=":hash/chat" element={<PatientChatPage />} />
            <Route path=":hash/imaging" element={<ImagingPage />} />
            <Route path=":hash/labs" element={<LabsPage />} />
            <Route path=":hash/memory" element={<MemoryGraphPage />} />
            <Route path=":hash/report" element={<ReportPage />} />
            <Route path=":hash/records" element={<MedicalRecordsPage />} />
          </Route>
          <Route
            path="/app/viewer/:studyId"
            element={
              <RequireAuth>
                <ViewerPage />
              </RequireAuth>
            }
          />
          <Route
            path="/app/research"
            element={
              <RequireAuth>
                <ResearchPage />
              </RequireAuth>
            }
          />
          <Route
            path="/app/research/:studyId"
            element={
              <RequireAuth>
                <ResearchDetailPage />
              </RequireAuth>
            }
          />
          <Route
            path="/app/writing"
            element={
              <RequireAuth>
                <WritingPage />
              </RequireAuth>
            }
          />
          <Route
            path="/app/writing/:docId"
            element={
              <RequireAuth>
                <WritingEditorPage />
              </RequireAuth>
            }
          />
          <Route
            path="/app/skills"
            element={
              <RequireAuth>
                <SkillsPage />
              </RequireAuth>
            }
          />
          <Route
            path="/app/plugins"
            element={
              <RequireAuth>
                <PluginsPage />
              </RequireAuth>
            }
          />
          <Route
            path="/app/knowledge"
            element={
              <RequireAuth>
                <KnowledgePage />
              </RequireAuth>
            }
          />
          <Route
            path="/app/settings"
            element={
              <RequireAuth>
                <SettingsPage />
              </RequireAuth>
            }
          />
          <Route
            path="/app/admin/users"
            element={
              <RequireAuth>
                <AdminUsersPage />
              </RequireAuth>
            }
          />
          <Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
      </ErrorBoundary>
    </>
  );
}
