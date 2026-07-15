"""
SkillEvolver — Learn from task outcomes AND conversations.

Architecture inspired by Hermes Agent's progressive disclosure:
  Level 0: Skill index (name + description, ~3k tokens total) — always in context
  Level 1: Full procedure (markdown, ~500-2000 tokens each) — loaded on demand

Two learning paths:
  1. Explicit tasks:   create_task → complete_task → record_task_outcome
  2. Conversation:     chat → learn_from_conversation (auto-detect tasks & patterns)

Usage tracking:
  Each skill tracks times_used, success_count, failure_count for
  evaluation feedback and smart eviction.

Phase D
-------
Single source of truth: ``skills_store`` (Phase J typed
``SkillsStore``). The internal ``_skills_cache`` is now a
denormalised projection rebuilt from the typed store on
``_load_skills_unlocked``; it carries operational fields
(evaluations / evolution_count / etc.) the Phase J schema doesn't
own, but skill identity, strategy, lessons, and counters all live
in the typed store.

What got deleted (vs. pre-D):

* the ``rune.artifacts.save("skills_registry.json", …)`` write
  path. Typed-store ``commit()`` is the only durable write.
* ``apply_rollback``. Verdict-driven typed-store rollbacks are
  already visible to chat-time reads as soon as
  ``_load_skills_unlocked`` rebuilds the cache.
* ``_mirror_to_typed_store``. Upsert writes go straight to the
  typed store now — there is no second source to mirror to.
* ``skills_registry.json`` legacy artifact loading. Cold-start
  hydrate reads from the typed store; chain-recovery reads
  through ``skills_store.recover_from_chain()``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import uuid
from typing import Any, Optional

from nexus_core import AgentRuntime
from nexus_core.evolution import EvolutionProposal
from nexus_core.memory import EventLog, LearnedSkill, SkillsStore

from .memory_evolver import _robust_json_parse

logger = logging.getLogger(__name__)

# ── Prompts ──────────────────────────────────────────────────────

SKILL_ANALYSIS_PROMPT = """Analyze this completed task and extract a skill learning.

Task:
  Type: {task_type}
  Description: {description}
  Strategy used: {strategy}
  Outcome: {outcome}
  User feedback: {feedback}

Existing skills for this task type:
{existing_skills}

Return a JSON object with:
- "skill_name": Short name for this skill (e.g., "travel_planning", "code_review")
- "description": One-line description of when to use this skill (max 100 chars)
- "procedure": Step-by-step procedure in markdown (3-8 steps, include pitfalls)
- "lesson": What was learned (1-2 sentences)
- "confidence": 0.0-1.0 (how confident based on evidence)
- "tags": List of relevant tags

Return ONLY valid JSON, no markdown fences."""


CONVERSATION_SKILL_PROMPT = """Analyze this conversation between a user and their AI twin.
Identify any skills being demonstrated or requested.

Conversation:
{conversation}

Current skill registry:
{existing_skills}

Look for:
1. IMPLICIT TASKS — Did the user ask the twin to DO something? (research, plan, write, analyze, compare, translate, explain, code, etc.)
   If the twin provided a helpful response, treat it as a successful task completion.
2. TOPIC EXPERTISE — Is the conversation about a specific domain? (travel, finance, coding, health, cooking, etc.)
   Repeated conversations on a topic mean the twin is developing expertise.
3. INTERACTION PATTERNS — How does the user prefer to interact? (wants step-by-step, prefers concise answers, asks follow-ups, etc.)

Return a JSON object:
{{
  "implicit_tasks": [
    {{
      "skill_name": "short_skill_name",
      "description": "One-line description of when to use this skill (max 100 chars)",
      "procedure": "## Procedure\\n1. Step one\\n2. Step two\\n\\n## Pitfalls\\n- Common mistake",
      "lesson": "What to remember for next time (1 sentence)",
      "confidence": 0.0-1.0,
      "tags": ["tag1", "tag2"]
    }}
  ],
  "topic_signals": [
    {{
      "topic": "topic_name",
      "evidence": "Why this topic was detected (1 sentence)"
    }}
  ]
}}

Rules:
- Only extract genuinely useful skills, not trivial chat (greetings, small talk)
- If nothing skill-worthy happened, return {{"implicit_tasks": [], "topic_signals": []}}
- Be conservative — only high-confidence observations
- skill_name should be snake_case (e.g., "travel_planning", "code_debugging")
- procedure should be markdown with ## headers for sections

