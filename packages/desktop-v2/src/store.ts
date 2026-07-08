import { create } from 'zustand';
import type { ModeKind, PatientCard, StudySummary, Workspace } from './lib/util';
// (MOCK_PATIENTS no longer imported — initial state is empty list,
// real data comes from refreshPatients() after login.)
import { api, type Identity } from './lib/api-client';
import type { ChatMsg, LlmStatus } from './lib/types';
import {
  readStoredLocale,
  writeStoredLocale,
  type Locale,
} from './lib/i18n';

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

  // F26.2 — Multi-identity (USER_MANAGEMENT.md §4-§6).
  // ``activeUserId`` is the source of truth for which user's data the
  // workspace is showing. ``identities`` drives the picker dropdown.
  // ``resetForIdentitySwitch`` is the surgical reset that drops the
  // current workspace state but preserves theme/locale/UI prefs so
  // the medic doesn't lose their layout when switching accounts.
  activeUserId: string | null;
  identities:   Identity[];
  setActiveUserId: (id: string | null) => void;
  setIdentities:   (list: Identity[]) => void;
  resetForIdentitySwitch: () => void;

  // Active selection ─────────────────────────────────
  activePatient: PatientCard | null;
  activeMode: ModeKind;
  patients: PatientCard[];

  // Research Workspace (decisions D1 + D14) ─────────
  // The app boots into 'research' on a fresh install; persisted to
  // localStorage afterwards. See docs/design/RESEARCH_WORKSPACE_DESIGN.md.
  activeWorkspace:    Workspace;
  studies:            StudySummary[];
  activeStudyId:      string | null;
  setActiveWorkspace: (w: Workspace) => void;
  setActiveStudyId:   (sid: string | null) => void;
  refreshStudies:     () => Promise<void>;
  // Currently-open chat session id. Empty string === synthetic
  // "Default chat" (wraps pre-sessions chat history). Persisted to
  // sessionStorage so the medic stays on the same thread across
  // page reloads but a fresh launch (where sessionStorage is wiped
  // along with the JWT) starts in Default.
  activeSessionId: string;
  setActiveSessionId: (id: string) => void;

  // F-chat-state-persist ─────────────────────────────
  // Per-session in-flight chat state. Lives in zustand (not in the
  // EncounterMode component) so a streaming turn survives tab
  // switches: the SSE consumer keeps writing into the store, and on
  // remount the chat pane rehydrates from the store (mid-stream
  // text + the streaming flag). Without this, switching tabs mid-
  // turn made the partial answer vanish until the next history pull.
  //
  // Keyed by ``sessionId`` (the same string EncounterMode uses for
  // ``effectiveSessionId`` — for un-named sessions this is
  // ``patient-${patient_hash}``, so it's already per-patient).
  chatMsgsBySession:      Record<string, ChatMsg[]>;
  chatStreamingBySession: Record<string, boolean>;
  setChatMsgs:        (sessionId: string, msgs: ChatMsg[]) => void;
  appendChatMsg:      (sessionId: string, msg: ChatMsg) => void;
  /** Mutate the last message in a session. Two forms:
   *   - Partial<ChatMsg>: shallow-merge into last
   *   - (last) => Partial<ChatMsg>: functional updater for cases
   *     that need the previous value (e.g. appending to reasoning[]) */
  updateLastChatMsg:  (
    sessionId: string,
    mut: Partial<ChatMsg> | ((last: ChatMsg) => Partial<ChatMsg>),
  ) => void;
  setChatStreaming:   (sessionId: string, streaming: boolean) => void;

  // Layout ───────────────────────────────────────────
  sidebarCollapsed: boolean;
  contextRailOpen: boolean;
  theme: Theme;

  // i18n ─────────────────────────────────────────────
  // ``locale`` is consumed by ``useT()`` in lib/i18n. Default is
  // zh-CN per the target audience (Chinese-speaking clinicians).
  // Persisted to localStorage under 'nexus.locale' so it survives
  // close-and-reopen.
  locale: Locale;
  setLocale: (l: Locale) => void;

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

  // F-archive-frontend — soft archive backed by the server's
  // ``POST /patients/{hash}/archive`` endpoint. Survives reinstall,
  // syncs across devices when sync ships, AND is what the
  // cross-patient roster filter consults, so the AI no longer sees
  // hidden patients in chat context. The legacy localStorage hide
  // list is auto-migrated to server-side archive on first launch.
  hidePatient: (hash: string) => Promise<void>;
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

/**
 * F-archive-frontend — one-shot migration from legacy localStorage
 * hide list to server-side ``archived_at``.
 *
 * Runs (lazily) on every refreshPatients call until the localStorage
 * key is empty. For each hash in the old list:
 *   - POST /patients/{hash}/archive
 *   - on success, remove from the local set
 *
 * If the backend can't reach the patient (already gone, hash not on
 * THIS user, etc.) we still drop the entry from localStorage — it's
 * not a recoverable state anyway. After the set is empty we remove
 * the key entirely so this function is a no-op on every subsequent
 * call.
 *
 * Idempotent + crash-safe: a half-completed migration just resumes
 * on the next refreshPatients call.
 */
