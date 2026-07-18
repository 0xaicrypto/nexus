"""
Tests for Nexus.

Uses MockBackend and a mock LLM — no external API calls needed.
"""

import json
import pytest

import nexus_core

from nexus.config import TwinConfig, LLMProvider
from nexus.twin import DigitalTwin
from nexus.evolution.memory_evolver import MemoryEvolver
from nexus.evolution.skill_evolver import SkillEvolver
from nexus.evolution.persona_evolver import PersonaEvolver
from nexus.evolution.engine import EvolutionEngine
from nexus.evolution.knowledge_compiler import KnowledgeCompiler


# ── Mock LLM ─────────────────────────────────────────────────────

class MockLLMClient:
    def __init__(self):
        self.calls = []

    async def chat(self, messages, system="", temperature=0.7, max_tokens=2048, **kwargs):
        self.calls.append({"type": "chat", "messages": messages})
        last_msg = messages[-1]["content"] if messages else ""
        if "sushi" in last_msg.lower():
            return "I've noted that you like sushi!"
        elif "tokyo" in last_msg.lower():
            return "Tokyo is a great choice!"
        return f"Got it: {last_msg[:50]}"

    async def complete(self, prompt, temperature=0.3):
        self.calls.append({"type": "complete", "prompt": prompt[:100]})
        if "analyze this completed task" in prompt.lower():
            return json.dumps({
                "skill_name": "travel_planning",
                "lesson": "User prefers detailed itineraries",
                "strategy_update": "Include restaurant suggestions",
                "confidence": 0.8,
                "tags": ["travel", "planning"],
            })
        elif "identify any skills" in prompt.lower():
            # Conversation skill detection prompt
            if "sushi" in prompt.lower() or "tokyo" in prompt.lower():
                return json.dumps({
                    "implicit_tasks": [
                        {
                            "skill_name": "food_recommendation",
                            "description": "Discussed food preferences",
                            "strategy": "Remember specific cuisine preferences",
                            "lesson": "User has strong sushi preference",
                            "confidence": 0.7,
                            "tags": ["food", "preference"],
                        }
                    ],
                    "topic_signals": [
                        {"topic": "japanese_food", "evidence": "User mentioned sushi"},
                    ],
                })
            return json.dumps({"implicit_tasks": [], "topic_signals": []})
        elif "analyze the following conversation" in prompt.lower():
            return json.dumps([
                {"content": "User likes sushi", "category": "preference", "importance": 4},
                {"content": "User planning Tokyo trip", "category": "fact", "importance": 3},
            ])
        elif "selecting relevant memories" in prompt.lower():
            # Memory selection prompt — return IDs found in the prompt
            import re
            ids = re.findall(r'\[([a-f0-9-]{36})\]', prompt)
            return json.dumps(ids[:3])
        elif "group them into topic clusters" in prompt.lower():
            # Knowledge clustering prompt
            return json.dumps({
                "food_preferences": [0, 1],
                "travel_plans": [2, 3],
            })
        elif "synthesize these related memories" in prompt.lower():
            # Knowledge article compilation prompt
            topic = "unknown"
            if "food" in prompt.lower():
                topic = "food_preferences"
            elif "travel" in prompt.lower():
                topic = "travel_plans"
            return json.dumps({
                "title": f"User {topic.replace('_', ' ').title()}",
                "summary": f"Summary of user's {topic}",
                "content": f"The user has clear {topic.replace('_', ' ')}. They enjoy sushi and plan trips to Tokyo.",
                "key_facts": ["likes sushi", "plans Tokyo trip"],
                "tags": [topic, "compiled"],
                "memory_count": 2,
                "confidence": 0.8,
            })
        elif "evolved persona" in prompt.lower():
            return json.dumps({
                "evolved_persona": "You are Twin, evolved. You know the user likes sushi and plans Tokyo trips.",
                "changes_summary": "Added food + travel knowledge",
                "confidence": 0.85,
                "version_notes": "v1: food+travel",
            })
        return "[]"

    async def close(self):
        pass


# ── Helpers ──────────────────────────────────────────────────────

def make_rune():
    return nexus_core.builder().mock_backend().build()


