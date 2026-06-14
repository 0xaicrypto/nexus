import { create } from 'zustand';
import type { ModeKind, PatientCard } from './lib/util';
// (MOCK_PATIENTS no longer imported — initial state is empty list,
// real data comes from refreshPatients() after login.)
import { api } from './lib/api-client';
import type { LlmStatus } from './lib/types';

export type Theme = 'dark' | 'light';

export interface ToastMsg {
  id: number;
  text: string;
  kind: 'info' | 'success' | 'error';
}

interface AppState {
  // Auth ─────────────────────────────────────────────
  token: string | null;
  displayName: string | null;     // remembered across launches; used by avatar pill
  bootHydrated: boolean;          // true after we've tried to read token from storage
  setToken: (t: string | null) => void;
  setDisplayName: (name: string | null) => void;
  logout: () => void;

  // Active selection ─────────────────────────────────
  activePatient: PatientCard | null;
  activeMode: ModeKind;
  patients: PatientCard[];
  // Currently-open chat session id. Empty string === synthetic
  // "Default chat" (wraps pre-sessions chat history). Persisted to
  // sessionStorage so the medic stays on the same thread across
  // page reloads but a fresh launch (where sessionStorage is wiped
  // along with the JWT) starts in Default.
  activeSessionId: string;
  setActiveSessionId: (id: string) => void;

  // Layout ───────────────────────────────────────────
  sidebarCollapsed: boolean;
  contextRailOpen: boolean;
  theme: Theme;

  // Dialogs / overlays ───────────────────────────────
  commandPaletteOpen: boolean;
  newPatientDialogOpen: boolean;
  toast: ToastMsg | null;

  // Context rail content — UX v2 §7.1
  contextRailContent:
    | { kind: 'closed' }
    | { kind: 'citation'; nodeId: number }
    | { kind: 'image'; sha256: string };

  openContextRailForCitation: (nodeId: number) => void;
  closeContextRail: () => void;

  // U1.1 — patients/projection refresh
  refreshPatients: () => Promise<void>;

  // U3.3 — client-side hide list for patients the user deleted while
  // the backend's DELETE endpoint isn't deployed yet. Survives reloads
  // via localStorage; cleared once the upstream list no longer
  // contains the hash (convergence).
  hidePatient: (hash: string) => void;
  unhideAllPatients: () => Promise<void>;

  // U3.3 — Settings · LLM status. Polled once after login so we can
  // show a startup reminder when no API key is configured for the
  // active provider. Null while loading / unauthed.
  llmStatus: LlmStatus | null;
  llmStatusChecked: boolean;
  refreshLlmStatus: () => Promise<void>;

  // U3 full-screen overlays
  practitionerOverlayOpen: boolean;
  openPractitionerOverlay: () => void;
  closePractitionerOverlay: () => void;
  settingsOverlayOpen: boolean;
  openSettingsOverlay: () => void;
  closeSettingsOverlay: () => void;

  // v2 email-send capability. ``emailComposerPrefill`` lets the
  // caller seed the dialog (e.g. Patient mode's "Email findings"
  // button drops the active findings into the Body field). Null
  // when opened from a neutral entry point (CommandPalette).
  emailComposerOpen: boolean;
  emailComposerPrefill: { to?: string; subject?: string; body?: string } | null;
  openEmailComposer: (prefill?: { to?: string; subject?: string; body?: string }) => void;
  closeEmailComposer: () => void;

  // Mutations
  setActivePatient: (p: PatientCard | null) => void;
  setActiveMode: (m: ModeKind) => void;
  toggleSidebar: () => void;
  toggleContextRail: () => void;
  toggleTheme: () => void;

  openCommandPalette: () => void;
  closeCommandPalette: () => void;
  openNewPatientDialog: () => void;
  closeNewPatientDialog: () => void;

  showToast: (text: string, kind?: ToastMsg['kind']) => void;
  dismissToast: () => void;
}

const TOKEN_KEY   = 'nexus.auth.token';
const NAME_KEY    = 'nexus.auth.displayName';
const THEME_KEY   = 'nexus.theme';
const HIDDEN_KEY  = 'nexus.patients.hidden';
const SESSION_ID_KEY = 'nexus.chat.session_id';

function readHiddenPatients(): Set<string> {
  try {
    const raw = localStorage.getItem(HIDDEN_KEY);
    if (!raw) return new Set();
    const arr = JSON.parse(raw);
    return Array.isArray(arr) ? new Set(arr) : new Set();
  } catch {
    return new Set();
  }
}
function writeHiddenPatients(s: Set<string>) {
  try {
    localStorage.setItem(HIDDEN_KEY, JSON.stringify(Array.from(s)));
  } catch { /* ignore */ }
}

function readStoredTheme(): Theme {
  try {
    const v = localStorage.getItem(THEME_KEY);
    if (v === 'light' || v === 'dark') return v;
  } catch {
    /* SSR / sandboxed */
  }
  return 'dark';
}