async function _migrateHiddenToArchive(): Promise<void> {
  const hidden = readHiddenPatients();
  if (hidden.size === 0) {
    // Clear the empty array entry too so the next call short-
    // circuits without even reading.
    try { localStorage.removeItem(HIDDEN_KEY); } catch { /* ignore */ }
    return;
  }
  console.info(
    '[archive-migration] migrating %d hidden patient(s) to server-side archive',
    hidden.size,
  );
  const remaining = new Set(hidden);
  for (const hash of hidden) {
    try {
      await api.archivePatient(hash);
      remaining.delete(hash);
    } catch (e) {
      // 404 = patient already gone OR not this user's. Drop from
      // legacy list anyway — it's terminal either way.
      const status = (e as any)?.status;
      if (status === 404) {
        remaining.delete(hash);
      } else {
        console.warn(
          '[archive-migration] failed for %s; will retry next refresh',
          hash.slice(0, 8), e,
        );
      }
    }
  }
  writeHiddenPatients(remaining);
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
  // F26.2 — multi-identity state hydrated from /auth/local-bootstrap
  activeUserId: null,
  identities: [],

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

  setActiveUserId: (id) => set({ activeUserId: id }),
  setIdentities:   (list) => set({ identities: list }),

  /**
   * F26.2 — surgical reset for switching to another identity (§8.2).
   *
   * Drops everything that's tied to the previous user_id:
   *   - patient list & active patient
   *   - active study / workspace selection
   *   - chat session pointer (a session belongs to one user_id)
   *   - LLM status cache (Settings · LLM is per-user via DB hydrate)
   *   - any other transient state showing data from the old user
   *
   * KEEPS (UI prefs, machine-level state):
   *   - theme / locale / sidebarCollapsed (the medic's layout)
   *   - bootHydrated (we're past first-mount)
   *   - identities list itself (just changing pointer, not list)
   *
   * Caller MUST set the new JWT via setToken() BEFORE invoking this —
   * otherwise the immediate refreshPatients() / refreshStudies()
   * triggered downstream will use stale credentials.
   */
  resetForIdentitySwitch: () => {
    try { sessionStorage.removeItem(SESSION_ID_KEY); } catch { /* ignore */ }
    set({
      activePatient: null,
      activeMode: 'today',
      activeSessionId: '',
      patients: [],
      studies: [],
      activeStudyId: null,
      activeWorkspace: 'research',
      commandPaletteOpen: false,
      newPatientDialogOpen: false,
      llmStatus: null,
      llmStatusChecked: false,
      contextRailContent: { kind: 'closed' },
      contextRailOpen: false,
      // F-chat-state-persist — wipe per-session chat state on
      // identity swap so the next user doesn't see the previous
      // user's drafted message buffer.
      chatMsgsBySession:      {},
      chatStreamingBySession: {},
    });
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
      // F-chat-state-persist — see resetForIdentitySwitch.
      chatMsgsBySession:      {},
      chatStreamingBySession: {},
    });
  },

  activePatient: null,
  activeMode: 'today',
  // Empty list — refreshPatients() populates from backend after login.
  // (Used to default to MOCK_PATIENTS which caused fake-patient flash on
  // every launch and confused medics into thinking real data existed.)
  patients: [],

  // Research Workspace state — default to 'research' on fresh install
  // (decision D14). Persisted to localStorage in the setter.
  activeWorkspace: ((): Workspace => {
    try {
      const v = localStorage.getItem('nexus.activeWorkspace');
      if (v === 'patient' || v === 'research') return v;
    } catch { /* ignore */ }
    return 'research';
  })(),
  studies: [],
  activeStudyId: null,
  setActiveWorkspace: (w) => {
    try { localStorage.setItem('nexus.activeWorkspace', w); } catch { /* ignore */ }
    set({ activeWorkspace: w });
  },
  setActiveStudyId: (sid) => set({ activeStudyId: sid }),
  refreshStudies: async () => {
    try {
      const list = await api.listStudies();
      set({ studies: list });
    } catch (e) {
      console.warn('refreshStudies failed', e);
    }
  },
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

  // F-chat-state-persist — see interface block for rationale.
  chatMsgsBySession:      {},
  chatStreamingBySession: {},
  setChatMsgs: (sessionId, msgs) =>
    set((s) => ({
      chatMsgsBySession: { ...s.chatMsgsBySession, [sessionId]: msgs },
    })),
  appendChatMsg: (sessionId, msg) =>
    set((s) => {
      const cur = s.chatMsgsBySession[sessionId] ?? [];
      return {
        chatMsgsBySession: { ...s.chatMsgsBySession, [sessionId]: [...cur, msg] },
      };
    }),
  updateLastChatMsg: (sessionId, mut) =>
    set((s) => {
      const cur = s.chatMsgsBySession[sessionId];
      if (!cur || cur.length === 0) return {};
      const last = cur[cur.length - 1];
      const patch = typeof mut === 'function' ? mut(last) : mut;
      const next = [...cur.slice(0, -1), { ...last, ...patch }];
      return {
        chatMsgsBySession: { ...s.chatMsgsBySession, [sessionId]: next },
      };
    }),
  setChatStreaming: (sessionId, streaming) =>
    set((s) => ({
      chatStreamingBySession: {
        ...s.chatStreamingBySession,
        [sessionId]: streaming,
      },
    })),

  sidebarCollapsed: false,
  contextRailOpen: false,
  theme: 'dark',

  // Hydrated from localStorage on store create — same pattern as
  // displayName, hidden patients, etc. A missing / corrupt value
  // falls back to DEFAULT_LOCALE inside readStoredLocale.
  locale: readStoredLocale(),
  setLocale: (l: Locale) => {
    writeStoredLocale(l);
    set({ locale: l });
  },

  commandPaletteOpen: false,
  newPatientDialogOpen: false,
  toast: null,

  contextRailContent: { kind: 'closed' },
  openContextRailForCitation: (nodeId) =>
    set({ contextRailContent: { kind: 'citation', nodeId }, contextRailOpen: true }),
  closeContextRail: () => set({ contextRailContent: { kind: 'closed' }, contextRailOpen: false }),

  setActivePatient: (p) => {
    // Switching patients MUST clear the active chat session id —
    // otherwise EncounterMode loads the previous patient's messages
    // when the medic clicks on a new patient (sessions are not
    // patient-scoped on the backend; cross-patient leakage is the
    // observable symptom — "Default chat · 12 messages" showing the
    // wrong patient's history). Clearing it forces the next render
    // to fall through to the per-patient derived default (see
    // EncounterMode `effectiveSessionId`).
    try {
      sessionStorage.removeItem(SESSION_ID_KEY);
    } catch { /* ignore */ }
    set({
      activePatient: p,
      activeMode: p ? 'patient' : 'today',
      activeSessionId: '',
    });
  },

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
      // F-archive-frontend — server's /patients endpoint now already
      // filters ``WHERE archived_at IS NULL``, so no client-side
      // filter is needed. The localStorage ``nexus.patients.hidden``
      // list is migrated to server-side archive on first launch
      // (see _migrateHiddenToArchive below).
      const list = await api.listPatients();
      set({ patients: list });

      // One-shot legacy migration: any patient_hash still in the
      // old localStorage hide list gets archived on the backend.
      // After it succeeds, clear localStorage so we don't try again.
      void _migrateHiddenToArchive();
    } catch (e) {
      console.warn('refreshPatients failed; keeping current list', e);
    }
  },

  /**
   * F-archive-frontend — hide a patient by archiving on the server.
   *
   * Optimistically removes the patient from the in-memory list so
   * the UI updates instantly. The POST then makes it stick across
   * reinstall / other devices. On failure we revert + refresh.
   */
  hidePatient: async (hash: string) => {
    const before = useAppState.getState().patients;
    set({ patients: before.filter((p) => p.patientHash !== hash) });
    try {
      await api.archivePatient(hash);
      // Sync — server is authoritative now, refresh to get any
      // sequence-number / sort-order changes.
      const list = await api.listPatients();
      set({ patients: list });
    } catch (e) {
      console.warn('archivePatient failed; rolling back', e);
      set({ patients: before });
      // Surface a toast so the medic isn't confused.
      useAppState.getState().showToast(
        '隐藏失败：服务器无法响应,请稍后重试',
        'error',
      );
    }
  },

  /**
   * F-archive-frontend — bulk restore. Fetches the list of archived
   * patients then unarchives each. Used by Settings → 恢复隐藏患者.
   * Idempotent + best-effort per patient (one failing patient
   * doesn't abort the whole pass).
   */
  unhideAllPatients: async () => {
    try {
      const archived = await api.listArchivedPatients();
      let restored = 0;
      for (const a of archived) {
        try {
          await api.unarchivePatient(a.patientHash);
          restored += 1;
        } catch { /* skip this one */ }
      }
      const list = await api.listPatients();
      set({ patients: list });
      useAppState.getState().showToast(
        `已恢复 ${restored} 位隐藏患者`,
        'success',
      );
    } catch (e) {
      console.warn('unhideAllPatients failed', e);
      useAppState.getState().showToast(
        '恢复失败：服务器无法响应,请稍后重试',
        'error',
      );
    }
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