def make_twin(rune=None, llm=None):
    rune = rune or make_rune()
    llm = llm or MockLLMClient()
    config = TwinConfig(agent_id="test-twin", name="TestTwin", owner="Tester")
    return DigitalTwin(config=config, rune=rune, llm=llm)


# ── Tests ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_twin_init():
    twin = make_twin()
    await twin._initialize()
    assert twin._initialized
    assert twin._thread_id.startswith("session_")
    await twin.close()


@pytest.mark.asyncio
async def test_twin_chat():
    twin = make_twin()
    await twin._initialize()
    resp = await twin.chat("I like sushi")
    assert "sushi" in resp.lower()
    assert twin._turn_count == 1
    await twin.close()


@pytest.mark.asyncio
async def test_chat_writes_episode_to_typed_store():
    """Regression for the EpisodesStore data-flow disconnect:
    twin.chat used to never write to ``self.episodes`` so the
    desktop "Episodes 0 items" namespace pill was always empty
    and chat-time autobiographical recall had no source.

    Verifies that after one chat turn, the typed EpisodesStore
    has a row for the active thread, and after another turn in
    the same thread the row updates in place (turn_count++).
    """
    twin = make_twin()
    await twin._initialize()

    # Empty before the first turn.
    assert len(twin.episodes.all()) == 0

    await twin.chat("I like sushi")
    eps = twin.episodes.all()
    assert len(eps) == 1, (
        "twin.chat did not upsert an Episode into the typed "
        "EpisodesStore — the desktop namespace pill will stay 0."
    )
    ep = eps[0]
    # Active thread on a fresh twin starts as a session_xxx id.
    assert ep.session_id == twin._thread_id
    assert (ep.extra or {}).get("turn_count") == 1
    assert "sushi" in ep.summary

    await twin.chat("And ramen too")
    eps = twin.episodes.all()
    # Same session → same row, turn count bumped, summary updated.
    assert len(eps) == 1
    assert (eps[0].extra or {}).get("turn_count") == 2
    assert "ramen" in eps[0].summary
    await twin.close()


@pytest.mark.asyncio
async def test_session_persistence():
    rune = make_rune()
    llm = MockLLMClient()
    twin1 = make_twin(rune=rune, llm=llm)
    await twin1._initialize()
    await twin1.chat("Hello")
    tid = twin1._thread_id
    await twin1._save_session()

    twin2 = DigitalTwin(config=twin1.config, rune=rune, llm=llm)
    await twin2._initialize()
    assert twin2._thread_id == tid
    assert len(twin2._messages) == 2
    await twin2.close()


@pytest.mark.asyncio
async def test_new_session():
    twin = make_twin()
    await twin._initialize()
    await twin.chat("Hello")
    old = twin._thread_id
    await twin.new_session()
    assert twin._thread_id != old
    assert twin._messages == []


@pytest.mark.asyncio
async def test_commands():
    twin = make_twin()
    await twin._initialize()
    assert "/stats" in await twin.chat("/help")
    assert "Evolution Stats" in await twin.chat("/stats")


@pytest.mark.asyncio
async def test_memory_evolver():
    """Phase D 续: extract_and_store writes to facts_store, not rune.memory."""
    rune = make_rune()
    evolver = MemoryEvolver(rune, "test", MockLLMClient().complete)
    stored = await evolver.extract_and_store([
        {"role": "user", "content": "I like sushi"},
        {"role": "assistant", "content": "Noted!"},
    ])
    assert len(stored) == 2
    facts = evolver.facts_store.all()
    assert len(facts) == 2


@pytest.mark.asyncio
async def test_skill_evolver():
    rune = make_rune()
    evolver = SkillEvolver(rune, "test", MockLLMClient().complete)
    learning = await evolver.record_task_outcome(
        "travel", "Plan Tokyo trip", "day-by-day", "success",
    )
    assert learning["skill_name"] == "travel_planning"
    assert await evolver.get_strategy_for("travel_planning") is not None


@pytest.mark.asyncio
async def test_persona_evolver():
    rune = make_rune()
    evolver = PersonaEvolver(rune, "test", MockLLMClient().complete)
    await evolver.load_persona("Default persona.")
    result = await evolver.evolve(["User likes sushi"], {"total_skills": 1})
    # Phase D: typed PersonaStore now owns the version pointer; no
    # more numeric ``version`` from the legacy artifact path.
    assert "typed_version" in result
    assert "sushi" in evolver.current_persona.lower()


