import { useCallback, useEffect, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { NavLink, Outlet, useNavigate, useParams } from 'react-router-dom';
import { ArrowLeft, ChevronRight, Paperclip, Plus, Search, User } from 'lucide-react';
import { AppShell } from '@/components/layout/AppShell';
import { NewPatientDialog } from '@/components/NewPatientDialog';
import { SkillsBar } from '@/components/SkillsBar';
import { Alert, Button, Input, Card, Badge, Skeleton, Textarea } from '@/components/ui';
import { cn } from '@/lib/utils';
import { api, ApiError } from '@/lib/api-client';
import { useChatStore } from '@/stores/chat';
import type { MemoryFinding, MemoryProjection, Patient, PatientDetail } from '@/lib/types';

function PatientList({
  patients,
  selectedHash,
  onCreatePatient,
}: {
  patients: Patient[];
  selectedHash?: string;
  onCreatePatient: () => void;
}) {
  const { t } = useTranslation();
  const [query, setQuery] = useState('');

  const filtered = patients.filter((p) => (p.initials || p.patient_hash).toLowerCase().includes(query.toLowerCase()));

  return (
    <div className="flex h-full w-64 flex-col border-r border-border bg-surface">
      <div className="flex h-14 items-center justify-between border-b border-border px-3">
        <h2 className="font-semibold text-text-primary">{t('nav.patients')}</h2>
          <Button size="sm" variant="ghost" onClick={onCreatePatient}>
            <Plus size={16} />
          </Button>
      </div>
      <div className="p-3">
        <div className="relative">
          <Search className="absolute left-2.5 top-2.5 h-4 w-4 text-text-tertiary" />
          <Input
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder={t('common.search')}
            className="pl-9"
          />
        </div>
      </div>
      <ul className="flex-1 overflow-y-auto px-3">
        {filtered.map((p) => (
          <li key={p.patient_hash}>
            <NavLink
              to={`/app/patients/${p.patient_hash}`}
              className={cn(
                'flex items-center gap-3 rounded-lg px-3 py-2 transition-colors',
                selectedHash === p.patient_hash
                  ? 'bg-accent/10 text-accent'
                  : 'text-text-secondary hover:bg-surface-elevated hover:text-text-primary',
              )}
            >
              <div className="flex h-8 w-8 items-center justify-center rounded-full bg-surface-elevated">
                <User size={14} />
              </div>
              <div className="min-w-0 flex-1">
                <p className="truncate text-sm font-medium">{p.initials || p.patient_hash.slice(0, 8)}</p>
                <p className="text-xs text-text-tertiary">
                  {p.age_value != null ? t('common.yearsOld', { age: p.age_value }) : null}
                  {p.age_value != null && p.sex ? ' / ' : ''}
                  {p.sex || ''}
                </p>
              </div>
            </NavLink>
          </li>
        ))}
      </ul>
    </div>
  );
}

function PatientTabs({ hash, active }: { hash?: string; active: 'summary' | 'chat' | 'imaging' | 'labs' | 'memory' | 'report' }) {
  const { t } = useTranslation();
  const tabs = [
    { to: `/app/patients/${hash}`, label: t('patient.summary'), key: 'summary' as const },
    { to: `/app/patients/${hash}/chat`, label: t('patient.chat'), key: 'chat' as const },
    { to: `/app/patients/${hash}/imaging`, label: 'Imaging', key: 'imaging' as const },
    { to: `/app/patients/${hash}/labs`, label: 'Labs', key: 'labs' as const },
    { to: `/app/patients/${hash}/memory`, label: 'Memory', key: 'memory' as const },
    { to: `/app/patients/${hash}/report`, label: 'Report', key: 'report' as const },
  ];

  return (
    <nav className="flex gap-1 border-b border-border px-6">
      {tabs.map((tab) => (
        <NavLink
          key={tab.key}
          to={tab.to}
          end
          className={cn(
            'border-b-2 px-3 py-3 text-sm font-medium transition-colors',
            active === tab.key
              ? 'border-accent text-accent'
              : 'border-transparent text-text-secondary hover:text-text-primary',
          )}
        >
          {tab.label}
        </NavLink>
      ))}
    </nav>
  );
}

export function PatientsLayout() {
  const { hash } = useParams<{ hash?: string }>();
  const { t } = useTranslation();
  const navigate = useNavigate();
  const [patients, setPatients] = useState<Patient[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [newPatientOpen, setNewPatientOpen] = useState(false);

  const loadPatients = useCallback(() => {
    setLoading(true);
    setError(null);
    api.listPatients()
      .then(setPatients)
      .catch((err) => setError(err instanceof ApiError ? err.messageText : t('patient.loadPatientsError')))
      .finally(() => setLoading(false));
  }, [t]);

  useEffect(() => {
    loadPatients();
  }, [loadPatients]);

  return (
    <AppShell>
      <div className="flex h-full">
        {loading ? (
          <div className="flex h-full w-64 flex-col border-r border-border bg-surface p-3 gap-3">
            <Skeleton className="h-10 w-full" />
            <Skeleton className="h-10 w-full" />
            <Skeleton className="h-10 w-full" />
          </div>
        ) : (
          <PatientList patients={patients} selectedHash={hash} onCreatePatient={() => setNewPatientOpen(true)} />
        )}
        <div className="flex min-w-0 flex-1 flex-col">
          {error && (
            <div className="p-3">
              <Alert variant="error">{error}</Alert>
            </div>
          )}
          <Outlet />
        </div>
      </div>
      <NewPatientDialog
        open={newPatientOpen}
        onClose={() => setNewPatientOpen(false)}
        onCreated={(patientHash) => { loadPatients(); navigate(`/app/patients/${patientHash}`); }}
      />
    </AppShell>
  );
}

export function PatientSummaryPage() {
  const { t } = useTranslation();
  const { hash } = useParams<{ hash?: string }>();
  const navigate = useNavigate();
  const [detail, setDetail] = useState<PatientDetail | null>(null);
  const [projection, setProjection] = useState<MemoryProjection | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!hash) return;
    setLoading(true);
    setError(null);
    Promise.all([
      api.getPatientDetail(hash).catch(() => null),
      api.getMemoryProjection(hash).catch(() => null),
    ])
      .then(([d, p]) => {
        setDetail(d);
        setProjection(p);
      })
      .catch((err) => setError(err instanceof ApiError ? err.messageText : String(err)))
      .finally(() => setLoading(false));
  }, [hash]);

  if (!hash) {
    return (
      <div className="flex h-full flex-col items-center justify-center p-6 text-center">
        <div className="mb-4 flex h-16 w-16 items-center justify-center rounded-full bg-surface-elevated">
          <User size={28} className="text-text-tertiary" />
        </div>
        <h2 className="text-lg font-semibold text-text-primary">{t('patient.noPatientSelected')}</h2>
        <p className="text-text-secondary">{t('patient.selectPatient')}</p>
      </div>
    );
  }

  if (loading) {
    return (
      <div className="flex h-full flex-col overflow-y-auto">
        <div className="flex h-14 items-center border-b border-border bg-surface px-6 gap-3">
          <Skeleton className="h-5 w-24" />
          <Skeleton className="h-5 w-16" />
        </div>
        <PatientTabs hash={hash} active="summary" />
        <div className="space-y-6 p-6">
          <Skeleton className="h-32 w-full rounded-xl" />
          <Skeleton className="h-20 w-full rounded-xl" />
        </div>
      </div>
    );
  }

  if (error) {
    return (
      <div className="flex h-full flex-col">
        <PatientTabs hash={hash} active="summary" />
        <div className="flex flex-1 items-center justify-center">
          <Alert variant="error">{error}</Alert>
        </div>
      </div>
    );
  }

  const findings = projection?.findings || [];
  const meds = projection?.medications || [];
  const timeline = projection?.timeline || [];

  return (
    <div className="flex h-full flex-col overflow-y-auto">
      <nav className="flex items-center gap-1 border-b border-border bg-surface px-6 py-2 text-sm text-text-secondary">
        <NavLink to="/app/patients" className="hover:text-text-primary">Patients</NavLink>
        <ChevronRight size={14} className="text-text-tertiary" />
        <span className="text-text-primary">{detail?.initials || hash}</span>
      </nav>
      <div className="flex h-14 items-center justify-between border-b border-border bg-surface px-6">
        <div className="flex items-center gap-3">
          <Button variant="ghost" size="sm" onClick={() => navigate('/app/patients')}>
            <ArrowLeft size={16} />
          </Button>
          <h1 className="font-semibold text-text-primary">{detail?.initials || hash}</h1>
          {(detail?.age_value != null || detail?.sex) && (
            <Badge>
              {detail.age_value != null ? t('common.yearsOld', { age: detail.age_value }) : ''}
              {detail.age_value != null && detail.sex ? ' / ' : ''}
              {detail.sex || ''}
            </Badge>
          )}
        </div>
        <Button size="sm" onClick={() => navigate(`/app/patients/${hash}/chat`)}>{t('patient.chat')}</Button>
      </div>
      <PatientTabs hash={hash} active="summary" />
      <main className="space-y-6 p-6">
        <Card className="p-6">
          <h3 className="mb-3 font-semibold text-text-primary">{t('patient.clinicalSummary')}</h3>
          {findings.length === 0 && meds.length === 0 ? (
            <p className="text-sm text-text-secondary">{t('patient.noStructuredSummary')}</p>
          ) : (
            <div className="space-y-4">
              {findings.length > 0 && (
                <div>
                  <h4 className="mb-2 text-xs font-semibold uppercase text-text-tertiary">
                    Findings ({findings.length})
                  </h4>
                  <ul className="space-y-2">
                    {findings.slice(0, 10).map((f) => (
                      <FindingItem key={f.node_id} finding={f} />
                    ))}
                  </ul>
                </div>
              )}
              {meds.length > 0 && (
                <div>
                  <h4 className="mb-2 text-xs font-semibold uppercase text-text-tertiary">
                    Medications ({meds.length})
                  </h4>
                  <ul className="space-y-2">
                    {meds.slice(0, 10).map((m) => (
                      <FindingItem key={m.node_id} finding={m} />
                    ))}
                  </ul>
                </div>
              )}
            </div>
          )}
        </Card>

        <Card className="p-6">
          <h3 className="mb-3 font-semibold text-text-primary">{t('patient.recentActivity')}</h3>
          {timeline.length === 0 ? (
            <p className="text-sm text-text-secondary">
              {t('patient.lastVisit')}: {detail?.last_seen_at || t('patient.unavailable')}
            </p>
          ) : (
            <ul className="space-y-3">
              {timeline.slice(0, 15).map((ev, i) => (
                <li key={ev.event_id || i} className="flex gap-3 text-sm">
                  <span className="shrink-0 text-xs text-text-tertiary">
                    {new Date(ev.timestamp).toLocaleDateString()}
                  </span>
                  <span className="text-text-secondary">{ev.content}</span>
                </li>
              ))}
            </ul>
          )}
        </Card>
      </main>
    </div>
  );
}

