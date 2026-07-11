import { useEffect } from 'react';
import { AlertTriangle } from 'lucide-react';
import { useAppState } from './store';
import { useT } from './lib/i18n';
import { useGlobalShortcuts } from './lib/keyboard';
import { GlobalHeader, PatientsSidebar, ModeTabs } from './components/layout';
import { CommandPalette, NewPatientDialog, ToastStrip, EmailComposerDialog } from './components/overlays';
import { AdminUsersView } from './components/admin-users';
import { ContextRailContent } from './components/memory-ui';
import {
  PractitionerHasLearnedView,
  SettingsDataView,
} from './components/full-screen-overlays';
import { LoginView } from './login';
import { BootGate } from './boot-gate';
import {
  TodayMode, PatientMode, EncounterMode,
  ImagingMode, LabsMode, MemoryMode, ReportMode,
} from './modes';
import { ResearchWorkspace } from './components/research-workspace';

function ActiveMode() {
  const mode = useAppState((s) => s.activeMode);
  switch (mode) {
    case 'today':     return <TodayMode />;
    case 'patient':   return <PatientMode />;
    case 'encounter': return <EncounterMode />;
    case 'imaging':   return <ImagingMode />;
    case 'labs':      return <LabsMode />;
    case 'memory':    return <MemoryMode />;
    case 'report':    return <ReportMode />;
    case 'research':  return <ResearchWorkspace />;
  }
}

function ContextRail() {
  const open = useAppState((s) => s.contextRailOpen);
  const content = useAppState((s) => s.contextRailContent);
  if (!open) return null;
  if (content.kind !== 'closed') {
    return <ContextRailContent />;
  }
  return (
    <aside className="flex h-full w-[320px] shrink-0 flex-col border-l border-border bg-bg p-4">
      <div className="mb-3 text-caption font-medium uppercase tracking-wider text-text-tertiary">
        Context
      </div>
      <div className="text-body text-text-secondary">
        Click a citation in any message to see its verbatim source +
        provenance trail here.
      </div>
    </aside>
  );
}

/**
 * Banner shown across the top of MainShell when the backend reports
 * that the active LLM provider has no API key. We probe once on login
 * and show the banner persistently until the medic either configures
 * a key (Settings · LLM writes it; refreshLlmStatus is called on save
 * and clears the advisory) or dismisses for the session.
 *
 * The user can't dismiss permanently — they'd lose track of why chat
 * is broken — but they can collapse the banner if they want to focus
 * on non-LLM features (patient roster, imaging upload).
 */
function LlmKeyReminderBanner() {
  const t              = useT();
  const status         = useAppState((s) => s.llmStatus);
  const checked        = useAppState((s) => s.llmStatusChecked);
  const openSettings   = useAppState((s) => s.openSettingsOverlay);

  if (!checked || !status || !status.advisory) return null;

  // The backend's advisory string is provider-specific ("Gemini API key
  // not set" / "OpenAI API key not set"). We surface it verbatim — that
  // text isn't part of the i18n dictionary because it's diagnostic data,
  // not UI chrome. The CTA + tail sentence ARE translated.
  return (
    <div className="flex items-center justify-between gap-3 border-b border-caution/40 bg-caution/10 px-5 py-2 text-caption text-caution">
      <div className="flex min-w-0 items-center gap-2">
        <AlertTriangle size={14} className="shrink-0" />
        <span className="truncate">
          {status.advisory}
        </span>
      </div>
      <div className="flex shrink-0 items-center gap-2">
        <button
          onClick={openSettings}
          className="rounded-sm border border-caution/40 px-2 py-0.5 hover:bg-caution/20"
        >
          {t('banner.llmCta')}
        </button>
      </div>
    </div>
  );
}

/**
 * Top-of-page 患者 | 研究 segmented control (decisions D1 + D14).
 * Defaults to 'research' on first launch. Tracks the visual mock at
 * docs/design/visual-mock/Research Workspace.dc.html.
 */
