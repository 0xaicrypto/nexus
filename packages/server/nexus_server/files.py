"""File upload endpoint + three-layer file resolver.

    POST /api/v1/files/upload   (multipart/form-data, field "file")
        → { "file_id": "...", "name": "...", "size": N, "mime": "..." }

The desktop streams attachments here; chat then references them by
``file_id`` rather than re-encoding base64 in every request.

Storage model — no in-memory state:

  Layer 1 (metadata, durable):
      A ``file_uploaded`` event lands in twin.event_log on upload
      and participates in the next BSC state-root anchor, making
      the upload's metadata (name, sha256, size) verifiable.

  Layer 2 (server storage):
      ``uploads`` SQLite row (file_id, sha256, extracted_text)
      + bytes on local disk. An S3-compatible object mirror will
      complement this in a future task.

  Layer 3 (tool surface, stateless):
      ``ReadUploadedFileTool`` calls ``resolve_file_text`` below.
      No in-memory state on the tool side — the previous
      ``store()`` / ``store_path()`` API was removed once every
      production caller switched to this resolver path. See
      ``packages/sdk/nexus_core/tools/file_reader.py`` for the
      tool contract; ``test_file_reader_resolver.py`` guards that
      no in-memory fallback creeps back in.
"""

from __future__ import annotations

import json
import logging
import mimetypes
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    File,
    Form,
    HTTPException,
    UploadFile,
    status,
)
from pydantic import BaseModel

from nexus_server.auth import get_current_user
from nexus_server.config import get_config
from nexus_server.database import get_db_connection

logger = logging.getLogger(__name__)
config = get_config()

router = APIRouter(prefix="/api/v1/files", tags=["files"])


# Hard cap on file upload size. Used to default to 100 MB but DICOM
# CT studies are routinely 500 MB - 1.5 GB (256-512 slices × 500 KB
# each + thin-slice variants). Bumped to 2 GB and made env-overridable
# so high-throughput sites can push further without a code change.
#
# Important: the upload route below streams to disk in 1 MB chunks
# rather than buffering the whole file in memory, so this ceiling
# only constrains disk space + sha256 cost — not RAM. A 1 GB upload
# uses ~1 MB peak RSS instead of 1 GB.
# Hard cap mirrors llm_gateway's MAX_ATTACHMENT_BYTES_TOTAL — a single
# file shouldn't be bigger than the chat-time per-call cap.
MAX_FILE_BYTES = int(
    __import__("os").environ.get(
        "NEXUS_MAX_FILE_BYTES",
        str(2 * 1024 * 1024 * 1024),   # 2 GB default for DICOM CT zips
    ),
)


def format_size_hint(size_bytes: int) -> str:
    """Human-readable byte count — ``312 B`` / ``45 KB`` / ``2 MB``.

    Shared by every place that surfaces a file size to either the user
    (Files page, chat chips) or the LLM (uploads memory injection block,
    curated MEMORY.md entries). Previously copy-pasted in 3+ spots;
    refactor #5 hoisted them here.
    """
    if size_bytes < 1024:
        return f"{size_bytes} B"
    if size_bytes < 1024 * 1024:
        return f"{size_bytes // 1024} KB"
    return f"{size_bytes // (1024 * 1024)} MB"


def _files_dir() -> Path:
    """Where uploads live before the twin consumes them."""
    base = Path(getattr(config, "UPLOAD_DIR",
                        Path.home() / ".nexus_server" / "uploads"))
    base.mkdir(parents=True, exist_ok=True)
    return base


def _ensure_uploads_table() -> None:
    """Lazy table create — keeps database.py focused on core schema.

    Schema evolution (live file storage):
      v1: file_id, user_id, name, mime, size_bytes, disk_path, created_at
      v2: + sha256, gnfd_path, extracted_text
          - sha256: content hash of the uploaded bytes
          - gnfd_path: LEGACY column from the removed decentralised
            object-storage mirror. Kept in the schema so existing
            databases don't need a migration; always ``""`` for new
            rows and never read.
          - extracted_text: cached plain-text projection so
            read_uploaded_file doesn't have to re-decode PDFs / DOCX
            on every cross-turn read. Lazy: filled on first read,
            persists for the file's lifetime.
    """
    with get_db_connection() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS uploads (
                file_id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                name TEXT NOT NULL,
                mime TEXT NOT NULL,
                size_bytes INTEGER NOT NULL,
                disk_path TEXT NOT NULL,
                created_at TIMESTAMP NOT NULL,
                sha256 TEXT NOT NULL DEFAULT '',
                gnfd_path TEXT NOT NULL DEFAULT '',
                extracted_text TEXT NOT NULL DEFAULT ''
            )
            """
        )
        # Migration for existing v1 rows: try ALTER TABLE; ignore
        # "duplicate column" failures so re-running on a v2 db is a
        # no-op. We don't drop the legacy DEFAULTs above (they keep
        # CREATE TABLE on a fresh db one statement).
        for col_def in (
            "sha256 TEXT NOT NULL DEFAULT ''",
            "gnfd_path TEXT NOT NULL DEFAULT ''",
            "extracted_text TEXT NOT NULL DEFAULT ''",
            # #152 — upload-time DICOM prerender status. Empty for
            # non-medical uploads; one of dicom.DICOM_STATUS_* for
            # zip archives that were probed at upload time.
            "dicom_status TEXT NOT NULL DEFAULT ''",
            "dicom_study_id TEXT NOT NULL DEFAULT ''",
            "dicom_preview_dir TEXT NOT NULL DEFAULT ''",
            # #160 — Gemini-incompatible-format normalizer. For TIFF
            # / RAW / BMP uploads we save a downsized JPEG copy at
            # upload time; chat-time resolver swaps the attachment
            # mime + bytes so the vision model sees something it can
            # actually decode. Empty for formats Gemini already
            # handles cleanly.
            "image_normalized_status TEXT NOT NULL DEFAULT ''",
            "image_normalized_path TEXT NOT NULL DEFAULT ''",
            # #178 — per-patient binding for *all* uploads (DICOM,
            # PDFs, TIFFs, lab notes). For DICOM uploads we fill this
            # from the parsed PatientName/ID hash; for everything else
            # we inherit the active session's patient_hash (set on
            # sessions table by #176) so PDFs and pathology TIFFs
            # land in the same patient bucket as the DICOM study they
            # belong to. Empty when the medic uploads outside any
            # patient context.
            "patient_hash TEXT NOT NULL DEFAULT ''",
            # F-unified-chat-files — chat-surface file library scope.
            # Canonically added by Alembic migration
            # versions/0005_unified_chat_files.py (which also builds
            # the covering index + backfills patient scope). Mirrored
            # here so environments that create the uploads table via
            # this lazy path (unit tests, fresh dev DBs that haven't
            # run alembic yet) can still INSERT the columns.
            "lib_scope_kind TEXT NOT NULL DEFAULT ''",
            "lib_scope_ref TEXT NOT NULL DEFAULT ''",
            # NOTE: memory_status / memory_summary / quick_scan_status /
            # quick_scan_summary previously lived here as inline ALTER
            # TABLE statements. They were moved to a proper Alembic
            # migration (versions/0002_add_ingester_status.py) per
            # ENGINEERING_STANDARDS.md rule 2. Don't add new columns
            # to this list — write a new versions/NNNN_*.py instead.
        ):
            try:
                conn.execute(f"ALTER TABLE uploads ADD COLUMN {col_def}")
            except Exception as e:
                logger.debug("adding uploads column failed: %s", e)  # column already exists
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_uploads_user "
            "ON uploads(user_id, created_at DESC)"
        )
        # Lookup-by-name path used by read_uploaded_file.
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_uploads_user_name "
            "ON uploads(user_id, name)"
        )
        conn.commit()


class FileEntry(BaseModel):
    """One row in the user-facing Files list."""
    file_id: str
    name: str
    mime: str
    size_bytes: int
    created_at: str
    sha256: str = ""
    has_text: bool = False
    excerpt: str = ""              # first ~300 chars of extracted_text


class FileListResponse(BaseModel):
    files: list[FileEntry]
    total: int


class FilePreviewResponse(BaseModel):
    """Full preview shape — bigger excerpt than the list endpoint."""
    file_id: str
    name: str
    mime: str
    size_bytes: int
    created_at: str
    sha256: str = ""
    extracted_text: str = ""       # full cached extraction, capped
    has_text: bool = False
    text_truncated: bool = False


# Preview returns up to this many chars of extracted text. Anything
# longer gets truncated + the response sets ``text_truncated=True`` so
# the client can render a "Show more" hint or open a different path.
_PREVIEW_TEXT_CAP = 100 * 1024


@router.get("/list", response_model=FileListResponse)
async def list_files(
    current_user: str = Depends(get_current_user),
    limit: int = 200,
) -> FileListResponse:
    """List the calling user's uploaded files, newest first.

    Powers the desktop "Files" page (D-2 follow-up — UX surface for
    Memory Fix A). The response is intentionally shallow: name,
    mime, size, ~300-char excerpt. For full text use ``/preview``.
    """
    _ensure_uploads_table()
    limit = max(1, min(int(limit), 1000))
    with get_db_connection() as conn:
        rows = conn.execute(
            """
            SELECT file_id, name, mime, size_bytes, created_at,
                   sha256, extracted_text
            FROM uploads WHERE user_id = ?
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (current_user, limit),
        ).fetchall()
    files: list[FileEntry] = []
    for r in rows:
        fid, name, mime, size_bytes, created_at, sha256, text = r
        text = text or ""
        excerpt = text[:300]
        if len(text) > 300:
            excerpt += "…"
        files.append(FileEntry(
            file_id=fid,
            name=name,
            mime=mime or "",
            size_bytes=int(size_bytes or 0),
            created_at=str(created_at) if created_at else "",
            sha256=sha256 or "",
            has_text=bool(text),
            excerpt=excerpt,
        ))
    return FileListResponse(files=files, total=len(files))


@router.get("/{file_id}/preview", response_model=FilePreviewResponse)
async def preview_file(
    file_id: str,
    current_user: str = Depends(get_current_user),
) -> FilePreviewResponse:
    """Fetch metadata + extracted text for one file. Used by the
    Files-page preview pane when the user clicks a row."""
    _ensure_uploads_table()
    with get_db_connection() as conn:
        row = conn.execute(
            """
            SELECT file_id, name, mime, size_bytes, created_at,
                   sha256, extracted_text, disk_path
            FROM uploads WHERE user_id = ? AND file_id = ?
            """,
            (current_user, file_id),
        ).fetchone()
    if not row:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"File {file_id} not found for this user",
        )
    fid, name, mime, size_bytes, created_at, sha256, text, disk_path = row
    text = text or ""
    # Lazy extract: if extracted_text was never populated AND disk
    # copy exists, run the SDK distiller now + cache result. Means
    # the user opens the file in the UI for the first time, sees
    # actual text, not a stub.
    if not text and disk_path:
        try:
            extracted = await _extract_from_disk(disk_path, name, mime or "")
            if extracted:
                _save_extracted_text(fid, extracted)
                text = extracted
        except Exception as e:  # noqa: BLE001
            logger.debug("lazy extract for %s failed: %s", name, e)
    truncated = len(text) > _PREVIEW_TEXT_CAP
    if truncated:
        text = text[:_PREVIEW_TEXT_CAP]
    return FilePreviewResponse(
        file_id=fid,
        name=name,
        mime=mime or "",
        size_bytes=int(size_bytes or 0),
        created_at=str(created_at) if created_at else "",
        sha256=sha256 or "",
        extracted_text=text,
        has_text=bool(text),
        text_truncated=truncated,
    )


