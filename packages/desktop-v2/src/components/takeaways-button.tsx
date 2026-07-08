/**
 * TakeawaysButton — "Nexus 学到 N 条" entry for every chat surface.
 *
 * Pops a right-anchored drawer listing the medic's session takeaways
 * (LLM-distilled qualitative insights from chat). Each row has:
 *   - tag chip (clinical_reasoning / preference / tool_use /
 *     decision_rationale / disagreement) colour-coded
 *   - the 1-3 sentence insight text
 *   - relative time + scope ref
 *   - dismiss (✕) button — calls /memory/takeaways/{id}/reject so
 *     this insight stops being injected into future system prompts.
 *
 * Privacy: per-user only. The list call is scoped to the current
 * medic's auth token; the server filters on user_id.
 *
 * Used by:
 *   - PatientMode 问诊 (EncounterMode) — scope: patient
 *   - Research per-study ChatTab — scope: research/{study_id}
 *   - CrossResearchChat — scope: cross_research
 *   - CrossPatientChat — scope: patient (current focused patient)
 *
 * The default props show ALL scopes for the user (no filter), which
 * matches the "show me what Nexus has learned about me overall"
 * read of the entry point. Callers can pass scopeKind/scopeRef to
 * narrow.
 */
import { useEffect, useState } from "react";
import { cn } from "../lib/util";
import { api } from "../lib/api-client";

type Takeaway = Awaited<ReturnType<typeof api.listTakeaways>>[number];

interface Props {
  /** Filter by scope kind. Omit to show everything (cross-scope view). */
  scopeKind?: "patient" | "research" | "cross_research" | "other";
  /** Filter by scope ref. Required when scopeKind is patient/research. */
  scopeRef?: string;
  /** Tone — match the surrounding chat palette. */
  tone?: "rw" | "base";
}

const TAG_COLOR: Record<string, string> = {
  clinical_reasoning: "bg-accent/15 text-accent border-accent/30",
  preference:         "bg-confirmed/15 text-confirmed border-confirmed/30",
  tool_use:           "bg-rw-accent/15 text-rw-accent border-rw-accent/30",
  decision_rationale: "bg-caution/15 text-caution border-caution/30",
  disagreement:       "bg-retract/15 text-retract border-retract/30",
};

const TAG_LABEL: Record<string, string> = {
  clinical_reasoning: "临床思路",
  preference:         "偏好",
  tool_use:           "工具用法",
  decision_rationale: "决策依据",
  disagreement:       "异议",
};

function formatRelative(unixSec: number): string {
  const ms = Date.now() - unixSec * 1000;
  const min = Math.floor(ms / 60_000);
  if (min < 1) return "刚刚";
  if (min < 60) return `${min} 分钟前`;
  const h = Math.floor(min / 60);
  if (h < 24) return `${h} 小时前`;
  const d = Math.floor(h / 24);
  if (d < 30) return `${d} 天前`;
  const mo = Math.floor(d / 30);
  return `${mo} 个月前`;
}

