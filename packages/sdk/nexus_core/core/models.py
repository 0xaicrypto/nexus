"""
Nexus — Framework-Agnostic Data Models.

    Checkpoint  — a point-in-time snapshot of agent state
    Artifact    — a versioned output file or data blob
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field


@dataclass
class Checkpoint:
    """A framework-agnostic state checkpoint."""

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


@dataclass
class Artifact:
    """A framework-agnostic artifact (versioned output)."""

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
