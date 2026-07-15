"""Read-only views over each user's per-twin EventLog SQLite.

Background — S5 of the post-Phase-D server cleanup
==================================================
Up to S4 the server kept its own table (``sync_events``) that mirrored
the desktop's local event log. Once chat moved through Nexus's
DigitalTwin (S1), twin's own SDK ``EventLog`` became the single
authoritative event store — server-side ``sync_events`` was reduced to
a passive mirror written by ``twin_manager._build_on_event``. S5
finishes the job: the agent_state HTTP endpoints stop reading the
mirror and read directly from each user's twin EventLog SQLite file.

Why this isn't reading via ``DigitalTwin.create``
-------------------------------------------------
Instantiating a twin is heavy (LLM client init, ChainBackend bring-up,
session restore). The ``/agent/state`` snapshot — and
the polled ``/agent/timeline`` / ``/agent/memories`` requests behind
the desktop sidebar — must be fast and shouldn't trigger any of that.
SDK's ``EventLog`` is plain SQLite under the hood (one ``events`` table,
WAL mode, well-defined schema), so we open the per-user DB read-only
with stdlib ``sqlite3`` and run direct queries. No twin start-up cost,
no risk of mutating state mid-read.

File layout
-----------
``twin_manager`` builds each twin with ::

    base_dir = TWIN_BASE_DIR / user_id
    agent_id = f"user-{user_id[:8]}"

SDK's EventLog stores its DB at
``{base_dir}/event_log/{agent_id}.db``, so for user ``abc1234…`` we end
up at ``~/.nexus_server/twins/abc1234…/event_log/user-abc12345.db``.

EventLog schema (single ``events`` table)
-----------------------------------------
``idx`` INTEGER PRIMARY KEY AUTOINCREMENT — used here as the ``sync_id``
the desktop expects on the wire (within-user monotonic).

``timestamp`` REAL — unix seconds, converted to ISO-8601 on output for
parity with the legacy ``sync_events.server_received_at`` shape.

``event_type`` TEXT, ``content`` TEXT, ``metadata`` TEXT (JSON),
``agent_id`` TEXT, ``session_id`` TEXT.

Falsey behaviour
----------------
A user who has never chatted has no ``events.db`` on disk. Every helper
in this module treats "file missing" as "no events" and returns the
empty answer for that read shape (empty list / zero count). That's the
correct behaviour: such a user genuinely has nothing to show, and the
sidebar already renders the empty state for it.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


# ── Path resolution ───────────────────────────────────────────────────


def _twin_base_dir() -> Path:
    """Where TwinManager places per-user twin data dirs.

    Read from NEXUS_TWIN_BASE_DIR if set (matches twin_manager's env
    contract), else fall back to ``~/.nexus_server/twins`` — same default
    as ``twin_manager.TWIN_BASE_DIR``.
    """
    return Path(
        os.environ.get(
            "NEXUS_TWIN_BASE_DIR",
            os.path.expanduser("~/.nexus_server/twins"),
        )
    )


def _agent_id_for(user_id: str) -> str:
    """Mirror twin_manager._create_twin's agent_id derivation so the
    db file path lines up. Keep these in lockstep — if you change one,
    change the other (or hoist into a shared constant)."""
    return f"user-{user_id[:8]}"


def _db_path(user_id: str) -> Path:
    return (
        _twin_base_dir() / user_id / "event_log" / f"{_agent_id_for(user_id)}.db"
    )


def _open_readonly(user_id: str) -> Optional[sqlite3.Connection]:
    """Open a user's EventLog SQLite read-only. ``None`` on miss.

    URI mode + ``mode=ro`` so a stray write would error rather than
    silently mutate the agent's source of truth.
    """
    p = _db_path(user_id)
    if not p.exists():
        return None
    try:
        # Path → URI: needs forward slashes and proper escaping. ``Path.as_uri``
        # produces ``file:///abs/path``; SQLite expects just the path part
        # plus ``?mode=ro``.
        uri = f"file:{p}?mode=ro"
        return sqlite3.connect(uri, uri=True, timeout=2.0)
    except sqlite3.Error as e:
        logger.warning("twin_event_log: open failed for %s: %s", user_id, e)
        return None


def snippet_around(
    text: str, query: str, half: int = 100,
) -> str:
    """Return a ~``2*half``-char window of ``text`` centred on the first
    case-insensitive occurrence of ``query``. Used by every keyword-
    search surface (chat search, file content search) so the LLM
    sees a consistent excerpt shape.

    Edge cases:
      - query empty → first 2*half chars of text.
      - query not found → first 2*half chars of text (caller should
        normally avoid passing non-matching text).
      - text shorter than window → returned as-is, no ellipsis.
    """
    text = text or ""
    if not query:
        return text[: 2 * half]
    pos = text.lower().find(query.lower())
    if pos < 0:
        return text[: 2 * half]
    start = max(0, pos - half)
    end = min(len(text), pos + len(query) + half)
    snippet = text[start:end]
    if start > 0:
        snippet = "…" + snippet
    if end < len(text):
        snippet = snippet + "…"
    return snippet


def _ts_to_iso(ts: float | int | None) -> str:
    if ts is None:
        return ""
    try:
        return (
            datetime.fromtimestamp(float(ts), tz=timezone.utc).isoformat()
        )
    except Exception:
        return ""


def _safe_json(s: str | None) -> dict:
    if not s:
        return {}
    try:
        v = json.loads(s)
        return v if isinstance(v, dict) else {}
    except Exception:
        return {}


# ── Counts ────────────────────────────────────────────────────────────


def count_by_type(user_id: str, event_types: list[str]) -> int:
    """Number of events whose ``event_type`` is in the given list."""
    if not event_types:
        return 0
    conn = _open_readonly(user_id)
    if conn is None:
        return 0
    try:
        placeholders = ",".join("?" * len(event_types))
        row = conn.execute(
            f"SELECT COUNT(*) FROM events WHERE event_type IN ({placeholders})",
            event_types,
        ).fetchone()
        return int(row[0]) if row else 0
    except sqlite3.Error as e:
        logger.warning("count_by_type failed for %s: %s", user_id, e)
        return 0
    finally:
        conn.close()


def memory_compact_count(user_id: str) -> int:
    """Compatibility helper: how many ``memory_compact`` events the
    user's twin has produced. Replaces the old
    ``memory_service.memory_compact_count`` (which read sync_events)."""
    return count_by_type(user_id, ["memory_compact"])


# ── Memories ──────────────────────────────────────────────────────────


def list_memory_compacts(user_id: str, limit: int = 50) -> list[dict]:
    """Return memory_compact events newest-first, shaped for the desktop's
    MemoryEntry model.

    Matches the contract that ``agent_state.MemoryEntry`` expects so the
    pivot to twin's event_log is invisible to the desktop client.
    """
    conn = _open_readonly(user_id)
    if conn is None:
        return []
    try:
        rows = conn.execute(
            """
            SELECT idx, content, metadata, timestamp
            FROM events
            WHERE event_type = 'memory_compact'
            ORDER BY idx DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    except sqlite3.Error as e:
        logger.warning("list_memory_compacts failed for %s: %s", user_id, e)
        rows = []
    finally:
        conn.close()

    out: list[dict] = []
    for idx, content, meta_json, ts in rows:
        meta = _safe_json(meta_json)
        projected = meta.get("projected_from") or [None, None]
        if not isinstance(projected, list):
            projected = [None, None]
        out.append({
            "sync_id": int(idx),
            "content": content or "",
            "first_sync_id": projected[0] if len(projected) >= 1 else None,
            "last_sync_id": projected[1] if len(projected) >= 2 else None,
            "event_count": int(meta.get("event_count", 0) or 0),
            "char_count": int(
                meta.get("char_count", len(content or "")) or 0
            ),
            "created_at": _ts_to_iso(ts),
        })
    return out


