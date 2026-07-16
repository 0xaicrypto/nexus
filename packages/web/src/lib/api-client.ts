/**
 * ApiClient — HTTP wrapper around the FastAPI backend for the web UI.
 *
 * M0 scope: health, auth, public config, LLM settings status/test, chat SSE.
 * Expand as more desktop-v2 features migrate to packages/web.
 */

import type { AdminUser, AgentState, AuthSession, ChatMessage, ChatSession, ChatStreamChunk, LlmStatus, LlmTestResult, LlmUpdateInput, LlmUpdateResult, MemoryProjection, Patient, PatientDetail, PublicConfig, SendChatOptions, TimelineEvent, UserProfile } from './types';

export const CLIENT_API_VERSION = 1;

const STORAGE_KEY_TOKEN = 'nexus.auth.token';
const STORAGE_KEY_USER_ID = 'nexus.auth.user_id';
const STORAGE_KEY_DISPLAY_NAME = 'nexus.auth.display_name';

function storageGet(key: string): string | null {
  try {
    return localStorage.getItem(key);
  } catch {
    return null;
  }
}

function storageSet(key: string, value: string): void {
  try {
    localStorage.setItem(key, value);
  } catch {
    /* ignore */
  }
}

function storageRemove(key: string): void {
  try {
    localStorage.removeItem(key);
  } catch {
    /* ignore */
  }
}

export class ApiError extends Error {
  constructor(
    public status: number,
    public body: string,
    public path: string,
  ) {
    super(`${path} → ${status}: ${body}`);
    this.name = 'ApiError';
  }

  get code(): string | null {
    try {
      const parsed = JSON.parse(this.body);
      return parsed.error?.code ?? parsed.code ?? null;
    } catch {
      return null;
    }
  }

  get messageText(): string {
    try {
      const parsed = JSON.parse(this.body);
      return parsed.error?.message ?? parsed.message ?? parsed.detail ?? this.body;
    } catch {
      return this.body || this.statusText;
    }
  }

  private get statusText(): string {
    return `HTTP ${this.status}`;
  }
}

class ApiClient {
  private token: string | null = storageGet(STORAGE_KEY_TOKEN);

  setToken(t: string | null) {
    this.token = t;
    if (t) storageSet(STORAGE_KEY_TOKEN, t);
    else storageRemove(STORAGE_KEY_TOKEN);
  }

  hasToken() {
    return this.token !== null;
  }

  getToken() {
    return this.token;
  }

  logout() {
    this.token = null;
    storageRemove(STORAGE_KEY_TOKEN);
    storageRemove(STORAGE_KEY_USER_ID);
    storageRemove(STORAGE_KEY_DISPLAY_NAME);
  }

  private headers(extra?: HeadersInit): Headers {
    const h = new Headers(extra);
    h.set('Accept', 'application/json');
    h.set('X-Nexus-Api-Version', String(CLIENT_API_VERSION));
    if (this.token) h.set('Authorization', `Bearer ${this.token}`);
    return h;
  }

  private async fetch<T>(path: string, init?: RequestInit): Promise<T> {
    const h = this.headers(init?.headers);
    if (init?.body && !h.has('Content-Type')) h.set('Content-Type', 'application/json');

    const r = await fetch(path, { ...init, headers: h });
    if (!r.ok) {
      const text = await r.text().catch(() => '');
      const err = new ApiError(r.status, text || r.statusText, path);
      if (r.status === 401 && !path.startsWith('/api/v1/auth/')) {
        this.logout();
        window.dispatchEvent(new CustomEvent('nexus:auth-expired'));
      }
      throw err;
    }
    if (r.status === 204) return undefined as unknown as T;
    return r.json() as Promise<T>;
  }

  /* ────────────────────────── health / config ────────────────────────── */

  async health(): Promise<'ok' | 'unreachable' | 'unhealthy'> {
    try {
      const r = await fetch('/healthz', {
        method: 'GET',
        signal: AbortSignal.timeout(2500),
      });
      return r.ok ? 'ok' : 'unhealthy';
    } catch {
      return 'unreachable';
    }
  }