@pytest.mark.asyncio
async def test_evolution_full_cycle():
    rune = make_rune()
    engine = EvolutionEngine(rune, "test", MockLLMClient().complete, "Default.")
    await engine.initialize()

    result = await engine.after_conversation_turn([
        {"role": "user", "content": "I love sushi"},
        {"role": "assistant", "content": "Great!"},
    ])
    assert len(result["actions"]) > 0

    reflection = await engine.trigger_reflection()
    assert reflection["type"] == "reflection"




@pytest.mark.asyncio
async def test_multi_turn():
    twin = make_twin()
    await twin._initialize()
    await twin.chat("Hi")
    await twin.chat("I like sushi")
    await twin.chat("Going to Tokyo")
    assert twin._turn_count == 3
    assert len(twin._messages) == 6
    await twin.close()


@pytest.mark.asyncio
async def test_conversation_skill_learning():
    """SkillEvolver learns skills from conversation (Path 2)."""
    rune = make_rune()
    evolver = SkillEvolver(rune, "test", MockLLMClient().complete)

    learned = await evolver.learn_from_conversation([
        {"role": "user", "content": "I love sushi, especially salmon"},
        {"role": "assistant", "content": "Noted your sushi preference!"},
    ])
    assert len(learned) >= 1
    # Should have detected the food_recommendation implicit task
    skill_names = [s["skill_name"] for s in learned]
    assert "food_recommendation" in skill_names

    # Skill should now be in the cache
    strategy = await evolver.get_strategy_for("food_recommendation")
    assert strategy is not None


@pytest.mark.asyncio
async def test_topic_accumulation():
    """Topics accumulate across conversations and promote to skills at threshold."""
    rune = make_rune()
    evolver = SkillEvolver(rune, "test", MockLLMClient().complete)

    # Default threshold is 3. Send 3 conversations mentioning sushi/japanese_food.
    for i in range(3):
        await evolver.learn_from_conversation([
            {"role": "user", "content": f"Tell me about sushi restaurants (round {i})"},
            {"role": "assistant", "content": "Here are some great options!"},
        ])

    # After 3 rounds, "japanese_food" topic should have been promoted to a skill
    assert evolver._topic_counts.get("japanese_food", 0) >= 3
    assert "japanese_food" in evolver._skills_cache


@pytest.mark.asyncio
async def test_evolution_engine_learns_skills_from_chat():
    """EvolutionEngine.after_conversation_turn() extracts skills from conversation."""
    rune = make_rune()
    engine = EvolutionEngine(rune, "test", MockLLMClient().complete, "Default.")
    await engine.initialize()

    result = await engine.after_conversation_turn([
        {"role": "user", "content": "I want sushi for dinner"},
        {"role": "assistant", "content": "Great choice!"},
    ])

    action_types = [a["type"] for a in result["actions"]]
    assert "memory_extraction" in action_types
    assert "skill_learning" in action_types

    # Verify the skill was actually stored
    stats = await engine.skills.get_stats()
    assert stats["total_skills"] >= 1


# ── Progressive Retrieval Tests ─────────────────────────────────
# Phase D 续 #2: rewritten against FactsStore (single source of truth).


def test_search_compact(tmp_path):
    """FactsStore.search_compact returns lightweight summaries."""
    from nexus_core.memory import Fact, FactsStore
    fs = FactsStore(base_dir=tmp_path)
    fs.upsert(Fact(content="User likes sushi", category="preference", importance=4))
    fs.upsert(Fact(content="User plans Tokyo trip in March", category="fact", importance=3))
    fs.upsert(Fact(content="User prefers window seats", category="preference", importance=2))

    hits = fs.search_compact("sushi food", top_k=10)
    assert len(hits) >= 1
    for h in hits:
        assert h["key"]
        assert h["preview"]
        assert len(h["preview"]) <= 80


def test_get_by_keys(tmp_path):
    """FactsStore.get fetches full Fact rows by key."""
    from nexus_core.memory import Fact, FactsStore
    fs = FactsStore(base_dir=tmp_path)
    f1 = Fact(content="Memory one")
    f2 = Fact(content="Memory two")
    f3 = Fact(content="Memory three")
    fs.bulk_add([f1, f2, f3])

    results = [fs.get(f1.key), fs.get(f2.key)]
    contents = {r.content for r in results if r}
    assert "Memory one" in contents
    assert "Memory two" in contents


