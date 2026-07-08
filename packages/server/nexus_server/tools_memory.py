"""Memory tools — partially superseded by `tools_clinical_graph` (Rev-8).

The ``SearchPastChatsTool`` here stays — it's the FTS-over-event_log
path that complements the new ClinicalGraph entity / encounter search
(per design v3 §6.6 / UX v2 §7.2 — both tools live side by side).

The historical ``SemanticSearchTool`` was deprecated by the per-patient
graph + Tier-1 cached views (Rev-4). New code should prefer:
* Entity-anchored queries → `tools_clinical_graph.SearchNodeTool`
* Temporal / encounter queries → `tools_clinical_graph.SearchEncounterTool`
* Keyword search over raw chat → ``SearchPastChatsTool`` here (kept)

────────────────────────────────────────────────────────────────────────
Original docstring (Phase C-1):

Memory & continuity tools for the agent.

Currently exposes one tool:

* ``search_past_chats`` — RAG-style retrieval over the user's whole
  event log so the agent can find earlier conversations on a topic and
  cite them in the current reply. Inspired by Claude.ai's "search past
  chats" feature, but built on our own append-only event log (which
  already exists for DPM / projection / chain anchoring — we're
  just adding a search index over it).

Design notes
============
* User-scoped: the tool closes over ``user_id``. The underlying
  event_log DB path is computed from ``user_id`` server-side, so a
  malicious LLM can't pivot to another user.
* Excludes the current session by default — the agent doesn't need
  this tool to "find" the message it just received; the chat history
  it already has covers that. Pass ``include_current_session=True``
  to override (rare, but useful when the user asks "earlier today I
  said X — find it").
* Returns ``session_id`` + ``sync_id`` for every hit so the desktop
  client can render a citation chip that links back to the source
  message. The chat UI already keys on sync_id for scroll-to-message.
* Does NOT do semantic / embedding search yet — substring + case-fold
  via SQLite LIKE. Good enough for keyword recall; we'll layer an
  embeddings index on top later if recall feels weak.
"""
from __future__ import annotations

import json
import logging
from typing import Optional

from nexus_core.tools.base import BaseTool, ToolResult

logger = logging.getLogger(__name__)


