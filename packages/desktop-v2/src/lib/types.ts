/**
 * Backend-shaped types (mirror of memory_router_v2.py + chat_router_v2.py).
 *
 * Per nexus-ux-redesign-v2.md §8 these mirror what the FastAPI server
 * returns. Keep in sync by hand for now; future task could autogenerate
 * via OpenAPI.
 */

export interface ProvenanceRow {
  nodeId: number;
  sourceKind: 'study' | 'chat' | 'lab' | 'manual';
  sourceRef: string;
  sourceLocator: Record<string, unknown>;
  evidenceQuote: string;
  extractionModel: string;
  extractionPromptId: string;
  confidence: number;
  redactionVersion: string;
  extractedAt: number;
  extractedByUser: string;
  supersededByNode: number | null;
  retractedAt: number | null;
}

export interface GraphNodeOut {
  nodeId: number;
  nodeType: string;
  content: Record<string, unknown>;
  weight: number;
  encounterId: string | null;
  updatedAt: number;
}

export interface PatientProjection {
  patientHash: string;
  findings: GraphNodeOut[];
  medications: GraphNodeOut[];
  differentials: GraphNodeOut[];
  studies: GraphNodeOut[];
  semanticFacts: GraphNodeOut[];
  unresolvedConflictCount: number;
}

export interface PractitionerCandidate {
  factKind: 'style' | 'workflow' | 'practice' | 'calibration';
  patternKey: string;
  patternValue: Record<string, unknown>;
  observedCount: number;
  distinctPatientCount: number;
  confidence: number;
  firstObservedAt: number;
  lastReinforcedAt: number;
}

export type TierKind = 'T1' | 'T2' | 'T3';

export interface SeriesInfo {
  seriesId: string;
  seriesInstanceUid: string;
  seriesNumber: number | null;
  modality: string;
  bodyPart: string;
  seriesDescription: string;
  defaultWl: number | null;
  defaultWw: number | null;
  instanceCount: number;
}

export interface StudyInfo {
  studyId: string;
  studyInstanceUid: string;
  studyDate: string;
  studyDescription: string;
  modality: string;
  patientHash: string;
  patientAgeGroup: string;
  patientSex: string;
  series: SeriesInfo[];
  createdAt: number;
}

/** Active LLM configuration as reported by /api/v1/settings/llm. The
 *  per-provider booleans are presence flags only — the keys themselves
 *  never leave the server. ``advisory`` is non-null when the active
 *  provider has no key configured, so the UI can render a banner. */
export interface LlmStatus {
  provider: 'gemini' | 'openai' | 'anthropic' | 'kimi';
  model: string;
  envFilePath: string;
  envFileExists: boolean;
  hasGeminiKey: boolean;
  hasOpenaiKey: boolean;
  hasAnthropicKey: boolean;
  hasKimiKey: boolean;
  advisory: string | null;
  /** Where the active provider's key came from. Lets the medic
   *  answer "did my DB-saved key actually load?" without grepping
   *  server logs. Server only knows 3 sources right now. */
  activeKeySource?: 'db' | 'env' | 'none' | null;
  /** First 6 + last 4 of the active provider's key with bullets in
   *  the middle. Confirms "yes that's the key I expect" — never the
   *  full secret. Example: ``AIzaSy••••••••KlMn``. Empty if none. */
  activeKeyPreview?: string;
  activeKeyLength?: number;
}

export interface LlmTestResult {
  ok: boolean;
  provider: string;
  model: string;
  latencyMs?: number;
  error?: string;
  /** Server-side classification of the failure. UI uses this to
   *  pick the right colour + remediation hint. */
  diagnosis?: 'key_missing' | 'key_invalid' | 'quota_exceeded'
            | 'network' | 'other' | null;
}

/** Citation reference. Comes in two flavours:
 *  - kind='graph_node' carries node_id → CitationChip [Nxx] → ContextRail
 *    opens the patient-graph node panel.
 *  - kind='web_source' carries w_id + url + title → WebCitationChip [Wxx] →
 *    ContextRail opens a web-source panel with the snippet preview. */
