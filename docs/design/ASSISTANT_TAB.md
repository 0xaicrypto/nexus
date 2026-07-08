# 助理 Tab — 设计方案

**Status:** Draft (2026-06-28) · **Author:** JZ + AI pair
**Replaces:** N/A · **Depends on:** F10 (session_takeaway), F-roster-active-only, F-roster-archive-filter, F26 (multi-identity)

---

## 1. 目标

把现有"跨研究 chat"从 Research Workspace 的 EmptyState 里**抽出来,提升为一个顶层 tab**(`助理 ✨`),平行于`患者`和`研究`。这个 tab 是医生的**个人长期助理**:

- **跨域可见**:能同时看到该用户所有**未归档**的患者 + **未归档**的研究。
- **持续演进**:每轮聊完后蒸馏 takeaway、积累跨会话事实、依据反馈修正风格,
  下次再聊时这些都自动进 system prompt。
- **独立会话**:跟现有跨研究 chat 不冲突——两边都保留,但用不同的 sessionId
  和 scope,避免上下文互染。

## 2. 用户旅程

```
医生上班 → 顶部 tab "助理 ✨" → 看到一组上次聊到的话题
       → 问 "上周哪些患者达到了 mPFS"
       → 助理回答(带 [N12] 类引用),底下有 👍 / 👎 + "教助理"
       → 医生 👎 一条"太长"
       → 后台抽出"医生不喜欢长 bullet"放进 assistant_facts(style)
       → 下次聊天时 system prompt 包含 "Avoid: long bullet lists (medic flagged 1x)"
```

## 3. 范围 (in / out)

### In
- 新顶层 tab `助理` 平行 `患者` / `研究`
- 一套带 sidebar 的 chat 界面(多 session)
- 后端 `ChatScope='assistant'` 路径
- 三层演进机制(下面 §6)

### Out (Phase 2+)
- 助理跨设备同步(等 F25d 落地)
- 助理被动主动通知(早间简报)——可作为 scheduled-task 接入
- 跨用户共享 facts(企业模式,USER_MANAGEMENT.md §10)

---

## 4. UI

### 4.1 顶部 Tab Bar

```
WORKSPACE: 患者 | 研究 | 助理 ✨
```

`Workspace` enum 增加 `'assistant'`(store.ts)。点击该 tab 进入
`<AssistantWorkspace>`。

### 4.2 AssistantWorkspace 布局

```
┌─────────────────────────────────────────────────────────┐
│  Sidebar (260px)              │  Main pane              │
│  ────────────────              │  ─────────────────────  │
│  + 新会话                       │  Today, 09:32          │
│  ────                           │  (chat 流)              │
│  📌 助理记得 (12 facts)         │                        │
│  ────                           │  ...                   │
│  Recent sessions                │                        │
│  · 2026-06-28 老李 入组讨论    │                        │
│  · 2026-06-27 周三晨间简报      │                        │
│  · 2026-06-26 NSCLC 新数据      │                        │
│  ...                            │  ────────────────────  │
│                                 │  [composer]            │
└─────────────────────────────────┴────────────────────────┘
```

- **左侧**:`+ 新会话`、`📌 助理记得`(进 Facts 管理子页)、最近会话列表
- **右侧主面板**:full-height chat — 不像 CrossResearchChat 是底部小 panel,
  这里是主战场。messages 占满,composer 钉底
- 沿用 F-crc-composer-pin / F-chat-state-persist 模式

### 4.3 Facts 管理子页 (`/助理/facts` overlay)

```
┌─ 助理记得 ──────────────────────────────────────────┐
│  按类型过滤: [全部] [偏好] [身份] [风格] [话题]      │
│  ──────────────                                      │
│  [✓] 你主攻 NSCLC + SCLC               (identity)    │
│      来源: 2026-06-15 session    [retire] [edit]     │
│                                                       │
│  [?] 你偏好 Hybrid RT 方案 (FLASH+IO)   (preference) │
│      来源: 2026-06-20 session    [confirm] [retire]  │
│                                                       │
│  [✓] [style] 简短 bullet > 长段落                    │
│      来源: 👎 ×3                  [retire]            │
│  ...                                                  │
│                                                       │
│  [+ 教助理一件事]                                     │
└──────────────────────────────────────────────────────┘
```

- `[✓]` confirmed by medic(实心)、`[?]` LLM 抽出未确认(虚线框)
- 每条都能 retire(软删,保留 90 天可恢复)
- `[+ 教助理]` 手动塞 fact(跳过 LLM 抽取,直接 confirmed)

