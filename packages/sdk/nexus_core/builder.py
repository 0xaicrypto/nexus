"""
nexus_core — top-level entry points + Builder.

The 80% case is two module-level factory functions:

    import nexus_core

    rt = nexus_core.local()                          # Zero config, file-backed
    rt = nexus_core.builder().mock_backend().build() # Unit tests / custom config

Each returns an :class:`AgentRuntime` backed by a single
:class:`StorageBackend`.

For complex configuration, :func:`builder` returns a fluent
:class:`Builder`:

    rt = (
        nexus_core.builder()
        .backend(my_custom_backend)
        .flush_policy(FlushPolicy.aggressive())
        .runtime_id("prod-runtime-1")
        .build()
    )
"""

from __future__ import annotations

from typing import Optional

from .core.backend import StorageBackend
from .core.flush import FlushPolicy
from .core.providers import AgentRuntime
from .providers.artifact import ArtifactProviderImpl
from .providers.session import SessionProviderImpl
from .providers.task import TaskProviderImpl

# ── Module-level factory functions (the 80% surface) ──────────────────


def local(base_dir: str = ".nexus_state") -> AgentRuntime:
    """Create a local-mode runtime. Zero config. All data stored
    as files under ``base_dir``.

    Args:
        base_dir: Directory for local storage (default: ``.nexus_state``).

    Returns:
        An :class:`AgentRuntime` backed by :class:`LocalBackend`.
    """
    return builder().local_backend(base_dir).build()


def builder() -> "Builder":
    """Start building a custom runtime configuration.

    Returns:
        A fluent :class:`Builder`.
    """
    return Builder()


# ── Builder ───────────────────────────────────────────────────────────


class Builder:
    """Fluent builder for :class:`AgentRuntime`.

    Use this when the simple factory functions (:func:`local`)
    don't fit — e.g. custom
    flush policy, an injected backend, or a specific runtime id
    for multi-runtime scenarios.

    Usage::

        import nexus_core
        rt = (
            nexus_core.builder()
            .local_backend(".nexus_state")
            .flush_policy(FlushPolicy.aggressive())
            .build()
        )
    """

    def __init__(self):
        self._backend: Optional[StorageBackend] = None
        self._flush_policy: FlushPolicy = FlushPolicy.balanced()
        self._runtime_id: Optional[str] = None

    def backend(self, backend: StorageBackend) -> "Builder":
        """Set a custom storage backend."""
        self._backend = backend
        return self

    def local_backend(self, base_dir: str = ".nexus_state") -> "Builder":
        """Use :class:`LocalBackend` (file-based, zero config)."""
        from .backends.local import LocalBackend
        self._backend = LocalBackend(base_dir=base_dir)
        return self

    def mock_backend(self) -> "Builder":
        """Use :class:`MockBackend` (in-memory, for tests)."""
        from .backends.mock import MockBackend
        self._backend = MockBackend()
        return self

    def flush_policy(self, policy: FlushPolicy) -> "Builder":
        """Set the flush policy for write batching."""
        self._flush_policy = policy
        return self

    def runtime_id(self, rid: str) -> "Builder":
        """Set the runtime identifier (for multi-runtime scenarios)."""
        self._runtime_id = rid
        return self

    def build(self) -> AgentRuntime:
        """Build the :class:`AgentRuntime` with all configured options.

        If no backend was set, defaults to :class:`LocalBackend`.
        """
        if self._backend is None:
            from .backends.local import LocalBackend
            self._backend = LocalBackend()

        return AgentRuntime(
            sessions=SessionProviderImpl(
                self._backend,
                runtime_id=self._runtime_id,
            ),
            artifacts=ArtifactProviderImpl(self._backend),
            tasks=TaskProviderImpl(self._backend),
            backend=self._backend,
        )
