"""MCP (Model Context Protocol) client.

Connects to external MCP servers and exposes their tools.
Supports stdio and HTTP/SSE transports.

Usage:
    from nexus_core.mcp import MCPManager, MCPServerConfig

    manager = MCPManager()
    await manager.add_server(MCPServerConfig(
        name="filesystem",
        transport="stdio",
        command="npx",
        args=["-y", "@modelcontextprotocol/server-filesystem", "/tmp"],
    ))

    tools = manager.list_all_tools()
    result = await manager.call_tool("filesystem", "read_file", {"path": "/tmp/test.txt"})
"""

from .client import MCPClient, MCPManager, MCPServerConfig, MCPTool

__all__ = ["MCPClient", "MCPServerConfig", "MCPManager", "MCPTool"]
