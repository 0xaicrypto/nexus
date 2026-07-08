"""Brain panel chain_status endpoint tests (Phase D 续 / #159).

The endpoint reads each typed namespace's
``VersionedStore.chain_status(last_anchor_at)`` and the chain
backend's health snapshot, returning a 3-state status per
namespace (local / mirrored / anchored).

These tests use the same FakeTwin pattern as
``test_namespaces_endpoint.py`` and inject a FakeChainBackend so we
can drive the anchor / mirror state.
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock

import pytest

from nexus_core.memory import (
    EpisodesStore,
    Fact, FactsStore,
    LearnedSkill, SkillsStore,
    PersonaVersion, PersonaStore,
    KnowledgeArticle, KnowledgeStore,
)


class FakeChainBackend:
    """Minimal stub matching ChainBackend's chain-status surface."""
    def __init__(self):
        self._anchor_at: dict[str, float] = {}
        self.health = {
            "bsc_ready": True,
            "bsc_failure_active": False,
            "last_bsc_anchor_error": None,
        }

    def last_anchor_at(self, agent_id: str):
        return self._anchor_at.get(agent_id)

    def chain_health_snapshot(self) -> dict:
        return dict(self.health)

    def is_path_mirrored(self, path: str) -> bool:
        return True


class FakeRune:
    def __init__(self, backend):
        self._backend = backend


class FakeConfig:
    def __init__(self, agent_id: str = "test-agent"):
        self.agent_id = agent_id


class FakeTwin:
    def __init__(self, base_dir: str, backend):
        self.config = FakeConfig()
        self.rune = FakeRune(backend)
        self.episodes = EpisodesStore(base_dir=base_dir, chain_backend=backend)
        self.facts = FactsStore(base_dir=base_dir, chain_backend=backend)
        self.skills_memory = SkillsStore(base_dir=base_dir, chain_backend=backend)
        self.persona_store = PersonaStore(base_dir=base_dir, chain_backend=backend)
        self.knowledge = KnowledgeStore(base_dir=base_dir, chain_backend=backend)

    async def close(self):
        pass


def test_chain_status_empty_twin_all_local(client, tmp_path):
    """Fresh twin with no commits → every namespace is local-only."""
    from nexus_server import twin_manager
    backend = FakeChainBackend()
    twin = FakeTwin(base_dir=str(tmp_path / "twin"), backend=backend)
    twin_manager._test_override = twin
    try:
        reg = client.post("/api/v1/auth/register", json={"display_name": "CS"})
        token = reg.json()["jwt_token"]

        resp = client.get(
            "/api/v1/agent/chain_status",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()

        names = {n["namespace"] for n in body["namespaces"]}
        assert names == {"persona", "knowledge", "skills", "facts", "episodes"}
        for n in body["namespaces"]:
            assert n["status"] == "local"
            assert n["version"] is None
        assert body["health"]["bsc_ready"] is True
    finally:
        twin_manager._test_override = None


def test_chain_status_committed_no_anchor_is_mirrored(client, tmp_path):
    """Twin with a committed version + no anchor yet → status='mirrored'.
    The data has been persisted by the backend but the agent state_root
    has not been re-anchored since the last commit."""
    from nexus_server import twin_manager
    backend = FakeChainBackend()
    twin = FakeTwin(base_dir=str(tmp_path / "twin"), backend=backend)

    # Commit a fact + persona version
    twin.facts.upsert(Fact(content="user likes sushi"))
    twin.facts.commit()
    twin.persona_store.propose_version(PersonaVersion(persona_text="helpful"))

    twin_manager._test_override = twin
    try:
        reg = client.post("/api/v1/auth/register", json={"display_name": "CS2"})
        token = reg.json()["jwt_token"]
        resp = client.get(
            "/api/v1/agent/chain_status",
            headers={"Authorization": f"Bearer {token}"},
        )
        body = resp.json()
        by_ns = {n["namespace"]: n for n in body["namespaces"]}
        # facts and persona were committed → mirrored
        assert by_ns["facts"]["status"] == "mirrored"
        assert by_ns["facts"]["version"] is not None
        assert by_ns["persona"]["status"] == "mirrored"
        # untouched stores stay local
        assert by_ns["episodes"]["status"] == "local"
    finally:
        twin_manager._test_override = None


def test_chain_status_anchor_after_commit_promotes_to_anchored(client, tmp_path):
    """When the chain backend reports an anchor timestamp ≥ the
    namespace's last_commit_at, status is 'anchored'."""
    from nexus_server import twin_manager
    backend = FakeChainBackend()
    twin = FakeTwin(base_dir=str(tmp_path / "twin"), backend=backend)

    twin.facts.upsert(Fact(content="user lives in Tokyo"))
    twin.facts.commit()

    # Mark anchor as having happened just after commit
    backend._anchor_at["test-agent"] = time.time() + 5.0

    twin_manager._test_override = twin
    try:
        reg = client.post("/api/v1/auth/register", json={"display_name": "CS3"})
        token = reg.json()["jwt_token"]
        resp = client.get(
            "/api/v1/agent/chain_status",
            headers={"Authorization": f"Bearer {token}"},
        )
        body = resp.json()
        by_ns = {n["namespace"]: n for n in body["namespaces"]}
        assert by_ns["facts"]["status"] == "anchored"
        assert by_ns["facts"]["last_anchor_at"] is not None
    finally:
        twin_manager._test_override = None


def test_chain_status_health_card_surfaces_backend_signals(client, tmp_path):
    """Backend health is plumbed through to the response so the
    Chain Health card can render anchor state."""
    from nexus_server import twin_manager
    backend = FakeChainBackend()
    backend.health = {
        "bsc_ready": False,
        "bsc_failure_active": True,
        "last_bsc_anchor_error": {"error": "nonce too low"},
    }
    twin = FakeTwin(base_dir=str(tmp_path / "twin"), backend=backend)
    twin_manager._test_override = twin
    try:
        reg = client.post("/api/v1/auth/register", json={"display_name": "CS4"})
        token = reg.json()["jwt_token"]
        resp = client.get(
            "/api/v1/agent/chain_status",
            headers={"Authorization": f"Bearer {token}"},
        )
        body = resp.json()
        h = body["health"]
        assert h["bsc_ready"] is False
        assert h["bsc_failure_active"] is True
    finally:
        twin_manager._test_override = None
