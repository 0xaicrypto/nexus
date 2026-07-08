/**
 * F-unified-chat-files — UI components for the per-chat file library.
 *
 * Two pieces, both consumed by all 4 chat surfaces (EncounterMode /
 * Research ChatTab / CrossResearchChat / AssistantWorkspace):
 *
 *   <ChatFileChipStrip /> -- compact horizontal strip sitting above
 *     the composer. Shows up to 3 chips + "全部" link to open the
 *     full drawer. Each chip carries [F1] [F2] tokens (the same the
 *     LLM uses for citations) + extraction-status badge.
 *
 *   <ChatFileLibDrawer /> -- modal/drawer showing the full library
 *     (active + removed tabs), each row with: f_id_token, status
 *     badge, size, age, view, delete/restore actions.
 *
 * The scope_kind / scope_ref pair is the ONLY input that differs
 * between chat surfaces; everything else (fetch, render, action
 * handlers) is identical.
 */
import { useCallback, useEffect, useState } from 'react';
import { api } from '../lib/api-client';
import { useAppState } from '../store';

export type LibScopeKind = 'patient' | 'research' | 'cross_research' | 'assistant';

export interface ChatFileEntry {
  fileId: string;
  name: string;
  mime: string;
  sizeBytes: number;
  createdAt: string;
  fIdToken: string;
  textExtractionStatus: string;
  hasText: boolean;
  deletedAt?: number | null;
}

interface UseChatFilesResult {
  files: ChatFileEntry[];
  removed: ChatFileEntry[];
  totalActive: number;
  totalRemoved: number;
  loading: boolean;
  refresh: () => Promise<void>;
  remove: (fileId: string) => Promise<void>;
  restore: (fileId: string) => Promise<void>;
  reextract: (fileId: string) => Promise<void>;
}

/**
 * Hook that owns the file-list state for one (scopeKind, scopeRef)
 * library. Returns the entries + mutation handlers. Caller-shared
 * across the chip strip + the drawer so both UIs stay in sync.
 */
export function useChatFiles(
  scopeKind: LibScopeKind,
  scopeRef: string,
): UseChatFilesResult {
  const showToast = useAppState((s) => s.showToast);
  const [files, setFiles] = useState<ChatFileEntry[]>([]);
  const [removed, setRemoved] = useState<ChatFileEntry[]>([]);
  const [totalActive, setTotalActive] = useState(0);
  const [totalRemoved, setTotalRemoved] = useState(0);
  const [loading, setLoading] = useState(false);

  const refresh = useCallback(async () => {
    if (!scopeRef) return;
    setLoading(true);
    try {
      const r = await api.listChatFiles({
        scopeKind, scopeRef, includeRemoved: true,
      });
      setFiles(r.files.filter((f) => !f.deletedAt));
      setRemoved(r.files.filter((f) => f.deletedAt));
      setTotalActive(r.totalActive);
      setTotalRemoved(r.totalRemoved);
    } catch (e) {
      // Library is optional UX — don't fail the chat surface; just
      // log + show empty.
      console.warn('listChatFiles failed', e);
      setFiles([]); setRemoved([]);
      setTotalActive(0); setTotalRemoved(0);
    } finally {
      setLoading(false);
    }
  }, [scopeKind, scopeRef]);

  useEffect(() => { refresh(); }, [refresh]);

  const remove = useCallback(async (fileId: string) => {
    try {
      await api.deleteChatFile(fileId);
      showToast('已移除,7 天内可恢复', 'success');
      await refresh();
    } catch (e) {
      showToast(`移除失败: ${String(e)}`, 'error');
    }
  }, [refresh, showToast]);

  const restore = useCallback(async (fileId: string) => {
    try {
      await api.restoreChatFile(fileId);
      showToast('已恢复', 'success');
      await refresh();
    } catch (e) {
      showToast(`恢复失败: ${String(e)}`, 'error');
    }
  }, [refresh, showToast]);

  const reextract = useCallback(async (fileId: string) => {
    try {
      const r = await api.reextractChatFile(fileId);
      showToast(`重新提取完成: status=${r.textExtractionStatus}`,
        r.textLength > 0 ? 'success' : 'info');
      await refresh();
    } catch (e) {
      showToast(`重新提取失败: ${String(e)}`, 'error');
    }
  }, [refresh, showToast]);

  return {
    files, removed, totalActive, totalRemoved, loading,
    refresh, remove, restore, reextract,
  };
}


// ──────────────────────────────────────────────────────────────────
// Status badge
// ──────────────────────────────────────────────────────────────────

