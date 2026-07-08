# 助理 Tab — 实施计划

**Companion to:** `ASSISTANT_TAB.md` (design)
**Status:** Ready to execute
**Total estimate:** 7-8 小时(MVP 1 小时,完整体 6-8 小时,Polish 再 +2 小时)

---

## 0 · Pre-flight: 现状盘点

### 已经现成的(直接复用)

| 已有                                                          | 用途                                  |
| ------------------------------------------------------------- | ------------------------------------- |
| `Workspace` enum (`lib/util.ts`)                              | 顶 tab 切换 — 加一个值即可            |
| `WorkspaceSwitcher` (`App.tsx`)                               | 顶 tab UI — 加一个按钮即可            |
| `api.sendChat({scope: {kind}})`                               | 后端 chat 路由 — 加一个 `kind` 值     |
| `ChatScope` (chat_router_v2.py)                               | 后端 scope 模型                       |
| `chat_takeaways` 表 + scope_kind 字段                         | F10 — 助理 takeaway 直接 scope_kind='assistant' |
| `session_takeaway.distill_takeaways()`                        | LLM 蒸馏管线                          |
| `TakeawaysButton` 组件                                        | 已经收 scopeKind 参数                 |
| `_gather_patient_roster()`                                    | 患者 roster(已过滤 archived_at)      |
| `_gather_all_studies_summary()`                               | 研究 roster(已过滤 archived_at)      |
| `nexus_sessions` 表 + 多 session                              | 助理多会话 sidebar                    |
| Alembic migration runner                                      | 加一个 `0005_assistant_facts.py`      |

### 需要新建的

| 新建                                                          | 类型             |
| ------------------------------------------------------------- | ---------------- |
| `assistant_facts` 表 + migration                              | 后端 schema      |
| `nexus_server/assistant_router.py`                            | 后端 endpoints   |
| `nexus_server/assistant/distiller.py`                         | 后端 worker      |
| `_gather_assistant_context()` in retrieval_tiers.py           | 后端 retrieval   |
| `src/components/assistant-workspace.tsx`                      | 前端 tab 内容    |
| `src/components/assistant-facts-overlay.tsx`                  | 前端 facts 管理 UI |
| `🎯 / 👎 反馈 UI`                                              | 前端反馈控件      |

---

## 1 · Phase 0 — Skeleton(30 min,MVP-1/2)

**目标:** "助理 ✨" tab 能进、能切、能发消息得到回应。回应只用 patient + studies roster,**还不带任何记忆**。

### 后端

1. **`chat_router_v2.py`**
   - `ChatScope.kind` 注释加入 `'assistant'`
   - `scope_tuple_from_request()` 加分支:`'assistant'` → `scope_kind='assistant', scope_ref='__assistant__'`

2. **`retrieval_tiers.py`** — 新函数:
   ```python
   def _gather_assistant_context(conn, user_id) -> str:
       """Phase 0 = patient roster + studies roster + 用户身份提示。"""
       roster = _gather_patient_roster(conn, user_id, limit=30)
       studies = _gather_all_studies_summary(conn, user_id, max_per_section=12)
       persona = (
           "\n\nYOU ARE THIS MEDIC'S PERSONAL CLINICAL ASSISTANT.\n"
           "  - Broad scope: this user's active patients + active/draft studies.\n"
           "  - You cite patients by hash prefix, studies by short_code.\n"
           "  - Never invent patients/studies not in the roster above.\n"
       )
       return persona + (roster or "") + (studies or "")
   ```

3. **`retrieve_async()`** 顶部加一个分支(参考现有的 `is_cross_research`):
   ```python
   is_assistant = (scope_kind == 'assistant')
   if is_assistant:
       context_block = _gather_assistant_context(conn, user_id)
       # external_block 也喂上,助理也能用 web search
   ```

### 前端

4. **`lib/util.ts`**
   ```ts
   export type Workspace = 'patient' | 'research' | 'assistant';
   ```