# ── Chat history ──────────────────────────────────────────────────────


# Known LLM-context block markers that pre-fix llm_gateway code
# accidentally persisted into the user_message column (#99). When the
# client fetches history, we strip these prefixes so old chats don't
# render the entire scaffolding as the user's bubble. Cheap no-op on
# uncontaminated rows.
_CONTEXT_BLOCK_PREFIXES = (
    "[CONTEXT — ",
    "[WORKFLOW RECIPES — ",
    "[CONTEXT — INFLIGHT WORKFLOWS]",
    "[CONTEXT — FILES YOU'VE PROCESSED BEFORE]",
)


def _strip_leaked_context_blocks(content: str) -> str:
    """Strip persisted LLM-context blocks from a user_message content.

    History: through #97 the llm_gateway concatenated context blocks
    (workflow recipes, uploaded-files reminder) onto ``effective_bare``
    before passing it to ``twin.chat``. ``effective_bare`` is the
    string that gets persisted as the user_message event, so old chat
    history shows the entire scaffolding as a giant blue user bubble.
    Post #97 fix the blocks only ride in ``effective_folded`` (LLM-
    only) so new turns are clean — but old DB rows still have the
    gunk.

    Strategy: if content starts with a known block marker, find the
    LAST ``\\n\\n`` in the string and treat everything after as the
    real user message. Works for the 99% case (user typed one or two
    lines). Edge case (user wrote a multi-paragraph message AND it
    was contaminated) → user loses earlier paragraphs but at least
    doesn't see the scaffolding. Best we can do without per-block
    parsing, and the alternative (rendering the full gunk) is worse.
    """
    if not content:
        return content
    if not any(content.startswith(p) for p in _CONTEXT_BLOCK_PREFIXES):
        return content  # uncontaminated
    last_para = content.rfind("\n\n")
    if last_para < 0:
        return content  # malformed; can't recover
    candidate = content[last_para + 2:].strip()
    if not candidate:
        return content
    # Don't return something that itself looks like a block marker
    # (would happen if the user_message was effectively empty and only
    # context survived to the end).
    if any(candidate.startswith(p) for p in _CONTEXT_BLOCK_PREFIXES):
        return content
    return candidate