@pytest.mark.asyncio
async def test_progressive_recall(tmp_path):
    """MemoryEvolver.recall_relevant() uses progressive retrieval.
    Phase D 续: facts_store-backed."""
    from nexus_core.memory import Fact, FactsStore
    rune = make_rune()
    facts_store = FactsStore(base_dir=tmp_path)
    evolver = MemoryEvolver(
        rune, "test", MockLLMClient().complete, facts_store=facts_store,
    )

    for text in ["User likes sushi", "User plans Tokyo trip", "User prefers window seats"]:
        facts_store.upsert(Fact(content=text, category="fact", importance=3))

    results = await evolver.recall_relevant("sushi food", top_k=5)
    assert len(results) > 0
    for r in results:
        assert "content" in r


# ── Knowledge Compiler Tests ────────────────────────────────────

@pytest.mark.asyncio
async def test_knowledge_compiler_basic(tmp_path):
    """KnowledgeCompiler clusters facts and compiles articles.
    Phase D 续: facts_store is the input source."""
    from nexus_core.memory import Fact, FactsStore
    rune = make_rune()
    facts_store = FactsStore(base_dir=tmp_path / "facts")
    compiler = KnowledgeCompiler(
        rune, "test", MockLLMClient().complete,
        facts_store=facts_store,
    )

    items = [
        ("User likes sushi", "preference"),
        ("User loves salmon nigiri", "preference"),
        ("User plans Tokyo trip in March", "fact"),
        ("User wants to visit Shibuya", "fact"),
        ("User prefers window seats", "preference"),
        ("User works at BNB Chain", "fact"),
    ]
    for content, category in items:
        facts_store.upsert(Fact(content=content, category=category, importance=3))

    result = await compiler.compile(min_memories=6)
    assert result["status"] == "compiled"
    assert result["total_articles"] >= 1
    assert len(result["new_articles"]) >= 1


@pytest.mark.asyncio
async def test_knowledge_compiler_skip_low_count(tmp_path):
    """KnowledgeCompiler skips compilation when too few facts."""
    from nexus_core.memory import Fact, FactsStore
    rune = make_rune()
    facts_store = FactsStore(base_dir=tmp_path / "facts")
    compiler = KnowledgeCompiler(
        rune, "test", MockLLMClient().complete,
        facts_store=facts_store,
    )

    facts_store.upsert(Fact(content="Single fact"))
    result = await compiler.compile(min_memories=10)
    assert result["status"] == "skipped"


@pytest.mark.asyncio
async def test_knowledge_compiler_context():
    """KnowledgeCompiler provides context for queries."""
    rune = make_rune()
    compiler = KnowledgeCompiler(rune, "test", MockLLMClient().complete)

    # Manually inject an article
    compiler._articles["food_preferences"] = {
        "title": "User Food Preferences",
        "content": "The user loves sushi, especially salmon nigiri.",
        "tags": ["food", "sushi"],
        "key_facts": ["likes sushi", "prefers salmon"],
    }

    ctx = await compiler.get_context_for_query("sushi dinner")
    assert "Compiled Knowledge" in ctx
    assert "Food Preferences" in ctx


@pytest.mark.asyncio
async def test_reflection_includes_knowledge(tmp_path):
    """EvolutionEngine.trigger_reflection() includes knowledge compilation.
    Phase D 续: facts_store is the input source for both reflection
    memory texts and knowledge compilation."""
    from nexus_core.memory import Fact, FactsStore
    rune = make_rune()
    facts_store = FactsStore(base_dir=tmp_path / "facts")
    engine = EvolutionEngine(
        rune, "test", MockLLMClient().complete, "Default.",
        facts_store=facts_store,
    )
    await engine.initialize()

    for text in ["sushi", "nigiri", "tokyo", "shibuya", "window seat", "BNB Chain"]:
        facts_store.upsert(Fact(
            content=f"User likes {text}", category="fact", importance=3,
        ))

    reflection = await engine.trigger_reflection()
    assert "knowledge_compilation" in reflection
    assert reflection["knowledge_compilation"]["status"] in ("compiled", "skipped", "no_clusters")
