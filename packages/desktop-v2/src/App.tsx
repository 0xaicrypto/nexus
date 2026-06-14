import { useEffect } from 'react';
import { AlertTriangle } from 'lucide-react';
import { useAppState } from './store';
import { useGlobalShortcuts } from './lib/keyboard';
import { GlobalHeader, PatientsSidebar, ModeTabs } from './components/layout';
import { CommandPalette, NewPatientDialog, ToastStrip, EmailComposerDialog } from './components/overlays';
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
  const status         = useAppState((s) => s.llmStatus);
  const checked        = useAppState((s) => s.llmStatusChecked);
  const openSettings   = useAppState((s) => s.openSettingsOverlay);

  if (!checked || !status || !status.advisory) return null;

  return (
    <div className="flex items-center justify-between gap-3 border-b border-caution/40 bg-caution/10 px-5 py-2 text-caption text-caution">
      <div className="flex min-w-0 items-center gap-2">
        <AlertTriangle size={14} className="shrink-0" />
        <span className="truncate">
          {status.advisory} Chat and reasoning won't work until a key is set.
        </span>
      </div>
      <div className="flex shrink-0 items-center gap-2">
        <button
          onClick={openSettings}
          className="rounded-sm border border-caution/40 px-2 py-0.5 hover:bg-caution/20"
        >
          Set up now
        </button>
      </div>
    </div>
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
      <LlmKeyReminderBanner />
      <div className="flex min-h-0 flex-1">
        <PatientsSidebar />
        <div className="flex min-w-0 flex-1 flex-col">
          <ModeTabs />
          <main className="min-h-0 flex-1 overflow-y-auto">
            <ActiveMode />
          </main>
        </div>
        <ContextRail />
      </div>

      {/* Overlays — rendered outside the layout flow */}
      <CommandPalette />
      <NewPatientDialog />
      <PractitionerHasLearnedView />
      <SettingsDataView />
      <EmailComposerDialog />
      <ToastStrip />
    </div>
  );
}

export default function App() {
  const token         = useAppState((s) => s.token);
  const bootHydrated  = useAppState((s) => s.bootHydrated);
  const logout        = useAppState((s) => s.logout);

  useGlobalShortcuts();

  // The api-client fires this when a 401 fails to recover via the
  // cached user_id (e.g. server's user table got reset, or first
  // sign-in on a new machine). Wiping the token bounces us to the
  // LoginView via the conditional render below.
  useEffect(() => {
    const handler = () => logout();
    window.addEventListener('nexus:auth-expired', handler);
    return () => window.removeEventListener('nexus:auth-expired', handler);
  }, [logout]);

  // Avoid a one-frame login flicker before hydrate completes.
  if (!bootHydrated) return null;

  // BootGate blocks the LoginView/MainShell render until the FastAPI
  // sidecar's /healthz returns 200. Without this, the user could fill
  // in the login form during the 3–15 s Alembic-migration window and
  // see a "Cannot reach server" error fired against a half-booted
  // process. The gate has a 15 s soft deadline + early bail when the
  // sidecar exits, so it can never strand the UI.
  return (
    <BootGate>
      {token ? <MainShell /> : <LoginView />}
    </BootGate>
  );
}
