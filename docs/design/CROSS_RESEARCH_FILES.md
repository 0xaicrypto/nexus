# 跨研究 Chat — 文件参考库设计

**Status:** Locked in (2026-06-28). MVP = full Phase 0-5.
**Companion to:** ASSISTANT_TAB.md (related; assistant tab will reuse the same pattern)

---

## 1. 目标

让医生在跨研究 chat 里**上传文件后**:

1. 能在 UI 里看到自己上传过哪些文件(持久,跨会话)
2. 明确知道这些文件会被 AI 作为参考(每轮 system prompt 注入)
3. AI 回答里引用文件时,医生能点击跳回原文件 (`[F1]` chip → file viewer)

---

## 2. 关键决策

| 决策                                                 | 选择                                     |
| ---------------------------------------------------- | ---------------------------------------- |
| 文件作用域                                           | **Workspace 级** — 全局参考库,所有 cross-research session 共享 |
| 数据落点                                             | `uploads.session_id = '__cross_research_lib__'` 哨兵值 — 不加新表 |
| 文件 ID                                              | `[F1] [F2] ...` 按 `created_at` 顺序,稳定不变 |
| 删除策略                                             | Soft delete(`uploads.deleted_at`),保留 7 天可恢复 |
| 上限                                                 | 单用户上限 50 个文件;超过阻止上传 + 提示清理 |
| 单文件 LLM 注入字符数                                | 4000 chars,截断时附 truncation note     |
| 单次 prompt 中的文件总字符数                         | 软上限 ~30K(占 system prompt 不超过 1/3) |

---

## 3. 实施分期

| Phase | 范围                                                              | 估时    |
| ----- | ----------------------------------------------------------------- | ------- |
| 0     | `acceptFiles` 传哨兵 session_id,uploads 持久化                    | 15 min  |
| 1     | `GET/DELETE /api/v1/chat/cross-research/files` API                | 30 min  |
| 2     | UI:持久 chip 条 + 全列表抽屉 modal                                | 1 hr    |
| 3     | LLM `[Fn]` 注入:`_gather_cross_research_files()` + system prompt | 45 min  |
| 4     | `[Fn]` chip 渲染 + 点击跳回 file viewer                           | 30 min  |
| 5     | 已移除 tab + 7d GC cron                                           | 30 min  |
| **合计** |                                                                | **~4 小时** |

---

## 4. 数据层

### 4.1 上传时绑哨兵 session_id

哨兵常量:
```python
CROSS_RESEARCH_LIB_SENTINEL = "__cross_research_lib__"
```

前端 `acceptFiles`(`research-workspace.tsx::CrossResearchChat`):
```ts
const r = await api.uploadFile(file, file.name, {
  sessionId: '__cross_research_lib__',  // 哨兵
});
```

后端 `/files/upload`(已有逻辑)把 `session_id` 写进 uploads 行,自动 OK,无需改 schema。

### 4.2 查询 / 删除

```sql
-- 列文件 (Phase 1 GET endpoint)
SELECT file_id, name, mime, size_bytes, created_at, sha256
  FROM uploads
 WHERE user_id = ?
   AND session_id = '__cross_research_lib__'
   AND deleted_at IS NULL
 ORDER BY created_at ASC;

-- 软删除 (Phase 1 DELETE endpoint)
UPDATE uploads SET deleted_at = ?
 WHERE user_id = ? AND file_id = ?
   AND session_id = '__cross_research_lib__';

-- 恢复 (Phase 5)
UPDATE uploads SET deleted_at = NULL
 WHERE user_id = ? AND file_id = ?
   AND deleted_at > (now - 7d);

-- 物理 GC (Phase 5 cron, 每日 04:00)
SELECT disk_path FROM uploads WHERE deleted_at IS NOT NULL AND deleted_at < (now - 7d);
-- 按 disk_path 删文件,然后 DELETE FROM uploads WHERE ...
```

