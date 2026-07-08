"""
SessionProviderImpl — concrete SessionProvider backed by StorageBackend.

Domain logic:
  - Checkpoint parent linking (builds a history chain)
  - In-memory cache for fast reads
  - On-chain anchoring after each save
  - Path management for the storage layout

Storage layout:
    agents/{agent_id}/sessions/{thread_id}/{checkpoint_id}.json
"""

from __future__ import annotations

import uuid
from typing import Optional

from ..core.backend import StorageBackend
from ..core.models import Checkpoint
from ..core.providers import SessionProvider


class SessionProviderImpl(SessionProvider):
    """
    Concrete session/checkpoint provider.

    Stores checkpoints as JSON via StorageBackend.
    Maintains an in-memory cache for fast reads.
    """

    def __init__(
        self,
        backend: StorageBackend,
        runtime_id: Optional[str] = None,
    ):
        self._backend = backend
        self._runtime_id = runtime_id or f"runtime-{uuid.uuid4().hex[:8]}"
        # In-memory cache: (agent_id, thread_id) -> list[Checkpoint]
        self._checkpoints: dict[tuple[str, str], list[Checkpoint]] = {}

    @staticmethod
    def _safe(value: str) -> str:
        """Sanitize a path component to prevent directory traversal."""
        return value.replace("/", "__").replace("\\", "__").replace("..", "__")

    def _path(self, agent_id: str, thread_id: str, checkpoint_id: str) -> str:
        return f"agents/{self._safe(agent_id)}/sessions/{self._safe(thread_id)}/{self._safe(checkpoint_id)}.json"

    async def save_checkpoint(self, checkpoint: Checkpoint) -> str:
        key = (checkpoint.agent_id, checkpoint.thread_id)

        if key not in self._checkpoints:
            self._checkpoints[key] = []

        # Auto-link to parent
        existing = self._checkpoints[key]
        if existing and not checkpoint.parent_id:
            checkpoint.parent_id = existing[-1].checkpoint_id

        self._checkpoints[key].append(checkpoint)

        # Persist via backend
        path = self._path(checkpoint.agent_id, checkpoint.thread_id, checkpoint.checkpoint_id)
        content_hash = await self._backend.store_json(path, checkpoint.to_dict())

        # Anchor on-chain
        await self._backend.anchor(checkpoint.agent_id, content_hash, namespace="state")

        return checkpoint.checkpoint_id

    async def load_checkpoint(
        self,
        agent_id: str,
        thread_id: str,
        checkpoint_id: Optional[str] = None,
    ) -> Optional[Checkpoint]:
        key = (agent_id, thread_id)

        # Try in-memory cache
        if key in self._checkpoints and self._checkpoints[key]:
            if checkpoint_id is None:
                return self._checkpoints[key][-1]
            for cp in self._checkpoints[key]:
                if cp.checkpoint_id == checkpoint_id:
                    return cp

        # Try loading from backend via anchor
        state_hash = await self._backend.resolve(agent_id, namespace="state")
        if state_hash:
            # List paths under this thread to find checkpoints
            prefix = f"agents/{self._safe(agent_id)}/sessions/{self._safe(thread_id)}/"
            paths = await self._backend.list_paths(prefix)
            if paths:
                # Load the last one (most recent by path sort)
                target = paths[-1] if checkpoint_id is None else None
                for p in paths:
                    if checkpoint_id and p.endswith(f"/{checkpoint_id}.json"):
                        target = p
                        break
                if target:
                    data = await self._backend.load_json(target)
                    if data:
                        cp = Checkpoint.from_dict(data)
                        if key not in self._checkpoints:
                            self._checkpoints[key] = []
                        # Avoid duplicate entries in cache
                        if not any(c.checkpoint_id == cp.checkpoint_id for c in self._checkpoints[key]):
                            self._checkpoints[key].append(cp)
                        return cp

        return None

    async def list_checkpoints(
        self,
        agent_id: str,
        thread_id: Optional[str] = None,
        limit: int = 100,
    ) -> list[Checkpoint]:
        # If cache is empty for this agent, try loading from backend
        has_cached = any(aid == agent_id for (aid, _) in self._checkpoints)
        if not has_cached:
            await self._load_from_backend(agent_id, thread_id)

        results = []
        for (aid, tid), cps in self._checkpoints.items():
            if aid != agent_id:
                continue
            if thread_id and tid != thread_id:
                continue
            results.extend(cps)
        results.sort(key=lambda c: c.created_at, reverse=True)
        return results[:limit]

    async def _load_from_backend(self, agent_id: str, thread_id: Optional[str] = None) -> None:
        """Load checkpoints from backend into cache (for session recovery)."""
        prefix = f"agents/{self._safe(agent_id)}/sessions/"
        if thread_id:
            prefix += f"{self._safe(thread_id)}/"

        try:
            paths = await self._backend.list_paths(prefix)
        except Exception:
            return

        for p in paths:
            try:
                data = await self._backend.load_json(p)
                if not data:
                    continue
                cp = Checkpoint.from_dict(data)
                key = (cp.agent_id, cp.thread_id)
                if key not in self._checkpoints:
                    self._checkpoints[key] = []
                if not any(c.checkpoint_id == cp.checkpoint_id for c in self._checkpoints[key]):
                    self._checkpoints[key].append(cp)
            except Exception:
                continue

    async def delete_checkpoint(
        self,
        agent_id: str,
        thread_id: str,
        checkpoint_id: Optional[str] = None,
    ) -> None:
        key = (agent_id, thread_id)
        if checkpoint_id is None:
            # Delete all checkpoints for this thread
            if key in self._checkpoints:
                for cp in self._checkpoints[key]:
                    path = self._path(agent_id, thread_id, cp.checkpoint_id)
                    await self._backend.delete(path)
                del self._checkpoints[key]
        elif key in self._checkpoints:
            path = self._path(agent_id, thread_id, checkpoint_id)
            await self._backend.delete(path)
            self._checkpoints[key] = [
                cp for cp in self._checkpoints[key]
                if cp.checkpoint_id != checkpoint_id
            ]

    async def flush(self, agent_id: str) -> None:
        # Current implementation writes on every save_checkpoint.
        # This hook exists for future batching.
        pass