  async getPublicConfig(): Promise<PublicConfig> {
    return this.fetch<PublicConfig>('/api/v1/config');
  }

  /* ────────────────────────── auth ────────────────────────── */

  async register(input: {
    username: string;
    password: string;
    displayName?: string;
  }): Promise<AuthSession> {
    const body: Record<string, string> = {
      username: input.username,
      password: input.password,
    };
    if (input.displayName?.trim()) body.display_name = input.displayName.trim();

    const r = await this.fetch<{
      user_id: string;
      jwt_token: string;
      created_at: string;
      role: string;
      expires_in_seconds: number;
    }>('/api/v1/auth/register', { method: 'POST', body: JSON.stringify(body) });

    this.token = r.jwt_token;
    storageSet(STORAGE_KEY_TOKEN, r.jwt_token);
    storageSet(STORAGE_KEY_USER_ID, r.user_id);
    storageSet(STORAGE_KEY_DISPLAY_NAME, input.displayName?.trim() || input.username);

    return {
      token: r.jwt_token,
      userId: r.user_id,
      role: r.role === 'admin' ? 'admin' : 'user',
      displayName: input.displayName?.trim() || input.username,
      expiresInSeconds: r.expires_in_seconds,
    };
  }

  async login(username: string, password: string): Promise<AuthSession> {
    const r = await this.fetch<{
      jwt_token: string;
      expires_in_seconds: number;
      user_id: string;
      role: string;
      display_name: string | null;
    }>('/api/v1/auth/login', {
      method: 'POST',
      body: JSON.stringify({ username, password }),
    });

    const displayName = r.display_name || username;
    this.token = r.jwt_token;
    storageSet(STORAGE_KEY_TOKEN, r.jwt_token);
    storageSet(STORAGE_KEY_USER_ID, r.user_id);
    storageSet(STORAGE_KEY_DISPLAY_NAME, displayName);

    return {
      token: r.jwt_token,
      userId: r.user_id,
      role: r.role === 'admin' ? 'admin' : 'user',
      displayName,
      expiresInSeconds: r.expires_in_seconds,
    };
  }

  /* ────────────────────────── settings ────────────────────────── */

  async getLlmStatus(): Promise<LlmStatus> {
    return this.fetch<LlmStatus>('/api/v1/settings/llm');
  }

  async testLlm(): Promise<LlmTestResult> {
    return this.fetch<LlmTestResult>('/api/v1/settings/llm/test', { method: 'POST' });
  }

  async updateLlmSettings(input: LlmUpdateInput): Promise<LlmUpdateResult> {
    return this.fetch<LlmUpdateResult>('/api/v1/settings/llm', {
      method: 'PUT',
      body: JSON.stringify(input),
    });
  }

  /* ────────────────────────── user profile ────────────────────────── */

  async getUserProfile(): Promise<UserProfile> {
    return this.fetch<UserProfile>('/api/v1/user/profile');
  }

  async updateUserProfile(data: Partial<Pick<UserProfile, 'display_name' | 'organization' | 'intended_use'>>): Promise<UserProfile> {
    return this.fetch<UserProfile>('/api/v1/user/profile', {
      method: 'PATCH',
      body: JSON.stringify(data),
    });
  }

  /* ────────────────────────── patients ────────────────────────── */

  async listPatients(): Promise<Patient[]> {
    return this.fetch<Patient[]>('/api/v1/dicom/patients/full');
  }

  async getPatientDetail(hash: string): Promise<PatientDetail> {
    return this.fetch<PatientDetail>(`/api/v1/dicom/patients/${hash}/detail`);
  }

  async getPatientStudies(patientHash: string): Promise<Array<{study_id: string; modality: string; body_part?: string; series_count: number; created_at: string}>> {
    return this.fetch(`/api/v1/dicom/patients/${patientHash}/studies`);
  }

  async getDicomStudy(studyId: string): Promise<{study_id: string; modality: string; body_part?: string; series_count: number; slice_count?: number; created_at: string; series?: Array<{series_uid: string; series_description?: string; slice_count: number}>}> {
    return this.fetch(`/api/v1/dicom/studies/${studyId}`);
  }

