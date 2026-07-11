"""Database utilities and initialization.

Handles SQLite connection management and schema setup.
"""

import logging
import sqlite3
from contextlib import contextmanager
from typing import Generator

from nexus_server.config import get_config

logger = logging.getLogger(__name__)
config = get_config()


@contextmanager
def get_db_connection() -> Generator[sqlite3.Connection, None, None]:
    """Get a database connection context manager.

    F21 — enable WAL + busy_timeout so readers don't block on the
    long-running write transactions that ``session_takeaway`` and
    ``chat_ingester`` hold open during their LLM round-trips
    (10-30s each). Without these PRAGMAs the medic saw "AbortError:
    Fetch is aborted" on the 病人 / 记忆 tabs while the post-turn
    work was still completing.

      - ``journal_mode=WAL``: readers and writers no longer block
        each other; a writer commits in WAL and snapshot reads
        proceed without waiting. Persistent across connections —
        only the FIRST conn after the DB is opened needs to set it,
        but PRAGMA is idempotent so calling it on every conn is
        cheap and safe.
      - ``busy_timeout=5000``: if a write IS contended (rare in WAL
        but possible during checkpoint), wait up to 5s instead of
        immediately erroring with SQLITE_BUSY.
      - ``synchronous=NORMAL``: safe with WAL + acceptable durability
        for a local-only clinical workstation (we accept that a
        crash mid-write may lose the last 1-2 committed transactions
        in exchange for ~3x write throughput). The event log is
        append-only so even a torn write is auto-recovered.

    Yields:
        SQLite connection with row factory + WAL PRAGMAs applied.
    """
    db_path = config.DATABASE_URL.replace("sqlite:///", "")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA busy_timeout=5000;")
        conn.execute("PRAGMA synchronous=NORMAL;")
    except sqlite3.Error as e:
        # PRAGMA failure is non-fatal — fall back to default journal
        # mode. The medic will still see longer waits but the conn
        # is usable. We don't want a transient file-lock collision
        # at boot to kill the whole connection.
        logger.debug("PRAGMA setup failed: %s", e)
    try:
        yield conn
    finally:
        conn.close()


