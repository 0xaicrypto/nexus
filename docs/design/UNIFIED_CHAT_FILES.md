# 统一聊天文件参考库 — 设计

**Status:** Locked in (2026-06-28). Supersedes `CROSS_RESEARCH_FILES.md` (now a sub-case).
**Principle (medic-stated):** 所有聊天窗口支持的文件类型 + UI/UX 必须一致。

---

## 1. 核心原则

四个聊天面(患者 chat、per-study 研究 chat、跨研究 chat、助理 chat)共用:

1. **一套** `UploadKind` 集合(支持哪些文件)
2. **一套** UI 组件(`<ChatFileLib>` + `<AttachmentChipsRow>`)
3. **一套** 行为:拖拽/粘贴 → 进库 → 每轮自动注入 → LLM 用 `[Fn]` 引用 → 点击跳回原文件
4. **一套** 提取管线(text-layer → OCR fallback → 状态反馈)

差异**只在作用域** —— 决定文件库属于哪个 scope:

| Chat 面          | scope_kind       | scope_ref            | 含义                                            |
| ---------------- | ---------------- | -------------------- | ----------------------------------------------- |
| 患者 chat        | `patient`        | `patient_hash`       | 这个患者的所有文件(影像、化验单、外院报告)    |
| per-study 研究   | `research`       | `study_id`           | 这个研究的资料库(协议、文献、表格)            |
| 跨研究 chat      | `cross_research` | `__workspace__`      | 跨研究通用资料库(共享指南、数据表)            |
| 助理 chat        | `assistant`      | `__workspace__`      | 助理专属知识库(医生个人的总结笔记)            |

---

## 2. 支持的文件类型(统一)

| 类型              | 扩展名                          | 提取路径                          | OCR fallback   |
| ----------------- | ------------------------------- | --------------------------------- | -------------- |
| 纯文本            | `.txt .md .csv .tsv .json`      | UTF-8 直读                        | n/a            |
| PDF               | `.pdf`                          | pypdf text layer                  | ✓ Gemini Vision |
| Word              | `.docx .doc(老格式拒绝)`        | python-docx → xml fallback        | n/a            |
| Excel             | `.xlsx .xls`                    | openpyxl/xlrd → markdown 表       | n/a            |
| 图像              | `.png .jpg .jpeg .webp .heic`   | 二进制保留,Vision LLM 直接喂图   | n/a (本身是图像)|
| DICOM             | `.dcm .zip(DICOM)`              | **不走文件库**,走 dicom_router    | n/a            |

**统一拒绝清单:**
- `.exe .dmg .iso` 等可执行/镜像
- 单文件 > 50 MB
- 单库总文件数 > 50

---

## 3. PDF 处理增强(F-pdf-ocr-fallback)

### 问题确认

刚才测试:
- ✅ **文字层 PDF**(reportlab 生成的) → pypdf 抽出文本正常,中英文都没问题
- ❌ **扫描版 PDF**(医院系统导出,纯图像) → pypdf 返回空字符串 → distiller 退化成 `[PDF 'xxx' — text extraction unavailable; N bytes]` 占位 stub → **LLM 看不到任何实际内容**

这是医生最容易撞上的:外院影像报告 / 老化验单 / 手写处方扫描件,全是扫描版。

### 三层 fallback 链

```
1. pypdf text layer
   ├─ 成功 (text 长度 > 100 chars 且非空) → done, status='text_layer'
   └─ 失败/极短
       ↓
2. Gemini Vision PDF 直读  (Gemini 2.5 Flash 原生支持 PDF input!)
   ├─ 把 PDF 整个 base64 发给 Gemini, prompt "请提取这份文档的全部文本"
   ├─ 成功 → done, status='vision_ocr'
   └─ 失败/无 key
       ↓
3. 标记为不可读 + 仍把元数据(name + size + first_page_thumbnail)塞进 prompt
   告诉 LLM "这个文件无法提取文本,如需内容请让医生重新上传文字版"
   status='unreadable'
```

### 状态反馈

