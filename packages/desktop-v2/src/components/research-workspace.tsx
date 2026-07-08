/**
 * Research Workspace UI — dark theme · 中文 · 全 7 tab
 *
 * Visual source of truth: docs/design/visual-mock/Research Workspace.dc.html
 * Design tokens live in src/index.css (`--rw-*`) + tailwind.config.ts (`rw.*`).
 *
 * Decisions baked in:
 *   D1   顶部 toggle (实现在 App.tsx WorkspaceSwitcher)
 *   D2   Research Chat 写入需要显式 scope (focus chip + 写入归属横幅)
 *   D3   LLM 给建议医生决策 (CandidateCard 显示置信度 + 待医生确认)
 *   D5   多臂研究 arm 列在 enrollment row
 *   D7   批量协议确认 (此页面是入口；批量编辑表是单独路由)
 *   D13  邮件提醒 + .ics (Invite 弹框含同意勾选)
 *   D14  研究优先 (默认 active, 患者描述为 ad-hoc)
 *   D16/17 scope_tags (Chat 显示 episodes/skills 引用 chip)
 *   D18  患者研究归属派生 (Drill-in 视图显示研究 chip)
 *   D19-D22  engine_config (Settings 弹框暴露这些旋钮)
 */
import { useEffect, useMemo, useState } from 'react';
import { api } from '../lib/api-client';
import { useAppState } from '../store';
import type { StudySummary } from '../lib/util';
import { ChatMarkdown, type FileChipRef } from './chat-markdown';
import { ChatFileChipStrip, useChatFiles } from './chat-file-lib';
import { StreamingFooter, StreamingCursor } from './thinking-indicator';
import { TakeawaysButton } from './takeaways-button';

type Tab = 'overview' | 'eligibility' | 'roster' | 'safety'
         | 'schedule' | 'chat' | 'reports';

const TAB_LABELS: Record<Tab, string> = {
  overview:    '概览',
  eligibility: '入排清单',
  roster:      '入组名单',
  safety:      '安全性',
  schedule:    '进度计划',
  chat:        '研究对话',
  reports:     '报告导出',
};


// ════════════════════════════════════════════════════════════════════
//  Root
// ════════════════════════════════════════════════════════════════════

export function ResearchWorkspace() {
  const studies          = useAppState((s) => s.studies);
  const refreshStudies   = useAppState((s) => s.refreshStudies);
  const activeStudyId    = useAppState((s) => s.activeStudyId);
  const setActiveStudyId = useAppState((s) => s.setActiveStudyId);
  const [newOpen, setNewOpen] = useState(false);
  // Pending delete is held at the workspace level so we can render the
  // confirmation dialog over the entire pane (not just the sidebar row).
  const [pendingDelete, setPendingDelete] = useState<StudySummary | null>(null);
  const [deleting, setDeleting]           = useState(false);

  useEffect(() => { refreshStudies(); }, [refreshStudies]);

  async function confirmDelete() {
    if (!pendingDelete) return;
    setDeleting(true);
    try {
      await api.archiveResearchStudy(pendingDelete.studyId);
      if (activeStudyId === pendingDelete.studyId) setActiveStudyId(null);
      await refreshStudies();
      setPendingDelete(null);
    } catch (e) {
      // Surface the failure in the dialog; don't close it.
      console.warn('archiveResearchStudy failed', e);
      alert(`删除失败：${(e as Error).message || String(e)}`);
    } finally {
      setDeleting(false);
    }
  }

  return (
    <div className="rw-root flex h-full w-full font-rw-display">
      <StudiesSidebar
        studies={studies}
        activeStudyId={activeStudyId}
        onSelect={setActiveStudyId}
        onNew={() => setNewOpen(true)}
        onDelete={setPendingDelete}
      />
      <main className="flex-1 min-w-0 overflow-hidden bg-rw-bg">
        {activeStudyId
          ? <StudyDetail key={activeStudyId} studyId={activeStudyId} />
          : <EmptyState />}
      </main>
      {newOpen && (
        <NewStudyDialog
          onCancel={() => setNewOpen(false)}
          onCreated={async (sid) => {
            setNewOpen(false);
            await refreshStudies();
            setActiveStudyId(sid);
          }}
        />
      )}
      {pendingDelete && (
        <DeleteStudyDialog
          study={pendingDelete}
          busy={deleting}
          onCancel={() => setPendingDelete(null)}
          onConfirm={confirmDelete}
        />
      )}
    </div>
  );
}

// Soft-archive confirmation. By design (RESEARCH_WORKSPACE_DESIGN §0.1
// anti-pattern: "不删数据") this is always an archive on the backend,
// not a hard delete — withdrawn enrollments, screening rows, and
// timeline events all stay. The dialog spells that out so the medic
// isn't surprised that a later "view archived" UI may show this row.
function DeleteStudyDialog(props: {
  study: StudySummary;
  busy: boolean;
  onCancel: () => void;
  onConfirm: () => void;
}) {
  const s = props.study;
  const hasEnrolled = s.enrolledCount > 0;
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60"
         onClick={props.onCancel}>
      <div className="w-[440px] rounded-lg border border-rw-border bg-rw-surface
                      p-5 shadow-2xl font-rw-display"
           onClick={(e) => e.stopPropagation()}>
        <h2 className="text-rw-t1 text-base font-semibold">归档研究</h2>
        <p className="mt-2 text-sm text-rw-t2 leading-relaxed">
          确认归档 <span className="text-rw-t1 font-medium">{s.displayName}</span>?
        </p>
        <ul className="mt-3 text-[12px] text-rw-t3 space-y-1 list-disc pl-4">
          <li>研究从侧栏隐藏,但所有事件、入组、筛查记录都保留(GCP 合规)</li>
          {hasEnrolled && (
            <li className="text-rw-orange">
              已有 {s.enrolledCount} 例入组 — 这些患者的 timeline 不受影响
            </li>
          )}
          <li>需要时可以联系工程恢复</li>
        </ul>
        <div className="mt-5 flex justify-end gap-2">
          <button onClick={props.onCancel}
                  disabled={props.busy}
                  className="px-3 py-1.5 rounded-md text-sm text-rw-t2
                             border border-rw-border hover:bg-rw-surface-2
                             disabled:opacity-50">
            取消
          </button>
          <button onClick={props.onConfirm}
                  disabled={props.busy}
                  className="px-3 py-1.5 rounded-md text-sm font-medium
                             bg-rw-red-bg border border-rw-red text-rw-red
                             hover:bg-rw-red hover:text-white
                             disabled:opacity-50">
            {props.busy ? '归档中…' : '归档'}
          </button>
        </div>
      </div>
    </div>
  );
}


// ════════════════════════════════════════════════════════════════════
//  Sidebar
// ════════════════════════════════════════════════════════════════════

function StudiesSidebar(props: {
  studies: StudySummary[];
  activeStudyId: string | null;
  onSelect: (id: string | null) => void;
  onNew: () => void;
  onDelete: (s: StudySummary) => void;
}) {
  const inboxCount = props.studies.reduce((n, s) => n + s.candidateCount, 0);
  return (
    <aside className="w-[252px] shrink-0 border-r border-rw-border bg-rw-bg-deep flex flex-col">
      <SidebarSection title="RESEARCH">
        <button
          onClick={props.onNew}
          className="w-full mx-3 mt-1 mb-3 px-3 py-2 rounded-md bg-rw-accent-bg
                     border border-rw-accent-bd text-rw-accent text-sm font-medium
                     hover:bg-rw-accent/10 transition flex items-center justify-center gap-1"
          style={{width: 'calc(100% - 24px)'}}
        >
          <span className="text-base leading-none">+</span> 新建研究
        </button>
      </SidebarSection>

      <SidebarSection title="MY STUDIES">
        <div className="px-1 flex flex-col gap-0.5">
          {props.studies.length === 0 && (
            <div className="px-3 py-4 text-xs text-rw-t4 italic">
              暂无研究 — 点上方 “新建研究” 或安装 starter
            </div>
          )}
          {props.studies.map((s) => (
            <StudySidebarRow key={s.studyId} study={s}
              active={props.activeStudyId === s.studyId}
              onClick={() => props.onSelect(s.studyId)}
              onDelete={() => props.onDelete(s)}
            />
          ))}
        </div>
      </SidebarSection>

      <div className="mt-auto" />

      <SidebarSection title="CROSS-STUDY">
        {/* Persistent entry to the workspace-level cross-research chat.
            Once a medic clicks into any study, EmptyState (which hosts
            the cross-research chat) is unmounted. This button puts the
            chat back one click away — set activeStudyId=null and the
            main pane re-renders EmptyState + CrossResearchChat. */}
        <button
          onClick={() => props.onSelect(null)}
          className={`w-full mx-1 px-3 py-2 rounded-md text-sm
                      flex items-center gap-2 transition border
                      ${props.activeStudyId === null
                        ? 'bg-rw-accent-bg text-rw-accent border-rw-accent-bd'
                        : 'border-transparent text-rw-t2 hover:bg-rw-surface'}`}
          style={{width:'calc(100% - 8px)'}}
        >
          <span className={props.activeStudyId === null ? 'text-rw-accent' : 'text-rw-t3'}>
            💬
          </span> 跨研究对话
        </button>
        <button className="w-full mx-1 px-3 py-2 rounded-md text-sm
                          hover:bg-rw-surface flex items-center justify-between
                          text-rw-t2 border border-transparent"
                style={{width:'calc(100% - 8px)'}}>
          <span className="flex items-center gap-2">
            <span className="text-rw-t3">⌬</span> 入排候选 Inbox
          </span>
          {inboxCount > 0 && (
            <span className="px-1.5 py-0.5 rounded-full bg-rw-red text-white text-[10px] font-mono">
              {inboxCount}
            </span>
          )}
        </button>
        <button className="w-full mx-1 px-3 py-2 rounded-md text-sm
                          hover:bg-rw-surface flex items-center gap-2 text-rw-t2
                          border border-transparent"
                style={{width:'calc(100% - 8px)'}}>
          <span className="text-rw-t3">○</span> 未分组患者
        </button>
      </SidebarSection>

      <div className="px-4 py-3 text-[10px] text-rw-t4 border-t border-rw-border-soft font-rw-mono">
        accumulate · not reset
      </div>
    </aside>
  );
}

function SidebarSection({title, children}: {title: string; children: React.ReactNode}) {
  return (
    <div>
      <div className="px-4 pt-3 pb-1 text-[10px] tracking-[0.18em] text-rw-t4 font-rw-mono">
        {title}
      </div>
      {children}
    </div>
  );
}

function StudySidebarRow(props: {
  study: StudySummary;
  active: boolean;
  onClick: () => void;
  onDelete: () => void;
}) {
  const s = props.study;
  const pct = s.targetN ? Math.min(100, Math.round(s.enrolledCount/s.targetN*100)) : 0;
  // The card itself is the click target; the trash icon is a nested
  // button that needs ``stopPropagation`` so it doesn't also select
  // the row. Nesting buttons isn't valid HTML — so the outer element
  // is a `div role="button"` instead.
  return (
    <div
      role="button"
      tabIndex={0}
      onClick={props.onClick}
      onKeyDown={(e) => {
        if (e.key === 'Enter' || e.key === ' ') props.onClick();
      }}
      className={`group relative text-left px-3 py-2 mx-1 rounded-md transition cursor-pointer
        ${props.active
          ? 'bg-rw-accent-bg border border-rw-accent-bd'
          : 'hover:bg-rw-surface border border-transparent'}`}
      style={{width: 'calc(100% - 8px)'}}
    >
      <div className={`text-sm font-medium pr-6 ${props.active ? 'text-rw-accent' : 'text-rw-t1'}`}>
        {s.displayName}
      </div>
      <div className="mt-1 flex items-center justify-between">
        <span className="text-[10px] font-rw-mono text-rw-t4">
          {s.enrolledCount}{s.targetN ? `/${s.targetN}` : ''}
        </span>
        {s.candidateCount > 0 && (
          <span className="text-[10px] font-rw-mono text-rw-orange">
            ⓘ {s.candidateCount}
          </span>
        )}
      </div>
      {s.targetN && (
        <div className="mt-1 h-1 w-full rounded-full bg-rw-surface-3 overflow-hidden">
          <div className="h-1 bg-rw-accent rounded-full" style={{width: `${pct}%`}} />
        </div>
      )}
      <button
        aria-label={`归档 ${s.displayName}`}
        onClick={(e) => { e.stopPropagation(); props.onDelete(); }}
        title="归档研究"
        className="absolute top-1.5 right-1.5 opacity-0 group-hover:opacity-100
                   focus:opacity-100 transition rounded p-0.5
                   text-rw-t4 hover:text-rw-red hover:bg-rw-surface-2"
      >
        {/* Inline trash icon — keeps the dependency footprint flat. */}
        <svg width="13" height="13" viewBox="0 0 16 16" fill="none"
             xmlns="http://www.w3.org/2000/svg">
          <path d="M2.5 4h11M6 4V2.5h4V4M4 4l.6 9.5a1 1 0 0 0 1 1h4.8a1 1 0 0 0 1-1L12 4M6.5 7v5M9.5 7v5"
                stroke="currentColor" strokeWidth="1.2" strokeLinecap="round"
                strokeLinejoin="round"/>
        </svg>
      </button>
    </div>
  );
}


// ════════════════════════════════════════════════════════════════════
//  Study Detail (right pane)
// ════════════════════════════════════════════════════════════════════

interface StudyData {
  study_id: string;
  display_name: string;
  short_code: string;
  phase: string;
  status: string;
  target_n: number | null;
  enrolled_count: number;
  candidate_count: number;
  primary_endpoint?: string | null;
  inclusion: Array<unknown>;
  exclusion: Array<unknown>;
  schedule: Array<unknown>;
}

function StudyDetail({studyId}: {studyId: string}) {
  const [tab, setTab] = useState<Tab>('overview');
  const [study, setStudy] = useState<StudyData | null>(null);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    setLoading(true);
    api.getResearchStudy(studyId)
       .then((s) => setStudy(s as never))
       .catch((e) => console.warn('getStudy', e))
       .finally(() => setLoading(false));
  }, [studyId]);

  if (loading && !study) {
    return <div className="p-8 text-rw-t3">载入研究中…</div>;
  }
  if (!study) {
    return <div className="p-8 text-rw-red">未找到研究</div>;
  }

  return (
    <div className="h-full flex flex-col">
      <StudyHeader study={study} tab={tab} setTab={setTab} />
      <section className="flex-1 overflow-y-auto">
        {tab === 'overview'    && <OverviewTab study={study} />}
        {tab === 'eligibility' && <EligibilityTab studyId={studyId} study={study} />}
        {tab === 'roster'      && <RosterTab     studyId={studyId} />}
        {tab === 'safety'      && <SafetyTab     studyId={studyId} />}
        {tab === 'schedule'    && <ScheduleTab   studyId={studyId} />}
        {tab === 'chat'        && <ChatTab       studyId={studyId} study={study} />}
        {tab === 'reports'     && <ReportsTab    studyId={studyId} />}
      </section>
    </div>
  );
}

