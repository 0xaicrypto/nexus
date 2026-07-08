#!/usr/bin/env python3
"""
Digital Twin CLI — Interactive chat interface.

Usage (simplest — reads everything from .env):
    python -m nexus

Or with explicit args:
    python -m nexus --provider gemini --api-key AIza...

Chain mode (BSC):
    python -m nexus --private-key 0x...
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
import time

# ── ANSI Colors ──────────────────────────────────────────────────
BOLD = "\033[1m"
DIM = "\033[2m"
RESET = "\033[0m"
GOLD = "\033[38;5;220m"
GREEN = "\033[38;5;114m"
BLUE = "\033[38;5;111m"
RED = "\033[38;5;203m"
GRAY = "\033[38;5;245m"
CYAN = "\033[38;5;80m"
WHITE = "\033[38;5;255m"


def _load_dotenv():
    """Load .env file — delegates to SDK shared utility."""
    from nexus_core.utils import load_dotenv
    return load_dotenv()


def _env(key: str, default: str = "") -> str:
    """Get env var value."""
    return os.environ.get(key, default)


def banner(name: str, agent_id: str, provider: str, model: str, chain_mode: bool, network: str):
    storage = f"BNB Chain ({network})" if chain_mode else "Local"
    print(f"""
{GOLD}{BOLD}╔══════════════════════════════════════════════════════════╗
║                    Nexus                             ║
║          Self-Evolving AI Avatar on BNBChain             ║
╚══════════════════════════════════════════════════════════╝{RESET}

  {WHITE}{BOLD}Name:{RESET}     {name}
  {WHITE}{BOLD}Agent ID:{RESET} {agent_id}
  {WHITE}{BOLD}LLM:{RESET}      {provider} / {model}
  {WHITE}{BOLD}Storage:{RESET}  {storage}

  {DIM}Type /help for commands, /identity for on-chain status{RESET}
  {DIM}Type 'quit' or 'exit' to end session{RESET}
""")


def print_response(text: str, name: str):
    """Print twin's response with formatting."""
    print(f"\n{CYAN}{BOLD}{name}:{RESET} {text}\n")


def print_evolution(info: str):
    """Print evolution activity."""
    print(f"  {GOLD}{DIM}[evolution]{RESET} {DIM}{info}{RESET}")


def print_error(text: str):
    print(f"  {RED}Error: {text}{RESET}")


# ── On-Chain Activity Renderer ───────────────────────────────────

# Unicode symbols for on-chain activity
CHAIN_ICON = "\u26d3"   # ⛓  chain link
BRAIN_ICON = "\u2699"   # ⚙  gear (brain/evolution)
MEMORY_ICON = "\u2b50"  # ⭐ star (memory)
SAVE_ICON = "\u2714"    # ✔  checkmark
ROCKET_ICON = "\u2b06"  # ⬆  up arrow (upload)
SHIELD_ICON = "\u2b22"  # ⬢  hexagon (identity)


