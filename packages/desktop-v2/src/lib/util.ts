import clsx, { type ClassValue } from 'clsx';

/** className helper. Used everywhere; keep it tiny. */
export function cn(...inputs: ClassValue[]): string {
  return clsx(inputs);
}

/* ────────────────────────────────────────────────────────────────── */
/* Types — mirror the backend's REST responses. Keep in sync with     */
/* nexus_server/dicom_router.py + patients_router.py.                 */
/* ────────────────────────────────────────────────────────────────── */

export interface PatientCard {
  patientHash: string;
  ageGroup: string;
  sex: 'M' | 'F' | '';
  studyCount: number;
  latestStudyDate: string;
  latestModality: string;
  lastSeenAt: number;
  initials: string;        // empty for DICOM-only / un-named patients
  mrn: string;             // empty unless medic entered one
  sequenceNumber: number;  // 1-based per-user ordinal (creation order)
  createdAt: number;
  unreadAgent?: boolean;
  hasConflict?: boolean;
}

/**
 * Build the human-readable label shown in the rail / mode header /
 * command palette. Order of preference (PHI-safe → max-info):
 *
 *   1. Initials present                → "J.D."
 *   2. MRN present                     → "MRN-12345"
 *   3. Sequence number known           → "Patient #3"
 *   4. DICOM-only with a study on file → "Patient · CT 2024-08-15"
 *   5. Last-resort fallback            → first 8 hex chars
 *
 * DICOM-only patients (imported via zip upload without manual register)
 * have empty initials + mrn but DO carry a modality + study date — those
 * give a much better label than a hash slice and aren't PHI on their
 * own. The sequence number, when present, is appended as a stable
 * handle so the medic can say "the patient I added third" even after
 * initials change or DICOM imports happen.
 */
export function patientDisplayLabel(p: PatientCard): string {
  const tail = p.sequenceNumber > 0 ? ` · #${p.sequenceNumber}` : '';
  if (p.initials && p.initials.trim()) return `${p.initials.trim()}${tail}`;
  if (p.mrn      && p.mrn.trim())      return `${p.mrn.trim()}${tail}`;
  if (p.sequenceNumber > 0)            return `Patient #${p.sequenceNumber}`;
  if (p.latestModality && p.latestStudyDate) {
    return `Patient · ${p.latestModality} ${p.latestStudyDate}`;
  }
  if (p.latestModality) return `Patient · ${p.latestModality}`;
  // last-resort fallback — legacy rows where everything is missing.
  // Truncate the hash to first 8 chars so the rail isn't a wall of hex.
  return p.patientHash ? p.patientHash.slice(0, 8) : 'Patient';
}

export type ModeKind =
  | 'today'
  | 'patient'
  | 'encounter'
  | 'imaging'
  | 'labs'
  | 'memory'
  | 'report'
  | 'research'; // Research Workspace — see docs/design/RESEARCH_WORKSPACE_DESIGN.md

export const MODE_LABELS: Record<ModeKind, string> = {
  today: 'Today',
  patient: 'Patient',
  encounter: 'Encounter',
  imaging: 'Imaging',
  labs: 'Labs',
  memory: 'Memory',
  report: 'Report',
  research: 'Research',
};

// Workspace = top-level Patient / Research / Writing toggle (decisions
// D1 + D14; Writing Studio added as the third top-level surface — see
// components/writing-studio.tsx). The active workspace lives in the
// Zustand store; the patient-side modes above keep their existing
// per-patient meaning.
export type Workspace = 'patient' | 'research' | 'writing';

// Research Workspace shapes returned by /api/v1/research/* endpoints.
export interface StudySummary {
  studyId:        string;
  displayName:    string;
  shortCode:      string;
  phase:          string;
  status:         string;
  targetN:        number | null;
  enrolledCount:  number;
  candidateCount: number;
  createdAt:      number;
  updatedAt:      number;
}

export interface StudyMembership {
  studyId:           string;
  studyShortCode:    string;
  studyDisplayName:  string;
  status:            string;
  enrollmentSeq:     number | null;
  arm:               string | null;
  enrolledAt:        number | null;
  withdrawnAt:       number | null;
  withdrawalReason:  string | null;
  consentSignedAt:   number | null;
}

/* MOCK_PATIENTS removed — store initial state is now empty [] and
 * refreshPatients() calls api.listPatients() after login. If you need
 * a quick demo populate use packages/server/scripts/demo_seed_and_verify.py
 * which inserts a deterministic set of test patients on the backend. */
