"""User-facing agent state endpoints.

These are the shape that powers the desktop's redesigned sidebar:

  GET /api/v1/agent/state      → quick snapshot (counts + latest anchor)
  GET /api/v1/agent/timeline   → unified event stream for the activity panel
  GET /api/v1/agent/memories   → list of MemoryService-extracted memories

Each endpoint is read-only and authenticated; all heavy lifting (memory
extraction, anchoring, etc.) happens elsewhere — these are just views.
"""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from nexus_server.auth import get_current_user
from nexus_server.database import get_db_connection
from nexus_server.sync_anchor import list_anchors_for_user
from nexus_server import twin_event_log
from nexus_server import twin_manager

# Back-compat re-exports (S3 → S5 evolution): these used to live here
# as functions reading sync_events. After S5 they delegate to
# twin_event_log, which reads each user's per-twin SQLite directly.
# Kept as module-level bindings so any straggler callers still work.
list_memory_compacts = twin_event_log.list_memory_compacts
memory_compact_count = twin_event_log.memory_compact_count

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/agent", tags=["agent"])

# Secondary router for the legacy /api/v1/sync/anchors path. Phase B
# deleted ``sync_hub.py`` (which used to host /sync/push, /sync/pull,
# AND /sync/anchors); the first two are gone for good but /sync/anchors
# is still useful as a read-only view of historical anchor lifecycle.
# The path stays /api/v1/sync/anchors so existing desktop clients
# don't need to change.
sync_router = APIRouter(prefix="/api/v1/sync", tags=["sync"])


# ───────────────────────────────────────────────────────────────────────────
# Models
# ───────────────────────────────────────────────────────────────────────────


class MemoryEntry(BaseModel):
    """One memory_compact event surfaced as a "memory" snapshot.

    Aligns with SDK ARCHITECTURE.md — there's no separate memory store,
    every memory is a derived view of EventLog's memory_compact entries.
    The desktop renders these in the sidebar's Memories panel.
    """
    sync_id: int
    content: str
    first_sync_id: Optional[int]
    last_sync_id: Optional[int]
    event_count: int
    char_count: int
    created_at: str


class MemoriesResponse(BaseModel):
    memories: list[MemoryEntry]
    total: int


class AttachmentInfo(BaseModel):
    """Structured attachment metadata for chat history reload.
    Phase Q: replaces the old chip-prefix-in-text approach so the
    desktop can render proper chips instead of fallback text."""
    name: str
    mime: str = "application/octet-stream"
    size_bytes: int = 0


class ChatMessageView(BaseModel):
    """A single chat message as rendered by the desktop. Server is the
    single source of truth — desktop never persists messages locally
    after the thin-client refactor (Round 2).

    ``message_kind`` lets the client pick the right renderer:
      * ``"text"`` (default) — normal user / assistant text bubble
      * ``"workflow_run"`` — inline workflow card. ``workflow_run_id``
        in metadata points to a row in nexus_workflow_runs; the client
        polls /api/v1/workflows/runs/{id} for live updates.
    """
    role: str           # "user" | "assistant"
    content: str
    timestamp: str      # ISO-8601 (server_received_at)
    sync_id: int
    attachments: list[AttachmentInfo] = []
    message_kind: str = "text"                       # "text" | "workflow_run"
    metadata: dict = {}                              # event-type-specific payload


class ChatMessagesResponse(BaseModel):
    messages: list[ChatMessageView]
    total: int


class TimelineItem(BaseModel):
    """One row of the activity stream.

    ``kind`` distinguishes events the UI renders differently:
      - chat.user / chat.assistant  → conversation turns
      - file.attached / file.distilled → attachment lifecycle
      - memory.extracted            → MemoryService output
      - anchor.{status}             → sync_anchor lifecycle
    """
    kind: str
    timestamp: str
    summary: str
    sync_id: Optional[int] = None
    anchor_id: Optional[int] = None
    metadata: dict = {}


class TimelineResponse(BaseModel):
    items: list[TimelineItem]


class AgentStateSnapshot(BaseModel):
    """Quick state read for the sidebar header / counters.

    The anchor counters are the **union** of two sources:
      - legacy ``sync_anchors`` rows (pre-S4 history; never grows for
        new users after S4 retired the /sync/push enqueue path)
      - new ``twin_chain_events`` rows (post-S4, written by the chain
        activity log handler in :mod:`twin_manager` whenever twin's
        ChainBackend commits a BSC anchor)

    For chat-mode users today the meaningful signal lives in
    twin_chain_events; sync_anchors is only relevant for users whose
    accounts pre-date S4.
    """
    user_id: str
    chain_agent_id: Optional[int]
    chain_register_tx: Optional[str]
    network: str
    on_chain: bool
    memory_count: int
    anchored_count: int
    pending_anchor_count: int
    failed_anchor_count: int
    total_anchor_count: int
    last_anchor: Optional[dict] = None
    # Last chain activity (success OR failure) — surfaced so the
    # desktop top bar can show "Last write: 3s ago, ok" or
    # "Last write: 12s ago, failed: bucket missing" without a separate
    # round-trip.
    last_chain_event: Optional[dict] = None
    server_time: str


# ───────────────────────────────────────────────────────────────────────────
# Helpers
# ───────────────────────────────────────────────────────────────────────────


_EVENT_KIND_MAP = {
    "user_message": "chat.user",
    "assistant_response": "chat.assistant",
    "attachment_added": "file.attached",
    "attachment_distilled": "file.distilled",
    "memory_compact": "memory.compact",
}


def _truncate(s: str, n: int = 280) -> str:
    if not s:
        return ""
    return s if len(s) <= n else s[:n] + "…"


def _build_timeline(user_id: str, limit: int) -> list[TimelineItem]:
    """Merge twin event_log events + sync_anchors into a single
    chronological feed.

    S5: events come from the user's twin EventLog SQLite (read via
    ``twin_event_log.list_timeline_events``) instead of the legacy
    server-side sync_events mirror. Anchors continue to come from
    ``sync_anchors`` because that table is still the only place the
    legacy anchor lifecycle lives — its eventual replacement (chain-mode
    twin's ChainBackend) doesn't expose a per-user anchor history yet.

    We over-fetch (limit×2) from each source then merge and trim, so a
    burst of one kind doesn't starve the other. Newest first.
    """
    over = max(limit * 2, 50)
    items: list[TimelineItem] = []

    evt_rows = twin_event_log.list_timeline_events(user_id, over)

    with get_db_connection() as conn:
        # Anchor lifecycle (use updated_at since it captures status transitions)
        anch_rows = conn.execute(
            """
            SELECT anchor_id, content_hash, bsc_tx_hash, status,
                   first_sync_id, last_sync_id, event_count, retry_count,
                   updated_at
            FROM sync_anchors
            WHERE user_id = ?
            ORDER BY anchor_id DESC
            LIMIT ?
            """,
            (user_id, over),
        ).fetchall()

    for evt in evt_rows:
        sync_id = evt["sync_id"]
        et = evt["event_type"]
        content = evt["content"]
        meta = evt["metadata"]
        ts = evt["timestamp"]
        kind = _EVENT_KIND_MAP.get(et, f"event.{et}")
        if kind == "chat.user":
            summary = _truncate(content, 200)
        elif kind == "chat.assistant":
            summary = _truncate(content, 200)
        elif kind == "file.attached":
            summary = f"📎 {meta.get('name', 'file')} ({meta.get('size_bytes', 0)} bytes)"
        elif kind == "file.distilled":
            summary = f"💎 distilled {meta.get('name', 'file')} → {meta.get('summary_chars', 0)} chars"
        elif kind == "memory.compact":
            n_events = meta.get("event_count", "?")
            summary = f"🧠 Memory snapshot ({n_events} events) — {_truncate(content, 160)}"
        else:
            summary = _truncate(content, 200)
        items.append(TimelineItem(
            kind=kind,
            timestamp=ts,
            summary=summary,
            sync_id=sync_id,
            metadata=meta,
        ))

    # Twin chain events (post-S4 — captured by the logging handler in
    # twin_manager). Surface every entry so the user can see both
    # successful AND failed BSC anchors right in the activity feed
    # instead of having to dig through server logs.
    with get_db_connection() as conn:
        twin_rows = conn.execute(
            """
            SELECT event_id, kind, status, summary, tx_hash, content_hash,
                   object_path, error, duration_ms, created_at
            FROM twin_chain_events
            WHERE user_id = ?
            ORDER BY event_id DESC
            LIMIT ?
            """,
            (user_id, over),
        ).fetchall()
    for (eid, kind, status, summary, txh, chash, opath, err,
         dur_ms, ts) in twin_rows:
        if kind == "bsc_anchor" and status == "ok":
            kind_str = "anchor.committed"
            display = (
                f"🔗 BSC anchor committed — tx {txh[:10]+'…' if txh else '?'}"
            )
        elif kind == "bsc_anchor" and status == "failed":
            kind_str = "anchor.failed"
            display = f"✕ BSC anchor failed: {(err or '')[:120]}"
        else:
            # Legacy kinds (rows written before the object-storage
            # mirror was removed) fall through to a generic rendering.
            kind_str = f"chain.{kind}.{status}"
            display = summary or f"{kind} {status}"
        items.append(TimelineItem(
            kind=kind_str,
            timestamp=ts,
            summary=display,
            metadata={
                "tx_hash": txh,
                "content_hash": chash,
                "object_path": opath,
                "error": err,
                "duration_ms": dur_ms,
                "twin_event_id": eid,
            },
        ))

    for (aid, chash, txh, status, first_id, last_id, n, retry, ts) in anch_rows:
        if status == "anchored":
            summary = (
                f"🔗 anchored {n} event(s) — tx {txh[:10]+'…' if txh else '(no tx)'}"
            )
        elif status == "pending":
            summary = f"⏳ anchoring {n} event(s) (hash {chash[:8]}…)"
        elif status == "failed":
            summary = f"↻ anchor retry #{retry} (hash {chash[:8]}…)"
        elif status == "failed_permanent":
            summary = f"✕ anchor failed permanently (hash {chash[:8]}…)"
        elif status == "awaiting_registration":
            summary = f"⌛ anchor waiting for chain registration ({n} event(s))"
        elif status == "stored_only":
            summary = f"◐ stored locally — chain disabled ({n} event(s))"
        else:
            summary = f"anchor {status}: {chash[:8]}…"
        items.append(TimelineItem(
            kind=f"anchor.{status}",
            timestamp=ts,
            summary=summary,
            anchor_id=aid,
            metadata={
                "content_hash": chash,
                "bsc_tx_hash": txh,
                "first_sync_id": first_id,
                "last_sync_id": last_id,
                "event_count": n,
                "retry_count": retry,
            },
        ))

    items.sort(key=lambda i: i.timestamp, reverse=True)
    return items[:limit]


