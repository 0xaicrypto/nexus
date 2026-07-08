"""F-unified-chat-files — REST surface for the per-chat file library.

One library per (user_id, scope_kind, scope_ref) tuple:

  patient        → kind='patient',       ref=<patient_hash>
  per-study      → kind='research',      ref=<study_id>
  cross-research → kind='cross_research', ref='__workspace__'
  assistant      → kind='assistant',     ref='__workspace__'

The four chat surfaces all consume the same endpoints; the only
difference is which `(kind, ref)` they pass. See
``docs/design/UNIFIED_CHAT_FILES.md`` for the full design.
"""
from __future__ import annotations

import logging
import sqlite3
import time
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from nexus_server.auth import get_current_user
from nexus_server.database import get_db_connection

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/chat/files", tags=["chat-files"])


# ─────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────

VALID_SCOPE_KINDS = {"patient", "research", "cross_research", "assistant"}

# Sentinel scope_ref for the two "workspace-wide" libraries
# (cross-research and assistant). Lets us key those with a constant
# instead of a synthetic UUID.
WORKSPACE_SENTINEL = "__workspace__"

# Soft cap on number of active files per library. Past this, /upload
# (or the upload's after-write check) should refuse. UI surfaces the
# count so medic can prune.
MAX_ACTIVE_FILES_PER_LIB = 50

# Grace period for soft-deleted files before the GC cron physically
# removes the disk file + SQL row. 7 days matches the medic's typical
# weekly review cadence and gives them a long-enough "undo" window.
SOFT_DELETE_RETENTION_DAYS = 7


# ─────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────


def _now_ms() -> int:
    return int(time.time() * 1000)


def _validate_scope(kind: str, ref: str) -> tuple[str, str]:
    """Trim + validate. Returns (kind, ref) or raises 400."""
    k = (kind or "").strip()
    r = (ref or "").strip()
    if k not in VALID_SCOPE_KINDS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"invalid scope_kind {k!r}; "
                   f"must be one of {sorted(VALID_SCOPE_KINDS)}",
        )
    if not r:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="scope_ref is required",
        )
    return k, r


# ─────────────────────────────────────────────────────────────────────
# Response models
# ─────────────────────────────────────────────────────────────────────


class ChatFileInfo(BaseModel):
    """One row in the file-library UI."""
    file_id: str
    name: str
    mime: str
    size_bytes: int
    created_at: str
    # Stable "[F1] [F2] ..." token assigned to this file within its
    # library. Same file in two different libraries gets two different
    # tokens; same file repeated within one library is impossible
    # (file_id is unique). Order is by created_at ASC so the token is
    # stable across requests as long as no file is hard-deleted from
    # the same library.
    f_id_token: str
    # Extraction status: 'pending' | 'text_layer' | 'vision_ocr'
    #                  | 'unreadable' | 'encrypted' | 'error: <msg>'
    text_extraction_status: str
    has_text: bool
    # When the file was soft-deleted (None for active files). Useful
    # for the "已移除" tab to show "N days until permanent delete".
    deleted_at: Optional[int] = None


class ChatFilesResponse(BaseModel):
    files: list[ChatFileInfo]
    total_active: int
    total_removed: int   # 7d-recoverable soft-deleted count
    scope_kind: str
    scope_ref: str


# ─────────────────────────────────────────────────────────────────────
# Server-side renderer (called by retrieval_tiers to build the prompt)
# ─────────────────────────────────────────────────────────────────────


