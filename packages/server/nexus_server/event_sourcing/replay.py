"""Replay — rebuild projection tables from event_log.

Per ADR-002 Rev-8, every projection table is a derived view; canonical
state lives only in ``twin_event_log`` plus content-addressed binaries
plus the meta-layer archive. This module is the function that performs
the derivation.

Usage::

    # Full rebuild from scratch (cold-start after corruption or schema upgrade)
    drop_projections(conn)
    replay(conn, from_event_idx=0)

    # Incremental catch-up (normal operation)
    cur = conn.execute(
        "SELECT last_applied_event_idx FROM projection_state WHERE projection_name='all'"
    )
    from_idx = cur.fetchone()[0]
    replay(conn, from_event_idx=from_idx + 1)

Determinism contract
--------------------

Per Rev-8: replay never invokes any LLM, never makes a network call,
never queries external services. Every state derivation is computed
from event payloads alone — LLM outputs were archived verbatim in
``ingestion_llm_response`` events at write time.

Unknown event kinds are a hard error. CI test enumerates every
(kind, version) ever shipped and asserts a handler exists. Per Rev-8 R23,
silently skipping an unhandled event is forbidden — replay halts.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from typing import Any, Callable

from nexus_server.event_sourcing.event_kinds import EVENT_REGISTRY, EventKind
from nexus_server.event_sourcing.schema import drop_projections, init_event_sourcing_schema

logger = logging.getLogger(__name__)


# Type alias.
ReplayHandler = Callable[[sqlite3.Cursor, dict[str, Any]], None]
"""Signature of a replay handler.

Args:
    cur: open cursor inside an outer transaction managed by replay().
    event: full event row, including event_idx, ts, user_id, patient_hash,
           payload (already JSON-decoded), caused_by.
"""


# (kind, version) → handler. Populated by import-time registration below
# and by handlers module.
REPLAY_HANDLERS: dict[tuple[EventKind, str], ReplayHandler] = {}


# ─────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────

class UnknownEventKindError(RuntimeError):
    """Raised when replay encounters an event it has no handler for.

    Per Rev-8 R23, replay halts loud rather than silently skipping —
    silent skip would diverge the rebuild invisibly.
    """


def register_handler(
    kind: EventKind,
    version: str,
    handler: ReplayHandler,
) -> None:
    """Register a replay handler for a (kind, version) pair.

    Called from the handlers module at import time. Refuses overrides.
    """
    key = (kind, version)
    if key in REPLAY_HANDLERS:
        raise ValueError(f"replay handler already registered for {key}")
    REPLAY_HANDLERS[key] = handler


def replay(
    conn: sqlite3.Connection,
    *,
    from_event_idx: int = 0,
    to_event_idx: int | None = None,
    batch_size: int = 1000,
) -> int:
    """Replay events into projection tables.

    Args:
        conn: an open SQLite connection. Must have canonical schema applied.
        from_event_idx: replay events with event_idx >= this. Default: 0
            (full rebuild).
        to_event_idx: optional upper bound (exclusive); used for time-travel
            queries ("rebuild state as of event N").
        batch_size: how many events to fetch per cursor round-trip.

    Returns:
        The event_idx of the last event applied (or from_event_idx-1 if
        nothing was replayed).

    Raises:
        UnknownEventKindError: an event kind has no registered handler.
    """
    last_applied = from_event_idx - 1
    where_clauses = ["event_idx >= ?"]
    params: list[Any] = [from_event_idx]
    if to_event_idx is not None:
        where_clauses.append("event_idx < ?")
        params.append(to_event_idx)

    query = (
        f"SELECT event_idx, event_kind, event_kind_version, user_id, "
        f"       patient_hash, ts, payload_json, caused_by "
        f"FROM twin_event_log "
        f"WHERE {' AND '.join(where_clauses)} "
        f"ORDER BY event_idx ASC"
    )

    # Single big transaction for the whole replay — atomic visibility,
    # rollback on any handler error. For huge replays you'd want to
    # commit in batches; we'll add that in a follow-up if R22 (rebuild
    # duration) bites.
    cur = conn.cursor()
    last_ts = 0

    try:
        cur.execute(query, params)
        while True:
            rows = cur.fetchmany(batch_size)
            if not rows:
                break

            for row in rows:
                (
                    event_idx, event_kind, event_kind_version, user_id,
                    patient_hash, ts, payload_json, caused_by,
                ) = row

                kind_enum = EventKind(event_kind)
                handler_key = (kind_enum, event_kind_version)
                handler = REPLAY_HANDLERS.get(handler_key)
                if handler is None:
                    raise UnknownEventKindError(
                        f"no replay handler for kind={event_kind} "
                        f"version={event_kind_version} (event_idx={event_idx}). "
                        f"Register one in nexus_server.event_sourcing.handlers "
                        f"or REPLAY_HANDLERS via register_handler()."
                    )

                event = {
                    "event_idx":          event_idx,
                    "event_kind":         event_kind,
                    "event_kind_version": event_kind_version,
                    "user_id":            user_id,
                    "patient_hash":       patient_hash,
                    "ts":                 ts,
                    "payload":            json.loads(payload_json),
                    "caused_by":          caused_by,
                }
                handler(cur, event)

                last_applied = event_idx
                last_ts = ts

        if last_applied >= from_event_idx:
            cur.execute(
                "UPDATE projection_state "
                "SET last_applied_event_idx = ?, last_applied_ts = ? "
                "WHERE projection_name = 'all'",
                (last_applied, last_ts),
            )

        conn.commit()
        logger.info(
            "replay complete: from=%d to=%d count=%d",
            from_event_idx, last_applied, max(last_applied - from_event_idx + 1, 0),
        )
    except Exception:
        conn.rollback()
        logger.exception("replay aborted; rolled back")
        raise

    return last_applied


def full_rebuild(conn: sqlite3.Connection) -> int:
    """Drop every projection, recreate schema, replay event_log from idx 0.

    Used after corruption, schema upgrade, or in the golden replay test.
    Canonical store (twin_event_log) is untouched.
    """
    logger.warning("FULL REBUILD: dropping projections then replaying from 0")
    drop_projections(conn)
    init_event_sourcing_schema(conn)   # recreates DROPped projection tables
    return replay(conn, from_event_idx=0)


# ─────────────────────────────────────────────────────────────────────
# Handler import — populates REPLAY_HANDLERS as a side effect of import.
# Kept at bottom so symbols defined above are available to handlers.
# ─────────────────────────────────────────────────────────────────────

from nexus_server.event_sourcing import (
    handlers as _handlers,  # noqa: E402, F401  (side-effect: registers handlers)
)

_ = _handlers  # silence unused-import lints; the import is the registration


def verify_handler_coverage() -> list[tuple[EventKind, str]]:
    """Return any registered (kind, version) pairs that lack a handler.

    Used by the CI test to enforce R23 mitigation: every registered
    event kind must have a handler before it can be shipped.
    """
    missing: list[tuple[EventKind, str]] = []
    for key in EVENT_REGISTRY:
        if key not in REPLAY_HANDLERS:
            missing.append(key)
    return missing
