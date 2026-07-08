# 影像 / 检查数据 患者隔离 Bug 排查报告

> **状态**: 已定位根因，尚未提交修复。等待 review 后落地。
> **影响**: 患者安全级 P0。新建患者后上传 imaging，quick scan 的 findings 可能写入**另一个旧患者**的 clinical_graph_nodes。
> **作者**: AI 助理；2026-06-15
> **相关文件**: `packages/server/nexus_server/files.py`、`dicom.py`、`quick_scan.py`、`memorization/dicom_ingester.py`、`event_sourcing/handlers.py`

---

## 1. 一句话结论

当**同一个 `StudyInstanceUID` 被先后绑定到两个不同患者**时（再次上传同一份 DICOM、teaching 用例、PACS 错配等），后台 ingester + quick scan 在解析 `uploads.patient_hash` 时使用了**尚未写入** `dicom_study_id` 的新 row，因此匹配到的是上一次属于旧患者的 row，**findings 落到旧患者名下**。

这一行为完全符合用户描述：新患者下 findings 为空，旧患者的 findings 凭空增加。

---

## 2. 端到端调用链 + Bug 触发位置

```
desktop modes.tsx:707          uploadFile(file, { patientHash: H_new })
   │                            （前端传入正确 — 已验证）
   ▼
files.py:434  POST /files/upload
   • Form: patient_hash = H_new, force_patient_hash=True
   • INSERT uploads (file_id=R_new, patient_hash=H_new, dicom_study_id='')   ← dicom_study_id 此时还是空串
   • 安排 _run_dicom_prerender_async 为 BackgroundTask
   • 立即返回 200
   │
   ▼ （后台 — 在 response 之后）
files.py:833  _run_dicom_prerender_async
   └─ persist_study(patient_hash_override=H_new)
       ↓
       dicom.py:942  SELECT FROM dicom_studies WHERE study_instance_uid = S1
       └─ HIT — existing 行（属于旧患者 H_old）
          dicom.py:947-963  UPDATE dicom_studies SET upload_file_id=R_new, extract_dir=...
                            ★ BUG #2: 完全没有 UPDATE patient_hash ←──
                            ★         patient_hash_override 被静默丢弃
   • 返回 new_study_id = U1
   │
   ▼
files.py:956  _run_dicom_ingester_safe(study_id=U1, file_id=R_new, force_patient_hash=True)
   • files.py:1925-1930
       SELECT patient_hash FROM uploads
       WHERE user_id=? AND dicom_study_id=? LIMIT 1
       ★ BUG #1: 用 dicom_study_id 而非 file_id 查找
       ★         R_new.dicom_study_id 此刻还是 '' → 唯一匹配的是 R_old
   • patient_hash 解析为 H_old → 图节点写入 H_old
   │
   ▼
files.py:989  _run_quick_scan_after_ingest(study_id=U1)
   • files.py:2091-2098
       SELECT patient_hash FROM uploads
       WHERE user_id=? AND dicom_study_id=?
       ORDER BY created_at DESC LIMIT 1
       ★ BUG #1（同样的问题）
   • 解析为 H_old → 每个 flagged finding 以 patient_hash=H_old 发布
                    NODE_ADDED event → clinical_graph_nodes
   │
   ▼
files.py:1013  UPDATE uploads SET dicom_study_id=U1, patient_hash=H_new WHERE file_id=R_new
files.py:1025  UPDATE dicom_studies SET patient_hash=H_new WHERE study_id=U1
   ★ BUG #3: 这两个 UPDATE 发生在 ingester 与 quick_scan 之后 ——
   ★         即使修好以上读取，时序上也已经迟了
```

---

## 3. 三个独立 Bug 站点（按修复优先级）

### Bug #1 — 错用 `dicom_study_id` 而非 `file_id` 查找 `patient_hash`

**位置**:
- `packages/server/nexus_server/files.py:1925-1930`（`_run_dicom_ingester_safe`）
- `packages/server/nexus_server/files.py:2091-2098`（`_run_quick_scan_after_ingest`）
- `packages/server/nexus_server/files.py:2227` 附近（`retry_quick_scan_for_study`，同样有 `ORDER BY created_at DESC LIMIT 1`）

`_run_dicom_ingester_safe` 的签名里已经收到 `file_id`，但**没有用**；它只用 `dicom_study_id` 去 uploads 表里查。`_run_quick_scan_after_ingest` 干脆连 `file_id` 都没接。

