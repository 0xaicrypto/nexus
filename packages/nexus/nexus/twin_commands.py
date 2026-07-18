"""Twin slash-command + formatter handlers.

All functions take the live ``DigitalTwin`` as their first argument
and read from its attributes directly.
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
    "  /new         — Start a new session\n"
    "  /help        — Show this help\n"
)


# ── Top-level dispatcher ─────────────────────────────────────────────


async def handle_command(twin: "DigitalTwin", message: str) -> Optional[str]:
    """Dispatch a slash command to its handler."""
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
    if msg == "/new":
        return await new_session(twin)
    if msg == "/help":
        return HELP_TEXT
    return None


# ── Session management ──────────────────────────────────────────────


async def new_session(twin: "DigitalTwin") -> str:
    twin._thread_id = f"session_{uuid.uuid4().hex[:8]}"
    twin._messages = []
    return f"New session started: {twin._thread_id}. Memories and skills carry over."


# ── Stats / memories / skills / history ─────────────────────────────


async def format_stats(twin: "DigitalTwin") -> str:
    stats = await twin.evolution.get_full_stats()
    lines = [
        f"=== {twin.config.name} Evolution Stats ===",
        f"Session: {twin._thread_id}",
        f"Total turns: {stats['turn_count']}",
        f"Storage: LOCAL",
    ]
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


__all__ = [
    "handle_command",
    "new_session",
    "format_stats",
    "format_memories",
    "format_skills",
    "format_evolution_history",
    "HELP_TEXT",
]
