"""ExtendedToolRegistry — adds MCP support on top of SDK's ToolRegistry.

Core classes (BaseTool, ToolResult, ToolCall, ToolRegistry) are in the SDK.
This module adds MCP server integration for Nexus.
"""

from __future__ import annotations

import logging
from typing import Optional

from nexus_core.mcp import MCPManager, MCPServerConfig

# Import everything from SDK
from nexus_core.tools import ToolCall, ToolRegistry, ToolResult

logger = logging.getLogger(__name__)


class ExtendedToolRegistry(ToolRegistry):
    """ToolRegistry with MCP server support.

    Extends SDK's ToolRegistry to also manage MCP tools.
    LLM sees all tools identically regardless of source.
    """

    def __init__(self):
        super().__init__()
        self._mcp_tools: dict[str, dict] = {}
        self._mcp_manager: Optional[MCPManager] = None

    async def register_mcp_server(self, config: MCPServerConfig) -> list[str]:
        """Connect to an MCP server and register all its tools."""
        if self._mcp_manager is None:
            self._mcp_manager = MCPManager()

        client = await self._mcp_manager.add_server(config)

        registered = []
        for tool in client.tools:
            self._mcp_tools[tool.name] = {
                "server_name": tool.server_name,
                "name": tool.name,
                "description": tool.description,
                "input_schema": tool.input_schema,
            }
            registered.append(tool.name)
            logger.info("Registered MCP tool: %s (server: %s)", tool.name, tool.server_name)

        return registered

    @property
    def tool_names(self) -> list[str]:
        return list(self._tools.keys()) + list(self._mcp_tools.keys())

    @property
    def mcp_tool_names(self) -> list[str]:
        return list(self._mcp_tools.keys())

    def __len__(self) -> int:
        return len(self._tools) + len(self._mcp_tools)

    def __bool__(self) -> bool:
        return bool(self._tools) or bool(self._mcp_tools)

    def get_definitions(self) -> list[dict]:
        defs = [tool.to_definition() for tool in self._tools.values()]
        for mcp in self._mcp_tools.values():
            defs.append({
                "name": mcp["name"],
                "description": mcp["description"],
                "parameters": mcp["input_schema"],
            })
        return defs

    async def execute(self, call: ToolCall) -> ToolResult:
        # 1. Built-in tools
        tool = self._tools.get(call.name)
        if tool:
            try:
                result = await tool.execute(**call.arguments)
                logger.debug("Tool %s: success=%s", call.name, result.success)
                return result
            except Exception as e:
                logger.warning("Tool %s error: %s", call.name, e)
                return ToolResult(success=False, error=f"Tool failed: {e}")

        # 2. MCP tools
        mcp_tool = self._mcp_tools.get(call.name)
        if mcp_tool and self._mcp_manager:
            try:
                result = await self._mcp_manager.call_tool(
                    mcp_tool["server_name"], call.name, call.arguments
                )
                content_parts = result.get("content", [])
                text_parts = [p.get("text", str(p)) for p in content_parts if isinstance(p, dict)]
                output = "\n".join(text_parts) if text_parts else str(result)
                is_error = result.get("isError", False)
                return ToolResult(success=not is_error, output=output if not is_error else "", error=output if is_error else "")
            except Exception as e:
                logger.warning("MCP tool %s error: %s", call.name, e)
                return ToolResult(success=False, error=f"MCP tool failed: {e}")

        # 3. Not found
        return ToolResult(success=False, error=f"Unknown tool: {call.name}. Available: {', '.join(self.tool_names)}")

    async def close(self) -> None:
        if self._mcp_manager:
            await self._mcp_manager.close_all()
            self._mcp_manager = None