function StatusBadge({ status }: { status: string }) {
  // Map the server's text_extraction_status to a tiny inline glyph.
  // Hover for the verbose tooltip.
  if (status === 'text_layer') return null;   // happy path, no clutter
  if (status === 'pending') {
    return <span title="正在提取..." className="text-[10px] ml-0.5">⏳</span>;
  }
  if (status === 'vision_ocr') {
    return (
      <span title="AI 视觉识别提取 — 医疗决策请额外核对"
            className="text-[10px] ml-0.5">🤖</span>
    );
  }
  if (status === 'encrypted') {
    return <span title="加密文件,无法提取" className="text-[10px] ml-0.5">🔒</span>;
  }
  if (status === 'unreadable') {
    return <span title="文本提取失败" className="text-[10px] ml-0.5">⚠</span>;
  }
  return <span title={status} className="text-[10px] ml-0.5">?</span>;
}


// ──────────────────────────────────────────────────────────────────
// Chip strip — compact, sits above composer
// ──────────────────────────────────────────────────────────────────

export interface ChatFileChipStripProps {
  scopeKind: LibScopeKind;
  scopeRef: string;
  /** How many chips to show inline before collapsing to "+ N more". */
  inlineMax?: number;
  /** Optional: gives the parent a way to drive the same drawer
   *  externally (e.g. from a header "📂 N 文件" button). */
  onOpenDrawer?: () => void;
  /** Provide an externally-managed `useChatFiles` result to avoid
   *  duplicate fetches when the drawer + strip live in the same
   *  parent. */
  controller?: UseChatFilesResult;
  tone?: 'base' | 'rw';
}

export function ChatFileChipStrip(props: ChatFileChipStripProps) {
  const internal = useChatFiles(props.scopeKind, props.scopeRef);
  const ctrl = props.controller ?? internal;
  const inlineMax = props.inlineMax ?? 3;
  const [drawerOpen, setDrawerOpen] = useState(false);
  const openDrawer = props.onOpenDrawer ?? (() => setDrawerOpen(true));

  if (ctrl.totalActive === 0) return null;

  const tone = props.tone ?? 'base';
  const chipBase = tone === 'rw'
    ? 'border-rw-border bg-rw-surface text-rw-t2'
    : 'border-border bg-surface-1 text-text-secondary';

  const visible = ctrl.files.slice(0, inlineMax);
  const more = Math.max(0, ctrl.totalActive - visible.length);

  return (
    <div className="flex flex-wrap items-center gap-1.5 text-[11px]">
      <span className={tone === 'rw' ? 'text-rw-t3' : 'text-text-tertiary'}>
        📂 {ctrl.totalActive} 参考文件:
      </span>
      {visible.map((f) => (
        <span
          key={f.fileId}
          className={`inline-flex items-center gap-1 rounded-sm border px-1.5 py-0.5 ${chipBase}`}
          title={`${f.name} (${f.mime}) — 引用为 [${f.fIdToken}]`}
        >
          <span className="font-mono text-[10px] opacity-70">{f.fIdToken}</span>
          <span className="max-w-[140px] truncate">{f.name}</span>
          <StatusBadge status={f.textExtractionStatus} />
          <button
            type="button"
            className="ml-1 opacity-60 hover:opacity-100"
            title="移除"
            onClick={() => ctrl.remove(f.fileId)}
          >✕</button>
        </span>
      ))}
      {more > 0 && (
        <button
          type="button"
          onClick={openDrawer}
          className={`underline ${tone === 'rw' ? 'text-rw-accent' : 'text-accent'}`}
        >
          + {more} more
        </button>
      )}
      <button
        type="button"
        onClick={openDrawer}
        className={`ml-auto underline ${tone === 'rw' ? 'text-rw-accent' : 'text-accent'}`}
      >
        全部 →
      </button>
      {drawerOpen && !props.onOpenDrawer && (
        <ChatFileLibDrawer
          scopeKind={props.scopeKind}
          scopeRef={props.scopeRef}
          controller={ctrl}
          onClose={() => setDrawerOpen(false)}
          tone={tone}
        />
      )}
    </div>
  );
}


// ──────────────────────────────────────────────────────────────────
// Drawer — full list modal
// ──────────────────────────────────────────────────────────────────

export interface ChatFileLibDrawerProps {
  scopeKind: LibScopeKind;
  scopeRef: string;
  controller?: UseChatFilesResult;
  onClose: () => void;
  tone?: 'base' | 'rw';
}