class SearchPastChatsTool(BaseTool):
    """Find earlier conversations on a topic across all the user's
    sessions. The agent uses this when the user references something
    they discussed before, or when the agent wants to build on prior
    context that isn't in the current session's rolling window."""

    def __init__(self, user_id: str, session_id_getter):
        self._user_id = user_id
        self._session_id_getter = session_id_getter

    @property
    def name(self) -> str:
        return "search_past_chats"

    @property
    def description(self) -> str:
        return (
            "Search the user's history across ALL sessions for messages "
            "AND uploaded files matching a query string. Use this when:\n"
            "  * The user references an earlier discussion ('what did we "
            "    decide about X last week?', 'remember the bug we hit?')\n"
            "  * The user asks about a previously uploaded file ('我之前 "
            "    上传过讲 X 的文章吗', 'find that paper about Y')\n"
            "  * You need context outside this session's rolling window.\n"
            "\n"
            "DO NOT use for:\n"
            "  * Messages already in your current context window — read "
            "    them directly.\n"
            "  * General-knowledge questions (use web_search).\n"
            "  * Live data (chain queries, MCP integrations).\n"
            "\n"
            "Returns ONE JSON object with two arrays:\n"
            "  - chat_hits: past chat messages matching the query, each "
            "with sync_id / session_id / role / timestamp / snippet.\n"
            "  - file_hits: uploaded files whose extracted text matches, "
            "each with file_name / mime / uploaded_at / snippet. Follow "
            "up with read_uploaded_file(name=…) to read the full content "
            "of a hit.\n"
            "Cite matches in your reply with timestamps + file names so "
            "the user knows what you're referring to."
        )

    @property
    def parameters(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": (
                        "Substring to search for. Case-insensitive. Use "
                        "the most distinctive phrase the user is likely "
                        "to have typed (e.g. for 'what did we say about "
                        "the BSC anchor design', search 'BSC anchor' or "
                        "'anchor design', not the whole sentence)."
                    ),
                },
                "limit": {
                    "type": "integer",
                    "description": (
                        "Max number of hits to return. Default 5, hard "
                        "cap 20. Newest matches first."
                    ),
                },
                "include_current_session": {
                    "type": "boolean",
                    "description": (
                        "If true, include hits from the current chat "
                        "session in the results. Default false — most "
                        "of the time the agent has the current session "
                        "in context already and including it just adds "
                        "noise."
                    ),
                },
            },
            "required": ["query"],
        }

    async def execute(
        self,
        query: str = "",
        limit: int = 5,
        include_current_session: bool = False,
        **kwargs,
    ) -> ToolResult:
        q = (query or "").strip()
        if not q:
            return ToolResult(
                success=False, error="`query` is required and cannot be empty.",
            )
        # Clamp the limit so a runaway tool call can't dump the whole
        # event log into the LLM context.
        try:
            n = max(1, min(20, int(limit) if limit else 5))
        except (TypeError, ValueError):
            n = 5

        exclude_sid: Optional[str] = None
        if not include_current_session:
            try:
                exclude_sid = (self._session_id_getter() or "").strip()
            except Exception:  # noqa: BLE001
                exclude_sid = None

        try:
            from nexus_server import twin_event_log
            hits = twin_event_log.search_messages(
                user_id=self._user_id,
                query=q,
                limit=n,
                exclude_session_id=exclude_sid,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "search_past_chats failed for %s: %s", self._user_id, e,
            )
            return ToolResult(success=False, error=f"Search failed: {e}")

        # Memory Fix C: also search uploaded files. Returns parallel hit
        # shape under a different key so the LLM can tell file content
        # from past chat lines.
        file_hits: list[dict] = []
        try:
            from nexus_server import files as _files
            file_hits = _files.search_uploaded_files(
                user_id=self._user_id, query=q, limit=max(1, n // 2),
            )
        except Exception as e:  # noqa: BLE001
            logger.debug("uploaded-file search skipped: %s", e)

        if not hits and not file_hits:
            return ToolResult(
                output=(
                    f"No past messages matched '{q}'. The user may not "
                    f"have discussed this before — answer based on the "
                    f"current session + your projected memory."
                ),
            )

        # Compact JSON; the LLM is smart enough to read this and
        # synthesise a citation in its reply. Memory Fix C unified
        # payload — chat hits + file hits in one envelope so the LLM
        # can reason across both surfaces.
        payload = {
            "chat_hits": hits,
            "file_hits": file_hits,
        }
        return ToolResult(output=json.dumps(payload, indent=2))


class SemanticSearchTool(BaseTool):
    """#137 — vector-space semantic search over chat history + image
    captions + attachment summaries. Complements ``search_past_chats``
    (which does literal substring) by finding things the user means
    rather than the exact words they used.

    Use cases the lexical tool misses but this one catches:

    * Synonyms: user asks 'find the X-ray we discussed', stored
      caption says 'chest radiograph'. LIKE %X-ray% misses it.
    * Cross-language: user uploaded a doc in English, asks about it
      in Chinese. LIKE misses the language boundary; embeddings
      cross it natively.
    * Conceptual: user says 'that thing about token caching' →
      vector finds the chunk that says 'KV cache hit rate' even
      though no words overlap.

    Both tools coexist. Agent picks one or both per query — the
    description below pushes semantic for paraphrased / fuzzy
    queries, lexical for exact identifiers (file_ids, error
    strings, ticker symbols).
    """

    def __init__(self, user_id: str):
        self._user_id = user_id

    @property
    def name(self) -> str:
        return "semantic_search"

    @property
    def description(self) -> str:
        return (
            "Semantic vector search over the user's history — past chat "
            "messages, image captions, and document summaries. USE WHEN:\n"
            "  * The user references something fuzzily ('that medical "
            "    image we looked at', 'the bug we discussed last week') "
            "    and you need to recover the actual content.\n"
            "  * Their phrasing differs from how it would have been "
            "    stored — synonyms, paraphrase, different language.\n"
            "  * You want to find images by their visual content "
            "    (uses image captions): 'find all chest CTs', 'show "
            "    me the trading screenshots'.\n"
            "\n"
            "DO NOT use for exact-string lookups (file_id, ticker, "
            "error code, hash) — those should go through "
            "search_past_chats which uses substring matching.\n"
            "\n"
            "Returns the top-k most similar chunks across all your "
            "history. Each hit includes source_kind ('chat' / "
            "'caption' / 'attachment') so you can tell whether you're "
            "looking at a past message or an image description.\n"
            "\n"
            "If semantic backend is unavailable (no embedding API "
            "key, network down), this tool returns an error — fall "
            "back to search_past_chats."
        )

    @property
    def parameters(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": (
                        "Free-text query. Don't quote-escape; just "
                        "type what you'd say. e.g. 'chest CT showing "
                        "right upper lobe nodule'."
                    ),
                },
                "limit": {
                    "type": "integer",
                    "description": (
                        "Max number of hits to return. Default 8, hard "
                        "cap 20. Closest matches first."
                    ),
                },
                "kinds": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Optional filter — limit to specific source "
                        "kinds. Valid: 'chat', 'caption' (images), "
                        "'attachment' (non-image files), 'skill'. "
                        "Omit to search everything."
                    ),
                },
            },
            "required": ["query"],
        }

    async def execute(
        self,
        query: str = "",
        limit: int = 8,
        kinds: Optional[list] = None,
        **kwargs,
    ) -> ToolResult:
        q = (query or "").strip()
        if not q:
            return ToolResult(
                success=False, error="`query` is required and cannot be empty.",
            )
        try:
            n = max(1, min(20, int(limit) if limit else 8))
        except (TypeError, ValueError):
            n = 8

        valid_kinds = {"chat", "caption", "attachment", "skill", "upload"}
        kind_filter: Optional[list[str]] = None
        if kinds:
            filtered = [k for k in kinds if k in valid_kinds]
            if filtered:
                kind_filter = filtered

        try:
            from nexus_server.vector_index import (
                search_chunks, EmbeddingUnavailable,
            )
            hits = await search_chunks(
                user_id=self._user_id,
                query=q,
                k=n,
                source_kinds=kind_filter,
            )
        except EmbeddingUnavailable as e:
            return ToolResult(
                success=False,
                error=(
                    f"Semantic search unavailable: {e}. Fall back to "
                    "`search_past_chats` for literal text matching."
                ),
            )
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "semantic_search failed for %s: %s", self._user_id, e,
            )
            return ToolResult(
                success=False, error=f"Semantic search failed: {e}",
            )

        if not hits:
            return ToolResult(
                output=(
                    f"No semantically similar content found for {q!r}. "
                    "The user may not have discussed this before, or the "
                    "embedding pipeline hasn't indexed it yet (very recent "
                    "turns can be lag a few seconds). Try "
                    "`search_past_chats` for literal matching."
                ),
            )

        payload = {
            "query": q,
            "kind_filter": kind_filter,
            "hits": [
                {
                    "rank": i + 1,
                    "source_kind": h.source_kind,
                    "source_id": h.source_id,
                    "text": h.text_chunk,
                    "distance": round(h.distance, 4),
                    "created_at_ms": h.created_at_ms,
                }
                for i, h in enumerate(hits)
            ],
        }
        return ToolResult(output=json.dumps(payload, indent=2, ensure_ascii=False))


def register_memory_tools(twin, user_id: str) -> None:
    """Register search_past_chats + semantic_search on the given twin."""
    twin.register_tool(
        SearchPastChatsTool(
            user_id=user_id,
            session_id_getter=lambda: getattr(twin, "_thread_id", "") or "",
        ),
    )
    # #137 — semantic vector search. Registered alongside the lexical
    # search tool so the agent can pick whichever fits the query (or
    # use both for high-recall blends). Independent failure modes:
    # if the embedding backend is down, semantic returns a friendly
    # error and the agent still has lexical search.
    twin.register_tool(SemanticSearchTool(user_id=user_id))
    logger.info("Memory tools registered for user %s", user_id)
