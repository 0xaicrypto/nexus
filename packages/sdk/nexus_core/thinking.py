"""ThinkingEmitter — live agent reasoning telemetry.

Background
==========
The "thinking panel" used to be a polled view over twin's EventLog
events filtered to a coarse type list (heard / responded / decided …).
That gave the user historical breadcrumbs but never the *live* feel of
the agent reasoning — they stared at a spinner, then the answer
appeared, with the recall / tool calls / reasoning all collapsed into
the past tense.

This module is the foundation of the redesign. It's a tiny in-process
pub/sub: ``twin.chat`` calls ``emit(...)`` at every interesting decision
point during a turn, and any subscribers (today: a server-side SSE
broadcaster) get the events as they happen, with their original
sequence and timing preserved.

Design choices
--------------

* **Push, not pull.** Polling can never beat "show what just happened
  the moment it happened" — the desktop must see ``recall`` *before*
  ``reasoning`` and ``reasoning`` before ``tool_call`` because that's
  the actual order the agent did them in.

* **Per-twin queue, not global.** Each twin owns its own emitter so
  multi-tenant servers can subscribe per-user without filtering. The
  server's SSE endpoint resolves a twin via ``twin_manager.get_twin``
  and subscribes to that twin's emitter.

* **Best-effort and non-blocking.** ``emit`` uses
  ``Queue.put_nowait``. If a subscriber is too slow we drop the event
  rather than slow the chat path — the LLM call latency is the user's
  experience; thinking telemetry is gravy.

* **No dependence on EventLog.** Some thinking events (e.g. raw
  Gemini thinking tokens, mid-tool decisions) aren't durable — they
  don't belong in the event_log audit trail. Others (e.g.
  evolution_proposal) DO get persisted via the existing
  ``twin.event_log.append`` path. The emitter is parallel to, not
  instead of, EventLog.

Event taxonomy
--------------
``kind`` is one of (matches the desktop's ``ThinkingStepViewModel``):

  * ``heard``             — twin received a user turn
  * ``memory_recall``     — twin queried fact / episode / skill stores
  * ``reasoning``         — Gemini "thinking" tokens (chain-of-thought)
  * ``tool_call``         — twin invoked a tool
  * ``tool_result``       — tool returned (success or error)
  * ``insight``           — twin noted a new fact / contradiction
  * ``evolution_propose`` — twin proposed a falsifiable edit
  * ``replying``          — twin started streaming the final reply
  * ``replied``           — twin finished a turn

Each event also carries a ``turn_id`` (a per-twin monotonically
increasing integer assigned at the start of the chat turn) and a
``seq`` (a per-turn step counter). The desktop uses both to group
steps into turn cards and order them within a card.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional

logger = logging.getLogger(__name__)


# Bytes threshold below which a thinking step's ``content`` rides
# inline in the EventLog row's ``metadata.raw_content``. Anything
# larger is offloaded to a backend blob (the EventLog row keeps
# only the sha256 + blob path + a short preview), so the event rows
# stay compact and state-root hashing stays fast even when Gemini
# emits a 50KB chain-of-thought.
_DEFAULT_INLINE_CAP = 2048


# Cap any one subscriber's queue. If the desktop SSE consumer falls
# behind, we drop the *oldest* event in its queue rather than the
# newest. The reasoning: a user catching up to a stale stream wants
# to see "where the agent ended up", not "where it started". 200
# steps is enough to cover ~5 chat turns of full instrumentation.
_QUEUE_CAP = 200


@dataclass
class ThinkingEvent:
    """One step of an agent's reasoning, ready for transport.

    All fields are JSON-friendly so the SSE serializer can dump them
    via ``json.dumps`` without a custom encoder. ``content`` is free
    text (sentence summarising what happened); ``metadata`` is a
    dict for typed details the UI may render specially (tool args,
    recalled fact ids, token counts, etc).

    Turn identification carries TWO ids by design:
      * ``turn_id`` — twin-global monotonic counter; uniquely
        identifies a chat turn across the agent's whole life.
        Audit / on-chain references quote this.
      * ``session_turn_id`` — per-session counter (resets when the
        user opens a new chat thread). UI shows this as "Turn N"
        because it matches the user's mental model: "the Nth turn
        of *this* conversation".

    ``session_id`` lets consumers filter to one thread (e.g.
    cognition panel scoped to the active session).
    """

    turn_id: int
    seq: int
    kind: str
    label: str
    content: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)
    # Optional duration in seconds, set on completion-style events
    # (tool_result, replied) so the UI can show "1.2s · 184 tokens".
    duration_ms: Optional[int] = None
    # Per-session turn context. Both default to "" / 0 so existing
    # callers / tests keep working unchanged.
    session_id: str = ""
    session_turn_id: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "turn_id": self.turn_id,
            "seq": self.seq,
            "kind": self.kind,
            "label": self.label,
            "content": self.content,
            "metadata": self.metadata,
            "timestamp": self.timestamp,
            "duration_ms": self.duration_ms,
            "session_id": self.session_id,
            "session_turn_id": self.session_turn_id,
        }


class _Subscription:
    """One consumer's view onto an emitter — a bounded async queue.

    The consumer awaits ``next_event()``; the emitter's ``emit()``
    drops into ``put_nowait`` and skips full queues so a slow consumer
    can't slow the producer. ``aclose()`` lets a consumer detach
    cleanly when its connection drops.
    """

    def __init__(self) -> None:
        self.id = uuid.uuid4().hex
        self._queue: asyncio.Queue[ThinkingEvent] = asyncio.Queue(maxsize=_QUEUE_CAP)
        self._closed = False

    async def next_event(self, timeout: Optional[float] = None) -> Optional[ThinkingEvent]:
        """Block until the next event arrives or ``timeout`` elapses.

        Returns ``None`` on timeout (no event ready) or when the
        subscription was closed. Lets the SSE handler emit periodic
        keepalive frames between live events without spinning.
        """
        if self._closed:
            return None
        try:
            if timeout is None:
                return await self._queue.get()
            return await asyncio.wait_for(self._queue.get(), timeout=timeout)
        except asyncio.TimeoutError:
            return None
        except asyncio.CancelledError:
            self._closed = True
            return None

    def push(self, event: ThinkingEvent) -> bool:
        """Best-effort enqueue. Returns ``False`` if the queue is full
        (event dropped) so the emitter can metric that out — the
        consumer is genuinely too slow to keep up."""
        if self._closed:
            return False
        try:
            self._queue.put_nowait(event)
            return True
        except asyncio.QueueFull:
            # Drop OLDEST to make room — the user wants the latest
            # state of the agent's reasoning when they finally see it,
            # not the oldest. ``get_nowait`` may itself raise QueueEmpty
            # in a race; in that case fall back to dropping the new
            # event.
            try:
                _ = self._queue.get_nowait()
                self._queue.put_nowait(event)
                return True
            except (asyncio.QueueEmpty, asyncio.QueueFull):
                return False

    def close(self) -> None:
        self._closed = True


class ThinkingEmitter:
    """Per-twin pub/sub for live thinking events.

    Owners construct one of these as part of twin's ``__init__`` and
    call ``emit`` at every interesting moment in ``twin.chat``.
    Subscribers (server SSE handlers, tests, telemetry) call
    ``subscribe()`` to get a queue-backed iterator over events as
    they fire.
    """

    def __init__(self) -> None:
        self._subs: list[_Subscription] = []
        # Twin-global monotonic counter — never resets, audit-friendly.
        self._turn_counter: int = 0
        self._seq_counter: int = 0
        self._current_turn_id: int = 0
        # Per-session counters. Resets only when a session is reopened
        # / new session is created — matches user-facing "Turn N" of
        # the current conversation. Keyed by session_id (empty string
        # for the synthetic default thread).
        self._session_turn_counts: dict[str, int] = {}
        self._current_session_id: str = ""
        self._current_session_turn_id: int = 0
        self._lock = asyncio.Lock()
        self._dropped: int = 0
        # Persistence wiring (attached after construction by
        # twin._initialize once the EventLog + ChainBackend are
        # ready). When unattached, emits go only to the in-process
        # subscribers — same behaviour as before Phase A2.
        self._event_log: Optional[Any] = None
        self._blob_writer: Optional[Callable[[str, bytes], Awaitable[Any]]] = None
        self._inline_cap: int = _DEFAULT_INLINE_CAP
        self._persist_failures: int = 0

    # ── Wiring (called once by the twin after construction) ──────

    def attach(
        self,
        event_log: Optional[Any] = None,
        blob_writer: Optional[Callable[[str, bytes], Awaitable[Any]]] = None,
        inline_cap: int = _DEFAULT_INLINE_CAP,
    ) -> None:
        """Hook the emitter to a persistent EventLog + (optional)
        backend blob writer.

        After ``attach``, every ``emit()`` ALSO appends a
        ``thinking_step`` event into the EventLog so the agent's
        reasoning becomes part of the audit trail (and rides into
        the next BSC state-root anchor via ChainBackend).

        ``blob_writer(path, data) -> awaitable`` lets us offload
        oversize content (Gemini chain-of-thought, tool result blobs)
        to the storage backend instead of bloating the EventLog row.
        Implementations should be non-blocking — the emitter calls
        them via ``asyncio.create_task`` so a slow write
        can't stall the chat path.

        Best-effort: if attach is never called, the emitter behaves
        as a pure in-process pub/sub (existing tests / standalone
        SDK use).
        """
        self._event_log = event_log
        self._blob_writer = blob_writer
        if inline_cap > 0:
            self._inline_cap = inline_cap

    @property
    def persist_failure_count(self) -> int:
        """Cumulative count of EventLog append failures. Inspectable
        for tests + ops dashboards."""
        return self._persist_failures

    # ── Producer side (twin internals) ───────────────────────────────

    def start_turn(self, session_id: str = "") -> int:
        """Mark the start of a new chat turn.

        Bumps both counters: the twin-global ``turn_id`` (unique
        across the agent's whole life — what audit / on-chain
        references quote) and the per-session ``session_turn_id``
        (resets per session — what the UI displays as "Turn N").
        Returns the global ``turn_id`` for back-compat with callers
        that want a single int handle on the turn.

        Resets the per-turn ``seq`` counter so the new turn's emits
        start at seq=1.
        """
        self._turn_counter += 1
        self._current_turn_id = self._turn_counter
        self._current_session_id = session_id
        self._session_turn_counts[session_id] = (
            self._session_turn_counts.get(session_id, 0) + 1
        )
        self._current_session_turn_id = self._session_turn_counts[session_id]
        self._seq_counter = 0
        return self._current_turn_id

    def emit(
        self,
        kind: str,
        label: str,
        content: str = "",
        metadata: Optional[dict[str, Any]] = None,
        duration_ms: Optional[int] = None,
    ) -> ThinkingEvent:
        """Fire one event. Non-blocking — drops on slow subscribers.

        Always succeeds (returns the event) so callers never need
        ``try`` around an emit. Metric-style observability (queue
        full → dropped events) lives in ``self._dropped`` for the
        operator to inspect.
        """
        if self._current_turn_id == 0:
            # Caller forgot start_turn(); auto-bootstrap so we don't
            # silently drop the first turn's instrumentation. Session
            # defaults to "" — matches the synthetic default thread.
            self.start_turn(session_id="")
        self._seq_counter += 1
        ev = ThinkingEvent(
            turn_id=self._current_turn_id,
            seq=self._seq_counter,
            kind=kind,
            label=label,
            content=content,
            metadata=dict(metadata or {}),
            duration_ms=duration_ms,
            session_id=self._current_session_id,
            session_turn_id=self._current_session_turn_id,
        )
        for sub in list(self._subs):
            if not sub.push(ev):
                self._dropped += 1
                logger.debug(
                    "ThinkingEmitter: dropped %s/%s for slow sub %s",
                    kind, label, sub.id,
                )

        # Phase A2: double-write to the persistent EventLog so each
        # thinking step also rides into the next BSC state-root
        # anchor. SSE delivery (above) is realtime; this path is
        # the audit trail. Best-effort — a persistence failure
        # never blocks emit's return, the realtime stream is
        # always preserved.
        self._persist_to_event_log(ev)

        return ev

    # ── Persistence (audit + on-chain anchor) ─────────────────────

    def _persist_to_event_log(self, ev: ThinkingEvent) -> None:
        """Append a ``thinking_step`` event to the wired EventLog.

        Content sizing strategy (matches the file_uploaded /
        memory_compact patterns elsewhere in the system):
          * Small content (≤ inline_cap bytes) rides inline in
            ``metadata.raw_content`` — the EventLog row is the
            canonical source.
          * Large content is offloaded to the backend via the
            blob_writer; the EventLog row stores
            ``metadata.content_hash`` (sha256) + ``metadata.blob_path``
            + a 512-char preview so the UI can still show
            something without paying a network round-trip.

        Either way, the ``thinking_step`` row gets hashed into the
        next state-root anchor exactly like every other event,
        making the agent's reasoning verifiable on-chain.
        """
        if self._event_log is None:
            return

        content_str = ev.content or ""
        content_bytes = content_str.encode("utf-8")

        meta: dict[str, Any] = {
            "kind": ev.kind,
            "label": ev.label,
            "turn_id": ev.turn_id,
            "session_turn_id": ev.session_turn_id,
            "session_id": ev.session_id,
            "seq": ev.seq,
            "duration_ms": ev.duration_ms,
            "step_metadata": dict(ev.metadata or {}),
            "ts": ev.timestamp,
        }

        if len(content_bytes) <= self._inline_cap:
            meta["raw_content"] = content_str
            meta["content_storage"] = "inline"
        else:
            sha = hashlib.sha256(content_bytes).hexdigest()
            blob_path = f"thinking/{ev.turn_id}/{ev.seq}.txt"
            meta["preview"] = content_str[:512]
            meta["content_hash"] = sha
            meta["content_bytes"] = len(content_bytes)
            meta["blob_path"] = blob_path
            meta["content_storage"] = "backend_blob"
            # Fire-and-forget the blob upload. We can't await here
            # (emit is sync) but ``asyncio.create_task`` schedules
            # the coroutine on the running loop; ChainBackend's
            # store_blob writes the local store synchronously.
            if self._blob_writer is not None:
                try:
                    loop = asyncio.get_running_loop()
                    loop.create_task(
                        self._blob_writer(blob_path, content_bytes),
                        name=f"thinking_blob_t{ev.turn_id}_s{ev.seq}",
                    )
                except RuntimeError:
                    # No running event loop (e.g. emit called from a
                    # sync test). Skip the blob upload — the EventLog
                    # row still has the hash + preview, recovery code
                    # can re-upload later if the row's blob_path
                    # turns up empty in the store.
                    pass
                except Exception as e:  # noqa: BLE001
                    logger.debug("thinking blob schedule failed: %s", e)

        try:
            self._event_log.append(
                event_type="thinking_step",
                content=ev.label or ev.kind,
                metadata=meta,
                session_id=ev.session_id,
            )
        except Exception as e:  # noqa: BLE001
            self._persist_failures += 1
            logger.debug(
                "thinking_step persist failed (turn=%s seq=%s): %s",
                ev.turn_id, ev.seq, e,
            )

    @property
    def dropped_count(self) -> int:
        """How many events have been dropped due to slow subscribers
        across this emitter's lifetime. Inspectable for tests."""
        return self._dropped

    # ── Consumer side (server SSE / tests) ───────────────────────────

    def subscribe(self) -> _Subscription:
        """Open a new subscription. Caller must call ``unsubscribe(s)``
        (or use the async iterator below) when done so we don't leak
        bounded queues on disconnected clients."""
        sub = _Subscription()
        self._subs.append(sub)
        logger.debug(
            "ThinkingEmitter: subscriber %s opened (total=%d)",
            sub.id, len(self._subs),
        )
        return sub

    def unsubscribe(self, sub: _Subscription) -> None:
        sub.close()
        try:
            self._subs.remove(sub)
        except ValueError:
            pass
        logger.debug(
            "ThinkingEmitter: subscriber %s closed (total=%d)",
            sub.id, len(self._subs),
        )

    @property
    def subscriber_count(self) -> int:
        return len(self._subs)