export interface CitationRef {
  kind:    string;          // 'graph_node' | 'web_source'
  // graph_node fields
  node_id?: number;
  // web_source fields
  w_id?:    number;
  url?:     string;
  title?:   string;
  domain?:  string;
  snippet?: string;
}

export type ChatStreamChunk =
  | { type: 'turn_started'; event_idx: number; patient_hash: string | null }
  | { type: 'tier_classified'; tier: TierKind; view_kind?: string; anchor?: string }
  | { type: 'reasoning_chunk'; text: string }
  | { type: 'search_query'; tool: string; query: string }
  | { type: 'search_results_summary'; count: number; preview: string }
  | { type: 'image_attached'; image_sha256s: string[] }
  | { type: 'final_answer_chunk'; text: string }
  | { type: 'citations'; refs: CitationRef[] }
  | { type: 'conflict_in_answer'; conflict_id: string; finding_label: string }
  /** Tavily web-search started. UI renders a transient
   *  "🔎 Searching {provider}…" card under the agent reply. */
  | { type: 'web_search_started'; query: string; provider: string }
  /** Tavily returned. UI lists the cited sources before the LLM
   *  synthesis chunks land — gives the medic a quick preview of
   *  what got grounded. */
  | {
      type: 'web_search_results';
      count: number;
      results: Array<{
        w_id:    number;
        url:     string;
        title:   string;
        snippet: string;
        domain:  string;
        score?:  number;
      }>;
    }
  | { type: 'turn_complete'; assistant_event_idx?: number }
  /** chat_ingester (Layer 1 patient graph) outcome for this turn.
   *  Drives the "✓ 已记忆 N 项 / 本轮未记忆" chip below the agent
   *  reply so the medic can see at a glance whether their SOAP
   *  text landed in the patient's structured graph. Three states:
   *    - ok=true, node_count>0 → success
   *    - ok=false, raw_count=0 → extractor never returned (API key /
   *      quota / source too thin)
   *    - ok=false, raw_count>0 → extractor returned entities but
   *      none survived the verbatim quote check */
  | {
      type: 'memory_ingested';
      ok: boolean;
      node_count: number;
      raw_count: number;
      error?: string;
    }
  /** Heuristic / LLM detected a future-action intent in the medic's
   *  message. UI renders an inline confirmation card; user clicks
   *  Confirm to actually persist via POST /api/v1/schedule/confirm. */
  | {
      type: 'scheduled_task_proposed';
      proposal_id:     string;
      kind:            'send_email';
      fire_at:         number;
      user_tz:         string;
      summary:         string;
      payload:         Record<string, unknown>;
      recurrence_cron: string | null;
      session_id:      string | null;
      patient_hash:    string | null;
      needs_user_input: string[];
    }
  | { type: 'error'; message: string };

/**
 * F-chat-state-persist — ChatMsg + ChatProposal lifted out of
 * `modes.tsx` so the zustand store can carry them across tab
 * switches. Lives in this shared types file alongside the related
 * CitationRef / ChatStreamChunk so the SSE consumer can write
 * directly into the store without a circular import on modes.tsx.
 */
export interface ChatProposal {
  proposalId:     string;
  kind:           'send_email';
  fireAt:         number;
  userTz:         string;
  summary:        string;
  payload:        Record<string, unknown>;
  recurrenceCron: string | null;
  sessionId:      string | null;
  patientHash:    string | null;
  needsUserInput: string[];
  uiState:        'editing' | 'submitting' | 'done' | 'cancelled';
  errorMsg?:      string;
}

export interface ChatMsg {
  role: 'user' | 'agent';
  text: string;
  ts: string;
  tier?: TierKind;
  reasoning?: string[];
  citations?: CitationRef[];
  elapsedMs?: number;
  streaming?: boolean;
  attachedFileNames?: string[];
  proposal?: ChatProposal | null;
  webResults?: Array<{
    w_id: number;
    url: string;
    title: string;
    snippet: string;
    domain: string;
  }>;
  memoryIngested?: {
    ok: boolean;
    nodeCount: number;
    rawCount: number;
    error?: string;
  };
}