def on_chain_event(event_type: str, detail: dict):
    """
    Render on-chain activity notifications.

    This is the callback wired into DigitalTwin.on_event.
    Each event type gets a distinct visual treatment so the user
    can SEE their data flowing onto the chain.
    """
    chain_mode = detail.get("storage", "") == "chain"
    storage_tag = f"{GOLD}on-chain{RESET}" if chain_mode else f"{DIM}local{RESET}"

    if event_type == "identity_found":
        agent_id = detail.get("erc8004_id", "?")
        source = detail.get("source", "chain")
        net = detail.get("network", "testnet")
        if source == "cache":
            print(
                f"  {GREEN}{SHIELD_ICON} ERC-8004{RESET} "
                f"Identity loaded from cache: agentId={BOLD}{agent_id}{RESET} ({net})"
            )
        else:
            wallet = detail.get("wallet", "?")
            print(
                f"  {GREEN}{SHIELD_ICON} ERC-8004{RESET} "
                f"Identity found: agentId={BOLD}{agent_id}{RESET} "
                f"wallet={DIM}{wallet[:10]}...{wallet[-6:]}{RESET} "
                f"({net})"
            )

    elif event_type == "identity_check":
        source = detail.get("source", "")
        if source == "background":
            print(f"\r  {GOLD}{SHIELD_ICON} ERC-8004{RESET} Registering identity in background ({detail.get('network', '')})")
        else:
            print(f"\r  {GOLD}{SHIELD_ICON} ERC-8004{RESET} Checking identity on BSC {detail.get('network', '')}...")

    elif event_type == "identity_registered":
        agent_id = detail.get("erc8004_id", "?")
        wallet = detail.get("wallet", "?")
        net = detail.get("network", "testnet")
        print(
            f"  {GREEN}{SHIELD_ICON} ERC-8004{RESET} "
            f"Identity registered! agentId={BOLD}{agent_id}{RESET} "
            f"wallet={DIM}{wallet[:10]}...{wallet[-6:]}{RESET} "
            f"({net})"
        )

    elif event_type == "session_save":
        turn = detail.get("turn", 0)
        msgs = detail.get("messages", 0)
        print(
            f"\r  {BLUE}{SAVE_ICON} Session{RESET} "
            f"Checkpoint #{turn} ({msgs} messages) "
            f"[{storage_tag}]"
        )

    elif event_type == "memory_stored":
        count = detail.get("count", 0)
        items = detail.get("items", [])
        preview = items[0][:40] + "..." if items else ""
        print(
            f"  {CYAN}{MEMORY_ICON} Memory{RESET} "
            f"Extracted {BOLD}{count}{RESET} insight{'s' if count > 1 else ''} "
            f"→ {DIM}{preview}{RESET} [{storage_tag}]"
        )

    elif event_type == "skill_learned":
        skill = detail.get("skill", "unknown")
        lesson = detail.get("lesson", "")[:50]
        print(
            f"  {GREEN}{BRAIN_ICON} Skill{RESET} "
            f"Learned [{BOLD}{skill}{RESET}]: {DIM}{lesson}{RESET} "
            f"[{storage_tag}]"
        )

    elif event_type == "persona_reflect":
        turn = detail.get("turn", 0)
        print(
            f"  {GOLD}{BRAIN_ICON} Persona{RESET} "
            f"Self-reflection triggered at turn {turn}..."
        )

    elif event_type == "persona_evolved":
        version = detail.get("version", "?")
        changes = detail.get("changes", "")[:60]
        confidence = detail.get("confidence", 0)
        print(
            f"  {GOLD}{ROCKET_ICON} Persona{RESET} "
            f"Evolved to v{BOLD}{version}{RESET} "
            f"(confidence: {confidence:.0%}) — {DIM}{changes}{RESET} "
            f"[{storage_tag}]"
        )

    elif event_type == "memory_extract":
        pass  # silent — only show if memories are actually stored

    elif event_type == "sync_error":
        component = detail.get("component", "Chain")
        error = detail.get("error", "unknown error")
        hint = detail.get("hint", "")
        RED = "\033[91m"
        print(
            f"\n  {RED}⚠ {component}{RESET} "
            f"Sync failed: {error}"
        )
        if hint:
            print(f"  {DIM}{hint}{RESET}")

    elif event_type == "shutdown_sync":
        pending = detail.get("pending", 0)
        grace = detail.get("grace_seconds", 15)
        print(
            f"\n  {GOLD}{SAVE_ICON} Syncing{RESET} "
            f"Waiting for {BOLD}{pending}{RESET} background task(s) to finish "
            f"(up to {grace:.0f}s)..."
        )

    elif event_type == "gossip_started":
        target = detail.get("target", "?")
        topic = detail.get("topic", "")
        print(
            f"  {CYAN}{BRAIN_ICON} Gossip{RESET} "
            f"Session started with {BOLD}{target}{RESET}"
            + (f" (topic: {topic})" if topic else "")
        )


