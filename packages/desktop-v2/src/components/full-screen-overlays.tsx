/**
 * Full-screen overlays (UX v2 §6):
 * - PractitionerHasLearnedView — "Nexus has learned" panel
 * - SettingsDataView           — Backup & Export
 *
 * Both render above the canvas; close via Esc or the X button.
 */

import { useEffect, useState } from 'react';
import * as Dialog from '@radix-ui/react-dialog';
import {
  X, CheckCircle, XCircle, Clock, Eye, AlertTriangle,
  Download, RefreshCw, Folder, Upload, Key, Database,
} from 'lucide-react';
import { Button, Chip, Card, Input } from './ui';
import { api } from '../lib/api-client';
import { useAppState } from '../store';
import { cn } from '../lib/util';
import type { LlmStatus, PractitionerCandidate } from '../lib/types';

/* ───────────── PractitionerHasLearnedView ───────────── */

export function PractitionerHasLearnedView() {
  const open = useAppState((s) => s.practitionerOverlayOpen);
  const close = useAppState((s) => s.closePractitionerOverlay);
  const showToast = useAppState((s) => s.showToast);
  const [candidates, setCandidates] = useState<PractitionerCandidate[]>([]);
  const [active, setActive] = useState<unknown[]>([]);
  const [loading, setLoading] = useState(false);

  async function refresh() {
    setLoading(true);
    try {
      const [cands, _activeResp] = await Promise.all([
        api.listPractitionerCandidates(),
        fetch('/api/v1/memory/practitioner/active', {
          headers: { Authorization: `Bearer ${api.getToken()}` },
        }).then((r) => r.json()).catch(() => ({ active: [] })),
      ]);
      setCandidates(cands);
      setActive(_activeResp.active || []);
    } finally { setLoading(false); }
  }

  useEffect(() => {
    if (open) refresh();
  }, [open]);

  async function confirm(c: PractitionerCandidate) {
    try {
      await api.confirmPractitionerFact(c.factKind, c.patternKey);
      showToast(`Confirmed "${c.factKind}/${c.patternKey}"`, 'success');
      refresh();
    } catch (e) {
      showToast(`Confirm failed: ${e}`, 'error');
    }
  }

  async function reject(c: PractitionerCandidate) {
    try {
      await api.rejectPractitionerFact(c.factKind, c.patternKey);
      showToast(`Rejected "${c.factKind}/${c.patternKey}"`, 'info');
      refresh();
    } catch (e) {
      showToast(`Reject failed: ${e}`, 'error');
    }
  }

  return (
    <Dialog.Root open={open} onOpenChange={(o) => !o && close()}>
      <Dialog.Portal>
        <Dialog.Overlay className="fixed inset-0 z-40 bg-black/50" />
        <Dialog.Content
          className={cn(
            'fixed inset-x-0 top-0 z-50 mx-auto my-8 max-w-3xl',
            'rounded-lg border border-border-strong bg-surface shadow-2xl',
            'max-h-[85vh] overflow-y-auto focus:outline-none',
          )}
        >
          <div className="flex items-center justify-between border-b border-border px-6 py-4">
            <Dialog.Title asChild>
              <h1 className="font-display text-section">
                Nexus has learned
                {candidates.length > 0 && (
                  <span className="ml-2 text-caption text-text-tertiary">
                    {candidates.length} pending
                  </span>
                )}
              </h1>
            </Dialog.Title>
            <Dialog.Close className="rounded-sm p-1 text-text-tertiary hover:bg-accent-subtle">
              <X size={16} />
            </Dialog.Close>
          </div>

          <div className="px-6 py-5 text-body text-text-secondary">
            These are patterns Nexus has noticed in your cases. Confirm the
            ones you want Nexus to start using; reject the rest. You can
            always change your mind later.
          </div>

          {loading && (
            <p className="px-6 py-3 text-caption text-text-tertiary">
              Loading candidates…
            </p>
          )}

          {!loading && candidates.length === 0 && (
            <div className="px-6 py-12 text-center text-caption text-text-tertiary">
              No new patterns yet. Nexus surfaces a candidate after observing
              the same behaviour across multiple of your patients.
            </div>
          )}

          {!loading && candidates.map((c) => (
            <PractitionerCandidateCard
              key={`${c.factKind}/${c.patternKey}`}
              candidate={c}
              onConfirm={() => confirm(c)}
              onReject={() => reject(c)}
            />
          ))}

          {active.length > 0 && (
            <div className="border-t border-border px-6 py-4">
              <p className="text-caption text-text-tertiary">
                Active patterns: {active.length}
              </p>
            </div>
          )}
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}

function PractitionerCandidateCard({
  candidate, onConfirm, onReject,
}: {
  candidate: PractitionerCandidate;
  onConfirm: () => void;
  onReject: () => void;
}) {
  const summary =
    (candidate.patternValue as Record<string, unknown>)?.evidence_sample
    || (candidate.patternValue as Record<string, unknown>)?.summary
    || candidate.patternKey;
  return (
    <div className="border-t border-border px-6 py-5">
      <div className="mb-2 flex items-center gap-2 text-caption text-text-tertiary">
        <Chip variant="tinted">{candidate.factKind.toUpperCase()}</Chip>
        <span>
          {candidate.observedCount} cases · {candidate.distinctPatientCount} patients
        </span>
        <span>·</span>
        <span>confidence {(candidate.confidence * 100).toFixed(0)}%</span>
      </div>
      <p className="mb-4 text-body text-text-primary">{String(summary)}</p>
      <div className="flex items-center gap-2">
        <Button variant="primary" onClick={onConfirm}>
          <CheckCircle size={14} /> confirm
        </Button>
        <Button variant="subtle" onClick={onReject}>
          <XCircle size={14} /> reject
        </Button>
        <Button variant="ghost">
          <Clock size={14} /> ask me later
        </Button>
        <Button variant="ghost">
          <Eye size={14} /> see cases
        </Button>
      </div>
    </div>
  );
}

/* ───────────── SettingsDataView ───────────── */

const SCHEDULE_STORAGE_KEY = 'nexus.data.scheduleMonthly';

function readScheduleMonthly(): boolean {
  try { return localStorage.getItem(SCHEDULE_STORAGE_KEY) === '1'; } catch { return false; }
}
function writeScheduleMonthly(v: boolean) {
  try {
    if (v) localStorage.setItem(SCHEDULE_STORAGE_KEY, '1');
    else   localStorage.removeItem(SCHEDULE_STORAGE_KEY);
  } catch { /* ignore */ }
}

function humanBytes(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  if (n < 1024 * 1024 * 1024) return `${(n / 1024 / 1024).toFixed(1)} MB`;
  return `${(n / 1024 / 1024 / 1024).toFixed(2)} GB`;
}

async function openInOsShell(path: string): Promise<boolean> {
  // Tauri 2's @tauri-apps/plugin-shell exposes `open(path)`. We import
  // dynamically so this file still loads when running plain `pnpm dev`
  // outside the Tauri shell (the import would throw on web).
  try {
    const mod = await import('@tauri-apps/plugin-shell');
    if (mod && typeof mod.open === 'function') {
      await mod.open(path);
      return true;
    }
  } catch {
    /* not in Tauri runtime — fall through */
  }
  return false;
}

interface LastExport {
  path: string;
  bytes: number;
  createdAt: number;
  counts: Record<string, number>;
}

type SettingsTab = 'data' | 'llm';

export function SettingsDataView() {
  const open = useAppState((s) => s.settingsOverlayOpen);
  const close = useAppState((s) => s.closeSettingsOverlay);
  const showToast = useAppState((s) => s.showToast);

  const [tab, setTab] = useState<SettingsTab>('llm');
  const [archivePath, setArchivePath] = useState<string | null>(null);
  const [scheduleMonthly, setScheduleMonthly] = useState(readScheduleMonthly);
  const [exporting, setExporting] = useState(false);
  const [lastExport, setLastExport] = useState<LastExport | null>(null);

  // Resolve the archive folder once the overlay opens. We keep this
  // best-effort; failure (e.g. unauthenticated) just leaves the UI on
  // the canonical "~/Documents/Nexus Archive/" string.
  useEffect(() => {
    if (!open) return;
    let cancelled = false;
    api.archiveFolder().then(
      (p) => { if (!cancelled) setArchivePath(p); },
      () => { /* leave default */ },
    );
    return () => { cancelled = true; };
  }, [open]);

  const displayPath = archivePath ?? '~/Documents/Nexus Archive/';

  const handleOpenArchive = async () => {
    const ok = await openInOsShell(displayPath);
    if (!ok) {
      // Plain-browser dev — copy the path so the medic can paste into
      // Finder/Explorer themselves.
      try { await navigator.clipboard.writeText(displayPath); }
      catch { /* ignore */ }
      showToast(`Path copied: ${displayPath}`, 'info');
    }
  };

  const handleExportNow = async () => {
    if (exporting) return;
    setExporting(true);
    try {
      const r = await api.exportBundle();
      setLastExport({
        path: r.bundlePath, bytes: r.bytes,
        createdAt: r.createdAt, counts: r.counts,
      });
      const nodeCount = r.counts['clinical_graph_nodes'] ?? 0;
      const evtCount  = r.counts['twin_event_log'] ?? 0;
      showToast(
        `Exported ${humanBytes(r.bytes)} · ${evtCount} events, ${nodeCount} nodes`,
        'success',
      );
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e);
      // Special-case the 404 the backend raises when the user has no
      // EventLog yet — the medic just signed in and hasn't done anything.
      if (msg.includes('not found')) {
        showToast('Nothing to export yet — chat or import a study first.', 'info');
      } else {
        showToast(`Export failed: ${msg}`, 'error');
      }
    } finally {
      setExporting(false);
    }
  };

  const handleToggleSchedule = () => {
    const next = !scheduleMonthly;
    setScheduleMonthly(next);
    writeScheduleMonthly(next);
    showToast(
      next ? 'Monthly export scheduled (local setting)' : 'Monthly export disabled',
      'info',
    );
  };

  const handleRestoreLocal = () => {
    showToast(
      'Restore is a destructive replace — ships behind a Rev-19 confirm dialog (M3.3 finalize).',
      'info',
    );
  };
  const handleImportBundle = () => {
    showToast(
      'Bundle import endpoint lands with M3.3 finalize. Today the export ' +
      'side is one-way; restore uses the EventLog replay tool in /scripts.',
      'info',
    );
  };

  return (
    <Dialog.Root open={open} onOpenChange={(o) => !o && close()}>
      <Dialog.Portal>
        <Dialog.Overlay className="fixed inset-0 z-40 bg-black/50" />
        <Dialog.Content
          className={cn(
            'fixed inset-x-0 top-0 z-50 mx-auto my-8 max-w-3xl',
            'rounded-lg border border-border-strong bg-surface shadow-2xl',
            'max-h-[85vh] overflow-y-auto focus:outline-none',
          )}
        >
          <div className="flex items-center justify-between border-b border-border px-6 py-4">
            <Dialog.Title asChild>
              <h1 className="font-display text-section">
                Settings · {tab === 'llm' ? 'LLM' : 'Data'}
              </h1>
            </Dialog.Title>
            <Dialog.Close className="rounded-sm p-1 text-text-tertiary hover:bg-accent-subtle">
              <X size={16} />
            </Dialog.Close>
          </div>

          {/* Tab bar */}
          <div className="flex gap-1 border-b border-border px-4 pt-2">
            <button
              onClick={() => setTab('llm')}
              className={cn(
                'flex items-center gap-2 rounded-t-sm px-3 py-2 text-caption',
                tab === 'llm'
                  ? 'border-b-2 border-accent text-text-primary'
                  : 'text-text-secondary hover:text-text-primary',
              )}
            >
              <Key size={12} /> LLM
            </button>
            <button
              onClick={() => setTab('data')}
              className={cn(
                'flex items-center gap-2 rounded-t-sm px-3 py-2 text-caption',
                tab === 'data'
                  ? 'border-b-2 border-accent text-text-primary'
                  : 'text-text-secondary hover:text-text-primary',
              )}
            >
              <Database size={12} /> Data
            </button>
          </div>

          {tab === 'llm' && <LlmSettingsBody />}

          {tab === 'data' && (
          <>
          <div className="px-6 py-5 italic text-caption text-text-secondary">
            Your data is yours. The export format is open and documented.
            Nexus going away does not take your records with it.
          </div>

          {/* Automatic backups card */}
          <div className="px-6 pb-5">
            <h2 className="mb-3 text-caption font-medium uppercase tracking-wider text-text-tertiary">
              Automatic backups · local · always on
            </h2>
            <Card>
              <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-2 text-caption">
                <dt className="text-text-tertiary">Schedule</dt>
                <dd className="text-text-primary">daily ~03:00 local</dd>
                <dt className="text-text-tertiary">Retention</dt>
                <dd className="text-text-primary">30 daily · 12 weekly · 24 monthly</dd>
                <dt className="text-text-tertiary">Location</dt>
                <dd className="font-mono text-text-primary truncate" title={displayPath}>
                  {displayPath}
                </dd>
              </dl>
              <div className="mt-4 flex gap-2">
                <Button variant="subtle" onClick={handleOpenArchive}>
                  <Folder size={14} /> Open Archive folder
                </Button>
              </div>
            </Card>
          </div>

          {/* Export card */}
          <div className="px-6 pb-5">
            <h2 className="mb-3 text-caption font-medium uppercase tracking-wider text-text-tertiary">
              Export all my data
            </h2>
            <Card>
              <p className="mb-3 text-body text-text-secondary">
                Builds a self-contained zip with the twin EventLog +
                manifest. The EventLog is the canonical, append-only source
                — every projection is rebuildable by replay. FHIR R5 and a
                SQL dump derivative land in M3.3 finalize.
              </p>
              <div className="flex flex-wrap items-center gap-2">
                <Button
                  variant="primary"
                  disabled={exporting}
                  onClick={handleExportNow}
                >
                  <Download size={14} /> {exporting ? 'Exporting…' : 'Export now…'}
                </Button>
                <Button
                  variant={scheduleMonthly ? 'primary' : 'subtle'}
                  onClick={handleToggleSchedule}
                >
                  <RefreshCw size={14} />
                  {scheduleMonthly ? 'Monthly · on' : 'Schedule monthly…'}
                </Button>
              </div>
              {lastExport && (
                <div className="mt-4 rounded-sm border border-confirmed/40 bg-confirmed/5 px-3 py-2 text-caption">
                  <div className="text-confirmed">
                    Last export · {humanBytes(lastExport.bytes)} ·
                    {' '}{new Date(lastExport.createdAt * 1000).toLocaleString()}
                  </div>
                  <div className="mt-1 flex items-center gap-3 text-text-secondary">
                    <span className="truncate font-mono" title={lastExport.path}>
                      {lastExport.path}
                    </span>
                    <button
                      onClick={() => openInOsShell(lastExport.path)}
                      className="rounded-sm border border-border px-2 py-0.5 hover:bg-accent-subtle"
                    >
                      reveal
                    </button>
                  </div>
                </div>
              )}
            </Card>
          </div>

          {/* Restore card */}
          <div className="px-6 pb-5">
            <h2 className="mb-3 text-caption font-medium uppercase tracking-wider text-text-tertiary">
              Restore from backup
            </h2>
            <Card>
              <p className="mb-3 text-body text-text-secondary">
                Per Rev-19 mitigation: restore is a destructive replace.
                Current state is itself snapshotted before the overwrite so
                the operation is reversible.
              </p>
              <div className="flex gap-2">
                <Button variant="subtle" onClick={handleRestoreLocal}>
                  <Upload size={14} /> Restore local snapshot…
                </Button>
                <Button variant="subtle" onClick={handleImportBundle}>
                  <Upload size={14} /> Import from archive bundle…
                </Button>
              </div>
            </Card>
          </div>

          {/* Cloud sync card */}
          <div className="px-6 pb-6">
            <h2 className="mb-3 text-caption font-medium uppercase tracking-wider text-text-tertiary">
              Cloud sync · optional
            </h2>
            <Card>
              <div className="flex items-center gap-3 text-caption text-text-tertiary">
                <AlertTriangle size={14} className="text-caution" />
                <span>Not configured · ships in D3 (post-v1)</span>
              </div>
            </Card>
          </div>
          </>
          )}
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}

/* ───────────── Settings · LLM body ───────────── */

const DEFAULT_MODEL_FOR: Record<'gemini' | 'openai' | 'anthropic' | 'kimi', string> = {
  gemini:    'gemini-2.5-flash',
  openai:    'gpt-4o',
  anthropic: 'claude-sonnet-4-20250514',
  kimi:      'kimi-k2.7-code',
};

function LlmSettingsBody() {
  const showToast        = useAppState((s) => s.showToast);
  const refreshLlmStatus = useAppState((s) => s.refreshLlmStatus);
  const [status, setStatus] = useState<LlmStatus | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  // Form state — initialised from status. Keys are write-only inputs;
  // the server never returns the secret value.
  const [provider, setProvider] = useState<'gemini' | 'openai' | 'anthropic' | 'kimi'>('gemini');
  const [model, setModel]       = useState<string>('');
  const [geminiKey,    setGeminiKey]    = useState('');
  const [openaiKey,    setOpenaiKey]    = useState('');
  const [anthropicKey, setAnthropicKey] = useState('');
  const [kimiKey,      setKimiKey]      = useState('');

  // ── Test-Key state (F12) ─────────────────────────────────────────
  // Triggers a real live call against the in-process active provider
  // key. Lets the medic answer "is my saved key actually accepted by
  // Google/OpenAI/Anthropic?" without having to start a chat and
  // wait for the error path.
  const [testing, setTesting] = useState(false);
  const [testResult, setTestResult] = useState<Awaited<
    ReturnType<typeof api.testLlmKey>
  > | null>(null);

  const runTest = async () => {
    if (testing) return;
    setTesting(true);
    setTestResult(null);
    try {
      const r = await api.testLlmKey();
      setTestResult(r);
    } catch (e) {
      // Endpoint missing (stale binary) or transport failure —
      // synthesise an "other" result so the chip still renders with
      // an actionable message.
      setTestResult({
        ok: false,
        provider: provider,
        model: model,
        error: e instanceof Error ? e.message : String(e),
        diagnosis: 'other',
      });
    } finally {
      setTesting(false);
    }
  };

  const refresh = async () => {
    try {
      const s = await api.getLlmSettings();
      setStatus(s);
      setProvider(s.provider);
      setModel(s.model);
      setError(null);
    } catch (e) {
      // Even when the GET probe fails (backend down, or stale binary
      // missing /api/v1/settings/llm), we still want to show the form
      // so the medic can enter keys. We synthesise a "nothing known"
      // status and surface the error as a non-blocking banner above
      // the form — the user can still type a key + click Save, and we
      // detect on Save whether the PUT endpoint exists.
      setError(e instanceof Error ? e.message : String(e));
      setStatus({
        provider:        'gemini',
        model:           'gemini-2.5-flash',
        envFilePath:     '~/Library/Application Support/RuneProtocol/.env',
        envFileExists:   false,
        hasGeminiKey:    false,
        hasOpenaiKey:    false,
        hasAnthropicKey: false,
        hasKimiKey:      false,
        advisory: null,
        activeKeySource: 'none',
        activeKeyPreview: '',
        activeKeyLength:  0,
      });
      setProvider('gemini');
      setModel('gemini-2.5-flash');
    }
  };

  useEffect(() => { refresh(); }, []);

  const save = async (override?: {
    provider: 'gemini' | 'openai' | 'anthropic' | 'kimi';
    model?: string;
  }) => {
    if (saving) return;
    setSaving(true);
    const prov = override?.provider ?? provider;
    const mod  = override ? (override.model || DEFAULT_MODEL_FOR[prov])
                          : (model || DEFAULT_MODEL_FOR[provider]);
    if (override) { setProvider(prov); setModel(mod); }
    try {
      const r = await api.putLlmSettings({
        provider: prov,
        model: mod,
        geminiApiKey:    geminiKey || undefined,
        openaiApiKey:    openaiKey || undefined,
        anthropicApiKey: anthropicKey || undefined,
        kimiApiKey:      kimiKey || undefined,
      });
      setStatus(r.status);
      // Clear key inputs after a successful write — they're on disk
      // now, no reason to keep the cleartext in the DOM.
      setGeminiKey('');
      setOpenaiKey('');
      setAnthropicKey('');
      setKimiKey('');
      // Refresh the global llmStatus so the startup reminder banner
      // disappears the instant the key is saved.
      refreshLlmStatus();
      // If we wrote via the Tauri IPC fallback (backend endpoint not
      // available), the running sidecar's in-memory config is still
      // stale. Auto-restart so the next chat picks up the new key.
      if (r.viaFallback) {
        showToast('Saved to .env — restarting backend so the key takes effect…', 'info');
        try {
          await api.restartSidecar();
          // Give the sidecar a moment to come back up before re-probe.
          await new Promise((res) => setTimeout(res, 1500));
          refreshLlmStatus();
          showToast('Backend restarted — chat should work now.', 'success');
        } catch {
          showToast(
            "Saved, but couldn't auto-restart the backend. Quit and relaunch Nexus to apply.",
            'info',
          );
        }
      } else {
        showToast(
          `Saved ${r.writtenKeys.length} setting${r.writtenKeys.length === 1 ? '' : 's'}`,
          'success',
        );
      }
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e);
      showToast(`Save failed: ${msg}`, 'error');
    } finally {
      setSaving(false);
    }
  };

  if (!status) {
    return (
      <div className="px-6 py-5 text-caption text-text-tertiary">
        Loading…
      </div>
    );
  }

  const keyChip = (have: boolean) =>
    have
      ? <Chip variant="confirmed">on file</Chip>
      : <Chip variant="neutral">not set</Chip>;

  /** Right side of each API-key row: key presence + active-provider
   *  indicator or a one-click "use this provider" switch. */
  const keyRowRight = (
    p: 'gemini' | 'openai' | 'anthropic' | 'kimi',
    have: boolean,
  ) => (
    <span className="flex items-center gap-2">
      {status?.provider === p ? (
        <Chip variant="confirmed">✓ 使用中</Chip>
      ) : have ? (
        <button
          onClick={() => save({ provider: p })}
          disabled={saving}
          className="rounded-sm border border-accent/50 px-2 py-0.5 text-caption text-accent hover:bg-accent-subtle disabled:opacity-50"
        >
          设为当前
        </button>
      ) : null}
      {keyChip(have)}
    </span>
  );

  return (
    <>
      <div className="px-6 py-5 italic text-caption text-text-secondary">
        Keys are stored at <span className="font-mono not-italic">{status.envFilePath}</span>
        {' '}— the same <span className="font-mono not-italic">.env</span> v1 reads from. The Tauri
        sidecar exports every entry into the backend's environment at
        launch (see <span className="font-mono not-italic">src-tauri/src/lib.rs::load_user_env</span>).
        Saving here writes the file and updates the running process —
        no restart needed.
      </div>

      {status.advisory && (
        <div className="mx-6 mb-4 flex items-center gap-2 rounded-md border border-caution/40 bg-caution/5 px-3 py-2 text-caption text-caution">
          <AlertTriangle size={14} />
          <span>{status.advisory}</span>
        </div>
      )}

      {error && (
        <div className="mx-6 mb-4 rounded-md border border-retract/40 bg-retract/5 px-3 py-2 text-caption text-retract">
          Backend probe failed: {error}. You can still enter keys below — Save
          will tell you whether the server accepted the write.
        </div>
      )}

      {/* Provider + model */}
      <div className="px-6 pb-5">
        <h2 className="mb-3 text-caption font-medium uppercase tracking-wider text-text-tertiary">
          Provider（当前使用的模型）
        </h2>
        <Card>
          <div className="mb-3 flex gap-2">
            {(['gemini', 'openai', 'anthropic', 'kimi'] as const).map((p) => {
              const hasKey =
                (p === 'gemini'    && status.hasGeminiKey)    ||
                (p === 'openai'    && status.hasOpenaiKey)    ||
                (p === 'anthropic' && status.hasAnthropicKey) ||
                (p === 'kimi'      && status.hasKimiKey);
              const isActive = status.provider === p;
              return (
                <button
                  key={p}
                  onClick={() => {
                    setProvider(p);
                    if (!model || Object.values(DEFAULT_MODEL_FOR).includes(model)) {
                      setModel(DEFAULT_MODEL_FOR[p]);
                    }
                  }}
                  className={cn(
                    'flex items-center gap-1.5 rounded-sm border px-3 py-1.5 text-caption capitalize',
                    provider === p
                      ? 'border-accent bg-accent-subtle text-accent'
                      : 'border-border text-text-secondary hover:border-border-strong',
                  )}
                  title={hasKey ? 'API key 已配置' : '未配置 API key'}
                >
                  {/* key-status dot: green = key on file, gray = not set */}
                  <span
                    className={cn(
                      'inline-block h-1.5 w-1.5 rounded-full',
                      hasKey ? 'bg-confirmed' : 'bg-border-strong',
                    )}
                  />
                  {p}
                  {isActive && (
                    <span className="rounded-sm bg-confirmed/15 px-1 text-[10px] text-confirmed">
                      使用中
                    </span>
                  )}
                </button>
              );
            })}
          </div>
          <p className="mb-3 text-caption text-text-tertiary">
            点击选择 provider（● 绿点 = key 已配置），修改后点击底部 <span className="font-medium text-text-secondary">Save</span> 生效——所有聊天与后台任务立即切换到所选模型。
          </p>
          <label className="mb-1 block text-caption text-text-tertiary">Model</label>
          <Input
            value={model}
            onChange={(e) => setModel(e.target.value)}
            placeholder={DEFAULT_MODEL_FOR[provider]}
          />
          <p className="mt-2 text-caption text-text-tertiary">
            Defaults per <span className="font-mono">packages/nexus/ARCHITECTURE.md</span>:
            {' '}gemini-2.5-flash · gpt-4o · claude-sonnet-4-20250514 · kimi-k2.7-code.
          </p>
        </Card>
      </div>

      {/* API keys */}
      <div className="px-6 pb-5">
        <h2 className="mb-3 text-caption font-medium uppercase tracking-wider text-text-tertiary">
          API keys
        </h2>
        <Card>
          <div className="space-y-3">
            <div>
              <div className="mb-1 flex items-center justify-between">
                <span className="text-caption text-text-primary">Gemini</span>
                {keyRowRight('gemini', status.hasGeminiKey)}
              </div>
              <Input
                type="password"
                value={geminiKey}
                onChange={(e) => setGeminiKey(e.target.value)}
                placeholder={status.hasGeminiKey ? 'Replace existing key (leave empty to keep)' : 'AIza…'}
                autoComplete="off"
              />
            </div>
            <div>
              <div className="mb-1 flex items-center justify-between">
                <span className="text-caption text-text-primary">OpenAI</span>
                {keyRowRight('openai', status.hasOpenaiKey)}
              </div>
              <Input
                type="password"
                value={openaiKey}
                onChange={(e) => setOpenaiKey(e.target.value)}
                placeholder={status.hasOpenaiKey ? 'Replace existing key (leave empty to keep)' : 'sk-…'}
                autoComplete="off"
              />
            </div>
            <div>
              <div className="mb-1 flex items-center justify-between">
                <span className="text-caption text-text-primary">Anthropic</span>
                {keyRowRight('anthropic', status.hasAnthropicKey)}
              </div>
              <Input
                type="password"
                value={anthropicKey}
                onChange={(e) => setAnthropicKey(e.target.value)}
                placeholder={status.hasAnthropicKey ? 'Replace existing key (leave empty to keep)' : 'sk-ant-…'}
                autoComplete="off"
              />
            </div>
            <div>
              <div className="mb-1 flex items-center justify-between">
                <span className="text-caption text-text-primary">Kimi (Moonshot)</span>
                {keyRowRight('kimi', status.hasKimiKey)}
              </div>
              <Input
                type="password"
                value={kimiKey}
                onChange={(e) => setKimiKey(e.target.value)}
                placeholder={status.hasKimiKey ? 'Replace existing key (leave empty to keep)' : 'sk-…（platform.moonshot.ai）'}
                autoComplete="off"
              />
            </div>
          </div>
          <p className="mt-3 text-caption text-text-tertiary">
            Empty fields are ignored — only the keys you fill in get
            written. Keys never leave the local machine; the server only
            uses them to call the upstream provider directly.
          </p>
        </Card>
      </div>

      {/* ─── Active key diagnostic + Test (F12 / F16) ─────────────
          The single most useful Q after "did my save succeed?" is
          "is the key actually valid?" — answer it with a live call. */}
      <div className="px-6 pb-5">
        <h2 className="mb-3 text-caption font-medium uppercase tracking-wider text-text-tertiary">
          活跃 Key · 实时校验
        </h2>
        <Card>
          <div className="flex items-start gap-4 flex-wrap">
            <div className="flex-1 min-w-[240px] space-y-1">
              <div className="text-caption text-text-secondary">
                Active provider: <span className="font-mono text-text-primary">{status.provider}</span>
                {' · '}
                model: <span className="font-mono text-text-primary">{status.model}</span>
              </div>
              <div className="text-caption text-text-secondary">
                Key source:{' '}
                {status.activeKeySource === 'db' && (
                  <Chip variant="confirmed">database (saved settings)</Chip>
                )}
                {status.activeKeySource === 'env' && (
                  <Chip variant="neutral">.env / shell</Chip>
                )}
                {(status.activeKeySource === 'none' || !status.activeKeySource) && (
                  <Chip variant="caution">no key</Chip>
                )}
              </div>
              {status.activeKeyPreview && (
                <div className="text-caption text-text-secondary">
                  Loaded key: <span className="font-mono text-text-primary">{status.activeKeyPreview}</span>
                  {' '}
                  <span className="text-text-tertiary">({status.activeKeyLength} chars)</span>
                </div>
              )}
            </div>
            <Button
              variant="subtle"
              onClick={runTest}
              disabled={testing || !(
                (status.provider === 'gemini'    && status.hasGeminiKey)    ||
                (status.provider === 'openai'    && status.hasOpenaiKey)    ||
                (status.provider === 'anthropic' && status.hasAnthropicKey) ||
                (status.provider === 'kimi'      && status.hasKimiKey)
              )}
            >
              {testing ? '正在测试…' : 'Test Key now ↗'}
            </Button>
          </div>

          {testResult && (
            <div className="mt-3">
              {testResult.ok ? (
                <div className="rounded-md border border-confirmed/40 bg-confirmed/5 px-3 py-2 text-caption text-confirmed">
                  ✓ Key 工作正常 · {testResult.provider}/{testResult.model}
                  {testResult.latencyMs != null && (
                    <span className="text-text-tertiary">
                      {' '}· round-trip {testResult.latencyMs}ms
                    </span>
                  )}
                </div>
              ) : (
                <div className="rounded-md border border-retract/40 bg-retract/5 px-3 py-2 text-caption text-retract space-y-1">
                  <div>
                    ✗ Key 调用失败
                    {testResult.diagnosis && (
                      <span className="ml-2 font-mono text-[11px]">
                        [{testResult.diagnosis}]
                      </span>
                    )}
                  </div>
                  {testResult.error && (
                    <div className="font-mono text-[11px] text-retract/80 break-all">
                      {testResult.error}
                    </div>
                  )}
                  {testResult.diagnosis === 'key_invalid' && (
                    <div className="text-text-secondary">
                      → 上面输入框里粘贴一个新的 Gemini/OpenAI/Anthropic/Kimi key,然后 Save。
                    </div>
                  )}
                  {testResult.diagnosis === 'quota_exceeded' && (
                    <div className="text-text-secondary">
                      → 配额已耗尽 / 触发速率限制。等几分钟,或者切换到另一个 provider。
                    </div>
                  )}
                  {testResult.diagnosis === 'network' && (
                    <div className="text-text-secondary">
                      → 无法连接上游 API。检查代理/防火墙。
                    </div>
                  )}
                </div>
              )}
            </div>
          )}
          <p className="mt-3 text-caption text-text-tertiary">
            "Test" 会向当前 provider 发一个最小的 ping 请求(temperature 0、max_tokens 4),
            如果 key 有效会立刻返回。建议保存新 key 后点一下确认。
          </p>
        </Card>
      </div>

      <div className="flex items-center justify-end gap-2 border-t border-border px-6 py-4">
        <Button variant="ghost" onClick={refresh} disabled={saving}>
          Refresh
        </Button>
        <Button variant="primary" onClick={() => save()} disabled={saving}>
          {saving ? 'Saving…' : 'Save'}
        </Button>
      </div>
    </>
  );
}
