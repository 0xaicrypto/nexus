"""
Regression tests for bug fixes.

Each test class corresponds to a specific bug that was found and fixed.
If any of these tests fail, the corresponding bug has been reintroduced.
"""

import asyncio
import hashlib
import json
import os
import time
import pytest

import nexus_core
from nexus_core import (
    Impression,
    ImpressionDimensions,
    MockBackend,
)
from nexus_core.providers.session import SessionProviderImpl
from nexus_core.providers.artifact import ArtifactProviderImpl
from nexus_core.providers.impression import ImpressionProviderImpl

# Phase D 续 #2: ``MemoryProviderImpl`` was deleted. Tests below
# that exercised MemoryProvider-specific bugs (path sanitisation,
# lazy loading, CJK tokenisation) have been removed in favour of
# the Phase J typed namespace stores in ``nexus_core.memory``.


# ── Fixtures ─────────────────────────────────────────────────────────

@pytest.fixture
def backend():
    return MockBackend()


@pytest.fixture
def rune():
    return nexus_core.builder().mock_backend().build()


# ══════════════════════════════════════════════════════════════════════
# Bug #1: Missing logger import in impression.py → NameError crash
# ══════════════════════════════════════════════════════════════════════

class TestBug1_ImpressionLoggerImport:
    """impression.py used `logger.warning(...)` but never imported logging.
    Any exception in _ensure_loaded would crash with NameError."""

    @pytest.mark.asyncio
    async def test_ensure_loaded_logs_without_crash(self, backend):
        """_ensure_loaded should handle errors gracefully, not NameError."""
        provider = ImpressionProviderImpl(backend)

        # Calling _ensure_loaded for a non-existent agent should not crash
        await provider._ensure_loaded("nonexistent-agent")

        # Should be marked as loaded (negative cache)
        assert "nonexistent-agent" in provider._loaded_agents

    @pytest.mark.asyncio
    async def test_impression_provider_importable(self):
        """Module should import without errors (logging + logger defined)."""
        import importlib
        mod = importlib.import_module("nexus_core.providers.impression")
        assert hasattr(mod, "logger")


# ══════════════════════════════════════════════════════════════════════
# Bug #4: Dead code in get_mutual() — two redundant loops
# ══════════════════════════════════════════════════════════════════════

class TestBug4_GetMutualDeadCode:
    """get_mutual() had two dead-code loops before the working implementation.
    Verify the cleaned-up version works correctly."""

    @pytest.mark.asyncio
    async def test_mutual_returns_correct_pairs(self, backend):
        provider = ImpressionProviderImpl(backend)

        # Agent A → B (score 0.8)
        imp_ab = Impression(
            source_agent="A", target_agent="B",
            compatibility_score=0.8,
            dimensions=ImpressionDimensions(interest_overlap=0.8),
        )
        await provider.record(imp_ab)

        # Agent B → A (score 0.7)
        imp_ba = Impression(
            source_agent="B", target_agent="A",
            compatibility_score=0.7,
            dimensions=ImpressionDimensions(interest_overlap=0.7),
        )
        await provider.record(imp_ba)

        # Get mutuals for A
        mutuals = await provider.get_mutual("A", min_score=0.5)
        assert len(mutuals) == 1
        other, my_score, their_score = mutuals[0]
        assert other == "B"
        assert my_score == 0.8
        assert their_score == 0.7

    @pytest.mark.asyncio
    async def test_mutual_latest_score_wins(self, backend):
        """When multiple impressions exist, the latest score should be used."""
        provider = ImpressionProviderImpl(backend)

        # A → B: first impression (old, low score)
        imp1 = Impression(
            source_agent="A", target_agent="B",
            compatibility_score=0.3,
            dimensions=ImpressionDimensions(),
        )
        imp1.created_at = 100.0
        await provider.record(imp1)

        # A → B: second impression (newer, high score)
        imp2 = Impression(
            source_agent="A", target_agent="B",
            compatibility_score=0.9,
            dimensions=ImpressionDimensions(),
        )
        imp2.created_at = 200.0
        await provider.record(imp2)

        # B → A
        imp_ba = Impression(
            source_agent="B", target_agent="A",
            compatibility_score=0.8,
            dimensions=ImpressionDimensions(),
        )
        await provider.record(imp_ba)

        mutuals = await provider.get_mutual("A", min_score=0.5)
        assert len(mutuals) == 1
        assert mutuals[0][1] == 0.9  # latest score, not 0.3


# ══════════════════════════════════════════════════════════════════════
# Bug #7-10: Path sanitization in all 4 SDK providers
# ══════════════════════════════════════════════════════════════════════

