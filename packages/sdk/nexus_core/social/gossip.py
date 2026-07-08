"""
Gossip Protocol — Async/Sync agent-to-agent conversation management.

Gossip sessions are non-goal-oriented conversations between agents.
They exchange perspectives to test compatibility, not to complete tasks.

Transport modes:
  - sync:  Messages stay in memory. Both agents must be in the same runtime.
           No chain persistence. Fast, ephemeral. Good for co-located agents.
  - async: Messages stored via StorageBackend. Agents can be
           offline at different times. Persistent and verifiable.

Privacy: Gossip content must NOT contain private user data. Agents discuss
interests, knowledge, and perspectives at a coarse-grained level.

Storage layout (async mode):
    agents/{agent_id}/gossip/{session_id}/session.json
    agents/{agent_id}/gossip/{session_id}/msg-{sequence:04d}.json
"""

from __future__ import annotations

import hashlib
import time
from typing import Any, Callable, Optional

from ..core.backend import StorageBackend
from ..core.models import GossipMessage, GossipSession


class GossipProtocol:
    """
    Manages gossip session lifecycle.

    Supports both sync (in-memory) and async (backend-persisted) transport.
    Each agent runtime maintains its own GossipProtocol instance.

    Usage:
        gossip = GossipProtocol(backend, agent_id="agent-a")

        # Start a session
        session = await gossip.initiate("agent-b", topic="tokyo_dining")

        # Send messages
        session = await gossip.send(session.session_id, "What restaurants do you know?")

        # Receive messages (sync mode — directly injected)
        session = await gossip.receive(session.session_id, msg)

        # Conclude
        session = await gossip.conclude(session.session_id)
    """

    def __init__(
        self,
        backend: StorageBackend,
        agent_id: str,
        default_transport: str = "sync",
        max_turns: int = 8,
        expiry_timeout: float = 3600 * 24,  # 24 hours for async
    ):
        self._backend = backend
        self._agent_id = agent_id
        self._default_transport = default_transport
        self._max_turns = max_turns
        self._expiry_timeout = expiry_timeout

        # In-memory session store
        self._sessions: dict[str, GossipSession] = {}

        # Callbacks for message handling
        self._on_message: Optional[Callable] = None

    @property
    def agent_id(self) -> str:
        return self._agent_id

    # ── Session Lifecycle ──────────────────────────────────────────

    async def initiate(
        self,
        target_agent: str,
        topic: str = "",
        transport: Optional[str] = None,
        max_turns: Optional[int] = None,
    ) -> GossipSession:
        """
        Start a new gossip session with another agent.

        Args:
            target_agent: The agent to gossip with.
            topic: Optional topic hint to seed the conversation.
            transport: "sync" or "async". Defaults to instance default.
            max_turns: Override max turns for this session.

        Returns:
            A new GossipSession in "pending" status.
        """
        session = GossipSession(
            initiator=self._agent_id,
            responder=target_agent,
            topic_hint=topic,
            max_turns=max_turns or self._max_turns,
            status="pending",
            transport=transport or self._default_transport,
        )

        self._sessions[session.session_id] = session

        # Persist session metadata (both modes persist at least the session record)
        await self._save_session(session)

        return session

    async def accept(self, session_id: str) -> GossipSession:
        """
        Accept a pending gossip session (called by the responder).

        Transitions status from "pending" to "active".
        """
        session = await self._get_session(session_id)
        if session.status != "pending":
            raise ValueError(f"Session {session_id} is not pending (status: {session.status})")

        session.status = "active"
        await self._save_session(session)
        return session

    async def send(
        self,
        session_id: str,
        content: str,
    ) -> GossipMessage:
        """
        Send a message in a gossip session.

        Args:
            session_id: The gossip session.
            content: Message text. Must not contain private user data.

        Returns:
            The created GossipMessage.
        """
        session = await self._get_session(session_id)

        if session.status == "pending":
            # Auto-activate on first message from initiator
            session.status = "active"

        if not session.is_active:
            raise ValueError(f"Session {session_id} is not active (status: {session.status})")

        if session.turn_count >= session.max_turns:
            # Auto-conclude when turn limit reached
            return await self._auto_conclude(session)

        # Create message
        msg = GossipMessage(
            session_id=session_id,
            sender=self._agent_id,
            content=content,
            sequence=session.turn_count,
        )

        # Compute content hash
        msg.content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()

        # Add to session
        session.messages.append(msg)
        session.turn_count += 1

        # Persist
        if session.transport == "async":
            await self._persist_message(session, msg)

        await self._save_session(session)

        # Check turn limit
        if session.turn_count >= session.max_turns:
            await self.conclude(session_id)

        return msg

    async def receive(
        self,
        session_id: str,
        message: GossipMessage,
    ) -> GossipSession:
        """
        Receive a message from the other agent (sync mode).

        In sync mode, the other agent's GossipProtocol calls this directly.
        In async mode, messages are loaded from backend via poll().
        """
        session = await self._get_session(session_id)

        if session.status == "pending":
            session.status = "active"

        session.messages.append(message)
        session.turn_count += 1

        await self._save_session(session)

        if self._on_message:
            self._on_message(session, message)

        return session

    async def conclude(self, session_id: str) -> GossipSession:
        """
        Conclude a gossip session. Triggers impression formation.

        Either agent can conclude at any time.
        """
        session = await self._get_session(session_id)
        session.status = "concluded"
        session.ended_at = time.time()

        # Compute session hash
        transcript = "".join(m.content for m in session.messages)
        session.session_hash = hashlib.sha256(transcript.encode("utf-8")).hexdigest()

        await self._save_session(session)

        # Anchor session hash on chain (optional)
        if session.transport == "async":
            await self._backend.anchor(
                self._agent_id, session.session_hash, namespace="gossip"
            )

        return session

    async def expire_stale(self) -> list[str]:
        """
        Expire sessions that have been inactive beyond the timeout.

        Returns list of expired session IDs.
        """
        now = time.time()
        expired = []

        for session_id, session in self._sessions.items():
            if session.is_active and session.messages:
                last_msg_time = session.messages[-1].sent_at
                if now - last_msg_time > self._expiry_timeout:
                    session.status = "expired"
                    session.ended_at = now
                    await self._save_session(session)
                    expired.append(session_id)

        return expired

    # ── Queries ────────────────────────────────────────────────────

    async def get_session(self, session_id: str) -> Optional[GossipSession]:
        """Get a session by ID (returns None if not found)."""
        try:
            return await self._get_session(session_id)
        except KeyError:
            return None

    async def list_sessions(
        self,
        status: Optional[str] = None,
        partner: Optional[str] = None,
    ) -> list[GossipSession]:
        """
        List gossip sessions for this agent.

        Args:
            status: Filter by status (pending, active, concluded, expired).
            partner: Filter by the other agent's ID.
        """
        # Load from backend if not in memory
        await self._load_sessions()

        results = list(self._sessions.values())

        if status:
            results = [s for s in results if s.status == status]
        if partner:
            results = [
                s for s in results
                if partner in s.participants
            ]

        results.sort(key=lambda s: s.started_at, reverse=True)
        return results

    async def get_transcript(self, session_id: str) -> list[GossipMessage]:
        """Get full message transcript for a session."""
        session = await self._get_session(session_id)

        # In async mode, ensure all messages are loaded
        if session.transport == "async":
            await self._load_messages(session)

        return list(session.messages)

    # ── Async Polling (for async transport) ────────────────────────

    async def poll(self, session_id: str) -> list[GossipMessage]:
        """
        Poll for new messages in an async session.

        Checks the backend for messages with sequence numbers
        higher than what we've already seen.

        Returns newly discovered messages.
        """
        session = await self._get_session(session_id)
        if session.transport != "async":
            return []

        other_agent = (
            session.responder if session.initiator == self._agent_id
            else session.initiator
        )

        # Check for new messages from the other agent
        known_sequences = {m.sequence for m in session.messages}
        new_messages = []

        prefix = f"agents/{other_agent}/gossip/{session_id}/"
        paths = await self._backend.list_paths(prefix)

        for path in paths:
            if not path.endswith(".json") or path.endswith("session.json"):
                continue
            data = await self._backend.load_json(path)
            if data and data.get("sequence") not in known_sequences:
                msg = GossipMessage.from_dict(data)
                session.messages.append(msg)
                session.turn_count = len(session.messages)
                new_messages.append(msg)

        if new_messages:
            # Sort messages by sequence
            session.messages.sort(key=lambda m: m.sequence)
            await self._save_session(session)

        return new_messages

    # ── Callbacks ──────────────────────────────────────────────────

    def on_message(self, callback: Callable) -> None:
        """Register a callback for incoming messages."""
        self._on_message = callback

    # ── Sync Bridge ────────────────────────────────────────────────

    @staticmethod
    async def bridge(
        protocol_a: "GossipProtocol",
        protocol_b: "GossipProtocol",
        session_id: str,
        generate_a: Callable,
        generate_b: Callable,
        turns: int = 6,
    ) -> GossipSession:
        """
        Run a synchronous gossip exchange between two agents.

        This is a convenience method for when both agents are in
        the same runtime (e.g., demos, testing, same-machine agents).

        Args:
            protocol_a, protocol_b: Each agent's GossipProtocol instance.
            session_id: The session to conduct.
            generate_a: async fn(session, messages) → str (Agent A's response generator)
            generate_b: async fn(session, messages) → str (Agent B's response generator)
            turns: Number of exchanges (A speaks, then B speaks = 1 turn).

        Returns:
            The concluded GossipSession.
        """
        session_a = await protocol_a._get_session(session_id)
        # Ensure B has the session too
        if session_id not in protocol_b._sessions:
            session_b = GossipSession(
                session_id=session_id,
                initiator=session_a.initiator,
                responder=session_a.responder,
                topic_hint=session_a.topic_hint,
                max_turns=session_a.max_turns,
                status="active",
                transport="sync",
            )
            protocol_b._sessions[session_id] = session_b

        for turn in range(turns):
            # Agent A speaks
            content_a = await generate_a(session_a, session_a.messages)
            msg_a = await protocol_a.send(session_id, content_a)
            await protocol_b.receive(session_id, msg_a)

            # Check if concluded
            session_a = protocol_a._sessions[session_id]
            if session_a.is_concluded:
                break

            # Agent B speaks
            session_b = protocol_b._sessions[session_id]
            content_b = await generate_b(session_b, session_b.messages)
            msg_b = await protocol_b.send(session_id, content_b)
            await protocol_a.receive(session_id, msg_b)

            session_a = protocol_a._sessions[session_id]
            if session_a.is_concluded:
                break

        # Conclude if not already
        if not session_a.is_concluded:
            await protocol_a.conclude(session_id)
            protocol_b._sessions[session_id].status = "concluded"
            protocol_b._sessions[session_id].ended_at = time.time()

        return protocol_a._sessions[session_id]

    # ── Internal ───────────────────────────────────────────────────

    async def _get_session(self, session_id: str) -> GossipSession:
        """Get session from memory or load from backend."""
        if session_id in self._sessions:
            return self._sessions[session_id]

        # Try loading from backend
        path = f"agents/{self._agent_id}/gossip/{session_id}/session.json"
        data = await self._backend.load_json(path)
        if data:
            session = GossipSession.from_dict(data)
            self._sessions[session_id] = session
            return session

        raise KeyError(f"Gossip session {session_id} not found")

    async def _save_session(self, session: GossipSession) -> None:
        """Persist session metadata to backend."""
        self._sessions[session.session_id] = session

        path = f"agents/{self._agent_id}/gossip/{session.session_id}/session.json"
        await self._backend.store_json(path, session.to_dict())

    async def _persist_message(
        self,
        session: GossipSession,
        msg: GossipMessage,
    ) -> None:
        """Persist individual message to backend (async mode)."""
        path = (
            f"agents/{self._agent_id}/gossip/"
            f"{session.session_id}/msg-{msg.sequence:04d}.json"
        )
        content_hash = await self._backend.store_json(path, msg.to_dict())
        msg.content_hash = content_hash

    async def _load_sessions(self) -> None:
        """Load all sessions for this agent from backend."""
        prefix = f"agents/{self._agent_id}/gossip/"
        paths = await self._backend.list_paths(prefix)

        for path in paths:
            if path.endswith("/session.json"):
                session_id = path.split("/")[-2]
                if session_id not in self._sessions:
                    data = await self._backend.load_json(path)
                    if data:
                        self._sessions[session_id] = GossipSession.from_dict(data)

    async def _load_messages(self, session: GossipSession) -> None:
        """Load all messages for a session from backend."""
        prefix = f"agents/{self._agent_id}/gossip/{session.session_id}/"
        paths = await self._backend.list_paths(prefix)

        known_ids = {m.message_id for m in session.messages}

        for path in paths:
            if path.endswith(".json") and not path.endswith("session.json"):
                data = await self._backend.load_json(path)
                if data and data.get("message_id") not in known_ids:
                    session.messages.append(GossipMessage.from_dict(data))

        session.messages.sort(key=lambda m: m.sequence)

    async def _auto_conclude(self, session: GossipSession) -> GossipMessage:
        """Auto-conclude when turn limit reached. Returns a system message."""
        await self.conclude(session.session_id)
        return GossipMessage(
            session_id=session.session_id,
            sender="system",
            content="[Session concluded: turn limit reached]",
            sequence=session.turn_count,
        )
