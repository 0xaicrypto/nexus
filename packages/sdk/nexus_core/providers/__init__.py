"""
Nexus — Concrete Provider Implementations.

Each provider implements the corresponding ABC from core.providers,
using a StorageBackend for actual persistence.

    SessionProviderImpl  — checkpoint save/load with parent linking
    ArtifactProviderImpl — versioned file storage with manifests
    TaskProviderImpl     — task lifecycle
"""

from .artifact import ArtifactProviderImpl
from .session import SessionProviderImpl
from .task import TaskProviderImpl

__all__ = [
    "SessionProviderImpl",
    "ArtifactProviderImpl",
    "TaskProviderImpl",
]