async def main_loop(args):
    """Main CLI interaction loop."""
    from .twin import DigitalTwin

    # Setup logging — console + file
    log_level = logging.DEBUG if args.verbose else logging.WARNING
    logging.basicConfig(
        level=log_level,
        format=f"{DIM}%(asctime)s %(name)s %(levelname)s: %(message)s{RESET}",
    )

    # Always write chain interactions to rune_debug.log (DEBUG level)
    # so we can trace I/O issues without --verbose cluttering the console.
    file_handler = logging.FileHandler("rune_debug.log", mode="a", encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(logging.Formatter(
        "%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))
    # Attach ONLY to top-level parent loggers — child loggers propagate up
    # automatically. Attaching to both parent + child caused every line
    # to appear twice in the log file.
    for logger_name in [
        "nexus_core.backend.chain",          # ChainBackend: reads, writes, anchoring
        "nexus_core.providers",     # All SDK providers (memory, session, etc.)
        "nexus.evolution",          # Evolution engine + all evolvers
        "nexus.twin",              # DigitalTwin core
    ]:
        lg = logging.getLogger(logger_name)
        lg.addHandler(file_handler)
        lg.setLevel(logging.DEBUG)  # File gets everything; console still respects log_level

    # Resolve config: CLI args > env vars > defaults
    provider = args.provider or _env("TWIN_LLM_PROVIDER", "gemini")
    model = args.model or _env("TWIN_LLM_MODEL", "")
    name = args.name or _env("TWIN_NAME", "Twin")
    owner = args.owner or _env("TWIN_OWNER", "")
    agent_id = args.agent_id or _env("TWIN_AGENT_ID", "digital-twin")
    data_dir = args.data_dir or _env("TWIN_DATA_DIR", ".nexus")

    # Resolve API key: --api-key > env var for provider
    api_key = args.api_key
    if not api_key:
        env_map = {
            "gemini": "GEMINI_API_KEY",
            "anthropic": "ANTHROPIC_API_KEY",
            "openai": "OPENAI_API_KEY",
        }
        env_var = env_map.get(provider, "GEMINI_API_KEY")
        api_key = _env(env_var, "")

    if not api_key:
        env_var = env_map.get(provider, "GEMINI_API_KEY")
        print_error(
            f"No API key found. Either:\n"
            f"  1. Set {env_var} in .env file\n"
            f"  2. Export {env_var} in shell\n"
            f"  3. Pass --api-key on command line"
        )
        sys.exit(1)

    # ── Chain mode config ──
    private_key = args.private_key or _env("NEXUS_PRIVATE_KEY", "")
    network = args.network or _env("NEXUS_NETWORK", "testnet")
    chain_mode = bool(private_key)

    # Chain addresses (env vars auto-resolved by SDK, but we can pass explicitly)
    net_prefix = "MAINNET" if "mainnet" in network else "TESTNET"
    rpc_url = _env(f"NEXUS_{net_prefix}_RPC", "")
    agent_state_address = _env(f"NEXUS_{net_prefix}_AGENT_STATE_ADDRESS", "")
    task_manager_address = _env(f"NEXUS_{net_prefix}_TASK_MANAGER_ADDRESS", "")
    identity_registry_address = (
        _env(f"NEXUS_{net_prefix}_IDENTITY_REGISTRY", "")
        or _env(f"NEXUS_{net_prefix}_IDENTITY_REGISTRY_ADDRESS", "")
    )
    if chain_mode:
        print(f"\n{GOLD}Initializing Nexus (chain mode: {network})...{RESET}")
        print(f"  {DIM}Loading ERC-8004 identity...{RESET}")
    else:
        print(f"\n{DIM}Initializing Nexus (local mode)...{RESET}")

    # ── Tool config ──
    tavily_key = _env("TAVILY_API_KEY", "")
    jina_key = _env("JINA_API_KEY", "")
    enable_tools = not args.no_tools

    # Create twin
    twin = await DigitalTwin.create(
        name=name,
        owner=owner,
        agent_id=agent_id,
        llm_provider=provider,
        llm_api_key=api_key,
        llm_model=model,
        base_dir=data_dir,
        # Tools
        enable_tools=enable_tools,
        tavily_api_key=tavily_key,
        jina_api_key=jina_key,
        # Chain params
        private_key=private_key,
        network=network,
        rpc_url=rpc_url,
        agent_state_address=agent_state_address,
        task_manager_address=task_manager_address,
        identity_registry_address=identity_registry_address,
    )

    # Wire up on-chain activity notifications
    twin.on_event = on_chain_event

    if chain_mode and twin._erc8004_agent_id is not None:
        print(f"  {GREEN}ERC-8004 identity ready: agentId={twin._erc8004_agent_id}{RESET}")

    if twin.tools:
        print(f"  {GREEN}Tools enabled: {', '.join(twin.tools.tool_names)}{RESET}")

    banner(name, agent_id, provider, twin.config.llm_model, chain_mode, network)

    # Show resumption info
    if twin._messages:
        n = len(twin._messages)
        print(f"  {GREEN}Resumed previous session with {n} messages{RESET}\n")

    try:
        while True:
            try:
                user_input = input(f"{WHITE}{BOLD}You:{RESET} ").strip()
            except (EOFError, KeyboardInterrupt):
                print(f"\n{DIM}Goodbye!{RESET}")
                break

            if not user_input:
                continue

            if user_input.lower() in ("quit", "exit", "bye"):
                print(f"\n{GOLD}Saving state and shutting down...{RESET}")
                break

            # Show thinking indicator
            start = time.time()
            print(f"  {DIM}thinking...{RESET}", end="", flush=True)

            try:
                response = await twin.chat(user_input)
                elapsed = time.time() - start
                # Clear thinking indicator
                print(f"\r  {DIM}({elapsed:.1f}s){RESET}   ")
                print_response(response, name)

            except Exception as e:
                print(f"\r")
                print_error(str(e))
                if args.verbose:
                    import traceback
                    traceback.print_exc()

    finally:
        await twin.close()
        storage_desc = f"on BNB Chain ({network})" if chain_mode else "locally"
        print(f"{GREEN}Nexus state persisted {storage_desc}. See you next time!{RESET}\n")


def cli_main():
    # Load .env BEFORE parsing args (so defaults come from .env)
    env_file = _load_dotenv()

    parser = argparse.ArgumentParser(
        description="Nexus — Self-Evolving AI Avatar",
        epilog="All options can also be set via .env file (see .env.example)",
    )
    parser.add_argument("--name", default="", help="Display name (env: TWIN_NAME)")
    parser.add_argument("--owner", default="", help="Owner name (env: TWIN_OWNER)")
    parser.add_argument("--agent-id", default="", help="Rune agent ID (env: TWIN_AGENT_ID)")
    parser.add_argument(
        "--provider", default="", choices=["gemini", "openai", "anthropic", ""],
        help="LLM provider (env: TWIN_LLM_PROVIDER)",
    )
    parser.add_argument("--api-key", default="", help="LLM API key (env: GEMINI_API_KEY)")
    parser.add_argument("--model", default="", help="LLM model (env: TWIN_LLM_MODEL)")
    parser.add_argument("--data-dir", default="", help="Data directory (env: TWIN_DATA_DIR)")

    # Chain mode options
    parser.add_argument(
        "--private-key", default="",
        help="BSC wallet private key — enables chain mode (env: NEXUS_PRIVATE_KEY)",
    )
    parser.add_argument(
        "--network", default="", choices=["testnet", "mainnet", ""],
        help="BSC network (env: NEXUS_NETWORK, default: testnet)",
    )

    # Tool options
    parser.add_argument(
        "--no-tools", action="store_true",
        help="Disable tool use (web search, URL reader)",
    )

    parser.add_argument("-v", "--verbose", action="store_true", help="Verbose logging")

    args = parser.parse_args()

    if env_file:
        print(f"{DIM}Loaded config from {env_file}{RESET}")

    asyncio.run(main_loop(args))


if __name__ == "__main__":
    cli_main()
