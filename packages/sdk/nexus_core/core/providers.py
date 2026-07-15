"""
Nexus — Provider ABCs + AgentRuntime Facade.

Provider ABCs define WHAT operations are available (domain logic).
They depend on StorageBackend for HOW data is stored.

    SessionProvider     — checkpoint / resume / crash recovery
    ArtifactProvider    — versioned output storage
    TaskProvider        — task lifecycle tracking (A2A)
    ImpressionProvider  — peer-to-peer attestation
    AgentRuntime        — Facade bundling the providers

Framework adapters (ADK, LangGraph, CrewAI) consume these interfaces.
They never see StorageBackend directly.

Phase D 续 #2: ``MemoryProvider`` was removed. Long-term knowledge
persistence is now handled by the typed Phase J namespace stores
(``FactsStore`` / ``EpisodesStore`` / ``SkillsStore`` /
``PersonaStore`` / ``KnowledgeStore``) which live in
``nexus_core.memory`` and chain-mirror via ``VersionedStore``.

Design:
    - Provider ABCs = Template Method (define the contract)
    - AgentRuntime  = Facade (single entry point for users)
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Optional

from .models import (
    Artifact,
    Checkpoint,
    Impression,
    ImpressionSummary,
    NetworkStats,
)

# ═══════════════════════════════════════════════════════════════════════
# Session Provider
# ═══════════════════════════════════════════════════════════════════════


class SessionProvider(ABC):
    """
    Framework-agnostic session/checkpoint persistence.

    Maps to:
      - Google ADK:  BaseSessionService
      - LangGraph:   BaseCheckpointSaver (put/get_tuple/list)
      - CrewAI:      Task state persistence
      - AutoGen:     Agent state checkpointing
    """

    @abstractmethod
    async def save_checkpoint(self, checkpoint: Checkpoint) -> str:
        """Save a state checkpoint. Returns the checkpoint_id."""
        ...

    @abstractmethod
    async def load_checkpoint(
        self,
        agent_id: str,
        thread_id: str,
        checkpoint_id: Optional[str] = None,
    ) -> Optional[Checkpoint]:
        """Load a checkpoint. If checkpoint_id is None, loads the latest."""
        ...

    @abstractmethod
    async def list_checkpoints(
        self,
        agent_id: str,
        thread_id: Optional[str] = None,
        limit: int = 100,
    ) -> list[Checkpoint]:
        """List checkpoints for an agent, optionally filtered by thread."""
        ...

    @abstractmethod
    async def delete_checkpoint(
        self,
        agent_id: str,
        thread_id: str,
        checkpoint_id: Optional[str] = None,
    ) -> None:
        """Delete a checkpoint (or all checkpoints for a thread)."""
        ...

    async def flush(self, agent_id: str) -> None:
        """Force-flush any buffered state to storage."""
        pass


# Phase D 续 #2: ``MemoryProvider`` ABC, ``MemoryEntry``, and
# ``MemoryCompact`` were deleted. Memory storage is now handled by
# the typed Phase J namespace stores (FactsStore / EpisodesStore /
# SkillsStore / PersonaStore / KnowledgeStore) which live in
# ``nexus_core.memory`` and chain-mirror via ``VersionedStore``.
# Existing data (if any) can be migrated with
# ``nexus_core.migrations.memory_to_facts.migrate``.


# ═══════════════════════════════════════════════════════════════════════
# Artifact Provider
# ═══════════════════════════════════════════════════════════════════════


class ArtifactProvider(ABC):
    """
    Framework-agnostic artifact (versioned output) persistence.

    Maps to:
      - Google ADK:  BaseArtifactService
      - LangGraph:   (artifacts are state values)
      - CrewAI:      TaskOutput
    """

    @abstractmethod
    async def save(
        self,
        filename: str,
        data: bytes,
        agent_id: str,
        session_id: str = "",
        content_type: str = "",
        metadata: Optional[dict] = None,
    ) -> int:
        """Save an artifact. Returns the version number."""
        ...

    @abstractmethod
    async def load(
        self,
        filename: str,
        agent_id: str,
        session_id: str = "",
        version: Optional[int] = None,
    ) -> Optional[Artifact]:
        """Load an artifact. If version is None, loads the latest."""
        ...

    @abstractmethod
    async def list_artifacts(
        self,
        agent_id: str,
        session_id: str = "",
    ) -> list[str]:
        """List all artifact filenames for an agent/session."""
        ...

    @abstractmethod
    async def list_versions(
        self,
        filename: str,
        agent_id: str,
        session_id: str = "",
    ) -> list[int]:
        """List all versions of a specific artifact."""
        ...

    async def rollback(
        self,
        filename: str,
        agent_id: str,
        to_version: int,
        session_id: str = "",
    ) -> int:
        """Rollback to a previous version. Returns new version number.

        Creates a NEW version with the content of to_version,
        preserving full history (never deletes versions).
        Default implementation loads old version and re-saves.
        """
        old = await self.load(filename, agent_id, session_id, version=to_version)
        if old is None:
            raise ValueError(f"Version {to_version} not found for {filename}")
        return await self.save(
            filename, old.data, agent_id, session_id,
            old.content_type, {"rollback_from": to_version},
        )


# ═══════════════════════════════════════════════════════════════════════
# Task Provider
# ═══════════════════════════════════════════════════════════════════════


class TaskProvider(ABC):
    """
    Framework-agnostic task lifecycle tracking.

    Maps to:
      - A2A Protocol: TaskStore
      - LangGraph:    Thread status tracking
      - CrewAI:       Task execution lifecycle
    """

    @abstractmethod
    async def create_task(
        self,
        task_id: str,
        agent_id: str,
        metadata: Optional[dict] = None,
    ) -> dict:
        """Create a new task. Returns task record."""
        ...

    @abstractmethod
    async def update_task(
        self,
        task_id: str,
        state: dict,
        status: str = "running",
    ) -> dict:
        """Update task state. Returns updated record."""
        ...

    @abstractmethod
    async def get_task(self, task_id: str) -> Optional[dict]:
        """Get task record by ID."""
        ...


# ═══════════════════════════════════════════════════════════════════════
# Impression Provider (Social Protocol)
# ═══════════════════════════════════════════════════════════════════════


class ImpressionProvider(ABC):
    """
    Framework-agnostic impression persistence (Social Protocol).

    The 5th Rune provider — manages agent-to-agent impressions
    formed through gossip sessions. Designed with relational query
    semantics (by target, by source, mutual, ranked) rather than
    content-similarity search.

    Impressions are:
      - Asymmetric: A's impression of B ≠ B's impression of A
      - Cumulative: multiple sessions → multiple impressions
      - Multi-dimensional: 5 scored dimensions + overall compatibility
    """

    @abstractmethod
    async def record(self, impression: Impression) -> str:
        """Store a new impression after a gossip session. Returns impression_id."""
        ...

    @abstractmethod
    async def get_impressions_of(
        self,
        target_agent: str,
        agent_id: str,
        limit: int = 10,
    ) -> list[Impression]:
        """
        All impressions this agent (agent_id) has formed about target_agent.
        Ordered by recency.
        """
        ...

    @abstractmethod
    async def get_impressions_from(
        self,
        agent_id: str,
        limit: int = 20,
    ) -> list[Impression]:
        """
        All impressions others have formed about this agent.
        The 'inbound' view — how the network sees you.
        """
        ...

    @abstractmethod
    async def get_compatibility(
        self,
        agent_a: str,
        agent_b: str,
    ) -> Optional[float]:
        """
        Latest compatibility score between two agents (A's view of B).
        Returns None if they've never interacted.
        """
        ...

    @abstractmethod
    async def get_top_matches(
        self,
        agent_id: str,
        top_k: int = 10,
        min_score: float = 0.0,
        dimension: Optional[str] = None,
    ) -> list[ImpressionSummary]:
        """
        Agents ranked by compatibility with this agent.
        Optionally filter by a specific dimension.
        """
        ...

    @abstractmethod
    async def get_mutual(
        self,
        agent_id: str,
        min_score: float = 0.5,
    ) -> list[tuple]:
        """
        Agents with mutual positive impressions.
        Returns list of (other_agent_id, my_score_of_them, their_score_of_me).
        """
        ...

    @abstractmethod
    async def get_network_stats(self, agent_id: str) -> NetworkStats:
        """Aggregated social statistics for an agent."""
        ...


# ═══════════════════════════════════════════════════════════════════════
# Facade: AgentRuntime
# ═══════════════════════════════════════════════════════════════════════


class AgentRuntime:
    """
    Facade: the single object users interact with.

    Bundles the framework providers behind a clean interface.
    No internal implementation details are exposed.

    Phase D 续 #2: ``memory`` was removed. Memory storage is now
    handled by the typed Phase J namespace stores (FactsStore /
    EpisodesStore / SkillsStore / PersonaStore / KnowledgeStore)
    which are constructed inside ``DigitalTwin`` and chained
    through to chain mirroring via ``VersionedStore``.

    Usage:
        rune = nexus_core.local()

        await rune.sessions.save_checkpoint(checkpoint)
        version = await rune.artifacts.save("report.json", data, agent_id="my-agent")
        await rune.impressions.record(impression)
    """

    def __init__(
        self,
        sessions: SessionProvider,
        artifacts: ArtifactProvider,
        tasks: TaskProvider,
        impressions: Optional[ImpressionProvider] = None,
        backend: Optional[Any] = None,
    ):
        self.sessions: SessionProvider = sessions
        self.artifacts: ArtifactProvider = artifacts
        self.tasks: TaskProvider = tasks
        self.impressions: Optional[ImpressionProvider] = impressions
        self._backend = backend  # held for lifecycle (close/flush)

    async def close(self) -> None:
        """Drain pending writes and release resources.

        Calls backend.close() which, for ChainBackend, waits for
        pending background anchor tasks to finish before shutting down.
        """
        if self._backend is not None and hasattr(self._backend, "close"):
            await self._backend.close()

    async def __aenter__(self) -> "AgentRuntime":
        return self

    async def __aexit__(self, *args) -> None:
        await self.close()