def list_messages(
    user_id: str,
    limit: int,
    before_idx: Optional[int] = None,
    session_id: Optional[str] = None,
) -> tuple[list[dict], int]:
    """Recent chat turns for the desktop's history pane.

    Returns ``(messages_oldest_first, total_count)``. ``before_idx`` is
    a pagination cursor (mirrors the legacy ``before_sync_id`` query
    param on /agent/messages). Each message is shaped like
    ``ChatMessageView`` so the existing endpoint Pydantic model
    serialises unchanged.

    ``session_id`` filter:
      * ``None``  — return all messages (legacy behaviour, used by tools
        that don't care about thread boundaries).
      * ``""``    — return only messages with empty session_id (the
        synthetic "default" session for pre-multi-session chat history).
      * any other — strict equality match against session_id stored in
        the event payload.
    Total count respects the same filter so the sidebar's count badges
    are coherent with what the user sees.

    Phase 2a unification (docs/design/EVENT_LOG_UNIFICATION.md):
    the canonical source of truth is the SHARED ``twin_event_log`` table
    in nexus_server.db, written by every chat_router_v2 turn via
    ``Store.emit_and_apply``. The per-user file at
    ``~/.nexus_server/twins/{user_id}/.../events.db`` is now a
    deprecated mirror — we read it as a fallback for older rows that
    predate the migration, but the primary read is the shared table.
    """
    # ── Primary path: shared twin_event_log (canonical) ─────────────
    try:
        shared_msgs, shared_total = _list_messages_shared(
            user_id=user_id,
            limit=limit,
            before_idx=before_idx,
            session_id=session_id,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "list_messages: shared-table query failed for %s: %s "
            "— falling back to per-user file",
            user_id, exc,
        )
        shared_msgs, shared_total = [], 0

    # ── Fallback path: per-user file (Phase 2a tolerates old rows) ──
    # Some legacy chat threads + the workflow_run cards (until Phase 2b
    # migrates them too) still live only in the per-user file. We merge
    # the two streams oldest-first so the UI sees one coherent
    # timeline. De-dupe by (sync_id, role, content) so the dual-write
    # E14 band-aid doesn't produce visible doubles.
    legacy_msgs, legacy_total = _list_messages_per_user_file(
        user_id=user_id,
        limit=limit,
        before_idx=before_idx,
        session_id=session_id,
    )
    if not legacy_msgs:
        return shared_msgs, shared_total

    if not shared_msgs:
        return legacy_msgs, legacy_total

    # Both populated → union + dedupe + cap to ``limit`` newest.
    seen: set[tuple] = set()
    merged: list[dict] = []
    # Build a dedupe key tolerant of either-side-only rows:
    #   primary key = (sync_id) since both tables use shared idx now
    #   (Phase 2a writes go to both, idx values are SHARED-table idxs);
    # for legacy-only rows the sync_id is a per-user idx which won't
    # collide with shared idxs in practice (different counters). Add a
    # secondary fingerprint of (role, content[:40], timestamp[:19]) to
    # catch dual-writes that did populate both with the same idx.
    def _key(m: dict) -> tuple:
        return (
            m.get("sync_id"),
            m.get("role"),
            (m.get("content") or "")[:40],
            (m.get("timestamp") or "")[:19],
        )
    for m in shared_msgs + legacy_msgs:
        k = _key(m)
        if k in seen:
            continue
        seen.add(k)
        merged.append(m)
    # Sort oldest-first by timestamp string (ISO-8601 sorts correctly).
    merged.sort(key=lambda m: m.get("timestamp") or "")
    if len(merged) > limit:
        merged = merged[-limit:]
    return merged, max(shared_total, legacy_total)


