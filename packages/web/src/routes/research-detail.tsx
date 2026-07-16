import { useCallback, useEffect, useState } from 'react';
import { useNavigate, useParams } from 'react-router-dom';
import { ArrowLeft, CalendarDays, Check, FlaskConical, Plus, X } from 'lucide-react';
import { AppShell } from '@/components/layout/AppShell';
import { Alert, Badge, Button, Card, Skeleton } from '@/components/ui';
import { api, ApiError } from '@/lib/api-client';
import { cn } from '@/lib/utils';
import type { Patient } from '@/lib/types';

interface StudyDetail {
  study_id: string;
  title: string;
  status: string;
  protocol_id?: string;
  created_at: string;
  updated_at?: string;
  description?: string;
}

interface RosterEntry {
  patient_hash: string;
  initials?: string;
  status: string;
  arm?: string;
  enrolled_at: string;
}

interface Screening {
  patient_hash: string;
  status: string;
  criteria_results?: Array<{criterion: string; passed: boolean}>;
}

interface Observation {
  observation_id: string;
  patient_hash: string;
  category: string;
  ae_grade?: number;
  is_dlt?: boolean;
  confirmed?: boolean;
  created_at: string;
}

interface Assessment {
  visit_id: string;
  patient_hash: string;
  scheduled_at: string;
  status: string;
  completed_at?: string;
}

interface SafetyStatus {
  triggered_rules: Array<{rule: string; description: string}>;
}

interface Enrollment {
  patient_hash: string;
  status: string;
  arm?: string;
  enrolled_at: string;
}

type Tab = 'overview' | 'roster' | 'eligibility' | 'schedule' | 'safety' | 'protocol';

const TABS: { key: Tab; label: string }[] = [
  { key: 'overview', label: 'Overview' },
  { key: 'roster', label: 'Roster' },
  { key: 'eligibility', label: 'Eligibility' },
  { key: 'schedule', label: 'Schedule' },
  { key: 'safety', label: 'Safety' },
  { key: 'protocol', label: 'Protocol' },
];

