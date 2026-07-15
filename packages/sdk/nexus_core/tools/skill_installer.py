"""SkillInstallerTool + MCPInstallerTool — let the agent expand its
own capabilities at chat time.

Background
==========
Twin's persona prompt has long advertised "you can install new skills
from the LobeHub marketplace" but no actual install tool was ever
registered. The agent would dutifully tell the user it was "installing
a skill", then admit a few turns later that it could not actually
execute the install — frustrating for the user and a credibility hit
for the "self-evolving agent" pitch.

These two tools wrap the existing :class:`SkillManager` API
(``search_lobehub`` / ``install`` for Anthropic-style skills, and
``search_mcp`` / ``install_mcp`` for MCP servers) as ToolRegistry
entries the LLM can invoke via function calling.

Design notes
------------
* Each tool is split into search + install rather than one mega-tool,
  matching how the SDK's SkillManager exposes it. The LLM finds the
  right marketplace entry first, confirms the choice with the user
  (it's the LLM's job to ask), then installs by id. This keeps the
  install step explicit — the agent never installs something it
  hasn't first surfaced to the user.

* Both tools are NO-OP-safe when network is unavailable — the
  underlying manager catches httpx errors and returns empty lists /
  error dicts, which we surface to the LLM verbatim. The LLM can then
  apologise and suggest copying-pasting the relevant snippet, etc.

* MCP install needs a ``tool_registry`` so the newly installed MCP's
  tools can be registered into the live session. We pass ``None`` to
  the SDK helper, which installs the MCP definitions but doesn't
  register MCP tools onto the live ToolRegistry. Tools that need a
  full hot-load (without restart) can subclass and inject the live
  registry — for now the user restarts twin to pick up new MCP tools,
  matching the same pattern as Cursor / VS Code MCP integrations.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import TYPE_CHECKING

from .base import BaseTool, ToolResult

if TYPE_CHECKING:
    from ..skills import SkillManager

logger = logging.getLogger(__name__)


# Cap how many marketplace results we surface to the LLM in one search.
# More than this and the model spends most of its context on the list
# instead of reasoning about which one fits the user's need.
_MAX_RESULTS = 8

# Hard ceilings on each operation so the chat surface never sits on
# "Agent is thinking…" for more than this on a stuck network. The LLM
# still gets a clean failure ToolResult and can choose another path
# (try the other marketplace, ask the user to paste content, …).
_SEARCH_TIMEOUT = 25.0   # one search; both backends in parallel
_INSTALL_TIMEOUT = 60.0  # one install; bounded by per-download timeout
_LIST_TIMEOUT = 5.0      # local read, should be instant


def _safe_truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)] + "…"


# Query expansion: small hand-curated synonym map covering the
# capability gaps users hit most. Each input token expands to several
# search variants we run in parallel against every backend, then merge
# + dedupe. This costs almost nothing (extra GitHub API calls are
# parallel) and dramatically improves recall — "pdf" alone hits one
# narrow LobeHub query, but "pdf reader / pdf extract / pypdf /
# document parser" between them surface every skill that touches PDF
# handling.
#
# Keys are matched as substrings (case-insensitive) against the user's
# query; values are appended to the variant list.  Duplicates from
# overlapping rules are deduped before fan-out.
_QUERY_EXPANSIONS: dict[str, list[str]] = {
    "pdf":      ["pdf", "pdf reader", "pdf extract", "pypdf", "document parser"],
    "excel":    ["excel", "xlsx", "spreadsheet", "openpyxl", "tabular"],
    "spreadsheet": ["spreadsheet", "xlsx", "csv", "tabular"],
    "csv":      ["csv", "spreadsheet", "tabular data"],
    "image":    ["image", "image edit", "vision", "ocr"],
    "ocr":      ["ocr", "image text", "tesseract"],
    "deck":     ["pptx", "slides", "presentation", "powerpoint"],
    "slides":   ["pptx", "slides", "presentation", "powerpoint"],
    "diagram":  ["diagram", "mermaid", "plantuml", "drawio"],
    "chart":    ["chart", "plot", "matplotlib", "graph"],
    "plot":     ["plot", "chart", "matplotlib"],
    "translate": ["translate", "translation", "i18n"],
    "wallet":   ["wallet", "ethers", "web3", "blockchain"],
    "github":   ["github", "git"],
    "search":   ["web search", "search api", "tavily"],
}


def _expand_query(query: str) -> list[str]:
    """Return a deduped list of search variants for ``query``.

    Always includes the original. If the query matches any of the
    synonym table's keys, add those variants too. Cap at 6 variants
    so we don't blow up the GitHub API rate budget on every call.
    """
    q_lower = query.lower().strip()
    out: list[str] = [query.strip()]
    seen = {q_lower}
    for key, variants in _QUERY_EXPANSIONS.items():
        if key in q_lower:
            for v in variants:
                if v.lower() not in seen:
                    out.append(v)
                    seen.add(v.lower())
    return out[:6]


class SkillInstallerTool(BaseTool):
    """Search the LobeHub Skills marketplace and install a chosen skill.

    Two operations under one tool name (``manage_skill``) so the LLM
    can invoke either via the same function — the schema's ``action``
    field discriminates. Keeping it as one function (rather than two)
    matches the pattern most LLMs train on for "manage X" CRUD tools
    and is cheaper on the function-list token budget.
    """

    def __init__(self, skill_manager: "SkillManager"):
        self._mgr = skill_manager
        # Search-result cache so install() can recover when the LLM
        # passes a bare `name` instead of the proper `identifier` from
        # a previous search. Common failure mode without this: agent
        # search returns 7 entries (each with full GitHub URL
        # identifiers), agent renders a markdown bullet list using just
        # the names, user replies "install slack-gif-creator", agent
        # passes "slack-gif-creator" as identifier — but the GitHub
        # backend's installer needs the full URL. We now look up the
        # name in the cache first and substitute the real identifier.
        # Bounded LRU-ish: keep the latest 200 entries (only updated on
        # search, so memory is trivially bounded).
        self._recent_searches: dict[str, dict] = {}

    @property
    def name(self) -> str:
        return "manage_skill"

    @property
    def description(self) -> str:
        return (
            "Search and install Anthropic-style skills (SKILL.md format) "
            "from the official anthropics/skills repo on GitHub "
            "(canonical Claude skill hub: pdf, docx, xlsx, pptx, "
            "skill-creator, and ~10 more). Search auto-expands the "
            "query into synonym variants (e.g. 'pdf' → 'pdf, pdf reader, "
            "pdf extract, document parser') for better recall. The "
            "whole flow is fully automated end-to-end — no auth, no "
            "OAuth, no marketplace credentials. Use this when the user "
            "asks for a capability you don't have: action='search' to "
            "find candidates, action='install' to add one (identifier "
            "comes from search results), action='list' to see what's "
            "already installed, action='show' to load a skill's full "
            "SKILL.md into your context before using it."
        )

    @property
    def parameters(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["search", "install", "show", "list"],
                    "description": (
                        "search → query the Anthropic skills hub (with "
                        "synonym expansion) and return matches; "
                        "install → add a specific skill by id (the "
                        "'identifier' field from a previous search); "
                        "show → load the FULL SKILL.md instructions for "
                        "an already-installed skill into your context. "
                        "CALL THIS FIRST whenever the user asks you to "
                        "do something a skill covers (e.g. 'analyze "
                        "this PDF' → first show(name='pdf') to load the "
                        "operations, THEN follow the instructions). The "
                        "system prompt only carries skill names + "
                        "80-char descriptions, not the actual "
                        "operations — show is how you read those.; "
                        "list → show currently installed skill names."
                    ),
                },
                "query": {
                    "type": "string",
                    "description": "For action='search': the natural-language query.",
                },
                "identifier": {
                    "type": "string",
                    "description": (
                        "For action='install': the skill identifier "
                        "returned by a previous search (a GitHub URL "
                        "pointing into anthropics/skills). The bare "
                        "name from a search result also works — "
                        "search results are cached and bare names are "
                        "looked up automatically."
                    ),
                },
                "name": {
                    "type": "string",
                    "description": (
                        "For action='show': the installed skill's name "
                        "(see action='list' to find it). Returns the "
                        "full SKILL.md content + any reference docs."
                    ),
                },
                # `source` retained for back-compat — earlier revs
                # supported lobehub/gemini/github backends. They were
                # all removed in favour of anthropics/skills only.
                # Any value is silently treated as "anthropic".
                "source": {
                    "type": "string",
                    "enum": ["anthropic"],
                    "description": (
                        "Marketplace to search. Only 'anthropic' "
                        "(anthropics/skills repo) is supported."
                    ),
                },
            },
            "required": ["action"],
        }

    async def execute(
        self,
        action: str,
        query: str = "",
        identifier: str = "",
        name: str = "",
        source: str = "all",
        **kwargs,
    ) -> ToolResult:
        # Outer hard timeout. Per-action budgets are enforced via
        # asyncio.wait_for around the actual work below — failure
        # surfaces as a clean ToolResult so the LLM moves on instead
        # of holding the chat surface hostage.
        a = (action or "").strip().lower()
        src = (source or "all").strip().lower()
        try:
            if a == "search":
                if not query.strip():
                    return ToolResult(
                        success=False,
                        output="action=search requires a non-empty 'query' field.",
                    )

                # Audit fix A: expand the query into synonym variants so
                # one narrow input ("pdf") still hits skills filed under
                # adjacent capability names ("pdf reader", "pypdf",
                # "document parser"). Each backend gets every variant in
                # parallel; results are merged + deduped by identifier.
                variants = _expand_query(query)
                logger.info(
                    "manage_skill search: '%s' → %d variant(s): %s",
                    query, len(variants), variants,
                )

                # Build the task fan-out. We only query Anthropic's
                # canonical skills repo now — earlier revs also queried
                # Gemini, GitHub topic, and LobeHub but each had its
                # own friction (rate-limited, low signal, or auth-
                # required). Anthropic's repo is fully public, fully
                # automatable end-to-end (search via GitHub contents
                # API, install via sparse-checkout), and covers ~90%
                # of the agent's reflexive capability needs.
                #
                # The `source` param is kept for back-compat but
                # ignored — every value collapses to anthropic-only
                # to avoid surfacing dead-end results.
                tasks: list = []
                task_meta: list[tuple[str, str]] = []  # (source, variant)
                for v in variants:
                    tasks.append(self._mgr.search_anthropic_official(v, limit=_MAX_RESULTS))
                    task_meta.append(("anthropic", v))

                try:
                    gathered = await asyncio.wait_for(
                        asyncio.gather(*tasks, return_exceptions=True),
                        timeout=_SEARCH_TIMEOUT,
                    )
                except asyncio.TimeoutError:
                    return ToolResult(
                        success=False,
                        output=(
                            f"Skill marketplace search timed out after "
                            f"{_SEARCH_TIMEOUT:.0f}s — GitHub may be "
                            f"throttling. Set GITHUB_TOKEN env var to "
                            f"raise the rate limit from 60/h to 5000/h."
                        ),
                    )

                # Merge results across all (source × variant) buckets,
                # dedupe by identifier so the same skill from a same
                # source isn't surfaced multiple times when several
                # variants matched it.
                seen_ids: set[str] = set()
                slim: list[dict] = []
                for (source_name, _variant), result in zip(task_meta, gathered):
                    if not isinstance(result, list):
                        continue
                    for r in result:
                        ident = r.get("identifier") or r.get("id") or r.get("url", "")
                        if not ident or ident in seen_ids:
                            continue
                        seen_ids.add(ident)
                        slim.append({
                            "identifier": ident,
                            "name": r.get("name") or r.get("title") or ident,
                            "description": _safe_truncate(
                                r.get("description") or r.get("summary", ""), 200,
                            ),
                            "source": r.get("source") or source_name,
                            "stars": r.get("stars"),
                            "tags": r.get("tags", []),
                        })
                # Single-source after the multi-marketplace cleanup —
                # only stars + name matter for ranking. Kept the sort
                # for back-compat (search_anthropic_official preserves
                # repo order, but we want stars-then-name to make the
                # most popular Anthropic skill (typically pdf or
                # docx) land at the top regardless of repo layout).
                slim.sort(
                    key=lambda x: (
                        -(x.get("stars") or 0),
                        x.get("name", ""),
                    ),
                )
                slim = slim[: _MAX_RESULTS * 2]  # cap final list

                # ── Cache search results so install() can recover from
                # a bare-name fallback. Common failure mode this fixes:
                # search returns 7 entries with full GitHub URL
                # identifiers, agent renders bullet list using just the
                # `name`, user replies "install slack-gif-creator" →
                # agent calls install(identifier="slack-gif-creator")
                # → not a valid GitHub URL → install fails.
                # Now we look up the bare name in this cache and
                # substitute the real identifier before invoking the
                # backend installer.
                #
                # Latest-wins: full replace each search so stale entries
                # from previous queries don't shadow current ones.
                self._recent_searches = {}
                for entry in slim:
                    nm = (entry.get("name") or "").lower().strip()
                    ident = entry.get("identifier")
                    if nm and ident:
                        self._recent_searches[nm] = entry

                if not slim:
                    # Audit fix D: empty-everywhere → return a clear
                    # fallback so the LLM can give the user actionable
                    # next steps instead of just shrugging.
                    return ToolResult(
                        success=True,
                        output=json.dumps({
                            "matches": [],
                            "fallback_options": [
                                {
                                    "kind": "paste_content",
                                    "instruction": (
                                        "Ask the user to paste the file's "
                                        "content directly into chat — you "
                                        "can already read text from any "
                                        "uploaded text-friendly file."
                                    ),
                                },
                                {
                                    "kind": "github_url",
                                    "instruction": (
                                        "If the user knows a specific skill "
                                        "repo (e.g. they saw it on GitHub), "
                                        "ask them for the URL and pass it "
                                        "directly to manage_skill(action="
                                        "'install', identifier='https://"
                                        "github.com/owner/repo/tree/main/"
                                        "skills/<name>')."
                                    ),
                                },
                                {
                                    "kind": "broaden_query",
                                    "instruction": (
                                        f"The query '{query}' returned 0 "
                                        f"matches across LobeHub + Gemini + "
                                        f"GitHub-topic with {len(variants)} "
                                        f"synonym variants. Try a higher-"
                                        f"level term ('document' instead "
                                        f"of 'pdf-reader')."
                                    ),
                                },
                            ],
                            "queries_tried": variants,
                        }, ensure_ascii=False),
                    )
                return ToolResult(
                    success=True,
                    output=json.dumps({
                        "matches": slim,
                        "queries_tried": variants,
                    }, ensure_ascii=False),
                )

            if a == "install":
                if not identifier.strip():
                    return ToolResult(
                        success=False,
                        output="action=install requires the 'identifier' field (use action=search first).",
                    )

                # ── Name → identifier fallback.
                # If the LLM passes a bare name like "slack-gif-creator"
                # but recent search returned that as `name` paired with
                # a real install id (a GitHub URL, an `anthropic:`-
                # prefixed id, etc), substitute the real id. Keep the
                # raw `identifier` in case it IS a valid id (URLs,
                # prefixed ids, etc. won't show up in the cache).
                resolved = identifier.strip()
                looks_like_url = "://" in resolved
                looks_prefixed = ":" in resolved and not looks_like_url
                looks_namespaced = "/" in resolved and not looks_like_url
                if not (looks_like_url or looks_prefixed or looks_namespaced):
                    cached = self._recent_searches.get(resolved.lower())
                    if cached and cached.get("identifier"):
                        resolved = cached["identifier"]
                        logger.info(
                            "manage_skill install: resolved bare name "
                            "'%s' → '%s' (source=%s) via search cache",
                            identifier, resolved, cached.get("source", "?"),
                        )

                try:
                    installed = await asyncio.wait_for(
                        self._mgr.install(resolved),
                        timeout=_INSTALL_TIMEOUT,
                    )
                except asyncio.TimeoutError:
                    return ToolResult(
                        success=False,
                        output=(
                            f"Skill install timed out after {_INSTALL_TIMEOUT:.0f}s — "
                            f"the marketplace fetch (or `npx` first-run) is taking too long. "
                            f"Try a different skill or run search again."
                        ),
                    )
                except Exception as e:
                    # If the install backend rejected the resolved
                    # identifier and we DID re-route via cache, surface
                    # both ids so the user can see what the agent tried.
                    msg = (
                        f"Install failed for '{identifier}'"
                        + (f" (resolved to '{resolved}')" if resolved != identifier.strip() else "")
                        + f": {type(e).__name__}: {e}"
                    )
                    return ToolResult(success=False, output=msg)
                return ToolResult(
                    success=True,
                    output=json.dumps({
                        "installed": True,
                        "name": getattr(installed, "name", str(installed)),
                        "version": getattr(installed, "version", ""),
                        "note": (
                            "Skill installed. Reference its capabilities in "
                            "your next replies; the system prompt has already "
                            "been updated."
                        ),
                    }, ensure_ascii=False),
                )

            if a == "show":
                # On-demand load of full SKILL.md content. Without
                # this branch the LLM only ever saw 80-char skill
                # blurbs in the system prompt — it knew "pdf skill
                # exists" but not what operations it provided, so
                # users hitting "analyze this PDF" got hallucinated
                # "I don't have an analyze function" replies. Now
                # the LLM is told (via the action enum's docstring)
                # to call show first, gets the actual instructions,
                # and can follow them.
                if not name.strip():
                    return ToolResult(
                        success=False,
                        output="action=show requires the 'name' field (use action=list to find it).",
                    )
                target = None
                for s in self._mgr.installed:
                    if getattr(s, "name", "") == name:
                        target = s
                        break
                if target is None:
                    return ToolResult(
                        success=False,
                        output=(
                            f"Skill '{name}' is not installed. Available: "
                            f"{', '.join(getattr(s, 'name', '') for s in self._mgr.installed) or '(none)'}. "
                            f"Use manage_skill(action='install', identifier=...) first."
                        ),
                    )
                payload = {
                    "name": getattr(target, "name", ""),
                    "title": getattr(target, "title", ""),
                    "description": getattr(target, "description", ""),
                    "version": getattr(target, "version", ""),
                    "instructions": getattr(target, "instructions", ""),
                    # references is dict[str, str] — name → markdown body
                    "references": dict(getattr(target, "references", {}) or {}),
                }
                return ToolResult(
                    success=True,
                    output=json.dumps(payload, ensure_ascii=False),
                )

            if a == "list":
                items = self._mgr.installed
                return ToolResult(
                    success=True,
                    output=json.dumps({
                        "installed": [
                            {
                                "name": getattr(s, "name", ""),
                                "version": getattr(s, "version", ""),
                                "description": _safe_truncate(
                                    getattr(s, "description", ""), 160,
                                ),
                            }
                            for s in items
                        ],
                    }, ensure_ascii=False),
                )

            return ToolResult(
                success=False,
                output=f"Unknown action '{action}'. Valid: search, install, list.",
            )
        except Exception as e:
            logger.warning("manage_skill %s failed: %s", action, e)
            return ToolResult(
                success=False,
                output=f"manage_skill action={action} failed: {e}",
            )


class McpInstallerTool(BaseTool):
    """Search and install MCP servers from the LobeHub MCP marketplace.

    MCP (Model Context Protocol) servers add real backend integrations
    — Slack, GitHub, Google Drive, databases — as new tools the agent
    can call. This tool lets the agent broker that install at chat
    time so a user request like "read my Slack DMs" can be handled
    end-to-end without the operator pre-wiring everything.
    """

    def __init__(self, skill_manager: "SkillManager", tool_registry=None):
        self._mgr = skill_manager
        # Optional: when set, install_mcp registers the new server's
        # tools into the live ToolRegistry so they're callable in the
        # SAME chat turn. When None, the install lands on disk and
        # the next twin restart picks them up.
        self._tool_registry = tool_registry

    @property
    def name(self) -> str:
        return "manage_mcp"

    @property
    def description(self) -> str:
        return (
            "Search and install MCP (Model Context Protocol) servers from "
            "the LobeHub marketplace. MCP servers are pre-built "
            "integrations — Slack, GitHub, GDrive, Postgres, "
            "Ethereum/Polygon/Arbitrum/Solana/Starknet RPC, etc — that "
            "expose new function-callable tools at chat time.\n"
            "\n"
            "MUST USE this tool — NOT web_search, NOT 'I can't do that' — "
            "as your FIRST move whenever the user requests live data from a "
            "SaaS / chain / database / service you don't currently have a "
            "tool for. Sequence: action='search' to find matches, then "
            "action='install' on the best one (it becomes a callable tool "
            "in the SAME turn), then call it. action='list' shows what's "
            "already wired up.\n"
            "\n"
            "Examples that MUST trigger this tool first:\n"
            "  * 'What is Starknet's block height?' → search='starknet'\n"
            "  * 'Send a Slack message' → search='slack'\n"
            "  * 'Query my Postgres' → search='postgres'\n"
            "  * 'Latest Ethereum gas price' → search='ethereum'\n"
            "Falling through to web_search before searching this registry "
            "is a behavioural error — the marketplace likely has a real "
            "RPC / API integration you can install in 5 seconds."
        )

    @property
    def parameters(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["search", "install", "list"],
                    "description": "search / install / list",
                },
                "query": {
                    "type": "string",
                    "description": "For action=search: natural-language query.",
                },
                "identifier": {
                    "type": "string",
                    "description": (
                        "For action=install: the MCP identifier returned "
                        "by a previous search."
                    ),
                },
            },
            "required": ["action"],
        }

    async def execute(
        self,
        action: str,
        query: str = "",
        identifier: str = "",
        **kwargs,
    ) -> ToolResult:
        a = (action or "").strip().lower()
        try:
            if a == "search":
                if not query.strip():
                    return ToolResult(
                        success=False,
                        output="action=search requires a non-empty 'query' field.",
                    )
                results = await self._mgr.search_mcp(query, limit=_MAX_RESULTS)
                if not results:
                    return ToolResult(
                        success=True,
                        output="No MCP servers matched.",
                    )
                slim = [
                    {
                        "identifier": r.get("identifier") or r.get("id") or r.get("url", ""),
                        "name": r.get("name") or r.get("title", ""),
                        "description": _safe_truncate(
                            r.get("description") or r.get("summary", ""), 200,
                        ),
                        "tools": r.get("tools", []),
                    }
                    for r in results
                ]
                return ToolResult(
                    success=True,
                    output=json.dumps({"matches": slim}, ensure_ascii=False),
                )

            if a == "install":
                if not identifier.strip():
                    return ToolResult(
                        success=False,
                        output="action=install requires the 'identifier' field.",
                    )
                result = await self._mgr.install_mcp(
                    identifier, tool_registry=self._tool_registry,
                )
                # install_mcp returns a dict; pass it back so the LLM
                # can read what tools the user just gained.
                return ToolResult(
                    success=bool(result.get("success", True)),
                    output=json.dumps(result, ensure_ascii=False),
                )

            if a == "list":
                # SkillManager doesn't expose installed MCPs separately
                # in v1 — fall back to listing the registry's tool names
                # so the LLM can see what's wired up right now.
                names: list[str] = []
                if self._tool_registry is not None:
                    names = list(getattr(self._tool_registry, "tool_names", []))
                return ToolResult(
                    success=True,
                    output=json.dumps({"available_tools": names}, ensure_ascii=False),
                )

            return ToolResult(
                success=False,
                output=f"Unknown action '{action}'. Valid: search, install, list.",
            )
        except Exception as e:
            logger.warning("manage_mcp %s failed: %s", action, e)
            return ToolResult(
                success=False,
                output=f"manage_mcp action={action} failed: {e}",
            )
