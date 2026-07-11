/**
 * Writing Studio (P3) — 写作 workspace, conversational co-writing.
 *
 * Three columns (chat + canvas, ChatGPT-Canvas style):
 *   left   — document list (create / select / delete; collapsible)
 *   middle — 写作对话 chat panel (~420px): transcript from
 *            GET /docs/{id}/chat rendered with the shared MessageRow
 *            + ChatComposer; each turn POSTs /docs/{id}/chat (SSE)
 *            and can rewrite the whole document.
 *   right  — the document CANVAS (flex): title + TipTap editor with
 *            a compact collapsible 引用与快照 drawer at the top,
 *            polish toolbar, streamed diff card, status bar.
 *
 * Flows:
 *   对话共写     → POST /docs/{id}/chat (SSE). reply_chunk streams into
 *                  the assistant bubble; doc_chunk frames are buffered
 *                  server-side text (never live-typed into TipTap); on
 *                  done{doc_body} the SERVER has already applied the
 *                  new body and snapshotted the previous one, so the
 *                  canvas just swaps content and a revision banner
 *                  offers 查看差异 (read-only word-diff modal) / 撤销
 *                  (restore the pre-revision snapshot). The canvas is
 *                  locked read-only while a turn streams, so
 *                  draft-vs-server conflicts can't happen.
 *   @ / ＋引用   → ReferencePickerModal (mock2) → POST /docs/{id}/references
 *                  → insert a refChip atom at the caret (editor), or —
 *                  when triggered from the chat composer — append the
 *                  chip label to the message as a context mention.
 *   选中润色     → activated toolbar row → POST /docs/{id}/polish (SSE)
 *                  → word-level diff card (mock3) with per-hunk ✓/✗.
 *   导出 docx    → POST /docs/{id}/export; on 422 phi_unresolved the
 *                  PHI gate modal (mock4) collects per-finding
 *                  resolutions and re-posts.
 *
 * Editor (P2): TipTap. The SERVER CONTRACT IS UNCHANGED — the document
 * body is still a plain string with {{ref:ID}} tokens and '\n'
 * paragraph breaks (PUT /docs/{id} {body}). lib/writing-doc-serial.ts
 * converts string ⇄ TipTap JSON; tokens hydrate into inline refChip
 * atoms (components/ref-chip.tsx). All starter-kit marks/blocks that
 * can't serialize to that string (bold/italic/headings/lists/…) are
 * DISABLED — only document/paragraph/text/undoRedo survive — so
 * nothing unserializable can enter the doc. draft.body in the store
 * remains the serialized string (re-serialized on every editor
 * update), which keeps autosave, word count, polish offsets, snapshots
 * and the PHI export flow operating on the exact wire format. The
 * read-only 预览 toggle renders {{ref:ID}} tokens as inline chips.
 */
import { useCallback, useEffect, useRef, useState } from 'react';
import {
  AlertTriangle, AtSign, Check, ChevronDown, ChevronRight, Download,
  Eye, PanelLeftClose, PanelLeftOpen, PenLine, Plus, RefreshCw,
  RotateCcw, Sparkles, Trash2, X,
} from 'lucide-react';
import { EditorContent, useEditor, type Editor } from '@tiptap/react';
import StarterKit from '@tiptap/starter-kit';
import {
  api, ApiError,
  type WritingDocMeta, type WritingPhiFinding, type WritingPhiResolution,
  type WritingRefGranularity, type WritingReference, type WritingSnapshot,
} from '../lib/api-client';
import { useAppState, type WritingChatMsg } from '../store';
import { cn, patientDisplayLabel } from '../lib/util';
import { useT } from '../lib/i18n';
import type { Dict } from '../lib/i18n/en-US';
import { Button } from './ui';
import { Modal } from './modal';
import { CopyButton } from './copy-button';
import {
  applyDiff, changeCount, diffWords, type DiffSegment,
} from '../lib/word-diff';
import {
  parseBodyToDoc, REF_TOKEN_RE, serializeDocToBody, type SerialDocNode,
} from '../lib/writing-doc-serial';
import { RefChip, RefChipProvider, refChipLeafText } from './ref-chip';
import { MessageRow } from './chat-message';
import { ChatComposer } from './chat-composer';

/* ───────────────────────── helpers ───────────────────────── */

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

/* ── TipTap doc ⇄ serialized-string offset mapping ──────────────
 *
 * Polish still works on START/END INDICES INTO THE SERIALIZED STRING
 * (the same string autosave PUTs), so ProseMirror positions must map
 * onto string offsets. ``doc.textBetween(a, b, '\n', refChipLeafText)``
 * emits exactly the serialized form of the [a, b) range — '\n' between
 * textblocks (matching serializeDocToBody's join('\n'), incl. empty
 * paragraphs) and each refChip atom as its {{ref:ID}} token — so a
 * position's string offset is just the length of the prefix. */

type PMDoc = Editor['state']['doc'];

/** Editor doc → wire string. Same result as
 *  ``doc.textBetween(0, size, '\n', refChipLeafText)`` — routed through
 *  the pure (unit-tested) serializer for a single source of truth. */
function serializePmDoc(doc: PMDoc): string {
  return serializeDocToBody(doc.toJSON() as SerialDocNode);
}

