"""The Store — single mutation entry point for the event-sourced memory.

Per ADR-002 Rev-8, every state mutation in Layer 1-4 of Nexus memory
goes through ``Store.emit_and_apply()``. No other code path is allowed
to write to projection tables. A CI lint rule (added in a follow-up
phase) scans for direct INSERT/UPDATE/DELETE against any name in
``PROJECTION_TABLES`` and fails the PR if found.

Why the indirection
-------------------

Contract B (event_log = single source of truth) only holds if every
projection write is preceded by an event in the same SQLite transaction.
If a code path bypasses this — even once — then dropping projections
and replaying ``event_log`` produces a divergent rebuild, and Rev-8 is
broken. The Store class makes the invariant impossible to violate by
mistake: there's no shortcut, no ``store.add_node_directly()`` escape
hatch.

Transaction semantics
---------------------

``emit_and_apply()`` does both writes under a single SQLite BEGIN ... COMMIT.
SQLite gives us:
- Atomic visibility: readers never see the event without the projection
  mutation (or vice versa).
- Crash safety: if the process dies between INSERT INTO event_log and
  the projection write, the whole transaction rolls back. event_log
  never contains an event whose projection mutation didn't apply.

This is the load-bearing guarantee.

Performance
-----------

Writing one event per mutation adds ~50µs per write on M-series SSD
(empirical, SQLite WAL). For bulk ingestion paths (DICOM with hundreds
of findings), batch events together via the ``emit_and_apply_many()``
helper which wraps multiple emit+apply pairs in a single transaction.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import time
from typing import Any, Callable

from nexus_server.event_sourcing.event_kinds import (
    EventKind,
    current_version,
    validate_payload,
)

logger = logging.getLogger(__name__)


# Projection tables — any direct write to these from outside this module
# is a hard violation of Rev-8. CI lint enforces.
PROJECTION_TABLES: frozenset[str] = frozenset({
    "clinical_graph_nodes",
    "clinical_graph_edges",
    "node_provenance",
    "cached_views",
    "practitioner_facts",
    "practitioner_observations",
    "reference_knowledge",
})


# Patterns that must not appear in Layer 2 ``practitioner_facts.pattern_value_json`` —
# enforced at write time as belt-and-braces against extractor leakage.
_HEX_HASH_PATTERN = re.compile(r"\b[0-9a-f]{32,}\b", re.IGNORECASE)
_ISO_DATE_PATTERN = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")


# ─────────────────────────────────────────────────────────────────────
# Errors
# ─────────────────────────────────────────────────────────────────────

class StoreError(Exception):
    """Base class for store-layer errors."""


class UnknownEventKindError(StoreError):
    """Raised when emitting an event whose (kind, version) isn't registered."""


class ProvenanceRequiredError(StoreError):
    """Raised when a clinical-fact node is added without provenance.

    Per Rev-2: every semantic_fact / finding / measurement node must
    have an accompanying node_provenance row in the same transaction.
    """


class PrivacyInvariantViolation(StoreError):
    """Raised when a write would violate a Layer 2 / Rev-8 privacy invariant.

    Currently fires when ``practitioner_facts.pattern_value_json`` contains
    a 32+ hex hash (likely patient_hash) or an ISO date (likely a specific
    encounter date that could re-identify).
    """


# ─────────────────────────────────────────────────────────────────────
# Type aliases
# ─────────────────────────────────────────────────────────────────────

ApplyFn = Callable[[sqlite3.Cursor, dict[str, Any]], None]
"""Signature of a projection-apply function.

Identical to replay.ReplayHandler so the same function works on both
the emit path and the replay path — by-construction guarantee that
they produce the same projection state.

Args:
    cur: an open cursor (same transaction as the event INSERT).
    event: full event dict — event_idx, event_kind, event_kind_version,
           user_id, patient_hash, ts, payload (already decoded),
           caused_by. Identical shape to what replay.replay() passes.

Must not commit or rollback — the Store owns transaction lifecycle.
Must only touch projection tables (CI-checked).
"""


# ─────────────────────────────────────────────────────────────────────
# Store
# ─────────────────────────────────────────────────────────────────────

