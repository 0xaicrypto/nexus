/**
 * Seven main-canvas modes. U1.1: Today/Patient/Encounter now real-backend.
 * U3.0: Memory rebuilt as layered view (L1 patient graph / L2 practitioner /
 *       L3 reference / meta). Report wired up as structured-impression
 *       composer with PDF / FHIR DiagnosticReport / DICOM SR export.
 *       Imaging/Labs remain stubs (U2/U3+).
 */

import { useCallback, useEffect, useMemo, useState } from 'react';
import { Mail } from 'lucide-react';
import { useAppState } from './store';
import { Button, Card, Chip, Section, EmptyState, Input } from './components/ui';
import { ChatMarkdown, type FileChipRef } from './components/chat-markdown';
import { ChatFileChipStrip, useChatFiles } from './components/chat-file-lib';
// F-thinking-uniform — every bubble now uses the wrapper pair
// (StreamingFooter for the persistent footer, StreamingCursor for
// the inline blink). The bare ThinkingIndicator is no longer
// imported here; consumers that need a standalone label can call
// StreamingFooter with the ``label`` override.
import { StreamingFooter, StreamingCursor } from './components/thinking-indicator';
import { TakeawaysButton } from './components/takeaways-button';
import {
  CitationChip2,
  ReasoningPane,
  TierIndicator,
  ConflictInlineBanner,
} from './components/memory-ui';
import {
  api, ApiError,
  type ChatSessionInfo, type QuickScanProgress,
} from './lib/api-client';
import { patientDisplayLabel, cn } from './lib/util';
import { useT, useModeLabel } from './lib/i18n';
import type {
  ChatMsg,
  ChatProposal,
  GraphNodeOut,
  PatientProjection,
  PractitionerCandidate,
  StudyInfo,
  TierKind,
} from './lib/types';

/* ─────────────── Today ─────────────── */

export function TodayMode() {
  const t = useT();
  const locale = useAppState((s) => s.locale);
  const patients = useAppState((s) => s.patients);
  const setActivePatient = useAppState((s) => s.setActivePatient);
  const refreshPatients = useAppState((s) => s.refreshPatients);
  const llmStatus       = useAppState((s) => s.llmStatus);
  const llmChecked      = useAppState((s) => s.llmStatusChecked);
  const openSettings    = useAppState((s) => s.openSettingsOverlay);
  const [pendingCount, setPendingCount] = useState(0);

  useEffect(() => {
    refreshPatients();
    api.practitionerPendingCount().then(setPendingCount).catch(() => {});
  }, [refreshPatients]);

  // Show a one-time, prominent setup card when no LLM key is configured
  // for the active provider. The top-of-screen banner ALSO fires (it
  // follows the medic everywhere), but landing right next to "Pinned
  // today" — the first thing seen on launch — makes the dependency
  // unmissable.
  const needsLlmSetup = llmChecked && llmStatus && llmStatus.advisory;

  const hour = new Date().getHours();
  // Use the active locale's intl rules for the date stamp. The greeting
  // word itself lives in the dictionary so zh-CN ("早上好" / "下午好" /
  // "晚上好") is a natural string rather than a forced literal.
  const greetingKey =
    hour < 12 ? 'today.welcome' : 'today.welcome';
  const greeting = t(greetingKey);
  const today = new Date().toLocaleDateString(locale, {
    weekday: 'long', month: 'long', day: 'numeric', year: 'numeric',
  });

  return (
    <div className="mx-auto max-w-2xl px-10 py-16">
      <div className="text-center">
        <h1 className="font-display text-display text-text-primary">{greeting}</h1>
        <p className="mt-2 text-body text-text-secondary">{today}</p>
      </div>

      {needsLlmSetup && (
        <div className="mt-8">
          <Card className="!p-5 !border-caution/50 !bg-caution/5">
            <div className="text-caption font-medium text-caution">
              {t('today.llmAdvisoryTitle')}
            </div>
            <p className="mt-2 text-body text-text-secondary">
              {llmStatus!.advisory} {t('settings.llm.envPath', { path: llmStatus!.envFilePath })}
            </p>
            <div className="mt-3">
              <Button variant="primary" onClick={openSettings}>
                {t('today.llmAdvisoryCta')}
              </Button>
            </div>
          </Card>
        </div>
      )}

      {pendingCount > 0 && (
        <div className="mt-8">
          <Card className="!p-5 border-accent/40">
            <div className="text-caption font-medium text-accent">
              {t('practitioner.title')} · {pendingCount}
            </div>
            <p className="mt-2 text-body text-text-secondary">
              {t('practitioner.intro')}
            </p>
          </Card>
        </div>
      )}

      <Section title={t('today.pinned')}>
        {patients.length === 0 ? (
          <p className="text-caption text-text-tertiary">
            {t('sidebar.empty')}
          </p>
        ) : (
          <div className="space-y-1">
            {patients.slice(0, 5).map((p) => (
              <button
                key={p.patientHash}
                onClick={() => setActivePatient(p)}
                className="flex w-full items-center justify-between rounded-sm px-3 py-2 text-left hover:bg-accent-subtle"
              >
                <div className="flex items-center gap-3">
                  <span className="text-caption text-text-primary">
                    {patientDisplayLabel(p)}
                  </span>
                  <span className="text-caption text-text-tertiary">
                    {p.sex} · {p.ageGroup}
                  </span>
                </div>
                <Chip mono>{p.latestModality || '—'}</Chip>
              </button>
            ))}
          </div>
        )}
      </Section>

      <Section title={t('today.ask')}>
        <CrossPatientChat />
      </Section>
    </div>
  );
}


/**
 * Cross-patient "ask Nexus about any patient" chat.
 *
 * Until 2026-06, the Today screen had a bare <Input> with no
 * onChange/onSubmit — typing did nothing. This component wires it to
 * api.sendChat with patient_hash=null so the agent answers in cohort
 * scope (across all patients the medic has access to). The visual mock
 * intends this to be the gateway to research-style cross-patient
 * Q&A; deeper questions naturally upgrade the user into Research
 * workspace via the suggestions strip below the answer.
 */
