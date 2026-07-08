"""
BNBChainSessionService — ADK-compatible SessionService backed by on-chain state.

Contract interactions:
  - ERC-8004 Identity Registry: read agent ownership (verify agentId exists)
  - AgentStateExtension.sol:    read/write state_root hash (our contract)
  - Object store:               read/write full session payloads

Write architecture (configurable via FlushPolicy):
  Layer 1 (Hot):  In-memory session + local WAL       ← every event
  Layer 2 (Warm): object-store payload write           ← batched
  Layer 3 (Cold): BSC updateStateRoot                  ← batched with Layer 2

Default: batch 5 events or 30 seconds, sync on session close.
Override: FlushPolicy.sync_every() for legacy every-event behavior.
"""

import hashlib
import json
import time
import uuid
from typing import Any, Optional

from google.adk.events import Event
from google.adk.sessions import Session
from google.adk.sessions.base_session_service import (
    BaseSessionService,
    GetSessionConfig,
    ListSessionsResponse,
)

from .state import StateManager
from .flush import FlushPolicy, FlushBuffer, WriteAheadLog


class BNBChainSessionService(BaseSessionService):
    """
    Session service that persists all state to the BNBChain state layer.

    Flow for each event (with default batching):
      1. Append event to in-memory session + WAL (instant)
      2. When flush triggers (N events / T seconds / explicit):
         a. Serialize session → JSON → object store → content_hash
         b. Update state_root on BSC (content_hash)
         c. Truncate WAL

    On crash recovery:
      1. Load last committed session from the object store (via BSC state_root)
      2. Replay WAL entries on top → restored to pre-crash state
      3. At most N-1 events may need replay (where N = every_n_events)
    """

    def __init__(
        self,
        state_manager: StateManager,
        runtime_id: Optional[str] = None,
        flush_policy: Optional[FlushPolicy] = None,
        memory_service: Optional[Any] = None,
    ):
        self._state = state_manager
        self._runtime_id = runtime_id or f"runtime-{uuid.uuid4().hex[:8]}"
        self._flush_policy = flush_policy or FlushPolicy()
        self._memory_service = memory_service  # Optional MemoryService

        # Per-session flush buffers: session_id → FlushBuffer
        self._buffers: dict[str, FlushBuffer] = {}

        # In-memory session cache (needed for batched writes —
        # we must keep the full session in memory between flushes)
        self._sessions: dict[str, Session] = {}

        mem_status = "enabled" if memory_service else "disabled"
        print(f"  [SDK] BNBChainSessionService initialized "
              f"(runtime={self._runtime_id}, "
              f"flush_every={self._flush_policy.every_n_events}, "
              f"interval={self._flush_policy.interval_seconds}s, "
              f"memory={mem_status})")

    @property
    def flush_policy(self) -> FlushPolicy:
        """Current flush policy.  Can be changed at runtime."""
        return self._flush_policy

    @flush_policy.setter
    def flush_policy(self, policy: FlushPolicy) -> None:
        self._flush_policy = policy
        # Propagate to existing buffers
        for buf in self._buffers.values():
            buf.policy = policy

    # ── Flush mechanics ─────────────────────────────────────────────

    def _get_buffer(self, session: Session) -> FlushBuffer:
        """Get or create a FlushBuffer for a session."""
        if session.id not in self._buffers:
            wal = None
            if self._flush_policy.wal_enabled:
                wal_key = f"{session.app_name}_{session.id}"
                wal = WriteAheadLog(self._flush_policy.wal_dir, wal_key)

            def on_flush(events: list[dict]) -> None:
                self._flush_session(session)

            self._buffers[session.id] = FlushBuffer(
                policy=self._flush_policy,
                on_flush=on_flush,
                wal=wal,
            )
        return self._buffers[session.id]

    def _flush_session(self, session: Session) -> None:
        """Write the full session to the object store + update BSC state_root."""
        session_data = self._serialize_session(session)

        # Structured path: rune/agents/{agentAddress}/sessions/{sessionId}/{hash}.json
        from .state import StateManager
        folder = self._state.agent_folder(session.app_name)
        data_bytes = json.dumps(session_data, default=str, sort_keys=True).encode("utf-8")
        chash = hashlib.sha256(data_bytes).hexdigest()
        obj_path = StateManager.storage_path(
            folder, "sessions", chash, sub_key=session.id,
        )
        # Use store_data (not store_json) to avoid re-serialization —
        # the hash we computed above must match the bytes we store.
        content_hash = self._state.store_data(data_bytes, object_path=obj_path)

        # Update index with new content hash + anchor on BSC
        index = self._get_index(session.app_name)
        if session.id in index:
            index[session.id]["content_hash"] = content_hash
            index[session.id]["updated_at"] = time.time()
        else:
            index[session.id] = {
                "content_hash": content_hash,
                "user_id": session.user_id,
                "created_at": time.time(),
            }
        self._save_index(session.app_name, index)

    def flush(self, session_id: Optional[str] = None) -> int:
        """
        Explicitly flush buffered events to the object store + BSC.

        Args:
            session_id: Flush a specific session.  If None, flush all sessions.

        Returns:
            Number of events flushed.
        """
        total = 0
        if session_id:
            buf = self._buffers.get(session_id)
            if buf:
                total = buf.force_flush()
        else:
            for buf in self._buffers.values():
                total += buf.force_flush()
        return total

    def close(self) -> None:
        """Flush all sessions and release resources.  Call on shutdown."""
        for buf in self._buffers.values():
            buf.close()
        self._buffers.clear()
        self._sessions.clear()

    # ── Index management ────────────────────────────────────────────

    def _get_index(self, app_name: str) -> dict:
        """Load the session index from chain."""
        # Check time trigger on reads (piggyback flush check)
        for buf in self._buffers.values():
            buf.check_time_trigger()

        agent = self._state.get_agent(app_name)
        if agent is None:
            return {}
        if not agent.state_root:
            return {}
        index = self._state.load_json(agent.state_root)
        if index is None:
            return {}
        return index.get("sessions", {})

    def _save_index(self, app_name: str, index: dict) -> None:
        """Save session index to chain."""
        from .state import StateManager
        folder = self._state.agent_folder(app_name)
        payload = {"sessions": index, "updated_at": time.time()}
        data_bytes = json.dumps(payload, default=str, sort_keys=True).encode("utf-8")
        chash = hashlib.sha256(data_bytes).hexdigest()
        obj_path = StateManager.storage_path(folder, "state", chash)
        content_hash = self._state.store_data(data_bytes, object_path=obj_path)
        self._state.update_state_root(app_name, content_hash, self._runtime_id)

    def _serialize_session(self, session: Session) -> dict:
        """Serialize a Session to a dict for storage."""
        return {
            "id": session.id,
            "app_name": session.app_name,
            "user_id": session.user_id,
            "state": session.state,
            "events": [e.model_dump(mode="json", exclude_none=True) for e in session.events],
            "last_update_time": session.last_update_time,
        }

    def _deserialize_session(self, data: dict) -> Session:
        """Deserialize a Session from stored dict."""
        events = []
        skipped = 0
        for e_data in data.get("events", []):
            try:
                events.append(Event.model_validate(e_data))
            except Exception as e:
                skipped += 1
                logger.warning("Skipping corrupt event during deserialization: %s", e)
        if skipped:
            logger.warning("Dropped %d/%d events from session %s",
                           skipped, skipped + len(events), data.get("id", "?"))
        return Session(
            id=data["id"],
            app_name=data["app_name"],
            user_id=data["user_id"],
            state=data.get("state", {}),
            events=events,
            last_update_time=data.get("last_update_time", 0.0),
        )

    # ── ADK Interface Implementation ─────────────────────────────────

    async def create_session(
        self,
        *,
        app_name: str,
        user_id: str,
        state: Optional[dict[str, Any]] = None,
        session_id: Optional[str] = None,
    ) -> Session:
        session_id = session_id or str(uuid.uuid4())
        session = Session(
            id=session_id,
            app_name=app_name,
            user_id=user_id,
            state=state or {},
            last_update_time=time.time(),
        )

        # Ensure agent is registered on chain
        if self._state.get_agent(app_name) is None:
            self._state.register_agent(app_name, owner=user_id)

        # Cache session in memory
        self._sessions[session_id] = session

        # Store initial session state (always sync on creation)
        session_data = self._serialize_session(session)
        # Build structured path for browsability (dual-write ensures
        # canonical rune/{hash} is also created for reads)
        from .state import StateManager
        folder = self._state.agent_folder(app_name)
        data_bytes = json.dumps(session_data, default=str, sort_keys=True).encode("utf-8")
        init_hash = hashlib.sha256(data_bytes).hexdigest()
        init_path = StateManager.storage_path(
            folder, "sessions", init_hash, sub_key=session_id,
        )
        content_hash = self._state.store_data(data_bytes, object_path=init_path)

        index = self._get_index(app_name)
        index[session_id] = {
            "content_hash": content_hash,
            "user_id": user_id,
            "created_at": time.time(),
        }
        self._save_index(app_name, index)

        print(f"  [SDK] Session created: {session_id} (chain-backed)")
        return session

    async def get_session(
        self,
        *,
        app_name: str,
        user_id: str,
        session_id: str,
        config: Optional[GetSessionConfig] = None,
    ) -> Optional[Session]:
        # Check in-memory cache first
        if session_id in self._sessions:
            session = self._sessions[session_id]
            if config:
                # Apply filters on a copy
                import copy
                session = copy.deepcopy(session)
                if config.num_recent_events is not None and session.events:
                    session.events = session.events[-config.num_recent_events:]
                if config.after_timestamp is not None:
                    session.events = [
                        e for e in session.events
                        if e.timestamp > config.after_timestamp
                    ]
            return session

        # Load from chain
        index = self._get_index(app_name)
        entry = index.get(session_id)
        if entry is None:
            return None

        session_data = self._state.load_json(entry["content_hash"])
        if session_data is None:
            return None

        session = self._deserialize_session(session_data)

        # WAL recovery: replay any events written after the last flush.
        # We must apply state_delta from each event (same as append_event).
        buf = self._get_buffer(session)
        wal_entries = buf.recover_from_wal()
        if wal_entries:
            print(f"  [SDK] WAL recovery: replaying {len(wal_entries)} events for {session_id}")
            for entry_data in wal_entries:
                try:
                    event = Event.model_validate(entry_data)
                    session.events.append(event)
                    # Apply state_delta (mirrors what ADK's base append_event does)
                    if event.actions and event.actions.state_delta:
                        session.state.update(event.actions.state_delta)
                except Exception as e:
                    logger.warning("Skipping corrupt WAL entry: %s", e)

        # Cache in memory
        self._sessions[session_id] = session

        if config:
            import copy
            session = copy.deepcopy(session)
            if config.num_recent_events is not None and session.events:
                session.events = session.events[-config.num_recent_events:]
            if config.after_timestamp is not None:
                session.events = [
                    e for e in session.events
                    if e.timestamp > config.after_timestamp
                ]

        # Auto-recall: inject relevant memories into session state
        if (self._memory_service
                and getattr(self._memory_service, '_config', None)
                and getattr(self._memory_service._config, 'auto_recall', False)):
            try:
                # Build query from recent session state
                query_parts = []
                for k, v in list(session.state.items())[:10]:
                    if isinstance(v, str) and len(v) > 5 and not k.startswith("_"):
                        query_parts.append(v)
                if query_parts:
                    query = " ".join(query_parts[:3])
                    recalled = await self._memory_service.recall_for_session(
                        query, agent_id=app_name, user_id=user_id,
                    )
                    if recalled.get("count", 0) > 0:
                        session.state["_recalled_memories"] = recalled
            except Exception as e:
                import logging
                logging.getLogger("nexus_core.session").warning(
                    "Auto-recall failed for session %s: %s", session_id, e)

        print(f"  [SDK] Session loaded from chain: {session_id} ({len(session.events)} events)")
        return session

    async def list_sessions(
        self, *, app_name: str, user_id: Optional[str] = None
    ) -> ListSessionsResponse:
        index = self._get_index(app_name)
        sessions = []
        for sid, entry in index.items():
            if user_id and entry.get("user_id") != user_id:
                continue
            sessions.append(Session(
                id=sid,
                app_name=app_name,
                user_id=entry.get("user_id", ""),
            ))
        return ListSessionsResponse(sessions=sessions)

    async def delete_session(
        self, *, app_name: str, user_id: str, session_id: str
    ) -> None:
        index = self._get_index(app_name)
        if session_id in index:
            del index[session_id]
            self._save_index(app_name, index)

        # Clean up buffer and cache
        buf = self._buffers.pop(session_id, None)
        if buf:
            buf.close()
        self._sessions.pop(session_id, None)
        print(f"  [SDK] Session deleted from chain: {session_id}")

    async def append_event(self, session: Session, event: Event) -> Event:
        """
        Append an event to the session.

        With batching (default):
          - Event is added to in-memory session + WAL immediately
          - Object-store + BSC write is deferred until flush triggers

        With sync_every policy:
          - Every event is written to the object store + BSC immediately
          - Equivalent to legacy behavior
        """
        # Let the base class handle state updates
        event = await super().append_event(session, event)

        if event.partial:
            return event

        # Update in-memory cache
        self._sessions[session.id] = session

        # Buffer the event (may trigger flush)
        buf = self._get_buffer(session)
        event_data = event.model_dump(mode="json", exclude_none=True)
        buf.append(event_data)

        # Auto-memorize: extract memories when session has enough events
        # Triggers on every N events (piggybacks on flush policy count)
        if (self._memory_service
                and getattr(self._memory_service, '_config', None)
                and getattr(self._memory_service._config, 'auto_memorize', False)
                and not event.partial
                and session.events):
            try:
                await self._memory_service.memorize_from_session(
                    session.events, agent_id=session.app_name,
                    user_id=session.user_id,
                )
            except Exception as e:
                import logging
                logging.getLogger("nexus_core.session").warning(
                    "Auto-memorize failed for session %s: %s", session.id, e)

        return event