function StudyHeader(props: {study: StudyData; tab: Tab; setTab: (t: Tab) => void}) {
  const s = props.study;
  return (
    <header className="px-6 pt-5 pb-3 border-b border-rw-border bg-rw-bg">
      <div className="flex items-baseline gap-3 flex-wrap">
        <h1 className="text-[21px] font-semibold tracking-tight text-rw-t1">
          {s.display_name}
        </h1>
        <span className="px-2 py-0.5 rounded text-[10px] font-rw-mono uppercase tracking-wider
                         bg-rw-surface-2 text-rw-t3">
          Phase {s.phase || '—'}
        </span>
        <span className="px-2 py-0.5 rounded text-[10px] font-rw-mono uppercase tracking-wider
                         bg-rw-green-bg text-rw-green">
          {s.status}
        </span>
        <div className="flex-1" />
        <button className="px-3 py-1.5 rounded-md bg-rw-surface border border-rw-border
                          text-xs text-rw-t2 hover:border-rw-accent-bd transition">
          ↧ 导出
        </button>
        <button className="px-2 py-1.5 rounded-md bg-rw-surface border border-rw-border
                          text-xs text-rw-t2 hover:border-rw-accent-bd transition">
          ⚙
        </button>
      </div>
      <div className="mt-3 flex flex-wrap gap-1">
        {(Object.keys(TAB_LABELS) as Tab[]).map((t) => (
          <button key={t} onClick={() => props.setTab(t)}
            className={`px-3 py-1.5 rounded-md text-[13px] transition
              ${props.tab === t
                ? 'bg-rw-accent-bg text-rw-accent border border-rw-accent-bd'
                : 'text-rw-t2 hover:bg-rw-surface border border-transparent'}`}>
            {TAB_LABELS[t]}
          </button>
        ))}
      </div>
    </header>
  );
}


// ════════════════════════════════════════════════════════════════════
//  Tab: 概览
// ════════════════════════════════════════════════════════════════════

function OverviewTab({study}: {study: StudyData}) {
  const [overview, setOverview] = useState<{
    enrolled_count: number; target_n: number | null;
    candidate_count: number; attention_count: number;
    median_followup_months: number; status: string;
    primary_endpoint: string | null;
  } | null>(null);
  const [activity, setActivity] = useState<Array<{
    when_ms: number; kind: string; text: string; patient_hash: string;
  }>>([]);

  useEffect(() => {
    api.getResearchStudyOverview(study.study_id)
       .then((o) => setOverview(o)).catch(console.warn);
    api.getResearchRecentActivity(study.study_id, 7, 12)
       .then((a) => setActivity(a)).catch(console.warn);
  }, [study.study_id]);

  const enrolled = overview?.enrolled_count ?? study.enrolled_count;
  const targetN  = overview?.target_n      ?? study.target_n;
  const cand     = overview?.candidate_count ?? study.candidate_count;
  const attn     = overview?.attention_count ?? Math.min(study.candidate_count, 9);
  const followup = overview?.median_followup_months ?? 0;
  const pct      = targetN ? Math.min(100, Math.round((enrolled/targetN)*100)) : 0;

  const fmtWhen = (ms: number) => {
    const d = new Date(ms);
    return `${d.getMonth()+1}-${String(d.getDate()).padStart(2,'0')}`;
  };

  return (
    <div className="px-6 py-5 space-y-5">
      <div className="grid grid-cols-4 gap-3">
        <KPICard label="入组进度"
                 value={targetN ? `${enrolled}/${targetN}` : `${enrolled}`}
                 sub={overview?.status || study.status} tone="accent"/>
        <KPICard label="候选总数" value={cand}
                 sub="eligibility_inbox" tone="default"/>
        <KPICard label="待医生" value={attn}
                 sub="未决候选" tone="orange"/>
        <KPICard label="中位随访"
                 value={followup > 0 ? followup.toFixed(1) : '—'}
                 suffix={followup > 0 ? '月' : undefined}
                 sub={enrolled > 0 ? `n=${enrolled}` : '尚无入组'} tone="default"/>
      </div>

      <Card title="入组进度" right={`${pct}%`}>
        <div className="h-2 w-full rounded-full bg-rw-surface-3 overflow-hidden">
          <div className="h-2 bg-rw-accent rounded-full" style={{width:`${pct}%`}} />
        </div>
        <div className="mt-3 text-xs text-rw-t3">
          主要终点：<span className="text-rw-t1">
            {overview?.primary_endpoint || study.primary_endpoint || '尚未设定'}
          </span>
        </div>
      </Card>

      <Card title="近 7 天动态">
        {activity.length === 0 ? (
          <div className="text-sm text-rw-t3 italic">最近 7 天暂无活动 — 入组、写病程、上传影像后会自动产生。</div>
        ) : (
          <ActivityFeed items={activity.map(a => ({
            when: fmtWhen(a.when_ms), text: a.text, kind: a.kind,
          }))} />
        )}
      </Card>
    </div>
  );
}


// ════════════════════════════════════════════════════════════════════
//  Tab: 入排清单
// ════════════════════════════════════════════════════════════════════

interface ScreeningRow {
  patient_hash: string;
  overall_status: string;
  decision: string;
  per_criterion: Record<string, {kind: string; verdict: string;
                                 confidence?: number; reasoning?: string;
                                 evidence_refs?: string[]}>;
  llm_recommendation?: {narrative?: string; overall_confidence?: number;
                        suggested_next_steps?: string[]} | null;
}

function EligibilityTab({studyId}: {studyId: string; study: StudyData}) {
  const [rows, setRows] = useState<ScreeningRow[]>([]);
  const [busy, setBusy] = useState(false);
  const [invite, setInvite] = useState<ScreeningRow | null>(null);

  const refresh = async () => {
    try {
      const r = await api.listCandidates(studyId, 'pending');
      setRows(r as never);
    } catch (e) { console.warn(e); }
  };
  useEffect(() => { refresh(); }, [studyId]);

  const rescan = async () => {
    setBusy(true);
    try { await api.rescanEligibility(studyId); await refresh(); }
    finally { setBusy(false); }
  };

  const skip = async (r: ScreeningRow) => {
    await api.decideScreening(studyId, r.patient_hash,
      {decision: 'excluded', reason: 'manual skip'});
    await refresh();
  };
  const snooze = async (r: ScreeningRow) => {
    await api.decideScreening(studyId, r.patient_hash,
      {decision: 'snoozed', snoozeUntil: Date.now() + 7*86400_000});
    await refresh();
  };

  return (
    <div className="px-6 py-5 space-y-4">
      <div className="flex items-center justify-between">
        <h2 className="text-base font-medium text-rw-t1">
          候选患者（<span className="font-rw-mono">{rows.length}</span>）
        </h2>
        <div className="flex items-center gap-2">
          <label className="text-xs text-rw-t3 flex items-center gap-2">
            <span className="w-8 h-4 rounded-full bg-rw-accent relative">
              <span className="absolute right-0.5 top-0.5 w-3 h-3 rounded-full bg-white"/>
            </span>
            自动扫描已开启
          </label>
          <button onClick={rescan} disabled={busy}
            className="px-3 py-1.5 rounded-md bg-rw-surface border border-rw-border
                       text-xs text-rw-t2 hover:border-rw-accent-bd">
            {busy ? '重新扫描中…' : '重新扫描'}
          </button>
        </div>
      </div>

      {rows.length === 0 && (
        <div className="text-sm text-rw-t3 italic py-6 text-center
                        border border-dashed border-rw-border rounded-lg">
          暂无待筛候选 — 触发 “重新扫描” 让 eligibility 引擎对所有患者重评估
        </div>
      )}

      {rows.map((r) => (
        <CandidateCard key={r.patient_hash} row={r}
          onInvite={() => setInvite(r)}
          onSkip={() => skip(r)}
          onSnooze={() => snooze(r)}
        />
      ))}

      {invite && <InviteModal row={invite} studyId={studyId}
                              onClose={() => setInvite(null)}
                              onConfirmed={async () => { setInvite(null); await refresh(); }} />}
    </div>
  );
}

function CandidateCard(props: {
  row: ScreeningRow;
  onInvite: () => void;
  onSkip: () => void;
  onSnooze: () => void;
}) {
  const {row} = props;
  const crits = useMemo(() => Object.entries(row.per_criterion), [row]);
  return (
    <article className="rounded-lg bg-rw-surface border border-rw-border p-4">
      <div className="flex items-start justify-between gap-3">
        <div>
          <div className="flex items-center gap-2">
            <span className="font-medium text-rw-t1">
              患者 #{row.patient_hash.slice(0,6)}
            </span>
            <span className="px-1.5 py-0.5 rounded text-[10px] font-rw-mono
                             bg-rw-accent-bg text-rw-accent uppercase">
              {row.overall_status}
            </span>
          </div>
        </div>
        <div className="flex gap-1.5">
          <button onClick={props.onInvite}
            className="px-3 py-1.5 rounded-md bg-rw-accent text-[#06252c]
                       text-xs font-medium hover:bg-rw-accent-2">
            邀请入组
          </button>
          <button onClick={props.onSkip}
            className="px-3 py-1.5 rounded-md bg-rw-surface-2 border border-rw-border
                       text-xs text-rw-t2 hover:border-rw-red">
            跳过
          </button>
          <button onClick={props.onSnooze}
            className="px-3 py-1.5 rounded-md bg-rw-surface-2 border border-rw-border
                       text-xs text-rw-t2 hover:border-rw-orange">
            稍后提醒
          </button>
        </div>
      </div>

      <div className="mt-3 grid grid-cols-1 gap-1">
        {crits.map(([cid, v]) => (
          <div key={cid} className="grid grid-cols-[2fr_1fr_60px] gap-3 items-baseline text-[12px]">
            <span className="text-rw-t2 truncate" title={cid}>{cid}</span>
            <span className="text-rw-t3 font-rw-mono text-[11px]">{v.kind}</span>
            <span className={`text-right font-rw-mono ${
              v.verdict === 'pass' ? 'text-rw-green' :
              v.verdict === 'fail' ? 'text-rw-red' :
              v.kind === 'manual' ? 'text-rw-orange' : 'text-rw-t4'
            }`}>
              {v.verdict === 'pass' ? '✓' :
               v.verdict === 'fail' ? '✗' :
               v.kind === 'manual' ? '⚠' : '?'}
              {v.confidence != null && (
                <span className="ml-1 text-rw-t4 text-[10px]">{v.confidence.toFixed(2)}</span>
              )}
            </span>
            {v.reasoning && (
              <div className="col-span-3 pl-3 text-[11px] text-rw-t3 italic mb-1">
                {v.reasoning}
              </div>
            )}
          </div>
        ))}
      </div>

      {row.llm_recommendation?.narrative && (
        <div className="mt-3 pt-3 border-t border-rw-border-soft">
          <div className="flex items-start gap-2">
            <span className="text-rw-accent">🤖</span>
            <div className="flex-1 text-[12px] text-rw-t2 leading-relaxed">
              <span className="font-medium text-rw-t1">LLM 综合建议：</span>
              {row.llm_recommendation.narrative}
              {typeof row.llm_recommendation.overall_confidence === 'number' && (
                <span className="ml-2 text-rw-t3 font-rw-mono">
                  (置信度 {row.llm_recommendation.overall_confidence.toFixed(2)})
                </span>
              )}
            </div>
          </div>
        </div>
      )}
    </article>
  );
}


// ════════════════════════════════════════════════════════════════════
//  Tab: 入组名单
// ════════════════════════════════════════════════════════════════════