function CrossPatientChat() {
  const t = useT();
  const setActiveWorkspace = useAppState((s) => s.setActiveWorkspace);
  const [q, setQ]                 = useState('');
  const [answer, setAnswer]       = useState('');
  const [busy, setBusy]           = useState(false);
  const [err, setErr]             = useState<string | null>(null);

  // Use a long-lived session for the Today bar so follow-ups stick
  // (medic asks "and that patient's last CT?" expecting context).
  const [sessionId] = useState<string>(() => {
    const k = 'nexus.todayChat.sessionId';
    try {
      const cur = localStorage.getItem(k);
      if (cur) return cur;
    } catch { /* ignore */ }
    const fresh = `today-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
    try { localStorage.setItem(k, fresh); } catch { /* ignore */ }
    return fresh;
  });

  const send = async () => {
    const text = q.trim();
    if (!text || busy) return;
    setErr(null);
    setAnswer('');
    setBusy(true);
    try {
      // patient_hash=null → cross-patient (cohort) scope on the backend.
      for await (const chunk of api.sendChat(text, sessionId, null)) {
        // chat_router_v2 SSE shape: `type` discriminator + payload fields
        // (see ChatStreamChunk in lib/types.ts and chat_router_v2.py).
        // We only render the assembled text here; tier/citation metadata
        // isn't surfaced in this compact widget (Research Chat shows it).
        if (chunk.type === 'final_answer_chunk' && chunk.text) {
          setAnswer((prev) => prev + chunk.text);
        }
      }
    } catch (e) {
      const ae = e as ApiError;
      setErr(ae?.message || String(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="space-y-3">
      <div className="flex items-center justify-end">
        {/* Per-user qualitative insights, no scope filter here — the
            Today bar is the "across everything" surface, so we surface
            ALL takeaways the medic accumulated. */}
        <TakeawaysButton tone="base" />
      </div>
      <div className="flex items-center gap-2">
        <Input
          value={q}
          onChange={(e) => setQ(e.currentTarget.value)}
          onKeyDown={(e) => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send(); }}}
          placeholder={t('today.askPlaceholder')}
          disabled={busy}
        />
        <Button variant="primary" onClick={send}
                disabled={busy || !q.trim()}>
          {busy ? '…' : '↑'}
        </Button>
      </div>

      {(answer || err || busy) && (
        <Card className="!p-4 !bg-surface">
          {answer && (
            <div className="text-body text-text-primary">
              <ChatMarkdown text={answer} />
              {busy && <StreamingCursor tone="base" />}
            </div>
          )}
          {/* F-thinking-uniform: persistent footer that stays visible
              while busy=true regardless of whether the answer text
              has started arriving. */}
          <StreamingFooter
            streaming={busy}
            hasText={!!(answer && answer.length > 0)}
            tone="base"
            label={answer ? undefined : '正在跨患者检索 + 思考'}
          />

          {err && (
            <div className="text-caption text-retract mt-2">出错：{err}</div>
          )}
          {!busy && answer && (
            <div className="mt-3 pt-3 border-t border-border flex items-center gap-2 text-caption text-text-tertiary">
              <span>需要更深入分析？</span>
              <button
                onClick={() => setActiveWorkspace('research')}
                className="underline hover:text-accent">
                打开 Research 工作台 →
              </button>
            </div>
          )}
        </Card>
      )}
    </div>
  );
}

/* ─────────────── Patient overview (real projection) ─────────────── */

/**
 * Plain-text body for the "Email findings" pre-fill. We use only
 * pseudonymous patient identifiers (initials / age group / sex) — no
 * MRN, no DOB — so the email itself doesn't carry direct identifiers.
 * The receiving clinician is expected to have access to the same
 * patient registry. Findings are listed with their urgency tag so the
 * recipient can triage at a glance.
 */
function buildFindingsEmailBody(
  p: { sex?: string | null; ageGroup?: string | null },
  proj: PatientProjection,
): string {
  const lines: string[] = [];
  lines.push('Findings list (Quick scan output, unconfirmed):');
  lines.push('');
  for (const f of proj.findings) {
    const c = f.content as Record<string, unknown>;
    const label   = String(c.label ?? '(unlabeled)');
    const urgency = String(c.urgency ?? '');
    const tag = urgency ? ` [${urgency}]` : '';
    lines.push(`  • ${label}${tag}`);
  }
  lines.push('');
  lines.push(`Patient: ${p.sex ?? '?'} · ${p.ageGroup ?? '?'}`);
  lines.push('');
  lines.push(
    'These are unconfirmed Quick scan candidates — please review '
    + 'the source DICOMs before acting. I can share the imaging on request.',
  );
  return lines.join('\n');
}

/**
 * IngestDiagnosisBanner — explains WHY 当前发现 is empty.
 *
 * When chat_ingester doesn't surface any findings for a patient
 * (despite the medic just pasting a full SOAP) the medic's only
 * cue used to be the silent "暂无活跃发现" line — that pushed all
 * debugging into "open the terminal, tail the server log". This
 * banner pulls /memory/patient/{hash}/ingest_debug and renders the
 * server-side diagnosis inline:
 *   - never triggered → patient_hash plumbing
 *   - LLM call raised → API key / quota / model permissions
 *   - LLM returned prose → safety filter / refusal
 *   - LLM returned empty → source thinness
 *   - all entities dropped → verbatim check too strict
 *
 * Collapsed by default to avoid clutter when the call is loading;
 * expands on click to show raw_output_head from the last extractor
 * call — that one string is usually enough to pin down the
 * root cause.
 */
function IngestDiagnosisBanner({ patientHash }: { patientHash: string }) {
  const [info, setInfo] = useState<Awaited<
    ReturnType<typeof api.getIngestDebug>
  > | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [showRaw, setShowRaw] = useState(false);

  useEffect(() => {
    let cancelled = false;
    api.getIngestDebug(patientHash).then(
      (r) => { if (!cancelled) setInfo(r); },
      (e) => { if (!cancelled) setError(String(e?.message || e)); },
    );
    return () => { cancelled = true; };
  }, [patientHash]);

  if (error) {
    // Backend missing the endpoint (stale binary) — silently no-op.
    return null;
  }
  if (!info) return null;
  // If the medic hasn't even chatted yet, no need for a diagnosis
  // banner — the empty state is correct by definition.
  if (info.ingestionStarted === 0) return null;

  return (
    <div className="mt-3 rounded-md border border-caution/30 bg-caution/5 p-3">
      <div className="flex items-start gap-2">
        <span aria-hidden className="text-caution">⚠</span>
        <div className="flex-1 min-w-0">
          <div className="text-caption font-medium text-caution">
            诊断: 为什么这位病人没有"当前发现"?
          </div>
          <div className="mt-1 text-caption text-text-secondary">
            {info.diagnosis}
          </div>
          <div className="mt-2 text-[11px] font-mono text-text-tertiary">
            triggered={info.ingestionStarted}
            {' · '}completed={info.ingestionCompleted}
            {' · '}node_added_events={info.nodeAddedEvents}
            {' · '}graph_rows={info.clinicalGraphNodes}
          </div>
          {info.latestLlmResponse?.rawOutputHead && (
            <div className="mt-2">
              <button
                type="button"
                onClick={() => setShowRaw((v) => !v)}
                className="text-[11px] underline text-text-tertiary hover:text-text-primary"
              >
                {showRaw ? '收起 LLM 原始返回' : '查看 LLM 原始返回 (前 400 字)'}
              </button>
              {showRaw && (
                <pre className="mt-1 max-h-48 overflow-auto rounded bg-bg p-2
                                text-[10px] leading-snug text-text-secondary
                                whitespace-pre-wrap break-all">
                  {info.latestLlmResponse.rawOutputHead}
                </pre>
              )}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}


export function PatientMode() {
  const t = useT();
  const p = useAppState((s) => s.activePatient);
  const setActiveMode = useAppState((s) => s.setActiveMode);
  const openEmail = useAppState((s) => s.openEmailComposer);
  const [proj, setProj] = useState<PatientProjection | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!p) return;
    let cancelled = false;
    setLoading(true);
    setError(null);
    api.getPatientProjection(p.patientHash).then(
      (r) => { if (!cancelled) { setProj(r); setLoading(false); } },
      (e) => { if (!cancelled) { setError(String(e)); setLoading(false); } },
    );
    return () => { cancelled = true; };
  }, [p]);

  if (!p) return <EmptyState title={t('patient.noSelection')} />;

  return (
    <div className="mx-auto max-w-3xl px-10 py-12 selectable">
      <div className="mb-6">
        <h1 className="font-display text-display text-text-primary">
          {patientDisplayLabel(p)}
        </h1>
        <div className="mt-2 flex items-center gap-2 text-body text-text-secondary">
          <span>{p.sex || '—'}</span><span>·</span><span>{p.ageGroup || '—'}</span>
          <span>·</span>
          <span>{t('patient.studies', { count: proj?.studies.length ?? 0 })}</span>
        </div>
      </div>

      {proj && proj.unresolvedConflictCount > 0 && (
        <ConflictInlineBanner
          count={proj.unresolvedConflictCount}
          onResolve={() => setActiveMode('memory')}
        />
      )}

      {loading && (
        <p className="text-caption text-text-tertiary">{t('patient.loading')}</p>
      )}
      {error && (
        <p className="text-caption text-retract">{t('patient.loadFailed', { error })}</p>
      )}

      {proj && (
        <>
          <Section title={t('patient.activeFindings')}>
            {proj.findings.length === 0 ? (
              <>
                <p className="text-caption text-text-tertiary">
                  {t('patient.findingsEmpty')}
                </p>
                <IngestDiagnosisBanner patientHash={p.patientHash} />
              </>
            ) : (
              <>
                <ul className="space-y-1 text-body text-text-primary">
                  {proj.findings.map((f) => (
                    <li key={f.nodeId} className="flex items-center gap-2">
                      <span>•</span>
                      <span>{(f.content as any).label ?? t('patient.unlabeled')}</span>
                      {(f.content as any).size_cm != null && (
                        <Chip variant="neutral">
                          {(f.content as any).size_cm} cm
                        </Chip>
                      )}
                      <CitationChip2 index={f.nodeId} nodeId={f.nodeId} />
                    </li>
                  ))}
                </ul>
                <div className="mt-3 flex items-center gap-2">
                  <Button
                    variant="subtle"
                    onClick={() => openEmail({
                      subject: `${t('patient.activeFindings')} · ${patientDisplayLabel(p)}`,
                      body: buildFindingsEmailBody(p, proj),
                    })}
                  >
                    <Mail size={14} /> {t('patient.emailFindings')}
                  </Button>
                  <span className="text-caption text-text-tertiary">
                    {t('patient.emailHint')}
                  </span>
                </div>
              </>
            )}
          </Section>

          <Section title={t('patient.medications')}>
            {proj.medications.length === 0 ? (
              <p className="text-caption text-text-tertiary">{t('patient.medsEmpty')}</p>
            ) : (
              <ul className="space-y-1 text-body text-text-primary">
                {proj.medications.map((m) => (
                  <li key={m.nodeId}>• {(m.content as any).label ?? '?'}</li>
                ))}
              </ul>
            )}
          </Section>

          <Section title={t('patient.recentImaging')}>
            <RecentImagingSection patientHash={p.patientHash} />
          </Section>
        </>
      )}

      {/* The bottom "Open in Nexus →" CTA was removed: it only mirrored
          the 问诊 tab in ModeTabs above, and labelling it "Open in
          Nexus" when the user is already inside Nexus made no sense.
          If we want a deliberate "start an encounter" CTA later, attach
          it to a specific action (e.g. open a new chat with a draft
          question), not just a tab switch. */}
    </div>
  );
}

/* ─────────────── Recent imaging — DICOM previews ─────────────── */

/**
 * Lists every DICOM study for the patient (via /api/v1/dicom/patients/
 * {hash}/studies) and renders a thumbnail per study using the existing
 * /studies/{id}/series/{id}/render endpoint (4×4 grid preset — gives a
 * good "see the whole study at a glance" view for axial CT/MR).
 *
 * Preset rationale: backend's prerender pass writes 768 px PNGs to
 * <preview_dir>/slices/{idx}-{preset}.png at upload time (see dicom.py
 * eager cache lookup in /render). Hitting the render endpoint with
 * kind=grid does NOT use that cache — it composes per-call. For
 * thumbnails we want one image per study, so the grid preset is
 * acceptable cost (~50 ms server-side) and the resulting <img> can
 * cache via the browser.
 */
function RecentImagingSection({ patientHash }: { patientHash: string }) {
  const [studies, setStudies] = useState<StudyInfo[] | null>(null);
  const [error,   setError]   = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setStudies(null);
    setError(null);
    api.listPatientStudies(patientHash).then(
      (s) => { if (!cancelled) setStudies(s); },
      (e) => { if (!cancelled) setError(e instanceof Error ? e.message : String(e)); },
    );
    return () => { cancelled = true; };
  }, [patientHash]);

  if (error) {
    return <p className="text-caption text-retract">Failed to load: {error}</p>;
  }
  if (studies === null) {
    return <p className="text-caption text-text-tertiary">Loading studies…</p>;
  }
  if (studies.length === 0) {
    return (
      <p className="text-caption text-text-tertiary">
        No DICOM studies yet — drop a <span className="font-mono">.zip</span>
        {' '}into <strong>Imaging</strong> and they'll appear here.
      </p>
    );
  }

  return (
    <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3">
      {studies.slice(0, 6).map((s) => (
        <StudyPreviewCard key={s.studyId} study={s} />
      ))}
    </div>
  );
}

function StudyPreviewCard({ study }: { study: StudyInfo }) {
  const [study2, setStudy2] = useState<StudyInfo | null>(null);
  const [imgUrl, setImgUrl] = useState<string | null>(null);
  const [imgErr, setImgErr] = useState<string | null>(null);

  // We need a series_id to render. The list endpoint returns series=[]
  // for efficiency; re-fetch the full study lazily for the first card
  // mount.
  useEffect(() => {
    let cancelled = false;
    let blobUrl: string | null = null;

    (async () => {
      try {
        const full = study.series.length > 0
          ? study
          : await api.getStudy(study.studyId);
        if (cancelled) return;
        setStudy2(full);

        // Prefer the primary series (largest instance count) for the
        // grid thumbnail — the prerender bundle key_image lives there.
        const primary = [...full.series]
          .sort((a, b) => (b.instanceCount || 0) - (a.instanceCount || 0))[0];
        if (!primary) {
          setImgErr('No series in study');
          return;
        }
        // Use the middle slice as the thumbnail.
        const mid = Math.max(0, Math.floor((primary.instanceCount || 1) / 2));
        const url = await api.renderBlobUrl(full.studyId, primary.seriesId, {
          kind:  'slice',
          slice: mid,
          window: 'default',
        });
        if (cancelled) {
          URL.revokeObjectURL(url);
          return;
        }
        blobUrl = url;
        setImgUrl(url);
      } catch (e) {
        if (!cancelled) {
          setImgErr(e instanceof Error ? e.message : String(e));
        }
      }
    })();

    return () => {
      cancelled = true;
      if (blobUrl) URL.revokeObjectURL(blobUrl);
    };
  }, [study.studyId]);

  const headerLeft = study.modality || study2?.modality || '?';
  const headerRight = study.studyDate || study2?.studyDate || '';
  const description = study.studyDescription || study2?.studyDescription || '';
  const seriesCount = (study2?.series.length ?? study.series.length) || 0;

  return (
    <a
      // Open the bundled Cornerstone viewer as a NEW TAURI WINDOW —
      // critical: the system browser has no access to the JWT in
      // sessionStorage, so the viewer's /api/v1/dicom/* fetches 401
      // and the page sits at "Loading…" forever. Going through
      // ``api.openDicomViewer`` spawns a separate WebviewWindow with
      // the token in the URL query so the page authenticates
      // correctly. Outside Tauri (pnpm dev) it falls back to
      // window.open + dev FastAPI.
      onClick={(e) => {
        e.preventDefault();
        api.openDicomViewer(study.studyId);
      }}
      href="#viewer"  // visual cursor; click handler does the real work
      className="block cursor-pointer overflow-hidden rounded-md border border-border bg-surface transition-colors hover:border-border-strong"
    >
      <div className="relative aspect-square w-full bg-black">
        {imgUrl ? (
          <img
            src={imgUrl}
            alt={`${headerLeft} ${headerRight}`}
            className="h-full w-full object-contain"
          />
        ) : imgErr ? (
          <div className="flex h-full w-full items-center justify-center p-3 text-caption text-retract">
            {imgErr}
          </div>
        ) : (
          <div className="flex h-full w-full items-center justify-center text-caption text-text-tertiary">
            Rendering…
          </div>
        )}
        <div className="absolute left-2 top-2 flex items-center gap-1">
          <Chip mono variant="tinted">{headerLeft}</Chip>
        </div>
        {headerRight && (
          <div className="absolute right-2 top-2 font-mono text-[10px] text-text-tertiary">
            {headerRight}
          </div>
        )}
      </div>
      <div className="p-3">
        <div className="truncate text-body text-text-primary">
          {description || `Study ${study.studyId.slice(0, 8)}`}
        </div>
        <div className="mt-1 text-caption text-text-tertiary">
          {seriesCount} series · open viewer →
        </div>
      </div>
    </a>
  );
}

/* ─────────────── Encounter (real SSE) ─────────────── */

// F-chat-state-persist — ChatMsg + ChatProposal moved to lib/types.ts
// so the zustand store (which also references them) doesn't import
// modes.tsx. See `chatMsgsBySession` in store.ts.
//
// Module-level map of in-flight AbortControllers keyed by sessionId.
// AbortController doesn't render anything, so it doesn't belong in
// React state. Keeping it here (instead of useRef) lets it survive
// EncounterMode unmounting — needed for the "Stop generating" UI we
// haven't built yet but want to leave room for. The SSE loop itself
// does NOT auto-abort on unmount; that's the whole point of
// F-chat-state-persist (medic can switch tabs and the AI keeps
// thinking).
const _chatAbortBySession = new Map<string, AbortController>();

/**
 * Inline confirmation card for a chat-detected scheduled-task
 * proposal. Renders under the agent's message bubble. Medic edits
 * the to/subject/body inline (Phase 1 only handles send_email),
 * picks Confirm → POST /schedule/confirm, or Cancel → mark dismissed
 * (no persistence; the SCHEDULED_TASK_PROPOSED event was already
 * audit-logged for false-positive rate analysis).
 *
 * Phase 1 keeps the time picker minimal — just shows fire_at as
 * read-only. Editing time-of-day is a Phase 2 task that ties into
 * a real datetime picker; for now medic can cancel + re-prompt
 * the chat with a different phrasing.
 */
function ScheduleProposalCard({
  proposal,
  onConfirm,
  onCancel,
}: {
  proposal: ChatProposal;
  onConfirm: (edited: { to: string; subject: string; body: string }) => Promise<void>;
  onCancel: () => void;
}) {
  const t = useT();
  const locale = useAppState((s) => s.locale);
  // Pre-fill 'to' from heuristic; medic can edit.
  const initialTo = Array.isArray(proposal.payload.to)
    ? (proposal.payload.to as string[]).join(', ')
    : '';
  const initialSubject = String(proposal.payload.subject ?? '');
  const initialBody    = String(proposal.payload.body    ?? '');
  const [to, setTo]           = useState(initialTo);
  const [subject, setSubject] = useState(initialSubject);
  const [body, setBody]       = useState(initialBody);

  const fireDate = new Date(proposal.fireAt * 1000);
  const fireLabel = `${fireDate.toLocaleString(locale)} (${proposal.userTz})`;
  const disabled = proposal.uiState === 'submitting' || proposal.uiState === 'done';

  return (
    <div className="mt-3 rounded-md border border-accent/40 bg-accent-subtle/30 p-3 text-caption">
      <div className="mb-2 flex items-center gap-2">
        <span className="text-text-primary">📅 {t('sched.proposalTitle')}</span>
        <span className="text-text-tertiary">·</span>
        <span className="text-text-secondary">
          {proposal.kind === 'send_email' ? t('sched.kind.sendEmail') : proposal.kind}
        </span>
      </div>
      <p className="mb-3 text-text-secondary">{t('sched.proposalIntro')}</p>

      <div className="mb-3 grid grid-cols-[auto_1fr] gap-x-3 gap-y-1.5 text-text-secondary">
        <span className="text-text-tertiary">{t('sched.fireAt')}</span>
        <span className="text-text-primary">{fireLabel}</span>
      </div>

      <label className="mb-1 mt-2 block text-text-tertiary">{t('sched.to')}</label>
      <input
        type="text"
        value={to}
        onChange={(e) => setTo(e.target.value)}
        disabled={disabled}
        placeholder={t('sched.recipientPlaceholder')}
        className="mb-2 w-full rounded-sm border border-border bg-bg px-2 py-1 text-body text-text-primary placeholder:text-text-tertiary focus:border-accent focus:outline-none"
      />

      <label className="mb-1 block text-text-tertiary">{t('sched.subject')}</label>
      <input
        type="text"
        value={subject}
        onChange={(e) => setSubject(e.target.value)}
        disabled={disabled}
        placeholder={t('sched.subjectPlaceholder')}
        className="mb-2 w-full rounded-sm border border-border bg-bg px-2 py-1 text-body text-text-primary placeholder:text-text-tertiary focus:border-accent focus:outline-none"
      />

      <label className="mb-1 block text-text-tertiary">{t('sched.body')}</label>
      <textarea
        rows={3}
        value={body}
        onChange={(e) => setBody(e.target.value)}
        disabled={disabled}
        placeholder={t('sched.bodyPlaceholder')}
        className="mb-2 w-full resize-y rounded-sm border border-border bg-bg px-2 py-1 text-body text-text-primary placeholder:text-text-tertiary focus:border-accent focus:outline-none"
      />

      {proposal.errorMsg && (
        <div className="mb-2 rounded-sm border border-retract/40 bg-retract/10 px-2 py-1 text-retract">
          {proposal.errorMsg}
        </div>
      )}

      {proposal.uiState === 'done' ? (
        <div className="rounded-sm border border-confirmed/40 bg-confirmed/10 px-2 py-1 text-confirmed">
          ✓ {t('sched.scheduledToast', { when: fireLabel })}
        </div>
      ) : proposal.uiState === 'cancelled' ? (
        <div className="text-text-tertiary">{t('sched.cancel')}</div>
      ) : (
        <div className="flex items-center gap-2">
          <Button
            variant="primary"
            disabled={disabled || !to.trim() || !subject.trim() || !body.trim()}
            onClick={() => onConfirm({ to, subject, body })}
          >
            {proposal.uiState === 'submitting'
              ? t('sched.scheduling')
              : t('sched.confirm')}
          </Button>
          <Button variant="subtle" onClick={onCancel} disabled={disabled}>
            {t('sched.cancel')}
          </Button>
        </div>
      )}
    </div>
  );
}

export function EncounterMode() {
  const t = useT();
  const p              = useAppState((s) => s.activePatient);
  const activeSessionId = useAppState((s) => s.activeSessionId);
  const setActiveSessionId = useAppState((s) => s.setActiveSessionId);
  const showToast      = useAppState((s) => s.showToast);
  const [draft, setDraft] = useState('');
  const [backendStatus, setBackendStatus] =
    useState<'ok' | 'unreachable' | 'unhealthy' | 'checking'>('checking');

  // F-chat-state-persist — msgs / streaming flag now live in zustand
  // keyed by sessionId. Reading them via selectors means the chat
  // pane rehydrates from the store on remount, so a streaming turn
  // that was started before the medic switched tabs keeps painting
  // as new chunks arrive (the SSE consumer below writes into the
  // same store keys). The previous useState-based approach lost the
  // partial answer on every unmount.
  const setChatMsgs       = useAppState((s) => s.setChatMsgs);
  const appendChatMsg     = useAppState((s) => s.appendChatMsg);
  const updateLastChatMsg = useAppState((s) => s.updateLastChatMsg);
  const setChatStreaming  = useAppState((s) => s.setChatStreaming);

  // F-unified-chat-files — patient-scoped file library hook.
  // p?.patientHash is the lib_scope_ref; falsy when no patient is
  // active (the EmptyState path), in which case the hook short-
  // circuits to an empty list and the chip strip won't render.
  const encounterChatFiles = useChatFiles(
    'patient', p?.patientHash ?? '',
  );
  // f_id_token → file metadata map for ChatMarkdown to inflate
  // [F1] inline references inside agent replies.
  const fileMap: Record<string, FileChipRef> = {};
  for (const f of encounterChatFiles.files) {
    fileMap[f.fIdToken] = {
      fileId: f.fileId, name: f.name,
      textExtractionStatus: f.textExtractionStatus,
    };
  }

  // Chat sessions ─────────────────────────────────────────────────
  const [sessions, setSessions] = useState<ChatSessionInfo[]>([]);
  const [showSessionList, setShowSessionList] = useState(false);

  // Files staged for the next send (paste / drop). Each carries the
  // server-assigned file_id once the upload completes; pending uploads
  // show a spinner chip until the id arrives.
  const [attachments, setAttachments] =
    useState<Array<{ key: string; name: string; sizeBytes: number; fileId: string | null; failed?: string; previewUrl?: string; isImage?: boolean }>>([]);

  // Probe the backend once on mount. A failed probe lets us tell the
  // medic "backend not running" instead of the opaque "TypeError: Load
  // failed" that WebKit emits when fetch can't reach the sidecar.
  useEffect(() => {
    let cancelled = false;
    api.health().then((s) => { if (!cancelled) setBackendStatus(s); });
    return () => { cancelled = true; };
  }, []);

  // F-tab-switch-race + F-chat-state-persist — the medic explicitly
  // wants the AI to keep thinking even when they switch tabs (ChatGPT
  // style: send, navigate away, come back later, see the answer). So
  // we do NOT abort the SSE on unmount.
  //
  // With F-chat-state-persist the ChatMsg array now lives in zustand,
  // so a streaming turn that started before unmount keeps writing
  // into the store as chunks arrive; on remount the chat pane shows
  // the partial answer immediately (no longer needs the post-turn
  // history-pull as a fallback).

  // Load the user's sessions on mount + after each send-back-to-default
  // (so a freshly-created session is visible in the picker).
  const refreshSessions = useCallback(async () => {
    try {
      const list = await api.listSessions(false);
      setSessions(list);
    } catch {
      /* sessions are nice-to-have — don't blow up the chat pane */
    }
  }, []);
  useEffect(() => { refreshSessions(); }, [refreshSessions]);

  // Effective session id for THIS encounter.
  //
  // Sessions are not patient-scoped on the backend — the medic's
  // `activeSessionId` is just a string. If we used `activeSessionId`
  // directly, switching patients would still load whatever session was
  // last opened (cross-patient bleed: 张三's 12-message history showing
  // up under 李四). To fix, we derive a per-patient default whenever the
  // medic hasn't explicitly picked a named session, mirroring the
  // Research Chat pattern (`research-${studyId}`):
  //
  //   activeSessionId is empty  →  use `patient-${patientHash}`
  //   activeSessionId is set    →  trust the medic's pick (named sessions
  //                                may legitimately cross patients in
  //                                multi-patient discussions)
  //
  // setActivePatient in store.ts also clears activeSessionId on switch,
  // so the default below is what loads after a sidebar click.
  const effectiveSessionId = activeSessionId
    || (p ? `patient-${p.patientHash}` : '');

  // F-chat-state-persist — read msgs + streaming for THIS session.
  // Both come from the zustand store keyed by sessionId, so when the
  // medic returns to a tab whose stream is still in flight, the
  // selector immediately yields the in-progress array and the
  // streaming flag (gates the Send button + "AI is thinking" hint).
  const msgs = useAppState((s) =>
    s.chatMsgsBySession[effectiveSessionId] ?? []);
  const sending = useAppState((s) =>
    !!s.chatStreamingBySession[effectiveSessionId]);

  // Load chat history whenever the effective session changes — that
  // covers both "medic picked a new session" and "medic switched
  // patients (so the derived default changed)".
  //
  // F-chat-state-persist — skip the load if a stream is in flight
  // for this session. The SSE consumer owns msgs while ``sending``
  // is true, and the previous unconditional ``setMsgs([])`` was the
  // bug that erased the partial answer on every tab re-mount.
  useEffect(() => {
    let cancelled = false;
    if (!effectiveSessionId) return;
    if (useAppState.getState().chatStreamingBySession[effectiveSessionId]) {
      // Stream in flight — leave the store alone; the consumer is
      // still writing chunks into it.
      return;
    }
    setChatMsgs(effectiveSessionId, []);
    api.listSessionMessages(effectiveSessionId, 200).then(
      (rows) => {
        if (cancelled) return;
        // Re-check the streaming flag — a fresh send() may have
        // landed between the request and the response.
        if (useAppState.getState().chatStreamingBySession[effectiveSessionId]) {
          return;
        }
        setChatMsgs(effectiveSessionId, rows.map((r): ChatMsg => ({
          role:   r.role === 'agent' ? 'agent' : 'user',
          text:   r.text,
          ts:     formatRelativeTs(r.ts),
          reasoning: [],
          citations: [],
          // F-history-attachments — hydrate attachment names from
          // the server's ChatMessageView. Without this, a turn that
          // had a file attached would lose its 📎 chip on
          // history reload, making it look like the medic never
          // sent the file. The chip is the medic's only visual
          // breadcrumb of "the AI did see this file".
          attachedFileNames: (r.attachments ?? []).map((a) => a.name),
        })));
      },
      () => { /* history is nice-to-have — empty pane is fine */ },
    );
    return () => { cancelled = true; };
  }, [effectiveSessionId, setChatMsgs]);

  if (!p) return <EmptyState title={t('encounter.noSelection')} />;

  async function startNewSession() {
    try {
      const s = await api.createSession('New chat');
      setActiveSessionId(s.id);
      await refreshSessions();
      showToast('Started a new chat', 'success');
    } catch (e) {
      showToast(`Could not create session: ${String(e)}`, 'error');
    }
  }

  async function uploadOne(file: File): Promise<string | null> {
    try {
      // F-unified-chat-files — bind upload into the patient's
      // file library so it shows in the chip strip + survives
      // turns. libScopeKind+Ref are written server-side into the
      // ``uploads`` row.
      const r = await api.uploadFile(file, file.name, {
        patientHash:   p?.patientHash,
        libScopeKind:  p ? 'patient' : undefined,
        libScopeRef:   p?.patientHash,
      });
      // Refresh chip strip so the new file shows up immediately.
      try { encounterChatFiles.refresh(); } catch { /* hook not ready yet */ }
      return r.fileId;
    } catch (e) {
      showToast(`Upload failed: ${String(e)}`, 'error');
      return null;
    }
  }

  // Attach one or more File objects (paste / drop / picker). Each gets
  // a placeholder chip immediately so the UI feels responsive; the
  // chip transitions to "ready" when its upload completes.
  function acceptFiles(files: FileList | File[]) {
    const arr = Array.from(files);
    if (arr.length === 0) return;
    const placeholders = arr.map((f) => {
      const isImage = (f.type || '').startsWith('image/');
      return {
        key: `${f.name}-${f.size}-${Date.now()}-${Math.random().toString(36).slice(2,6)}`,
        name: f.name,
        sizeBytes: f.size,
        fileId: null as string | null,
        // Image attachments get a local blob URL so the chip can show
        // a thumbnail. The chat surface is the medic's working
        // memory — they should *see* what they just dropped before
        // pressing send, not be told "you dropped a 230K thing".
        previewUrl: isImage ? URL.createObjectURL(f) : undefined,
        isImage,
      };
    });
    setAttachments((prev) => [...prev, ...placeholders]);

    arr.forEach((file, idx) => {
      const key = placeholders[idx].key;
      uploadOne(file).then((fid) => {
        setAttachments((prev) => prev.map((a) =>
          a.key === key ? { ...a, fileId: fid, failed: fid ? undefined : 'upload failed' } : a,
        ));
      });
    });
  }

  // Clipboard paste handler — captures pasted images (screen-grab /
  // copy-image-from-browser / image off Slack etc.) and dropped /
  // copied files. e.clipboardData.files covers both image bitmaps
  // and arbitrary files dragged from Finder.
  function onPaste(e: React.ClipboardEvent<HTMLTextAreaElement>) {
    const files = e.clipboardData?.files;
    if (files && files.length > 0) {
      e.preventDefault();
      acceptFiles(files);
    }
    // Otherwise let the default text paste through.
  }

  // Drag-drop directly onto the textarea — same effect as paste.
  function onDrop(e: React.DragEvent<HTMLDivElement>) {
    e.preventDefault();
    if (e.dataTransfer?.files?.length) acceptFiles(e.dataTransfer.files);
  }

  function removeAttachment(key: string) {
    // Revoke any blob URL the thumbnail was using to avoid leaking
    // bitmap memory in the WebView (every paste-then-remove of a
    // multi-MB screenshot otherwise stays resident until the page
    // is closed).
    setAttachments((prev) => {
      const found = prev.find((a) => a.key === key);
      if (found?.previewUrl) {
        try { URL.revokeObjectURL(found.previewUrl); } catch { /* ignore */ }
      }
      return prev.filter((a) => a.key !== key);
    });
  }

  async function send() {
    if (sending) return;
    // Allow send-with-attachments only — i.e. an image-with-no-text
    // counts as a valid turn.
    if (!draft.trim() && attachments.length === 0) return;

    // Wait for all in-flight uploads to settle so the file_ids we
    // pass to sendChat are real — a half-uploaded paste shouldn't
    // silently drop the file.
    const pending = attachments.filter((a) => a.fileId === null && !a.failed);
    if (pending.length > 0) {
      showToast(`Waiting for ${pending.length} upload(s)…`, 'info');
      return;
    }
    const fileIds = attachments
      .filter((a) => a.fileId)
      .map((a) => a.fileId as string);
    const stagedAttachments = [...attachments];

    const userText = draft;
    setDraft('');
    setAttachments([]);
    const sid = effectiveSessionId;
    appendChatMsg(sid, {
      role: 'user', text: userText, ts: 'now',
      attachedFileNames: stagedAttachments.map((a) => a.name),
    });
    setChatStreaming(sid, true);

    const startTs = Date.now();
    appendChatMsg(sid, {
      role: 'agent', text: '', ts: 'now',
      reasoning: [], citations: [], streaming: true,
    });

    // F-chat-state-persist — ``update`` writes through the zustand
    // store, so chunks landing after the component unmounted (medic
    // switched tabs) still hit the right state and are visible when
    // the chat pane remounts. The elapsedMs stamp is local-only;
    // it'll be the time since THIS send() started, even if the
    // medic re-mounted in between.
    const update = (mut: Partial<ChatMsg>) =>
      updateLastChatMsg(sid, { ...mut, elapsedMs: Date.now() - startTs });

    // F-tab-switch-race — bind this turn's SSE to a fresh
    // AbortController. Stored at module level so a future
    // "Stop generating" UI can find it; unmount no longer aborts.
    const ctrl = new AbortController();
    _chatAbortBySession.set(sid, ctrl);

    try {
      for await (const chunk of api.sendChat(
        userText, effectiveSessionId, p!.patientHash, fileIds,
        undefined,        // scope (none for patient-bound chat)
        ctrl.signal,
      )) {
        switch (chunk.type) {
          case 'tier_classified':
            update({ tier: chunk.tier });
            break;
          case 'reasoning_chunk':
            updateLastChatMsg(sid, (last) => ({
              reasoning: [...(last.reasoning ?? []), chunk.text],
            }));
            break;
          case 'final_answer_chunk':
            updateLastChatMsg(sid, (last) => ({
              text: last.text + chunk.text,
            }));
            break;
          case 'citations':
            update({ citations: chunk.refs });
            break;
          case 'web_search_started':
            updateLastChatMsg(sid, (last) => ({
              reasoning: [
                ...(last.reasoning ?? []),
                `🔎 Searching ${chunk.provider}…`,
              ],
            }));
            break;
          case 'web_search_results':
            // Attach the result list to the message; UI renders the
            // sources card under the agent bubble before the LLM
            // synthesis chunks fill in the final answer text.
            update({ webResults: chunk.results });
            break;
          case 'scheduled_task_proposed':
            // Heuristic detected a future-action intent. Attach the
            // proposal to the current agent message so the UI renders
            // a confirmation card under it. Default fields are filled
            // from the proposal; the medic edits + confirms in the card.
            update({
              proposal: {
                proposalId:     chunk.proposal_id,
                kind:           chunk.kind,
                fireAt:         chunk.fire_at,
                userTz:         chunk.user_tz,
                summary:        chunk.summary,
                payload:        chunk.payload,
                recurrenceCron: chunk.recurrence_cron,
                sessionId:      chunk.session_id,
                patientHash:    chunk.patient_hash,
                needsUserInput: chunk.needs_user_input,
                uiState:        'editing',
              },
            });
            break;
          case 'turn_complete':
            update({ streaming: false });
            // Note: 病人 tab re-pulls the projection on next open via
            // its useEffect; we don't share state across tabs here so
            // there's no in-place refresh from this chat surface.
            break;
          case 'memory_ingested':
            update({
              memoryIngested: {
                ok:        !!chunk.ok,
                nodeCount: Number(chunk.node_count) || 0,
                rawCount:  Number(chunk.raw_count) || 0,
                error:     chunk.error || '',
              },
            });
            break;
          case 'error':
            update({ text: `[error: ${chunk.message}]`, streaming: false });
            break;
          default:
            break;
        }
      }
    } catch (err) {
      // F-tab-switch-race — AbortError is a deliberate user action
      // (medic switched tabs / sent a new turn / closed the chat).
      // Treat it as a clean cancel: no "connection error" message,
      // no backendStatus update, no toast.
      const isAbort =
        (err instanceof DOMException && err.name === 'AbortError') ||
        (err as any)?.name === 'AbortError' ||
        ctrl.signal.aborted;
      if (isAbort) {
        update({ streaming: false });
        return;
      }

      // Turn "TypeError: Load failed" into something the medic can act on.
      // The Tauri/WebKit fetch wrapper throws TypeError when the TCP
      // socket can't even be opened — almost always: backend sidecar
      // not running, or we're in `pnpm dev` without a separate FastAPI.
      let message: string;
      if (err instanceof ApiError) {
        message = err.message;
      } else if (err instanceof TypeError) {
        // Re-probe so the banner above updates too.
        api.health().then(setBackendStatus);
        message =
          'Backend is unreachable. Make sure the nexus-server sidecar is ' +
          'running (or launch FastAPI on http://localhost:8001 when using `pnpm dev`).';
      } else {
        message = String(err);
      }
      update({ text: `[connection error: ${message}]`, streaming: false });
    } finally {
      setChatStreaming(sid, false);
      // Clear the in-flight controller so the next send / next
      // unmount doesn't try to abort an already-completed stream.
      if (_chatAbortBySession.get(sid) === ctrl) {
        _chatAbortBySession.delete(sid);
      }
    }
  }

  const activeSessionTitle = (() => {
    if (!activeSessionId) return 'Default chat';
    const s = sessions.find((x) => x.id === activeSessionId);
    return s?.title ?? 'New chat';
  })();

  return (
    <div className="mx-auto flex h-full max-w-2xl flex-col px-10 py-6">
      <div className="mb-4 flex items-center justify-between border-b border-border pb-3 text-caption text-text-secondary">
        <div className="flex items-center gap-3">
          <span>{patientDisplayLabel(p)}</span>
          <span className="text-text-tertiary">·</span>
          {/* Session picker — click to open the dropdown of all
              non-archived sessions (the Default chat is always last). */}
          <div className="relative">
            <button
              type="button"
              onClick={() => setShowSessionList((v) => !v)}
              className="flex items-center gap-1 rounded-sm border border-border px-2 py-0.5 hover:bg-surface-2"
              title="Switch chat session"
            >
              <span className="max-w-[180px] truncate">{activeSessionTitle}</span>
              <span className="text-text-tertiary">▾</span>
            </button>
            {showSessionList && (
              <div className="absolute left-0 top-full z-10 mt-1 max-h-72 w-64 overflow-y-auto rounded-md border border-border bg-surface-1 py-1 shadow-lg">
                <button
                  type="button"
                  onClick={() => {
                    startNewSession();
                    setShowSessionList(false);
                  }}
                  className="block w-full px-3 py-1.5 text-left text-caption hover:bg-surface-2"
                >
                  + New chat
                </button>
                <div className="my-1 border-t border-border" />
                {sessions.length === 0 ? (
                  <div className="px-3 py-1.5 text-caption text-text-tertiary">
                    No saved sessions yet
                  </div>
                ) : sessions.map((s) => (
                  <button
                    key={s.id || '__default__'}
                    type="button"
                    onClick={() => {
                      setActiveSessionId(s.id);
                      setShowSessionList(false);
                    }}
                    className={cn(
                      'block w-full px-3 py-1.5 text-left text-caption hover:bg-surface-2',
                      s.id === activeSessionId && 'bg-surface-2 font-medium',
                    )}
                  >
                    <div className="truncate">
                      {s.isDefault ? `${s.title} (legacy)` : s.title}
                    </div>
                    {s.lastMessageAt && (
                      <div className="truncate text-[10px] text-text-tertiary">
                        {s.messageCount} msgs · {s.lastMessageAt}
                      </div>
                    )}
                  </button>
                ))}
              </div>
            )}
          </div>
        </div>
        <div className="flex items-center gap-3">
          {p && (
            <TakeawaysButton
              scopeKind="patient"
              scopeRef={p.patientHash}
              tone="base"
            />
          )}
          <span>{msgs.length} messages</span>
        </div>
      </div>

      {backendStatus === 'unreachable' && (
        <div className="mb-3 flex items-center justify-between rounded-md border border-retract/40 bg-retract/5 px-3 py-2 text-caption text-retract">
          <span>
            Backend unreachable at <span className="font-mono">localhost:8001</span>.
            The nexus-server sidecar isn't responding — `pnpm tauri:dev` or
            launch the FastAPI server.
          </span>
          <button
            onClick={() => {
              setBackendStatus('checking');
              api.health().then(setBackendStatus);
            }}
            className="rounded-sm border border-retract/40 px-2 py-0.5 hover:bg-retract/10"
          >
            retry
          </button>
        </div>
      )}
      {backendStatus === 'unhealthy' && (
        <div className="mb-3 rounded-md border border-caution/40 bg-caution/5 px-3 py-2 text-caption text-caution">
          Backend reachable but unhealthy. Check the sidecar logs.
        </div>
      )}

      <div className="flex-1 space-y-6 overflow-y-auto py-4 selectable">
        {msgs.length === 0 && (
          <p className="text-center text-caption text-text-tertiary">
            Ask Nexus anything about this patient. The agent uses the
            backend's tier-classified retrieval (T1 cached / T2 single-shot
            / T3 multi-turn streamed).
          </p>
        )}
        {msgs.map((m, i) => (
          <div key={i}>
            <div className="mb-1 flex items-baseline gap-2">
              <span className="text-caption font-medium text-text-primary">
                {m.role === 'user' ? 'You' : 'Nexus'}
              </span>
              <span className="text-caption text-text-tertiary">{m.ts}</span>
              {m.tier && (
                <TierIndicator tier={m.tier} elapsedMs={m.elapsedMs} />
              )}
            </div>
            {m.role === 'agent' && m.reasoning && m.reasoning.length > 0 && (
              <ReasoningPane steps={m.reasoning} defaultOpen={m.streaming} />
            )}
            {/* F-thinking-uniform: render text + inline cursor + footer
                indicator. The footer keeps the medic informed AFTER
                the first chunk arrives (reasoning / citations are
                often still streaming for 5-15s after first text). */}
            <div className="text-body leading-relaxed text-text-primary">
              {m.text && <ChatMarkdown text={m.text} fileMap={fileMap} />}
              {m.streaming && m.text && <StreamingCursor tone="base" />}
              {m.citations?.map((c, ci) => {
                // Two kinds: graph_node (patient memory) and
                // web_source (Tavily search result). Each opens a
                // different ContextRail panel — graph_node uses the
                // existing /memory/citation lookup, web_source has
                // the URL + snippet directly on the ref.
                if (c.kind === 'web_source' && c.w_id != null) {
                  return (
                    <span key={`w-${c.w_id}-${ci}`}>{' '}
                      <a
                        href={c.url}
                        target="_blank"
                        rel="noopener noreferrer"
                        className="rounded-sm border border-accent/40 bg-accent/10 px-1 py-0.5 font-mono text-[10px] text-accent hover:bg-accent/20"
                        title={c.title ?? c.url}
                      >
                        W{c.w_id}
                      </a>
                    </span>
                  );
                }
                if (c.node_id != null) {
                  return (
                    <span key={`n-${c.node_id}-${ci}`}>{' '}
                      <CitationChip2 index={ci + 1} nodeId={c.node_id} />
                    </span>
                  );
                }
                return null;
              })}
            </div>
            {/* F-thinking-uniform: persistent footer indicator. Lives
                BELOW the body so it's visible even while body is
                full of text / citations / web cards. */}
            {m.role === 'agent' && (
              <StreamingFooter
                streaming={m.streaming}
                hasText={!!(m.text && m.text.length > 0)}
                tone="base"
              />
            )}
            {/* Memory-ingest chip. Surfaces what chat_ingester did
                with this turn so the medic doesn't have to wonder why
                "当前发现/用药" stayed empty. Three states:
                  - 已记忆 N 项     (ok=true, nodeCount>0)
                  - 本轮未记忆     (raw=0, likely API key/quota)
                  - LLM 提取了 N 但全部被丢弃 (raw>0, kept=0 — prompt issue)  */}
            {m.role === 'agent' && m.memoryIngested && (
              <div className="mt-2 text-caption">
                {m.memoryIngested.ok && m.memoryIngested.nodeCount > 0 && (
                  <span className="inline-flex items-center gap-1 rounded-sm
                                    border border-confirmed/30 bg-confirmed/10
                                    px-2 py-0.5 text-confirmed">
                    ✓ 已记忆 {m.memoryIngested.nodeCount} 项 · 病人 / 记忆 已更新
                  </span>
                )}
                {!m.memoryIngested.ok && m.memoryIngested.rawCount === 0 && (
                  <span className="inline-flex items-center gap-1 rounded-sm
                                    border border-caution/30 bg-caution/10
                                    px-2 py-0.5 text-caution"
                        title={m.memoryIngested.error || '提取器未返回任何实体，多半是 LLM API key/quota 问题，请检查 Settings · LLM'}>
                    ⚠ 本轮未记忆（提取器无返回）
                  </span>
                )}
                {!m.memoryIngested.ok && m.memoryIngested.rawCount > 0 && (
                  <span className="inline-flex items-center gap-1 rounded-sm
                                    border border-caution/30 bg-caution/10
                                    px-2 py-0.5 text-caution"
                        title="LLM 提取出了实体，但每一条的 evidence_quote 都通不过 verbatim 校验（连 fuzzy_rescue 也救不回来）。多半是 extractor 提示词需要再宽松一些。">
                    ⚠ LLM 提取了 {m.memoryIngested.rawCount} 条但全部被弃用
                  </span>
                )}
              </div>
            )}
            {/* Web search results card — renders once Tavily returns
                a payload, BEFORE the LLM synthesis chunks land. Gives
                the medic a preview of what got grounded so the wait
                feels productive instead of opaque. */}
            {m.role === 'agent' && m.webResults && m.webResults.length > 0 && (
              <div className="mt-3 rounded-md border border-border bg-surface/40 p-3 text-caption">
                <div className="mb-2 text-text-secondary">
                  🔎 {m.webResults.length} source{m.webResults.length === 1 ? '' : 's'}
                </div>
                <ul className="space-y-1.5">
                  {m.webResults.map((r) => (
                    <li key={r.w_id} className="flex items-start gap-2">
                      <span className="mt-0.5 font-mono text-[10px] text-accent">[W{r.w_id}]</span>
                      <div className="min-w-0 flex-1">
                        <a
                          href={r.url}
                          target="_blank"
                          rel="noopener noreferrer"
                          className="block truncate text-text-primary hover:underline"
                          title={r.url}
                        >
                          {r.title}
                        </a>
                        <div className="truncate text-[11px] text-text-tertiary">
                          {r.domain}
                        </div>
                      </div>
                    </li>
                  ))}
                </ul>
              </div>
            )}
            {/* Scheduled-task confirmation card. Renders under the
                agent message that carries the proposal. The card writes
                its UI state into the zustand store (per F-chat-state-
                persist) — confirmation stamps the proposal as 'done'
                and ALSO leaves it attached so the medic sees the green
                "scheduled" line until they cancel the session. */}
            {m.role === 'agent' && m.proposal && (
              <ScheduleProposalCard
                proposal={m.proposal}
                onConfirm={async (edited) => {
                  // Phase 1: only send_email is supported. Pack edits
                  // into the payload and POST /schedule/confirm.
                  const toList = edited.to
                    .split(',').map((s) => s.trim()).filter(Boolean);
                  // F-chat-state-persist — read live msgs via
                  // ``getState()`` so a chunk that landed between
                  // render and click is still seen.
                  const mutate = (
                    fn: (mm: ChatMsg) => ChatMsg,
                  ) => {
                    const cur = useAppState.getState()
                      .chatMsgsBySession[effectiveSessionId] ?? [];
                    setChatMsgs(effectiveSessionId,
                      cur.map((mm, mi) => mi === i ? fn(mm) : mm));
                  };
                  // Move to 'submitting' for the spinner.
                  mutate((mm) => mm.proposal
                    ? { ...mm, proposal: { ...mm.proposal, uiState: 'submitting', errorMsg: undefined } }
                    : mm);
                  try {
                    const userTz = (() => {
                      try { return Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC'; }
                      catch { return 'UTC'; }
                    })();
                    await api.confirmScheduledTask({
                      kind: 'send_email',
                      payload: {
                        to: toList,
                        subject: edited.subject,
                        body: edited.body,
                      },
                      fireAt: m.proposal!.fireAt,
                      userTz,
                      sessionId: m.proposal!.sessionId,
                      patientHash: m.proposal!.patientHash,
                      proposalId: m.proposal!.proposalId,
                    });
                    mutate((mm) => mm.proposal
                      ? { ...mm, proposal: { ...mm.proposal, uiState: 'done' } }
                      : mm);
                    showToast(t('sched.scheduledToast', {
                      when: new Date(m.proposal!.fireAt * 1000).toLocaleString(),
                    }), 'success');
                  } catch (e) {
                    const errMsg = e instanceof Error ? e.message : String(e);
                    mutate((mm) => mm.proposal
                      ? { ...mm, proposal: { ...mm.proposal, uiState: 'editing', errorMsg: errMsg } }
                      : mm);
                  }
                }}
                onCancel={() => {
                  // Dismiss the card; the SCHEDULED_TASK_PROPOSED audit
                  // event was already emitted server-side. No persist call.
                  const cur = useAppState.getState()
                    .chatMsgsBySession[effectiveSessionId] ?? [];
                  setChatMsgs(effectiveSessionId, cur.map((mm, mi) =>
                    mi === i && mm.proposal
                      ? { ...mm, proposal: { ...mm.proposal, uiState: 'cancelled' } }
                      : mm,
                  ));
                }}
              />
            )}
            {/* Attached-file chips on user turns. Read-only — the
                actual bytes are in uploads + (for DICOM) the imaging
                tab; this row is just to show the medic what they
                sent. */}
            {m.role === 'user' && m.attachedFileNames && m.attachedFileNames.length > 0 && (
              <div className="mt-1 flex flex-wrap gap-1">
                {m.attachedFileNames.map((name, fi) => (
                  <span
                    key={fi}
                    className="rounded-sm border border-border bg-surface-1 px-1.5 py-0.5 text-[10px] text-text-tertiary"
                  >
                    📎 {name}
                  </span>
                ))}
              </div>
            )}
          </div>
        ))}
      </div>

      {/* Composer — paste / drop aware. Drag a file onto the textarea
          OR Cmd+V a screen capture / image off the web, and it gets
          uploaded + attached to the next send. */}
      <div className="mt-4 border-t border-border pt-4" onDrop={onDrop} onDragOver={(e) => e.preventDefault()}>
        {/* F-unified-chat-files — persistent file library for THIS
            patient. Lives above the composer so the medic always sees
            which files the AI has access to (📂 chip + click for
            full drawer). Distinct from `attachments` below, which is
            the EPHEMERAL "this turn's uploads-in-flight" strip that
            clears on send. */}
        {p && (
          <div className="mb-2">
            <ChatFileChipStrip
              scopeKind="patient"
              scopeRef={p.patientHash}
              controller={encounterChatFiles}
            />
          </div>
        )}
        {/* Pending attachments — show chips above the input so the
            medic can verify what's going out before they press Send. */}
        {attachments.length > 0 && (
          <div className="mb-2 flex flex-wrap gap-1.5">
            {attachments.map((a) => {
              const stateCls = a.failed
                ? 'border-retract/40 bg-retract/10 text-retract'
                : a.fileId
                ? 'border-confirmed/40 bg-confirmed/10 text-confirmed'
                : 'border-border bg-surface-1 text-text-tertiary';
              const stateBadge = a.failed ? '✕' : a.fileId ? '✓' : '⟳';
              if (a.previewUrl) {
                // Image: render a 56px thumbnail with state badge +
                // remove button overlaid. Lets the medic confirm the
                // CT screenshot / lab result image they just dropped
                // BEFORE pressing send.
                return (
                  <div
                    key={a.key}
                    className={cn(
                      'relative w-14 h-14 rounded overflow-hidden border',
                      stateCls,
                    )}
                    title={`${a.name} · ${formatBytes(a.sizeBytes)}`}
                  >
                    <img src={a.previewUrl} alt={a.name}
                         className="w-full h-full object-cover" />
                    <span className="absolute top-0.5 left-0.5 px-1 py-0
                                     rounded bg-black/60 text-white
                                     text-[10px] leading-tight">
                      {stateBadge}
                    </span>
                    <button
                      type="button"
                      onClick={() => removeAttachment(a.key)}
                      className="absolute top-0.5 right-0.5 w-4 h-4
                                 rounded-full bg-black/60 text-white
                                 text-[10px] leading-none flex items-center
                                 justify-center hover:bg-black/80"
                      aria-label={`remove ${a.name}`}
                    >
                      ×
                    </button>
                  </div>
                );
              }
              return (
                <span
                  key={a.key}
                  className={cn(
                    'flex items-center gap-1 rounded-sm border px-2 py-0.5 text-caption',
                    stateCls,
                  )}
                >
                  {stateBadge} {a.name}
                  <span className="text-[10px] text-text-tertiary">
                    ({formatBytes(a.sizeBytes)})
                  </span>
                  <button
                    type="button"
                    onClick={() => removeAttachment(a.key)}
                    className="ml-1 text-text-tertiary hover:text-retract"
                    aria-label={`remove ${a.name}`}
                  >
                    ×
                  </button>
                </span>
              );
            })}
          </div>
        )}
        <div className="flex gap-2">
          <textarea
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter' && !e.shiftKey) {
                e.preventDefault();
                send();
              }
            }}
            onPaste={onPaste}
            placeholder="Ask anything about this patient… (paste images or drop files here)"
            disabled={sending}
            rows={2}
            className="flex-1 resize-none rounded-md border border-border bg-bg px-3 py-2 text-body text-text-primary placeholder:text-text-tertiary focus:border-border-strong focus:outline-none"
          />
          <Button variant="primary" onClick={send} disabled={sending}
                  className="!px-5 !py-2">
            {sending ? '…' : 'Send'}
          </Button>
        </div>
      </div>
    </div>
  );
}

/** Render a backend Unix-seconds timestamp as a short relative string
 *  ("3m ago" / "2h ago" / "Jun 14"). Used by the chat-history hydration
 *  path; live messages keep their server-issued ``"now"`` label. */
function formatRelativeTs(ts: number): string {
  if (!ts) return '';
  const now = Date.now() / 1000;
  const dt = Math.max(0, now - ts);
  if (dt < 60)       return `${Math.floor(dt)}s ago`;
  if (dt < 60 * 60)  return `${Math.floor(dt / 60)}m ago`;
  if (dt < 60 * 60 * 24) return `${Math.floor(dt / 3600)}h ago`;
  const d = new Date(ts * 1000);
  return d.toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
}

/* ─────────────── Memory mode (layered, per m3-memory-architecture.md) ─────────────── */

type LayerKey = 'L1' | 'L2' | 'L3' | 'meta';

interface LayerMeta {
  key: LayerKey;
  title: string;
  scope: string;           // chip — "per patient · PHI" etc.
  blurb: string;           // one-line explanation of what this layer holds.
}

/** Build the layered Memory-mode meta from the active locale.
 *  We expose this as a hook (not a frozen constant) so the strings
 *  are re-evaluated on locale change. The shape stays identical to
 *  the original LAYERS constant — only the source of strings moves. */
function useLayers(): LayerMeta[] {
  const t = useT();
  return [
    {
      key: 'L1',
      title: t('memory.layer1.title'),
      scope: t('memory.layer1.tag'),
      blurb: t('memory.layer1.empty'),
    },
    {
      key: 'L2',
      title: t('memory.layer2.title'),
      scope: t('memory.layer2.tag'),
      blurb: t('memory.layer2.intro'),
    },
    {
      key: 'L3',
      title: t('memory.layer3.title'),
      scope: t('memory.layer3.tag'),
      blurb: t('memory.layer3.empty'),
    },
    {
      key: 'meta',
      title: t('memory.meta.title'),
      scope: t('memory.meta.tag'),
      blurb: t('memory.meta.tag'),
    },
  ];
}

/* node_type → human label + visual variant for L1 grouping */
const NODE_KIND_LABEL: Record<string, string> = {
  finding: 'Findings',
  med: 'Medications',
  ddx: 'Differentials',
  study: 'Studies',
  semantic_fact: 'Semantic facts',
  measurement: 'Measurements',
  lab: 'Labs',
  key_image: 'Key images',
  anatomical_region: 'Anatomical regions',
  episodic_event: 'Episodic events',
};

function LayerHeader({ meta, count }: { meta: LayerMeta; count?: number }) {
  return (
    <div className="mb-3 flex flex-wrap items-baseline gap-x-3 gap-y-1">
      <h2 className="font-display text-section text-text-primary">
        {meta.title}
        {count !== undefined && (
          <span className="ml-2 font-mono text-caption text-text-tertiary">
            ({count})
          </span>
        )}
      </h2>
      <Chip mono>{meta.scope}</Chip>
      <p className="basis-full text-caption text-text-secondary leading-relaxed">
        {meta.blurb}
      </p>
    </div>
  );
}

function LayerBand({
  meta,
  count,
  defaultOpen = true,
  children,
}: {
  meta: LayerMeta;
  count?: number;
  defaultOpen?: boolean;
  children: React.ReactNode;
}) {
  const [open, setOpen] = useState(defaultOpen);
  return (
    <section className="mb-8 rounded-md border border-border bg-surface/40">
      <button
        onClick={() => setOpen((o) => !o)}
        className="flex w-full items-start gap-3 px-5 pt-4 pb-2 text-left hover:bg-accent-subtle/30"
      >
        <span className="mt-1 font-mono text-caption text-text-tertiary">
          {open ? '▾' : '▸'}
        </span>
        <div className="flex-1">
          <LayerHeader meta={meta} count={count} />
        </div>
      </button>
      {open && <div className="px-5 pb-5 pt-1">{children}</div>}
    </section>
  );
}

function L1NodeGroup({
  kind,
  nodes,
}: {
  kind: string;
  nodes: GraphNodeOut[];
}) {
  if (nodes.length === 0) return null;
  return (
    <div className="mb-4">
      <div className="mb-1 flex items-center gap-2">
        <span className="text-caption font-medium text-text-primary">
          {NODE_KIND_LABEL[kind] ?? kind}
        </span>
        <span className="font-mono text-caption text-text-tertiary">
          ({nodes.length})
        </span>
      </div>
      <ul className="space-y-1 pl-3">
        {nodes.map((n) => {
          const c = n.content as Record<string, unknown>;
          const label =
            (c.label as string) ??
            (c.modality as string) ??
            (c.study_date as string) ??
            (c.name as string) ??
            `node #${n.nodeId}`;
          const detailParts: string[] = [];
          if (typeof c.size_cm === 'number' || typeof c.size_cm === 'string')
            detailParts.push(`${c.size_cm} cm`);
          if (kind === 'study' && typeof c.body_part === 'string')
            detailParts.push(c.body_part);
          if (kind === 'lab' && typeof c.value === 'string')
            detailParts.push(c.value);
          return (
            <li key={n.nodeId} className="flex items-center gap-2 text-body">
              <span className="text-text-tertiary">•</span>
              <span className="text-text-primary">{label}</span>
              {detailParts.map((d, i) => (
                <Chip key={i} variant="neutral">{d}</Chip>
              ))}
              <CitationChip2 index={n.nodeId} nodeId={n.nodeId} />
            </li>
          );
        })}
      </ul>
    </div>
  );
}

