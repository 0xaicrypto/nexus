"""Tool framework — BaseTool, ToolResult, ToolRegistry + built-in tools.

Built-in tools:
  - WebSearchTool: Web search via Tavily API
  - URLReaderTool: URL content extraction via Jina API
  - FileGeneratorTool: Generate files for user download
  - ReadUploadedFileTool: Read uploaded file content by section
  - SkillInstallerTool: Search + install Anthropic-style skills (LobeHub)
  - McpInstallerTool: Search + install MCP servers (LobeHub)
"""

from .base import BaseTool, ToolCall, ToolRegistry, ToolResult
from .file_generator import FileGeneratorTool
from .file_reader import ReadUploadedFileTool
from .skill_installer import McpInstallerTool, SkillInstallerTool
from .url_reader import URLReaderTool
from .web_search import WebSearchTool

__all__ = [
    "BaseTool", "ToolResult", "ToolCall", "ToolRegistry",
    "WebSearchTool", "URLReaderTool", "FileGeneratorTool",
    "ReadUploadedFileTool",
    "SkillInstallerTool", "McpInstallerTool",
]