def _list_messages_shared(
    *,
    user_id: str,
    limit: int,
    before_idx: Optional[int],
    session_id: Optional[str],
) -> tuple[list[dict], int]:
    """Phase 2a — read chat history from the shared ``twin_event_log``.

    session_id lives inside ``payload_json`` (string field) on the shared
    table — extracted with SQLite's ``json_extract``. The shared schema
    indexes on ``(user_id, ts)`` so the WHERE clause is cheap; the
    session-id filter adds a `json_extract` predicate that SQLite
    evaluates row-by-row (acceptable for chat-volume tables).
    """
    from nexus_server.database import get_db_connection
    # Chat-substrate event kinds the desktop renders. workflow_run is
    # included so future Phase 2b migrations show up the moment they
    # land; until then the legacy path still handles existing
    # workflow_run cards.
    chat_kinds = ("user_message", "assistant_response", "workflow_run")
    placeholders = ",".join("?" * len(chat_kinds))

    where = f"user_id = ? AND event_kind IN ({placeholders})"
    params: list = [user_id, *chat_kinds]
    if before_idx is not None:
        where += " AND event_idx < ?"
        params.append(int(before_idx))
    if session_id is not None:
        # COALESCE catches both NULL session_id (legacy) and missing
        # JSON key. Equal-empty-string semantics match the per-user
        # filter so default-session callers see the same rows.
        where += (
            " AND COALESCE(json_extract(payload_json, '$.session_id'), '') = ?"
        )
        params.append(session_id)

    with get_db_connection() as conn:
        rows = conn.execute(
            f"""
            SELECT event_idx, event_kind, payload_json, ts
            FROM twin_event_log
            WHERE {where}
            ORDER BY event_idx DESC
            LIMIT ?
            """,
            (*params, int(limit)),
        ).fetchall()
        # Total count: same filter, no pagination.
        total_where = f"user_id = ? AND event_kind IN ({placeholders})"
        total_params: list = [user_id, *chat_kinds]
        if session_id is not None:
            total_where += (
                " AND COALESCE(json_extract(payload_json, '$.session_id'), '') = ?"
            )
            total_params.append(session_id)
        total = int(conn.execute(
            f"SELECT COUNT(*) FROM twin_event_log WHERE {total_where}",
            total_params,
        ).fetchone()[0])

    # Newest-first → oldest-first for the renderer.
    rows = list(reversed(rows))
    msgs: list[dict] = []
    for r in rows:
        event_idx, event_kind, payload_json, ts_us = r
        payload = _safe_json(payload_json) if payload_json else {}
        text = payload.get("text", "") or ""
        attachments = payload.get("attachments") or []
        if not isinstance(attachments, list):
            attachments = []
        # Strip injected context-blocks from user_message — same defence
        # the per-user reader runs (see _strip_leaked_context_blocks).
        if event_kind == "user_message":
            text = _strip_leaked_context_blocks(text)
        if event_kind == "user_message":
            role, kind = "user", "text"
        elif event_kind == "workflow_run":
            role, kind = "assistant", "workflow_run"
        else:
            role, kind = "assistant", "text"
        # Microseconds (int) → ISO-8601 for the wire.
        try:
            iso = _ts_to_iso(int(ts_us) / 1_000_000.0)
        except Exception:  # noqa: BLE001
            iso = _ts_to_iso(0.0)
        # Repackage metadata field for the desktop. The per-user reader
        # had a `metadata` JSON column; here we synthesise it from
        # payload entries that aren't `text`/`session_id`/`attachments`.
        meta = {
            k: v for (k, v) in payload.items()
            if k not in ("text", "session_id", "attachments")
        }
        msgs.append({
            "role":         role,
            "content":      text,
            "timestamp":    iso,
            "sync_id":      int(event_idx),
            "attachments":  attachments,
            "message_kind": kind,
            "metadata":     meta,
        })
    return msgs, total