**Schema 需要的**:`uploads.deleted_at` 字段。若已有跳过,否则加一个 migration 0005:
```python
op.execute("ALTER TABLE uploads ADD COLUMN deleted_at INTEGER")
```

---

## 5. API

### 5.1 列文件
```
GET /api/v1/chat/cross-research/files
→ {
    files: [
      { fileId, name, mime, sizeBytes, createdAt, fIdToken: "F1" },
      ...
    ],
    totalActive: 4,
    totalRemoved: 2,        // 7d 内可恢复的数量
  }
```

### 5.2 删除
```
DELETE /api/v1/chat/cross-research/files/{file_id}
→ { fileId, deletedAt }
```

### 5.3 恢复 (Phase 5)
```
POST /api/v1/chat/cross-research/files/{file_id}/restore
→ { fileId }
```

### 5.4 列已移除 (Phase 5)
```
GET /api/v1/chat/cross-research/files/removed
→ { files: [...], expiresInDays: number }
```

---

## 6. UI

### 6.1 持久 chip 条(composer 上方)

```
┌── 跨研究 panel ────────────────────────────────────────┐
│   (messages...)                                        │
│   ────────────────────────────────────────────         │
│   📂 4 个参考文件:                                      │
│   [F1 RECIST_v1.1.pdf ✕]  [F2 cohort_ae.xlsx ✕]        │
│   [F3 csco_2024.pdf ✕]    [展开全部 →]                  │
│   ┌──────────────────────────────────────┐             │
│   │ [📎 输入框 + 发送]                    │             │
│   └──────────────────────────────────────┘             │
└────────────────────────────────────────────────────────┘
```

- chip 限 3 个 + "展开" 链接
- chip 左边显示 `F1` 标识(就是 LLM 引用用的 ID)
- ✕ 调 DELETE,toast "✓ 已移除,7 天内可恢复"

### 6.2 全列表抽屉(点 "展开全部" / 点 header chip 都可触发)

```
┌─ 跨研究参考文件库 ────────────────────────────────────┐
│  [当前 (4)]  [已移除 (2)]                              │
│  ─────                                                 │
│  [F1] RECIST_v1.1.pdf          120 KB   2h 前   👁 ✕   │
│  [F2] cohort_ae_log.xlsx        45 KB   1h 前   👁 ✕   │
│  [F3] guideline_csco_2024.pdf  680 KB   1d 前   👁 ✕   │
│  [F4] hybrid_rt_data.png       220 KB   30m 前  👁 ✕   │
│  ─────                                                 │
│  [+ 添加(也支持拖拽 / 粘贴)]                            │
│  ─────                                                 │
│  注:每轮聊天会自动把全部 4 个文件作为上下文喂给 AI       │
└────────────────────────────────────────────────────────┘
```

👁 = file viewer(已有,复用)
✕ = soft delete
"已移除" tab = Phase 5 恢复 UI

---

## 7. LLM 引用机制

### 7.1 后端注入(`_gather_cross_research_files`,在 retrieval_tiers.py)

```python
def _gather_cross_research_files(conn, user_id) -> str:
    rows = conn.execute("""
        SELECT file_id, name, mime, extracted_text
          FROM uploads
         WHERE user_id = ?
           AND session_id = '__cross_research_lib__'
           AND deleted_at IS NULL
         ORDER BY created_at ASC
    """, (user_id,)).fetchall()
    if not rows:
        return ""

    parts = [
        "\n\nSESSION REFERENCE FILES "
        "(cite as [F1], [F2], etc. — never invent IDs):"
    ]
    for i, (fid, name, mime, text) in enumerate(rows, start=1):
        excerpt = (text or "")[:4000]
        truncated = " (truncated)" if text and len(text) > 4000 else ""
        parts.append(f"\n  [F{i}] {name}  ({mime})")
        if excerpt:
            parts.append(f"        --- excerpt{truncated} ---\n{excerpt}\n        --- end excerpt ---")
        else:
            parts.append("        (binary file, no extracted text — name + type only)")

    parts.append("\nCITATION RULES FOR FILES:")
    parts.append("  - When grounding on file content, cite [Fn] inline")
    parts.append("  - Never invent an [Fn] — only IDs listed above are valid")
    parts.append("  - You may combine: e.g. \"RECIST PR = ≥30% decrease [F1, NCCN 2024]\"")
    return "\n".join(parts)
```

