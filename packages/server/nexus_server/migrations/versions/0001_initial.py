"""Initial schema — wraps existing init_*_table() functions.

Revision ID: 0001
Revises:
Create Date: 2026-06-13

This is the "absorb everything that existed before Alembic" migration.
It does NOT redefine tables in SQL here — we reuse the existing
init_*_table() helpers each module already exposes. Those helpers are
themselves idempotent (CREATE TABLE IF NOT EXISTS + ALTER TABLE ADD
COLUMN inside try/except), so re-running them is safe.

After this migration succeeds, the alembic_version table records us at
revision "0001". From here on, every schema change adds a new
versions/NNNN_*.py file.
"""
from __future__ import annotations

import logging

from alembic import op

logger = logging.getLogger(__name__)

# revision identifiers, used by Alembic.
revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Bring a fresh (or pre-Alembic) DB up to the legacy initial
    schema by delegating to each module's init function. They're
    idempotent (CREATE TABLE IF NOT EXISTS + safe ALTER), so this
    works for both fresh installs and DBs that were created by older
    builds without Alembic tracking."""
    # We can't use op.execute() with these helpers — they manage
    # their own connection. Run them through the alembic-bound
    # bind so they see the same DB:
    bind = op.get_bind()
    raw_conn = bind.connection           # actual DB-API connection

    # Each module's init function takes a sqlite3.Connection-like
    # object via its own get_db_connection. They write to whatever
    # nexus_server.config.DATABASE_URL points at — the same DB Alembic
    # is operating on. We invoke them in dependency order.
    import nexus_server.database as _db_mod
    from nexus_server.event_sourcing import init_event_sourcing_schema

    # event_log + projection_state + clinical_graph_* + cached_views
    init_event_sourcing_schema(raw_conn)

    # patients (manual roster)
    try:
        from nexus_server.patients_router import init_patients_table
        init_patients_table()
    except Exception as e:
        logger.debug("init_patients_table failed: %s", e)

    # uploads + memory_status etc.
    try:
        from nexus_server.files import _ensure_uploads_table
        _ensure_uploads_table()
    except Exception as e:
        logger.debug("_ensure_uploads_table failed: %s", e)

    # sessions / nexus_workflows / nexus_workflow_runs / async_tasks —
    # nexus_server.database.init_db() is the one-shot bootstrap that
    # creates all the "platform" tables. Cheap, idempotent.
    try:
        _db_mod.init_db()
    except Exception as e:
        logger.debug("init_db failed: %s", e)


def downgrade() -> None:
    """We don't support rolling back the initial schema — that would
    require dropping every table, and there's nowhere to go from
    'nothing'. Documented as no-op."""
    pass
