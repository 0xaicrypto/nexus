"""
KnowledgeCompiler — Compile scattered memories into structured knowledge articles.

Inspired by claude-memory-compiler (Karpathy's LLM Knowledge Base architecture):
  Raw memories (fragments) → cluster by topic → LLM synthesizes → structured articles

Benefits:
  - Higher information density (1 article replaces ~10-20 raw memories in context)
  - More useful for retrieval (structured knowledge > scattered fragments)
  - Higher value on-chain (verifiable knowledge graph, not just raw observations)

Storage (Phase D)
-----------------
Articles live in the typed Phase J ``KnowledgeStore`` (single
source of truth). The internal ``_articles`` dict is now a
denormalised projection rebuilt from the typed store on every
``load_articles`` call; ``rune.artifacts.save("knowledge_articles.json", …)``
is gone, ``apply_rollback`` is gone, and there is no second
artifact to keep in sync.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from typing import Any, Optional

from nexus_core import AgentRuntime
from nexus_core.evolution import EvolutionProposal
from nexus_core.memory import EventLog, FactsStore, KnowledgeArticle, KnowledgeStore

from .memory_evolver import _robust_json_parse

logger = logging.getLogger(__name__)

# ── Prompts ──────────────────────────────────────────────────────

CLUSTER_PROMPT = """Analyze these memories and group them into topic clusters.

Memories:
{memories}

Return a JSON object mapping topic names to lists of memory indices (0-based).
Topics should be descriptive (e.g., "food_preferences", "travel_plans", "work_habits").