def _anchor_status_counts(user_id: str) -> dict[str, int]:
    """Legacy sync_anchors counters (pre-S4 history only)."""
    with get_db_connection() as conn:
        rows = conn.execute(
            "SELECT status, COUNT(*) FROM sync_anchors WHERE user_id = ? "
            "GROUP BY status",
            (user_id,),
        ).fetchall()
    return {r[0]: int(r[1]) for r in rows}


def _twin_chain_event_counts(user_id: str) -> dict[str, int]:
    """Bug 3: counts of chain writes captured from twin's ChainBackend.

    Returns ``{"bsc_anchor_ok", "bsc_anchor_failed", ...}`` keys
    (``{kind}_{status}``), each an int. Empty/zero for users whose
    twin hasn't written anything yet.
    """
    with get_db_connection() as conn:
        rows = conn.execute(
            "SELECT kind, status, COUNT(*) FROM twin_chain_events "
            "WHERE user_id = ? GROUP BY kind, status",
            (user_id,),
        ).fetchall()
    out: dict[str, int] = {}
    for kind, status, n in rows:
        out[f"{kind}_{status}"] = int(n)
    return out


def _last_twin_chain_event(user_id: str, *, status: Optional[str] = None) -> Optional[dict]:
    """Newest twin_chain_events row, optionally filtered by status."""
    where = "user_id = ?"
    params: list = [user_id]
    if status is not None:
        where += " AND status = ?"
        params.append(status)
    with get_db_connection() as conn:
        row = conn.execute(
            f"""
            SELECT event_id, kind, status, summary, tx_hash, content_hash,
                   object_path, error, duration_ms, created_at
            FROM twin_chain_events
            WHERE {where}
            ORDER BY event_id DESC
            LIMIT 1
            """,
            params,
        ).fetchone()
    if row is None:
        return None
    return {
        "event_id": int(row[0]),
        "kind": row[1],
        "status": row[2],
        "summary": row[3] or "",
        "tx_hash": row[4],
        "content_hash": row[5],
        "object_path": row[6],
        "error": row[7],
        "duration_ms": row[8],
        "created_at": row[9],
    }


# ───────────────────────────────────────────────────────────────────────────
# Routes
# ───────────────────────────────────────────────────────────────────────────


@router.get("/state", response_model=AgentStateSnapshot)
async def get_agent_state(
    current_user: str = Depends(get_current_user),
) -> AgentStateSnapshot:
    """One-shot read for the sidebar."""
    from datetime import datetime, timezone
    from nexus_server.config import get_config
    config = get_config()

    with get_db_connection() as conn:
        urow = conn.execute(
            "SELECT chain_agent_id, chain_register_tx FROM users WHERE id = ?",
            (current_user,),
        ).fetchone()
    chain_agent_id = urow[0] if urow else None
    register_tx = urow[1] if urow else None

    legacy = _anchor_status_counts(current_user)
    twin_counts = _twin_chain_event_counts(current_user)

    # Sum legacy + new sources so the UI sees one unified counter
    # regardless of which path produced the anchor. After S4 the legacy
    # bucket only carries pre-existing history; new chat traffic flows
    # through twin_chain_events.
    anchored = (
        legacy.get("anchored", 0)
        + twin_counts.get("bsc_anchor_ok", 0)
    )
    # No "pending" semantic in twin_chain_events (chain writes are
    # synchronous w.r.t. the BSC tx receipt). Pending only reflects
    # legacy rows.
    pending = (
        legacy.get("pending", 0)
        + legacy.get("awaiting_registration", 0)
    )
    failed = (
        legacy.get("failed", 0)
        + legacy.get("failed_permanent", 0)
        + twin_counts.get("bsc_anchor_failed", 0)
    )
    total = (
        sum(legacy.values())
        + sum(twin_counts.values())
    )

    anchors = list_anchors_for_user(current_user, limit=1)
    last_anchor = anchors[0] if anchors else None
    last_chain_event = _last_twin_chain_event(current_user)

    return AgentStateSnapshot(
        user_id=current_user,
        chain_agent_id=chain_agent_id,
        chain_register_tx=register_tx,
        network=config.NEXUS_NETWORK,
        on_chain=chain_agent_id is not None,
        memory_count=memory_compact_count(current_user),
        anchored_count=anchored,
        pending_anchor_count=pending,
        failed_anchor_count=failed,
        total_anchor_count=total,
        last_anchor=last_anchor,
        last_chain_event=last_chain_event,
        server_time=datetime.now(timezone.utc).isoformat(),
    )


@router.get("/timeline", response_model=TimelineResponse)
async def get_timeline(
    limit: int = 60,
    current_user: str = Depends(get_current_user),
) -> TimelineResponse:
    """Newest-first activity stream."""
    if limit <= 0 or limit > 200:
        limit = 60
    return TimelineResponse(items=_build_timeline(current_user, limit))


@router.get("/messages", response_model=ChatMessagesResponse)
async def get_messages(
    limit: int = 200,
    before_sync_id: Optional[int] = None,
    session_id: Optional[str] = None,
    current_user: str = Depends(get_current_user),
) -> ChatMessagesResponse:
    """Server-authoritative chat history.

    Replaces the desktop's old LocalEventLog (Round 2 thin-client
    refactor): the desktop pulls history from here on every login and
    renders messages from this stream alone.

    S5: the source of truth pivoted from server-side sync_events (a
    mirror) to twin's per-user EventLog SQLite (the original).
    ``twin_event_log.list_messages`` opens that file read-only so this
    endpoint never has to instantiate a DigitalTwin to serve a state
    read.

    Returns oldest first within the requested window. ``before_sync_id``
    is the pagination cursor — it maps to the EventLog's ``idx``.

    ``session_id`` (multi-session, see ``sessions_router``):
      * omitted — return all messages across every thread. Used by
        tools that don't care about thread boundaries (audit, dump).
      * ``""``  — only the synthetic default session (rows with empty
        session_id, i.e. pre-multi-session chat history).
      * any other — only that named session's messages.
    """
    if limit <= 0 or limit > 500:
        limit = 200

    raw_messages, total = twin_event_log.list_messages(
        current_user, limit,
        before_idx=before_sync_id,
        session_id=session_id,
    )
    messages = [
        ChatMessageView(
            role=m["role"],
            content=m["content"],
            timestamp=m["timestamp"],
            sync_id=m["sync_id"],
            attachments=[
                AttachmentInfo(**a) for a in (m.get("attachments") or [])
                if isinstance(a, dict) and a.get("name")
            ],
        )
        for m in raw_messages
    ]
    return ChatMessagesResponse(messages=messages, total=total)