/** ProseMirror position → offset into the serialized body string. */
function pmPosToOffset(doc: PMDoc, pos: number): number {
  return doc.textBetween(0, pos, '\n', refChipLeafText).length;
}

/** Inverse of ``pmPosToOffset`` — walks the doc counting serialized
 *  chars. Offsets landing INSIDE a chip token clamp to just after the
 *  chip (atoms are indivisible). Out-of-range clamps to doc end. */
function offsetToPmPos(doc: PMDoc, offset: number): number {
  if (offset <= 0) return 1; // start of the first paragraph's content
  let str = 0;        // serialized chars consumed so far
  let result = -1;
  let firstBlock = true;
  doc.descendants((node, pos) => {
    if (result !== -1) return false;
    if (node.isTextblock) {
      if (!firstBlock) str += 1; // the '\n' before this paragraph
      firstBlock = false;
      if (str >= offset) { result = pos + 1; return false; }
      return true; // descend into inline content
    }
    if (node.isText) {
      const len = node.text?.length ?? 0;
      if (str + len >= offset) { result = pos + (offset - str); return false; }
      str += len;
      return false;
    }
    if (node.isLeaf) {
      const len = refChipLeafText(node).length;
      if (str + len >= offset) { result = pos + node.nodeSize; return false; }
      str += len;
      return false;
    }
    return true;
  });
  return result !== -1 ? result : Math.max(doc.content.size - 1, 1);
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

/** F-draft-persist key for a doc's chat composer text (store.drafts). */
function chatDraftKey(docId: string): string {
  return `writing-chat-${docId}`;
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

type PickerRequest =
  /** From the editor: the chip atom goes at ``pmPos``. ``replaceAt``
   *  is true when triggered by typing '@' — that char gets replaced. */
  | { kind: 'editor'; pmPos: number; replaceAt: boolean }
  /** From the chat composer: the reference is attached to the doc
   *  (POST /references) and its chip label is appended to the draft
   *  message as a context mention. */
  | { kind: 'chat' };

/** One AI revision applied by a chat turn — drives the canvas
 *  revision banner + the read-only diff modal. Kept per doc id so
 *  switching documents doesn't lose the banner. */
interface RevisionState {
  /** word-diff of the pre-revision body vs the applied doc_body. */
  segments: DiffSegment[];
  /** Numbers flagged by the provenance_warning frame. */
  warnings: string[];
  /** Pre-revision snapshot — restoring it undoes this revision.
   *  Null if the server didn't return one. */
  snapshotId: string | null;
  messageId: string;
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
  const storeSetDraft        = useAppState((s) => s.setDraft);

  // Co-writing chat — per-doc store state (survives doc / workspace
  // switches mid-stream, mirroring chatMsgsBySession).
  const chatStreaming = useAppState((s) =>
    s.activeWritingDocId
      ? !!s.writingChatStreamingByDoc[s.activeWritingDocId]
      : false);
  const chatMsgs = useAppState((s) =>
    s.activeWritingDocId ? s.writingChatByDoc[s.activeWritingDocId] : undefined);

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
  const [previewMode, setPreviewMode] = useState(false);
  /** Selection as OFFSETS INTO THE SERIALIZED BODY STRING (kept in
   *  sync from TipTap's selection via pmPosToOffset). */
  const [sel, setSel] = useState<{ start: number; end: number }>({ start: 0, end: 0 });
  const [picker, setPicker] = useState<PickerRequest | null>(null);

  // Layout: collapsible docs sidebar + the 引用与快照 drawer at the
  // top of the canvas (they replaced the old right rail).
  const [docsCollapsed, setDocsCollapsed] = useState(false);
  const [drawerOpen, setDrawerOpen] = useState(false);

  // Co-writing revisions — banner + diff modal state, keyed by doc id
  // so switching docs keeps each doc's last-revision banner.
  const [revisions, setRevisions] = useState<Record<string, RevisionState>>({});
  const [diffOpen, setDiffOpen] = useState(false);
  /** Doc currently receiving doc_chunk frames (drives the 正在改写文档…
   *  indicator in the canvas header). */
  const [revisingDocId, setRevisingDocId] = useState<string | null>(null);
  const revision = activeDocId ? revisions[activeDocId] : undefined;
  /** Empty doc + empty (loaded) chat → centered starter templates. */
  const starterVisible = !!activeDocId && draft !== undefined
    && draft.body === '' && chatMsgs !== undefined
    && chatMsgs.length === 0 && !chatStreaming;

  // Polish
  const [polish, setPolish] = useState<PolishState | null>(null);
  const [customInstruction, setCustomInstruction] = useState('');
  const [excludedRefIds, setExcludedRefIds] = useState<Set<string>>(new Set());
  const polishAbortRef = useRef<AbortController | null>(null);

  // Export / PHI gate
  const [exporting, setExporting] = useState(false);
  const [includeSources, setIncludeSources] = useState(true);
  const [phiFindings, setPhiFindings] = useState<WritingPhiFinding[] | null>(null);

  /* ── TipTap editor ────────────────────────────────────────────
   *
   * One instance per document (deps: [activeDocId]) so ⌘Z history
   * never leaks across docs. ``lastEditorBodyRef`` holds the
   * serialized string of what the editor currently contains — the
   * draft→editor sync effect below only rehydrates when the store
   * draft diverges from it (doc load, snapshot restore, polish
   * apply), never while the medic is typing. */

  const lastEditorBodyRef = useRef<string | null>(null);

  const editor = useEditor({
    extensions: [
      StarterKit.configure({
        // Server contract is a plain {{ref:ID}}-tokenized string —
        // every mark/block that can't serialize to it is DISABLED.
        // Survivors: document, paragraph, text, undoRedo (⌘Z),
        // dropcursor/gapcursor (cosmetic).
        blockquote: false,
        bold: false,
        bulletList: false,
        code: false,
        codeBlock: false,
        hardBreak: false,
        heading: false,
        horizontalRule: false,
        italic: false,
        link: false,
        listItem: false,
        listKeymap: false,
        orderedList: false,
        strike: false,
        trailingNode: false,
        underline: false,
      }),
      RefChip,
    ],
    content: parseBodyToDoc(
      useAppState.getState().writingDrafts[activeDocId ?? '']?.body ?? ''),
    editorProps: {
      attributes: { spellcheck: 'false' },
      // '@' at the caret → ReferencePickerModal (mock2). We don't
      // block the keystroke: the literal '@' lands in the doc (and
      // STAYS there if the picker is cancelled — same semantics as
      // the old textarea); on confirm the chip replaces it. The
      // setTimeout lets the char insert before the modal mounts.
      handleKeyDown: (view, event) => {
        if (event.key === '@' && !event.metaKey && !event.ctrlKey && !event.altKey) {
          const pmPos = view.state.selection.from;
          window.setTimeout(
            () => setPicker({ kind: 'editor', pmPos, replaceAt: true }), 0);
        }
        return false;
      },
    },
    onCreate: ({ editor: ed }) => {
      lastEditorBodyRef.current = serializePmDoc(ed.state.doc);
    },
    // Serialize on EVERY update (throttled to onUpdate itself — docs
    // are small; the 1.5 s autosave debounce does the real batching)
    // so draft.body in the store is always the exact wire string.
    onUpdate: ({ editor: ed }) => {
      const body = serializePmDoc(ed.state.doc);
      lastEditorBodyRef.current = body;
      const st = useAppState.getState();
      if (!activeDocId || st.activeWritingDocId !== activeDocId) return;
      const prev = st.writingDrafts[activeDocId];
      if (prev?.body !== body) {
        st.setWritingDraft(activeDocId, { title: prev?.title ?? '', body });
      }
    },
    onSelectionUpdate: ({ editor: ed }) => {
      const { from, to } = ed.state.selection;
      const start = pmPosToOffset(ed.state.doc, from);
      const selected = ed.state.doc.textBetween(from, to, '\n', refChipLeafText);
      setSel({ start, end: start + selected.length });
    },
  }, [activeDocId]);

  /** Replace the editor doc from a serialized body string, OUTSIDE
   *  undo history (hydrations aren't medic edits — snapshots are the
   *  revert path). Optional caret as a string offset. */
  const applyBodyToEditor = useCallback(
    (ed: Editor, body: string, caretOffset?: number) => {
      lastEditorBodyRef.current = body;
      const newDoc = ed.schema.nodeFromJSON(parseBodyToDoc(body));
      const tr = ed.state.tr
        .replaceWith(0, ed.state.doc.content.size, newDoc.content)
        .setMeta('addToHistory', false);
      ed.view.dispatch(tr);
      if (caretOffset !== undefined) {
        ed.commands.setTextSelection(Math.min(
          offsetToPmPos(ed.state.doc, caretOffset),
          Math.max(ed.state.doc.content.size - 1, 1),
        ));
      }
    }, []);

  // Store draft changed under the editor (doc fetch landed, snapshot
  // restored) → rehydrate. No-op while typing: onUpdate already set
  // lastEditorBodyRef to the same string it wrote to the store.
  const draftBodyForSync = draft?.body;
  useEffect(() => {
    if (!editor || editor.isDestroyed || draftBodyForSync === undefined) return;
    if (lastEditorBodyRef.current === draftBodyForSync) return;
    applyBodyToEditor(editor, draftBodyForSync);
  }, [editor, draftBodyForSync, applyBodyToEditor]);

  // Canvas lock — while a chat turn is streaming the editor goes
  // read-only (dim + tooltip), so the medic's typing can never race
  // the server-applied doc_body (no draft-vs-server conflict).
  useEffect(() => {
    if (editor && !editor.isDestroyed) editor.setEditable(!chatStreaming);
  }, [editor, chatStreaming]);

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
    setDiffOpen(false);
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
  // The '@' keydown trigger itself lives in the editor's
  // handleKeyDown (see useEditor above).

  function openPickerFromToolbar() {
    if (!activeDocId || !editor) return;
    setPicker({ kind: 'editor', pmPos: editor.state.selection.from, replaceAt: false });
  }

  function onReferenceInserted(ref: WritingReference) {
    if (!activeDocId || !picker) {
      setPicker(null);
      return;
    }
    if (picker.kind === 'chat') {
      // Chat mention — the reference is already attached to the doc
      // (the picker POSTed /references); the draft message just gets
      // its chip label appended as a context mention. The trailing
      // '@' the medic typed (if still there) is absorbed.
      const key = chatDraftKey(activeDocId);
      const cur = useAppState.getState().drafts[key] ?? '';
      const mention = `@${ref.chipLabel}`;
      const next = cur.endsWith('@')
        ? `${cur.slice(0, -1)}${mention} `
        : `${cur}${cur && !/\s$/.test(cur) ? ' ' : ''}${mention} `;
      storeSetDraft(key, next);
      setReferences((rs) => [...rs, ref]);
      setDocs((ds) => ds.map((d) => d.id === activeDocId
        ? { ...d, refCount: d.refCount + 1 } : d));
      setPicker(null);
      return;
    }
    if (!editor || editor.isDestroyed) {
      setPicker(null);
      return;
    }
    const max = editor.state.doc.content.size;
    const pos = Math.max(1, Math.min(picker.pmPos, Math.max(max - 1, 1)));
    // Replace the typed '@' only if it is still there (defensive —
    // the picker is modal, but don't eat an arbitrary char).
    const replaceAt = picker.replaceAt
      && pos + 1 <= max
      && editor.state.doc.textBetween(pos, pos + 1) === '@';
    // One chained transaction = ONE history step: ⌘Z removes the chip
    // (and restores the '@' it replaced). Caret lands after the chip.
    editor
      .chain()
      .focus()
      .insertContentAt(
        replaceAt ? { from: pos, to: pos + 1 } : pos,
        { type: RefChip.name, attrs: { refId: ref.refId } },
      )
      .run();
    setReferences((rs) => [...rs, ref]);
    setDocs((ds) => ds.map((d) => d.id === activeDocId
      ? { ...d, refCount: d.refCount + 1 } : d));
    setPicker(null);
  }

  /* ── co-writing chat ──────────────────────────────────────── */

  /**
   * One chat turn. All transcript mutations go through the store
   * (keyed by doc id) so the stream survives doc / workspace switches;
   * editor / save-state side effects only run if the doc is still
   * active when the frame lands. Deliberately NOT abortable: once the
   * server starts a turn it may apply a doc revision — abandoning the
   * stream client-side would desync the canvas from the server copy.
   */
  async function sendChatTurn(text: string) {
    if (!activeDocId) return;
    const docId = activeDocId;
    const st = useAppState.getState();
    if (st.writingChatStreamingByDoc[docId]) return;

    st.setWritingChatError(docId, null);
    // Mark the transcript as hydrated so the panel's history fetch
    // can't clobber the live turn.
    if (st.writingChatByDoc[docId] === undefined) {
      st.setWritingChatMsgs(docId, []);
    }
    const now = new Date().toISOString();
    st.appendWritingChatMsg(docId, {
      id: `local-user-${Date.now()}`, role: 'user', text,
      docApplied: false, createdAt: now,
    });
    st.appendWritingChatMsg(docId, {
      id: `local-assistant-${Date.now()}`, role: 'assistant', text: '',
      docApplied: false, createdAt: now,
    });
    st.setWritingChatStreaming(docId, true);
    st.setDraft(chatDraftKey(docId), '');

    // provenance_warning arrives before done — buffer it. doc_chunk
    // frames are NOT accumulated into the editor (no live typing);
    // done.doc_body is the canonical applied text.
    let warnings: string[] = [];
    try {
      for await (const frame of api.chatWritingDoc(
        docId, { message: text, refIds: includedRefIds },
      )) {
        const g = useAppState.getState();
        if (frame.type === 'reply_chunk') {
          g.updateLastWritingChatMsg(docId, (last) => ({
            text: last.text + frame.text,
          }));
        } else if (frame.type === 'doc_started') {
          setRevisingDocId(docId);
        } else if (frame.type === 'doc_chunk') {
          // Buffered server-side; the canvas swaps once on done.
        } else if (frame.type === 'provenance_warning') {
          warnings = frame.numbers ?? [];
        } else if (frame.type === 'done') {
          g.updateLastWritingChatMsg(docId, {
            id: frame.message_id,
            text: frame.reply,
            docApplied: frame.doc_body !== null,
          });
          if (frame.doc_body !== null) {
            const newBody = frame.doc_body;
            const snapId = frame.snapshot_id === null
              || frame.snapshot_id === undefined
              ? null : String(frame.snapshot_id);
            if (snapId) g.setWritingChatSnapshot(frame.message_id, snapId);
            const prevDraft = g.writingDrafts[docId];
            const title = prevDraft?.title ?? '';
            setRevisions((r) => ({
              ...r,
              [docId]: {
                segments: diffWords(prevDraft?.body ?? '', newBody),
                warnings,
                snapshotId: snapId,
                messageId: frame.message_id,
              },
            }));
            // The server ALREADY applied doc_body + snapshotted the
            // previous body — mirror it locally.
            const stillActive = g.activeWritingDocId === docId;
            if (stillActive && editor && !editor.isDestroyed) {
              // Sets lastEditorBodyRef so the draft-sync effect no-ops.
              applyBodyToEditor(editor, newBody);
            }
            g.setWritingDraft(docId, { title, body: newBody });
            if (stillActive) {
              lastSavedRef.current = { docId, title, body: newBody };
              setSaveState('saved');
              setSavedAt(fmtClock(new Date()));
              try {
                setSnapshots(await api.listWritingSnapshots(docId));
              } catch { /* non-fatal — drawer refresh only */ }
            }
          }
        } else if (frame.type === 'error') {
          throw new Error(frame.message);
        }
      }
    } catch (e) {
      const g = useAppState.getState();
      // Drop the empty assistant bubble (its reply never arrived) —
      // the error renders as the inline row above the composer.
      const cur = g.writingChatByDoc[docId] ?? [];
      const last = cur[cur.length - 1];
      if (last && last.role === 'assistant' && last.text === '' && !last.docApplied) {
        g.setWritingChatMsgs(docId, cur.slice(0, -1));
      }
      g.setWritingChatError(
        docId, t('writing.chat.failed', { error: errMsg(e) }));
    } finally {
      useAppState.getState().setWritingChatStreaming(docId, false);
      setRevisingDocId((d) => (d === docId ? null : d));
    }
  }

  /** Undo an AI revision — restore the pre-revision snapshot the
   *  server minted for this chat turn, then drop the matching banner. */
  async function onUndoRevision(snapshotId: string) {
    if (!activeDocId) return;
    const docId = activeDocId;
    await onRestoreSnapshot(snapshotId);
    setDiffOpen(false);
    setRevisions((r) => {
      const cur = r[docId];
      if (!cur || cur.snapshotId !== snapshotId) return r;
      const next = { ...r };
      delete next[docId];
      return next;
    });
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
    // Rehydrate the editor from the spliced string FIRST (sets
    // lastEditorBodyRef so the sync effect no-ops) with the caret at
    // the end of the merged region, then persist the draft.
    if (editor && !editor.isDestroyed) {
      applyBodyToEditor(editor, newBody, start + merged.length);
    }
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
      {/* left — documents (collapsible) */}
      <DocsSidebar
        docs={docs}
        activeDocId={activeDocId}
        collapsed={docsCollapsed}
        onToggleCollapsed={() => setDocsCollapsed((v) => !v)}
        onSelect={setActiveDocId}
        onNew={() => void onCreateDoc()}
        onDelete={setPendingDelete}
      />

      {/* middle — 写作对话 chat panel */}
      {activeDocId && draft !== undefined && (
        <WritingChatPanel
          docId={activeDocId}
          streaming={chatStreaming}
          onSend={(text) => void sendChatTurn(text)}
          onAtTyped={() => setPicker({ kind: 'chat' })}
          onUndoRevision={(sid) => void onUndoRevision(sid)}
        />
      )}

      {/* right — document canvas */}
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
              <div className="flex items-center gap-3">
                {revisingDocId === activeDocId && (
                  <span className="flex items-center gap-1.5 text-caption text-rw-accent">
                    <RefreshCw size={12} className="animate-spin" />
                    {t('writing.chat.revising')}
                  </span>
                )}
                <Button
                  variant="rw-primary"
                  disabled={exporting}
                  onClick={() => void runExport([])}
                >
                  <Download size={13} />
                  {exporting ? t('writing.toolbar.exporting') : t('writing.toolbar.export')}
                </Button>
              </div>
            </div>

            {/* 引用与快照 — compact collapsible drawer (replaces the
                old right rail; the chat panel took that column). */}
            <ContextDrawer
              open={drawerOpen}
              onToggle={() => setDrawerOpen((v) => !v)}
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

            {/* revision banner — the last AI revision on this doc */}
            {revision && (
              <div className="flex shrink-0 flex-wrap items-center gap-2 border-b border-rw-border bg-rw-accent-bg px-5 py-1.5">
                <Sparkles size={13} className="shrink-0 text-rw-accent" />
                <span className="text-[12px] font-medium text-rw-t1">
                  {t('writing.revision.banner')}
                </span>
                {revision.warnings.length > 0 && (
                  <span
                    title={t('writing.polish.provenance', {
                      numbers: revision.warnings.join(' · '),
                    })}
                    className="inline-flex items-center gap-1 rounded-full border
                               border-rw-orange bg-rw-orange-bg px-2 py-0.5
                               text-[11px] text-rw-orange"
                  >
                    <AlertTriangle size={11} />
                    {t('writing.revision.warnChip')}
                  </span>
                )}
                <div className="ml-auto flex items-center gap-1.5">
                  <Button variant="rw-secondary" onClick={() => setDiffOpen(true)}>
                    <Eye size={13} />
                    {t('writing.revision.viewDiff')}
                  </Button>
                  {revision.snapshotId && (
                    <Button
                      variant="rw-secondary"
                      onClick={() => void onUndoRevision(revision.snapshotId!)}
                    >
                      <RotateCcw size={13} />
                      {t('writing.revision.undo')}
                    </Button>
                  )}
                  <Button
                    variant="rw-secondary"
                    onClick={() => setRevisions((r) => {
                      const next = { ...r };
                      delete next[activeDocId];
                      return next;
                    })}
                  >
                    <Check size={13} />
                    {t('writing.revision.keep')}
                  </Button>
                </div>
              </div>
            )}

            {/* title + body */}
            <div className="flex min-h-0 flex-1 flex-col overflow-y-auto px-6 pt-4">
              <input
                value={draft.title}
                onChange={(e) => setDraftTitle(e.target.value)}
                placeholder={t('writing.editor.titlePlaceholder')}
                disabled={chatStreaming}
                title={chatStreaming ? t('writing.canvas.locked') : undefined}
                className="w-full shrink-0 border-b border-rw-border bg-transparent pb-3
                           text-xl font-semibold text-rw-t1 outline-none
                           placeholder:text-rw-t4 disabled:opacity-60"
              />
              {previewMode ? (
                <PreviewBody body={draft.body} references={references} />
              ) : (
                <div
                  className={cn(
                    'relative min-h-[280px] w-full flex-1',
                    // Canvas lock — read-only + dim while a chat turn
                    // streams (editor.setEditable is the hard lock).
                    chatStreaming && 'pointer-events-none opacity-60',
                  )}
                  title={chatStreaming ? t('writing.canvas.locked') : undefined}
                >
                  {/* TipTap ships no placeholder in starter-kit v3 —
                      a pointer-transparent overlay is enough here. */}
                  {draft.body === '' && !starterVisible && (
                    <div
                      aria-hidden
                      className="pointer-events-none absolute left-0 top-4 font-mono
                                 text-[13.5px] leading-7 text-rw-t4"
                    >
                      {t('writing.editor.bodyPlaceholder')}
                    </div>
                  )}
                  {/* empty doc + empty chat → starter templates that
                      prefill the chat composer */}
                  {starterVisible && (
                    <StarterPanel
                      onPick={(text) =>
                        storeSetDraft(chatDraftKey(activeDocId), text)}
                    />
                  )}
                  <RefChipProvider references={references}>
                    <EditorContent
                      editor={editor}
                      className="h-full w-full
                                 [&_.ProseMirror]:min-h-[280px] [&_.ProseMirror]:w-full
                                 [&_.ProseMirror]:bg-transparent [&_.ProseMirror]:py-4
                                 [&_.ProseMirror]:font-mono [&_.ProseMirror]:text-[13.5px]
                                 [&_.ProseMirror]:leading-7 [&_.ProseMirror]:text-rw-t1
                                 [&_.ProseMirror]:outline-none [&_.ProseMirror_p]:m-0"
                    />
                  </RefChipProvider>
                </div>
              )}

              {/* polish toolbar — activates on non-empty selection */}
              {!previewMode && (
                <PolishToolbar
                  active={hasSelection && !chatStreaming}
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

      {/* modals */}
      {diffOpen && revision && (
        <Modal
          open
          onClose={() => setDiffOpen(false)}
          title={t('writing.revision.diffTitle')}
          tone="rw"
          width={680}
        >
          <div className="max-h-[380px] overflow-y-auto whitespace-pre-wrap
                          rounded-md border border-rw-border bg-rw-bg-deep p-4
                          text-[14px] leading-7">
            <ReadOnlyDiff segments={revision.segments} />
          </div>
          {revision.warnings.length > 0 && (
            <div className="mt-3 flex items-start gap-2 rounded-md border border-rw-orange bg-rw-orange-bg px-3 py-2 text-caption text-rw-orange">
              <AlertTriangle size={13} className="mt-0.5 shrink-0" />
              <span>
                {t('writing.polish.provenance', {
                  numbers: revision.warnings.join(' · '),
                })}
              </span>
            </div>
          )}
          {/* The revision is ALREADY applied server-side, so per-hunk
              accept/reject would fight the server state — the choices
              collapse to keep (close) or undo (restore snapshot). */}
          <div className="mt-4 flex justify-end gap-2 border-t border-rw-border-soft pt-3">
            {revision.snapshotId && (
              <Button
                variant="rw-secondary"
                onClick={() => void onUndoRevision(revision.snapshotId!)}
              >
                <RotateCcw size={13} />
                {t('writing.revision.undo')}
              </Button>
            )}
            <Button variant="rw-primary" onClick={() => setDiffOpen(false)}>
              <Check size={13} />
              {t('writing.revision.keep')}
            </Button>
          </div>
        </Modal>
      )}
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
  docs, activeDocId, collapsed, onToggleCollapsed, onSelect, onNew, onDelete,
}: {
  docs: WritingDocMeta[];
  activeDocId: string | null;
  collapsed: boolean;
  onToggleCollapsed: () => void;
  onSelect: (id: string) => void;
  onNew: () => void;
  onDelete: (d: WritingDocMeta) => void;
}) {
  const t = useT();
  if (collapsed) {
    return (
      <aside className="flex h-full w-11 shrink-0 flex-col items-center border-r border-rw-border bg-rw-bg-deep pt-3">
        <button
          type="button"
          onClick={onToggleCollapsed}
          title={t('writing.docs.expand')}
          aria-label={t('writing.docs.expand')}
          className="rounded-md p-1.5 text-rw-t3 transition-colors duration-80
                     hover:bg-rw-surface hover:text-rw-t1"
        >
          <PanelLeftOpen size={15} />
        </button>
      </aside>
    );
  }
  return (
    <aside className="flex h-full w-[240px] shrink-0 flex-col border-r border-rw-border bg-rw-bg-deep">
      <div className="flex items-center justify-between px-4 pb-1 pt-4">
        <span className="text-[10px] font-medium uppercase tracking-[0.12em] text-rw-t4">
          {t('writing.docs.title')}
        </span>
        <button
          type="button"
          onClick={onToggleCollapsed}
          title={t('writing.docs.collapse')}
          aria-label={t('writing.docs.collapse')}
          className="rounded-md p-1 text-rw-t4 transition-colors duration-80
                     hover:bg-rw-surface hover:text-rw-t1"
        >
          <PanelLeftClose size={13} />
        </button>
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
   Context drawer — 引用与快照. Compact collapsible strip at the top of
   the document canvas (the old right rail's content; the 写作对话 chat
   panel took over that column).
   ════════════════════════════════════════════════════════════════════ */

function ContextDrawer({
  open, onToggle, references, snapshots, excludedRefIds, onToggleRef, onRestore,
}: {
  open: boolean;
  onToggle: () => void;
  references: WritingReference[];
  snapshots: WritingSnapshot[];
  excludedRefIds: Set<string>;
  onToggleRef: (refId: string, included: boolean) => void;
  onRestore: (snapshotId: string) => void;
}) {
  const t = useT();
  return (
    <div className="shrink-0 border-b border-rw-border bg-rw-bg-deep">
      <button
        type="button"
        onClick={onToggle}
        aria-expanded={open}
        className="flex w-full items-center gap-1.5 px-5 py-1.5 text-left
                   text-[10px] font-medium uppercase tracking-[0.12em] text-rw-t4
                   transition-colors duration-80 hover:text-rw-t2"
      >
        {open ? <ChevronDown size={12} /> : <ChevronRight size={12} />}
        <span>{t('writing.drawer.title')}</span>
        <span className="normal-case tracking-normal text-rw-t4">
          {t('writing.refs.title', { count: references.length })}
          {' · '}
          {t('writing.editor.snapshotCount', { count: snapshots.length })}
        </span>
      </button>
      {open && (
        <div className="grid max-h-[240px] grid-cols-2 gap-4 overflow-y-auto px-5 pb-3">
          {/* references */}
          <section className="min-w-0">
            {references.length === 0 && (
              <div className="py-1 text-caption text-rw-t3">
                {t('writing.refs.empty')}
              </div>
            )}
            <div className="space-y-2">
              {references.map((r) => {
                const included = !excludedRefIds.has(r.refId);
                return (
                  <div
                    key={r.refId}
                    className="rounded-md border border-rw-border bg-rw-surface p-2.5"
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
                      <div className="mt-1 line-clamp-2 text-[11px] leading-4 text-rw-t4">
                        {r.snapshotPreview}
                      </div>
                    )}
                  </div>
                );
              })}
            </div>
          </section>

          {/* snapshots */}
          <section className="min-w-0">
            <div className="pb-1 text-[10px] font-medium uppercase tracking-[0.12em] text-rw-t4">
              {t('writing.snapshots.title')}
            </div>
            {snapshots.length === 0 && (
              <div className="text-caption text-rw-t3">{t('writing.snapshots.empty')}</div>
            )}
            <div className="space-y-1.5">
              {snapshots.map((s) => (
                <div
                  key={s.id}
                  className="flex items-center justify-between gap-2 rounded-md border border-rw-border bg-rw-surface px-2.5 py-1.5"
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
          </section>
        </div>
      )}
    </div>
  );
}

/* ════════════════════════════════════════════════════════════════════
   写作对话 — the co-writing chat panel (middle column)
   ════════════════════════════════════════════════════════════════════ */

function WritingChatPanel({
  docId, streaming, onSend, onAtTyped, onUndoRevision,
}: {
  docId: string;
  streaming: boolean;
  onSend: (text: string) => void;
  /** '@' typed into the composer → open the reference picker. */
  onAtTyped: () => void;
  onUndoRevision: (snapshotId: string) => void;
}) {
  const t = useT();
  const msgs = useAppState((s) => s.writingChatByDoc[docId]);
  const error = useAppState((s) => s.writingChatErrorByDoc[docId] ?? null);
  const setWritingChatError = useAppState((s) => s.setWritingChatError);
  const snapshotByMsg = useAppState((s) => s.writingChatSnapshotByMsg);
  const draftText = useAppState((s) => s.drafts[chatDraftKey(docId)] ?? '');
  const setDraft = useAppState((s) => s.setDraft);

  // History hydrate — once per doc (undefined = never loaded; a live
  // send marks the transcript loaded first, so we never clobber it).
  const loaded = msgs !== undefined;
  useEffect(() => {
    if (loaded) return;
    let cancelled = false;
    (async () => {
      try {
        const list = await api.getWritingChat(docId);
        if (cancelled) return;
        const st = useAppState.getState();
        if (st.writingChatByDoc[docId] !== undefined) return;
        st.setWritingChatMsgs(docId, list.map((m): WritingChatMsg => ({
          id: m.id, role: m.role, text: m.text,
          docApplied: m.docApplied, createdAt: m.createdAt,
        })));
      } catch (e) {
        if (!cancelled) {
          useAppState.getState().showToast(
            t('writing.chat.loadFailed', { error: errMsg(e) }), 'error');
        }
      }
    })();
    return () => { cancelled = true; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [docId, loaded]);

  // Keep the transcript pinned to the bottom as messages stream in.
  const scrollRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const el = scrollRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [msgs]);

  const list = msgs ?? [];

  return (
    <aside className="flex h-full w-[420px] shrink-0 flex-col border-r border-rw-border bg-rw-bg-deep">
      <div className="shrink-0 px-4 pb-1 pt-4 text-[10px] font-medium uppercase tracking-[0.12em] text-rw-t4">
        {t('writing.chat.title')}
      </div>
      <div ref={scrollRef} className="flex-1 space-y-4 overflow-y-auto px-4 py-3">
        {loaded && list.length === 0 && (
          <div className="py-2 text-caption text-rw-t3">
            {t('writing.chat.empty')}
          </div>
        )}
        {list.map((m, i) => {
          const isLast = i === list.length - 1;
          const snapId = m.role === 'assistant' ? snapshotByMsg[m.id] : undefined;
          return (
            <MessageRow
              key={m.id}
              role={m.role === 'user' ? 'user' : 'agent'}
              text={m.text}
              ts={m.createdAt ? fmtDateTime(m.createdAt) : undefined}
              tone="base"
              streaming={streaming && isLast && m.role === 'assistant'}
            >
              {m.role === 'assistant' && m.docApplied && (
                <div className="mt-1.5 flex flex-wrap items-center gap-2">
                  <span className="inline-flex items-center gap-1 rounded-full border border-rw-green bg-rw-green-bg px-2 py-0.5 text-[11px] text-rw-green">
                    <Check size={11} />
                    {t('writing.chat.docApplied')}
                  </span>
                  {/* GET /chat doesn't return snapshot ids — undo only
                      renders for turns captured live this launch. */}
                  {snapId && (
                    <button
                      type="button"
                      onClick={() => onUndoRevision(snapId)}
                      className="inline-flex items-center gap-1 rounded-full border border-rw-border
                                 px-2 py-0.5 text-[11px] text-rw-t3 transition-colors duration-80
                                 hover:border-rw-accent-bd hover:text-rw-t1"
                    >
                      <RotateCcw size={11} />
                      {t('writing.chat.undoRevision')}
                    </button>
                  )}
                </div>
              )}
            </MessageRow>
          );
        })}
      </div>
      <div className="shrink-0 border-t border-rw-border px-3 py-3">
        <ChatComposer
          value={draftText}
          onChange={(text) => {
            // '@' typed (composer grew by exactly that char) → open
            // the reference picker in chat-mention mode.
            if (text.length === draftText.length + 1 && text.endsWith('@')) {
              onAtTyped();
            }
            setDraft(chatDraftKey(docId), text);
          }}
          onSend={() => {
            const v = draftText.trim();
            if (v && !streaming) onSend(v);
          }}
          disabled={streaming}
          sendDisabled={!draftText.trim()}
          tone="base"
          placeholder={t('writing.chat.placeholder')}
          error={error}
          onDismissError={() => setWritingChatError(docId, null)}
        />
      </div>
    </aside>
  );
}

/* ════════════════════════════════════════════════════════════════════
   Read-only word diff — the revision modal body. Same visual language
   as PolishDiffCard's segments, minus the per-hunk ✓/✗ (the revision
   is already applied server-side; choices collapse to 保留/撤销).
   ════════════════════════════════════════════════════════════════════ */

function ReadOnlyDiff({ segments }: { segments: DiffSegment[] }) {
  return (
    <>
      {segments.map((seg, i) => {
        if (seg.kind === 'same') {
          return <span key={i} className="text-rw-t2">{seg.text}</span>;
        }
        return (
          <span key={i} className="mx-0.5">
            {seg.del && (
              <span className="rounded-sm bg-rw-red-bg px-0.5 text-rw-red line-through">
                {seg.del}
              </span>
            )}
            {seg.add && (
              <span className="rounded-sm bg-rw-green-bg px-0.5 text-rw-green">
                {seg.add}
              </span>
            )}
          </span>
        );
      })}
    </>
  );
}

/* ════════════════════════════════════════════════════════════════════
   Starter panel — empty doc + empty chat. Template prompts that
   prefill the chat composer.
   ════════════════════════════════════════════════════════════════════ */

const STARTER_TEMPLATE_KEYS: Array<keyof Dict> = [
  'writing.starter.t1',
  'writing.starter.t2',
  'writing.starter.t3',
];

function StarterPanel({ onPick }: { onPick: (text: string) => void }) {
  const t = useT();
  return (
    <div className="absolute inset-0 z-10 flex items-center justify-center">
      <div className="w-full max-w-[420px] rounded-md border border-rw-border bg-rw-bg-deep p-5 text-center shadow-lg">
        <Sparkles size={18} className="mx-auto text-rw-accent" />
        <div className="mt-2 text-[14px] font-semibold text-rw-t1">
          {t('writing.starter.title')}
        </div>
        <div className="mt-1 text-caption text-rw-t3">
          {t('writing.chat.empty')}
        </div>
        <div className="mt-4 space-y-2">
          {STARTER_TEMPLATE_KEYS.map((k) => (
            <button
              key={k}
              type="button"
              onClick={() => onPick(t(k))}
              className="block w-full rounded-md border border-rw-border bg-rw-surface
                         px-3 py-2 text-left text-[13px] text-rw-t2 transition-colors
                         duration-80 hover:border-rw-accent-bd hover:text-rw-t1"
            >
              {t(k)}
            </button>
          ))}
        </div>
      </div>
    </div>
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
