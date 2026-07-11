"""Writing Studio HTTP surface (P1 MVP).

See docs/design/WRITING_STUDIO_DESIGN.docx. Implements the server side
of the in-app writing mode: documents + version snapshots + data
reference chips (auto de-identified at insert time) + selection polish
(SSE) + PHI scan + docx export gated on PHI resolution.

Endpoints (all mounted under /api/v1, all auth-gated via
``Depends(get_current_user)``; every query is user_id-scoped so one
medic can never touch another's documents):

  GET    /docs                              list (id, title, updated_at, ref_count)
  POST   /docs                              create {title}
  GET    /docs/{id}                         full doc + references
  PUT    /docs/{id}                         save {title?, body?} (+snapshot on body change)
  DELETE /docs/{id}                         delete doc + refs + snapshots
  GET    /docs/{id}/snapshots               version chain (latest 50)
  POST   /docs/{id}/snapshots/{sid}/restore restore a snapshot
  POST   /docs/{id}/references              insert a de-identified data chip
  POST   /docs/{id}/polish                  SSE selection rewrite
  POST   /docs/{id}/phi-scan                regex + roster-name PHI findings
  POST   /docs/{id}/export                  expand chips → .docx (PHI gate)

De-identification rules (design §5):
  patient  — name/initials → 'P-' + patient_hash[:6]; birth data → age;
             absolute dates in the timeline → relative 'D+N周' (falls
             back to month precision YYYY-MM when unparseable); MRN
             scrubbed. Snapshot is FROZEN at insert time.
  study    — aggregate counts only ('42/60 例'); roster members appear
             as screening codes (SHORTCODE-007), never hashes/names.
  file     — reuses the distill/summary text stored at upload time.

Audit: every reference insert emits a DOC_REFERENCE_CREATED event into
twin_event_log via Store.emit_and_apply (who / when / which doc /
which target / which granularity).
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import uuid
from datetime import date, datetime, timezone
from io import BytesIO
from typing import Any, AsyncIterator, Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, Field

from nexus_server.auth.routes import get_current_user
from nexus_server.database import get_db_connection

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1", tags=["writing"])

# Version-snapshot retention per doc. Older rows are pruned on save.
_SNAPSHOT_CAP = 50

# {{ref:UUID}} chip placeholder in doc bodies.
_REF_PLACEHOLDER_RE = re.compile(r"\{\{ref:([0-9a-fA-F-]{8,64})\}\}")

# Numbers for the provenance (hallucinated-value) check.
_NUM_RE = re.compile(r"\d+(?:\.\d+)?%?")

# ── PHI regexes (layer 1) ────────────────────────────────────────────
# Full dates: 2025年3月14日 / 2025-03-14 / 2025/3/14 / 2025.3.14.
_PHI_DATE_RE = re.compile(
    r"((?:19|20)\d{2})\s*[年/\-.]\s*(\d{1,2})\s*[月/\-.]\s*(\d{1,2})\s*日?"
)
# 18-digit CN resident ID (last char may be X).
_PHI_ID_RE = re.compile(r"(?<![\dXx])\d{17}[\dXx](?![\dXx\d])")
# CN mobile numbers.
_PHI_PHONE_RE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")


# ─────────────────────────────────────────────────────────────────────
# Schema (defensive mirror of database.init_db — idempotent)
# ─────────────────────────────────────────────────────────────────────

def _ensure_schema(conn: sqlite3.Connection) -> None:
    """Bring up the Writing Studio tables if a stale deployment booted
    before database.init_db learned about them. Idempotent."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS docs (
            id         TEXT PRIMARY KEY,
            user_id    TEXT NOT NULL,
            title      TEXT NOT NULL DEFAULT '',
            body       TEXT NOT NULL DEFAULT '',
            created_at TIMESTAMP NOT NULL,
            updated_at TIMESTAMP NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS doc_references (
            id           TEXT PRIMARY KEY,
            doc_id       TEXT NOT NULL,
            user_id      TEXT NOT NULL,
            ref_type     TEXT NOT NULL,
            target_id    TEXT NOT NULL,
            granularity  TEXT NOT NULL DEFAULT '',
            snapshot     TEXT NOT NULL DEFAULT '',
            source_nodes TEXT NOT NULL DEFAULT '[]',
            created_at   TIMESTAMP NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS doc_snapshots (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            doc_id     TEXT NOT NULL,
            user_id    TEXT NOT NULL,
            body       TEXT NOT NULL,
            label      TEXT NOT NULL DEFAULT '',
            created_at TIMESTAMP NOT NULL
        )
        """
    )


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _get_doc_or_404(
    conn: sqlite3.Connection, user_id: str, doc_id: str,
) -> sqlite3.Row:
    row = conn.execute(
        "SELECT id, user_id, title, body, created_at, updated_at "
        "FROM docs WHERE user_id = ? AND id = ?",
        (user_id, doc_id),
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="doc not found")
    return row


def _sse(event: dict) -> str:
    """Serialise a chunk as an SSE message (same convention as
    chat_router)."""
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n"


# ─────────────────────────────────────────────────────────────────────
# Models
# ─────────────────────────────────────────────────────────────────────

class DocCreateRequest(BaseModel):
    title: str = Field("", max_length=500)


class DocUpdateRequest(BaseModel):
    title: Optional[str] = Field(None, max_length=500)
    body: Optional[str] = None
    snapshot_label: str = Field("save", max_length=200)


class ReferenceCreateRequest(BaseModel):
    ref_type: str = Field(..., pattern="^(patient|study|file)$")
    target_id: str = Field(..., min_length=1, max_length=256)
    # patient: basics | timeline    study: progress | roster
    # file: summary (default)
    granularity: str = Field("basics", max_length=64)


class PolishRequest(BaseModel):
    selection: str = Field(..., min_length=1)
    instruction: str = ""
    ref_ids: list[str] = []


class PhiScanRequest(BaseModel):
    # Optional override so the frontend can scan an unsaved draft.
    body: Optional[str] = None


class ExportRequest(BaseModel):
    # Each resolution addresses one phi-scan finding. Matching is by
    # exact (start, end) span OR by excerpt string. Optional
    # ``replacement`` rewrites the flagged text before export;
    # a resolution without replacement means "explicitly keep".
    resolutions: list[dict] = []
    include_sources: bool = False


# ─────────────────────────────────────────────────────────────────────
# De-identification helpers
# ─────────────────────────────────────────────────────────────────────

_GRANULARITY_LABELS = {
    "basics":   "基本特征",
    "timeline": "治疗时间线",
    "progress": "研究进展",
    "roster":   "roster 快照",
    "summary":  "摘要",
}


def _patient_code(patient_hash: str) -> str:
    return "P-" + (patient_hash or "")[:6]


def _scrub_terms(text: str, terms: list[str], replacement: str) -> str:
    """Replace every occurrence of each identifying term (and its
    whitespace-free variant) with ``replacement``."""
    out = text
    variants: list[str] = []
    for t in terms:
        t = (t or "").strip()
        if len(t) < 2:
            continue
        variants.append(t)
        squeezed = re.sub(r"\s+", "", t)
        if squeezed != t and len(squeezed) >= 2:
            variants.append(squeezed)
    # Longest first so "张三丰" wins over "张三".
    for v in sorted(set(variants), key=len, reverse=True):
        out = out.replace(v, replacement)
    return out


def _parse_date(y: str, m: str, d: str) -> Optional[date]:
    try:
        return date(int(y), int(m), int(d))
    except ValueError:
        return None


def _relativize_dates(text: str, anchor: Optional[date]) -> str:
    """Rewrite absolute full dates to 'D+N周' relative form (anchored
    at the earliest date in the reference material). Unparseable dates
    degrade to month precision YYYY-MM."""

    def _sub(m: re.Match) -> str:
        d = _parse_date(m.group(1), m.group(2), m.group(3))
        if d is None:
            return f"{m.group(1)}-{int(m.group(2) or 1):02d}"
        if anchor is None:
            return f"{d.year}-{d.month:02d}"
        delta_days = (d - anchor).days
        if delta_days == 0:
            return "D0"
        weeks = delta_days // 7 if delta_days > 0 else -((-delta_days) // 7)
        sign = "+" if delta_days > 0 else "-"
        return f"D{sign}{abs(weeks)}周"

    return _PHI_DATE_RE.sub(_sub, text)


def _earliest_date(texts: list[str]) -> Optional[date]:
    found: list[date] = []
    for t in texts:
        for m in _PHI_DATE_RE.finditer(t or ""):
            d = _parse_date(m.group(1), m.group(2), m.group(3))
            if d is not None:
                found.append(d)
    return min(found) if found else None


def _node_display(content: dict) -> str:
    """Best-effort human string for one clinical_graph node payload."""
    for key in ("label", "name", "text", "summary", "value"):
        v = content.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip()
    try:
        return json.dumps(content, ensure_ascii=False)[:120]
    except Exception:  # noqa: BLE001
        return ""


def _build_patient_snapshot(
    conn: sqlite3.Connection, user_id: str, patient_hash: str,
    granularity: str,
) -> tuple[str, list[Any], str]:
    """Compose the de-identified patient snapshot.

    Returns (snapshot_text, source_node_ids, chip_label). Defensive by
    design: any missing table / row just shortens the snapshot — this
    function never raises for missing data.
    """
    code = _patient_code(patient_hash)
    scrub: list[str] = []
    lines: list[str] = []
    source_nodes: list[Any] = []

    prow = None
    try:
        prow = conn.execute(
            "SELECT initials, mrn, age_value, age_group, sex, "
            "       chief_complaint, notes "
            "FROM patients WHERE user_id = ? AND patient_hash = ?",
            (user_id, patient_hash),
        ).fetchone()
    except sqlite3.Error as e:
        logger.debug("patients lookup failed: %s", e)

    if prow is not None:
        initials, mrn = str(prow[0] or ""), str(prow[1] or "")
        age_value, age_group = int(prow[2] or 0), str(prow[3] or "")
        sex, chief = str(prow[4] or ""), str(prow[5] or "")
        scrub = [initials, mrn]
        sex_label = {"M": "男", "F": "女"}.get(sex, "")
        age_label = (
            f"{age_value}岁" if age_value > 0
            else (f"{age_group}岁段" if age_group else "")
        )
        header = f"患者 {code}"
        details = "，".join(x for x in (age_label, sex_label) if x)
        if details:
            header += f"（{details}）"
        lines.append(header)
        if chief:
            lines.append(f"主诉：{chief}")
    else:
        lines.append(f"患者 {code}")

    if granularity == "timeline":
        rows: list[sqlite3.Row] = []
        try:
            rows = conn.execute(
                "SELECT node_id, node_type, content_json, encounter_id, "
                "       updated_at "
                "FROM clinical_graph_nodes "
                "WHERE user_id = ? AND patient_hash = ? "
                "  AND node_type IN "
                "      ('finding','measurement','med','study','encounter') "
                "ORDER BY updated_at ASC LIMIT 100",
                (user_id, patient_hash),
            ).fetchall()
        except sqlite3.Error as e:
            logger.debug("timeline nodes lookup failed: %s", e)

        entries: list[tuple[str, str]] = []  # (type_label, text)
        type_labels = {
            "finding": "发现", "measurement": "测量", "med": "用药",
            "study": "检查", "encounter": "就诊",
        }
        raw_texts: list[str] = []
        for r in rows:
            try:
                content = json.loads(r[2] or "{}")
            except (TypeError, ValueError):
                content = {}
            display = _node_display(content)
            if not display:
                continue
            date_hint = str(
                content.get("date") or content.get("study_date") or ""
            )
            text = f"{display}" + (f"（{date_hint}）" if date_hint else "")
            raw_texts.append(text)
            entries.append((type_labels.get(str(r[1]), str(r[1])), text))
            source_nodes.append(r[0])

        anchor = _earliest_date(raw_texts)
        if entries:
            lines.append("治疗时间线：")
            for tl, text in entries:
                lines.append(f"- [{tl}] {_relativize_dates(text, anchor)}")
        else:
            lines.append("治疗时间线：暂无结构化记录")

    snapshot = "\n".join(lines)
    # Final de-identification pass: names/MRN → code, then any
    # residual full dates / IDs / phone numbers scrubbed.
    snapshot = _scrub_terms(snapshot, scrub, code)
    snapshot = _relativize_dates(snapshot, None) if granularity != "timeline" else snapshot
    snapshot = _PHI_ID_RE.sub("[已脱敏]", snapshot)
    snapshot = _PHI_PHONE_RE.sub("[已脱敏]", snapshot)

    label = f"{code}·{_GRANULARITY_LABELS.get(granularity, granularity)}"
    return snapshot, source_nodes, label


def _build_study_snapshot(
    conn: sqlite3.Connection, user_id: str, study_id: str, granularity: str,
) -> tuple[str, list[Any], str]:
    """Study reference snapshot — aggregate counts / roster codes only,
    no per-patient identifiers (design §5: 成员患者一律以编号呈现)."""
    srow = None
    try:
        srow = conn.execute(
            "SELECT display_name, short_code, target_n, status "
            "FROM research_studies WHERE user_id = ? AND study_id = ?",
            (user_id, study_id),
        ).fetchone()
    except sqlite3.Error as e:
        logger.debug("research_studies lookup failed: %s", e)
    if srow is None:
        raise HTTPException(status_code=404, detail="study not found")

    display_name = str(srow[0] or "")
    short_code = str(srow[1] or "") or study_id[:8]
    target_n = srow[2]
    status = str(srow[3] or "")

    lines: list[str] = [f"研究 {short_code}（{display_name}）"]
    source_nodes: list[Any] = []

    def _count(sql: str, params: tuple) -> int:
        try:
            row = conn.execute(sql, params).fetchone()
            return int(row[0] or 0) if row else 0
        except sqlite3.Error:
            return 0

    enrolled = _count(
        "SELECT COUNT(*) FROM study_enrollments "
        "WHERE user_id = ? AND study_id = ? "
        "  AND status IN ('enrolled','completed')",
        (user_id, study_id),
    )
    screened = _count(
        "SELECT COUNT(*) FROM screening_evaluations "
        "WHERE user_id = ? AND study_id = ?",
        (user_id, study_id),
    )

    if granularity == "roster":
        lines.append(f"roster 快照（入组 {enrolled} 例）：")
        try:
            rows = conn.execute(
                "SELECT enrollment_seq, status, arm "
                "FROM study_enrollments "
                "WHERE user_id = ? AND study_id = ? "
                "ORDER BY enrollment_seq ASC LIMIT 200",
                (user_id, study_id),
            ).fetchall()
        except sqlite3.Error:
            rows = []
        for r in rows:
            seq, st, arm = int(r[0] or 0), str(r[1] or ""), str(r[2] or "")
            line = f"- {short_code}-{seq:03d}：{st}"
            if arm:
                line += f"（{arm} 组）"
            lines.append(line)
        if not rows:
            lines.append("- 暂无入组记录")
    else:  # 'progress' (default)
        target = str(target_n) if target_n else "?"
        lines.append(f"入组进度：{enrolled}/{target} 例")
        lines.append(f"筛查评估：{screened} 人次")
        if status:
            lines.append(f"研究状态：{status}")

    label = (
        f"{short_code}·{_GRANULARITY_LABELS.get(granularity, granularity)}"
    )
    return "\n".join(lines), source_nodes, label


def _build_file_snapshot(
    conn: sqlite3.Connection, user_id: str, file_id: str, granularity: str,
) -> tuple[str, list[Any], str]:
    """File/literature reference — reuse the distill/summary text
    captured at upload time (quick_scan_summary / memory_summary /
    extracted_text head)."""
    urow = None
    try:
        urow = conn.execute(
            "SELECT name, extracted_text, "
            "       COALESCE(quick_scan_summary, ''), "
            "       COALESCE(memory_summary, '') "
            "FROM uploads WHERE user_id = ? AND file_id = ?",
            (user_id, file_id),
        ).fetchone()
    except sqlite3.Error:
        # Older uploads schema without the summary columns.
        try:
            r = conn.execute(
                "SELECT name, extracted_text FROM uploads "
                "WHERE user_id = ? AND file_id = ?",
                (user_id, file_id),
            ).fetchone()
            urow = (r[0], r[1], "", "") if r else None
        except sqlite3.Error as e:
            logger.debug("uploads lookup failed: %s", e)
            urow = None
    if urow is None:
        raise HTTPException(status_code=404, detail="file not found")

    name = str(urow[0] or file_id)
    extracted = str(urow[1] or "").strip()
    qs_summary = str(urow[2] or "").strip()
    mem_summary = str(urow[3] or "").strip()

    summary = qs_summary or mem_summary or extracted[:1200]
    lines = [f"文件《{name}》"]
    if summary:
        lines.append(summary)
    else:
        lines.append("（无可用摘要）")
    return "\n".join(lines), [file_id], name


def _build_reference_snapshot(
    conn: sqlite3.Connection, user_id: str, ref_type: str, target_id: str,
    granularity: str,
) -> tuple[str, list[Any], str]:
    if ref_type == "patient":
        return _build_patient_snapshot(conn, user_id, target_id, granularity)
    if ref_type == "study":
        return _build_study_snapshot(conn, user_id, target_id, granularity)
    return _build_file_snapshot(conn, user_id, target_id, granularity)


def _chip_label_for_row(
    conn: sqlite3.Connection, user_id: str, ref_row: sqlite3.Row,
) -> str:
    """Recompute a chip label from a doc_references row (cheap lookups,
    all defensive)."""
    ref_type = str(ref_row["ref_type"])
    target_id = str(ref_row["target_id"])
    granularity = str(ref_row["granularity"])
    g_label = _GRANULARITY_LABELS.get(granularity, granularity)
    if ref_type == "patient":
        return f"{_patient_code(target_id)}·{g_label}"
    if ref_type == "study":
        short = target_id[:8]
        try:
            r = conn.execute(
                "SELECT short_code FROM research_studies "
                "WHERE user_id = ? AND study_id = ?",
                (user_id, target_id),
            ).fetchone()
            if r and r[0]:
                short = str(r[0])
        except sqlite3.Error:
            pass
        return f"{short}·{g_label}"
    # file
    try:
        r = conn.execute(
            "SELECT name FROM uploads WHERE user_id = ? AND file_id = ?",
            (user_id, target_id),
        ).fetchone()
        if r and r[0]:
            return str(r[0])
    except sqlite3.Error:
        pass
    return target_id[:16]


# ─────────────────────────────────────────────────────────────────────
# PHI scan
# ─────────────────────────────────────────────────────────────────────

def _placeholder_spans(body: str) -> list[tuple[int, int]]:
    return [(m.start(), m.end()) for m in _REF_PLACEHOLDER_RE.finditer(body)]


def _inside_any(start: int, end: int, spans: list[tuple[int, int]]) -> bool:
    return any(s <= start and end <= e for s, e in spans)


def _phi_scan_body(
    conn: sqlite3.Connection, user_id: str, body: str,
) -> list[dict]:
    """Two-layer PHI scan over manually-typed doc text.

    Layer 1 — regex: full dates, 18-digit resident IDs, CN mobile
    numbers. Layer 2 — high-precision roster match: THIS user's
    patients table names (initials field + whitespace-free variants)
    flagged wherever they appear.

    Chip placeholders ({{ref:...}}) are skipped — their content was
    de-identified at insert time.
    """
    findings: list[dict] = []
    skip_spans = _placeholder_spans(body)
    taken: list[tuple[int, int]] = []

    def _add(kind: str, m_start: int, m_end: int, excerpt: str,
             suggestion: str) -> None:
        if _inside_any(m_start, m_end, skip_spans):
            return
        # Overlap guard: an 18-digit ID also matches the phone regex's
        # digit run etc. First (higher-priority) finding wins the span.
        if any(not (m_end <= s or m_start >= e) for s, e in taken):
            return
        taken.append((m_start, m_end))
        findings.append({
            "kind": kind, "excerpt": excerpt,
            "start": m_start, "end": m_end,
            "suggestion": suggestion,
        })

    # Layer 2 first — patient names are the highest-value catch and
    # should win any span overlap with layer-1 patterns.
    try:
        rows = conn.execute(
            "SELECT patient_hash, initials FROM patients "
            "WHERE user_id = ? AND initials != ''",
            (user_id,),
        ).fetchall()
    except sqlite3.Error:
        rows = []
    for r in rows:
        phash, name = str(r[0]), str(r[1] or "").strip()
        if len(name) < 2:
            continue  # single chars are too noisy to flag
        code = _patient_code(phash)
        variants = {name, re.sub(r"\s+", "", name)}
        for v in variants:
            if len(v) < 2:
                continue
            for m in re.finditer(re.escape(v), body):
                _add(
                    "patient_name", m.start(), m.end(), m.group(0),
                    f"替换为研究编号 {code}",
                )

    # Layer 1 — IDs before phones (an ID contains a phone-shaped run).
    for m in _PHI_ID_RE.finditer(body):
        _add("id_number", m.start(), m.end(), m.group(0),
             "移除证件号或替换为研究编号")
    for m in _PHI_PHONE_RE.finditer(body):
        _add("phone", m.start(), m.end(), m.group(0), "移除电话号码")
    for m in _PHI_DATE_RE.finditer(body):
        _add("exact_date", m.start(), m.end(), m.group(0), "改为相对时间")

    findings.sort(key=lambda f: f["start"])
    return findings


# ─────────────────────────────────────────────────────────────────────
# Docs CRUD
# ─────────────────────────────────────────────────────────────────────

@router.get("/docs")
async def list_docs(
    current_user: str = Depends(get_current_user),
) -> dict[str, Any]:
    with get_db_connection() as conn:
        _ensure_schema(conn)
        rows = conn.execute(
            "SELECT d.id, d.title, d.updated_at, "
            "       (SELECT COUNT(*) FROM doc_references r "
            "         WHERE r.user_id = d.user_id AND r.doc_id = d.id) "
            "       AS ref_count "
            "FROM docs d WHERE d.user_id = ? "
            "ORDER BY d.updated_at DESC",
            (current_user,),
        ).fetchall()
        return {
            "docs": [
                {
                    "id": r[0], "title": r[1],
                    "updated_at": r[2], "ref_count": int(r[3] or 0),
                }
                for r in rows
            ]
        }


@router.post("/docs")
async def create_doc(
    req: DocCreateRequest,
    current_user: str = Depends(get_current_user),
) -> dict[str, Any]:
    doc_id = str(uuid.uuid4())
    now = _now()
    with get_db_connection() as conn:
        _ensure_schema(conn)
        conn.execute(
            "INSERT INTO docs (id, user_id, title, body, created_at, "
            "updated_at) VALUES (?, ?, ?, '', ?, ?)",
            (doc_id, current_user, req.title, now, now),
        )
        conn.commit()
    return {
        "id": doc_id, "title": req.title, "body": "",
        "created_at": now, "updated_at": now,
    }


@router.get("/docs/{doc_id}")
async def get_doc(
    doc_id: str,
    current_user: str = Depends(get_current_user),
) -> dict[str, Any]:
    with get_db_connection() as conn:
        conn.row_factory = sqlite3.Row
        _ensure_schema(conn)
        row = _get_doc_or_404(conn, current_user, doc_id)
        refs = conn.execute(
            "SELECT id, ref_type, target_id, granularity, snapshot, "
            "       source_nodes, created_at "
            "FROM doc_references WHERE user_id = ? AND doc_id = ? "
            "ORDER BY created_at ASC",
            (current_user, doc_id),
        ).fetchall()
        return {
            "id": row["id"], "title": row["title"], "body": row["body"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "references": [
                {
                    "ref_id":      r["id"],
                    "ref_type":    r["ref_type"],
                    "target_id":   r["target_id"],
                    "granularity": r["granularity"],
                    "chip_label":  _chip_label_for_row(conn, current_user, r),
                    "snapshot":    r["snapshot"],
                    "source_nodes": json.loads(r["source_nodes"] or "[]"),
                    "created_at":  r["created_at"],
                }
                for r in refs
            ],
        }


@router.put("/docs/{doc_id}")
async def update_doc(
    doc_id: str,
    req: DocUpdateRequest,
    current_user: str = Depends(get_current_user),
) -> dict[str, Any]:
    with get_db_connection() as conn:
        conn.row_factory = sqlite3.Row
        _ensure_schema(conn)
        row = _get_doc_or_404(conn, current_user, doc_id)
        now = _now()
        new_title = req.title if req.title is not None else row["title"]
        body_changed = req.body is not None and req.body != row["body"]
        new_body = req.body if req.body is not None else row["body"]
        conn.execute(
            "UPDATE docs SET title = ?, body = ?, updated_at = ? "
            "WHERE user_id = ? AND id = ?",
            (new_title, new_body, now, current_user, doc_id),
        )
        snapshot_id: Optional[int] = None
        if body_changed:
            cur = conn.execute(
                "INSERT INTO doc_snapshots "
                "(doc_id, user_id, body, label, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (doc_id, current_user, new_body, req.snapshot_label, now),
            )
            snapshot_id = cur.lastrowid
            # Retention cap — keep the latest _SNAPSHOT_CAP per doc.
            conn.execute(
                "DELETE FROM doc_snapshots "
                "WHERE user_id = ? AND doc_id = ? AND id NOT IN ("
                "  SELECT id FROM doc_snapshots "
                "  WHERE user_id = ? AND doc_id = ? "
                "  ORDER BY id DESC LIMIT ?)",
                (current_user, doc_id, current_user, doc_id, _SNAPSHOT_CAP),
            )
        conn.commit()
        return {
            "ok": True, "updated_at": now,
            "snapshot_created": body_changed,
            "snapshot_id": snapshot_id,
        }


@router.delete("/docs/{doc_id}")
async def delete_doc(
    doc_id: str,
    current_user: str = Depends(get_current_user),
) -> dict[str, Any]:
    with get_db_connection() as conn:
        conn.row_factory = sqlite3.Row
        _ensure_schema(conn)
        _get_doc_or_404(conn, current_user, doc_id)
        conn.execute(
            "DELETE FROM doc_references WHERE user_id = ? AND doc_id = ?",
            (current_user, doc_id),
        )
        conn.execute(
            "DELETE FROM doc_snapshots WHERE user_id = ? AND doc_id = ?",
            (current_user, doc_id),
        )
        conn.execute(
            "DELETE FROM docs WHERE user_id = ? AND id = ?",
            (current_user, doc_id),
        )
        conn.commit()
    return {"ok": True}


# ─────────────────────────────────────────────────────────────────────
# Version snapshots
# ─────────────────────────────────────────────────────────────────────

@router.get("/docs/{doc_id}/snapshots")
async def list_snapshots(
    doc_id: str,
    current_user: str = Depends(get_current_user),
) -> dict[str, Any]:
    with get_db_connection() as conn:
        conn.row_factory = sqlite3.Row
        _ensure_schema(conn)
        _get_doc_or_404(conn, current_user, doc_id)
        rows = conn.execute(
            "SELECT id, label, created_at, LENGTH(body) AS chars "
            "FROM doc_snapshots WHERE user_id = ? AND doc_id = ? "
            "ORDER BY id DESC",
            (current_user, doc_id),
        ).fetchall()
        return {
            "snapshots": [
                {
                    "id": r["id"], "label": r["label"],
                    "created_at": r["created_at"],
                    "chars": int(r["chars"] or 0),
                }
                for r in rows
            ]
        }


@router.post("/docs/{doc_id}/snapshots/{snapshot_id}/restore")
async def restore_snapshot(
    doc_id: str,
    snapshot_id: int,
    current_user: str = Depends(get_current_user),
) -> dict[str, Any]:
    with get_db_connection() as conn:
        conn.row_factory = sqlite3.Row
        _ensure_schema(conn)
        _get_doc_or_404(conn, current_user, doc_id)
        snap = conn.execute(
            "SELECT id, body FROM doc_snapshots "
            "WHERE user_id = ? AND doc_id = ? AND id = ?",
            (current_user, doc_id, snapshot_id),
        ).fetchone()
        if snap is None:
            raise HTTPException(status_code=404, detail="snapshot not found")
        now = _now()
        conn.execute(
            "UPDATE docs SET body = ?, updated_at = ? "
            "WHERE user_id = ? AND id = ?",
            (snap["body"], now, current_user, doc_id),
        )
        # The restore is itself a version event so the medic can undo
        # the undo.
        conn.execute(
            "INSERT INTO doc_snapshots "
            "(doc_id, user_id, body, label, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (doc_id, current_user, snap["body"],
             f"restore:{snapshot_id}", now),
        )
        conn.commit()
        return {"ok": True, "body": snap["body"], "updated_at": now}


# ─────────────────────────────────────────────────────────────────────
# References (@ chips)
# ─────────────────────────────────────────────────────────────────────

@router.post("/docs/{doc_id}/references")
async def create_reference(
    doc_id: str,
    req: ReferenceCreateRequest,
    current_user: str = Depends(get_current_user),
) -> dict[str, Any]:
    """Resolve an @ selection into a FROZEN de-identified snapshot +
    chip metadata, and write the audit event (who/when/doc/target/
    granularity)."""
    with get_db_connection() as conn:
        conn.row_factory = sqlite3.Row
        _ensure_schema(conn)
        _get_doc_or_404(conn, current_user, doc_id)

        snapshot, source_nodes, chip_label = _build_reference_snapshot(
            conn, current_user, req.ref_type, req.target_id,
            req.granularity,
        )

        ref_id = str(uuid.uuid4())
        now = _now()
        conn.execute(
            "INSERT INTO doc_references "
            "(id, doc_id, user_id, ref_type, target_id, granularity, "
            " snapshot, source_nodes, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (ref_id, doc_id, current_user, req.ref_type, req.target_id,
             req.granularity, snapshot,
             json.dumps(source_nodes, ensure_ascii=False), now),
        )
        conn.commit()

        # Audit event — best-effort but loud on failure. The reference
        # row is the projection; the event is the medico-legal record.
        try:
            from nexus_server.event_sourcing import (
                EventKind, Store, init_event_sourcing_schema,
            )
            init_event_sourcing_schema(conn)
            store = Store(conn)
            store.emit_and_apply(
                kind=EventKind.DOC_REFERENCE_CREATED,
                payload={
                    "doc_id":            doc_id,
                    "ref_id":            ref_id,
                    "ref_type":          req.ref_type,
                    "target_id":         req.target_id,
                    "granularity":       req.granularity,
                    "source_node_count": len(source_nodes),
                },
                apply_fn=lambda *_a, **_k: None,
                user_id=current_user,
                patient_hash=(
                    req.target_id if req.ref_type == "patient" else None
                ),
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("doc_reference audit emit failed: %s", exc)

        return {
            "ref_id": ref_id,
            "chip_label": chip_label,
            "snapshot_preview": snapshot[:400],
        }


# ─────────────────────────────────────────────────────────────────────
# Polish (SSE)
# ─────────────────────────────────────────────────────────────────────

_POLISH_PERSONA = (
    "你是一名严谨的临床写作编辑，服务于医生用户的病例报告、研究摘要与"
    "伦理材料写作。保持医学术语准确、语气专业；只输出改写后的文本本身，"
    "不要输出解释、前言或 Markdown 代码块。"
)
_POLISH_GROUNDING_RULE = "仅基于提供的引用数据改写，不得编造数值。"


def _extract_numbers(text: str) -> set[str]:
    """Normalised numeric tokens (percent sign stripped) for the
    provenance check."""
    return {m.group(0).rstrip("%") for m in _NUM_RE.finditer(text or "")}


@router.post("/docs/{doc_id}/polish")
async def polish_selection(
    doc_id: str,
    req: PolishRequest,
    current_user: str = Depends(get_current_user),
):
    """Rewrite the selected text per instruction, grounded in the doc's
    reference snapshots. Streams SSE frames:

        {type:'revised_chunk', text}          — revised text, chunked
        {type:'provenance_warning', numbers}  — numbers in the revision
                                                with no source in the
                                                selection or refs
        {type:'done', revised}                — full revised text
        {type:'error', message}               — on failure
    """
    # Resolve context BEFORE starting the stream so a bad doc_id is a
    # clean 404, not an SSE error frame.
    with get_db_connection() as conn:
        conn.row_factory = sqlite3.Row
        _ensure_schema(conn)
        _get_doc_or_404(conn, current_user, doc_id)
        snapshots: list[str] = []
        if req.ref_ids:
            qmarks = ",".join("?" * len(req.ref_ids))
            rows = conn.execute(
                f"SELECT snapshot FROM doc_references "
                f"WHERE user_id = ? AND doc_id = ? AND id IN ({qmarks})",
                (current_user, doc_id, *req.ref_ids),
            ).fetchall()
            snapshots = [str(r["snapshot"] or "") for r in rows]

    system_prompt = _POLISH_PERSONA + "\n" + _POLISH_GROUNDING_RULE
    if req.instruction.strip():
        system_prompt += f"\n改写要求：{req.instruction.strip()}"
    if snapshots:
        system_prompt += (
            "\n\n以下是本文档的引用数据（已脱敏），改写时只能使用这些"
            "数据中的数值：\n"
            + "\n---\n".join(snapshots)
        )

    async def event_stream() -> AsyncIterator[str]:
        try:
            # Late import + module-attribute call so tests can
            # monkeypatch llm_gateway.call_llm (same pattern the chat
            # tests rely on).
            from nexus_server import llm_gateway
            content, model_used, _stop, _tools = await llm_gateway.call_llm(
                [{"role": "user", "content": req.selection}],
                system_prompt,
                None,      # model → DEFAULT_LLM_MODEL via provider dispatch
                0.3,
                4096,
            )
            revised = (content or "").strip()
            if not revised:
                yield _sse({
                    "type": "error",
                    "message": "LLM returned empty revision",
                })
                return

            # Stream the revision in chunks so the frontend can render
            # the diff progressively.
            chunk_size = 120
            for i in range(0, len(revised), chunk_size):
                yield _sse({
                    "type": "revised_chunk",
                    "text": revised[i:i + chunk_size],
                })

            # Hallucinated-value guard: numbers in the revision that
            # appear in neither the selection nor any ref snapshot.
            allowed = _extract_numbers(req.selection)
            for s in snapshots:
                allowed |= _extract_numbers(s)
            suspicious: list[str] = []
            for m in _NUM_RE.finditer(revised):
                token = m.group(0)
                if token.rstrip("%") not in allowed and token not in suspicious:
                    suspicious.append(token)
            if suspicious:
                yield _sse({
                    "type": "provenance_warning",
                    "numbers": suspicious,
                })

            yield _sse({
                "type": "done",
                "revised": revised,
                "model": model_used,
            })
        except Exception as exc:  # noqa: BLE001
            logger.warning("polish stream failed: %s", exc)
            yield _sse({"type": "error", "message": str(exc)[:500]})

    return StreamingResponse(event_stream(), media_type="text/event-stream")


# ─────────────────────────────────────────────────────────────────────
# PHI scan
# ─────────────────────────────────────────────────────────────────────

@router.post("/docs/{doc_id}/phi-scan")
async def phi_scan(
    doc_id: str,
    req: Optional[PhiScanRequest] = None,
    current_user: str = Depends(get_current_user),
) -> dict[str, Any]:
    with get_db_connection() as conn:
        conn.row_factory = sqlite3.Row
        _ensure_schema(conn)
        row = _get_doc_or_404(conn, current_user, doc_id)
        override = req.body if req is not None else None
        body = override if override is not None else str(row["body"] or "")
        return {"findings": _phi_scan_body(conn, current_user, body)}


# ─────────────────────────────────────────────────────────────────────
# Export
# ─────────────────────────────────────────────────────────────────────

def _resolution_matches(finding: dict, resolution: dict) -> bool:
    if resolution.get("kind") and resolution["kind"] != finding["kind"]:
        return False
    if (
        resolution.get("start") is not None
        and resolution.get("end") is not None
    ):
        return (
            int(resolution["start"]) == finding["start"]
            and int(resolution["end"]) == finding["end"]
        )
    excerpt = resolution.get("excerpt")
    return bool(excerpt) and excerpt == finding["excerpt"]


@router.post("/docs/{doc_id}/export")
async def export_doc(
    doc_id: str,
    req: ExportRequest,
    current_user: str = Depends(get_current_user),
):
    """PHI gate → chip expansion → .docx.

    422 {code:'phi_unresolved', findings:[...]} when the body still has
    unresolved PHI findings. Resolutions with a ``replacement`` string
    rewrite the flagged span before export; without one they count as
    an explicit keep.
    """
    with get_db_connection() as conn:
        conn.row_factory = sqlite3.Row
        _ensure_schema(conn)
        row = _get_doc_or_404(conn, current_user, doc_id)
        title = str(row["title"] or "")
        body = str(row["body"] or "")

        findings = _phi_scan_body(conn, current_user, body)
        unresolved: list[dict] = []
        resolved: list[tuple[dict, dict]] = []
        for f in findings:
            match = next(
                (r for r in req.resolutions if _resolution_matches(f, r)),
                None,
            )
            if match is None:
                unresolved.append(f)
            else:
                resolved.append((f, match))
        if unresolved:
            return JSONResponse(
                status_code=422,
                content={"code": "phi_unresolved", "findings": unresolved},
            )

        # Apply replacements back-to-front so earlier spans stay valid.
        for f, r in sorted(resolved, key=lambda p: p[0]["start"],
                           reverse=True):
            replacement = r.get("replacement")
            if isinstance(replacement, str):
                body = body[:f["start"]] + replacement + body[f["end"]:]

        # Expand {{ref:ID}} chips to their frozen de-identified text.
        refs = conn.execute(
            "SELECT id, ref_type, target_id, granularity, snapshot, "
            "       source_nodes, created_at "
            "FROM doc_references WHERE user_id = ? AND doc_id = ? "
            "ORDER BY created_at ASC",
            (current_user, doc_id),
        ).fetchall()
        ref_by_id = {str(r["id"]): r for r in refs}

        def _expand(m: re.Match) -> str:
            r = ref_by_id.get(m.group(1))
            return str(r["snapshot"]) if r is not None else ""

        expanded = _REF_PLACEHOLDER_RE.sub(_expand, body)

        # Build the .docx.
        from docx import Document
        document = Document()
        document.add_heading(title or "未命名文档", level=1)
        for para in re.split(r"\n\s*\n", expanded):
            para = para.strip()
            if para:
                document.add_paragraph(para)

        if req.include_sources and refs:
            document.add_heading("引用来源", level=2)
            for r in refs:
                label = _chip_label_for_row(conn, current_user, r)
                try:
                    nodes = json.loads(r["source_nodes"] or "[]")
                except (TypeError, ValueError):
                    nodes = []
                node_str = (
                    ", ".join(str(n) for n in nodes) if nodes else "—"
                )
                document.add_paragraph(
                    f"{label}（{r['ref_type']}/{r['granularity']}）"
                    f" · 来源节点: {node_str}",
                    style="List Bullet",
                )

        buf = BytesIO()
        document.save(buf)

    filename = f"doc-{doc_id[:8]}.docx"
    return Response(
        content=buf.getvalue(),
        media_type=(
            "application/vnd.openxmlformats-officedocument"
            ".wordprocessingml.document"
        ),
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
        },
    )