5. **`store.ts`** — `activeWorkspace` 初始化校验拓宽:
   ```ts
   if (v === 'patient' || v === 'research' || v === 'assistant') return v;
   ```

6. **`lib/api-client.ts`**
   - `sendChat` 的 scope 类型字段 union 加 `'assistant'`
   - `ChatSessionInfo.scopeKind` 同步加值

7. **`App.tsx::WorkspaceSwitcher`** — 加按钮:
   ```tsx
   {btn('assistant', '助理 ✨', 'your personal AI')}
   ```
   渲染分支:
   ```tsx
   {activeWorkspace === 'assistant' && <AssistantWorkspace />}
   ```

8. **新文件 `src/components/assistant-workspace.tsx`**(~150 行):
   - 复用 `CrossResearchChat` 整体结构,但作为主面板(full-height)
   - sessionId 默认 `assistant-default`,后续支持多 session
   - 调 `api.sendChat(text, sid, null, fileIds, {kind: 'assistant', focusPatientHash: null})`
   - 用 F-chat-state-persist 的 zustand store 持久化 ChatMsg(切 tab 不丢)
   - 顶部一行 chip:Patients × N · Studies × M · 助理记得 × K(facts 数)
   - composer 钉底(F-crc-composer-pin 模式)

### 验收标准(Phase 0)

- [ ] 顶部出现"助理 ✨" tab
- [ ] 点进去看到一个空 chat 界面
- [ ] 发 "我有哪些研究" → LLM 列出当前 active studies(正确,且自动跟随归档)
- [ ] 发 "我有哪些患者" → LLM 列出当前 active patients
- [ ] 切到其他 tab 再切回来,在写的 AI 回复不丢(F-chat-state-persist 已经给了)

**MVP cut line:** Phase 0 结束就能用。基础架子立起来,后面三层都是在这上面叠记忆。

---

## 2 · Phase 1 — Layer A:Takeaway 复用(30 min)

**目标:** 每轮聊完自动蒸馏 takeaway,下次聊天 system prompt 拉最近 15 条。

### 后端

1. **`session_takeaway.distill_takeaways()`** — 已经按 scope_kind 工作,只需在 chat_router_v2 turn-complete 钩子里确认 `scope_kind='assistant'` 也走 distill 路径(F10 应该已经覆盖,检查一遍即可)

2. **`_gather_assistant_context()`** 扩展:
   ```python
   def _gather_assistant_context(conn, user_id) -> str:
       # ... 前面 persona + roster + studies 不变
       takeaways = _gather_recent_takeaways(
           conn, user_id, scope_kind='assistant', limit=15,
       )
       return persona + roster + studies + takeaways
   ```

3. **新 `_gather_recent_takeaways()`**(retrieval_tiers.py):
   ```sql
   SELECT created_at, takeaway_text
     FROM chat_takeaways
    WHERE user_id = ? AND scope_kind = ?
    ORDER BY created_at DESC
    LIMIT ?
   ```
   渲染成:
   ```
   RECENT TAKEAWAYS (last N sessions, newest first):
     · 2026-06-28: 关心 老李 进入 HYBRID-RT 的可能性
     · 2026-06-27: 周三晨间简报关注 G3+ AE 趋势
     ...
   ```

### 前端

4. **`AssistantWorkspace`** 顶部 chip 接 `<TakeawaysButton scopeKind='assistant' scopeRef='__assistant__' />`,医生可点开看 takeaway 列表

### 验收

- [ ] 第二次聊天能在 system prompt 里看到上一次的 takeaway(开 dev tools 看 chat_router 的 outbound prompt)
- [ ] LLM 回答能体现"上次我们聊了 X" 的连续性

---

## 3 · Phase 2 — Layer B:持久化 Facts(3 hr,核心)

**目标:** facts 表立起来,distiller worker 跑起来,医生能 confirm/retire/手动 teach。

### 3.1 Schema(15 min)