def _list_messages_per_user_file(
    *,
    user_id: str,
    limit: int,
    before_idx: Optional[int],
    session_id: Optional[str],
) -> tuple[list[dict], int]:
    """Legacy reader — per-user SQLite file. Kept for fallback during
    Phase 2a so any pre-migration rows (and unmigrated workflow_run
    cards) still appear in the timeline."""
    conn = _open_readonly(user_id)
    if conn is None:
        return [], 0
    try:
        # Filter set: traditional chat turns + the new workflow_run
        # inline cards. workflow_run rows carry a workflow_run_id in
        # their metadata; the client renders them as live-polling
        # cards instead of plain text bubbles.
        _CHAT_EVENT_TYPES = (
            "user_message", "assistant_response", "assistant_message",
            "workflow_run",
        )
        types_sql = "(" + ",".join("?" * len(_CHAT_EVENT_TYPES)) + ")"
        where = f"event_type IN {types_sql}"
        params: list = list(_CHAT_EVENT_TYPES)
        if before_idx is not None:
            where += " AND idx < ?"
            params.append(int(before_idx))
        if session_id is not None:
            # Use COALESCE so rows from before the session_id column was
            # populated (NULL) compare equal to '' — both represent the
            # synthetic default session.
            where += " AND COALESCE(session_id, '') = ?"
            params.append(session_id)
        params.append(limit)
        rows = conn.execute(
            f"""
            SELECT idx, event_type, content, timestamp, metadata
            FROM events
            WHERE {where}
            ORDER BY idx DESC
            LIMIT ?
            """,
            params,
        ).fetchall()
        # Total count: same filter minus pagination cursor + limit.
        count_where = f"event_type IN {types_sql}"
        count_params: list = list(_CHAT_EVENT_TYPES)
        if session_id is not None:
            count_where += " AND COALESCE(session_id, '') = ?"
            count_params.append(session_id)
        total = int(conn.execute(
            f"SELECT COUNT(*) FROM events WHERE {count_where}",
            count_params,
        ).fetchone()[0])
    except sqlite3.Error as e:
        logger.warning("list_messages failed for %s: %s", user_id, e)
        return [], 0
    finally:
        conn.close()

    # DESC fetch above so the LIMIT picks the *newest* N; flip back to
    # oldest-at-top for the desktop renderer.
    rows = list(reversed(rows))
    msgs = []
    for r in rows:
        meta = _safe_json(r[4]) if len(r) > 4 else {}
        # Attachments (Phase Q): user_message events store the
        # structured attachment list under metadata.attachments;
        # surface it on the wire so the desktop can render real chips.
        attachments = meta.get("attachments") if isinstance(meta, dict) else None
        if not isinstance(attachments, list):
            attachments = []
        # Map event_type → (role, message_kind). user_message stays
        # user-text; assistant_response / assistant_message both render
        # as assistant text. workflow_run gets a special kind so the
        # client picks the live-card renderer instead of a text bubble.
        et = r[1]
        if et == "user_message":
            role, kind = "user", "text"
        elif et == "workflow_run":
            role, kind = "assistant", "workflow_run"
        else:  # assistant_response / assistant_message / future
            role, kind = "assistant", "text"
        content = r[2] or ""
        # Read-time strip for pre-fix contaminated user_message rows
        # (see _strip_leaked_context_blocks docstring). Cheap no-op on
        # clean rows; aggressive cleanup on poisoned ones. Doesn't
        # touch the DB — pure render-time filter so existing chat
        # history doesn't have to be migrated.
        if et == "user_message":
            content = _strip_leaked_context_blocks(content)
        msgs.append({
            "role": role,
            "content": content,
            "timestamp": _ts_to_iso(r[3]),
            "sync_id": int(r[0]),
            "attachments": attachments,
            "message_kind": kind,
            "metadata": meta if isinstance(meta, dict) else {},
        })
    return msgs, total


