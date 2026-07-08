"""Multi-session support — per-user chat thread management.

Background
==========
Pre-this-module the desktop's chat surface was effectively single-
session: every message a user sent landed in the same conversation
thread, and the only way to "start over" was to delete the JWT or wait
for compaction to bury old context. That's fine for a demo agent but
breaks down once a user has multiple unrelated projects in flight.

This module adds the session abstraction. A *session* is one logical
chat thread — title, created/last-touched timestamps, message count,
archived flag. The user maintains as many as they want in parallel;
the desktop sidebar lists them, and ``POST /api/v1/llm/chat`` routes
each turn to the right session via the ``session_id`` request field.

Storage layout
==============
Two tables, two ownership boundaries:

  1. ``nexus_sessions`` (this module's table, see database.py)
     Server-owned metadata: id, user_id, title, created_at,
     last_message_at, message_count, archived. Backs the sidebar list.

  2. Twin's per-user EventLog SQLite (SDK-owned)
     Source of truth for messages. Each row carries a ``session_id``
     column that twin populates from its current ``_thread_id``. The
     ``id`` field in (1) is intentionally identical in shape (e.g.
     ``session_3f9a2c1b``) so a join from a session to its messages is
     a literal string compare on ``events.session_id``.

Why split it this way
=====================
We want session metadata reads (sidebar list, count badges) to be fast
and not require opening twin's per-user SQLite. We also want session
metadata edits (rename, archive) to be a server-local concern that
doesn't touch twin's owned DB. Putting the metadata in our own SQLite
gives us both. Messages stay in twin's event_log because that's
already the source of truth for everything else (DPM, anchors, audit).

Sessions and sessions-without-id (legacy)
=========================================
For users who chatted before this module existed, twin's event_log
has rows with ``session_id = ''`` (empty). We model those as belonging
to the user's "default" session — a synthetic row this module returns
in ``list_sessions`` for any user with at least one event. The default
session has id ``''`` (empty string) so the join trivially matches the
old rows. Once the user creates a real named session, new turns are
tagged with that session's id; the default session keeps its old
content.

This avoids any data migration in twin's owned DB.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Optional

from pydantic import BaseModel, Field

from nexus_server.database import get_db_connection

logger = logging.getLogger(__name__)


# ── Constants ─────────────────────────────────────────────────────────


# id used for the synthetic "default" session that wraps pre-sessions
# messages (events with session_id=''). Empty string keeps the SQL join
# against twin event_log a no-op.
DEFAULT_SESSION_ID: str = ""

# Title shown in the sidebar for the default session before the user
# renames it. Not "Untitled" because we want to communicate that this
# is the wrap-around for pre-existing chat history.
DEFAULT_SESSION_TITLE: str = "Default chat"

# How many messages we wait for before auto-titling. Two = one user
# turn + one assistant reply; we want enough content to summarise.
AUTOTITLE_AFTER_MESSAGES: int = 2

# Soft cap on title length when auto-generated — keeps the sidebar tidy.
MAX_AUTOTITLE_CHARS: int = 48


# ── Pydantic models ───────────────────────────────────────────────────


class SessionInfo(BaseModel):
    """Wire shape for a session row.

    ``message_count`` is the row's stored counter; we keep it in sync
    via ``increment_message_count`` rather than recomputing from twin's
    event_log on every list call. The default session computes it
    on the fly from twin's event_log because we don't track it in our
    own table (no row).
    """

    id: str
    title: str
    created_at: str
    last_message_at: Optional[str] = None
    message_count: int = 0
    archived: bool = False
    is_default: bool = False


class CreateSessionRequest(BaseModel):
    """Body for ``POST /api/v1/sessions``."""

    title: Optional[str] = Field(
        default=None, max_length=120,
        description="Optional initial title; auto-generated later if left blank.",
    )


class UpdateSessionRequest(BaseModel):
    """Body for ``PATCH /api/v1/sessions/{id}``."""

    title: Optional[str] = Field(default=None, max_length=120)
    archived: Optional[bool] = None


class SessionListResponse(BaseModel):
    """Response shape for ``GET /api/v1/sessions``."""

    sessions: list[SessionInfo]


# ── Session id helper ─────────────────────────────────────────────────


def new_session_id() -> str:
    """Generate a fresh session id in twin's ``_thread_id`` format.

    Twin uses ``session_{uuid4_hex[:8]}`` for new threads (see
    nexus.twin._initialize). We mirror that exactly so a server-issued
    id and a twin-internally-generated id are indistinguishable on
    the wire — twin can adopt one as its ``_thread_id`` without any
    translation layer.
    """
    return f"session_{uuid.uuid4().hex[:8]}"


# ── CRUD ──────────────────────────────────────────────────────────────


def create_session(user_id: str, title: Optional[str] = None) -> SessionInfo:
    """Insert a new session row and return its info.

    Returns the freshly inserted SessionInfo. Title defaults to
    "New chat" — the first chat call's auto-title pass will replace it.
    """
    sid = new_session_id()
    now = datetime.now(timezone.utc).isoformat()
    final_title = (title or "").strip() or "New chat"
    with get_db_connection() as conn:
        conn.execute(
            """
            INSERT INTO nexus_sessions
            (id, user_id, title, created_at, last_message_at, message_count, archived)
            VALUES (?, ?, ?, ?, NULL, 0, 0)
            """,
            (sid, user_id, final_title, now),
        )
        conn.commit()
    logger.info("sessions.create user=%s id=%s title=%r", user_id, sid, final_title)
    return SessionInfo(
        id=sid,
        title=final_title,
        created_at=now,
        last_message_at=None,
        message_count=0,
        archived=False,
        is_default=False,
    )


def list_sessions(user_id: str, include_archived: bool = False) -> list[SessionInfo]:
    """List sessions for ``user_id``, newest activity first.

    Always prepends the synthetic default session if the user has any
    pre-sessions chat history (events with session_id=''). The default
    session keeps the old conversation accessible without forcing a
    migration on twin's owned DB.
    """
    rows = _select_sessions(user_id, include_archived=include_archived)
    out: list[SessionInfo] = [
        SessionInfo(
            id=r["id"],
            title=r["title"],
            created_at=r["created_at"],
            last_message_at=r["last_message_at"],
            message_count=int(r["message_count"] or 0),
            archived=bool(r["archived"]),
            is_default=False,
        )
        for r in rows
    ]

    default = _build_default_session_if_any(user_id)
    if default is not None:
        # Default session goes at the bottom by default — most users
        # will spend most of their time in named sessions, but we keep
        # the legacy thread accessible.
        out.append(default)
    return out


def get_session(user_id: str, session_id: str) -> Optional[SessionInfo]:
    """Fetch one session, or the synthetic default. Returns None if
    the id doesn't belong to this user."""
    if session_id == DEFAULT_SESSION_ID:
        return _build_default_session_if_any(user_id) or SessionInfo(
            id=DEFAULT_SESSION_ID,
            title=DEFAULT_SESSION_TITLE,
            created_at=datetime.now(timezone.utc).isoformat(),
            last_message_at=None,
            message_count=0,
            archived=False,
            is_default=True,
        )
    with get_db_connection() as conn:
        row = conn.execute(
            "SELECT * FROM nexus_sessions WHERE id = ? AND user_id = ?",
            (session_id, user_id),
        ).fetchone()
    if row is None:
        return None
    return SessionInfo(
        id=row["id"],
        title=row["title"],
        created_at=row["created_at"],
        last_message_at=row["last_message_at"],
        message_count=int(row["message_count"] or 0),
        archived=bool(row["archived"]),
        is_default=False,
    )