function L1PatientGraph({ proj }: { proj: PatientProjection }) {
  // Group the projection arrays into one rendering loop so empty groups
  // just disappear instead of showing "(0)" rows everywhere.
  const groups: { kind: string; nodes: GraphNodeOut[] }[] = [
    { kind: 'finding',       nodes: proj.findings },
    { kind: 'med',           nodes: proj.medications },
    { kind: 'ddx',           nodes: proj.differentials },
    { kind: 'study',         nodes: proj.studies },
    { kind: 'semantic_fact', nodes: proj.semanticFacts },
  ];
  const total = groups.reduce((a, g) => a + g.nodes.length, 0);

  if (total === 0) {
    return (
      <p className="text-caption text-text-tertiary">
        No nodes yet. Drop a DICOM study, chat in Encounter, or paste a lab
        report — every ingester writes here.
      </p>
    );
  }
  return (
    <>
      {groups.map((g) => (
        <L1NodeGroup key={g.kind} kind={g.kind} nodes={g.nodes} />
      ))}
    </>
  );
}

const FACT_KIND_LABEL: Record<string, string> = {
  style:       'Style',
  workflow:    'Workflow',
  practice:    'Practice',
  calibration: 'Calibration',
};

function L2Practitioner() {
  const t = useT();
  const [cands, setCands] = useState<PractitionerCandidate[] | null>(null);
  const [err,   setErr]   = useState<string | null>(null);
  const openOverlay = useAppState((s) => s.openPractitionerOverlay);

  useEffect(() => {
    let cancelled = false;
    api.listPractitionerCandidates().then(
      (r) => { if (!cancelled) setCands(r); },
      (e) => { if (!cancelled) setErr(String(e)); },
    );
    return () => { cancelled = true; };
  }, []);

  if (err) {
    return <p className="text-caption text-retract">{t('patient.loadFailed', { error: err })}</p>;
  }
  if (!cands) {
    return <p className="text-caption text-text-tertiary">{t('patient.loading')}</p>;
  }
  if (cands.length === 0) {
    return (
      <p className="text-caption text-text-tertiary">
        {t('memory.layer2.empty')}
      </p>
    );
  }

  // Group by fact_kind.
  const byKind = new Map<string, PractitionerCandidate[]>();
  for (const c of cands) {
    if (!byKind.has(c.factKind)) byKind.set(c.factKind, []);
    byKind.get(c.factKind)!.push(c);
  }

  return (
    <>
      {Array.from(byKind.entries()).map(([kind, items]) => (
        <div key={kind} className="mb-4">
          <div className="mb-1 flex items-center gap-2">
            <span className="text-caption font-medium text-text-primary">
              {FACT_KIND_LABEL[kind] ?? kind}
            </span>
            <span className="font-mono text-caption text-text-tertiary">
              ({items.length})
            </span>
          </div>
          <ul className="space-y-1 pl-3">
            {items.slice(0, 5).map((c) => (
              <li key={`${c.factKind}:${c.patternKey}`}
                  className="flex items-center gap-2 text-body">
                <span className="text-text-tertiary">•</span>
                <span className="text-text-primary truncate">{c.patternKey}</span>
                <Chip variant="neutral">
                  {c.distinctPatientCount} pt · conf {c.confidence.toFixed(2)}
                </Chip>
              </li>
            ))}
          </ul>
        </div>
      ))}
      <div className="mt-3">
        <Button variant="subtle" onClick={openOverlay}>
          Review & confirm →
        </Button>
      </div>
    </>
  );
}

