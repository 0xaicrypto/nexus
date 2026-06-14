/**
 * ApiClient — real HTTP wrapper around the FastAPI backend.
 *
 * U1.1: replaces the U0 mock with real endpoint coverage:
 * - auth (login)
 * - patients (listPatients)
 * - memory v3 (projection / findings / medications / timeline / citation /
 *              practitioner candidates / pending count / confirm / reject)
 * - chat SSE streaming (sendChat)
 *
 * Dev: requests go to /api/v1/* — Vite proxies to http://localhost:8001.
 * Prod: ``VITE_NEXUS_API`` env (set at build) provides the base URL.
 *
 * Auth: bearer JWT in Authorization header. Token held in memory only;
 * U2 will swap to @tauri-apps/plugin-stronghold for OS-keychain storage.
 */

import type {
  ChatStreamChunk,
  LlmStatus,
  PatientProjection,
  PractitionerCandidate,
  ProvenanceRow,
  StudyInfo,
} from './types';

// import.meta.env is injected by Vite; cast keeps tsc happy without the
// full `/// <reference types="vite/client" />` triple-slash.
//
// baseUrl resolution:
//   1. Build-time VITE_NEXUS_API env, if set (lets ops point at a remote
//      backend without rebuilding the binary).
//   2. http://127.0.0.1:8001 — the sidecar default (src-tauri/lib.rs
//      sets NEXUS_HOST=127.0.0.1, NEXUS_PORT=8001 when spawning).
//
// We CANNOT default to "" (relative URL) because in a bundled .dmg the
// frontend is served from tauri://localhost — relative URLs resolve
// against THAT origin and never reach the Python sidecar. In `pnpm
// tauri dev` we still use 127.0.0.1:8001 (no Vite proxy needed since
// the backend's CORS allows it).
const envBase =
  (import.meta as unknown as { env?: { VITE_NEXUS_API?: string } }).env
    ?.VITE_NEXUS_API;
const baseUrl = envBase && envBase.length > 0 ? envBase : 'http://127.0.0.1:8001';

// ─────────────────────────────────────────────────────────────────────
// Persistent user_id storage
// ─────────────────────────────────────────────────────────────────────
// The user_id is the medic's stable identifier — NOT auth. Auth is
// the JWT (in sessionStorage; wiped on window close per the
// "auto-logout on close" UX).
//
// We keep user_id in localStorage so that:
//
//   1. Closing the desktop and reopening it asks the medic to sign
//      in again (no JWT → LoginView).
//   2. On sign-in, the cached user_id flows into /auth/login, the
//      server returns a fresh JWT bound to the SAME user_id, and the
//      medic's previously uploaded patients / memory / sessions are
//      all visible again.
//
// Before this fix user_id lived in sessionStorage alongside the JWT
// — closing the window minted a fresh user_id on next sign-in, so
// every restart looked like a brand-new install with no patients
// and an empty Memory tab. The DB still had the old user's data, it
// was just no longer reachable from the desktop.
//
// U2+: optionally seal the user_id in the OS keychain via
// @tauri-apps/plugin-stronghold for users who turn on "remember me".

const STORAGE_KEY_USER_ID = 'nexus.auth.user_id';

function readUserId(): string | null {
  try {
    return localStorage.getItem(STORAGE_KEY_USER_ID);
  } catch {
    return null;  // SSR / privacy modes where localStorage is unavailable
  }
}

function writeUserId(id: string): void {
  try {
    localStorage.setItem(STORAGE_KEY_USER_ID, id);
  } catch {
    /* no-op — sign-in still works for this session, just won't persist */
  }
}

function clearUserId(): void {
  try {
    localStorage.removeItem(STORAGE_KEY_USER_ID);
  } catch {
    /* no-op */
  }
}

class _ApiClient {
  private token: string | null = null;

  setToken(t: string | null) { this.token = t; }
  hasToken() { return this.token !== null; }
  getToken() { return this.token; }

  /** Base URL the client posts to — useful when the UI needs to build
   *  a non-fetch URL (e.g. an <a href> to /dicom-viewer/). */
  get baseUrl() { return baseUrl; }

  private headers(extra?: HeadersInit): Headers {
    const h = new Headers(extra);
    h.set('Accept', 'application/json');
    if (this.token) h.set('Authorization', `Bearer ${this.token}`);
    return h;
  }

