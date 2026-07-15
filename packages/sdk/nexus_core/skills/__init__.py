"""Skill system — install and load external skills (Binance Skills Hub compatible).

A skill is a folder with a SKILL.md file containing YAML frontmatter + LLM instructions.
When loaded, skill instructions are injected into the LLM system prompt, teaching the agent
how to use external CLIs, APIs, or protocols.

Usage:
    from nexus_core.skills import SkillManager

    manager = SkillManager(base_dir=".rune_data/skills")

    # Install from GitHub (Binance Skills Hub format)
    await manager.install("https://github.com/binance/binance-skills-hub/tree/main/skills/binance-web3/binance-agentic-wallet")

    # Install from local path
    manager.install_local("/path/to/skill-folder")

    # Get all loaded skill instructions (for system prompt)
    context = manager.get_prompt_context()
"""

from .manager import InstalledSkill, SkillManager

__all__ = ["SkillManager", "InstalledSkill"]