def update_session(
    user_id: str,
    session_id: str,
    title: Optional[str] = None,
    archived: Optional[bool] = None,
) -> Optional[SessionInfo]:
    """Rename and/or archive a session. ``None`` means "don't touch
    that field". Returns the updated row or ``None`` if not found."""
    if session_id == DEFAULT_SESSION_ID:
        # Default session is synthetic — not editable. Renaming it
        # would imply persisting a row that maps id='' which we've
        # specifically chosen not to do.
        return None
    sets: list[str] = []
    params: list = []
    if title is not None:
        cleaned = title.strip()
        if not cleaned:
            cleaned = "Untitled"
        if len(cleaned) > 120:
            cleaned = cleaned[:120]
        sets.append("title = ?")
        params.append(cleaned)
    if archived is not None:
        sets.append("archived = ?")
        params.append(1 if archived else 0)
    if not sets:
        return get_session(user_id, session_id)
    params.extend([session_id, user_id])
    with get_db_connection() as conn:
        cur = conn.execute(
            f"UPDATE nexus_sessions SET {', '.join(sets)} "
            f"WHERE id = ? AND user_id = ?",
            params,
        )
        conn.commit()
        if cur.rowcount == 0:
            return None
    return get_session(user_id, session_id)


def archive_session(user_id: str, session_id: str) -> bool:
    """Mark archived=1. Soft delete — twin's event_log keeps every
    message; un-archive (PATCH archived=False) is reversible.

    Returns True if a row was changed.
    """
    if session_id == DEFAULT_SESSION_ID:
        return False
    with get_db_connection() as conn:
        cur = conn.execute(
            "UPDATE nexus_sessions SET archived = 1 "
            "WHERE id = ? AND user_id = ?",
            (session_id, user_id),
        )
        conn.commit()
        return cur.rowcount > 0