const REFERENCE_SHELVES = [
  { id: 'nccn',   label: 'NCCN / ACR-AC',    note: 'Imaging-appropriateness + oncology guidelines' },
  { id: 'rxnorm', label: 'RxNorm',           note: 'Drug normalisation + interaction graph' },
  { id: 'radlex', label: 'RadLex',           note: 'Radiology terminology' },
  { id: 'snomed', label: 'SNOMED-CT',        note: 'Clinical findings + procedures' },
  { id: 'icd',    label: 'ICD / CPT',        note: 'Coding for billing + reporting' },
  { id: 'labs',   label: 'Lab ranges',       note: 'Age / sex stratified reference intervals' },
];

function L3Reference() {
  return (
    <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
      {REFERENCE_SHELVES.map((s) => (
        <Card key={s.id} className="!p-3">
          <div className="flex items-center justify-between">
            <span className="text-caption font-medium text-text-primary">
              {s.label}
            </span>
            <Chip variant="neutral">not yet populated</Chip>
          </div>
          <p className="mt-1 text-caption text-text-secondary">{s.note}</p>
        </Card>
      ))}
      <p className="col-span-full mt-1 text-caption text-text-tertiary">
        Schema lives; population is a separate workstream (M4). Once
        populated, Layer 3 snippets are composed into the system prompt
        whenever a turn cites a guideline-aware tool.
      </p>
    </div>
  );
}