/**
 * Token is held in ``sessionStorage`` (NOT localStorage) so that
 * closing the Nexus window clears it. This is per user requirement
 * 2026-06-14: "登陆之后，关闭desktop，应该首先自动登出，下次重新打开
 * 需要重新登陆".
 *
 * Persistence behaviour by storage tier:
 *
 *   sessionStorage (per-window-lifetime)
 *     - ``nexus.auth.token``   — JWT
 *     - ``nexus.auth.user_id`` — cached for silent 401 recovery
 *       (read inside api-client.ts; mirrored here so logout clears
 *       both in one shot)
 *
 *   localStorage (persists across restarts)
 *     - displayName  → pre-fills the login form on next launch.
 *     - theme        → light/dark mode preference.
 *     - hidden patients → client-side hide list.
 *
 * Minimise / focus changes don't kill the webview, so sessionStorage
 * survives them. Only window-close, app-quit, or a sidecar respawn
 * clears it — which is the desired UX.
 */
function readStoredToken(): string | null {
  try {
    return sessionStorage.getItem(TOKEN_KEY);
  } catch {
    return null;
  }
}

export const useAppState = create<AppState>((set, get) => ({
  token: null,
  displayName: null,
  bootHydrated: false,

  setToken: (t) => {
    try {
      // sessionStorage, not localStorage — see readStoredToken's
      // docstring for the rationale (closing the window must
      // log the user out).
      if (t) sessionStorage.setItem(TOKEN_KEY, t);
      else sessionStorage.removeItem(TOKEN_KEY);
    } catch { /* ignore */ }
    api.setToken(t);
    set({ token: t });
  },

  setDisplayName: (name) => {
    try {
      if (name) localStorage.setItem(NAME_KEY, name);
      else localStorage.removeItem(NAME_KEY);
    } catch { /* ignore */ }
    set({ displayName: name });
  },

  logout: () => {
    try {
      sessionStorage.removeItem(TOKEN_KEY);
      // Keep displayName so the next sign-in pre-fills it. The
      // ApiClient still has the cached user_id in sessionStorage,
      // which goes away on window close — so a manual /logout +
      // re-sign-in inside the same window stays one click, but
      // closing the app forces a full re-login (the user_id is
      // also wiped with the session).
    } catch { /* ignore */ }
    api.setToken(null);
    try { sessionStorage.removeItem(SESSION_ID_KEY); } catch { /* ignore */ }
    set({
      token: null,
      activePatient: null,
      activeMode: 'today',
      activeSessionId: '',
      commandPaletteOpen: false,
      newPatientDialogOpen: false,
      // Drop the cached LLM status — next sign-in re-probes.
      llmStatus: null,
      llmStatusChecked: false,
    });
  },

  activePatient: null,
  activeMode: 'today',
  // Empty list — refreshPatients() populates from backend after login.
  // (Used to default to MOCK_PATIENTS which caused fake-patient flash on
  // every launch and confused medics into thinking real data existed.)
  patients: [],
  activeSessionId: '',  // hydrateAppState reads from sessionStorage
  setActiveSessionId: (id) => {
    try {
      // sessionStorage — same tier as auth state; closing the window
      // wipes it so the next launch starts on Default chat. Medic
      // who explicitly reopens an old session after re-login picks
      // it from the sidebar.
      if (id) sessionStorage.setItem(SESSION_ID_KEY, id);
      else sessionStorage.removeItem(SESSION_ID_KEY);
    } catch { /* ignore */ }
    set({ activeSessionId: id });
  },

  sidebarCollapsed: false,
  contextRailOpen: false,
  theme: 'dark',

  commandPaletteOpen: false,
  newPatientDialogOpen: false,
  toast: null,

  contextRailContent: { kind: 'closed' },
  openContextRailForCitation: (nodeId) =>
    set({ contextRailContent: { kind: 'citation', nodeId }, contextRailOpen: true }),
  closeContextRail: () => set({ contextRailContent: { kind: 'closed' }, contextRailOpen: false }),

  setActivePatient: (p) =>
    set({
      activePatient: p,
      activeMode: p ? 'patient' : 'today',
    }),

  // Refresh the patients list from backend.
  practitionerOverlayOpen: false,
  openPractitionerOverlay: () => set({ practitionerOverlayOpen: true }),
  closePractitionerOverlay: () => set({ practitionerOverlayOpen: false }),

  settingsOverlayOpen: false,
  openSettingsOverlay: () => set({ settingsOverlayOpen: true }),
  closeSettingsOverlay: () => set({ settingsOverlayOpen: false }),

  emailComposerOpen: false,
  emailComposerPrefill: null,
  openEmailComposer: (prefill) => set({
    emailComposerOpen: true,
    emailComposerPrefill: prefill ?? null,
  }),
  closeEmailComposer: () => set({
    emailComposerOpen: false,
    emailComposerPrefill: null,
  }),

  refreshPatients: async () => {
    try {
      const list = await api.listPatients();
      // Apply the client-side hide list: when the backend's DELETE
      // endpoint is missing (stale binary), we still want delete to
      // appear to work for the user. The next time the backend gains
      // the endpoint, the real DELETE wipes the rows and the hide
      // list converges automatically (filtered-out entries are gone
      // from the upstream too).
      const hidden = readHiddenPatients();
      const filtered = hidden.size === 0
        ? list
        : list.filter((p) => !hidden.has(p.patientHash));
      set({ patients: filtered });
    } catch (e) {
      console.warn('refreshPatients failed; keeping current list', e);
    }
  },

  hidePatient: (hash: string) => {
    const next = new Set(readHiddenPatients());
    next.add(hash);
    writeHiddenPatients(next);
    set((s) => ({
      patients: s.patients.filter((p) => p.patientHash !== hash),
    }));
  },
  unhideAllPatients: async () => {
    writeHiddenPatients(new Set());
    // Refresh from backend so anything we'd hidden returns.
    try {
      const list = await api.listPatients();
      set({ patients: list });
    } catch { /* keep current */ }
  },

  llmStatus: null,
  llmStatusChecked: false,
  refreshLlmStatus: async () => {
    try {
      const s = await api.getLlmSettings();
      set({ llmStatus: s, llmStatusChecked: true });
    } catch (e) {
      // The probe can fail for two distinct reasons and the UI should
      // call them out differently:
      //   (a) network/timeout → backend not running. The encounter
      //       banner already covers this; we just mark checked so the
      //       LLM-key banner doesn't show on top of "Backend down".
      //   (b) 404 / 4xx / 5xx → backend up but missing the new
      //       /settings/llm endpoint (bundled binary predates U3.3).
      //       Synthesise a status that drives the warning banner so
      //       the user is told to update / restart the server.
      console.warn('refreshLlmStatus failed', e);
      const isHttp = e instanceof Error && /API \d+/.test(e.message);
      if (isHttp) {
        set({
          llmStatus: {
            provider: 'gemini',
            model: '',
            envFilePath: '~/Library/Application Support/RuneProtocol/.env',
            envFileExists: false,
            hasGeminiKey: false,
            hasOpenaiKey: false,
            hasAnthropicKey: false,
            advisory:
              "This Nexus server build is missing /api/v1/settings/llm — " +
              "restart the FastAPI sidecar from source (or rebuild the " +
              "bundled binary) so the LLM key settings endpoint is " +
              "available.",
          },
          llmStatusChecked: true,
        });
      } else {
        set({ llmStatusChecked: true });
      }
    }
  },

  setActiveMode: (m) => set({ activeMode: m }),

  toggleSidebar: () => set((s) => ({ sidebarCollapsed: !s.sidebarCollapsed })),
  toggleContextRail: () => set((s) => ({ contextRailOpen: !s.contextRailOpen })),

  toggleTheme: () => {
    const next: Theme = get().theme === 'dark' ? 'light' : 'dark';
    try { localStorage.setItem(THEME_KEY, next); } catch { /* ignore */ }
    applyThemeToDOM(next);
    set({ theme: next });
  },

  openCommandPalette: () => set({ commandPaletteOpen: true }),
  closeCommandPalette: () => set({ commandPaletteOpen: false }),
  openNewPatientDialog: () => set({ newPatientDialogOpen: true }),
  closeNewPatientDialog: () => set({ newPatientDialogOpen: false }),

  showToast: (text, kind = 'info') => {
    const id = Date.now();
    set({ toast: { id, text, kind } });
    // Auto-dismiss after 4s
    setTimeout(() => {
      const current = get().toast;
      if (current && current.id === id) set({ toast: null });
    }, 4000);
  },
  dismissToast: () => set({ toast: null }),
}));

/** Apply theme by toggling .dark class on <html>. Call at boot + on toggle. */
export function applyThemeToDOM(theme: Theme) {
  const root = document.documentElement;
  if (theme === 'dark') root.classList.add('dark');
  else root.classList.remove('dark');
}

/**
 * One-time boot hydration. Reads token + theme from localStorage,
 * pushes both into the store, applies theme class to <html>.
 * Called once from main.tsx before initial render.
 */
export function hydrateAppState() {
  const token = readStoredToken();
  const theme = readStoredTheme();
  let displayName: string | null = null;
  try { displayName = localStorage.getItem(NAME_KEY); } catch { /* ignore */ }
  // Restore the last-active session id from sessionStorage. Lives in
  // the same tier as the JWT — closing the window wipes both. Within
  // a session, this stops a page reload from kicking the medic back
  // to Default chat.
  let activeSessionId = '';
  try {
    activeSessionId = sessionStorage.getItem(SESSION_ID_KEY) ?? '';
  } catch { /* ignore */ }
  applyThemeToDOM(theme);
  if (token) api.setToken(token);
  useAppState.setState({
    token, displayName, theme, activeSessionId, bootHydrated: true,
  });
}