IMPORTANT: You MUST always respond with a JSON object. Even if the conversation is trivial, respond with:
{{"implicit_tasks": [], "topic_signals": []}}

Return ONLY valid JSON, no markdown fences, no explanation."""


class SkillEvolver:
    """
    Tracks and evolves task execution strategies with progressive disclosure.

    Skills have two layers:
      Level 0 (index): name + description + tags (~20 tokens per skill)
      Level 1 (full):  procedure markdown + lessons + history (~500-2000 tokens)

    Two learning paths:
      1. record_task_outcome()      — explicit task delegation (API-driven)
      2. learn_from_conversation()  — auto-detect skills from chat (LLM-driven)
    """

    def __init__(
        self,
        rune: AgentRuntime,
        agent_id: str,
        llm_fn: Any,
        event_log: EventLog | None = None,
        skills_store: SkillsStore | None = None,
    ):
        if skills_store is None:
            # Phase D: typed store is the only path. Synthesise a
            # scratch store under tempdir when the caller doesn't
            # pass one (tests / standalone use). DigitalTwin always
            # wires the real, chain-mirrored one in production.
            import tempfile
            from pathlib import Path
            scratch = Path(tempfile.gettempdir()) / f"nexus-skills-scratch-{agent_id}"
            scratch.mkdir(parents=True, exist_ok=True)
            skills_store = SkillsStore(base_dir=scratch)
        self.rune = rune
        self.agent_id = agent_id
        self.llm_fn = llm_fn
        # Denormalised projection of skills_store. Carries operational
        # fields (evaluations / evolution_count / etc.) the Phase J
        # schema doesn't own. Rebuilt from skills_store on every
        # _load_skills_unlocked call so a verdict-driven rollback
        # surfaces here automatically.
        self._skills_cache: dict[str, dict] = {}
        # Topic frequency tracker: {"travel": 3, "coding": 5, ...}.
        # Local-only ephemeral state — losing it on cold start just
        # delays the next topic→skill promotion. Persisted to a
        # sidecar file under skills_store.base_dir so a process
        # restart picks up where we left off.
        self._topic_counts: dict[str, int] = {}
        # Threshold: after N occurrences of a topic, synthesize a skill
        self._topic_skill_threshold: int = 3
        self._dirty: bool = False
        self._lock = asyncio.Lock()
        # Phase O.2: emit evolution_proposal before each learn / topic
        # promotion. Optional — without it, SkillEvolver behaves
        # exactly as it did pre-Phase O.
        self.event_log = event_log
        self.skills_store = skills_store
        # Skill names that are blocked from learning (e.g., names that conflict
        # with registered tools). Set by EvolutionEngine on init.
        self._blocked_names: set[str] = set()
        # Lazy hydrate: defer reading the typed store until the first
        # load_skills() call so test fixtures can still construct a
        # SkillEvolver pointing at an empty store without I/O.
        self._hydrated: bool = False

    # ── Progressive Disclosure (Level 0 / Level 1) ──────────────

    def get_skill_index(self) -> list[dict]:
        """Level 0: Return lightweight summaries for all skills.

        Designed to be always injected into LLM context.
        ~20 tokens per skill, so 50 skills ≈ 1000 tokens.
        """
        index = []
        for name, skill in self._skills_cache.items():
            if name.startswith("_"):
                continue
            index.append({
                "name": name,
                "description": skill.get("description", skill.get("best_strategy", "")[:100]),
                "tags": skill.get("tags", []),
                "times_used": skill.get("times_used", 0),
                "success_rate": (
                    skill["success_count"] / skill["times_used"]
                    if skill.get("times_used", 0) > 0
                    else 0.0
                ),
            })
        return index

    def get_full_content(self, skill_name: str) -> Optional[str]:
        """Level 1: Return full procedure for a specific skill.

        Only loaded when LLM decides it needs this skill.
        """
        skill = self._skills_cache.get(skill_name)
        if not skill:
            return None

        parts = []

        # Procedure (markdown)
        procedure = skill.get("procedure", "")
        if procedure:
            parts.append(procedure)

        # Best strategy (legacy format, still useful)
        strategy = skill.get("best_strategy", "")
        if strategy and not procedure:
            parts.append(f"**Strategy:** {strategy}")

        # Recent lessons
        lessons = skill.get("lessons", [])
        if lessons:
            recent = lessons[-3:]  # Last 3 lessons
            parts.append("\n## Recent Lessons")
            for l in recent:
                parts.append(f"- {l.get('lesson', '')}")

        return "\n".join(parts) if parts else None

    def match_skills(self, query: str, top_k: int = 2) -> list[dict]:
        """Match query to skills using tag overlap + description TF-IDF.

        Returns the top_k most relevant skills (Level 0 info).
        """
        if not self._skills_cache:
            return []

        query_tokens = set(self._tokenize(query))
        if not query_tokens:
            return []

        scored = []
        for name, skill in self._skills_cache.items():
            if name.startswith("_"):
                continue

            # Tag matching (weight: 2x)
            skill_tags = set(t.lower() for t in skill.get("tags", []))
            tag_score = len(query_tokens & skill_tags) * 2

            # Skill name matching (weight: 3x)
            name_tokens = set(self._tokenize(name))
            name_score = len(query_tokens & name_tokens) * 3

            # Description TF-IDF (weight: 1x)
            desc = skill.get("description", "") + " " + skill.get("best_strategy", "")
            desc_tokens = set(self._tokenize(desc))
            desc_score = len(query_tokens & desc_tokens)

            # Usage frequency boost (up to 0.5 extra)
            usage_boost = min(skill.get("times_used", 0) / 20, 0.5)

            total = tag_score + name_score + desc_score + usage_boost
            if total > 0:
                scored.append((total, skill))

        scored.sort(key=lambda x: x[0], reverse=True)
        return [s[1] for s in scored[:top_k]]

    def record_skill_usage(self, skill_name: str, success: bool = True):
        """Record that a skill was used in context generation.

        Called by EvolutionEngine after injecting a skill into LLM context.
        Enables evaluation feedback loop.
        """
        skill = self._skills_cache.get(skill_name)
        if not skill:
            return
        skill["times_used"] = skill.get("times_used", 0) + 1
        if success:
            skill["success_count"] = skill.get("success_count", 0) + 1
        else:
            skill["failure_count"] = skill.get("failure_count", 0) + 1
        skill["last_used"] = time.time()
        self._dirty = True

    @staticmethod
    def _tokenize(text: str) -> list[str]:
        """Simple word tokenizer for matching."""
        return [w for w in re.findall(r'\w+', text.lower()) if len(w) > 1]

    # ── Load / Save ─────────────────────────────────────────────

    async def load_skills(self) -> dict[str, dict]:
        async with self._lock:
            return await self._load_skills_unlocked()

    async def _load_skills_unlocked(self) -> dict[str, dict]:
        """Rebuild the in-memory projection from the typed store.

        Called at startup, after typed-store rollbacks, and any time
        the engine wants a fresh view. Carries forward operational
        fields the Phase J schema doesn't own (evaluations,
        evolution_count, version, last_used, times_used, lessons[])
        from the previous in-memory projection so a refresh doesn't
        wipe usage counters.
        """
        try:
            typed_skills = list(self.skills_store.all())
        except Exception as e:  # noqa: BLE001
            logger.warning("SkillsStore.all() failed: %s", e)
            typed_skills = []

        preserved = {n: dict(s) for n, s in self._skills_cache.items()}
        new_cache: dict[str, dict] = {}
        for ls in typed_skills:
            old = preserved.get(ls.skill_name) or {}
            # Convert the typed lessons[] (list of {lesson, success,
            # timestamp}) into the legacy projection shape (list of
            # {lesson, outcome, source, timestamp}). Preserve any
            # extra dict shape the legacy cache had — old
            # evaluations / evolution_count fields are kept.
            typed_lessons = list(ls.lessons or [])
            projected_lessons = old.get("lessons") or []
            if typed_lessons:
                projected_lessons = [
                    {
                        "lesson": l.get("lesson", ""),
                        "outcome": "success" if l.get("success") else "failure",
                        "source": l.get("source", "unknown"),
                        "timestamp": l.get("timestamp", 0.0),
                    }
                    for l in typed_lessons[-10:]
                ]
            new_cache[ls.skill_name] = {
                **old,
                "name": ls.skill_name,
                "description": ls.description,
                "best_strategy": ls.strategy,
                "procedure": ls.strategy,
                "confidence": float(ls.confidence),
                "tags": list(ls.tags),
                "success_count": int(ls.success_count),
                "failure_count": int(ls.failure_count),
                "task_count": int(ls.success_count + ls.failure_count),
                "times_used": int(ls.times_used),
                "last_used": float(ls.last_used),
                "lessons": projected_lessons,
                "version": old.get("version", 1),
                "last_lesson": ls.last_lesson,
                "updated_at": ls.updated_at,
                "created_at": ls.created_at,
            }
        self._skills_cache = new_cache

        # Hydrate topic_counts from sidecar on first load.
        if not self._hydrated:
            self._topic_counts = self._read_topic_counts()
            self._hydrated = True

        self._dirty = False
        return self._skills_cache

    def _topic_counts_path(self):
        # Sidecar file inside skills_store base_dir. Local-only — not
        # chain-mirrored: topic counts are ephemeral progress markers
        # and reconstruct cheaply from chat history if lost.
        from pathlib import Path
        base = Path(self.skills_store.base_dir)
        return base / "_topic_counts.json"

    def _read_topic_counts(self) -> dict[str, int]:
        p = self._topic_counts_path()
        if not p.exists():
            return {}
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            return {str(k): int(v) for k, v in (data or {}).items()}
        except Exception as e:  # noqa: BLE001
            logger.warning("topic_counts read failed: %s", e)
            return {}

    def _write_topic_counts(self) -> None:
        p = self._topic_counts_path()
        try:
            tmp = p.with_suffix(".tmp")
            tmp.write_text(
                json.dumps(self._topic_counts, ensure_ascii=False),
                encoding="utf-8",
            )
            tmp.replace(p)
        except Exception as e:  # noqa: BLE001
            logger.warning("topic_counts write failed: %s", e)

    async def _save_skills_unlocked(self):
        """Persist the cache → typed store + commit a new version.

        This replaces the old ``rune.artifacts.save`` write path.
        Every dirty entry in the cache is upserted into the typed
        store, then the store is committed (which triggers chain
        mirroring via VersionedStore). Topic counts are persisted
        to a local-only sidecar file.
        """
        for name, skill in self._skills_cache.items():
            if name.startswith("_"):
                continue
            try:
                self._upsert_to_typed_store(name, skill)
            except Exception as e:  # noqa: BLE001
                logger.warning(
                    "skills_store.upsert failed for %s: %s", name, e,
                )
        try:
            self.skills_store.commit()
        except Exception as e:  # noqa: BLE001
            logger.warning("skills_store.commit failed: %s", e)
        self._write_topic_counts()

    def _upsert_to_typed_store(self, skill_name: str, skill: dict) -> None:
        """Project a cache entry into a :class:`LearnedSkill` and
        upsert it into the typed store.

        Inverse of the projection in ``_load_skills_unlocked``.
        Preserves the operational counters that ``LearnedSkill``
        owns (times_used, last_used, lessons[]).
        """
        confidence = float(skill.get("confidence", 0.5) or 0.0)
        confidence = max(0.0, min(1.0, confidence))
        tags = [str(t) for t in skill.get("tags", []) if t]
        legacy_lessons = skill.get("lessons") or []
        typed_lessons = []
        for l in legacy_lessons[-10:]:
            if not isinstance(l, dict):
                continue
            typed_lessons.append({
                "lesson": l.get("lesson", "") or "",
                "success": l.get("outcome", "success") in ("success", "pattern"),
                "source": l.get("source", "unknown"),
                "timestamp": l.get("timestamp", 0.0),
            })
        last_lesson = ""
        if legacy_lessons and isinstance(legacy_lessons[-1], dict):
            last_lesson = legacy_lessons[-1].get("lesson", "") or ""
        learned = LearnedSkill(
            skill_name=skill_name,
            description=str(skill.get("description", "") or "")[:500],
            strategy=str(
                skill.get("best_strategy", "")
                or skill.get("procedure", "")
                or "",
            )[:4000],
            last_lesson=last_lesson[:500],
            confidence=confidence,
            success_count=int(skill.get("success_count", 0) or 0),
            failure_count=int(skill.get("failure_count", 0) or 0),
            times_used=int(skill.get("times_used", 0) or 0),
            last_used=float(skill.get("last_used", 0.0) or 0.0),
            lessons=typed_lessons,
            task_kinds=[],
            tags=tags,
        )
        self.skills_store.upsert(learned)

    # ── Path 1: Explicit Task Learning ───────────────────────────

    async def record_task_outcome(
        self,
        task_type: str,
        description: str,
        strategy: str,
        outcome: str,
        feedback: str = "",
    ) -> Optional[dict]:
        async with self._lock:
            return await self._record_task_outcome_unlocked(
                task_type, description, strategy, outcome, feedback,
            )

    async def _record_task_outcome_unlocked(
        self,
        task_type: str,
        description: str,
        strategy: str,
        outcome: str,
        feedback: str = "",
    ) -> Optional[dict]:
        await self._load_skills_unlocked()

        existing = self._skills_cache.get(task_type, {})
        prompt = SKILL_ANALYSIS_PROMPT.format(
            task_type=task_type,
            description=description,
            strategy=strategy,
            outcome=outcome,
            feedback=feedback or "None provided",
            existing_skills=json.dumps(existing, ensure_ascii=False) if existing else "None yet",
        )

        try:
            raw = await self.llm_fn(prompt)
            logger.debug(
                "Skill extraction: LLM raw response (%d chars): %.200s%s",
                len(raw), raw, "..." if len(raw) > 200 else "",
            )
            learning = _robust_json_parse(raw)
        except Exception as e:
            raw_preview = repr(raw[:300]) if 'raw' in dir() and raw else "<no response>"
            logger.warning(
                "Skill extraction failed: %s | LLM response (%s chars): %s",
                e, len(raw) if 'raw' in dir() and raw else 0, raw_preview,
            )
            return None

        if not isinstance(learning, dict):
            logger.warning(f"Skill analysis returned non-dict: {type(learning)}")
            return None

        skill_name = learning.get("skill_name", task_type)
        self._upsert_skill(
            skill_name=skill_name,
            description=learning.get("description", ""),
            procedure=learning.get("procedure", ""),
            lesson=learning.get("lesson", ""),
            strategy=learning.get("strategy_update", learning.get("procedure", "")),
            confidence=learning.get("confidence", 0.5),
            tags=learning.get("tags", []),
            outcome=outcome,
            source="task",
        )
        await self._save_skills_unlocked()
        logger.info(f"Skill updated [{skill_name}]: {learning.get('lesson', '')[:60]}...")
        return learning

    # ── Path 2: Conversation-Based Learning ──────────────────────

    async def learn_from_conversation(
        self,
        conversation: list[dict],
        max_skills: int = 3,
    ) -> list[dict]:
        """
        Analyze a conversation and extract skills automatically.

        Returns list of learned skills (may be empty if conversation
        was trivial or no skills were detected).
        """
        async with self._lock:
            return await self._learn_from_conversation_unlocked(conversation, max_skills)

    async def _learn_from_conversation_unlocked(
        self,
        conversation: list[dict],
        max_skills: int = 3,
    ) -> list[dict]:
        await self._load_skills_unlocked()

        convo_text = "\n".join(
            f"{'User' if m['role'] == 'user' else 'Nexus'}: {m['content']}"
            for m in conversation[-10:]
        )

        # Filter out skills cache internal keys
        visible_skills = {
            k: v for k, v in self._skills_cache.items()
            if not k.startswith("_")
        }

        prompt = CONVERSATION_SKILL_PROMPT.format(
            conversation=convo_text,
            existing_skills=json.dumps(
                {name: {
                    "description": s.get("description", ""),
                    "strategy": s.get("best_strategy", ""),
                    "tasks": s.get("task_count", 0),
                }
                 for name, s in visible_skills.items()},
                ensure_ascii=False,
            ) if visible_skills else "Empty — no skills learned yet",
        )

        try:
            raw = await self.llm_fn(prompt)
            # Gemini often returns empty for trivial conversations (greetings,
            # small talk) where no skills are detected. This is expected — skip
            # silently instead of logging a warning every turn.
            if not raw or not raw.strip():
                logger.debug("Skill detection: LLM returned empty (no skills in conversation)")
                return []
            logger.debug(
                "Skill detection: LLM raw response (%d chars): %.200s%s",
                len(raw), raw, "..." if len(raw) > 200 else "",
            )
            result = _robust_json_parse(raw)
        except Exception as e:
            # Log the raw LLM response so we can see what failed to parse
            raw_preview = repr(raw[:300]) if 'raw' in dir() and raw else "<no response>"
            logger.warning(
                "Conversation skill detection failed: %s | LLM response (%s chars): %s",
                e, len(raw) if 'raw' in dir() and raw else 0, raw_preview,
            )
            return []

        if not isinstance(result, dict):
            return []

        learned = []

        # ── Process implicit tasks ──
        implicit_tasks = result.get("implicit_tasks", [])
        if isinstance(implicit_tasks, list):
            for task in implicit_tasks[:max_skills]:
                if not isinstance(task, dict):
                    continue
                skill_name = task.get("skill_name", "")
                if not skill_name:
                    continue

                # Skip skills that conflict with registered tool names.
                # Without this, the LLM learns "web_search" as a text skill
                # and then role-plays using it instead of calling the real tool.
                if skill_name in self._blocked_names:
                    logger.debug(
                        "Skipping skill '%s' — conflicts with registered tool",
                        skill_name,
                    )
                    continue

                self._upsert_skill(
                    skill_name=skill_name,
                    description=task.get("description", ""),
                    procedure=task.get("procedure", ""),
                    lesson=task.get("lesson", ""),
                    strategy=task.get("strategy", task.get("procedure", "")),
                    confidence=task.get("confidence", 0.5),
                    tags=task.get("tags", []),
                    outcome="success",
                    source="conversation",
                )
                learned.append({
                    "skill_name": skill_name,
                    "lesson": task.get("lesson", ""),
                    "source": "implicit_task",
                    "description": task.get("description", ""),
                })
                logger.info(
                    f"Skill from conversation [{skill_name}]: "
                    f"{task.get('lesson', '')[:60]}..."
                )

        # ── Process topic signals → accumulate and promote ──
        topic_signals = result.get("topic_signals", [])
        if isinstance(topic_signals, list):
            promoted = self._accumulate_topics(topic_signals)
            for topic_skill in promoted:
                learned.append(topic_skill)

        if learned:
            # Phase O.2: emit evolution_proposal BEFORE the durable
            # save so the verdict scorer can correlate this batch's
            # promotions with subsequent observed regressions.
            edit_id = self._emit_proposal_for_learn(learned)
            if edit_id:
                for s in learned:
                    s["evolution_edit_id"] = edit_id
            await self._save_skills_unlocked()

        return learned

    def _emit_proposal_for_learn(self, learned: list[dict]) -> str:
        """Emit an ``evolution_proposal`` event for this learn batch.

        Mirrors the pattern in MemoryEvolver / PersonaEvolver:
        opt-in (gated on ``self.event_log``), best-effort (failures
        logged + returned ""), conservative empty predictions until
        Phase O.4's task_kind classifier lands.
        """
        if self.event_log is None or not learned:
            return ""
        edit_id = str(uuid.uuid4())
        target_pre = (
            self.skills_store.current_version()
            if self.skills_store is not None else ""
        ) or "(uncommitted)"
        change_diff = [
            {
                "op": "upsert",
                "skill_name": s.get("skill_name", ""),
                "source": s.get("source", "conversation"),
                "preview": (s.get("description") or s.get("lesson") or "")[:80],
            }
            for s in learned
        ]
        # Phase C: distinguish skill-extraction batches (LLM detected
        # implicit tasks) from topic-promotion batches (frequency
        # threshold reached) so the lineage card can show "caused
        # by 3 conversations about X" vs "extracted from a single
        # conversation".
        sources = {s.get("source", "conversation") for s in learned}
        promoted = [
            s for s in learned if s.get("source") == "topic_pattern"
        ]
        proposal = EvolutionProposal(
            edit_id=edit_id,
            evolver="SkillEvolver",
            target_namespace="memory.skills",
            target_version_pre=target_pre,
            target_version_post=target_pre,  # working-state edit
            change_summary=f"learn {len(learned)} skill(s) from conversation",
            change_diff=change_diff,
            evidence_summary="conversation skill detection",
            rollback_pointer=target_pre,
            predicted_fixes=[],
            predicted_regressions=[],
            triggered_by={
                "trigger_reason": (
                    "topic_threshold_reached" if promoted
                    else "conversation_skill_detected"
                ),
                "counts": {
                    "skills_learned": len(learned),
                    "topic_promotions": len(promoted),
                },
                "sources": sorted(sources),
                "topic_counts": dict(self._topic_counts),
            },
        )
        try:
            self.event_log.append(
                event_type="evolution_proposal",
                content=(
                    f"SkillEvolver → memory.skills: "
                    f"learn {len(learned)} skill(s)"
                ),
                metadata=proposal.to_event_metadata(),
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("emit evolution_proposal (skills) failed: %s", e)
            return ""
        return edit_id

    def _accumulate_topics(self, signals: list[dict]) -> list[dict]:
        """
        Track topic frequencies. When a topic crosses the threshold,
        promote it to a skill entry.
        """
        promoted = []
        for signal in signals:
            if not isinstance(signal, dict):
                continue
            topic = signal.get("topic", "").lower().strip()
            if not topic:
                continue

            self._topic_counts[topic] = self._topic_counts.get(topic, 0) + 1
            count = self._topic_counts[topic]

            # Check if this topic should be promoted to a skill
            skill_name = topic.replace(" ", "_").replace("-", "_")
            already_skill = skill_name in self._skills_cache

            if count >= self._topic_skill_threshold and not already_skill:
                self._upsert_skill(
                    skill_name=skill_name,
                    description=f"Domain expertise in {topic}",
                    procedure=f"## When to Use\nConversations about {topic}.\n\n## Approach\nDraw on accumulated knowledge from {count} past conversations about {topic}.",
                    lesson=f"User frequently discusses {topic} ({count} conversations). Developing domain expertise.",
                    strategy=f"Draw on accumulated knowledge about {topic} from past conversations.",
                    confidence=min(0.3 + count * 0.1, 0.9),
                    tags=[topic, "topic_expertise"],
                    outcome="pattern",
                    source="topic_accumulation",
                )
                promoted.append({
                    "skill_name": skill_name,
                    "lesson": f"Topic expertise unlocked: {topic} ({count} conversations)",
                    "source": "topic_pattern",
                })
                logger.info(
                    f"Topic promoted to skill [{skill_name}] "
                    f"after {count} conversations"
                )
            elif already_skill and count % self._topic_skill_threshold == 0:
                # Reinforce existing topic skill
                skill = self._skills_cache[skill_name]
                skill["confidence"] = min(skill.get("confidence", 0.5) + 0.05, 0.95)
                skill["task_count"] = skill.get("task_count", 0) + 1
                skill["updated_at"] = time.time()

        return promoted

    # ── Shared: Upsert Skill Entry ───────────────────────────────

    def _upsert_skill(
        self,
        skill_name: str,
        description: str = "",
        procedure: str = "",
        lesson: str = "",
        strategy: str = "",
        confidence: float = 0.5,
        tags: list[str] = None,
        outcome: str = "success",
        source: str = "task",
    ):
        """Create or update a skill entry in the cache."""
        if tags is None:
            tags = []

        if skill_name not in self._skills_cache:
            self._skills_cache[skill_name] = {
                "name": skill_name,
                "description": "",
                "procedure": "",
                "lessons": [],
                "best_strategy": "",
                "confidence": 0.0,
                "tags": [],
                "task_count": 0,
                "success_count": 0,
                "times_used": 0,
                "failure_count": 0,
                "last_used": 0.0,
                "version": 1,
                "created_at": time.time(),
            }

        skill = self._skills_cache[skill_name]

        # Update description (keep latest non-empty)
        if description:
            skill["description"] = description[:200]

        # Update procedure (keep latest non-empty)
        if procedure:
            skill["procedure"] = procedure

        # Append lesson
        if lesson:
            skill["lessons"].append({
                "lesson": lesson,
                "outcome": outcome,
                "source": source,
                "timestamp": time.time(),
            })
            skill["lessons"] = skill["lessons"][-10:]  # keep last 10

        if strategy:
            skill["best_strategy"] = strategy
        skill["confidence"] = max(skill["confidence"], confidence)
        skill["tags"] = list(set(skill.get("tags", []) + tags))
        skill["task_count"] = skill.get("task_count", 0) + 1
        if outcome.lower() in ("success", "pattern"):
            skill["success_count"] = skill.get("success_count", 0) + 1
        skill["updated_at"] = time.time()
        self._dirty = True  # Mark as locally modified — background load will merge

        # Phase D: write-through to typed store happens via the
        # next ``_save_skills_unlocked()`` call. We don't upsert
        # eagerly here because batched commits keep the typed
        # store's version count from exploding (one commit per
        # learn-batch instead of one per skill).

    # ── Query (typed-store backed) ───────────────────────────────

    def get_strategy_from_cache(self, task_type: str) -> Optional[str]:
        """
        Get strategy from in-memory cache ONLY.

        Never triggers a load — if skills aren't loaded yet, returns None.
        This is the non-blocking version used during chat.
        """
        return self._match_strategy(task_type)

    async def get_strategy_for(self, task_type: str, context: str = "") -> Optional[str]:
        async with self._lock:
            await self._load_skills_unlocked()
            return self._match_strategy(task_type)

    def _match_strategy(self, task_type: str) -> Optional[str]:
        """Pure in-memory strategy lookup."""
        if task_type in self._skills_cache:
            return self._skills_cache[task_type].get("best_strategy")
        for name, skill in self._skills_cache.items():
            if name.startswith("_"):
                continue
            if task_type.lower() in [t.lower() for t in skill.get("tags", [])]:
                return skill.get("best_strategy")
        return None

    # Phase D removed apply_rollback. Verdict-driven typed-store
    # rollback now propagates automatically: the engine calls
    # _load_skills_unlocked() on its next read, which rebuilds the
    # cache from skills_store.all() — and that already reflects the
    # rolled-back active version.

    async def get_stats(self) -> dict:
        await self.load_skills()
        visible = {k: v for k, v in self._skills_cache.items() if not k.startswith("_")}
        return {
            "total_skills": len(visible),
            "total_tasks_completed": sum(
                s.get("task_count", 0) for s in visible.values()
            ),
            "topic_tracking": dict(self._topic_counts),
            "skills": {
                name: {
                    "tasks": s.get("task_count", 0),
                    "times_used": s.get("times_used", 0),
                    "success_rate": (
                        s["success_count"] / s["task_count"]
                        if s.get("task_count", 0) > 0 else 0
                    ),
                    "confidence": s.get("confidence", 0),
                    "has_procedure": bool(s.get("procedure")),
                }
                for name, s in visible.items()
            },
        }

    # ── Phase C: Evolution Pressure dashboard ─────────────────────

    def pressure_state(self) -> dict:
        """Per-evolver state for the Pressure Dashboard.

        SkillEvolver has TWO modes:
          * conversation-driven (every turn ⇒ "live")
          * topic-accumulator (per-topic counter → promotes to a
            skill at threshold)

        We surface a top-level "live" status (the conversation path
        always runs) plus a ``details.topics`` breakdown so the UI
        can render per-topic gauges side-by-side. The accumulator is
        the **most-active topic's count** — gives the dashboard a
        single number to draw a primary gauge with.
        """
        topics = dict(self._topic_counts)
        # Pick the topic closest to (but not yet over) threshold for
        # the headline gauge — that's what's "about to evolve".
        threshold = max(1, self._topic_skill_threshold)
        not_yet_promoted = {
            t: c for t, c in topics.items()
            if c < threshold and t not in self._skills_cache
        }
        primary_topic = max(
            not_yet_promoted.items(),
            key=lambda kv: kv[1],
            default=("(none)", 0),
        )
        return {
            "evolver": "SkillEvolver",
            "layer": "L2",
            "accumulator": float(primary_topic[1]),
            "threshold": float(threshold),
            "unit": "topic_count",
            "status": "live",  # conversation path always runs
            "fed_by": ["chat.turn"],
            "last_fired_at": None,
            "details": {
                "primary_topic": primary_topic[0],
                "topics": {
                    t: {
                        "count": c,
                        "ready": c >= threshold,
                        "promoted": t in self._skills_cache,
                    }
                    for t, c in topics.items()
                },
                "total_topics_tracked": len(topics),
                "total_skills": len(
                    {k: v for k, v in self._skills_cache.items()
                     if not k.startswith("_")}
                ),
                "topic_threshold": threshold,
            },
        }
