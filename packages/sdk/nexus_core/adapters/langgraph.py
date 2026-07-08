"""
LangGraph Adapter — bridges LangGraph's BaseCheckpointSaver to Rune Providers.

LangGraph uses a checkpoint-based persistence model where graph state is
saved at every step. This adapter implements BaseCheckpointSaver using
Rune's SessionProvider.

Usage:
    from nexus_core.rune_providers import create_provider
    from nexus_core.adapters.langgraph import RuneCheckpointer

    rune = create_provider(mode="local")
    checkpointer = RuneCheckpointer(rune.sessions)

    # Use with LangGraph
    graph = workflow.compile(checkpointer=checkpointer)
    config = {"configurable": {"thread_id": "conversation-1"}}
    result = graph.invoke({"messages": [...]}, config)

    # State persists to BNBChain — survives crashes, portable across machines.

LangGraph interface mapping:
    LangGraph                    Rune
    ─────────────────────────    ──────────────────────────
    put(config, checkpoint)  →   save_checkpoint()
    get_tuple(config)        →   load_checkpoint()
    list(config)             →   list_checkpoints()
    put_writes(...)          →   (included in checkpoint state)
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any, AsyncIterator, Iterator, Optional, Sequence

from ..core.models import Checkpoint
from ..core.providers import SessionProvider
from .registry import AdapterRegistry


class RuneCheckpointer:
    """
    LangGraph-compatible checkpoint saver backed by Rune.

    This implements the LangGraph BaseCheckpointSaver protocol using
    Rune's SessionProvider. Install langgraph to use the full
    typed interface; this class works without it as a duck-type
    implementation.

    Architecture:
        LangGraph graph.invoke()
            → RuneCheckpointer.put(config, checkpoint)
                → SessionProvider.save_checkpoint()
                    → object store (full state) + BSC (state root hash)

    The checkpoint includes the full graph state at each step,
    enabling:
      - Crash recovery (resume from last checkpoint)
      - Time travel (load any previous state)
      - Human-in-the-loop (pause, inspect, resume)
      - Multi-machine portability (any runtime can load from chain)
    """

    def __init__(
        self,
        session_provider: SessionProvider,
        agent_id: str = "langgraph-agent",
    ):
        """
        Args:
            session_provider: Rune session provider for persistence.
            agent_id: Agent identifier (maps to ERC-8004 tokenId).
        """
        self._provider = session_provider
        self._agent_id = agent_id

    # ── LangGraph BaseCheckpointSaver interface ────────────────────

    def put(
        self,
        config: dict,
        checkpoint: dict,
        metadata: Optional[dict] = None,
        new_versions: Optional[dict] = None,
    ) -> dict:
        """
        Save a checkpoint (LangGraph sync interface).

        Args:
            config: LangGraph config with {"configurable": {"thread_id": ...}}
            checkpoint: Graph state snapshot {"v": 1, "id": ..., "ts": ..., ...}
            metadata: Optional metadata (step number, source, etc.)
            new_versions: Channel version updates.

        Returns:
            Updated config with checkpoint_id.
        """
        thread_id = config.get("configurable", {}).get("thread_id", "default")
        checkpoint_id = checkpoint.get("id", str(uuid.uuid4()))
        parent_id = config.get("configurable", {}).get("checkpoint_id", "")

        cp = Checkpoint(
            checkpoint_id=checkpoint_id,
            thread_id=thread_id,
            agent_id=self._agent_id,
            state=checkpoint,
            metadata={
                **(metadata or {}),
                "framework": "langgraph",
                "new_versions": new_versions or {},
            },
            parent_id=parent_id,
        )

        # Run async provider in sync context
        _run_sync(self._provider.save_checkpoint(cp))

        return {
            "configurable": {
                "thread_id": thread_id,
                "checkpoint_ns": config.get("configurable", {}).get("checkpoint_ns", ""),
                "checkpoint_id": checkpoint_id,
            }
        }

    async def aput(
        self,
        config: dict,
        checkpoint: dict,
        metadata: Optional[dict] = None,
        new_versions: Optional[dict] = None,
    ) -> dict:
        """Save a checkpoint (LangGraph async interface)."""
        thread_id = config.get("configurable", {}).get("thread_id", "default")
        checkpoint_id = checkpoint.get("id", str(uuid.uuid4()))
        parent_id = config.get("configurable", {}).get("checkpoint_id", "")

        cp = Checkpoint(
            checkpoint_id=checkpoint_id,
            thread_id=thread_id,
            agent_id=self._agent_id,
            state=checkpoint,
            metadata={
                **(metadata or {}),
                "framework": "langgraph",
                "new_versions": new_versions or {},
            },
            parent_id=parent_id,
        )

        await self._provider.save_checkpoint(cp)

        return {
            "configurable": {
                "thread_id": thread_id,
                "checkpoint_ns": config.get("configurable", {}).get("checkpoint_ns", ""),
                "checkpoint_id": checkpoint_id,
            }
        }

    def get_tuple(self, config: dict) -> Optional[dict]:
        """
        Load the latest checkpoint for a thread (LangGraph sync interface).

        Returns:
            CheckpointTuple-like dict with config, checkpoint, metadata, parent_config.
        """
        thread_id = config.get("configurable", {}).get("thread_id", "default")
        checkpoint_id = config.get("configurable", {}).get("checkpoint_id")

        cp = _run_sync(self._provider.load_checkpoint(
            self._agent_id, thread_id, checkpoint_id,
        ))

        if cp is None:
            return None

        return {
            "config": {
                "configurable": {
                    "thread_id": thread_id,
                    "checkpoint_ns": "",
                    "checkpoint_id": cp.checkpoint_id,
                }
            },
            "checkpoint": cp.state,
            "metadata": cp.metadata,
            "parent_config": {
                "configurable": {
                    "thread_id": thread_id,
                    "checkpoint_ns": "",
                    "checkpoint_id": cp.parent_id,
                }
            } if cp.parent_id else None,
        }

    async def aget_tuple(self, config: dict) -> Optional[dict]:
        """Load latest checkpoint (async)."""
        thread_id = config.get("configurable", {}).get("thread_id", "default")
        checkpoint_id = config.get("configurable", {}).get("checkpoint_id")

        cp = await self._provider.load_checkpoint(
            self._agent_id, thread_id, checkpoint_id,
        )

        if cp is None:
            return None

        return {
            "config": {
                "configurable": {
                    "thread_id": thread_id,
                    "checkpoint_ns": "",
                    "checkpoint_id": cp.checkpoint_id,
                }
            },
            "checkpoint": cp.state,
            "metadata": cp.metadata,
            "parent_config": {
                "configurable": {
                    "thread_id": thread_id,
                    "checkpoint_ns": "",
                    "checkpoint_id": cp.parent_id,
                }
            } if cp.parent_id else None,
        }

    def list(
        self,
        config: Optional[dict] = None,
        *,
        filter: Optional[dict] = None,
        before: Optional[dict] = None,
        limit: int = 100,
    ) -> Iterator[dict]:
        """List checkpoints (sync)."""
        thread_id = None
        if config:
            thread_id = config.get("configurable", {}).get("thread_id")

        checkpoints = _run_sync(self._provider.list_checkpoints(
            self._agent_id, thread_id, limit=limit,
        ))

        for cp in checkpoints:
            yield {
                "config": {
                    "configurable": {
                        "thread_id": cp.thread_id,
                        "checkpoint_ns": "",
                        "checkpoint_id": cp.checkpoint_id,
                    }
                },
                "checkpoint": cp.state,
                "metadata": cp.metadata,
                "parent_config": {
                    "configurable": {
                        "thread_id": cp.thread_id,
                        "checkpoint_ns": "",
                        "checkpoint_id": cp.parent_id,
                    }
                } if cp.parent_id else None,
            }

    async def alist(
        self,
        config: Optional[dict] = None,
        *,
        filter: Optional[dict] = None,
        before: Optional[dict] = None,
        limit: int = 100,
    ) -> AsyncIterator[dict]:
        """List checkpoints (async)."""
        thread_id = None
        if config:
            thread_id = config.get("configurable", {}).get("thread_id")

        checkpoints = await self._provider.list_checkpoints(
            self._agent_id, thread_id, limit=limit,
        )

        for cp in checkpoints:
            yield {
                "config": {
                    "configurable": {
                        "thread_id": cp.thread_id,
                        "checkpoint_ns": "",
                        "checkpoint_id": cp.checkpoint_id,
                    }
                },
                "checkpoint": cp.state,
                "metadata": cp.metadata,
                "parent_config": {
                    "configurable": {
                        "thread_id": cp.thread_id,
                        "checkpoint_ns": "",
                        "checkpoint_id": cp.parent_id,
                    }
                } if cp.parent_id else None,
            }

    def put_writes(
        self,
        config: dict,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
    ) -> None:
        """
        Store pending writes (LangGraph interface).

        Writes are intermediate channel updates between steps.
        We include them in the next checkpoint's state.
        """
        # Pending writes are captured in the next put() call's checkpoint.
        # This is a no-op in the current implementation.
        pass

    async def aput_writes(
        self,
        config: dict,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
    ) -> None:
        """Store pending writes (async)."""
        pass


# ── Helpers ─────────────────────────────────────────────────────────


def _run_sync(coro):
    """Run an async coroutine from synchronous code."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)

    # If loop is running, we need a new thread
    import concurrent.futures
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(asyncio.run, coro)
        return future.result(timeout=60)


AdapterRegistry.register("langgraph", RuneCheckpointer)