@router.get("/memories", response_model=MemoriesResponse)
async def get_memories(
    limit: int = 50,
    current_user: str = Depends(get_current_user),
) -> MemoriesResponse:
    """List MemoryService-extracted memories, newest first."""
    if limit <= 0 or limit > 200:
        limit = 50
    rows = list_memory_compacts(current_user, limit=limit)
    return MemoriesResponse(
        memories=[MemoryEntry(**r) for r in rows],
        total=memory_compact_count(current_user),
    )


# ── Phase J: typed memory namespaces (BEP-Nexus §3.3) ──────────────


class NamespaceSummary(BaseModel):
    """Compact summary for the desktop sidebar's Memory tab."""
    name: str                         # "episodes" | "facts" | "skills" | "persona" | "knowledge"
    item_count: int
    current_version: Optional[str]    # VersionedStore label, or None if never committed
    version_count: int                # how many committed snapshots exist


class NamespacesResponse(BaseModel):
    """Aggregated read across all 5 typed namespaces."""
    namespaces: list[NamespaceSummary]
    # Optional rich payload for clients that want detail without
    # making 5 separate calls. Keys mirror the namespace name; values
    # are lists of dicts (each store's ``to_dict`` shape).
    items: dict[str, list[dict]]


class SyncStatus(BaseModel):
    """Per-path sync state.

    The chain backend's data plane writes synchronously to its local
    store, so there is never a pending write queue any more. The
    shape is kept (empty) for client compatibility; the fields will
    become meaningful again when the S3-compatible mirror lands.
    """
    pending_paths: list[str]
    wal_entry_count: int


@router.get("/sync_status", response_model=SyncStatus)
async def get_sync_status(
    current_user: str = Depends(get_current_user),
) -> SyncStatus:
    """Report pending storage writes.

    ChainBackend persists data synchronously to its local store, so
    this always reports an empty queue today. Kept for the desktop's
    Work Directory badge polling; it will report real queue state
    again once a remote object-storage mirror is reintroduced.
    """
    # Touch the twin so lazy creation still happens on first poll
    # (previous behaviour) — cheap once the twin is cached.
    await twin_manager.get_twin(current_user)
    return SyncStatus(
        pending_paths=[],
        wal_entry_count=0,
    )


@router.get("/memory/namespaces", response_model=NamespacesResponse)
async def get_memory_namespaces(
    include_items: bool = True,
    items_limit: int = 50,
    current_user: str = Depends(get_current_user),
) -> NamespacesResponse:
    """Read the 5 Phase J typed namespaces for the current user's twin.

    Returns per-namespace counts + (optionally) the items themselves so
    the desktop Memory panel can render typed views without making
    five separate calls.

    Notes:
      * ``persona`` returns ``item_count == version_count`` because
        every persona update IS a new version (no working file).
      * ``items_limit`` caps each list (newest-or-recent-first) to keep
        the response bounded.
      * Errors when reading a namespace are isolated — a corrupt store
        returns an empty list with a logged warning instead of failing
        the whole request.
    """
    if items_limit <= 0 or items_limit > 500:
        items_limit = 50

    twin = await twin_manager.get_twin(current_user)

    summaries: list[NamespaceSummary] = []
    items: dict[str, list[dict]] = {}

    # Layout matches the field names assigned in DigitalTwin._initialize.
    # ``persona`` is special-cased below.
    namespace_specs = [
        ("episodes",  "episodes",      lambda s: [e.to_dict() for e in s.recent(limit=items_limit)] if hasattr(s, "recent") else [e.to_dict() for e in s.all()][-items_limit:]),
        ("facts",     "facts",         lambda s: [f.to_dict() for f in s.all()][-items_limit:]),
        ("skills",    "skills_memory", lambda s: [sk.to_dict() for sk in s.all()][-items_limit:]),
        ("knowledge", "knowledge",     lambda s: [a.to_dict() for a in s.all()][-items_limit:]),
    ]

    for label, attr, lister in namespace_specs:
        store = getattr(twin, attr, None)
        if store is None:
            continue
        try:
            all_items = lister(store)
            history = store.history()
            summaries.append(NamespaceSummary(
                name=label,
                item_count=len(store.all()),
                current_version=store.current_version(),
                version_count=len(history),
            ))
            if include_items:
                items[label] = all_items
        except Exception as e:  # noqa: BLE001
            logger.warning("namespace %s read failed: %s", label, e)
            summaries.append(NamespaceSummary(
                name=label, item_count=0, current_version=None, version_count=0,
            ))
            if include_items:
                items[label] = []

    # Persona — every version IS a snapshot; "items" is the version
    # history rendered as dicts so the UI can show a timeline.
    persona = getattr(twin, "persona_store", None)
    if persona is not None:
        try:
            history = persona.history()
            current = persona.current()
            summaries.append(NamespaceSummary(
                name="persona",
                item_count=len(history),
                current_version=persona.current_version(),
                version_count=len(history),
            ))
            if include_items:
                items["persona"] = history[:items_limit]
        except Exception as e:  # noqa: BLE001
            logger.warning("namespace persona read failed: %s", e)
            summaries.append(NamespaceSummary(
                name="persona", item_count=0, current_version=None, version_count=0,
            ))
            if include_items:
                items["persona"] = []

    return NamespacesResponse(namespaces=summaries, items=items)


# ── Brain panel chain status (Phase D 续) ────────────────────────────


class NamespaceChainStatus(BaseModel):
    """Per-namespace on-chain mirror state.

    ``status`` is one of:
      * ``"local"`` — committed locally; not yet persisted by the
        chain backend
      * ``"mirrored"`` — the chain backend stored the blob; the agent's
        on-chain state_root has NOT been re-anchored since the last
        commit (so chain readers will not yet see this version)
      * ``"anchored"`` — ``last_anchor_at >= last_commit_at``; this
        version is part of the on-chain state root
    """
    namespace: str
    status: str
    version: Optional[str] = None
    last_commit_at: Optional[float] = None
    last_anchor_at: Optional[float] = None
    mirrored: bool = False


class ChainHealthCard(BaseModel):
    bsc_ready: bool = False
    # BSC anchor observability. Previously bsc_ready was
    # `chain_client is not None`, so the dot stayed green even while
    # every anchor call was reverting (RPC down, nonce stuck, gas
    # exhausted). Now bsc_ready flips false on a recent failure and
    # last_bsc_anchor_error carries the reason.
    bsc_failure_active: bool = False
    last_bsc_anchor_error: Optional[dict] = None
    # All fields default to safe values so older snapshot shapes
    # still deserialize.


class ChainStatusResponse(BaseModel):
    """Brain panel's "is my data permanent?" data model.

    Each of the 5 typed namespaces has a 3-state status; the
    ``health`` card surfaces backend-level signals so the user can
    distinguish "agent is busy mirroring" from "agent's chain
    backend is broken".
    """
    namespaces: list[NamespaceChainStatus]
    health: ChainHealthCard


@router.get("/chain_status", response_model=ChainStatusResponse)
async def get_chain_status(
    current_user: str = Depends(get_current_user),
) -> ChainStatusResponse:
    """Brain panel: per-namespace mirror+anchor state + chain health.

    Reads:
      * ``store._versioned.chain_status(last_anchor_at)`` for each
        of the 5 typed namespaces
      * ``rune._backend.chain_health_snapshot()`` for the bottom
        Chain Health card

    Falls back to ``local`` for every namespace if the backend
    isn't a chain-aware ChainBackend (e.g. local LocalBackend).
    """
    twin = await twin_manager.get_twin(current_user)
    backend = getattr(twin.rune, "_backend", None)

    last_anchor: Optional[float] = None
    health_dict: dict = {
        "bsc_ready": False,
    }
    if backend is not None:
        try:
            last_anchor = backend.last_anchor_at(twin.config.agent_id)
        except Exception:
            last_anchor = None
        try:
            health_dict = backend.chain_health_snapshot()
        except Exception:
            pass

    namespace_specs = [
        ("persona",   "persona_store"),
        ("knowledge", "knowledge"),
        ("skills",    "skills_memory"),
        ("facts",     "facts"),
        ("episodes",  "episodes"),
    ]

    rows: list[NamespaceChainStatus] = []
    for label, attr in namespace_specs:
        store = getattr(twin, attr, None)
        if store is None:
            rows.append(NamespaceChainStatus(namespace=label, status="local"))
            continue
        # Reach into the underlying VersionedStore via the typed
        # store's _versioned attribute (consistent across all 5).
        versioned = getattr(store, "_versioned", None)
        if versioned is None:
            rows.append(NamespaceChainStatus(namespace=label, status="local"))
            continue
        try:
            s = versioned.chain_status(last_anchor_at=last_anchor)
            rows.append(NamespaceChainStatus(
                namespace=label,
                status=s["status"],
                version=s.get("version"),
                last_commit_at=s.get("last_commit_at"),
                last_anchor_at=s.get("last_anchor_at"),
                mirrored=bool(s.get("mirrored", False)),
            ))
        except Exception as e:
            logger.warning("chain_status %s read failed: %s", label, e)
            rows.append(NamespaceChainStatus(namespace=label, status="local"))

    return ChainStatusResponse(
        namespaces=rows,
        health=ChainHealthCard(**health_dict),
    )


