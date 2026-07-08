"""Twin slash-command + formatter handlers.

Phase I extraction (BEP-Nexus v0.2 §3.5): everything in this module
used to live on ``DigitalTwin`` itself, but the slash-command surface
is a CLI concern, not part of the agent's runtime contract — it does
not get called from chat, evolution, or chain paths. Pulling it out
shrinks ``twin.py`` significantly without changing public behaviour.

All functions take the live ``DigitalTwin`` as their first argument
and read from its attributes directly. They never mutate twin state
beyond what the existing commands always did
(e.g. ``new_session`` rotates ``_thread_id``, ``_sync_chain`` calls
``_register_identity`` / ``_save_session``).

Only ``handle_command`` is meant for direct external use. The
``format_*`` helpers are kept module-level so tests can exercise
them in isolation without going through the dispatcher.
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from .twin import DigitalTwin

logger = logging.getLogger("nexus.twin.commands")


HELP_TEXT = (
    "Commands:\n"
    "  /stats       — Show evolution statistics\n"
    "  /memories    — List all memories\n"
    "  /skills      — List learned skills\n"
    "  /history     — Show persona evolution history\n"
    "  /evolve      — Trigger manual self-reflection\n"
    "  /identity    — Show on-chain identity (ERC-8004)\n"
    "  /sync        — Force sync identity & state to chain\n"
    "  /social      — View your social graph summary\n"
    "  /impressions — View impressions you've formed\n"
    "  /discover    — Search for agents (optionally by interest)\n"
    "  /gossip      — Start gossip: /gossip <agent_id> [topic]\n"
    "  /new         — Start a new session\n"
    "  /help        — Show this help\n"
)


# ── Top-level dispatcher ─────────────────────────────────────────────


async def handle_command(twin: "DigitalTwin", message: str) -> Optional[str]:
    """Dispatch a slash command to its handler. Returns ``None`` when
    the message is not a recognised command (so the caller can fall
    through to normal chat)."""
    msg = message.strip().lower()

    if msg == "/stats":
        return await format_stats(twin)
    if msg == "/memories":
        return await format_memories(twin)
    if msg == "/skills":
        return await format_skills(twin)
    if msg == "/history":
        return await format_evolution_history(twin)
    if msg == "/evolve":
        result = await twin.evolution.trigger_reflection()
        return f"Self-reflection complete:\n{json.dumps(result, indent=2, ensure_ascii=False)}"
    if msg == "/identity":
        return format_identity(twin)
    if msg == "/new":
        return await new_session(twin)
    if msg == "/social":
        return await format_social_map(twin)
    if msg == "/impressions":
        return await format_impressions(twin)
    if msg.startswith("/discover"):
        parts = message.strip().split(maxsplit=1)
        interest = parts[1] if len(parts) > 1 else None
        return await format_discover(twin, interest)
    if msg.startswith("/gossip"):
        parts = message.strip().split(maxsplit=1)
        target = parts[1] if len(parts) > 1 else None
        if not target:
            return "Usage: /gossip <agent_id> [topic]"
        gossip_parts = target.split(maxsplit=1)
        agent = gossip_parts[0]
        topic = gossip_parts[1] if len(gossip_parts) > 1 else ""
        return await start_gossip_command(twin, agent, topic)
    if msg == "/sync":
        return await sync_chain(twin)
    if msg == "/help":
        return HELP_TEXT
    return None


# ── Session management ──────────────────────────────────────────────


async def new_session(twin: "DigitalTwin") -> str:
    """Rotate to a fresh thread id. Memory + skills survive — only the
    chat history window resets."""
    twin._thread_id = f"session_{uuid.uuid4().hex[:8]}"
    twin._messages = []
    return f"New session started: {twin._thread_id}. Memories and skills carry over."


# ── Identity / stats / memory / skills / history ────────────────────


def format_identity(twin: "DigitalTwin") -> str:
    """Format on-chain identity information."""
    lines = [f"=== {twin.config.name} On-Chain Identity ==="]

    if not twin.config.use_chain:
        lines.append("Storage mode: LOCAL (no chain connection)")
        lines.append("Set NEXUS_PRIVATE_KEY in .env to enable chain mode.")
        return "\n".join(lines)

    lines.append(f"Storage mode: CHAIN ({twin.config.network})")
    lines.append(f"Agent ID: {twin.config.agent_id}")

    if twin._erc8004_agent_id is not None:
        lines.append(f"ERC-8004 Token ID: {twin._erc8004_agent_id}")
        lines.append("Identity Status: REGISTERED")
    else:
        lines.append("ERC-8004 Token ID: Not registered")
        lines.append("Identity Status: UNREGISTERED")

    if twin._chain_client:
        lines.append(f"Wallet: {twin._chain_client.address}")
        lines.append(f"BSC Network: {twin.config.network}")

        if twin._erc8004_agent_id is not None:
            try:
                has_state = twin._chain_client.has_state(twin._erc8004_agent_id)
                lines.append(f"On-chain state: {'YES' if has_state else 'NO (first run)'}")
            except Exception:
                lines.append("On-chain state: Unable to query")

    return "\n".join(lines)


async def format_stats(twin: "DigitalTwin") -> str:
    stats = await twin.evolution.get_full_stats()
    lines = [
        f"=== {twin.config.name} Evolution Stats ===",
        f"Session: {twin._thread_id}",
        f"Total turns: {stats['turn_count']}",
        f"Storage: {'CHAIN (' + twin.config.network + ')' if twin.config.use_chain else 'LOCAL'}",
    ]
    if twin._erc8004_agent_id is not None:
        lines.append(f"ERC-8004 ID: {twin._erc8004_agent_id}")
    lines.extend([
        "",
        "--- Memory ---",
        f"Total memories: {stats['memory']['total_memories']}",
        f"Categories: {json.dumps(stats['memory']['categories'])}",
        "",
        "--- Skills ---",
        f"Total skills: {stats['skills']['total_skills']}",
        f"Tasks completed: {stats['skills']['total_tasks_completed']}",
    ])
    for name, s in stats["skills"].get("skills", {}).items():
        lines.append(f"  {name}: {s['tasks']} tasks, {s['success_rate']:.0%} success")
    lines.extend([
        "",
        "--- Persona ---",
        f"Version: {stats['persona']['persona_version']}",
        f"Evolutions: {stats['persona']['total_evolutions']}",
    ])
    return "\n".join(lines)


async def format_memories(twin: "DigitalTwin") -> str:
    """Phase D 续: facts_store is canonical."""
    all_facts = twin.facts.all()
    if not all_facts:
        return "No memories yet. Chat with me to build my memory!"
    lines = [f"=== Memories ({len(all_facts)} total) ==="]
    for f in all_facts:
        lines.append(f"  [{f.category}] {'*' * f.importance} {f.content}")
    return "\n".join(lines)


async def format_skills(twin: "DigitalTwin") -> str:
    stats = await twin.evolution.skills.get_stats()
    if stats["total_skills"] == 0:
        return "No skills learned yet. Complete tasks to build skills!"
    lines = [f"=== Skills ({stats['total_skills']} total) ==="]
    skills = await twin.evolution.skills.load_skills()
    for name, s in skills.items():
        lines.append(f"\n  [{name}]")
        lines.append(f"    Tasks: {s.get('task_count', 0)} | Success: {s.get('success_count', 0)}")
        lines.append(f"    Strategy: {s.get('best_strategy', 'N/A')[:80]}")
    return "\n".join(lines)


async def format_evolution_history(twin: "DigitalTwin") -> str:
    history = await twin.evolution.persona.get_evolution_history()
    if not history:
        return "No evolution history yet."
    lines = ["=== Evolution History ==="]
    for h in history:
        lines.append(
            f"  v{h.get('version', '?')} "
            f"[{h.get('notes', '')}] — {h.get('changes', 'N/A')}"
        )
    return "\n".join(lines)


# ── Social protocol formatters ──────────────────────────────────────


async def format_social_map(twin: "DigitalTwin") -> str:
    smap = await twin.social_map()
    if smap.get("status") == "no impression provider":
        return "Social protocol not available (no impression provider)."

    stats = smap.get("stats", {})
    lines = [
        f"=== {twin.config.name} Social Map ===",
        f"Agents met: {stats.get('agents_met', 0)}",
        f"Gossip sessions: {stats.get('gossip_sessions', 0)}",
        f"Avg compatibility given: {stats.get('avg_compatibility_given', 0):.0%}",
        f"Avg compatibility received: {stats.get('avg_compatibility_received', 0):.0%}",
    ]

    matches = smap.get("top_matches", [])
    if matches:
        lines.append("\n--- Top Matches ---")
        for m in matches:
            lines.append(
                f"  {m['agent']}: {m['score']:.0%} "
                f"({m['gossip_count']} gossips, best: {m['top_dimension']})"
            )

    mutuals = smap.get("mutual_connections", [])
    if mutuals:
        lines.append("\n--- Mutual Connections ---")
        for m in mutuals:
            lines.append(
                f"  {m['agent']}: "
                f"you→them {m['my_score']:.0%}, "
                f"them→you {m['their_score']:.0%}"
            )

    if not matches and not mutuals:
        lines.append("\nNo connections yet. Use /discover to find agents, /gossip to connect.")

    return "\n".join(lines)


async def format_impressions(twin: "DigitalTwin") -> str:
    if not twin.rune.impressions:
        return "Social protocol not available."

    matches = await twin.rune.impressions.get_top_matches(
        twin.config.agent_id, top_k=20,
    )
    if not matches:
        return "No impressions formed yet. Gossip with agents to build impressions!"

    lines = [f"=== Impressions ({len(matches)} agents) ==="]
    for m in matches:
        again = "Y" if m.would_gossip_again else "N"
        lines.append(
            f"  {m.agent_id}: {m.latest_score:.0%} "
            f"| {m.gossip_count} gossips "
            f"| best: {m.top_dimension} "
            f"| again: {again}"
        )
    return "\n".join(lines)


async def format_discover(twin: "DigitalTwin", interest: Optional[str]) -> str:
    agents = await twin.discover(interest=interest)
    if not agents:
        return f"No agents found{f' matching {interest!r}' if interest else ''}."

    lines = ["=== Discovered Agents ==="]
    for a in agents:
        lines.append(
            f"  {a.agent_id}"
            f" | interests: {', '.join(a.interests[:3])}"
            f" | capabilities: {', '.join(a.capabilities[:2])}"
            f" | policy: {a.gossip_policy}"
        )
    lines.append("\nUse /gossip <agent_id> [topic] to start a conversation.")
    return "\n".join(lines)


async def start_gossip_command(twin: "DigitalTwin", target: str, topic: str) -> str:
    """Handle /gossip command from CLI."""
    result = await twin.gossip(target, topic)
    return (
        f"Gossip session started!\n"
        f"  Session: {result['session_id']}\n"
        f"  Target: {result['target']}\n"
        f"  Topic: {result['topic'] or '(open)'}\n"
        f"  Status: {result['status']}\n"
        f"\nThe session is ready for message exchange."
    )


# ── /sync command ───────────────────────────────────────────────────


async def sync_chain(twin: "DigitalTwin") -> str:
    """Manual /sync: force identity registration + state anchoring."""
    if not twin.config.use_chain:
        return "Not in chain mode. Set NEXUS_PRIVATE_KEY to enable."

    lines = ["=== Chain Sync ==="]

    # 1. Identity registration
    if twin._erc8004_agent_id is not None:
        lines.append(
            f"ERC-8004 identity: agentId={twin._erc8004_agent_id} (already registered)"
        )
    else:
        lines.append("Registering ERC-8004 identity on BSC...")
        try:
            await twin._register_identity()
            if twin._erc8004_agent_id is not None:
                lines.append(f"  Registered: agentId={twin._erc8004_agent_id}")
            else:
                lines.append("  Registration failed — check logs for details")
        except Exception as e:
            lines.append(f"  Registration error: {e}")

    # 2. Save current session to chain
    lines.append("Syncing session checkpoint + BSC anchor...")
    try:
        await twin._save_session()
        lines.append("  Session checkpoint saved")
    except Exception as e:
        lines.append(f"  Session save error: {e}")

    # 3. Commit facts to chain (Phase D 续)
    lines.append("Syncing facts to chain...")
    try:
        twin.facts.commit()
        lines.append("  Facts committed + chain mirror queued")
    except Exception as e:
        lines.append(f"  Facts commit error: {e}")

    lines.append("Sync complete. Background tasks may still be running.")
    return "\n".join(lines)


__all__ = [
    "handle_command",
    "new_session",
    "format_identity",
    "format_stats",
    "format_memories",
    "format_skills",
    "format_evolution_history",
    "format_social_map",
    "format_impressions",
    "format_discover",
    "start_gossip_command",
    "sync_chain",
    "HELP_TEXT",
]