def _gather_file_lib(
    conn: sqlite3.Connection,
    user_id: str,
    scope_kind: str,
    scope_ref: str,
    *,
    excerpt_char_cap: int = 4000,
    total_char_cap: int = 30_000,
) -> str:
    """Render the file library as a system-prompt block.

    Called from ``retrieval_tiers.retrieve_async`` for whichever chat
    surface is active; the returned string is appended to the system
    prompt so the LLM:
      1. Knows what files the medic has uploaded.
      2. Knows the stable ``[F1] [F2]`` tokens for citing them.
      3. Has the verbatim excerpts available (capped, with truncation
         note when applicable).

    Returns "" when the library is empty so the prompt builder can
    simply concatenate without conditional spacing.
    """
    if scope_kind not in VALID_SCOPE_KINDS or not scope_ref:
        return ""
    try:
        rows = conn.execute(
            """
            SELECT file_id, name, mime, extracted_text,
                   text_extraction_status
              FROM uploads
             WHERE user_id = ?
               AND lib_scope_kind = ?
               AND lib_scope_ref  = ?
               AND deleted_at IS NULL
             ORDER BY created_at ASC
            """,
            (user_id, scope_kind, scope_ref),
        ).fetchall()
    except sqlite3.Error as exc:
        logger.warning(
            "_gather_file_lib SQL failed (user=%s scope=%s/%s): %s",
            user_id, scope_kind, scope_ref, exc,
        )
        return ""
    if not rows:
        return ""

    label = {
        "patient":        "this patient's",
        "research":       "this research study's",
        "cross_research": "your cross-research workspace's",
        "assistant":      "your assistant workspace's",
    }.get(scope_kind, "this chat's")

    parts: list[str] = [
        f"\n\nREFERENCE FILES ({label} library; "
        f"cite [F1], [F2], ... in your answers):"
    ]
    total = 0
    for i, (file_id, name, mime, text, ext_status) in enumerate(rows, start=1):
        status_badge = {
            "text_layer": "",
            "vision_ocr": " (extracted by AI vision)",
            "unreadable": " (content unreadable -- scanned with no text layer)",
            "encrypted":  " (encrypted, content unreadable)",
            "pending":    " (extraction pending)",
        }.get(ext_status, f" ({ext_status})")
        parts.append(f"\n  [F{i}] {name}  ({mime}{status_badge})")
        if not text:
            # Binary file or extraction failed entirely. Tell the LLM
            # so it doesn't pretend to have read content.
            parts.append(
                "        (no extracted text available -- the medic "
                "uploaded this file but extraction did not succeed; "
                "ask them to verify or re-upload if needed)"
            )
            continue
        # Truncate per file + bail if total > cap.
        room = total_char_cap - total
        if room <= 0:
            parts.append(
                "        (further files omitted -- prompt budget reached; "
                "the medic can still reference them by name)"
            )
            break
        excerpt = text[: min(excerpt_char_cap, room)]
        truncated_per_file = len(text) > len(excerpt)
        parts.append("        --- excerpt ---")
        parts.append(f"        {excerpt}")
        if truncated_per_file:
            parts.append(
                f"        (truncated; full file has "
                f"{len(text)} chars total)"
            )
        parts.append("        --- end excerpt ---")
        total += len(excerpt)

    parts.append("\nCITATION RULES FOR FILES:")
    parts.append("  - When grounding on file content, cite [Fn] inline.")
    parts.append("  - Never invent an [Fn]; only IDs listed above are valid.")
    parts.append("  - You may combine with other sources:")
    parts.append("    \"RECIST PR = >=30% decrease [F1, NCCN 2024]\".")
    parts.append("  - For files marked 'extracted by AI vision', add a")
    parts.append("    brief '(per OCR)' note if the answer is")
    parts.append("    medical-decision-critical.")
    return "\n".join(parts)


# ─────────────────────────────────────────────────────────────────────
# Endpoints
# ─────────────────────────────────────────────────────────────────────


@router.get("", response_model=ChatFilesResponse)
async def list_files(
    scope_kind: str = Query(...),
    scope_ref: str = Query(...),
    include_removed: bool = Query(False),
    user_id: str = Depends(get_current_user),
) -> ChatFilesResponse:
    """List files in one chat surface's library.

    By default returns active files only. ``include_removed=true``
    additionally returns soft-deleted rows whose ``deleted_at`` is
    within the 7-day grace window (for the "已移除" tab).
    """
    kind, ref = _validate_scope(scope_kind, scope_ref)
    cutoff_removed = _now_ms() - SOFT_DELETE_RETENTION_DAYS * 24 * 3600 * 1000
    with get_db_connection() as conn:
        # Active.
        active = conn.execute(
            """
            SELECT file_id, name, mime, size_bytes, created_at,
                   text_extraction_status, extracted_text
              FROM uploads
             WHERE user_id = ?
               AND lib_scope_kind = ?
               AND lib_scope_ref  = ?
               AND deleted_at IS NULL
             ORDER BY created_at ASC
            """,
            (user_id, kind, ref),
        ).fetchall()
        # Soft-deleted (within 7d).
        removed = conn.execute(
            """
            SELECT file_id, name, mime, size_bytes, created_at,
                   text_extraction_status, extracted_text, deleted_at
              FROM uploads
             WHERE user_id = ?
               AND lib_scope_kind = ?
               AND lib_scope_ref  = ?
               AND deleted_at IS NOT NULL
               AND deleted_at > ?
             ORDER BY deleted_at DESC
            """,
            (user_id, kind, ref, cutoff_removed),
        ).fetchall() if include_removed else []

    files: list[ChatFileInfo] = []
    for i, r in enumerate(active, start=1):
        (fid, name, mime, size, created_at, status_, etext) = r
        files.append(ChatFileInfo(
            file_id=fid, name=name, mime=mime,
            size_bytes=int(size or 0),
            created_at=str(created_at),
            f_id_token=f"F{i}",
            text_extraction_status=status_ or "pending",
            has_text=bool(etext),
        ))
    # Removed rows get f_id_token='-' since they're not active.
    for r in removed:
        (fid, name, mime, size, created_at, status_, etext, deleted_at) = r
        files.append(ChatFileInfo(
            file_id=fid, name=name, mime=mime,
            size_bytes=int(size or 0),
            created_at=str(created_at),
            f_id_token="-",
            text_extraction_status=status_ or "pending",
            has_text=bool(etext),
            deleted_at=int(deleted_at) if deleted_at is not None else None,
        ))

    return ChatFilesResponse(
        files=files,
        total_active=len(active),
        total_removed=len(removed),
        scope_kind=kind,
        scope_ref=ref,
    )


