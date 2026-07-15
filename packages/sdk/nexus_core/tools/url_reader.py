"""
URLReaderTool — Fetch and extract main content from a URL.

Supports two backends:
  1. Jina Reader API (recommended) — clean markdown output, free tier available
  2. Direct fetch + readability (fallback) — uses httpx + basic HTML extraction

Configure via environment variable:
  JINA_API_KEY=jina_...         → uses Jina Reader (higher quality)
  (no key set)                  → uses Jina Reader without auth (rate-limited)
"""

from __future__ import annotations

import logging
import os
import re

from .base import BaseTool, ToolResult

logger = logging.getLogger(__name__)


class URLReaderTool(BaseTool):
    """Fetch a URL and extract its main content as clean text."""

    def __init__(self, api_key: str = "", max_length: int = 8000):
        self._api_key = api_key or os.environ.get("JINA_API_KEY", "")
        self._max_length = max_length

    @property
    def name(self) -> str:
        return "read_url"

    @property
    def description(self) -> str:
        return (
            "Fetch and read the content of a web page. Returns the main article "
            "text as clean markdown, stripping navigation, ads, and boilerplate. "
            "Use this after web_search to read a specific article in full, or when "
            "a user shares a URL they want you to read."
        )

    @property
    def parameters(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "The URL to fetch and read.",
                },
                "max_length": {
                    "type": "integer",
                    "description": "Maximum content length in characters (default: 8000).",
                },
            },
            "required": ["url"],
        }

    async def execute(self, url: str, max_length: int = 0, **kwargs) -> ToolResult:
        """Fetch and extract content from a URL."""
        limit = max_length or self._max_length

        # Try Jina Reader first (best quality), fall back to direct fetch
        result = await self._read_via_jina(url, limit)
        if result.success:
            return result

        logger.debug("Jina Reader failed, falling back to direct fetch")
        return await self._read_direct(url, limit)

    async def _read_via_jina(self, url: str, max_length: int) -> ToolResult:
        """Use Jina Reader API to extract clean content.

        Jina Reader (r.jina.ai) converts any URL to clean markdown.
        Free tier: 20 req/min. With API key: higher limits.
        """
        try:
            import httpx
        except ImportError:
            return ToolResult(
                success=False,
                error="httpx not installed. Run: pip install httpx",
            )

        try:
            headers = {
                "Accept": "text/markdown",
                "X-Return-Format": "markdown",
            }
            if self._api_key:
                headers["Authorization"] = f"Bearer {self._api_key}"

            jina_url = f"https://r.jina.ai/{url}"

            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.get(jina_url, headers=headers)
                response.raise_for_status()
                content = response.text

            if not content or len(content.strip()) < 50:
                return ToolResult(
                    success=False,
                    error="Jina Reader returned empty content",
                )

            # Truncate if too long
            if len(content) > max_length:
                content = content[:max_length] + "\n\n[Content truncated]"

            return ToolResult(output=content)

        except Exception as e:
            logger.debug("Jina Reader error: %s", e)
            return ToolResult(success=False, error=f"Jina Reader failed: {e}")

    async def _read_direct(self, url: str, max_length: int) -> ToolResult:
        """Direct HTTP fetch with basic HTML-to-text extraction.

        Strips HTML tags and extracts readable text. Less clean than Jina
        but works without external dependencies beyond httpx.
        """
        try:
            import httpx
        except ImportError:
            return ToolResult(
                success=False,
                error="httpx not installed. Run: pip install httpx",
            )

        try:
            async with httpx.AsyncClient(
                timeout=20.0,
                headers={
                    "User-Agent": "Mozilla/5.0 (compatible; RuneNexus/1.0)",
                },
                follow_redirects=True,
            ) as client:
                response = await client.get(url)
                response.raise_for_status()
                html = response.text

            # Extract text from HTML
            content = self._html_to_text(html)

            if not content or len(content.strip()) < 50:
                return ToolResult(
                    success=False,
                    error="Page returned no readable content",
                )

            if len(content) > max_length:
                content = content[:max_length] + "\n\n[Content truncated]"

            return ToolResult(output=content)

        except Exception as e:
            logger.warning("Direct URL fetch failed: %s", e)
            return ToolResult(success=False, error=f"Failed to fetch URL: {e}")

    @staticmethod
    def _html_to_text(html: str) -> str:
        """Convert HTML to readable text.

        Basic extraction: removes scripts, styles, tags, and cleans whitespace.
        Not as clean as a proper readability parser, but functional.
        """
        # Remove script and style blocks
        text = re.sub(r'<script[^>]*>.*?</script>', '', html, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r'<style[^>]*>.*?</style>', '', text, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r'<nav[^>]*>.*?</nav>', '', text, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r'<header[^>]*>.*?</header>', '', text, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r'<footer[^>]*>.*?</footer>', '', text, flags=re.DOTALL | re.IGNORECASE)

        # Convert some tags to markdown-ish format
        text = re.sub(r'<h[1-6][^>]*>(.*?)</h[1-6]>', r'\n## \1\n', text, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r'<p[^>]*>(.*?)</p>', r'\1\n\n', text, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r'<br\s*/?\s*>', '\n', text, flags=re.IGNORECASE)
        text = re.sub(r'<li[^>]*>(.*?)</li>', r'- \1\n', text, flags=re.DOTALL | re.IGNORECASE)

        # Remove all remaining tags
        text = re.sub(r'<[^>]+>', '', text)

        # Decode common HTML entities
        text = text.replace('&amp;', '&')
        text = text.replace('&lt;', '<')
        text = text.replace('&gt;', '>')
        text = text.replace('&quot;', '"')
        text = text.replace('&#39;', "'")
        text = text.replace('&nbsp;', ' ')

        # Clean up whitespace
        text = re.sub(r'\n{3,}', '\n\n', text)
        text = re.sub(r'[ \t]+', ' ', text)

        return text.strip()
