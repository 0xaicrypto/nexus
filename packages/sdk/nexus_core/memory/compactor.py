"""EventLogCompactor — auto-compact event log into curated memory.

Periodically projects recent events into CuratedMemory when the event log
grows beyond a threshold. The projection result is appended to the EventLog
as a `memory_compact` event (immutable, persisted with the log).

The projection function is injected — SDK doesn't depend on LLM.

Usage:
    from nexus_core.memory import EventLog, CuratedMemory, EventLogCompactor

    log = EventLog(base_dir=".", agent_id="my-agent")
    mem = CuratedMemory(base_dir=".")

    async def my_projection(query, budget):
        return await llm.complete(f"Summarize: {query}")

    compactor = EventLogCompactor(log, mem, projection_fn=my_projection)

    # Check every turn — only triggers when thresholds are met
    if compactor.should_compact(turn_count=20):
        await compactor.compact()
"""

from __future__ import annotations

import logging
from typing import Callable, Awaitable, Optional

from .event_log import EventLog
from .curated import CuratedMemory

logger = logging.getLogger(__name__)


class EventLogCompactor:
    """Auto-compact event log into curated memory.

    Trigger conditions (both must be true):
      - turn_count is a multiple of compact_interval
      - event log trajectory exceeds char_threshold * 0.8

    Projection result is appended to EventLog as a `memory_compact` event,
    ensuring it persists with everything else.
    """

    def __init__(
        self,
        event_log: EventLog,
        curated_memory: CuratedMemory,
        projection_fn: Callable[..., Awaitable[str]] = None,
        compact_interval: int = 20,
        char_threshold: int = 30000,
    ):
        self._log = event_log
        self._mem = curated_memory
        self._project = projection_fn
        self._interval = compact_interval
        self._threshold = char_threshold
        self._last_compact_turn = 0

    def should_compact(self, turn_count: int) -> bool:
        """Check if compaction should trigger at this turn."""
        if not self._project:
            return False
        if turn_count <= 0 or turn_count % self._interval != 0:
            return False
        if turn_count <= self._last_compact_turn:
            return False

        trajectory = self._log.get_trajectory(max_chars=self._threshold)
        return len(trajectory) > self._threshold * 0.8

    async def compact(self, session_id: str = "") -> bool:
        """Execute compaction: project → EventLog + CuratedMemory.

        Returns True if compaction succeeded.
        """
        if not self._project:
            logger.warning("No projection function set — cannot compact")
            return False

        try:
            projection = await self._project(
                "Summarize the key facts, user preferences, and important context from recent events",
                2000,
            )
            if not projection:
                return False

            # 1. Append projection to EventLog (immutable, persisted with the log)
            self._log.append(
                "memory_compact",
                projection,
                session_id=session_id,
                metadata={
                    "type": "auto_compact",
                    "event_count": self._log.count(),
                },
            )

            # 2. Update CuratedMemory (local derived view)
            self._update_curated(projection)

            self._last_compact_turn = self._log.count()
            logger.info("Compact: %d memory + %d user entries",
                        self._mem.memory_count, self._mem.user_count)
            return True

        except Exception as e:
            logger.warning("Compact failed: %s", e)
            return False

    def _update_curated(self, projection: str) -> None:
        """Parse projection text and update CuratedMemory."""
        user_keywords = [
            "user", "prefers", "likes", "style", "language", "tone",
            "用户", "偏好", "喜欢", "风格", "语言",
        ]
        for line in projection.split("\n"):
            line = line.strip().lstrip("- •·")
            if not line or len(line) < 10:
                continue
            if any(kw in line.lower() for kw in user_keywords):
                self._mem.add_user_info(line)
            else:
                self._mem.add_memory(line)

        self._mem.refresh_snapshot()

    @property
    def last_compact_turn(self) -> int:
        return self._last_compact_turn

    # ── Phase C: Evolution Pressure dashboard ─────────────────────

    def pressure_state(self) -> dict:
        """Per-evolver state for the Pressure Dashboard.

        Compactor fires every ``compact_interval`` turns (default
        20). Accumulator = events_since_last_compact, threshold =
        the interval. Status transitions:
          * "warming"     — fewer than threshold events accumulated
          * "ready"       — threshold reached, next chat turn fires
          * "fired_recently" — fired within the last 60s

        ``details.trajectory_chars`` exposes the secondary trigger
        (compact only fires when trajectory > 0.8 × char_threshold)
        so the lineage view can show "fired because volume > X" vs
        "fired because turn count hit interval".
        """
        current = self._log.count()
        delta = max(0, current - self._last_compact_turn)
        # "fired recently" detection: if a memory_compact event
        # showed up in the last few rows, the gauge briefly flashes
        # so the user sees their compaction lit up. Cheap scan over
        # bounded recent window.
        last_fired_at = None
        try:
            for ev in self._log.recent(limit=20):
                if ev.event_type == "memory_compact":
                    last_fired_at = float(ev.timestamp)
                    break
        except Exception:  # noqa: BLE001
            pass
        if delta == 0:
            status = "fired_recently" if last_fired_at else "idle"
        elif delta >= self._interval:
            status = "ready"
        else:
            status = "warming"
        return {
            "evolver": "EventLogCompactor",
            "layer": "L1",
            "accumulator": float(delta),
            "threshold": float(self._interval),
            "unit": "events",
            "status": status,
            "fed_by": ["chat.turn"],
            "last_fired_at": last_fired_at,
            "details": {
                "interval": self._interval,
                "char_threshold": self._threshold,
                "last_compact_turn": self._last_compact_turn,
                "current_event_count": current,
            },
        }