export function ChatFileLibDrawer(props: ChatFileLibDrawerProps) {
  const internal = useChatFiles(props.scopeKind, props.scopeRef);
  const ctrl = props.controller ?? internal;
  const [tab, setTab] = useState<'active' | 'removed'>('active');
  const tone = props.tone ?? 'base';

  const surfaceClass = tone === 'rw'
    ? 'bg-rw-bg-deep border border-rw-border text-rw-t1'
    : 'bg-surface-1 border border-border text-text-primary';
  const headerSubClass = tone === 'rw' ? 'text-rw-t3' : 'text-text-secondary';

  const rows = tab === 'active' ? ctrl.files : ctrl.removed;
  const scopeLabel = {
    patient:        '本患者',
    research:       '本研究',
    cross_research: '跨研究',
    assistant:      '助理',
  }[props.scopeKind];

  return (
    <div
      className="fixed inset-0 z-40 flex items-center justify-center bg-black/50"
      onClick={props.onClose}
    >
      <div
        className={`w-[640px] max-w-[92vw] max-h-[80vh] rounded-md shadow-xl flex flex-col ${surfaceClass}`}
        onClick={(e) => e.stopPropagation()}
      >
        {/* Header */}
        <div className="flex items-center justify-between px-4 py-3 border-b border-border">
          <div>
            <div className="text-base font-semibold">参考文件库</div>
            <div className={`text-[11px] ${headerSubClass}`}>
              作用域: {scopeLabel} · 引用时 LLM 用 [Fn] 标记
            </div>
          </div>
          <button
            onClick={props.onClose}
            className="text-xl leading-none opacity-60 hover:opacity-100"
          >×</button>
        </div>

        {/* Tabs */}
        <div className="flex items-center gap-2 px-4 py-2 border-b border-border text-[12px]">
          <button
            onClick={() => setTab('active')}
            className={`px-2 py-1 rounded-sm ${
              tab === 'active'
                ? (tone === 'rw' ? 'bg-rw-accent-bg text-rw-accent' : 'bg-accent-subtle text-accent')
                : (tone === 'rw' ? 'text-rw-t3' : 'text-text-secondary')
            }`}
          >当前 ({ctrl.totalActive})</button>
          <button
            onClick={() => setTab('removed')}
            className={`px-2 py-1 rounded-sm ${
              tab === 'removed'
                ? (tone === 'rw' ? 'bg-rw-accent-bg text-rw-accent' : 'bg-accent-subtle text-accent')
                : (tone === 'rw' ? 'text-rw-t3' : 'text-text-secondary')
            }`}
          >已移除 ({ctrl.totalRemoved})</button>
          {ctrl.loading && (
            <span className={`text-[10px] ${headerSubClass} ml-auto`}>加载中…</span>
          )}
        </div>

        {/* List */}
        <div className="flex-1 overflow-y-auto px-4 py-2">
          {rows.length === 0 ? (
            <div className={`py-12 text-center text-sm ${headerSubClass}`}>
              {tab === 'active'
                ? '本会话还没有参考文件 — 在聊天输入框拖拽 / 粘贴文件即可上传'
                : '没有已移除的文件'}
            </div>
          ) : (
            <ul className="space-y-1">
              {rows.map((f) => (
                <li
                  key={f.fileId}
                  className="flex items-center gap-2 py-1.5 px-2 rounded-sm hover:bg-surface-2 text-sm"
                >
                  <span className="font-mono text-[11px] opacity-60 w-8">
                    {f.fIdToken}
                  </span>
                  <StatusBadge status={f.textExtractionStatus} />
                  <span className="flex-1 truncate" title={f.name}>{f.name}</span>
                  <span className={`text-[10px] ${headerSubClass} font-mono`}>
                    {formatSize(f.sizeBytes)}
                  </span>
                  {tab === 'active' ? (
                    <>
                      {f.textExtractionStatus === 'unreadable' && (
                        <button
                          onClick={() => ctrl.reextract(f.fileId)}
                          className={`text-[11px] underline ${tone === 'rw' ? 'text-rw-accent' : 'text-accent'}`}
                          title="重新尝试提取(配置好 Gemini key 之后再试一次)"
                        >重试</button>
                      )}
                      <button
                        onClick={() => ctrl.remove(f.fileId)}
                        className="text-[11px] text-retract hover:opacity-80"
                      >移除</button>
                    </>
                  ) : (
                    <button
                      onClick={() => ctrl.restore(f.fileId)}
                      className={`text-[11px] underline ${tone === 'rw' ? 'text-rw-accent' : 'text-accent'}`}
                    >恢复</button>
                  )}
                </li>
              ))}
            </ul>
          )}
        </div>

        {/* Footer */}
        <div className={`px-4 py-2 border-t border-border text-[11px] ${headerSubClass}`}>
          {tab === 'active' ? (
            <>每轮聊天会自动把全部当前文件注入 LLM 上下文,AI 引用时用 [Fn]。</>
          ) : (
            <>已移除文件在 7 天内可点"恢复";超过 7 天将由后台清理。</>
          )}
        </div>
      </div>
    </div>
  );
}


function formatSize(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  if (n < 1024 * 1024 * 1024) return `${(n / 1024 / 1024).toFixed(1)} MB`;
  return `${(n / 1024 / 1024 / 1024).toFixed(2)} GB`;
}
