"""Scheduled tasks projection table.

Revision ID: 0003
Revises: 0002
Create Date: 2026-06-14

Backs the Scheduled Tasks Phase 1 feature (see
``docs/design/scheduled-tasks-and-calendar.md``). Medic delegates a
future action through chat ("two hours from now email Dr Smith…"),
heuristic extractor proposes a task, user confirms via UI button,
worker fires it at fire_at.

Per ENGINEERING_STANDARDS rule 2 schema changes flow through
Alembic. The table is rebuildable by event-log replay (the
``REPLAY_HANDLERS`` for SCHEDULED_TASK_CREATED / _FIRED / _CANCELLED
project events into rows here), so a `DROP TABLE` + replay
reconstructs byte-identical state.

Columns:
  task_id        — uuid string, primary key
  user_id        — owning medic
  patient_hash   — nullable (cross-patient tasks like a weekly summary)
  session_id     — nullable (which chat thread proposed it)
  kind           — 'send_email' | 'chat_brief' | 'reminder'
  payload_json   — kind-specific payload (to/cc/subject/body for send_email)
  fire_at        — unix seconds UTC; worker scans for fire_at <= now()
  user_tz        — IANA zone ('Asia/Shanghai'); for display rendering
  recurrence_cron — NULL for one-shot; cron string for recurring
  status         — 'pending' | 'running' | 'done' | 'error' | 'cancelled'
  last_run_at    — unix sec the worker last attempted this row
  last_error     — verbatim error from last failed attempt
  result_json    — kind-specific result (relay msg, brief text, etc.)
  created_at     — unix sec
  updated_at     — unix sec (touched on every status change)
  cancelled_at   — unix sec (soft-delete marker)

Indices:
  (status, fire_at) — worker hot path
  (user_id)         — UI list endpoint hot path
"""
from __future__ import annotations

from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def _table_exists(bind, name: str) -> bool:
    rows = bind.exec_driver_sql(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (name,),
    ).fetchall()
    return bool(rows)


def upgrade() -> None:
    bind = op.get_bind()
    if _table_exists(bind, "scheduled_tasks"):
        return

    bind.exec_driver_sql(
        """
        CREATE TABLE scheduled_tasks (
            task_id         TEXT PRIMARY KEY,
            user_id         TEXT NOT NULL,
            patient_hash    TEXT,
            session_id      TEXT,
            kind            TEXT NOT NULL,
            payload_json    TEXT NOT NULL,
            fire_at         INTEGER NOT NULL,
            user_tz         TEXT NOT NULL DEFAULT 'UTC',
            recurrence_cron TEXT,
            status          TEXT NOT NULL DEFAULT 'pending',
            last_run_at     INTEGER,
            last_error      TEXT,
            result_json     TEXT,
            created_at      INTEGER NOT NULL,
            updated_at      INTEGER NOT NULL,
            cancelled_at    INTEGER
        )
        """
    )
    bind.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS idx_sched_status_fire "
        "ON scheduled_tasks (status, fire_at)"
    )
    bind.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS idx_sched_user "
        "ON scheduled_tasks (user_id)"
    )


def downgrade() -> None:
    # Soft no-op: dropping this table would lose user-confirmed pending
    # tasks. Per ENGINEERING_STANDARDS rule 2, data loss on downgrade
    # is never automatic. A reverter writes a forward migration if
    # they need to retire the feature.
    pass
