"""
CrewAI Adapter — bridges CrewAI's memory system to Rune Providers.

CrewAI uses a cognitive memory model with encode, recall, consolidate,
and forget operations. This adapter maps those to Rune's MemoryProvider
for persistent, verifiable memory on BNBChain.

Usage:
    from nexus_core.rune_providers import create_provider
    from nexus_core.adapters.crewai import RuneCrewStorage

    rune = create_provider(mode="local")
    storage = RuneCrewStorage(rune.memory, agent_id="my-crew-agent")

    # Use with CrewAI
    from crewai.memory import Memory
    memory = Memory(storage=storage)

CrewAI interface mapping:
    CrewAI                       Rune
    ─────────────────────────    ──────────────────────────
    memory.encode(content)   →   memory.add()
    memory.recall(query)     →   memory.search()
    memory.forget(id)        →   memory.delete()
    memory.consolidate()     →   (future: merge similar memories)
"""

from __future__ import annotations

import asyncio
from typing import Any, Optional

# ─────────────────────────────────────────────────────────────────────
# RuneCrewStorage — DELETED in Phase D 续 #2
# ─────────────────────────────────────────────────────────────────────
#
# The CrewAI memory bridge was removed when MemoryProvider was
# deleted. CrewAI integration now needs to talk to the typed Phase J
# namespace stores directly (``FactsStore`` / etc.). If/when CrewAI
# Memory parity is rebuilt, it should be a thin wrapper over those
# typed stores — not a re-introduction of the old MemoryProvider.


class RuneCrewCheckpointStorage:
    """
    CrewAI-compatible task output storage backed by Rune.

    Persists CrewAI task outputs (raw text, JSON, Pydantic models)
    as versioned artifacts on BNBChain.

    Usage:
        from nexus_core.adapters.crewai import RuneCrewCheckpointStorage

        storage = RuneCrewCheckpointStorage(rune.artifacts, agent_id="my-crew")
        # CrewAI can use this to persist task results
    """

    def __init__(self, artifact_provider, agent_id: str = "crewai-agent"):
        self._provider = artifact_provider
        self._agent_id = agent_id

    def save(self, task_id: str, output: Any) -> int:
        """Save a task output as a versioned artifact."""
        data = str(output).encode("utf-8") if not isinstance(output, bytes) else output
        return _run_sync(self._provider.save(
            filename=f"task_{task_id}_output",
            data=data,
            agent_id=self._agent_id,
            content_type="application/json",
        ))

    def load(self, task_id: str, version: Optional[int] = None) -> Optional[str]:
        """Load a task output."""
        artifact = _run_sync(self._provider.load(
            filename=f"task_{task_id}_output",
            agent_id=self._agent_id,
            version=version,
        ))
        if artifact is None:
            return None
        return artifact.data.decode("utf-8")


# ── Helpers ─────────────────────────────────────────────────────────


def _run_sync(coro):
    """Run an async coroutine from synchronous code."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)

    import concurrent.futures
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(asyncio.run, coro)
        return future.result(timeout=60)


# Phase D 续 #2: ``RuneCrewStorage`` was removed; the adapter
# registration is gone with it. Re-register here once a typed-store
# CrewAI memory bridge exists.
