"""
ImpressionProviderImpl — concrete ImpressionProvider backed by StorageBackend.

Domain logic:
  - Store/retrieve agent-to-agent impressions
  - Relational queries: by target, by source, mutual, ranked
  - Network statistics aggregation
  - Confidence gating (outlier detection)

Storage layout:
    agents/{agent_id}/impressions/{impression_id}.json — individual impressions
    agents/{agent_id}/impressions/index.json            — impression index
"""

from __future__ import annotations

import logging
import time
from typing import Optional

logger = logging.getLogger(__name__)

from ..core.backend import StorageBackend
from ..core.models import Impression, ImpressionSummary, NetworkStats
from ..core.providers import ImpressionProvider


class ImpressionProviderImpl(ImpressionProvider):
    """
    Concrete impression provider backed by StorageBackend.

    Maintains an in-memory index for fast queries, persisted
    to backend for durability. Same pattern as MemoryProviderImpl.
    """

    @staticmethod
    def _safe(value: str) -> str:
        """Sanitize a path component to prevent directory traversal."""
        return value.replace("/", "__").replace("\\", "__").replace("..", "__")

    def __init__(self, backend: StorageBackend):
        self._backend = backend
        # In-memory index: impression_id → Impression
        self._impressions: dict[str, Impression] = {}
        # Secondary indices for fast queries
        self._by_source: dict[str, list[str]] = {}   # source_agent → [impression_ids]
        self._by_target: dict[str, list[str]] = {}   # target_agent → [impression_ids]
        self._loaded_agents: set[str] = set()

    # ── Core CRUD ──────────────────────────────────────────────────

    async def record(self, impression: Impression) -> str:
        """Store a new impression. Returns impression_id."""
        # Persist to backend
        path = f"agents/{self._safe(impression.source_agent)}/impressions/{self._safe(impression.impression_id)}.json"
        await self._backend.store_json(path, impression.to_dict())

        # Update in-memory index
        self._impressions[impression.impression_id] = impression
        self._by_source.setdefault(impression.source_agent, []).append(impression.impression_id)
        self._by_target.setdefault(impression.target_agent, []).append(impression.impression_id)

        # Persist index
        await self._save_index(impression.source_agent)

        # Anchor on chain (optional — only if backend supports it)
        content_hash = StorageBackend.content_hash(
            StorageBackend.json_bytes(impression.to_dict())
        )
        await self._backend.anchor(
            impression.source_agent, content_hash, namespace="impression"
        )

        return impression.impression_id

    async def get_impressions_of(
        self,
        target_agent: str,
        agent_id: str,
        limit: int = 10,
    ) -> list[Impression]:
        """All impressions agent_id has formed about target_agent."""
        await self._ensure_loaded(agent_id)

        results = []
        for imp_id in self._by_source.get(agent_id, []):
            imp = self._impressions.get(imp_id)
            if imp and imp.target_agent == target_agent:
                results.append(imp)

        # Sort by recency (newest first)
        results.sort(key=lambda i: i.created_at, reverse=True)
        return results[:limit]

    async def get_impressions_from(
        self,
        agent_id: str,
        limit: int = 20,
    ) -> list[Impression]:
        """All impressions others have formed about this agent (inbound view)."""
        # This requires scanning across all agents — expensive but necessary.
        # In production, the SocialGraphAnchor contract provides this.
        # For Local/Mock, we scan all loaded impressions.
        results = []
        for imp in self._impressions.values():
            if imp.target_agent == agent_id:
                results.append(imp)

        results.sort(key=lambda i: i.created_at, reverse=True)
        return results[:limit]

    async def get_compatibility(
        self,
        agent_a: str,
        agent_b: str,
    ) -> Optional[float]:
        """Latest compatibility score (A's view of B)."""
        await self._ensure_loaded(agent_a)

        latest = None
        for imp_id in self._by_source.get(agent_a, []):
            imp = self._impressions.get(imp_id)
            if imp and imp.target_agent == agent_b:
                if latest is None or imp.created_at > latest.created_at:
                    latest = imp

        return latest.compatibility_score if latest else None

    async def get_top_matches(
        self,
        agent_id: str,
        top_k: int = 10,
        min_score: float = 0.0,
        dimension: Optional[str] = None,
    ) -> list[ImpressionSummary]:
        """Agents ranked by compatibility with this agent."""
        await self._ensure_loaded(agent_id)

        # Group impressions by target agent, keep latest per target
        by_target: dict[str, list[Impression]] = {}
        for imp_id in self._by_source.get(agent_id, []):
            imp = self._impressions.get(imp_id)
            if imp:
                by_target.setdefault(imp.target_agent, []).append(imp)

        summaries = []
        for target_id, imps in by_target.items():
            imps.sort(key=lambda i: i.created_at, reverse=True)
            latest = imps[0]

            # Apply score filter
            score = latest.compatibility_score
            if dimension:
                score = getattr(latest.dimensions, dimension, score)
            if score < min_score:
                continue

            # Find top dimension
            dims = latest.dimensions
            dim_scores = {
                "interest_overlap": dims.interest_overlap,
                "knowledge_complementarity": dims.knowledge_complementarity,
                "style_compatibility": dims.style_compatibility,
                "reliability": dims.reliability,
                "depth": dims.depth,
            }
            top_dim = max(dim_scores, key=dim_scores.get)

            summaries.append(ImpressionSummary(
                agent_id=target_id,
                latest_score=latest.compatibility_score,
                gossip_count=len(imps),
                last_gossip_at=latest.created_at,
                top_dimension=top_dim,
                would_gossip_again=latest.would_gossip_again,
            ))

        # Sort by score if dimension filter, else by compatibility
        if dimension:
            summaries.sort(
                key=lambda s: s.latest_score, reverse=True,
            )
        else:
            summaries.sort(key=lambda s: s.latest_score, reverse=True)

        return summaries[:top_k]

    async def get_mutual(
        self,
        agent_id: str,
        min_score: float = 0.5,
    ) -> list[tuple]:
        """
        Agents with mutual positive impressions.
        Returns list of (other_agent_id, my_score_of_them, their_score_of_me).
        """
        await self._ensure_loaded(agent_id)

        # Latest impression per target
        my_scores: dict[str, float] = {}
        my_latest_time: dict[str, float] = {}
        for imp_id in self._by_source.get(agent_id, []):
            imp = self._impressions.get(imp_id)
            if imp:
                target = imp.target_agent
                if target not in my_latest_time or imp.created_at > my_latest_time[target]:
                    my_scores[target] = imp.compatibility_score
                    my_latest_time[target] = imp.created_at

        # Find mutual: check if the other agent also has impressions of me
        mutuals = []
        for other_id, my_score in my_scores.items():
            if my_score < min_score:
                continue

            # Check their impressions of me
            their_score = None
            their_latest_time = 0.0
            for imp in self._impressions.values():
                if imp.source_agent == other_id and imp.target_agent == agent_id:
                    if imp.created_at > their_latest_time:
                        their_score = imp.compatibility_score
                        their_latest_time = imp.created_at

            if their_score is not None and their_score >= min_score:
                mutuals.append((other_id, my_score, their_score))

        # Sort by average mutual score
        mutuals.sort(key=lambda m: (m[1] + m[2]) / 2, reverse=True)
        return mutuals

    async def get_network_stats(self, agent_id: str) -> NetworkStats:
        """Aggregated social statistics for an agent."""
        await self._ensure_loaded(agent_id)

        # Outbound stats
        outbound = [
            self._impressions[imp_id]
            for imp_id in self._by_source.get(agent_id, [])
            if imp_id in self._impressions
        ]
        # Inbound stats
        inbound = [
            imp for imp in self._impressions.values()
            if imp.target_agent == agent_id
        ]

        unique_targets = set(imp.target_agent for imp in outbound)
        unique_sources = set(imp.source_agent for imp in inbound)
        unique_agents = unique_targets | unique_sources

        # Session count (unique gossip sessions)
        session_ids = set()
        for imp in outbound + inbound:
            if imp.gossip_session_id:
                session_ids.add(imp.gossip_session_id)

        avg_given = (
            sum(imp.compatibility_score for imp in outbound) / len(outbound)
            if outbound else 0.0
        )
        avg_received = (
            sum(imp.compatibility_score for imp in inbound) / len(inbound)
            if inbound else 0.0
        )

        # Find strongest mutual connections
        mutuals = await self.get_mutual(agent_id, min_score=0.0)
        strongest = [m[0] for m in mutuals[:5]]

        return NetworkStats(
            total_gossip_sessions=len(session_ids),
            unique_agents_met=len(unique_agents),
            avg_compatibility_given=round(avg_given, 3),
            avg_compatibility_received=round(avg_received, 3),
            top_interests_overlap=[],  # Populated when profiles are available
            strongest_connections=strongest,
        )

    # ── Confidence Gating ──────────────────────────────────────────

    async def check_confidence(
        self,
        impression: Impression,
        threshold: float = 0.3,
    ) -> bool:
        """
        Check if an impression's dimension scores are within confidence bounds.

        Returns True if all dimensions are within threshold of historical mean.
        Returns False (flagged for re-evaluation) if any dimension is an outlier.
        """
        history = await self.get_impressions_of(
            impression.target_agent,
            impression.source_agent,
            limit=50,
        )
        if len(history) < 2:
            return True  # Not enough history to check

        # Compute historical means per dimension
        dim_fields = [
            "interest_overlap", "knowledge_complementarity",
            "style_compatibility", "reliability", "depth",
        ]
        for dim in dim_fields:
            hist_values = [getattr(h.dimensions, dim) for h in history]
            mean = sum(hist_values) / len(hist_values)
            current = getattr(impression.dimensions, dim)
            if abs(current - mean) > threshold:
                return False  # Outlier detected

        return True

    # ── Index Management ───────────────────────────────────────────

    async def _ensure_loaded(self, agent_id: str) -> None:
        """Lazy-load impressions for an agent from backend.

        Uses _loaded_agents as both positive and negative cache:
        once checked (hit or miss), won't re-query during this session.

        IMPORTANT: Uses try/finally to ensure _loaded_agents is set even
        when the task is cancelled (e.g., by asyncio.wait_for timeout).
        Without this, every subsequent call would retry the slow backend GET.
        """
        if agent_id in self._loaded_agents:
            return

        try:
            index_path = f"agents/{self._safe(agent_id)}/impressions/index.json"
            index_data = await self._backend.load_json(index_path)

            if index_data:
                for imp_id in index_data.get("impression_ids", []):
                    if imp_id not in self._impressions:
                        path = f"agents/{self._safe(agent_id)}/impressions/{self._safe(imp_id)}.json"
                        data = await self._backend.load_json(path)
                        if data:
                            imp = Impression.from_dict(data)
                            self._impressions[imp_id] = imp
                            self._by_source.setdefault(imp.source_agent, []).append(imp_id)
                            self._by_target.setdefault(imp.target_agent, []).append(imp_id)
        except Exception as e:
            logger.warning("Impression load failed for %s: %s", agent_id, e)
        finally:
            # Mark as loaded (even on failure/cancel) to prevent repeated slow queries.
            # CancelledError bypasses `except Exception` in Python 3.9+,
            # so `finally` is required to ensure this always runs.
            self._loaded_agents.add(agent_id)

    async def _save_index(self, agent_id: str) -> None:
        """Persist the impression index for an agent."""
        imp_ids = self._by_source.get(agent_id, [])
        index_path = f"agents/{self._safe(agent_id)}/impressions/index.json"
        await self._backend.store_json(index_path, {
            "agent_id": agent_id,
            "impression_ids": imp_ids,
            "count": len(imp_ids),
            "updated_at": time.time(),
        })