function MetaLayer() {
  const items = [
    { label: 'Prompt versions',         note: 'Extraction template revisions' },
    { label: 'Tier thresholds',         note: 'T1 / T2 / T3 classifier cutoffs' },
    { label: 'Evidence-rank tuning',    note: 'Composer weight per source kind' },
    { label: 'Cached-view recipes',     note: 'Which projections are precomputed' },
    { label: 'Conflict thresholds',     note: 'Per-finding-type retraction sensitivity' },
  ];
  return (
    <ul className="space-y-1 text-body">
      {items.map((i) => (
        <li key={i.label} className="flex items-center gap-2">
          <span className="text-text-tertiary">•</span>
          <span className="text-text-primary">{i.label}</span>
          <span className="text-caption text-text-secondary">— {i.note}</span>
        </li>
      ))}
      <li className="mt-2 text-caption text-text-tertiary">
        Surfaces here read-only for now; tuning UI is Settings → Evolution
        (M5). See <span className="font-mono">docs/design/nexus-architecture.md</span>.
      </li>
    </ul>
  );
}

function RetrievalTierLegend() {
  const rows: { tier: TierKind; label: string; budget: string; example: string }[] = [
    { tier: 'T1', label: 'cached view',         budget: '≤ 50 ms',  example: '"how many studies?"' },
    { tier: 'T2', label: 'single-shot lookup',  budget: '≤ 300 ms', example: '"latest creatinine"' },
    { tier: 'T3', label: 'multi-turn reasoning', budget: '5–15 s',   example: '"what changed since the prior CT?"' },
  ];
  return (
    <div className="rounded-md border border-border bg-bg/40 px-4 py-3">
      <div className="mb-2 text-[10px] uppercase tracking-wider text-text-tertiary">
        Retrieval tiers (how a turn composes the layers above)
      </div>
      <ul className="space-y-1">
        {rows.map((r) => (
          <li key={r.tier} className="flex flex-wrap items-center gap-2 text-caption">
            <TierIndicator tier={r.tier} />
            <span className="text-text-primary">{r.label}</span>
            <span className="text-text-tertiary">·</span>
            <span className="font-mono text-text-secondary">{r.budget}</span>
            <span className="text-text-tertiary">·</span>
            <span className="italic text-text-tertiary">{r.example}</span>
          </li>
        ))}
      </ul>
    </div>
  );
}

export function MemoryMode() {
  const t = useT();
  const layers = useLayers();
  const p = useAppState((s) => s.activePatient);
  const setActiveMode = useAppState((s) => s.setActiveMode);
  const [proj, setProj] = useState<PatientProjection | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!p) return;
    let cancelled = false;
    setProj(null);
    setError(null);
    api.getPatientProjection(p.patientHash).then(
      (r) => { if (!cancelled) setProj(r); },
      (e) => { if (!cancelled) setError(String(e)); },
    );
    return () => { cancelled = true; };
  }, [p]);

  if (!p) return <EmptyState title={t('memory.noSelection')} />;

  const l1Count = proj
    ? proj.findings.length + proj.medications.length + proj.differentials.length +
      proj.studies.length + proj.semanticFacts.length
    : undefined;

  return (
    <div className="mx-auto max-w-4xl px-10 py-12 selectable">
      <h1 className="font-display text-display text-text-primary">
        {t('memory.title')} · {patientDisplayLabel(p)}
      </h1>

      {proj && proj.unresolvedConflictCount > 0 && (
        <div className="mt-4">
          <ConflictInlineBanner
            count={proj.unresolvedConflictCount}
            onResolve={() => setActiveMode('memory')}
          />
        </div>
      )}

      {error && (
        <p className="mt-4 text-caption text-retract">{t('patient.loadFailed', { error })}</p>
      )}

      <div className="mt-8">
        <LayerBand meta={layers[0]} count={l1Count}>
          {!proj
            ? <p className="text-caption text-text-tertiary">{t('patient.loading')}</p>
            : <L1PatientGraph proj={proj} />}
        </LayerBand>

        <LayerBand meta={layers[1]} defaultOpen={false}>
          <L2Practitioner />
        </LayerBand>

        <LayerBand meta={layers[2]} defaultOpen={false}>
          <L3Reference />
        </LayerBand>

        <LayerBand meta={layers[3]} defaultOpen={false}>
          <MetaLayer />
        </LayerBand>
      </div>

      <div className="mt-8">
        <RetrievalTierLegend />
      </div>
    </div>
  );
}