---

## 5. 后端

### 5.1 新增 ChatScope

`chat_router_v2.py` 已经支持 `scope_kind` 字段。新增 `'assistant'` 分支:

```python
elif scope_kind == "assistant":
    ctx_block = await _gather_assistant_context(conn, user_id)
```

### 5.2 `_gather_assistant_context()` (新)

位于 `retrieval_tiers.py`,生成助理 system prompt 的 context block:

```
PERSISTENT BACKGROUND (things this medic has confirmed):
  · 主攻 NSCLC + SCLC
  · 偏好 Hybrid RT (FLASH + IO)
  · [style] 简短 bullet 答案 > 长段落散文

THIS MEDIC'S DOWN-VOTED PATTERNS (avoid):
  · quick lookup 问题里给超长 bullet (3x)
  · 引用论文却不给 PMID (1x)

PATIENT ROSTER (ACTIVE):
  · ZS · #1 — hash=abc123  | NSCLC IIIB, PD-1 维持
  · LW · #2 — hash=def456  | SCLC, hybrid RT II 期
  ...

RESEARCH STUDIES (ACTIVE + DRAFT):
  · HYBRID-RT-NSCLC — II 期, 招募中
  · ES-SCLC-CRT-II — II 期, 草稿
  ...

RECENT TAKEAWAYS (last 15 sessions):
  · 2026-06-25: 关心 Hybrid-RT 是否适用于 oligo-recurrent SCLC
  · 2026-06-26: 评估 老李 进入 HYBRID-RT-NSCLC 的可能性
  ...
```

四块依赖:
1. **PERSISTENT BACKGROUND** ← `assistant_facts` 表 (kind != 'style', retired_at IS NULL)
2. **DOWN-VOTED PATTERNS** ← `assistant_facts` 表 (kind = 'style')
3. **PATIENT ROSTER** ← 复用 `_gather_patient_roster()` (F-merge-patients-db 后已自动过滤 archived_at)
4. **RESEARCH STUDIES** ← 复用 `_gather_all_studies_summary()` (F-roster-archive-filter 后已过滤)
5. **RECENT TAKEAWAYS** ← `chat_takeaways` WHERE scope_kind='assistant' ORDER BY created_at DESC LIMIT 15

### 5.3 Schema 变化 — 新表 `assistant_facts`

```sql
CREATE TABLE assistant_facts (
  fact_id              TEXT NOT NULL,
  user_id              TEXT NOT NULL,
  kind                 TEXT NOT NULL,   -- 'identity'|'preference'|'style'|'topic'
  text                 TEXT NOT NULL,   -- 人读句子,中英都行
  evidence_session_id  TEXT,            -- 蒸馏出该 fact 的 session
  evidence_quote       TEXT,            -- 原话(防幻觉)
  source               TEXT NOT NULL,   -- 'llm_distilled'|'medic_taught'|'thumbs_down'
  created_at           INTEGER NOT NULL,
  confirmed_at         INTEGER,         -- 医生 confirm 时间
  retired_at           INTEGER,         -- 软删
  thumbs_down_count    INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (user_id, fact_id)
);
CREATE INDEX idx_assistant_facts_user ON assistant_facts(user_id, retired_at);
CREATE INDEX idx_assistant_facts_active ON assistant_facts(user_id, kind)
  WHERE retired_at IS NULL;
```

**为什么不直接复用 `chat_takeaways`?**
- Takeaways 是**会话级**摘要 ("这次聊到的点");facts 是**长期断言** ("医生主攻 NSCLC")
- Takeaways 每轮自动产生、自动衰减(只看最近 15 条);facts 经过确认会**永久**留着
- Takeaways 不需要 confirm;facts 需要医生显式确认才进 PERSISTENT BACKGROUND(降低幻觉)
- 两者**互补**:takeaway 是短期工作记忆,fact 是长期人格记忆

### 5.4 新 endpoints (`assistant_router.py`)

```
GET    /api/v1/assistant/facts?kind=&include_unconfirmed=
POST   /api/v1/assistant/facts                      创建(医生手动 teach)
PATCH  /api/v1/assistant/facts/{fact_id}/confirm
PATCH  /api/v1/assistant/facts/{fact_id}/retire
POST   /api/v1/assistant/feedback                   收 thumbs_down
                                                     body: {message_event_idx, reason}
```

### 5.5 演进 worker — `assistant_distiller`