function RosterTab({studyId}: {studyId: string}) {
  const [rows, setRows] = useState<Array<{
    patient_hash: string; enrollment_seq: number; status: string;
    arm: string | null; enrolled_at: number;
  }>>([]);
  useEffect(() => {
    api.getRoster(studyId).then((r) => setRows(r as never)).catch(console.warn);
  }, [studyId]);

  return (
    <div className="px-6 py-5 space-y-3">
      <div className="flex items-center justify-between">
        <h2 className="text-base font-medium text-rw-t1">
          入组名单（<span className="font-rw-mono">{rows.length}</span>）
        </h2>
        <button className="px-3 py-1.5 rounded-md bg-rw-surface border border-rw-border
                          text-xs text-rw-t2 hover:border-rw-accent-bd">
          + 手动添加
        </button>
      </div>
      <div className="rounded-lg border border-rw-border overflow-hidden">
        <table className="w-full text-sm">
          <thead className="bg-rw-surface-2 text-[11px] uppercase tracking-wider text-rw-t3">
            <tr>
              <th className="text-left px-4 py-2 font-rw-mono">#</th>
              <th className="text-left px-4 py-2">患者</th>
              <th className="text-left px-4 py-2">Arm</th>
              <th className="text-left px-4 py-2">入组日期</th>
              <th className="text-left px-4 py-2">治疗阶段</th>
              <th className="text-left px-4 py-2">末次随访</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r, i) => (
              <tr key={r.patient_hash}
                  className={`border-t border-rw-border-soft hover:bg-rw-surface-2 ${i % 2 ? 'bg-rw-surface/40' : ''}`}>
                <td className="px-4 py-2 font-rw-mono text-rw-accent">#{r.enrollment_seq}</td>
                <td className="px-4 py-2 text-rw-t1">
                  患者 {r.patient_hash.slice(0,8)}…
                </td>
                <td className="px-4 py-2 text-rw-t2">{r.arm || '—'}</td>
                <td className="px-4 py-2 text-rw-t3 font-rw-mono text-xs">
                  {r.enrolled_at ? new Date(r.enrolled_at).toISOString().slice(0,10) : '—'}
                </td>
                <td className="px-4 py-2"><Pill tone="accent">{r.status}</Pill></td>
                <td className="px-4 py-2 text-rw-t3">—</td>
              </tr>
            ))}
            {rows.length === 0 && (
              <tr><td colSpan={6}
                className="py-6 text-center text-rw-t3 italic">尚无入组患者</td></tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}


// ════════════════════════════════════════════════════════════════════
//  Tab: 安全性
// ════════════════════════════════════════════════════════════════════

interface Observation {
  observation_id:             string;
  study_id:                   string;
  patient_hash:               string;
  created_at:                 number;
  category:                   string;
  ae_grade:                   string | null;
  ae_grade_confirmed:         boolean;
  is_dlt:                     boolean | null;
  source_kind:                string;
  source_node_id:             string | null;
  source_text_excerpt:        string | null;
  linked_assessment_visit_id: string | null;
  medic_confirmed_at:         number | null;
  unlinked_at:                number | null;
  unlink_reason:              string | null;
}

interface StopRuleStatus {
  dlt_observed: number;
  dlt_cap:      number | null;
  run_in_n:     number | null;
  triggered:    boolean;
  note:         string;
}

const GRADES = ['G1', 'G2', 'G3', 'G4', 'G5'] as const;

function fmtTs(ms: number): string {
  const d = new Date(ms);
  const mm = String(d.getMonth() + 1).padStart(2, '0');
  const dd = String(d.getDate()).padStart(2, '0');
  const hh = String(d.getHours()).padStart(2, '0');
  const mi = String(d.getMinutes()).padStart(2, '0');
  return `${mm}-${dd} ${hh}:${mi}`;
}

function SafetyTab({studyId}: {studyId: string}) {
  const [observations, setObservations] = useState<Observation[] | null>(null);
  const [stopRule,     setStopRule]     = useState<StopRuleStatus | null>(null);
  const [filter,       setFilter]       = useState<'all' | 'g3+' | string>('all');
  const [recordOpen,   setRecordOpen]   = useState(false);
  const [error,        setError]        = useState<string | null>(null);

  async function reload() {
    setError(null);
    try {
      const [obs, sr] = await Promise.all([
        api.listStudyObservations(studyId),
        api.getStopRuleStatus(studyId),
      ]);
      setObservations(obs);
      setStopRule(sr);
    } catch (e) {
      setError((e as Error).message || String(e));
    }
  }
  useEffect(() => { reload(); /* eslint-disable-next-line react-hooks/exhaustive-deps */ }, [studyId]);

  // Filter dropdown is mostly client-side. Categories are computed
  // off the actual data so the dropdown only ever shows real options.
  const categories = useMemo(
    () => Array.from(new Set((observations ?? []).map((o) => o.category))).sort(),
    [observations],
  );
  const filtered = useMemo(() => {
    const list = observations ?? [];
    if (filter === 'all')  return list;
    if (filter === 'g3+')  return list.filter((o) => o.ae_grade && o.ae_grade >= 'G3');
    return list.filter((o) => o.category === filter);
  }, [observations, filter]);

  // dlt_cap may be null when the study has no stop_rules_json (e.g.
  // a manually-created study). In that case the bar shows the count
  // but no fill width, and the helper text spells out "未配置".
  const pctFill = stopRule && stopRule.dlt_cap
    ? Math.min(100, Math.round(stopRule.dlt_observed / stopRule.dlt_cap * 100))
    : 0;

  return (
    <div className="px-6 py-5 space-y-4">
      <Card title="Stop-rule 状态 · run-in 队列">
        {stopRule ? (
          <>
            <div className="flex items-center gap-3">
              <div className="flex-1 h-3 rounded-full bg-rw-surface-3 overflow-hidden">
                <div className={`h-3 rounded-full ${stopRule.triggered ? 'bg-rw-red' : 'bg-rw-orange'}`}
                     style={{width: `${pctFill}%`}}/>
              </div>
              <span className="text-xs font-rw-mono text-rw-t2 whitespace-nowrap">
                {stopRule.dlt_observed}
                {stopRule.dlt_cap !== null && ` / ${stopRule.dlt_cap}`} DLT
                {stopRule.dlt_cap === null && ' (未配置)'}
              </span>
            </div>
            <p className={`mt-2 text-xs ${stopRule.triggered ? 'text-rw-red' : 'text-rw-t3'}`}>
              {stopRule.note}
            </p>
          </>
        ) : (
          <p className="text-xs text-rw-t3">载入中…</p>
        )}
      </Card>

      <div className="flex items-center justify-between">
        <h2 className="text-base font-medium text-rw-t1">
          安全性 / 观察事件流
          {observations && (
            <span className="ml-2 text-[11px] font-rw-mono text-rw-t4">
              {filtered.length}/{observations.length}
            </span>
          )}
        </h2>
        <div className="flex items-center gap-2 text-xs text-rw-t3">
          <button onClick={() => setRecordOpen(true)}
            className="px-2.5 py-1 rounded-md bg-rw-accent-bg border border-rw-accent-bd
                       text-rw-accent hover:bg-rw-accent/10 transition">
            + 记录 AE
          </button>
          <select
            value={filter}
            onChange={(e) => setFilter(e.target.value)}
            className="bg-rw-surface border border-rw-border rounded-md px-2 py-1 text-xs">
            <option value="all">全部</option>
            <option value="g3+">仅 ≥G3</option>
            {categories.map((c) => <option key={c} value={c}>{c}</option>)}
          </select>
        </div>
      </div>

      {error && (
        <div className="rounded-md border border-rw-red bg-rw-red-bg p-3 text-sm text-rw-red">
          载入失败：{error}
          <button onClick={reload} className="ml-2 underline">重试</button>
        </div>
      )}

      {observations !== null && filtered.length === 0 && !error && (
        // 这是真实的空状态(没有 mock 数据),向医生交代为何空 + 给出
        // 唯一能产生数据的入口("+ 记录 AE")。SOAP→AE 自动镜像是 Phase 2。
        <div className="rounded-lg border border-dashed border-rw-border bg-rw-surface-2
                        p-8 text-center text-sm text-rw-t3">
          <div className="text-2xl mb-2">🩺</div>
          <div className="text-rw-t2">本研究暂无安全性事件</div>
          <div className="mt-1 text-[11px] text-rw-t4">
            点上方 "+ 记录 AE" 手动录入,或等 Patient Mode 的 SOAP 病程自动镜像
            <br/>(SOAP → AE 自动抽取在 Phase 2 上线)
          </div>
        </div>
      )}

      {filtered.map((o) => (
        <ObservationItem key={o.observation_id} obs={o} onChanged={reload}
                         studyId={studyId} />
      ))}

      {recordOpen && (
        <RecordObservationDialog
          studyId={studyId}
          onCancel={() => setRecordOpen(false)}
          onSaved={() => { setRecordOpen(false); reload(); }}
        />
      )}
    </div>
  );
}

function ObservationItem({obs, studyId, onChanged}: {
  obs: Observation; studyId: string; onChanged: () => Promise<void> | void;
}) {
  const [busy, setBusy] = useState(false);

  // Helper actions hit the canonical mutation endpoints; on success
  // we re-pull the entire list so the DLT counter above also refreshes.
  async function setGrade(g: string) {
    if (busy) return;
    setBusy(true);
    try {
      await api.confirmStudyObservation(studyId, obs.observation_id, {
        aeGrade: g,
        // ≥G3 implicitly counts toward DLT unless explicitly cleared
        // later. The medic can untick is_dlt later via a more advanced
        // editor (out of MVP scope).
        isDlt:   g >= 'G3' ? true : undefined,
      });
      await onChanged();
    } finally {
      setBusy(false);
    }
  }
  async function unlink() {
    const reason = window.prompt('解除关联(标记为误判)的原因:', '');
    if (reason === null) return;  // cancelled
    setBusy(true);
    try {
      await api.unlinkStudyObservation(studyId, obs.observation_id, reason);
      await onChanged();
    } finally {
      setBusy(false);
    }
  }

  const tone: 'red' | 'orange' = obs.ae_grade && obs.ae_grade >= 'G3' ? 'red' : 'orange';
  return (
    <article className="rounded-lg border border-rw-border bg-rw-surface p-4 space-y-2">
      <header className="flex items-center justify-between text-xs text-rw-t3">
        <span className="font-rw-mono">{fmtTs(obs.created_at)}</span>
        {obs.linked_assessment_visit_id && (
          <span>关联到访视：{obs.linked_assessment_visit_id}</span>
        )}
      </header>
      <div className="flex items-center gap-2 text-sm">
        <span className="text-rw-orange">🚩</span>
        <span className="text-rw-t1 font-medium font-rw-mono">
          {obs.patient_hash.slice(0, 10)}
        </span>
        {obs.ae_grade && (
          <Pill tone={tone}>
            {obs.ae_grade}{obs.ae_grade_confirmed ? '' : ' ?'}
          </Pill>
        )}
        {obs.is_dlt && <Pill tone="red">DLT</Pill>}
      </div>
      <div className="grid grid-cols-[100px_1fr] gap-x-3 gap-y-1 text-[12px]">
        <div className="text-rw-t3">类别</div>
        <div className="text-rw-t2">
          {obs.category}
          {obs.source_kind !== 'manual' && (
            <span className="text-rw-t4 italic ml-1">（{obs.source_kind} 自动归类）</span>
          )}
        </div>
        <div className="text-rw-t3">来源</div>
        <div className="text-rw-t2">
          {obs.source_kind === 'manual' ? '医生手动录入' : `Patient Mode · ${obs.source_kind}`}
        </div>
        {obs.source_text_excerpt && (
          <>
            <div className="text-rw-t3">摘录</div>
            <div className="text-rw-t2 italic">"{obs.source_text_excerpt}"</div>
          </>
        )}
      </div>
      <div className="pt-2 flex items-center gap-2 text-xs flex-wrap">
        <span className="text-rw-t3">AE 分级</span>
        {GRADES.map((g) => (
          <button
            key={g}
            disabled={busy}
            onClick={() => setGrade(g)}
            className={`px-2 py-0.5 rounded text-[11px] font-rw-mono transition
              ${g === obs.ae_grade
                ? (obs.ae_grade_confirmed
                    ? 'bg-rw-orange-bg text-rw-orange border border-rw-orange'
                    : 'bg-rw-orange-bg text-rw-orange border border-dashed border-rw-orange')
                : 'bg-rw-surface-2 text-rw-t3 border border-rw-border hover:border-rw-accent-bd'}
              disabled:opacity-50`}>
            {g}
          </button>
        ))}
        {!obs.ae_grade_confirmed && (
          <span className="text-rw-t4 italic ml-1">← 待医生确认</span>
        )}
        {obs.ae_grade_confirmed && obs.medic_confirmed_at && (
          <span className="text-rw-t4 ml-1">✓ {fmtTs(obs.medic_confirmed_at)}</span>
        )}
        <span className="flex-1" />
        <button onClick={unlink} disabled={busy}
          className="text-rw-t2 text-[11px] hover:text-rw-red disabled:opacity-50">
          解除关联（误判）
        </button>
      </div>
    </article>
  );
}

// MVP manual-entry path. Phase 2 swaps this for an auto-mirrored
// SOAP → AE pipeline; until then, the medic must click "+ 记录 AE"
// so we have any observations to confirm at all.
function RecordObservationDialog({studyId, onCancel, onSaved}: {
  studyId: string;
  onCancel: () => void;
  onSaved: () => void;
}) {
  const [roster,        setRoster]        = useState<Array<{
    patient_hash: string; enrollment_seq: number;
  }>>([]);
  const [patientHash,   setPatientHash]   = useState<string>('');
  const [category,      setCategory]      = useState<string>('');
  const [aeGrade,       setAeGrade]       = useState<string>('');
  const [isDlt,         setIsDlt]         = useState<boolean>(false);
  const [excerpt,       setExcerpt]       = useState<string>('');
  const [busy,          setBusy]          = useState<boolean>(false);
  const [err,           setErr]           = useState<string | null>(null);

  useEffect(() => {
    api.getRoster(studyId).then((r) => {
      const rows = r as Array<{patient_hash: string; enrollment_seq: number}>;
      setRoster(rows);
      if (rows.length > 0) setPatientHash(rows[0].patient_hash);
    }).catch((e) => setErr(String(e)));
  }, [studyId]);

  async function save() {
    if (!patientHash || !category.trim()) {
      setErr('患者 + 类别 必填');
      return;
    }
    setBusy(true); setErr(null);
    try {
      await api.recordStudyObservation(studyId, {
        patientHash,
        category:          category.trim(),
        aeGrade:           aeGrade || undefined,
        isDlt:             isDlt || undefined,
        sourceTextExcerpt: excerpt.trim() || undefined,
      });
      onSaved();
    } catch (e) {
      setErr((e as Error).message || String(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60"
         onClick={onCancel}>
      <div className="w-[520px] rounded-lg border border-rw-border bg-rw-surface
                      p-5 shadow-2xl font-rw-display space-y-3"
           onClick={(e) => e.stopPropagation()}>
        <h2 className="text-rw-t1 text-base font-semibold">记录 AE / 观察事件</h2>
        <p className="text-[11px] text-rw-t3 -mt-1">
          仅录入 — 录入后默认 <span className="text-rw-t2">未确认</span>;医生在事件流里点 G1-G5 按钮才正式确认。
        </p>

        <div>
          <label className="text-xs text-rw-t3 block mb-1">患者(入组列表)</label>
          <select value={patientHash}
            onChange={(e) => setPatientHash(e.target.value)}
            className="w-full bg-rw-bg border border-rw-border rounded-md px-2 py-1.5 text-sm text-rw-t1">
            {roster.length === 0 && <option value="">(暂无入组患者)</option>}
            {roster.map((r) => (
              <option key={r.patient_hash} value={r.patient_hash}>
                #{r.enrollment_seq} · {r.patient_hash.slice(0, 12)}
              </option>
            ))}
          </select>
        </div>

        <div>
          <label className="text-xs text-rw-t3 block mb-1">类别 / category</label>
          <input value={category}
            onChange={(e) => setCategory(e.target.value)}
            placeholder="如 肺部毒性、疲劳 / Constitutional、肝功能异常 …"
            className="w-full bg-rw-bg border border-rw-border rounded-md px-2 py-1.5 text-sm text-rw-t1"/>
        </div>

        <div className="flex items-center gap-3">
          <div>
            <label className="text-xs text-rw-t3 block mb-1">建议分级</label>
            <select value={aeGrade}
              onChange={(e) => setAeGrade(e.target.value)}
              className="bg-rw-bg border border-rw-border rounded-md px-2 py-1.5 text-sm text-rw-t1">
              <option value="">(未定)</option>
              {GRADES.map((g) => <option key={g} value={g}>{g}</option>)}
            </select>
          </div>
          <label className="flex items-center gap-1.5 text-xs text-rw-t2 mt-5">
            <input type="checkbox" checked={isDlt}
              onChange={(e) => setIsDlt(e.target.checked)} />
            视为 DLT(计入 stop-rule)
          </label>
        </div>

        <div>
          <label className="text-xs text-rw-t3 block mb-1">摘录 / 原文片段(可选)</label>
          <textarea value={excerpt}
            onChange={(e) => setExcerpt(e.target.value)}
            rows={3}
            placeholder="比如 SOAP 原文里的关键句"
            className="w-full bg-rw-bg border border-rw-border rounded-md px-2 py-1.5 text-sm text-rw-t1"/>
        </div>

        {err && <div className="text-xs text-rw-red">{err}</div>}

        <div className="flex justify-end gap-2 pt-1">
          <button onClick={onCancel} disabled={busy}
            className="px-3 py-1.5 rounded-md text-sm text-rw-t2 border border-rw-border
                       hover:bg-rw-surface-2 disabled:opacity-50">
            取消
          </button>
          <button onClick={save} disabled={busy || !patientHash || !category.trim()}
            className="px-3 py-1.5 rounded-md text-sm font-medium
                       bg-rw-accent text-[#06252c] disabled:opacity-50">
            {busy ? '保存中…' : '保存'}
          </button>
        </div>
      </div>
    </div>
  );
}


// ════════════════════════════════════════════════════════════════════
//  Tab: 进度计划
// ════════════════════════════════════════════════════════════════════

interface GanttRow {
  patient_hash: string;
  enrollment_seq: number;
  enrollment_status: string;
  enrolled_at: number;
  cells: Array<{
    timepoint: string;
    status: 'planned' | 'in_progress' | 'completed' | 'missed' | 'overdue' | 'future';
    kinds: string[];
    due_at?: number;
    completed_at?: number;
  }>;
}
interface GanttData {
  timepoints: Array<{label: string; offset_days: number; visit_id: string}>;
  rows: GanttRow[];
}

function ScheduleTab({studyId}: {studyId: string}) {
  const [data, setData] = useState<GanttData | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    setLoading(true);
    api.getResearchScheduleGantt(studyId)
       .then((d) => setData(d as never))
       .catch((e) => { console.warn('gantt', e); setData(null); })
       .finally(() => setLoading(false));
  }, [studyId]);

  const cellMark = (status: string) => {
    const map: Record<string, {label: string; color: string; bg?: string}> = {
      completed:    {label: '✓', color: 'text-rw-green'},
      in_progress:  {label: '◐', color: 'text-rw-accent'},
      planned:      {label: '●', color: 'text-rw-accent'},
      missed:       {label: '✗', color: 'text-rw-red'},
      overdue:      {label: '!', color: 'text-rw-red'},
      future:       {label: '○', color: 'text-rw-t4'},
    };
    return map[status] || map.future;
  };
  const labelShort = (lbl: string, off: number) => {
    // Compress common labels for column headers
    if (lbl === 'baseline')  return 'Base';
    if (lbl === 'screen')    return '-' + Math.abs(off) + 'd';
    if (off >= 365 && off % 365 === 0) return `${off/365}y`;
    if (off >= 30  && off % 30  === 0) return `${off/30}m`;
    if (off >= 7   && off % 7   === 0) return `${off/7}w`;
    if (off === 0) return lbl.slice(0, 5);
    return `${off}d`;
  };

  return (
    <div className="px-6 py-5 space-y-4">
      <h2 className="text-base font-medium text-rw-t1">访视计划甘特图</h2>
      {loading && (
        <div className="text-sm text-rw-t3 italic">载入中…</div>
      )}
      {!loading && (!data || data.rows.length === 0) && (
        <div className="rounded-lg border border-dashed border-rw-border bg-rw-surface
                        p-8 text-center text-sm text-rw-t3 italic">
          暂无访视数据 — 入组患者后 schedule 会自动展开。
        </div>
      )}
      {!loading && data && data.rows.length > 0 && (
        <>
          <div className="rounded-lg border border-rw-border overflow-x-auto bg-rw-surface">
            <div className="min-w-max">
              {/* Header row */}
              <div className="grid gap-1 px-4 py-2 border-b border-rw-border-soft
                              text-[11px] uppercase tracking-wider text-rw-t3 font-rw-mono"
                   style={{gridTemplateColumns: `200px repeat(${data.timepoints.length}, minmax(48px, 1fr))`}}>
                <div>患者</div>
                {data.timepoints.map((tp) => (
                  <div key={tp.visit_id} className="text-center"
                       title={`${tp.label} (offset ${tp.offset_days}d)`}>
                    {labelShort(tp.label, tp.offset_days)}
                  </div>
                ))}
              </div>
              {/* Data rows */}
              {data.rows.map((r) => {
                const enrolledMs = r.enrolled_at;
                const weeks = enrolledMs
                  ? Math.floor((Date.now() - enrolledMs) / (7*86400_000))
                  : 0;
                return (
                  <div key={r.patient_hash}
                       className="grid gap-1 px-4 py-2 border-b border-rw-border-soft items-center"
                       style={{gridTemplateColumns: `200px repeat(${data.timepoints.length}, minmax(48px, 1fr))`}}>
                    <div className="text-rw-t1 text-sm truncate">
                      #{r.enrollment_seq} · {r.patient_hash.slice(0,8)}…
                      <span className="ml-2 text-rw-t4 text-[10px] font-rw-mono">W{weeks}</span>
                      {r.enrollment_status === 'withdrawn' && (
                        <span className="ml-1 text-[9px] text-rw-orange font-rw-mono">退出</span>
                      )}
                    </div>
                    {r.cells.map((c, i) => {
                      const M = cellMark(c.status);
                      const tooltip = `${c.timepoint} · ${c.status}` +
                        (c.kinds.length ? `\n${c.kinds.join(', ')}` : '');
                      return (
                        <div key={i}
                             title={tooltip}
                             className={`text-center text-base font-mono ${M.color}
                                         hover:bg-rw-surface-3 rounded cursor-pointer`}>
                          {M.label}
                        </div>
                      );
                    })}
                  </div>
                );
              })}
            </div>
          </div>
          <div className="text-[11px] text-rw-t3 flex gap-4 px-2 flex-wrap">
            <span><span className="text-rw-green">✓</span> 已完成</span>
            <span><span className="text-rw-accent">●</span> 当前/计划</span>
            <span><span className="text-rw-t4">○</span> 未来</span>
            <span><span className="text-rw-red">!</span> 已逾期</span>
            <span><span className="text-rw-red">✗</span> 已 missed</span>
            <span className="text-rw-t4 italic ml-auto">点单元格 → 打开 visit checklist (TODO)</span>
          </div>
        </>
      )}
    </div>
  );
}


// ════════════════════════════════════════════════════════════════════
//  Tab: 研究对话
// ════════════════════════════════════════════════════════════════════

interface ChatMessage {
  role: 'user' | 'agent';
  text: string;
  streaming?: boolean;
  scope_info?: { cohort_size: number; focus_patient_hash: string | null };
  /** F-history-attachments — filenames the medic attached to this
   *  user turn. Comes from the server's ChatMessageView.attachments[].
   *  Without this, history-load drops the 📎 chip and the medic
   *  can't see which files they sent. */
  attachedFileNames?: string[];
}

interface ChatAttachment {
  key: string;
  name: string;
  sizeBytes: number;
  fileId: string | null;
  failed?: string;
  /** ``image/*`` MIME types get a local blob URL so the chip can
   *  render a real thumbnail in the composer (the way a chat app
   *  should — showing only "image.png 230K" is hostile UX when the
   *  medic just dropped a screenshot of a CT and wants to confirm
   *  it's the right one before sending). For non-image files this
   *  is undefined. */
  previewUrl?: string;
  /** Cached so the chip render path doesn't have to re-parse the
   *  mime on every render. */
  isImage?: boolean;
}

// Shared chip renderer used by the per-study Research ChatTab AND the
// workspace-level CrossResearchChat. Lives at module scope so the two
// chat components can both grow their own attachment list independently
// without duplicating the (now non-trivial) layout / thumbnail / state
// chips logic.
function AttachmentChipsRow({
  attachments, onRemove,
}: {
  attachments: ChatAttachment[];
  onRemove: (key: string) => void;
}) {
  if (attachments.length === 0) return null;
  return (
    <div className="mb-2 flex flex-wrap gap-1.5">
      {attachments.map((a) => {
        const stateClasses = a.failed
          ? 'border-rw-red text-rw-red bg-rw-red-bg'
          : a.fileId
            ? 'border-rw-accent-bd text-rw-accent bg-rw-accent-bg'
            : 'border-rw-border text-rw-t3 bg-rw-surface-2';
        const stateBadge = a.failed ? '✕' : a.fileId ? '✓' : '⟳';
        if (a.previewUrl) {
          // Image attachment — render a real thumbnail with the chip's
          // state badge floated in the corner. 56px is tall enough to
          // see what the image is but compact enough to keep several
          // attachments visible at once.
          return (
            <div key={a.key}
              className={`relative w-14 h-14 rounded overflow-hidden border ${stateClasses}`}
              title={`${a.name} · ${Math.max(1, Math.round(a.sizeBytes / 1024))}K`}>
              <img src={a.previewUrl} alt={a.name}
                   className="w-full h-full object-cover"/>
              <span className="absolute top-0.5 left-0.5 px-1 py-0
                               rounded bg-black/60 text-white
                               text-[10px] leading-tight">
                {stateBadge}
              </span>
              <button onClick={() => onRemove(a.key)}
                className="absolute top-0.5 right-0.5 w-4 h-4 rounded-full
                           bg-black/60 text-white text-[10px] leading-none
                           flex items-center justify-center
                           hover:bg-black/80">
                ×
              </button>
            </div>
          );
        }
        // Non-image file — keep the text chip.
        return (
          <span key={a.key}
            className={`inline-flex items-center gap-1.5 px-2 py-1 rounded
                        text-[11px] font-rw-mono border ${stateClasses}`}>
            {stateBadge}
            <span className="max-w-[180px] truncate">{a.name}</span>
            <span className="opacity-60">
              {Math.max(1, Math.round(a.sizeBytes / 1024))}K
            </span>
            <button onClick={() => onRemove(a.key)}
                    className="opacity-60 hover:opacity-100">×</button>
          </span>
        );
      })}
    </div>
  );
}

// Helper: build a ChatAttachment placeholder from a File. If the file
// is an image, also generate a blob URL for the thumbnail. The caller
// is responsible for revoking the URL when the chip is removed
// (handled in the onRemove handlers below).
function _makeChatAttachment(file: File): ChatAttachment {
  const isImage = (file.type || '').startsWith('image/');
  return {
    key: `${file.name}-${file.size}-${Date.now()}-${Math.random().toString(36).slice(2, 6)}`,
    name: file.name,
    sizeBytes: file.size,
    fileId: null,
    isImage,
    previewUrl: isImage ? URL.createObjectURL(file) : undefined,
  };
}

function ChatTab({studyId, study}: {studyId: string; study: StudyData}) {
  // F-unified-chat-files — research-scope library, one per study.
  const chatFiles = useChatFiles('research', studyId);
  const fileMap: Record<string, FileChipRef> = {};
  for (const f of chatFiles.files) {
    fileMap[f.fIdToken] = {
      fileId: f.fileId, name: f.name,
      textExtractionStatus: f.textExtractionStatus,
    };
  }
  // Persist focus across tab switches; per-study session id stays put
  const [focus, setFocus]         = useState<string | null>(null);
  const [messages, setMessages]   = useState<ChatMessage[]>([]);
  const [input, setInput]         = useState<string>('');
  const [busy, setBusy]           = useState<boolean>(false);
  const [roster, setRoster]       = useState<Array<{patient_hash: string; enrollment_seq: number}>>([]);
  // Files staged for the next turn. Same UX contract as Patient Chat
  // (see modes.tsx PatientMode): chip appears immediately on paste /
  // drop / picker; fileId fills in when /files/upload returns.
  const [attachments, setAttachments] = useState<ChatAttachment[]>([]);

  // Session ID = study scoped. Different studies have different chats.
  const sessionId = useMemo(() => `research-${studyId}`, [studyId]);

  // Pull a roster snapshot for the 🎯 focus picker.
  useEffect(() => {
    api.getRoster(studyId).then((r) => setRoster(r as never)).catch(console.warn);
  }, [studyId]);

  // Load chat history for THIS study's session on mount + when
  // sessionId changes (i.e. when the medic switches study). Without
  // this the medic loses every previous turn when they navigate away
  // and come back — "对话记录也没有了".
  useEffect(() => {
    let cancelled = false;
    setMessages([]);
    api.listSessionMessages(sessionId, 200).then(
      (rows) => {
        if (cancelled) return;
        setMessages(rows.map((r) => ({
          role: r.role === 'agent' ? 'agent' : 'user',
          text: r.text,
          attachedFileNames: (r.attachments ?? []).map((a) => a.name),
        })));
      },
      () => { /* history is nice-to-have — empty pane is fine */ },
    );
    return () => { cancelled = true; };
  }, [sessionId]);

  async function uploadOne(file: File): Promise<string | null> {
    try {
      // F-unified-chat-files — bind to research-scope library so the
      // file shows in chip strip + sticks around for future turns.
      const r = await api.uploadFile(file, file.name, {
        patientHash: focus ?? undefined,
        libScopeKind: 'research',
        libScopeRef:  studyId,
      });
      try { chatFiles.refresh(); } catch { /* hook not yet ready */ }
      return r.fileId;
    } catch (e) {
      console.warn('research chat upload failed', e);
      return null;
    }
  }

  function acceptFiles(files: FileList | File[]) {
    const arr = Array.from(files);
    if (arr.length === 0) return;
    const placeholders: ChatAttachment[] = arr.map(_makeChatAttachment);
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

  function onPaste(e: React.ClipboardEvent<HTMLInputElement>) {
    const files = e.clipboardData?.files;
    if (files && files.length > 0) {
      e.preventDefault();
      acceptFiles(files);
    }
  }

  function onDrop(e: React.DragEvent<HTMLDivElement>) {
    e.preventDefault();
    if (e.dataTransfer?.files?.length) acceptFiles(e.dataTransfer.files);
  }

  function removeAttachment(key: string) {
    // Revoke any blob URL the thumbnail was using before dropping the
    // attachment — otherwise every paste-then-remove leaks a few MB
    // of bitmap data in the WebView's memory.
    setAttachments((prev) => {
      const found = prev.find((a) => a.key === key);
      if (found?.previewUrl) {
        try { URL.revokeObjectURL(found.previewUrl); } catch { /* ignore */ }
      }
      return prev.filter((a) => a.key !== key);
    });
  }

  const send = async () => {
    const text = input.trim();
    if ((!text && attachments.length === 0) || busy) return;

    // Wait for in-flight uploads to settle so the file_ids we pass to
    // sendChat are real — matches Patient Chat semantics.
    const pending = attachments.filter((a) => a.fileId === null && !a.failed);
    if (pending.length > 0) {
      console.info(`research chat: waiting for ${pending.length} upload(s)`);
      return;
    }
    const fileIds = attachments
      .filter((a) => a.fileId)
      .map((a) => a.fileId as string);
    const stagedNames = attachments.map((a) => a.name);

    setBusy(true);
    setInput('');
    setAttachments([]);
    // F-history-attachments — capture attached names on the user turn
    // so the chip strip survives history reload (server returns these
    // via ChatMessageView.attachments, which we now hydrate too).
    const userMsg: ChatMessage = {
      role: 'user', text, attachedFileNames: stagedNames,
    };
    setMessages((m) => [...m, userMsg, { role: 'agent', text: '', streaming: true }]);

    try {
      for await (const chunk of api.sendChat(text, sessionId, focus, fileIds, {
        kind: 'research',
        studyId,
        focusPatientHash: focus,
      })) {
        // Canonical SSE shape uses `type` discriminator (see
        // ChatStreamChunk in lib/types.ts). Two extra payloads the
        // research scope carries that aren't in the public union yet:
        //   - scope_resolved { cohort_size, focus_patient_hash }
        // are read off the chunk via a local cast.
        if (chunk.type === 'final_answer_chunk' && chunk.text) {
          setMessages((m) => {
            const next = m.slice();
            const last = next[next.length - 1];
            if (last && last.role === 'agent') {
              last.text += chunk.text;
            }
            return next;
          });
          continue;
        }
        const sr = chunk as unknown as {
          type?: string;
          cohort_size?: number;
          focus_patient_hash?: string | null;
        };
        if (sr.type === 'scope_resolved') {
          setMessages((m) => {
            const next = m.slice();
            const last = next[next.length - 1];
            if (last && last.role === 'agent') {
              last.scope_info = {
                cohort_size: sr.cohort_size || 0,
                focus_patient_hash: sr.focus_patient_hash || null,
              };
            }
            return next;
          });
        }
      }
    } catch (e) {
      setMessages((m) => {
        const next = m.slice();
        const last = next[next.length - 1];
        if (last && last.role === 'agent') {
          last.text = `(出错：${(e as Error).message || String(e)})`;
        }
        return next;
      });
    } finally {
      setBusy(false);
      setMessages((m) => {
        const next = m.slice();
        const last = next[next.length - 1];
        if (last) last.streaming = false;
        return next;
      });
    }
  };

  return (
    <div className="h-full flex flex-col"
         onDrop={onDrop}
         onDragOver={(e) => e.preventDefault()}>
      <div className="px-6 py-3 border-b border-rw-border-soft flex items-center gap-3 flex-wrap">
        <span className="text-xs text-rw-t3">
          scope: <span className="text-rw-accent font-rw-mono">
            {study.enrolled_count} 入组 + {study.candidate_count} 候选
          </span>
        </span>
        <span className="px-2 py-0.5 rounded text-[10px] font-rw-mono uppercase tracking-wider
                         bg-rw-accent-bg text-rw-accent border border-rw-accent-bd">
          + 文献
        </span>
        {focus && (
          <span className="px-2 py-0.5 rounded text-[10px] font-rw-mono
                           bg-rw-orange-bg text-rw-orange border border-rw-orange">
            🎯 聚焦 {focus.slice(0, 8)}
            <button onClick={() => setFocus(null)} className="ml-1.5 hover:text-rw-t1">×</button>
          </span>
        )}
        <div className="flex-1" />
        {/* Per-user insights distilled from earlier chats; scoped to
            this study so the medic sees what Nexus has learned about
            HOW they reason within this trial specifically. */}
        <TakeawaysButton scopeKind="research" scopeRef={studyId} tone="rw" />
        {/* Focus picker */}
        {roster.length > 0 && (
          <select value={focus || ''}
            onChange={(e) => setFocus(e.target.value || null)}
            className="bg-rw-surface border border-rw-border rounded-md px-2 py-1 text-xs text-rw-t2">
            <option value="">— 不聚焦（cohort 模式） —</option>
            {roster.map((r) => (
              <option key={r.patient_hash} value={r.patient_hash}>
                #{r.enrollment_seq} · {r.patient_hash.slice(0, 8)}
              </option>
            ))}
          </select>
        )}
      </div>

      <div className="flex-1 overflow-y-auto px-6 py-5 space-y-4">
        {messages.length === 0 && (
          <div className="text-center text-sm text-rw-t3 italic py-10">
            <div className="text-2xl mb-2">💬</div>
            cohort-aware AI 已就位 — 试试问：
            <ul className="mt-3 text-[12px] inline-block text-left space-y-1">
              <li>· 入组 ≥3 月的患者中谁达到 mPFS？</li>
              <li>· 对 G2 IO 肺炎，我们用过什么剂量？</li>
              <li>· 我们的 mPFS 趋势与 PACIFIC、CheckMate-816 对比？</li>
            </ul>
          </div>
        )}
        {messages.map((m, i) => (
          <div key={i}>
            <div className={`flex ${m.role === 'user' ? 'justify-end' : 'justify-start'}`}>
              <div className={`max-w-[78%] rounded-lg px-4 py-2.5 text-sm
                ${m.role === 'user'
                  ? 'bg-rw-accent text-[#06252c]'
                  : 'bg-rw-surface text-rw-t1 border border-rw-border'}`}>
                {/* F-thinking-uniform: ChatMarkdown if text, otherwise
                    nothing in body — the StreamingFooter below the
                    bubble carries the "still working" signal so the
                    medic always sees ONE indicator while busy. */}
                {m.text && (
                  <ChatMarkdown text={m.text}
                                tone={m.role === 'user' ? 'inverse' : 'agent'}
                                fileMap={fileMap} />
                )}
                {m.streaming && m.text && <StreamingCursor tone="rw" />}
              </div>
            </div>
            {m.role === 'agent' && (
              <StreamingFooter
                streaming={m.streaming}
                hasText={!!(m.text && m.text.length > 0)}
                tone="rw"
              />
            )}
            {m.scope_info && (
              <div className="text-[10px] text-rw-t4 mt-1 font-rw-mono pl-1">
                ← scope resolved · cohort {m.scope_info.cohort_size} 例
                {m.scope_info.focus_patient_hash &&
                  ` · 聚焦 ${m.scope_info.focus_patient_hash.slice(0,8)}`}
              </div>
            )}
            {/* F-history-attachments — render attachment chips on user
                turns so the medic sees which files they sent, both
                fresh in this session AND on history reload. */}
            {m.role === 'user' && m.attachedFileNames && m.attachedFileNames.length > 0 && (
              <div className="mt-1 flex flex-wrap gap-1 justify-end">
                {m.attachedFileNames.map((name, fi) => (
                  <span
                    key={fi}
                    className="rounded-sm border border-rw-border bg-rw-surface px-1.5 py-0.5 text-[10px] text-rw-t3"
                  >
                    📎 {name}
                  </span>
                ))}
              </div>
            )}
          </div>
        ))}
      </div>

      <div className="px-6 py-3 border-t border-rw-border">
        {focus && (
          <div className="mb-2 text-[11px] text-rw-orange flex items-center gap-1.5">
            🎯 写入将归属于 {focus.slice(0,8)} —
            <button onClick={() => setFocus(null)}
                    className="underline hover:text-rw-accent">取消聚焦</button>
          </div>
        )}
        {/* F-unified-chat-files — research-scope file library */}
        <div className="mb-2">
          <ChatFileChipStrip
            scopeKind="research"
            scopeRef={studyId}
            controller={chatFiles}
            tone="rw"
          />
        </div>
        <AttachmentChipsRow
          attachments={attachments}
          onRemove={removeAttachment}
        />
        <div className="flex items-center gap-2 rounded-lg border border-rw-border
                        bg-rw-surface px-3 py-2">
          <label className="cursor-pointer text-rw-t3 hover:text-rw-accent text-base leading-none"
                 title="附件（也可粘贴/拖拽）">
            📎
            <input type="file" multiple hidden
              onChange={(e) => {
                if (e.target.files) acceptFiles(e.target.files);
                // Reset so picking the same file twice re-fires onChange.
                e.target.value = '';
              }}
            />
          </label>
          <input value={input}
            onChange={(e) => setInput(e.target.value)}
            onPaste={onPaste}
            onKeyDown={(e) => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send(); }}}
            disabled={busy}
            placeholder="提问、summarize、find candidates、draft Table 1…（可粘贴/拖拽文件）"
            className="flex-1 bg-transparent text-sm text-rw-t1 placeholder:text-rw-t4 outline-none"
          />
          <button onClick={send} disabled={busy || (!input.trim() && attachments.length === 0)}
            className="px-3 py-1 rounded-md bg-rw-accent text-[#06252c] text-xs font-medium
                       disabled:opacity-60">
            {busy ? '…' : '发送'}
          </button>
        </div>
      </div>
    </div>
  );
}


// ════════════════════════════════════════════════════════════════════
//  Tab: 报告导出
// ════════════════════════════════════════════════════════════════════

function ReportsTab({studyId}: {studyId: string}) {
  const [busy, setBusy] = useState<string | null>(null);
  const [history, setHistory] = useState<Array<{when:string;name:string;fid?:string}>>([
    {when:'2026-04-30', name:'Interim safety report (n=8)'},
    {when:'2026-01-15', name:'Study kickoff baseline'},
  ]);
  const gen = async (kind: string) => {
    setBusy(kind);
    try {
      const r = await api.generateInterimReport(studyId);
      setHistory(h => [{when: new Date().toISOString().slice(0,10),
        name: kind + ' (新)', fid: r.file_id}, ...h]);
    } finally { setBusy(null); }
  };
  return (
    <div className="px-6 py-5 space-y-5">
      <Card title="快捷操作">
        <div className="grid grid-cols-2 gap-3">
          <QuickAction icon="📊" title="Interim 报告 (.docx)"
            sub="Table 1 / AE / KM / CONSORT 一次生成"
            busy={busy === 'interim'}
            onClick={() => gen('interim')} />
          <QuickAction icon="📤" title="脱敏数据集 (.xlsx)"
            sub="一行 = 一位患者 × 一次访视；PHI 已脱敏"
            onClick={() => gen('xlsx')} />
          <QuickAction icon="📐" title="CONSORT 图 (.svg)"
            sub="入组流图 — 自动从 screen / enroll / withdraw 计数"
            onClick={() => gen('consort')} />
          <QuickAction icon="✍" title="手稿草稿 (.docx)"
            sub="IMRaD 结构 + 文献占位 + 引用脚注"
            onClick={() => gen('manuscript')} />
        </div>
      </Card>

      <Card title="已生成报告">
        {history.length === 0 && (
          <div className="text-sm text-rw-t3 italic">暂无</div>
        )}
        {history.map((h, i) => (
          <div key={i} className="flex items-center justify-between
                                   py-2 border-b border-rw-border-soft last:border-0">
            <div>
              <div className="text-sm text-rw-t1">{h.name}</div>
              <div className="text-[11px] text-rw-t4 font-rw-mono">{h.when}</div>
            </div>
            <div className="flex gap-2">
              <button className="px-3 py-1 rounded-md bg-rw-surface-2 border border-rw-border
                                text-xs text-rw-t2 hover:border-rw-accent-bd">打开</button>
              <button className="px-3 py-1 rounded-md bg-rw-surface-2 border border-rw-border
                                text-xs text-rw-t2 hover:border-rw-accent-bd">导出 CSV</button>
            </div>
          </div>
        ))}
      </Card>
    </div>
  );
}

function QuickAction({icon, title, sub, onClick, busy}: {
  icon: string; title: string; sub: string;
  onClick: () => void; busy?: boolean;
}) {
  return (
    <button onClick={onClick} disabled={busy}
      className="text-left rounded-lg border border-rw-border bg-rw-surface
                 p-4 hover:border-rw-accent-bd transition disabled:opacity-60">
      <div className="text-2xl mb-1">{icon}</div>
      <div className="text-sm font-medium text-rw-t1">{title}</div>
      <div className="text-[11px] text-rw-t3 mt-0.5">{sub}</div>
      {busy && <div className="mt-1 text-[10px] text-rw-accent">生成中…</div>}
    </button>
  );
}


// ════════════════════════════════════════════════════════════════════
//  Invite modal
// ════════════════════════════════════════════════════════════════════

function InviteModal(props: {
  row: ScreeningRow; studyId: string;
  onClose: () => void; onConfirmed: () => void;
}) {
  const [consentDate, setConsentDate] = useState<string>(new Date().toISOString().slice(0,10));
  const [emailConsent, setEmailConsent] = useState<boolean>(false);
  const [arm, setArm] = useState<string>('');
  const [busy, setBusy] = useState(false);

  const confirm = async () => {
    setBusy(true);
    try {
      await api.enrollPatient(props.studyId, {
        patientHash: props.row.patient_hash,
        arm: arm || undefined,
        consentSignedAt: new Date(consentDate).getTime(),
        notes: emailConsent ? 'email_reminder_consent=true' : undefined,
      });
      props.onConfirmed();
    } finally { setBusy(false); }
  };

  return (
    <div className="fixed inset-0 z-50 bg-black/60 flex items-center justify-center">
      <div className="rw-root w-[460px] rounded-xl bg-rw-bg border border-rw-border p-5 space-y-3">
        <h3 className="text-base font-semibold text-rw-t1">
          邀请入组 · 患者 #{props.row.patient_hash.slice(0,6)}
        </h3>
        <p className="text-xs text-rw-t3">
          确认后将写入 study_enrollments，并按协议自动展开访视计划。
        </p>
        <label className="block">
          <span className="text-[11px] text-rw-t3">知情同意签署时间</span>
          <input type="date" value={consentDate}
            onChange={(e) => setConsentDate(e.target.value)}
            className="mt-0.5 w-full bg-rw-surface border border-rw-border rounded-md
                       px-3 py-1.5 text-sm text-rw-t1"/>
        </label>
        <label className="block">
          <span className="text-[11px] text-rw-t3">分配 Arm（多臂研究时填）</span>
          <input value={arm} onChange={(e) => setArm(e.target.value)}
            placeholder="留空 = 单臂"
            className="mt-0.5 w-full bg-rw-surface border border-rw-border rounded-md
                       px-3 py-1.5 text-sm text-rw-t1"/>
        </label>
        <label className="flex items-center gap-2 text-sm text-rw-t2 pt-1">
          <input type="checkbox" checked={emailConsent}
            onChange={(e) => setEmailConsent(e.target.checked)} />
          患者同意通过邮件接收随访提醒
        </label>
        <div className="flex justify-end gap-2 pt-2">
          <button onClick={props.onClose}
            className="px-3 py-1.5 rounded-md bg-rw-surface-2 border border-rw-border
                       text-xs text-rw-t2">取消</button>
          <button onClick={confirm} disabled={busy}
            className="px-3 py-1.5 rounded-md bg-rw-accent text-[#06252c] text-xs font-medium
                       disabled:opacity-60">
            {busy ? '入组中…' : '确认入组'}
          </button>
        </div>
      </div>
    </div>
  );
}


// ════════════════════════════════════════════════════════════════════
//  New Study dialog
// ════════════════════════════════════════════════════════════════════

/**
 * Two-mode New Study dialog:
 *   - 手动 — fill the basic fields and create a draft study
 *   - 导入 .docx — upload the IRB-approved protocol, let the parser
 *     extract inclusion / exclusion / schedule, then medic batch-confirms
 *     in-place (D7 ✅).
 */
function NewStudyDialog(props: {
  onCancel: () => void;
  onCreated: (studyId: string) => void;
}) {
  const [mode, setMode] = useState<'manual' | 'docx'>('manual');
  return (
    <div className="fixed inset-0 z-50 bg-black/60 flex items-center justify-center">
      <div className="rw-root w-[640px] max-h-[88vh] rounded-xl bg-rw-bg
                      border border-rw-border flex flex-col overflow-hidden">
        <header className="px-5 pt-4 pb-3 border-b border-rw-border-soft flex items-center justify-between">
          <h3 className="text-base font-semibold text-rw-t1">新建研究</h3>
          <div className="inline-flex bg-rw-surface border border-rw-border rounded-md p-0.5 gap-0.5">
            <button onClick={() => setMode('manual')}
              className={`px-3 py-1 text-xs rounded ${
                mode === 'manual' ? 'bg-rw-accent text-[#06252c] font-medium'
                                   : 'text-rw-t2 hover:bg-rw-surface-2'}`}>
              手动填写
            </button>
            <button onClick={() => setMode('docx')}
              className={`px-3 py-1 text-xs rounded ${
                mode === 'docx'   ? 'bg-rw-accent text-[#06252c] font-medium'
                                   : 'text-rw-t2 hover:bg-rw-surface-2'}`}>
              导入 .docx
            </button>
          </div>
        </header>
        {mode === 'manual'
          ? <NewStudyManualPane onCancel={props.onCancel} onCreated={props.onCreated} />
          : <NewStudyDocxPane   onCancel={props.onCancel} onCreated={props.onCreated} />}
      </div>
    </div>
  );
}


function NewStudyManualPane(props: {
  onCancel: () => void;
  onCreated: (studyId: string) => void;
}) {
  const [name, setName] = useState('');
  const [code, setCode] = useState('');
  const [phase, setPhase] = useState('II');
  const [target, setTarget] = useState('');
  const [endpoint, setEndpoint] = useState('');
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string|null>(null);

  const submit = async () => {
    if (!name.trim() || !code.trim()) { setErr('显示名与简称都是必填'); return; }
    setBusy(true);
    try {
      const r = await api.createStudy({
        displayName: name.trim(), shortCode: code.trim(), phase,
        targetN: target ? Number(target) : null,
        primaryEndpoint: endpoint || undefined,
      });
      const sid = (r as {study_id?: string}).study_id;
      if (sid) props.onCreated(sid); else setErr('后端未返回 study_id');
    } catch (e) { setErr(String((e as Error).message || e)); }
    finally { setBusy(false); }
  };

  return (
    <>
      <div className="px-5 py-4 space-y-3 overflow-y-auto">
        <Field label="显示名">
          <input value={name} onChange={(e) => setName(e.target.value)}
                 className="w-full bg-rw-surface border border-rw-border rounded-md px-3 py-1.5 text-sm text-rw-t1"/>
        </Field>
        <Field label="简称（如 HybridRT-IV）">
          <input value={code} onChange={(e) => setCode(e.target.value)}
                 className="w-full bg-rw-surface border border-rw-border rounded-md px-3 py-1.5 text-sm text-rw-t1"/>
        </Field>
        <Field label="期次">
          <select value={phase} onChange={(e) => setPhase(e.target.value)}
            className="w-full bg-rw-surface border border-rw-border rounded-md px-3 py-1.5 text-sm text-rw-t1">
            <option>I</option><option>I/II</option><option>II</option>
            <option>III</option><option>IV</option>
          </select>
        </Field>
        <Field label="目标入组数（可选）">
          <input value={target} onChange={(e) => setTarget(e.target.value)} inputMode="numeric"
                 placeholder="e.g. 35"
                 className="w-full bg-rw-surface border border-rw-border rounded-md px-3 py-1.5 text-sm text-rw-t1"/>
        </Field>
        <Field label="主要终点（可选）">
          <input value={endpoint} onChange={(e) => setEndpoint(e.target.value)}
                 placeholder="e.g. ≥G3 放射性肺炎发生率"
                 className="w-full bg-rw-surface border border-rw-border rounded-md px-3 py-1.5 text-sm text-rw-t1"/>
        </Field>
        {err && <div className="text-xs text-rw-red">{err}</div>}
      </div>
      <footer className="px-5 py-3 border-t border-rw-border-soft flex justify-end gap-2">
        <button onClick={props.onCancel}
          className="px-3 py-1.5 rounded-md bg-rw-surface-2 border border-rw-border text-xs text-rw-t2">取消</button>
        <button onClick={submit} disabled={busy}
          className="px-3 py-1.5 rounded-md bg-rw-accent text-[#06252c] text-xs font-medium disabled:opacity-60">
          {busy ? '建中…' : '建立研究'}
        </button>
      </footer>
    </>
  );
}


// ── DOCX import + batch confirm flow ─────────────────────────────────

interface CritDef {
  id: string;
  text: string;
  // `kind` is decided by the LLM extractor (or hand-edited via the
  // "高级" toggle on the row). Medics interact via `confirmed`; the
  // kind chip is informational.
  kind: 'auto-rule' | 'auto-llm' | 'manual';
  // The medic's explicit ✓ on this row. Newly extracted items default
  // to `false`; the medic confirms each one (or all in bulk) before
  // POST /studies persists them. Matches the design's
  // "propose → confirm" anti-pattern: nothing the AI proposed becomes
  // policy without a medic action.
  confirmed?: boolean;
  rule_dsl?: string | null;
  llm_prompt?: string | null;
  evidence_sources?: string[] | null;
}
interface ScheduleDef {
  label: string;
  offset_days: number;
  assessments: string[];
  repeat_every_days?: number | null;
  repeat_until_days?: number | null;
}

function NewStudyDocxPane(props: {
  onCancel: () => void;
  onCreated: (studyId: string) => void;
}) {
  // 3 stages: pick file → parsing → batch confirm
  const [stage, setStage] = useState<'pick'|'parsing'|'confirm'>('pick');
  const [name, setName] = useState('');
  const [code, setCode] = useState('');
  const [phase, setPhase] = useState('II');
  const [target, setTarget] = useState('');
  const [endpoint, setEndpoint] = useState('');
  const [inclusion, setInclusion] = useState<CritDef[]>([]);
  const [exclusion, setExclusion] = useState<CritDef[]>([]);
  const [schedule,  setSchedule]  = useState<ScheduleDef[]>([]);
  const [summary,   setSummary]   = useState<string>('');
  const [notes,     setNotes]     = useState<string[]>([]);
  const [busy,      setBusy]      = useState(false);
  const [err,       setErr]       = useState<string|null>(null);

  const onFile = async (file: File) => {
    setErr(null);
    setStage('parsing');
    // F-docx-import-diag — three-step pipeline, each can fail
    // differently. Previously a single try/catch caught any error and
    // displayed only ``e.message`` ("Load failed"), so the medic had
    // no clue WHICH step broke. We now label each step explicitly
    // and prefix the surfaced error with the step name so it's
    // immediately actionable ("[2/3 建立草稿] 后端未返回 study_id",
    // "[1/3 上传文件] upload network error", etc.).
    const _classify = (step: string, e: unknown): string => {
      const msg = e instanceof Error ? e.message : String(e);
      // WebKit's signature opaque fetch failure. Means the TCP / TLS
      // layer never produced a response — sidecar isn't running, was
      // restarted mid-request, or the host doesn't resolve.
      if (msg === 'Load failed' || msg.includes('network error')) {
        return `[${step}] 后端无响应（sidecar 可能未启动或刚刚崩溃）— ${msg}`;
      }
      return `[${step}] ${msg}`;
    };

    // 1) upload
    let up;
    try {
      up = await api.uploadFile(file, file.name);
    } catch (e) {
      setErr(_classify('1/3 上传文件', e));
      setStage('pick');
      return;
    }

    // 2) create a placeholder study row so we have study_id for the
    // parse endpoint. The displayName/shortCode below are temporary
    // — we'll overwrite them from the LLM extraction below before
    // the medic ever sees them, unless the medic has already typed
    // something into the name/code field.
    const filenameStem = file.name.replace(/\.docx?$/i, '');
    let sid: string | undefined;
    try {
      const created = await api.createStudy({
        displayName: name || filenameStem,
        shortCode:   code || filenameStem.slice(0, 16),
        phase, targetN: null, primaryEndpoint: endpoint || undefined,
      });
      sid = (created as {study_id?: string}).study_id;
      if (!sid) throw new Error('后端未返回 study_id');
    } catch (e) {
      setErr(_classify('2/3 建立草稿', e));
      setStage('pick');
      return;
    }

    // 3) parse the .docx — slow (LLM round-trip, 10-30s). Wrapped
    // separately so a parse failure doesn't claim the upload or
    // the create-study step is broken. Must go through api-client
    // so the request hits the sidecar (http://localhost:8001/...)
    // and carries the bearer JWT from sessionStorage. A bare relative
    // ``fetch()`` would resolve to ``tauri://localhost/...`` in a
    // bundled .dmg and WebKit throws "The string did not match the
    // expected pattern".
    try {
      const r = await api.importStudyProtocol(sid, up.fileId);
      const draft = (r.draft || {}) as {
        study_title?: string;
        short_code?: string;
        phase?: string;
        primary_endpoint?: string;
        inclusion?: CritDef[]; exclusion?: CritDef[];
        schedule?: ScheduleDef[];
        protocol_summary?: string; notes?: string[];
      };
      // Auto-name from LLM-extracted study_title / short_code / phase
      // / primary_endpoint, but ONLY when the medic hasn't typed
      // something themselves. Showing "TEST_PROTOCOL_NSCLC_PD1" as the
      // study name (the filename stem) was wrong UX — the protocol
      // already has a real human-readable name inside.
      if (!name && (draft.study_title || '').trim()) {
        setName((draft.study_title || '').trim());
      }
      if (!code && (draft.short_code || '').trim()) {
        setCode((draft.short_code || '').trim());
      }
      if (!phase && (draft.phase || '').trim()) {
        setPhase((draft.phase || '').trim());
      }
      if (!endpoint && (draft.primary_endpoint || '').trim()) {
        setEndpoint((draft.primary_endpoint || '').trim());
      }
      setInclusion(draft.inclusion || []);
      setExclusion(draft.exclusion || []);
      setSchedule (draft.schedule  || []);
      setSummary  (draft.protocol_summary || '');
      setNotes    (draft.notes || []);
      // Stash study_id for confirm step
      (window as unknown as {__rwDraftStudyId?: string}).__rwDraftStudyId = sid;
      setStage('confirm');
    } catch (e) {
      setErr(_classify('3/3 解析协议', e));
      setStage('pick');
    }
  };

  const confirm = async () => {
    const sid = (window as unknown as {__rwDraftStudyId?: string}).__rwDraftStudyId;
    if (!sid) { setErr('内部错误：study_id 丢失'); return; }
    setBusy(true);
    try {
      await api.patchResearchStudy(sid, {
        display_name: name.trim() || undefined,
        short_code:   code.trim() || undefined,
        phase,
        target_n:     target ? Number(target) : undefined,
        primary_endpoint: endpoint || undefined,
        inclusion, exclusion, schedule,
        protocol_summary: summary || undefined,
        status: 'enrolling',
      });
      props.onCreated(sid);
    } catch (e) {
      setErr(String((e as Error).message || e));
    } finally { setBusy(false); }
  };

  return (
    <>
      <div className="px-5 py-4 overflow-y-auto flex-1 space-y-3">
        {stage === 'pick' && (
          <div className="space-y-3">
            <p className="text-sm text-rw-t2">
              上传研究方案 .docx — LLM 自动抽取入排标准、访视计划，然后由您逐项 review、confirm 入库。
            </p>
            <FilePicker
              accept=".docx,.doc"
              onPick={onFile}
              caption="拖入或点击选择协议文件 (.docx)"
            />
            {err && <div className="text-xs text-rw-red">{err}</div>}
            <div className="text-[11px] text-rw-t4">
              注：解析过程中会同步建好基础信息的 draft，下一步可调整。
            </div>
          </div>
        )}

        {stage === 'parsing' && (
          <div className="py-10 text-center">
            <div className="inline-block w-6 h-6 border-2 border-rw-accent border-t-transparent rounded-full animate-spin"/>
            <div className="mt-3 text-sm text-rw-t2">正在抽取协议规则…</div>
            <div className="mt-1 text-[11px] text-rw-t4">通常 10–30 秒</div>
          </div>
        )}

        {stage === 'confirm' && (
          <div className="space-y-4">
            <div className="text-xs text-rw-t3 italic">
              D7：以下是 LLM 抽取的草案。<strong>整张表必须有医生显式 confirm 动作才入库。</strong>
              逐条修改 kind、规则、或删除条目；末尾点 “建立研究” 一次性 commit。
            </div>

            <details open className="rounded-md border border-rw-border bg-rw-surface">
              <summary className="px-3 py-2 text-sm text-rw-t1 cursor-pointer flex items-center justify-between">
                <span>基础信息</span>
                <span className="text-[11px] text-rw-t3">必填</span>
              </summary>
              <div className="p-3 space-y-2 border-t border-rw-border-soft">
                <Field label="显示名">
                  <input value={name} onChange={e => setName(e.target.value)}
                    className="w-full bg-rw-surface-2 border border-rw-border rounded-md px-3 py-1.5 text-sm text-rw-t1"/>
                </Field>
                <Field label="简称">
                  <input value={code} onChange={e => setCode(e.target.value)}
                    className="w-full bg-rw-surface-2 border border-rw-border rounded-md px-3 py-1.5 text-sm text-rw-t1"/>
                </Field>
                <div className="grid grid-cols-3 gap-2">
                  <Field label="期次">
                    <select value={phase} onChange={e => setPhase(e.target.value)}
                      className="w-full bg-rw-surface-2 border border-rw-border rounded-md px-2 py-1.5 text-sm text-rw-t1">
                      <option>I</option><option>I/II</option><option>II</option>
                      <option>III</option><option>IV</option>
                    </select>
                  </Field>
                  <Field label="目标入组">
                    <input value={target} onChange={e => setTarget(e.target.value)}
                      inputMode="numeric"
                      className="w-full bg-rw-surface-2 border border-rw-border rounded-md px-2 py-1.5 text-sm text-rw-t1"/>
                  </Field>
                  <Field label="主要终点">
                    <input value={endpoint} onChange={e => setEndpoint(e.target.value)}
                      className="w-full bg-rw-surface-2 border border-rw-border rounded-md px-2 py-1.5 text-sm text-rw-t1"/>
                  </Field>
                </div>
                {summary && (
                  <Field label="协议摘要（LLM）">
                    <textarea value={summary} onChange={e => setSummary(e.target.value)}
                      rows={3}
                      className="w-full bg-rw-surface-2 border border-rw-border rounded-md px-2 py-1.5 text-xs text-rw-t2"/>
                  </Field>
                )}
              </div>
            </details>

            <CritGroup title="入选标准" tone="green"
              items={inclusion} setItems={setInclusion}/>
            <CritGroup title="排除标准" tone="red"
              items={exclusion} setItems={setExclusion}/>
            <ScheduleGroup items={schedule} setItems={setSchedule}/>

            {notes.length > 0 && (
              <div className="text-[11px] text-rw-orange">
                ⚠ {notes.join(' · ')}
              </div>
            )}

            {err && <div className="text-xs text-rw-red">{err}</div>}
          </div>
        )}
      </div>

      <footer className="px-5 py-3 border-t border-rw-border-soft flex justify-end gap-2">
        <button onClick={props.onCancel}
          className="px-3 py-1.5 rounded-md bg-rw-surface-2 border border-rw-border text-xs text-rw-t2">
          取消
        </button>
        {stage === 'confirm' && (
          <button onClick={confirm} disabled={busy}
            className="px-3 py-1.5 rounded-md bg-rw-accent text-[#06252c] text-xs font-medium disabled:opacity-60">
            {busy ? 'commit 中…' : '建立研究 · 写入 ' +
              `${inclusion.length} 入选 / ${exclusion.length} 排除 / ${schedule.length} 访视`}
          </button>
        )}
      </footer>
    </>
  );
}


function FilePicker(props: {
  accept: string;
  onPick: (file: File) => void;
  caption: string;
}) {
  const [drag, setDrag] = useState(false);
  return (
    <label
      onDragOver={(e) => { e.preventDefault(); setDrag(true); }}
      onDragLeave={() => setDrag(false)}
      onDrop={(e) => {
        e.preventDefault(); setDrag(false);
        const f = e.dataTransfer.files?.[0];
        if (f) props.onPick(f);
      }}
      className={`block w-full rounded-lg border-2 border-dashed
                  ${drag ? 'border-rw-accent bg-rw-accent-bg/40' : 'border-rw-border bg-rw-surface'}
                  px-6 py-8 text-center cursor-pointer transition`}>
      <div className="text-rw-accent text-2xl mb-1">↑</div>
      <div className="text-sm text-rw-t1">{props.caption}</div>
      <input type="file" accept={props.accept} className="hidden"
        onChange={(e) => {
          const f = e.target.files?.[0];
          if (f) props.onPick(f);
        }}/>
    </label>
  );
}


function CritGroup(props: {
  title: string;
  tone: 'green' | 'red';
  items: CritDef[];
  setItems: (v: CritDef[]) => void;
}) {
  const update = (i: number, patch: Partial<CritDef>) => {
    const next = props.items.slice();
    next[i] = { ...next[i], ...patch };
    props.setItems(next);
  };
  const remove = (i: number) => {
    const next = props.items.slice();
    next.splice(i, 1);
    props.setItems(next);
  };
  const add = () => {
    // Newly hand-added items default to `manual` because the medic
    // hasn't given us any rule text. They also start `confirmed=true`
    // — if the medic typed it in themselves, they're already confirming.
    props.setItems([
      ...props.items,
      { id: `c-${Math.random().toString(36).slice(2, 8)}`, text: '',
        kind: 'manual', confirmed: true },
    ]);
  };
  const allConfirm = () => {
    props.setItems(props.items.map((c) => ({ ...c, confirmed: true })));
  };
  const pendingCount = props.items.filter((c) => !c.confirmed).length;

  return (
    <details open className="rounded-md border border-rw-border bg-rw-surface">
      <summary className="px-3 py-2 text-sm text-rw-t1 cursor-pointer flex items-center justify-between">
        <span>
          {props.title}{' '}
          <span className="text-rw-t3 ml-1">({props.items.length})</span>
          {pendingCount > 0 && (
            <span className="ml-2 text-[11px] font-rw-mono text-rw-orange">
              {pendingCount} 待确认
            </span>
          )}
        </span>
        <span className="flex items-center gap-2">
          {pendingCount > 0 && (
            <button onClick={(e) => { e.preventDefault(); allConfirm(); }}
              className="text-[11px] text-rw-accent hover:underline">
              全部确认
            </button>
          )}
          <button onClick={(e) => { e.preventDefault(); add(); }}
            className="text-[11px] text-rw-accent hover:underline">+ 添加</button>
        </span>
      </summary>
      <div className="p-3 space-y-2 border-t border-rw-border-soft">
        {props.items.length === 0 && (
          <div className="text-[11px] text-rw-t4 italic">无 — 点 “+ 添加” 手动新增</div>
        )}
        {props.items.map((c, i) => (
          <CriterionRow key={c.id || i} item={c} index={i}
                        onUpdate={(patch) => update(i, patch)}
                        onRemove={() => remove(i)} />
        ))}
      </div>
    </details>
  );
}

// One inclusion / exclusion row.
//
// UX contract:
//   - Primary surface = the criterion text (read-only display, click
//     to edit inline) + a "✓ 确认" toggle on the right.
//   - `kind` (auto-rule / auto-llm / manual) is a small grey chip
//     beside the text. It's the LLM extractor's decision and the
//     medic should NOT have to touch it most of the time.
//   - "高级" link reveals the kind editor + rule_dsl / llm_prompt
//     inputs for the rare cases the medic wants to override.
//
// Why this changed: showing the medic a `auto-rule / auto-llm /
// manual` dropdown leaks the implementation. Medics think in
// "condition is right / wrong / needs fix", not in
// "is this evaluable by SQL or LLM?".
function CriterionRow({item: c, onUpdate, onRemove}: {
  item: CritDef;
  index: number;
  onUpdate: (patch: Partial<CritDef>) => void;
  onRemove: () => void;
}) {
  const [advanced, setAdvanced] = useState(false);
  const kindColour =
    c.kind === 'auto-rule' ? 'text-rw-accent border-rw-accent-bd' :
    c.kind === 'auto-llm'  ? 'text-rw-orange border-rw-orange' :
                             'text-rw-t3 border-rw-border';
  const confirmed = !!c.confirmed;
  return (
    <div className={`rounded border p-2 space-y-1.5 transition
                     ${confirmed
                       ? 'border-rw-green/40 bg-rw-green-bg/30'
                       : 'border-rw-border-soft bg-rw-surface-2'}`}>
      <div className="flex items-start gap-2">
        <button
          onClick={() => onUpdate({ confirmed: !confirmed })}
          title={confirmed ? '取消确认' : '确认这一条'}
          className={`shrink-0 mt-0.5 w-5 h-5 rounded border flex items-center justify-center
                      text-[11px] font-bold transition
                      ${confirmed
                        ? 'bg-rw-green border-rw-green text-[#06252c]'
                        : 'border-rw-border hover:border-rw-accent-bd text-transparent hover:text-rw-t3'}`}
        >
          ✓
        </button>
        <input value={c.text}
          onChange={e => onUpdate({ text: e.target.value, confirmed: false })}
          placeholder="条目原文"
          className="flex-1 bg-transparent border-b border-rw-border-soft px-1 py-0.5
                     text-[12px] text-rw-t1 focus:outline-none focus:border-rw-accent-bd"/>
        <span className={`shrink-0 px-1.5 py-0.5 rounded border text-[10px] font-rw-mono ${kindColour}`}
              title={
                c.kind === 'auto-rule' ? '机器规则自动判定(不需 LLM)' :
                c.kind === 'auto-llm'  ? 'LLM 读病历自动判定' :
                                          '医生每次手动确认'
              }>
          {c.kind}
        </span>
        <button onClick={() => setAdvanced(!advanced)}
          className="text-rw-t4 hover:text-rw-accent text-[10px]"
          title="改判定方式 / 规则表达式">
          {advanced ? '收起' : '高级'}
        </button>
        <button onClick={onRemove}
          className="text-rw-t4 hover:text-rw-red text-[11px]"
          title="删除这一条">✕</button>
      </div>
      {advanced && (
        <div className="pl-7 space-y-1.5">
          <div className="flex items-center gap-2 text-[11px]">
            <span className="text-rw-t3">判定方式:</span>
            <select value={c.kind}
              onChange={e => onUpdate({ kind: e.target.value as CritDef['kind'] })}
              className="bg-rw-surface border border-rw-border rounded px-1.5 py-0.5
                         text-[11px] text-rw-t2 font-rw-mono">
              <option value="auto-rule">auto-rule(机器规则)</option>
              <option value="auto-llm">auto-llm(LLM 判定)</option>
              <option value="manual">manual(医生手动)</option>
            </select>
          </div>
          {c.kind === 'auto-rule' && (
            <input value={c.rule_dsl || ''}
              onChange={e => onUpdate({ rule_dsl: e.target.value })}
              placeholder="rule_dsl, e.g. age BETWEEN 18 AND 70"
              className="w-full bg-transparent border-b border-rw-border-soft px-1 py-0.5
                         text-[11px] text-rw-accent font-rw-mono focus:outline-none focus:border-rw-accent-bd"/>
          )}
          {c.kind === 'auto-llm' && (
            <input value={c.llm_prompt || ''}
              onChange={e => onUpdate({ llm_prompt: e.target.value })}
              placeholder="llm_prompt — 描述 LLM 该如何判断这条"
              className="w-full bg-transparent border-b border-rw-border-soft px-1 py-0.5
                         text-[11px] text-rw-orange focus:outline-none focus:border-rw-accent-bd"/>
          )}
        </div>
      )}
    </div>
  );
}


function ScheduleGroup(props: {
  items: ScheduleDef[];
  setItems: (v: ScheduleDef[]) => void;
}) {
  const update = (i: number, patch: Partial<ScheduleDef>) => {
    const next = props.items.slice();
    next[i] = { ...next[i], ...patch };
    props.setItems(next);
  };
  const remove = (i: number) => {
    const next = props.items.slice();
    next.splice(i, 1);
    props.setItems(next);
  };
  const add = () => {
    props.setItems([
      ...props.items,
      { label: 'fu_new', offset_days: 90, assessments: ['lab_panel'] },
    ]);
  };
  return (
    <details open className="rounded-md border border-rw-border bg-rw-surface">
      <summary className="px-3 py-2 text-sm text-rw-t1 cursor-pointer flex items-center justify-between">
        <span>访视计划 <span className="text-rw-t3 ml-1">({props.items.length})</span></span>
        <button onClick={(e) => { e.preventDefault(); add(); }}
          className="text-[11px] text-rw-accent hover:underline">+ 添加</button>
      </summary>
      <div className="p-3 space-y-2 border-t border-rw-border-soft">
        {props.items.length === 0 && (
          <div className="text-[11px] text-rw-t4 italic">无访视</div>
        )}
        {props.items.map((v, i) => (
          <div key={i}
               className="grid grid-cols-[1fr_80px_2fr_24px] gap-2 items-center
                          bg-rw-surface-2 border border-rw-border-soft rounded p-2">
            <input value={v.label} onChange={e => update(i, { label: e.target.value })}
              placeholder="label (baseline / fu_3m …)"
              className="bg-transparent border-b border-rw-border-soft px-1 py-0.5 text-[12px] text-rw-t1"/>
            <input value={String(v.offset_days)}
              onChange={e => update(i, { offset_days: Number(e.target.value) || 0 })}
              inputMode="numeric"
              className="bg-transparent border-b border-rw-border-soft px-1 py-0.5 text-[12px] text-rw-accent font-rw-mono"/>
            <input value={(v.assessments || []).join(',')}
              onChange={e => update(i, { assessments: e.target.value.split(',').map(s => s.trim()).filter(Boolean) })}
              placeholder="imaging_ct, lab_panel, ecog…"
              className="bg-transparent border-b border-rw-border-soft px-1 py-0.5 text-[11px] text-rw-t2 font-rw-mono"/>
            <button onClick={() => remove(i)}
              className="text-rw-t4 hover:text-rw-red text-[11px]">✕</button>
          </div>
        ))}
      </div>
    </details>
  );
}

function Field({label, children}: {label: string; children: React.ReactNode}) {
  return (
    <div>
      <label className="block text-[11px] text-rw-t3 mb-0.5">{label}</label>
      {children}
    </div>
  );
}


// ════════════════════════════════════════════════════════════════════
//  Generic primitives
// ════════════════════════════════════════════════════════════════════

function KPICard(props: {
  label: string; value: string | number; suffix?: string; sub?: string;
  tone?: 'default' | 'accent' | 'orange' | 'red';
}) {
  const toneStyles = {
    default: 'text-rw-t1',
    accent:  'text-rw-accent',
    orange:  'text-rw-orange',
    red:     'text-rw-red',
  }[props.tone || 'default'];
  return (
    <div className="rounded-lg border border-rw-border bg-rw-surface px-4 py-3.5">
      <div className="text-[10px] uppercase tracking-wider text-rw-t3 font-rw-mono">{props.label}</div>
      <div className={`mt-1 text-[28px] font-semibold leading-none ${toneStyles}`}>
        {props.value}
        {props.suffix && <span className="ml-1 text-base font-normal text-rw-t3">{props.suffix}</span>}
      </div>
      {props.sub && <div className="mt-1 text-[10px] text-rw-t4 font-rw-mono">{props.sub}</div>}
    </div>
  );
}

function Card(props: {title: string; right?: string; children: React.ReactNode}) {
  return (
    <section className="rounded-lg border border-rw-border bg-rw-surface p-4">
      <div className="flex items-baseline justify-between mb-3">
        <h3 className="text-sm font-medium text-rw-t1">{props.title}</h3>
        {props.right && <span className="text-[11px] font-rw-mono text-rw-t3">{props.right}</span>}
      </div>
      {props.children}
    </section>
  );
}

function Pill(props: {tone: 'accent'|'green'|'orange'|'red'; children: React.ReactNode}) {
  const cls = {
    accent: 'bg-rw-accent-bg text-rw-accent border-rw-accent-bd',
    green:  'bg-rw-green-bg text-rw-green border-rw-green/30',
    orange: 'bg-rw-orange-bg text-rw-orange border-rw-orange/30',
    red:    'bg-rw-red-bg text-rw-red border-rw-red/30',
  }[props.tone];
  return (
    <span className={`px-1.5 py-0.5 rounded text-[10px] font-rw-mono border uppercase tracking-wide ${cls}`}>
      {props.children}
    </span>
  );
}

function ActivityFeed(props: {items: Array<{when: string; text: string; kind: string}>}) {
  return (
    <ol className="space-y-2.5">
      {props.items.map((it, i) => (
        <li key={i} className="grid grid-cols-[60px_1fr] gap-3 text-[12px]">
          <span className="font-rw-mono text-rw-t4">{it.when}</span>
          <span className="text-rw-t2">{it.text}</span>
        </li>
      ))}
    </ol>
  );
}


// ════════════════════════════════════════════════════════════════════
//  Empty state
// ════════════════════════════════════════════════════════════════════

function EmptyState() {
  // Showing a curl command in a clinical-facing UI is absurd — the
  // medic isn't going to open a terminal. Replace with a real button
  // that calls the same endpoint. Refresh the studies list on success.
  const refreshStudies = useAppState((s) => s.refreshStudies);
  const setActiveStudyId = useAppState((s) => s.setActiveStudyId);
  const studies = useAppState((s) => s.studies);
  const [busy, setBusy] = useState(false);
  const [err,  setErr]  = useState<string | null>(null);

  async function install() {
    setBusy(true);
    setErr(null);
    try {
      const r = await api.installResearchStarters();
      await refreshStudies();
      // Auto-select the first installed study so the medic doesn't
      // land on an empty pane after the click.
      if (r.installed && r.installed[0]) setActiveStudyId(r.installed[0]);
    } catch (e) {
      setErr((e as Error).message || String(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="h-full flex flex-col">
      <div className="flex-1 flex items-center justify-center text-center p-12">
        <div className="max-w-md text-rw-t3">
          <div className="text-5xl mb-3 text-rw-accent">⌬</div>
          <h3 className="text-lg font-semibold text-rw-t1 mb-1">Research Workspace</h3>
          <p className="text-sm">
            {studies.length === 0
              ? '从左侧"+ 新建研究"开始 — 或一键导入 3 个真实研究协议作为起步样本。'
              : '从左侧选一个研究查看详情,或在下方对话框跨所有研究提问。'}
          </p>
          {studies.length === 0 && (
            <div className="mt-5 flex flex-col items-center gap-2">
              <button onClick={install} disabled={busy}
                className="px-4 py-2 rounded-md bg-rw-accent text-[#06252c] text-sm font-medium
                           hover:opacity-90 disabled:opacity-50 transition">
                {busy ? '正在安装样例协议…' : '安装样例协议(3 个真实研究)'}
              </button>
              {err && (
                <div className="text-xs text-rw-red mt-1">安装失败:{err}</div>
              )}
              <div className="text-[11px] text-rw-t4 mt-1">
                包含:Hybrid RT NSCLC · ES-SCLC 全残留病灶大分割 · 8Gy 免疫点火
              </div>
            </div>
          )}
        </div>
      </div>
      <CrossResearchChat />
    </div>
  );
}

// Workspace-level cross-research chat. Lives in EmptyState (i.e. when
// no specific study is selected) and lets the medic ask questions that
// span all studies — "across my 3 trials, who's eligible for more than
// one?", "show me G3+ AE across all studies last month", etc.
//
// Implementation note: the backend's ChatScope currently knows
// `patient` / `research` / `cross_patient`. We use `research` with no
// study_id — the server-side retrieval treats that as "all enrolled
// patients across all of this user's studies" via retrieval_tiers'
// research scope branch.
function CrossResearchChat() {
  // F-unified-chat-files — workspace-wide cross-research library.
  const crossFiles = useChatFiles('cross_research', '__workspace__');
  const fileMap: Record<string, FileChipRef> = {};
  for (const f of crossFiles.files) {
    fileMap[f.fIdToken] = {
      fileId: f.fileId, name: f.name,
      textExtractionStatus: f.textExtractionStatus,
    };
  }
  const [input,    setInput]    = useState('');
  const [messages, setMessages] = useState<Array<{
    role: 'user' | 'agent';
    text: string;
    streaming?: boolean;
    /** F-history-attachments — preserve file names across history reload */
    attachedFileNames?: string[];
  }>>([]);
  const [busy, setBusy] = useState(false);
  // Attachment chip strip — same UX contract as the patient chat
  // (modes.tsx PatientMode) and per-study Research ChatTab. Each chip
  // shows a placeholder immediately; ✓ once the upload settles.
  const [attachments, setAttachments] = useState<ChatAttachment[]>([]);

  // Per-user, persistent across study selections. Sticking a fixed
  // session id means the cross-research chat history is recoverable
  // (and SOAPs / earlier turns inform the next answer).
  const sessionId = 'research-workspace-cross';

  // Hydrate from history once on mount. Same pattern as ChatTab and
  // EncounterMode — without this the medic loses every prior cross-
  // research thread when the EmptyState unmounts and remounts.
  useEffect(() => {
    let cancelled = false;
    api.listSessionMessages(sessionId, 200).then(
      (rows) => {
        if (cancelled) return;
        setMessages(rows.map((r) => ({
          role: r.role === 'agent' ? 'agent' : 'user',
          text: r.text,
          attachedFileNames: (r.attachments ?? []).map((a) => a.name),
        })));
      },
      () => { /* history optional */ },
    );
    return () => { cancelled = true; };
  }, [sessionId]);

  // ── Resizable panel height ──────────────────────────────────────
  // Medics complained the cross-research chat at the bottom of
  // EmptyState felt cramped — only ~260px tall, with a hero block
  // hogging the rest of the page. The chat IS the main work surface
  // for "patient → trial matching", so it should be growable.
  // We persist the chosen height in localStorage so the resize
  // sticks across mounts (returning to EmptyState shouldn't reset
  // back to default).
  const _PANEL_HEIGHT_KEY = 'nexus.cross-research.panel-height';
  // F-crc-panel-viewport — default 360 was too tall on a 13" laptop
  // (typical 800px viewport): hero block + panel exceeded the parent
  // <main>'s viewport height, and since <main> has overflow-hidden
  // the panel's bottom (where the composer lives) was clipped off
  // screen. 280 fits on 14" / 13" without scrolling; medics with
  // bigger monitors can drag taller and the localStorage remembers it.
  const _PANEL_HEIGHT_DEFAULT = 280;
  const _PANEL_HEIGHT_MIN = 180;
  // Same reasoning for the ceiling — 800 was bigger than most
  // laptop viewports. Cap dynamically below at render time too.
  const _PANEL_HEIGHT_MAX = 600;
  const [panelHeight, setPanelHeight] = useState<number>(() => {
    try {
      const raw = localStorage.getItem(_PANEL_HEIGHT_KEY);
      const v = raw ? parseInt(raw, 10) : NaN;
      if (Number.isFinite(v) && v >= _PANEL_HEIGHT_MIN && v <= _PANEL_HEIGHT_MAX) {
        return v;
      }
    } catch { /* localStorage unavailable */ }
    return _PANEL_HEIGHT_DEFAULT;
  });

  // F-crc-panel-viewport — track the live viewport height so the
  // panel can never exceed (viewport - hero_min). When the window
  // shrinks (split-screen / external monitor disconnected / etc.)
  // we shrink the panel's effective height with it, keeping the
  // composer visible. ``panelHeight`` (the medic-chosen drag value)
  // stays untouched in localStorage; what we render is
  // ``min(panelHeight, viewportCap)``.
  const [viewportH, setViewportH] = useState<number>(
    () => (typeof window !== 'undefined' ? window.innerHeight : 800),
  );
  useEffect(() => {
    if (typeof window === 'undefined') return;
    const onResize = () => setViewportH(window.innerHeight);
    window.addEventListener('resize', onResize);
    return () => window.removeEventListener('resize', onResize);
  }, []);
  // Reserve ~220px for the hero block above (icon + heading + paragraph
  // + a little breathing room). That's enough for the hero to stay
  // readable; below that the panel shrinks first, then the hero.
  const _HERO_RESERVE = 220;
  const effectivePanelHeight = Math.max(
    _PANEL_HEIGHT_MIN,
    Math.min(panelHeight, viewportH - _HERO_RESERVE),
  );

  function onResizeMouseDown(e: React.MouseEvent<HTMLDivElement>) {
    // Vertical drag: anchor on the current y, then on every mousemove
    // compute height = (start_height + (start_y - current_y)) clamped.
    // We use document-level listeners so the drag survives if the
    // cursor leaves the handle div mid-drag.
    e.preventDefault();
    const startY = e.clientY;
    const startH = panelHeight;
    const onMove = (ev: MouseEvent) => {
      const dy = startY - ev.clientY;   // dragging up → positive
      // Clamp by both the static max AND the live viewport cap so the
      // medic can't drag the composer off-screen on a tight viewport.
      const liveMax = Math.min(
        _PANEL_HEIGHT_MAX,
        Math.max(_PANEL_HEIGHT_MIN, viewportH - _HERO_RESERVE),
      );
      const next = Math.max(
        _PANEL_HEIGHT_MIN,
        Math.min(liveMax, startH + dy),
      );
      setPanelHeight(next);
    };
    const onUp = () => {
      document.removeEventListener('mousemove', onMove);
      document.removeEventListener('mouseup',   onUp);
      // Persist final value once the medic releases the mouse —
      // avoids hammering localStorage during the drag.
      try { localStorage.setItem(_PANEL_HEIGHT_KEY, String(panelHeight)); }
      catch { /* ignore */ }
    };
    document.addEventListener('mousemove', onMove);
    document.addEventListener('mouseup',   onUp);
  }

  // Persist on every height change (covers the localStorage write
  // missed by the onUp closure capturing stale panelHeight).
  useEffect(() => {
    try { localStorage.setItem(_PANEL_HEIGHT_KEY, String(panelHeight)); }
    catch { /* ignore */ }
  }, [panelHeight]);

  async function uploadOne(file: File): Promise<string | null> {
    try {
      // F-unified-chat-files — cross-research workspace library
      // (shared across all cross-research sessions, no per-study
      // partitioning). Sentinel scope_ref '__workspace__' keys this.
      const r = await api.uploadFile(file, file.name, {
        libScopeKind: 'cross_research',
        libScopeRef:  '__workspace__',
      });
      try { crossFiles.refresh(); } catch { /* hook not yet ready */ }
      return r.fileId;
    } catch (e) {
      console.warn('cross-research upload failed', e);
      return null;
    }
  }

  function acceptFiles(files: FileList | File[]) {
    const arr = Array.from(files);
    if (arr.length === 0) return;
    const placeholders: ChatAttachment[] = arr.map(_makeChatAttachment);
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

  function onPaste(e: React.ClipboardEvent<HTMLInputElement>) {
    const files = e.clipboardData?.files;
    if (files && files.length > 0) {
      e.preventDefault();
      acceptFiles(files);
    }
  }

  function onDrop(e: React.DragEvent<HTMLDivElement>) {
    e.preventDefault();
    if (e.dataTransfer?.files?.length) acceptFiles(e.dataTransfer.files);
  }

  function removeAttachment(key: string) {
    // Revoke any blob URL the thumbnail was using before dropping the
    // attachment — otherwise every paste-then-remove leaks a few MB
    // of bitmap data in the WebView's memory.
    setAttachments((prev) => {
      const found = prev.find((a) => a.key === key);
      if (found?.previewUrl) {
        try { URL.revokeObjectURL(found.previewUrl); } catch { /* ignore */ }
      }
      return prev.filter((a) => a.key !== key);
    });
  }

  async function send() {
    const text = input.trim();
    if ((!text && attachments.length === 0) || busy) return;
    // Wait for in-flight uploads to settle so the file_ids we send are
    // real. Matches the Research ChatTab + PatientMode pattern.
    const pending = attachments.filter((a) => a.fileId === null && !a.failed);
    if (pending.length > 0) {
      console.info(`cross-research chat: waiting for ${pending.length} upload(s)`);
      return;
    }
    const fileIds = attachments
      .filter((a) => a.fileId)
      .map((a) => a.fileId as string);
    const stagedNames = attachments.map((a) => a.name);

    setBusy(true);
    setInput('');
    setAttachments([]);
    setMessages((m) => [
      ...m,
      // F-history-attachments — see ChatTab note.
      { role: 'user', text, attachedFileNames: stagedNames },
      { role: 'agent', text: '', streaming: true },
    ]);
    try {
      for await (const chunk of api.sendChat(text, sessionId, null, fileIds, {
        kind:  'research',
        // Intentionally no studyId — that's what makes this "cross".
        focusPatientHash: null,
      })) {
        if (chunk.type === 'final_answer_chunk' && chunk.text) {
          setMessages((m) => {
            const next = m.slice();
            const last = next[next.length - 1];
            if (last && last.role === 'agent') {
              last.text += chunk.text;
            }
            return next;
          });
        }
      }
    } catch (e) {
      setMessages((m) => {
        const next = m.slice();
        const last = next[next.length - 1];
        if (last && last.role === 'agent') {
          last.text = `(出错：${(e as Error).message || String(e)})`;
        }
        return next;
      });
    } finally {
      setBusy(false);
      setMessages((m) => {
        const next = m.slice();
        const last = next[next.length - 1];
        if (last) last.streaming = false;
        return next;
      });
    }
  }

  return (
    <div
      className="border-t border-rw-border bg-rw-bg-deep flex flex-col shrink-0"
      style={{ height: `${effectivePanelHeight}px` }}
      onDrop={onDrop}
      onDragOver={(e) => e.preventDefault()}
    >
      {/* Drag handle — pulls up to grow, down to shrink. The
          ``cursor-row-resize`` + the centred grip strip make the
          affordance obvious without a heavyweight drag library. */}
      <div
        onMouseDown={onResizeMouseDown}
        role="separator"
        aria-orientation="horizontal"
        aria-valuenow={effectivePanelHeight}
        aria-valuemin={_PANEL_HEIGHT_MIN}
        aria-valuemax={_PANEL_HEIGHT_MAX}
        title="拖动调整高度"
        className="group h-2 -mt-1 flex items-center justify-center
                   cursor-row-resize hover:bg-rw-accent/10 transition"
      >
        <span className="w-12 h-[3px] rounded-full bg-rw-border
                         group-hover:bg-rw-accent transition" />
      </div>
      {/* F-crc-composer-pin — split the panel into a SCROLLING message
          area + a STATIC composer block. Previously the composer lived
          inside the scroll container, so as messages piled up the
          composer got pushed below the panel's visible bottom and the
          medic could no longer reach the input/send button (especially
          with the 360px default height). Now only messages scroll;
          composer stays glued to the bottom of the panel. */}
      <div className="flex-shrink-0 px-6 pt-2">
       <div className="max-w-3xl mx-auto flex items-center gap-2">
          <span className="text-[10px] tracking-[0.18em] uppercase text-rw-t4 font-rw-mono">
            Cross-research
          </span>
          <span className="text-xs text-rw-t3">跨所有研究提问</span>
          <div className="ml-auto flex items-center gap-3">
            <TakeawaysButton
              scopeKind="cross_research"
              scopeRef="__cross_research__"
              tone="rw"
            />
            <span className="text-[10px] font-rw-mono text-rw-t4">
              {effectivePanelHeight}px · 拖动顶部调节
            </span>
          </div>
       </div>
      </div>
      <div className="flex-1 min-h-0 overflow-y-auto px-6 pt-2 pb-1">
       <div className="max-w-3xl mx-auto">
        {messages.length > 0 && (
          <div className="space-y-3 mb-3 pr-2">
            {messages.map((m, i) => (
              <div key={i}>
                <div className={`flex ${m.role === 'user' ? 'justify-end' : 'justify-start'}`}>
                  <div className={`max-w-[80%] rounded-lg px-3 py-2 text-sm
                    ${m.role === 'user'
                      ? 'bg-rw-accent text-[#06252c]'
                      : 'bg-rw-surface text-rw-t1 border border-rw-border'}`}>
                    {/* F-thinking-uniform: same pattern as ChatTab —
                        text body + inline cursor + footer below. */}
                    {m.text && (
                      <ChatMarkdown text={m.text}
                                    tone={m.role === 'user' ? 'inverse' : 'agent'}
                                    fileMap={fileMap} />
                    )}
                    {m.streaming && m.text && <StreamingCursor tone="rw" />}
                  </div>
                </div>
                {m.role === 'agent' && (
                  <StreamingFooter
                    streaming={m.streaming}
                    hasText={!!(m.text && m.text.length > 0)}
                    tone="rw"
                  />
                )}
                {/* F-history-attachments — render attached file chips
                    on user turns so the medic can see which files
                    they sent, both in fresh sessions AND after
                    history reload. */}
                {m.role === 'user' && m.attachedFileNames && m.attachedFileNames.length > 0 && (
                  <div className="mt-1 flex flex-wrap gap-1 justify-end">
                    {m.attachedFileNames.map((name, fi) => (
                      <span
                        key={fi}
                        className="rounded-sm border border-rw-border bg-rw-surface px-1.5 py-0.5 text-[10px] text-rw-t3"
                      >
                        📎 {name}
                      </span>
                    ))}
                  </div>
                )}
              </div>
            ))}
          </div>
        )}
       </div>
      </div>
      {/* Composer sticks at the bottom of the panel regardless of how
          many messages are above. A thin separator line keeps it
          visually distinct from the scrolling pane. */}
      <div className="flex-shrink-0 border-t border-rw-border/50 bg-rw-bg-deep px-6 pt-2 pb-3">
       <div className="max-w-3xl mx-auto">
        {/* F-unified-chat-files — workspace-wide cross-research lib */}
        <div className="mb-2">
          <ChatFileChipStrip
            scopeKind="cross_research"
            scopeRef="__workspace__"
            controller={crossFiles}
            tone="rw"
          />
        </div>
        <AttachmentChipsRow
          attachments={attachments}
          onRemove={removeAttachment}
        />
        <div className="flex items-center gap-2 rounded-lg border border-rw-border
                        bg-rw-surface px-3 py-2">
          <label className="cursor-pointer text-rw-t3 hover:text-rw-accent text-base leading-none"
                 title="附件(也可粘贴/拖拽)">
            📎
            <input type="file" multiple hidden
              onChange={(e) => {
                if (e.target.files) acceptFiles(e.target.files);
                e.target.value = '';
              }}
            />
          </label>
          <input value={input}
            onChange={(e) => setInput(e.target.value)}
            onPaste={onPaste}
            onKeyDown={(e) => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send(); }}}
            disabled={busy}
            placeholder="跨所有研究提问 — 例如「这周哪些研究有新的 ≥G3 AE?」/ 「哪位患者命中多个研究?」(可粘贴/拖拽文件)"
            className="flex-1 bg-transparent text-sm text-rw-t1 placeholder:text-rw-t4 outline-none"
          />
          <button onClick={send}
                  disabled={busy || (!input.trim() && attachments.length === 0)}
            className="px-3 py-1 rounded-md bg-rw-accent text-[#06252c] text-xs font-medium
                       disabled:opacity-60">
            {busy ? '…' : '发送'}
          </button>
        </div>
       </div>
      </div>
    </div>
  );
}