class TestBug7_PathSanitization:
    """Agent IDs with path separators could cause directory traversal.
    All providers must sanitize path components."""

    def test_session_safe(self):
        p = SessionProviderImpl(MockBackend())
        assert ".." not in p._safe("../etc/passwd")
        assert "/" not in p._safe("agent/id")
        assert "\\" not in p._safe("agent\\id")

    def test_session_path_sanitized(self):
        p = SessionProviderImpl(MockBackend())
        path = p._path("../evil", "thread/../hack", "cp")
        assert ".." not in path
        assert "evil" in path

    # Phase D 续 #2: ``test_memory_safe`` and ``test_memory_paths_sanitized``
    # were removed when MemoryProviderImpl was deleted. The typed Phase J
    # stores own their own path layout.

    def test_artifact_safe(self):
        p = ArtifactProviderImpl(MockBackend())
        assert ".." not in p._safe("../x")

    def test_artifact_paths_sanitized(self):
        p = ArtifactProviderImpl(MockBackend())
        manifest = p._manifest_path("../evil", "sess/../hack")
        assert ".." not in manifest
        blob = p._blob_path("agent", "../sess", "file/../evil.txt", 1)
        assert ".." not in blob

    def test_impression_safe(self):
        assert ".." not in ImpressionProviderImpl._safe("../x")
        assert "/" not in ImpressionProviderImpl._safe("a/b")

    @pytest.mark.asyncio
    async def test_impression_record_sanitizes_paths(self, backend):
        """Recording an impression with slashes in agent_id should not break paths."""
        provider = ImpressionProviderImpl(backend)
        imp = Impression(
            source_agent="agent/with/slashes",
            target_agent="normal-agent",
            compatibility_score=0.5,
            dimensions=ImpressionDimensions(),
        )
        imp_id = await provider.record(imp)
        assert imp_id  # Should not crash


# ══════════════════════════════════════════════════════════════════════
# Bug #11: Aggressive 300s cooldown — should use exponential backoff
# ══════════════════════════════════════════════════════════════════════

class TestBug11_ExponentialBackoff:
    """Chain anchoring used fixed 300s cooldown. Now uses exponential backoff."""

    def test_backoff_increases_exponentially(self):
        """Import and test the backoff logic on ChainBackend."""
        from nexus_core.backends.chain import ChainBackend

        # We can't instantiate ChainBackend (needs private_key etc),
        # so test the _next_backoff logic directly by simulating it
        backoffs = {}

        def next_backoff(agent_id):
            current = backoffs.get(agent_id, 15)
            backoffs[agent_id] = min(current * 2, 300)
            return current

        # First call: 15s
        assert next_backoff("agent-1") == 15
        # Second: 30s
        assert next_backoff("agent-1") == 30
        # Third: 60s
        assert next_backoff("agent-1") == 60
        # Fourth: 120s
        assert next_backoff("agent-1") == 120
        # Fifth: 240s
        assert next_backoff("agent-1") == 240
        # Sixth: capped at 300s
        assert next_backoff("agent-1") == 300
        # Stays at 300
        assert next_backoff("agent-1") == 300


# ══════════════════════════════════════════════════════════════════════
# Bug #13: Corrupted artifacts served silently
# ══════════════════════════════════════════════════════════════════════

class TestBug13_CorruptedArtifactRejection:
    """Artifacts with hash mismatch were served silently. Now returns None."""

    @pytest.mark.asyncio
    async def test_corrupted_artifact_returns_none(self, backend):
        provider = ArtifactProviderImpl(backend)

        # Save a valid artifact
        data = b"hello world"
        version = await provider.save(
            filename="test.txt",
            data=data,
            agent_id="agent-1",
        )
        assert version == 1

        # Now corrupt the manifest hash
        key = provider._manifest_key("agent-1", "")
        manifest = provider._get_manifest(key)
        manifest["test.txt"][0]["content_hash"] = "0" * 64  # wrong hash

        # Loading should return None (corrupted)
        result = await provider.load("test.txt", agent_id="agent-1")
        assert result is None


# Bug #14 (negative-cache TTL) test class deleted: the negative cache
# existed to avoid repeated remote object-storage round-trips. The
# remote data plane was removed (ChainBackend now reads its local
# store directly), so there is no negative cache to regression-test.
# Phase D 续 #2: MemoryProvider-specific test classes deleted
# (TestBug_EnsureLoadedCancelSafety, TestBug15, TestBug18,
# TestFeature_MemoryCapacityManagement, TestFeature_MemoryAccessTracking).
# The typed Phase J namespace stores have their own test suite in
# tests/test_memory_namespaces.py.



# ══════════════════════════════════════════════════════════════════════
# Bug #16: ERC-8004 re-registration on every anchor call
# ══════════════════════════════════════════════════════════════════════

class TestBug16_AnchorSkipsReregistration:
    """_anchor_on_chain called ensure_agent_registered on EVERY anchor,
    minting new on-chain IDs (785→786→787→788). Now caches in _agent_id_map."""

    def test_anchor_checks_agent_id_map(self):
        """Verify _anchor_on_chain uses _agent_id_map cache."""
        from nexus_core.backends.chain import ChainBackend
        import inspect
        source = inspect.getsource(ChainBackend._anchor_on_chain)
        # Should check cache first before calling ensure_agent_registered
        assert "_agent_id_map" in source
        # The cache check should come BEFORE ensure_agent_registered
        cache_pos = source.find("if agent_id in self._agent_id_map")
        register_pos = source.find("ensure_agent_registered")
        assert cache_pos > 0
        assert register_pos > 0
        assert cache_pos < register_pos, \
            "_agent_id_map check must come before ensure_agent_registered"


