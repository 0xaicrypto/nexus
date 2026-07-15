"""
Nexus — Core abstractions.

This package defines the foundational interfaces and data models
that all other layers build upon:

  - models.py    — Framework-agnostic data models (Checkpoint, Artifact, …)
  - backend.py   — StorageBackend ABC (Strategy pattern for local/chain/mock)
  - providers.py — Provider ABCs (SessionProvider, ArtifactProvider, …)
  - flush.py     — FlushPolicy, FlushBuffer, WriteAheadLog

Phase D 续 #2: ``MemoryProvider`` ABC + ``MemoryEntry`` /
``MemoryCompact`` were deleted. Use the typed Phase J namespace
stores from ``nexus_core.memory`` instead.
"""

from .backend import StorageBackend
from .flush import FlushBuffer, FlushPolicy, WriteAheadLog
from .models import Artifact, Checkpoint
from .providers import (
    AgentRuntime,
    ArtifactProvider,
    SessionProvider,
    TaskProvider,
)

__all__ = [
    "Checkpoint",
    "Artifact",
    "StorageBackend",
    "SessionProvider",
    "ArtifactProvider",
    "TaskProvider",
    "AgentRuntime",
    "FlushPolicy",
    "FlushBuffer",
    "WriteAheadLog",
]