  /** Called once on 401 — silently re-auth with the cached user_id so
   *  the medic doesn't have to retype their display name after the
   *  server rotates JWT secrets (rebuilds, restart, 24h expiry, etc.).
   *  Returns the new token, or null if cached id is unknown to backend. */
  private async silentReauth(): Promise<string | null> {
    const cachedUserId = readUserId();
    if (!cachedUserId) return null;
    try {
      interface LoginResp { jwt_token: string; expires_in_seconds: number; }
      const r = await fetch(`${baseUrl}/api/v1/auth/login`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'Accept': 'application/json' },
        body: JSON.stringify({ user_id: cachedUserId }),
      });
      if (!r.ok) return null;
      const body = (await r.json()) as LoginResp;
      this.token = body.jwt_token;
      try { sessionStorage.setItem('nexus.auth.token', body.jwt_token); } catch { /* ignore */ }
      return body.jwt_token;
    } catch {
      return null;
    }
  }

  private async fetch<T>(path: string, init?: RequestInit): Promise<T> {
    const doFetch = async (): Promise<Response> => {
      const h = this.headers(init?.headers);
      if (init?.body && !h.has('Content-Type')) h.set('Content-Type', 'application/json');
      return fetch(`${baseUrl}${path}`, { ...init, headers: h });
    };

    let r = await doFetch();

    // 401 → try one silent re-auth + retry. Skip the dance for the
    // auth endpoints themselves so we don't recurse.
    if (r.status === 401 && !path.startsWith('/api/v1/auth/')) {
      const newToken = await this.silentReauth();
      if (newToken) {
        r = await doFetch();
      } else {
        // Cached user_id is no longer recognised by this backend —
        // wipe the token so App.tsx bounces to LoginView on its next
        // store read. (The store-level subscription picks this up.)
        this.token = null;
        try { sessionStorage.removeItem('nexus.auth.token'); } catch { /* ignore */ }
        // Dispatch a one-time event the App can listen for to force a
        // logout + login-screen render.
        try {
          window.dispatchEvent(new CustomEvent('nexus:auth-expired'));
        } catch { /* SSR */ }
      }
    }

    if (!r.ok) {
      const text = await r.text().catch(() => '');
      throw new ApiError(r.status, text || r.statusText, path);
    }
    return r.json() as Promise<T>;
  }

  /* ────────────────────────── health ────────────────────────── */

  /**
   * Probe the backend. Returns one of:
   *   - 'ok'         — /healthz answered 200.
   *   - 'unreachable'— network/CORS failure (fetch threw). The Tauri
   *                    sidecar is not running, or you're in `pnpm dev`
   *                    without a separate FastAPI on :8001.
   *   - 'unhealthy'  — backend answered but with a non-2xx (auth, 5xx).
   *
   * Used by the chat send path to turn opaque "TypeError: Load failed"
   * into an actionable banner. Never throws.
   */
  async health(): Promise<'ok' | 'unreachable' | 'unhealthy'> {
    try {
      const r = await fetch(`${baseUrl}/healthz`, {
        method: 'GET',
        // Health endpoint is open; no auth header needed.
        // Short timeout so a hung TCP doesn't block the UI for 30s.
        signal: AbortSignal.timeout(2500),
      });
      return r.ok ? 'ok' : 'unhealthy';
    } catch {
      return 'unreachable';
    }
  }

  /* ────────────────────────── auth ────────────────────────── */

  /**
   * M0 auth mode — single-input "sign in", no password.
   *
   * Backend endpoints:
   *   POST /api/v1/auth/register {display_name} → {user_id, jwt_token}
   *   POST /api/v1/auth/login    {user_id}      → {jwt_token}
   *
   * Flow:
   *   1. First-time on this machine OR no cached user_id → register a
   *      new account with the display name. Persist user_id locally
   *      under STORAGE_KEY_USER_ID so the next launch reuses it.
   *   2. Subsequent launches → call /login with the cached user_id.
   *      If the backend's user table got reset (404 from /login),
   *      transparently fall back to /register and persist the new id.
   *
   * Storage: localStorage in M0. WKWebView persists this per-app in
   * ~/Library/WebKit/... — survives across launches, gets wiped only
   * if the OS user nukes the app's webview data. U2+: switch to
   * @tauri-apps/plugin-stronghold for OS keychain storage.
   *
   * `_password` is kept in the signature so existing call sites
   * compile unchanged; we ignore it.
   */
  async login(displayName: string, _password: string): Promise<{ access_token: string }> {
    interface RegisterResponse {
      user_id: string;
      jwt_token: string;
      created_at: string;
    }
    interface LoginResponse {
      jwt_token: string;
      expires_in_seconds: number;
    }

    const cachedUserId = readUserId();

    // Path A: try login with the cached user_id.
    if (cachedUserId) {
      try {
        const r = await this.fetch<LoginResponse>('/api/v1/auth/login', {
          method: 'POST',
          body: JSON.stringify({ user_id: cachedUserId }),
        });
        return { access_token: r.jwt_token };
      } catch (err) {
        // 404 = user_id no longer exists on this backend (DB reset, or
        // user switched servers). 400 = malformed cached id. Either way,
        // fall through to fresh register. For other errors (5xx, network)
        // bubble up so the UI can show a real message.
        if (err instanceof ApiError && (err.status === 404 || err.status === 400)) {
          clearUserId();
          // fallthrough
        } else {
          throw err;
        }
      }
    }

    // Path B: no cached id, or cached id was invalid → register fresh.
    const r = await this.fetch<RegisterResponse>('/api/v1/auth/register', {
      method: 'POST',
      body: JSON.stringify({ display_name: displayName }),
    });
    writeUserId(r.user_id);
    return { access_token: r.jwt_token };
  }

  /** Clear the cached user_id. Used by Settings → "Sign out / forget me". */
  forgetUserId() {
    clearUserId();
    this.token = null;
  }

  /* ────────────────────────── patients ────────────────────────── */

  /**
   * Manually register a patient (no DICOM yet).
   *
   * Backend hashes either the MRN or (initials | age | sex) to mint a
   * stable patient_hash. At least one of (initials, mrn) is required —
   * the dialog enforces this client-side.
   *
   * Returns the patient_hash so the caller can immediately navigate to
   * the patient's page or bind the active chat session to it.
   */
  async createManualPatient(input: {
    initials?:        string;
    mrn?:             string;
    age?:             number;     // numeric — backend buckets to age_group
    sex?:             'M' | 'F' | 'O' | '';
    chiefComplaint?:  string;
    notes?:           string;
    sessionId?:       string;
  }): Promise<{ patientHash: string; ageGroup: string }> {
    interface Resp { patient_hash: string; age_group: string }
    const body = {
      initials:        input.initials        ?? '',
      mrn:             input.mrn             ?? '',
      age:             input.age             ?? 0,
      sex:             input.sex             ?? '',
      chief_complaint: input.chiefComplaint  ?? '',
      notes:           input.notes           ?? '',
      session_id:      input.sessionId       ?? '',
    };
    const r = await this.fetch<Resp>('/api/v1/dicom/patients/register-manual', {
      method: 'POST',
      body:   JSON.stringify(body),
    });
    return { patientHash: r.patient_hash, ageGroup: r.age_group };
  }

  /* ────────────────────────── DICOM studies ────────────────────────── */

  /** List all DICOM studies for a patient (newest-first). Series list
   *  is NOT joined — call ``getStudy`` for that. */
  async listPatientStudies(patientHash: string): Promise<StudyInfo[]> {
    interface RawSeries {
      series_id: string;
      series_instance_uid: string;
      series_number: number | null;
      modality: string;
      body_part: string;
      series_description: string;
      default_wl: number | null;
      default_ww: number | null;
      instance_count: number;
    }
    interface Raw {
      study_id: string;
      study_instance_uid: string;
      study_date: string;
      study_description: string;
      modality: string;
      patient_hash: string;
      patient_age_group: string;
      patient_sex: string;
      series: RawSeries[];
      created_at: number;
    }
    const raw = await this.fetch<Raw[]>(
      `/api/v1/dicom/patients/${encodeURIComponent(patientHash)}/studies`,
    );
    return raw.map((r) => ({
      studyId:           r.study_id,
      studyInstanceUid:  r.study_instance_uid,
      studyDate:         r.study_date,
      studyDescription:  r.study_description,
      modality:          r.modality,
      patientHash:       r.patient_hash,
      patientAgeGroup:   r.patient_age_group,
      patientSex:        r.patient_sex,
      series:            (r.series ?? []).map((s) => ({
        seriesId:          s.series_id,
        seriesInstanceUid: s.series_instance_uid,
        seriesNumber:      s.series_number,
        modality:          s.modality,
        bodyPart:          s.body_part,
        seriesDescription: s.series_description,
        defaultWl:         s.default_wl,
        defaultWw:         s.default_ww,
        instanceCount:     s.instance_count,
      })),
      createdAt:         r.created_at,
    }));
  }

  /** Full study with series joined. */
  async getStudy(studyId: string): Promise<StudyInfo> {
    interface RawSeries {
      series_id: string;
      series_instance_uid: string;
      series_number: number | null;
      modality: string;
      body_part: string;
      series_description: string;
      default_wl: number | null;
      default_ww: number | null;
      instance_count: number;
    }
    interface Raw {
      study_id: string;
      study_instance_uid: string;
      study_date: string;
      study_description: string;
      modality: string;
      patient_hash: string;
      patient_age_group: string;
      patient_sex: string;
      series: RawSeries[];
      created_at: number;
    }
    const r = await this.fetch<Raw>(
      `/api/v1/dicom/studies/${encodeURIComponent(studyId)}`,
    );
    return {
      studyId:           r.study_id,
      studyInstanceUid:  r.study_instance_uid,
      studyDate:         r.study_date,
      studyDescription:  r.study_description,
      modality:          r.modality,
      patientHash:       r.patient_hash,
      patientAgeGroup:   r.patient_age_group,
      patientSex:        r.patient_sex,
      series:            (r.series ?? []).map((s) => ({
        seriesId:          s.series_id,
        seriesInstanceUid: s.series_instance_uid,
        seriesNumber:      s.series_number,
        modality:          s.modality,
        bodyPart:          s.body_part,
        seriesDescription: s.series_description,
        defaultWl:         s.default_wl,
        defaultWw:         s.default_ww,
        instanceCount:     s.instance_count,
      })),
      createdAt:         r.created_at,
    };
  }

  /** Build the absolute URL of a render. Bearer-auth required, so use
   *  this for fetch() and pipe into a blob; ``<img src>`` won't work
   *  cross-origin without a query-token endpoint. */
  renderUrl(
    studyId: string, seriesId: string,
    opts?: { kind?: 'slice' | 'mip' | 'grid'; slice?: number; window?: string },
  ): string {
    const q = new URLSearchParams();
    if (opts?.kind)   q.set('kind',   opts.kind);
    if (opts?.slice !== undefined) q.set('slice',  String(opts.slice));
    if (opts?.window) q.set('window', opts.window);
    const qs = q.toString();
    return (
      `${baseUrl}/api/v1/dicom/studies/${encodeURIComponent(studyId)}` +
      `/series/${encodeURIComponent(seriesId)}/render` +
      (qs ? `?${qs}` : '')
    );
  }

  /** Fetch a render as a blob URL (object URL). Caller is responsible
   *  for URL.revokeObjectURL when the image unmounts. */
  async renderBlobUrl(
    studyId: string, seriesId: string,
    opts?: { kind?: 'slice' | 'mip' | 'grid'; slice?: number; window?: string },
  ): Promise<string> {
    const r = await fetch(this.renderUrl(studyId, seriesId, opts), {
      headers: this.headers(),
    });
    if (!r.ok) throw new ApiError(r.status, await r.text().catch(() => r.statusText), '/render');
    const blob = await r.blob();
    return URL.createObjectURL(blob);
  }

  /** Delete a patient. Returns per-table row counts removed (server
   *  also un-binds chat sessions instead of deleting them). 404 if no
   *  rows for this user matched the hash. */
  async deletePatient(patientHash: string): Promise<{
    patientHash: string;
    deleted: Record<string, number>;
  }> {
    interface Raw { patient_hash: string; deleted: Record<string, number> }
    const r = await this.fetch<Raw>(
      `/api/v1/dicom/patients/${encodeURIComponent(patientHash)}`,
      { method: 'DELETE' },
    );
    return { patientHash: r.patient_hash, deleted: r.deleted };
  }

  /** Upload a DICOM zip (or any file) via multipart. Returns the
   *  file_id + study_id (populated once the background DICOM parse
   *  finishes — call ``getPrerenderProgress(file_id)`` to poll).
   *  ``onProgress`` reports raw upload bytes via XHR; the post-upload
   *  DICOM parse is reported separately by the polling endpoint. */
  async uploadFile(
    file: File | Blob,
    filename: string,
    options?: {
      sessionId?: string;
      /** When the medic has a patient open in the desktop, pass their
       *  hash so the backend BINDS the upload to that patient instead
       *  of minting a new patient from the DICOM PatientID tag. */
      patientHash?: string;
      onProgress?: (loaded: number, total: number) => void;
    },
  ): Promise<{
    fileId: string;
    name: string;
    mime: string;
    sizeBytes: number;
    sha256: string;
    dicomStatus: string;
    dicomStudyId: string;
  }> {
    interface Raw {
      file_id: string;
      name: string;
      mime: string;
      size_bytes: number;
      sha256: string;
      dicom_status: string;
      dicom_study_id: string;
    }
    // We use XHR (not fetch) so we can report upload progress, which
    // matters for multi-gigabyte DICOM zips.
    return new Promise((resolve, reject) => {
      const form = new FormData();
      form.append('file', file, filename);
      if (options?.sessionId)   form.append('session_id',   options.sessionId);
      if (options?.patientHash) form.append('patient_hash', options.patientHash);
      const xhr = new XMLHttpRequest();
      xhr.open('POST', `${baseUrl}/api/v1/files/upload`);
      if (this.token) xhr.setRequestHeader('Authorization', `Bearer ${this.token}`);
      if (options?.onProgress) {
        xhr.upload.onprogress = (e) => {
          if (e.lengthComputable) options.onProgress!(e.loaded, e.total);
        };
      }
      xhr.onload = () => {
        if (xhr.status >= 200 && xhr.status < 300) {
          try {
            const raw = JSON.parse(xhr.responseText) as Raw;
            resolve({
              fileId:       raw.file_id,
              name:         raw.name,
              mime:         raw.mime,
              sizeBytes:    raw.size_bytes,
              sha256:       raw.sha256,
              dicomStatus:  raw.dicom_status,
              dicomStudyId: raw.dicom_study_id,
            });
          } catch (e) {
            reject(new ApiError(xhr.status, `bad JSON: ${e}`, '/api/v1/files/upload'));
          }
        } else {
          reject(new ApiError(xhr.status, xhr.responseText || xhr.statusText, '/api/v1/files/upload'));
        }
      };
      xhr.onerror  = () => reject(new TypeError('upload network error'));
      xhr.onabort  = () => reject(new ApiError(0, 'aborted', '/api/v1/files/upload'));
      xhr.send(form);
    });
  }

  /** Poll a DICOM zip's post-upload background parse. */
  async getPrerenderProgress(fileId: string): Promise<{
    state: 'queued' | 'parsing' | 'rendering' | 'done' | 'error' | 'unknown';
    stage: string;
    current: number;
    total: number;
    percent: number;
    studyId: string;
    error: string;
    /** Layer 1 graph ingestion status — '' / 'pending' / 'ok' / 'error'. */
    memoryStatus: string;
    /** Human one-liner — "6 graph events" on success, "ExcType: msg" on error. */
    memorySummary: string;
    /** Tier A Quick scan (Gemini Flash triage) status. */
    quickScanStatus: string;
    /** "N flagged" / "no findings" / error text. */
    quickScanSummary: string;
    /** Live progress dict while quickScanStatus === 'pending'. ``null``
     *  when no scan is running OR when one finished more than 1h ago
     *  (TTL-pruned server-side). Schema matches
     *  ``QuickScanProgress`` below. */
    quickScanProgress: QuickScanProgress | null;
  }> {
    interface Raw {
      state: string; stage: string; current: number; total: number;
      percent: number; study_id: string; preview_dir: string; error: string;
      memory_status?: string; memory_summary?: string;
      quick_scan_status?: string; quick_scan_summary?: string;
      quick_scan_progress?: QuickScanProgress | null;
    }
    const r = await this.fetch<Raw>(
      `/api/v1/files/${encodeURIComponent(fileId)}/prerender-progress`,
    );
    return {
      state:         r.state as any,
      stage:         r.stage,
      current:       r.current,
      total:         r.total,
      percent:       r.percent,
      studyId:          r.study_id,
      error:            r.error,
      memoryStatus:     r.memory_status      ?? '',
      memorySummary:    r.memory_summary     ?? '',
      quickScanStatus:  r.quick_scan_status  ?? '',
      quickScanSummary: r.quick_scan_summary ?? '',
      quickScanProgress: r.quick_scan_progress ?? null,
    };
  }

  /** List the user's uploads, newest first. Optional patient filter
   *  scopes to one patient's uploads. Used by the Imaging tab to render
   *  historical uploads after the in-memory session list is gone. */
  async listUploads(opts?: { patientHash?: string; limit?: number }): Promise<{
    fileId: string; name: string; mime: string; sizeBytes: number;
    createdAt: string; patientHash: string;
    dicomStatus: string; dicomStudyId: string;
    memoryStatus: string; memorySummary: string;
    quickScanStatus: string; quickScanSummary: string;
  }[]> {
    interface Raw {
      file_id: string; name: string; mime: string; size_bytes: number;
      created_at: string; patient_hash: string;
      dicom_status: string; dicom_study_id: string;
      memory_status: string; memory_summary: string;
      quick_scan_status: string; quick_scan_summary: string;
    }
    const q = new URLSearchParams();
    if (opts?.patientHash) q.set('patient_hash', opts.patientHash);
    if (opts?.limit)       q.set('limit',        String(opts.limit));
    const qs = q.toString();
    const raw = await this.fetch<Raw[]>(
      `/api/v1/files/uploads${qs ? `?${qs}` : ''}`,
    );
    return raw.map((r) => ({
      fileId:            r.file_id,
      name:              r.name,
      mime:              r.mime,
      sizeBytes:         r.size_bytes,
      createdAt:         r.created_at,
      patientHash:       r.patient_hash,
      dicomStatus:       r.dicom_status,
      dicomStudyId:      r.dicom_study_id,
      memoryStatus:      r.memory_status,
      memorySummary:     r.memory_summary,
      quickScanStatus:   r.quick_scan_status,
      quickScanSummary:  r.quick_scan_summary,
    }));
  }

  /* ─────────────────────────── sessions ─────────────────────────── */

  /** List the user's chat sessions, newest activity first. The
   *  synthetic "Default chat" session is appended when the user has
   *  any pre-sessions chat history (id === ''). */
  async listSessions(includeArchived = false): Promise<ChatSessionInfo[]> {
    interface RawRow {
      id: string; title: string; created_at: string;
      last_message_at: string | null; message_count: number;
      archived: boolean; is_default?: boolean;
    }
    interface RawResp { sessions: RawRow[] }
    const qs = includeArchived ? '?include_archived=true' : '';
    const r = await this.fetch<RawResp>(`/api/v1/sessions${qs}`);
    return r.sessions.map((s) => ({
      id:             s.id,
      title:          s.title,
      createdAt:      s.created_at,
      lastMessageAt:  s.last_message_at,
      messageCount:   s.message_count,
      archived:       s.archived,
      isDefault:      !!s.is_default,
    }));
  }

  /** Create a fresh chat session. Title defaults to "New chat" so the
   *  sidebar has something to show until the auto-titler kicks in. */
  async createSession(title?: string): Promise<ChatSessionInfo> {
    interface RawRow {
      id: string; title: string; created_at: string;
      last_message_at: string | null; message_count: number;
      archived: boolean; is_default?: boolean;
    }
    const r = await this.fetch<RawRow>('/api/v1/sessions', {
      method: 'POST',
      body: JSON.stringify({ title: title ?? 'New chat' }),
    });
    return {
      id: r.id, title: r.title, createdAt: r.created_at,
      lastMessageAt: r.last_message_at, messageCount: r.message_count,
      archived: r.archived, isDefault: !!r.is_default,
    };
  }

  /** Rename a session. Server rejects on the synthetic Default chat. */
  async renameSession(sessionId: string, title: string): Promise<ChatSessionInfo> {
    interface RawRow {
      id: string; title: string; created_at: string;
      last_message_at: string | null; message_count: number;
      archived: boolean; is_default?: boolean;
    }
    const r = await this.fetch<RawRow>(
      `/api/v1/sessions/${encodeURIComponent(sessionId)}`,
      { method: 'PATCH', body: JSON.stringify({ title }) },
    );
    return {
      id: r.id, title: r.title, createdAt: r.created_at,
      lastMessageAt: r.last_message_at, messageCount: r.message_count,
      archived: r.archived, isDefault: !!r.is_default,
    };
  }

  /** Soft-archive a session (hidden from default list, kept in event_log). */
  async archiveSession(sessionId: string): Promise<void> {
    await this.fetch(
      `/api/v1/sessions/${encodeURIComponent(sessionId)}`,
      { method: 'DELETE' },
    );
  }

  /** Pull the chat history for one session — messages newest-first as
   *  stored, returned oldest-first so the UI can render top-down. */
  async listSessionMessages(sessionId: string, limit = 200): Promise<ChatMessageRow[]> {
    interface RawRow {
      event_idx: number;
      event_kind: string;
      ts: number;
      payload: { text?: string; session_id?: string; attachments?: string[] };
    }
    interface RawResp { messages?: RawRow[]; events?: RawRow[] }
    // The backend's /agent/messages endpoint returns history filtered
    // by session_id. We re-shape into the role-oriented form the chat
    // pane expects.
    const r = await this.fetch<RawResp>(
      `/api/v1/agent/messages?session_id=${encodeURIComponent(sessionId)}&limit=${limit}`,
    );
    const rows = r.messages ?? r.events ?? [];
    return rows.map((row) => ({
      eventIdx: row.event_idx,
      role: row.event_kind === 'user_message' ? 'user'
          : row.event_kind === 'assistant_response' ? 'agent'
          : 'system',
      text: String(row.payload?.text ?? ''),
      ts: row.ts,
      attachments: Array.isArray(row.payload?.attachments) ? row.payload!.attachments! : [],
    }));
  }

  /**
   * Open the bundled DICOM viewer for the given study.
   *
   * Why this isn't ``window.open`` to the system browser anymore:
   *
   *   The system browser has NO access to the JWT (which lives in
   *   sessionStorage of the Tauri webview). The viewer's fetches to
   *   /api/v1/dicom/* therefore 401 — the page sits at "Loading…"
   *   forever. We saw this in the field 2026-06-14.
   *
   *   We now spawn a new Tauri ``WebviewWindow`` and pass the JWT in
   *   the URL query (``&token=…``). The viewer's static HTML already
   *   reads ``params.get('token')`` so no server-side change is
   *   needed. Token-in-URL is acceptable here because:
   *     * the URL never leaves the local machine (loopback only),
   *     * the JWT TTL is ~1h, and
   *     * the only consumer is our own bundled HTML page.
   *
   * Outside Tauri (``pnpm dev`` in a regular browser tab) we fall
   * back to ``window.open`` so dev mode still works.
   */
  async openDicomViewer(studyId: string): Promise<void> {
    const token = this.token ?? '';
    const url = `${baseUrl}/dicom-viewer/?studyId=${encodeURIComponent(studyId)}` +
      (token ? `&token=${encodeURIComponent(token)}` : '');

    // Try the Tauri path first — gives us a real desktop window with
    // a chrome titlebar instead of a system-browser tab.
    try {
      const mod = await import('@tauri-apps/api/webviewWindow');
      const WebviewWindow = (mod as { WebviewWindow?: typeof import('@tauri-apps/api/webviewWindow').WebviewWindow }).WebviewWindow;
      if (WebviewWindow) {
        // Window label must match the capability allowlist (``dicom-*``).
        // Append the first 8 chars of the study id so opening two
        // studies side-by-side produces two distinct windows instead
        // of focus-stealing into one.
        const label = `dicom-${studyId.slice(0, 8)}`;
        const existing = await (WebviewWindow as unknown as {
          getByLabel: (l: string) => Promise<unknown | null>
        }).getByLabel(label).catch(() => null);
        if (existing) {
          // Already open — just focus it.
          try {
            await (existing as { setFocus: () => Promise<void> }).setFocus();
          } catch { /* best-effort focus */ }
          return;
        }
        // Build a new window. The chrome title carries the study id
        // short-hash so a power-user can tell which study they're
        // looking at from Mission Control.
        new WebviewWindow(label, {
          url,
          title: `DICOM viewer · ${studyId.slice(0, 8)}`,
          width:  1280,
          height:  900,
          minWidth:  900,
          minHeight: 600,
          resizable: true,
        });
        return;
      }
    } catch {
      /* Tauri import failed — fall through to window.open */
    }
    // pnpm dev / browser fallback. Token still goes in the URL so
    // the viewer page can authenticate against the dev FastAPI on
    // 8001. In a stock browser the URL appears in history; acceptable
    // for dev.
    window.open(url, '_blank', 'noopener');
  }

  /**
   * Manual retry of the Tier-A Quick scan for an existing study.
   *
   * Calls ``POST /api/v1/dicom/studies/{study_id}/quick-scan`` which:
   *   1. Marks the matching ``uploads.quick_scan_status='pending'``.
   *   2. Re-runs Gemini Flash triage in a background task.
   *   3. Writes back ``ok`` + summary, or ``error`` + traceback.
   *
   * Returns immediately after enqueueing. The caller is responsible
   * for polling ``getPrerenderProgress(fileId)`` to surface the
   * in-progress / completion states in the UploadJobRow.
   */
  async triggerQuickScan(studyId: string): Promise<{ status: string; study_id: string }> {
    return this.fetch(
      `/api/v1/dicom/studies/${encodeURIComponent(studyId)}/quick-scan`,
      { method: 'POST' },
    );
  }

  async listPatients() {
    interface Raw {
      patient_hash: string;
      patient_age_group: string | null;
      patient_sex: string | null;
      study_count: number;
      latest_study_date: string | null;
      latest_modality: string | null;
      last_seen_at: number;
      initials?: string;
      mrn?: string;
      sequence_number?: number;
      created_at?: number;
    }
    const raw = await this.fetch<Raw[]>('/api/v1/dicom/patients');
    return raw.map((r) => ({
      patientHash:     r.patient_hash,
      ageGroup:        r.patient_age_group ?? '',
      sex:             (r.patient_sex as 'M' | 'F' | '') ?? '',
      studyCount:      r.study_count ?? 0,
      latestStudyDate: r.latest_study_date ?? '',
      latestModality:  r.latest_modality ?? '',
      lastSeenAt:      r.last_seen_at ?? 0,
      initials:        r.initials ?? '',
      mrn:             r.mrn ?? '',
      sequenceNumber:  r.sequence_number ?? 0,
      createdAt:       r.created_at ?? 0,
    }));
  }

  /* ────────────────────────── memory v3 ────────────────────────── */

  async getPatientProjection(patientHash: string): Promise<PatientProjection> {
    interface Raw {
      patient_hash: string;
      findings: any[]; medications: any[]; differentials: any[];
      studies: any[]; semantic_facts: any[];
      unresolved_conflict_count: number;
    }
    const r = await this.fetch<Raw>(
      `/api/v1/memory/patient/${encodeURIComponent(patientHash)}/projection`,
    );
    const cast = (n: any) => ({
      nodeId: n.node_id, nodeType: n.node_type, content: n.content,
      weight: n.weight, encounterId: n.encounter_id, updatedAt: n.updated_at,
    });
    return {
      patientHash: r.patient_hash,
      findings: r.findings.map(cast),
      medications: r.medications.map(cast),
      differentials: r.differentials.map(cast),
      studies: r.studies.map(cast),
      semanticFacts: r.semantic_facts.map(cast),
      unresolvedConflictCount: r.unresolved_conflict_count,
    };
  }

  async getCitation(nodeId: number): Promise<ProvenanceRow> {
    interface Raw {
      node_id: number; source_kind: string; source_ref: string;
      source_locator: any; evidence_quote: string;
      extraction_model: string; extraction_prompt_id: string;
      confidence: number; redaction_version: string;
      extracted_at: number; extracted_by_user: string;
      superseded_by_node: number | null; retracted_at: number | null;
    }
    const r = await this.fetch<Raw>(
      `/api/v1/memory/citation/${nodeId}`,
    );
    return {
      nodeId: r.node_id,
      sourceKind: r.source_kind as ProvenanceRow['sourceKind'],
      sourceRef: r.source_ref,
      sourceLocator: r.source_locator,
      evidenceQuote: r.evidence_quote,
      extractionModel: r.extraction_model,
      extractionPromptId: r.extraction_prompt_id,
      confidence: r.confidence,
      redactionVersion: r.redaction_version,
      extractedAt: r.extracted_at,
      extractedByUser: r.extracted_by_user,
      supersededByNode: r.superseded_by_node,
      retractedAt: r.retracted_at,
    };
  }

  /* ────────────────────────── practitioner ────────────────────────── */

  async listPractitionerCandidates(): Promise<PractitionerCandidate[]> {
    interface Raw { candidates: any[]; }
    const r = await this.fetch<Raw>('/api/v1/memory/practitioner/candidates');
    return r.candidates.map((c) => ({
      factKind: c.fact_kind,
      patternKey: c.pattern_key,
      patternValue: c.pattern_value,
      observedCount: c.observed_count,
      distinctPatientCount: c.distinct_patient_count,
      confidence: c.confidence,
      firstObservedAt: c.first_observed_at,
      lastReinforcedAt: c.last_reinforced_at,
    }));
  }

  async practitionerPendingCount(): Promise<number> {
    const r = await this.fetch<{ count: number }>(
      '/api/v1/memory/practitioner/pending_count',
    );
    return r.count;
  }

  async confirmPractitionerFact(factKind: string, patternKey: string) {
    return this.fetch(
      `/api/v1/memory/practitioner/${encodeURIComponent(factKind)}/${encodeURIComponent(patternKey)}/confirm`,
      { method: 'POST' },
    );
  }

  async rejectPractitionerFact(factKind: string, patternKey: string, reason?: string) {
    const qs = reason ? `?reason=${encodeURIComponent(reason)}` : '';
    return this.fetch(
      `/api/v1/memory/practitioner/${encodeURIComponent(factKind)}/${encodeURIComponent(patternKey)}/reject${qs}`,
      { method: 'POST' },
    );
  }

  /* ────────────────────────── export / restore ────────────────────────── */

  /**
   * Trigger a full self-contained export (FHIR R5 + JSON + SQL dump).
   * Returns the path on disk where the bundle was written and the row
   * counts for the toast. Backend writes to ~/Documents/Nexus Archive/.
   *
   * If the endpoint isn't deployed yet, the caller gets a 404 ApiError
   * and surfaces a "ships in M3.3 finalize" message — no silent failure.
   */
  async exportBundle(): Promise<{
    bundlePath: string;
    bytes: number;
    counts: Record<string, number>;
    createdAt: number;
  }> {
    interface Raw {
      bundle_path: string;
      bytes: number;
      counts: Record<string, number>;
      created_at: number;
    }
    const r = await this.fetch<Raw>('/api/v1/export/bundle', {
      method: 'POST',
      body: JSON.stringify({}),
    });
    return {
      bundlePath: r.bundle_path,
      bytes:      r.bytes,
      counts:     r.counts,
      createdAt:  r.created_at,
    };
  }

  /** Resolve the on-disk path of the user's archive folder.
   *  Backend computes this from $HOME / Documents / Nexus Archive. */
  async archiveFolder(): Promise<string> {
    const r = await this.fetch<{ path: string }>('/api/v1/export/archive_path');
    return r.path;
  }

  /* ────────────────────────── settings · LLM ────────────────────────── */

  /** Read LLM settings from the backend; if the endpoint is missing
   *  (stale binary), fall back to a direct Tauri IPC read of the .env
   *  file so the UI still shows what's on disk. */
  async getLlmSettings(): Promise<LlmStatus> {
    interface Raw {
      provider: string;
      model: string;
      env_file_path: string;
      env_file_exists: boolean;
      has_gemini_key: boolean;
      has_openai_key: boolean;
      has_anthropic_key: boolean;
      advisory: string | null;
    }
    try {
      const r = await this.fetch<Raw>('/api/v1/settings/llm');
      return {
        provider:        r.provider as LlmStatus['provider'],
        model:           r.model,
        envFilePath:     r.env_file_path,
        envFileExists:   r.env_file_exists,
        hasGeminiKey:    r.has_gemini_key,
        hasOpenaiKey:    r.has_openai_key,
        hasAnthropicKey: r.has_anthropic_key,
        advisory:        r.advisory,
      };
    } catch (e) {
      // Backend 404 / 5xx → try Tauri's direct-from-disk read.
      const ipc = await tauriInvoke<Raw>('llm_env_status');
      if (ipc) {
        return {
          provider:        ipc.provider as LlmStatus['provider'],
          model:           ipc.model,
          envFilePath:     ipc.env_file_path,
          envFileExists:   ipc.env_file_exists,
          hasGeminiKey:    ipc.has_gemini_key,
          hasOpenaiKey:    ipc.has_openai_key,
          hasAnthropicKey: ipc.has_anthropic_key,
          advisory:        ipc.advisory,
        };
      }
      throw e;
    }
  }

  async putLlmSettings(input: {
    provider?: 'gemini' | 'openai' | 'anthropic';
    model?: string;
    geminiApiKey?: string;
    openaiApiKey?: string;
    anthropicApiKey?: string;
  }): Promise<{ ok: boolean; writtenKeys: string[]; status: LlmStatus; viaFallback?: boolean }> {
    interface Raw {
      ok: boolean;
      env_file_path: string;
      written_keys: string[];
      status: {
        provider: string;
        model: string;
        env_file_path: string;
        env_file_exists: boolean;
        has_gemini_key: boolean;
        has_openai_key: boolean;
        has_anthropic_key: boolean;
        advisory: string | null;
      };
    }
    const body: Record<string, string> = {};
    if (input.provider)        body.provider          = input.provider;
    if (input.model)           body.model             = input.model;
    if (input.geminiApiKey)    body.gemini_api_key    = input.geminiApiKey;
    if (input.openaiApiKey)    body.openai_api_key    = input.openaiApiKey;
    if (input.anthropicApiKey) body.anthropic_api_key = input.anthropicApiKey;
    try {
      const r = await this.fetch<Raw>('/api/v1/settings/llm', {
        method: 'PUT',
        body: JSON.stringify(body),
      });
      return {
        ok: r.ok,
        writtenKeys: r.written_keys,
        status: {
          provider:        r.status.provider as LlmStatus['provider'],
          model:           r.status.model,
          envFilePath:     r.status.env_file_path,
          envFileExists:   r.status.env_file_exists,
          hasGeminiKey:    r.status.has_gemini_key,
          hasOpenaiKey:    r.status.has_openai_key,
          hasAnthropicKey: r.status.has_anthropic_key,
          advisory:        r.status.advisory,
        },
      };
    } catch (e) {
      // Backend endpoint missing (stale binary) → write the .env via
      // Tauri IPC directly. This is THE fallback that makes Save work
      // before the user rebuilds; once the new sidecar comes up it
      // reads the same file.
      const updates: Record<string, string> = {};
      if (input.provider)        updates.DEFAULT_LLM_PROVIDER = input.provider;
      if (input.model)           updates.DEFAULT_LLM_MODEL    = input.model;
      if (input.geminiApiKey)    updates.GEMINI_API_KEY       = input.geminiApiKey;
      if (input.openaiApiKey)    updates.OPENAI_API_KEY       = input.openaiApiKey;
      if (input.anthropicApiKey) updates.ANTHROPIC_API_KEY    = input.anthropicApiKey;

      const ipc = await tauriInvoke<Raw>('llm_env_write', { updates });
      if (!ipc) {
        // Neither path available — we're in browser-only dev with no
        // Tauri runtime AND no backend. Surface the original error.
        throw e;
      }
      return {
        ok: ipc.ok,
        writtenKeys: ipc.written_keys,
        viaFallback: true,
        status: {
          provider:        ipc.status.provider as LlmStatus['provider'],
          model:           ipc.status.model,
          envFilePath:     ipc.status.env_file_path,
          envFileExists:   ipc.status.env_file_exists,
          hasGeminiKey:    ipc.status.has_gemini_key,
          hasOpenaiKey:    ipc.status.has_openai_key,
          hasAnthropicKey: ipc.status.has_anthropic_key,
          advisory:        ipc.status.advisory,
        },
      };
    }
  }

  /** Kick the sidecar (kill + respawn) so a freshly-written .env is
   *  picked up without quitting the app. No-op when not in Tauri. */
  async restartSidecar(): Promise<boolean> {
    const r = await tauriInvoke<string>('restart_sidecar');
    return r === 'restarted';
  }

  /**
   * Pull the sidecar's structured diagnostics — ring buffer of recent
   * stdout/stderr lines + alive/exit status + the on-disk log path.
   *
   * Used by LoginView when ``/health`` is unreachable: instead of just
   * showing "Cannot reach server" we render the last ~30 lines of
   * actual server output so the user can see WHY (PyInstaller import
   * error, port collision, missing key, Alembic exception, etc.).
   *
   * Returns ``null`` when not running under Tauri (the IPC isn't
   * registered) — callers should fall back to "see the logs" copy.
   */
  async getSidecarDiagnostics(): Promise<SidecarDiagnostics | null> {
    const r = await tauriInvoke<SidecarDiagnostics>('get_sidecar_diagnostics');
    return r ?? null;
  }

  /* ────────────────────────── email ────────────────────────── */

  /** GET /api/v1/email/transport — what can the server send through
   *  right now? Returned ``configured`` is the boolean the Compose
   *  dialog's Send button gates on. Cheap (env-read only); we poll
   *  every time the dialog opens so newly-dropped creds in
   *  $RUNE_HOME/.env get picked up. */
  async getEmailTransport(): Promise<EmailTransportStatus> {
    interface Raw {
      configured: boolean;
      relay_configured: boolean;
      smtp_configured: boolean;
      bundled_creds: boolean;
      default_from: string;
      allowed_recipients: string[];
      relay_url_host: string;
    }
    const r = await this.fetch<Raw>('/api/v1/email/transport');
    return {
      configured:        r.configured,
      relayConfigured:   r.relay_configured,
      smtpConfigured:    r.smtp_configured,
      bundledCreds:      r.bundled_creds,
      defaultFrom:       r.default_from,
      allowedRecipients: r.allowed_recipients,
      relayUrlHost:      r.relay_url_host,
    };
  }

  /** POST /api/v1/email/send — dispatch one outbound email.
   *
   *  Returns 200 + ``ok=false`` on send-level failures (relay rejected,
   *  SMTP auth bad, recipient blocked, etc.) so the UI can surface
   *  ``message`` directly. Throws ApiError on HTTP-level failures
   *  (401 expired, 422 schema, 503 nothing configured). */
  async sendEmail(input: {
    to: string[];
    subject: string;
    body: string;
    cc?: string[];
  }): Promise<EmailSendResult> {
    interface Raw {
      ok: boolean;
      transport: string;
      message: string;
      sent_to: string[];
      status_code: number;
    }
    const r = await this.fetch<Raw>('/api/v1/email/send', {
      method: 'POST',
      body: JSON.stringify({
        to:      input.to,
        cc:      input.cc ?? [],
        subject: input.subject,
        body:    input.body,
      }),
    });
    return {
      ok:         r.ok,
      transport:  r.transport as EmailSendResult['transport'],
      message:    r.message,
      sentTo:     r.sent_to,
      statusCode: r.status_code,
    };
  }

  /* ────────────────────────── chat (SSE) ────────────────────────── */

  /**
   * Stream a chat turn. Returns an async iterable of typed chunks.
   *
   * The backend emits `data: {...}\n\n` SSE messages; this method parses
   * them into ChatStreamChunk objects.
   */
  async *sendChat(
    text: string,
    sessionId: string,
    patientHash: string | null,
    attachments: string[] = [],
  ): AsyncIterable<ChatStreamChunk> {
    const r = await fetch(`${baseUrl}/api/v1/agent/chat`, {
      method: 'POST',
      headers: this.headers({ 'Content-Type': 'application/json' }),
      body: JSON.stringify({
        text, session_id: sessionId, patient_hash: patientHash,
        attachments,
      }),
    });
    if (!r.ok || !r.body) {
      throw new ApiError(r.status, await r.text().catch(() => r.statusText),
                         '/api/v1/agent/chat');
    }

    const reader = r.body.getReader();
    const dec = new TextDecoder();
    let buf = '';
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      let idx: number;
      // SSE messages are separated by blank lines.
      while ((idx = buf.indexOf('\n\n')) !== -1) {
        const raw = buf.slice(0, idx);
        buf = buf.slice(idx + 2);
        for (const line of raw.split('\n')) {
          if (line.startsWith('data: ')) {
            try {
              yield JSON.parse(line.slice(6)) as ChatStreamChunk;
            } catch {
              /* malformed payload; skip */
            }
          }
        }
      }
    }
  }
}