function FindingItem({ finding }: { finding: MemoryFinding }) {
  return (
    <li className="rounded-lg border border-border bg-surface p-3 text-sm">
      <p className="text-text-primary">{finding.content}</p>
      <p className="mt-1 text-xs text-text-tertiary">{finding.node_type} · {finding.node_id.slice(0, 8)}</p>
    </li>
  );
}

export function PatientChatPage() {
  const { t } = useTranslation();
  const { hash } = useParams<{ hash: string }>();
  const store = useChatStore();
  const sessionId = hash ? `patient-${hash}` : '';
  const session = store.sessions[sessionId];
  const [input, setInput] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [uploadingFile, setUploadingFile] = useState(false);
  const [attachedFiles, setAttachedFiles] = useState<Array<{name: string; fileId: string}>>([]);
  const [activeSkills, setActiveSkills] = useState<string[]>([]);
  const bottomRef = useRef<HTMLDivElement | null>(null);
  const fileInputRef = useRef<HTMLInputElement | null>(null);

  useEffect(() => {
    if (!sessionId || !hash) return;
    const existing = store.sessions[sessionId]?.messages?.length;
    if (existing) return;
    api.getMessages(sessionId, 50).then((r) => {
      const msgs = r.messages.map((m) => ({
        id: crypto.randomUUID(),
        role: m.role,
        text: m.content,
      }));
      if (msgs.length > 0) store.setMessages(sessionId, msgs);
    }).catch(() => {});
  }, [sessionId, hash, store]);

  useEffect(() => {
    const el = bottomRef.current;
    if (!el) return;
    const parent = el.parentElement;
    if (!parent) return;
    const nearBottom = parent.scrollHeight - parent.scrollTop - parent.clientHeight < 150;
    if (nearBottom) el.scrollIntoView({ behavior: 'smooth' });
  }, [session?.messages]);

  const handleSend = async () => {
    if (!input.trim() || !sessionId || session?.loading) return;
    const text = input.trim();
    setInput('');
    setError(null);
    await store.sendMessage(sessionId, {
      text,
      sessionId,
      patientHash: hash || null,
      attachments: attachedFiles.map((a) => ({ name: a.name, file_id: a.fileId })),
      skills: activeSkills,
    });
  };

  const handleStop = () => store.stopStream(sessionId);

  const toggleSkill = (name: string) => {
    setActiveSkills((prev) => prev.includes(name) ? prev.filter((s) => s !== name) : [...prev, name]);
  };

  const handleFile = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const f = e.target.files?.[0];
    if (!f) return;
    setUploadingFile(true);
    try {
      const result = await api.uploadFile(f, hash || undefined);
      setAttachedFiles((prev) => [...prev, { name: result.name, fileId: result.file_id }]);
    } catch (err) {
      // silently fail
    } finally {
      setUploadingFile(false);
    }
  };

  const handlePaste = async (e: React.ClipboardEvent<HTMLTextAreaElement>) => {
    const items = e.clipboardData?.items;
    if (!items) return;
    for (const item of Array.from(items)) {
      if (item.kind === 'file') {
        e.preventDefault();
        const file = item.getAsFile();
        if (!file) continue;
        setUploadingFile(true);
        try {
          const result = await api.uploadFile(file, hash || undefined);
          setAttachedFiles((prev) => [...prev, { name: result.name, fileId: result.file_id }]);
        } catch { /* ignore */ }
        finally { setUploadingFile(false); }
      }
    }
  };

  const handleKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); handleSend(); }
  };

  if (!hash) {
    return (
      <div className="flex h-full items-center justify-center text-text-tertiary">
        <p>No patient selected</p>
      </div>
    );
  }

  const messages = session?.messages || [];

  return (
    <div className="flex h-full flex-col">
      <PatientTabs hash={hash} active="chat" />
      {messages.length === 0 && (
        <div className="flex flex-1 items-center justify-center px-6 text-center">
          <div>
            <p className="text-lg text-text-tertiary">{t('chat.startConversation')}</p>
            <p className="text-sm text-text-tertiary">{t('chat.contextHint')}</p>
          </div>
        </div>
      )}
      <main className="flex-1 overflow-y-auto px-4 py-6">
        <div className="mx-auto max-w-3xl space-y-4">
          {messages.map((m) => (
            <div key={m.id} className={`flex ${m.role === 'user' ? 'justify-end' : 'justify-start'}`}>
              <div className={`max-w-[85%] rounded-2xl px-4 py-3 text-sm leading-relaxed ${
                m.role === 'user'
                  ? 'bg-accent text-white'
                  : 'border border-border bg-surface-elevated text-text-primary shadow-sm'
              }`}>
                {m.tier && <div className="mb-1 text-xs opacity-70">Tier: {m.tier}</div>}
                {m.reasoning && (
                  <details className="mb-2">
                    <summary className="cursor-pointer text-xs text-text-tertiary">{t('chat.reasoning')}</summary>
                    <p className="mt-1 whitespace-pre-wrap text-xs text-text-tertiary">{m.reasoning}</p>
                  </details>
                )}
                {m.citations && m.citations.length > 0 && (
                  <div className="mt-2 flex flex-wrap gap-1">
                    {m.citations.map((c, i) => (
                      <span key={i} className="inline-flex rounded-full bg-surface px-2 py-0.5 text-xs text-text-tertiary border border-border">
                        {c.source ? `[${c.source}] ` : ''}{c.text.slice(0, 60)}
                      </span>
                    ))}
                  </div>
                )}
                {m.text || (m.isStreaming ? (
                  <span role="status" aria-label={t('chat.streaming')} className="animate-pulse">●</span>
                ) : null)}
              </div>
            </div>
          ))}
          <div ref={bottomRef} />
        </div>
      </main>
      {error && (
        <div className="mx-auto w-full max-w-3xl px-4 pb-2">
          <Alert variant="error">{error}</Alert>
        </div>
      )}
      <footer className="border-t border-border bg-surface px-4 py-4">
        <div className="mx-auto flex max-w-3xl flex-col gap-2">
          <SkillsBar active={activeSkills} onToggle={toggleSkill} />
          {attachedFiles.length > 0 && (
            <div className="flex gap-2 flex-wrap">
              {attachedFiles.map((f) => (
                <Badge key={f.fileId} variant="default">{f.name}</Badge>
              ))}
            </div>
          )}
          <div className="flex gap-2">
            <input
              ref={fileInputRef}
              type="file"
              onChange={handleFile}
              className="hidden"
              disabled={uploadingFile}
            />
            <Button
              variant="ghost"
              size="sm"
              onClick={() => fileInputRef.current?.click()}
              disabled={session?.loading || uploadingFile}
              isLoading={uploadingFile}
              className="shrink-0"
            >
              <Paperclip size={16} />
            </Button>
            <Textarea
              value={input}
              onChange={(e) => setInput(e.target.value)}
              onKeyDown={handleKeyDown}
              onPaste={handlePaste}
              placeholder={t('chat.placeholder')}
              disabled={session?.loading}
              rows={1}
              className="min-h-0 flex-1 resize-none py-3"
              style={{ maxHeight: '160px' }}
            />
            {session?.loading ? (
              <Button onClick={handleStop} variant="secondary" className="shrink-0">{t('common.stop')}</Button>
            ) : (
              <Button onClick={handleSend} disabled={!input.trim()} className="shrink-0">{t('common.send')}</Button>
            )}
          </div>
        </div>
      </footer>
    </div>
  );
}


