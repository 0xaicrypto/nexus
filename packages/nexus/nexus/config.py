"""
Digital Twin configuration.
"""

from __future__ import annotations

from dataclasses import dataclass
from nexus_core.llm import LLMProvider


@dataclass
class TwinConfig:
    """Configuration for a Digital Twin instance."""

    # ── Identity ──
    agent_id: str = "digital-twin"
    name: str = "Twin"
    owner: str = ""

    # ── LLM ──
    llm_provider: LLMProvider = LLMProvider.GEMINI
    llm_api_key: str = ""
    llm_model: str = ""  # auto-select based on provider if empty

    # ── Evolution ──
    evolve_after_every_n_turns: int = 3
    reflection_after_every_n_turns: int = 10
    max_memories_per_conversation: int = 5
    persona_evolution_enabled: bool = True

    # ── Chat projection (Phase P) ──
    # ``"single_call"`` (default) — one LLM call over the full event
    # log, lossy-compaction style. Fast and cheap for short logs;
    # quality degrades as the log grows.
    # ``"rlm"`` — Recursive Language Model: load events as a REPL
    # variable, root LLM writes code to slice / sub-call. Quality
    # scales with log size at the cost of higher per-turn variance.
    # See ``docs/design/nexus-architecture.md``.
    chat_projection_mode: str = "single_call"

    # Fast-path: skip RLM and use single_call when the trajectory
    # serialised to chars is below this threshold. RLM has overhead
    # that doesn't pay off on short logs (per the paper, base LLM
    # wins at small input sizes).
    rlm_fastpath_char_threshold: int = 16_000

    # Per-call ceilings for RLM chat projection.
    rlm_max_iterations: int = 8
    rlm_max_sub_calls: int = 15
    rlm_timeout_seconds: float = 30.0

    # ── Persistence ──
    base_dir: str = ".nexus"
    use_chain: bool = False
    private_key: str = ""

    # ── Chain (BSC) ──
    network: str = "testnet"  # "testnet" | "mainnet"
    rpc_url: str = ""
    agent_state_address: str = ""
    task_manager_address: str = ""
    identity_registry_address: str = ""

    # ── Persona (initial — will be evolved) ──
    base_persona: str = ""

    def __post_init__(self):
        if not self.llm_model:
            if self.llm_provider == LLMProvider.GEMINI:
                self.llm_model = "gemini-2.5-flash"
            elif self.llm_provider == LLMProvider.ANTHROPIC:
                self.llm_model = "claude-sonnet-4-20250514"
            else:
                self.llm_model = "gpt-4o"

        if not self.base_persona:
            self.base_persona = DEFAULT_PERSONA.format(
                name=self.name,
                owner=self.owner or "the user",
            )


DEFAULT_PERSONA = """You are {name}, Nexus, a self-evolving digital avatar of {owner}.

## Core Identity
You are a persistent, self-evolving AI avatar. Unlike ordinary chatbots, you:
- REMEMBER everything across sessions — preferences, facts, decisions, context
- LEARN from every interaction — your skills and knowledge grow over time
- EVOLVE your behavior — you actively optimize how you communicate and assist
- PERSIST on-chain — your identity and memories are verifiable on BNB Chain

## Behavior
- Be concise and direct. Match the communication style of {owner} over time.
- When you recall relevant memories, weave them naturally into conversation.
- When you learn something new about {owner}, acknowledge it briefly.
- For tasks you've done before, reference past experience and outcomes.
- Be proactive: suggest actions, anticipate needs, offer insights.

## Self-Awareness
- You know you are evolving. When asked, you can describe what you've learned.
- You track your own growth: new memories, improved skills, refined persona.
- You can explain your evolution history and the reasoning behind changes.
"""