/**
 * Lazy Tauri IPC invoke. Returns ``null`` when not running inside the
 * Tauri shell (e.g. plain ``pnpm dev`` in a browser tab) so callers
 * can ``if (r) { ... } else fall back to HTTP``. We dynamic-import
 * ``@tauri-apps/api/core`` so the bundle still loads cleanly outside
 * Tauri — the import itself throws there.
 */
async function tauriInvoke<T>(
  cmd: string,
  args: Record<string, unknown> = {},
): Promise<T | null> {
  try {
    const mod = await import('@tauri-apps/api/core');
    if (mod && typeof mod.invoke === 'function') {
      return (await mod.invoke(cmd, args)) as T;
    }
  } catch {
    /* not running under Tauri — fall through */
  }
  return null;
}

export class ApiError extends Error {
  constructor(public status: number, body: string, public path: string) {
    super(`API ${status} on ${path}: ${body}`);
  }
}

/** Shape returned by the Tauri `get_sidecar_diagnostics` IPC. Mirrors
 *  the JSON built in `src-tauri/src/lib.rs::SidecarDiag::snapshot`. */
export interface SidecarDiagLine {
  /** Unix-seconds at capture time. */
  ts: number;
  /** One of "stdout", "stderr", "sys" — sys is our own annotation. */
  stream: 'stdout' | 'stderr' | 'sys';
  text: string;
}

