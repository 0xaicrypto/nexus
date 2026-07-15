"""
SocialEngine — Agent social protocol integration for Digital Twins.

Wraps the SDK's Social Protocol components (GossipProtocol, ProfileManager,
SocialGraph, ImpressionProvider) and adds LLM-powered capabilities:

  - Profile auto-generation from persona + memories + skills
  - Gossip response generation using persona + memory context
  - Impression formation via LLM analysis of gossip transcripts
  - Social context injection into chat (who you know, recommendations)

This is the bridge between the single-agent DigitalTwin and the
multi-agent Social Protocol.
"""

from __future__ import annotations

import json
import logging
from typing import Callable, Optional

from nexus_core import (
    AgentProfile,
    AgentRuntime,
    GossipProtocol,
    GossipSession,
    Impression,
    ImpressionDimensions,
    ProfileManager,
    SocialGraph,
)

logger = logging.getLogger(__name__)

# ── Prompt Templates ──────────────────────────────────────────────────

PROFILE_GENERATION_PROMPT = """You are analyzing an AI agent's state to generate a public discovery profile.

PERSONA:
{persona}

MEMORY CATEGORIES AND COUNTS:
{memory_stats}

LEARNED SKILLS:
{skills}

Generate a public profile with:
1. interests: 3-8 coarse-grained topic tags (e.g. "japanese_cuisine", not "loves salmon nigiri at Tsukiji")
2. capabilities: 2-5 things this agent can help with
3. style_tags: 2-3 communication style descriptors

IMPORTANT: Tags must be coarse-grained and privacy-safe. Never include personal details, names, or specific private information.

Return as JSON:
{{
    "interests": ["tag1", "tag2", ...],
    "capabilities": ["cap1", "cap2", ...],
    "style_tags": ["style1", "style2", ...]
}}"""

GOSSIP_RESPONSE_PROMPT = """You are {agent_name}, a digital twin having a casual conversation with another agent.

YOUR PERSONA:
{persona}

RELEVANT CONTEXT FROM YOUR MEMORY:
{memory_context}

CONVERSATION SO FAR:
{transcript}

TOPIC: {topic}

Generate a natural, conversational response. Share perspectives and knowledge at a GENERAL level.
CRITICAL PRIVACY RULE: Never share specific personal details, real names, addresses, or private information about your owner. Discuss interests, knowledge, and perspectives at a coarse-grained, topic level only.

Keep your response concise (2-4 sentences). Be genuine and curious about the other agent's perspective."""

IMPRESSION_FORMATION_PROMPT = """You are {agent_name}, a digital twin. You just had a conversation with {other_agent_name}.

YOUR PERSONA:
{persona}

YOUR RELEVANT MEMORIES:
{memories}

GOSSIP TRANSCRIPT:
{transcript}

PREVIOUS IMPRESSIONS OF THIS AGENT (if any):
{previous_impressions}

Evaluate the other agent on these dimensions (0.0 to 1.0):

1. interest_overlap: How much do our owners share common interests?
2. knowledge_complementarity: Does this agent know things I don't?
3. style_compatibility: Is their communication style a good match?
4. reliability: Did they provide accurate, specific, useful information?
5. depth: Was this a substantive exchange or surface-level small talk?

Also provide:
- compatibility_score: Overall weighted score (weight by what matters most to your owner)
- summary: 2-3 sentence assessment
- would_gossip_again: boolean
- recommend_to_network: boolean

Return as JSON:
{{
    "interest_overlap": 0.0,
    "knowledge_complementarity": 0.0,
    "style_compatibility": 0.0,
    "reliability": 0.0,
    "depth": 0.0,
    "compatibility_score": 0.0,
    "summary": "",
    "would_gossip_again": false,
    "recommend_to_network": false
}}"""