在 `practitioner/session_takeaway.py` 旁边加一个 `assistant_distiller.py`:

```
def distill_assistant_facts(user_id, session_id):
    """每个 assistant scope session 结束时跑一次:
       1. 拉取该 session 的 turn texts
       2. 加载已有 facts(避免重复)
       3. LLM 抽取 0-3 条候选 fact(identity / preference / topic)
       4. 写入 assistant_facts,confirmed_at=NULL(等医生确认)
       5. 发 SSE 事件给前端,让 sidebar 的 "助理记得" 角标 +1
    """
```

触发时机:
- Session 关闭时 (前端发 `/assistant/session-end`)
- 后台定时 (cron,每天 04:00) 扫所有 assistant session

---

## 6. 三层演进机制

### Layer A · 会话级 Takeaway(复用 F10,30 分钟)

- 每轮 chat 结束触发 `session_takeaway` LLM,产出 1-3 条 takeaway
- 已有基础设施只需:
  1. `chat_takeaways` 表 加 `scope_kind='assistant'` 值识别
  2. system prompt 拼装时,WHERE scope_kind='assistant' AND user_id=? ORDER BY created_at DESC LIMIT 15
- **现成可用**

### Layer B · 持久化 Facts(新,3 小时)

- 新表 `assistant_facts`
- distill worker 跑在 session-end + cron
- "助理记得" UI 允许 confirm / retire / 手动 teach
- **system prompt 里**:
  - confirmed facts 直接列出
  - unconfirmed facts 不进 prompt(避免 LLM 把猜测当事实进一步强化)
  - retired facts 永远不进 prompt

### Layer C · 风格反馈(新,2 小时)

- 每条 agent 回复底下 👍 / 👎
- 👎 弹小弹框,选原因(预设 4 个 + "其他"自填):
  - 太长
  - 语气不对
  - 没引用证据
  - 答非所问
  - 其他 (自填)
- `POST /assistant/feedback` 写入 assistant_facts(kind='style', source='thumbs_down')
- 重复同类型 👎 时 `thumbs_down_count += 1`(不创建新 fact)
- system prompt 列出 `thumbs_down_count >= 1` 的 style facts 到 "AVOID" 区

---

## 7. 安全性 / Anti-foot-gun

| 风险                                  | 防护                                                    |
| ------------------------------------- | ------------------------------------------------------- |
| Facts 把患者 PHI 误当成医生属性记下来 | distill prompt 明确"只抽医生自身偏好/习惯,**不要**抽 patient-specific 内容";写入时正则禁掉患者 hash/姓名 |
| LLM 幻觉产生不实 facts                | 每条 fact 带 `evidence_quote`,医生 confirm 时显示原话验证 |
| 不可恢复的删除                        | 全部软删(`retired_at`),Settings 可看 90 天内的 retired facts 并恢复 |
| 跨身份污染                            | facts 严格 `user_id` 范围;F26 切换身份时 reset state    |
| Fact bloat                            | active facts 上限 50;超出后按 `confirmed_at` 旧的先 retire |
| 风格 facts 一时冲动                   | thumbs-down 必须选原因,纯 thumbs-down without reason 只做计数不创建 fact |

---

## 8. 实施分期

| Phase | 范围                                                                 | 估时 |
| ----- | -------------------------------------------------------------------- | ---- |
| **0 · Skeleton**  | `Workspace='assistant'`、顶 tab、空 AssistantWorkspace、`scope_kind='assistant'` 后端识别 + 复用 patient + studies roster | 30 min |
| **1 · Layer A**   | Takeaway 复用、TakeawaysButton with `scope_kind='assistant'`、system prompt 拉 15 条 | 30 min |
| **2 · Layer B**   | `assistant_facts` 表 + 迁移、`assistant_router.py` CRUD、distiller worker、"助理记得" Settings 子页 | 3 hr |
| **3 · Layer C**   | 每条 agent 消息加 👍/👎、反馈弹框、`POST /assistant/feedback`、style facts 进 system prompt AVOID 区 | 2 hr |
| **4 · Polish**    | 多 session 切换、session-end hook、cron 定时 distill、Facts 90 天 GC | 2 hr |

**MVP = Phase 0 + 1**(1 小时即可可用,只是不会进化);**完整体 = Phase 0-3**(~6 小时)

---

## 9. 决策记录