/* ─────────────── Report mode (structured impression export) ─────────────── */

interface ReportDraft {
  clinicalInfo: string;
  selectedFindings: Set<number>;
  selectedDdx: Set<number>;
  impression: string;
  recommendation: string;
}

function buildImpressionDefault(proj: PatientProjection): string {
  if (proj.findings.length === 0) return '';
  const lines = proj.findings.slice(0, 5).map((f) => {
    const c = f.content as Record<string, unknown>;
    const label = (c.label as string) ?? '?';
    const size  = c.size_cm != null ? ` (${c.size_cm} cm)` : '';
    return `• ${label}${size}`;
  });
  return lines.join('\n');
}

function buildFhirDiagnosticReport(
  patientLabel: string,
  patientHash: string,
  proj: PatientProjection,
  draft: ReportDraft,
): Record<string, unknown> {
  const now = new Date().toISOString();
  const pick = (arr: GraphNodeOut[], picked: Set<number>) =>
    arr.filter((n) => picked.has(n.nodeId));
  const findings = pick(proj.findings,      draft.selectedFindings);
  const ddx      = pick(proj.differentials, draft.selectedDdx);
  return {
    resourceType: 'DiagnosticReport',
    status: 'preliminary',
    code: {
      coding: [
        {
          system: 'http://loinc.org',
          code: '18748-4',
          display: 'Diagnostic imaging report',
        },
      ],
    },
    subject: {
      identifier: { system: 'urn:rune:patient-hash', value: patientHash },
      display: patientLabel,
    },
    effectiveDateTime: now,
    issued: now,
    conclusion: draft.impression,
    conclusionCode: ddx.map((d) => ({
      text: (d.content as any).label ?? `node ${d.nodeId}`,
    })),
    result: findings.map((f) => ({
      reference: `Observation/${f.nodeId}`,
      display: (f.content as any).label ?? `node ${f.nodeId}`,
    })),
    presentedForm: [
      {
        contentType: 'text/plain',
        title: 'Clinical info',
        data: btoa(unescape(encodeURIComponent(draft.clinicalInfo))),
      },
      {
        contentType: 'text/plain',
        title: 'Recommendation',
        data: btoa(unescape(encodeURIComponent(draft.recommendation))),
      },
    ],
    extension: [
      {
        url: 'urn:rune:provenance-node-ids',
        valueString: findings.map((f) => f.nodeId).join(','),
      },
    ],
  };
}

function buildDicomSrStub(
  patientLabel: string,
  patientHash: string,
  proj: PatientProjection,
  draft: ReportDraft,
): Record<string, unknown> {
  // True DICOM SR (Part 3, TID 2000 "Basic Diagnostic Imaging Report") is
  // a binary DICOM dataset. The encoding requires pydicom on the server,
  // so for U3 we emit the SR content tree as JSON; backend M3.2 will turn
  // this into a real .dcm via tools/dicom_sr_writer.py.
  const pick = (arr: GraphNodeOut[], picked: Set<number>) =>
    arr.filter((n) => picked.has(n.nodeId));
  const findings = pick(proj.findings,      draft.selectedFindings);
  const ddx      = pick(proj.differentials, draft.selectedDdx);

  return {
    SOPClassUID:  '1.2.840.10008.5.1.4.1.1.88.33', // Comprehensive SR
    SOPInstanceUID: `urn:rune:sr:${patientHash}:${Date.now()}`,
    PatientID:    patientHash,
    PatientName:  patientLabel,
    StudyDate:    new Date().toISOString().slice(0, 10).replace(/-/g, ''),
    ContentTemplateSequence: [
      { TemplateIdentifier: '2000', MappingResource: 'DCMR' },
    ],
    ContentSequence: [
      {
        ValueType:  'TEXT',
        ConceptNameCodeSequence: [{ CodeValue: '121060', CodingSchemeDesignator: 'DCM', CodeMeaning: 'History' }],
        TextValue:  draft.clinicalInfo,
      },
      {
        ValueType:  'CONTAINER',
        ConceptNameCodeSequence: [{ CodeValue: '121070', CodingSchemeDesignator: 'DCM', CodeMeaning: 'Findings' }],
        ContinuityOfContent: 'SEPARATE',
        ContentSequence: findings.map((f) => ({
          ValueType: 'TEXT',
          ConceptNameCodeSequence: [{ CodeValue: '121071', CodingSchemeDesignator: 'DCM', CodeMeaning: 'Finding' }],
          TextValue: (f.content as any).label ?? `node ${f.nodeId}`,
        })),
      },
      {
        ValueType:  'TEXT',
        ConceptNameCodeSequence: [{ CodeValue: '121072', CodingSchemeDesignator: 'DCM', CodeMeaning: 'Impression' }],
        TextValue:  draft.impression,
      },
      {
        ValueType:  'CONTAINER',
        ConceptNameCodeSequence: [{ CodeValue: '121074', CodingSchemeDesignator: 'DCM', CodeMeaning: 'Differential diagnosis' }],
        ContinuityOfContent: 'SEPARATE',
        ContentSequence: ddx.map((d) => ({
          ValueType: 'TEXT',
          ConceptNameCodeSequence: [{ CodeValue: '121075', CodingSchemeDesignator: 'DCM', CodeMeaning: 'Differential' }],
          TextValue: (d.content as any).label ?? `node ${d.nodeId}`,
        })),
      },
      {
        ValueType:  'TEXT',
        ConceptNameCodeSequence: [{ CodeValue: '121076', CodingSchemeDesignator: 'DCM', CodeMeaning: 'Recommendation' }],
        TextValue:  draft.recommendation,
      },
    ],
    _note: 'JSON content tree — backend M3.2 emits the real .dcm via pydicom.',
  };
}

function downloadBlob(filename: string, mime: string, body: string) {
  const blob = new Blob([body], { type: mime });
  const url  = URL.createObjectURL(blob);
  const a    = document.createElement('a');
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

function ReportToggleList({
  title,
  nodes,
  selected,
  onToggle,
}: {
  title: string;
  nodes: GraphNodeOut[];
  selected: Set<number>;
  onToggle: (id: number) => void;
}) {
  if (nodes.length === 0) {
    return (
      <div className="mb-4">
        <div className="mb-1 text-caption font-medium text-text-primary">{title}</div>
        <p className="pl-3 text-caption text-text-tertiary">None on file.</p>
      </div>
    );
  }
  return (
    <div className="mb-4">
      <div className="mb-1 flex items-center gap-2">
        <span className="text-caption font-medium text-text-primary">{title}</span>
        <span className="font-mono text-caption text-text-tertiary">
          ({selected.size}/{nodes.length})
        </span>
      </div>
      <ul className="space-y-1 pl-1">
        {nodes.map((n) => {
          const c     = n.content as Record<string, unknown>;
          const label = (c.label as string) ?? `node ${n.nodeId}`;
          const size  = (c.size_cm as number | string | undefined);
          const isOn  = selected.has(n.nodeId);
          return (
            <li key={n.nodeId}>
              <label className="flex cursor-pointer items-center gap-2 rounded-sm px-2 py-1 hover:bg-accent-subtle">
                <input
                  type="checkbox"
                  checked={isOn}
                  onChange={() => onToggle(n.nodeId)}
                  className="accent-accent"
                />
                <span className="text-body text-text-primary">{label}</span>
                {size != null && <Chip variant="neutral">{size} cm</Chip>}
                <CitationChip2 index={n.nodeId} nodeId={n.nodeId} />
              </label>
            </li>
          );
        })}
      </ul>
    </div>
  );
}

/** Last PDF the medic just exported. Renders the "Last report" card
 *  with path + Open Folder + size + timestamp so the medic always
 *  knows where the file went — fix for the previous window.print()
 *  flow that produced no file path and no UI feedback. */
interface LastReport {
  path: string;
  bytes: number;
  createdAt: number;
}

/** Open a path in the OS file manager. We dynamic-import the Tauri
 *  shell plugin so this file still loads under `pnpm dev` outside
 *  the Tauri shell (where the import would throw). Mirrors the same
 *  helper used by Settings · Data export. */
async function openPathInOsShell(path: string): Promise<boolean> {
  try {
    const mod = await import('@tauri-apps/plugin-shell');
    if (mod && typeof mod.open === 'function') {
      await mod.open(path);
      return true;
    }
  } catch {
    /* not in Tauri runtime */
  }
  return false;
}

/** Human-readable byte size — matches the helper in
 *  components/full-screen-overlays.tsx for symmetric "Last export"
 *  / "Last report" card display. */
function humanBytes(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  if (n < 1024 * 1024 * 1024) return `${(n / 1024 / 1024).toFixed(1)} MB`;
  return `${(n / 1024 / 1024 / 1024).toFixed(2)} GB`;
}

export function ReportMode() {
  const t = useT();
  const locale = useAppState((s) => s.locale);
  const p = useAppState((s) => s.activePatient);
  const showToast = useAppState((s) => s.showToast);
  const [proj, setProj] = useState<PatientProjection | null>(null);
  const [error, setError] = useState<string | null>(null);
  // PDF export state — shows in the export rail's "Last report" card
  // after a successful export. Survives until next export or mode
  // change; not persisted (the card is a transient hint, not history).
  const [exporting, setExporting] = useState(false);
  const [lastReport, setLastReport] = useState<LastReport | null>(null);

  const [draft, setDraft] = useState<ReportDraft>(() => ({
    clinicalInfo: '',
    selectedFindings: new Set(),
    selectedDdx: new Set(),
    impression: '',
    recommendation: '',
  }));

  useEffect(() => {
    if (!p) return;
    let cancelled = false;
    setProj(null);
    setError(null);
    api.getPatientProjection(p.patientHash).then(
      (r) => {
        if (cancelled) return;
        setProj(r);
        // Pre-fill: select every finding + ddx so the medic deselects
        // what they don't want rather than building the list from zero.
        setDraft((d) => ({
          ...d,
          selectedFindings: new Set(r.findings.map((f) => f.nodeId)),
          selectedDdx:      new Set(r.differentials.map((dx) => dx.nodeId)),
          impression: d.impression || buildImpressionDefault(r),
        }));
      },
      (e) => { if (!cancelled) setError(String(e)); },
    );
    return () => { cancelled = true; };
  }, [p]);

  const patientLabel = useMemo(() => (p ? patientDisplayLabel(p) : ''), [p]);

  if (!p) return <EmptyState title={t('report.noSelection')} />;
  if (error) return <p className="p-10 text-caption text-retract">{t('patient.loadFailed', { error })}</p>;
  if (!proj) return <p className="p-10 text-caption text-text-tertiary">{t('patient.loading')}</p>;

  const toggle = (set: Set<number>, id: number) => {
    const next = new Set(set);
    if (next.has(id)) next.delete(id); else next.add(id);
    return next;
  };

  // Exports ───────────────────────────────────────────────────────────
  //
  // PDF: hits the server's POST /api/v1/report/pdf which renders via
  // reportlab and writes to <Archive>/Reports/<hash>-<ts>.pdf. The
  // server returns {path, bytes, createdAt} so we can populate the
  // "Last report" card below and reveal the file in the OS finder.
  //
  // Why server-side render: WKWebView (Tauri's renderer) doesn't
  // surface a usable print dialog to the embedding app. window.print()
  // was a silent no-op in production and produced no path feedback,
  // which is exactly the symptom the medic reported 2026-06-14.
  const exportPdf = async () => {
    if (exporting) return;
    setExporting(true);
    try {
      const r = await api.exportReportPdf({
        patientHash:     p.patientHash,
        patientLabel:    patientLabel,
        patientSex:      p.sex ?? '',
        patientAgeGroup: p.ageGroup ?? '',
        latestModality:  p.latestModality ?? '',
        latestStudyDt:   p.latestStudyDate ?? '',
        clinicalInfo:    draft.clinicalInfo,
        impression:      draft.impression,
        recommendation:  draft.recommendation,
        findings: proj.findings
          .filter((f) => draft.selectedFindings.has(f.nodeId))
          .map((f) => ({
            nodeId:  f.nodeId,
            label:   String((f.content as any).label ?? `node ${f.nodeId}`),
            urgency: String((f.content as any).urgency ?? ''),
          })),
        differentials: proj.differentials
          .filter((d) => draft.selectedDdx.has(d.nodeId))
          .map((d) => ({
            nodeId:  d.nodeId,
            label:   String((d.content as any).label ?? `node ${d.nodeId}`),
            urgency: '',
          })),
        locale: locale,
      });
      setLastReport({ path: r.path, bytes: r.bytes, createdAt: r.createdAt });
      showToast(
        t('report.exportedToast', { size: humanBytes(r.bytes) }),
        'success',
      );
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e);
      showToast(t('report.exportFailed', { error: msg }), 'error');
    } finally {
      setExporting(false);
    }
  };

  const exportFhir = () => {
    const doc = buildFhirDiagnosticReport(patientLabel, p.patientHash, proj, draft);
    downloadBlob(
      `diagnostic-report-${p.patientHash.slice(0, 8)}.json`,
      'application/fhir+json',
      JSON.stringify(doc, null, 2),
    );
    showToast('FHIR DiagnosticReport downloaded', 'success');
  };

  const exportSr = () => {
    const doc = buildDicomSrStub(patientLabel, p.patientHash, proj, draft);
    downloadBlob(
      `dicom-sr-${p.patientHash.slice(0, 8)}.json`,
      'application/json',
      JSON.stringify(doc, null, 2),
    );
    showToast('DICOM SR content tree downloaded (JSON; M3.2 → .dcm)', 'success');
  };

  // Renders ────────────────────────────────────────────────────────────
  return (
    <div className="mx-auto grid max-w-5xl grid-cols-1 gap-8 px-10 py-12 lg:grid-cols-[1fr_360px]">
      {/* LEFT: composer */}
      <div className="selectable">
        <h1 className="font-display text-display text-text-primary">
          Report · {patientLabel}
        </h1>
        <p className="mt-2 text-caption text-text-secondary">
          Structured impression composed from Layer 1 evidence. Every node
          you keep carries its citation into the export.
        </p>

        <Section title="Clinical information">
          <textarea
            value={draft.clinicalInfo}
            onChange={(e) => setDraft((d) => ({ ...d, clinicalInfo: e.target.value }))}
            rows={3}
            placeholder="Indication, prior treatment, comparison study…"
            className="w-full rounded-sm border border-border bg-surface px-3 py-2 text-body text-text-primary placeholder:text-text-tertiary focus:border-accent focus:outline-none focus:ring-2 focus:ring-accent/30"
          />
        </Section>

        <Section title="Findings">
          <ReportToggleList
            title="From Layer 1"
            nodes={proj.findings}
            selected={draft.selectedFindings}
            onToggle={(id) =>
              setDraft((d) => ({ ...d, selectedFindings: toggle(d.selectedFindings, id) }))
            }
          />
        </Section>

        <Section title="Impression">
          <textarea
            value={draft.impression}
            onChange={(e) => setDraft((d) => ({ ...d, impression: e.target.value }))}
            rows={5}
            placeholder="Synthesis — what the findings mean together."
            className="w-full rounded-sm border border-border bg-surface px-3 py-2 text-body text-text-primary placeholder:text-text-tertiary focus:border-accent focus:outline-none focus:ring-2 focus:ring-accent/30"
          />
        </Section>

        <Section title="Differential diagnosis">
          <ReportToggleList
            title="From Layer 1"
            nodes={proj.differentials}
            selected={draft.selectedDdx}
            onToggle={(id) =>
              setDraft((d) => ({ ...d, selectedDdx: toggle(d.selectedDdx, id) }))
            }
          />
        </Section>

        <Section title="Recommendation">
          <textarea
            value={draft.recommendation}
            onChange={(e) => setDraft((d) => ({ ...d, recommendation: e.target.value }))}
            rows={3}
            placeholder="Next steps, follow-up interval, recommended correlation…"
            className="w-full rounded-sm border border-border bg-surface px-3 py-2 text-body text-text-primary placeholder:text-text-tertiary focus:border-accent focus:outline-none focus:ring-2 focus:ring-accent/30"
          />
        </Section>
      </div>

      {/* RIGHT: live preview + export rail */}
      <aside className="lg:sticky lg:top-6 lg:self-start">
        <div className="rounded-md border border-border bg-surface p-5">
          <div className="mb-2 text-[10px] uppercase tracking-wider text-text-tertiary">
            Preview
          </div>
          <div className="report-print space-y-3">
            <div>
              <div className="text-caption text-text-tertiary">Patient</div>
              <div className="text-body text-text-primary">{patientLabel}</div>
            </div>
            {draft.clinicalInfo && (
              <div>
                <div className="text-caption text-text-tertiary">Clinical</div>
                <p className="whitespace-pre-wrap text-body text-text-primary">
                  {draft.clinicalInfo}
                </p>
              </div>
            )}
            <div>
              <div className="text-caption text-text-tertiary">Findings</div>
              {draft.selectedFindings.size === 0 ? (
                <p className="text-caption text-text-tertiary">None selected.</p>
              ) : (
                <ul className="text-body text-text-primary">
                  {proj.findings
                    .filter((f) => draft.selectedFindings.has(f.nodeId))
                    .map((f) => (
                      <li key={f.nodeId}>• {(f.content as any).label ?? `node ${f.nodeId}`}</li>
                    ))}
                </ul>
              )}
            </div>
            <div>
              <div className="text-caption text-text-tertiary">Impression</div>
              <p className="whitespace-pre-wrap text-body text-text-primary">
                {draft.impression || <span className="text-text-tertiary">—</span>}
              </p>
            </div>
            <div>
              <div className="text-caption text-text-tertiary">Differential</div>
              {draft.selectedDdx.size === 0 ? (
                <p className="text-caption text-text-tertiary">None selected.</p>
              ) : (
                <ul className="text-body text-text-primary">
                  {proj.differentials
                    .filter((d) => draft.selectedDdx.has(d.nodeId))
                    .map((d) => (
                      <li key={d.nodeId}>• {(d.content as any).label ?? `node ${d.nodeId}`}</li>
                    ))}
                </ul>
              )}
            </div>
            {draft.recommendation && (
              <div>
                <div className="text-caption text-text-tertiary">Recommendation</div>
                <p className="whitespace-pre-wrap text-body text-text-primary">
                  {draft.recommendation}
                </p>
              </div>
            )}
          </div>
        </div>

        <div className="mt-4 space-y-2">
          <Button
            variant="primary"
            className="w-full"
            onClick={exportPdf}
            disabled={exporting}
          >
            {exporting ? t('report.exporting') : t('report.exportPdf')}
          </Button>
          <Button variant="subtle" className="w-full" onClick={exportFhir}>
            {t('report.exportFhir')}
          </Button>
          <Button variant="subtle" className="w-full" onClick={exportSr}>
            {t('report.exportSr')}
          </Button>

          {/* Last-report card — appears after a successful PDF export
              so the medic always knows where the file went. Mirrors
              the lastExport card in Settings · Data so the two
              export flows feel consistent. */}
          {lastReport && (
            <div className="mt-3 rounded-sm border border-confirmed/40 bg-confirmed/5 px-3 py-2 text-caption">
              <div className="text-confirmed">
                {t('report.lastExport', {
                  size: humanBytes(lastReport.bytes),
                  when: new Date(lastReport.createdAt * 1000).toLocaleString(locale),
                })}
              </div>
              <div className="mt-1 flex items-center gap-3 text-text-secondary">
                <span className="truncate font-mono" title={lastReport.path}>
                  {lastReport.path}
                </span>
                <button
                  onClick={async () => {
                    const ok = await openPathInOsShell(lastReport.path);
                    if (!ok) {
                      try { await navigator.clipboard.writeText(lastReport.path); }
                      catch { /* ignore */ }
                      showToast(`${lastReport.path}`, 'info');
                    }
                  }}
                  className="shrink-0 rounded-sm border border-border px-2 py-0.5 hover:bg-accent-subtle"
                >
                  {t('report.openFolder')}
                </button>
              </div>
            </div>
          )}
        </div>
      </aside>
    </div>
  );
}

