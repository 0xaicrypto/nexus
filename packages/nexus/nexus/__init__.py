"""
Nexus — self-evolving Digital Twin agent framework.

Built on top of :mod:`nexus_core` (the SDK). Nexus adds the
agent runtime: ``DigitalTwin`` (compactor + chat loop + memory
evolution + MCP-aware tool registry) on top of the deterministic
projection memory primitives that live in the SDK.

    from nexus import DigitalTwin

    twin = await DigitalTwin.create("my-twin", llm_api_key="AIza...")
    await twin.chat("Help me plan a trip to Tokyo")

Phase E note — only :class:`ExtendedToolRegistry` is genuinely
Nexus-specific. The other names below (``BaseTool``, ``ToolResult``,
``ToolCall``, ``ToolRegistry``, ``MCPManager``, ``MCPServerConfig``,
``SkillManager``) are convenience re-exports from
:mod:`nexus_core` so callers don't have to remember the package
split. The legacy submodule shims (``nexus.tools.web_search``,
``nexus.skills.manager``, ``nexus.mcp.client``) have been removed —
import those directly from ``nexus_core.*``.
"""

from nexus_core.mcp import MCPManager, MCPServerConfig
from nexus_core.skills import SkillManager

# Re-export SDK classes for convenience
from nexus_core.tools import BaseTool, ToolCall, ToolRegistry, ToolResult

from .config import LLMProvider, TwinConfig
from .tools import ExtendedToolRegistry
from .twin import DigitalTwin

__version__ = "0.1.0"
__all__ = [
    "DigitalTwin",
    "TwinConfig",
    "LLMProvider",
    "ExtendedToolRegistry",
    # Re-exported from SDK
    "BaseTool",
    "ToolResult",
    "ToolCall",
    "ToolRegistry",
    "MCPManager",
    "MCPServerConfig",
    "SkillManager",
]