def delete_session_row(user_id: str, session_id: str) -> bool:
    """Hard-delete the metadata row from ``nexus_sessions``.

    Counterpart to :func:`archive_session`'s soft delete. The actual
    message rows live in twin's per-user EventLog; the server-side
    twin.delete_session call wipes them and (best-effort) any stored
    objects. This function only owns the metadata row — call it AFTER
    twin.delete_session so the audit trail (the ``session_deleted``
    event we wrote into event_log) stays linkable to the session id
    until just before we drop the title row.

    Returns True iff a metadata row was removed.
    """
    if session_id == DEFAULT_SESSION_ID:
        return False
    with get_db_connection() as conn:
        cur = conn.execute(
            "DELETE FROM nexus_sessions WHERE id = ? AND user_id = ?",
            (session_id, user_id),
        )
        conn.commit()
        return cur.rowcount > 0


# ── Bookkeeping ───────────────────────────────────────────────────────


def touch_session(
    user_id: str,
    session_id: str,
    delta_message_count: int = 0,
) -> None:
    """Update ``last_message_at`` (now) and bump message_count.

    Called from the chat handler after each successful turn. For the
    default session this is a no-op (no row to update — the sidebar
    computes its counts from twin event_log on demand).
    """
    if session_id == DEFAULT_SESSION_ID:
        return
    now = datetime.now(timezone.utc).isoformat()
    with get_db_connection() as conn:
        conn.execute(
            """
            UPDATE nexus_sessions
            SET last_message_at = ?,
                message_count = message_count + ?
            WHERE id = ? AND user_id = ?
            """,
            (now, int(delta_message_count), session_id, user_id),
        )
        conn.commit()


def ensure_session_exists(user_id: str, session_id: str) -> bool:
    """Verify ownership before letting a chat call proceed.

    Returns True if the session exists for the user OR is the default
    session. Used by the chat handler to reject forged/foreign ids.
    """
    if session_id == DEFAULT_SESSION_ID:
        return True
    with get_db_connection() as conn:
        row = conn.execute(
            "SELECT 1 FROM nexus_sessions WHERE id = ? AND user_id = ?",
            (session_id, user_id),
        ).fetchone()
    return row is not None