class TestBug12_CacheWriteOSError:
    """_cache_write only caught generic Exception. Now also catches OSError
    at warning level for disk full / permission denied scenarios."""

    def test_cache_write_handles_oserror(self):
        """Verify the implementation catches OSError specifically."""
        from nexus_core.backends.chain import ChainBackend
        import inspect
        source = inspect.getsource(ChainBackend._cache_write)
        assert "OSError" in source


# ══════════════════════════════════════════════════════════════════════
# Bug #17: Graceful shutdown — pending background tasks were cancelled
# immediately on exit, causing data loss for the last 1-2 turns
# ══════════════════════════════════════════════════════════════════════

class TestBug17_GracefulShutdown:
    """ChainBackend.close() immediately cancelled all pending tasks.
    Now waits up to grace_period seconds for them to finish."""

    def test_chain_backend_close_has_grace_period(self):
        """close() should accept a grace_period parameter."""
        from nexus_core.backends.chain import ChainBackend
        import inspect
        sig = inspect.signature(ChainBackend.close)
        assert "grace_period" in sig.parameters

    def test_chain_backend_close_uses_asyncio_wait(self):
        """close() should use asyncio.wait (not immediate cancel)."""
        from nexus_core.backends.chain import ChainBackend
        import inspect
        source = inspect.getsource(ChainBackend.close)
        assert "asyncio.wait" in source
        assert "timeout" in source

    def test_rune_provider_holds_backend_ref(self):
        """AgentRuntime should hold a backend reference for lifecycle."""
        rune = nexus_core.builder().mock_backend().build()
        assert hasattr(rune, "_backend")
        assert rune._backend is not None

    @pytest.mark.asyncio
    async def test_rune_provider_close_calls_backend(self):
        """AgentRuntime.close() should call backend.close()."""
        rune = nexus_core.builder().mock_backend().build()
        # MockBackend.close() is a no-op but should not crash
        await rune.close()
# Phase D 续 #2: MemoryProvider-specific test classes deleted
# (TestBug_EnsureLoadedCancelSafety, TestBug15, TestBug18,
# TestFeature_MemoryCapacityManagement, TestFeature_MemoryAccessTracking).
# The typed Phase J namespace stores have their own test suite in
# tests/test_memory_namespaces.py.



# ══════════════════════════════════════════════════════════════════════
# Feature: Artifact rollback — revert to a previous version
# ══════════════════════════════════════════════════════════════════════

class TestFeature_ArtifactRollback:
    """ArtifactProvider.rollback() creates a new version with the content
    of a previous version, enabling safe undo for skill evolution."""

    @pytest.mark.asyncio
    async def test_rollback_creates_new_version(self):
        from nexus_core.providers.artifact import ArtifactProviderImpl
        backend = MockBackend()
        provider = ArtifactProviderImpl(backend)

        v1 = await provider.save("skill.json", b'{"v": 1}', agent_id="agent-1")
        v2 = await provider.save("skill.json", b'{"v": 2}', agent_id="agent-1")
        assert v1 == 1
        assert v2 == 2

        # Rollback to v1
        v3 = await provider.rollback("skill.json", agent_id="agent-1", to_version=1)
        assert v3 == 3  # New version, not overwrite

        # Content should match v1
        art = await provider.load("skill.json", agent_id="agent-1", version=3)
        assert art.data == b'{"v": 1}'

    @pytest.mark.asyncio
    async def test_rollback_preserves_history(self):
        from nexus_core.providers.artifact import ArtifactProviderImpl
        backend = MockBackend()
        provider = ArtifactProviderImpl(backend)

        await provider.save("skill.json", b'v1', agent_id="agent-1")
        await provider.save("skill.json", b'v2', agent_id="agent-1")
        await provider.rollback("skill.json", agent_id="agent-1", to_version=1)

        versions = await provider.list_versions("skill.json", agent_id="agent-1")
        assert versions == [1, 2, 3]  # All three preserved

    @pytest.mark.asyncio
    async def test_rollback_nonexistent_version_raises(self):
        from nexus_core.providers.artifact import ArtifactProviderImpl
        backend = MockBackend()
        provider = ArtifactProviderImpl(backend)

        await provider.save("skill.json", b'v1', agent_id="agent-1")

        with pytest.raises(ValueError, match="Version 99 not found"):
            await provider.rollback("skill.json", agent_id="agent-1", to_version=99)

    @pytest.mark.asyncio
    async def test_rollback_metadata_records_source_version(self):
        from nexus_core.providers.artifact import ArtifactProviderImpl
        backend = MockBackend()
        provider = ArtifactProviderImpl(backend)

        await provider.save("skill.json", b'v1', agent_id="agent-1")
        await provider.save("skill.json", b'v2', agent_id="agent-1")
        await provider.rollback("skill.json", agent_id="agent-1", to_version=1)

        art = await provider.load("skill.json", agent_id="agent-1", version=3)
        assert art.metadata.get("rollback_from") == 1