**新 migration:** `packages/server/nexus_server/migrations/versions/0005_assistant_facts.py`

```python
"""Assistant facts table"""
from alembic import op

revision = "0005"
down_revision = "0004"

def upgrade():
    op.execute("""
        CREATE TABLE IF NOT EXISTS assistant_facts (
            fact_id              TEXT NOT NULL,
            user_id              TEXT NOT NULL,
            kind                 TEXT NOT NULL,
            text                 TEXT NOT NULL,
            evidence_session_id  TEXT,
            evidence_quote       TEXT,
            source               TEXT NOT NULL,
            created_at           INTEGER NOT NULL,
            confirmed_at         INTEGER,
            retired_at           INTEGER,
            thumbs_down_count    INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (user_id, fact_id)
        )
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_assistant_facts_user
        ON assistant_facts(user_id, retired_at)
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_assistant_facts_active
        ON assistant_facts(user_id, kind) WHERE retired_at IS NULL
    """)

def downgrade():
    op.execute("DROP TABLE IF EXISTS assistant_facts")
```

### 3.2 后端 router(45 min)

**新文件:** `packages/server/nexus_server/assistant_router.py`

```python
@router.get("/facts")
async def list_facts(
    kind: str | None = None,
    include_unconfirmed: bool = True,
    include_retired: bool = False,
    user_id: str = Depends(get_current_user),
) -> dict:
    """返回 facts 列表 + 计数。前端 sidebar 角标用。"""

@router.post("/facts")
async def create_fact(
    req: CreateFactRequest,    # text, kind, source ('medic_taught' 默认)
    user_id: str = Depends(get_current_user),
) -> dict:
    """医生手动 teach 一条 — 直接 confirmed_at=now。"""

@router.patch("/facts/{fact_id}/confirm")
async def confirm_fact(fact_id, user_id=Depends(...)) -> dict:
    """UPDATE confirmed_at = now WHERE retired_at IS NULL."""

@router.patch("/facts/{fact_id}/retire")
async def retire_fact(fact_id, user_id=Depends(...)) -> dict:
    """UPDATE retired_at = now."""

@router.patch("/facts/{fact_id}/unretire")
async def unretire_fact(...) -> dict:
    """90 天内可恢复。"""

@router.post("/feedback")
async def feedback(req: FeedbackRequest, user_id=...) -> dict:
    """收 thumbs-down。Phase 3 才接 UI,但端点先建好。
    body: {assistant_event_idx: int, reason: str, free_text: str}
    """
```

注册到 `main.py`:
```python
from nexus_server.assistant_router import router as assistant_router
app.include_router(assistant_router, prefix='/api/v1/assistant')
```

### 3.3 Distiller worker(60 min)

**新文件:** `packages/server/nexus_server/assistant/__init__.py` + `assistant/distiller.py`

```python
async def distill_assistant_facts(
    user_id: str, session_id: str,
) -> list[dict]:
    """1. 拉这个 session 的 user/agent turn text
       2. 加载该 user 已有的 confirmed facts(避免重复)
       3. LLM 抽取 0-3 条候选 fact:
          - kind=identity / preference / topic
          - text=单句陈述
          - evidence_quote=原话(防幻觉)
       4. PHI 过滤:正则禁掉 patient_hash 形态 + 显式名字字段
       5. 写入 assistant_facts, source='llm_distilled', confirmed_at=NULL
       6. 返回新创建的 fact_id 列表,让 SSE event 推送给前端
    """
```

**触发时机:**
- chat_router_v2 的 turn-complete handler 里,如果 scope_kind=='assistant':
  ```python
  asyncio.create_task(distill_assistant_facts(user_id, session_id))
  ```
- 用 fire-and-forget,不阻塞响应