export function TakeawaysButton({ scopeKind, scopeRef, tone = "rw" }: Props) {
  const [open, setOpen] = useState(false);
  const [items, setItems] = useState<Takeaway[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [count, setCount] = useState<number | null>(null);

  // Lazy-load count on mount (small payload, cheap) so the button
  // shows "Nexus 学到 N 条" even before the drawer opens. We re-fetch
  // when the drawer opens to get the freshest list.
  useEffect(() => {
    let cancelled = false;
    api.listTakeaways({ scopeKind, scopeRef, limit: 50 }).then(
      (r) => { if (!cancelled) setCount(r.length); },
      () => { if (!cancelled) setCount(0); },
    );
    return () => { cancelled = true; };
  }, [scopeKind, scopeRef]);

  // When opening: pull the full list. F-loading-timeouts — bump a
  // ``retryNonce`` to force re-run when the medic clicks "重试".
  // Without retry, the timeout error would persist until the drawer
  // is closed + reopened, exactly the friction we're fixing.
  const [retryNonce, setRetryNonce] = useState(0);
  useEffect(() => {
    if (!open) return;
    let cancelled = false;
    setError(null);
    setItems(null);   // ← reset so "加载中…" shows again on retry
    api.listTakeaways({ scopeKind, scopeRef, limit: 50 }).then(
      (r) => { if (!cancelled) { setItems(r); setCount(r.length); } },
      (e) => { if (!cancelled) setError(String(e?.message || e)); },
    );
    return () => { cancelled = true; };
  }, [open, scopeKind, scopeRef, retryNonce]);

  async function handleReject(id: number) {
    // Optimistic remove + best-effort POST.
    setItems((prev) => prev ? prev.filter((t) => t.id !== id) : prev);
    setCount((c) => (c === null ? c : Math.max(0, c - 1)));
    try {
      await api.rejectTakeaway(id);
    } catch {
      // On failure, just leave the optimistic state — reopening
      // re-pulls fresh. Don't surface a toast for an idempotent
      // dismiss.
    }
  }

  const buttonCls = tone === "rw"
    ? "text-rw-t2 hover:text-rw-t1 hover:bg-rw-surface-2"
    : "text-text-secondary hover:text-text-primary hover:bg-surface-1";
  const panelCls = tone === "rw"
    ? "bg-rw-surface border-rw-border text-rw-t1"
    : "bg-surface border-border text-text-primary";

  return (
    <>
      <button
        type="button"
        onClick={() => setOpen(true)}
        className={cn(
          "inline-flex items-center gap-1.5 rounded px-2 py-1 text-caption transition-colors",
          buttonCls,
        )}
        title="Nexus 从此前的对话中学到的医生思路、偏好、工具用法"
      >
        <span aria-hidden>🧠</span>
        <span>Nexus 学到 {count ?? "…"} 条</span>
      </button>

      {open && (
        <div
          className="fixed inset-0 z-50 flex items-stretch justify-end bg-black/40"
          onClick={() => setOpen(false)}
        >
          <div
            className={cn(
              "h-full w-[420px] max-w-[90vw] overflow-y-auto border-l shadow-xl",
              panelCls,
            )}
            onClick={(e) => e.stopPropagation()}
          >
            <div className="sticky top-0 flex items-center justify-between border-b border-inherit bg-inherit px-4 py-3">
              <div>
                <div className="text-section font-medium">Nexus 学到的</div>
                <div className="text-caption opacity-60">
                  {scopeKind
                    ? `仅显示 ${scopeKind} / ${scopeRef?.slice(0, 12) ?? "—"}`
                    : "包含所有聊天范围"}
                </div>
              </div>
              <button
                type="button"
                onClick={() => setOpen(false)}
                className="rounded px-2 py-1 text-caption hover:bg-black/20"
              >
                ✕
              </button>
            </div>

            <div className="px-4 py-3">
              {error && (
                <div className="rounded border border-retract/30 bg-retract/10 px-3 py-2 text-caption text-retract space-y-2">
                  <div>无法加载：{error}</div>
                  <button
                    type="button"
                    onClick={() => setRetryNonce((n) => n + 1)}
                    className="rounded border border-retract/40 px-2 py-0.5
                               text-[11px] hover:bg-retract/20"
                  >
                    重试
                  </button>
                </div>
              )}
              {!error && items === null && (
                <div className="text-caption opacity-60">加载中…</div>
              )}
              {!error && items && items.length === 0 && (
                <div className="text-caption opacity-60">
                  暂无心得。Nexus 会在每 2-3 轮聊天后蒸馏一次,持续观察你的思路。
                </div>
              )}
              {!error && items && items.length > 0 && (
                <ul className="space-y-3">
                  {items.map((t) => (
                    <li
                      key={t.id}
                      className="rounded border border-inherit bg-black/10 p-3"
                    >
                      <div className="mb-1.5 flex items-center justify-between gap-2">
                        {t.tag && (
                          <span
                            className={cn(
                              "rounded-sm border px-1.5 py-0.5 text-[10px]",
                              TAG_COLOR[t.tag] ??
                                "bg-surface-1 text-text-tertiary border-border",
                            )}
                          >
                            {TAG_LABEL[t.tag] ?? t.tag}
                          </span>
                        )}
                        <button
                          type="button"
                          onClick={() => handleReject(t.id)}
                          className="text-[11px] opacity-60 hover:opacity-100 hover:text-retract"
                          title="从未来的 AI 提示中移除这条心得"
                        >
                          移除 ✕
                        </button>
                      </div>
                      <div className="text-caption leading-relaxed">
                        {t.text}
                      </div>
                      <div className="mt-1.5 text-[10px] opacity-50">
                        {formatRelative(t.distilledAt)} · {t.scopeKind}
                        {t.scopeRef && ` · ${t.scopeRef.slice(0, 12)}`}
                      </div>
                    </li>
                  ))}
                </ul>
              )}
            </div>
          </div>
        </div>
      )}
    </>
  );
}
