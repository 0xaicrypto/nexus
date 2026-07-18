"""EventLog — append-only event log for DPM (Deterministic Projection Memory).

Based on: "Stateless Decision Memory for Enterprise AI Agents" (arXiv:2604.20158)

The event log is the single source of truth. Events are never edited, summarized,
or overwritten. At decision time, a projection function extracts a task-conditioned
view from the log.

Properties (by construction):
  - Deterministic replay: same log + same model = same projection
  - Auditable rationale: projection cites event indices
  - Multi-tenant isolation: each agent has its own log
  - Stateless: no mutable memory state, only append-only log + pure projection

Storage: SQLite (local, fast, durable) with optional chain-backend snapshots.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class Event:
    """A single immutable event in the log."""
    index: int = 0                      # Auto-assigned by EventLog
    timestamp: float = 0.0              # Unix timestamp
    event_type: str = ""                # "user_message", "assistant_response", "tool_call", "tool_result", "system"
    content: str = ""                   # The actual content
    metadata: dict = field(default_factory=dict)  # Extra data (tool name, model, etc.)
    agent_id: str = ""
    session_id: str = ""

    def to_log_line(self) -> str:
        """Format for projection prompt: [index] content"""
        return f"[{self.index}] {self.content}"


class EventLog:
    """Append-only event log backed by SQLite.

    Thread-safe. Each agent has its own log file.
    Events are never modified or deleted (append-only by construction).

    Usage:
        log = EventLog(base_dir=".nexus", agent_id="my-agent")
        log.append("user_message", "What's the weather?", session_id="s1")
        log.append("assistant_response", "It's sunny today.", session_id="s1")

        # At decision time: get recent events for projection
        events = log.recent(limit=50)
        # Or search
        events = log.search("weather")
    """

    def __init__(self, base_dir: str | Path, agent_id: str):
        self._dir = Path(base_dir) / "event_log"
        self._dir.mkdir(parents=True, exist_ok=True)
        safe_id = agent_id.replace("/", "_").replace("\\", "_")
        self._db_path = self._dir / f"{safe_id}.db"
        self._agent_id = agent_id
        self._conn: Optional[sqlite3.Connection] = None
        self._init_db()

    def _init_db(self) -> None:
        """Initialize SQLite database with WAL mode and FTS5."""
        self._conn = sqlite3.connect(str(self._db_path), check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")

        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS events (
                idx INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp REAL NOT NULL,
                event_type TEXT NOT NULL,
                content TEXT NOT NULL,
                metadata TEXT DEFAULT '{}',
                agent_id TEXT NOT NULL,
                session_id TEXT DEFAULT ''
            )
        """)

        # FTS5 for full-text search across all events
        self._conn.execute("""
            CREATE VIRTUAL TABLE IF NOT EXISTS events_fts
            USING fts5(content, content=events, content_rowid=idx)
        """)

        # Triggers to keep FTS in sync
        self._conn.executescript("""
            CREATE TRIGGER IF NOT EXISTS events_ai AFTER INSERT ON events BEGIN
                INSERT INTO events_fts(rowid, content) VALUES (new.idx, new.content);
            END;
        """)

        self._conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_events_session
            ON events(session_id, timestamp)
        """)
        self._conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_events_type
            ON events(event_type)
        """)

        self._conn.commit()
        logger.debug("EventLog initialized: %s", self._db_path)

    def append(self, event_type: str, content: str,
               session_id: str = "", metadata: dict = None) -> int:
        """Append an event to the log. Returns the event index."""
        ts = time.time()
        meta_json = json.dumps(metadata or {}, default=str)

        cursor = self._conn.execute(
            "INSERT INTO events (timestamp, event_type, content, metadata, agent_id, session_id) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (ts, event_type, content, meta_json, self._agent_id, session_id),
        )
        self._conn.commit()
        idx = cursor.lastrowid
        logger.debug("Event appended: [%d] %s (%d chars)", idx, event_type, len(content))
        return idx

    def recent(self, limit: int = 50, session_id: str = None) -> list[Event]:
        """Get the most recent events (optionally filtered by session)."""
        if session_id:
            rows = self._conn.execute(
                "SELECT idx, timestamp, event_type, content, metadata, agent_id, session_id "
                "FROM events WHERE session_id = ? ORDER BY idx DESC LIMIT ?",
                (session_id, limit),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT idx, timestamp, event_type, content, metadata, agent_id, session_id "
                "FROM events ORDER BY idx DESC LIMIT ?",
                (limit,),
            ).fetchall()

        events = [self._row_to_event(r) for r in reversed(rows)]
        return events

    def search(self, query: str, limit: int = 20) -> list[Event]:
        """Full-text search across all events."""
        rows = self._conn.execute(
            "SELECT e.idx, e.timestamp, e.event_type, e.content, e.metadata, e.agent_id, e.session_id "
            "FROM events e JOIN events_fts f ON e.idx = f.rowid "
            "WHERE events_fts MATCH ? ORDER BY rank LIMIT ?",
            (query, limit),
        ).fetchall()
        return [self._row_to_event(r) for r in rows]

    def count(self, session_id: str = None) -> int:
        """Count total events (optionally for a session)."""
        if session_id:
            row = self._conn.execute(
                "SELECT COUNT(*) FROM events WHERE session_id = ?", (session_id,)
            ).fetchone()
        else:
            row = self._conn.execute("SELECT COUNT(*) FROM events").fetchone()
        return row[0] if row else 0

    def get_trajectory(self, session_id: str = None,
                       max_chars: int = 100000) -> str:
        """Get the full trajectory as formatted text for projection.

        Returns events formatted as: [index] content
        Truncated to max_chars from the most recent end.
        """
        events = self.recent(limit=500, session_id=session_id)
        lines = []
        total = 0
        for e in events:
            line = e.to_log_line()
            if total + len(line) > max_chars:
                break
            lines.append(line)
            total += len(line) + 1
        return "\n".join(lines)

    def get_session_ids(self, limit: int = 20) -> list[str]:
        """Get recent unique session IDs."""
        rows = self._conn.execute(
            "SELECT DISTINCT session_id FROM events "
            "WHERE session_id != '' ORDER BY MAX(timestamp) DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [r[0] for r in rows]

    def delete_session(self, session_id: str) -> int:
        """Hard-delete every event tagged with ``session_id``.

        Returns the count of rows removed. Also clears the FTS rowid
        entries for those rows so full-text search doesn't surface
        zombies.

        Caller responsibilities:
          * Chain-backend object copies (if any) — issue separate
            object delete calls against the chain backend. This
            method only touches the local SQLite event_log.
          * BSC state-root anchors are immutable on chain. Existing
            anchors will keep referencing hashes that were computed
            when these rows were present; that's the honest record
            of "session existed at block N, was deleted at block M".

        Refuses to operate on an empty ``session_id`` — too easy to
        mistake the empty string for a real id and wipe every legacy
        (pre-multi-session) message.
        """
        if not session_id:
            raise ValueError(
                "delete_session refuses to operate on empty session_id; "
                "pass an explicit id."
            )
        cur = self._conn.execute(
            "SELECT idx FROM events WHERE session_id = ?", (session_id,),
        )
        idxs = [r[0] for r in cur.fetchall()]
        if not idxs:
            return 0
        # Delete from FTS first (FTS rows are keyed by rowid). Chunk
        # the IN-list so we don't blow the SQLite parameter cap.
        for chunk_start in range(0, len(idxs), 500):
            chunk = idxs[chunk_start: chunk_start + 500]
            placeholders = ",".join("?" * len(chunk))
            self._conn.execute(
                f"DELETE FROM events_fts WHERE rowid IN ({placeholders})",
                chunk,
            )
        self._conn.execute(
            "DELETE FROM events WHERE session_id = ?", (session_id,),
        )
        self._conn.commit()
        logger.info(
            "EventLog: deleted %d rows for session %s", len(idxs), session_id,
        )
        return len(idxs)

    def close(self) -> None:
        """Close the database connection."""
        if self._conn:
            self._conn.close()
            self._conn = None

    def _row_to_event(self, row) -> Event:
        idx, ts, etype, content, meta_json, agent_id, session_id = row
        try:
            metadata = json.loads(meta_json) if meta_json else {}
        except json.JSONDecodeError:
            metadata = {}
        return Event(
            index=idx, timestamp=ts, event_type=etype,
            content=content, metadata=metadata,
            agent_id=agent_id, session_id=session_id,
        )

    @property
    def db_path(self) -> Path:
        return self._db_path
