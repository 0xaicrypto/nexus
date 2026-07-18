// Generated from frontend lib/types.ts — single source of truth for API types
// Keep in sync with packages/web/src/lib/types.ts

export type UserRole = 'admin' | 'user'

export interface AuthSession {
  token: string
  userId: string
  role: UserRole
  displayName: string
  expiresInSeconds: number
}

export interface LoginInput {
  username: string
  password: string
}

export interface RegisterInput {
  username: string
  password: string
  displayName: string
}

export interface UserProfile {
  userId: string
  displayName: string
  createdAt: string
  updatedAt?: string
  email?: string
  organization?: string
  intendedUse?: string
  status?: string
  tier?: string
}

export type ProviderKind = 'gemini' | 'openai' | 'anthropic' | 'kimi' | 'deepseek'

export interface LlmStatus {
  provider: ProviderKind
  model: string
  hasGeminiKey: boolean
  hasOpenaiKey: boolean
  hasAnthropicKey: boolean
  hasKimiKey: boolean
  hasDeepseekKey: boolean
  advisory: string | null
  activeKeySource?: 'db' | 'env' | 'none' | null
}

export interface LlmUpdateInput {
  provider?: ProviderKind
  model?: string
  geminiApiKey?: string
  openaiApiKey?: string
  anthropicApiKey?: string
  kimiApiKey?: string
  deepseekApiKey?: string
}

export interface Patient {
  patientHash: string
  initials?: string
  mrn?: string
  ageValue?: number
  ageGroup?: string
  sex?: string
  chiefComplaint?: string
  notes?: string
  createdAt: string
  studyCount: number
  latestStudyDate?: string
  latestModality?: string
  source?: 'manual' | 'dicom'
}

export interface PatientDetail extends Patient {
  archive?: { archivedAt?: string }
}

export interface RegisterPatientInput {
  initials?: string
  ageValue?: number
  sex?: string
  chiefComplaint?: string
  notes?: string
}

export interface ChatSession {
  id: string
  title: string
  createdAt: string
  archived?: boolean
  messageCount?: number
}

export interface ChatMessage {
  role: 'user' | 'assistant'
  content: string
  timestamp: string
  syncId?: string
  attachments?: unknown[]
  messageKind?: string
  metadata?: Record<string, unknown>
}

export interface SendChatInput {
  text: string
  sessionId?: string
  patientHash?: string | null
  attachments?: unknown[]
  scope?: { kind: string; ref: string }
  skills?: string[]
}

export interface MemoryFinding {
  nodeId: string
  nodeType: string
  content: string
  weight?: number
  encounterId?: string
  updatedAt?: string
}

export interface MemoryTimelineEvent {
  eventId: string
  eventType: string
  content: string
  timestamp: string
}

export interface MemoryProjection {
  findings?: MemoryFinding[]
  medications?: MemoryFinding[]
  timeline?: MemoryTimelineEvent[]
}

export interface AgentState {
  memoryCount: number
  serverTime: string
}

export interface TimelineEvent {
  kind: string
  timestamp: string
  summary: string
  syncId?: string
  metadata?: Record<string, unknown>
}

export interface AdminUser {
  userId: string
  username: string
  role: string
  createdAt: string
  disabledAt?: string | null
  lastLoginAt?: string
}

// --- Research types ---

export interface Study {
  id: string
  name: string
  shortCode: string
  createdAt: string
  rosterCount?: number
}

export interface StudyDetail extends Study {
  protocol?: Record<string, unknown>
}

export interface RosterEntry {
  patientHash: string
  initials?: string
  enrolledAt: string
  arm?: string
}

export interface Screening {
  patientHash: string
  initials?: string
  verdict: string
  reason?: string
  scannedAt: string
}

export interface Observation {
  id: string
  kind: string
  patientHash: string
  grade?: number
  dlt?: boolean
  confirmed: boolean
  createdAt: string
}

export interface Assessment {
  visit: string
  title: string
  patientHash: string
  dueAt: string
  completedAt?: string
}

export interface Enrollment {
  patientHash: string
  arm: string
  enrolledAt: string
}

export interface SafetyStatus {
  stopRules: { name: string; triggered: boolean; detail?: string }[]
}

// --- Document types ---

export interface Doc {
  id: string
  title: string
  body: string
  createdAt: string
  updatedAt: string
}

export interface SnapshotEntry {
  id: number
  body: string
  label: string
  createdAt: string
}

export interface PhiFinding {
  kind: string
  text: string
  start: number
  end: number
}

// --- Skill types ---

export interface InstalledSkill {
  name: string
  enabled: boolean
  autoApply: boolean
  source: string
  createdAt: string
}

export interface SearchResult {
  identifier: string
  name: string
  description: string
  source: string
  installed: boolean
}

// --- File types ---

export interface FileItem {
  id: number
  filename: string
  contentType: string
  sizeBytes: number
  patientHash?: string | null
  createdAt: string
}

// --- Chat stream chunks (SSE) ---

export type ChatStreamChunk =
  | { type: 'turn_started'; eventIdx: number; patientHash: string | null }
  | { type: 'tier_classified'; tier: 'T1' | 'T2' | 'T3'; viewKind?: string }
  | { type: 'context_info'; text: string; kind?: string }
  | { type: 'reasoning_chunk'; text: string }
  | { type: 'search_query'; query: string }
  | { type: 'search_results_summary'; text: string }
  | { type: 'image_attached'; url?: string; studyId?: string; caption?: string }
  | { type: 'final_answer_chunk'; text: string }
  | { type: 'citations'; items: { text: string; source?: string }[] }
  | { type: 'turn_complete'; assistantEventIdx?: number }
  | { type: 'error'; message: string }
