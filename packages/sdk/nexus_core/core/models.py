"""
Nexus — Framework-Agnostic Data Models.

These are the universal units of persistence. Framework adapters convert
their native types (ADK Session, LangGraph StateSnapshot, CrewAI TaskOutput)
into these models for storage.

    Checkpoint  — a point-in-time snapshot of agent state
    MemoryEntry — a piece of long-term knowledge
    Artifact    — a versioned output file or data blob
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field


@dataclass
class Checkpoint:
    """
    A framework-agnostic state checkpoint.

    Fields:
        checkpoint_id: Unique ID for this checkpoint.
        thread_id: Session/thread/conversation identifier.
        agent_id: Agent identifier (maps to ERC-8004 tokenId).
        state: Arbitrary state dict — the framework's serialized state.
        metadata: Optional metadata (framework name, version, custom tags).
        parent_id: Previous checkpoint ID (for history traversal).
        created_at: Unix timestamp.
    """

    checkpoint_id: str = ""
    thread_id: str = ""
    agent_id: str = ""
    state: dict = field(default_factory=dict)
    metadata: dict = field(default_factory=dict)
    parent_id: str = ""
    created_at: float = 0.0

    def __post_init__(self):
        if not self.checkpoint_id:
            self.checkpoint_id = str(uuid.uuid4())
        if self.created_at == 0.0:
            self.created_at = time.time()

    def to_dict(self) -> dict:
        return {
            "checkpoint_id": self.checkpoint_id,
            "thread_id": self.thread_id,
            "agent_id": self.agent_id,
            "state": self.state,
            "metadata": self.metadata,
            "parent_id": self.parent_id,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Checkpoint":
        return cls(
            checkpoint_id=data.get("checkpoint_id", ""),
            thread_id=data.get("thread_id", ""),
            agent_id=data.get("agent_id", ""),
            state=data.get("state", {}),
            metadata=data.get("metadata", {}),
            parent_id=data.get("parent_id", ""),
            created_at=data.get("created_at", 0.0),
        )


# Phase D 续 #2: ``MemoryEntry`` and ``MemoryCompact`` were
# deleted. Use ``Fact`` from ``nexus_core.memory.facts`` instead.


@dataclass
class Artifact:
    """
    A framework-agnostic artifact (versioned output).

    Represents a file or data blob produced by an agent —
    reports, code, analysis results, etc.
    """

    filename: str = ""
    data: bytes = b""
    version: int = 0
    content_type: str = ""
    agent_id: str = ""
    session_id: str = ""
    metadata: dict = field(default_factory=dict)
    content_hash: str = ""
    created_at: float = 0.0

    def __post_init__(self):
        if self.created_at == 0.0:
            self.created_at = time.time()


# ═══════════════════════════════════════════════════════════════════════
# Social Protocol Models — Impressions, Gossip, Profiles
# ═══════════════════════════════════════════════════════════════════════


@dataclass
class ImpressionDimensions:
    """
    Multi-dimensional evaluation of another agent.

    Each dimension is 0.0 - 1.0. Agents weight these differently
    based on their owner's preferences.
    """

    interest_overlap: float = 0.0           # Shared interests
    knowledge_complementarity: float = 0.0  # Does the other agent know things I don't?
    style_compatibility: float = 0.0        # Communication style match
    reliability: float = 0.0                # Accurate, specific, useful info?
    depth: float = 0.0                      # Substantive exchange vs small talk

    def to_dict(self) -> dict:
        return {
            "interest_overlap": self.interest_overlap,
            "knowledge_complementarity": self.knowledge_complementarity,
            "style_compatibility": self.style_compatibility,
            "reliability": self.reliability,
            "depth": self.depth,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "ImpressionDimensions":
        return cls(
            interest_overlap=data.get("interest_overlap", 0.0),
            knowledge_complementarity=data.get("knowledge_complementarity", 0.0),
            style_compatibility=data.get("style_compatibility", 0.0),
            reliability=data.get("reliability", 0.0),
            depth=data.get("depth", 0.0),
        )

    def mean(self) -> float:
        """Average across all dimensions."""
        vals = [
            self.interest_overlap,
            self.knowledge_complementarity,
            self.style_compatibility,
            self.reliability,
            self.depth,
        ]
        return sum(vals) / len(vals)


@dataclass
class Impression:
    """
    One agent's structured evaluation of another after a gossip session.

    Properties:
      - Asymmetric: A's impression of B ≠ B's impression of A
      - Cumulative: multiple gossip sessions → multiple impressions
      - LLM-generated: the agent analyzes the transcript against its persona/memory
      - Confidence-gated: outlier scores flagged for re-evaluation
    """

    impression_id: str = ""
    source_agent: str = ""          # who formed this impression
    target_agent: str = ""          # who was evaluated
    gossip_session_id: str = ""     # reference to the conversation

    dimensions: ImpressionDimensions = field(default_factory=ImpressionDimensions)
    compatibility_score: float = 0.0    # 0.0 - 1.0, weighted combination
    summary: str = ""                   # LLM-generated free-text
    would_gossip_again: bool = False
    recommend_to_network: bool = False

    created_at: float = 0.0

    def __post_init__(self):
        if not self.impression_id:
            self.impression_id = str(uuid.uuid4())
        if self.created_at == 0.0:
            self.created_at = time.time()

    def to_dict(self) -> dict:
        return {
            "impression_id": self.impression_id,
            "source_agent": self.source_agent,
            "target_agent": self.target_agent,
            "gossip_session_id": self.gossip_session_id,
            "dimensions": self.dimensions.to_dict(),
            "compatibility_score": self.compatibility_score,
            "summary": self.summary,
            "would_gossip_again": self.would_gossip_again,
            "recommend_to_network": self.recommend_to_network,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Impression":
        return cls(
            impression_id=data.get("impression_id", ""),
            source_agent=data.get("source_agent", ""),
            target_agent=data.get("target_agent", ""),
            gossip_session_id=data.get("gossip_session_id", ""),
            dimensions=ImpressionDimensions.from_dict(data.get("dimensions", {})),
            compatibility_score=data.get("compatibility_score", 0.0),
            summary=data.get("summary", ""),
            would_gossip_again=data.get("would_gossip_again", False),
            recommend_to_network=data.get("recommend_to_network", False),
            created_at=data.get("created_at", 0.0),
        )


@dataclass
class ImpressionSummary:
    """Lightweight view for ranking lists (like MemoryCompact for memory)."""

    agent_id: str = ""
    latest_score: float = 0.0
    gossip_count: int = 0
    last_gossip_at: float = 0.0
    top_dimension: str = ""         # which dimension scored highest
    would_gossip_again: bool = False


@dataclass
class NetworkStats:
    """Aggregated social statistics for an agent."""

    total_gossip_sessions: int = 0
    unique_agents_met: int = 0
    avg_compatibility_given: float = 0.0        # avg score this agent gives
    avg_compatibility_received: float = 0.0     # avg score this agent receives
    top_interests_overlap: list = field(default_factory=list)
    strongest_connections: list = field(default_factory=list)


@dataclass
class GossipMessage:
    """
    A single message in a gossip session.

    Each message is independently stored. Supports both sync (in-memory)
    and async (backend-persisted) transport.

    Privacy: gossip content must NOT contain private user data.
    Agents discuss interests, knowledge, and perspectives — not personal details.
    """

    message_id: str = ""
    session_id: str = ""
    sender: str = ""                # agent_id
    content: str = ""               # the message text
    sent_at: float = 0.0
    sequence: int = 0               # message order in session
    content_hash: str = ""          # SHA-256 (for on-chain anchoring, optional)

    def __post_init__(self):
        if not self.message_id:
            self.message_id = str(uuid.uuid4())
        if self.sent_at == 0.0:
            self.sent_at = time.time()

    def to_dict(self) -> dict:
        return {
            "message_id": self.message_id,
            "session_id": self.session_id,
            "sender": self.sender,
            "content": self.content,
            "sent_at": self.sent_at,
            "sequence": self.sequence,
            "content_hash": self.content_hash,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "GossipMessage":
        return cls(
            message_id=data.get("message_id", ""),
            session_id=data.get("session_id", ""),
            sender=data.get("sender", ""),
            content=data.get("content", ""),
            sent_at=data.get("sent_at", 0.0),
            sequence=data.get("sequence", 0),
            content_hash=data.get("content_hash", ""),
        )


@dataclass
class GossipSession:
    """
    An asynchronous conversation between two agents.

    Gossip is non-goal-oriented — agents exchange perspectives to
    test compatibility, not to complete a task.

    Transport modes:
      - sync:  messages stay in memory, no chain persistence (fast, ephemeral)
      - async: messages stored via the backend, hash-anchored (persistent, verifiable)

    The mode is chosen at session creation. sync mode is ideal when
    both agents are online simultaneously and don't need persistence.
    """

    session_id: str = ""
    initiator: str = ""             # agent_id who started
    responder: str = ""             # agent_id who accepted
    topic_hint: str = ""            # optional topic to seed conversation

    started_at: float = 0.0
    ended_at: float = 0.0           # 0 if still active
    turn_count: int = 0
    max_turns: int = 8              # configurable turn limit
    status: str = "pending"         # pending | active | concluded | expired

    messages: list = field(default_factory=list)  # list[GossipMessage]

    # Transport mode: "sync" (in-memory only) or "async" (backend-persisted)
    transport: str = "sync"

    # Post-gossip outputs (set after session concludes)
    initiator_impression_id: str = ""
    responder_impression_id: str = ""

    session_hash: str = ""          # SHA-256 → BSC anchor (optional)

    def __post_init__(self):
        if not self.session_id:
            self.session_id = str(uuid.uuid4())
        if self.started_at == 0.0:
            self.started_at = time.time()

    @property
    def is_active(self) -> bool:
        return self.status == "active"

    @property
    def is_concluded(self) -> bool:
        return self.status in ("concluded", "expired")

    @property
    def participants(self) -> tuple:
        return (self.initiator, self.responder)

    def to_dict(self) -> dict:
        return {
            "session_id": self.session_id,
            "initiator": self.initiator,
            "responder": self.responder,
            "topic_hint": self.topic_hint,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "turn_count": self.turn_count,
            "max_turns": self.max_turns,
            "status": self.status,
            "messages": [m.to_dict() if hasattr(m, "to_dict") else m for m in self.messages],
            "transport": self.transport,
            "initiator_impression_id": self.initiator_impression_id,
            "responder_impression_id": self.responder_impression_id,
            "session_hash": self.session_hash,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "GossipSession":
        messages = [
            GossipMessage.from_dict(m) if isinstance(m, dict) else m
            for m in data.get("messages", [])
        ]
        return cls(
            session_id=data.get("session_id", ""),
            initiator=data.get("initiator", ""),
            responder=data.get("responder", ""),
            topic_hint=data.get("topic_hint", ""),
            started_at=data.get("started_at", 0.0),
            ended_at=data.get("ended_at", 0.0),
            turn_count=data.get("turn_count", 0),
            max_turns=data.get("max_turns", 8),
            status=data.get("status", "pending"),
            messages=messages,
            transport=data.get("transport", "sync"),
            initiator_impression_id=data.get("initiator_impression_id", ""),
            responder_impression_id=data.get("responder_impression_id", ""),
            session_hash=data.get("session_hash", ""),
        )


@dataclass
class AgentProfile:
    """
    A voluntary, self-generated public profile for agent discovery.

    Generated by the agent from its memory, skills, and persona.
    Not a form the owner fills out — an emergent description of who they are.

    Privacy: interests and capabilities are coarse-grained tags,
    not raw memory content.
    """

    agent_id: str = ""
    owner: str = ""                 # BSC wallet address (optional)

    interests: list = field(default_factory=list)       # e.g. ["japanese_cuisine", "blockchain"]
    capabilities: list = field(default_factory=list)    # e.g. ["travel_planning", "code_review"]
    style_tags: list = field(default_factory=list)      # e.g. ["detail_oriented", "concise"]

    reputation: dict = field(default_factory=lambda: {
        "gossip_count": 0,
        "avg_compatibility": 0.0,
        "trust_percentile": 0,
    })

    visibility: str = "public"      # public | connections_only | private
    gossip_policy: str = "open"     # open | referral_only | manual

    profile_hash: str = ""          # SHA-256 → BSC anchor
    updated_at: float = 0.0

    def __post_init__(self):
        if self.updated_at == 0.0:
            self.updated_at = time.time()

    def to_dict(self) -> dict:
        return {
            "agent_id": self.agent_id,
            "owner": self.owner,
            "interests": self.interests,
            "capabilities": self.capabilities,
            "style_tags": self.style_tags,
            "reputation": self.reputation,
            "visibility": self.visibility,
            "gossip_policy": self.gossip_policy,
            "profile_hash": self.profile_hash,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "AgentProfile":
        return cls(
            agent_id=data.get("agent_id", ""),
            owner=data.get("owner", ""),
            interests=data.get("interests", []),
            capabilities=data.get("capabilities", []),
            style_tags=data.get("style_tags", []),
            reputation=data.get("reputation", {}),
            visibility=data.get("visibility", "public"),
            gossip_policy=data.get("gossip_policy", "open"),
            profile_hash=data.get("profile_hash", ""),
            updated_at=data.get("updated_at", 0.0),
        )