  async getUploads(patientHash?: string, limit = 100): Promise<Array<{file_id: string; name: string; mime: string; size_bytes: number; created_at: string; patient_hash?: string; dicom_status?: string; dicom_study_id?: string}>> {
    const q = patientHash ? `?patient_hash=${patientHash}&limit=${limit}` : `?limit=${limit}`;
    return this.fetch(`/api/v1/files/uploads${q}`);
  }

  async triggerQuickScan(studyId: string): Promise<{job_id: string; status: string}> {
    return this.fetch(`/api/v1/dicom/studies/${studyId}/quick-scan`, { method: 'POST' });
  }

  /* ────────────────────────── report ────────────────────────── */

  async generateReport(data: {patient_hash: string; patient_label?: string; patient_sex?: string; patient_age_group?: string; clinical_info?: string; impression?: string; recommendation?: string}): Promise<{pdf_path: string; size_bytes: number; filename: string}> {
    return this.fetch('/api/v1/report/pdf', { method: 'POST', body: JSON.stringify(data) });
  }

  /* ────────────────────────── sessions ────────────────────────── */

  async listSessions(includeArchived = false): Promise<{ sessions: ChatSession[] }> {
    return this.fetch<{ sessions: ChatSession[] }>(`/api/v1/sessions?include_archived=${includeArchived}`);
  }

  async createSession(title: string): Promise<ChatSession> {
    return this.fetch<ChatSession>('/api/v1/sessions', {
      method: 'POST',
      body: JSON.stringify({ title }),
    });
  }

  async deleteSession(sessionId: string): Promise<void> {
    return this.fetch<void>(`/api/v1/sessions/${sessionId}`, { method: 'DELETE' });
  }

  /* ────────────────────────── agent state ────────────────────────── */

  async getAgentState(limit?: number): Promise<AgentState> {
    const q = limit ? `?limit=${limit}` : '';
    return this.fetch<AgentState>('/api/v1/agent/state' + q);
  }

  async getTimeline(limit = 20): Promise<{ items: TimelineEvent[] }> {
    return this.fetch<{ items: TimelineEvent[] }>(`/api/v1/agent/timeline?limit=${limit}`);
  }

  async getMessages(sessionId?: string, limit = 50): Promise<{ messages: ChatMessage[]; total: number }> {
    const q = sessionId ? `?session_id=${sessionId}&limit=${limit}` : `?limit=${limit}`;
    return this.fetch<{ messages: ChatMessage[]; total: number }>(`/api/v1/agent/messages${q}`);
  }

  /* ────────────────────────── memory ────────────────────────── */

  async getMemoryProjection(patientHash: string): Promise<MemoryProjection> {
    return this.fetch<MemoryProjection>(`/api/v1/memory/patient/${patientHash}/projection`);
  }

  async getFindings(patientHash: string): Promise<Array<{node_id: string; node_type: string; content: string; weight?: number; encounter_id?: string; updated_at?: string}>> {
    return this.fetch(`/api/v1/memory/patient/${patientHash}/findings`);
  }

  async getMedications(patientHash: string): Promise<Array<{node_id: string; node_type: string; content: string; weight?: number; encounter_id?: string; updated_at?: string}>> {
    return this.fetch(`/api/v1/memory/patient/${patientHash}/medications`);
  }

  async getMemoryTimeline(patientHash: string): Promise<Array<{event_id: string; event_type: string; content: string; timestamp: string}>> {
    return this.fetch(`/api/v1/memory/patient/${patientHash}/timeline`);
  }

  /* ────────────────────────── files ────────────────────────── */

  async uploadFile(file: File, patientHash?: string): Promise<{ file_id: string; name: string; mime: string; size_bytes: number }> {
    const form = new FormData();
    form.append('file', file);
    if (patientHash) form.append('patient_hash', patientHash);
    const h = this.headers();
    h.delete('Content-Type');
    const r = await fetch('/api/v1/files/upload', { method: 'POST', headers: h, body: form });
    if (!r.ok) {
      const text = await r.text().catch(() => '');
      throw new ApiError(r.status, text || r.statusText, '/api/v1/files/upload');
    }
    return r.json();
  }

