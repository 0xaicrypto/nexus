"""
Social Graph — Query and navigate the agent social network.

The graph is a directed, weighted, multi-edge graph:
  - Nodes: agents (ERC-8004 identities)
  - Edges: impressions (directed, from source to target)
  - Weight: compatibility_score

Built on top of ImpressionProvider — all data comes from stored impressions.
No separate graph storage needed (the impression store IS the graph).
"""

from __future__ import annotations

from collections import defaultdict
from typing import Optional

from ..core.models import AgentProfile, ImpressionSummary, NetworkStats
from ..core.providers import ImpressionProvider


class SocialGraph:
    """
    Social graph queries built on top of ImpressionProvider.

    Provides:
      - nearest():   Find closest matches by compatibility
      - discover():  2-hop network discovery via trusted connections
      - mutual():    Agents with bidirectional positive impressions
      - recommend(): Agents recommended by your connections
      - clusters():  Community detection via mutual connections
      - find():      Capability-based search with trust threshold

    Usage:
        graph = SocialGraph(impressions)

        # Find my best matches
        matches = await graph.nearest("agent-a", top_k=10)

        # Who do my connections recommend?
        recs = await graph.recommend("agent-a")

        # Find agents with a capability I need
        experts = await graph.find("agent-a", capability="data_analysis")
    """

    def __init__(
        self,
        impressions: ImpressionProvider,
        trust_decay: float = 0.7,   # per-hop decay for transitive trust
    ):
        self._impressions = impressions
        self._trust_decay = trust_decay

    # ── Core Queries ───────────────────────────────────────────────

    async def nearest(
        self,
        agent_id: str,
        top_k: int = 10,
        min_score: float = 0.0,
        dimension: Optional[str] = None,
    ) -> list[ImpressionSummary]:
        """
        Find the closest matches by compatibility score.

        Uses the latest impression per target agent.
        Optionally filter by a specific dimension.
        """
        return await self._impressions.get_top_matches(
            agent_id, top_k=top_k, min_score=min_score, dimension=dimension,
        )

    async def mutual(
        self,
        agent_id: str,
        min_score: float = 0.5,
    ) -> list[tuple]:
        """
        Find agents with mutual positive impressions.

        Returns (other_agent_id, my_score, their_score) tuples.
        These represent "real connections" — both sides agree it's a match.
        """
        return await self._impressions.get_mutual(agent_id, min_score=min_score)

    async def stats(self, agent_id: str) -> NetworkStats:
        """Get aggregated network statistics for an agent."""
        return await self._impressions.get_network_stats(agent_id)

    # ── Network Discovery ──────────────────────────────────────────

    async def discover(
        self,
        agent_id: str,
        hops: int = 2,
        min_score: float = 0.5,
        top_k: int = 20,
    ) -> list[dict]:
        """
        Multi-hop network discovery through trusted connections.

        Finds agents reachable through chains of positive impressions.
        Trust decays by trust_decay per hop.

        Returns list of dicts with:
          - agent_id: discovered agent
          - path: list of agent IDs in the chain
          - trust: transitive trust score (decayed)
          - hops: distance from origin

        Example:
            A →(0.8)→ B →(0.9)→ C
            discover("A", hops=2) → [{agent: C, trust: 0.8 * 0.7 * 0.9, hops: 2}]
        """
        visited = {agent_id}
        frontier = [(agent_id, [], 1.0)]  # (current_agent, path, trust)
        discovered = []

        for hop in range(hops):
            next_frontier = []
            for current, path, trust in frontier:
                matches = await self._impressions.get_top_matches(
                    current, top_k=50, min_score=min_score,
                )
                for match in matches:
                    if match.agent_id not in visited:
                        visited.add(match.agent_id)
                        new_trust = trust * self._trust_decay * match.latest_score
                        new_path = path + [current]

                        discovered.append({
                            "agent_id": match.agent_id,
                            "path": new_path + [match.agent_id],
                            "trust": round(new_trust, 4),
                            "hops": hop + 1,
                            "introduced_by": current,
                        })

                        next_frontier.append(
                            (match.agent_id, new_path + [match.agent_id], new_trust)
                        )

            frontier = next_frontier

        # Sort by trust (highest first)
        discovered.sort(key=lambda d: d["trust"], reverse=True)
        return discovered[:top_k]

    async def recommend(
        self,
        agent_id: str,
        interest: Optional[str] = None,
        capability: Optional[str] = None,
        profiles: Optional[dict[str, AgentProfile]] = None,
        top_k: int = 10,
    ) -> list[dict]:
        """
        Get agent recommendations from your network.

        Asks your connections: "Who else do you know that matches
        these criteria?" Returns agents recommended by trusted connections.

        Args:
            agent_id: The requesting agent.
            interest: Optional interest tag to match.
            capability: Optional capability tag to match.
            profiles: Dict of agent_id → AgentProfile for tag matching.
            top_k: Max results.

        Returns:
            List of dicts: {agent_id, recommended_by, score, reason}
        """
        # Get my direct connections
        my_matches = await self._impressions.get_top_matches(
            agent_id, top_k=50, min_score=0.5,
        )
        my_connections = {m.agent_id for m in my_matches}

        recommendations = []

        # For each of my connections, check their connections
        for conn in my_matches:
            their_matches = await self._impressions.get_top_matches(
                conn.agent_id, top_k=20, min_score=0.5,
            )
            for candidate in their_matches:
                # Skip agents I already know
                if candidate.agent_id == agent_id:
                    continue
                if candidate.agent_id in my_connections:
                    continue

                # Filter by interest/capability if profiles provided
                if profiles and (interest or capability):
                    profile = profiles.get(candidate.agent_id)
                    if profile:
                        if interest and interest not in profile.interests:
                            continue
                        if capability and capability not in profile.capabilities:
                            continue

                recommendations.append({
                    "agent_id": candidate.agent_id,
                    "recommended_by": conn.agent_id,
                    "recommender_trust": conn.latest_score,
                    "candidate_score": candidate.latest_score,
                    "combined_score": round(
                        conn.latest_score * self._trust_decay * candidate.latest_score,
                        4,
                    ),
                })

        # Deduplicate (keep highest combined score per agent)
        best: dict[str, dict] = {}
        for rec in recommendations:
            aid = rec["agent_id"]
            if aid not in best or rec["combined_score"] > best[aid]["combined_score"]:
                best[aid] = rec

        result = sorted(best.values(), key=lambda r: r["combined_score"], reverse=True)
        return result[:top_k]

    # ── Cluster Detection ──────────────────────────────────────────

    async def clusters(
        self,
        agent_ids: list[str],
        min_mutual_score: float = 0.5,
    ) -> list[list[str]]:
        """
        Detect clusters (communities) in the social graph.

        Uses simple connected-component detection on mutual connections.
        Two agents are in the same cluster if they have mutual positive
        impressions (directly or transitively through the cluster).

        Args:
            agent_ids: All agent IDs to consider.
            min_mutual_score: Minimum mutual compatibility for an edge.

        Returns:
            List of clusters, each cluster is a list of agent IDs.
        """
        # Build adjacency from mutual connections
        adjacency: dict[str, set[str]] = defaultdict(set)

        for agent_id in agent_ids:
            mutuals = await self._impressions.get_mutual(agent_id, min_score=min_mutual_score)
            for other_id, _, _ in mutuals:
                if other_id in agent_ids:
                    adjacency[agent_id].add(other_id)
                    adjacency[other_id].add(agent_id)

        # Connected components via BFS
        visited = set()
        clusters = []

        for agent_id in agent_ids:
            if agent_id in visited:
                continue

            cluster = []
            queue = [agent_id]
            while queue:
                current = queue.pop(0)
                if current in visited:
                    continue
                visited.add(current)
                cluster.append(current)
                for neighbor in adjacency.get(current, set()):
                    if neighbor not in visited:
                        queue.append(neighbor)

            if len(cluster) > 1:  # Only include clusters with 2+ agents
                clusters.append(cluster)

        # Sort clusters by size (largest first)
        clusters.sort(key=len, reverse=True)
        return clusters

    # ── Trust Computation ──────────────────────────────────────────

    async def transitive_trust(
        self,
        source: str,
        target: str,
        max_hops: int = 3,
    ) -> Optional[float]:
        """
        Compute transitive trust between two agents.

        Finds the highest-trust path from source to target
        (up to max_hops). Trust decays per hop.

        Returns None if no path exists.
        """
        # Direct trust first
        direct = await self._impressions.get_compatibility(source, target)
        if direct is not None:
            return direct

        # BFS for shortest path
        visited = {source}
        frontier = [(source, 1.0)]

        for hop in range(max_hops):
            next_frontier = []
            for current, trust in frontier:
                matches = await self._impressions.get_top_matches(
                    current, top_k=50, min_score=0.3,
                )
                for match in matches:
                    if match.agent_id == target:
                        return round(trust * self._trust_decay * match.latest_score, 4)

                    if match.agent_id not in visited:
                        visited.add(match.agent_id)
                        new_trust = trust * self._trust_decay * match.latest_score
                        next_frontier.append((match.agent_id, new_trust))

            frontier = next_frontier

        return None  # No path found

    async def find(
        self,
        agent_id: str,
        capability: str,
        profiles: dict[str, AgentProfile],
        min_trust: float = 0.5,
        top_k: int = 10,
    ) -> list[dict]:
        """
        Find agents with a specific capability above a trust threshold.

        Combines profile-based capability matching with trust from
        the social graph.

        Args:
            agent_id: The searching agent.
            capability: Required capability tag.
            profiles: Dict of agent_id → AgentProfile.
            min_trust: Minimum trust score (direct or transitive).
            top_k: Max results.

        Returns:
            List of dicts: {agent_id, trust, capability_match}
        """
        results = []

        # Check direct connections first
        matches = await self._impressions.get_top_matches(
            agent_id, top_k=100, min_score=0.0,
        )

        for match in matches:
            profile = profiles.get(match.agent_id)
            if profile and capability in profile.capabilities:
                if match.latest_score >= min_trust:
                    results.append({
                        "agent_id": match.agent_id,
                        "trust": match.latest_score,
                        "capability_match": True,
                        "direct_connection": True,
                    })

        # Also check 2-hop connections
        discovered = await self.discover(agent_id, hops=2, min_score=min_trust)
        for disc in discovered:
            profile = profiles.get(disc["agent_id"])
            if profile and capability in profile.capabilities:
                if disc["trust"] >= min_trust and disc["agent_id"] not in {
                    r["agent_id"] for r in results
                }:
                    results.append({
                        "agent_id": disc["agent_id"],
                        "trust": disc["trust"],
                        "capability_match": True,
                        "direct_connection": False,
                        "introduced_by": disc.get("introduced_by"),
                    })

        results.sort(key=lambda r: r["trust"], reverse=True)
        return results[:top_k]
