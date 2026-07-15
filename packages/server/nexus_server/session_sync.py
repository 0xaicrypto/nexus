"""
session_sync — Make session metadata (title, archived state, deletion)
durable across server migrations by routing every change through the
twin's EventLog.

Why this module exists
======================

Before this module, the only place the server knew that ``session_16fa``
was titled "BSC 调试" was the local SQLite ``nexus_sessions`` table.
The chat content of that session was already durable — it lives in
twin's EventLog, which anchors on BSC. But the
**human-friendly metadata** (title, archived flag) was server-local.

So restoring from backup / migrating to a new VPS / wiping the SQL
volume would lose all titles. The sidebar would render
"session_16fa0e99 / session_acdaa106 / …" — an unfriendly UI even
though the underlying chat data is intact.

Design
======

Each session metadata change emits a ``session_metadata`` event into
twin's EventLog with this metadata schema::

    {
      "session_id": "session_16fa0e99",
      "action":     "create" | "rename" | "archive" | "unarchive" | "delete",
      "title":      "BSC 调试" | None,
      "archived":   true | false | None,
    }

Any ``None`` field means "not changed by this event". Replay applies
events oldest-first; last-write-wins per ``session_id``.

EventLog already does a backend snapshot + BSC state-root anchor on
compaction, so these metadata events ride the same durability path as
chat messages — no new sync infrastructure.

Reconstruction at startup
=========================

``replay_session_metadata(user_id)`` walks the user's EventLog filtered
to ``event_type='session_metadata'`` (oldest first) and idempotently
re-applies each to the SQL ``nexus_sessions`` table. Called once when
TwinManager creates / loads a twin so a freshly-mounted DB rebuilds the
exact title/archive state the user last saw.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Optional

from nexus_server.database import get_db_connection

logger = logging.getLogger(__name__)


SESSION_METADATA_EVENT = "session_metadata"


def _twin_event_log(twin):
    """Reach the EventLog through twin's structure. Tolerant of None
    so callers don't have to guard — best-effort sync is acceptable
    (the SQL row is the authoritative cache; this just makes it
    durable across server moves)."""
    if twin is None:
        return None
    return getattr(twin, "event_log", None)


def emit_session_metadata(
    twin,
    *,
    session_id: str,
    action: str,
    title: Optional[str] = None,
    archived: Optional[bool] = None,
) -> None:
    """Record a session metadata change in twin's EventLog.

    Best-effort: a failure to emit is logged + swallowed. The SQL
    update has already happened by this point, so the user-visible
    state is correct; we just lose durability across migrations
    until the next emit succeeds.
    """
    log = _twin_event_log(twin)
    if log is None:
        logger.debug("session_metadata not emitted: no event_log on twin")
        return
    metadata = {
        "session_id": session_id,
        "action":     action,
    }
    if title is not None:
        metadata["title"] = title
    if archived is not None:
        metadata["archived"] = bool(archived)

    # Content is a one-line human-readable summary so a raw EventLog
    # dump (used for debugging / audit trails) is readable without
    # parsing the metadata JSON.
    bits = [f"action={action}", f"session={session_id}"]
    if title is not None:
        bits.append(f"title={title!r}")
    if archived is not None:
        bits.append(f"archived={archived}")
    content = " ".join(bits)

    try:
        log.append(
            event_type=SESSION_METADATA_EVENT,
            content=content,
            session_id=session_id,
            metadata=metadata,
        )
    except Exception as e:  # noqa: BLE001
        logger.warning(
            "Failed to emit session_metadata event for %s/%s: %s",
            session_id, action, e,
        )


# ── Replay (oldest-first reconstruction) ─────────────────────────────


def replay_session_metadata(user_id: str, twin) -> int:
    """Walk twin's EventLog and rebuild the ``nexus_sessions`` SQL
    table from ``session_metadata`` events.

    Idempotent — running it twice in a row is a no-op. Safe to call on
    every twin construction; cheap because we filter by event_type at
    the SQL level (uses the events table's index on event_type).

    Returns the number of events applied.
    """
    log = _twin_event_log(twin)
    if log is None:
        return 0

    try:
        rows = log._conn.execute(  # noqa: SLF001 — direct SQL is the cheap path
            "SELECT timestamp, content, metadata, session_id "
            "FROM events WHERE event_type = ? ORDER BY idx ASC",
            (SESSION_METADATA_EVENT,),
        ).fetchall()
    except Exception as e:  # noqa: BLE001
        logger.warning("session_metadata replay query failed: %s", e)
        return 0

    if not rows:
        return 0

    applied = 0
    for row in rows:
        try:
            # Tuple-style or sqlite3.Row — both index by integer.
            ts = row[0]
            metadata_json = row[2] or "{}"
            md = json.loads(metadata_json)
        except Exception:  # noqa: BLE001
            continue

        session_id = md.get("session_id") or row[3]
        action = md.get("action", "")
        title = md.get("title")            # may be None
        archived = md.get("archived")      # may be None
        if not session_id:
            continue
        # Re-apply against SQL. For 'create' / 'rename' / 'archive',
        # we use INSERT OR UPDATE semantics; for 'delete' we drop the
        # row.
        try:
            _apply_one(user_id, session_id, action, title, archived, ts)
            applied += 1
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "session_metadata replay step failed for %s/%s: %s",
                session_id, action, e,
            )

    if applied:
        logger.info(
            "Replayed %d session_metadata events for user=%s",
            applied, user_id,
        )
    return applied


def _apply_one(
    user_id: str,
    session_id: str,
    action: str,
    title: Optional[str],
    archived: Optional[bool],
    ts: float,
) -> None:
    """Apply a single replayed event to the SQL table.

    UPSERT semantics:
      * create   → INSERT OR IGNORE; if row exists, leave it (later
                   rename/archive events refine it).
      * rename   → UPDATE title.
      * archive  → UPDATE archived=1.
      * unarchive→ UPDATE archived=0.
      * delete   → DELETE row.
    """
    iso_ts = datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
    with get_db_connection() as conn:
        if action == "delete":
            conn.execute(
                "DELETE FROM nexus_sessions "
                "WHERE id = ? AND user_id = ?",
                (session_id, user_id),
            )
            conn.commit()
            return

        if action == "create":
            conn.execute(
                """
                INSERT OR IGNORE INTO nexus_sessions
                (id, user_id, title, created_at, last_message_at,
                 message_count, archived)
                VALUES (?, ?, ?, ?, NULL, 0, 0)
                """,
                (session_id, user_id,
                 title or "New chat", iso_ts),
            )
            # If the row already existed (e.g. from an earlier run that
            # half-completed) and a title was provided here, surface it
            # — INSERT OR IGNORE wouldn't override an existing title
            # otherwise.
            if title is not None:
                conn.execute(
                    "UPDATE nexus_sessions SET title = ? "
                    "WHERE id = ? AND user_id = ?",
                    (title, session_id, user_id),
                )
            conn.commit()
            return

        # rename / archive / unarchive: build a partial UPDATE.
        sets: list[str] = []
        params: list = []
        if title is not None:
            sets.append("title = ?")
            params.append(title)
        if archived is not None:
            sets.append("archived = ?")
            params.append(1 if archived else 0)
        if not sets:
            return
        params.extend([session_id, user_id])
        conn.execute(
            f"UPDATE nexus_sessions SET {', '.join(sets)} "
            f"WHERE id = ? AND user_id = ?",
            params,
        )
        # If the UPDATE affected zero rows (because the SQL table is
        # fresh and the create event was somehow missing — should be
        # rare but defensive), insert a synthetic row so subsequent
        # events have something to act on.
        cur = conn.execute(
            "SELECT 1 FROM nexus_sessions WHERE id = ? AND user_id = ?",
            (session_id, user_id),
        ).fetchone()
        if cur is None:
            conn.execute(
                """
                INSERT INTO nexus_sessions
                (id, user_id, title, created_at, last_message_at,
                 message_count, archived)
                VALUES (?, ?, ?, ?, NULL, 0, ?)
                """,
                (session_id, user_id,
                 title or "New chat", iso_ts,
                 1 if archived else 0),
            )
        conn.commit()


__all__ = [
    "SESSION_METADATA_EVENT",
    "emit_session_metadata",
    "replay_session_metadata",
]
