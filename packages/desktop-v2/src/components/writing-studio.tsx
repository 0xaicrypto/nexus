/**
 * Writing Studio (P1) — 写作 workspace.
 *
 * Three columns (mock1_writing_panel.svg):
 *   left   — document list (create / select / delete)
 *   center — title + plain <textarea> markdown editor with {{ref:ID}}
 *            placeholder tokens, polish toolbar, streamed diff card,
 *            status bar (autosave / word count)
 *   right  — 引用与来源 reference chips + 快照历史 snapshots
 *
 * Flows:
 *   @ / ＋引用   → ReferencePickerModal (mock2) → POST /docs/{id}/references
 *                  → insert {{ref:ID}} at the cursor.
 *   选中润色     → activated toolbar row → POST /docs/{id}/polish (SSE)
 *                  → word-level diff card (mock3) with per-hunk ✓/✗.
 *   导出 docx    → POST /docs/{id}/export; on 422 phi_unresolved the
 *                  PHI gate modal (mock4) collects per-finding
 *                  resolutions and re-posts.
 *
 * Editor is deliberately a plain textarea (TipTap chips are P2). The
 * read-only 预览 toggle renders {{ref:ID}} tokens as inline chips.
 */
import { useCallback, useEffect, useRef, useState } from 'react';
import {
  AlertTriangle, AtSign, Check, Download, Eye, PenLine, Plus,
  RefreshCw, RotateCcw, Sparkles, Trash2, X,
} from 'lucide-react';
import {
  api, ApiError,
  type WritingDocMeta, type WritingPhiFinding, type WritingPhiResolution,
  type WritingRefGranularity, type WritingReference, type WritingSnapshot,
} from '../lib/api-client';
import { useAppState } from '../store';
import { cn, patientDisplayLabel } from '../lib/util';
import { useT } from '../lib/i18n';
import type { Dict } from '../lib/i18n/en-US';
import { Button } from './ui';
import { Modal } from './modal';
import { CopyButton } from './copy-button';
import {
  applyDiff, changeCount, diffWords, type DiffSegment,
} from '../lib/word-diff';

/* ───────────────────────── helpers ───────────────────────── */

const REF_TOKEN_RE = /\{\{ref:([^}]+)\}\}/g;

function errMsg(e: unknown): string {
  if (e instanceof ApiError) return e.serverMessage || e.message;
  return (e as Error)?.message ?? String(e);
}

function fmtClock(d: Date): string {
  return `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`;
}

/** Compact "M/D HH:MM" for list rows; tolerant of bad timestamps. */
function fmtDateTime(iso: string): string {
  const t = Date.parse(iso);
  if (Number.isNaN(t)) return iso;
  const d = new Date(t);
  return `${d.getMonth() + 1}/${d.getDate()} ${fmtClock(d)}`;
}

/** Char count for the status bar — CJK chars count 1 each; ref
 *  placeholder tokens and whitespace are excluded. */
function wordCount(body: string): number {
  return body.replace(REF_TOKEN_RE, '').replace(/\s+/g, '').length;
}

function downloadBlob(blob: Blob, filename: string) {
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  // Give the browser a beat to start the download before revoking.
  window.setTimeout(() => URL.revokeObjectURL(url), 10_000);
}

const GRANULARITY_LABEL_KEY: Record<WritingRefGranularity, keyof Dict> = {
  basics:   'writing.picker.gBasics',
  timeline: 'writing.picker.gTimeline',
  progress: 'writing.picker.gProgress',
  roster:   'writing.picker.gRoster',
};

/* ───────────────────────── local state types ───────────────────────── */

interface PolishState {
  status: 'streaming' | 'ready';
  /** Frozen body offsets of the selection at run time. */
  start: number;
  end: number;
  selection: string;
  instruction: string;
  refIds: string[];
  streamText: string;
  /** Numbers flagged by provenance_warning frames. */
  warnings: string[];
  segments: DiffSegment[] | null;
  /** Per-change-hunk accept flags (indexed in change-hunk order). */
  accepted: boolean[];
}

interface PickerRequest {
  /** Body offset where the token goes. */
  pos: number;
  /** True when triggered by typing '@' — that char gets replaced. */
  replaceAt: boolean;
}

/* ════════════════════════════════════════════════════════════════════
   Root
   ════════════════════════════════════════════════════════════════════ */

