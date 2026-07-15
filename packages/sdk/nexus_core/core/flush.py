"""
Flush Policy & Write-Ahead Log — configurable write batching for Nexus.

Three-layer write architecture:
  Layer 1 (Hot):  In-memory buffer + local WAL file    ← every event
  Layer 2 (Warm): object-store write                   ← batched every N events or T seconds
  Layer 3 (Cold): BSC state_root anchor                ← batched with Layer 2, or on critical events

The default FlushPolicy batches writes for performance. Users can override
to sync every event (maximum safety) or flush only on explicit call
(maximum control).

Usage:
    from nexus_core.flush import FlushPolicy

    # Default: batch every 5 events or 30 seconds
    policy = FlushPolicy()

    # Maximum safety: every event goes to chain
    policy = FlushPolicy.sync_every()

    # Maximum control: only flush when you say so
    policy = FlushPolicy.manual()

    # Custom: batch every 10 events, flush on task complete
    policy = FlushPolicy(every_n_events=10, interval_seconds=60)
"""

import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("nexus_core.flush")


@dataclass
class FlushPolicy:
    """
    Controls when buffered state is flushed to storage + BSC.

    Attributes:
        every_n_events:       Flush after accumulating this many events.
                              Set to 1 for sync-every-event behavior.
                              Set to 0 to disable event-count trigger.
        interval_seconds:     Max seconds between automatic flushes.
                              Set to 0 to disable time-based trigger.
        sync_task_transitions: Always flush synchronously on task status
                              changes (Pending→Running, Running→Completed, etc.).
                              Recommended True for data integrity.
        sync_on_close:        Flush remaining buffer when session/runtime closes.
        wal_enabled:          Write events to a local WAL file before buffering.
                              On crash recovery, WAL replays events since last flush.
        wal_dir:              Directory for WAL files.  Each agent gets its own file.
    """

    every_n_events: int = 5
    interval_seconds: float = 30.0
    sync_task_transitions: bool = True
    sync_on_close: bool = True
    wal_enabled: bool = True
    wal_dir: str = ".rune_wal"

    # ── Presets ──────────────────────────────────────────────────────

    @classmethod
    def sync_every(cls) -> "FlushPolicy":
        """Every event is written to storage + BSC immediately.

        Maximum safety, highest gas cost, highest latency.
        Equivalent to the legacy (pre-batching) behavior.
        """
        return cls(every_n_events=1, interval_seconds=0, wal_enabled=False)

    @classmethod
    def manual(cls) -> "FlushPolicy":
        """Nothing is flushed automatically.  Caller must invoke flush() explicitly.

        Maximum control.  Use this when you want to batch across multiple
        sessions or agents and control the exact commit points.
        """
        return cls(every_n_events=0, interval_seconds=0)

    @classmethod
    def balanced(cls) -> "FlushPolicy":
        """Default: batch 5 events or 30 seconds, sync on task transitions."""
        return cls()

    @classmethod
    def aggressive(cls) -> "FlushPolicy":
        """Larger batches for high-throughput agents.

        Trades crash-recovery granularity for lower gas cost.
        """
        return cls(every_n_events=20, interval_seconds=120)


