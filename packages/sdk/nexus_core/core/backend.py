"""
Nexus — StorageBackend ABC (Strategy Pattern).

Defines HOW data is stored and retrieved. Each backend implements
the same interface but with different storage engines:

    LocalBackend  — file-based, zero config, for development
    MockBackend   — in-memory, for unit tests

Providers (SessionProvider, ArtifactProvider, etc.) depend on this
interface — they never know whether data goes to local files or in-memory.

Design: Strategy Pattern
    - StorageBackend is the Strategy interface
    - LocalBackend / MockBackend are Concrete Strategies
    - Providers are the Context that uses the Strategy
"""

from __future__ import annotations

import hashlib
import json
from abc import ABC, abstractmethod
from typing import Optional


class StorageBackend(ABC):
    """
    Abstract storage backend — the Strategy interface.

    Responsibilities:
      1. Store/load JSON payloads (sessions, memory indices, manifests)
      2. Store/load binary blobs (artifacts)

    NOT responsible for:
       - Domain logic (checkpoint linking, version management, semantic search)
       - Framework-specific type conversions
       - Flush batching (handled by FlushBuffer at the provider level)
    """

    # ── JSON payloads ───────────────────────────────────────────────

    @abstractmethod
    async def store_json(self, path: str, data: dict) -> str:
        """
        Store a JSON-serializable dict. Returns SHA-256 content hash.

        Args:
            path: Structured storage path (e.g. "agents/{id}/sessions/{hash}.json")
            data: JSON-serializable dict to store.

        Returns:
            SHA-256 hex digest of the stored content.
        """
        ...

    @abstractmethod
    async def load_json(self, path: str) -> Optional[dict]:
        """
        Load a JSON payload by path.

        Returns None if the path does not exist.
        """
        ...

    # ── Binary blobs ────────────────────────────────────────────────

    @abstractmethod
    async def store_blob(self, path: str, data: bytes) -> str:
        """
        Store raw bytes. Returns SHA-256 content hash.

        Args:
            path: Structured storage path.
            data: Raw bytes to store.

        Returns:
            SHA-256 hex digest of the stored content.
        """
        ...

    @abstractmethod
    async def load_blob(self, path: str) -> Optional[bytes]:
        """
        Load raw bytes by path.

        Returns None if the path does not exist.
        """
        ...

    # ── Listing ─────────────────────────────────────────────────────

    @abstractmethod
    async def list_paths(self, prefix: str) -> list[str]:
        """
        List all storage paths under a prefix.

        Args:
            prefix: Path prefix (e.g. "agents/{id}/artifacts/")

        Returns:
            List of full paths matching the prefix.
        """
        ...

    # ── Deletion ────────────────────────────────────────────────────

    async def delete(self, path: str) -> bool:
        """
        Delete a stored object by path.

        Returns True if something was deleted, False if path didn't exist.
        Default implementation is a no-op (not all backends support deletion).
        """
        return False

    # ── Lifecycle ───────────────────────────────────────────────────

    async def close(self) -> None:
        """Release any resources (connections, file handles, etc.)."""
        pass

    # ── Helpers ─────────────────────────────────────────────────────

    @staticmethod
    def content_hash(data: bytes) -> str:
        """Compute SHA-256 content hash."""
        return hashlib.sha256(data).hexdigest()

    @staticmethod
    def json_bytes(data: dict) -> bytes:
        """Serialize dict to deterministic JSON bytes."""
        return json.dumps(data, default=str, sort_keys=True).encode("utf-8")