export function WritingStudio() {
  const t = useT();
  const activeDocId          = useAppState((s) => s.activeWritingDocId);
  const setActiveDocId       = useAppState((s) => s.setActiveWritingDocId);
  const draft                = useAppState((s) =>
    s.activeWritingDocId ? s.writingDrafts[s.activeWritingDocId] : undefined);
  const setWritingDraft      = useAppState((s) => s.setWritingDraft);
  const clearWritingDraft    = useAppState((s) => s.clearWritingDraft);
  const showToast            = useAppState((s) => s.showToast);

  const [docs, setDocs]             = useState<WritingDocMeta[]>([]);
  const [references, setReferences] = useState<WritingReference[]>([]);
  const [snapshots, setSnapshots]   = useState<WritingSnapshot[]>([]);
  const [pendingDelete, setPendingDelete] = useState<WritingDocMeta | null>(null);
  const [deleting, setDeleting]     = useState(false);

  // Save machinery
  const [saveState, setSaveState] = useState<'idle' | 'dirty' | 'saving' | 'saved'>('idle');
  const [savedAt, setSavedAt]     = useState<string | null>(null);
  const lastSavedRef = useRef<{ docId: string; title: string; body: string } | null>(null);

  // Editor UI state
  const textareaRef = useRef<HTMLTextAreaElement | null>(null);
  const [previewMode, setPreviewMode] = useState(false);
  const [sel, setSel] = useState<{ start: number; end: number }>({ start: 0, end: 0 });
  const [picker, setPicker] = useState<PickerRequest | null>(null);

  // Polish
  const [polish, setPolish] = useState<PolishState | null>(null);
  const [customInstruction, setCustomInstruction] = useState('');
  const [excludedRefIds, setExcludedRefIds] = useState<Set<string>>(new Set());
  const polishAbortRef = useRef<AbortController | null>(null);

  // Export / PHI gate
  const [exporting, setExporting] = useState(false);
  const [includeSources, setIncludeSources] = useState(true);
  const [phiFindings, setPhiFindings] = useState<WritingPhiFinding[] | null>(null);

  /* ── doc list ─────────────────────────────────────────────── */

  const refreshDocs = useCallback(async () => {
    try {
      setDocs(await api.listWritingDocs());
    } catch (e) {
      useAppState.getState().showToast(
        tFail('writing.docs.loadFailed', e), 'error');
    }
  }, []);
  useEffect(() => { void refreshDocs(); }, [refreshDocs]);

  /* ── load doc on selection ────────────────────────────────── */

  useEffect(() => {
    // Abort any in-flight polish from the previous doc + reset
    // per-doc UI state.
    polishAbortRef.current?.abort();
    polishAbortRef.current = null;
    setPolish(null);
    setSel({ start: 0, end: 0 });
    setPicker(null);
    setPhiFindings(null);
    setPreviewMode(false);
    setExcludedRefIds(new Set());
    setSaveState('idle');
    setSavedAt(null);
    setReferences([]);
    setSnapshots([]);

    if (!activeDocId) return;
    let cancelled = false;
    (async () => {
      try {
        const doc = await api.getWritingDoc(activeDocId);
        if (cancelled) return;
        setReferences(doc.references);
        const st = useAppState.getState();
        if (!st.writingDrafts[activeDocId]) {
          // No local draft — hydrate from the server copy.
          st.setWritingDraft(activeDocId, { title: doc.title, body: doc.body });
          lastSavedRef.current = { docId: activeDocId, title: doc.title, body: doc.body };
        } else {
          // Local draft wins (F-draft-persist); mark the server copy
          // as the last-saved baseline so unchanged drafts don't
          // trigger a redundant PUT.
          lastSavedRef.current = { docId: activeDocId, title: doc.title, body: doc.body };
        }
        const snaps = await api.listWritingSnapshots(activeDocId);
        if (!cancelled) setSnapshots(snaps);
      } catch (e) {
        if (!cancelled) {
          useAppState.getState().showToast(
            tFail('writing.docs.loadFailed', e), 'error');
        }
      }
    })();
    return () => { cancelled = true; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeDocId]);

  /** i18n-with-error convenience bound to the CURRENT locale via the
   *  store (safe outside render because tFor reads the store copy). */
  function tFail(key: keyof Dict, e: unknown): string {
    return t(key, { error: errMsg(e) });
  }

  /* ── autosave (1.5s debounce) ─────────────────────────────── */

  const draftTitle = draft?.title;
  const draftBody  = draft?.body;
  useEffect(() => {
    if (!activeDocId || draftTitle === undefined || draftBody === undefined) return;
    const saved = lastSavedRef.current;
    if (saved && saved.docId === activeDocId
        && saved.title === draftTitle && saved.body === draftBody) {
      return; // nothing to save
    }
    setSaveState('dirty');
    const docId = activeDocId;
    const timer = window.setTimeout(async () => {
      setSaveState('saving');
      try {
        await api.updateWritingDoc(docId, { title: draftTitle, body: draftBody });
        lastSavedRef.current = { docId, title: draftTitle, body: draftBody };
        // Only flip to "saved" if the medic is still on this doc.
        if (useAppState.getState().activeWritingDocId === docId) {
          setSaveState('saved');
          setSavedAt(fmtClock(new Date()));
        }
        setDocs((ds) => ds.map((d) => d.id === docId
          ? { ...d, title: draftTitle, updatedAt: new Date().toISOString() }
          : d));
      } catch (e) {
        setSaveState('dirty');
        useAppState.getState().showToast(
          t('writing.editor.saveFailed', { error: errMsg(e) }), 'error');
      }
    }, 1500);
    return () => window.clearTimeout(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeDocId, draftTitle, draftBody]);

  /** Immediate flush — used before polish-apply snapshots + export. */
  async function saveNow(docId: string, d: { title: string; body: string }) {
    await api.updateWritingDoc(docId, { title: d.title, body: d.body });
    lastSavedRef.current = { docId, ...d };
    setSaveState('saved');
    setSavedAt(fmtClock(new Date()));
  }

  /* ── draft setters ────────────────────────────────────────── */

  function setDraftTitle(title: string) {
    if (!activeDocId) return;
    setWritingDraft(activeDocId, { title, body: draft?.body ?? '' });
  }
  function setDraftBody(body: string) {
    if (!activeDocId) return;
    setWritingDraft(activeDocId, { title: draft?.title ?? '', body });
  }

  /* ── doc create / delete ──────────────────────────────────── */

  async function onCreateDoc() {
    const title = t('writing.docs.untitled');
    try {
      const { id } = await api.createWritingDoc(title);
      setWritingDraft(id, { title, body: '' });
      lastSavedRef.current = { docId: id, title, body: '' };
      setActiveDocId(id);
      await refreshDocs();
    } catch (e) {
      showToast(t('writing.docs.createFailed', { error: errMsg(e) }), 'error');
    }
  }

  async function onConfirmDelete() {
    if (!pendingDelete) return;
    setDeleting(true);
    try {
      await api.deleteWritingDoc(pendingDelete.id);
      clearWritingDraft(pendingDelete.id);
      if (activeDocId === pendingDelete.id) setActiveDocId(null);
      setPendingDelete(null);
      await refreshDocs();
      showToast(t('writing.docs.deleted'), 'success');
    } catch (e) {
      showToast(t('writing.docs.deleteFailed', { error: errMsg(e) }), 'error');
    } finally {
      setDeleting(false);
    }
  }

  /* ── @ trigger + reference insert ─────────────────────────── */

  function onBodyChange(e: React.ChangeEvent<HTMLTextAreaElement>) {
    const el = e.target;
    const v = el.value;
    const pos = el.selectionStart ?? v.length;
    const prev = draft?.body ?? '';
    // Open the picker when the single char just typed is '@' (cheap
    // heuristic: length grew by exactly 1 and the char before the
    // caret is '@'). The '@' stays in the body until an insert
    // replaces it — cancelling the picker keeps the literal '@'.
    if (activeDocId && v.length === prev.length + 1 && pos > 0 && v[pos - 1] === '@') {
      setPicker({ pos: pos - 1, replaceAt: true });
    }
    setDraftBody(v);
  }

  function openPickerFromToolbar() {
    if (!activeDocId) return;
    const el = textareaRef.current;
    const pos = el ? (el.selectionStart ?? (draft?.body.length ?? 0)) : (draft?.body.length ?? 0);
    setPicker({ pos, replaceAt: false });
  }

  function onReferenceInserted(ref: WritingReference) {
    if (!activeDocId || !picker || draft === undefined) { setPicker(null); return; }
    const token = `{{ref:${ref.refId}}}`;
    const body = draft.body;
    const start = Math.min(picker.pos, body.length);
    const end = picker.replaceAt ? Math.min(start + 1, body.length) : start;
    const newBody = body.slice(0, start) + token + body.slice(end);
    setDraftBody(newBody);
    setReferences((rs) => [...rs, ref]);
    setDocs((ds) => ds.map((d) => d.id === activeDocId
      ? { ...d, refCount: d.refCount + 1 } : d));
    setPicker(null);
    // Put the caret right after the inserted token.
    const caret = start + token.length;
    window.setTimeout(() => {
      const el = textareaRef.current;
      if (el) { el.focus(); el.setSelectionRange(caret, caret); }
    }, 0);
  }

  /* ── polish ───────────────────────────────────────────────── */

  const includedRefIds = references
    .filter((r) => !excludedRefIds.has(r.refId))
    .map((r) => r.refId);
  const hasSelection = sel.end > sel.start;

  async function runPolish(instruction: string, reuse?: PolishState) {
    if (!activeDocId || draft === undefined) return;
    const start = reuse ? reuse.start : sel.start;
    const end   = reuse ? reuse.end   : sel.end;
    if (end <= start) { showToast(t('writing.polish.selectHint'), 'info'); return; }
    const selection = reuse ? reuse.selection : draft.body.slice(start, end);
    const refIds = reuse ? reuse.refIds : includedRefIds;

    polishAbortRef.current?.abort();
    const ac = new AbortController();
    polishAbortRef.current = ac;

    const base: PolishState = {
      status: 'streaming', start, end, selection, instruction, refIds,
      streamText: '', warnings: [], segments: null, accepted: [],
    };
    setPolish(base);
    try {
      for await (const frame of api.polishWritingDoc(
        activeDocId, { selection, instruction, refIds }, ac.signal,
      )) {
        if (ac.signal.aborted) return;
        if (frame.type === 'revised_chunk') {
          setPolish((p) => p && { ...p, streamText: p.streamText + frame.text });
        } else if (frame.type === 'provenance_warning') {
          setPolish((p) => p && { ...p, warnings: frame.numbers ?? [] });
        } else if (frame.type === 'done') {
          const segments = diffWords(selection, frame.revised);
          setPolish((p) => p && {
            ...p,
            status: 'ready',
            streamText: frame.revised,
            segments,
            accepted: new Array(changeCount(segments)).fill(true),
          });
        } else if (frame.type === 'error') {
          throw new Error(frame.message);
        }
      }
    } catch (e) {
      if (ac.signal.aborted) return;
      setPolish(null);
      showToast(t('writing.polish.failed', { error: errMsg(e) }), 'error');
    }
  }

  function applyPolish(acceptedFlags: boolean[]) {
    if (!activeDocId || !polish || !polish.segments || draft === undefined) return;
    const merged = applyDiff(polish.segments, acceptedFlags);
    const body = draft.body;
    let { start, end } = polish;
    // The body may have drifted since the selection was frozen (the
    // medic kept typing). Re-anchor on the exact selection text.
    if (body.slice(start, end) !== polish.selection) {
      const idx = body.indexOf(polish.selection);
      if (idx === -1) {
        showToast(t('writing.polish.failed', { error: 'selection changed' }), 'error');
        return;
      }
      start = idx;
      end = idx + polish.selection.length;
    }
    const newBody = body.slice(0, start) + merged + body.slice(end);
    const newDraft = { title: draft.title, body: newBody };
    setDraftBody(newBody);
    setPolish(null);
    const docId = activeDocId;
    void (async () => {
      try {
        // Immediate PUT — the server snapshots on save, which is what
        // makes the polish revertible from 快照历史.
        await saveNow(docId, newDraft);
        setSnapshots(await api.listWritingSnapshots(docId));
        showToast(t('writing.polish.applied'), 'success');
      } catch (e) {
        showToast(t('writing.editor.saveFailed', { error: errMsg(e) }), 'error');
      }
    })();
  }

  /* ── snapshots ────────────────────────────────────────────── */

  async function onRestoreSnapshot(sid: string) {
    if (!activeDocId || draft === undefined) return;
    try {
      const body = await api.restoreWritingSnapshot(activeDocId, sid);
      setWritingDraft(activeDocId, { title: draft.title, body });
      lastSavedRef.current = { docId: activeDocId, title: draft.title, body };
      setSaveState('saved');
      setSavedAt(fmtClock(new Date()));
      setSnapshots(await api.listWritingSnapshots(activeDocId));
      showToast(t('writing.snapshots.restored'), 'success');
    } catch (e) {
      showToast(t('writing.snapshots.restoreFailed', { error: errMsg(e) }), 'error');
    }
  }

  /* ── export + PHI gate ────────────────────────────────────── */

  async function runExport(resolutions: WritingPhiResolution[]) {
    if (!activeDocId || draft === undefined) return;
    setExporting(true);
    try {
      if (saveState !== 'saved') {
        // Flush the draft first so the server exports what the medic sees.
        await saveNow(activeDocId, { title: draft.title, body: draft.body });
      }
      const blob = await api.exportWritingDocx(activeDocId, {
        resolutions, includeSources,
      });
      const safeName = (draft.title || 'document').replace(/[\\/:*?"<>|]/g, '_');
      downloadBlob(blob, `${safeName}.docx`);
      setPhiFindings(null);
      showToast(t('writing.export.done'), 'success');
    } catch (e) {
      if (e instanceof ApiError && e.status === 422 && e.code === 'phi_unresolved') {
        try {
          const findings = await api.phiScanWritingDoc(activeDocId);
          setPhiFindings(findings);
        } catch (e2) {
          showToast(t('writing.phi.scanFailed', { error: errMsg(e2) }), 'error');
        }
      } else {
        showToast(t('writing.export.failed', { error: errMsg(e) }), 'error');
      }
    } finally {
      setExporting(false);
    }
  }

  /* ── render ───────────────────────────────────────────────── */

  return (
    <div className="rw-root flex h-full w-full font-rw-display">
      {/* left — documents */}
      <DocsSidebar
        docs={docs}
        activeDocId={activeDocId}
        onSelect={setActiveDocId}
        onNew={() => void onCreateDoc()}
        onDelete={setPendingDelete}
      />

      {/* center — editor */}
      <main className="flex min-w-0 flex-1 flex-col overflow-hidden bg-rw-bg">
        {!activeDocId || draft === undefined ? (
          <div className="flex flex-1 flex-col items-center justify-center gap-2 text-center">
            <div className="text-base font-semibold text-rw-t2">
              {t('writing.editor.pickDoc')}
            </div>
            <div className="text-caption text-rw-t3">
              {t('writing.editor.pickDocHint')}
            </div>
          </div>
        ) : (
          <>
            {/* toolbar */}
            <div className="flex shrink-0 items-center justify-between gap-2 border-b border-rw-border px-5 py-2">
              <div className="flex items-center gap-2">
                <Button variant="rw-secondary" onClick={openPickerFromToolbar}>
                  <AtSign size={13} />
                  {t('writing.toolbar.addRef')}
                </Button>
                <Button
                  variant="rw-secondary"
                  onClick={() => setPreviewMode((v) => !v)}
                >
                  {previewMode ? <PenLine size={13} /> : <Eye size={13} />}
                  {previewMode ? t('writing.editor.edit') : t('writing.editor.preview')}
                </Button>
              </div>
              <Button
                variant="rw-primary"
                disabled={exporting}
                onClick={() => void runExport([])}
              >
                <Download size={13} />
                {exporting ? t('writing.toolbar.exporting') : t('writing.toolbar.export')}
              </Button>
            </div>

            {/* title + body */}
            <div className="flex min-h-0 flex-1 flex-col overflow-y-auto px-6 pt-4">
              <input
                value={draft.title}
                onChange={(e) => setDraftTitle(e.target.value)}
                placeholder={t('writing.editor.titlePlaceholder')}
                className="w-full shrink-0 border-b border-rw-border bg-transparent pb-3
                           text-xl font-semibold text-rw-t1 outline-none
                           placeholder:text-rw-t4"
              />
              {previewMode ? (
                <PreviewBody body={draft.body} references={references} />
              ) : (
                <textarea
                  ref={textareaRef}
                  value={draft.body}
                  onChange={onBodyChange}
                  onSelect={(e) => {
                    const el = e.target as HTMLTextAreaElement;
                    setSel({
                      start: el.selectionStart ?? 0,
                      end:   el.selectionEnd ?? 0,
                    });
                  }}
                  placeholder={t('writing.editor.bodyPlaceholder')}
                  spellCheck={false}
                  className="min-h-[280px] w-full flex-1 resize-none bg-transparent py-4
                             font-mono text-[13.5px] leading-7 text-rw-t1 outline-none
                             placeholder:text-rw-t4"
                />
              )}

              {/* polish toolbar — activates on non-empty selection */}
              {!previewMode && (
                <PolishToolbar
                  active={hasSelection}
                  refCount={includedRefIds.length}
                  customInstruction={customInstruction}
                  onCustomInstructionChange={setCustomInstruction}
                  onRun={(instruction) => void runPolish(instruction)}
                />
              )}

              {/* diff card */}
              {polish && (
                <PolishDiffCard
                  polish={polish}
                  onToggleHunk={(idx, accept) =>
                    setPolish((p) => {
                      if (!p) return p;
                      const accepted = [...p.accepted];
                      accepted[idx] = accept;
                      return { ...p, accepted };
                    })}
                  onAcceptAll={() =>
                    polish.segments && applyPolish(polish.segments.map(() => true))}
                  onRejectAll={() => {
                    polishAbortRef.current?.abort();
                    setPolish(null);
                  }}
                  onRegenerate={() => void runPolish(polish.instruction, polish)}
                  onApply={() => applyPolish(polish.accepted)}
                />
              )}
              <div className="h-6 shrink-0" />
            </div>

            {/* status bar */}
            <div className="flex h-9 shrink-0 items-center justify-between border-t border-rw-border px-5 text-caption text-rw-t3">
              <div className="flex items-center gap-3">
                <span>
                  {t('writing.editor.wordCount', {
                    count: wordCount(draft.body).toLocaleString(),
                  })}
                </span>
                <span>·</span>
                <span className={cn(saveState === 'dirty' && 'text-rw-orange')}>
                  {saveState === 'saving'
                    ? t('writing.editor.saving')
                    : saveState === 'dirty'
                      ? t('writing.editor.unsaved')
                      : savedAt
                        ? t('writing.editor.savedAt', { time: savedAt })
                        : '—'}
                </span>
                <span>·</span>
                <span>{t('writing.editor.snapshotCount', { count: snapshots.length })}</span>
              </div>
              <CopyButton text={draft.body} tone="rw" />
            </div>
          </>
        )}
      </main>

      {/* right — references + snapshots */}
      {activeDocId && draft !== undefined && (
        <RightRail
          references={references}
          snapshots={snapshots}
          excludedRefIds={excludedRefIds}
          onToggleRef={(refId, included) =>
            setExcludedRefIds((prev) => {
              const next = new Set(prev);
              if (included) next.delete(refId);
              else next.add(refId);
              return next;
            })}
          onRestore={(sid) => void onRestoreSnapshot(sid)}
        />
      )}

      {/* modals */}
      {picker && activeDocId && (
        <ReferencePickerModal
          docId={activeDocId}
          onClose={() => setPicker(null)}
          onInserted={onReferenceInserted}
        />
      )}
      {pendingDelete && (
        <Modal
          open
          onClose={() => setPendingDelete(null)}
          title={t('writing.docs.deleteConfirmTitle')}
          tone="rw"
        >
          <p className="mb-4 text-body text-rw-t2">
            {t('writing.docs.deleteConfirmBody', {
              title: pendingDelete.title || t('writing.docs.untitled'),
            })}
          </p>
          <div className="flex justify-end gap-2">
            <Button variant="rw-secondary" onClick={() => setPendingDelete(null)}>
              {t('writing.docs.cancel')}
            </Button>
            <Button
              variant="rw-danger"
              disabled={deleting}
              onClick={() => void onConfirmDelete()}
            >
              {t('writing.docs.delete')}
            </Button>
          </div>
        </Modal>
      )}
      {phiFindings && (
        <PhiGateModal
          findings={phiFindings}
          exporting={exporting}
          includeSources={includeSources}
          onIncludeSourcesChange={setIncludeSources}
          onClose={() => setPhiFindings(null)}
          onExport={(resolutions) => void runExport(resolutions)}
        />
      )}
    </div>
  );
}

/* ════════════════════════════════════════════════════════════════════
   Left rail — document list
   ════════════════════════════════════════════════════════════════════ */

function DocsSidebar({
  docs, activeDocId, onSelect, onNew, onDelete,
}: {
  docs: WritingDocMeta[];
  activeDocId: string | null;
  onSelect: (id: string) => void;
  onNew: () => void;
  onDelete: (d: WritingDocMeta) => void;
}) {
  const t = useT();
  return (
    <aside className="flex h-full w-[240px] shrink-0 flex-col border-r border-rw-border bg-rw-bg-deep">
      <div className="px-4 pb-1 pt-4 text-[10px] font-medium uppercase tracking-[0.12em] text-rw-t4">
        {t('writing.docs.title')}
      </div>
      <div className="flex-1 space-y-1 overflow-y-auto px-2 py-2">
        {docs.length === 0 && (
          <div className="px-2 py-3 text-caption text-rw-t3">
            {t('writing.docs.empty')}
          </div>
        )}
        {docs.map((d) => (
          <div
            key={d.id}
            role="button"
            tabIndex={0}
            onClick={() => onSelect(d.id)}
            onKeyDown={(e) => {
              if (e.key === 'Enter' || e.key === ' ') onSelect(d.id);
            }}
            className={cn(
              'group flex w-full cursor-pointer items-start justify-between gap-1',
              'rounded-md border px-3 py-2 text-left transition-colors duration-80',
              d.id === activeDocId
                ? 'border-rw-accent-bd bg-rw-accent-bg'
                : 'border-transparent hover:bg-rw-surface',
            )}
          >
            <div className="min-w-0 flex-1">
              <div className="truncate text-[13px] text-rw-t1">
                {d.title || t('writing.docs.untitled')}
              </div>
              <div className="mt-0.5 text-[11px] text-rw-t4">
                {fmtDateTime(d.updatedAt)}
                {d.refCount > 0 && (
                  <> · {t('writing.docs.refCount', { count: d.refCount })}</>
                )}
              </div>
            </div>
            <button
              aria-label={t('writing.docs.delete')}
              title={t('writing.docs.delete')}
              onClick={(e) => { e.stopPropagation(); onDelete(d); }}
              className="rounded-sm p-1 text-rw-t4 opacity-0 transition-opacity
                         duration-80 hover:bg-rw-red-bg hover:text-rw-red
                         focus:opacity-100 group-hover:opacity-100"
            >
              <Trash2 size={12} />
            </button>
          </div>
        ))}
        <button
          type="button"
          onClick={onNew}
          className="mt-1 flex w-full items-center justify-center rounded-md border
                     border-dashed border-rw-border px-3 py-2 text-[13px] text-rw-t3
                     transition-colors duration-80 hover:border-rw-accent-bd hover:text-rw-t1"
        >
          <Plus size={13} className="mr-1" />
          {t('writing.docs.new').replace(/^＋\s*/, '')}
        </button>
      </div>
    </aside>
  );
}

/* ════════════════════════════════════════════════════════════════════
   Read-only preview — {{ref:ID}} tokens rendered as inline chips
   ════════════════════════════════════════════════════════════════════ */

function PreviewBody({
  body, references,
}: {
  body: string;
  references: WritingReference[];
}) {
  const byId = new Map(references.map((r) => [r.refId, r]));
  const parts = body.split(/(\{\{ref:[^}]+\}\})/g);
  return (
    <div className="min-h-[280px] w-full flex-1 whitespace-pre-wrap py-4 text-[14px] leading-7 text-rw-t2">
      {parts.map((p, i) => {
        const m = /^\{\{ref:([^}]+)\}\}$/.exec(p);
        if (!m) return <span key={i}>{p}</span>;
        const ref = byId.get(m[1]);
        return (
          <span
            key={i}
            title={ref?.snapshotPreview ?? m[1]}
            className={cn(
              'mx-0.5 inline-flex items-center gap-1 rounded-full border px-2 py-0.5',
              'align-baseline text-[12px]',
              ref?.refType === 'study'
                ? 'border-rw-green bg-rw-green-bg text-rw-green'
                : 'border-rw-accent-bd bg-rw-accent-bg text-rw-accent',
            )}
          >
            ◇ {ref?.chipLabel ?? m[1]}
          </span>
        );
      })}
    </div>
  );
}

/* ════════════════════════════════════════════════════════════════════
   Polish toolbar — activated row (P1: no floating positioning)
   ════════════════════════════════════════════════════════════════════ */

const POLISH_PRESET_KEYS: Array<keyof Dict> = [
  'writing.polish.presetConcise',
  'writing.polish.presetAcademic',
  'writing.polish.presetGrammar',
  'writing.polish.presetEnglish',
];

function PolishToolbar({
  active, refCount, customInstruction, onCustomInstructionChange, onRun,
}: {
  active: boolean;
  refCount: number;
  customInstruction: string;
  onCustomInstructionChange: (v: string) => void;
  onRun: (instruction: string) => void;
}) {
  const t = useT();
  return (
    <div
      className={cn(
        'flex shrink-0 flex-wrap items-center gap-2 rounded-full border px-4 py-2',
        'transition-colors duration-150',
        active
          ? 'border-rw-accent-bd bg-rw-surface'
          : 'border-rw-border bg-transparent opacity-60',
      )}
      title={active ? undefined : t('writing.polish.selectHint')}
    >
      <span className="flex items-center gap-1 text-[13px] font-semibold text-rw-accent">
        <Sparkles size={13} />
        {t('writing.polish.title')}
      </span>
      {POLISH_PRESET_KEYS.map((k) => (
        <button
          key={k}
          type="button"
          disabled={!active}
          onClick={() => onRun(t(k))}
          className="rounded-full border border-transparent px-2 py-0.5 text-[13px]
                     text-rw-t2 transition-colors duration-80 hover:border-rw-accent-bd
                     hover:text-rw-t1 disabled:pointer-events-none"
        >
          {t(k)}
        </button>
      ))}
      <span className="mx-1 h-4 w-px bg-rw-border" aria-hidden />
      <input
        value={customInstruction}
        onChange={(e) => onCustomInstructionChange(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === 'Enter' && active && customInstruction.trim()) {
            onRun(customInstruction.trim());
          }
        }}
        placeholder={t('writing.polish.customPlaceholder')}
        disabled={!active}
        className="min-w-[120px] flex-1 bg-transparent text-[13px] text-rw-t1
                   outline-none placeholder:text-rw-t4"
      />
      <button
        type="button"
        disabled={!active || !customInstruction.trim()}
        onClick={() => onRun(customInstruction.trim())}
        className="rounded-full border border-rw-accent-bd px-2.5 py-0.5 text-[12px]
                   text-rw-accent transition-colors duration-80 hover:bg-rw-accent-bg
                   disabled:pointer-events-none disabled:opacity-50"
      >
        {t('writing.polish.run')}
      </button>
      <span className="text-[11px] text-rw-t4">
        {t('writing.polish.refsLabel', { count: refCount })}
      </span>
    </div>
  );
}

/* ════════════════════════════════════════════════════════════════════
   Diff card (mock3) — streamed text → word-level diff with per-hunk ✓/✗
   ════════════════════════════════════════════════════════════════════ */

function PolishDiffCard({
  polish, onToggleHunk, onAcceptAll, onRejectAll, onRegenerate, onApply,
}: {
  polish: PolishState;
  onToggleHunk: (changeIdx: number, accept: boolean) => void;
  onAcceptAll: () => void;
  onRejectAll: () => void;
  onRegenerate: () => void;
  onApply: () => void;
}) {
  const t = useT();
  const streaming = polish.status === 'streaming';

  // Render change hunks with a running index so ✓/✗ maps onto
  // polish.accepted[changeIdx].
  let changeIdx = -1;

  return (
    <div className="mt-4 shrink-0 rounded-md border border-rw-border bg-rw-bg-deep p-4">
      <div className="mb-3 flex items-center justify-between">
        <div className="text-[11px] font-medium uppercase tracking-[0.12em] text-rw-t4">
          {t('writing.polish.cardTitle', { instruction: polish.instruction })}
        </div>
        {streaming && (
          <div className="flex items-center gap-1.5 text-caption text-rw-t3">
            <RefreshCw size={12} className="animate-spin" />
            {t('writing.polish.streaming')}
          </div>
        )}
      </div>

      {streaming ? (
        <div className="whitespace-pre-wrap text-[14px] leading-7 text-rw-t2">
          {polish.streamText || '…'}
        </div>
      ) : (
        <div className="whitespace-pre-wrap text-[14px] leading-7">
          {(polish.segments ?? []).map((seg, i) => {
            if (seg.kind === 'same') {
              return <span key={i} className="text-rw-t2">{seg.text}</span>;
            }
            changeIdx += 1;
            const idx = changeIdx;
            const accepted = polish.accepted[idx] !== false;
            return (
              <span key={i} className="mx-0.5">
                {seg.del && (
                  <span
                    className={cn(
                      'rounded-sm px-0.5',
                      accepted
                        ? 'bg-rw-red-bg text-rw-red line-through'
                        : 'text-rw-t1',
                    )}
                  >
                    {seg.del}
                  </span>
                )}
                {seg.add && (
                  <span
                    className={cn(
                      'rounded-sm px-0.5',
                      accepted
                        ? 'bg-rw-green-bg text-rw-green'
                        : 'text-rw-t4 line-through opacity-60',
                    )}
                  >
                    {seg.add}
                  </span>
                )}
                <span className="ml-0.5 inline-flex translate-y-[-1px] items-center gap-0.5 align-middle">
                  <button
                    type="button"
                    title={t('writing.polish.acceptHunk')}
                    onClick={() => onToggleHunk(idx, true)}
                    className={cn(
                      'inline-flex h-[18px] w-[18px] items-center justify-center rounded-sm border',
                      accepted
                        ? 'border-rw-green bg-rw-green-bg text-rw-green'
                        : 'border-rw-border text-rw-t4 hover:text-rw-green',
                    )}
                  >
                    <Check size={11} />
                  </button>
                  <button
                    type="button"
                    title={t('writing.polish.rejectHunk')}
                    onClick={() => onToggleHunk(idx, false)}
                    className={cn(
                      'inline-flex h-[18px] w-[18px] items-center justify-center rounded-sm border',
                      !accepted
                        ? 'border-rw-red bg-rw-red-bg text-rw-red'
                        : 'border-rw-border text-rw-t4 hover:text-rw-red',
                    )}
                  >
                    <X size={11} />
                  </button>
                </span>
              </span>
            );
          })}
        </div>
      )}

      {/* provenance warning banner */}
      {polish.warnings.length > 0 && (
        <div className="mt-3 flex items-start gap-2 rounded-md border border-rw-orange bg-rw-orange-bg px-3 py-2 text-caption text-rw-orange">
          <AlertTriangle size={13} className="mt-0.5 shrink-0" />
          <span>
            {t('writing.polish.provenance', { numbers: polish.warnings.join(' · ') })}
          </span>
        </div>
      )}

      {/* footer actions */}
      {!streaming && (
        <div className="mt-4 flex items-center gap-2 border-t border-rw-border pt-3">
          <Button variant="rw-primary" onClick={onAcceptAll}>
            <Check size={13} />
            {t('writing.polish.acceptAll')}
          </Button>
          <Button variant="rw-secondary" onClick={onApply}>
            {t('writing.polish.apply')}
          </Button>
          <Button variant="rw-secondary" onClick={onRejectAll}>
            <X size={13} />
            {t('writing.polish.rejectAll')}
          </Button>
          <Button variant="rw-secondary" onClick={onRegenerate}>
            <RefreshCw size={13} />
            {t('writing.polish.regen')}
          </Button>
        </div>
      )}
    </div>
  );
}

/* ════════════════════════════════════════════════════════════════════
   Right rail — 引用与来源 + 快照历史
   ════════════════════════════════════════════════════════════════════ */

function RightRail({
  references, snapshots, excludedRefIds, onToggleRef, onRestore,
}: {
  references: WritingReference[];
  snapshots: WritingSnapshot[];
  excludedRefIds: Set<string>;
  onToggleRef: (refId: string, included: boolean) => void;
  onRestore: (snapshotId: string) => void;
}) {
  const t = useT();
  return (
    <aside className="flex h-full w-[292px] shrink-0 flex-col overflow-y-auto border-l border-rw-border bg-rw-bg-deep p-4">
      <div className="pb-2 text-[10px] font-medium uppercase tracking-[0.12em] text-rw-t4">
        {t('writing.refs.title', { count: references.length })}
      </div>
      {references.length === 0 && (
        <div className="pb-2 text-caption text-rw-t3">
          {t('writing.refs.empty')}
        </div>
      )}
      <div className="space-y-2">
        {references.map((r) => {
          const included = !excludedRefIds.has(r.refId);
          return (
            <div
              key={r.refId}
              className="rounded-md border border-rw-border bg-rw-surface p-3"
            >
              <div className="flex items-start justify-between gap-2">
                <div
                  className={cn(
                    'min-w-0 truncate text-[13px]',
                    r.refType === 'study' ? 'text-rw-green' : 'text-rw-accent',
                  )}
                  title={r.chipLabel}
                >
                  ◇ {r.chipLabel}
                </div>
                <label
                  className="flex shrink-0 cursor-pointer items-center gap-1 text-[11px] text-rw-t4"
                  title={t('writing.refs.inPolish')}
                >
                  <input
                    type="checkbox"
                    checked={included}
                    onChange={(e) => onToggleRef(r.refId, e.target.checked)}
                    className="accent-[var(--rw-accent)]"
                  />
                  <Sparkles size={11} />
                </label>
              </div>
              <div className="mt-1 text-[11px] text-rw-t3">
                {t(GRANULARITY_LABEL_KEY[r.granularity] ?? 'writing.picker.gBasics')}
                {r.createdAt ? ` · ${fmtDateTime(r.createdAt)}` : ''}
              </div>
              {r.snapshotPreview && (
                <div className="mt-1.5 line-clamp-3 text-[11px] leading-4 text-rw-t4">
                  {r.snapshotPreview}
                </div>
              )}
            </div>
          );
        })}
      </div>

      <div className="mt-5 border-t border-rw-border pt-4">
        <div className="pb-2 text-[10px] font-medium uppercase tracking-[0.12em] text-rw-t4">
          {t('writing.snapshots.title')}
        </div>
        {snapshots.length === 0 && (
          <div className="text-caption text-rw-t3">{t('writing.snapshots.empty')}</div>
        )}
        <div className="space-y-1.5">
          {snapshots.map((s) => (
            <div
              key={s.id}
              className="flex items-center justify-between gap-2 rounded-md border border-rw-border bg-rw-surface px-3 py-2"
            >
              <div className="min-w-0">
                <div className="truncate text-[12px] text-rw-t2">{s.label || s.id}</div>
                <div className="text-[11px] text-rw-t4">{fmtDateTime(s.createdAt)}</div>
              </div>
              <button
                type="button"
                onClick={() => onRestore(s.id)}
                title={t('writing.snapshots.restore')}
                className="flex shrink-0 items-center gap-1 rounded-md border border-rw-border
                           px-2 py-1 text-[11px] text-rw-t3 transition-colors duration-80
                           hover:border-rw-accent-bd hover:text-rw-t1"
              >
                <RotateCcw size={11} />
                {t('writing.snapshots.restore')}
              </button>
            </div>
          ))}
        </div>
      </div>
    </aside>
  );
}

/* ════════════════════════════════════════════════════════════════════
   Reference picker modal (mock2) — 患者/研究 tabs + granularity chips
   ════════════════════════════════════════════════════════════════════ */

const PATIENT_GRANULARITIES: WritingRefGranularity[] = ['basics', 'timeline'];
const STUDY_GRANULARITIES:   WritingRefGranularity[] = ['progress', 'roster'];

function ReferencePickerModal({
  docId, onClose, onInserted,
}: {
  docId: string;
  onClose: () => void;
  onInserted: (ref: WritingReference) => void;
}) {
  const t = useT();
  const patients        = useAppState((s) => s.patients);
  const studies         = useAppState((s) => s.studies);
  const refreshPatients = useAppState((s) => s.refreshPatients);
  const refreshStudies  = useAppState((s) => s.refreshStudies);
  const showToast       = useAppState((s) => s.showToast);

  const [tab, setTab]           = useState<'patient' | 'study'>('patient');
  const [query, setQuery]       = useState('');
  const [targetId, setTargetId] = useState<string | null>(null);
  const [gran, setGran]         = useState<WritingRefGranularity>('basics');
  const [busy, setBusy]         = useState(false);

  useEffect(() => {
    if (patients.length === 0) void refreshPatients();
    if (studies.length === 0)  void refreshStudies();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  function switchTab(next: 'patient' | 'study') {
    setTab(next);
    setTargetId(null);
    setGran(next === 'patient' ? 'basics' : 'progress');
  }

  const q = query.trim().toLowerCase();
  const patientRows = patients.filter((p) =>
    !q
    || patientDisplayLabel(p).toLowerCase().includes(q)
    || p.mrn.toLowerCase().includes(q)
    || p.patientHash.toLowerCase().startsWith(q));
  const studyRows = studies.filter((s) =>
    !q
    || s.displayName.toLowerCase().includes(q)
    || s.shortCode.toLowerCase().includes(q));

  const granOptions = tab === 'patient' ? PATIENT_GRANULARITIES : STUDY_GRANULARITIES;

  async function insert() {
    if (!targetId || busy) return;
    setBusy(true);
    try {
      const r = await api.createWritingReference(docId, {
        refType: tab, targetId, granularity: gran,
      });
      onInserted({
        refId:           r.refId,
        refType:         tab,
        targetId,
        granularity:     gran,
        chipLabel:       r.chipLabel,
        snapshotPreview: r.snapshotPreview,
        createdAt:       new Date().toISOString(),
      });
    } catch (e) {
      showToast(t('writing.picker.insertFailed', { error: errMsg(e) }), 'error');
    } finally {
      setBusy(false);
    }
  }

  const tabBtn = (key: 'patient' | 'study', label: string) => (
    <button
      type="button"
      onClick={() => switchTab(key)}
      className={cn(
        'rounded-full border px-3 py-1 text-[13px] transition-colors duration-80',
        tab === key
          ? 'border-transparent bg-rw-accent font-medium text-[#06252c]'
          : 'border-rw-border text-rw-t2 hover:bg-rw-surface-2',
      )}
    >
      {label}
    </button>
  );

  return (
    <Modal open onClose={onClose} title={t('writing.picker.title')} tone="rw" width={620}>
      <div className="mb-3 flex items-center gap-2">
        {tabBtn('patient', t('writing.picker.tabPatients'))}
        {tabBtn('study',   t('writing.picker.tabStudies'))}
        <input
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder={t('writing.picker.search')}
          className="ml-auto w-[200px] rounded-md border border-rw-border bg-rw-bg-deep
                     px-3 py-1.5 text-[13px] text-rw-t1 outline-none
                     placeholder:text-rw-t4 focus:border-rw-accent-bd"
        />
      </div>

      {/* candidate list */}
      <div className="max-h-[240px] space-y-1 overflow-y-auto rounded-md border border-rw-border-soft p-1.5">
        {tab === 'patient' && patientRows.length === 0 && (
          <div className="px-2 py-3 text-caption text-rw-t3">{t('writing.picker.empty')}</div>
        )}
        {tab === 'study' && studyRows.length === 0 && (
          <div className="px-2 py-3 text-caption text-rw-t3">{t('writing.picker.empty')}</div>
        )}
        {tab === 'patient' && patientRows.map((p) => (
          <button
            key={p.patientHash}
            type="button"
            onClick={() => setTargetId(p.patientHash)}
            className={cn(
              'flex w-full items-center justify-between rounded-md border px-3 py-2 text-left',
              'transition-colors duration-80',
              targetId === p.patientHash
                ? 'border-rw-accent-bd bg-rw-accent-bg'
                : 'border-transparent hover:bg-rw-surface-2',
            )}
          >
            <div className="min-w-0">
              <div className="truncate text-[13px] text-rw-t1">{patientDisplayLabel(p)}</div>
              <div className="text-[11px] text-rw-t4">
                {(p.sex || '—')} · {(p.ageGroup || '—')}
                {p.latestModality ? ` · ${p.latestModality}` : ''}
              </div>
            </div>
            {targetId === p.patientHash && <Check size={14} className="shrink-0 text-rw-accent" />}
          </button>
        ))}
        {tab === 'study' && studyRows.map((s) => (
          <button
            key={s.studyId}
            type="button"
            onClick={() => setTargetId(s.studyId)}
            className={cn(
              'flex w-full items-center justify-between rounded-md border px-3 py-2 text-left',
              'transition-colors duration-80',
              targetId === s.studyId
                ? 'border-rw-accent-bd bg-rw-accent-bg'
                : 'border-transparent hover:bg-rw-surface-2',
            )}
          >
            <div className="min-w-0">
              <div className="truncate text-[13px] text-rw-t1">{s.displayName}</div>
              <div className="text-[11px] text-rw-t4">
                {s.shortCode}{s.phase ? ` · ${s.phase}` : ''} · n={s.enrolledCount}
              </div>
            </div>
            {targetId === s.studyId && <Check size={14} className="shrink-0 text-rw-accent" />}
          </button>
        ))}
      </div>

      {/* granularity */}
      <div className="mt-3 text-[10px] font-medium uppercase tracking-[0.12em] text-rw-t4">
        {t('writing.picker.granularity')}
      </div>
      <div className="mt-1.5 flex flex-wrap gap-1.5">
        {granOptions.map((g) => (
          <button
            key={g}
            type="button"
            onClick={() => setGran(g)}
            className={cn(
              'rounded-full border px-3 py-1 text-[13px] transition-colors duration-80',
              gran === g
                ? 'border-rw-accent-bd bg-rw-accent-bg text-rw-accent'
                : 'border-rw-border text-rw-t2 hover:bg-rw-surface-2',
            )}
          >
            {gran === g ? '✓ ' : ''}{t(GRANULARITY_LABEL_KEY[g])}
          </button>
        ))}
      </div>

      {/* privacy strip */}
      <div className="mt-3 rounded-md border border-rw-green bg-rw-green-bg px-3 py-2">
        <div className="text-[13px] font-semibold text-rw-green">
          {t('writing.picker.privacyTitle')}
        </div>
        <div className="mt-0.5 text-[11px] text-rw-t3">
          {t('writing.picker.privacyBody')}
        </div>
      </div>

      <div className="mt-4 flex justify-end gap-2">
        <Button variant="rw-secondary" onClick={onClose}>
          {t('writing.docs.cancel')}
        </Button>
        <Button
          variant="rw-primary"
          disabled={!targetId || busy}
          onClick={() => void insert()}
        >
          {busy ? t('writing.picker.inserting') : t('writing.picker.insert')}
        </Button>
      </div>
    </Modal>
  );
}

/* ════════════════════════════════════════════════════════════════════
   PHI gate modal (mock4) — per-finding 替换建议/忽略 → re-export
   ════════════════════════════════════════════════════════════════════ */

function PhiGateModal({
  findings, exporting, includeSources, onIncludeSourcesChange, onClose, onExport,
}: {
  findings: WritingPhiFinding[];
  exporting: boolean;
  includeSources: boolean;
  onIncludeSourcesChange: (v: boolean) => void;
  onClose: () => void;
  onExport: (resolutions: WritingPhiResolution[]) => void;
}) {
  const t = useT();
  const [resolutions, setResolutions] =
    useState<Array<WritingPhiResolution | null>>(findings.map(() => null));
  const allResolved = resolutions.every((r) => r !== null);

  function resolve(idx: number, r: WritingPhiResolution | null) {
    setResolutions((prev) => prev.map((x, i) => (i === idx ? r : x)));
  }

  return (
    <Modal open onClose={onClose} title={t('writing.phi.title')} tone="rw" width={680}>
      <div className="mb-3 flex items-center gap-2 text-caption text-rw-t3">
        <AlertTriangle size={13} className="text-rw-orange" />
        {t('writing.phi.subtitle', { count: findings.length })}
      </div>

      <div className="max-h-[340px] space-y-2 overflow-y-auto pr-1">
        {findings.map((f, i) => {
          const r = resolutions[i];
          return (
            <div
              key={`${f.start}-${f.end}-${i}`}
              className={cn(
                'rounded-md border p-3',
                r === null ? 'border-rw-orange bg-rw-orange-bg/40' : 'border-rw-border bg-rw-surface',
              )}
            >
              <div className="flex items-center justify-between gap-2">
                <div className="text-[13px] font-semibold text-rw-orange">
                  ⚠ {f.kind}
                </div>
                <div className="text-[11px] text-rw-t4">
                  {r === null
                    ? t('writing.phi.pending')
                    : r.action === 'replace'
                      ? `${t('writing.phi.willReplace')} → ${r.replacement ?? ''}`
                      : t('writing.phi.ignored')}
                </div>
              </div>
              <div className="mt-1 text-[13px] text-rw-t2">
                「…<span className="font-semibold text-rw-red">{f.excerpt}</span>…」
              </div>
              <div className="mt-2 flex flex-wrap items-center gap-2">
                <button
                  type="button"
                  onClick={() => resolve(i, {
                    start: f.start, end: f.end,
                    action: 'replace', replacement: f.suggestion,
                  })}
                  className={cn(
                    'rounded-md px-2.5 py-1 text-[12px] font-medium transition-colors duration-80',
                    r?.action === 'replace'
                      ? 'bg-rw-accent text-[#06252c]'
                      : 'border border-rw-accent-bd text-rw-accent hover:bg-rw-accent-bg',
                  )}
                >
                  {t('writing.phi.replaceWith', { suggestion: f.suggestion })}
                </button>
                <button
                  type="button"
                  onClick={() => resolve(
                    i,
                    r?.action === 'ignore'
                      ? null
                      : { start: f.start, end: f.end, action: 'ignore' },
                  )}
                  className={cn(
                    'rounded-md border px-2.5 py-1 text-[12px] transition-colors duration-80',
                    r?.action === 'ignore'
                      ? 'border-rw-t3 bg-rw-surface-2 text-rw-t1'
                      : 'border-rw-border text-rw-t2 hover:bg-rw-surface-2',
                  )}
                >
                  {t('writing.phi.ignore')}
                </button>
              </div>
            </div>
          );
        })}
      </div>

      <label className="mt-3 flex cursor-pointer items-center gap-2 text-caption text-rw-t3">
        <input
          type="checkbox"
          checked={includeSources}
          onChange={(e) => onIncludeSourcesChange(e.target.checked)}
          className="accent-[var(--rw-accent)]"
        />
        {t('writing.phi.includeSources')}
      </label>

      <div className="mt-4 flex items-center justify-between gap-2 border-t border-rw-border-soft pt-3">
        <div className="text-[11px] text-rw-t4">
          {!allResolved && `⚠ ${t('writing.phi.unresolvedNote')}`}
        </div>
        <div className="flex gap-2">
          <Button variant="rw-secondary" onClick={onClose}>
            {t('writing.phi.back')}
          </Button>
          <Button
            variant="rw-primary"
            disabled={!allResolved || exporting}
            onClick={() => onExport(resolutions.filter((x): x is WritingPhiResolution => x !== null))}
          >
            <Download size={13} />
            {exporting ? t('writing.toolbar.exporting') : t('writing.phi.exportBtn')}
          </Button>
        </div>
      </div>
    </Modal>
  );
}