class WriteAheadLog:
    """
    Append-only local log for crash recovery.

    Each WAL file stores JSON-Lines: one event per line.
    On flush, the WAL is truncated (events are now safely persisted + anchored).
    On crash recovery, the WAL is replayed from the last flush point.

    WAL files are stored at: {wal_dir}/{agent_id}.wal

    Thread safety: WAL operations are NOT thread-safe. Each agent runtime
    should have its own WAL instance.
    """

    def __init__(self, wal_dir: str, agent_id: str):
        self._dir = Path(wal_dir)
        self._dir.mkdir(parents=True, exist_ok=True)
        # Sanitize agent_id for filesystem use
        safe_id = str(agent_id).replace("/", "_").replace("\\", "_")
        self._path = self._dir / f"{safe_id}.wal"
        self._fd = None

    def append(self, entry: dict) -> None:
        """Append a single entry to the WAL.

        Raises OSError if the WAL file cannot be opened (disk full,
        permissions, etc.).  Callers must handle this — a silent failure
        here means events are NOT durable and crash recovery will lose data.
        """
        if self._fd is None:
            try:
                self._fd = open(self._path, "a")
            except OSError:
                logger.error("WAL open failed: %s — events will NOT be durable", self._path)
                raise
        line = json.dumps(entry, default=str, separators=(",", ":"))
        self._fd.write(line + "\n")
        self._fd.flush()
        os.fsync(self._fd.fileno())

    def read_all(self) -> list[dict]:
        """Read all entries from the WAL (for crash recovery)."""
        if not self._path.exists():
            return []
        entries = []
        with open(self._path, "r") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        entries.append(json.loads(line))
                    except json.JSONDecodeError:
                        logger.warning("Corrupt WAL entry, skipping: %s", line[:80])
        return entries

    def truncate(self) -> None:
        """Clear the WAL after a successful flush."""
        if self._fd is not None:
            self._fd.close()
            self._fd = None
        if self._path.exists():
            self._path.write_text("")
        logger.debug("WAL truncated: %s", self._path)

    def remove(self, predicate) -> int:
        """Remove every WAL entry where ``predicate(entry)`` returns True.

        Returns the count removed. Implementation: read full WAL,
        filter, atomic-replace via temp file. Atomicity matters because
        a crash mid-rewrite would otherwise lose every kept entry too.
        Cost is O(N) per call — fine for the per-entry-on-success
        pattern (~1 small WAL line / chat turn) but don't call this
        in tight loops; if you need to drop many entries, batch via
        a single predicate call.

        Use cases:
          * Per-entry remove on successful persistence — keeps the
            WAL bounded during long sessions instead of growing
            forever (it used to only get cleared on close()/replay).
          * ``drop_session(session_id)`` for hard-delete: remove every
            pending write under a session's prefix so a subsequent
            replay doesn't re-create what we just deleted.
        """
        if not self._path.exists():
            return 0
        # Close any append fd so we can rewrite cleanly.
        if self._fd is not None:
            self._fd.close()
            self._fd = None
        kept: list[str] = []
        removed = 0
        with open(self._path, "r") as f:
            for line in f:
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    entry = json.loads(stripped)
                except json.JSONDecodeError:
                    # Corrupt line — drop it (matches read_all's behaviour).
                    continue
                if predicate(entry):
                    removed += 1
                    continue
                kept.append(stripped)

        # Atomic rewrite via temp + rename so a crash in the middle
        # leaves either the old WAL or the new one — never an empty
        # one with all entries lost.
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        with open(tmp, "w") as f:
            for line in kept:
                f.write(line + "\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self._path)
        if removed:
            logger.debug(
                "WAL: removed %d entr(y/ies), %d remain", removed, len(kept),
            )
        return removed

    def drop_session(self, session_id: str) -> int:
        """Remove every pending write whose ``path`` references the
        given session id. Used by hard-delete so a stale WAL replay
        on next start can't resurrect data we've already wiped at
        the agent layer.

        Empty / falsey ``session_id`` is refused — same guard as
        :meth:`EventLog.delete_session`.
        """
        if not session_id:
            return 0
        token = str(session_id)
        return self.remove(
            lambda e: token in str(e.get("path", ""))
            or token == str(e.get("session_id", "")),
        )

    def close(self) -> None:
        """Close the WAL file handle."""
        if self._fd is not None:
            self._fd.close()
            self._fd = None

    @property
    def size(self) -> int:
        """Number of entries in the WAL (approximate, from file line count)."""
        if not self._path.exists():
            return 0
        with open(self._path, "r") as f:
            return sum(1 for line in f if line.strip())

    def __del__(self):
        self.close()


class FlushBuffer:
    """
    In-memory buffer that accumulates events and triggers flushes
    according to a FlushPolicy.

    The buffer does NOT perform the actual writes — it delegates to
    a callback.  This decouples the flush timing logic from the
    storage implementation (StateManager, SessionService, etc.).

    Usage:
        def do_flush(events):
            # Batch write to storage + anchor on BSC
            ...

        buf = FlushBuffer(policy=FlushPolicy(), on_flush=do_flush)
        buf.append(event_data)    # buffers; may trigger flush
        buf.force_flush()         # explicit flush
        buf.close()               # final flush on shutdown
    """

    def __init__(
        self,
        policy: FlushPolicy,
        on_flush: Any,  # Callable[[list[dict]], None]
        wal: Optional[WriteAheadLog] = None,
    ):
        self._policy = policy
        self._on_flush = on_flush
        self._wal = wal
        self._buffer: list[dict] = []
        self._last_flush_time = time.time()
        self._total_flushed = 0

    @property
    def policy(self) -> FlushPolicy:
        return self._policy

    @policy.setter
    def policy(self, new_policy: FlushPolicy) -> None:
        """Allow runtime policy changes."""
        self._policy = new_policy

    @property
    def pending_count(self) -> int:
        """Number of events in the buffer (not yet flushed)."""
        return len(self._buffer)

    @property
    def total_flushed(self) -> int:
        """Total number of events flushed since creation."""
        return self._total_flushed

    def append(self, entry: dict) -> bool:
        """
        Add an event to the buffer.  May trigger a flush.

        Returns True if a flush was triggered, False otherwise.
        """
        # Write to WAL first (crash safety)
        if self._wal and self._policy.wal_enabled:
            self._wal.append(entry)

        self._buffer.append(entry)

        # Check flush triggers
        if self._should_flush():
            self._do_flush()
            return True
        return False

    def force_flush(self) -> int:
        """
        Flush all buffered events immediately, regardless of policy.

        Returns the number of events flushed.
        """
        if not self._buffer:
            return 0
        return self._do_flush()

    def check_time_trigger(self) -> bool:
        """
        Check if the time-based flush trigger has fired.

        Call this periodically (e.g. from a timer or before reads)
        to ensure events don't sit in the buffer too long.

        Returns True if a flush was triggered.
        """
        if not self._buffer:
            return False
        if self._policy.interval_seconds <= 0:
            return False
        elapsed = time.time() - self._last_flush_time
        if elapsed >= self._policy.interval_seconds:
            self._do_flush()
            return True
        return False

    def close(self) -> None:
        """Flush remaining events and close resources."""
        if self._buffer and self._policy.sync_on_close:
            self._do_flush()
        if self._wal:
            self._wal.close()

    def recover_from_wal(self) -> list[dict]:
        """
        Read unflushed events from WAL (call on startup for crash recovery).

        Returns the recovered entries.  Caller should re-apply them to
        the session/state before continuing.
        """
        if not self._wal:
            return []
        entries = self._wal.read_all()
        if entries:
            logger.info("WAL recovery: %d entries found", len(entries))
        return entries

    def _should_flush(self) -> bool:
        """Check if any flush trigger has fired."""
        p = self._policy

        # Event count trigger
        if p.every_n_events > 0 and len(self._buffer) >= p.every_n_events:
            return True

        # Time trigger
        if p.interval_seconds > 0:
            elapsed = time.time() - self._last_flush_time
            if elapsed >= p.interval_seconds:
                return True

        return False

    def _do_flush(self) -> int:
        """Execute the flush callback and reset state."""
        count = len(self._buffer)
        if count == 0:
            return 0

        try:
            self._on_flush(list(self._buffer))
            self._total_flushed += count
            self._buffer.clear()
            self._last_flush_time = time.time()

            # Truncate WAL — events are now persisted + anchored
            if self._wal and self._policy.wal_enabled:
                self._wal.truncate()

            logger.debug("Flushed %d events (total: %d)", count, self._total_flushed)
        except Exception:
            # On flush failure, keep events in buffer for retry
            logger.error("Flush failed, %d events retained in buffer", count)
            raise

        return count