  /* ────────────────────────── patient registration ────────────────────────── */

  async registerPatient(data: {
    initials?: string;
    mrn?: string;
    age?: number;
    sex?: string;
    chief_complaint?: string;
    notes?: string;
  }): Promise<{ patient_hash: string }> {
    return this.fetch('/api/v1/dicom/patients/register-manual', {
      method: 'POST',
      body: JSON.stringify(data),
    });
  }

  /* ────────────────────────── admin ────────────────────────── */

  async listUsers(): Promise<{ users: AdminUser[] }> {
    return this.fetch<{ users: AdminUser[] }>('/api/v1/admin/users');
  }

  async disableUser(userId: string): Promise<{ user_id: string; disabled_at: string; ok: boolean }> {
    return this.fetch(`/api/v1/admin/users/${userId}/disable`, { method: 'POST' });
  }

  async enableUser(userId: string): Promise<{ user_id: string; disabled_at: null; ok: boolean }> {
    return this.fetch(`/api/v1/admin/users/${userId}/enable`, { method: 'POST' });
  }

  async resetUserPassword(userId: string, newPassword: string): Promise<{ user_id: string; ok: boolean }> {
    return this.fetch(`/api/v1/admin/users/${userId}/reset-password`, {
      method: 'POST',
      body: JSON.stringify({ new_password: newPassword }),
    });
  }

  /* ────────────────────────── research ────────────────────────── */

  async listStudies(): Promise<Array<{study_id: string; title: string; status: string; protocol_id?: string; created_at: string}>> {
    return this.fetch('/api/v1/research/studies');
  }

  async createStudy(data: {title: string; protocol_id?: string}): Promise<{study_id: string; title: string; status: string}> {
    return this.fetch('/api/v1/research/studies', { method: 'POST', body: JSON.stringify(data) });
  }

  async getStudy(studyId: string): Promise<{study_id: string; title: string; status: string; protocol_id?: string; created_at: string; updated_at?: string; description?: string}> {
    return this.fetch(`/api/v1/research/studies/${studyId}`);
  }

  /* ────────────────────────── research detail ────────────────────────── */

  async getStudyRoster(studyId: string): Promise<Array<{patient_hash: string; initials?: string; status: string; arm?: string; enrolled_at: string}>> {
    return this.fetch(`/api/v1/research/studies/${studyId}/roster`);
  }

  async getStudyEligibility(studyId: string): Promise<{screenings: Array<{patient_hash: string; status: string; criteria_results?: Array<{criterion: string; passed: boolean}>}>}> {
    return this.fetch(`/api/v1/research/studies/${studyId}/eligibility`);
  }

  async getStudyObservations(studyId: string): Promise<Array<{observation_id: string; patient_hash: string; category: string; ae_grade?: number; is_dlt?: boolean; created_at: string}>> {
    return this.fetch(`/api/v1/research/studies/${studyId}/observations`);
  }

  async getStudyEnrollments(studyId: string): Promise<Array<{patient_hash: string; status: string; arm?: string; enrolled_at: string}>> {
    return this.fetch(`/api/v1/research/studies/${studyId}/enrollments`);
  }

  async enrollPatient(studyId: string, patientHash: string, arm?: string): Promise<{patient_hash: string; status: string}> {
    return this.fetch(`/api/v1/research/studies/${studyId}/enrollments`, { method: 'POST', body: JSON.stringify({ patient_hash: patientHash, arm }) });
  }

  async getSafetyStatus(studyId: string): Promise<{triggered_rules: Array<{rule: string; description: string}>}> {
    return this.fetch(`/api/v1/research/studies/${studyId}/safety/stop-rule-status`);
  }

  async rescanEligibility(studyId: string): Promise<{job_id: string; status: string}> {
    return this.fetch(`/api/v1/research/studies/${studyId}/eligibility/rescan`, { method: 'POST' });
  }

  /* ────────────────────────── skills ────────────────────────── */