`uploads` 表加字段 `text_extraction_status TEXT NOT NULL DEFAULT 'pending'`:
- `pending` — 还在异步提取
- `text_layer` — pypdf 成功
- `vision_ocr` — 走了视觉模型
- `unreadable` — 扫描版且 vision 也失败
- `encrypted` — 受密码保护
- `error: <msg>` — 其他错误

UI 在 chip 上显示状态徽章:
- ✅ `text_layer` — 不显示(默认 OK)
- 🤖 `vision_ocr` — 小图标,鼠标悬停提示"由 AI 视觉识别提取"
- ⚠ `unreadable / encrypted` — 红色感叹号 + 提示
- ⏳ `pending` — 转圈

---

## 4. 数据模型

### Schema 改动

加一个迁移 0005:

```sql
-- 给 uploads 加文件库归属 + 提取状态 + 软删字段
ALTER TABLE uploads ADD COLUMN lib_scope_kind TEXT NOT NULL DEFAULT '';
ALTER TABLE uploads ADD COLUMN lib_scope_ref  TEXT NOT NULL DEFAULT '';
ALTER TABLE uploads ADD COLUMN text_extraction_status TEXT NOT NULL DEFAULT 'pending';
ALTER TABLE uploads ADD COLUMN deleted_at INTEGER;          -- 软删

CREATE INDEX IF NOT EXISTS idx_uploads_lib
  ON uploads(user_id, lib_scope_kind, lib_scope_ref)
  WHERE deleted_at IS NULL;
```

为什么不开新表:`uploads` 已经持有 file_id + bytes + extracted_text + name + mime + sha256;加几个字段比维护一张关联表轻。

### 上传时

前端 `acceptFiles` 透传 `libScopeKind` + `libScopeRef`:
```ts
api.uploadFile(file, name, {
  libScopeKind: 'patient',          // or 'research' / 'cross_research' / 'assistant'
  libScopeRef:  p.patientHash,      // or study_id / '__workspace__'
});
```

后端 `/files/upload` 写入对应字段,触发异步提取:
- text/md/csv/json:同步抽
- pdf/docx/xlsx:同步抽
- 图像:不抽 text,等 LLM 用 Vision 读

---

## 5. API(统一)

```
GET    /api/v1/chat/files?scope_kind=&scope_ref=
       → { files: [{file_id, name, mime, size_bytes, created_at,
                    f_id_token, text_extraction_status, has_text}],
           total_active, total_removed }

DELETE /api/v1/chat/files/{file_id}              → soft delete

POST   /api/v1/chat/files/{file_id}/restore      → 撤销 soft delete (7d 内)

GET    /api/v1/chat/files/removed?scope_kind=&scope_ref=
       → 已移除列表 + expires_in_days

POST   /api/v1/chat/files/{file_id}/reextract    → 手动重跑 OCR / 提取
       (用于:首次失败但医生想再试 / 修了 Gemini key 之后)
```

---

## 6. UI 组件层

### 6.1 `<ChatFileLib>` — 抽屉/面板组件

接收 `scopeKind`, `scopeRef`,在 4 个 chat 里渲染相同 UI(只是数据范围不同):

```
┌─ 参考文件库 (这个患者 / 这个研究 / 跨研究 / 助理) ──┐
│  [当前 (4)]  [已移除 (2)]                          │
│  ─────                                              │
│  [F1] ✅ RECIST_v1.1.pdf       120K  2h 前  👁 ✕    │
│  [F2] 🤖 lab_2026_05_18.pdf     45K  1h 前  👁 ✕    │  ← 走了 OCR
│  [F3] ⚠ scan_old_xray.pdf      680K  1d 前  👁 🔄 ✕│  ← 不可读,可重试
│  [F4] ✅ cohort_ae.xlsx         32K 30m 前  👁 ✕    │
│  ─────                                              │
│  [+ 添加(拖拽 / 粘贴 / 选择)]                       │
└─────────────────────────────────────────────────────┘
```

### 6.2 `<ChatComposer>` — 持久 chip 条

每个 chat 的 composer 上方:

