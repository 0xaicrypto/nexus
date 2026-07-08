"""Unified chat file library — uploads table scope + extraction status.

Revision ID: 0005
Revises: 0004
Create Date: 2026-06-28

Per UNIFIED_CHAT_FILES.md design. Adds the columns the unified file
library needs across all 4 chat surfaces (patient / research /
cross-research / assistant):

  lib_scope_kind            TEXT NOT NULL DEFAULT ''
      Which chat surface this file belongs to:
        '' (legacy / unattached) | 'patient' | 'research'
        | 'cross_research' | 'assistant'

  lib_scope_ref             TEXT NOT NULL DEFAULT ''
      Scope target:
        patient_hash       (for kind='patient')
        study_id           (for kind='research')
        '__workspace__'    (for kind='cross_research' / 'assistant')
        ''                 (legacy)

  text_extraction_status    TEXT NOT NULL DEFAULT 'pending'
      How extracted_text was obtained:
        'pending'    -- not yet attempted (upload still streaming)
        'text_layer' -- pypdf / python-docx / utf-8 success
        'vision_ocr' -- Gemini Vision fallback succeeded (scanned PDF
                        or image)
        'unreadable' -- all extraction paths failed
        'encrypted'  -- PDF is password-protected
        'error: ...' -- other exception (truncated to 200 chars)
      The UI surfaces this as a small badge on each file chip so the
      medic knows when AI vision was used vs deterministic extraction.

  deleted_at                INTEGER  (nullable)
      Soft-delete timestamp (unix ms). The 7-day GC cron physically
      deletes disk file + row after deleted_at + 7d. Until then the
      "已移除" tab lets the medic restore.

All adds are idempotent via PRAGMA table_info check — a fresh DB
gets them; an existing DB that already has any of them is a no-op.
"""
from __future__ import annotations

from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def _column_exists(bind, table: str, column: str) -> bool:
    rows = bind.exec_driver_sql(f"PRAGMA table_info({table})").fetchall()
    return any(r[1] == column for r in rows)


def _index_exists(bind, name: str) -> bool:
    row = bind.exec_driver_sql(
        "SELECT name FROM sqlite_master "
        "WHERE type='index' AND name=?",
        (name,),
    ).fetchone()
    return row is not None


def upgrade() -> None:
    bind = op.get_bind()

    new_cols = [
        # (column, definition)
        ("lib_scope_kind",         "TEXT NOT NULL DEFAULT ''"),
        ("lib_scope_ref",          "TEXT NOT NULL DEFAULT ''"),
        ("text_extraction_status", "TEXT NOT NULL DEFAULT 'pending'"),
        ("deleted_at",             "INTEGER"),
    ]
    for col, defn in new_cols:
        if _column_exists(bind, "uploads", col):
            continue
        bind.exec_driver_sql(
            f"ALTER TABLE uploads ADD COLUMN {col} {defn}"
        )

    # Index for the file-lib query: WHERE user_id = ? AND
    # lib_scope_kind = ? AND lib_scope_ref = ? AND deleted_at IS NULL.
    # Partial index keeps it small (skip soft-deleted rows).
    if not _index_exists(bind, "idx_uploads_lib"):
        bind.exec_driver_sql(
            "CREATE INDEX idx_uploads_lib "
            "ON uploads(user_id, lib_scope_kind, lib_scope_ref) "
            "WHERE deleted_at IS NULL"
        )

    # Backfill: existing rows pre-0005 have lib_scope_kind=''. We map
    # rows that already carry a patient_hash onto kind='patient' so
    # they auto-appear in the patient chat's file library without the
    # medic re-attaching them. Rows without patient_hash stay as ''
    # (legacy / unattached).
    bind.exec_driver_sql(
        "UPDATE uploads "
        "   SET lib_scope_kind = 'patient', "
        "       lib_scope_ref  = patient_hash "
        " WHERE lib_scope_kind = '' "
        "   AND patient_hash IS NOT NULL "
        "   AND patient_hash != ''"
    )

    # Backfill: existing rows have text_extraction_status='pending' from
    # the default. If extracted_text is already non-empty, mark as
    # 'text_layer' so we don't redundantly re-extract on every chat
    # turn. We can't tell text_layer vs vision_ocr retrospectively, but
    # it doesn't matter — the badge just reflects "we have text".
    bind.exec_driver_sql(
        "UPDATE uploads "
        "   SET text_extraction_status = 'text_layer' "
        " WHERE text_extraction_status = 'pending' "
        "   AND extracted_text != ''"
    )


def downgrade() -> None:
    # Same policy as 0002 — SQLite < 3.35 doesn't DROP COLUMN cleanly,
    # and rolling back would lose the lib_scope linkage that the chat
    # UIs depend on. Forward-only.
    pass