**LLM prompt(关键):**
```
You are extracting LONG-TERM facts about THE MEDIC (the user), NOT
about patients. Look at this conversation and extract 0-3 statements
the medic IS / PREFERS / OFTEN ASKS ABOUT.

★ STRICT RULES:
- Facts must be about the medic's identity, preferences, or topical
  focus areas. NEVER about specific patients.
- Each fact must have a verbatim quote from the conversation as
  evidence. If you can't quote them, don't claim it.
- Skip if already in existing facts (passed in).
- Return JSON array; empty array if nothing extractable.
```

### 3.4 retrieval 接入 facts(15 min)

`_gather_assistant_context()` 扩展第三段:

```python
def _gather_assistant_context(conn, user_id):
    # ... persona + roster + studies + takeaways

    facts_rows = conn.execute("""
        SELECT kind, text, thumbs_down_count
          FROM assistant_facts
         WHERE user_id = ? AND retired_at IS NULL AND confirmed_at IS NOT NULL
         ORDER BY confirmed_at DESC
         LIMIT 30
    """, (user_id,)).fetchall()
    if facts_rows:
        bg = ["\n\nPERSISTENT BACKGROUND (medic-confirmed):"]
        for kind, text, _ in facts_rows:
            if kind != 'style':
                bg.append(f"  · {text}")
        avoid = [f for f in facts_rows
                 if f[0] == 'style' and f[2] >= 1]
        if avoid:
            bg.append("\nAVOID (down-voted patterns):")
            for _, text, n in avoid:
                bg.append(f"  · {text} ({n}x)")
        return bg
```

### 3.5 前端 Facts 管理 UI(45 min)

**新文件:** `src/components/assistant-facts-overlay.tsx`

- Modal overlay,从 AssistantWorkspace sidebar 的 "📌 助理记得" 入口打开
- 三栏列表(按 kind 分组:身份 / 偏好 / 风格 / 话题)
- 每条 fact 一行:
  - 状态 chip(`✓ 已确认` / `? 待确认` / `已 retired`)
  - 文本
  - 来源 + 时间
  - 操作按钮:`确认` / `修改` / `淘汰`
- 底部 "教助理一件事" 输入框
- 复用 `api.listFacts / api.createFact / api.confirmFact / api.retireFact`

### 3.6 api-client.ts 扩展(15 min)

```ts
async listAssistantFacts(opts?: { kind?, includeUnconfirmed?, includeRetired? })
async createAssistantFact(body: { text, kind, source? })
async confirmAssistantFact(factId)
async retireAssistantFact(factId)
async unretireAssistantFact(factId)
async submitAssistantFeedback(body: { assistantEventIdx, reason, freeText? })
```

### 验收(Phase 2)

- [ ] 跑 2-3 轮聊天,后端日志里能看到 distiller 跑过、写入 1-3 条 unconfirmed facts
- [ ] 点 "📌 助理记得" 看到候选 facts 列表
- [ ] 确认其中 1 条,下次聊天 system prompt 出现 "PERSISTENT BACKGROUND: ..."
- [ ] 测试:`tests/test_assistant_facts.py` 覆盖 CRUD + retired 过滤 + PHI 拒绝

---

## 4 · Phase 3 — Layer C:风格反馈(2 hr)

### 4.1 后端(30 min)

`POST /api/v1/assistant/feedback` 已经在 Phase 2 建好端点。实现:

```python
async def feedback(req, user_id):
    # 找已存在的同类 style fact
    existing = conn.execute("""
        SELECT fact_id, thumbs_down_count
          FROM assistant_facts
         WHERE user_id = ? AND kind = 'style' AND text LIKE ?
           AND retired_at IS NULL
    """, (user_id, f"%{req.reason}%")).fetchone()

    if existing:
        # 已有同类,只 bump 计数
        conn.execute("""
            UPDATE assistant_facts SET thumbs_down_count = ? WHERE fact_id = ?
        """, (existing[1] + 1, existing[0]))
    else:
        # 新建 style fact,自动 confirmed
        text = _style_text_from_reason(req.reason, req.free_text)
        conn.execute("""
            INSERT INTO assistant_facts(...) VALUES (..., 'style',
                ?, NULL, ?, 'thumbs_down', ?, ?, NULL, 1)
        """, (text, req.assistant_event_idx, _now(), _now()))
```