export function ResearchDetailPage() {
  const { studyId } = useParams<{ studyId: string }>();
  const navigate = useNavigate();
  const [study, setStudy] = useState<StudyDetail | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [tab, setTab] = useState<Tab>('overview');

  // overview
  const [enrollments, setEnrollments] = useState<Enrollment[]>([]);

  // roster
  const [roster, setRoster] = useState<RosterEntry[]>([]);
  const [rosterLoading, setRosterLoading] = useState(false);

  // eligibility
  const [eligibility, setEligibility] = useState<{screenings: Screening[]} | null>(null);
  const [eligLoading, setEligLoading] = useState(false);
  const [rescanning, setRescanning] = useState(false);

  // safety
  const [observations, setObservations] = useState<Observation[]>([]);
  const [safetyStatus, setSafetyStatus] = useState<SafetyStatus | null>(null);
  const [safetyLoading, setSafetyLoading] = useState(false);
  const [confirmingObs, setConfirmingObs] = useState<Record<string, { aeGrade?: number; isDlt?: boolean }>>({});
  const [confirmingIds, setConfirmingIds] = useState<Set<string>>(new Set());

  // schedule
  const [assessments, setAssessments] = useState<Assessment[]>([]);
  const [scheduleLoading, setScheduleLoading] = useState(false);
  const [completingIds, setCompletingIds] = useState<Set<string>>(new Set());

  // unenroll
  const [unenrollingHash, setUnenrollingHash] = useState<string | null>(null);

  // enroll dialog
  const [showEnroll, setShowEnroll] = useState(false);
  const [patients, setPatients] = useState<Patient[]>([]);
  const [patientsLoading, setPatientsLoading] = useState(false);
  const [enrollingHash, setEnrollingHash] = useState<string | null>(null);

  useEffect(() => {
    if (!studyId) return;
    setLoading(true);
    setError(null);
    api.getStudy(studyId)
      .then(setStudy)
      .catch((err) => setError(err instanceof ApiError ? err.messageText : String(err)))
      .finally(() => setLoading(false));
  }, [studyId]);

  useEffect(() => {
    if (!studyId || !study) return;
    api.getStudyEnrollments(studyId)
      .then(setEnrollments)
      .catch(() => {});
  }, [studyId, study]);

  const loadRoster = useCallback(() => {
    if (!studyId) return;
    setRosterLoading(true);
    api.getStudyRoster(studyId)
      .then(setRoster)
      .catch(() => {})
      .finally(() => setRosterLoading(false));
  }, [studyId]);

  const loadEligibility = useCallback(() => {
    if (!studyId) return;
    setEligLoading(true);
    api.getStudyEligibility(studyId)
      .then(setEligibility)
      .catch(() => {})
      .finally(() => setEligLoading(false));
  }, [studyId]);

  const loadSafety = useCallback(() => {
    if (!studyId) return;
    setSafetyLoading(true);
    Promise.all([
      api.getStudyObservations(studyId),
      api.getSafetyStatus(studyId),
    ])
      .then(([obs, status]) => {
        setObservations(obs);
        setSafetyStatus(status);
        const init: Record<string, { aeGrade?: number; isDlt?: boolean }> = {};
        obs.forEach((o) => {
          if (!o.confirmed) {
            init[o.observation_id] = { aeGrade: o.ae_grade, isDlt: o.is_dlt };
          }
        });
        setConfirmingObs(init);
      })
      .catch(() => {})
      .finally(() => setSafetyLoading(false));
  }, [studyId]);

  const loadSchedule = useCallback(() => {
    if (!studyId) return;
    setScheduleLoading(true);
    api.getStudyAssessments(studyId)
      .then(setAssessments)
      .catch(() => {})
      .finally(() => setScheduleLoading(false));
  }, [studyId]);

  useEffect(() => {
    if (!study) return;
    if (tab === 'roster') loadRoster();
    else if (tab === 'eligibility') loadEligibility();
    else if (tab === 'safety') loadSafety();
    else if (tab === 'schedule') loadSchedule();
  }, [tab, study, loadRoster, loadEligibility, loadSafety, loadSchedule]);

  const openEnroll = async () => {
    if (!studyId) return;
    setShowEnroll(true);
    setPatientsLoading(true);
    try {
      const list = await api.listPatients();
      setPatients(list);
    } catch {
      setPatients([]);
    } finally {
      setPatientsLoading(false);
    }
  };

  const handleEnroll = async (patientHash: string, arm?: string) => {
    if (!studyId) return;
    setEnrollingHash(patientHash);
    try {
      await api.enrollPatient(studyId, patientHash, arm);
      setShowEnroll(false);
      loadRoster();
    } catch (err) {
      setError(err instanceof ApiError ? err.messageText : String(err));
    } finally {
      setEnrollingHash(null);
    }
  };

  const handleRescan = async () => {
    if (!studyId) return;
    setRescanning(true);
    try {
      await api.rescanEligibility(studyId);
      loadEligibility();
    } catch (err) {
      setError(err instanceof ApiError ? err.messageText : String(err));
    } finally {
      setRescanning(false);
    }
  };

  const handleUnenroll = async (patientHash: string) => {
    if (!studyId) return;
    setUnenrollingHash(patientHash);
    try {
      await api.unenrollPatient(studyId, patientHash);
      loadRoster();
    } catch (err) {
      setError(err instanceof ApiError ? err.messageText : String(err));
    } finally {
      setUnenrollingHash(null);
    }
  };

  const handleConfirmObservation = async (obsId: string) => {
    if (!studyId) return;
    const vals = confirmingObs[obsId];
    const next = new Set(confirmingIds);
    next.add(obsId);
    setConfirmingIds(next);
    try {
      await api.confirmObservation(studyId, obsId, vals?.aeGrade, vals?.isDlt);
      loadSafety();
    } catch (err) {
      setError(err instanceof ApiError ? err.messageText : String(err));
    } finally {
      const after = new Set(confirmingIds);
      after.delete(obsId);
      setConfirmingIds(after);
    }
  };

  const updateObsForm = (obsId: string, field: 'aeGrade' | 'isDlt', value: number | boolean) => {
    setConfirmingObs((prev) => ({
      ...prev,
      [obsId]: { ...prev[obsId], [field]: value },
    }));
  };

  const handleCompleteAssessment = async (visitId: string) => {
    if (!studyId) return;
    const next = new Set(completingIds);
    next.add(visitId);
    setCompletingIds(next);
    try {
      await api.completeAssessment(studyId, visitId);
      loadSchedule();
    } catch (err) {
      setError(err instanceof ApiError ? err.messageText : String(err));
    } finally {
      const after = new Set(completingIds);
      after.delete(visitId);
      setCompletingIds(after);
    }
  };

  const statusVariant = (s: string): 'default' | 'success' | 'warning' | 'error' => {
    switch (s.toLowerCase()) {
      case 'completed': return 'success';
      case 'in_progress':
      case 'running': return 'warning';
      case 'failed':
      case 'error': return 'error';
      default: return 'default';
    }
  };

  const aeGradeColor = (grade?: number) => {
    if (!grade) return 'text-text-secondary';
    return grade >= 3 ? 'text-error' : 'text-warning';
  };

  if (loading) {
    return (
      <AppShell>
        <div className="flex h-full flex-col">
          <div className="flex h-14 items-center border-b border-border bg-surface px-6 gap-3">
            <Skeleton className="h-5 w-5" />
            <Skeleton className="h-5 w-48" />
          </div>
          <div className="p-6 space-y-4">
            <Skeleton className="h-24 w-full rounded-xl" />
            <Skeleton className="h-32 w-full rounded-xl" />
          </div>
        </div>
      </AppShell>
    );
  }

  if (error && !study) {
    return (
      <AppShell>
        <div className="flex h-full flex-col">
          <div className="flex h-14 items-center border-b border-border bg-surface px-6">
            <Button variant="ghost" size="sm" onClick={() => navigate('/app/research')}>
              <ArrowLeft size={16} className="mr-1" /> Back
            </Button>
          </div>
          <div className="p-6">
            <Alert variant="error">{error}</Alert>
          </div>
        </div>
      </AppShell>
    );
  }

  if (!study) {
    return (
      <AppShell>
        <div className="flex h-full flex-col">
          <div className="flex h-14 items-center border-b border-border bg-surface px-6">
            <Button variant="ghost" size="sm" onClick={() => navigate('/app/research')}>
              <ArrowLeft size={16} className="mr-1" /> Back
            </Button>
          </div>
          <div className="flex flex-1 items-center justify-center">
            <p className="text-text-tertiary">Study not found</p>
          </div>
        </div>
      </AppShell>
    );
  }

  return (
    <AppShell>
      <div className="flex h-full flex-col overflow-y-auto">
        <header className="flex h-14 items-center gap-3 border-b border-border bg-surface px-6">
          <Button variant="ghost" size="sm" onClick={() => navigate('/app/research')}>
            <ArrowLeft size={16} />
          </Button>
          <h1 className="font-semibold text-text-primary">{study.title}</h1>
          <Badge variant={statusVariant(study.status)}>{study.status}</Badge>
        </header>

        <nav className="flex gap-1 border-b border-border px-6">
          {TABS.map((t) => (
            <button
              key={t.key}
              onClick={() => setTab(t.key)}
              className={cn(
                'border-b-2 px-3 py-3 text-sm font-medium transition-colors',
                tab === t.key
                  ? 'border-accent text-accent'
                  : 'border-transparent text-text-secondary hover:text-text-primary',
              )}
            >
              {t.label}
            </button>
          ))}
        </nav>

        <main className="flex-1 p-6">
          {error && (
            <div className="mb-4">
              <Alert variant="error">{error}</Alert>
            </div>
          )}

          {tab === 'overview' && (
            <div className="max-w-2xl space-y-4">
              <Card className="p-6 space-y-3">
                <div>
                  <div className="text-xs text-text-tertiary">Study ID</div>
                  <div className="font-mono text-sm text-text-secondary">{study.study_id}</div>
                </div>
                {study.protocol_id && (
                  <div>
                    <div className="text-xs text-text-tertiary">Protocol ID</div>
                    <div className="text-sm text-text-primary">{study.protocol_id}</div>
                  </div>
                )}
                <div>
                  <div className="text-xs text-text-tertiary">Created</div>
                  <div className="text-sm text-text-primary">{new Date(study.created_at).toLocaleDateString()}</div>
                </div>
                {study.updated_at && (
                  <div>
                    <div className="text-xs text-text-tertiary">Updated</div>
                    <div className="text-sm text-text-primary">{new Date(study.updated_at).toLocaleDateString()}</div>
                  </div>
                )}
                {study.description && (
                  <div>
                    <div className="text-xs text-text-tertiary">Description</div>
                    <div className="text-sm text-text-primary">{study.description}</div>
                  </div>
                )}
              </Card>

              <Card className="p-6">
                <h3 className="mb-3 text-sm font-semibold text-text-secondary">Recent Activity</h3>
                {enrollments.length === 0 ? (
                  <p className="text-sm text-text-tertiary">No enrollments yet</p>
                ) : (
                  <div className="space-y-2">
                    {enrollments.slice(0, 10).map((e, i) => (
                      <div key={`${e.patient_hash}-${i}`} className="flex items-center justify-between text-sm">
                        <span className="font-mono text-text-secondary">{e.patient_hash.slice(0, 12)}...</span>
                        <Badge variant={e.status === 'active' ? 'success' : 'default'}>{e.status}</Badge>
                        <span className="text-text-tertiary">{new Date(e.enrolled_at).toLocaleDateString()}</span>
                      </div>
                    ))}
                  </div>
                )}
              </Card>
            </div>
          )}

          {tab === 'roster' && (
            <div className="max-w-3xl space-y-4">
              <div className="flex items-center justify-between">
                <h2 className="text-sm font-semibold text-text-secondary">Roster</h2>
                <Button size="sm" onClick={openEnroll}>
                  <Plus size={14} className="mr-1" /> Enroll Patient
                </Button>
              </div>
              {rosterLoading ? (
                <div className="space-y-3">
                  <Skeleton className="h-10 w-full rounded-xl" />
                  <Skeleton className="h-10 w-full rounded-xl" />
                </div>
              ) : roster.length === 0 ? (
                <div className="flex flex-col items-center justify-center rounded-xl border border-border py-12 text-center">
                  <FlaskConical size={36} className="mb-3 text-text-tertiary" />
                  <p className="text-text-tertiary">No patients enrolled</p>
                </div>
              ) : (
                <div className="overflow-x-auto rounded-xl border border-border">
                  <table className="w-full text-sm">
                    <thead>
                      <tr className="border-b border-border bg-surface">
                        <th className="px-4 py-2 text-left font-medium text-text-secondary">Patient</th>
                        <th className="px-4 py-2 text-left font-medium text-text-secondary">Status</th>
                        <th className="px-4 py-2 text-left font-medium text-text-secondary">Arm</th>
                        <th className="px-4 py-2 text-left font-medium text-text-secondary">Enrolled</th>
                        <th className="px-4 py-2 text-left font-medium text-text-secondary"></th>
                      </tr>
                    </thead>
                    <tbody>
                      {roster.map((r) => (
                        <tr key={r.patient_hash} className="border-b border-border last:border-0">
                          <td className="px-4 py-2 font-mono text-text-primary">
                            {r.initials ? `${r.initials} — ` : ''}{r.patient_hash.slice(0, 12)}...
                          </td>
                          <td className="px-4 py-2">
                            <Badge variant={r.status === 'active' ? 'success' : 'default'}>{r.status}</Badge>
                          </td>
                          <td className="px-4 py-2 text-text-secondary">{r.arm || '—'}</td>
                          <td className="px-4 py-2 text-text-tertiary">{new Date(r.enrolled_at).toLocaleDateString()}</td>
                          <td className="px-4 py-2">
                            <button
                              className="rounded p-1 text-text-tertiary hover:bg-error/10 hover:text-error transition-colors"
                              onClick={() => handleUnenroll(r.patient_hash)}
                              disabled={unenrollingHash === r.patient_hash}
                              title="Unenroll patient"
                            >
                              <X size={14} />
                            </button>
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </div>
          )}

          {tab === 'eligibility' && (
            <div className="max-w-3xl space-y-4">
              <div className="flex items-center justify-between">
                <h2 className="text-sm font-semibold text-text-secondary">Eligibility Screenings</h2>
                <Button size="sm" onClick={handleRescan} isLoading={rescanning} disabled={rescanning}>
                  Re-scan
                </Button>
              </div>
              {eligLoading ? (
                <div className="space-y-3">
                  <Skeleton className="h-10 w-full rounded-xl" />
                </div>
              ) : !eligibility || eligibility.screenings.length === 0 ? (
                <div className="flex flex-col items-center justify-center rounded-xl border border-border py-12 text-center">
                  <FlaskConical size={36} className="mb-3 text-text-tertiary" />
                  <p className="text-text-tertiary">No eligibility data</p>
                </div>
              ) : (
                <div className="space-y-3">
                  {eligibility.screenings.map((s, i) => (
                    <Card key={`${s.patient_hash}-${i}`} className="p-4">
                      <div className="flex items-center justify-between mb-2">
                        <span className="font-mono text-sm text-text-primary">{s.patient_hash.slice(0, 12)}...</span>
                        <Badge variant={s.status === 'eligible' ? 'success' : s.status === 'ineligible' ? 'error' : 'default'}>
                          {s.status}
                        </Badge>
                      </div>
                      {s.criteria_results && s.criteria_results.length > 0 && (
                        <div className="space-y-1 mt-2">
                          {s.criteria_results.map((c, j) => (
                            <div key={j} className="flex items-center gap-2 text-xs">
                              <span className={c.passed ? 'text-success' : 'text-error'}>
                                {c.passed ? '\u2713' : '\u2717'}
                              </span>
                              <span className="text-text-secondary">{c.criterion}</span>
                            </div>
                          ))}
                        </div>
                      )}
                    </Card>
                  ))}
                </div>
              )}
            </div>
          )}

          {tab === 'schedule' && (
            <div className="max-w-3xl space-y-4">
              <h2 className="text-sm font-semibold text-text-secondary">Scheduled Assessments</h2>
              {scheduleLoading ? (
                <div className="space-y-3">
                  <Skeleton className="h-10 w-full rounded-xl" />
                  <Skeleton className="h-10 w-full rounded-xl" />
                </div>
              ) : assessments.length === 0 ? (
                <div className="flex flex-col items-center justify-center rounded-xl border border-border py-12 text-center">
                  <CalendarDays size={36} className="mb-3 text-text-tertiary" />
                  <p className="text-text-tertiary">No scheduled assessments</p>
                </div>
              ) : (
                <div className="overflow-x-auto rounded-xl border border-border">
                  <table className="w-full text-sm">
                    <thead>
                      <tr className="border-b border-border bg-surface">
                        <th className="px-4 py-2 text-left font-medium text-text-secondary">Date</th>
                        <th className="px-4 py-2 text-left font-medium text-text-secondary">Patient</th>
                        <th className="px-4 py-2 text-left font-medium text-text-secondary">Status</th>
                        <th className="px-4 py-2 text-left font-medium text-text-secondary"></th>
                      </tr>
                    </thead>
                    <tbody>
                      {assessments.map((a) => (
                        <tr key={a.visit_id} className="border-b border-border last:border-0">
                          <td className="px-4 py-2 text-text-primary">{new Date(a.scheduled_at).toLocaleString()}</td>
                          <td className="px-4 py-2 font-mono text-text-primary">{a.patient_hash.slice(0, 12)}...</td>
                          <td className="px-4 py-2">
                            <Badge variant={a.status === 'completed' ? 'success' : a.status === 'pending' ? 'warning' : 'default'}>
                              {a.status}
                            </Badge>
                          </td>
                          <td className="px-4 py-2">
                            {a.status !== 'completed' && (
                              <Button
                                size="sm"
                                onClick={() => handleCompleteAssessment(a.visit_id)}
                                isLoading={completingIds.has(a.visit_id)}
                                disabled={completingIds.has(a.visit_id)}
                              >
                                Complete
                              </Button>
                            )}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </div>
          )}

          {tab === 'safety' && (
            <div className="max-w-3xl space-y-4">
              <h2 className="text-sm font-semibold text-text-secondary">Safety</h2>

              {safetyLoading ? (
                <div className="space-y-3">
                  <Skeleton className="h-10 w-full rounded-xl" />
                </div>
              ) : (
                <>
                  {safetyStatus && safetyStatus.triggered_rules.length > 0 && (
                    <Card className="p-4">
                      <h3 className="mb-2 text-sm font-medium text-text-primary">Stop Rules Triggered</h3>
                      <div className="space-y-2">
                        {safetyStatus.triggered_rules.map((r, i) => (
                          <div key={i} className="flex items-start gap-2 text-sm">
                            <Badge variant="error">{r.rule}</Badge>
                            <span className="text-text-secondary">{r.description}</span>
                          </div>
                        ))}
                      </div>
                    </Card>
                  )}

                  {observations.length === 0 ? (
                    <div className="flex flex-col items-center justify-center rounded-xl border border-border py-12 text-center">
                      <FlaskConical size={36} className="mb-3 text-text-tertiary" />
                      <p className="text-text-tertiary">No observations recorded</p>
                    </div>
                  ) : (
                    <div className="overflow-x-auto rounded-xl border border-border">
                      <table className="w-full text-sm">
                        <thead>
                          <tr className="border-b border-border bg-surface">
                            <th className="px-4 py-2 text-left font-medium text-text-secondary">Patient</th>
                            <th className="px-4 py-2 text-left font-medium text-text-secondary">Category</th>
                            <th className="px-4 py-2 text-left font-medium text-text-secondary">AE Grade</th>
                            <th className="px-4 py-2 text-left font-medium text-text-secondary">DLT</th>
                            <th className="px-4 py-2 text-left font-medium text-text-secondary">Date</th>
                            <th className="px-4 py-2 text-left font-medium text-text-secondary"></th>
                          </tr>
                        </thead>
                        <tbody>
                          {observations.map((o) => (
                            <tr key={o.observation_id} className="border-b border-border last:border-0">
                              <td className="px-4 py-2 font-mono text-text-primary">{o.patient_hash.slice(0, 12)}...</td>
                              <td className="px-4 py-2 text-text-secondary">{o.category}</td>
                              <td className={cn('px-4 py-2 font-medium', aeGradeColor(o.ae_grade))}>
                                {o.confirmed ? (
                                  o.ae_grade ?? '—'
                                ) : (
                                  <select
                                    className="rounded border border-border bg-surface px-2 py-1 text-sm"
                                    value={confirmingObs[o.observation_id]?.aeGrade ?? ''}
                                    onChange={(e) => updateObsForm(o.observation_id, 'aeGrade', e.target.value ? Number(e.target.value) : undefined as unknown as number)}
                                  >
                                    <option value="">—</option>
                                    <option value="1">1</option>
                                    <option value="2">2</option>
                                    <option value="3">3</option>
                                    <option value="4">4</option>
                                    <option value="5">5</option>
                                  </select>
                                )}
                              </td>
                              <td className="px-4 py-2">
                                {o.confirmed ? (
                                  o.is_dlt ? <Badge variant="error">DLT</Badge> : '—'
                                ) : (
                                  <input
                                    type="checkbox"
                                    checked={!!confirmingObs[o.observation_id]?.isDlt}
                                    onChange={(e) => updateObsForm(o.observation_id, 'isDlt', e.target.checked)}
                                    className="h-4 w-4"
                                  />
                                )}
                              </td>
                              <td className="px-4 py-2 text-text-tertiary">{new Date(o.created_at).toLocaleDateString()}</td>
                              <td className="px-4 py-2">
                                {o.confirmed ? (
                                  <span className="inline-flex items-center gap-1 text-success text-xs">
                                    <Check size={14} /> Confirmed
                                  </span>
                                ) : (
                                  <Button
                                    size="sm"
                                    onClick={() => handleConfirmObservation(o.observation_id)}
                                    isLoading={confirmingIds.has(o.observation_id)}
                                    disabled={confirmingIds.has(o.observation_id)}
                                  >
                                    Confirm
                                  </Button>
                                )}
                              </td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </div>
                  )}
                </>
              )}
            </div>
          )}

          {tab === 'protocol' && (
            <div className="max-w-2xl space-y-4">
              <div className="flex flex-col items-center justify-center rounded-xl border border-border py-16 text-center">
                <FlaskConical size={36} className="mb-3 text-text-tertiary" />
                <p className="text-text-tertiary">Protocol import/extract coming soon</p>
              </div>
            </div>
          )}
        </main>

        {showEnroll && (
          <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/30">
            <div className="w-full max-w-md rounded-xl border border-border bg-surface-elevated p-6 shadow-lg">
              <h2 className="mb-4 text-lg font-semibold text-text-primary">Enroll Patient</h2>
              {patientsLoading ? (
                <div className="space-y-3">
                  <Skeleton className="h-10 w-full rounded-xl" />
                  <Skeleton className="h-10 w-full rounded-xl" />
                </div>
              ) : patients.length === 0 ? (
                <div className="py-8 text-center">
                  <p className="text-text-tertiary">No patients available</p>
                </div>
              ) : (
                <div className="max-h-80 space-y-2 overflow-y-auto">
                  {patients.map((p) => (
                    <div
                      key={p.patient_hash}
                      className="flex items-center justify-between rounded-lg border border-border p-3"
                    >
                      <div>
                        <span className="font-mono text-sm text-text-primary">
                          {p.initials ? `${p.initials} — ` : ''}{p.patient_hash.slice(0, 12)}...
                        </span>
                      </div>
                      <Button
                        size="sm"
                        onClick={() => handleEnroll(p.patient_hash)}
                        disabled={enrollingHash === p.patient_hash}
                        isLoading={enrollingHash === p.patient_hash}
                      >
                        Enroll
                      </Button>
                    </div>
                  ))}
                </div>
              )}
              <div className="mt-4 flex justify-end">
                <Button variant="ghost" onClick={() => setShowEnroll(false)}>Cancel</Button>
              </div>
            </div>
          </div>
        )}
      </div>
    </AppShell>
  );
}
