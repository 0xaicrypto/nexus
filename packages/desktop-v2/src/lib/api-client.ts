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
//   2. http://localhost:8001 — the sidecar default (src-tauri/lib.rs
//      sets NEXUS_HOST=localhost, NEXUS_PORT=8001 when spawning).
//
// Why ``localhost`` and not ``127.0.0.1`` (F19): sticking to the DNS
// name end-to-end keeps the origin consistent so CORS and CSP rules
// don't have to allowlist both spellings.
//
// We CANNOT default to "" (relative URL) because in a bundled .dmg the
// frontend is served from tauri://localhost — relative URLs resolve
// against THAT origin and never reach the Python sidecar.
const envBase =
  (import.meta as unknown as { env?: { VITE_NEXUS_API?: string } }).env
    ?.VITE_NEXUS_API;
const baseUrl = envBase && envBase.length > 0 ? envBase : 'http://localhost:8001';

// ─────────────────────────────────────────────────────────────────────
// Persistent user_id storage
// ─────────────────────────────────────────────────────────────────────
// The user_id is the medic's stable identifier — NOT auth. Auth is
// the JWT (in sessionStorage; wiped on window close per the
// "auto-logout on close" UX). We keep the last-seen user_id in
// localStorage purely as a diagnostic / display convenience.

