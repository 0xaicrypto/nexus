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
    MockBackend,
)
from nexus_core.providers.session import SessionProviderImpl
from nexus_core.providers.artifact import ArtifactProviderImpl

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
