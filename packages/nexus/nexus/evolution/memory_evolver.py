"""
MemoryEvolver — Extract insights from conversations and persist as memories.

Memory management strategy (inspired by Hermes Agent):
  - Bounded capacity: configurable hard limit (default 500 memories per agent)
  - Smart eviction: when capacity is reached, consolidate least-accessed memories
  - Consolidation: LLM merges similar low-value memories into summaries
  - Access tracking: facts_store tracks access_count per fact for eviction

Phase D 续 (#157)
-----------------
Single source of truth: the typed ``FactsStore``. The evolver no
longer dual-writes to ``rune.memory`` (MemoryProvider) — facts
go straight into ``facts_store`` and chat-time recall queries
``facts_store.search_compact`` directly.

Removed paths:
* ``rune.memory.bulk_add`` / ``bulk_delete`` / ``list_all`` /
  ``search_compact`` / ``get_by_ids`` / ``count`` /
  ``get_least_accessed`` — all replaced with ``facts_store``
  equivalents.
* ``_dual_write_facts`` — facts_store IS the canonical write,
  there is no second store to mirror to.
* `_MEMORY_TO_FACT_CATEGORY` — kept (it normalises freeform LLM
  category labels into FactsStore's 5-bucket vocabulary).
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any

from nexus_core import AgentRuntime
from nexus_core.evolution import EvolutionProposal
from nexus_core.memory import EventLog, Fact, FactsStore
from nexus_core.utils import robust_json_parse as _sdk_robust_json_parse

logger = logging.getLogger(__name__)


# ── Phase J mapping: MemoryEvolver categories → FactsStore categories ──
#
# The legacy extraction prompt uses a richer category vocabulary than
# the typed FactsStore (which keeps to 5 deliberately narrow buckets per
# BEP-Nexus §3.3). The mapping below is a deliberate lossy projection:
# only the dimensions FactsStore actually distinguishes are kept; the
# rest collapse into "context". "skill" extractions are intentionally
# *not* written to FactsStore — those belong in SkillsStore via
# SkillEvolver.learn_from_conversation, and double-writing would
# clobber category-based filtering on the Facts side.
_MEMORY_TO_FACT_CATEGORY: dict[str, str | None] = {
    "preference": "preference",
    "fact": "fact",
    "decision_pattern": "context",
    "style": "context",
    # Phase D 续 (per user direction "映射进 facts"): legacy used
    # to drop "skill" extractions because SkillEvolver was the
    # owner. Now that FactsStore is the single source of truth for
    # declarative claims, "user wants to learn X" / "user is good
    # at Y" are valid facts about the user — they ride on top of
    # whatever SkillEvolver records about the agent's own learned
    # strategies. Mapped to ``context`` (the catch-all bucket).
    "skill": "context",
    "relationship": "context",
}

# ── Consolidation Prompt ───────────────────────────────────────

CONSOLIDATION_PROMPT = """You are consolidating agent memories to save space.

These memories are the LEAST accessed and may contain overlapping or low-value information.
Merge them into fewer, higher-quality summary memories.

Memories to consolidate:
{memories}

Rules:
- Merge similar memories into concise summaries
- Drop truly trivial information
- Preserve any unique, important facts
- Each consolidated memory should be 1-2 sentences
- Aim to reduce count by at least 50%

Return a JSON array of consolidated memories:
[
  {{"content": "Consolidated insight", "category": "fact", "importance": 3}},
  ...
]

Return ONLY valid JSON array, no markdown fences."""


# ── JSON parsing: delegate to SDK shared utility ─────────────
# Kept as module-level name so existing `from .memory_evolver import _robust_json_parse`
# continues to work in skill_evolver, skill_evaluator, knowledge_compiler, and tests.
_robust_json_parse = _sdk_robust_json_parse


# Legacy: the following large block has been replaced by nexus_core.utils.robust_json_parse.
# The old code is removed; _robust_json_parse above is a re-export alias.
MEMORY_SELECT_PROMPT = """You are selecting relevant memories for a conversation.