@router.delete("/{file_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_file(
    file_id: str,
    current_user: str = Depends(get_current_user),
):
    """Remove a file: SQL row + on-disk copy. Curated memory entries
    that mention the file are left in place — they're historical
    record, not actionable references."""
    _ensure_uploads_table()
    with get_db_connection() as conn:
        row = conn.execute(
            "SELECT disk_path FROM uploads "
            "WHERE user_id = ? AND file_id = ?",
            (current_user, file_id),
        ).fetchone()
        if not row:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"File {file_id} not found",
            )
        disk_path = row[0]
        conn.execute(
            "DELETE FROM uploads WHERE user_id = ? AND file_id = ?",
            (current_user, file_id),
        )
        conn.commit()

    # Best-effort disk cleanup.
    try:
        p = Path(disk_path)
        if p.exists():
            p.unlink()
    except Exception as e:  # noqa: BLE001
        logger.debug("disk delete for %s failed: %s", disk_path, e)
    logger.info("Deleted file %s for user %s", file_id, current_user)
    return None


class UploadResponse(BaseModel):
    file_id: str
    name: str
    mime: str
    size_bytes: int
    # #152 — DICOM prerender status carried back to the client so the
    # attachment chip can show whether the medical archive was
    # successfully ingested at upload time. ``""`` for non-DICOM
    # uploads. See dicom.DICOM_STATUS_* for the value space.
    # #158 — for DICOM zip uploads this starts as "prerendering";
    # the client polls /api/v1/files/{file_id}/prerender-progress
    # and waits for state="done" before treating the study as
    # viewer-ready.
    dicom_status: str = ""
    # Persisted study_id (uuid) when prerender succeeded. The desktop
    # uses this to open the dedicated viewer without a separate
    # /studies lookup. Empty when not applicable. With #158 async
    # prerender this only becomes non-empty AFTER the background task
    # has parsed the archive — clients should rely on the progress
    # endpoint's study_id field for the canonical value.
    dicom_study_id: str = ""
    # #158 — when True the client should poll the progress endpoint.
    # Set to True for any zip-like upload (we won't know until probe
    # whether it's actually DICOM, so we err on the side of polling
    # and the progress endpoint returns state="done" quickly when it
    # isn't).
    dicom_prerender_active: bool = False


class PrerenderProgressResponse(BaseModel):
    """#158 — polled by the desktop chip to drive the progress bar
    during long DICOM prerenders. Returned by GET
    /api/v1/files/{file_id}/prerender-progress.
    """
    state:        str   # queued|parsing|rendering|done|error|unknown
    stage:        str   # human label: detecting / parse_archive / cache_slices / ...
    current:      int   # current item count within stage
    total:        int   # total items in stage (0 when not applicable)
    percent:      float # 0..100 convenience
    study_id:     str   # populated once the study has been persisted
    preview_dir:  str   # absolute path under the user uploads dir
    error:        str   # populated only when state == "error"
    # U3.3 — Layer 1 ingestion result. Set after the prerender-derived
    # study runs through dicom_ingester:
    #   ''       — not run yet (or non-DICOM upload)
    #   'pending'— prerender finished but ingester hasn't completed
    #   'ok'     — graph nodes emitted; ``memory_summary`` is "N graph events"
    #   'error'  — ingester crashed; ``memory_summary`` is "ExcType: message"
    # The desktop's Imaging upload card reads this so an ingester
    # failure is visible to the medic instead of silently producing
    # an empty Memory tab.
    memory_status:  str = ""
    memory_summary: str = ""
    # Tier A — Quick scan (Gemini Flash triage) result. Same status
    # vocabulary as memory_status. quick_scan_summary is a short
    # human string the Imaging upload row renders inline.
    quick_scan_status:  str = ""
    quick_scan_summary: str = ""
    # Live progress dict for an IN-FLIGHT Quick scan. Populated by
    # ``quick_scan._set_quick_scan_progress`` after each grid render
    # and after each Gemini Flash return. Schema (see
    # ``nexus_server/quick_scan.py:_set_quick_scan_progress``):
    #   { stage, current_preset, presets, total_grids, rendered_grids,
    #     triaged_grids, errors, recent: [...], elapsed_s, ... }
    # ``None`` when no scan has run for this file's study (e.g. before
    # the prerender finishes, or 1h+ after a completed scan when the
    # progress dict has TTL-pruned). The desktop's UploadJobRow shows
    # the live block under "Quick scan: running…" while
    # quick_scan_status == 'pending'.
    quick_scan_progress: Optional[dict] = None