# ── Installed skills (external SKILL.md tools) ──────────────────────
#
# IMPORTANT: This is the list of EXTERNAL skills the agent has
# `manage_skill install`'d (think `pip install` for capabilities) —
# things like the pdf / xlsx / docx skills from Anthropic's marketplace
# or LobeHub. It is NOT the same as Brain panel's "Heuristics" card,
# which is the strategies the SkillEvolver learned from chat history
# and lives in namespaces/skills/v{N}.json in the agent's store.
#
# The two are distinct concepts:
#   * Heuristics (= internal strategies, learned)  — agent's reflexes
#   * Skills     (= external tool packages, installed) — agent's tools
#
# The historical "skills" namespace name on chain points at Heuristics
# for back-compat; UI labels the right thing.


class InstalledSkillSummary(BaseModel):
    """One row in the desktop's INSTALLED SKILLS panel."""
    name: str
    title: str = ""
    description: str = ""
    version: str = ""
    author: str = ""
    # `path` deliberately omitted — exposing a /data/.../skills/<name>
    # filesystem path to the desktop client serves no purpose and
    # leaks server-side layout.
    has_references: bool = False


class InstalledSkillsResponse(BaseModel):
    skills: list[InstalledSkillSummary]
    total: int


@router.get("/skills", response_model=InstalledSkillsResponse)
async def get_installed_skills(
    current_user: str = Depends(get_current_user),
) -> InstalledSkillsResponse:
    """List externally-installed skills for the desktop's INSTALLED
    SKILLS panel.

    Reads from ``twin.skills.installed`` (a SkillManager instance
    populated lazily as the agent calls ``manage_skill install``).
    Falls back to an empty list if the twin is in local mode or
    SkillManager is unavailable — the desktop renders the panel
    empty rather than crashing.
    """
    twin = await twin_manager.get_twin(current_user)
    mgr = getattr(twin, "skills", None)
    if mgr is None:
        return InstalledSkillsResponse(skills=[], total=0)
    rows: list[InstalledSkillSummary] = []
    try:
        for s in mgr.installed:
            rows.append(InstalledSkillSummary(
                name=getattr(s, "name", "") or "",
                title=getattr(s, "title", "") or "",
                description=getattr(s, "description", "") or "",
                version=getattr(s, "version", "") or "",
                author=getattr(s, "author", "") or "",
                has_references=bool(getattr(s, "references", None)),
            ))
    except Exception as e:
        logger.warning("get_installed_skills failed: %s", e)
    return InstalledSkillsResponse(skills=rows, total=len(rows))


# ── Chain operations log — every BSC anchor attempt with status ──


class ChainEvent(BaseModel):
    """One row from ``twin_chain_events``.

    Statuses (set by twin_manager._ChainActivityLogHandler from log
    line regexes):
      * ``ok``        — the write actually landed on chain.
      * ``degraded``  — legacy status from the removed object-storage
                        mirror; may appear on historical rows.
      * ``failed``    — the chain write did not land.

    The desktop's chain log panel renders these three differently
    (green / amber / red) so the operator can audit recent activity
    without SSH-ing into the server to query the SQLite table.
    """
    kind: str          # "bsc_anchor" (legacy rows may carry other kinds)
    status: str        # "ok" | "degraded" | "failed"
    summary: str = ""
    tx_hash: Optional[str] = None
    content_hash: Optional[str] = None
    object_path: Optional[str] = None
    error: Optional[str] = None
    duration_ms: Optional[int] = None
    created_at: str = ""


class ChainEventsResponse(BaseModel):
    events: list[ChainEvent]
    total_returned: int


@router.get("/chain_events", response_model=ChainEventsResponse)
async def get_chain_events(
    limit: int = 20,
    current_user: str = Depends(get_current_user),
) -> ChainEventsResponse:
    """Recent chain operations for this user, newest first.

    Used by the desktop's "Chain Operations" log to show the last N
    BSC anchors with their status. Replaces the old
    workflow of "SSH to the server and SELECT from twin_chain_events"
    every time something looks off.

    Limit is capped at 200 so a runaway request can't ask for the whole
    table — that's what /admin tools are for.
    """
    from nexus_server.database import get_db_connection
    capped = max(1, min(int(limit or 20), 200))
    rows: list[ChainEvent] = []
    try:
        with get_db_connection() as conn:
            cursor = conn.execute(
                """
                SELECT kind, status, summary, tx_hash, content_hash,
                       object_path, error, duration_ms, created_at
                FROM twin_chain_events
                WHERE user_id = ?
                ORDER BY event_id DESC
                LIMIT ?
                """,
                (current_user, capped),
            )
            for r in cursor.fetchall():
                rows.append(ChainEvent(
                    kind=r[0] or "",
                    status=r[1] or "",
                    summary=r[2] or "",
                    tx_hash=r[3],
                    content_hash=r[4],
                    object_path=r[5],
                    error=r[6],
                    duration_ms=r[7],
                    created_at=r[8] or "",
                ))
    except Exception as e:
        logger.warning("chain_events read failed: %s", e)
    return ChainEventsResponse(events=rows, total_returned=len(rows))


# ── Phase O.5: Evolution timeline (BEP-Nexus §3.4) ──────────────────


class EvolutionEvent(BaseModel):
    """One row of the evolution timeline.

    ``kind`` is the EventLog event_type: "evolution_proposal" /
    "evolution_verdict" / "evolution_revert". Clients render each
    differently — proposals as pending edits, verdicts as outcomes,
    reverts as red rollback markers.
    """
    index: int
    timestamp: float
    kind: str
    edit_id: str
    evolver: str = ""
    target_namespace: str = ""
    decision: Optional[str] = None       # only on verdicts
    change_summary: str = ""
    content: str = ""
    metadata: dict


class EvolutionTimelineResponse(BaseModel):
    proposals: int
    verdicts: int
    reverts: int
    events: list[EvolutionEvent]
    pending: list[str]  # edit_ids with proposal but no verdict yet


@router.get("/evolution/verdicts", response_model=EvolutionTimelineResponse)
async def get_evolution_verdicts(
    limit: int = 100,
    current_user: str = Depends(get_current_user),
) -> EvolutionTimelineResponse:
    """Read the user's twin's falsifiable-evolution timeline.

    Each Phase O.2 evolver run emits an ``evolution_proposal`` into
    the EventLog before its write. After the proposal's window
    elapses, Phase O.4's VerdictRunner (wired into the twin's
    compaction loop in Phase O.5) writes back an
    ``evolution_verdict`` and — when the decision is "reverted" —
    an ``evolution_revert`` row plus a real namespace store rollback.

    This endpoint surfaces all three event kinds so the desktop's
    Evolution panel can render the timeline. ``pending`` lists the
    edit_ids that have a proposal but no verdict yet — those are
    the rows the UI marks as "in observation window".
    """
    if limit <= 0 or limit > 500:
        limit = 100

    twin = await twin_manager.get_twin(current_user)
    event_log = getattr(twin, "event_log", None)
    if event_log is None:
        return EvolutionTimelineResponse(
            proposals=0, verdicts=0, reverts=0, events=[], pending=[],
        )

    # Pull more than the limit so we can index proposal/verdict pairs
    # before truncating. The full scan is bounded — twin event logs
    # are local SQLite, this is cheap.
    raw = event_log.recent(limit=max(limit * 4, 400))
    raw = [e for e in raw if e.event_type in (
        "evolution_proposal", "evolution_verdict", "evolution_revert",
    )]
    raw.sort(key=lambda e: e.index)

    # Index settled edit_ids
    settled: set[str] = set()
    for e in raw:
        if e.event_type == "evolution_verdict":
            eid = (e.metadata or {}).get("edit_id")
            if eid:
                settled.add(eid)

    proposal_ids: set[str] = set()
    out: list[EvolutionEvent] = []
    proposals = verdicts = reverts = 0
    for e in raw:
        md = e.metadata or {}
        edit_id = md.get("edit_id", "")
        kind = e.event_type
        decision = None
        if kind == "evolution_proposal":
            proposals += 1
            if edit_id:
                proposal_ids.add(edit_id)
        elif kind == "evolution_verdict":
            verdicts += 1
            decision = md.get("decision")
        elif kind == "evolution_revert":
            reverts += 1

        out.append(EvolutionEvent(
            index=e.index,
            timestamp=e.timestamp,
            kind=kind,
            edit_id=edit_id,
            evolver=md.get("evolver", ""),
            target_namespace=md.get("target_namespace", ""),
            decision=decision,
            change_summary=md.get("change_summary", ""),
            content=e.content,
            metadata=md,
        ))

    # Newest first, then truncate to caller's limit.
    out.sort(key=lambda x: x.index, reverse=True)
    out = out[:limit]
    pending = sorted(proposal_ids - settled)

    return EvolutionTimelineResponse(
        proposals=proposals,
        verdicts=verdicts,
        reverts=reverts,
        events=out,
        pending=pending,
    )