```
┌──────────────────────────────────────────────────────┐
│  📂 4 个参考文件:                                     │
│  [F1 RECIST.pdf ✕] [F2 lab.pdf 🤖 ✕] [F3 ⚠ ...]    │
│  + 1 more  [全部]                                    │
│  ┌─────────────────────────────────────┐             │
│  │ 📎 输入框 + 发送                     │             │
│  └─────────────────────────────────────┘             │
└──────────────────────────────────────────────────────┘
```

- chip 限 3-4 个 + "全部"链接打开 `<ChatFileLib>` 抽屉
- chip 显示状态徽章
- 拖拽/粘贴到 composer 区域 = 加入库,不是"绑这一轮"

### 6.3 4 个 chat 适配

| Chat 面            | 数据传入             | 备注                                                  |
| ------------------ | -------------------- | ----------------------------------------------------- |
| EncounterMode      | `scope='patient', ref=p.patientHash` | 删患者时 cascade 删库                |
| Research ChatTab   | `scope='research', ref=studyId`       | 删研究时 cascade 删库                |
| CrossResearchChat  | `scope='cross_research', ref='__workspace__'` | 全局共享                  |
| AssistantWorkspace | `scope='assistant', ref='__workspace__'`      | 跟助理 facts 互补          |

**所有 4 个 chat 的 composer 都用同一个 `<ChatComposer scopeKind={..} scopeRef={..} />`**

---

## 7. LLM 注入(统一)

每个 chat 的 system prompt 里都注入相同结构的文件块:

```
═══════════════════════════════════════════════════════════════════
REFERENCE FILES (this <scope_kind> library; cite as [F1], [F2], ...)
═══════════════════════════════════════════════════════════════════
  [F1] RECIST_v1.1.pdf  (application/pdf, text_layer)
       --- excerpt ---
       Complete response (CR) requires disappearance of all target ...
       --- end excerpt ---

  [F2] lab_2026_05_18.pdf  (application/pdf, vision_ocr)
       --- excerpt ---
       Hemoglobin: 12.1 g/dL  WBC: 5.6 K/uL  ...
       --- end excerpt ---
       ★ note: extracted by AI vision; verify before clinical use

  [F3] scan_old_xray.pdf  (application/pdf, unreadable)
       [content unavailable — scanned image, text extraction failed.
        Ask the medic to re-upload a text version OR examine the
        page thumbnail provided via tools.read_uploaded_file]

  [F4] cohort_ae.xlsx  (xlsx, text_layer)
       --- excerpt (markdown table) ---
       | patient | grade | onset_day | resolution |
       | abc123  | G3    | 28        | unresolved |
       ...

CITATION RULES FOR FILES:
  - Cite [Fn] inline when grounding on file content
  - Never invent an [Fn] — only IDs listed above
  - For status=vision_ocr files, mention "(per OCR)" if the answer is medical-decision-critical
```

### Server 实现要点

新函数 `_gather_file_lib(conn, user_id, scope_kind, scope_ref) → str`,**单一函数**所有 chat 都调:

```python
def _gather_file_lib(conn, user_id, scope_kind, scope_ref) -> str:
    rows = conn.execute("""
        SELECT file_id, name, mime, extracted_text, text_extraction_status
          FROM uploads
         WHERE user_id = ?
           AND lib_scope_kind = ?
           AND lib_scope_ref  = ?
           AND deleted_at IS NULL
         ORDER BY created_at ASC
    """, (user_id, scope_kind, scope_ref)).fetchall()
    # ... render with [F1] [F2] tokens + status badges + excerpts
```

`retrieve_async` 里:

```python
file_block = _gather_file_lib(conn, user_id, scope_kind, scope_ref)
# 拼到 system prompt 的 reference section
```

---

## 8. 实施分期