class DeleteResponse(BaseModel):
    file_id: str
    deleted_at: int


@router.delete("/{file_id}", response_model=DeleteResponse)
async def soft_delete_file(
    file_id: str,
    user_id: str = Depends(get_current_user),
) -> DeleteResponse:
    """Soft-delete: sets deleted_at but keeps the disk file. A 7d GC
    cron physically removes after the grace window."""
    now = _now_ms()
    with get_db_connection() as conn:
        cur = conn.execute(
            "UPDATE uploads SET deleted_at = ? "
            "WHERE user_id = ? AND file_id = ? AND deleted_at IS NULL",
            (now, user_id, file_id),
        )
        conn.commit()
        if cur.rowcount == 0:
            raise HTTPException(
                status_code=404,
                detail="file not found or already deleted",
            )
    return DeleteResponse(file_id=file_id, deleted_at=now)


class RestoreResponse(BaseModel):
    file_id: str


@router.post("/{file_id}/restore", response_model=RestoreResponse)
async def restore_file(
    file_id: str,
    user_id: str = Depends(get_current_user),
) -> RestoreResponse:
    """Undo a soft delete -- only valid within the 7d grace window."""
    cutoff = _now_ms() - SOFT_DELETE_RETENTION_DAYS * 24 * 3600 * 1000
    with get_db_connection() as conn:
        cur = conn.execute(
            "UPDATE uploads SET deleted_at = NULL "
            "WHERE user_id = ? AND file_id = ? "
            "  AND deleted_at IS NOT NULL "
            "  AND deleted_at > ?",
            (user_id, file_id, cutoff),
        )
        conn.commit()
        if cur.rowcount == 0:
            raise HTTPException(
                status_code=404,
                detail="file not found, not deleted, or past 7-day "
                       "restore window",
            )
    return RestoreResponse(file_id=file_id)


class ReextractResponse(BaseModel):
    file_id: str
    text_extraction_status: str
    text_length: int


@router.post("/{file_id}/reextract", response_model=ReextractResponse)
async def reextract_file(
    file_id: str,
    user_id: str = Depends(get_current_user),
) -> ReextractResponse:
    """Manually re-run text extraction on a file. Useful when:
      - First extraction failed (status='unreadable') but a fresh
        Gemini key landed, enabling Vision fallback.
      - File was uploaded before the OCR path existed.

    Falls through to the same pipeline as the upload-time extractor.
    """
    with get_db_connection() as conn:
        row = conn.execute(
            "SELECT name, mime, disk_path FROM uploads "
            "WHERE user_id = ? AND file_id = ? AND deleted_at IS NULL",
            (user_id, file_id),
        ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="file not found")
    name, mime, disk_path = row

    # Defer to the extraction pipeline (Phase 2 — pdf_ocr.py).
    from nexus_server.pdf_extract import extract_and_persist
    text, new_status = await extract_and_persist(
        user_id=user_id, file_id=file_id,
        name=name, mime=mime, disk_path=disk_path,
    )
    return ReextractResponse(
        file_id=file_id,
        text_extraction_status=new_status,
        text_length=len(text or ""),
    )
