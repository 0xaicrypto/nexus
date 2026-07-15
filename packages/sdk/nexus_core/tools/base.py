"""BaseTool — abstract base class and ToolRegistry for managing tools.

Tools are external capabilities an agent can invoke. Each tool defines:
  - name: unique identifier
  - description: what the tool does
  - parameters: JSON Schema describing arguments
  - execute(): async method that performs the action

The ToolRegistry holds all registered tools and provides:
  - get_definitions(): tool definitions in provider-agnostic format
  - execute(): routes a call to the right tool
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class ToolResult:
    """Result of a tool execution."""
    success: bool = True
    output: str = ""
    error: str = ""

    def to_str(self) -> str:
        if self.success:
            return self.output
        return f"[Tool Error] {self.error}"


class BaseTool(ABC):
    """Abstract base class for all tools."""

    @property
    @abstractmethod
    def name(self) -> str: ...

    @property
    @abstractmethod
    def description(self) -> str: ...

    @property
    @abstractmethod
    def parameters(self) -> dict: ...

    @abstractmethod
    async def execute(self, **kwargs) -> ToolResult: ...

    def to_definition(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "parameters": self.parameters,
        }


@dataclass
class ToolCall:
    """Represents an LLM's request to call a tool."""
    id: str
    name: str
    arguments: dict = field(default_factory=dict)


class ToolRegistry:
    """Registry of available tools.

    Manages tool registration, definition export, and execution routing.
    Extensible: subclass or compose to add MCP, skill, or other tool sources.
    """

    def __init__(self):
        self._tools: dict[str, BaseTool] = {}

    def register(self, tool: BaseTool) -> None:
        """Register a tool. Overwrites if name already exists."""
        self._tools[tool.name] = tool
        logger.info("Registered tool: %s", tool.name)

    def unregister(self, name: str) -> None:
        self._tools.pop(name, None)

    def get(self, name: str) -> Optional[BaseTool]:
        return self._tools.get(name)

    @property
    def tool_names(self) -> list[str]:
        return list(self._tools.keys())

    def __len__(self) -> int:
        return len(self._tools)

    def __bool__(self) -> bool:
        return bool(self._tools)

    def get_definitions(self) -> list[dict]:
        """Get all tool definitions in provider-agnostic format."""
        return [tool.to_definition() for tool in self._tools.values()]

    async def execute(self, call: ToolCall) -> ToolResult:
        """Execute a tool call. Override in subclass for MCP routing."""
        tool = self._tools.get(call.name)
        if not tool:
            return ToolResult(
                success=False,
                error=f"Unknown tool: {call.name}. Available: {', '.join(self._tools.keys())}",
            )
        try:
            result = await tool.execute(**call.arguments)
            logger.debug("Tool %s: success=%s, output_len=%d",
                         call.name, result.success, len(result.output))
            return result
        except Exception as e:
            logger.warning("Tool %s error: %s", call.name, e)
            return ToolResult(success=False, error=f"Tool execution failed: {e}")

    async def close(self) -> None:
        """Clean up resources. Override in subclass for MCP cleanup."""
        pass