| Phase | 范围                                                            | 估时    |
| ----- | --------------------------------------------------------------- | ------- |
| **0** Schema | migration 0005 加字段                                          | 15 min  |
| **1** 后端核心 API | `_gather_file_lib` + GET/DELETE/restore endpoints + 上传时绑 scope | 1 hr    |
| **2** PDF OCR fallback | distiller 加 Gemini Vision 兜底 + status 字段        | 1 hr    |
| **3** 统一 UI 组件 | `<ChatFileLib>` 抽屉 + `<ChatComposer>` chip 条           | 1.5 hr  |
| **4** LLM `[Fn]` 注入 | system prompt + file_citations SSE 事件                 | 45 min  |
| **5** `[Fn]` chip 渲染 + file viewer | 扩 `CitationChip2`                       | 30 min  |
| **6** 4 个 chat 接入 | 每个 chat 把 attachments 替换成 `<ChatComposer>` + lib | 1 hr    |
| **7** 已移除恢复 + 7d GC | UI tab + cron                                       | 30 min  |
| **合计** |                                                              | **~6.5 hr** |

---

## 9. MVP vs 完整体

按你之前确认的 "B + C 都做",对应到统一模型:
- **MVP-1**(Phase 0-3,~3.5 hr):4 个 chat 都有持久库 + UI 看得见 + 能管 + OCR fallback
- **MVP-2**(Phase 0-5,~5 hr):MVP-1 + LLM `[Fn]` 引用 + 点击跳回
- **完整体**(Phase 0-7,~6.5 hr):MVP-2 + 已移除恢复 + GC

---

## 10. 验收测试矩阵

| 场景                                            | 期望                                                                  |
| ----------------------------------------------- | --------------------------------------------------------------------- |
| 文字层 PDF 上传                                 | 提取成功,status='text_layer',chip 无徽章,LLM 能引用 `[F1]`           |
| 扫描版 PDF 上传                                 | pypdf 失败,Vision fallback 成功,status='vision_ocr',chip 显 🤖      |
| Vision 也失败(无 key / 加密)                  | status='unreadable',chip 显 ⚠ + "重试"按钮                            |
| 同一文件在患者 chat 和跨研究 chat 上传          | 各自独立 file_id,不互通                                              |
| 患者 chat 上传文件 → 删除患者                   | 该库整体 cascade 软删                                                 |
| 在 4 个 chat 任意一个拖拽相同文件               | UI 体验完全一致(同样的 chip、同样的状态、同样的全部按钮)            |
| LLM 在回答里引用 `[F2]`                         | chip 渲染,点击打开 file viewer 定位到 F2                              |
| 文件总数到 50,再上传                            | 阻止 + 弹"库已满"对话框                                               |
| 上传超大 PDF (40 MB)                            | 上传通过,提取 cap 在前 60K chars,prompt 标 `(truncated, X more)`     |

---

## 11. 关键决策记录

1. **D1 · 文件作用域** — 每个 chat 一个独立 scope。患者私有 / 研究私有 / 跨研究共享 / 助理共享。
2. **D2 · UI 完全一致** — 一份 `<ChatFileLib>` + `<ChatComposer>` 组件,4 处复用,**唯一差异是 props**。
3. **D3 · OCR fallback** — Gemini Vision 兜底(已有 LLM gateway 直接用)。不引入 Tesseract(避免额外 200MB 依赖)。
4. **D4 · Soft delete + 7 天 GC** — 防误删。
5. **D5 · Status 字段透明化** — 医生能看到提取失败/降级,不被静默欺骗。
6. **D6 · 跨身份强隔离** — `user_id` + `lib_scope_kind/ref` 双重过滤,F-multiuser-isolation 修好后双保险。

---

## 12. 与已有设计的关系

- `CROSS_RESEARCH_FILES.md`(之前我写的) → **作废**,被这份取代(它是这份的 cross-research 子集)
- `ASSISTANT_TAB.md` → 不变,但其 "助理参考库" 部分用这份的 `<ChatFileLib>` 实现
- `ASSISTANT_TAB_IMPL_PLAN.md` → 助理 Phase 0 直接复用 ChatFileLib 即可
- `m3-memory-architecture.md` → 文件作为外部记忆源,跟 Layer 1 graph 互补
- `USER_MANAGEMENT.md` → 文件按 user_id 隔离,跨设备同步走 §11 的 file_id+sha256 同步管线
