"""
Rune Social Protocol — Agent-to-Agent Social Infrastructure.

Modules:
    gossip      — Async/sync gossip session management
    profile     — Agent profile generation and discovery
    graph       — Social graph queries and propagation
    impression  — Impression generation helpers (LLM prompts)
"""

from .gossip import GossipProtocol
from .graph import SocialGraph
from .profile import ProfileManager

__all__ = [
    "GossipProtocol",
    "ProfileManager",
    "SocialGraph",
]
