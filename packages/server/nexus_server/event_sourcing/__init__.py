"""Event sourcing for Nexus memory (M0 foundation).

Per ADR-002 Rev-8 + Rev-9, the canonical store for all state mutations
in the Layer 1-4 memory architecture is ``event_log``. Every projection
table (clinical_graph_nodes, node_provenance, practitioner_facts, etc.)
is a materialised view derived from event_log by replay.

Public surface
--------------

The only legal mutation entry point is ``Store.emit_and_apply()``. CI
lint rules forbid any other code from issuing INSERT / UPDATE / DELETE
against projection tables. Reads go through the projection tables for
performance; writes go through the event log for truth.

::

    from nexus_server.event_sourcing import Store, EventKind

    store = Store(db)
    store.emit_and_apply(
        kind=EventKind.NODE_ADDED,
        payload={"node_type": "finding", "content": {...}},
        user_id="dr_chen",
        patient_hash="7a3f...",
    )

Replay
------

::

    from nexus_server.event_sourcing import replay

    replay(target_db, from_event_idx=0)
    # all projections rebuilt byte-identical (modulo schema version)

Golden test
-----------

::

    pytest tests/test_event_sourcing_replay.py
    # asserts deep equal against a recorded staging snapshot
"""

from nexus_server.event_sourcing.event_kinds import (
    EVENT_REGISTRY,
    EventKind,
    EventValidationError,
    validate_payload,
)
from nexus_server.event_sourcing.replay import (
    REPLAY_HANDLERS,
    register_handler,
    replay,
)
from nexus_server.event_sourcing.schema import (
    CANONICAL_SCHEMA_DDL,
    PROJECTION_SCHEMA_DDL,
    SCHEMA_VERSION,
    init_event_sourcing_schema,
)
from nexus_server.event_sourcing.store import (
    ProvenanceRequiredError,
    Store,
    StoreError,
    UnknownEventKindError,
)

__all__ = [
    "EventKind",
    "EVENT_REGISTRY",
    "validate_payload",
    "EventValidationError",
    "Store",
    "StoreError",
    "UnknownEventKindError",
    "ProvenanceRequiredError",
    "replay",
    "register_handler",
    "REPLAY_HANDLERS",
    "CANONICAL_SCHEMA_DDL",
    "PROJECTION_SCHEMA_DDL",
    "init_event_sourcing_schema",
    "SCHEMA_VERSION",
]
