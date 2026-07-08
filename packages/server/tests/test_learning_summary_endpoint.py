"""Brain panel learning_summary endpoint tests (Phase D 续 / #159).

Drives the new `/api/v1/agent/learning_summary` endpoint with a
fake twin pre-seeded with facts/skills/persona/episodes/knowledge.
"""

from __future__ import annotations

import time

import pytest

from nexus_core.memory import (
    Episode, EpisodesStore,
    EventLog,
    Fact, FactsStore,
    LearnedSkill, SkillsStore,
    PersonaVersion, PersonaStore,
    KnowledgeArticle, KnowledgeStore,
)


class _FakeBackend:
    def last_anchor_at(self, agent_id: str):
        return None

    def chain_health_snapshot(self):
        return {
            "bsc_ready": True,
            "bsc_failure_active": False,
            "last_bsc_anchor_error": None,
        }

    def is_path_mirrored(self, path: str) -> bool:
        return True


class _FakeRune:
    def __init__(self, backend):
        self._backend = backend


class _FakeConfig:
    def __init__(self, agent_id="test-agent"):
        self.agent_id = agent_id


class FakeTwin:
    def __init__(self, base_dir: str):
        backend = _FakeBackend()
        self.config = _FakeConfig()
        self.rune = _FakeRune(backend)
        self.episodes = EpisodesStore(base_dir=base_dir)
        self.facts = FactsStore(base_dir=base_dir)
        self.skills_memory = SkillsStore(base_dir=base_dir)
        self.persona_store = PersonaStore(base_dir=base_dir)
        self.knowledge = KnowledgeStore(base_dir=base_dir)
        self.event_log = EventLog(base_dir=base_dir, agent_id="test-agent")
        # No evolution engine — _build_data_flow handles None gracefully.
        self.evolution = None
        self.curated_memory = type("CM", (), {"memory_count": 0})()

    async def close(self):
        pass


def _populate(twin: FakeTwin) -> None:
    twin.facts.upsert(Fact(content="user likes spicy", category="preference", importance=4))
    twin.facts.upsert(Fact(content="user lives in Tokyo", category="fact", importance=5))
    twin.skills_memory.upsert(LearnedSkill(
        skill_name="code_review", strategy="check concurrency",
        last_lesson="user cares about gas",
    ))
    twin.persona_store.propose_version(PersonaVersion(
        persona_text="Helpful", changes_summary="initial",
    ))
    twin.knowledge.upsert(KnowledgeArticle(
        title="Travel preferences", content="...",
    ))
    twin.knowledge.commit()


def test_learning_summary_returns_seven_day_timeline(client, tmp_path):
    from nexus_server import twin_manager
    twin = FakeTwin(base_dir=str(tmp_path / "twin"))
    _populate(twin)

    twin_manager._test_override = twin
    try:
        reg = client.post("/api/v1/auth/register", json={"display_name": "LS"})
        token = reg.json()["jwt_token"]
        resp = client.get(
            "/api/v1/agent/learning_summary",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["window_days"] == 7
        assert len(body["timeline"]) == 7
        # Each timeline row has the 5 namespace counters
        for row in body["timeline"]:
            assert "facts" in row
            assert "skills" in row
            assert "knowledge" in row
            assert "persona" in row
            assert "episodes" in row
    finally:
        twin_manager._test_override = None


def test_learning_summary_just_learned_merges_namespaces(client, tmp_path):
    from nexus_server import twin_manager
    twin = FakeTwin(base_dir=str(tmp_path / "twin"))
    _populate(twin)

    twin_manager._test_override = twin
    try:
        reg = client.post("/api/v1/auth/register", json={"display_name": "LS2"})
        token = reg.json()["jwt_token"]
        resp = client.get(
            "/api/v1/agent/learning_summary?window=14d",
            headers={"Authorization": f"Bearer {token}"},
        )
        body = resp.json()
        assert body["window_days"] == 14
        kinds = {item["kind"] for item in body["just_learned"]}
        # We seeded all 4 store types (no episodes) → expect at least 4 kinds
        assert "fact" in kinds
        assert "skill" in kinds
        assert "knowledge" in kinds
        assert "persona" in kinds
        # Sorted newest first
        ts = [it["timestamp"] for it in body["just_learned"]]
        assert ts == sorted(ts, reverse=True)
    finally:
        twin_manager._test_override = None


def test_learning_summary_chain_status_present_on_each_item(client, tmp_path):
    from nexus_server import twin_manager
    twin = FakeTwin(base_dir=str(tmp_path / "twin"))
    _populate(twin)

    twin_manager._test_override = twin
    try:
        reg = client.post("/api/v1/auth/register", json={"display_name": "LS3"})
        token = reg.json()["jwt_token"]
        resp = client.get(
            "/api/v1/agent/learning_summary",
            headers={"Authorization": f"Bearer {token}"},
        )
        body = resp.json()
        for item in body["just_learned"]:
            assert item["chain_status"] in ("local", "mirrored", "anchored")
    finally:
        twin_manager._test_override = None


def test_learning_summary_invalid_window_falls_back_to_7d(client, tmp_path):
    from nexus_server import twin_manager
    twin = FakeTwin(base_dir=str(tmp_path / "twin"))

    twin_manager._test_override = twin
    try:
        reg = client.post("/api/v1/auth/register", json={"display_name": "LS4"})
        token = reg.json()["jwt_token"]
        resp = client.get(
            "/api/v1/agent/learning_summary?window=garbage",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200
        assert resp.json()["window_days"] == 7
    finally:
        twin_manager._test_override = None