### 4.2 前端反馈 UI(60 min)

每条 agent 消息底下加 `👍 / 👎`:

```tsx
<MessageFooter>
  <button onClick={() => thumbsUp()}>👍</button>
  <button onClick={() => setShowDownReasons(true)}>👎</button>
</MessageFooter>

{showDownReasons && (
  <ReasonPicker
    options={[
      { id: 'too_long',   label: '太长' },
      { id: 'wrong_tone', label: '语气不对' },
      { id: 'no_cite',    label: '没引用证据' },
      { id: 'off_topic',  label: '答非所问' },
      { id: 'other',      label: '其他(写一段)' },
    ]}
    onPick={(reason, freeText) => {
      api.submitAssistantFeedback({
        assistantEventIdx: m.assistantEventIdx,
        reason, freeText,
      });
      showToast('助理已记住,下次会避免这样');
    }}
  />
)}
```

### 4.3 验收

- [ ] 在 assistant chat 给一条回复点 👎 → 选"太长"
- [ ] 下一轮 system prompt 出现 `AVOID: ... too_long (1x)`
- [ ] 同样的回复再点 👎 → 计数变 2x,不创建新 fact
- [ ] "助理记得" UI 能看到该 style fact

---

## 5 · Phase 4 — Polish & GC(2 hr)

### 5.1 多 session 切换(45 min)

AssistantWorkspace sidebar:
- "+ 新会话" 按钮 → POST `/sessions` with scope_kind='assistant'
- session 列表 + 点击切换 activeSessionId
- 复用现有 `api.listSessions()` 和 `api.createSession()`

### 5.2 Session-end hook(15 min)

前端:
- 切 session / 关 tab / window unload 时,POST `/assistant/session-end`
- 后端触发 distiller(如果上一轮没触发的话)

### 5.3 Fact 90 天 GC(20 min)

后端 scheduled task(已有 cron 框架):
- 每天 04:00 跑
- `DELETE FROM assistant_facts WHERE retired_at IS NOT NULL AND retired_at < (now - 90d)`
- `DELETE FROM assistant_facts WHERE source='llm_distilled' AND confirmed_at IS NULL AND created_at < (now - 30d)`(未 confirm 的 30 天清掉)
- Active facts 上限 50:超出后按 confirmed_at 旧的先自动 retire

### 5.4 Fact 数量上限保护(20 min)

`distill_assistant_facts` 写入前查 active count,超 50 拒绝新建并 log。

### 5.5 一键导出助理 facts(20 min)

`GET /api/v1/assistant/facts/export` → JSON 文件,给医生备份/迁移用。

---

## 6 · 测试矩阵

| 测试文件                                 | 覆盖范围                                  |
| ---------------------------------------- | ----------------------------------------- |
| `tests/test_assistant_router.py` (新)    | CRUD endpoints + auth + PHI 拒绝          |
| `tests/test_assistant_distiller.py` (新) | LLM stub + 抽取契约 + 重复去重 + 患者 PHI 过滤 |
| `tests/test_retrieval_assistant.py` (新) | `_gather_assistant_context()` 各字段拼接 + 空状态 |
| 扩 `test_research_router.py`              | 助理 scope 不漏掉 / 不污染 patient & research scope |

### 关键回归测试断言

```python
# 助理 facts 严禁含患者 PHI
def test_distiller_rejects_patient_specific_facts():
    facts = distill_with_stub_llm(returns=[{
        'kind':'preference',
        'text':'你偏好对 hash=abc123 的患者用 Hybrid RT',
        'evidence_quote':'对 老李 用 Hybrid RT'
    }])
    assert facts == []  # PHI 过滤掉了

# 切身份不漏 facts
def test_facts_scoped_per_user():
    # 用户 A 建一条 fact
    # 切到用户 B
    # B 看不到 A 的 fact

# 助理 prompt 不被研究 chat 污染
def test_research_chat_does_not_see_assistant_facts():
    # 同一 user 在助理里 confirm 了一条 fact
    # 走研究 chat scope,system prompt 不含该 fact
```