const STORAGE_KEY_USER_ID = 'nexus.auth.user_id';

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
  /** Role from the most recent register/login/claim response. The
   *  authoritative persisted copy lives in the zustand store
   *  (``useAppState().role``); this mirror lets non-React callers
   *  gate admin requests without a store import. */
  private role: UserRole | null = null;

  setToken(t: string | null) { this.token = t; }
  hasToken() { return this.token !== null; }
  getToken() { return this.token; }
  getRole() { return this.role; }

  /** Base URL the client posts to — useful when the UI needs to build
   *  a non-fetch URL (e.g. an <a href> to /dicom-viewer/). */
  get baseUrl() { return baseUrl; }

  private headers(extra?: HeadersInit): Headers {
    const h = new Headers(extra);
    h.set('Accept', 'application/json');
    if (this.token) h.set('Authorization', `Bearer ${this.token}`);
    return h;
  }

  private async fetch<T>(path: string, init?: RequestInit): Promise<T> {
    const doFetch = async (): Promise<Response> => {
      const h = this.headers(init?.headers);
      if (init?.body && !h.has('Content-Type')) h.set('Content-Type', 'application/json');
      return fetch(`${baseUrl}${path}`, { ...init, headers: h });
    };

    const r = await doFetch();

    if (!r.ok) {
      const text = await r.text().catch(() => '');
      const err = new ApiError(r.status, text || r.statusText, path);

      // Password auth means there is no silent re-auth path any more:
      // a 401 outside the auth endpoints = expired / invalid JWT →
      // wipe the token and bounce to LoginView. App.tsx listens for
      // the event and performs the store-level logout.
      if (r.status === 401 && !path.startsWith('/api/v1/auth/')) {
        this.token = null;
        this.role = null;
        try { sessionStorage.removeItem('nexus.auth.token'); } catch { /* ignore */ }
        try {
          window.dispatchEvent(new CustomEvent('nexus:auth-expired'));
        } catch { /* SSR */ }
      }

      // 403 account_disabled — an admin disabled this account while a
      // session was live. Globally handled: App.tsx logs out + toasts.
      if (r.status === 403 && err.code === 'account_disabled') {
        try {
          window.dispatchEvent(new CustomEvent('nexus:account-disabled'));
        } catch { /* SSR */ }
      }

      throw err;
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
      // F-tab-switch-race fallback — WebKit has a 6-concurrent-per-
      // origin fetch limit. Long-running background SSEs (the AI
      // continuing to think while the medic switched tabs) can
      // occupy all 6 slots, starving the health probe and making
      // it falsely time out → "Backend unreachable" banner even
      // though the sidecar is fine.
      //
      // Tauri's ``server_health`` IPC command bypasses WebKit's
      // HTTP stack entirely — it hits the sidecar through the
      // shell-plugin process bridge. If that returns ok, the
      // sidecar IS healthy and the fetch-side failure is just
      // connection-slot starvation, not a real outage.
      try {
        const ipc = await tauriInvoke<{ ok: boolean }>('server_health');
        if (ipc && ipc.ok) return 'ok';
      } catch { /* IPC also failed; fall through */ }
      return 'unreachable';
    }
  }

  /* ────────────────────────── auth ────────────────────────── */

  /**
   * Username + password auth (2026-07 server rework).
   *
   * Backend endpoints:
   *   POST /api/v1/auth/register {username, password, display_name?}
   *     → 201 {user_id, jwt_token, created_at, role, expires_in_seconds}
   *   POST /api/v1/auth/login    {username, password}
   *     → 200 {jwt_token, expires_in_seconds, user_id, role, display_name}
   *   POST /api/v1/auth/claim    {username, password}
   *     → one-time set-password for legacy passwordless accounts;
   *       200 with the same shape as /login.
   *
   * Errors arrive as the envelope
   *   {"error": {"code": "...", "message": "..."}, "status_code": N}
   * which ``ApiError`` parses — route on ``err.code``:
   *   register: 409 username_taken · 422 validation · 429 rate_limited
   *   login:    401 invalid_credentials · 409 claim_required
   *             · 403 account_disabled · 429 rate_limited
   *   claim:    404 user_not_found · 409 already_claimed
   */
  async register(input: {
    username:     string;
    password:     string;
    displayName?: string;
  }): Promise<AuthSession> {
    interface Raw {
      user_id:            string;
      jwt_token:          string;
      created_at:         string;
      role:               string;
      expires_in_seconds: number;
    }
    const body: Record<string, string> = {
      username: input.username,
      password: input.password,
    };
    if (input.displayName && input.displayName.trim()) {
      body.display_name = input.displayName.trim();
    }
    const r = await this.fetch<Raw>('/api/v1/auth/register', {
      method: 'POST',
      body: JSON.stringify(body),
    });
    writeUserId(r.user_id);
    this.token = r.jwt_token;
    this.role  = _castRole(r.role);
    return {
      token:            r.jwt_token,
      userId:           r.user_id,
      role:             this.role,
      displayName:      input.displayName?.trim() || input.username,
      expiresInSeconds: r.expires_in_seconds,
    };
  }

  async login(username: string, password: string): Promise<AuthSession> {
    interface Raw {
      jwt_token:          string;
      expires_in_seconds: number;
      user_id:            string;
      role:               string;
      display_name:       string | null;
    }
    const r = await this.fetch<Raw>('/api/v1/auth/login', {
      method: 'POST',
      body: JSON.stringify({ username, password }),
    });
    writeUserId(r.user_id);
    this.token = r.jwt_token;
    this.role  = _castRole(r.role);
    return {
      token:            r.jwt_token,
      userId:           r.user_id,
      role:             this.role,
      displayName:      r.display_name || username,
      expiresInSeconds: r.expires_in_seconds,
    };
  }

  /** One-time set-password for a legacy passwordless account. The
   *  login screen routes here when /login returns 409 claim_required. */
  async claim(username: string, password: string): Promise<AuthSession> {
    interface Raw {
      jwt_token:          string;
      expires_in_seconds: number;
      user_id:            string;
      role:               string;
      display_name:       string | null;
    }
    const r = await this.fetch<Raw>('/api/v1/auth/claim', {
      method: 'POST',
      body: JSON.stringify({ username, password }),
    });
    writeUserId(r.user_id);
    this.token = r.jwt_token;
    this.role  = _castRole(r.role);
    return {
      token:            r.jwt_token,
      userId:           r.user_id,
      role:             this.role,
      displayName:      r.display_name || username,
      expiresInSeconds: r.expires_in_seconds,
    };
  }

  /** Clear the cached user_id + in-memory credentials. Used by
   *  Settings → "Sign out / forget me". */
  forgetUserId() {
    clearUserId();
    this.token = null;
    this.role = null;
  }

  /* ────────────────────── admin · user management ────────────────── */
  // All four require a JWT whose role=admin; the server answers
  // 403 admin_required otherwise. Disabling yourself is rejected
  // with 400 cannot_disable_self.

  async adminListUsers(): Promise<AdminUser[]> {
    interface RawUser {
      user_id:       string;
      username:      string;
      role:          string;
      created_at:    string | number | null;
      disabled_at:   string | number | null;
      last_login_at: string | number | null;
      has_password:  boolean;
    }
    const r = await this.fetch<{ users: RawUser[] }>('/api/v1/admin/users');
    return (r.users ?? []).map((u) => ({
      userId:      u.user_id,
      username:    u.username,
      role:        _castRole(u.role),
      createdAt:   u.created_at ?? null,
      disabledAt:  u.disabled_at ?? null,
      lastLoginAt: u.last_login_at ?? null,
      hasPassword: !!u.has_password,
    }));
  }

  async adminDisableUser(userId: string): Promise<void> {
    await this.fetch<unknown>(
      `/api/v1/admin/users/${encodeURIComponent(userId)}/disable`,
      { method: 'POST', body: JSON.stringify({}) },
    );
  }

  async adminEnableUser(userId: string): Promise<void> {
    await this.fetch<unknown>(
      `/api/v1/admin/users/${encodeURIComponent(userId)}/enable`,
      { method: 'POST', body: JSON.stringify({}) },
    );
  }

  async adminResetPassword(userId: string, newPassword: string): Promise<void> {
    await this.fetch<unknown>(
      `/api/v1/admin/users/${encodeURIComponent(userId)}/reset-password`,
      { method: 'POST', body: JSON.stringify({ new_password: newPassword }) },
    );
  }

  /* ─────────────────── F-unified-chat-files — chat file lib ──────── */

  async listChatFiles(opts: {
    scopeKind: 'patient' | 'research' | 'cross_research' | 'assistant';
    scopeRef: string;
    includeRemoved?: boolean;
  }): Promise<{
    files: Array<{
      fileId: string;
      name: string;
      mime: string;
      sizeBytes: number;
      createdAt: string;
      fIdToken: string;
      textExtractionStatus: string;
      hasText: boolean;
      deletedAt?: number | null;
    }>;
    totalActive: number;
    totalRemoved: number;
  }> {
    interface RawFile {
      file_id: string;
      name: string;
      mime: string;
      size_bytes: number;
      created_at: string;
      f_id_token: string;
      text_extraction_status: string;
      has_text: boolean;
      deleted_at?: number | null;
    }
    interface Raw {
      files: RawFile[];
      total_active: number;
      total_removed: number;
      scope_kind: string;
      scope_ref: string;
    }
    const qs = new URLSearchParams({
      scope_kind: opts.scopeKind,
      scope_ref:  opts.scopeRef,
    });
    if (opts.includeRemoved) qs.set('include_removed', 'true');
    const r = await this.fetch<Raw>(`/api/v1/chat/files?${qs.toString()}`);
    return {
      files: r.files.map((f) => ({
        fileId: f.file_id, name: f.name, mime: f.mime,
        sizeBytes: f.size_bytes, createdAt: f.created_at,
        fIdToken: f.f_id_token,
        textExtractionStatus: f.text_extraction_status,
        hasText: f.has_text,
        deletedAt: f.deleted_at ?? null,
      })),
      totalActive: r.total_active,
      totalRemoved: r.total_removed,
    };
  }

  async deleteChatFile(fileId: string): Promise<{ fileId: string; deletedAt: number }> {
    interface Raw { file_id: string; deleted_at: number }
    const r = await this.fetch<Raw>(
      `/api/v1/chat/files/${encodeURIComponent(fileId)}`,
      { method: 'DELETE' },
    );
    return { fileId: r.file_id, deletedAt: r.deleted_at };
  }

  async restoreChatFile(fileId: string): Promise<{ fileId: string }> {
    interface Raw { file_id: string }
    const r = await this.fetch<Raw>(
      `/api/v1/chat/files/${encodeURIComponent(fileId)}/restore`,
      { method: 'POST', body: JSON.stringify({}) },
    );
    return { fileId: r.file_id };
  }

  async reextractChatFile(fileId: string): Promise<{
    fileId: string;
    textExtractionStatus: string;
    textLength: number;
  }> {
    interface Raw {
      file_id: string;
      text_extraction_status: string;
      text_length: number;
    }
    const r = await this.fetch<Raw>(
      `/api/v1/chat/files/${encodeURIComponent(fileId)}/reextract`,
      { method: 'POST', body: JSON.stringify({}) },
    );
    return {
      fileId: r.file_id,
      textExtractionStatus: r.text_extraction_status,
      textLength: r.text_length,
    };
  }

  /* ────────────────────── identities (read-only list) ────────────── */
  // 2026-07 auth rework: the server-side "create identity" and
  // "switch identity" endpoints are GONE — accounts are now
  // username+password, so switching = log out + sign in as the other
  // account. Only the read/patch/delete surface remains.

  async listIdentities(): Promise<{
    identities:   Identity[];
    activeUserId: string | null;
  }> {
    interface Raw {
      identities:     IdentityRaw[];
      active_user_id: string | null;
      schema_version: number;
    }
    const r = await this.fetch<Raw>('/api/v1/auth/identities');
    return {
      identities:   r.identities.map(_castIdentity),
      activeUserId: r.active_user_id,
    };
  }

  /** Rename / change emoji. Auth scoped to the calling identity. */
  async patchIdentity(userId: string, patch: {
    displayName?: string;
    avatarEmoji?: string;
  }): Promise<Identity> {
    interface Raw extends IdentityRaw {}
    const body: Record<string, string> = {};
    if (patch.displayName != null) body.display_name = patch.displayName;
    if (patch.avatarEmoji != null) body.avatar_emoji = patch.avatarEmoji;
    const r = await this.fetch<Raw>(
      `/api/v1/auth/identities/${encodeURIComponent(userId)}`,
      { method: 'PATCH', body: JSON.stringify(body) },
    );
    return _castIdentity(r);
  }

  /** Soft delete (90-day grace, then GC). Picker hides immediately. */
  async deleteIdentity(userId: string): Promise<void> {
    await this.fetch<void>(
      `/api/v1/auth/identities/${encodeURIComponent(userId)}`,
      { method: 'DELETE' },
    );
  }

  /** HARD wipe — irreversible. UI must require explicit 2-step
   *  confirmation; the magic token below is the deliberate friction. */
  async wipeIdentity(userId: string): Promise<void> {
    await this.fetch<void>(
      `/api/v1/auth/identities/${encodeURIComponent(userId)}/wipe`,
      {
        method: 'POST',
        body: JSON.stringify({ confirm_token: 'I-UNDERSTAND-WIPE' }),
      },
    );
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

  /**
   * F-archive-frontend — server-side soft hide.
   *
   * Replaces the previous localStorage-based ``hidePatient``
   * mechanism (which only filtered client-side and meant the
   * cross-patient chat's roster STILL saw the patient — the AI
   * would happily volunteer info about a "hidden" patient).
   *
   * Server flips ``patients.archived_at`` to now-ms. Every "list
   * patients" path on the server (including the cross-patient
   * roster injected into the LLM system prompt) filters
   * ``WHERE archived_at IS NULL``, so the AI truly no longer
   * sees the patient. DB rows are untouched; the medic can
   * unarchive at any time to bring everything back.
   */
  async archivePatient(patientHash: string): Promise<{
    patientHash: string;
    archivedAt: number;
  }> {
    interface Raw { patient_hash: string; archived_at: number }
    const r = await this.fetch<Raw>(
      `/api/v1/dicom/patients/${encodeURIComponent(patientHash)}/archive`,
      { method: 'POST', body: JSON.stringify({}) },
    );
    return { patientHash: r.patient_hash, archivedAt: r.archived_at };
  }

  /** Restore an archived patient. Idempotent on already-active rows
   *  (returns 404, which we treat as already-active in the store). */
  async unarchivePatient(patientHash: string): Promise<{
    patientHash: string;
  }> {
    interface Raw { patient_hash: string; archived_at: number }
    const r = await this.fetch<Raw>(
      `/api/v1/dicom/patients/${encodeURIComponent(patientHash)}/unarchive`,
      { method: 'POST', body: JSON.stringify({}) },
    );
    return { patientHash: r.patient_hash };
  }

  /** List ARCHIVED patients (for the "restore" UI in Settings).
   *  Adds ``?include=archived`` so server returns ONLY archived.
   *  This is a small dedicated endpoint to keep the default
   *  /patients list lean — most callers want active only. */
  async listArchivedPatients(): Promise<Array<{
    patientHash: string;
    initials: string;
    mrn: string;
    sex: string;
    ageGroup: string;
    archivedAt: number;
  }>> {
    interface Raw {
      patient_hash: string;
      initials: string;
      mrn: string;
      sex: string;
      age_group: string;
      archived_at: number;
    }
    const list = await this.fetch<Raw[]>(
      '/api/v1/dicom/patients?include=archived',
    );
    return list.map((r) => ({
      patientHash: r.patient_hash,
      initials:    r.initials,
      mrn:         r.mrn,
      sex:         r.sex,
      ageGroup:    r.age_group,
      archivedAt:  r.archived_at,
    }));
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
      /** F-unified-chat-files — which chat surface's library this
       *  upload joins. One of 'patient' | 'research' | 'cross_research'
       *  | 'assistant'. Omitted = legacy unattached behaviour. */
      libScopeKind?: 'patient' | 'research' | 'cross_research' | 'assistant';
      /** Scope target: patient_hash / study_id / '__workspace__'. */
      libScopeRef?: string;
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
      if (options?.libScopeKind) form.append('lib_scope_kind', options.libScopeKind);
      if (options?.libScopeRef)  form.append('lib_scope_ref',  options.libScopeRef);
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
   *  stored, returned oldest-first so the UI can render top-down.
   *
   *  Backend shape (as of S5 thin-client refactor, see
   *  ``packages/server/nexus_server/agent_state.py::ChatMessageView``):
   *
   *      { messages: [{
   *          role: "user" | "assistant",
   *          content: string,
   *          timestamp: ISO-8601 string,
   *          sync_id: number,
   *          attachments: [{ name, mime, size_bytes }],
   *          message_kind: "text" | "workflow_run",
   *          metadata: object
   *        }],
   *        total: number }
   *
   *  Bug history (2026-06-14)
   *  ────────────────────────
   *  This parser was coded against the OLD raw-event-log shape
   *  (``row.event_kind`` + ``row.payload.text``). After the S5
   *  pivot the backend returns the higher-level ChatMessageView,
   *  so every parsed row degraded to ``role='system'`` (which the
   *  renderer then coerced to 'user') with an empty ``text``. The
   *  symptom was 6 history rows all labelled "You" and visually
   *  empty — the original turns were intact in the DB, just never
   *  extracted by this method.
   */
  async listSessionMessages(sessionId: string, limit = 200): Promise<ChatMessageRow[]> {
    // Match server's ChatMessageView wire format exactly. Any field
    // marked optional here is something we tolerate missing (older
    // sidecar builds before .dmg upgrade) — required-side fields
    // mirror the Pydantic model.
    interface RawAttachment {
      name: string;
      mime?: string;
      size_bytes?: number;
    }
    interface RawRow {
      role: string;                  // "user" | "assistant"
      content: string;
      timestamp: string;             // ISO-8601
      sync_id: number;
      attachments?: RawAttachment[];
      message_kind?: string;
      metadata?: Record<string, unknown>;
    }
    interface RawResp { messages: RawRow[]; total: number }
    const r = await this.fetch<RawResp>(
      `/api/v1/agent/messages?session_id=${encodeURIComponent(sessionId)}&limit=${limit}`,
    );
    const rows = r.messages ?? [];
    return rows.map((row): ChatMessageRow => {
      // Map server roles to the UI's 3-way taxonomy. Anything not
      // "user" / "assistant" gets bucketed as 'system' so unknown
      // future kinds don't silently render in the wrong bubble.
      let role: ChatMessageRow['role'];
      if (row.role === 'assistant') role = 'agent';
      else if (row.role === 'user') role = 'user';
      else role = 'system';

      // ISO-8601 timestamp → unix seconds. Falls back to 0 on parse
      // failure rather than crashing the whole map() — a single bad
      // row shouldn't black-hole the entire history pane.
      let ts = 0;
      try {
        const t = Date.parse(row.timestamp);
        if (!Number.isNaN(t)) ts = Math.floor(t / 1000);
      } catch { /* leave ts=0 */ }

      return {
        eventIdx:    row.sync_id,
        role,
        text:        String(row.content ?? ''),
        ts,
        attachments: (row.attachments ?? []).map((a) => ({
          name:      a.name,
          mime:      a.mime ?? 'application/octet-stream',
          sizeBytes: a.size_bytes ?? 0,
        })),
      };
    });
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
    // 20s hard timeout. F21: medic reported "AbortError: Fetch is
    // aborted" with 8s — the projection endpoint was waiting on the
    // SQLite write lock because session_takeaway was holding a conn
    // open for a 10-15s LLM call after each chat turn (F10). Until
    // we move LLM calls off the shared conn (WAL + separate worker
    // pool), give projection a longer ceiling so the medic doesn't
    // see a spurious "load failed" while the in-process post-turn
    // work is finishing.
    const r = await this.fetch<Raw>(
      `/api/v1/memory/patient/${encodeURIComponent(patientHash)}/projection`,
      { signal: AbortSignal.timeout(20000) },
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

  /** Diagnostic snapshot of the ingestion pipeline for one patient.
   *  Lets the medic see WHY 当前发现 is empty without having to grep
   *  server logs. The ``diagnosis`` field is a single-sentence
   *  plain-Chinese summary the UI can render directly. */
  async getIngestDebug(patientHash: string): Promise<{
    ingestionStarted:   number;
    ingestionCompleted: number;
    nodeAddedEvents:    number;
    clinicalGraphNodes: number;
    latestLlmResponse: {
      model?: string;
      promptId?: string;
      latencyMs?: number;
      ts?: number;
      rawOutputHead?: string;
      rawOutputChars?: number;
    };
    latestCompleted: {
      emittedNodeCount?: number;
      errors?: string[];
      ts?: number;
    };
    diagnosis: string;
  }> {
    interface Raw {
      user_id: string;
      patient_hash: string;
      ingestion_started: number;
      ingestion_completed: number;
      node_added_events: number;
      clinical_graph_nodes: number;
      latest_llm_response: any;
      latest_completed: any;
      diagnosis: string;
    }
    const r = await this.fetch<Raw>(
      `/api/v1/memory/patient/${encodeURIComponent(patientHash)}/ingest_debug`,
      { signal: AbortSignal.timeout(5000) },
    );
    return {
      ingestionStarted:   r.ingestion_started,
      ingestionCompleted: r.ingestion_completed,
      nodeAddedEvents:    r.node_added_events,
      clinicalGraphNodes: r.clinical_graph_nodes,
      latestLlmResponse: {
        model:          r.latest_llm_response?.model,
        promptId:       r.latest_llm_response?.prompt_id,
        latencyMs:      r.latest_llm_response?.latency_ms,
        ts:             r.latest_llm_response?.ts,
        rawOutputHead:  r.latest_llm_response?.raw_output_head,
        rawOutputChars: r.latest_llm_response?.raw_output_chars,
      },
      latestCompleted: {
        emittedNodeCount: r.latest_completed?.emitted_node_count,
        errors:           r.latest_completed?.errors,
        ts:               r.latest_completed?.ts,
      },
      diagnosis: r.diagnosis,
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

  /* ──────────────────── Layer 2b · session takeaways ──────────────── */

  /** Per-user qualitative insights distilled by ``session_takeaway``.
   *  Optionally filter by scope (patient / research / cross_research /
   *  other) + scope_ref. Sorted newest first. Excludes rejected. */
  async listTakeaways(opts: {
    scopeKind?: 'patient' | 'research' | 'cross_research' | 'other';
    scopeRef?: string;
    limit?: number;
  } = {}): Promise<Array<{
    id:            number;
    scopeKind:     string;
    scopeRef:      string;
    sessionId:     string;
    text:          string;
    tag:           string | null;
    confidence:    number;
    distilledAt:   number;
    medicAckedAt:  number | null;
  }>> {
    const q = new URLSearchParams();
    if (opts.scopeKind) q.set('scope_kind', opts.scopeKind);
    if (opts.scopeRef) q.set('scope_ref', opts.scopeRef);
    if (opts.limit) q.set('limit', String(opts.limit));
    const qs = q.toString() ? `?${q.toString()}` : '';
    interface Raw {
      takeaways: any[];
      count: number;
    }
    // F-loading-timeouts: 6s hard cap. Without this, if the sidecar
    // wasn't ready when the request fired (boot-race), the fetch
    // hung forever and the drawer stayed on "加载中..." until the
    // medic closed + reopened. Now: 6s → throw → caller renders an
    // error state with a retry button.
    const r = await this.fetch<Raw>(`/api/v1/memory/takeaways${qs}`, {
      signal: AbortSignal.timeout(6000),
    });
    return r.takeaways.map((t) => ({
      id:           t.id,
      scopeKind:    t.scope_kind,
      scopeRef:     t.scope_ref,
      sessionId:    t.session_id,
      text:         t.text,
      tag:          t.tag ?? null,
      confidence:   t.confidence,
      distilledAt:  t.distilled_at,
      medicAckedAt: t.medic_acked_at ?? null,
    }));
  }

  async ackTakeaway(id: number) {
    return this.fetch(
      `/api/v1/memory/takeaways/${id}/ack`,
      { method: 'POST' },
    );
  }

  async rejectTakeaway(id: number) {
    return this.fetch(
      `/api/v1/memory/takeaways/${id}/reject`,
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
      has_kimi_key?: boolean;
      advisory: string | null;
      active_key_source?: string | null;
      active_key_preview?: string | null;
      active_key_length?: number | null;
    }
    try {
      // 5-second hard timeout. If the sidecar isn't running / is
      // still booting / has crashed, ``this.fetch`` would otherwise
      // hang the WHOLE Settings · LLM panel on the "Loading…"
      // spinner forever — the medic can't update their API key,
      // every chat fails, every ingester fails, the Memory tab
      // stays at 0, and the entire app feels broken end-to-end.
      // After 5s we surrender to the Tauri IPC fallback (reads the
      // same .env from disk) so the form always renders.
      const r = await this.fetch<Raw>('/api/v1/settings/llm', {
        signal: AbortSignal.timeout(5000),
      });
      return {
        provider:        r.provider as LlmStatus['provider'],
        model:           r.model,
        envFilePath:     r.env_file_path,
        envFileExists:   r.env_file_exists,
        hasGeminiKey:    r.has_gemini_key,
        hasOpenaiKey:    r.has_openai_key,
        hasAnthropicKey: r.has_anthropic_key,
        hasKimiKey:      r.has_kimi_key ?? false,
        advisory:        r.advisory,
        activeKeySource: (r.active_key_source ?? null) as LlmStatus['activeKeySource'],
        activeKeyPreview: r.active_key_preview ?? '',
        activeKeyLength:  r.active_key_length ?? 0,
      };
    } catch (e) {
      // Backend 404 / 5xx / timeout → try Tauri's direct-from-disk
      // read so the form is always usable. The IPC fallback can't
      // tell us source / preview (it just reads .env), so those
      // fields default to "env" / "" — the medic still gets the
      // form, just without the load-source confirmation badge.
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
          hasKimiKey:      ipc.has_kimi_key ?? false,
          advisory:        ipc.advisory,
          activeKeySource: 'env',
          activeKeyPreview: '',
          activeKeyLength:  0,
        };
      }
      throw e;
    }
  }

  /** Live-test the in-process active provider key — sends a tiny
   *  generation through the server and reports either ✓ + latency or
   *  the verbatim upstream error + a classified diagnosis. THE
   *  canonical answer to "is my saved key actually valid?" — checks
   *  reachability + acceptance, not just presence. */
  async testLlmKey(): Promise<{
    ok: boolean;
    provider: string;
    model: string;
    latencyMs?: number;
    error?: string;
    diagnosis?: 'key_missing' | 'key_invalid' | 'quota_exceeded'
              | 'network' | 'other';
  }> {
    interface Raw {
      ok: boolean;
      provider: string;
      model: string;
      latency_ms?: number | null;
      error?: string | null;
      diagnosis?: string | null;
    }
    const r = await this.fetch<Raw>('/api/v1/settings/llm/test', {
      method: 'POST',
      body: JSON.stringify({}),
    });
    return {
      ok:        r.ok,
      provider:  r.provider,
      model:     r.model,
      latencyMs: r.latency_ms ?? undefined,
      error:     r.error ?? undefined,
      diagnosis: (r.diagnosis ?? undefined) as
        'key_missing' | 'key_invalid' | 'quota_exceeded' | 'network' | 'other' | undefined,
    };
  }

  async putLlmSettings(input: {
    provider?: 'gemini' | 'openai' | 'anthropic' | 'kimi';
    model?: string;
    geminiApiKey?: string;
    openaiApiKey?: string;
    anthropicApiKey?: string;
    kimiApiKey?: string;
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
        has_kimi_key?: boolean;
        advisory: string | null;
        active_key_source?: string | null;
        active_key_preview?: string | null;
        active_key_length?: number | null;
      };
    }
    const body: Record<string, string> = {};
    if (input.provider)        body.provider          = input.provider;
    if (input.model)           body.model             = input.model;
    if (input.geminiApiKey)    body.gemini_api_key    = input.geminiApiKey;
    if (input.openaiApiKey)    body.openai_api_key    = input.openaiApiKey;
    if (input.anthropicApiKey) body.anthropic_api_key = input.anthropicApiKey;
    if (input.kimiApiKey)      body.kimi_api_key      = input.kimiApiKey;
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
          hasKimiKey:      r.status.has_kimi_key ?? false,
          advisory:        r.status.advisory,
          activeKeySource: (r.status.active_key_source ?? null) as LlmStatus['activeKeySource'],
          activeKeyPreview: r.status.active_key_preview ?? '',
          activeKeyLength:  r.status.active_key_length ?? 0,
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
      if (input.kimiApiKey)      updates.KIMI_API_KEY         = input.kimiApiKey;

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
          hasKimiKey:      ipc.status.has_kimi_key ?? false,
          advisory:        ipc.status.advisory,
          activeKeySource: 'env',
          activeKeyPreview: '',
          activeKeyLength:  0,
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

  /* ────────────────────────── report PDF ────────────────────────── */

  /** POST /api/v1/report/pdf — build a clinical report PDF.
   *  Replaces the broken window.print() flow that worked under no
   *  browser inside Tauri's WKWebView. The server renders via reportlab
   *  Platypus and writes to <Archive>/Reports/<hash>-<ts>.pdf, then
   *  returns the path so ReportMode's "Last report" card can show it
   *  and open the containing folder. */
  async exportReportPdf(input: {
    patientHash: string;
    patientLabel: string;
    patientSex: string;
    patientAgeGroup: string;
    latestModality: string;
    latestStudyDt: string;
    clinicalInfo: string;
    impression: string;
    recommendation: string;
    findings: Array<{ nodeId?: number; label: string; urgency?: string }>;
    differentials: Array<{ nodeId?: number; label: string; urgency?: string }>;
    locale: 'zh-CN' | 'en-US';
  }): Promise<{
    path: string;
    bytes: number;
    createdAt: number;
    patientHash: string;
    locale: string;
  }> {
    interface Raw {
      path: string;
      bytes: number;
      created_at: number;
      patient_hash: string;
      locale: string;
    }
    // Server-side schema uses snake_case (NodeRef.node_id /
    // patient_hash) — translate at the boundary to keep the JS-side
    // camelCase consistent everywhere else.
    const body = {
      patient_hash:        input.patientHash,
      patient_label:       input.patientLabel,
      patient_sex:         input.patientSex,
      patient_age_group:   input.patientAgeGroup,
      latest_modality:     input.latestModality,
      latest_study_dt:     input.latestStudyDt,
      clinical_info:       input.clinicalInfo,
      impression:          input.impression,
      recommendation:      input.recommendation,
      findings: input.findings.map((n) => ({
        node_id: n.nodeId ?? null,
        label:   n.label,
        urgency: n.urgency ?? null,
      })),
      differentials: input.differentials.map((n) => ({
        node_id: n.nodeId ?? null,
        label:   n.label,
        urgency: n.urgency ?? null,
      })),
      locale: input.locale,
    };
    const r = await this.fetch<Raw>('/api/v1/report/pdf', {
      method: 'POST',
      body: JSON.stringify(body),
    });
    return {
      path:         r.path,
      bytes:        r.bytes,
      createdAt:    r.created_at,
      patientHash:  r.patient_hash,
      locale:       r.locale,
    };
  }

  /* ────────────────────────── scheduled tasks ────────────────────────── */

  /** POST /api/v1/schedule/confirm — the user confirmed a scheduled
   *  task (proposed by chat heuristic or composed manually from the
   *  UI). Creates the row + emits SCHEDULED_TASK_CREATED. */
  async confirmScheduledTask(input: {
    kind: 'send_email';
    payload: Record<string, unknown>;
    fireAt: number;            // unix sec
    userTz: string;            // IANA zone
    recurrenceCron?: string | null;
    sessionId?: string | null;
    patientHash?: string | null;
    proposalId?: string | null;
  }): Promise<ScheduledTaskView> {
    interface Raw {
      task_id: string;
      user_id: string;
      patient_hash: string | null;
      session_id: string | null;
      kind: string;
      payload: Record<string, unknown>;
      fire_at: number;
      user_tz: string;
      recurrence_cron: string | null;
      status: string;
      last_run_at: number | null;
      last_error: string | null;
      result: Record<string, unknown> | null;
      created_at: number;
      updated_at: number;
      cancelled_at: number | null;
    }
    const r = await this.fetch<Raw>('/api/v1/schedule/confirm', {
      method: 'POST',
      body: JSON.stringify({
        kind:            input.kind,
        payload:         input.payload,
        fire_at:         input.fireAt,
        user_tz:         input.userTz,
        recurrence_cron: input.recurrenceCron ?? null,
        session_id:      input.sessionId      ?? null,
        patient_hash:    input.patientHash    ?? null,
        proposal_id:     input.proposalId     ?? null,
      }),
    });
    return rawScheduledTaskToView(r);
  }

  /** GET /api/v1/schedule/list — list this user's scheduled tasks.
   *  status: optional filter ('pending' | 'done' | 'error' | 'cancelled'). */
  async listScheduledTasks(
    status?: 'pending' | 'done' | 'error' | 'cancelled' | 'running',
    limit = 100,
  ): Promise<ScheduledTaskView[]> {
    interface Raw {
      tasks: Array<{
        task_id: string;
        user_id: string;
        patient_hash: string | null;
        session_id: string | null;
        kind: string;
        payload: Record<string, unknown>;
        fire_at: number;
        user_tz: string;
        recurrence_cron: string | null;
        status: string;
        last_run_at: number | null;
        last_error: string | null;
        result: Record<string, unknown> | null;
        created_at: number;
        updated_at: number;
        cancelled_at: number | null;
      }>;
    }
    const q = status
      ? `?status_filter=${encodeURIComponent(status)}&limit=${limit}`
      : `?limit=${limit}`;
    const r = await this.fetch<Raw>(`/api/v1/schedule/list${q}`);
    return r.tasks.map(rawScheduledTaskToView);
  }

  /** DELETE /api/v1/schedule/{task_id} — soft cancel. Idempotent. */
  async cancelScheduledTask(taskId: string): Promise<ScheduledTaskView> {
    const r = await this.fetch<{
      task_id: string; user_id: string;
      patient_hash: string | null; session_id: string | null;
      kind: string; payload: Record<string, unknown>;
      fire_at: number; user_tz: string;
      recurrence_cron: string | null; status: string;
      last_run_at: number | null; last_error: string | null;
      result: Record<string, unknown> | null;
      created_at: number; updated_at: number; cancelled_at: number | null;
    }>(`/api/v1/schedule/${encodeURIComponent(taskId)}`, { method: 'DELETE' });
    return rawScheduledTaskToView(r);
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

  /* ────────────────────── skills / plugins ─────────────────────── */

  /** GET /api/v1/skills — every skill installed for this user. */
  async listSkills(): Promise<Skill[]> {
    interface RawSkill {
      name: string; description: string; source: string;
      enabled: boolean; installed_at: string; invocable: boolean;
    }
    const r = await this.fetch<{ skills: RawSkill[] }>('/api/v1/skills');
    return (r.skills ?? []).map((s) => ({
      name:        s.name,
      description: s.description,
      source:      s.source,
      enabled:     s.enabled,
      installedAt: s.installed_at,
      invocable:   s.invocable,
    }));
  }

  /** GET /api/v1/skills/search — discover installable skills.
   *  Throws ApiError with ``code === 'search_unavailable'`` (502) when
   *  the upstream registry / GitHub is unreachable. */
  async searchSkills(
    q: string,
    source?: 'official' | 'github',
  ): Promise<SkillSearchResult[]> {
    interface RawResult {
      identifier: string; name: string; description: string;
      source: string; installed: boolean;
    }
    const qs = new URLSearchParams({ q });
    if (source) qs.set('source', source);
    const r = await this.fetch<{ results: RawResult[] }>(
      `/api/v1/skills/search?${qs.toString()}`,
    );
    return (r.results ?? []).map((x) => ({
      identifier:  x.identifier,
      name:        x.name,
      description: x.description,
      source:      x.source,
      installed:   x.installed,
    }));
  }

  /** POST /api/v1/skills/install — 409 ``already_installed`` when the
   *  skill is already present (callers may treat that as success). */
  async installSkill(
    identifier: string,
  ): Promise<{ ok: boolean; skill: { name: string; description: string } }> {
    return this.fetch<{ ok: boolean; skill: { name: string; description: string } }>(
      '/api/v1/skills/install',
      { method: 'POST', body: JSON.stringify({ identifier }) },
    );
  }

  /** DELETE /api/v1/skills/{name}. */
  async uninstallSkill(name: string): Promise<void> {
    await this.fetch<{ ok: boolean }>(
      `/api/v1/skills/${encodeURIComponent(name)}`,
      { method: 'DELETE' },
    );
  }

  /** POST /api/v1/skills/{name}/toggle → the effective enabled state. */
  async toggleSkill(name: string, enabled: boolean): Promise<boolean> {
    const r = await this.fetch<{ ok: boolean; enabled: boolean }>(
      `/api/v1/skills/${encodeURIComponent(name)}/toggle`,
      { method: 'POST', body: JSON.stringify({ enabled }) },
    );
    return r.enabled;
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
    /**
     * Research Workspace scope (optional). When provided, the backend
     * resolves the cohort from study_enrollments + screening_evaluations
     * and reshapes the system prompt to be cohort-aware + load external
     * knowledge tools. See chat_router_v2.ChatScope.
     */
    scope?: {
      kind: 'patient' | 'research' | 'cross_patient';
      studyId?: string | null;
      focusPatientHash?: string | null;
    },
    /**
     * F-tab-switch-race — caller-provided abort signal. When the
     * medic switches tabs mid-stream, the chat component's unmount
     * cleanup aborts the controller; this frees WebKit's
     * limited-per-origin connection slot (6 concurrent fetches max)
     * so the next ``api.health()`` probe on remount doesn't get
     * stuck behind the abandoned SSE and falsely show "Backend
     * unreachable".
     */
    abortSignal?: AbortSignal,
    /**
     * F-skills — per-turn request options. ``skills`` carries the names
     * the medic picked from the composer's "/" menu; the v2 chat router
     * loads those skills for this turn (unknown fields are dropped
     * safely by older servers).
     */
    opts?: { skills?: string[] },
  ): AsyncIterable<ChatStreamChunk> {
    const r = await fetch(`${baseUrl}/api/v1/agent/chat`, {
      method: 'POST',
      headers: this.headers({ 'Content-Type': 'application/json' }),
      body: JSON.stringify({
        text, session_id: sessionId, patient_hash: patientHash,
        attachments,
        scope: scope ? {
          kind: scope.kind,
          study_id: scope.studyId ?? null,
          focus_patient_hash: scope.focusPatientHash ?? null,
        } : undefined,
        skills: opts?.skills && opts.skills.length > 0
          ? opts.skills
          : undefined,
      }),
      signal: abortSignal,
    });
    if (!r.ok || !r.body) {
      throw new ApiError(r.status, await r.text().catch(() => r.statusText),
                         '/api/v1/agent/chat');
    }

    const reader = r.body.getReader();
    const dec = new TextDecoder();
    let buf = '';
    // Hook abort → cancel the reader so the `for await` loop terminates
    // cleanly. Without this, ``signal.abort()`` only aborts the request
    // metadata; the body stream keeps draining in the background until
    // the server closes it (could be minutes).
    if (abortSignal) {
      abortSignal.addEventListener('abort', () => {
        try { reader.cancel(); } catch { /* already cancelled */ }
      }, { once: true });
    }
    try {
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
    } finally {
      // Defensive: if we exit the loop normally (done=true) OR via a
      // for-await break OR via an aborted reader.cancel(), make sure
      // the underlying fetch reader is fully released. WebKit on
      // macOS keeps holding the connection slot otherwise.
      try { reader.releaseLock(); } catch { /* ok */ }
    }
  }

  // ───────────────────────────────────────────────────────────────
  // Writing Studio — /api/v1/docs (P1). See components/writing-studio.tsx.
  // ───────────────────────────────────────────────────────────────

  /** GET /docs — the medic's writing documents, for the left rail. */
  async listWritingDocs(): Promise<WritingDocMeta[]> {
    interface RawDoc {
      id: string; title: string; updated_at: string; ref_count: number;
    }
    const r = await this.fetch<{ docs: RawDoc[] }>('/api/v1/docs');
    return (r.docs ?? []).map((d) => ({
      id:        d.id,
      title:     d.title,
      updatedAt: d.updated_at,
      refCount:  d.ref_count ?? 0,
    }));
  }

  /** POST /docs — create an empty document. */
  async createWritingDoc(title: string): Promise<{ id: string }> {
    const r = await this.fetch<{ id: string }>('/api/v1/docs', {
      method: 'POST',
      body: JSON.stringify({ title }),
    });
    return { id: r.id };
  }

  /** GET /docs/{id} — full body + reference chips. */
  async getWritingDoc(id: string): Promise<WritingDoc> {
    interface RawRef {
      ref_id: string; ref_type: string; target_id: string;
      granularity: string; chip_label: string;
      snapshot_preview: string; created_at: string;
    }
    interface Raw {
      id: string; title: string; body: string; references: RawRef[];
    }
    const r = await this.fetch<Raw>(
      `/api/v1/docs/${encodeURIComponent(id)}`,
    );
    return {
      id:    r.id,
      title: r.title,
      body:  r.body,
      references: (r.references ?? []).map((x) => ({
        refId:           x.ref_id,
        refType:         x.ref_type as WritingReference['refType'],
        targetId:        x.target_id,
        granularity:     x.granularity as WritingReference['granularity'],
        chipLabel:       x.chip_label,
        snapshotPreview: x.snapshot_preview,
        createdAt:       x.created_at,
      })),
    };
  }

  /** PUT /docs/{id} — autosave (title and/or body). */
  async updateWritingDoc(
    id: string,
    patch: { title?: string; body?: string },
  ): Promise<void> {
    const body: Record<string, string> = {};
    if (patch.title !== undefined) body.title = patch.title;
    if (patch.body  !== undefined) body.body  = patch.body;
    await this.fetch<{ ok: boolean }>(
      `/api/v1/docs/${encodeURIComponent(id)}`,
      { method: 'PUT', body: JSON.stringify(body) },
    );
  }

  /** DELETE /docs/{id}. */
  async deleteWritingDoc(id: string): Promise<void> {
    await this.fetch<unknown>(
      `/api/v1/docs/${encodeURIComponent(id)}`,
      { method: 'DELETE' },
    );
  }

  /** GET /docs/{id}/snapshots — version history for the 引用与快照
   *  drawer. */
  async listWritingSnapshots(id: string): Promise<WritingSnapshot[]> {
    interface RawSnap { id: string; label: string; created_at: string }
    const r = await this.fetch<{ snapshots: RawSnap[] }>(
      `/api/v1/docs/${encodeURIComponent(id)}/snapshots`,
    );
    return (r.snapshots ?? []).map((s) => ({
      id: s.id, label: s.label, createdAt: s.created_at,
    }));
  }

  /** POST /docs/{id}/snapshots/{sid}/restore → the restored body. */
  async restoreWritingSnapshot(id: string, snapshotId: string): Promise<string> {
    const r = await this.fetch<{ ok: boolean; body: string }>(
      `/api/v1/docs/${encodeURIComponent(id)}/snapshots/` +
      `${encodeURIComponent(snapshotId)}/restore`,
      { method: 'POST', body: JSON.stringify({}) },
    );
    return r.body;
  }

  /** POST /docs/{id}/references — mint a de-identified reference chip.
   *  The server snapshots the target at the chosen granularity and
   *  returns the chip label + preview text. */
  async createWritingReference(
    id: string,
    input: {
      refType: 'patient' | 'study';
      targetId: string;
      granularity: WritingRefGranularity;
    },
  ): Promise<{ refId: string; chipLabel: string; snapshotPreview: string }> {
    interface Raw {
      ref_id: string; chip_label: string; snapshot_preview: string;
    }
    const r = await this.fetch<Raw>(
      `/api/v1/docs/${encodeURIComponent(id)}/references`,
      {
        method: 'POST',
        body: JSON.stringify({
          ref_type:    input.refType,
          target_id:   input.targetId,
          granularity: input.granularity,
        }),
      },
    );
    return {
      refId:           r.ref_id,
      chipLabel:       r.chip_label,
      snapshotPreview: r.snapshot_preview,
    };
  }

  /**
   * POST /docs/{id}/polish — stream the revised selection. Same SSE
   * frame parsing as ``sendChat`` above (``data: {...}\n\n``).
   */
  async *polishWritingDoc(
    id: string,
    input: { selection: string; instruction: string; refIds: string[] },
    abortSignal?: AbortSignal,
  ): AsyncIterable<WritingPolishFrame> {
    const path = `/api/v1/docs/${encodeURIComponent(id)}/polish`;
    const r = await fetch(`${baseUrl}${path}`, {
      method: 'POST',
      headers: this.headers({ 'Content-Type': 'application/json' }),
      body: JSON.stringify({
        selection:   input.selection,
        instruction: input.instruction,
        ref_ids:     input.refIds,
      }),
      signal: abortSignal,
    });
    if (!r.ok || !r.body) {
      throw new ApiError(r.status, await r.text().catch(() => r.statusText), path);
    }
    const reader = r.body.getReader();
    const dec = new TextDecoder();
    let buf = '';
    if (abortSignal) {
      abortSignal.addEventListener('abort', () => {
        try { reader.cancel(); } catch { /* already cancelled */ }
      }, { once: true });
    }
    try {
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buf += dec.decode(value, { stream: true });
        let idx: number;
        while ((idx = buf.indexOf('\n\n')) !== -1) {
          const raw = buf.slice(0, idx);
          buf = buf.slice(idx + 2);
          for (const line of raw.split('\n')) {
            if (line.startsWith('data: ')) {
              try {
                yield JSON.parse(line.slice(6)) as WritingPolishFrame;
              } catch { /* malformed payload; skip */ }
            }
          }
        }
      }
    } finally {
      try { reader.releaseLock(); } catch { /* ok */ }
    }
  }

  /** POST /docs/{id}/phi-scan — rule+model PHI findings for the gate. */
  async phiScanWritingDoc(id: string): Promise<WritingPhiFinding[]> {
    interface RawFinding {
      kind: string; excerpt: string; start: number; end: number;
      suggestion: string;
    }
    const r = await this.fetch<{ findings: RawFinding[] }>(
      `/api/v1/docs/${encodeURIComponent(id)}/phi-scan`,
      { method: 'POST', body: JSON.stringify({}) },
    );
    return (r.findings ?? []).map((f) => ({
      kind: f.kind, excerpt: f.excerpt, start: f.start, end: f.end,
      suggestion: f.suggestion,
    }));
  }

  /**
   * POST /docs/{id}/export → the .docx bytes as a Blob. Throws
   * ``ApiError`` with ``status===422 && code==='phi_unresolved'`` when
   * the server refuses because PHI findings are unresolved — the
   * caller then runs phiScanWritingDoc + reopens the gate modal.
   */
  async exportWritingDocx(
    id: string,
    input: { resolutions: WritingPhiResolution[]; includeSources: boolean },
  ): Promise<Blob> {
    const path = `/api/v1/docs/${encodeURIComponent(id)}/export`;
    const r = await fetch(`${baseUrl}${path}`, {
      method: 'POST',
      headers: this.headers({ 'Content-Type': 'application/json' }),
      body: JSON.stringify({
        resolutions: input.resolutions.map((x) => ({
          start:  x.start,
          end:    x.end,
          action: x.action,
          ...(x.replacement !== undefined ? { replacement: x.replacement } : {}),
        })),
        include_sources: input.includeSources,
      }),
    });
    if (!r.ok) {
      throw new ApiError(r.status, await r.text().catch(() => r.statusText), path);
    }
    return r.blob();
  }

  /** GET /docs/{id}/chat — chronological co-writing transcript. */
  async getWritingChat(id: string): Promise<WritingChatMessage[]> {
    interface RawMsg {
      id: string; role: string; text: string;
      doc_applied: number; created_at: string;
    }
    const r = await this.fetch<{ messages: RawMsg[] }>(
      `/api/v1/docs/${encodeURIComponent(id)}/chat`,
    );
    return (r.messages ?? []).map((m) => ({
      id:         m.id,
      role:       m.role === 'assistant' ? 'assistant' : 'user',
      text:       m.text,
      docApplied: !!m.doc_applied,
      createdAt:  m.created_at,
    }));
  }

  /**
   * POST /docs/{id}/chat — one co-writing turn. Streams SSE frames
   * (``data: {...}\n\n`` — same parser as ``polishWritingDoc``):
   * reply_chunk* → [doc_started → doc_chunk*] → [provenance_warning]
   * → done — or a terminal error frame (HTTP stays 200; the user
   * message is persisted server-side either way). A non-null
   * ``doc_body`` on done means the server ALREADY applied it to the
   * doc and snapshotted the previous body (``snapshot_id`` = that
   * pre-revision snapshot — restoring it undoes the revision).
   */
  async *chatWritingDoc(
    id: string,
    input: { message: string; refIds?: string[] },
    abortSignal?: AbortSignal,
  ): AsyncIterable<WritingChatFrame> {
    const path = `/api/v1/docs/${encodeURIComponent(id)}/chat`;
    const r = await fetch(`${baseUrl}${path}`, {
      method: 'POST',
      headers: this.headers({ 'Content-Type': 'application/json' }),
      body: JSON.stringify({
        message: input.message,
        ...(input.refIds !== undefined ? { ref_ids: input.refIds } : {}),
      }),
      signal: abortSignal,
    });
    if (!r.ok || !r.body) {
      throw new ApiError(r.status, await r.text().catch(() => r.statusText), path);
    }
    const reader = r.body.getReader();
    const dec = new TextDecoder();
    let buf = '';
    if (abortSignal) {
      abortSignal.addEventListener('abort', () => {
        try { reader.cancel(); } catch { /* already cancelled */ }
      }, { once: true });
    }
    try {
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buf += dec.decode(value, { stream: true });
        let idx: number;
        while ((idx = buf.indexOf('\n\n')) !== -1) {
          const raw = buf.slice(0, idx);
          buf = buf.slice(idx + 2);
          for (const line of raw.split('\n')) {
            if (line.startsWith('data: ')) {
              try {
                yield JSON.parse(line.slice(6)) as WritingChatFrame;
              } catch { /* malformed payload; skip */ }
            }
          }
        }
      }
    } finally {
      try { reader.releaseLock(); } catch { /* ok */ }
    }
  }

  // ───────────────────────────────────────────────────────────────
  // Research Workspace — /api/v1/research/* (design §6)
  // ───────────────────────────────────────────────────────────────

  async listStudies(includeArchived = false) {
    interface Raw {
      study_id: string;
      display_name: string;
      short_code: string;
      phase: string;
      status: string;
      target_n: number | null;
      enrolled_count: number;
      candidate_count: number;
      created_at: number;
      updated_at: number;
    }
    const qs = includeArchived ? '?include_archived=true' : '';
    const raw = await this.fetch<Raw[]>(`/api/v1/research/studies${qs}`);
    return raw.map((r) => ({
      studyId: r.study_id,
      displayName: r.display_name,
      shortCode: r.short_code,
      phase: r.phase,
      status: r.status,
      targetN: r.target_n,
      enrolledCount: r.enrolled_count,
      candidateCount: r.candidate_count,
      createdAt: r.created_at,
      updatedAt: r.updated_at,
    }));
  }

  async createStudy(body: {
    displayName: string;
    shortCode: string;
    phase?: string;
    targetN?: number | null;
    primaryEndpoint?: string;
    secondaryEndpoints?: string[];
    inclusion?: Array<{ id: string; text: string; kind: string;
                       rule_dsl?: string; llm_prompt?: string }>;
    exclusion?: Array<{ id: string; text: string; kind: string;
                       rule_dsl?: string; llm_prompt?: string }>;
    schedule?: Array<{ label: string; offset_days: number;
                       assessments: string[] }>;
  }) {
    return this.fetch<unknown>('/api/v1/research/studies', {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({
        display_name: body.displayName,
        short_code:   body.shortCode,
        phase:        body.phase ?? '',
        target_n:     body.targetN ?? null,
        primary_endpoint: body.primaryEndpoint,
        secondary_endpoints: body.secondaryEndpoints ?? [],
        inclusion: body.inclusion ?? [],
        exclusion: body.exclusion ?? [],
        schedule:  body.schedule ?? [],
      }),
    });
  }

  async getResearchStudy(studyId: string) {
    return this.fetch<unknown>(
      `/api/v1/research/studies/${encodeURIComponent(studyId)}`,
    );
  }

  /**
   * Parse a previously-uploaded .docx protocol and return a draft
   * { inclusion, exclusion, schedule, protocol_summary, notes } for the
   * "New Study → Import .docx" review step.
   *
   * The upstream file must already be in /files (upload via uploadFile);
   * pass the returned file_id as `uploadFileId`.
   *
   * Backend: POST /api/v1/research/studies/{study_id}/protocol/import.
   * Goes through `_ApiClient.fetch` so it picks up baseUrl + bearer +
   * silent re-auth like every other call. (Before this method existed,
   * the only call site used a relative `fetch('/api/v1/...')` which the
   * Tauri bundle resolved to `tauri://localhost/...` and WebKit threw
   * `The string did not match the expected pattern` on parse — never
   * reaching the sidecar.)
   */
  async importStudyProtocol(
    studyId: string,
    uploadFileId: string,
  ): Promise<{
    draft: {
      inclusion?: Array<Record<string, unknown>>;
      exclusion?: Array<Record<string, unknown>>;
      schedule?:  Array<Record<string, unknown>>;
      protocol_summary?: string;
      notes?: string[];
    };
  }> {
    return this.fetch(
      `/api/v1/research/studies/${encodeURIComponent(studyId)}/protocol/import`,
      {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ upload_file_id: uploadFileId }),
      },
    );
  }

  async patchResearchStudy(studyId: string, body: Record<string, unknown>) {
    return this.fetch<unknown>(
      `/api/v1/research/studies/${encodeURIComponent(studyId)}`,
      {
        method: 'PATCH',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify(body),
      },
    );
  }

  // ─── Safety / observations ──────────────────────────────────────
  // The Research Workspace "安全性" tab needs three reads and three
  // writes against the per-study observation stream:
  //   GET    /observations            — full list (filterable)
  //   GET    /safety/stop-rule-status — aggregated DLT counter + cap
  //   POST   /observations            — manual entry (medic typing in
  //                                      an AE candidate; the SOAP →
  //                                      AE auto-extractor is Phase 2)
  //   POST   /observations/{id}/confirm — medic locks AE grade / DLT flag
  //   POST   /observations/{id}/unlink — medic marks a row as 误判
  // Schemas mirror research_router.py:ObservationRow.

  async listStudyObservations(
    studyId: string,
    opts?: { patientHash?: string; category?: string },
  ): Promise<Array<{
    observation_id: string;
    study_id: string;
    patient_hash: string;
    created_at: number;
    category: string;
    ae_grade: string | null;
    ae_grade_confirmed: boolean;
    is_dlt: boolean | null;
    source_kind: string;
    source_node_id: string | null;
    source_text_excerpt: string | null;
    linked_assessment_visit_id: string | null;
    medic_confirmed_at: number | null;
    unlinked_at: number | null;
    unlink_reason: string | null;
  }>> {
    const qs = new URLSearchParams();
    if (opts?.patientHash) qs.set('patient_hash', opts.patientHash);
    if (opts?.category)    qs.set('category',     opts.category);
    const suffix = qs.toString() ? `?${qs.toString()}` : '';
    return this.fetch(
      `/api/v1/research/studies/${encodeURIComponent(studyId)}/observations${suffix}`,
    );
  }

  async recordStudyObservation(
    studyId: string,
    body: {
      patientHash: string;
      category: string;
      aeGrade?: string;
      isDlt?: boolean;
      sourceTextExcerpt?: string;
      linkedAssessmentVisitId?: string;
    },
  ): Promise<{ observation_id: string }> {
    return this.fetch(
      `/api/v1/research/studies/${encodeURIComponent(studyId)}/observations`,
      {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({
          patient_hash:               body.patientHash,
          category:                   body.category,
          ae_grade:                   body.aeGrade,
          is_dlt:                     body.isDlt,
          source_text_excerpt:        body.sourceTextExcerpt,
          linked_assessment_visit_id: body.linkedAssessmentVisitId,
        }),
      },
    );
  }

  async confirmStudyObservation(
    studyId: string, obsId: string,
    body: { aeGrade?: string; isDlt?: boolean; notes?: string },
  ): Promise<{ status: string }> {
    return this.fetch(
      `/api/v1/research/studies/${encodeURIComponent(studyId)}/observations/${encodeURIComponent(obsId)}/confirm`,
      {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({
          ae_grade: body.aeGrade,
          is_dlt:   body.isDlt,
          notes:    body.notes,
        }),
      },
    );
  }

  async unlinkStudyObservation(
    studyId: string, obsId: string, reason = '',
  ): Promise<{ status: string }> {
    return this.fetch(
      `/api/v1/research/studies/${encodeURIComponent(studyId)}/observations/${encodeURIComponent(obsId)}/unlink`,
      {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ reason }),
      },
    );
  }

  async getStopRuleStatus(studyId: string): Promise<{
    dlt_observed: number;
    dlt_cap: number | null;
    run_in_n: number | null;
    triggered: boolean;
    note: string;
  }> {
    return this.fetch(
      `/api/v1/research/studies/${encodeURIComponent(studyId)}/safety/stop-rule-status`,
    );
  }

  /**
   * Install a starter pack of canonical research protocols (3 real-world
   * Chinese oncology trials shipped with the server, see
   * ``packages/server/nexus_server/research/starter_protocols.py``).
   * Used by the empty-state CTA so the medic has *something* to look
   * at without having to import a .docx first.
   *
   * Pass ``starterIds = null`` to install all; pass an explicit list to
   * pick a subset. ``overwrite=false`` is idempotent — re-clicking the
   * button after installation is a no-op.
   */
  async installResearchStarters(
    starterIds: string[] | null = null,
    overwrite = false,
  ): Promise<{ installed: string[]; count: number }> {
    return this.fetch(
      `/api/v1/research/starters/install`,
      {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ starter_ids: starterIds, overwrite }),
      },
    );
  }

  async archiveResearchStudy(studyId: string) {
    return this.fetch<unknown>(
      `/api/v1/research/studies/${encodeURIComponent(studyId)}`,
      { method: 'DELETE' },
    );
  }

  async getRoster(studyId: string, includeWithdrawn = false) {
    const qs = includeWithdrawn ? '?include_withdrawn=true' : '';
    return this.fetch<unknown[]>(
      `/api/v1/research/studies/${encodeURIComponent(studyId)}/roster${qs}`,
    );
  }

  async enrollPatient(
    studyId: string,
    body: { patientHash: string; arm?: string; consentSignedAt?: number;
            notes?: string },
  ) {
    return this.fetch<unknown>(
      `/api/v1/research/studies/${encodeURIComponent(studyId)}/enrollments`,
      {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({
          patient_hash: body.patientHash,
          arm: body.arm,
          consent_signed_at: body.consentSignedAt,
          notes: body.notes,
        }),
      },
    );
  }

  async withdrawPatient(studyId: string, patientHash: string, reason = '') {
    return this.fetch<unknown>(
      `/api/v1/research/studies/${encodeURIComponent(studyId)}` +
      `/enrollments/${encodeURIComponent(patientHash)}`,
      {
        method: 'DELETE',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ reason }),
      },
    );
  }

  async listCandidates(studyId: string, decision?: string) {
    const qs = decision ? `?decision=${encodeURIComponent(decision)}` : '';
    return this.fetch<unknown[]>(
      `/api/v1/research/studies/${encodeURIComponent(studyId)}/eligibility${qs}`,
    );
  }

  async rescanEligibility(studyId: string) {
    return this.fetch<{ status: string; patients_evaluated?: number }>(
      `/api/v1/research/studies/${encodeURIComponent(studyId)}/eligibility/rescan`,
      { method: 'POST' },
    );
  }

  async decideScreening(
    studyId: string, patientHash: string,
    body: { decision: 'invited' | 'enrolled' | 'excluded' | 'snoozed' | 'pending';
            reason?: string; snoozeUntil?: number },
  ) {
    return this.fetch<unknown>(
      `/api/v1/research/studies/${encodeURIComponent(studyId)}` +
      `/screenings/${encodeURIComponent(patientHash)}/decision`,
      {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({
          decision: body.decision,
          reason: body.reason,
          snooze_until: body.snoozeUntil,
        }),
      },
    );
  }

  async getPatientStudies(patientHash: string) {
    interface Raw {
      study_id: string;
      study_short_code: string;
      study_display_name: string;
      status: string;
      enrollment_seq: number | null;
      arm: string | null;
      enrolled_at: number | null;
      withdrawn_at: number | null;
      withdrawal_reason: string | null;
      consent_signed_at: number | null;
    }
    const raw = await this.fetch<Raw[]>(
      `/api/v1/patients/${encodeURIComponent(patientHash)}/studies`,
    );
    return raw.map((r) => ({
      studyId: r.study_id,
      studyShortCode: r.study_short_code,
      studyDisplayName: r.study_display_name,
      status: r.status,
      enrollmentSeq: r.enrollment_seq,
      arm: r.arm,
      enrolledAt: r.enrolled_at,
      withdrawnAt: r.withdrawn_at,
      withdrawalReason: r.withdrawal_reason,
      consentSignedAt: r.consent_signed_at,
    }));
  }

  async getResearchStudyOverview(studyId: string) {
    return this.fetch<{
      study_id: string;
      enrolled_count: number;
      target_n: number | null;
      candidate_count: number;
      attention_count: number;
      median_followup_months: number;
      status: string;
      primary_endpoint: string | null;
    }>(`/api/v1/research/studies/${encodeURIComponent(studyId)}/overview`);
  }

  async getResearchScheduleGantt(studyId: string) {
    return this.fetch<{
      timepoints: Array<{label: string; offset_days: number; visit_id: string}>;
      rows: Array<{
        patient_hash: string;
        enrollment_seq: number;
        enrollment_status: string;
        enrolled_at: number;
        cells: Array<{
          timepoint: string;
          status: 'planned' | 'in_progress' | 'completed' | 'missed' | 'overdue' | 'future';
          kinds: string[];
          due_at?: number;
          completed_at?: number;
        }>;
      }>;
    }>(`/api/v1/research/studies/${encodeURIComponent(studyId)}/schedule/gantt`);
  }

  async getResearchRecentActivity(studyId: string, days = 7, limit = 30) {
    return this.fetch<Array<{
      when_ms: number;
      kind: string;
      text: string;
      patient_hash: string;
    }>>(
      `/api/v1/research/studies/${encodeURIComponent(studyId)}/recent-activity` +
      `?days=${days}&limit=${limit}`,
    );
  }

  async generateInterimReport(studyId: string) {
    return this.fetch<{ status: string; file_id?: string }>(
      `/api/v1/research/studies/${encodeURIComponent(studyId)}/reports/interim`,
      { method: 'POST' },
    );
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

// ─────────────────────────────────────────────────────────────────────
// F26.2 — Identity types for the picker UI
// ─────────────────────────────────────────────────────────────────────

export interface Identity {
  userId:       string;
  displayName:  string;
  avatarEmoji:  string;
  createdAt:    string;
  lastActiveAt: string | null;
}

interface IdentityRaw {
  user_id:        string;
  display_name:   string;
  avatar_emoji:   string;
  created_at:     string;
  last_active_at: string | null;
}

function _castIdentity(r: IdentityRaw): Identity {
  return {
    userId:       r.user_id,
    displayName:  r.display_name,
    avatarEmoji:  r.avatar_emoji,
    createdAt:    r.created_at,
    lastActiveAt: r.last_active_at ?? null,
  };
}


/** Parse the server's error envelope
 *  ``{"error": {"code": "...", "message": "..."}, "status_code": N}``.
 *  Non-envelope bodies (plain-text 500s, proxy pages, FastAPI 422
 *  ``{"detail": ...}``) degrade gracefully to code='' + raw body. */
function parseErrorEnvelope(body: string): { code: string; message: string } {
  try {
    const parsed = JSON.parse(body) as {
      error?: { code?: unknown; message?: unknown };
      detail?: unknown;
    };
    if (parsed && typeof parsed === 'object') {
      if (parsed.error && typeof parsed.error === 'object') {
        return {
          code:    String(parsed.error.code ?? ''),
          message: String(parsed.error.message ?? body),
        };
      }
      // FastAPI validation errors (422) come as {"detail": [...]}.
      if (parsed.detail !== undefined) {
        return {
          code: 'validation_error',
          message: typeof parsed.detail === 'string'
            ? parsed.detail
            : JSON.stringify(parsed.detail),
        };
      }
    }
  } catch { /* not JSON */ }
  return { code: '', message: body };
}

export class ApiError extends Error {
  /** HTTP status. */
  public readonly status: number;
  /** Request path (for logs). */
  public readonly path: string;
  /** Machine-routable error code from the envelope — e.g.
   *  'invalid_credentials', 'claim_required', 'username_taken',
   *  'account_disabled', 'rate_limited', 'user_not_found',
   *  'already_claimed', 'cannot_disable_self', 'admin_required'.
   *  '' when the body wasn't envelope-shaped. */
  public readonly code: string;
  /** Human-readable message from the envelope (falls back to raw body). */
  public readonly serverMessage: string;

  constructor(status: number, body: string, path: string) {
    const env = parseErrorEnvelope(body);
    super(`API ${status} on ${path}: ${env.message || body}`);
    this.status = status;
    this.path = path;
    this.code = env.code;
    this.serverMessage = env.message;
  }
}

// ─────────────────────────────────────────────────────────────────────
// Auth types (2026-07 username+password rework)
// ─────────────────────────────────────────────────────────────────────

export type UserRole = 'admin' | 'user';

function _castRole(r: unknown): UserRole {
  return r === 'admin' ? 'admin' : 'user';
}

/** Normalised result of register / login / claim. */
export interface AuthSession {
  token:            string;
  userId:           string;
  role:             UserRole;
  displayName:      string;
  expiresInSeconds: number;
}

/** One row of GET /api/v1/admin/users. Timestamps come back as ISO
 *  strings (or epoch numbers on older builds); null = never/active. */
export interface AdminUser {
  userId:      string;
  username:    string;
  role:        UserRole;
  createdAt:   string | number | null;
  disabledAt:  string | number | null;
  lastLoginAt: string | number | null;
  hasPassword: boolean;
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

/** One attachment metadata blob carried on a persisted ChatMessageRow.
 *  Mirrors the server's ``AttachmentInfo`` (agent_state.py:74). The
 *  desktop renders these as chips alongside the message bubble; the
 *  bytes themselves stay on the server. */
export interface ChatAttachmentInfo {
  name: string;
  mime: string;
  sizeBytes: number;
}

/** One persisted chat row as returned by ``listSessionMessages``.
 *  Includes attachments so the UI can re-render the pasted-file chips
 *  when the medic reopens an old session. */
export interface ChatMessageRow {
  eventIdx: number;
  role: 'user' | 'agent' | 'system';
  text: string;
  /** Unix seconds. Parsed from server's ISO-8601 timestamp; 0 on parse
   *  failure (single-row defensive default so one bad ts doesn't black-
   *  hole the whole history pane). */
  ts: number;
  attachments: ChatAttachmentInfo[];
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

/** Scheduled task — projection row shape the UI consumes. Mirrors
 *  ``scheduler.ScheduledTask.to_dict`` on the server. snake_case →
 *  camelCase at the boundary so everything inside the UI stays JS-ish. */
export interface ScheduledTaskView {
  taskId:         string;
  userId:         string;
  patientHash:    string | null;
  sessionId:      string | null;
  kind:           'send_email';
  payload:        Record<string, unknown>;
  fireAt:         number;       // unix seconds (UTC)
  userTz:         string;       // IANA zone (display)
  recurrenceCron: string | null;
  status:         'pending' | 'running' | 'done' | 'error' | 'cancelled';
  lastRunAt:      number | null;
  lastError:      string | null;
  result:         Record<string, unknown> | null;
  createdAt:      number;
  updatedAt:      number;
  cancelledAt:    number | null;
}

/** SSE event the chat pane receives mid-turn when the heuristic
 *  extractor detected a future-intent. UI renders an inline
 *  confirmation card; medic clicks Confirm → POST /schedule/confirm. */
export interface ScheduleProposalView {
  proposalId:     string;
  kind:           'send_email';
  fireAt:         number;
  userTz:         string;
  summary:        string;       // human one-liner; safe to display
  payload:        Record<string, unknown>;
  recurrenceCron: string | null;
  sessionId:      string | null;
  patientHash:    string | null;
  needsUserInput: string[];     // e.g. ['to','subject','body']
}

function rawScheduledTaskToView(r: {
  task_id: string; user_id: string;
  patient_hash: string | null; session_id: string | null;
  kind: string; payload: Record<string, unknown>;
  fire_at: number; user_tz: string;
  recurrence_cron: string | null;
  status: string;
  last_run_at: number | null;
  last_error: string | null;
  result: Record<string, unknown> | null;
  created_at: number; updated_at: number;
  cancelled_at: number | null;
}): ScheduledTaskView {
  return {
    taskId:         r.task_id,
    userId:         r.user_id,
    patientHash:    r.patient_hash,
    sessionId:      r.session_id,
    kind:           r.kind as 'send_email',
    payload:        r.payload,
    fireAt:         r.fire_at,
    userTz:         r.user_tz,
    recurrenceCron: r.recurrence_cron,
    status:         r.status as ScheduledTaskView['status'],
    lastRunAt:      r.last_run_at,
    lastError:      r.last_error,
    result:         r.result,
    createdAt:      r.created_at,
    updatedAt:      r.updated_at,
    cancelledAt:    r.cancelled_at,
  };
}

// ─────────────────────────────────────────────────────────────────────
// Writing Studio types (P1) — wire shapes for /api/v1/docs/*.
// ─────────────────────────────────────────────────────────────────────

/** One row in the Writing Studio's left-rail document list. */
export interface WritingDocMeta {
  id: string;
  title: string;
  /** ISO-8601 server timestamp of the last PUT. */
  updatedAt: string;
  refCount: number;
}

/** What a reference chip can snapshot. Patient side: basics /
 *  timeline; study side: progress / roster. */
export type WritingRefGranularity =
  | 'basics' | 'timeline' | 'progress' | 'roster';

/** One de-identified reference chip attached to a document. The body
 *  text carries a matching ``{{ref:REF_ID}}`` placeholder token. */
export interface WritingReference {
  refId: string;
  refType: 'patient' | 'study';
  targetId: string;
  granularity: WritingRefGranularity;
  chipLabel: string;
  snapshotPreview: string;
  createdAt: string;
}

/** Full document as returned by GET /docs/{id}. */
export interface WritingDoc {
  id: string;
  title: string;
  body: string;
  references: WritingReference[];
}

/** One entry in GET /docs/{id}/snapshots. */
export interface WritingSnapshot {
  id: string;
  label: string;
  createdAt: string;
}

/** SSE frames streamed by POST /docs/{id}/polish. */
export type WritingPolishFrame =
  | { type: 'revised_chunk'; text: string }
  | { type: 'provenance_warning'; numbers: string[] }
  | { type: 'done'; revised: string }
  | { type: 'error'; message: string };

/** One PHI finding from POST /docs/{id}/phi-scan. ``start``/``end``
 *  are body offsets; ``suggestion`` is the proposed replacement. */
export interface WritingPhiFinding {
  kind: string;
  excerpt: string;
  start: number;
  end: number;
  suggestion: string;
}

/** Per-finding decision sent back with POST /docs/{id}/export. */
export interface WritingPhiResolution {
  start: number;
  end: number;
  action: 'replace' | 'ignore';
  replacement?: string;
}

/** One message from GET /docs/{id}/chat (co-writing transcript). */
export interface WritingChatMessage {
  id: string;
  role: 'user' | 'assistant';
  text: string;
  /** True when this assistant turn rewrote the document. */
  docApplied: boolean;
  createdAt: string;
}

/** SSE frames streamed by POST /docs/{id}/chat. ``snapshot_id`` on
 *  done is the PRE-revision snapshot (restore = undo the revision);
 *  the server emits it as an integer row id. */
export type WritingChatFrame =
  | { type: 'reply_chunk'; text: string }
  | { type: 'doc_started' }
  | { type: 'doc_chunk'; text: string }
  | { type: 'provenance_warning'; numbers: string[] }
  | {
      type: 'done';
      reply: string;
      doc_body: string | null;
      snapshot_id: number | string | null;
      message_id: string;
      model: string;
    }
  | { type: 'error'; message: string };

// ─────────────────────────────────────────────────────────────────────
// Skills / plugins types — wire shapes for /api/v1/skills*.
// ─────────────────────────────────────────────────────────────────────

/** One installed skill as returned by ``GET /api/v1/skills``. */
export interface Skill {
  name:        string;
  description: string;
  /** Where the skill came from — 'official', 'github', … */
  source:      string;
  /** Disabled skills stay installed but are hidden from the "/" menu. */
  enabled:     boolean;
  /** ISO-8601 server timestamp. */
  installedAt: string;
  /** Whether this skill can be applied to a chat turn. */
  invocable:   boolean;
}

/** One row of ``GET /api/v1/skills/search`` (the Discover tab). */
export interface SkillSearchResult {
  /** Stable install handle — pass to ``installSkill``. */
  identifier:  string;
  name:        string;
  description: string;
  source:      string;
  installed:   boolean;
}

export const api = new _ApiClient();