function WorkspaceSwitcher() {
  const ws    = useAppState((s) => s.activeWorkspace);
  const setWs = useAppState((s) => s.setActiveWorkspace);
  const btn = (key: 'patient' | 'research', label: string, sub: string) => (
    <button
      onClick={() => setWs(key)}
      title={sub}
      className={`px-3.5 py-1 text-sm rounded-md transition-colors ${
        ws === key
          ? 'bg-rw-accent text-[#06252c] shadow-sm font-medium'
          : 'text-text-secondary hover:bg-gray-100 dark:hover:bg-gray-800'
      }`}
    >
      {label}
    </button>
  );
  return (
    <div className="border-b border-border bg-bg px-4 py-1.5 flex items-center gap-1">
      <span className="text-[10px] tracking-[0.18em] uppercase text-text-tertiary mr-2 font-mono">
        Workspace
      </span>
      <div className="inline-flex bg-surface border border-border rounded-lg p-0.5 gap-0.5">
        {btn('patient',  '患者', 'ad-hoc 单患者视角')}
        {btn('research', '研究', '研究优先工作台（默认）')}
      </div>
    </div>
  );
}

/**
 * Body switches between Patient layout (current PatientsSidebar / mode
 * tabs / ActiveMode / ContextRail) and the new ResearchWorkspace.
 */
function WorkspaceBody() {
  const ws = useAppState((s) => s.activeWorkspace);
  if (ws === 'research') {
    return <ResearchWorkspace />;
  }
  return (
    <>
      <PatientsSidebar />
      <div className="flex min-w-0 flex-1 flex-col">
        <ModeTabs />
        <main className="min-h-0 flex-1 overflow-y-auto">
          <ActiveMode />
        </main>
      </div>
      <ContextRail />
    </>
  );
}

function MainShell() {
  const token            = useAppState((s) => s.token);
  const refreshLlmStatus = useAppState((s) => s.refreshLlmStatus);

  // Probe the LLM settings once we have a token. This is the "on
  // startup, if there's no API key, remind the user" hook the medic
  // asked for. Re-probe whenever the token changes (sign-in flow).
  useEffect(() => {
    if (token) refreshLlmStatus();
  }, [token, refreshLlmStatus]);

  return (
    <div className="flex h-screen flex-col bg-bg text-text-primary">
      <GlobalHeader />
      <WorkspaceSwitcher />
      <LlmKeyReminderBanner />
      <div className="flex min-h-0 flex-1">
        <WorkspaceBody />
      </div>

      {/* Overlays — rendered outside the layout flow */}
      <CommandPalette />
      <NewPatientDialog />
      <PractitionerHasLearnedView />
      <SettingsDataView />
      <EmailComposerDialog />
      {/* Admin-only user management (renders null unless role==='admin') */}
      <AdminUsersView />
      <ToastStrip />
    </div>
  );
}

export default function App() {
  const token         = useAppState((s) => s.token);
  const bootHydrated  = useAppState((s) => s.bootHydrated);
  const logout        = useAppState((s) => s.logout);
  const showToast     = useAppState((s) => s.showToast);
  const t             = useT();

  useGlobalShortcuts();

  // The api-client fires this on any 401 outside the auth endpoints
  // (expired / invalid JWT — there is no silent re-auth path with
  // password auth). Wiping the token bounces us to the LoginView via
  // the conditional render below.
  useEffect(() => {
    const handler = () => logout();
    window.addEventListener('nexus:auth-expired', handler);
    return () => window.removeEventListener('nexus:auth-expired', handler);
  }, [logout]);

  // Global 403 account_disabled handler — an admin disabled this
  // account while its session was live. The api-client detects the
  // error-envelope code and fires this event; we log out and tell
  // the user why (instead of a wall of failing requests).
  useEffect(() => {
    const handler = () => {
      logout();
      showToast(t('auth.disabledToast'), 'error');
    };
    window.addEventListener('nexus:account-disabled', handler);
    return () => window.removeEventListener('nexus:account-disabled', handler);
  }, [logout, showToast, t]);

  // 2026-07 auth rework: the silent bootstrap sign-in endpoint is
  // gone — accounts are username+password now, so a fresh window goes
  // straight to LoginView. The JWT still survives page reloads within
  // one window via sessionStorage (hydrateAppState).

  // Avoid a one-frame login flicker before hydrate completes.
  if (!bootHydrated) return null;

  // BootGate blocks the LoginView/MainShell render until the FastAPI
  // sidecar's /healthz returns 200. Without this, the user could fill
  // in the login form during the 3–15 s Alembic-migration window and
  // see a "Cannot reach server" error fired against a half-booted
  // process.
  //
  // F23 — NEVER render ``null`` as the inner child. A black window
  // with no UI is the worst possible state for the medic; if we
  // can't tell which view to show, default to LoginView so they at
  // least have a recover surface (password auth, diagnostics panel). The auto-login still races and will swap
  // in MainShell the moment it returns a token.
  return (
    <BootGate>
      {token ? <MainShell /> : <LoginView />}
    </BootGate>
  );
}
