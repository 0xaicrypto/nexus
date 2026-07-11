"""Schema migration registry (ADR-002 Rev-7 / design v3 §16.6).

Three invariants enforced (per §16.6):

1. Migrations never DELETE columns. Only ``ADD COLUMN`` + mark-as-deprecated.
2. Every record carries ``_schema_version`` (or ``schema_version`` for
   tables that pre-date the invariant).
3. ``twin_event_log`` is migration-immutable. Migrations may add new
   columns; they may not rewrite existing events.

Adding a migration:
* Append a function to ``MIGRATIONS`` with the new (target) version
  string and an ``up`` callable.
* On boot, ``apply_pending`` reads the current schema_version from
  ``event_log_schema_version`` and runs every migration with a higher
  target version in order.
* Each successful migration emits a ``SCHEMA_MIGRATION_APPLIED``
  event into the canonical log so replay sees the migration history.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from typing import Callable

from nexus_server.event_sourcing import EventKind, Store
from nexus_server.event_sourcing.handlers import _h_schema_migration_applied

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Migration:
    target_version: str
    description: str
    up: Callable[[sqlite3.Connection], None]


def _v3_2_add_finding_uncertainty(conn: sqlite3.Connection) -> None:
    """Example future migration — add an uncertainty field to finding
    nodes. content_json is JSON so we don't actually need a column add;
    this migration records intent in event_log so replay handlers can
    upgrade old content. M2+."""
    # No-op DDL — content_json is JSON; uncertainty key is just a new
    # field new ingester runs include. The migration EVENT itself is
    # the marker that replay's content-shape upgrader uses.
    pass


def _v3_3_add_redaction_policy_index(conn: sqlite3.Connection) -> None:
    """Add an index on redaction policy lookups (Rev-9 perf)."""
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_event_redaction_policy "
        "ON twin_event_log(event_kind, ts) "
        "WHERE event_kind = 'redaction_policy_changed'"
    )


# Append-only registry. New migrations get appended; old ones never
# removed or reordered.
MIGRATIONS: list[Migration] = [
    # M0 ships schema 3.1; nothing here is required for fresh installs.
    # These are placeholders showing the pattern for future versions.
    Migration(
        target_version="3.2",
        description="reserved for future content-shape upgrades",
        up=_v3_2_add_finding_uncertainty,
    ),
    Migration(
        target_version="3.3",
        description="redaction policy lookup index (Rev-9 perf)",
        up=_v3_3_add_redaction_policy_index,
    ),
]


def current_schema_version(conn: sqlite3.Connection) -> str:
    """Return the highest applied schema version (by semver, not ts).

    Sub-second test runs can produce multiple rows with identical
    ``applied_at`` int seconds; we pick by parsed semver so the tiebreak
    is deterministic.
    """
    rows = conn.execute(
        "SELECT version FROM event_log_schema_version"
    ).fetchall()
    if not rows:
        return "3.0"
    versions = [r[0] for r in rows]
    return max(versions, key=lambda v: tuple(
        int(p) for p in v.split(".") if p.isdigit()
    ))


def apply_pending(conn: sqlite3.Connection) -> list[str]:
    """Apply every migration whose target version is newer than current.

    Returns the list of target versions actually applied. Emits a
    ``SCHEMA_MIGRATION_APPLIED`` event for each, so replay knows.
    """
    import time
    current = current_schema_version(conn)
    applied: list[str] = []

    store = Store(conn)
    for m in MIGRATIONS:
        if _version_le(m.target_version, current):
            continue
        logger.info(
            "applying migration → %s (%s)", m.target_version, m.description,
        )
        try:
            m.up(conn)
            # Record completion in canonical event log
            store.emit_and_apply(
                kind=EventKind.SCHEMA_MIGRATION_APPLIED,
                payload={
                    "migration_id":   m.target_version,
                    "version_before": current,
                    "version_after":  m.target_version,
                },
                apply_fn=_h_schema_migration_applied,
                user_id="system",
            )
            # Update the schema_version table
            conn.execute(
                "INSERT OR REPLACE INTO event_log_schema_version "
                "(version, applied_at) VALUES (?, ?)",
                (m.target_version, int(time.time())),
            )
            conn.commit()
            applied.append(m.target_version)
            current = m.target_version
        except Exception:
            logger.exception("migration to %s failed; rolling back", m.target_version)
            conn.rollback()
            raise

    if applied:
        logger.info("applied %d migrations: %s", len(applied), applied)
    return applied


def _version_le(a: str, b: str) -> bool:
    """Lex-compare semver triplets ('3.1' vs '3.2'). Adequate for our scheme."""
    def parts(v: str) -> tuple[int, ...]:
        return tuple(int(p) for p in v.split(".") if p.isdigit())
    return parts(a) <= parts(b)