- **D1 · 跨研究 chat 是否保留?** 保留。两个不同 sessionId / scope,EmptyState 内的还是研究域窄上下文,助理 tab 是全局宽上下文。
- **D2 · 演进强度?** 三层都做:takeaway(L1)+ facts(L2)+ 反馈(L3)。L1 复用,L2/L3 新建。
- **D3 · Tab 命名?** "助理 ✨"。简洁中文 + emoji,统一外观语言。
- **D4 · facts 与 takeaways 的关系?** facts ⊂ 长期(永久,需 confirm),takeaways ⊂ 短期(会话级,自动衰减)。两者独立,distiller 把 takeaways 升格为 facts 候选。
- **D5 · 患者 PHI 进 facts?** 严禁。distill prompt + 正则双重过滤。

---

## 10. Open Questions

1. **首页落地**:助理 tab 是不是开机默认?当前默认 `today`,我倾向保持不变,让医生主动点。
2. **助理能不能主动 push 通知**(早间简报)?可挂 scheduled-tasks 系统,Phase 4 再说。
3. **助理跟患者 chat / 研究 chat 共享 takeaways 吗?** 不共享。assistant scope 的 takeaways 是助理专属。但 LLM 答题时可能引用患者域的 graph_nodes 作为证据(这是 retrieval 的责任,不是 takeaway 共享)。
4. **多用户(团队模式)的 facts**:per-user。Managed Mode 上线后可考虑团队级 shared facts,但 MVP 是 per-user only。

---

## 附录 · System Prompt 全文示例

```
You are this medic's personal clinical research assistant. You have
broad workspace visibility and persistent memory of their preferences.

═══════════════════════════════════════════════════════════════════
PERSISTENT BACKGROUND (things this medic has confirmed)
═══════════════════════════════════════════════════════════════════
  · You are a thoracic radiation oncologist specializing in NSCLC and SCLC.
  · You prefer Hybrid RT (FLASH + IO) over conventional protocols when feasible.
  · You typically review imaging before reading clinical notes.

═══════════════════════════════════════════════════════════════════
AVOID (patterns the medic has down-voted)
═══════════════════════════════════════════════════════════════════
  · Long bullet lists for quick-lookup questions (3 times)
  · Citing papers without PMID (1 time)

═══════════════════════════════════════════════════════════════════
PATIENT ROSTER (ACTIVE, not archived)
═══════════════════════════════════════════════════════════════════
[reference by hash prefix [hash=abc...] or sequence #N]
  · ZS · #1 (M · 60-69) — hash=4a8f3e2b9c1d
      [N42] NSCLC IIIB · [N51] PET-CT 2026-05-18 · [N67] PD-1 维持中
  · LW · #2 (M · 60-69) — hash=def456abc890
      [N12] SCLC LD · [N18] EP×4 完成 · [N29] CRT 计划中
  ...

═══════════════════════════════════════════════════════════════════
RESEARCH STUDIES (ACTIVE + DRAFT, not archived)
═══════════════════════════════════════════════════════════════════
  ── ACTIVE (recommend-eligible) ──
  Study HYBRID-RT-NSCLC — Hybrid RT NSCLC Phase II (招募中)
      inclusion:
        - 年龄 18-75 岁
        - ECOG 0-1
        - AJCC IIIA/IIIB NSCLC
      exclusion:
        - 驱动基因阳性
        … (+3 more)
  Study ES-SCLC-CRT-II — ES-SCLC 化疗+免疫序贯放疗 (招募中)
      …

  ── DRAFT (mention only, do NOT recommend patients) ──
    Study NSCLC-PD1-MAINT-V2 (II, draft)

═══════════════════════════════════════════════════════════════════
RECENT TAKEAWAYS (last 15 conversations)
═══════════════════════════════════════════════════════════════════
  · 2026-06-26: 评估老李进入 HYBRID-RT-NSCLC 的可能性
  · 2026-06-25: 关心 Hybrid-RT 是否适用 oligo-recurrent SCLC
  · 2026-06-24: 周三晨间简报关注 G3+ AE 趋势
  ...

═══════════════════════════════════════════════════════════════════
INSTRUCTIONS
═══════════════════════════════════════════════════════════════════
- Cite patients by hash prefix [hash=abc...] and studies by short_code
- Use the medic's preferred style above
- NEVER invent patient-specific facts; only reference what's in PATIENT ROSTER
- NEVER invent a [Nxx] tag; only cite IDs that appear above
- If unsure about a fact, ask before claiming
- When the medic teaches something new, suggest creating a persistent fact
```