# ── Side-effect events from a single chat turn (Phase B fix) ──────────


# Event types that the chat surface renders inline (between the user
# bubble and the assistant bubble) when they appear mid-turn. Today
# only ``workflow_run`` qualifies, but the registry is extensible.
_SIDE_EFFECT_EVENT_TYPES = ("workflow_run",)


def latest_event_idx(user_id: str) -> int:
    """Return the highest event idx currently in the user's log. Used
    by the chat gateway to snapshot a "before" marker so it can detect
    new events the agent's tools insert during the upcoming turn.

    Returns 0 if the log doesn't exist yet (fresh user) — caller should
    treat 0 as "everything counts as new"."""
    conn = _open_readonly(user_id)
    if conn is None:
        return 0
    try:
        row = conn.execute("SELECT COALESCE(MAX(idx), 0) FROM events").fetchone()
        return int(row[0]) if row else 0
    except sqlite3.Error as e:
        logger.warning("latest_event_idx failed for %s: %s", user_id, e)
        return 0
    finally:
        conn.close()


def find_recent_workflow_run_ids(
    user_id: str,
    session_id: str,
    max_age_seconds: int = 30 * 60,
    limit: int = 5,
) -> list[dict]:
    """Return recent workflow_run events from this session, newest first.

    Used by the chat gateway to figure out which workflow runs the
    user might still want to talk about while they're in flight. The
    runs returned here are CANDIDATES — the caller is expected to
    query ``workflows.get_run()`` on each to filter down to runs
    actually still in progress.

    Each row: {sync_id, run_id, workflow_name, total_steps, started_at}.
    """
    conn = _open_readonly(user_id)
    if conn is None:
        return []
    try:
        cutoff = (
            datetime.now(timezone.utc).timestamp() - max_age_seconds
        )
        rows = conn.execute(
            """
            SELECT idx, content, timestamp, metadata
            FROM events
            WHERE event_type = 'workflow_run'
              AND COALESCE(session_id, '') = ?
              AND timestamp >= ?
            ORDER BY idx DESC
            LIMIT ?
            """,
            (session_id or "", cutoff, int(limit)),
        ).fetchall()
    except sqlite3.Error as e:
        logger.warning(
            "find_recent_workflow_run_ids failed for %s: %s", user_id, e,
        )
        return []
    finally:
        conn.close()

    out: list[dict] = []
    for r in rows:
        meta = _safe_json(r[3])
        rid = meta.get("workflow_run_id") if isinstance(meta, dict) else None
        if not rid:
            continue
        out.append({
            "sync_id":       int(r[0]),
            "content":       r[1] or "",
            "started_at":    _ts_to_iso(r[2]),
            "run_id":        rid,
            "workflow_name": meta.get("workflow_name", "") if isinstance(meta, dict) else "",
            "total_steps":   meta.get("total_steps", 0) if isinstance(meta, dict) else 0,
        })
    return out


