"""MCP Client — JSON-RPC 2.0 over stdio or HTTP.

Implements the Model Context Protocol client side:
  - initialize handshake
  - tools/list → discover available tools
  - tools/call → invoke a tool and get results

Lightweight, no external MCP SDK dependency.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class MCPServerConfig:
    """Configuration for an MCP server connection."""
    name: str                           # Unique name for this server
    transport: str = "stdio"            # "stdio" or "http"
    # stdio transport
    command: str = ""                   # Executable (e.g., "npx", "python")
    args: list[str] = field(default_factory=list)  # Command arguments
    env: dict[str, str] = field(default_factory=dict)  # Extra env vars
    # http transport
    url: str = ""                       # Base URL (e.g., "http://localhost:3000")


@dataclass
class MCPTool:
    """Tool descriptor from an MCP server."""
    name: str
    description: str
    input_schema: dict                  # JSON Schema for parameters
    server_name: str = ""               # Which server this tool belongs to


class MCPClient:
    """Client for a single MCP server.

    Handles the JSON-RPC 2.0 protocol over stdio (subprocess) or HTTP.
    """

    def __init__(self, config: MCPServerConfig):
        self.config = config
        self.name = config.name
        self._process: subprocess.Popen | None = None
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._request_id = 0
        self._tools: list[MCPTool] = []
        self._connected = False

    async def connect(self) -> None:
        """Start the MCP server and perform initialization handshake."""
        if self.config.transport == "stdio":
            await self._connect_stdio()
        elif self.config.transport == "http":
            await self._connect_http()
        else:
            raise ValueError(f"Unknown transport: {self.config.transport}")

        # Initialize handshake
        result = await self._request("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "rune-nexus", "version": "1.0.0"},
        })
        logger.info("MCP %s: initialized (server: %s %s)",
                     self.name,
                     result.get("serverInfo", {}).get("name", "?"),
                     result.get("serverInfo", {}).get("version", "?"))

        # Send initialized notification
        await self._notify("notifications/initialized", {})

        # Discover tools
        await self.refresh_tools()
        self._connected = True

    async def _connect_stdio(self) -> None:
        """Spawn subprocess and wire up stdin/stdout."""
        env = {**os.environ, **self.config.env}
        cmd = [self.config.command] + self.config.args

        logger.info("MCP %s: starting stdio server: %s", self.name, " ".join(cmd))

        self._process = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )

        if self._process.stdout is None or self._process.stdin is None:
            raise RuntimeError(f"Failed to start MCP server: {self.config.command}")

        self._reader = self._process.stdout
        self._writer_raw = self._process.stdin

    async def _connect_http(self) -> None:
        """Connect to HTTP/SSE MCP server."""
        # For HTTP transport, we'll use aiohttp or simple HTTP
        # This is a simplified implementation
        raise NotImplementedError("HTTP transport coming soon. Use stdio for now.")

    async def refresh_tools(self) -> list[MCPTool]:
        """Fetch available tools from the server."""
        result = await self._request("tools/list", {})
        raw_tools = result.get("tools", [])

        self._tools = []
        for t in raw_tools:
            tool = MCPTool(
                name=t["name"],
                description=t.get("description", ""),
                input_schema=t.get("inputSchema", {}),
                server_name=self.name,
            )
            self._tools.append(tool)

        logger.info("MCP %s: discovered %d tools: %s",
                     self.name, len(self._tools),
                     [t.name for t in self._tools])
        return self._tools

    @property
    def tools(self) -> list[MCPTool]:
        return self._tools

    async def call_tool(self, tool_name: str, arguments: dict[str, Any] = None) -> dict:
        """Invoke a tool and return the result.

        Returns:
            {"content": [{"type": "text", "text": "..."}], "isError": False}
        """
        result = await self._request("tools/call", {
            "name": tool_name,
            "arguments": arguments or {},
        })
        return result

    async def close(self) -> None:
        """Shut down the server connection."""
        if self._process:
            try:
                self._process.terminate()
                await asyncio.wait_for(self._process.wait(), timeout=5)
            except (asyncio.TimeoutError, ProcessLookupError):
                self._process.kill()
            self._process = None
        self._connected = False
        logger.info("MCP %s: closed", self.name)

    # ── JSON-RPC Protocol ──

    async def _request(self, method: str, params: dict) -> dict:
        """Send a JSON-RPC request and wait for response."""
        self._request_id += 1
        msg = {
            "jsonrpc": "2.0",
            "id": self._request_id,
            "method": method,
            "params": params,
        }
        await self._send(msg)
        return await self._recv(self._request_id)

    async def _notify(self, method: str, params: dict) -> None:
        """Send a JSON-RPC notification (no response expected)."""
        msg = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params,
        }
        await self._send(msg)

    async def _send(self, msg: dict) -> None:
        """Write a JSON-RPC message to the transport."""
        if self.config.transport == "stdio":
            data = json.dumps(msg)
            # MCP uses Content-Length framing over stdio
            header = f"Content-Length: {len(data.encode())}\r\n\r\n"
            self._writer_raw.write(header.encode() + data.encode())
            await self._writer_raw.drain()

    async def _recv(self, expected_id: int) -> dict:
        """Read a JSON-RPC response from the transport."""
        if self.config.transport == "stdio":
            # Read Content-Length header
            while True:
                header_line = await asyncio.wait_for(
                    self._reader.readline(), timeout=30
                )
                header_str = header_line.decode().strip()
                if header_str.startswith("Content-Length:"):
                    content_length = int(header_str.split(":")[1].strip())
                    # Read blank line
                    await self._reader.readline()
                    # Read body
                    body = await asyncio.wait_for(
                        self._reader.readexactly(content_length), timeout=30
                    )
                    msg = json.loads(body.decode())

                    # Skip notifications (no id field)
                    if "id" not in msg:
                        continue

                    if msg.get("id") == expected_id:
                        if "error" in msg:
                            err = msg["error"]
                            raise RuntimeError(
                                f"MCP error {err.get('code', '?')}: {err.get('message', '?')}"
                            )
                        return msg.get("result", {})
                elif not header_str:
                    continue  # skip blank lines

        raise RuntimeError("No response received from MCP server")


class MCPManager:
    """Manages multiple MCP server connections.

    Provides a unified interface for discovering and calling tools
    across all connected servers.
    """

    def __init__(self):
        self._clients: dict[str, MCPClient] = {}

    async def add_server(self, config: MCPServerConfig) -> MCPClient:
        """Connect to an MCP server and discover its tools.

        Args:
            config: Server configuration (name, transport, command/url)

        Returns:
            The connected MCPClient instance.
        """
        if config.name in self._clients:
            logger.warning("MCP server '%s' already connected, replacing", config.name)
            await self._clients[config.name].close()

        client = MCPClient(config)
        await client.connect()
        self._clients[config.name] = client

        logger.info("MCPManager: added server '%s' with %d tools",
                     config.name, len(client.tools))
        return client

    async def remove_server(self, name: str) -> None:
        """Disconnect and remove an MCP server."""
        client = self._clients.pop(name, None)
        if client:
            await client.close()

    def list_all_tools(self) -> list[MCPTool]:
        """Get all tools from all connected servers."""
        tools = []
        for client in self._clients.values():
            tools.extend(client.tools)
        return tools

    async def call_tool(self, server_name: str, tool_name: str,
                        arguments: dict[str, Any] = None) -> dict:
        """Call a tool on a specific server."""
        client = self._clients.get(server_name)
        if not client:
            raise ValueError(f"MCP server '{server_name}' not connected")
        return await client.call_tool(tool_name, arguments)

    async def call_tool_by_name(self, tool_name: str,
                                arguments: dict[str, Any] = None) -> dict:
        """Call a tool by name, auto-routing to the correct server."""
        for client in self._clients.values():
            for tool in client.tools:
                if tool.name == tool_name:
                    return await client.call_tool(tool_name, arguments)
        raise ValueError(f"MCP tool '{tool_name}' not found in any server")

    @property
    def servers(self) -> dict[str, MCPClient]:
        return dict(self._clients)

    @property
    def connected(self) -> bool:
        return len(self._clients) > 0

    async def close_all(self) -> None:
        """Disconnect all servers."""
        for client in self._clients.values():
            await client.close()
        self._clients.clear()