class EvolutionDecisionResult(BaseModel):
    """Outcome of a manual approve / revert action."""
    edit_id: str
    decision: str                 # "kept" | "reverted"
    rolled_back_from: str = ""
    rolled_back_to: str = ""
    target_namespace: str = ""
    note: str = ""


def _find_proposal(event_log, edit_id: str):
    """Locate the proposal event whose metadata.edit_id matches.

    Returns the EvolutionProposal dataclass + the raw Event row,
    or (None, None) when no match. Searches recent events first
    so the common case (just-emitted proposal) is fast.
    """
    from nexus.evolution.verdict_runner import _proposal_from_event

    raw = event_log.recent(limit=500)
    raw.sort(key=lambda e: e.index, reverse=True)
    for e in raw:
        if e.event_type != "evolution_proposal":
            continue
        if (e.metadata or {}).get("edit_id") == edit_id:
            p = _proposal_from_event(e)
            if p is not None:
                return p, e
    return None, None


def _already_settled(event_log, edit_id: str) -> bool:
    """True if any evolution_verdict / evolution_revert event already
    exists for this edit_id — manual decisions are idempotent."""
    raw = event_log.recent(limit=500)
    for e in raw:
        if e.event_type in ("evolution_verdict", "evolution_revert"):
            if (e.metadata or {}).get("edit_id") == edit_id:
                return True
    return False


def _resolve_store(twin, target_namespace: str):
    """Map a proposal's target_namespace string → the live store on
    the twin. Mirrors the wiring in VerdictRunner; kept local to
    avoid coupling the endpoint to the runner's internals."""
    mapping = {
        "memory.persona": getattr(twin, "persona_store", None),
        "memory.facts": getattr(twin, "facts", None),
        "memory.episodes": getattr(twin, "episodes", None),
        "memory.skills": getattr(twin, "skills_memory", None),
        "memory.knowledge": getattr(twin, "knowledge", None),
    }
    return mapping.get(target_namespace)


@router.post(
    "/evolution/{edit_id}/revert",
    response_model=EvolutionDecisionResult,
)
async def manual_revert_proposal(
    edit_id: str,
    current_user: str = Depends(get_current_user),
) -> EvolutionDecisionResult:
    """User-initiated rollback for a specific evolution proposal.

    Bypasses the verdict window — the user has decided this edit
    was bad, regardless of what the scorer would have said. Writes
    an ``evolution_verdict`` (decision=reverted) followed by an
    ``evolution_revert`` event with ``trigger="manual"``, and rolls
    the namespace store back to the proposal's ``rollback_pointer``
    when one is wired in. Idempotent — calling on an already-
    settled proposal returns the prior decision without re-firing
    side effects.
    """
    from fastapi import HTTPException
    from nexus_core.evolution import (
        EvolutionVerdict, EvolutionRevert,
    )

    twin = await twin_manager.get_twin(current_user)
    event_log = getattr(twin, "event_log", None)
    if event_log is None:
        raise HTTPException(status_code=503, detail="twin event log unavailable")

    proposal, _ = _find_proposal(event_log, edit_id)
    if proposal is None:
        raise HTTPException(status_code=404, detail=f"no proposal for {edit_id!r}")

    if _already_settled(event_log, edit_id):
        return EvolutionDecisionResult(
            edit_id=edit_id,
            decision="reverted",
            target_namespace=proposal.target_namespace,
            note="already settled (idempotent)",
        )

    # 1. Write verdict event with manual reverted decision.
    verdict = EvolutionVerdict(
        edit_id=edit_id,
        verdict_at_event=0,
        events_observed=0,
        decision="reverted",
    )
    event_log.append(
        event_type="evolution_verdict",
        content=f"manual revert for {edit_id}",
        metadata=verdict.to_event_metadata(),
    )

    # 2. Try the actual rollback. Best-effort: log + record empty
    # rolled_back_from when no store is wired in.
    rolled_from = ""
    rolled_to = proposal.rollback_pointer or ""
    store = _resolve_store(twin, proposal.target_namespace)
    if store is not None and rolled_to and rolled_to != "(uncommitted)":
        try:
            rolled_from = store.current_version() or ""
            store.rollback(rolled_to)
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "manual revert rollback failed for %s → %s: %s",
                proposal.target_namespace, rolled_to, e,
            )
            rolled_from = ""

    # 3. Write the revert event so the timeline endpoint shows
    # the user-driven action.
    revert = EvolutionRevert(
        edit_id=edit_id,
        rolled_back_to=rolled_to,
        rolled_back_from=rolled_from,
        trigger="manual",
        evidence=f"user {current_user} requested revert",
    )
    event_log.append(
        event_type="evolution_revert",
        content=f"manual revert {edit_id}: {rolled_from} → {rolled_to}",
        metadata=revert.to_event_metadata(),
    )

    return EvolutionDecisionResult(
        edit_id=edit_id,
        decision="reverted",
        rolled_back_from=rolled_from,
        rolled_back_to=rolled_to,
        target_namespace=proposal.target_namespace,
        note="manual revert applied",
    )


class ThinkingStep(BaseModel):
    """One row of the agent's inner-monologue / thinking trace.

    Synthesised from the twin's EventLog by filtering to the event
    types that constitute a "thinking step" (vs storage churn or
    chain anchoring). Each row carries a stable ``kind`` the UI uses
    to pick an icon + label, plus the original event content.
    """
    sync_id: int
    timestamp: str
    kind: str           # "heard" | "checked" | "recalled" | "decided" |
                        # "responded" | "violated" | "compacted" |
                        # "evolving" | "evolved" | "reverted"
    label: str
    content: str
    metadata: dict


class ThinkingResponse(BaseModel):
    steps: list[ThinkingStep]
    total: int


# Maps EventLog event_type → (UI kind, friendly label).
# Anything not in this map is filtered out — the thinking panel is
# deliberately narrow, only showing turns of the agent's reasoning
# rather than every storage-side event.
_THINKING_MAP: dict[str, tuple[str, str]] = {
    "user_message":        ("heard",     "Heard the user say"),
    "contract_check":      ("checked",   "Ran a safety check"),
    "memory_compact":      ("compacted", "Compacted memory"),
    "memory_extract":      ("recalled",  "Extracted memories from this turn"),
    "memory_extracted":    ("recalled",  "Stored a memory"),
    "memory_stored":       ("recalled",  "Stored a memory"),
    "skill_learned":       ("decided",   "Learned a skill"),
    "persona_evolved":     ("evolved",   "Evolved persona"),
    "persona_reflect":     ("evolving",  "Reflecting on persona"),
    "evolution_proposal":  ("evolving",  "Proposed an edit (pending verdict)"),
    "evolution_verdict":   ("decided",   "Verdict on a recent edit"),
    "evolution_revert":    ("reverted",  "Reverted a bad edit"),
    "contract_violation":  ("violated",  "Caught a contract violation"),
    "assistant_response":  ("responded", "Replied"),
}


# ── Phase C: Evolution Pressure dashboard ──────────────────────────


class EvolutionPressureItem(BaseModel):
    """One evolver's pressure gauge reading.

    Mirrors the shape returned by each evolver's ``pressure_state()``
    method (Phase C1 of the Pressure Dashboard). Renders directly in
    the desktop's gauges segment of the cognition panel:

      * ``accumulator / threshold`` → progress bar fill ratio
      * ``status`` → idle | warming | ready | live | fired_recently
      * ``fed_by`` → arrows in the lineage view drawn between layers
      * ``details`` → free-form, evolver-specific extras the lineage
        card may surface (e.g. per-topic SkillEvolver counters,
        days_since for PersonaEvolver, current event count for
        EventLogCompactor)
    """
    evolver: str
    layer: str                          # "L0" / "L1" / "L2" / "L4"
    accumulator: float
    threshold: float
    unit: str
    status: str
    fed_by: list[str]
    last_fired_at: Optional[float] = None
    details: dict = {}


