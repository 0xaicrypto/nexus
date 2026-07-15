"""
WebSearchTool — Search the web for information.

Supports two backends:
  1. Tavily API (recommended) — high-quality search results with snippets
  2. DuckDuckGo (fallback, no API key) — free but lower quality

Configure via environment variable:
  TAVILY_API_KEY=tvly-...       → uses Tavily
  (no key set)                  → falls back to DuckDuckGo
"""

from __future__ import annotations

import logging
import os

from .base import BaseTool, ToolResult

logger = logging.getLogger(__name__)


class WebSearchTool(BaseTool):
    """Search the web and return results with titles, snippets, and URLs."""

    def __init__(self, api_key: str = "", max_results: int = 5):
        self._api_key = api_key or os.environ.get("TAVILY_API_KEY", "")
        self._max_results = max_results

    @property
    def name(self) -> str:
        return "web_search"

    @property
    def description(self) -> str:
        return (
            "Search the web for current information — articles, news, "
            "general facts, blog posts. Returns titles, snippets, and URLs.\n"
            "\n"
            "DO NOT use as a first resort for STRUCTURED LIVE DATA. "
            "Specifically, do NOT use web_search for:\n"
            "  * Blockchain data (block height, balance, tx receipt) — use "
            "    bsc_query for BSC, or manage_mcp(search='<chain>') to "
            "    install an RPC server for any other chain.\n"
            "  * SaaS data (Slack messages, Notion pages, Postgres rows) — "
            "    use manage_mcp(search='<service>') to install an integration.\n"
            "  * Real-time prices / balances / inventory — use the matching "
            "    MCP server, not scraped search snippets.\n"
            "\n"
            "Search engines hallucinate context (e.g. confuse Bitcoin's "
            "block height for BSC's, return stale prices), so structured "
            "queries belong on dedicated tools. Use web_search ONLY when "
            "the answer is genuinely a free-form web document and no "
            "matching MCP / skill exists."
        )

    @property
    def parameters(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "The search query. Be specific for better results.",
                },
                "max_results": {
                    "type": "integer",
                    "description": "Maximum number of results to return (default: 5, max: 10).",
                },
            },
            "required": ["query"],
        }

    async def execute(self, query: str, max_results: int = 0, **kwargs) -> ToolResult:
        """Execute a web search."""
        n = min(max_results or self._max_results, 10)

        if self._api_key:
            return await self._search_tavily(query, n)
        else:
            return await self._search_duckduckgo(query, n)

    async def _search_tavily(self, query: str, max_results: int) -> ToolResult:
        """Search using Tavily API."""
        try:
            import httpx
        except ImportError:
            return ToolResult(
                success=False,
                error="httpx not installed. Run: pip install httpx",
            )

        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                response = await client.post(
                    "https://api.tavily.com/search",
                    json={
                        "api_key": self._api_key,
                        "query": query,
                        "max_results": max_results,
                        "include_answer": True,
                        "search_depth": "basic",
                    },
                )
                response.raise_for_status()
                data = response.json()

            # Format results
            parts = []
            answer = data.get("answer")
            if answer:
                parts.append(f"**Summary:** {answer}\n")

            results = data.get("results", [])
            for i, r in enumerate(results, 1):
                title = r.get("title", "Untitled")
                url = r.get("url", "")
                snippet = r.get("content", "")[:300]
                parts.append(f"{i}. **{title}**\n   {snippet}\n   URL: {url}")

            if not parts:
                return ToolResult(output="No results found.")

            return ToolResult(output="\n\n".join(parts))

        except httpx.HTTPStatusError as e:
            return ToolResult(success=False, error=f"Tavily API error: {e.response.status_code}")
        except Exception as e:
            logger.warning("Tavily search failed: %s", e)
            return ToolResult(success=False, error=f"Search failed: {e}")

    async def _search_duckduckgo(self, query: str, max_results: int) -> ToolResult:
        """Search using DuckDuckGo HTML (no API key required).

        Uses the DuckDuckGo Lite HTML interface for simplicity.
        Falls back gracefully if parsing fails.
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
                timeout=15.0,
                headers={"User-Agent": "Mozilla/5.0 (compatible; RuneNexus/1.0)"},
                follow_redirects=True,
            ) as client:
                response = await client.get(
                    "https://lite.duckduckgo.com/lite/",
                    params={"q": query},
                )
                response.raise_for_status()
                html = response.text

            # Simple extraction from DuckDuckGo Lite results
            results = self._parse_ddg_lite(html, max_results)
            if not results:
                return ToolResult(output=f"No results found for: {query}")

            parts = []
            for i, r in enumerate(results, 1):
                parts.append(f"{i}. **{r['title']}**\n   {r['snippet']}\n   URL: {r['url']}")

            return ToolResult(output="\n\n".join(parts))

        except Exception as e:
            logger.warning("DuckDuckGo search failed: %s", e)
            return ToolResult(success=False, error=f"Search failed: {e}")

    @staticmethod
    def _parse_ddg_lite(html: str, max_results: int) -> list[dict]:
        """Parse DuckDuckGo Lite HTML for search results.

        Extracts result links and snippets from the HTML table format.
        Uses basic string parsing to avoid requiring an HTML parser dependency.
        """
        import re
        results = []

        # DDG Lite uses tables with result links as <a> tags in specific cells
        # Pattern: find result links — they have class="result-link"
        links = re.findall(
            r'class="result-link"[^>]*href="([^"]+)"[^>]*>([^<]+)</a>',
            html,
        )

        # Find snippet text — appears in <td class="result-snippet">
        snippets = re.findall(
            r'class="result-snippet"[^>]*>(.*?)</td>',
            html,
            re.DOTALL,
        )

        for i, (url, title) in enumerate(links[:max_results]):
            snippet = ""
            if i < len(snippets):
                # Clean HTML tags from snippet
                snippet = re.sub(r'<[^>]+>', '', snippets[i]).strip()[:300]

            results.append({
                "title": title.strip(),
                "url": url.strip(),
                "snippet": snippet,
            })

        return results
