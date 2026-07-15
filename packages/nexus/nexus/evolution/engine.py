"""
EvolutionEngine — Orchestrates memory, skill, persona evolution + knowledge compilation.
"""

from __future__ import annotations

import logging
from typing import Any

from nexus_core import AgentRuntime
from nexus_core.memory import (
    EventLog,
    FactsStore,
    KnowledgeStore,
    PersonaStore,
    SkillsStore,
)

from .knowledge_compiler import KnowledgeCompiler
from .memory_evolver import MemoryEvolver
from .persona_evolver import PersonaEvolver
from .skill_evaluator import SkillEvaluator
from .skill_evolver import SkillEvolver
from .social_engine import SocialEngine

logger = logging.getLogger(__name__)


class EvolutionEngine:
    """Orchestrates the self-evolution loop."""

    def __init__(
        self,
        rune: AgentRuntime,
        agent_id: str,
        llm_fn: Any,
        default_persona: str = "",
        agent_name: str = "Twin",
        facts_store: FactsStore | None = None,
        event_log: EventLog | None = None,
        persona_store: PersonaStore | None = None,
        skills_store: SkillsStore | None = None,
        knowledge_store: KnowledgeStore | None = None,
    ):
        self.rune = rune
        self.agent_id = agent_id
        self.llm_fn = llm_fn

        # Phase D: typed stores are required by every evolver. When
        # the caller doesn't provide one (tests / standalone use),
        # we synthesise an in-memory-equivalent store under a per-
        # process temp dir so the engine still constructs cleanly.
        # In production, DigitalTwin always passes the real,
        # chain-mirrored stores constructed at twin init.
        if (persona_store is None or skills_store is None
                or knowledge_store is None or facts_store is None):
            import tempfile
            from pathlib import Path
            scratch = Path(tempfile.gettempdir()) / f"nexus-engine-scratch-{agent_id}"
            scratch.mkdir(parents=True, exist_ok=True)
            if persona_store is None:
                persona_store = PersonaStore(base_dir=scratch)
            if skills_store is None:
                skills_store = SkillsStore(base_dir=scratch)
            if knowledge_store is None:
                knowledge_store = KnowledgeStore(base_dir=scratch)
            if facts_store is None:
                facts_store = FactsStore(base_dir=scratch)

        self.memory = MemoryEvolver(
            rune, agent_id, llm_fn,
            facts_store=facts_store,
            event_log=event_log,  # Phase O.2: emit evolution_proposal
        )
        self.skills = SkillEvolver(
            rune, agent_id, llm_fn,
            event_log=event_log,         # Phase O.2 instrumentation
            skills_store=skills_store,
        )
        self.evaluator = SkillEvaluator(llm_fn)
        self.persona = PersonaEvolver(
            rune, agent_id, llm_fn,
            event_log=event_log,        # Phase O.2 instrumentation
            persona_store=persona_store,
        )
        self.knowledge = KnowledgeCompiler(
            rune, agent_id, llm_fn,
            event_log=event_log,         # Phase O.2 instrumentation
            knowledge_store=knowledge_store,
            # Phase D 续: source facts come from facts_store
            # (canonical), not rune.memory anymore.
            facts_store=facts_store,
        )
        self.social = SocialEngine(rune, agent_id, llm_fn, agent_name=agent_name)

        self._default_persona = default_persona
        self._turn_count = 0
        self._initialized = False
        # Track which skills were used in the last context build
        # so after_conversation_turn can evaluate them
        self._last_used_skills: list[str] = []
        self._last_query: str = ""

    async def initialize(self):
        if self._initialized:
            return
        # Load persona, skills, knowledge, AND memories in parallel.
        # Memory preloading is critical: without it, the first chat() call
        # triggers lazy-load inside get_context_for_query() which has a 3s
        # timeout. Cold-start loads can exceed that,
        # causing the timeout to fire and memories to be unavailable.
        import asyncio
        results = await asyncio.gather(
            self.persona.load_persona(self._default_persona),
            self.skills.load_skills(),
            self.knowledge.load_articles(),
            self._preload_memories(),
            return_exceptions=True,  # Don't crash if one loader fails
        )
        names = ["persona", "skills", "knowledge", "memory"]
        for i, r in enumerate(results):
            if isinstance(r, Exception):
                logger.warning("Evolution %s load failed: %s", names[i], r)
        self._initialized = True

    async def _preload_memories(self):
        """Phase D 续: facts_store reads synchronously from disk —
        there is no async warm-up. Kept as a no-op so the
        ``initialize`` gather signature (4 awaitables) doesn't change.
        """
        return None

    async def after_conversation_turn(
        self, conversation: list[dict], max_memories: int = 5,
    ) -> dict:
        self._turn_count += 1
        result = {"turn": self._turn_count, "actions": []}

        # ── 1. Extract memories ──
        extracted = await self.memory.extract_and_store(
            conversation, max_memories=max_memories,
        )
        if extracted:
            result["actions"].append({
                "type": "memory_extraction",
                "count": len(extracted),
                "items": [m["content"][:50] for m in extracted],
            })

        # ── 2. Learn skills from conversation ──
        try:
            learned = await self.skills.learn_from_conversation(conversation)
            if learned:
                result["actions"].append({
                    "type": "skill_learning",
                    "count": len(learned),
                    "skills": [s["skill_name"] for s in learned],
                    "details": learned,
                })
        except Exception as e:
            logger.warning(f"Conversation skill learning failed: {e}")

        # ── 3. Async skill evaluation (LLM-as-Judge) ──
        # Evaluate skills that were used in the last context build.
        # Non-blocking: failures are logged but never surface to user.
        if self._last_used_skills and len(conversation) >= 2:
            try:
                response_text = conversation[-1].get("content", "") if conversation[-1].get("role") != "user" else ""
                eval_results = await self._evaluate_used_skills(
                    self._last_query, response_text,
                )
                if eval_results:
                    result["actions"].append({
                        "type": "skill_evaluation",
                        "evaluated": len(eval_results),
                        "scores": {
                            name: f"{scores.get('overall', 0):.1f}/10"
                            for name, scores in eval_results.items()
                        },
                    })

                # ── 4. Check if any skills need evolution ──
                evolution_results = await self._check_skill_evolution()
                if evolution_results:
                    result["actions"].append({
                        "type": "skill_evolution",
                        "results": evolution_results,
                    })
            except Exception as e:
                logger.debug("Skill evaluation/evolution failed: %s", e)

        return result

    async def _evaluate_used_skills(
        self, query: str, response: str,
    ) -> dict[str, dict]:
        """Evaluate all skills used in the last context build."""
        results = {}
        for skill_name in self._last_used_skills:
            skill_data = self.skills._skills_cache.get(skill_name)
            if not skill_data:
                continue

            evaluation = await self.evaluator.evaluate_usage(
                query=query,
                response=response,
                skill_name=skill_name,
                skill_data=skill_data,
            )
            if evaluation:
                self.evaluator.record_evaluation(skill_data, evaluation)
                results[skill_name] = evaluation
                self.skills._dirty = True

        # Save updated evaluations
        if results:
            async with self.skills._lock:
                await self.skills._save_skills_unlocked()

        return results

    async def _check_skill_evolution(self) -> list[dict]:
        """Check all skills for evolution triggers and run pipeline if needed."""
        evolution_results = []
        for name, skill_data in self.skills._skills_cache.items():
            if name.startswith("_"):
                continue
            if not self.evaluator.needs_evolution(skill_data):
                continue

            result = await self.evaluator.run_evolution_pipeline(name, skill_data)
            if result and result.get("action") == "accepted":
                # Apply the evolution
                self.evaluator.apply_evolution(skill_data, result)
                self.skills._dirty = True
                evolution_results.append({
                    "skill": name,
                    "action": "evolved",
                    "version": skill_data.get("version", 1),
                    "changes": result.get("changes_made", ""),
                    "win_rate": result.get("win_rate", 0),
                })
            elif result and result.get("action") == "rejected":
                evolution_results.append({
                    "skill": name,
                    "action": "rejected",
                    "win_rate": result.get("win_rate", 0),
                })

        if evolution_results:
            async with self.skills._lock:
                await self.skills._save_skills_unlocked()

        return evolution_results

    async def trigger_reflection(self) -> dict:
        """Full reflection cycle: persona evolution + knowledge compilation."""
        mem_stats = await self.memory.get_stats()
        skill_stats = await self.skills.get_stats()
        # Phase D 续: pull recent facts from facts_store (canonical),
        # newest-first, take the last 15. ``memory.facts_store`` is
        # the auto-synthesised or twin-injected store — same source
        # MemoryEvolver writes to.
        all_facts = self.memory.facts_store.all()
        all_facts.sort(key=lambda f: f.created_at)
        memory_texts = [f.content for f in all_facts[-15:]]

        # ── Persona evolution ──
        evolution_result = await self.persona.evolve(
            memories_sample=memory_texts,
            skills_summary=skill_stats,
        )

        # ── Knowledge compilation ──
        compilation_result = await self.knowledge.compile(min_memories=6)

        # ── Profile generation (social protocol) ──
        profile_result = {}
        try:
            profile = await self.social.generate_profile(
                persona=self.get_current_persona(),
                memory_stats=mem_stats,
                skills_summary=skill_stats,
            )
            profile_result = {
                "interests": profile.interests,
                "capabilities": profile.capabilities,
                "style_tags": profile.style_tags,
            }
        except Exception as e:
            logger.warning(f"Profile generation during reflection failed: {e}")

        return {
            "type": "reflection",
            "memory_stats": mem_stats,
            "skill_stats": skill_stats,
            "persona_evolution": evolution_result,
            "knowledge_compilation": compilation_result,
            "profile_update": profile_result,
        }

    def get_current_persona(self) -> str:
        return self.persona.current_persona

    async def get_context_for_query(
        self, query: str, top_k: int = 5, tool_names: set[str] | None = None,
    ) -> str:
        """
        Build context for LLM from in-memory caches ONLY.

        NEVER blocks on storage/chain. All sources are checked with timeouts
        so chat() always returns fast. If evolution hasn't finished loading yet,
        context is simply empty — it will be available on the next turn.

        Args:
            tool_names: Names of registered tools. Skills with matching names
                are filtered out to prevent the LLM from role-playing tool use
                instead of making actual function calls.

        Sources:
          1. Compiled knowledge articles (in-memory dict)
          2. Individual memories (in-memory index)
          3. Learned skill strategies (in-memory dict)
          4. Social network context (in-memory impressions)

        Steps 2 and 4 run IN PARALLEL to avoid sequential timeout stacking
        (2s + 2s = 4s > 3s outer timeout in chat()).
        """
        import asyncio
        parts = []
        self._last_query = query
        self._last_used_skills = []
        _tool_names = tool_names or set()

        # ── 1. Compiled knowledge (in-memory only — no lazy load) ──
        try:
            knowledge_ctx = self.knowledge.get_context_from_cache(query)
            if knowledge_ctx:
                parts.append(knowledge_ctx)
        except Exception as e:
            logger.debug("compiled knowledge lookup failed: %s", e)

        # ── 2 + 4 in PARALLEL: memory recall + social context ──
        # Both may hit slow I/O on cold start. Running them in parallel
        # ensures total time = max(step2, step4) instead of sum.
        async def _get_memories():
            return await self.memory.recall_relevant(query, top_k=top_k)

        async def _get_social():
            return await self.social.get_social_context(query)

        memory_result = None
        social_result = None
        try:
            results = await asyncio.gather(
                asyncio.wait_for(_get_memories(), timeout=2.0),
                asyncio.wait_for(_get_social(), timeout=2.0),
                return_exceptions=True,
            )
            if not isinstance(results[0], BaseException):
                memory_result = results[0]
            if not isinstance(results[1], BaseException):
                social_result = results[1]
        except Exception as e:
            logger.debug("memory/social context gather failed: %s", e)

        if memory_result:
            parts.append("\n## Relevant Memories")
            for m in memory_result:
                imp = "*" * m.get("importance", 3)
                parts.append(f"- [{m['category']}] {m['content']} ({imp})")

        # ── 3. Skills: Two-layer progressive disclosure ──────────
        # Layer 0: Inject lightweight skill index (always, ~20 tokens/skill)
        # Layer 1: Load full procedure only for matched skills
        try:
            skill_index = self.skills.get_skill_index()
            if skill_index:
                # Filter out skills that share names with registered tools.
                # Without this, the LLM sees "web_search" as both a text-based
                # skill AND a callable function — it then "role-plays" using
                # web search (generating text about it) instead of making an
                # actual function call via the tool API.
                filtered_index = [
                    s for s in skill_index
                    if s["name"] not in _tool_names
                ]

                if filtered_index:
                    parts.append("\n## Available Skills")
                    for s in filtered_index:
                        rate = f" ({s['success_rate']:.0%} success)" if s.get("times_used", 0) > 0 else ""
                        parts.append(f"- **{s['name']}**: {s['description']}{rate}")

                # Match query → top-2 relevant skills → load full procedure
                matched = self.skills.match_skills(query, top_k=2)
                for skill in matched:
                    skill_name = skill.get("name", "")
                    # Skip tool-conflicting skills
                    if skill_name in _tool_names:
                        continue
                    full_content = self.skills.get_full_content(skill_name)
                    if full_content:
                        parts.append(f"\n### Skill: {skill_name}")
                        parts.append(full_content)
                        # Track usage for evaluation feedback loop
                        self.skills.record_skill_usage(skill_name)
                        self._last_used_skills.append(skill_name)
            else:
                # Fallback to legacy strategy lookup for backward compat
                words = query.lower().split()
                for word in words:
                    strategy = self.skills.get_strategy_from_cache(word)
                    if strategy:
                        parts.append(f"\n## Learned Strategy for '{word}'")
                        parts.append(strategy)
                        break
        except Exception as e:
            logger.debug("strategy lookup failed: %s", e)

        if social_result:
            parts.append(social_result)

        return "\n".join(parts) if parts else ""

    async def get_full_stats(self) -> dict:
        skill_stats = await self.skills.get_stats()
        # Enrich skill stats with evaluation data
        for name, sinfo in skill_stats.get("skills", {}).items():
            skill_data = self.skills._skills_cache.get(name, {})
            evals = skill_data.get("evaluations", [])
            sinfo["eval_count"] = len(evals)
            sinfo["avg_score"] = self.evaluator.get_avg_score(skill_data) if evals else None
            sinfo["needs_evolution"] = self.evaluator.needs_evolution(skill_data)
            sinfo["evolution_count"] = skill_data.get("evolution_count", 0)
        return {
            "turn_count": self._turn_count,
            "memory": await self.memory.get_stats(),
            "skills": skill_stats,
            "persona": await self.persona.get_stats(),
            "knowledge": await self.knowledge.get_stats(),
        }