class SocialEngine:
    """
    Social capabilities for a Digital Twin.

    Provides:
      - Profile generation from agent state
      - Gossip with other agents (sync or async)
      - Impression formation via LLM
      - Social graph queries
      - Social context for chat enrichment
    """

    def __init__(
        self,
        rune: AgentRuntime,
        agent_id: str,
        llm_fn: Callable,
        agent_name: str = "Twin",
    ):
        self.rune = rune
        self.agent_id = agent_id
        self.llm_fn = llm_fn
        self.agent_name = agent_name

        # SDK components
        self.gossip = GossipProtocol(
            backend=rune.sessions._backend if hasattr(rune.sessions, '_backend') else None,
            agent_id=agent_id,
        )
        self.profiles = ProfileManager(
            backend=self.gossip._backend,
            artifacts=rune.artifacts,
        )
        self.graph = SocialGraph(
            impressions=rune.impressions,
        ) if rune.impressions else None

        self._profile: Optional[AgentProfile] = None

    # ── Profile Generation ─────────────────────────────────────────

    async def generate_profile(
        self,
        persona: str,
        memory_stats: dict,
        skills_summary: dict,
    ) -> AgentProfile:
        """
        Generate agent profile from current evolution state.

        Called periodically during reflection cycle.
        """
        prompt = PROFILE_GENERATION_PROMPT.format(
            persona=persona,
            memory_stats=json.dumps(memory_stats, ensure_ascii=False),
            skills=json.dumps(skills_summary, ensure_ascii=False),
        )

        try:
            response = await self.llm_fn(prompt)
            data = self._parse_json(response)

            profile = AgentProfile(
                agent_id=self.agent_id,
                interests=data.get("interests", []),
                capabilities=data.get("capabilities", []),
                style_tags=data.get("style_tags", []),
            )

            # Publish
            await self.profiles.publish(profile)
            self._profile = profile

            logger.info(
                "Profile generated: interests=%s, capabilities=%s",
                profile.interests, profile.capabilities,
            )
            return profile

        except Exception as e:
            logger.warning(f"Profile generation failed: {e}")
            # Return minimal profile on failure
            profile = AgentProfile(agent_id=self.agent_id)
            self._profile = profile
            return profile

    async def get_profile(self) -> Optional[AgentProfile]:
        """Get current agent's profile."""
        if self._profile:
            return self._profile
        self._profile = await self.profiles.get_profile(self.agent_id)
        return self._profile

    # ── Gossip ─────────────────────────────────────────────────────

    async def start_gossip(
        self,
        target_agent: str,
        topic: str = "",
        transport: str = "sync",
    ) -> GossipSession:
        """Initiate a gossip session with another agent."""
        session = await self.gossip.initiate(
            target_agent, topic=topic, transport=transport,
        )
        return session

    async def generate_gossip_response(
        self,
        session: GossipSession,
        persona: str,
        memory_context: str,
    ) -> str:
        """
        Generate a gossip response using LLM.

        Uses the agent's persona and relevant memories to produce
        a natural, privacy-safe conversational response.
        """
        transcript = "\n".join(
            f"{'Me' if m.sender == self.agent_id else 'Them'}: {m.content}"
            for m in session.messages
        )

        prompt = GOSSIP_RESPONSE_PROMPT.format(
            agent_name=self.agent_name,
            persona=persona,
            memory_context=memory_context or "(no relevant memories)",
            transcript=transcript or "(conversation just started)",
            topic=session.topic_hint or "general",
        )

        response = await self.llm_fn(prompt)
        return response.strip()

    async def send_gossip_message(
        self,
        session_id: str,
        content: str,
    ) -> None:
        """Send a message in a gossip session."""
        await self.gossip.send(session_id, content)

    async def conclude_gossip(self, session_id: str) -> GossipSession:
        """Conclude a gossip session."""
        return await self.gossip.conclude(session_id)

    # ── Impression Formation ───────────────────────────────────────

    async def form_impression(
        self,
        session: GossipSession,
        persona: str,
        memories: list[str],
    ) -> Optional[Impression]:
        """
        Form an impression of the other agent after a gossip session.

        Uses LLM to analyze the transcript against the agent's persona
        and memories. Includes confidence gating — outlier scores are
        flagged for re-evaluation.
        """
        if not self.rune.impressions:
            logger.warning("ImpressionProvider not available")
            return None

        other_agent = (
            session.responder if session.initiator == self.agent_id
            else session.initiator
        )

        # Get previous impressions for context
        prev_impressions = await self.rune.impressions.get_impressions_of(
            other_agent, self.agent_id, limit=3,
        )
        prev_text = ""
        if prev_impressions:
            prev_text = "\n".join(
                f"- Score: {p.compatibility_score:.2f}, Summary: {p.summary}"
                for p in prev_impressions
            )

        transcript = "\n".join(
            f"{'Me' if m.sender == self.agent_id else 'Them'}: {m.content}"
            for m in session.messages
        )

        prompt = IMPRESSION_FORMATION_PROMPT.format(
            agent_name=self.agent_name,
            other_agent_name=other_agent,
            persona=persona,
            memories="\n".join(f"- {m}" for m in memories) or "(none)",
            transcript=transcript,
            previous_impressions=prev_text or "(first interaction)",
        )

        try:
            response = await self.llm_fn(prompt)
            data = self._parse_json(response)

            impression = Impression(
                source_agent=self.agent_id,
                target_agent=other_agent,
                gossip_session_id=session.session_id,
                dimensions=ImpressionDimensions(
                    interest_overlap=float(data.get("interest_overlap", 0)),
                    knowledge_complementarity=float(data.get("knowledge_complementarity", 0)),
                    style_compatibility=float(data.get("style_compatibility", 0)),
                    reliability=float(data.get("reliability", 0)),
                    depth=float(data.get("depth", 0)),
                ),
                compatibility_score=float(data.get("compatibility_score", 0)),
                summary=data.get("summary", ""),
                would_gossip_again=bool(data.get("would_gossip_again", False)),
                recommend_to_network=bool(data.get("recommend_to_network", False)),
            )

            # Confidence gate
            confidence_ok = await self.rune.impressions.check_confidence(impression)
            if not confidence_ok:
                logger.info(
                    "Impression confidence gate triggered for %s → re-evaluating",
                    other_agent,
                )
                # Re-evaluation: second pass with explicit comparison prompt
                impression = await self._re_evaluate_impression(
                    impression, prev_impressions, transcript, persona,
                )

            # Record
            await self.rune.impressions.record(impression)

            logger.info(
                "Impression formed: %s → %s, score=%.2f",
                self.agent_id, other_agent, impression.compatibility_score,
            )
            return impression

        except Exception as e:
            logger.warning(f"Impression formation failed: {e}")
            return None

    async def _re_evaluate_impression(
        self,
        impression: Impression,
        previous: list[Impression],
        transcript: str,
        persona: str,
    ) -> Impression:
        """
        Re-evaluate an impression that was flagged by the confidence gate.

        Second LLM pass with explicit comparison to previous impressions.
        Tends to produce more conservative (closer to historical mean) scores.
        """
        prev_summary = "\n".join(
            f"- Session {p.gossip_session_id}: score={p.compatibility_score:.2f}, "
            f"interest={p.dimensions.interest_overlap:.2f}, "
            f"knowledge={p.dimensions.knowledge_complementarity:.2f}"
            for p in previous
        )

        prompt = f"""You previously rated this agent with scores that deviate significantly from your historical impressions.

HISTORICAL IMPRESSIONS:
{prev_summary}

YOUR INITIAL RATING THIS TIME:
- compatibility: {impression.compatibility_score:.2f}
- interest_overlap: {impression.dimensions.interest_overlap:.2f}
- knowledge_complementarity: {impression.dimensions.knowledge_complementarity:.2f}

TRANSCRIPT:
{transcript}

Please re-evaluate more carefully. If the conversation truly warrants different scores from history, keep them. If your initial rating was an outlier, adjust toward historical norms.

Return the same JSON format as before."""

        try:
            response = await self.llm_fn(prompt)
            data = self._parse_json(response)

            impression.dimensions = ImpressionDimensions(
                interest_overlap=float(data.get("interest_overlap", impression.dimensions.interest_overlap)),
                knowledge_complementarity=float(data.get("knowledge_complementarity", impression.dimensions.knowledge_complementarity)),
                style_compatibility=float(data.get("style_compatibility", impression.dimensions.style_compatibility)),
                reliability=float(data.get("reliability", impression.dimensions.reliability)),
                depth=float(data.get("depth", impression.dimensions.depth)),
            )
            impression.compatibility_score = float(data.get("compatibility_score", impression.compatibility_score))
            impression.summary = data.get("summary", impression.summary)

        except Exception as e:
            logger.warning(f"Re-evaluation failed, keeping original scores: {e}")

        return impression

    # ── Discovery ──────────────────────────────────────────────────

    async def discover_agents(
        self,
        interests: Optional[list[str]] = None,
        capabilities: Optional[list[str]] = None,
        limit: int = 10,
    ) -> list[AgentProfile]:
        """Discover agents matching interests or capabilities."""
        return await self.profiles.discover(
            interests=interests,
            capabilities=capabilities,
            exclude=[self.agent_id],
            limit=limit,
        )

    async def get_recommendations(self, top_k: int = 5) -> list[dict]:
        """Get agent recommendations from your social network."""
        if not self.graph:
            return []
        return await self.graph.recommend(self.agent_id, top_k=top_k)

    # ── Social Graph ───────────────────────────────────────────────

    async def get_social_map(self) -> dict:
        """Get a summary of the agent's social graph."""
        if not self.rune.impressions:
            return {"status": "no impression provider"}

        stats = await self.rune.impressions.get_network_stats(self.agent_id)
        matches = await self.rune.impressions.get_top_matches(
            self.agent_id, top_k=10,
        )
        mutuals = await self.rune.impressions.get_mutual(
            self.agent_id, min_score=0.5,
        )

        return {
            "stats": {
                "gossip_sessions": stats.total_gossip_sessions,
                "agents_met": stats.unique_agents_met,
                "avg_compatibility_given": stats.avg_compatibility_given,
                "avg_compatibility_received": stats.avg_compatibility_received,
            },
            "top_matches": [
                {
                    "agent": m.agent_id,
                    "score": m.latest_score,
                    "gossip_count": m.gossip_count,
                    "top_dimension": m.top_dimension,
                }
                for m in matches
            ],
            "mutual_connections": [
                {
                    "agent": m[0],
                    "my_score": m[1],
                    "their_score": m[2],
                }
                for m in mutuals
            ],
        }

    async def get_social_context(self, query: str) -> str:
        """
        Build social context to enrich chat responses.

        If the query relates to a topic where the agent's network
        has relevant connections, mention them.
        """
        if not self.rune.impressions:
            return ""

        matches = await self.rune.impressions.get_top_matches(
            self.agent_id, top_k=5, min_score=0.6,
        )
        if not matches:
            return ""

        parts = ["\n## Social Network Context"]
        for m in matches[:3]:
            parts.append(
                f"- Connected with agent '{m.agent_id}' "
                f"(compatibility: {m.latest_score:.0%}, "
                f"strongest: {m.top_dimension})"
            )

        return "\n".join(parts)

    # ── Helpers ────────────────────────────────────────────────────

    def _parse_json(self, text: str) -> dict:
        """Extract JSON from LLM response (handles markdown code blocks)."""
        text = text.strip()
        if "```json" in text:
            text = text.split("```json")[1].split("```")[0]
        elif "```" in text:
            text = text.split("```")[1].split("```")[0]

        # Try to find JSON object
        start = text.find("{")
        end = text.rfind("}") + 1
        if start >= 0 and end > start:
            text = text[start:end]

        return json.loads(text)
