/**
 * Backend-shaped types for the web UI.
 *
 * These are intentionally minimal for M0 (login + chat). Expand as more
 * desktop-v2 features are migrated.
 */

export type UserRole = 'admin' | 'user';

export interface AuthSession {
  token: string;
  userId: string;
  role: UserRole;
  displayName: string;
  expiresInSeconds: number;
}

export interface AuthError {
  code: string;
  message: string;
}

export type ProviderKind = 'gemini' | 'openai' | 'anthropic' | 'kimi' | 'deepseek';

export interface LlmStatus {
  provider: ProviderKind;
  model: string;
  envFilePath: string;
  envFileExists: boolean;
  hasGeminiKey: boolean;
  hasOpenaiKey: boolean;
  hasAnthropicKey: boolean;
  hasKimiKey: boolean;
  hasDeepseekKey: boolean;
  advisory: string | null;
  activeKeySource?: 'db' | 'env' | 'none' | null;
  activeKeyPreview?: string;
  activeKeyLength?: number;
}

export interface LlmTestResult {
  ok: boolean;
  provider: string;
  model: string;
  latencyMs?: number;
  error?: string;
  diagnosis?: 'key_missing' | 'key_invalid' | 'quota_exceeded' | 'network' | 'other' | null;
}

export interface PublicConfig {
  appName: string;
  apiVersion: number;
  minClientApiVersion: number;
  defaultProvider?: ProviderKind;
  billingEnabled: boolean;
}

export type ChatStreamChunk =
  | { type: 'turn_started'; event_idx: number; patient_hash: string | null }
  | { type: 'tier_classified'; tier: 'T1' | 'T2' | 'T3'; view_kind?: string; anchor?: string }
  | { type: 'reasoning_chunk'; text: string }
  | { type: 'final_answer_chunk'; text: string }
  | { type: 'turn_complete'; assistant_event_idx?: number }
  | { type: 'error'; message: string };
