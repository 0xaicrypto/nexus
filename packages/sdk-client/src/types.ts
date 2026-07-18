// ── Auth ──
export type UserRole = 'admin' | 'user'

export interface AuthSession {
  jwt_token: string
  user_id: string
  role: UserRole
  display_name: string
  expires_in_seconds: number
  created_at?: string
}

export interface UserProfile {
  user_id: string
  display_name: string
  created_at: string
  updated_at?: string
  role?: string
  email?: string
  organization?: string
  intended_use?: string
  status?: string
  tier?: string
}

export type ProviderKind = 'gemini' | 'openai' | 'anthropic' | 'kimi' | 'deepseek'

export interface LlmStatus {
  provider: ProviderKind
  model: string
  hasGeminiKey: boolean; hasOpenaiKey: boolean; hasAnthropicKey: boolean
  hasKimiKey: boolean; hasDeepseekKey: boolean
  advisory: string | null
  activeKeySource?: 'db' | 'env' | 'none' | null
}

// ── Chat ──
export interface SendChatOptions {
  text: string
  sessionId?: string
  patientHash?: string | null
  attachments?: unknown[]
  scope?: { kind: string; ref: string }
  skills?: string[]
}

export interface ChatSession {
  id: string; title: string; created_at: string
  updated_at?: string; archived?: boolean; message_count?: number
}

export type ChatStreamChunk =
  | { type: 'turn_started'; event_idx: number; patient_hash: string | null }
  | { type: 'tier_classified'; tier: 'T1' | 'T2' | 'T3'; view_kind?: string }
  | { type: 'context_info'; text: string; kind?: string }
  | { type: 'reasoning_chunk'; text: string }
  | { type: 'final_answer_chunk'; text: string }
  | { type: 'citations'; items: { text: string; source?: string }[] }
  | { type: 'turn_complete'; assistant_event_idx?: number }
  | { type: 'error'; message: string }

// ── Patients ──
export interface Patient {
  patient_hash: string; initials?: string; mrn?: string
  age_value?: number; age_group?: string; sex?: string
  chief_complaint?: string; study_count: number
  latest_study_date?: string; latest_modality?: string; created_at: string
  source?: 'manual' | 'dicom'
}
export interface PatientDetail extends Patient { archive?: { archived_at?: string } }

// ── Research ──
export interface Study {
  id: string; userId?: string; name: string; shortCode: string; createdAt?: string; updatedAt?: string
  short_code?: string; created_at?: string; updated_at?: string; roster_count?: number
}
export interface RosterEntry { patient_hash: string; arm: string; enrolled_at: string; study_id?: string }
export interface Screening { id: string; patient_hash: string; initials?: string; verdict: string; reason?: string; scanned_at: string }
export interface Observation { id: string; patient_hash: string; kind: string; grade?: number; dlt?: number; confirmed?: number; note?: string; created_at: string }
export interface Assessment { id: string; patient_hash: string; visit: string; title: string; due_at: string; completed_at?: string }
export interface SafetyStatus { stop_rules?: Array<{ name: string; triggered: boolean; detail?: string }>; stopRules?: Array<{ name: string; triggered: boolean; detail?: string }> }

// ── Documents ──
export interface Doc { id: string; title: string; body: string; created_at: string; updated_at: string; ref_count?: number }
export interface SnapshotEntry { id: number; body: string; label: string; created_at: string }
export interface PhiFinding { kind: string; text: string; start: number; end: number }

// ── Skills ──
export interface InstalledSkill { name: string; title?: string; description?: string; version?: string; author?: string; enabled?: boolean }
export interface SearchResult { identifier: string; name: string; description: string; source: string; installed: boolean; version?: string; author?: string }

// ── Files ──
export interface FileItem { id: number | string; filename: string; content_type: string; size_bytes: number; patient_hash?: string | null; created_at: string }

// ── Admin ──
export interface AdminUser { user_id: string; username: string; role: string; created_at: string; disabled_at?: string | null; last_login_at?: string }

// ── Memory ──
export interface MemoryFinding { node_id: string; node_type: string; content: string; weight?: number; encounter_id?: string; updated_at?: string }
export interface MemoryTimelineEvent { event_id?: string; event_type: string; content: string; timestamp: string }
export interface MemoryProjection { findings?: MemoryFinding[]; medications?: MemoryFinding[]; timeline?: MemoryTimelineEvent[] }
