"""Add ingester + Quick scan status columns to uploads.

Revision ID: 0002
Revises: 0001
Create Date: 2026-06-13

Per ENGINEERING_STANDARDS.md rule 2, these columns should never have
landed as `ALTER TABLE ADD COLUMN` inside _ensure_uploads_table(). This
migration moves them into the versioned chain so a fresh DB at head=0002
has them present, and an old DB at head=0001 gets them added cleanly
on upgrade.

Columns added (matching the existing _ensure_uploads_table definitions
so this migration is a no-op-equivalent for any DB that already has
the columns from the pre-Alembic code path):

  memory_status      TEXT NOT NULL DEFAULT ''
  memory_summary     TEXT NOT NULL DEFAULT ''
  quick_scan_status  TEXT NOT NULL DEFAULT ''
  quick_scan_summary TEXT NOT NULL DEFAULT ''

Idempotency is achieved by checking PRAGMA table_info first — Alembic's
op.add_column would otherwise fail with "duplicate column name" on
DBs that already have them.
"""
from __future__ import annotations

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def _column_exists(bind, table: str, column: str) -> bool:
    """SQLite-flavoured 'does this column exist?' check."""
    rows = bind.exec_driver_sql(f"PRAGMA table_info({table})").fetchall()
    return any(r[1] == column for r in rows)


def upgrade() -> None:
    bind = op.get_bind()
    new_cols = [
        # (column_name, definition)
        ("memory_status",      "TEXT NOT NULL DEFAULT ''"),
        ("memory_summary",     "TEXT NOT NULL DEFAULT ''"),
        ("quick_scan_status",  "TEXT NOT NULL DEFAULT ''"),
        ("quick_scan_summary", "TEXT NOT NULL DEFAULT ''"),
    ]
    for col, defn in new_cols:
        if _column_exists(bind, "uploads", col):
            continue
        bind.exec_driver_sql(f"ALTER TABLE uploads ADD COLUMN {col} {defn}")


def downgrade() -> None:
    # SQLite < 3.35 doesn't support DROP COLUMN. Even when supported,
    # dropping these would lose user data (which uploads were successfully
    # ingested + Quick-scanned). Per ENGINEERING_STANDARDS rule 2, data
    # loss on downgrade is never automatic — would-be reverters write a
    # new forward migration instead.
    pass