def list_side_effect_events_since(
    user_id: str,
    session_id: str,
    since_idx: int,
) -> list[dict]:
    """Return any inline-renderable side-effect events (workflow_run
    today) that landed in the user's event log AFTER ``since_idx``,
    scoped to the given chat session. Caller is the chat gateway which
    needs to ship these to the desktop so the inline workflow card
    shows up immediately, not on next session re-open."""
    conn = _open_readonly(user_id)
    if conn is None:
        return []
    try:
        types_sql = "(" + ",".join("?" * len(_SIDE_EFFECT_EVENT_TYPES)) + ")"
        rows = conn.execute(
            f"""
            SELECT idx, event_type, content, timestamp, metadata,
                   COALESCE(session_id, '') AS sid
            FROM events
            WHERE event_type IN {types_sql}
              AND idx > ?
              AND COALESCE(session_id, '') = ?
            ORDER BY idx ASC
            """,
            (*_SIDE_EFFECT_EVENT_TYPES, int(since_idx), session_id or ""),
        ).fetchall()
    except sqlite3.Error as e:
        logger.warning(
            "list_side_effect_events_since failed for %s: %s", user_id, e,
        )
        return []
    finally:
        conn.close()

    out = []
    for r in rows:
        out.append({
            "sync_id": int(r[0]),
            "event_type": r[1],
            "content": r[2] or "",
            "timestamp": _ts_to_iso(r[3]),
            "metadata": _safe_json(r[4]),
        })
    return out


# ── Cross-session search (Phase C-1) ──────────────────────────────────


_SEARCHABLE_EVENT_TYPES = (
    "user_message", "assistant_response", "assistant_message",
)


def search_messages(
    user_id: str,
    query: str,
    limit: int = 5,
    exclude_session_id: Optional[str] = None,
) -> list[dict]:
    """Substring search over chat messages in the user's event_log.

    Returns at most ``limit`` matches, newest-first, each shaped as::

        {
          "sync_id":     int,    # events.idx
          "session_id":  str,    # "" means default/legacy session
          "role":        "user" | "assistant",
          "snippet":     str,    # ~240-char window centred on the match
          "timestamp":   ISO-8601 str,
        }

    Implementation note: SQLite LIKE %q% — not FTS — because the event
    log is small (single-digit MB even for power users), the deployment
    is single-tenant per twin DB (no shared index to maintain), and a
    LIKE pass scans <50k rows in well under 100ms on commodity disks.
    If a user's log ever crosses 100k chat events we can add an FTS5
    virtual-table mirror, but that's a future optimisation.

    ``exclude_session_id`` lets the caller hide the current session so
    "search past chats" doesn't surface the user's just-typed sentence
    as a hit on itself. Pass ``None`` to disable.
    """
    q = (query or "").strip()
    if not q:
        return []

    conn = _open_readonly(user_id)
    if conn is None:
        return []
    try:
        types_sql = "(" + ",".join("?" * len(_SEARCHABLE_EVENT_TYPES)) + ")"
        where = (
            f"event_type IN {types_sql} "
            f"AND content IS NOT NULL "
            # LIKE is case-insensitive on ASCII when both sides are
            # uppercased — cheap CI without ICU.
            f"AND UPPER(content) LIKE UPPER(?)"
        )
        params: list = list(_SEARCHABLE_EVENT_TYPES) + [f"%{q}%"]
        if exclude_session_id is not None:
            where += " AND COALESCE(session_id, '') != ?"
            params.append(exclude_session_id)
        params.append(int(limit))
        rows = conn.execute(
            f"""
            SELECT idx, event_type, content, timestamp,
                   COALESCE(session_id, '') AS sid
            FROM events
            WHERE {where}
            ORDER BY idx DESC
            LIMIT ?
            """,
            params,
        ).fetchall()
    except sqlite3.Error as e:
        logger.warning("search_messages failed for %s: %s", user_id, e)
        return []
    finally:
        conn.close()

    hits: list[dict] = []
    for r in rows:
        idx, et, content, ts, sid = r
        role = "user" if et == "user_message" else "assistant"
        # Same strip as list_messages — don't surface leaked context
        # blocks in search results either.
        snippet_source = content or ""
        if et == "user_message":
            snippet_source = _strip_leaked_context_blocks(snippet_source)
        hits.append({
            "sync_id":    int(idx),
            "session_id": sid,
            "role":       role,
            "snippet":    snippet_around(snippet_source, q),
            "timestamp":  _ts_to_iso(ts),
        })
    return hits


# ── Timeline (raw, server merges with anchors) ────────────────────────