**正确做法**: 直接按 `file_id` 唯一查询。`file_id` 是 uploads 的主键，永远唯一，永远在该函数被调用前已经存在。

### Bug #2 — `persist_study` UPSERT 分支静默丢弃 `patient_hash_override`

**位置**: `packages/server/nexus_server/dicom.py:940-963`

```python
if existing:
    study_id = existing[0]
    conn.execute("DELETE FROM dicom_instances ...")
    conn.execute("DELETE FROM dicom_series ...")
    conn.execute(
        "UPDATE dicom_studies SET upload_file_id = ?, "
        "extract_dir = ? WHERE study_id = ?",
        (upload_file_id, str(extract_dir), study_id),
    )                          # ← 没有 patient_hash 字段
else:
    effective_patient_hash = (
        patient_hash_override.strip()
        if patient_hash_override and patient_hash_override.strip()
        else study.patient_hash
    )
    conn.execute("INSERT INTO dicom_studies ... patient_hash ... VALUES ...",
                 (..., effective_patient_hash, ...))   # ← else 分支正确
```

`else` 分支正确处理了 override；`if existing` 分支却把它扔了。结果是：旧绑定的 `patient_hash=H_old` 留在 `dicom_studies` 行里，被下游 `load_study()` 读出。

### Bug #3 — `uploads.dicom_study_id` 的 UPDATE 发生在 ingester 与 quick scan **之后**

**位置**: `packages/server/nexus_server/files.py:946-1052`

时序：

| 顺序 | 行号 | 动作 |
|------|------|------|
| 1 | 956 | `_run_dicom_ingester_safe` — 此时读 `uploads.dicom_study_id` 还是 `''` |
| 2 | 989 | `_run_quick_scan_after_ingest` — 同上 |
| 3 | 1013 | `UPDATE uploads SET dicom_study_id = U1` ← 才把 U1 写进去 |
| 4 | 1025 | `UPDATE dicom_studies SET patient_hash = H_new` ← 才纠正 dicom_studies |

即使 Bug #1 修了，只要还有人按 `dicom_study_id` 查，时序仍然错。最干净的做法是把第 3 步移到 1 之前，**或**让所有下游查询改用 `file_id`。

---

## 4. 验证过的下游 / 邻接系统

| 路径 | 状态 |
|------|------|
| `clinical_graph_nodes` 表（`_h_node_added` in `event_sourcing/handlers.py:133-156`） | ✓ 正确 — 持久化时取的是 event row 的 `patient_hash`，handler 本身没有 bug |
| `assistant_response.metadata.patient_hash` （`quick_scan.py:488,825`） | ✗ 会被同样污染（来自 `study.patient_hash`） |
| 前端 `uploadFile` 传参（`api-client.ts:475-520`、`modes.tsx:707`） | ✓ 已经正确传入 `activePatient.patientHash` |
| Labs 写入 (`chat_router_v2.py:347-352` → `chat_ingester` → `_h_node_added`) | ✓ **目前不受影响**。Labs 走 chat 路径，`patient_hash` 直接从 `ChatRequest` 拿，不经过 uploads.dicom_study_id 这种间接 lookup。`memorization/__init__.py:12` 标记 `lab_ingester` 为 M5 未实现 |
| `vector_index` chunks | ⚠ 全局按 user_id 隔离，**没有** patient_hash 列。Research Workspace 阶段需要补 |

**结论：Labs 暂时安全，但当 M5 lab_ingester 上线时，必须按 file_id 而非外部识别码绑定 patient_hash，避免重蹈覆辙。**

---

## 5. 推荐的修复方案（**等 review 后再落地**）

按依赖关系编号；可以单独提交也可以打包成一个 PR。

### Fix-A：所有下游 lookup 改成按 `file_id` 查 patient_hash

- 修改 `_run_dicom_ingester_safe`：把现有的 `SELECT … WHERE dicom_study_id=?` 改成 `SELECT … WHERE file_id=?`。`file_id` 已经在签名里。
- 修改 `_run_quick_scan_after_ingest`：在签名中增加 `file_id`，由 `_run_dicom_prerender_async` 传入；同样按 `file_id` 查。
- 修改 `retry_quick_scan_for_study`：先按 `dicom_study_id` 找到 file_id，再传下去。
- 一并去掉 `LIMIT 1 / ORDER BY created_at DESC` 这类"碰运气"的兜底语句，让 SQL 严格唯一。