/* ─────────────── Remaining stubs ─────────────── */

function ModeStub({ mode, note }: { mode: ModeKindForStub; note: string }) {
  const modeLabel = useModeLabel();
  const t = useT();
  return (
    <EmptyState
      title={t('empty.modeStub', { mode: modeLabel(mode) })}
      description={note}
    />
  );
}
// Local alias; ModeKind is exported from ../lib/util but importing it
// here would duplicate the type and risk drift if a new mode is added.
type ModeKindForStub = 'today' | 'patient' | 'encounter' | 'imaging' | 'labs' | 'memory' | 'report';

/* ─────────────── Imaging mode (DICOM zip upload) ─────────────── */

interface UploadJob {
  id: string;                  // local UUID; survives across renders.
  fileName: string;
  sizeBytes: number;
  // Upload phase
  uploadedBytes: number;
  uploadedTotal: number;
  uploadDone: boolean;
  // Backend file ID returned by POST /api/v1/files/upload
  fileId: string | null;
  // DICOM background parse (only for .zip / application/zip)
  parseState: 'idle' | 'queued' | 'parsing' | 'rendering' | 'done' | 'error';
  parseStage: string;
  parsePercent: number;
  parseStudyId: string | null;
  parseError: string | null;
  // Memorization (Layer 1 graph ingester) — runs after parse finishes.
  // status is '' until the prerender completes, then 'pending' briefly,
  // then 'ok' / 'error'. summary is "N graph events" on success or
  // "ExcType: msg" on failure. Surfaced inline below the parse state
  // so the medic can see when ingestion fails (e.g. missing LLM key,
  // crashed extractor) instead of just an empty Memory tab.
  memoryStatus: '' | 'pending' | 'ok' | 'error';
  memorySummary: string;
  // Tier A — Quick scan (Gemini Flash triage). Runs AFTER the
  // ingester succeeds; emits finding nodes the medic can see in
  // Patient → Active findings and Memory · L1 · Findings.
  quickScanStatus: '' | 'pending' | 'ok' | 'error';
  quickScanSummary: string;
  // Live progress for an in-flight Quick scan (null when not running
  // or when the server's TTL pruned it). Updated from each
  // prerender-progress poll while status === 'pending'.
  quickScanProgress: QuickScanProgress | null;
  // True for rows hydrated from the history endpoint (no active
  // upload pipeline). Stops the row from rendering progress bars.
  fromHistory?: boolean;
}