class Store:
    """Mutation gateway. The only legal mutation entry point.

    Usage::

        store = Store(conn)
        event_idx = store.emit_and_apply(
            kind=EventKind.NODE_ADDED,
            payload={
                "node_type": "finding",
                "content_json": {"label": "left renal mass", "size_cm": 2.4},
                "weight": 1.0,
                "originating_event_idx": parent_idx,
            },
            apply_fn=apply_node_added_v1,
            user_id="dr_chen",
            patient_hash="7a3f...",
            caused_by=parent_idx,
        )

    For ingester flows that emit many events in a row, use
    ``emit_and_apply_many()`` to batch under a single transaction.
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        # We rely on the caller to ensure conn has PRAGMA foreign_keys=ON.
        # Schema.init_event_sourcing_schema sets it.

    # ─────────────────────────────────────────────────────────────
    # Core API
    # ─────────────────────────────────────────────────────────────

    def emit_and_apply(
        self,
        *,
        kind: EventKind,
        payload: dict[str, Any],
        apply_fn: ApplyFn,
        user_id: str,
        patient_hash: str | None = None,
        caused_by: int | None = None,
        version: str | None = None,
    ) -> int:
        """Append one event + apply its projection mutation atomically.

        Returns the autoincrement ``event_idx`` of the newly emitted event.

        Raises:
            UnknownEventKindError: kind/version isn't in EVENT_REGISTRY.
            EventValidationError: payload missing required fields.
            ProvenanceRequiredError: clinical-fact node added without provenance.
            PrivacyInvariantViolation: Layer 2 write contains PHI markers.
        """
        version = version or current_version(kind)

        # 1. Validate payload against the registered spec.
        validate_payload(kind, version, payload, patient_hash=patient_hash)

        # 2. Privacy belt-and-braces (Layer 2 specifically).
        self._check_privacy_invariants(kind, payload)

        # 3. Atomic write: event + projection mutation.
        with self._conn:  # BEGIN ... COMMIT or ROLLBACK
            cur = self._conn.cursor()
            ts = _monotonic_now_us()
            cur.execute(
                "INSERT INTO twin_event_log "
                "(event_kind, event_kind_version, user_id, patient_hash, "
                " ts, payload_json, caused_by) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    kind.value,
                    version,
                    user_id,
                    patient_hash,
                    ts,
                    json.dumps(payload, separators=(",", ":"), sort_keys=True),
                    caused_by,
                ),
            )
            event_idx = cur.lastrowid
            assert event_idx is not None  # sqlite3 always returns int after INSERT

            # Build the event dict in the same shape replay uses.
            # Critical: same shape → emit + replay can share handlers
            # → replay determinism is by-construction.
            event_dict = {
                "event_idx":          event_idx,
                "event_kind":         kind.value,
                "event_kind_version": version,
                "user_id":            user_id,
                "patient_hash":       patient_hash,
                "ts":                 ts,
                "payload":            payload,
                "caused_by":          caused_by,
            }
            apply_fn(cur, event_dict)

            # Update projection checkpoint.
            cur.execute(
                "UPDATE projection_state "
                "SET last_applied_event_idx = ?, last_applied_ts = ? "
                "WHERE projection_name = 'all'",
                (event_idx, ts),
            )
            if cur.rowcount == 0:
                cur.execute(
                    "INSERT OR IGNORE INTO projection_state "
                    "(projection_name, last_applied_event_idx, last_applied_ts) "
                    "VALUES ('all', ?, ?)",
                    (event_idx, ts),
                )

        logger.debug(
            "emit_and_apply kind=%s v=%s event_idx=%d user=%s patient=%s",
            kind.value, version, event_idx, user_id, patient_hash,
        )
        return event_idx

    def emit_and_apply_many(
        self,
        *,
        user_id: str,
        items: list[dict[str, Any]],
    ) -> list[int]:
        """Batch emit+apply N events in a single SQLite transaction.

        Each ``item`` is a dict with the same keys as ``emit_and_apply``
        kwargs (kind, payload, apply_fn, patient_hash, caused_by, version).

        Used by ingesters that emit (started → llm_response → many nodes →
        completed) — collapses N transactions into 1.
        """
        if not items:
            return []

        event_idxs: list[int] = []
        with self._conn:
            cur = self._conn.cursor()
            last_event_ts = 0
            last_event_idx = 0

            for item in items:
                kind: EventKind = item["kind"]
                payload: dict[str, Any] = item["payload"]
                apply_fn: ApplyFn = item["apply_fn"]
                patient_hash: str | None = item.get("patient_hash")
                caused_by: int | None = item.get("caused_by")
                version: str = item.get("version") or current_version(kind)

                validate_payload(kind, version, payload, patient_hash=patient_hash)
                self._check_privacy_invariants(kind, payload)

                ts = _monotonic_now_us()
                cur.execute(
                    "INSERT INTO twin_event_log "
                    "(event_kind, event_kind_version, user_id, patient_hash, "
                    " ts, payload_json, caused_by) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        kind.value, version, user_id, patient_hash, ts,
                        json.dumps(payload, separators=(",", ":"), sort_keys=True),
                        caused_by,
                    ),
                )
                event_idx = cur.lastrowid
                assert event_idx is not None
                event_dict = {
                    "event_idx":          event_idx,
                    "event_kind":         kind.value,
                    "event_kind_version": version,
                    "user_id":            user_id,
                    "patient_hash":       patient_hash,
                    "ts":                 ts,
                    "payload":            payload,
                    "caused_by":          caused_by,
                }
                apply_fn(cur, event_dict)
                event_idxs.append(event_idx)
                last_event_ts = ts
                last_event_idx = event_idx

            cur.execute(
                "UPDATE projection_state "
                "SET last_applied_event_idx = ?, last_applied_ts = ? "
                "WHERE projection_name = 'all'",
                (last_event_idx, last_event_ts),
            )

        logger.debug("emit_and_apply_many count=%d user=%s", len(items), user_id)
        return event_idxs

    # ─────────────────────────────────────────────────────────────
    # Read helpers — for the rare case a write path needs to look at
    # the canonical store directly (e.g. resolving caused_by chains).
    # Projection reads should use the projection tables directly.
    # ─────────────────────────────────────────────────────────────

    def read_event(self, event_idx: int) -> dict[str, Any] | None:
        cur = self._conn.cursor()
        row = cur.execute(
            "SELECT event_idx, event_kind, event_kind_version, user_id, "
            "       patient_hash, ts, payload_json, caused_by "
            "FROM twin_event_log WHERE event_idx = ?",
            (event_idx,),
        ).fetchone()
        if row is None:
            return None
        return {
            "event_idx":          row[0],
            "event_kind":         row[1],
            "event_kind_version": row[2],
            "user_id":            row[3],
            "patient_hash":       row[4],
            "ts":                 row[5],
            "payload":            json.loads(row[6]),
            "caused_by":          row[7],
        }

    def last_event_idx(self) -> int:
        cur = self._conn.cursor()
        row = cur.execute(
            "SELECT COALESCE(MAX(event_idx), 0) FROM twin_event_log"
        ).fetchone()
        return int(row[0])

    # ─────────────────────────────────────────────────────────────
    # Private
    # ─────────────────────────────────────────────────────────────

    def _check_privacy_invariants(
        self,
        kind: EventKind,
        payload: dict[str, Any],
    ) -> None:
        """Layer 2 belt-and-braces privacy enforcement.

        For ``practitioner_*`` events that go into the active facts table,
        the pattern_value_json must not contain patient_hash-shaped strings
        or specific ISO dates. The aggregation across patients is the
        primary de-identification mechanism; this is the secondary check.
        """
        # Layer 2 writes specifically — facts table fed by these events.
        if kind not in (
            EventKind.PRACTITIONER_FACT_CONFIRMED,
            EventKind.PRACTITIONER_CANDIDATE_SURFACED,
        ):
            return

        pattern_value = payload.get("pattern_value_json")
        if pattern_value is None:
            return

        text_repr = (
            json.dumps(pattern_value)
            if not isinstance(pattern_value, str)
            else pattern_value
        )

        if _HEX_HASH_PATTERN.search(text_repr):
            raise PrivacyInvariantViolation(
                f"Layer 2 write blocked: pattern_value_json for "
                f"{kind.value} contains 32+ hex chars (likely patient_hash). "
                f"Aggregation must strip patient identifiers."
            )
        if _ISO_DATE_PATTERN.search(text_repr):
            raise PrivacyInvariantViolation(
                f"Layer 2 write blocked: pattern_value_json for "
                f"{kind.value} contains an ISO date (likely re-identifying). "
                f"Patterns are dateless by design."
            )


# ─────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────

_LAST_TS_US = 0


def _monotonic_now_us() -> int:
    """Return current unix time in microseconds, monotonically nondecreasing.

    SQLite's ROWID is the autoincrement event_idx; ts is for forensic
    human-readable ordering. We monotonically clamp to handle the
    (rare) case of system clock going backwards by a small amount.
    """
    global _LAST_TS_US
    now = int(time.time() * 1_000_000)
    if now <= _LAST_TS_US:
        now = _LAST_TS_US + 1
    _LAST_TS_US = now
    return now