### Fix-B：`persist_study` 的 UPSERT-UPDATE 分支也要 honor override

- 在 `dicom.py:947-963` 的 UPDATE 加上 `patient_hash = COALESCE(NULLIF(?, ''), patient_hash)`，从而和 `else` 分支统一。
- 这样即使将来有别的调用绕过我们的 Fix-A，也能让 `dicom_studies.patient_hash` 永远与最新的 override 一致。

### Fix-C：调整时序，把 `dicom_study_id` UPDATE 移到 ingester / quick scan **之前**

- 在 `files.py` 里，`persist_study` 一返回 `new_study_id` 就立刻 `UPDATE uploads SET dicom_study_id = ?, patient_hash = COALESCE(NULLIF(?, ''), patient_hash) WHERE file_id = ?`。
- 然后再跑 `_run_dicom_ingester_safe` 和 `_run_quick_scan_after_ingest`。
- 这样 Fix-A、Fix-B 即使写得不够防御性，时序上也不会被绕过去。

### Fix-D：防御性 guardrail —— 不允许 patient_hash 跨绑定

- 在 `_run_quick_scan_after_ingest` 解析 `patient_hash` 后做一次断言：
  ```sql
  SELECT DISTINCT patient_hash FROM uploads WHERE dicom_study_id = ?
  ```
  如果返回的 distinct 数 > 1，**直接 raise**，让 row 进入 `quick_scan_status='error'`，前端会显示明显的红色 retry 按钮。患者安全优先于"自动完成"。

### Fix-E（可选）：在 quick_scan worker 内把 `patient_hash` 改为**形参**

- 当前 `quick_scan._run_quick_scan_async` 自己从 `load_study()` 读 `study.patient_hash`。改成由 router 显式传入，能根除"二次读 dicom_studies 仍然读到污染值"的风险。

---

## 6. 防回归测试用例（建议放到 `packages/server/tests/`）

| 测试名 | 期望 |
|--------|------|
| `test_quick_scan_finding_uses_new_patient_on_reupload` | 同一 StudyInstanceUID 第二次绑定到 H_new。finding 节点应全部落在 H_new；H_old 不应新增任何 finding；`dicom_studies.patient_hash` 应为 H_new |
| `test_persist_study_upsert_honors_patient_hash_override` | 单元测试：第二次 `persist_study(override=H_new)` 之后，`dicom_studies.patient_hash = H_new` |
| `test_run_dicom_ingester_safe_looks_up_by_file_id` | 同一 dicom_study_id 下有两条 uploads，分属不同 patient_hash。按 file_id 查必须返回正确那一条 |
| `test_quick_scan_after_ingest_with_unwritten_dicom_study_id` | 模拟 Bug #3 的时序：新 row 的 `dicom_study_id` 还是空串时调用，结果不能写到旧患者 |
| `test_post_quick_scan_route_resolves_correct_patient` | 端到端：HTTP 层 POST quick-scan，验证 finding 节点的 patient_hash 与最新 upload row 一致 |
| `test_multiple_uploads_same_study_uid_distinct_patient_hashes_raises` | Fix-D 的兜底：跨绑定时应 raise，不应静默继续 |
| `test_labs_isolation_when_lab_ingester_ships` | 提前给 M5 lab_ingester 留位置：当它实现后，labs 也必须按 file_id 而非 source_id 绑 patient |

---

## 7. 提交建议

**不要直接一次性合并 Fix-A + B + C + D + E。** 建议拆成两个 PR：

- **PR-1（紧急）**: Fix-A + Fix-B + Fix-C + 上面前 4 条回归测试。这是最小修复集，能彻底关闭已知泄漏路径。
- **PR-2（防御加固）**: Fix-D + Fix-E + 其余测试。可以慢一点。

修复 PR 上线后建议**回看历史**：
```sql
-- 找出可能受影响的 finding 节点：
SELECT cgn.patient_hash, cgn.node_id, cgn.content_json
FROM clinical_graph_nodes cgn
WHERE cgn.source = 'quick_scan'
  AND cgn.study_id IN (
    -- 同一 study_id 在 uploads 表中有过多个 patient_hash 绑定
    SELECT dicom_study_id
    FROM uploads
    WHERE dicom_study_id != ''
    GROUP BY dicom_study_id
    HAVING COUNT(DISTINCT patient_hash) > 1
  );
```
对于每条潜在错挂的 finding，要么让医生手动确认，要么标记 `status='needs_review'` 并在 UI 上提示。