function newJob(file: File): UploadJob {
  return {
    id: `${file.name}-${file.size}-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
    fileName: file.name,
    sizeBytes: file.size,
    uploadedBytes: 0,
    uploadedTotal: file.size,
    uploadDone: false,
    fileId: null,
    parseState: 'idle',
    parseStage: '',
    parsePercent: 0,
    parseStudyId: null,
    parseError: null,
    memoryStatus: '',
    memorySummary: '',
    quickScanStatus: '',
    quickScanSummary: '',
    quickScanProgress: null,
  };
}

function formatBytes(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  if (n < 1024 * 1024 * 1024) return `${(n / 1024 / 1024).toFixed(1)} MB`;
  return `${(n / 1024 / 1024 / 1024).toFixed(2)} GB`;
}

export function ImagingMode() {
  // ImagingMode has many static strings (upload zone, history rows,
  // Quick-scan status). Translation rolls in progressively — for
  // now we just call useT() so the locale subscription is registered
  // and future t() calls work without a reload. Strings inside this
  // mode will move from literals to t() keys in a follow-up pass.
  useT();
  const p             = useAppState((s) => s.activePatient);
  const showToast     = useAppState((s) => s.showToast);
  const refreshPatients = useAppState((s) => s.refreshPatients);
  const [jobs, setJobs]   = useState<UploadJob[]>([]);
  const [dragOver, setDragOver] = useState(false);

  // Hydrate historical uploads on mount + whenever the active patient
  // changes — so the medic sees their past CTs / PDFs / lab reports
  // for THIS patient (or every patient if none selected) instead of
  // a blank list that only fills with in-session uploads.
  useEffect(() => {
    let cancelled = false;
    api.listUploads({ patientHash: p?.patientHash, limit: 50 }).then(
      (rows) => {
        if (cancelled) return;
        const fromHistory: UploadJob[] = rows.map((r) => ({
          id:            `history:${r.fileId}`,
          fileName:      r.name,
          sizeBytes:     r.sizeBytes,
          uploadedBytes: r.sizeBytes,
          uploadedTotal: r.sizeBytes,
          uploadDone:    true,
          fileId:        r.fileId,
          parseState:    (r.dicomStudyId ? 'done' : 'idle') as UploadJob['parseState'],
          parseStage:    '',
          parsePercent:  100,
          parseStudyId:  r.dicomStudyId || null,
          parseError:    null,
          memoryStatus:  (r.memoryStatus as UploadJob['memoryStatus']) || '',
          memorySummary: r.memorySummary || '',
          quickScanStatus:  (r.quickScanStatus as UploadJob['quickScanStatus']) || '',
          quickScanSummary: r.quickScanSummary || '',
          // History rows never have an in-flight scan attached — the
          // server's progress dict gets TTL-pruned long before history
          // hydration. Live polls (runJob / pollForJobProgress) fill
          // this in when a retry kicks off.
          quickScanProgress: null,
          fromHistory:   true,
        }));
        // Merge: keep any in-session jobs (they're newer) above history.
        setJobs((prev) => {
          const activeIds = new Set(prev.map((j) => j.fileId).filter(Boolean));
          const merged = [
            ...prev,
            ...fromHistory.filter((h) => !activeIds.has(h.fileId)),
          ];
          return merged;
        });
      },
      () => { /* silent — history is nice-to-have, not blocking */ },
    );
    return () => { cancelled = true; };
  }, [p?.patientHash]);

  // Update a single row by id. Same shape as runJob's local helper,
  // hoisted so the retry handler below can reuse it (the retry runs
  // outside the runJob closure, against a job from history).
  const updateById = (id: string, mut: Partial<UploadJob>) =>
    setJobs((js) => js.map((j) => (j.id === id ? { ...j, ...mut } : j)));

  /**
   * Poll the prerender progress endpoint for one job's fileId until
   * quick_scan + memory both reach a terminal state, or 60s elapses.
   *
   * Used by the manual Retry path — duplicates the polling loop in
   * runJob() but starts from an already-existing fileId instead of a
   * fresh upload. Doing it standalone keeps runJob() readable and lets
   * a retry kick in for history rows (which never went through
   * runJob in this React session).
   */
  const pollForJobProgress = async (job: UploadJob) => {
    if (!job.fileId) return;
    let ticks = 0;
    while (ticks++ < 30) {  // ~60s @ 2s
      await new Promise((res) => setTimeout(res, 2000));
      try {
        const pr = await api.getPrerenderProgress(job.fileId);
        updateById(job.id, {
          parseState:        pr.state as UploadJob['parseState'],
          parseStage:        pr.stage,
          parsePercent:      pr.percent,
          parseStudyId:      pr.studyId || job.parseStudyId,
          parseError:        pr.error || null,
          memoryStatus:      (pr.memoryStatus as UploadJob['memoryStatus']) || '',
          memorySummary:     pr.memorySummary || '',
          quickScanStatus:   (pr.quickScanStatus as UploadJob['quickScanStatus']) || '',
          quickScanSummary:  pr.quickScanSummary || '',
          quickScanProgress: pr.quickScanProgress ?? null,
        });
        const scanDone = pr.quickScanStatus === 'ok' || pr.quickScanStatus === 'error';
        if (scanDone) break;
      } catch {
        /* transient — keep polling */
      }
    }
  };

  /**
   * Manual Retry handler for the 🔍 Quick scan failed row. Marks the
   * job pending locally so the Retry button hides immediately, hits the
   * backend retry endpoint, then polls for completion.
   *
   * Backend contract: ``POST /api/v1/dicom/studies/{study_id}/quick-scan``
   * also flips uploads.quick_scan_status='pending' so any other client
   * polling the same fileId sees the in-progress state.
   */
  const retryQuickScan = async (job: UploadJob) => {
    if (!job.parseStudyId) {
      showToast('Cannot retry — no study id on this upload', 'error');
      return;
    }
    updateById(job.id, {
      quickScanStatus: 'pending',
      quickScanSummary: '',
      // Clear any stale streaming snapshot from the previous run so
      // the UI doesn't briefly show last attempt's "8/75 grids ·
      // lung window" before the fresh poll lands.
      quickScanProgress: null,
    });
    try {
      await api.triggerQuickScan(job.parseStudyId);
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e);
      updateById(job.id, {
        quickScanStatus: 'error',
        quickScanSummary: msg,
      });
      showToast(`Retry failed: ${msg}`, 'error');
      return;
    }
    showToast('🔍 Quick scan retry enqueued', 'info');
    // Fire-and-forget — UI updates as polling sees the new statuses.
    pollForJobProgress(job);
  };

  // Run one upload job to completion: stream upload → poll parse.
  const runJob = async (job: UploadJob, file: File) => {
    const update = (mut: Partial<UploadJob>) =>
      setJobs((js) => js.map((j) => (j.id === job.id ? { ...j, ...mut } : j)));

    try {
      const r = await api.uploadFile(file, file.name, {
        // Bind to the currently-open patient if there is one. Without
        // this, a DICOM zip with its own PatientID would mint a NEW
        // patient row instead of attaching to the one the medic has
        // open in the desktop.
        patientHash: p?.patientHash,
        onProgress: (loaded, total) =>
          update({ uploadedBytes: loaded, uploadedTotal: total }),
      });
      update({
        uploadDone: true,
        uploadedBytes: r.sizeBytes,
        uploadedTotal: r.sizeBytes,
        fileId: r.fileId,
        parseState: r.dicomStatus === 'prerendering' ? 'queued' : 'idle',
        parseStudyId: r.dicomStudyId || null,
      });

      // Only poll the DICOM pipeline for zips / DICOM uploads.
      if (r.dicomStatus !== 'prerendering') {
        showToast(`Uploaded ${file.name}`, 'success');
        return;
      }

      // Poll until done / error / 60 ticks (~2 min at 2s) to keep the UI
      // honest if the backend's progress endpoint gets stuck.
      let ticks = 0;
      while (ticks++ < 60) {
        await new Promise((res) => setTimeout(res, 2000));
        try {
          const pr = await api.getPrerenderProgress(r.fileId);
          update({
            parseState:        pr.state as UploadJob['parseState'],
            parseStage:        pr.stage,
            parsePercent:      pr.percent,
            parseStudyId:      pr.studyId || null,
            parseError:        pr.error || null,
            memoryStatus:      (pr.memoryStatus as UploadJob['memoryStatus']) || '',
            memorySummary:     pr.memorySummary || '',
            quickScanStatus:   (pr.quickScanStatus as UploadJob['quickScanStatus']) || '',
            quickScanSummary:  pr.quickScanSummary || '',
            quickScanProgress: pr.quickScanProgress ?? null,
          });
          if (pr.state === 'error') break;
          // Keep polling until BOTH the ingester AND Quick scan reach a
          // terminal state — that's the moment the medic has the full
          // post-upload picture. Cap at ~60s of grace to avoid
          // infinite loops if either path hangs.
          const ingestDone = pr.memoryStatus === 'ok'   || pr.memoryStatus === 'error';
          const scanDone   = pr.quickScanStatus === 'ok' || pr.quickScanStatus === 'error';
          if (pr.state === 'done' && ingestDone && scanDone) break;
          if (pr.state === 'done' && ticks >= 30) break;  // ~60s grace
        } catch {
          // transient — keep polling
        }
      }
      // Refresh the patient list so a new DICOM-derived patient row
      // shows up in the sidebar immediately.
      refreshPatients();
      showToast(`Imported ${file.name}`, 'success');
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e);
      update({
        parseState: 'error',
        parseError: msg,
        uploadDone: false,
      });
      showToast(`Upload failed: ${msg}`, 'error');
    }
  };

  const acceptFiles = (files: FileList | File[]) => {
    const fileArr = Array.from(files);
    if (fileArr.length === 0) return;
    setJobs((prev) => {
      const next = [...prev];
      for (const f of fileArr) {
        const job = newJob(f);
        next.unshift(job);
        // Kick off the upload outside of setState; we re-read the
        // freshly created job from the closure.
        queueMicrotask(() => runJob(job, f));
      }
      return next;
    });
  };

  const onDrop = (e: React.DragEvent<HTMLLabelElement>) => {
    e.preventDefault();
    setDragOver(false);
    if (e.dataTransfer?.files?.length) acceptFiles(e.dataTransfer.files);
  };

  const onPick = (e: React.ChangeEvent<HTMLInputElement>) => {
    if (e.target.files) acceptFiles(e.target.files);
    // Reset so picking the same file again still triggers onChange.
    e.target.value = '';
  };

  return (
    <div className="mx-auto max-w-3xl px-10 py-12">
      <h1 className="font-display text-display text-text-primary">
        Imaging
      </h1>
      <p className="mt-2 text-body text-text-secondary">
        Drop a DICOM <span className="font-mono">.zip</span> here (or any
        clinical file). The server hashes it, parses DICOM headers, and
        derives the patient anchor from <span className="font-mono">PatientID</span>
        {' '}automatically — no need to pre-register the patient.
      </p>

      <label
        htmlFor="imaging-file-picker"
        onDragOver={(e) => { e.preventDefault(); setDragOver(true); }}
        onDragLeave={() => setDragOver(false)}
        onDrop={onDrop}
        className={cn(
          'mt-8 flex h-44 cursor-pointer flex-col items-center justify-center',
          'rounded-md border-2 border-dashed text-center transition-colors duration-100',
          dragOver
            ? 'border-accent bg-accent-subtle/50'
            : 'border-border hover:border-border-strong hover:bg-accent-subtle/20',
        )}
      >
        <div className="text-body text-text-primary">
          Drop DICOM <span className="font-mono">.zip</span> or click to choose
        </div>
        <div className="mt-1 text-caption text-text-tertiary">
          Multipart upload to <span className="font-mono">/api/v1/files/upload</span>
          {p && <> · binding to patient <strong>{patientDisplayLabel(p)}</strong></>}
        </div>
        <input
          id="imaging-file-picker"
          type="file"
          accept=".zip,application/zip,.dcm,application/dicom,*/*"
          multiple
          className="hidden"
          onChange={onPick}
        />
      </label>

      {jobs.length > 0 && (
        <div className="mt-8">
          <h2 className="mb-3 text-caption font-medium uppercase tracking-wider text-text-tertiary">
            Uploads ({jobs.length})
          </h2>
          <div className="space-y-2">
            {jobs.map((j) => (
              <UploadJobRow
                key={j.id}
                job={j}
                onRetryQuickScan={retryQuickScan}
              />
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

/** One-line header for the streaming Quick scan progress.
 *
 * Examples:
 *   "rendering 23/75 grids · lung window"     (during Stage 0)
 *   "triaging 41/75 grids · mediastinum"      (during Stage 1)
 *   "errors: 12/41 — check Gemini API key"    (mid-scan, lots of errors)
 *
 * Surfacing errors mid-flight is intentional: if every grid errors
 * out, we want the medic to know within 5 s of clicking Retry, not
 * after the full 25 s wait for the worker to finish.
 */
function quickScanProgressHeader(p: QuickScanProgress): string {
  if (p.stage === 'rendering') {
    const tag = p.current_preset ? ` · ${p.current_preset} window` : '';
    return `rendering ${p.rendered_grids}/${p.total_grids || '?'} grids${tag}`;
  }
  if (p.stage === 'triaging') {
    if (p.errors > 0 && p.errors >= Math.max(1, p.triaged_grids - p.errors)) {
      return `errors: ${p.errors}/${p.triaged_grids} — check GEMINI_API_KEY`;
    }
    const tag = p.current_preset ? ` · ${p.current_preset}` : '';
    return `triaging ${p.triaged_grids}/${p.total_grids || '?'} grids${tag}`;
  }
  if (p.stage === 'complete') return 'finishing up…';
  if (p.stage === 'error')    return `failed: ${p.last_error ?? 'see log'}`;
  return 'starting…';
}

/** The expandable progress block under the running "Quick scan:…"
 *  line. Renders the recent-findings tail and an unobtrusive bar.
 *  Layout intentionally compact so a chest-CT triple-window scan
 *  (75 grids over ~25 s) doesn't push the next upload card off-screen. */
function QuickScanProgressBlock({ progress }: { progress: QuickScanProgress }) {
  const recent = (progress.recent || []).slice(-3);
  const total  = progress.total_grids || 1;
  // Use the slower of the two counters (rendering or triaging) so the
  // bar advances monotonically — once triaging starts, rendered_grids
  // is already at total.
  const done = progress.stage === 'triaging'
    ? progress.triaged_grids
    : progress.rendered_grids;
  const pct = Math.min(100, (done / total) * 100);

  return (
    <div className="mt-1.5 selectable">
      {/* Thin progress bar. */}
      <div className="h-0.5 w-full overflow-hidden rounded-full bg-border/40">
        <div
          className={cn(
            'h-full transition-all duration-200',
            progress.errors > 0 && progress.errors >= Math.max(1, progress.triaged_grids - progress.errors)
              ? 'bg-retract' : 'bg-accent',
          )}
          style={{ width: `${pct}%` }}
        />
      </div>

      {/* Recent findings tail. Empty when every grid so far has been
          clean — render a calm "no findings yet" instead of nothing
          so the medic knows the scan IS reading the slices. */}
      {recent.length === 0 ? (
        <div className="mt-1 text-[10px] text-text-tertiary">
          {progress.stage === 'rendering'
            ? `Preparing 4×4 PNG grids of ${progress.scan_count ?? '?'} slices…`
            : 'No findings flagged so far.'}
        </div>
      ) : (
        <ul className="mt-1 space-y-0.5 text-[10px]">
          {recent.map((r, i) => (
            <li
              key={`${r.slice_start}-${r.slice_end}-${r.window}-${i}`}
              className={cn(
                'font-mono',
                r.verdict === 'error'      && 'text-retract',
                r.verdict === 'suspicious' && 'text-caution',
                r.verdict === 'unsure'     && 'text-text-secondary',
              )}
            >
              <span className="text-text-tertiary">
                slices {r.slice_start}–{r.slice_end} [{r.window}]:
              </span>{' '}
              {r.verdict === 'error'
                ? (r.error || 'API error')
                : (r.finding || r.verdict)}
              {r.urgency && r.verdict !== 'error' && (
                <span className="ml-1 text-text-tertiary">({r.urgency})</span>
              )}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function UploadJobRow({
  job,
  onRetryQuickScan,
}: {
  job: UploadJob;
  /** When provided AND the job's Quick scan failed AND we have a
   *  study id, the UploadJobRow renders a Retry link next to the
   *  red "Quick scan failed: …" text. Click → ImagingMode flips
   *  the row back to ``pending`` and polls the prerender progress
   *  endpoint until the worker finishes. */
  onRetryQuickScan?: (job: UploadJob) => void;
}) {
  const uploadPct = job.uploadedTotal > 0
    ? Math.min(100, (job.uploadedBytes / job.uploadedTotal) * 100)
    : 0;
  const isDicom    = job.parseState !== 'idle' || job.fileId === null;
  const isParsing  = job.parseState === 'queued' || job.parseState === 'parsing' || job.parseState === 'rendering';
  const isDone     = job.parseState === 'done' || (!isDicom && job.uploadDone);
  const isError    = job.parseState === 'error';

  let stateText: string;
  let stateChip: 'neutral' | 'tinted' | 'confirmed' | 'caution' | 'retract' = 'neutral';
  if (isError)              { stateText = 'Failed';             stateChip = 'retract'; }
  else if (isDone)          { stateText = isDicom ? 'Imported' : 'Uploaded'; stateChip = 'confirmed'; }
  else if (isParsing)       { stateText = job.parseStage || 'Parsing DICOM'; stateChip = 'tinted'; }
  else if (job.uploadDone)  { stateText = 'Queued for parse';   stateChip = 'neutral'; }
  else                      { stateText = `Uploading ${uploadPct.toFixed(0)}%`; stateChip = 'tinted'; }

  // Progress bar: upload bytes during upload, parse % afterwards.
  const barPct = !job.uploadDone
    ? uploadPct
    : (isParsing ? job.parsePercent : isDone ? 100 : 0);

  return (
    <div className="rounded-md border border-border bg-surface p-3">
      <div className="flex items-center justify-between gap-3">
        <div className="min-w-0 flex-1">
          <div className="truncate text-body text-text-primary">
            {job.fileName}
          </div>
          <div className="mt-0.5 text-caption text-text-tertiary">
            {formatBytes(job.sizeBytes)}
            {job.parseStudyId && (
              <> · study <span className="font-mono">{job.parseStudyId.slice(0, 12)}</span></>
            )}
          </div>
        </div>
        <Chip variant={stateChip}>{stateText}</Chip>
      </div>
      {(isParsing || !job.uploadDone) && (
        <div className="mt-2 h-1 w-full overflow-hidden rounded-full bg-border">
          <div
            className={cn(
              'h-full transition-all duration-200',
              isError ? 'bg-retract' : 'bg-accent',
            )}
            style={{ width: `${barPct}%` }}
          />
        </div>
      )}
      {/* Memory ingestion result — shows up under the parse row once
          the prerender + dicom_ingester finish. "Memory: 6 graph events"
          on success; "Memory failed: <reason>" on error. Never silent. */}
      {(job.memoryStatus === 'ok' || job.memoryStatus === 'error' || job.memoryStatus === 'pending') && (
        <div className={cn(
          'mt-2 text-caption',
          job.memoryStatus === 'ok'      && 'text-confirmed',
          job.memoryStatus === 'error'   && 'text-retract',
          job.memoryStatus === 'pending' && 'text-text-tertiary',
        )}>
          {job.memoryStatus === 'ok'      && `Memory: ${job.memorySummary || 'updated'}`}
          {job.memoryStatus === 'pending' && 'Memory: ingesting…'}
          {job.memoryStatus === 'error'   && `Memory failed: ${job.memorySummary || 'unknown error'}`}
        </div>
      )}
      {/* Tier A — Quick scan result. AI initial read; flagged findings
          land in Memory · L1 · Findings (unconfirmed) so the medic can
          accept/reject. Decision support only. */}
      {(job.quickScanStatus === 'ok' || job.quickScanStatus === 'error' || job.quickScanStatus === 'pending') && (
        <div className="mt-1 text-caption">
          <div className={cn(
            'flex items-center gap-2',
            job.quickScanStatus === 'ok'      && (job.quickScanSummary.includes('flagged') ? 'text-caution' : 'text-confirmed'),
            job.quickScanStatus === 'error'   && 'text-retract',
            job.quickScanStatus === 'pending' && 'text-text-tertiary',
          )}>
            <span>
              {job.quickScanStatus === 'pending' && (
                job.quickScanProgress
                  ? `🔍 Quick scan: ${quickScanProgressHeader(job.quickScanProgress)}`
                  : '🔍 Quick scan: starting…'
              )}
              {job.quickScanStatus === 'ok'      && `🔍 Quick scan: ${job.quickScanSummary}`}
              {job.quickScanStatus === 'error'   && `🔍 Quick scan failed: ${job.quickScanSummary}`}
            </span>
            {/* Retry button — only on error, and only when we have a
                study id to retry against. Hidden during pending so the
                medic doesn't double-click and stack background tasks. */}
            {job.quickScanStatus === 'error' && job.parseStudyId && onRetryQuickScan && (
              <button
                type="button"
                onClick={() => onRetryQuickScan(job)}
                className="rounded-sm border border-retract/40 px-1.5 py-0.5 text-[10px] text-retract hover:bg-retract/10"
                title="Re-run Gemini Flash triage on this study"
              >
                Retry
              </button>
            )}
          </div>

          {/* Live streaming progress — only while pending AND we have
              a server snapshot. Renders the running grid counter +
              the last few non-clean findings inline so the medic sees
              the scan "thinking" instead of a 25-second blank
              spinner. */}
          {job.quickScanStatus === 'pending' && job.quickScanProgress && (
            <QuickScanProgressBlock progress={job.quickScanProgress} />
          )}
        </div>
      )}
      {isError && job.parseError && (
        <div className="mt-2 text-caption text-retract">{job.parseError}</div>
      )}
    </div>
  );
}
export function LabsMode() {
  const t = useT();
  return <ModeStub mode="labs" note={t('labs.stub')} />;
}