@router.post("/upload", response_model=UploadResponse)
async def upload_file(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    # #178 — optional session_id Form param so non-DICOM uploads
    # inherit the active session's patient_hash. The desktop client
    # passes this when the user has a patient selected; without it
    # the upload still works but won't auto-file into a patient
    # bucket (the DICOM path can still self-bind from tags).
    session_id: str = Form(""),
    # Explicit patient_hash override — when the medic has a patient
    # open in the desktop and drops a CT, they expect the upload to
    # ATTACH to that patient, not mint a new patient row from the
    # DICOM PatientID tag. The desktop passes this; backend uses it
    # to short-circuit both the session→patient_hash lookup and the
    # DICOM-tag-derived hash. Empty string means "no override".
    patient_hash: str = Form(""),
    # F-unified-chat-files — which chat surface's file library this
    # upload should join. All four chats pass these:
    #   patient        → lib_scope_kind='patient',  lib_scope_ref=<patient_hash>
    #   per-study chat → lib_scope_kind='research', lib_scope_ref=<study_id>
    #   cross-research → lib_scope_kind='cross_research', lib_scope_ref='__workspace__'
    #   assistant      → lib_scope_kind='assistant',     lib_scope_ref='__workspace__'
    # Empty strings preserve the legacy "unattached" mode (e.g. DICOM
    # zips that self-bind to a patient via DICOM tags).
    lib_scope_kind: str = Form(""),
    lib_scope_ref: str = Form(""),
    current_user: str = Depends(get_current_user),
) -> UploadResponse:
    """Receive a multipart upload, persist it across the layers of
    the file-storage model, and return its addressable file_id.

    Layer 1 (metadata, durable): a ``file_uploaded`` event lands in
    twin.event_log so the metadata (file_id + sha256 + name + mime +
    size) participates in the next state-root anchor on BSC.

    Layer 2 (server storage): bytes kept on local disk under
    ``UPLOAD_DIR/<user>/`` and indexed by the ``uploads`` SQLite
    table. ``read_uploaded_file`` always tries this layer first.

    Layer 3 (tool surface, stateless): the SDK
    ``ReadUploadedFileTool`` queries Layer 2 by user_id + name. This
    means twin instance lifecycle (idle eviction, cold restart) no
    longer affects file recall — the previous in-memory ``_file_reader``
    cache was the source of the cross-turn file-not-found bug.
    """
    _ensure_uploads_table()

    name = file.filename or "upload"
    # ── Filename normalisation ────────────────────────────────────
    # Some HTTP clients send non-ASCII filenames RFC 2047 encoded-word
    # style ("=?utf-8?B?<base64>?=") — desktop's multipart layer
    # does this for Chinese characters. Without decoding, the chip
    # shows gibberish AND extension-based detectors (image
    # normalizer, DICOM zip detector) miss the real ".tif" / ".zip"
    # suffix because it's hidden inside the base64 payload.
    # email.header.decode_header handles all encoding-word variants
    # (UTF-8 / GBK / ISO-8859 / etc.) and returns a list of
    # (chunk, charset) tuples we just join + decode.
    if name.startswith("=?") and "?=" in name:
        try:
            import email.header as _eh
            parts = _eh.decode_header(name)
            name = "".join(
                p.decode(c or "utf-8", errors="replace")
                if isinstance(p, bytes) else p
                for p, c in parts
            )
        except Exception as e:  # noqa: BLE001
            logger.debug("filename decode_header failed for %r: %s", name, e)

    # Mac / Linux clients sometimes send filename as a full path
    # ("Documents/2026-06/foo.tif" with embedded "/"). Strip the dir
    # portion — we already namespace by user + file_id on disk.
    if "/" in name or "\\" in name:
        name = name.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]

    # Trust client-provided mime when given, else guess from extension.
    mime = file.content_type or (mimetypes.guess_type(name)[0] or
                                 "application/octet-stream")
    # Application/octet-stream is the default desktop fallback for
    # MIMEs it doesn't recognise; on a real medical image we'd
    # rather lean on extension. mimetypes.guess_type is slightly
    # more comprehensive than our hand-rolled desktop table.
    if mime == "application/octet-stream":
        guessed = mimetypes.guess_type(name)[0]
        if guessed:
            mime = guessed
    file_id = uuid.uuid4().hex

    user_dir = _files_dir() / current_user
    user_dir.mkdir(parents=True, exist_ok=True)
    disk_path = user_dir / f"{file_id}-{_safe_name(name)}"

    # Stream the upload to disk in 1 MB chunks rather than buffering the
    # full payload in memory. Critical for DICOM CT studies — a 1 GB
    # zip used to OOM the server at `await file.read()`; now peak RSS
    # stays around 1 MB regardless of file size.
    #
    # Size cap is enforced as we go: when total > MAX_FILE_BYTES we
    # delete the partial file and 413. sha256 is computed incrementally
    # over the same chunk stream so we don't pay a second read pass.
    import hashlib
    hasher = hashlib.sha256()
    total = 0
    CHUNK = 1024 * 1024  # 1 MB
    try:
        with disk_path.open("wb") as out:
            while True:
                chunk = await file.read(CHUNK)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_FILE_BYTES:
                    # Clean up the partial write before raising so we
                    # don't leak disk space on rejected uploads.
                    out.close()
                    try:
                        disk_path.unlink(missing_ok=True)
                    except OSError as exc:
                        logger.debug("removing partial upload failed: %s", exc)
                    raise HTTPException(
                        status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                        detail=(
                            f"File exceeds {MAX_FILE_BYTES // (1024*1024)} MB "
                            f"limit (set NEXUS_MAX_FILE_BYTES to override)"
                        ),
                    )
                out.write(chunk)
                hasher.update(chunk)
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        # Mid-stream error (disk full, connection drop). Clean up.
        try:
            disk_path.unlink(missing_ok=True)
        except OSError as exc:
            logger.debug("removing partial upload failed: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Upload stream failed: {e}",
        )
    sha256 = hasher.hexdigest()

    # ── Layer 1: EventLog → BSC anchor ─────────────────────────────
    # Best-effort. If the user's twin isn't ready (fresh signup
    # pre-registration, local-mode dev), we still accept the upload —
    # Layer 2 (disk + SQL) is enough for chat to work.
    gnfd_path = ""  # legacy column value — kept "" for schema compat
    try:
        from nexus_server.twin_manager import get_twin
        twin = await get_twin(current_user)
        # Emit the file_uploaded event so the metadata participates
        # in the next state-root anchor.
        try:
            twin.event_log.append(
                "file_uploaded",
                f"📎 uploaded {name}",
                metadata={
                    "file_id": file_id,
                    "name": name,
                    "mime": mime,
                    "size_bytes": total,
                    "sha256": sha256,
                },
            )
        except Exception as e:  # noqa: BLE001
            logger.debug(
                "event_log append for file_uploaded failed: %s", e,
            )
    except Exception as e:  # noqa: BLE001
        # Twin not ready (e.g. unauthenticated test path): we still
        # write to disk + SQL so the next chat works.
        logger.debug("twin/chain unavailable for %s: %s", name, e)

    # ── #160: image format normalizer ─────────────────────────────
    # TIFF / RAW / BMP uploads need a JPEG transcoded copy because
    # Gemini's vision model doesn't accept those MIMEs (and even
    # those it does accept can be 100+ MB, well over the per-image
    # cap). Run synchronously in the upload route — typical 50 MB
    # TIFF transcodes in ~2 s and we WANT the response to wait so
    # the chat-time resolver always sees a fully-baked normalized
    # copy. If transcode fails, the upload still succeeds with the
    # original on disk — the medic can re-export from their PACS in
    # a supported format.
    image_normalized_status = ""
    image_normalized_path_str = ""
    try:
        from nexus_server.image_normalizer import (
            derive_normalized_path,
            looks_normalizable,
            transcode_to_jpeg,
        )
        if looks_normalizable(name, mime):
            norm_dest = derive_normalized_path(disk_path)
            norm_result = transcode_to_jpeg(
                source_path=disk_path,
                dest_path=norm_dest,
            )
            image_normalized_status = norm_result.get("status", "") or ""
            if image_normalized_status == "converted":
                image_normalized_path_str = str(norm_dest)
            logger.info(
                "image normalize — file=%s status=%s src=%d B → out=%d B "
                "(%dx%d)%s",
                name, image_normalized_status or "(none)",
                int(norm_result.get("src_bytes", 0)),
                int(norm_result.get("out_bytes", 0)),
                int(norm_result.get("out_width", 0)),
                int(norm_result.get("out_height", 0)),
                f" err={norm_result['error']}" if norm_result.get("error") else "",
            )
    except Exception as e:  # noqa: BLE001
        logger.warning(
            "image_normalize hook crashed on %s: %s — file still "
            "accepted, vision will fall back to whatever Gemini does "
            "with the original.", name, e,
        )

    # ── #158: async DICOM prerender ───────────────────────────────
    # Pre-#158 we did prerender synchronously here, which made
    # multi-GB CT uploads block the upload HTTP response for
    # 30-60s with zero progress feedback. The medic saw "uploading"
    # then "✓ DICOM ingested" with no idea of what happened in
    # between.
    #
    # New design: kick off the prerender on the BackgroundTasks queue
    # (which FastAPI runs AFTER the response goes out) and stamp the
    # uploads row with dicom_status="prerendering". The client polls
    # /api/v1/files/{file_id}/prerender-progress for stage / current /
    # total and updates the chip's progress bar in real time. Once
    # the background task hits state="done", the client refreshes the
    # chip to ✓ + Preview.
    name_lower = (name or "").lower()
    looks_like_zip = (
        mime == "application/zip" or name_lower.endswith(".zip")
    )
    if looks_like_zip:
        dicom_status = "prerendering"
        dicom_prerender_active = True
        # Initialise the progress tracker IMMEDIATELY (before the
        # response goes out) so the client's first poll always
        # sees a valid entry instead of an unknown-id 404.
        try:
            from nexus_server.dicom import _set_prerender_progress
            _set_prerender_progress(
                file_id, state="queued", stage="queued", total=1,
            )
        except Exception as exc:
            logger.debug("init prerender progress failed: %s", exc)
    else:
        dicom_status = ""
        dicom_prerender_active = False
    dicom_study_id = ""
    dicom_preview_dir = ""

    # ── Layer 2: SQL + disk index ─────────────────────────────────
    # Patient binding priority (highest → lowest):
    #   1. Explicit ``patient_hash`` form param — when the medic has
    #      a patient open in the desktop and drops a CT, they expect
    #      the upload to ATTACH to that patient. We honor this even
    #      for DICOM uploads, suppressing the PatientID-derived hash
    #      via the FORCE flag below.
    #   2. ``session_id`` → sessions.patient_hash  (#178 — inherited
    #      from active chat session for non-DICOM uploads).
    #   3. DICOM PatientID tag (parsed by background prerender; the
    #      COALESCE in that path keeps an inherited value only when
    #      the DICOM-derived hash is empty).
    inherited_patient_hash = ""
    force_patient_hash = False
    if patient_hash.strip():
        inherited_patient_hash = patient_hash.strip()
        force_patient_hash = True
        logger.info(
            "upload bound to patient %s by explicit override",
            inherited_patient_hash[:12],
        )
    elif session_id:
        try:
            with get_db_connection() as conn:
                row = conn.execute(
                    "SELECT patient_hash FROM sessions "
                    "WHERE session_id = ? AND user_id = ?",
                    (session_id, current_user),
                ).fetchone()
                if row and row[0]:
                    inherited_patient_hash = row[0]
        except Exception as e:  # noqa: BLE001
            logger.debug(
                "session→patient_hash lookup failed (%s): %s",
                session_id[:8], e,
            )
    now_iso = datetime.now(timezone.utc).isoformat()
    # F-unified-chat-files — default lib_scope to patient when the
    # caller didn't pass explicit scope AND we resolved a patient_hash.
    # This keeps existing per-patient uploads visible in the patient
    # chat's new file library without the desktop having to pass two
    # redundant fields on the same call.
    effective_lib_kind = (lib_scope_kind or "").strip()
    effective_lib_ref  = (lib_scope_ref  or "").strip()
    if not effective_lib_kind and inherited_patient_hash:
        effective_lib_kind = "patient"
        effective_lib_ref  = inherited_patient_hash
    with get_db_connection() as conn:
        conn.execute(
            """
            INSERT INTO uploads
            (file_id, user_id, name, mime, size_bytes, disk_path,
             created_at, sha256, gnfd_path, extracted_text,
             dicom_status, dicom_study_id, dicom_preview_dir,
             image_normalized_status, image_normalized_path,
             patient_hash, lib_scope_kind, lib_scope_ref)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, '', ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (file_id, current_user, name, mime, total,
             str(disk_path), now_iso, sha256, gnfd_path,
             dicom_status, dicom_study_id, dicom_preview_dir,
             image_normalized_status, image_normalized_path_str,
             inherited_patient_hash,
             effective_lib_kind, effective_lib_ref),
        )
        conn.commit()

    logger.info(
        "Uploaded file %s (%s, %d bytes, sha256=%s) for user %s",
        name, mime, total, sha256[:12], current_user,
    )

    # Memory Fix B: append a curated memory entry so the file persists
    # in MEMORY.md across sessions. Without this, MEMORY.md never
    # grows from user activity — only from explicit agent reflection.
    # Best-effort: if twin / curated_memory is not ready we skip.
    try:
        await _record_upload_in_curated_memory(
            current_user, name=name, mime=mime,
            size_bytes=total, uploaded_at=now_iso,
        )
    except Exception as e:  # noqa: BLE001
        logger.debug(
            "curated_memory upload record skipped for %s: %s", name, e,
        )

    # #158 — schedule the background prerender AFTER everything that
    # the synchronous response depends on has been committed to disk
    # + DB. FastAPI runs BackgroundTasks once the response has been
    # written to the wire, so the upload completes from the client's
    # POV in ~upload-time milliseconds (network only), and the heavy
    # DICOM parse + slice cache happens out of band.
    if dicom_prerender_active:
        background_tasks.add_task(
            _run_dicom_prerender_async,
            user_id=current_user, file_id=file_id, name=name,
            mime=mime, size_bytes=total, disk_path=disk_path,
            force_patient_hash=force_patient_hash,
        )

    # F-pdf-ocr-fallback + F-pdf-perpage-vision — kick the OCR pipeline
    # asynchronously for any file Gemini Vision can read:
    #   * PDFs (text-layer or scanned, per-page batched if scanned)
    #   * Direct images (jpg/png/heic/webp/etc) — Vision-only path
    # All other formats (docx, xlsx, txt) go through the lazy distiller
    # path triggered by ``read_uploaded_file`` and don't need a
    # background extractor (the distiller's text-mode handlers are
    # synchronous and cheap).
    _name_lc = str(name).lower()
    _is_pdf = (mime == "application/pdf"
               or _name_lc.endswith(".pdf"))
    _is_image = (
        (mime or "").startswith("image/")
        or _name_lc.endswith((
            ".jpg", ".jpeg", ".png", ".webp",
            ".heic", ".heif", ".gif", ".bmp",
        ))
    )
    if _is_pdf or _is_image:
        background_tasks.add_task(
            _run_pdf_extract_async,
            user_id=current_user, file_id=file_id, name=name,
            mime=mime, disk_path=str(disk_path),
        )

    return UploadResponse(
        file_id=file_id, name=name, mime=mime, size_bytes=total,
        dicom_status=dicom_status,
        dicom_study_id=dicom_study_id,
        dicom_prerender_active=dicom_prerender_active,
    )


def _run_pdf_extract_async(
    *, user_id: str, file_id: str, name: str, mime: str, disk_path: str,
) -> None:
    """Background task — populate ``uploads.extracted_text`` for a
    just-uploaded PDF or image via the OCR pipeline.

    Despite the legacy ``_pdf_`` name, this also handles direct image
    uploads (the routing inside ``pdf_extract.extract_and_persist``
    picks the right path by mime type). PDFs go pypdf → per-page
    Vision; images go straight to single-image Vision.

    Runs after the synchronous upload response has been flushed, so
    the medic isn't blocked. Status writes back to
    ``uploads.text_extraction_status`` so the chip UI can show:
      * (no badge)  text_layer success
      * 🤖          vision_ocr success
      * ⚠           unreadable (medic can re-extract from the UI)
      * 🔒          encrypted

    Sync wrapper around the async extractor — FastAPI's BackgroundTasks
    accepts both sync + async callables, but routing through asyncio
    keeps the inner work explicit.
    """
    import asyncio
    try:
        from nexus_server.pdf_extract import extract_and_persist
        asyncio.run(extract_and_persist(
            user_id=user_id, file_id=file_id,
            name=name, mime=mime, disk_path=disk_path,
        ))
        logger.info(
            "PDF extract: %s persisted for file_id=%s",
            name, file_id[:12],
        )
    except RuntimeError as exc:
        # asyncio.run() raises RuntimeError if we're already inside
        # an event loop (unlikely from a BackgroundTask, but defensive).
        # Spawn on a fresh thread loop in that case.
        if "asyncio.run" in str(exc) or "running event loop" in str(exc):
            import concurrent.futures as _cf
            with _cf.ThreadPoolExecutor(max_workers=1) as ex:
                from nexus_server.pdf_extract import extract_and_persist
                ex.submit(
                    asyncio.run,
                    extract_and_persist(
                        user_id=user_id, file_id=file_id,
                        name=name, mime=mime, disk_path=disk_path,
                    ),
                ).result()
        else:
            logger.warning(
                "PDF extract failed for %s (file_id=%s): %s",
                name, file_id[:12], exc,
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "PDF extract failed for %s (file_id=%s): %s",
            name, file_id[:12], exc,
        )


def _run_dicom_prerender_async(
    *, user_id: str, file_id: str, name: str, mime: str,
    size_bytes: int, disk_path: Path,
    force_patient_hash: bool = False,
) -> None:
    """Background task body — runs the existing
    prerender_archive_for_upload helper, then writes the resulting
    status + study_id + preview_dir back into the uploads row so
    subsequent file_id resolutions (chat-time DICOM rewrite, viewer
    open) see the persisted result.

    Errors are logged + reflected into the in-memory progress tracker
    (state="error"). The upload itself is already accepted by the
    time we get here — failures here only mean the agent / viewer
    fall back to less-rich behaviour.
    """
    try:
        from nexus_server.dicom import (
            _set_prerender_progress,
            prerender_archive_for_upload,
        )
        # If the medic bound this upload to a patient at upload time
        # (force_patient_hash), the uploads row already has that hash —
        # forward it into prerender so the dicom_studies INSERT uses it
        # directly. Eliminates the race window where the sidebar's
        # /api/v1/dicom/patients poll could observe a DICOM-PatientID-
        # derived hash between the INSERT and the post-ingest rebind.
        forced_hash = ""
        if force_patient_hash:
            try:
                with get_db_connection() as _conn:
                    _row = _conn.execute(
                        "SELECT patient_hash FROM uploads WHERE file_id = ?",
                        (file_id,),
                    ).fetchone()
                    if _row and _row[0]:
                        forced_hash = str(_row[0])
            except Exception as err:  # noqa: BLE001
                logger.debug("reading forced patient_hash failed: %s", err)
        prerender = prerender_archive_for_upload(
            user_id=user_id,
            upload_file_id=file_id,
            upload_name=name,
            upload_mime=mime,
            upload_size=size_bytes,
            disk_path=disk_path,
            patient_hash_override=forced_hash,
        )
        new_status = prerender.get("status", "") or ""
        new_study_id = prerender.get("study_id", "") or ""
        new_preview_dir = prerender.get("preview_dir", "") or ""
        logger.info(
            "DICOM upload verdict (async) — file=%s file_id=%s "
            "status=%s study_id=%s series=%d instances=%d modality=%s%s",
            name, file_id[:8], new_status or "(none)",
            (new_study_id or "(none)")[:8],
            int(prerender.get("series_count", 0)),
            int(prerender.get("instance_count", 0)),
            prerender.get("modality", ""),
            f" err={prerender['error']}" if prerender.get("error") else "",
        )
        # Persist updated fields onto the uploads row so chat-time
        # lookups (resolve_files / _maybe_rewrite_dicom_archive_to_pngs)
        # see them.
        # #178 — also propagate the patient_hash from the parsed
        # DICOM study onto the uploads row. Lets the per-patient
        # files endpoint return non-DICOM + DICOM uploads together
        # under one patient bucket (the medic's mental model is
        # "this patient's files," not "this study's PNGs").
        # Best-effort: feed the parsed study into the DicomIngester so
        # Layer 1 graph nodes (patient + study + key_image + …) appear
        # in clinical_graph_nodes. Without this, Memory tab stays at 0
        # and yield_t3_llm has no PATIENT CONTEXT to ground in.
        #
        # Failure mode handling: don't silently swallow. Persist the
        # error string onto the upload row so the desktop's progress
        # poll surfaces it ("Memory failed: <reason>") instead of the
        # Imaging mode card showing "Imported" while Memory stays at 0.
        # Helper for incremental status writes. We commit AFTER EACH
        # phase change instead of one big UPDATE at the end so the
        # desktop's 2-second poll sees:
        #   memory_status='pending'    (ingester running, ~10s)
        #   memory_status='ok'         (graph nodes emitted)
        #   quick_scan_status='pending'(Gemini calls running, ~30s)
        #   quick_scan_status='ok'/'error' + summary
        #
        # Without these intermediate commits the row sat at empty
        # strings for the full ~45-second pipeline and the UploadJobRow
        # rendered nothing under the "Imported" badge — medic just
        # saw a quiet card and assumed nothing was happening.
        def _bump(
            *, m_status=None, m_summary=None,
            qs_status=None, qs_summary=None,
        ):
            sets, args = [], []
            if m_status is not None:
                sets.append("memory_status = ?");      args.append(m_status)
            if m_summary is not None:
                sets.append("memory_summary = ?");     args.append(m_summary)
            if qs_status is not None:
                sets.append("quick_scan_status = ?");  args.append(qs_status)
            if qs_summary is not None:
                sets.append("quick_scan_summary = ?"); args.append(qs_summary)
            if not sets:
                return
            args.append(file_id)
            try:
                with get_db_connection() as conn:
                    conn.execute(
                        f"UPDATE uploads SET {', '.join(sets)} WHERE file_id = ?",
                        args,
                    )
                    conn.commit()
            except Exception as e:  # noqa: BLE001
                logger.warning("updating upload row failed: %s", e)

        memory_status = "pending"
        memory_summary = ""
        if new_status == "rendered" and new_study_id:
            # Bug fix (2026-06-15, P0 patient safety, Fix-C):
            # Write uploads.dicom_study_id NOW, BEFORE the ingester and
            # quick-scan helpers run. Previously this UPDATE happened
            # only at line ~1013, AFTER both helpers, so any lookup of
            # patient_hash via dicom_study_id during the helpers
            # resolved against stale prior-upload rows and findings
            # leaked across patients.
            # See: docs/design/IMAGING_PATIENT_ISOLATION_BUGFIX.md (Bug #3)
            try:
                with get_db_connection() as conn:
                    conn.execute(
                        "UPDATE uploads SET dicom_study_id = ? "
                        "WHERE file_id = ?",
                        (new_study_id, file_id),
                    )
                    conn.commit()
            except Exception as e:  # noqa: BLE001
                logger.warning(
                    "early dicom_study_id write failed (file=%s): %s",
                    file_id[:8], e,
                )
            # Mark pending IMMEDIATELY so the next desktop poll surfaces
            # "Memory: ingesting…" — without this the row showed only
            # "Imported" for 10+ seconds while the ingester worked.
            _bump(m_status="pending", m_summary="")
            try:
                summary = _run_dicom_ingester_safe(
                    user_id=user_id,
                    study_id=new_study_id,
                    file_id=file_id,
                    force_patient_hash=force_patient_hash,
                )
                memory_status  = "ok"
                memory_summary = summary
            except Exception as exc:  # noqa: BLE001
                logger.exception("dicom_ingester failed (%s)", exc)
                memory_status  = "error"
                memory_summary = f"{type(exc).__name__}: {exc}"[:480]
            _bump(m_status=memory_status, m_summary=memory_summary)

        # Tier A — automatic Quick scan after ingestion. Reuses the
        # existing Gemini Flash worker; on success the worker emits an
        # assistant_response event with metadata.kind="quick_scan_report"
        # AND we also write finding nodes here so Memory · L1 · Findings
        # populates. Result string lands on the uploads row for the
        # progress endpoint to surface.
        quick_scan_status = ""
        quick_scan_summary = ""
        if new_status == "rendered" and new_study_id and memory_status == "ok":
            quick_scan_status = "pending"
            # Same trick — commit pending BEFORE the long Gemini sweep
            # so the desktop's poll sees the in-progress state +
            # streams quick_scan_progress under the "Quick scan:
            # running…" line.
            _bump(qs_status="pending", qs_summary="")
            try:
                qs_summary = _run_quick_scan_after_ingest(
                    user_id=user_id,
                    study_id=new_study_id,
                    file_id=file_id,
                )
                quick_scan_status  = "ok"
                quick_scan_summary = qs_summary
            except Exception as exc:  # noqa: BLE001
                logger.exception("quick_scan failed (%s)", exc)
                quick_scan_status  = "error"
                quick_scan_summary = f"{type(exc).__name__}: {exc}"[:480]
            _bump(qs_status=quick_scan_status, qs_summary=quick_scan_summary)

        new_patient_hash = ""
        if new_status == "rendered" and new_study_id and not force_patient_hash:
            # Only derive patient_hash from the DICOM PatientID tag
            # when the medic DIDN'T explicitly bind the upload to a
            # patient. When force_patient_hash is set, the upload row
            # already has the desired patient_hash from the synchronous
            # insert above — we must not overwrite it.
            try:
                from nexus_server.dicom import load_study
                _study = load_study(user_id, new_study_id)
                new_patient_hash = (_study.patient_hash if _study else "") or ""
            except Exception as err:
                logger.debug("deriving patient_hash from study failed: %s", err)
        try:
            with get_db_connection() as conn:
                conn.execute(
                    "UPDATE uploads SET dicom_status = ?, "
                    "dicom_study_id = ?, dicom_preview_dir = ?, "
                    "patient_hash = COALESCE(NULLIF(?, ''), patient_hash) "
                    "WHERE file_id = ?",
                    (
                        new_status, new_study_id, new_preview_dir,
                        new_patient_hash, file_id,
                    ),
                )
                # Also rebind the DICOM study row to the override
                # patient_hash so /api/v1/dicom/patients/{hash}/studies
                # surfaces the study under the right patient.
                if force_patient_hash and new_study_id:
                    try:
                        import sqlite3 as _sql

                        from nexus_server.dicom import _index_db_path
                        dconn = _sql.connect(_index_db_path())
                        try:
                            # Re-read the synchronous insert's patient_hash
                            # (we don't have it as a param, so look it up).
                            row = conn.execute(
                                "SELECT patient_hash FROM uploads WHERE file_id = ?",
                                (file_id,),
                            ).fetchone()
                            forced = row[0] if row else ""
                            if forced:
                                dconn.execute(
                                    "UPDATE dicom_studies SET patient_hash = ? "
                                    "WHERE user_id = ? AND study_id = ?",
                                    (forced, user_id, new_study_id),
                                )
                                dconn.commit()
                                logger.info(
                                    "rebound study %s → patient %s (force)",
                                    new_study_id[:8], forced[:12],
                                )
                        finally:
                            dconn.close()
                    except Exception as e:  # noqa: BLE001
                        logger.warning("study rebind failed: %s", e)
                conn.commit()
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "Failed to persist async prerender status for %s: %s",
                file_id[:8], e,
            )

        # #178 — proactively emit an assistant_response event with a
        # one-paragraph patient summary right after prerender. This
        # way the medic, on next chat refresh, sees the agent has
        # already noticed the upload + volunteered patient context —
        # no need to type "what did I just upload?" or "summarize
        # this study." Eliminates one round-trip per study.
        #
        # NOTE: this is a *sync* background task (def, not async def);
        # we use `asyncio.run` to drive the async get_twin call.
        # asyncio.run is safe here because BackgroundTasks runs each
        # callable in its own thread (anyio.to_thread.run_sync) — there
        # is no enclosing event loop in this thread, so .run() can
        # spin up a fresh one without "RuntimeError: this event loop
        # is already running".
        if new_status == "rendered" and new_study_id:
            try:
                from nexus_server.dicom import load_study
                study = load_study(user_id, new_study_id)
                if study is not None:
                    summary_lines = []
                    short_hash = (study.patient_hash or "")[:12] or "(anonymous)"
                    demo = []
                    if study.patient_sex: demo.append(study.patient_sex)
                    if study.patient_age_group: demo.append(study.patient_age_group)
                    demo_str = " · ".join(demo) if demo else "no demographics"
                    summary_lines.append(
                        f"📁 New study ingested for patient "
                        f"PHI-hash:{short_hash} ({demo_str})."
                    )
                    summary_lines.append(
                        f"  • File: {name}  · "
                        f"{study.modality or '?'}  · "
                        f"{study.study_date or 'date unknown'}"
                    )
                    if study.study_description:
                        summary_lines.append(
                            f"  • Description: {study.study_description}"
                        )
                    summary_lines.append(
                        f"  • {len(study.series)} series · "
                        f"{study.total_instances} total slices"
                    )
                    summary_lines.append("")
                    summary_lines.append(
                        "Ready when you are — click 🩻 Preview on the "
                        "chip to open the viewer, or ask: 'what do "
                        "you see in the chest CT?' / '描述一下这个 "
                        "study 的发现'."
                    )

                    async def _emit_summary() -> None:
                        from nexus_server.twin_manager import get_twin
                        twin = await get_twin(user_id)
                        twin.event_log.append(
                            "assistant_response",
                            "\n".join(summary_lines),
                            metadata={
                                "kind":         "auto_patient_summary",
                                "study_id":     new_study_id,
                                "file_id":      file_id,
                                "patient_hash": study.patient_hash or "",
                            },
                        )
                    import asyncio as _asyncio
                    _asyncio.run(_emit_summary())
            except Exception as e:  # noqa: BLE001
                logger.debug(
                    "auto patient summary emit failed for %s: %s",
                    file_id[:8], e,
                )

        # #162 — write a curated memory entry for the patient so the
        # association ("medic uploaded study X for patient hash Y on
        # date Z") survives across chat sessions. Cross-session
        # patient continuity is the whole point of patient binding —
        # without this, if the medic logs out and back in, the agent
        # forgets which patient any previously-uploaded study belonged
        # to. Best-effort: any failure here just means cross-session
        # memory is shallower, the live turn still works fine.
        if new_status == "rendered" and new_study_id:
            try:
                import asyncio as _asyncio
                _asyncio.run(_record_patient_in_curated_memory(
                    user_id=user_id,
                    study_id=new_study_id,
                    modality=prerender.get("modality", ""),
                    name=name,
                    series_count=int(prerender.get("series_count", 0)),
                    instance_count=int(prerender.get("instance_count", 0)),
                ))
            except Exception as e:  # noqa: BLE001
                logger.debug(
                    "patient curated-memory write skipped for %s: %s",
                    file_id[:8], e,
                )
    except Exception as e:  # noqa: BLE001
        logger.exception(
            "DICOM prerender background task crashed for %s: %s",
            file_id[:8], e,
        )
        try:
            from nexus_server.dicom import _set_prerender_progress
            _set_prerender_progress(
                file_id, state="error", stage="task_crashed",
                error=f"{type(e).__name__}: {e}",
            )
        except Exception as err:
            logger.debug("setting prerender error status failed: %s", err)


@router.get(
    "/{file_id}/prerender-progress",
    response_model=PrerenderProgressResponse,
)
async def prerender_progress(
    file_id: str,
    current_user: str = Depends(get_current_user),
) -> PrerenderProgressResponse:
    """#158 — poll endpoint for the desktop's prerender progress bar.

    Returns the latest snapshot from the in-memory tracker in
    dicom._prerender_progress. When the tracker has no entry (either
    the upload was never a DICOM zip, or it happened more than an
    hour ago and was GC'd), we return state="unknown" so the client
    knows to stop polling.

    Auth note: we don't gate by user_id on the progress dict (it's
    keyed by file_id which is itself a uuid). Strictly, the caller
    could probe arbitrary file_ids — but they'd only get an opaque
    progress state, no bytes. If a stronger boundary is needed
    later, we can join through uploads.user_id.
    """
    from nexus_server.dicom import get_prerender_progress
    p = get_prerender_progress(file_id)

    # Tack on the ingester result (Layer 1 graph status) from the
    # uploads row. It's set by _run_dicom_prerender_async AFTER
    # prerender finishes so the desktop's upload card can render
    # "Imported · Memory: 6 graph events" or "Memory failed: …".
    memory_status = ""
    memory_summary = ""
    quick_scan_status = ""
    quick_scan_summary = ""
    upload_study_id = ""
    try:
        with get_db_connection() as conn:
            row = conn.execute(
                "SELECT memory_status, memory_summary, "
                "quick_scan_status, quick_scan_summary, dicom_study_id "
                "FROM uploads "
                "WHERE user_id = ? AND file_id = ?",
                (current_user, file_id),
            ).fetchone()
            if row:
                memory_status      = str(row[0] or "")
                memory_summary     = str(row[1] or "")
                quick_scan_status  = str(row[2] or "")
                quick_scan_summary = str(row[3] or "")
                upload_study_id    = str(row[4] or "")
    except Exception as e:  # noqa: BLE001
        logger.debug("reading upload status columns failed: %s", e)

    # Live progress for an in-flight Quick scan. Keyed by study_id —
    # whichever study this file is rendered into. The desktop polls
    # this endpoint every 2 s while quick_scan_status == 'pending', so
    # the medic sees the running tally + recent findings stream.
    qs_progress: Optional[dict] = None
    if upload_study_id:
        try:
            from nexus_server.quick_scan import get_quick_scan_progress
            qs_progress = get_quick_scan_progress(upload_study_id)
        except Exception:  # noqa: BLE001
            qs_progress = None

    if p is None:
        return PrerenderProgressResponse(
            state="unknown", stage="", current=0, total=0,
            percent=0.0,
            study_id=upload_study_id, preview_dir="", error="",
            memory_status=memory_status, memory_summary=memory_summary,
            quick_scan_status=quick_scan_status,
            quick_scan_summary=quick_scan_summary,
            quick_scan_progress=qs_progress,
        )
    total = max(0, int(p.get("total") or 0))
    current = max(0, int(p.get("current") or 0))
    pct = (100.0 * current / total) if total > 0 else (
        100.0 if p.get("state") == "done" else 0.0
    )
    return PrerenderProgressResponse(
        state=str(p.get("state") or "unknown"),
        stage=str(p.get("stage") or ""),
        current=current,
        total=total,
        percent=round(pct, 1),
        study_id=str(p.get("study_id") or upload_study_id),
        preview_dir=str(p.get("preview_dir") or ""),
        error=str(p.get("error") or ""),
        memory_status=memory_status,
        memory_summary=memory_summary,
        quick_scan_status=quick_scan_status,
        quick_scan_summary=quick_scan_summary,
        quick_scan_progress=qs_progress,
    )


# ─────────────────────────────────────────────────────────────────────
# Upload history — list endpoint for the Imaging tab
# ─────────────────────────────────────────────────────────────────────


class UploadHistoryRow(BaseModel):
    file_id:            str
    name:               str
    mime:               str
    size_bytes:         int
    created_at:         str
    patient_hash:       str
    dicom_status:       str
    dicom_study_id:     str
    memory_status:      str
    memory_summary:     str
    quick_scan_status:  str
    quick_scan_summary: str


@router.get("/uploads", response_model=list[UploadHistoryRow])
async def list_uploads(
    current_user: str = Depends(get_current_user),
    limit: int = 100,
    patient_hash: str = "",
) -> list[UploadHistoryRow]:
    """List the user's uploads, newest first. Used by the Imaging tab to
    render historical uploads (the in-memory job list only survives
    the current session). Optional ``patient_hash`` filter scopes to
    one patient's uploads."""
    _ensure_uploads_table()
    rows: list[UploadHistoryRow] = []
    where = "user_id = ?"
    params: list = [current_user]
    if patient_hash:
        where += " AND patient_hash = ?"
        params.append(patient_hash)
    try:
        with get_db_connection() as conn:
            for r in conn.execute(
                f"SELECT file_id, name, mime, size_bytes, created_at, "
                f"patient_hash, dicom_status, dicom_study_id, "
                f"memory_status, memory_summary, "
                f"quick_scan_status, quick_scan_summary "
                f"FROM uploads WHERE {where} "
                f"ORDER BY created_at DESC LIMIT ?",
                tuple(params) + (max(1, min(500, limit)),),
            ).fetchall():
                rows.append(UploadHistoryRow(
                    file_id=str(r[0] or ""),
                    name=str(r[1] or ""),
                    mime=str(r[2] or ""),
                    size_bytes=int(r[3] or 0),
                    created_at=str(r[4] or ""),
                    patient_hash=str(r[5] or ""),
                    dicom_status=str(r[6] or ""),
                    dicom_study_id=str(r[7] or ""),
                    memory_status=str(r[8] or ""),
                    memory_summary=str(r[9] or ""),
                    quick_scan_status=str(r[10] or ""),
                    quick_scan_summary=str(r[11] or ""),
                ))
    except Exception as exc:  # noqa: BLE001
        logger.warning("list_uploads failed: %s", exc)
    return rows


async def _record_upload_in_curated_memory(
    user_id: str, *,
    name: str, mime: str, size_bytes: int, uploaded_at: str,
) -> None:
    """Append a one-line fact to the user's MEMORY.md so the agent
    remembers ``user uploaded X on Y`` across sessions.

    Format: ``[YYYY-MM-DD] Uploaded file 'name.pdf' (mime, size).``

    Idempotent: CuratedMemory.add_memory dedupes by exact string match,
    so a re-upload of the same file on the same day produces no new
    entry. Different day → new entry, which is correct (multiple
    uploads of the same name are legitimate revision history).
    """
    from nexus_server.twin_manager import get_twin
    twin = await get_twin(user_id)
    cm = getattr(twin, "curated_memory", None)
    if cm is None:
        return

    # Honour the user's pause toggle (Phase C-2 memory_router).
    pause_marker = cm._dir / ".paused"
    try:
        if pause_marker.exists():
            return
    except Exception as exc:  # noqa: BLE001
        logger.debug("checking curated-memory pause marker failed: %s", exc)

    size_hint = format_size_hint(size_bytes)

    # Date-only stamp — agent doesn't need precise time-of-day in
    # curated memory; that's what event_log timestamps are for.
    date_part = uploaded_at[:10] if uploaded_at else ""

    entry = (
        f"[{date_part}] Uploaded file {name!r} "
        f"({mime}, {size_hint}). Use read_uploaded_file({name!r}) "
        f"to recall full contents."
    )
    try:
        cm.add_memory(entry)
    except Exception as e:  # noqa: BLE001
        logger.debug("curated_memory.add_memory failed: %s", e)


async def _record_patient_in_curated_memory(
    *, user_id: str, study_id: str, modality: str, name: str,
    series_count: int, instance_count: int,
) -> None:
    """#162 — emit a patient-identity memory entry into the user's
    MEMORY.md once a DICOM upload finishes prerendering successfully.

    Entry format::

        [YYYY-MM-DD] Patient PHI-hash:abc12345 — uploaded {modality}
        study {name!r} ({series_count} series, {instance_count} slices).

    Cross-session continuity: when the medic logs out and back in,
    or starts a new chat session, the agent loads this from
    curated memory and knows that any reference to "the patient"
    in subsequent uploads of the same study (via patient_hash) refers
    to the same person. Without this, the binding lives only in the
    in-memory dicom_studies table joined at chat-time — and any
    process restart breaks the link.

    Idempotent via CuratedMemory.add_memory's exact-match dedupe.
    """
    from datetime import datetime, timezone

    from nexus_server.dicom import get_patient_context_block
    from nexus_server.twin_manager import get_twin

    twin = await get_twin(user_id)
    cm = getattr(twin, "curated_memory", None)
    if cm is None:
        return
    try:
        if (cm._dir / ".paused").exists():
            return
    except Exception as exc:  # noqa: BLE001
        logger.debug("checking curated-memory pause marker failed: %s", exc)

    # Re-use the same patient context formatter so the memory entry
    # is consistent with what the agent sees in-chat — no chance of
    # drift between "what's in memory" and "what gets injected
    # per-turn".
    context = get_patient_context_block(user_id, study_id)
    date_part = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if context:
        entry = (
            f"[{date_part}] DICOM upload: {name!r} ({modality or '?'}, "
            f"{series_count} series, {instance_count} slices).\n"
            f"{context}"
        )
    else:
        entry = (
            f"[{date_part}] DICOM upload: {name!r} ({modality or '?'}, "
            f"{series_count} series, {instance_count} slices). "
            f"study_id={study_id[:8]}"
        )
    try:
        cm.add_memory(entry)
    except Exception as e:  # noqa: BLE001
        logger.debug("patient curated_memory.add_memory failed: %s", e)


def _safe_name(name: str) -> str:
    """Strip path traversal + bad chars from filename for disk storage.

    Normalises to NFC. macOS APFS stores filenames in NFD form on disk
    while every other layer (HTTP multipart headers, SQLite, Python's
    ``open(path)``) carries them as NFC. Without explicit normalisation,
    a Chinese filename can land on disk as NFD while ``uploads.disk_path``
    in SQLite holds the NFC form — and a subsequent ``Document(disk_path)``
    call ends up with two different byte strings that point at the same
    file only if APFS happens to auto-resolve them. python-docx hides the
    resulting ``FileNotFoundError`` behind its generic "Package not found"
    message, which makes the bug nearly impossible to diagnose.

    Picking NFC for both disk and DB keeps them in lockstep.
    """
    import unicodedata as _ud
    name = _ud.normalize("NFC", name)
    bad = '/\\:*?"<>|'
    return "".join("_" if c in bad else c for c in name)[:128]


# ── Internal helpers used by llm_gateway when resolving attachment_ids ──


def resolve_files(user_id: str, file_ids: list[str]) -> list[dict]:
    """Look up uploaded files by id (scoped to user) and return their
    on-disk content + metadata for the chat handler / distiller. The
    caller — typically llm_gateway when an Attachment.file_id is set —
    is responsible for reading bytes from ``disk_path``.
    """
    if not file_ids:
        return []
    placeholders = ",".join("?" * len(file_ids))
    with get_db_connection() as conn:
        rows = conn.execute(
            f"""
            SELECT file_id, name, mime, size_bytes, disk_path,
                   dicom_status, dicom_study_id, dicom_preview_dir,
                   image_normalized_status, image_normalized_path
            FROM uploads
            WHERE user_id = ? AND file_id IN ({placeholders})
            """,
            (user_id, *file_ids),
        ).fetchall()
    out = []
    for r in rows:
        fid, name, mime, size_bytes, disk_path = r[0], r[1], r[2], int(r[3]), r[4]
        norm_status = r[8] or ""
        norm_path = r[9] or ""
        # #160 — if the upload had a Gemini-incompatible format and the
        # normalizer produced a JPEG copy, transparently swap the
        # downstream view to point at the normalized file. The
        # original on disk keeps the lossless
        # source; only the chat-time multimodal path sees the JPEG.
        effective_mime = mime
        effective_disk = disk_path
        if norm_status == "converted" and norm_path:
            from pathlib import Path as _P
            np = _P(norm_path)
            if np.exists():
                effective_mime = "image/jpeg"
                effective_disk = norm_path
                try:
                    size_bytes = np.stat().st_size
                except OSError as e:
                    logger.debug("stat of normalized image failed: %s", e)
        out.append({
            "file_id":            fid,
            "name":               name,
            "mime":               effective_mime,
            "size_bytes":         size_bytes,
            "disk_path":          effective_disk,
            # #152 — DICOM prerender outputs carried through.
            "dicom_status":       r[5] or "",
            "dicom_study_id":     r[6] or "",
            "dicom_preview_dir":  r[7] or "",
            # #160 — image normalize metadata. The mime/disk_path above
            # already point at the normalized copy when applicable;
            # these extra fields are for diagnostic logging / UI badge.
            "image_normalized_status": norm_status,
            "original_mime":      mime,    # before swap (for "from .tif" hint)
        })
    return out


def read_file_bytes(disk_path: str) -> Optional[bytes]:
    p = Path(disk_path)
    if not p.exists():
        return None
    return p.read_bytes()


# ── Layer 3 surface used by ReadUploadedFileTool ─────────────────────


async def resolve_file_text(
    user_id: str, name: str,
) -> Optional[tuple[str, str]]:
    """Layered fallback resolution for ``read_uploaded_file``.

    Returns ``(filename, full_text)`` on hit; ``None`` if the file
    isn't reachable through any layer.

    Lookup strategy:
      1. **SQL cache** — ``uploads.extracted_text`` is the hot path
         (already-decoded plain text, ready to slice).
      2. **Disk → extract** — bytes still on local disk under
         ``UPLOAD_DIR``. Run the SDK distiller's text extractor and
         write the result back to ``extracted_text`` so future
         turns are O(1) again.

    All layers are best-effort and isolated — a disk problem can't
    make the SQL fast path stop working.
    """
    _ensure_uploads_table()

    # Match by file_id (exact) OR name (substring tolerated). The tool
    # supports partial-name matching for ergonomics, but we resolve
    # the canonical row via SQL ORDER BY most-recent-first to avoid
    # ambiguity when the user uploaded the same name twice.
    row = None
    with get_db_connection() as conn:
        # Exact name first.
        rs = conn.execute(
            """
            SELECT file_id, name, mime, size_bytes, disk_path,
                   sha256, gnfd_path, extracted_text
            FROM uploads
            WHERE user_id = ? AND name = ?
            ORDER BY created_at DESC LIMIT 1
            """,
            (user_id, name),
        ).fetchone()
        if rs is not None:
            row = rs
        else:
            # Substring fallback (matches tool's _find_file behaviour).
            like = f"%{name}%"
            rs = conn.execute(
                """
                SELECT file_id, name, mime, size_bytes, disk_path,
                       sha256, gnfd_path, extracted_text
                FROM uploads
                WHERE user_id = ? AND name LIKE ?
                ORDER BY created_at DESC LIMIT 1
                """,
                (user_id, like),
            ).fetchone()
            if rs is not None:
                row = rs

    if row is None:
        return None

    file_id = row[0]
    real_name = row[1]
    mime = row[2]
    disk_path = row[4]
    cached_text = row[7] or ""

    # Layer 1 hit: cached extracted_text.
    if cached_text:
        return real_name, cached_text

    # Layer 2: bytes on disk → extract → cache.
    text = await _extract_from_disk(disk_path, real_name, mime)
    if text:
        _save_extracted_text(file_id, text)
        return real_name, text

    return None


def _save_extracted_text(file_id: str, text: str) -> None:
    """Persist extracted text back into ``uploads.extracted_text`` so
    the next read for the same file is a SQL hit. We cap at a sane
    upper bound (1 MB) — anything larger is an LLM-context-busting
    document we shouldn't be inlining anyway."""
    capped = text if len(text) <= 1_000_000 else text[:1_000_000]
    try:
        with get_db_connection() as conn:
            conn.execute(
                "UPDATE uploads SET extracted_text = ? WHERE file_id = ?",
                (capped, file_id),
            )
            conn.commit()
    except Exception as e:  # noqa: BLE001
        logger.debug("save_extracted_text(%s) failed: %s", file_id, e)


async def _extract_from_disk(
    disk_path: str, name: str, mime: str,
) -> Optional[str]:
    p = Path(disk_path)
    if not p.exists():
        return None
    try:
        raw = p.read_bytes()
    except Exception as e:  # noqa: BLE001
        logger.debug("disk read failed for %s: %s", disk_path, e)
        return None
    return _bytes_to_text(raw, name, mime)


def _bytes_to_text(
    raw: bytes, name: str, mime: str,
) -> Optional[str]:
    """Run the SDK distiller's text extractor on raw bytes."""
    try:
        import base64 as _b64

        from nexus_core.distiller import extract_text
        b64 = _b64.b64encode(raw).decode("ascii")
        text, _src = extract_text(name, mime, None, b64)
        return text or None
    except Exception as e:  # noqa: BLE001
        logger.debug("extract_text(%s) failed: %s", name, e)
        return None


def list_user_files(user_id: str) -> dict[str, int]:
    """Return ``{filename: total_chars_or_size_bytes}`` for the
    ``read_uploaded_file()`` listing surface. Prefers
    ``len(extracted_text)`` when cached, falls back to
    ``size_bytes`` so the LLM still sees the file even if we
    haven't decoded it yet."""
    _ensure_uploads_table()
    with get_db_connection() as conn:
        rows = conn.execute(
            """
            SELECT name, size_bytes, extracted_text
            FROM uploads WHERE user_id = ?
            ORDER BY created_at DESC
            """,
            (user_id,),
        ).fetchall()
    out: dict[str, int] = {}
    for r in rows:
        # Replacing duplicate-name entries with the latest is fine —
        # the tool's listing surface is informational.
        out[r[0]] = len(r[2]) if r[2] else int(r[1])
    return out


def search_uploaded_files(
    user_id: str, query: str, limit: int = 5,
) -> list[dict]:
    """Memory Fix C: substring search over uploaded files' extracted
    text. Returns each hit's name, snippet centred on the match, and
    a sync_id-shaped ``file_id`` so the chat surface can link to it.

    Mirrors the wire shape ``search_messages`` uses so the
    ``search_past_chats`` tool can interleave file hits with chat hits.
    """
    q = (query or "").strip()
    if not q:
        return []

    _ensure_uploads_table()
    pattern = f"%{q}%"
    with get_db_connection() as conn:
        rows = conn.execute(
            """
            SELECT name, mime, created_at, extracted_text
            FROM uploads
            WHERE user_id = ?
              AND extracted_text != ''
              AND UPPER(extracted_text) LIKE UPPER(?)
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (user_id, pattern, int(limit)),
        ).fetchall()

    from nexus_server.twin_event_log import snippet_around as _snip

    hits: list[dict] = []
    for r in rows:
        name, mime, created_at, extracted_text = r
        hits.append({
            "file_name":   name,
            "mime":        mime or "",
            "uploaded_at": str(created_at) if created_at else "",
            "snippet":     _snip(extracted_text or "", q),
        })
    return hits


def list_recent_files_for_prompt(
    user_id: str, limit: int = 8, snippet_chars: int = 300,
) -> list[dict]:
    """Memory Fix A: return rich file metadata + a snippet of
    extracted_text for the twin's system prompt builder.

    Without this, the agent forgets files across sessions even though
    the bytes + extracted_text are still in the SQL store — it just
    has no idea they exist.

    Returns newest-first, at most ``limit`` rows. Each entry::

        {
          "name":         str,
          "mime":         str,
          "size_bytes":   int,
          "created_at":   ISO-8601 str,
          "snippet":      str,    # first ~snippet_chars of extracted_text
          "has_text":     bool,   # True iff extracted_text is non-empty
        }
    """
    _ensure_uploads_table()
    with get_db_connection() as conn:
        rows = conn.execute(
            """
            SELECT name, mime, size_bytes, created_at, extracted_text
            FROM uploads WHERE user_id = ?
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (user_id, int(limit)),
        ).fetchall()
    out: list[dict] = []
    seen_names: set[str] = set()
    for r in rows:
        name, mime, size_bytes, created_at, extracted_text = r
        if name in seen_names:
            continue
        seen_names.add(name)
        text = extracted_text or ""
        snippet = text[:snippet_chars].strip()
        if len(text) > snippet_chars:
            snippet += "…"
        out.append({
            "name":       name,
            "mime":       mime or "",
            "size_bytes": int(size_bytes or 0),
            "created_at": str(created_at) if created_at else "",
            "snippet":    snippet,
            "has_text":   bool(text),
        })
    return out


# ─────────────────────────────────────────────────────────────────────
# DICOM ingester bridge — runs after prerender to populate Layer 1 graph
# ─────────────────────────────────────────────────────────────────────


def _run_dicom_ingester_safe(
    *, user_id: str, study_id: str, file_id: str = "",
    force_patient_hash: bool = False,
) -> str:
    """Load the freshly-prerendered study from disk + feed it to the
    DicomIngester. Wraps the §5.1 pipeline so post-upload memorization
    happens automatically (Layer 1 graph nodes — patient/study/series/
    key_image — appear in clinical_graph_nodes).

    ``force_patient_hash``: when true, the upload was bound to an
    EXISTING patient by the medic. We honor that by passing the bound
    patient_hash to the ingester instead of the DICOM-derived one.

    Returns a short human-readable summary string for the upload row's
    ``memory_summary`` column (rendered by the desktop's Imaging
    upload card). Raises on any failure — the caller wraps and
    persists the exception text.

    Idempotent: the underlying _h_dicom_uploaded handler dedupes via
    (user_id, study_uid, sha256).
    """
    from nexus_server.dicom import load_study
    from nexus_server.event_sourcing import Store, init_event_sourcing_schema
    from nexus_server.memorization.dicom_ingester import (
        DicomIngester,
        StudyInput,
    )

    parsed = load_study(user_id, study_id)
    if parsed is None:
        raise RuntimeError(f"load_study returned None for study_id={study_id[:8]}")

    # Honor the override binding.
    patient_hash = parsed.patient_hash or ""
    if force_patient_hash:
        # The synchronous upload-time INSERT wrote the override into
        # uploads.patient_hash. Reuse the same value here so the graph
        # nodes are tagged with the chosen patient.
        #
        # Bug fix (2026-06-15, P0 patient safety, Fix-A):
        # Look up by file_id, NOT dicom_study_id. The previous query
        # used `WHERE dicom_study_id = ?` which could match a DIFFERENT
        # upload row (under a different patient_hash) that happened to
        # share the same StudyInstanceUID — re-uploading the same
        # DICOM under a fresh patient caused findings to land on the
        # OLD patient. file_id is the uploads PK, never ambiguous.
        # See: docs/design/IMAGING_PATIENT_ISOLATION_BUGFIX.md (Bug #1)
        if file_id:
            with get_db_connection() as conn:
                row = conn.execute(
                    "SELECT patient_hash FROM uploads "
                    "WHERE user_id = ? AND file_id = ?",
                    (user_id, file_id),
                ).fetchone()
                if row and row[0]:
                    patient_hash = row[0]
        else:
            logger.warning(
                "_run_dicom_ingester_safe: force_patient_hash=True but "
                "no file_id provided; falling back to DICOM-derived hash"
            )

    if not patient_hash:
        return "skipped — no patient binding"

    # Compose a StudyInput. We don't have rendered PNG bytes handy
    # here without re-reading the prerender cache; pass an empty
    # key_slices list. The ingester still emits the study + series +
    # patient nodes which is the part Memory mode reads. Key-image
    # nodes can be backfilled by a later pass.
    primary_series = parsed.series[0] if parsed.series else None
    study_input = StudyInput(
        study_uid=parsed.study_instance_uid or study_id,
        series_uid=(primary_series.series_instance_uid if primary_series else ""),
        modality=parsed.modality or "",
        body_part=(primary_series.body_part if primary_series else None),
        study_date=parsed.study_date or "",
        # DicomSeries has `.instances: list[DicomInstance]` + a
        # `.slice_count` @property — there is no `instance_count`
        # attribute. (Caught at runtime by my own ingester crash; the
        # error surfaced cleanly in the Imaging upload row, exactly as
        # the new failure-surfacing path intended.)
        frame_count=sum(len(s.instances) for s in parsed.series),
        # DicomStudy is the in-memory parsed object — it has no
        # `extract_dir`. That lives on the dicom_studies SQL TABLE,
        # accessed via SELECT in load_study() callers. For the
        # ingester StudyInput we only need a path *if* downstream
        # MONAI routing wants to re-open the file; for U3.3 Tier A
        # we pass empty (event_sourcing handlers ignore it).
        dicom_file_path="",
        dicom_sha256="",
        file_size_bytes=0,
        key_slices=[],
    )

    with get_db_connection() as conn:
        init_event_sourcing_schema(conn)
        store = Store(conn)
        ingester = DicomIngester(store=store, conn=conn)
        # Let exceptions PROPAGATE — the caller (the prerender task)
        # wants the error text so it can persist it for the UI.
        summary = ingester.ingest(
            user_id=user_id,
            patient_hash=patient_hash,
            study=study_input,
        )
        logger.info(
            "dicom_ingester complete: user=%s patient=%s study=%s summary=%s",
            user_id, patient_hash[:12], study_id[:8], summary,
        )
        # Summary shape per dicom_ingester.ingest's return type is a
        # dict of event counts. Render it as a one-line string for the
        # uploads.memory_summary column.
        if isinstance(summary, dict):
            nodes_total = sum(int(v) for v in summary.values() if isinstance(v, int))
            return f"{nodes_total} graph events"
        return str(summary)[:120]


# ─────────────────────────────────────────────────────────────────────
# Tier A — Quick scan after DICOM ingest
# ─────────────────────────────────────────────────────────────────────


def _run_quick_scan_after_ingest(
    *, user_id: str, study_id: str, file_id: str = "",
) -> str:
    """Drive nexus_server.quick_scan's worker synchronously + return a
    short human-readable summary for the upload row.

    The Quick scan worker emits its own assistant_response event with
    metadata.kind="quick_scan_report" into twin_event_log. We ALSO
    convert its flagged findings into `finding` graph nodes here so
    Memory · L1 · Findings populates (the same node type that comes
    out of chat_ingester — keeps Encounter's PATIENT CONTEXT able to
    reference them).

    ``file_id``: the uploads PK for the row that triggered this scan.
    Used to look up the bound ``patient_hash`` unambiguously. When
    omitted (legacy callers / older retry paths), we fall back to a
    dicom_study_id lookup with a safety guard against ambiguous
    cross-patient bindings.

    Returns "N flagged" / "no findings" / error-text.
    """
    from nexus_server import quick_scan
    from nexus_server.event_sourcing import (
        EventKind,
        Store,
        init_event_sourcing_schema,
    )
    from nexus_server.event_sourcing.handlers import _h_node_added

    # The worker is sync-wrappable (it drives its own asyncio loop).
    quick_scan._run_quick_scan_sync(user_id, study_id)

    # After the worker returns, fetch the report from the SDK's per-user
    # EventLog. quick_scan._emit_report wrote ``twin.event_log.append(
    # "assistant_response", body, metadata={"kind":"quick_scan_report",
    # "findings":[...], ...})`` — that lands as a row in the SDK's
    # ``events`` table (schema: nexus_core/memory/event_log.py — columns
    # ``idx, timestamp, event_type, content, metadata, agent_id,
    # session_id``).
    #
    # Bug history (2026-06-14): this used to SELECT FROM twin_event_log
    # with columns ``kind`` and ``payload``. Those names belong to the
    # MAIN DB's canonical event store (event_sourcing.schema.py), NOT
    # the SDK's per-user EventLog. Running the query against the wrong
    # table threw ``OperationalError: no such table: twin_event_log``
    # which the parent ``process_dicom_upload_async`` caught and
    # surfaced as "🔍 Quick scan failed: …" on the upload card — even
    # though the actual triage worker had succeeded.
    try:
        from nexus_server.twin_event_log import _open_readonly
    except Exception:
        _open_readonly = None  # type: ignore[assignment]

    report_findings: list[dict] = []
    # Also capture summary_counts so we can distinguish "all grids
    # came back clean" (medically interesting — image is genuinely
    # unremarkable) from "every Gemini call errored" (likely a dead
    # API key / network / quota issue — UI should NOT call this
    # 'no findings').
    summary_counts: dict = {}
    if _open_readonly is not None:
        evt_db = _open_readonly(user_id)
        if evt_db is not None:
            try:
                rows = evt_db.execute(
                    "SELECT metadata FROM events "
                    "WHERE event_type = 'assistant_response' "
                    "ORDER BY idx DESC LIMIT 20"
                ).fetchall()
                for (raw,) in rows:
                    try:
                        meta = json.loads(raw or "{}")
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(meta, dict):
                        continue
                    if meta.get("kind") != "quick_scan_report":
                        continue
                    if (meta.get("study_id") or "") != study_id:
                        continue
                    report_findings = list(meta.get("findings") or [])
                    summary_counts = dict(meta.get("summary_counts") or {})
                    break
            except sqlite3.OperationalError as exc:
                # Defensive: a stale SDK DB or a future schema rename
                # shouldn't take down the Quick scan workflow. Log,
                # continue with empty findings — the actual triage
                # report still lives in the SDK EventLog and the chat
                # view can render it from there.
                logger.warning(
                    "post-scan event lookup failed (user=%s): %s",
                    user_id, exc,
                )
            finally:
                evt_db.close()

    flagged = [
        f for f in report_findings
        if f.get("verdict") not in ("clean", "", None)
    ]

    # Resolve the patient_hash for this study via the uploads row
    # (which respects the upload-time force binding).
    #
    # Bug fix (2026-06-15, P0 patient safety, Fix-A + Fix-D):
    # Prefer the file_id lookup (uploads PK — never ambiguous). The
    # previous dicom_study_id-based query with ORDER BY created_at DESC
    # LIMIT 1 silently picked the WRONG row when the same
    # StudyInstanceUID was bound to multiple patients across uploads.
    # Result: quick-scan finding nodes landed on the prior patient.
    # If a caller did not supply file_id (legacy retry / external),
    # we fall back to the dicom_study_id lookup BUT first assert that
    # the lookup is unambiguous; otherwise we raise so the row goes
    # to quick_scan_status='error' and the medic sees a loud retry.
    # See: docs/design/IMAGING_PATIENT_ISOLATION_BUGFIX.md (Bug #1, Fix-D)
    patient_hash = ""
    try:
        with get_db_connection() as conn:
            if file_id:
                row = conn.execute(
                    "SELECT patient_hash FROM uploads "
                    "WHERE user_id = ? AND file_id = ?",
                    (user_id, file_id),
                ).fetchone()
                if row and row[0]:
                    patient_hash = str(row[0])
            else:
                # Defensive guardrail: require unambiguous binding.
                distinct = conn.execute(
                    "SELECT DISTINCT patient_hash FROM uploads "
                    "WHERE user_id = ? AND dicom_study_id = ? "
                    "AND patient_hash <> ''",
                    (user_id, study_id),
                ).fetchall()
                if len(distinct) > 1:
                    raise RuntimeError(
                        f"ambiguous patient binding for study {study_id[:8]}: "
                        f"{len(distinct)} distinct patient_hash values in "
                        f"uploads — refusing to write findings; please retry "
                        f"after disambiguating the upload row."
                    )
                if distinct:
                    patient_hash = str(distinct[0][0])
    except RuntimeError:
        raise
    except Exception as e:  # noqa: BLE001
        logger.debug("resolving patient_hash from uploads failed: %s", e)

    # Emit one `finding` node per flagged region, unconfirmed.
    if flagged and patient_hash:
        try:
            with get_db_connection() as conn:
                init_event_sourcing_schema(conn)
                store = Store(conn)
                for f in flagged:
                    text = str(f.get("finding") or "").strip()
                    if not text:
                        continue
                    urgency = str(f.get("urgency") or "").lower()
                    confidence = 0.6 if urgency == "critical" else 0.5
                    # Bug history (2026-06-14): this payload used the
                    # key "content" instead of "content_json". The
                    # EventSpec for NODE_ADDED ``required_fields`` is
                    # ("node_type", "content_json") — validate_payload
                    # raised KeyError before the row could land in
                    # clinical_graph_nodes. Quick scan reported "10
                    # flagged" (count from the report metadata) but
                    # the Memory tab + Patient · Active findings + chat
                    # PATIENT CONTEXT were all empty. Confusingly the
                    # exception got caught by the outer try, surfaced
                    # in the upload summary as "…  graph emit failed:
                    # …", but the medic saw the *count* on the row
                    # while the actual nodes were silently gone.
                    store.emit_and_apply(
                        kind=EventKind.NODE_ADDED,
                        payload={
                            "node_type": "finding",
                            "content_json": {
                                "label": text[:200],
                                "source": "quick_scan",
                                "study_id": study_id,
                                "urgency": urgency,
                                "status": "unconfirmed",
                            },
                            "encounter_id": f"quick_scan:{study_id[:8]}",
                        },
                        apply_fn=_h_node_added,
                        user_id=user_id,
                        patient_hash=patient_hash,
                    )
                conn.commit()
            logger.info(
                "quick_scan: wrote %d finding node(s) for study=%s patient=%s",
                len(flagged), study_id[:8], patient_hash[:12],
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("quick_scan finding-node emit failed: %s", exc)
            # Don't fail the whole quick_scan because of node-emit
            # trouble — the assistant_response event is already in
            # the log. Surface a partial-ok message.
            return f"{len(flagged)} flagged · graph emit failed: {exc}"

    if flagged:
        return f"{len(flagged)} flagged finding(s)"

    # No flagged findings — but distinguish "everything came back clean"
    # from "every Gemini call errored". The summary_counts dict (emitted
    # by quick_scan._summarise_counts) has separate counters for each
    # verdict bucket: clean / unsure / critical / moderate / incidental
    # / error.
    #
    # When errors dominate we RAISE rather than return a string — that
    # bubbles up to the caller's try/except and forces
    # ``quick_scan_status='error'`` on the uploads row. Without that
    # flip, the UI badge stays green ("Quick scan: ok") even though
    # the message says "scan failed", AND the Retry button (which
    # only renders on ``status='error'``) never appears — leaving the
    # medic stuck on a misleading "no findings" with no way out.
    #
    # Typical causes: expired GEMINI_API_KEY (most common — Google
    # rotates demo keys), Gemini quota / billing block, network outage.
    err_n   = int(summary_counts.get("error", 0) or 0)
    clean_n = int(summary_counts.get("clean", 0) or 0)
    total_n = sum(int(v or 0) for v in summary_counts.values())
    if err_n > 0 and err_n >= max(1, clean_n):
        # Concise error string — the caller writes
        # ``f"{type(exc).__name__}: {exc}"`` into uploads.quick_scan_summary
        # so we trim the message to keep the upload card readable.
        # Likely causes hint goes in the message; the diag panel /
        # server.log have the full Gemini traceback for triage.
        raise RuntimeError(
            f"scan failed on {err_n}/{total_n} grids — "
            f"check GEMINI_API_KEY in Settings · LLM (or network)."
        )

    if total_n > 0:
        return f"no flagged findings (scan complete, {clean_n}/{total_n} clean)"
    if report_findings:
        return "no flagged findings (scan complete)"
    return "no findings"


def retry_quick_scan_for_study(user_id: str, study_id: str) -> None:
    """Re-run Quick scan for an existing study, then write status back.

    Used by the manual retry button on the desktop's Imaging card when a
    Tier-A Quick scan failed. Behaves identically to the auto-fire path
    inside ``process_dicom_upload_async``:

      1. Find the uploads row matching ``(user_id, dicom_study_id)``.
      2. Mark its ``quick_scan_status='pending'`` so the existing
         prerender-progress poll on the frontend sees the in-progress
         state (and the Retry button hides itself).
      3. Run the full ``_run_quick_scan_after_ingest`` pipeline
         synchronously (it itself spawns the Gemini worker via
         ``asyncio.run`` internally).
      4. Capture the result string + write back
         ``quick_scan_status='ok'`` (or ``'error'`` on exception) and
         the matching summary.

    Idempotent: re-trying a row that's already ``ok`` re-runs the scan
    and overwrites the row. Re-trying a row whose ``dicom_study_id``
    no longer exists in uploads is a no-op (logs a warning).

    Designed to be invoked from FastAPI ``BackgroundTasks`` so the
    HTTP request returns immediately; the heavy lift happens off-band.
    """
    file_id: Optional[str] = None
    try:
        with get_db_connection() as conn:
            # Bug fix (2026-06-15, Fix-A + Fix-D):
            # Refuse to silently pick "the most recent" upload row when
            # multiple distinct patient_hash values are bound to the
            # same dicom_study_id — that was the exact mechanism by
            # which findings leaked across patients on re-upload.
            rows = conn.execute(
                "SELECT file_id, patient_hash FROM uploads "
                "WHERE user_id = ? AND dicom_study_id = ? "
                "ORDER BY created_at DESC",
                (user_id, study_id),
            ).fetchall()
            if not rows:
                logger.warning(
                    "retry_quick_scan: no uploads row for "
                    "user=%s study=%s",
                    user_id, study_id[:8],
                )
                return
            distinct_patients = {
                str(r[1] or "") for r in rows if (r[1] or "")
            }
            if len(distinct_patients) > 1:
                logger.error(
                    "retry_quick_scan: REFUSING to retry — study=%s has "
                    "%d distinct patient_hash bindings in uploads; "
                    "ambiguous which patient should own findings.",
                    study_id[:8], len(distinct_patients),
                )
                return
            file_id = str(rows[0][0])
            conn.execute(
                "UPDATE uploads SET "
                "quick_scan_status = 'pending', quick_scan_summary = '' "
                "WHERE file_id = ?",
                (file_id,),
            )
            conn.commit()
    except sqlite3.Error as exc:
        logger.exception(
            "retry_quick_scan: failed to mark pending (%s)", exc,
        )
        return

    new_status: str
    new_summary: str
    try:
        # Bug fix (2026-06-15, Fix-A): pass the file_id down so the
        # worker resolves patient_hash unambiguously even when multiple
        # uploads share the same StudyInstanceUID.
        new_summary = _run_quick_scan_after_ingest(
            user_id=user_id, study_id=study_id, file_id=file_id or "",
        )
        new_status = "ok"
    except Exception as exc:  # noqa: BLE001
        logger.exception("retry_quick_scan worker failed (%s)", exc)
        new_status = "error"
        new_summary = f"{type(exc).__name__}: {exc}"[:480]

    try:
        with get_db_connection() as conn:
            conn.execute(
                "UPDATE uploads SET "
                "quick_scan_status = ?, quick_scan_summary = ? "
                "WHERE file_id = ?",
                (new_status, new_summary, file_id),
            )
            conn.commit()
    except sqlite3.Error as exc:
        logger.exception(
            "retry_quick_scan: failed to write back status (%s)", exc,
        )