def list_timeline_events(user_id: str, limit: int) -> list[dict]:
    """Return raw event rows for the timeline endpoint to render.

    The server merges these with sync_anchors and converts them to
    ``TimelineItem`` shape. Keeping the merge there (rather than baking
    anchors into this helper) preserves layer separation: this module
    is a thin reader over twin's event_log; anchor lifecycle is a
    legacy concern owned by sync_anchor.
    """
    conn = _open_readonly(user_id)
    if conn is None:
        return []
    try:
        rows = conn.execute(
            """
            SELECT idx, event_type, content, metadata, timestamp
            FROM events
            ORDER BY idx DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    except sqlite3.Error as e:
        logger.warning("list_timeline_events failed for %s: %s", user_id, e)
        rows = []
    finally:
        conn.close()

    return [
        {
            "sync_id": int(idx),
            "event_type": et,
            "content": content or "",
            "metadata": _safe_json(meta_json),
            "timestamp": _ts_to_iso(ts),
        }
        for (idx, et, content, meta_json, ts) in rows
    ]


# ── Direct event-log writer ───────────────────────────────────────────
#
# Originally introduced as a test helper (hence the older
# ``_test_append_event`` name, kept below as a backwards-compat alias).
# Phase B promoted this to a production write surface — tools like
# ``run_workflow`` and routes like ``/run-in-chat`` use it to drop
# inline workflow_run cards into chat without spinning up a full
# DigitalTwin chat turn. It mirrors what twin's append() does, but
# bypasses the LLM round-trip.


def append_event(
    user_id: str,
    event_type: str,
    content: str,
    metadata: Optional[dict] = None,
    session_id: str = "",
    timestamp: Optional[float] = None,
) -> int:
    """Append one row to a user's twin EventLog.

    Creates the directory + table layout on first call so callers
    can seed events for a freshly-registered user that never opened
    a twin. Returns the row's ``idx`` (== sync_id on the wire).

    Used by tests AND by Phase B production paths
    (``tools_workflow.RunWorkflowTool`` and
    ``workflows_router.start_run_in_chat_endpoint``).
    """
    p = _db_path(user_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p))
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS events (
                idx INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp REAL NOT NULL,
                event_type TEXT NOT NULL,
                content TEXT NOT NULL,
                metadata TEXT DEFAULT '{}',
                agent_id TEXT NOT NULL,
                session_id TEXT DEFAULT ''
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_events_type ON events(event_type)"
        )
        ts = timestamp if timestamp is not None else datetime.now(
            timezone.utc
        ).timestamp()
        cur = conn.execute(
            """
            INSERT INTO events
            (timestamp, event_type, content, metadata, agent_id, session_id)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                ts,
                event_type,
                content,
                json.dumps(metadata or {}),
                _agent_id_for(user_id),
                session_id,
            ),
        )
        conn.commit()
        return int(cur.lastrowid)
    finally:
        conn.close()


# Backwards-compat alias — historical name used by every existing test.
# Keep around so the existing test suite keeps passing; new code uses
# ``append_event``.
_test_append_event = append_event


def replace_last_assistant_response(
    user_id: str, session_id: str, new_content: str,
) -> bool:
    """Overwrite the content of the most recent assistant_response row
    in the user's event log for this session. Used by the Level-2
    self-correcting workflow rescue: when Gemini hallucinates "我将
    启动 X 工作流" without emitting a function_call, the server starts
    the workflow itself and replaces the hallucinated reply with a
    crisp "已启动" ack so the user doesn't see two contradictory
    messages.

    Returns True on success, False if no matching row found.
    """
    p = _db_path(user_id)
    if not p.exists():
        return False
    conn = sqlite3.connect(str(p))
    try:
        row = conn.execute(
            """
            SELECT idx FROM events
            WHERE event_type IN ('assistant_response', 'assistant_message')
              AND COALESCE(session_id, '') = ?
            ORDER BY idx DESC LIMIT 1
            """,
            (session_id or "",),
        ).fetchone()
        if not row:
            return False
        conn.execute(
            "UPDATE events SET content = ? WHERE idx = ?",
            (new_content, int(row[0])),
        )
        conn.commit()
        return True
    except sqlite3.Error as e:
        logger.warning(
            "replace_last_assistant_response failed for %s: %s", user_id, e,
        )
        return False
    finally:
        conn.close()