Given a user query and a list of memory summaries, return the IDs of memories
that are relevant to the query. Be selective — only pick genuinely useful ones.

User query: {query}

Memory summaries:
{summaries}

Return ONLY a JSON array of memory_id strings, e.g. ["id1", "id2"].
If none are relevant, return [].
No markdown fences."""


EXTRACTION_PROMPT = """Analyze the following conversation and extract key insights.

Return a JSON array of memory objects. Each object has:
- "content": The insight as a concise statement (1-2 sentences)
- "category": One of "preference", "fact", "decision_pattern", "style", "skill", "relationship"
- "importance": 1-5 (5 = critical to remember)

Rules:
- Only extract genuinely new, meaningful insights
- Skip trivial/obvious information
- Prefer specific facts over vague observations
- Max {max_memories} items
- If nothing meaningful to extract, return an empty array []

Conversation:
{conversation}

Existing memories (avoid duplicates):
{existing_memories}

Return ONLY valid JSON array, no markdown fences."""


class MemoryEvolver:
    """Extracts and persists insights from conversations.

    Memory capacity management:
      - Hard limit (default 500) prevents unbounded growth
      - When capacity is reached, consolidate least-accessed memories
      - Consolidation uses LLM to merge similar low-value memories
      - Access tracking (from SDK) drives smart eviction decisions
    """

    # Capacity defaults (can be overridden via constructor)
    DEFAULT_MAX_MEMORIES = 500
    CONSOLIDATION_BATCH_SIZE = 20  # How many to consolidate at once
    CONSOLIDATION_TRIGGER_RATIO = 0.9  # Trigger at 90% capacity

    def __init__(
        self,
        rune: AgentRuntime,
        agent_id: str,
        llm_fn: Any,
        max_memories: int = DEFAULT_MAX_MEMORIES,
        facts_store: FactsStore | None = None,
        event_log: EventLog | None = None,
    ):
        if facts_store is None:
            # Phase D 续: typed store is the only path. Synthesise a
            # scratch store under tempdir when the caller doesn't
            # pass one (tests / standalone use). DigitalTwin always
            # wires the real, chain-mirrored one in production.
            # Path uses a UUID suffix so each evolver gets a fresh
            # store — avoids cross-test pollution when several
            # MemoryEvolver instances share the same agent_id.
            import tempfile
            import uuid as _uuid
            from pathlib import Path
            scratch = (
                Path(tempfile.gettempdir())
                / f"nexus-facts-scratch-{agent_id}-{_uuid.uuid4().hex[:8]}"
            )
            scratch.mkdir(parents=True, exist_ok=True)
            facts_store = FactsStore(base_dir=scratch)
        self.rune = rune
        self.agent_id = agent_id
        self.llm_fn = llm_fn
        self.max_memories = max_memories
        # Phase D 续 (#157): typed FactsStore is the single source of
        # truth for declarative facts. The legacy ``rune.memory``
        # (MemoryProvider) write path is gone.
        self.facts_store = facts_store
        # Phase O.2 instrumentation: when set, every extract_and_store
        # call emits an `evolution_proposal` event before the write so
        # the verdict scorer (Phase O.4) can later evaluate the edit
        # against observed events. Optional — without it, MemoryEvolver
        # behaves exactly as it did pre-Phase O.
        self.event_log = event_log
        self._extraction_count = 0
        self._consolidation_count = 0

    async def extract_and_store(
        self,
        conversation: list[dict],
        max_memories: int = 5,
    ) -> list[dict]:
        # Dedup against everything currently in the typed FactsStore.
        existing_texts = [f.content for f in self.facts_store.all()]

        convo_text = "\n".join(
            f"{'User' if m['role'] == 'user' else 'Twin'}: {m['content']}"
            for m in conversation[-10:]
        )

        prompt = EXTRACTION_PROMPT.format(
            conversation=convo_text,
            existing_memories=json.dumps(existing_texts, ensure_ascii=False) if existing_texts else "[]",
            max_memories=max_memories,
        )

        try:
            raw = await self.llm_fn(prompt)
            logger.debug(
                "Memory extraction: LLM raw response (%d chars): %.200s%s",
                len(raw), raw, "..." if len(raw) > 200 else "",
            )
            memories = _robust_json_parse(raw)
        except (json.JSONDecodeError, Exception) as e:
            raw_preview = repr(raw[:300]) if 'raw' in dir() and raw else "<no response>"
            logger.warning(
                "Memory extraction failed: %s | LLM response (%s chars): %s",
                e, len(raw) if 'raw' in dir() and raw else 0, raw_preview,
            )
            return []

        if not isinstance(memories, list):
            logger.warning(
                "Memory extraction: expected list, got %s: %.100s",
                type(memories).__name__, repr(memories),
            )
            return []

        # Build batch for bulk_add — single index write instead of N
        batch = []
        for mem in memories:
            if not isinstance(mem, dict) or "content" not in mem:
                continue
            content = mem["content"]
            category = mem.get("category", "fact")
            importance = mem.get("importance", 3)
            batch.append({
                "content": content,
                "metadata": {
                    "category": category,
                    "importance": importance,
                    "source": "conversation_extraction",
                    "extraction_round": self._extraction_count,
                },
                "_category": category,
                "_importance": importance,
            })

        stored = []
        if batch:
            # Phase O.2: emit evolution_proposal BEFORE the write so the
            # verdict scorer can correlate the proposal's predictions
            # against subsequent observed events.
            proposal_id = self._emit_proposal_for_batch(batch)
            if proposal_id:
                # Annotate each stored entry with the proposal id so the
                # verdict scorer can attribute observed regressions back
                # to the specific edit that introduced them.
                for item in batch:
                    item["metadata"]["evolution_edit_id"] = proposal_id

            # Phase D 续: write straight to FactsStore. "skill"-typed
            # extractions are dropped here (they belong in SkillEvolver
            # — see _MEMORY_TO_FACT_CATEGORY). Categories outside the
            # FactsStore vocabulary collapse via the same map.
            facts_to_add: list[Fact] = []
            kept_items: list[dict] = []
            for item in batch:
                src_category = item["_category"]
                mapped = _MEMORY_TO_FACT_CATEGORY.get(src_category, "fact")
                if mapped is None:
                    continue  # e.g. "skill" → owned by SkillEvolver
                importance = max(1, min(5, int(item["_importance"])))
                fact = Fact(
                    content=item["content"],
                    category=mapped,  # type: ignore[arg-type]
                    importance=importance,
                    extra={
                        "source": "memory_evolver",
                        "extraction_round": self._extraction_count,
                        "original_category": src_category,
                        **(
                            {"evolution_edit_id": proposal_id}
                            if proposal_id else {}
                        ),
                    },
                )
                facts_to_add.append(fact)
                kept_items.append(item)

            if facts_to_add:
                fact_keys = self.facts_store.bulk_add(facts_to_add)
                for key, item in zip(fact_keys, kept_items):
                    stored.append({
                        "memory_id": key,
                        "content": item["content"],
                        "category": item["_category"],
                        "importance": item["_importance"],
                    })
                    logger.info(
                        f"Stored fact [{item['_category']}]: "
                        f"{item['content'][:60]}...",
                    )

        self._extraction_count += 1

        # ── Capacity check: consolidate if approaching limit ──
        if stored:
            await self._check_and_consolidate()

        return stored

    # ── Phase O.2: emit evolution_proposal events ─────────────

    def _emit_proposal_for_batch(self, batch: list[dict]) -> str:
        """Emit an ``evolution_proposal`` event for this extraction.

        Returns the new edit_id (UUID4) so callers can stamp it onto
        the extracted entries' metadata. Returns ``""`` and is a no-op
        when ``self.event_log`` is not configured — Phase O.2 is opt-in
        per twin.

        Predictions are intentionally empty for now: MemoryEvolver
        cannot reliably forecast which task_kinds a freshly-extracted
        memory will improve or regress. The verdict scorer treats
        empty predictions as "any observed regression is unpredicted",
        which is the conservative reading per BEP §3.4.
        """
        if self.event_log is None or not batch:
            return ""

        edit_id = str(uuid.uuid4())
        target_pre = (
            self.facts_store.current_version() if self.facts_store is not None else ""
        ) or "(uncommitted)"

        # change_diff: one row per added entry, capturing what the
        # post-write state will see. Keep it lean — no full content.
        change_diff = [
            {
                "op": "add",
                "category": item.get("_category", "fact"),
                "importance": item.get("_importance", 3),
                "preview": (item.get("content") or "")[:80],
            }
            for item in batch
        ]
        proposal = EvolutionProposal(
            edit_id=edit_id,
            evolver="MemoryEvolver",
            target_namespace="memory.facts",
            target_version_pre=target_pre,
            target_version_post=target_pre,  # working-state edit, no commit
            change_summary=f"extract+upsert {len(batch)} memories",
            change_diff=change_diff,
            evidence_summary=f"extraction round #{self._extraction_count}",
            rollback_pointer=target_pre,
            # Predictions are deferred to Phase O.4's task_kind classifier.
            # Empty lists are valid — the scorer is conservative when
            # nothing was promised.
            predicted_fixes=[],
            predicted_regressions=[],
            # Phase C: lineage data for the Pressure Dashboard's
            # "caused by" view. MemoryEvolver fires once per turn,
            # so the trigger reason is always per-turn extraction —
            # the count tells the lineage card "this batch had N
            # facts" and the upstream chain layer is the chat turn
            # itself.
            triggered_by={
                "trigger_reason": "per_turn_extraction",
                "counts": {
                    "facts_in_batch": len(batch),
                    "extraction_round": self._extraction_count,
                },
            },
        )
        try:
            self.event_log.append(
                event_type="evolution_proposal",
                content=(
                    f"MemoryEvolver → memory.facts: "
                    f"extract+upsert {len(batch)} memories"
                ),
                metadata=proposal.to_event_metadata(),
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("emit evolution_proposal failed: %s", e)
            return ""
        return edit_id

    # ── Memory Capacity Management ─────────────────────────────

    async def _check_and_consolidate(self) -> int:
        """Check memory count and consolidate if approaching capacity.

        Returns the number of memories freed (0 if no consolidation needed).
        Phase D 续: uses ``facts_store`` exclusively.
        """
        current_count = self.facts_store.count()
        trigger_threshold = int(self.max_memories * self.CONSOLIDATION_TRIGGER_RATIO)

        if current_count < trigger_threshold:
            return 0

        logger.info(
            "Memory capacity %d/%d (%.0f%%) — triggering consolidation",
            current_count, self.max_memories,
            current_count / self.max_memories * 100,
        )

        # Get least-accessed facts as consolidation candidates
        candidates = self.facts_store.get_least_accessed(
            limit=self.CONSOLIDATION_BATCH_SIZE,
        )

        if len(candidates) < 3:
            logger.info("Too few candidates for consolidation (%d)", len(candidates))
            return 0

        # Use LLM to consolidate
        freed = await self._consolidate_memories(candidates)
        self._consolidation_count += 1

        logger.info(
            "Consolidation #%d: %d memories → freed %d slots (now %d/%d)",
            self._consolidation_count,
            len(candidates),
            freed,
            current_count - freed,
            self.max_memories,
        )
        return freed

    async def _consolidate_memories(self, candidates: list) -> int:
        """Merge a batch of low-value facts into fewer summaries.

        SAFETY: Add consolidated summaries FIRST, then delete originals.
        If the LLM produces empty/invalid results, originals are preserved.
        This prevents memory loss from LLM failures during consolidation.

        Phase D 续: candidates are :class:`Fact` rows from
        ``facts_store.get_least_accessed``. Returns net facts freed
        (deleted − added).
        """
        memories_text = "\n".join(
            f"- [{f.category}] (accessed {f.access_count}x) {f.content}"
            for f in candidates
        )

        prompt = CONSOLIDATION_PROMPT.format(memories=memories_text)

        try:
            raw = await self.llm_fn(prompt)
            logger.debug(
                "Memory consolidation: LLM raw response (%d chars): %.200s%s",
                len(raw), raw, "..." if len(raw) > 200 else "",
            )
            consolidated = _robust_json_parse(raw)
        except Exception as e:
            raw_preview = repr(raw[:300]) if 'raw' in dir() and raw else "<no response>"
            logger.warning(
                "Memory consolidation LLM failed: %s | LLM response: %s", e, raw_preview,
            )
            # SAFE FALLBACK: do NOT delete anything — just log the failure.
            return 0

        if not isinstance(consolidated, list):
            return 0

        # STEP 1: Add consolidated summaries FIRST (before any deletion)
        valid_summaries = [
            mem for mem in consolidated
            if isinstance(mem, dict) and mem.get("content")
        ]

        # Safety gate: require at least 1 valid summary before deleting originals.
        if not valid_summaries:
            logger.warning(
                "Consolidation produced 0 valid summaries from %d candidates — aborting",
                len(candidates),
            )
            return 0

        # Build typed Fact rows for the summaries.
        summary_facts: list[Fact] = []
        for mem in valid_summaries:
            raw_cat = mem.get("category", "fact")
            mapped = _MEMORY_TO_FACT_CATEGORY.get(raw_cat, "fact") or "context"
            importance = max(1, min(5, int(mem.get("importance", 3))))
            summary_facts.append(Fact(
                content=mem["content"],
                category=mapped,  # type: ignore[arg-type]
                importance=importance,
                extra={
                    "source": "consolidation",
                    "consolidation_round": self._consolidation_count,
                    "merged_count": len(candidates),
                    "original_category": raw_cat,
                },
            ))
        added_keys = self.facts_store.bulk_add(summary_facts)
        added = len(added_keys)

        # STEP 2: Only delete originals after summaries are stored
        original_keys = [f.key for f in candidates]
        deleted = self.facts_store.bulk_delete(original_keys)

        logger.info(
            "Consolidation: %d originals → %d summaries (freed %d slots)",
            len(candidates), added, deleted - added,
        )
        return deleted - added

    async def force_consolidate(self, batch_size: int = 0) -> int:
        """Manually trigger consolidation regardless of capacity.

        Useful for maintenance or testing.
        Returns net memories freed.
        """
        size = batch_size or self.CONSOLIDATION_BATCH_SIZE
        candidates = self.facts_store.get_least_accessed(limit=size)
        if len(candidates) < 3:
            return 0
        return await self._consolidate_memories(candidates)

    async def recall_relevant(self, query: str, top_k: int = 5) -> list[dict]:
        """
        Progressive fact retrieval (Phase D 续):

          Layer 1: facts_store.search_compact → ranked summaries
          Layer 2: LLM selects relevant keys from the compact index
          Layer 3: facts_store.get → full content for selected facts

        TF-IDF ranking from search_compact is usually good enough;
        Layer 2 only kicks in when len(compacts) > top_k*3 (i.e.
        the candidate set is large enough that an LLM-as-judge
        picks meaningfully). Layer 3 also bumps access_count via
        ``touch_many`` so consolidation can spot truly cold facts.
        """
        compacts = self.facts_store.search_compact(query, top_k=top_k * 4)

        if not compacts:
            # No token overlap (common for cross-language queries).
            # Fall back to returning the most recent facts so the
            # LLM still has *some* context.
            all_facts = self.facts_store.all()
            if not all_facts:
                return []
            all_facts.sort(key=lambda f: f.created_at, reverse=True)
            return [
                {
                    "content": f.content,
                    "category": f.category,
                    "importance": f.importance,
                    "score": 0.0,
                }
                for f in all_facts[:top_k]
            ]

        if len(compacts) <= top_k * 3:
            keys = [c["key"] for c in compacts]
            self.facts_store.touch_many(keys)
            return [
                {
                    "content": (self.facts_store.get(k).content
                                if self.facts_store.get(k) else ""),
                    "category": c["category"],
                    "importance": c["importance"],
                    "score": c["score"],
                }
                for k, c in zip(keys, compacts)
                if self.facts_store.get(k) is not None
            ]

        # ── Layer 2: LLM selects relevant facts ──
        summaries_text = "\n".join(
            f"- [{c['key']}] ({c['category']}, importance={c['importance']}) {c['preview']}"
            for c in compacts
        )

        try:
            prompt = MEMORY_SELECT_PROMPT.format(
                query=query,
                summaries=summaries_text,
            )
            raw = await self.llm_fn(prompt)
            logger.debug(
                "Memory selection: LLM raw response (%d chars): %.200s%s",
                len(raw), raw, "..." if len(raw) > 200 else "",
            )
            selected_keys = _robust_json_parse(raw)
            if not isinstance(selected_keys, list):
                selected_keys = []
            selected_keys = selected_keys[:top_k]
        except Exception as e:
            raw_preview = repr(raw[:300]) if 'raw' in dir() and raw else "<no response>"
            logger.warning(
                "LLM memory selection failed, falling back: %s | LLM response: %s",
                e, raw_preview,
            )
            selected_keys = [c["key"] for c in compacts[:top_k]]

        if not selected_keys:
            selected_keys = [c["key"] for c in compacts[:top_k]]

        # ── Layer 3: Fetch full content + bump access counters ──
        self.facts_store.touch_many(selected_keys)
        results = []
        score_by_key = {c["key"]: c["score"] for c in compacts}
        for key in selected_keys:
            f = self.facts_store.get(key)
            if f is None:
                continue
            results.append({
                "content": f.content,
                "category": f.category,
                "importance": f.importance,
                "score": score_by_key.get(key, 0.0),
            })
        return results

    async def get_stats(self) -> dict:
        count = self.facts_store.count()
        all_facts = self.facts_store.all()
        categories: dict[str, int] = {}
        for f in all_facts:
            categories[f.category] = categories.get(f.category, 0) + 1
        return {
            "total_memories": count,
            "max_memories": self.max_memories,
            "capacity_pct": round(count / self.max_memories * 100, 1) if self.max_memories > 0 else 0,
            "categories": categories,
            "extraction_rounds": self._extraction_count,
            "consolidation_rounds": self._consolidation_count,
        }

    # ── Phase C: Evolution Pressure dashboard ─────────────────────

    def pressure_state(self) -> dict[str, Any]:
        """Per-evolver state for the desktop's Pressure Dashboard.

        MemoryEvolver fires once per chat turn (via
        ``extract_and_store``) — there's no accumulator counting up
        toward a threshold. We report ``status="live"`` so the UI
        shows a flat-line gauge rather than a progress bar that
        never moves. The "fed by" relationship is the chat layer
        itself: each turn produces fact-extraction work.

        ``details`` carries free-form context the lineage card may
        surface (e.g. how many extraction rounds have run, capacity
        usage of the underlying CuratedMemory store).
        """
        return {
            "evolver": "MemoryEvolver",
            "layer": "L1",
            "accumulator": float(self._extraction_count),
            # No real threshold — fires every turn. Use inf so the
            # UI knows to render "live" instead of a percentage.
            "threshold": float("inf"),
            "unit": "turns",
            "status": "live",
            "fed_by": ["chat.turn"],
            "last_fired_at": None,
            "details": {
                "extraction_rounds": self._extraction_count,
                "consolidation_rounds": self._consolidation_count,
                "max_memories": self.max_memories,
            },
        }
