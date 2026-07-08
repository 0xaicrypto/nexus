"""
nexus_core — top-level entry points + Builder.

The 80% case is the four module-level factory functions:

    import nexus_core

    rt = nexus_core.local()                          # Zero config, file-backed
    rt = nexus_core.testnet(private_key="0x...")     # BSC testnet anchoring
    rt = nexus_core.mainnet(private_key="0x...")     # BSC mainnet anchoring
    rt = nexus_core.builder().mock_backend().build() # Unit tests / custom config

Each returns an :class:`AgentRuntime` — the 5-provider facade
(``sessions``, ``memory``, ``artifacts``, ``tasks``, ``impressions``)
backed by a single :class:`StorageBackend`. The ``backend`` is
exposed on the runtime for low-level callers.

For complex configuration, :func:`builder` returns a fluent
:class:`Builder`:

    rt = (
        nexus_core.builder()
        .backend(my_custom_backend)
        .flush_policy(FlushPolicy.aggressive())
        .runtime_id("prod-runtime-1")
        .build()
    )

Phase H note — the previous public surface used a static-factory
class ``Rune`` (``Rune.local()`` / ``Rune.testnet()`` /
``Rune.builder()``). That class is gone; call the module-level
functions directly. ``RuneBuilder`` was renamed to :class:`Builder`,
and the returned facade ``RuneProvider`` to :class:`AgentRuntime`.
"""

from __future__ import annotations

from typing import Optional

from .core.backend import StorageBackend
from .core.flush import FlushPolicy
from .core.providers import AgentRuntime
from .providers.session import SessionProviderImpl
from .providers.artifact import ArtifactProviderImpl
from .providers.task import TaskProviderImpl
from .providers.impression import ImpressionProviderImpl


# ── Module-level factory functions (the 80% surface) ──────────────────


def local(base_dir: str = ".nexus_state") -> AgentRuntime:
    """Create a local-mode runtime. Zero config, no blockchain.

    Perfect for development, testing, and demos. All data stored
    as files under ``base_dir``.

    Args:
        base_dir: Directory for local storage (default: ``.nexus_state``).

    Returns:
        An :class:`AgentRuntime` backed by :class:`LocalBackend`.
    """
    return builder().local_backend(base_dir).build()


def testnet(private_key: str, **kwargs) -> AgentRuntime:
    """Create a testnet-mode runtime — anchored on BSC testnet.

    Requires:
      - BNB testnet tokens (from a faucet).
      - Deployed contracts (AgentStateExtension + TaskStateManager).

    Args:
        private_key: BSC wallet private key (0x-prefixed hex).
        **kwargs: Additional :class:`ChainBackend` options
            (``rpc_url``, ``agent_state_address``,
            ``task_manager_address``, ``identity_registry_address``).

    Returns:
        An :class:`AgentRuntime` backed by :class:`ChainBackend`.
    """
    return builder().chain_backend(private_key, network="testnet", **kwargs).build()


def mainnet(private_key: str, **kwargs) -> AgentRuntime:
    """Create a mainnet-mode runtime — anchored on BSC mainnet.

    Args:
        private_key: BSC wallet private key.
        **kwargs: Additional :class:`ChainBackend` options.

    Returns:
        An :class:`AgentRuntime` backed by :class:`ChainBackend`.
    """
    return builder().chain_backend(private_key, network="mainnet", **kwargs).build()


def builder() -> "Builder":
    """Start building a custom runtime configuration.

    Returns:
        A fluent :class:`Builder`.
    """
    return Builder()


# ── Builder ───────────────────────────────────────────────────────────


class Builder:
    """Fluent builder for :class:`AgentRuntime`.

    Use this when the simple factory functions (:func:`local`,
    :func:`testnet`, :func:`mainnet`) don't fit — e.g. custom
    flush policy, an injected backend, or a specific runtime id
    for multi-runtime scenarios.

    Usage::

        import nexus_core
        rt = (
            nexus_core.builder()
            .backend(my_custom_backend)
            .flush_policy(FlushPolicy.aggressive())
            .runtime_id("prod-runtime-1")
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

    def chain_backend(self, private_key: str, network: str = "testnet", **kwargs) -> "Builder":
        """Use :class:`ChainBackend` (local store + BSC anchoring)."""
        from .backends.chain import ChainBackend
        self._backend = ChainBackend(private_key=private_key, network=network, **kwargs)
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
            impressions=ImpressionProviderImpl(self._backend),
            backend=self._backend,
        )