# ── Auto-title ────────────────────────────────────────────────────────


def maybe_apply_autotitle(
    user_id: str,
    session_id: str,
    first_user_message: str,
) -> None:
    """Generate a short title from the first user message if the
    session is still on its placeholder ("New chat").

    Heuristic-only — we don't burn an LLM call for this. The user's
    first message is usually descriptive ("help me debug X"), so we
    take its first sentence-or-line, trim to MAX_AUTOTITLE_CHARS, and
    use that. If the heuristic gives us nothing useful, we leave the
    placeholder and hope a later turn does better.
    """
    if session_id == DEFAULT_SESSION_ID:
        return
    cleaned = (first_user_message or "").strip()
    if not cleaned:
        return
    # First line, then first sentence boundary
    first_line = cleaned.splitlines()[0]
    for sep in ("?", "。", ". ", "!", "\n"):
        idx = first_line.find(sep)
        if 4 < idx <= MAX_AUTOTITLE_CHARS:
            first_line = first_line[: idx + (1 if sep in ("?", "。", "!") else 0)]
            break
    if len(first_line) > MAX_AUTOTITLE_CHARS:
        first_line = first_line[: MAX_AUTOTITLE_CHARS - 1].rstrip() + "…"
    title = first_line.strip()
    if not title:
        return
    with get_db_connection() as conn:
        conn.execute(
            """
            UPDATE nexus_sessions
            SET title = ?
            WHERE id = ? AND user_id = ? AND title = 'New chat'
            """,
            (title, session_id, user_id),
        )
        conn.commit()


# ── Internals ─────────────────────────────────────────────────────────


def _select_sessions(user_id: str, include_archived: bool) -> list:
    where = "WHERE user_id = ?"
    params: list = [user_id]
    if not include_archived:
        where += " AND archived = 0"
    with get_db_connection() as conn:
        cur = conn.execute(
            f"""
            SELECT id, title, created_at, last_message_at,
                   message_count, archived
            FROM nexus_sessions
            {where}
            ORDER BY COALESCE(last_message_at, created_at) DESC
            """,
            params,
        )
        return [dict(r) for r in cur.fetchall()]


def _build_default_session_if_any(user_id: str) -> Optional[SessionInfo]:
    """Return a synthetic SessionInfo for pre-sessions chat history,
    or ``None`` if the user has no events with empty session_id.

    Counting + last-activity timestamp come from twin's event_log
    SQLite (read-only). We intentionally don't cache this — the read
    is cheap (single SELECT with index) and inventing a cache would
    just create staleness bugs at session boundaries.
    """
    from nexus_server import twin_event_log

    conn = twin_event_log._open_readonly(user_id)
    if conn is None:
        return None
    try:
        # Count + max timestamp for events with session_id = '' that
        # are user/assistant messages (the same set the chat history
        # endpoint counts).
        row = conn.execute(
            """
            SELECT COUNT(*) AS n, MAX(timestamp) AS ts
            FROM events
            WHERE event_type IN ('user_message', 'assistant_response')
              AND COALESCE(session_id, '') = ''
            """
        ).fetchone()
    except Exception as e:
        logger.debug("default session probe failed for %s: %s", user_id, e)
        return None
    finally:
        conn.close()

    n = int(row[0] or 0) if row else 0
    if n == 0:
        return None
    last_ts = row[1] if row else None
    last_iso = (
        datetime.fromtimestamp(float(last_ts), tz=timezone.utc).isoformat()
        if last_ts is not None else None
    )
    return SessionInfo(
        id=DEFAULT_SESSION_ID,
        title=DEFAULT_SESSION_TITLE,
        created_at=last_iso or datetime.now(timezone.utc).isoformat(),
        last_message_at=last_iso,
        message_count=n,
        archived=False,
        is_default=True,
    )
