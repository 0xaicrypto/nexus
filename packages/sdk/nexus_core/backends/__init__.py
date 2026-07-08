"""
Nexus — Storage Backends (Strategy implementations).

    LocalBackend  — file-based, zero configuration
    ChainBackend  — local store + BSC anchoring, production
    MockBackend   — in-memory, for unit tests
"""

from .local import LocalBackend
from .mock import MockBackend

__all__ = ["LocalBackend", "MockBackend"]

# ChainBackend requires web3 — lazy import
try:
    from .chain import ChainBackend
    __all__.append("ChainBackend")
except ImportError:
    pass
