/**
 * ApiClient — HTTP wrapper around the FastAPI backend for the web UI.
 *
 * M0 scope: health, auth, public config, LLM settings status/test, chat SSE.
 * Expand as more desktop-v2 features migrate to packages/web.
 */

import type { AuthSession, ChatStreamChunk, LlmStatus, LlmTestResult, PublicConfig } from './types';

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

  /* ────────────────────────── chat (SSE) ────────────────────────── */

  async *sendChat(
    text: string,
    sessionId: string,
    abortSignal?: AbortSignal,
  ): AsyncIterable<ChatStreamChunk> {
    const r = await fetch('/api/v1/agent/chat', {
      method: 'POST',
      headers: this.headers({ 'Content-Type': 'application/json' }),
      body: JSON.stringify({ text, session_id: sessionId, patient_hash: null }),
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