---

## 7 · 提交边界(commits)

建议每个 Phase 独立提交,这样回滚粒度小:

```
commit 1: F-assistant-skeleton (Phase 0)
  - Workspace='assistant' enum + tab UI
  - AssistantWorkspace 基础 layout + sendChat
  - _gather_assistant_context() 最小版

commit 2: F-assistant-takeaway (Phase 1)
  - _gather_recent_takeaways
  - TakeawaysButton 接入

commit 3: F-assistant-facts-schema (Phase 2.1)
  - migration 0005
  - assistant_facts CREATE TABLE

commit 4: F-assistant-facts-api (Phase 2.2-2.4)
  - assistant_router.py 全部 endpoints
  - retrieval 接入 facts

commit 5: F-assistant-facts-distiller (Phase 2.3)
  - distiller.py + LLM prompt
  - turn-complete hook

commit 6: F-assistant-facts-ui (Phase 2.5)
  - assistant-facts-overlay.tsx
  - api-client 扩展

commit 7: F-assistant-feedback (Phase 3)
  - thumbs-down UI
  - feedback endpoint impl

commit 8: F-assistant-polish (Phase 4)
  - multi-session
  - GC cron
  - export
```

每个 commit 都跑一次完整 test suite(~30s)。

---

## 8 · 风险 & 替代方案

| 风险                                       | 缓解                                                   |
| ------------------------------------------ | ------------------------------------------------------ |
| Distiller LLM 调用慢,阻塞 turn 返回         | `asyncio.create_task` fire-and-forget;反正下一轮才用    |
| Facts 越积越多 → prompt 爆炸                | active 上限 50 + 优先级排序 + 旧的自动 retire           |
| 医生忘了 confirm,unconfirmed 越堆越多       | 30 天 GC + sidebar 角标 + 一次性 batch confirm 弹框    |
| 助理把患者 PHI 当成医生属性记下来           | distill prompt 明令 + 正则双重过滤 + 单测              |
| F26 切身份后助理记忆漏到下一个身份          | 严格 user_id 范围 + 切身份 resetForIdentitySwitch 时检查 |
| LLM 把已 retired 的 fact 仍提起来           | 永远不进 prompt;retired_at IS NULL 是 SQL where 死规则 |

**简化路线(如果 8 小时太长):**
- MVP-1(Phase 0 only,1 小时):只做 skeleton,没记忆但已经能用。
- MVP-2(Phase 0+1,1.5 小时):加 takeaway 复用,有跨会话短期记忆。
- 完整体(Phase 0-3,6 小时):带 confirmed facts + 反馈学习。
- 收尾(Phase 4,+2 小时):多 session + GC + 导出。

---

## 9 · Open Questions(开工前需对齐)

1. **Facts 默认要不要自动 confirm?**
   - A. unconfirmed 默认不进 prompt,医生 confirm 后才生效(更安全,但医生要点确认)
   - B. unconfirmed 也进 prompt 但标"?",一次 thumbs-down 自动 retire(更主动,但有幻觉风险)
   - **推荐 A**(更可控)

2. **AssistantWorkspace 要不要有"今日简报"区?**
   - 假如有,首次打开渲染一份固定模板(今日新患者 / 今日 G3+ AE / 今日入排候选)
   - **推荐 Phase 4 再说**,Phase 0-3 先把记忆系统跑通

3. **第一次进助理 tab 的引导?**
   - "试试问:'我有哪些患者' / '上周谁达到了 mPFS' / '我偏好哪种 RT 方案'"
   - 三条 quick prompt 按钮,点了就发出去
   - **推荐 Phase 0 顺手加**(5 分钟)