Rules:
- Each memory should belong to at most one cluster
- Skip memories that don't fit any meaningful cluster
- Minimum 2 memories per cluster (singletons aren't worth an article)
- Maximum 8 clusters
- Topic names should be snake_case

Example: {{"food_preferences": [0, 3, 7], "travel_plans": [1, 4, 5]}}

Return ONLY valid JSON, no markdown fences."""


COMPILE_PROMPT = """Synthesize these related memories into a structured knowledge article.

Topic: {topic}

Raw memories:
{memories}

Existing article (if updating):
{existing_article}

Write a concise knowledge article (100-200 words) that:
1. Summarizes the key patterns and facts from these memories
2. Highlights preferences, habits, or recurring themes
3. Notes any changes or evolution over time
4. Is written in third person ("The user prefers..." not "You prefer...")

Return a JSON object:
{{
  "title": "Short descriptive title",
  "summary": "1-sentence overview",
  "content": "The full article text",
  "key_facts": ["fact1", "fact2", ...],
  "tags": ["tag1", "tag2", ...],
  "memory_count": {memory_count},
  "confidence": 0.0-1.0
}}

Return ONLY valid JSON, no markdown fences."""


class KnowledgeCompiler:
    """
    Compiles scattered memories into structured knowledge articles.

    Triggered during reflection cycles (every N turns) by the EvolutionEngine.
    Articles are stored as versioned Rune Artifacts, creating a
    verifiable knowledge graph on-chain.
    """

    def __init__(
        self,
        rune: AgentRuntime,
        agent_id: str,
        llm_fn: Any,
        event_log: "EventLog | None" = None,
        knowledge_store: "KnowledgeStore | None" = None,
        facts_store: "FactsStore | None" = None,
    ):
        if knowledge_store is None:
            # Phase D: typed store is the only path. Synthesise a
            # scratch store under tempdir when the caller doesn't
            # pass one (tests / standalone use). DigitalTwin always
            # wires the real, chain-mirrored one in production.
            import tempfile
            import uuid as _uuid
            from pathlib import Path
            scratch = (
                Path(tempfile.gettempdir())
                / f"nexus-knowledge-scratch-{agent_id}-{_uuid.uuid4().hex[:8]}"
            )
            scratch.mkdir(parents=True, exist_ok=True)
            knowledge_store = KnowledgeStore(base_dir=scratch)
        if facts_store is None:
            # Phase D 续: KnowledgeCompiler reads facts as input for
            # clustering. Auto-synthesise a scratch FactsStore so
            # tests can construct without wiring (DigitalTwin wires
            # the real one in production).
            import tempfile
            import uuid as _uuid
            from pathlib import Path
            scratch = (
                Path(tempfile.gettempdir())
                / f"nexus-kn-facts-scratch-{agent_id}-{_uuid.uuid4().hex[:8]}"
            )
            scratch.mkdir(parents=True, exist_ok=True)
            facts_store = FactsStore(base_dir=scratch)
        self.rune = rune
        self.agent_id = agent_id
        self.llm_fn = llm_fn
        # Denormalised projection of knowledge_store. Rebuilt from
        # the typed store on each load_articles call so a verdict-
        # driven rollback surfaces here automatically.
        self._articles: dict[str, dict] = {}
        self._last_compiled_at: float = 0.0
        self._compilation_count: int = 0
        self._dirty: bool = False
        self._lock = asyncio.Lock()
        # Phase O.2: emit evolution_proposal once per compile() call
        # that actually produced new/updated articles. Optional —
        # without it, KnowledgeCompiler behaves exactly as it did
        # pre-Phase O.
        self.event_log = event_log
        self.knowledge_store = knowledge_store
        self.facts_store = facts_store

    async def load_articles(self) -> dict[str, dict]:
        """Rebuild the in-memory projection from the typed store."""
        try:
            typed_articles = list(self.knowledge_store.all())
        except Exception as e:  # noqa: BLE001
            logger.warning("KnowledgeStore.all() failed: %s", e)
            typed_articles = []
        new_articles: dict[str, dict] = {}
        for art in typed_articles:
            key = art.title or art.article_id
            new_articles[key] = {
                "article_id": art.article_id,
                "title": art.title,
                "summary": art.summary,
                "content": art.content,
                "key_facts": list(art.key_facts),
                "tags": list(art.tags),
                "source_fact_keys": list(art.source_fact_keys),
                "source_episode_ids": list(art.source_episode_ids),
                "confidence": float(art.confidence),
                "visibility": art.visibility,
                "updated_at": float(art.updated_at),
                "created_at": float(art.created_at),
            }
        self._articles = new_articles
        self._dirty = False
        return self._articles

    async def _save_articles(self):
        """Persist projection → typed store + commit a new version.

        Replaces the old ``rune.artifacts.save`` write path.
        Iterates the in-memory projection, upserts each entry into
        the typed store, then commits (which triggers chain
        mirroring via VersionedStore).
        """
        for topic, article in self._articles.items():
            try:
                self._upsert_to_typed_store(topic, article)
            except Exception as e:  # noqa: BLE001
                logger.warning(
                    "knowledge_store.upsert failed for %r: %s", topic, e,
                )
        try:
            self.knowledge_store.commit()
        except Exception as e:  # noqa: BLE001
            logger.warning("knowledge_store.commit failed: %s", e)

    async def compile(self, min_memories: int = 10) -> dict:
        """
        Main compilation pipeline:
          1. Load all raw memories
          2. Cluster by topic (LLM)
          3. Synthesize each cluster into a knowledge article (LLM)
          4. Store as versioned artifact

        Args:
            min_memories: Minimum memories required to trigger compilation.

        Returns:
            Compilation result with stats and new/updated articles.
        """
        await self.load_articles()

        # ── 1. Load all raw facts (Phase D 续) ──
        all_facts = self.facts_store.all()
        if len(all_facts) < min_memories:
            return {
                "status": "skipped",
                "reason": f"Only {len(all_facts)} facts (need {min_memories})",
                "articles_count": len(self._articles),
            }

        memory_texts = [
            f"[{f.category}, importance={f.importance}] {f.content}"
            for f in all_facts
        ]

        # ── 2. Cluster memories by topic ──
        clusters = await self._cluster_memories(memory_texts)
        if not clusters:
            return {
                "status": "no_clusters",
                "reason": "LLM found no meaningful topic clusters",
                "articles_count": len(self._articles),
            }

        # ── 3. Compile each cluster into an article ──
        new_articles = []
        updated_articles = []

        for topic, indices in clusters.items():
            # Gather memory texts for this cluster
            cluster_memories = [
                memory_texts[i] for i in indices
                if i < len(memory_texts)
            ]
            if len(cluster_memories) < 2:
                continue

            existing = self._articles.get(topic, {}).get("content", "")

            article = await self._compile_article(
                topic, cluster_memories, existing,
            )
            if not article:
                continue

            article["updated_at"] = time.time()
            article["memory_indices"] = indices

            if topic in self._articles:
                article["version"] = self._articles[topic].get("version", 0) + 1
                updated_articles.append(topic)
            else:
                article["version"] = 1
                article["created_at"] = time.time()
                new_articles.append(topic)

            self._articles[topic] = article

        self._dirty = True  # Mark as locally modified — background load will merge

        # Phase O.2: emit evolution_proposal BEFORE the durable save.
        # KnowledgeCompiler operates batch-style — one proposal per
        # compile() run that produced new/updated articles, with the
        # diff capturing the topic-level changes.
        edit_id = self._emit_proposal_for_compile(
            new_articles=new_articles,
            updated_articles=updated_articles,
        )

        # ── 4. Persist ──
        # Phase D: ``_save_articles`` writes the projection through
        # to the typed store and commits. The legacy artifact path
        # is gone — typed store is the single source of truth.
        self._compilation_count += 1
        self._last_compiled_at = time.time()
        await self._save_articles()

        result = {
            "status": "compiled",
            "compilation": self._compilation_count,
            "total_memories": len(all_facts),
            "clusters_found": len(clusters),
            "new_articles": new_articles,
            "updated_articles": updated_articles,
            "total_articles": len(self._articles),
            "evolution_edit_id": edit_id,
        }

        logger.info(
            f"Knowledge compiled: {len(new_articles)} new, "
            f"{len(updated_articles)} updated, "
            f"{len(self._articles)} total articles"
        )
        return result

    # ── Phase C: Evolution Pressure dashboard ─────────────────────

    def pressure_state(self, fact_count: int = 0, min_memories: int = 10) -> dict:
        """Per-evolver state for the Pressure Dashboard.

        KnowledgeCompiler fires when accumulated facts ≥
        ``min_memories`` (default 10). Caller passes the current
        ``fact_count`` (read from FactsStore / CuratedMemory) since
        the compiler doesn't own that store directly.
        """
        if min_memories <= 0:
            min_memories = 10
        accumulator = float(fact_count)
        threshold = float(min_memories)
        if accumulator >= threshold:
            status = "ready"
        elif accumulator >= threshold * 0.9:
            status = "warming"
        elif self._compilation_count > 0 and self._last_compiled_at > 0:
            status = "fired_recently" if (
                # Within last 5 min counts as "just fired"
                _now := __import__("time").time()
            ) - self._last_compiled_at < 300 else "warming"
        else:
            status = "warming"
        return {
            "evolver": "KnowledgeCompiler",
            "layer": "L1",
            "accumulator": accumulator,
            "threshold": threshold,
            "unit": "facts",
            "status": status,
            "fed_by": ["MemoryEvolver"],
            "last_fired_at": self._last_compiled_at or None,
            "details": {
                "compilation_count": self._compilation_count,
                "current_articles": len(self._articles),
                "min_memories": min_memories,
            },
        }

    def _emit_proposal_for_compile(
        self,
        *,
        new_articles: list[str],
        updated_articles: list[str],
    ) -> str:
        """Emit an ``evolution_proposal`` event for this compile batch.

        Same opt-in / best-effort pattern as MemoryEvolver /
        SkillEvolver / PersonaEvolver. KnowledgeCompiler operates at
        the article level, so the diff lists topics rather than full
        content (full content lives in KnowledgeStore version chain
        + the artifact store).
        """
        if self.event_log is None or (not new_articles and not updated_articles):
            return ""
        edit_id = str(uuid.uuid4())
        target_pre = (
            self.knowledge_store.current_version()
            if self.knowledge_store is not None else ""
        ) or "(uncommitted)"
        change_diff = (
            [{"op": "add", "topic": t} for t in new_articles] +
            [{"op": "update", "topic": t} for t in updated_articles]
        )
        proposal = EvolutionProposal(
            edit_id=edit_id,
            evolver="KnowledgeCompiler",
            target_namespace="memory.knowledge",
            target_version_pre=target_pre,
            target_version_post=target_pre,  # working-state edit
            change_summary=(
                f"compile: +{len(new_articles)} new, "
                f"~{len(updated_articles)} updated"
            ),
            change_diff=change_diff,
            evidence_summary=f"compilation round #{self._compilation_count + 1}",
            rollback_pointer=target_pre,
            predicted_fixes=[],
            predicted_regressions=[],
            # Phase C: KnowledgeCompiler is fed by the FactsStore.
            # Lineage card uses these counts to render
            # "caused by N facts → N+M articles".
            triggered_by={
                "trigger_reason": "fact_threshold_reached",
                "counts": {
                    "new_articles": len(new_articles),
                    "updated_articles": len(updated_articles),
                },
                "compilation_round": self._compilation_count + 1,
            },
        )
        try:
            self.event_log.append(
                event_type="evolution_proposal",
                content=(
                    f"KnowledgeCompiler → memory.knowledge: "
                    f"+{len(new_articles)} new, ~{len(updated_articles)} updated"
                ),
                metadata=proposal.to_event_metadata(),
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("emit evolution_proposal (knowledge) failed: %s", e)
            return ""
        return edit_id

    def _upsert_to_typed_store(self, topic: str, legacy: dict) -> None:
        """Project a single in-memory article into a typed
        :class:`KnowledgeArticle` and upsert it.

        Article id stability comes from looking up the existing
        typed row by title before upserting; this lets a retry on
        ``compile()`` produce the same article_id.
        """
        existing_id = legacy.get("article_id")
        if not existing_id:
            try:
                for ka in self.knowledge_store.all():
                    if ka.title and ka.title == legacy.get("title", topic):
                        existing_id = ka.article_id
                        break
            except Exception:  # noqa: BLE001
                existing_id = None
        article = KnowledgeArticle(
            article_id=existing_id or str(uuid.uuid4()),
            title=str(legacy.get("title") or topic)[:200],
            summary=str(legacy.get("summary", ""))[:1000],
            content=str(legacy.get("content", "")),
            key_facts=[
                str(f) for f in (legacy.get("key_facts") or []) if f
            ],
            tags=[
                str(t) for t in (legacy.get("tags") or []) if t
            ],
            source_fact_keys=[
                str(k) for k in (legacy.get("source_fact_keys") or []) if k
            ],
            source_episode_ids=[
                str(i) for i in (legacy.get("source_episode_ids") or []) if i
            ],
            confidence=max(
                0.0, min(1.0, float(legacy.get("confidence", 0.5) or 0.0)),
            ),
            visibility=(
                legacy.get("visibility")
                if legacy.get("visibility") in ("private", "connections", "public")
                else "private"
            ),
        )
        self.knowledge_store.upsert(article)

    async def _cluster_memories(self, memory_texts: list[str]) -> dict[str, list[int]]:
        """Use LLM to cluster memories into topics."""
        # Format as numbered list for index reference
        numbered = "\n".join(
            f"[{i}] {text}" for i, text in enumerate(memory_texts)
        )

        prompt = CLUSTER_PROMPT.format(memories=numbered)

        try:
            raw = await self.llm_fn(prompt)
            clusters = _robust_json_parse(raw)
        except Exception as e:
            logger.warning(f"Memory clustering failed: {e}")
            return {}

        if not isinstance(clusters, dict):
            return {}

        # Validate: each value should be a list of ints
        valid = {}
        for topic, indices in clusters.items():
            if isinstance(indices, list) and len(indices) >= 2:
                valid_indices = [
                    i for i in indices
                    if isinstance(i, int) and 0 <= i < len(memory_texts)
                ]
                if len(valid_indices) >= 2:
                    valid[topic] = valid_indices

        return valid

    async def _compile_article(
        self,
        topic: str,
        memories: list[str],
        existing_article: str = "",
    ) -> Optional[dict]:
        """Synthesize a cluster of memories into a knowledge article."""
        prompt = COMPILE_PROMPT.format(
            topic=topic,
            memories="\n".join(f"- {m}" for m in memories),
            existing_article=existing_article or "None (new article)",
            memory_count=len(memories),
        )

        try:
            raw = await self.llm_fn(prompt)
            article = _robust_json_parse(raw)
        except Exception as e:
            logger.warning(f"Article compilation failed for {topic}: {e}")
            return None

        if not isinstance(article, dict) or "content" not in article:
            return None

        return article

    def get_article(self, topic: str) -> Optional[dict]:
        """Get a specific knowledge article by topic."""
        return self._articles.get(topic)

    def get_all_articles(self) -> dict[str, dict]:
        """Get all knowledge articles."""
        return dict(self._articles)

    def get_context_from_cache(self, query: str, max_articles: int = 3) -> str:
        """
        Get relevant knowledge from in-memory cache ONLY.

        Never triggers a load — if articles aren't loaded yet, returns empty.
        This is the non-blocking version used during chat.
        """
        return self._match_articles(query, max_articles)

    async def get_context_for_query(self, query: str, max_articles: int = 3) -> str:
        """
        Get relevant knowledge articles for a query.

        Simple keyword matching against article titles, tags, and key_facts.
        Returns formatted context string for injection into LLM prompt.
        """
        if not self._articles:
            await self.load_articles()

        return self._match_articles(query, max_articles)

    # Phase D removed apply_rollback. A verdict-driven typed-store
    # rollback now propagates automatically: ``load_articles`` on
    # the next read rebuilds the projection from
    # ``knowledge_store.all()`` which already reflects the rolled-
    # back active version.

    def _match_articles(self, query: str, max_articles: int = 3) -> str:
        """Pure in-memory keyword matching against loaded articles."""
        if not self._articles:
            return ""

        query_words = set(query.lower().split())
        scored = []

        for topic, article in self._articles.items():
            # Score by keyword overlap with title, tags, key_facts
            searchable = (
                topic.replace("_", " ") + " " +
                article.get("title", "") + " " +
                " ".join(article.get("tags", [])) + " " +
                " ".join(article.get("key_facts", []))
            ).lower()

            article_words = set(searchable.split())
            overlap = len(query_words & article_words)
            if overlap > 0:
                scored.append((overlap, topic, article))

        if not scored:
            return ""

        scored.sort(reverse=True)
        parts = ["## Compiled Knowledge"]
        for _, topic, article in scored[:max_articles]:
            parts.append(f"\n### {article.get('title', topic)}")
            parts.append(article.get("content", ""))

        return "\n".join(parts)

    async def get_stats(self) -> dict:
        """Return compilation statistics."""
        if not self._articles:
            await self.load_articles()

        return {
            "total_articles": len(self._articles),
            "compilation_count": self._compilation_count,
            "last_compiled_at": self._last_compiled_at,
            "topics": list(self._articles.keys()),
            "articles": {
                topic: {
                    "title": a.get("title", ""),
                    "version": a.get("version", 1),
                    "memory_count": a.get("memory_count", 0),
                    "confidence": a.get("confidence", 0),
                }
                for topic, a in self._articles.items()
            },
        }