class EvolutionVerdictItem(BaseModel):
    """One verdict event for the Pressure Dashboard's verdict feed.

    Phase D 续 / #159: surface the kept-vs-reverted decision + the
    verdict's reasoning (regression / drift) inline in the UI so
    the user can see what the agent rejected and why.
    """
    edit_id: str
    evolver: str
    target_namespace: str
    decision: str                       # "kept" | "reverted" | "(unknown)"
    timestamp: float
    regression_score: float = 0.0
    abc_drift_delta: float = 0.0
    evidence: str = ""
    change_summary: str = ""


class EvolutionPressureResponse(BaseModel):
    """Aggregated pressure dashboard payload — one row per evolver
    + a 24h evolution histogram so the UI can show the
    "pyramid shape" of the agent's growth (slow at top, busy at
    bottom).

    ``histogram_24h`` is a dict keyed by evolver name; each value
    is a 24-element list (one per hour, oldest first) of fire
    counts. Allows a sparkline render without per-evolver round-
    trips.

    ``recent_verdicts`` is the last N verdict events (Phase D 续 /
    #159), newest-first, for the dashboard's verdict feed.
    """
    evolvers: list[EvolutionPressureItem]
    histogram_24h: dict[str, list[int]]
    recent_verdicts: list[EvolutionVerdictItem] = []