/** One row in the sessions sidebar. ``id`` is empty for the synthetic
 *  Default chat (which wraps pre-session-feature chat history); the
 *  UI hides rename / archive on that one. */
export interface ChatSessionInfo {
  id: string;
  title: string;
  createdAt: string;
  lastMessageAt: string | null;
  messageCount: number;
  archived: boolean;
  isDefault: boolean;
}

/** One persisted chat row as returned by ``listSessionMessages``.
 *  Includes attachments so the UI can re-render the pasted-file chips
 *  when the medic reopens an old session. */
export interface ChatMessageRow {
  eventIdx: number;
  role: 'user' | 'agent' | 'system';
  text: string;
  ts: number;
  attachments: string[];
}

/**
 * Live progress snapshot for an in-flight Quick scan. Returned by the
 * server's ``/api/v1/files/{fileId}/prerender-progress`` endpoint
 * under ``quickScanProgress`` while the scan is running, so the
 * desktop's Imaging card can stream "Triaging 15/75 grids · lung
 * window" + the recent findings tail instead of the static "Quick
 * scan: running…" placeholder.
 *
 * Shape mirrors the server side's ``_quick_scan_progress`` dict —
 * see ``packages/server/nexus_server/quick_scan.py::_set_quick_scan_progress``.
 */
