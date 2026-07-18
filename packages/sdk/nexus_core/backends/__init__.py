"""
Nexus — Storage Backends.

    LocalBackend  — file-based, zero configuration
    MockBackend   — in-memory, for unit tests
"""

from .local import LocalBackend
from .mock import MockBackend

__all__ = ["LocalBackend", "MockBackend"]