注入位置:`retrieve_async()` 里 cross-research 分支,在 `RESEARCH STUDIES` 块之后,`EXTERNAL KNOWLEDGE TOOLS` 之前。

### 7.2 SSE 增加 file citation 事件

chat_router_v2 在 streaming 完成后,扫 final answer text,正则 `\[F(\d+)\]` 找出 LLM 引用了哪些 file。emit 一个新 chunk type:

```typescript
{ type: 'file_citations'; refs: Array<{ fIdToken: 'F1'; fileId: '...'; name: '...' }> }
```

(参考已有 `web_search_results` chunk 的形式。)

### 7.3 前端 chip 渲染

扩 `CitationChip2` 组件支持新 `kind='file'`:

```tsx
function CitationChip2({ ref }: { ref: CitationRef }) {
  if (ref.kind === 'graph_node') {
    return <NodeChip nodeId={ref.node_id} />;
  }
  if (ref.kind === 'web_source') {
    return <WebChip ... />;
  }
  if (ref.kind === 'file') {
    return (
      <button
        className="..."
        onClick={() => openFileViewer(ref.file_id)}
      >
        📎 {ref.f_id_token} {ref.name}
      </button>
    );
  }
}
```

LLM 输出的 markdown 里 `[F1]` 在 `react-markdown` 渲染时被 plugin 替换成 chip 组件。这个 plugin 已经处理 `[Nxx]` 和 `[Wxx]`,加 `[Fxx]` 是延伸。

---

## 8. 安全 / Anti-foot-gun

| 风险                                          | 防护                                                    |
| --------------------------------------------- | ------------------------------------------------------- |
| 文件含患者 PHI 漏到跨研究 LLM 上下文          | extracted_text 在上传时已经做了 patient_hash 屏蔽?需要复查 |
| 50 个文件 cap 触达后医生无法上传              | 阻止 + 弹"已满,请先清理"对话框                          |
| LLM 引用了无效 `[F99]`                        | 前端渲染时检测 → 灰显 "⚠ 引用了不存在的文件"            |
| LLM 把所有文件全 copy 一遍当回答              | system prompt 明令:"cite [Fn], don't paste whole files" |
| 同名文件二次上传                              | 不去重,新一个 file_id,后续 [Fn] 排在后面                |
| 跨身份污染                                    | 已有 `WHERE user_id = ?` 保护(F-multiuser-isolation 修好后双保险) |
| 截断后医生不知道                              | 抽屉 modal 显示每个文件 "extracted: N chars / X chars total" |

---

## 9. 与"助理 tab"的关系

`ASSISTANT_TAB.md` 里的助理也需要同样的文件参考能力。**实现复用:**

- 数据层:助理用另一个哨兵 `__assistant_lib__`
- API:把 `/chat/cross-research/files` 抽象成 `/chat/lib/{lib_kind}/files`,`lib_kind ∈ {'cross_research', 'assistant'}`
- UI:`CrossResearchFilesPanel` 抽象成 `ChatFileLibPanel`,两个 tab 都用
- LLM 注入:`_gather_chat_lib_files(conn, user_id, lib_kind)` 通用化

这一步在 Phase 0-5 跑完后做(~1 hr 重构)即可。MVP 阶段先把跨研究做完。

---

## 10. Open questions(开工前对齐)

我没有问题了,设计 + 实施都已定调。等你 "go" 我就开始 Phase 0。