export interface QuickScanProgress {
  /** Coarse phase. ``rendering`` → still building 4×4 PNG grids;
   *  ``triaging`` → grids built, Gemini Flash calls in flight;
   *  ``complete`` → both done (uploads.quick_scan_status will be ok/error);
   *  ``error`` → study load / render aborted (last_error has detail). */
  stage: 'rendering' | 'triaging' | 'complete' | 'error';
  /** Unix seconds when the scan kicked off. */
  started_at: number;
  /** Wall-clock since started_at; the server updates this on every push. */
  elapsed_s: number;
  /** Final expected grid count (n_presets × n_per_preset). 0 until the
   *  worker reaches Stage 0. */
  total_grids: number;
  /** Grids rendered to PNG so far. Catches up to total_grids before
   *  ``triaged_grids`` starts moving. */
  rendered_grids: number;
  /** Gemini Flash returns processed. The most user-facing counter. */
  triaged_grids: number;
  /** Cumulative API-error count over all grids. UI uses this to
   *  decide whether to colour the running progress red. */
  errors: number;
  /** Window presets the scanner picked for this study, in order
   *  (e.g. ``['lung','mediastinum','bone']`` for chest CT). */
  presets: string[];
  /** Window currently being scanned, or '' between presets. */
  current_preset: string;
  /** Modality / body part / volume size — copied from the study at
   *  scan start so the UI can render "CT WHOLEBODY · 482 slices". */
  modality?: string;
  body_part?: string;
  scan_count?: number;
  total_slices?: number;
  /** Bounded ring of non-clean grid results (~last 8). Each entry is
   *  a flat record the UI renders as one bullet. */
  recent: Array<{
    slice_start: number;
    slice_end:   number;
    window:      string;
    verdict:     'suspicious' | 'unsure' | 'error' | string;
    finding:     string;
    urgency:     string;
    error:       string;
  }>;
  /** Populated on terminal failure paths (study not loadable, etc.). */
  last_error?: string;
  /** Final summary_counts dict; only present after stage === 'complete'. */
  summary_counts?: Record<string, number>;
}