  async listSkills(): Promise<{skills: Array<{name: string; title: string; description: string; version: string; author: string; enabled?: boolean}>}> {
    return this.fetch('/api/v1/skills');
  }

  async searchSkills(query: string): Promise<{results: Array<{identifier: string; name: string; description: string; version: string; author: string}>}> {
    return this.fetch(`/api/v1/skills/search?query=${encodeURIComponent(query)}`);
  }

  async installSkill(identifier: string): Promise<{name: string}> {
    return this.fetch('/api/v1/skills/install', { method: 'POST', body: JSON.stringify({ identifier }) });
  }

  async toggleSkill(name: string, enabled: boolean): Promise<{name: string; enabled: boolean}> {
    return this.fetch(`/api/v1/skills/${name}/toggle`, { method: 'POST', body: JSON.stringify({ enabled }) });
  }

  async uninstallSkill(name: string): Promise<{ok: boolean; name: string}> {
    return this.fetch(`/api/v1/skills/${name}`, { method: 'DELETE' });
  }

  /* ────────────────────────── writing ────────────────────────── */

  async listDocs(): Promise<{docs: Array<{id: string; title: string; updated_at: string; ref_count: number}>}> {
    return this.fetch('/api/v1/docs/docs');
  }

  async createDoc(title: string): Promise<{id: string; title: string; body: string; created_at: string; updated_at: string}> {
    return this.fetch('/api/v1/docs/docs', { method: 'POST', body: JSON.stringify({ title }) });
  }

  async getDoc(docId: string): Promise<{id: string; title: string; body: string; created_at: string; updated_at: string}> {
    return this.fetch(`/api/v1/docs/docs/${docId}`);
  }

  async updateDoc(docId: string, data: {title: string; body: string}): Promise<{id: string; title: string; body: string; updated_at: string}> {
    return this.fetch(`/api/v1/docs/docs/${docId}`, { method: 'PUT', body: JSON.stringify(data) });
  }

  async getDocSnapshots(docId: string): Promise<{snapshots: Array<{snapshot_id: string; created_at: string; body_preview: string}>}> {
    return this.fetch(`/api/v1/docs/docs/${docId}/snapshots`);
  }

  async restoreSnapshot(docId: string, snapshotId: string): Promise<{id: string; body: string}> {
    return this.fetch(`/api/v1/docs/docs/${docId}/snapshots/${snapshotId}/restore`, { method: 'POST' });
  }

  /* ────────────────────────── chat (SSE) ────────────────────────── */

  async *sendChat(
    text: string,
    sessionId: string,
    abortSignal?: AbortSignal,
  ): AsyncIterable<ChatStreamChunk> {
    return yield* this.sendChatFull({ text, sessionId }, abortSignal);
  }

  async *sendChatFull(
    opts: SendChatOptions,
    abortSignal?: AbortSignal,
  ): AsyncIterable<ChatStreamChunk> {
    const body: Record<string, unknown> = {
      text: opts.text,
      session_id: opts.sessionId || '',
      patient_hash: opts.patientHash ?? null,
    };
    if (opts.attachments) body.attachments = opts.attachments;
    if (opts.scope) body.scope = opts.scope;
    if (opts.skills) body.skills = opts.skills;
    const r = await fetch('/api/v1/agent/chat', {
      method: 'POST',
      headers: this.headers({ 'Content-Type': 'application/json' }),
      body: JSON.stringify(body),
      signal: abortSignal,
    });
    if (!r.ok || !r.body) {
      throw new ApiError(r.status, await r.text().catch(() => r.statusText), '/api/v1/agent/chat');
    }

    const reader = r.body.getReader();
    const dec = new TextDecoder();
    let buf = '';

    abortSignal?.addEventListener(
      'abort',
      () => {
        try {
          reader.cancel();
        } catch {
          /* ignore */
        }
      },
      { once: true },
    );

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
                yield JSON.parse(line.slice(6)) as ChatStreamChunk;
              } catch {
                /* malformed payload; skip */
              }
            }
          }
        }
      }
    } finally {
      try {
        reader.releaseLock();
      } catch {
        /* ignore */
      }
    }
  }
}

export const api = new ApiClient();