def _aggregate_evolution_histogram(event_log) -> dict[str, list[int]]:
    """Bucket evolution_verdict events into 24 hourly buckets per
    evolver. Cheap full-scan over the bounded recent window.

    The histogram is intentionally rough — point of the
    visualization is "shape" not exact counts."""
    import time as _time
    now = _time.time()
    cutoff = now - 24 * 3600
    out: dict[str, list[int]] = {}
    try:
        rows = event_log.recent(limit=1000)
    except Exception:
        return {}
    for ev in rows:
        if ev.event_type != "evolution_verdict":
            continue
        if ev.timestamp < cutoff:
            continue
        md = ev.metadata or {}
        # The verdict event references its proposal — we look up
        # the evolver from the matching proposal's metadata. If we
        # can't resolve it (corrupt row, replay edge case), bucket
        # under "Unknown" so the histogram still shows the firing.
        evolver = ""
        edit_id = md.get("edit_id")
        if edit_id:
            for prop in rows:
                if (
                    prop.event_type == "evolution_proposal"
                    and (prop.metadata or {}).get("edit_id") == edit_id
                ):
                    evolver = (prop.metadata or {}).get("evolver", "") or ""
                    break
        evolver = evolver or "Unknown"
        bucket_idx = int((ev.timestamp - cutoff) // 3600)
        bucket_idx = max(0, min(23, bucket_idx))
        if evolver not in out:
            out[evolver] = [0] * 24
        out[evolver][bucket_idx] += 1
    return out


def _aggregate_verdict_feed(event_log, limit: int = 10) -> list[EvolutionVerdictItem]:
    """Pull the last N verdict events out of the EventLog, attach
    each to its proposal so the UI shows what was being decided.

    Returns newest-first.
    """
    try:
        rows = event_log.recent(limit=500)
    except Exception:
        return []
    rows = sorted(rows, key=lambda e: e.index)

    proposals: dict[str, dict] = {}
    for ev in rows:
        if ev.event_type == "evolution_proposal":
            md = ev.metadata or {}
            edit_id = md.get("edit_id")
            if edit_id:
                proposals[edit_id] = md

    out: list[EvolutionVerdictItem] = []
    for ev in reversed(rows):  # newest first
        if ev.event_type != "evolution_verdict":
            continue
        md = ev.metadata or {}
        edit_id = md.get("edit_id", "") or ""
        prop = proposals.get(edit_id, {})
        out.append(EvolutionVerdictItem(
            edit_id=edit_id,
            evolver=prop.get("evolver", "") or md.get("evolver", "") or "",
            target_namespace=prop.get("target_namespace", "") or "",
            decision=md.get("decision", "(unknown)") or "(unknown)",
            timestamp=float(ev.timestamp),
            regression_score=float(md.get("regression_score", 0.0) or 0.0),
            abc_drift_delta=float(md.get("abc_drift_delta", 0.0) or 0.0),
            evidence=str(md.get("evidence", "") or "")[:500],
            change_summary=str(prop.get("change_summary", "") or "")[:200],
        ))
        if len(out) >= limit:
            break
    return out


def _count_facts_for_pressure(twin) -> int:
    """Count facts currently visible to KnowledgeCompiler — uses
    the same source it would on its own (CuratedMemory)."""
    try:
        return int(getattr(twin.curated_memory, "memory_count", 0) or 0)
    except Exception:
        return 0


@router.get("/evolution/pressure", response_model=EvolutionPressureResponse)
async def get_evolution_pressure(
    current_user: str = Depends(get_current_user),
) -> EvolutionPressureResponse:
    """Snapshot of every evolver's accumulator + a 24h histogram.

    Drives the desktop's Pressure Dashboard:
      * **Gauges** — one progress bar per evolver, colour-coded by
        layer; ``status`` decides idle/warming/ready styling.
      * **Lineage** — arrows drawn from upstream evolvers (``fed_by``)
        to the current one, so the user sees the causal chain.
      * **Histogram** — 24h sparkline per evolver showing the
        pyramid of evolution rates (L0 sparse, L1 busy).

    Endpoint is meant to be polled every 5s — much slower cadence
    than the per-2s Cognition stream because pressure changes
    slowly. All work is local (in-process pressure_state calls +
    a bounded EventLog scan), no remote round-trips.
    """
    twin = await twin_manager.get_twin(current_user)
    items: list[EvolutionPressureItem] = []

    # Aliases for the evolution engine and its sub-evolvers.
    engine = getattr(twin, "evolution", None)
    memory = getattr(engine, "memory", None) if engine else None
    skills = getattr(engine, "skills", None) if engine else None
    persona = getattr(engine, "persona", None) if engine else None
    knowledge = getattr(engine, "knowledge", None) if engine else None
    compactor = getattr(twin, "_compactor", None)

    def _safe_pressure(obj, *args, **kwargs):
        try:
            if obj is None or not hasattr(obj, "pressure_state"):
                return None
            return obj.pressure_state(*args, **kwargs)
        except Exception as e:  # noqa: BLE001
            logger.debug("pressure_state(%s) failed: %s", obj, e)
            return None

    # MemoryEvolver — live, fed by chat turns.
    s = _safe_pressure(memory)
    if s:
        items.append(EvolutionPressureItem(**s))

    # EventLogCompactor — accumulator = events since last compact.
    s = _safe_pressure(compactor)
    if s:
        items.append(EvolutionPressureItem(**s))

    # KnowledgeCompiler — accumulator = current fact count.
    fact_count = _count_facts_for_pressure(twin)
    s = _safe_pressure(
        knowledge,
        fact_count=fact_count,
        min_memories=10,
    )
    if s:
        items.append(EvolutionPressureItem(**s))

    # SkillEvolver — per-topic accumulators rolled into one primary.
    s = _safe_pressure(skills)
    if s:
        items.append(EvolutionPressureItem(**s))

    # PersonaEvolver — drift + days-since dual trigger. Drift score
    # comes from twin.drift if present; falls back to 0.
    drift_score = 0.0
    drift = getattr(twin, "drift", None)
    if drift is not None:
        try:
            drift_score = float(getattr(drift, "current", 0.0) or 0.0)
            if hasattr(drift, "drift_score") and callable(drift.drift_score):
                drift_score = float(drift.drift_score() or drift_score)
        except Exception:  # noqa: BLE001
            drift_score = 0.0
    s = _safe_pressure(
        persona,
        cadence_days=30.0,
        drift_threshold=0.7,
        drift_score=drift_score,
    )
    if s:
        items.append(EvolutionPressureItem(**s))

    # 24h histogram + recent verdict feed (Phase D 续 / #159).
    histogram: dict[str, list[int]] = {}
    verdicts: list[EvolutionVerdictItem] = []
    event_log = getattr(twin, "event_log", None)
    if event_log is not None:
        histogram = _aggregate_evolution_histogram(event_log)
        verdicts = _aggregate_verdict_feed(event_log, limit=10)

    return EvolutionPressureResponse(
        evolvers=items,
        histogram_24h=histogram,
        recent_verdicts=verdicts,
    )


# ── Brain panel: learning_summary (Phase D 续 / #159) ─────────────


class TimelineDay(BaseModel):
    """One day of learning activity for the Brain panel timeline."""
    day: str                            # ISO date "YYYY-MM-DD"
    facts: int = 0
    skills: int = 0
    knowledge: int = 0
    persona: int = 0
    episodes: int = 0


class JustLearnedItem(BaseModel):
    """One recent change across any namespace, for the Just Learned feed."""
    kind: str                           # fact / skill / persona / knowledge / episode
    content: str                        # short preview / title
    category: str = ""                  # namespace-specific category if any
    importance: int = 3
    timestamp: float
    version: Optional[str] = None
    chain_status: str = "local"         # local | mirrored | anchored


class DataFlowStage(BaseModel):
    """One evolver's status for the Data Flow viz this turn."""
    evolver: str
    layer: str = ""
    status: str = "live"                # live | warming | ready | fired_recently
    accumulator: float = 0.0
    threshold: float = 0.0
    unit: str = ""
    fed_by: list[str] = []
    last_fired_at: Optional[float] = None


class LearningSummaryResponse(BaseModel):
    """Brain panel data: 7-day timeline + just-learned feed +
    data-flow snapshot.

    Polled every ~10s by the desktop Brain panel — much slower than
    Cognition (per-2s) since this is summary data."""
    window_days: int
    timeline: list[TimelineDay]
    just_learned: list[JustLearnedItem]
    data_flow: list[DataFlowStage]


def _bucket_by_day(events, window_days: int) -> dict[str, dict[str, int]]:
    """Bucket evolution_proposal events by ISO day → namespace counts."""
    import datetime as _dt
    import time as _time
    cutoff = _time.time() - window_days * 86400
    out: dict[str, dict[str, int]] = {}
    for ev in events:
        if ev.event_type != "evolution_proposal":
            continue
        if ev.timestamp < cutoff:
            continue
        md = ev.metadata or {}
        target = (md.get("target_namespace") or "").replace("memory.", "")
        if target not in {"facts", "skills", "knowledge", "persona", "episodes"}:
            continue
        day = _dt.datetime.fromtimestamp(ev.timestamp).strftime("%Y-%m-%d")
        out.setdefault(day, {}).setdefault(target, 0)
        out[day][target] += 1
    return out


def _build_learning_timeline(events, window_days: int) -> list[TimelineDay]:
    import datetime as _dt
    bucket = _bucket_by_day(events, window_days)
    today = _dt.datetime.now().date()
    out: list[TimelineDay] = []
    for delta in range(window_days - 1, -1, -1):
        day = today - _dt.timedelta(days=delta)
        key = day.strftime("%Y-%m-%d")
        counts = bucket.get(key, {})
        out.append(TimelineDay(
            day=key,
            facts=counts.get("facts", 0),
            skills=counts.get("skills", 0),
            knowledge=counts.get("knowledge", 0),
            persona=counts.get("persona", 0),
            episodes=counts.get("episodes", 0),
        ))
    return out


def _classify_chain_status(versioned, last_anchor_at: Optional[float]) -> str:
    """Returns 'local' | 'mirrored' | 'anchored' for a typed-store
    version, mirroring VersionedStore.chain_status logic."""
    if versioned is None:
        return "local"
    try:
        s = versioned.chain_status(last_anchor_at=last_anchor_at)
        return str(s.get("status", "local"))
    except Exception:
        return "local"


def _build_just_learned(twin, last_anchor_at, limit: int = 12) -> list[JustLearnedItem]:
    """Merge recent items across all 5 stores by timestamp, newest first."""
    out: list[JustLearnedItem] = []

    def _push(kind, content, category, importance, ts, version, versioned):
        out.append(JustLearnedItem(
            kind=kind,
            content=str(content)[:200],
            category=str(category)[:64],
            importance=int(importance) if importance is not None else 3,
            timestamp=float(ts) if ts else 0.0,
            version=version,
            chain_status=_classify_chain_status(versioned, last_anchor_at),
        ))

    facts = getattr(twin, "facts", None)
    if facts is not None:
        try:
            for f in facts.all()[-limit:]:
                _push(
                    "fact", f.content, f.category, f.importance,
                    f.created_at, facts.current_version(),
                    getattr(facts, "_versioned", None),
                )
        except Exception:
            pass

    skills = getattr(twin, "skills_memory", None)
    if skills is not None:
        try:
            for s in skills.all()[-limit:]:
                preview = s.last_lesson or s.description or s.skill_name
                _push(
                    "skill", preview, s.skill_name, 3,
                    getattr(s, "updated_at", 0.0),
                    skills.current_version(),
                    getattr(skills, "_versioned", None),
                )
        except Exception:
            pass

    knowledge = getattr(twin, "knowledge", None)
    if knowledge is not None:
        try:
            for a in knowledge.all()[-limit:]:
                _push(
                    "knowledge", a.title, "article", 4,
                    getattr(a, "updated_at", 0.0),
                    knowledge.current_version(),
                    getattr(knowledge, "_versioned", None),
                )
        except Exception:
            pass

    persona = getattr(twin, "persona_store", None)
    if persona is not None:
        try:
            for entry in persona.history(limit=limit):
                _push(
                    "persona",
                    entry.get("changes_summary") or entry.get("version_notes") or entry.get("version", ""),
                    "version",
                    5,
                    entry.get("created_at", 0.0),
                    entry.get("version"),
                    getattr(persona, "_versioned", None),
                )
        except Exception:
            pass

    episodes = getattr(twin, "episodes", None)
    if episodes is not None:
        try:
            ep_list = episodes.recent(limit=limit) if hasattr(episodes, "recent") else episodes.all()[-limit:]
            for e in ep_list:
                _push(
                    "episode", getattr(e, "summary", "") or "(no summary)",
                    "session", 2,
                    getattr(e, "created_at", 0.0),
                    episodes.current_version(),
                    getattr(episodes, "_versioned", None),
                )
        except Exception:
            pass

    out.sort(key=lambda x: x.timestamp, reverse=True)
    return out[:limit]


def _build_data_flow(twin) -> list[DataFlowStage]:
    """Snapshot of each evolver's current status — same data the
    pressure dashboard surfaces, but flattened into the order the
    Brain panel's data-flow viz draws (chat → facts → skills →
    knowledge → persona)."""
    engine = getattr(twin, "evolution", None)
    if engine is None:
        return []

    def _safe(obj, *args, **kwargs):
        try:
            if obj is None or not hasattr(obj, "pressure_state"):
                return None
            return obj.pressure_state(*args, **kwargs)
        except Exception:
            return None

    stages: list[DataFlowStage] = []

    fact_count = _count_facts_for_pressure(twin)
    drift_score = 0.0
    drift = getattr(twin, "drift", None)
    if drift is not None:
        try:
            drift_score = float(getattr(drift, "current", 0.0) or 0.0)
        except Exception:
            drift_score = 0.0

    spec = [
        ("MemoryEvolver",     getattr(engine, "memory", None),    {}),
        ("SkillEvolver",      getattr(engine, "skills", None),    {}),
        ("KnowledgeCompiler", getattr(engine, "knowledge", None), {"fact_count": fact_count, "min_memories": 10}),
        ("PersonaEvolver",    getattr(engine, "persona", None),   {"cadence_days": 30.0, "drift_threshold": 0.7, "drift_score": drift_score}),
    ]
    for name, obj, kwargs in spec:
        s = _safe(obj, **kwargs)
        if s:
            stages.append(DataFlowStage(
                evolver=s.get("evolver", name),
                layer=s.get("layer", ""),
                status=s.get("status", "live"),
                accumulator=float(s.get("accumulator", 0.0) or 0.0),
                threshold=float(s.get("threshold", 0.0) or 0.0)
                          if s.get("threshold") not in (None, float("inf")) else 0.0,
                unit=s.get("unit", ""),
                fed_by=list(s.get("fed_by", []) or []),
                last_fired_at=s.get("last_fired_at"),
            ))
    return stages


@router.get("/learning_summary", response_model=LearningSummaryResponse)
async def get_learning_summary(
    window: str = "7d",
    current_user: str = Depends(get_current_user),
) -> LearningSummaryResponse:
    """Brain panel: timeline + just-learned feed + data flow.

    ``window`` accepts ``"7d"`` (default) / ``"14d"`` / ``"30d"`` —
    everything else falls back to 7. The timeline is one row per day
    with 5 namespace counts; just_learned is a merged feed of recent
    items across stores, newest first; data_flow is the same
    per-evolver state the pressure dashboard uses, in pyramid order.
    """
    if window.endswith("d"):
        try:
            days = max(1, min(60, int(window[:-1])))
        except ValueError:
            days = 7
    else:
        days = 7

    twin = await twin_manager.get_twin(current_user)

    # Pull events once — both timeline and (eventually) drift use them.
    event_log = getattr(twin, "event_log", None)
    events = []
    if event_log is not None:
        try:
            events = event_log.recent(limit=2000)
        except Exception:
            events = []

    timeline = _build_learning_timeline(events, days)

    # Resolve last_anchor_at once for chain_status in the feed.
    backend = getattr(twin.rune, "_backend", None)
    last_anchor: Optional[float] = None
    if backend is not None:
        try:
            last_anchor = backend.last_anchor_at(twin.config.agent_id)
        except Exception:
            last_anchor = None

    just_learned = _build_just_learned(twin, last_anchor, limit=12)
    data_flow = _build_data_flow(twin)

    return LearningSummaryResponse(
        window_days=days,
        timeline=timeline,
        just_learned=just_learned,
        data_flow=data_flow,
    )


@router.get("/thinking", response_model=ThinkingResponse)
async def get_thinking_trace(
    limit: int = 60,
    since_sync_id: Optional[int] = None,
    current_user: str = Depends(get_current_user),
) -> ThinkingResponse:
    """Read the agent's recent inner-monologue / thinking trace.

    Surface a curated subset of the twin's EventLog as a stream of
    "what the agent did and why" — the agent's reasoning made visible
    to the user. Entries are newest-first.

    The desktop client polls this while a chat is in progress so the
    user sees the agent thinking out loud (memory recall, contract
    checks, evolution proposals, tool decisions) instead of staring
    at a spinner.

    Args:
        limit: max rows to return (1–200).
        since_sync_id: pagination cursor — return only rows with
            ``sync_id`` strictly greater than this. Lets the client
            poll efficiently for new steps.
    """
    if limit <= 0 or limit > 200:
        limit = 60

    raw = twin_event_log.list_timeline_events(current_user, limit=limit * 2)
    steps: list[ThinkingStep] = []
    for row in raw:
        et = row.get("event_type", "")
        mapping = _THINKING_MAP.get(et)
        if mapping is None:
            continue
        sid = int(row.get("sync_id", 0))
        if since_sync_id is not None and sid <= since_sync_id:
            continue
        kind, label = mapping
        steps.append(ThinkingStep(
            sync_id=sid,
            timestamp=row.get("timestamp", ""),
            kind=kind,
            label=label,
            content=(row.get("content") or "")[:600],
            metadata=row.get("metadata") or {},
        ))
        if len(steps) >= limit:
            break

    return ThinkingResponse(steps=steps, total=len(steps))


@router.post(
    "/evolution/{edit_id}/approve",
    response_model=EvolutionDecisionResult,
)
async def manual_approve_proposal(
    edit_id: str,
    current_user: str = Depends(get_current_user),
) -> EvolutionDecisionResult:
    """User-initiated keep for a specific evolution proposal.

    Skips the verdict window and pins the edit as approved. Writes
    an ``evolution_verdict`` with decision=kept; no store-side
    side effect (the working state already reflects the edit).
    Idempotent.
    """
    from fastapi import HTTPException
    from nexus_core.evolution import EvolutionVerdict

    twin = await twin_manager.get_twin(current_user)
    event_log = getattr(twin, "event_log", None)
    if event_log is None:
        raise HTTPException(status_code=503, detail="twin event log unavailable")

    proposal, _ = _find_proposal(event_log, edit_id)
    if proposal is None:
        raise HTTPException(status_code=404, detail=f"no proposal for {edit_id!r}")

    if _already_settled(event_log, edit_id):
        return EvolutionDecisionResult(
            edit_id=edit_id,
            decision="kept",
            target_namespace=proposal.target_namespace,
            note="already settled (idempotent)",
        )

    verdict = EvolutionVerdict(
        edit_id=edit_id,
        verdict_at_event=0,
        events_observed=0,
        decision="kept",
    )
    event_log.append(
        event_type="evolution_verdict",
        content=f"manual approve for {edit_id}",
        metadata={
            **verdict.to_event_metadata(),
            "trigger": "manual",   # extra field for forward-compat
            "approver": current_user,
        },
    )
    return EvolutionDecisionResult(
        edit_id=edit_id,
        decision="kept",
        target_namespace=proposal.target_namespace,
        note="manual approve applied",
    )


# ── /api/v1/sync/anchors — read-only legacy view (Phase B migration) ─


@sync_router.get("/anchors")
async def get_sync_anchors(
    limit: int = 20,
    current_user: str = Depends(get_current_user),
) -> dict:
    """Newest-first anchor lifecycle rows (pre-S4 history).

    The /sync/push enqueue path is gone (Phase B); chat-mode twin's
    ChainBackend writes BSC anchors directly without touching this
    table. So in production this list only grows for users that
    pre-date S4. Kept as a back-compat read view for the desktop
    sidebar's anchor count badge.
    """
    if limit <= 0 or limit > 200:
        limit = 20
    rows = list_anchors_for_user(current_user, limit=limit)
    return {"anchors": rows}


# ── #105: orphan twin recovery — endpoints ────────────────────────────


class OrphanTwinSummary(BaseModel):
    """One orphan twin row surfaced to the desktop's Account view."""
    user_id: str
    agent_id: str
    event_count: int
    message_count: int
    session_count: int
    last_active: Optional[str]


class OrphanTwinListResponse(BaseModel):
    """GET /api/v1/agent/orphan_twins response."""
    enabled: bool                          # False → recovery disabled in this deployment
    twins: list[OrphanTwinSummary]


@router.get("/orphan_twins", response_model=OrphanTwinListResponse)
async def list_orphan_twins_endpoint(
    current_user: str = Depends(get_current_user),
) -> OrphanTwinListResponse:
    """List twin DBs on this server that aren't owned by the calling
    user. Returns ``enabled=False`` when single-tenant gate is off
    (production hosted deployments)."""
    from nexus_server import orphan_twins as _ot
    if not _ot.is_enabled():
        return OrphanTwinListResponse(enabled=False, twins=[])
    rows = _ot.list_orphan_twins(current_user)
    return OrphanTwinListResponse(
        enabled=True,
        twins=[OrphanTwinSummary(**r) for r in rows],
    )


class OrphanTwinMergeResponse(BaseModel):
    """POST /api/v1/agent/orphan_twins/{user_id}/merge response."""
    merged_event_count: int
    orphan_removed: bool


@router.post(
    "/orphan_twins/{orphan_user_id}/merge",
    response_model=OrphanTwinMergeResponse,
)
async def merge_orphan_twin_endpoint(
    orphan_user_id: str,
    delete_after: bool = True,
    current_user: str = Depends(get_current_user),
) -> OrphanTwinMergeResponse:
    """Copy every event from the orphan twin into the current user's
    twin. ``delete_after=True`` (default) rms the orphan's data dir
    once the merge is done; set to False if you want to keep an
    untouched copy as a backup."""
    from nexus_server import orphan_twins as _ot
    if not _ot.is_enabled():
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Orphan twin recovery is disabled on this server "
                   "(NEXUS_ALLOW_ORPHAN_RECOVERY not set).",
        )
    if orphan_user_id == current_user:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Can't merge a user into itself.",
        )
    try:
        n = _ot.merge_orphan_into(orphan_user_id, current_user)
    except FileNotFoundError as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(e),
        )
    removed = False
    if delete_after and n > 0:
        try:
            removed = _ot.rm_orphan_twin(orphan_user_id)
        except Exception as e:  # noqa: BLE001
            logger.warning("orphan_twins delete-after failed: %s", e)
    return OrphanTwinMergeResponse(
        merged_event_count=n, orphan_removed=removed,
    )


# ── #113: token spend meter — usage summary endpoint ──────────────────


class UsageRollupResponse(BaseModel):
    """GET /api/v1/agent/usage_summary?days=N. Aggregated token usage
    + estimated $ cost for the meter UI."""
    total:        dict
    today:        dict
    by_day:       list[dict]
    by_model:     list[dict]
    window_days:  int


@router.get("/usage_summary", response_model=UsageRollupResponse)
async def usage_summary_endpoint(
    days: int = 7,
    current_user: str = Depends(get_current_user),
) -> UsageRollupResponse:
    """Roll up token usage for the current user over the last N days
    (default 7, cap 90). Drives the desktop cognition panel's
    'Today: X tokens · $Y' indicator."""
    from nexus_server import llm_usage
    summary = llm_usage.usage_summary(current_user, days=days)
    return UsageRollupResponse(**summary)