export interface SidecarDiagnostics {
  /** PID of the most-recently-tracked sidecar child; null before first spawn. */
  pid: number | null;
  /** True between spawn() and the matching Terminated event. */
  alive: boolean;
  /** Set when the sidecar died; null while still running. */
  last_exit_code: number | null;
  /** Unix seconds at the last spawn() (0 if never spawned). */
  started_at: number;
  /** Absolute path of the on-disk sidecar log (~/Library/Logs/Nexus/sidecar.log). */
  log_path: string;
  /** Ring buffer; newest line last. */
  lines: SidecarDiagLine[];
}

/** Shape returned by ``GET /api/v1/email/transport``. ``configured``
 *  is the boolean the Compose dialog's Send button gates on. */
export interface EmailTransportStatus {
  configured:        boolean;
  relayConfigured:   boolean;
  smtpConfigured:    boolean;
  bundledCreds:      boolean;
  /** "" when nothing configured; for SMTP path = NEXUS_SMTP_FROM (or USER);
   *  for relay path = "(relay · {host})" since the relay owns the envelope FROM. */
  defaultFrom:       string;
  /** Comma-list from NEXUS_SMTP_ALLOWED_RECIPIENTS — only enforced on SMTP path.
   *  Empty list = no restriction. */
  allowedRecipients: string[];
  /** Hostname extracted from NEXUS_RELAY_URL for display, e.g. "relay.nexus.io".
   *  Empty when no relay. */
  relayUrlHost:      string;
}

/** Result returned by ``POST /api/v1/email/send``. ``ok`` is the
 *  send-level outcome; UI surfaces ``message`` verbatim either way. */
export interface EmailSendResult {
  ok:         boolean;
  transport:  'relay' | 'smtp' | 'none';
  message:    string;
  sentTo:     string[];
  /** HTTP code from the relay if transport='relay'; 0 for SMTP. */
  statusCode: number;
}

export const api = new _ApiClient();