def init_db() -> None:
    """Initialize SQLite database with required tables."""
    db_path = config.DATABASE_URL.replace("sqlite:///", "")
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    # Users table
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            id TEXT PRIMARY KEY,
            display_name TEXT NOT NULL,
            passkey_credential TEXT,  -- LEGACY: retired passkey auth; column kept for existing DBs, no code reads/writes it
            jwt_secret TEXT NOT NULL,
            created_at TIMESTAMP NOT NULL,
            updated_at TIMESTAMP NOT NULL,
            chain_agent_id INTEGER,
            chain_register_tx TEXT
        )
        """
    )

    # Idempotent migration: add new columns if the table predates them.
    # CREATE TABLE IF NOT EXISTS won't add new columns; ALTER guarded by
    # PRAGMA inspection is the SQLite-portable pattern.
    cursor.execute("PRAGMA table_info(users)")
    existing_cols = {row[1] for row in cursor.fetchall()}
    if "chain_agent_id" not in existing_cols:
        cursor.execute("ALTER TABLE users ADD COLUMN chain_agent_id INTEGER")
    if "chain_register_tx" not in existing_cols:
        cursor.execute("ALTER TABLE users ADD COLUMN chain_register_tx TEXT")

    # ── Billing + approval columns (Phase: paid SaaS) ────────────────
    # status:       lifecycle gate set by admin approval flow.
    #               pending / approved / suspended / declined.
    # email:        primary contact + magic-link recovery target.
    # organization / intended_use / signup_metadata: collected at
    #               signup for the admin's review.
    # is_admin:     1 enables /api/v1/admin/* endpoints for this user.
    # tier:         subscription tier — drives quota + price.
    #               beta / trial / pro / pro_plus / radiology_pro /
    #               team_seat / enterprise.
    # stripe_customer_id:    Stripe Customer ID (cus_...) created on first
    #                        checkout.
    # stripe_subscription_id: Stripe Subscription ID (sub_...). Null when
    #                        user is on trial / no active sub.
    # subscription_state:    mirrors Stripe's status — none / trialing /
    #                        active / past_due / canceled / unpaid /
    #                        incomplete.
    # trial_ends_at:         when the no-card trial expires. Server
    #                        flips to 'trial_expired' if no subscription
    #                        by this time.
    # subscription_renews_at: convenience denormalisation of Stripe's
    #                        current_period_end. Avoids needing to hit
    #                        the API just to render "renews on …".
    billing_cols = {
        "status":                ("TEXT", "'approved'"),  # legacy users grandfathered
        "email":                 ("TEXT", "NULL"),
        "organization":          ("TEXT", "NULL"),
        "intended_use":          ("TEXT", "NULL"),
        "signup_metadata":       ("TEXT", "NULL"),
        "signup_ip":             ("TEXT", "NULL"),
        "approved_at":           ("TIMESTAMP", "NULL"),
        "approved_by":           ("TEXT", "NULL"),
        "admin_notes":           ("TEXT", "NULL"),
        "is_admin":              ("INTEGER", "0"),
        "tier":                  ("TEXT", "'beta'"),
        "stripe_customer_id":    ("TEXT", "NULL"),
        "stripe_subscription_id":("TEXT", "NULL"),
        "subscription_state":    ("TEXT", "NULL"),
        "trial_ends_at":         ("TIMESTAMP", "NULL"),
        "subscription_renews_at":("TIMESTAMP", "NULL"),
        # F26.1 — multi-identity (USER_MANAGEMENT.md §4) :
        # avatar_emoji  per-identity single emoji for picker (default 🩺)
        # deleted_at    soft-delete timestamp; queries WHERE deleted_at
        #               IS NULL by default. 90-day GC job hard-deletes
        #               (see F26.3 distiller).
        # last_active_at  refreshed on every successful /auth/login or
        #               /identities/{id}/activate; drives picker sort.
        "avatar_emoji":          ("TEXT", "'🩺'"),
        "deleted_at":            ("TIMESTAMP", "NULL"),
        "last_active_at":        ("TIMESTAMP", "NULL"),
        # Password auth redesign (2026-07):
        # password_hash  bcrypt hash. NULL = legacy account that has not
        #                been claimed yet (see /auth/claim).
        # role           'admin' | 'user'. First registered user = admin.
        # disabled_at    set by an admin to lock the account out; JWTs
        #                of disabled users are rejected in
        #                get_current_user.
        # last_login_at  updated on every successful password login /
        #                claim; surfaced in GET /api/v1/admin/users.
        "password_hash":         ("TEXT", "NULL"),
        "role":                  ("TEXT NOT NULL", "'user'"),
        "disabled_at":           ("TEXT", "NULL"),
        "last_login_at":         ("TEXT", "NULL"),
    }
    for col, (typ, default) in billing_cols.items():
        if col not in existing_cols:
            cursor.execute(
                f"ALTER TABLE users ADD COLUMN {col} {typ} DEFAULT {default}"
            )

    # Index frequent admin queries: "pending users sorted by signup time".
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_users_status_created "
        "ON users(status, created_at DESC)"
    )

    # Usernames (display_name) must be unique among live accounts now
    # that they are the password-login key. Legacy DBs may contain
    # duplicates from the old passwordless register — in that case the
    # index creation fails and we fall back to the check-before-insert
    # guard in /auth/register (which is always active anyway).
    try:
        cursor.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_users_username_unique "
            "ON users(lower(display_name)) WHERE deleted_at IS NULL"
        )
    except sqlite3.Error as e:
        logger.warning(
            "unique username index not created (legacy duplicate "
            "display_names?): %s — register falls back to "
            "check-before-insert", e,
        )

    # Phase B: ``sync_events`` table dropped.
    #
    # Pre-S5 the server mirrored every twin emit here so legacy
    # /agent/timeline + /agent/memories endpoints could read events
    # without poking into twin's per-user EventLog. After S5 those
    # endpoints opened twin's EventLog directly via ``twin_event_log``,
    # and the mirror became write-only — no production read path
    # consulted it. Phase B drops the table along with its three
    # writers (twin_manager._mirror_to_sync_events,
    # attachment_distiller.record_distilled_event, and the deleted
    # sync_hub /sync/push handler). If a stale instance still has the
    # table from a pre-Phase-B boot, it sits there harmless — nothing
    # writes to or reads from it.

    # Rate limiting table
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS rate_limits (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            endpoint TEXT NOT NULL,
            request_count INTEGER NOT NULL,
            window_start TIMESTAMP NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users(id)
        )
        """
    )

    # NOTE: an earlier short-lived design had a separate `memories` table
    # for per-row insights (Nexus MemoryEvolver style). We walked that
    # back to align with SDK ARCHITECTURE.md's DPM principle: EventLog
    # is the single source of truth, every "memory" is a derived view of
    # `memory_compact` events in sync_events. The CREATE TABLE for
    # memories is intentionally absent here — its sister code in
    # memory_service.py reads memory_compact events from sync_events
    # directly.

    # Sync anchors table — one row per /sync/push batch that we attempt
    # to anchor on BSC. Status is the source of truth for "did the
    # anchor land yet".
    #
    # Status values:
    #   'pending'              — created, work hasn't started
    #   'stored_only'          — hash computed + stored locally, BSC
    #                            anchor skipped (no chain config or no
    #                            agent id)
    #   'anchored'             — BSC anchor succeeded
    #   'failed'               — terminal failure (see error column)
    #   'awaiting_registration' — user has no chain_agent_id yet
    #
    # greenfield_path is a LEGACY column from the removed decentralised
    # object-storage mirror. Kept in the schema so existing databases
    # don't need a migration; never written for new rows.
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS sync_anchors (
            anchor_id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL,
            first_sync_id INTEGER NOT NULL,
            last_sync_id INTEGER NOT NULL,
            event_count INTEGER NOT NULL,
            content_hash TEXT NOT NULL,
            greenfield_path TEXT,
            bsc_tx_hash TEXT,
            status TEXT NOT NULL,
            error TEXT,
            created_at TIMESTAMP NOT NULL,
            updated_at TIMESTAMP NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users(id)
        )
        """
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_sync_anchors_user "
        "ON sync_anchors(user_id, anchor_id DESC)"
    )

    # retry_count: how many times the daemon has tried to push this row
    # past 'failed'/'awaiting_registration' into a terminal good state.
    # Idempotent migration so we don't crash on existing DBs.
    cursor.execute("PRAGMA table_info(sync_anchors)")
    sync_anchor_cols = {row[1] for row in cursor.fetchall()}
    if "retry_count" not in sync_anchor_cols:
        cursor.execute(
            "ALTER TABLE sync_anchors ADD COLUMN retry_count INTEGER NOT NULL DEFAULT 0"
        )
    if "next_retry_at" not in sync_anchor_cols:
        cursor.execute(
            "ALTER TABLE sync_anchors ADD COLUMN next_retry_at TIMESTAMP"
        )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_sync_anchors_retry "
        "ON sync_anchors(status, next_retry_at)"
    )

    # ── twin_chain_events (Bug 3 visibility, post-S4) ────────────────
    # After S4 the legacy /sync/push → enqueue_anchor path stopped firing
    # for chat traffic, so ``sync_anchors`` no longer accumulates rows
    # for normal chat-mode users. The desktop sidebar's anchor counters
    # were therefore stuck at 0/0/0 even when twin's ChainBackend was
    # successfully writing BSC anchors in the background. Without a row
    # anywhere, anchor failures only surfaced in server stderr —
    # invisible to the operator.
    #
    # This table is the new mirror: a logging.Handler in twin_manager
    # subscribes to the SDK's ``rune.backend.chain`` logger and writes
    # one row per chain write
    # attempt. ``status`` is intentionally only ``ok`` / ``failed`` —
    # twin's ChainBackend is synchronous w.r.t. the BSC tx, so there
    # is no "pending" state to track here (unlike legacy sync_anchors).
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS twin_chain_events (
            event_id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL,
            kind TEXT NOT NULL,
            status TEXT NOT NULL,
            summary TEXT,
            tx_hash TEXT,
            content_hash TEXT,
            object_path TEXT,
            error TEXT,
            duration_ms INTEGER,
            created_at TIMESTAMP NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users(id)
        )
        """
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_twin_chain_events_user "
        "ON twin_chain_events(user_id, event_id DESC)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_twin_chain_events_status "
        "ON twin_chain_events(user_id, status)"
    )

    # ── nexus_sessions (multi-session per user) ─────────────────────
    # Each row is one logical "conversation thread" — analogous to
    # ChatGPT's left-side chat list or Cowork's task list. The user
    # can keep many parallel threads with the same agent; the server
    # routes /llm/chat to the right one via the ``session_id`` field
    # the client passes in.
    #
    # ``id`` is a short opaque token (twin's _thread_id format —
    # ``session_xxxxxxxx``) so it lines up with what twin's EventLog
    # already stamps onto each row's ``session_id`` column. That keeps
    # the join from this table → twin event_log a literal string match
    # without any indirection layer.
    #
    # ``title`` is the human-readable label shown in the sidebar. We
    # auto-generate one from the first user message after a few turns
    # (see sessions.maybe_autotitle); operators / users can rename via
    # PATCH /api/v1/sessions/{id}.
    #
    # ``archived`` is a soft-delete flag. We never hard-delete because:
    #   * twin's event_log still has the messages; deleting the row
    #     would orphan them and confuse the audit trail.
    #   * operators sometimes want to un-archive a thread they thought
    #     they were done with.
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS nexus_sessions (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            title TEXT NOT NULL,
            created_at TIMESTAMP NOT NULL,
            last_message_at TIMESTAMP,
            message_count INTEGER NOT NULL DEFAULT 0,
            archived INTEGER NOT NULL DEFAULT 0,
            FOREIGN KEY (user_id) REFERENCES users(id)
        )
        """
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_nexus_sessions_user "
        "ON nexus_sessions(user_id, archived, last_message_at DESC)"
    )

    # ── Workflows (Phase 1a) ─────────────────────────────────────────
    #
    # A workflow is a stored sequence of agent steps (skill names) the
    # user can run end-to-end. Definition is held as a JSON blob in
    # ``definition`` so the schema can evolve without per-field
    # migrations — see workflows.py for the WorkflowDefinition shape.
    #
    # We track runs separately so a single workflow can have many
    # concurrent / historical executions, each with its own per-step
    # input/output trace. This is the "chain-anchored audit" anchor
    # point for Phase 4 — each run gets a Merkle root over its step
    # rows + a BSC tx hash once anchored.
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS nexus_workflows (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            name TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            definition TEXT NOT NULL,                 -- JSON: WorkflowDefinition
            created_at TIMESTAMP NOT NULL,
            updated_at TIMESTAMP NOT NULL,
            archived INTEGER NOT NULL DEFAULT 0,
            FOREIGN KEY (user_id) REFERENCES users(id)
        )
        """
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_nexus_workflows_user "
        "ON nexus_workflows(user_id, archived, updated_at DESC)"
    )

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS nexus_workflow_runs (
            id TEXT PRIMARY KEY,
            workflow_id TEXT NOT NULL,
            user_id TEXT NOT NULL,
            status TEXT NOT NULL,                     -- pending | running | succeeded | failed | cancelled
            inputs TEXT NOT NULL DEFAULT '{}',        -- JSON: input map at submit time
            error_message TEXT NOT NULL DEFAULT '',
            current_step INTEGER NOT NULL DEFAULT 0,
            total_steps INTEGER NOT NULL DEFAULT 0,
            total_cost_usd REAL NOT NULL DEFAULT 0.0, -- rolled up across steps
            started_at TIMESTAMP NOT NULL,
            finished_at TIMESTAMP,
            anchor_tx TEXT,                           -- BSC tx hash (Phase 4)
            -- v2.1 iterative mode tracking. Default 0 / "" so existing
            -- linear-mode rows continue to deserialise cleanly.
            current_iteration INTEGER NOT NULL DEFAULT 0,
            max_iterations INTEGER NOT NULL DEFAULT 0,
            last_gatekeeper_verdict TEXT NOT NULL DEFAULT '',
            FOREIGN KEY (workflow_id) REFERENCES nexus_workflows(id),
            FOREIGN KEY (user_id) REFERENCES users(id)
        )
        """
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_nexus_workflow_runs_user "
        "ON nexus_workflow_runs(user_id, started_at DESC)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_nexus_workflow_runs_workflow "
        "ON nexus_workflow_runs(workflow_id, started_at DESC)"
    )

    # Migration: backfill v2.1 columns on existing tables (idempotent).
    # ALTER TABLE ADD COLUMN doesn't have IF NOT EXISTS in SQLite, so we
    # introspect first.
    existing_cols = {
        r[1] for r in cursor.execute(
            "PRAGMA table_info(nexus_workflow_runs)"
        ).fetchall()
    }
    for col, ddl in [
        ("current_iteration",       "INTEGER NOT NULL DEFAULT 0"),
        ("max_iterations",          "INTEGER NOT NULL DEFAULT 0"),
        ("last_gatekeeper_verdict", "TEXT NOT NULL DEFAULT ''"),
    ]:
        if col not in existing_cols:
            cursor.execute(
                f"ALTER TABLE nexus_workflow_runs ADD COLUMN {col} {ddl}"
            )

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS nexus_workflow_run_steps (
            run_id TEXT NOT NULL,
            step_index INTEGER NOT NULL,
            skill_name TEXT NOT NULL,
            status TEXT NOT NULL,                     -- pending | running | succeeded | failed | skipped
            input TEXT NOT NULL DEFAULT '',           -- handoff payload sent to LLM
            output TEXT NOT NULL DEFAULT '',          -- model's full response
            model_used TEXT NOT NULL DEFAULT '',
            cost_usd REAL NOT NULL DEFAULT 0.0,
            started_at TIMESTAMP,
            finished_at TIMESTAMP,
            error_message TEXT NOT NULL DEFAULT '',
            -- v2.1: iteration this step belongs to (0 for linear).
            -- The PRIMARY KEY changes from (run_id, step_index) to
            -- (run_id, iteration, step_index) so an iterative run can
            -- store multiple rows per (run_id, step_index).
            iteration INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (run_id, iteration, step_index),
            FOREIGN KEY (run_id) REFERENCES nexus_workflow_runs(id)
        )
        """
    )

    # Migration: existing nexus_workflow_run_steps rows have PRIMARY KEY
    # (run_id, step_index); we need to upgrade to include iteration.
    # SQLite can't ALTER PRIMARY KEY, so we only do the ALTER for the
    # iteration column. New iterative runs accept the collision risk —
    # we mitigate by always writing iteration>=1 for iterative runs and
    # checking for existing rows before INSERT (handled in runner).
    existing_step_cols = {
        r[1] for r in cursor.execute(
            "PRAGMA table_info(nexus_workflow_run_steps)"
        ).fetchall()
    }
    if "iteration" not in existing_step_cols:
        cursor.execute(
            "ALTER TABLE nexus_workflow_run_steps "
            "ADD COLUMN iteration INTEGER NOT NULL DEFAULT 0"
        )

    # ── user_settings ───────────────────────────────────────────────
    # Persistent key/value store for LLM API keys + per-user prefs.
    # Lives in rune_server.db (NOT the .env file) so:
    #   - Reinstalling Nexus.app keeps keys (db is at $RUNE_HOME/data
    #     or wherever DATABASE_URL points, both survive .app removal).
    #   - Upgrading the bundle keeps keys (same reason).
    #   - Operators can grep / dump / migrate keys with one SQL query.
    # .env still gets written for backward-compat with the v1 bootstrap
    # that reads .env at sidecar launch (see lib.rs::load_user_env);
    # but the DB row is the SOURCE OF TRUTH from now on.
    #
    # ``user_id = '_global'`` is the operator-owned default that applies
    # to anyone who hasn't set a per-user override. Per-user override
    # is a Phase-2 feature (multi-tenant SaaS); current desktop use
    # writes/reads _global only.
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS user_settings (
            user_id    TEXT NOT NULL,
            key        TEXT NOT NULL,
            value      TEXT NOT NULL,
            updated_at INTEGER NOT NULL,
            PRIMARY KEY (user_id, key)
        )
        """
    )

    conn.commit()
    conn.close()
    logger.info("Database initialized")
