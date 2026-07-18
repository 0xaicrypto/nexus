"""
Nexus — Self-evolving AI avatar powered by Nexus SDK.

Startup flow:
  1. If chain mode (private_key provided):
     a. Connect to BSC via nexus_core.testnet() / nexus_core.mainnet()
     b. Register ERC-8004 identity (one-time, auto-detected)
      c. All data persists locally
  2. If local mode (no private_key):
     a. Use nexus_core.local()
     b. All data persists to local files
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

import nexus_core
from nexus_core import AgentRuntime, Checkpoint, LLMClient, LLMProvider, ThinkingEmitter

from .config import TwinConfig
from .evolution.engine import EvolutionEngine
from .tools.base import ExtendedToolRegistry

logger = logging.getLogger(__name__)

class DigitalTwin:
    """
    Nexus: A self-evolving digital avatar with persistent memory on BNB Chain.

    Usage:
        # Local mode (dev)
        twin = await DigitalTwin.create("my-twin", llm_api_key="AIza...")

        # Chain mode (production — registers ERC-8004 on first startup)
        twin = await DigitalTwin.create(
            "my-twin",
            llm_api_key="AIza...",
            private_key="0x...",
            network="testnet",
        )

        response = await twin.chat("Hello, remember I like sushi")
        await twin.close()
    """

    def __init__(self, config: TwinConfig, rune: AgentRuntime, llm: LLMClient):
        self.config = config
        self.rune = rune
        self.llm = llm

        self._thread_id: str = ""
        self._messages: list[dict] = []
        # #127 — vision parts for the current turn, surfaced so sub-agents
        # invoked via the delegate tool can re-attach them to their own
        # one-shot call. Set at the top of chat(), cleared in its
        # finally block. None outside an active turn.
        self._active_turn_images: list[dict] | None = None
        self._turn_count: int = 0

        # Live thinking telemetry — pub/sub fired during chat() so the
        # server's SSE endpoint can stream the agent's reasoning to
        # the desktop in real time. Always present (no subscribers ⇒
        # emit is a no-op), so call sites don't need null checks.
        self.thinking = ThinkingEmitter()

        # Tool registry — tools are registered after creation via register_tool()
        self.tools = ExtendedToolRegistry()
        self._file_reader = None  # Set by _register_default_tools

        # Skill manager — external skills (Binance Skills Hub compatible)
        from nexus_core.skills import SkillManager
        self.skills = SkillManager(base_dir=config.base_dir)

        # DPM: Event log (append-only, SDK layer) + Projection (Nexus layer)
        from nexus_core.memory import (
            CuratedMemory,
            Episode,
            EpisodesStore,
            EventLog,
            FactsStore,
            KnowledgeStore,
            PersonaStore,
            SkillsStore,
        )
        # Stash the dataclass on the instance so _upsert_active_episode
        # can use it without re-importing every turn.
        self._Episode = Episode
        self.event_log = EventLog(base_dir=config.base_dir, agent_id=config.agent_id)
        self.curated_memory = CuratedMemory(base_dir=config.base_dir)

        # Phase J: 5-namespace memory taxonomy. Each store is independently
        # versioned (VersionedStore under the hood) so the falsifiable-
        # evolution verdict scorer can roll back individual namespaces
        # without touching the rest.
        #
        self.episodes = EpisodesStore(base_dir=config.base_dir)
        self.facts = FactsStore(base_dir=config.base_dir)
        self.skills_memory = SkillsStore(base_dir=config.base_dir)
        self.persona_store = PersonaStore(base_dir=config.base_dir)
        self.knowledge = KnowledgeStore(base_dir=config.base_dir)

        # Compactor initialized after LLM (needs projection_fn)
        self._compactor = None

        # ABC: Contract enforcement engine
        from nexus_core.contracts import ContractEngine, ContractSpec, DriftScore
        contract_path = Path(config.base_dir) / "contracts" / "system.yaml"
        user_rules_path = Path(config.base_dir) / "contracts" / "user_rules.json"
        self._contract_spec = ContractSpec.from_yaml(contract_path) if contract_path.exists() else ContractSpec()
        self._contract_spec.load_user_rules(user_rules_path)
        self.contract = ContractEngine(self._contract_spec, event_log=self.event_log)
        self.drift = DriftScore(
            compliance_weight=self._contract_spec.compliance_weight,
            distributional_weight=self._contract_spec.distributional_weight,
            warning_threshold=self._contract_spec.warning_threshold,
            intervention_threshold=self._contract_spec.intervention_threshold,
            observation_window=self._contract_spec.observation_window,
        )
        self._user_rules_path = user_rules_path

        # Projection initialized after LLM is ready (in _initialize)
        self._projection = None

        # Event callback for on-chain activity notifications
        # Signature: on_event(event_type: str, detail: dict) -> None
        self.on_event: Optional[Callable[[str, dict], None]] = None

        self.evolution = EvolutionEngine(
            rune=rune,
            agent_id=config.agent_id,
            llm_fn=llm.complete,
            default_persona=config.base_persona,
            agent_name=config.name,
            # Phase J: hand the FactsStore to MemoryEvolver so each
            # extracted memory dual-writes a typed Fact.
            facts_store=self.facts,
            # Phase O.2: hand the EventLog so each evolver run emits
            # an evolution_proposal before its writes — verdict scoring
            # (Phase O.4) reads back from the same log.
            event_log=self.event_log,
            persona_store=self.persona_store,
            skills_store=self.skills_memory,
            knowledge_store=self.knowledge,
        )
        self._initialized = False
        self._bg_tasks: set[asyncio.Task] = set()

    @classmethod
    async def create(
        cls,
        name: str = "Twin",
        owner: str = "",
        agent_id: str = "digital-twin",
        llm_provider: str = "gemini",
        llm_api_key: str = "",
        llm_model: str = "",
        base_dir: str = ".nexus",
        # ── Tools ──
        enable_tools: bool = True,
        tavily_api_key: str = "",
        jina_api_key: str = "",
    ) -> "DigitalTwin":
        provider = LLMProvider(llm_provider)

        config = TwinConfig(
            agent_id=agent_id,
            name=name,
            owner=owner,
            llm_provider=provider,
            llm_api_key=llm_api_key,
            llm_model=llm_model or "",
            base_dir=base_dir,
        )

        rune = nexus_core.local(base_dir=base_dir)
        logger.info("Local mode: data stored in %s", base_dir)

        llm = LLMClient(
            provider=config.llm_provider,
            api_key=config.llm_api_key,
            model=config.llm_model,
        )

        twin = cls(config=config, rune=rune, llm=llm)

        # ── Register default tools ──
        if enable_tools:
            twin._register_default_tools(
                tavily_api_key=tavily_api_key,
                jina_api_key=jina_api_key,
            )
            # Tell SkillEvolver to never learn skills that share names with
            # registered tools — prevents the name collision bug where the
            # LLM role-plays tool use instead of making actual function calls.
            twin.evolution.skills._blocked_names = set(twin.tools.tool_names)

        await twin._initialize()
        return twin

    async def _initialize(self):
        if self._initialized:
            return

        # ── Step 1: Initialize evolution engine (persona + skills + knowledge + memory) ──
        # Memory preloading is included in initialize() to avoid cold-start timeouts
        # during the first chat() call. Cold-start loads can be slow, so we give
        # generous time here (10s). If it still times out, memories will
        # lazy-load on next access (slightly delayed first response).
        try:
            await asyncio.wait_for(self.evolution.initialize(), timeout=10.0)
        except asyncio.TimeoutError:
            logger.info("Evolution init timed out (10s) — loading in background with defaults")
            self._bg_task("evolution-init", self.evolution.initialize())

        # ── Step 2b: Clean up any learned skills that conflict with tools ──
        # This handles the case where a previous session learned "web_search" etc.
        # as a text skill before the tool was registered.
        if self.tools:
            for tool_name in self.tools.tool_names:
                if tool_name in self.evolution.skills._skills_cache:
                    logger.info(
                        "Removing conflicting learned skill '%s' (shadows registered tool)",
                        tool_name,
                    )
                    del self.evolution.skills._skills_cache[tool_name]
                    self.evolution.skills._dirty = True

        # ── Step 3: Restore last session ──
        # Try loading from cache (instant). If the backend is slow, start fresh
        # and recover old session knowledge in background.
        session_restored = False
        try:
            checkpoints = await asyncio.wait_for(
                self.rune.sessions.list_checkpoints(
                    agent_id=self.config.agent_id, limit=1,
                ),
                timeout=10.0,  # generous: cold-start backend read
            )
        except asyncio.TimeoutError:
            logger.info("Session restore timed out (10s) — starting fresh, recovering in background")
            checkpoints = []
            # Background: load old session and extract memories from it
            # (don't overwrite current messages — just mine the knowledge)
            self._bg_task("session-recovery", self._recover_old_session())

        if checkpoints:
            last = checkpoints[0]
            self._thread_id = last.thread_id
            self._messages = last.state.get("messages", [])
            self._turn_count = last.state.get("turn_count", 0)
            session_restored = True
            logger.info(
                "Resumed session %s (%d messages)",
                self._thread_id, len(self._messages),
            )

        if not session_restored:
            self._thread_id = f"session_{uuid.uuid4().hex[:8]}"
            self._messages = []
            self._turn_count = 0

        # Initialize DPM projection + compactor.
        # Phase P: chat_projection_mode in TwinConfig switches between
        # the canonical single-call DPM projection and the
        # RLM-style runtime navigation. Default stays single_call —
        # rlm is opt-in until dogfooding signs off.
        from nexus_core.memory import EventLogCompactor
        from nexus_core.rlm import RLMConfig

        from .evolution.projection import ProjectionMemory

        proj_mode = getattr(self.config, "chat_projection_mode", "single_call")
        proj_kwargs: dict = {"mode": proj_mode}
        if proj_mode == "rlm":
            # In RLM mode, the same LLMClient drives both root and
            # sub calls by default — which is suboptimal per the
            # paper (cheaper sub-LLM is the cost win) but works
            # without requiring callers to wire a second client.
            # When TwinConfig grows a `sub_llm_client` field this
            # branch will use it instead.
            proj_kwargs["sub_llm_fn"] = lambda q: self.llm.complete(q, temperature=0.0)
            proj_kwargs["rlm_config"] = RLMConfig(
                max_iterations=getattr(self.config, "rlm_max_iterations", 8),
                max_sub_calls=getattr(self.config, "rlm_max_sub_calls", 15),
                timeout_seconds=getattr(self.config, "rlm_timeout_seconds", 30.0),
            )
            proj_kwargs["fastpath_char_threshold"] = getattr(
                self.config, "rlm_fastpath_char_threshold", 16_000,
            )

        self._projection = ProjectionMemory(
            self.event_log, self.llm.complete, **proj_kwargs,
        )
        self._compactor = EventLogCompactor(
            self.event_log, self.curated_memory,
            projection_fn=self._projection.project,
        )

        # Phase O.5: VerdictRunner consumes the proposal events emitted
        # by Phase O.2 evolvers and scores them after their windows
        # elapse. Wire in the namespace stores so a "reverted" decision
        # can rollback the actual on-disk state, and the drift score so
        # high-drift windows trigger revert even without explicit
        # contract violations.
        from .evolution.verdict_runner import VerdictRunner
        self.verdict_runner = VerdictRunner(
            event_log=self.event_log,
            stores={
                "memory.persona": self.persona_store,
                "memory.facts": self.facts,
                "memory.episodes": self.episodes,
                "memory.skills": self.skills_memory,
                "memory.knowledge": self.knowledge,
            },
            # Phase D removed rollback_handlers. The legacy artifacts
            # that needed re-syncing (persona.json,
            # skills_registry.json, knowledge_articles.json) are gone
            # — typed store rollback is now the complete rollback,
            # because chat-time projections rebuild from the typed
            # store on every load.
            drift=self.drift,
            thresholds=None,  # use BEP-Nexus defaults
        )

        backend = getattr(self.rune, "_backend", None)

        # Phase A2: hook the ThinkingEmitter to the EventLog (and
        # backend writer when active) so
        # every step the agent reasons about now flows into the
        # audit trail.
        # Pre-existing in-process SSE delivery is preserved — the
        # double-write is additive.
        try:
            blob_writer = None
            if backend is not None and hasattr(backend, "store_blob"):
                blob_writer = backend.store_blob
            self.thinking.attach(
                event_log=self.event_log,
                blob_writer=blob_writer,
            )
            logger.info(
                "ThinkingEmitter attached: event_log=%s, blob_writer=%s",
                "yes", "yes" if blob_writer else "no (local mode)",
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("ThinkingEmitter.attach failed: %s", e)

        # Cold-start chain recovery (Phase A+ task #155): if local
        # EventLog SQLite is empty AND there's a snapshot on chain,
        # replay it. This is the "永生 (immortality)" guarantee:
        # a fresh server / migrated VM / wiped disk doesn't lose the
        # agent's history — the chain copy is the source of truth.
        # Best-effort: any failure leaves the local EventLog as-is
        # (empty for a brand-new agent, untouched for a legitimate
        # cold start that just hasn't snapshotted yet).
        if backend is not None and self.event_log.count() == 0:
            try:
                restored = await self.event_log.recover_from(backend)
                if restored:
                    logger.info(
                        "Chain recovery: restored %d events from "
                        "backend snapshot",
                        restored,
                    )
            except Exception as e:  # noqa: BLE001
                logger.debug("Chain recovery skipped: %s", e)

        self._initialized = True

    # ── Tool Management ──────────────────────────────────────────

    def _register_default_tools(
        self, tavily_api_key: str = "", jina_api_key: str = "",
    ) -> None:
        """Register the default tool set (web search + URL reader).

        Tools are only registered if their dependencies (httpx) are available.
        Missing dependencies are logged but don't prevent startup.
        """
        try:
            from nexus_core.tools.url_reader import URLReaderTool
            from nexus_core.tools.web_search import WebSearchTool

            tavily_key = tavily_api_key or os.environ.get("TAVILY_API_KEY", "")
            jina_key = jina_api_key or os.environ.get("JINA_API_KEY", "")

            self.tools.register(WebSearchTool(api_key=tavily_key))
            self.tools.register(URLReaderTool(api_key=jina_key))

            # File generator — agent can create files for user download
            from nexus_core.tools import FileGeneratorTool
            output_dir = Path(self.config.base_dir).parent / "outputs"
            output_dir.mkdir(parents=True, exist_ok=True)
            self.tools.register(FileGeneratorTool(output_dir=output_dir))

            # File reader — agent reads uploaded file content on demand
            from nexus_core.tools import ReadUploadedFileTool
            self._file_reader = ReadUploadedFileTool()
            self.tools.register(self._file_reader)

            # Self-evolution: agent can search + install new skills /
            # MCP servers at chat time. The persona prompt has long
            # advertised this capability; until now no actual tools
            # were registered, so the LLM apologetically claimed it
            # could not install. SkillInstallerTool / McpInstallerTool
            # wrap the existing SkillManager API as function-calling
            # entries.
            try:
                from nexus_core.tools import McpInstallerTool, SkillInstallerTool
                self.tools.register(SkillInstallerTool(self.skills))
                self.tools.register(
                    McpInstallerTool(self.skills, tool_registry=self.tools),
                )
            except Exception as e:
                logger.debug("Skill installer tools skipped: %s", e)

            # Direct BSC chain queries — block height, balances, tx
            # receipts. Bypasses web_search for live chain data so the
            # LLM stops mistaking Bitcoin's block height for BSC's.
            # Read-only by design (sending txs belongs to the chain
            # backend, not an LLM-callable surface).
            try:
                from nexus_core.tools import BscQueryTool, ChainQueryTool
                self.tools.register(BscQueryTool())
                # Multi-chain EVM RPC: Ethereum / Polygon / Arbitrum /
                # Optimism / Base. Companion to bsc_query.
                self.tools.register(ChainQueryTool())
            except Exception as e:
                logger.debug("Chain query tools skipped: %s", e)

            logger.info(
                "Registered %d tools: %s",
                len(self.tools), ", ".join(self.tools.tool_names),
            )
        except Exception as e:
            logger.debug("Default tool registration skipped: %s", e)

    def register_tool(self, tool) -> None:
        """Register a custom tool.

        Args:
            tool: A BaseTool instance to register. Must have name, description,
                  parameters, and execute() method.

        Example:
            from nexus_core.tools import BaseTool, ToolResult

            class MyTool(BaseTool):
                name = "my_tool"
                description = "Does something custom"
                parameters = {"type": "object", "properties": {}, "required": []}

                async def execute(self, **kwargs):
                    return ToolResult(output="done")

            twin.register_tool(MyTool())
        """
        self.tools.register(tool)

    # ── Background task helpers ──────────────────────────────────

    def _bg_task(self, label: str, coro) -> None:
        """Fire-and-forget a coroutine as a tracked background task.

        The coroutine is wrapped in a safety wrapper that catches CancelledError
        and exceptions. If no event loop is available, the coroutine is explicitly
        closed to avoid 'was never awaited' warnings.
        """
        async def _safe():
            try:
                await coro
            except asyncio.CancelledError:
                logger.debug("[%s] cancelled (shutdown)", label)
            except Exception as e:
                logger.warning("[%s] failed: %s", label, e)

        try:
            loop = asyncio.get_running_loop()
            task = loop.create_task(_safe())
            self._bg_tasks.add(task)
            task.add_done_callback(self._bg_tasks.discard)
        except RuntimeError:
            # No event loop — explicitly close the coroutine to prevent
            # "coroutine was never awaited" warnings from Python GC
            coro.close()
            logger.debug("No event loop for background task %s", label)

    def _enqueue_vector_embed(
        self,
        user_id: str,
        user_msg_idx: int,
        user_msg_text: str,
        assistant_msg_idx: int,
        assistant_msg_text: str,
        distilled: list[dict],
    ) -> None:
        """#136 — fire-and-forget this turn's chunks to the vector index.

        The actual work happens in a background task spawned via
        :meth:`_bg_task` so the chat response never blocks on the
        embedding API. If the server-side vector_index module isn't
        available (running in pure-SDK mode without nexus_server),
        we no-op silently — embedding is a layer above the SDK.

        Per-chunk routing:
          * ``user_message`` / ``assistant_response`` →
            source_kind="chat", source_id=f"event-{idx}". Lets
            semantic_search filter by kind when the caller only
            wants chat hits, not file content.
          * ``attachment_distilled`` → source_kind based on MIME:
            "caption" for image/* (so medical-imaging callers can
            filter scope to images only), "attachment" otherwise.
            source_id is the file_id when available — stable across
            sessions so re-uploading the same file via sha256-dedupe
            doesn't double-index.
        """
        # Lazy import — vector_index lives in nexus_server, not the SDK.
        # The SDK is published independently of the server; a SDK-only
        # consumer (e.g. a downstream agent) won't have it. ImportError
        # is the no-op signal.
        try:
            from nexus_server.vector_index import upsert_chunks
        except ImportError:
            return

        async def _embed_all():
            # Each upsert is independent — one failing shouldn't take
            # the others down. Errors are EmbeddingUnavailable when
            # the API is unreachable; demoted to debug since the
            # chat already succeeded.
            jobs: list[tuple[str, str, str]] = []
            if user_msg_text:
                jobs.append((
                    "chat", f"event-{user_msg_idx}", user_msg_text,
                ))
            if assistant_msg_text:
                jobs.append((
                    "chat", f"event-{assistant_msg_idx}", assistant_msg_text,
                ))
            for d in distilled:
                summary = d.get("summary") or ""
                if not summary:
                    continue
                mime = d.get("mime", "") or ""
                kind = "caption" if mime.startswith("image/") else "attachment"
                # Stable source_id: prefer file_id (cross-session
                # dedup); fall back to filename when no upload route
                # was used (legacy inline path).
                source_id = d.get("file_id") or d.get("name") or "anon"
                jobs.append((kind, source_id, summary))

            for kind, source_id, text in jobs:
                try:
                    await upsert_chunks(
                        user_id=user_id,
                        source_kind=kind,
                        source_id=source_id,
                        text=text,
                    )
                except Exception as e:  # noqa: BLE001
                    logger.debug(
                        "vector embed failed for %s/%s: %s",
                        kind, source_id, e,
                    )

        self._bg_task("vector_embed_turn", _embed_all())

    async def _recover_old_session(self) -> None:
        """
        Background: load old session from the backend and extract memories.

        Does NOT overwrite current _messages — the user is already chatting
        in a new session. Instead, feeds old messages to the evolution engine
        so the knowledge isn't lost.

        Total timeout: 60s (if the backend is completely stuck, don't block forever).
        """
        try:
            # list_checkpoints is in-memory only, but the data might need
            # loading from backend — wrap everything in a generous timeout
            checkpoints = await asyncio.wait_for(
                self.rune.sessions.list_checkpoints(
                    agent_id=self.config.agent_id, limit=1,
                ),
                timeout=30.0,
            )
            if not checkpoints:
                logger.debug("No old session found for recovery")
                return

            old = checkpoints[0]
            old_messages = old.state.get("messages", [])
            if not old_messages:
                return

            logger.info(
                "Recovered old session %s (%d messages) — extracting memories",
                old.thread_id, len(old_messages),
            )

            # Feed old conversation to evolution engine for memory extraction
            try:
                await asyncio.wait_for(
                    self.evolution.after_conversation_turn(
                        old_messages,
                        max_memories=8,
                    ),
                    timeout=30.0,
                )
                self._emit("memory_stored", {
                    "count": len(old_messages),
                    "items": [f"Recovered from previous session ({old.thread_id})"],
                    "storage": "local",
                })
            except asyncio.TimeoutError:
                logger.warning("Memory extraction from recovered session timed out")
            except Exception as e:
                logger.warning("Memory extraction from recovered session failed: %s", e)

        except asyncio.TimeoutError:
            logger.warning("Session recovery timed out (30s)")
        except Exception as e:
            logger.warning("Background session recovery failed: %s", e)

    # ── Event System ───────────────────────────────────────────

    def _emit(self, event_type: str, detail: dict = None):
        """Emit an on-chain activity event for CLI display."""
        if self.on_event:
            try:
                self.on_event(event_type, detail or {})
            except Exception as e:
                logger.debug("event callback failed: %s", e)  # never let event callbacks break the main flow

    # ── Identity Context for LLM ───────────────────────────────

    def _build_identity_context(self) -> str:
        """Build identity context for the system prompt."""
        parts = ["## Your Identity"]
        parts.append(f"- Agent Name: {self.config.name}")
        parts.append(f"- Agent ID: {self.config.agent_id}")
        parts.append("- Storage: Local")

        return "\n".join(parts)

    # ── Chat ─────────────────────────────────────────────────────

    async def chat(
        self,
        user_message: str,
        session_id: Optional[str] = None,
        attachment_chips: str = "",
        attachments_meta: Optional[list[dict]] = None,
        folded_user_message: Optional[str] = None,
        images: Optional[list[dict]] = None,
        referenced_file_ids: Optional[list[str]] = None,
        distilled_attachments: Optional[list[dict]] = None,
    ) -> str:
        """Run one chat turn.

        ``user_message`` is the raw text the user typed. This is what
        we PERSIST to event_log — clean, no inline file content. If
        ``attachment_chips`` is provided ("📎 paper.pdf, deck.pptx"),
        we prepend it to the persisted text so the chat history shows
        which files were attached.

        ``folded_user_message`` is the LLM-context view: the same text
        plus distilled summaries of each attachment folded in
        ([Attachments]\\n--- file ---\\n<summary>\\n--- end ---). This
        version goes to the LLM ONLY for this turn — we don't keep it
        in ``_messages`` long-term, otherwise every subsequent turn
        would re-send the attachment summaries (token leak that used
        to be the previous behaviour). After the LLM call, the entry
        in ``_messages`` is rewritten back to the persisted form.

        ``session_id`` (optional, multi-thread support):
          * ``None`` (default) — chat continues in the twin's current
            ``_thread_id``. Backward-compatible behaviour: callers that
            never knew about session ids (tests, old paths) keep working.
          * a string id — if it differs from the current ``_thread_id``,
            twin saves a checkpoint of the current thread, switches its
            in-memory state to the new thread (loading recent messages
            from the event_log filtered by that session_id so the LLM
            sees the right context), then proceeds with the chat.
        """
        if not self._initialized:
            await self._initialize()

        # #127 — stash the current turn's images so the delegate tool
        # (if the agent calls it) can re-attach them to the sub-agent's
        # call. Setting unconditionally is safe: subsequent turns just
        # overwrite, and tools only ever read this during the same
        # turn that's currently executing.
        self._active_turn_images = list(images) if images else None

        # ── Session switch (multi-thread) ─────────────────────────────
        # Fast path: caller didn't override or asked for the same
        # session we're already in → no work, no extra LLM call, no
        # disk I/O. The session-switching path saves a checkpoint of
        # the outgoing thread first so we can resume it later if the
        # user comes back.
        if session_id is not None and session_id != self._thread_id:
            await self._switch_session(session_id)

        # Live thinking telemetry — open a new turn so the desktop
        # can group every step that follows under one "Turn N" card.
        # Pass the active session_id so the emitter's per-session
        # turn counter advances correctly: the desktop renders
        # session_turn_id (1, 2, 3 of THIS conversation) rather than
        # the twin-global turn_id (which keeps climbing across
        # session switches and confuses users). Both ids ride along
        # on each event for audit reference.
        turn_start_wall = time.time()
        self.thinking.start_turn(session_id=self._thread_id)
        self.thinking.emit(
            "heard", "Heard the user",
            content=user_message[:200],
            metadata={"length_chars": len(user_message)},
        )

        cmd_result = await self._handle_command(user_message)
        if cmd_result is not None:
            return cmd_result

        # ABC: Pre-check (hard governance)
        pre = self.contract.pre_check(user_message)
        if pre.blocked:
            self.thinking.emit(
                "insight", "Contract pre-check blocked the turn",
                content=pre.reason or "",
                metadata={"phase": "pre_check"},
            )
            return f"[Contract violation] {pre.reason}"

        # DPM: Append user message to event log.
        #
        # Persistence shape (Phase Q v2):
        #   * ``content``  = BARE user text only — what the user typed,
        #     no chip prefix, no attachment bodies. Reload renders this
        #     verbatim in the bubble.
        #   * ``metadata`` = structured attachment list when present.
        #     The desktop reads ``metadata.attachments`` from the
        #     /agent/messages endpoint and renders chips on top of the
        #     message bubble — proper UI, not fallback text.
        #
        # The LLM-context view (``_messages``) still uses the chip
        # prefix below so the model has natural-language context that
        # an attachment WAS sent in this turn (helps it remember
        # "earlier the user attached paper.pdf" in subsequent turns).
        persisted_user_msg = user_message
        persisted_meta = (
            {"attachments": attachments_meta} if attachments_meta else None
        )
        user_msg_idx = self.event_log.append(
            "user_message",
            persisted_user_msg,
            session_id=self._thread_id,
            metadata=persisted_meta,
        )

        # #136 — write attachment_distilled events to the event log
        # so Memory Fix B (memory_evolver scans this event_type) and
        # Memory Fix C (search_past_chats lexical hits) actually have
        # something to consume. Previously the summaries were returned
        # to the desktop client and dropped — the Phase Q persistence
        # path was killed in Phase B without a replacement. We add
        # them here, between user_message and assistant_response, so
        # the chronological story is "user attached → file got
        # distilled → assistant replied".
        distilled_idx_by_source_id: dict[str, int] = {}
        if distilled_attachments:
            for d in distilled_attachments:
                d_summary = d.get("summary") or ""
                if not d_summary:
                    continue
                d_meta = {
                    "name": d.get("name", ""),
                    "mime": d.get("mime", ""),
                    "size_bytes": d.get("size_bytes", 0),
                    "source": d.get("source", ""),
                    "file_id": d.get("file_id", ""),
                }
                d_idx = self.event_log.append(
                    "attachment_distilled",
                    d_summary,
                    session_id=self._thread_id,
                    metadata=d_meta,
                )
                # Index by file_id when available (stable across
                # sessions) so the embed step below can use it as the
                # vector index's source_id. Fall back to event idx as
                # the source_id string when no file_id exists.
                key = d.get("file_id") or f"event-{d_idx}"
                distilled_idx_by_source_id[key] = d_idx

        # Build the chip+text version used for LLM context only.
        chip_prefixed_user_msg = user_message
        if attachment_chips:
            chip_prefixed_user_msg = (
                f"{attachment_chips}\n\n{user_message}"
                if user_message else attachment_chips
            )

        # DPM Smart Context Strategy (inspired by Claude Cowork compact):
        #
        # Trigger      │ Context source          │ Extra LLM calls
        # ─────────────┼─────────────────────────┼─────────────────
        # <10 events   │ _messages (full history) │ 0
        # 10-50 events │ CuratedMemory snapshot   │ 0
        # >50 events   │ CuratedMemory snapshot   │ 0
        # Recall ask   │ Projection (EventLog)    │ 1
        # Auto-compact │ Projection → CuratedMem  │ 1 (background)
        #
        # Auto-compact: when event log exceeds COMPACT_THRESHOLD chars,
        # do a background projection and update CuratedMemory.
        # Similar to Cowork's "compact_boundary" at token limits.
        COMPACT_THRESHOLD = 30000  # chars in event log before auto-compact
        COMPACT_INTERVAL = 20     # minimum turns between compacts

        event_count = self.event_log.count()
        evo_context = ""

        # Check if user is explicitly asking for recall
        recall_keywords = ["之前聊", "聊过什么", "记得", "remember", "recall", "previous", "earlier", "last time", "上次"]
        needs_recall = any(kw in user_message.lower() for kw in recall_keywords)

        if needs_recall and self._projection and event_count > 5:
            # Explicit recall — do projection (1 LLM call, synchronous)
            self.thinking.emit(
                "memory_recall", "Projecting from event log",
                content=f"User asked for recall — running RLM projection over {event_count} events",
                metadata={"strategy": "rlm_projection", "event_count": event_count},
            )
            recall_t0 = time.time()
            try:
                evo_context = await asyncio.wait_for(
                    self._projection.project(user_message, budget=2000),
                    timeout=8.0,
                )
            except asyncio.TimeoutError:
                logger.warning("Projection timed out (8s)")
                evo_context = ""
            self.thinking.emit(
                "memory_recall", "Memory recall finished",
                content=(evo_context[:200] + "…") if len(evo_context) > 200 else evo_context,
                metadata={"context_chars": len(evo_context)},
                duration_ms=int((time.time() - recall_t0) * 1000),
            )
        elif event_count > 10:
            # Medium+ session — use curated memory snapshot (0ms)
            evo_context = self.curated_memory.get_prompt_context()
            if evo_context:
                self.thinking.emit(
                    "memory_recall", "Loaded curated memory",
                    content=(evo_context[:160] + "…") if len(evo_context) > 160 else evo_context,
                    metadata={
                        "strategy": "curated_snapshot",
                        "context_chars": len(evo_context),
                    },
                )

        # Auto-compact: delegate threshold check to SDK's EventLogCompactor
        if self._compactor and self._compactor.should_compact(self._turn_count):
            logger.info("Auto-compact triggered at turn %d", self._turn_count)
            self._bg_task("auto-compact", self._auto_compact())
        # else: short session — LLM sees full _messages history, no extra context needed

        registered_tool_names = set(self.tools.tool_names) if self.tools else set()

        persona = self.evolution.get_current_persona()
        system = persona

        # Inject current date/time
        from datetime import datetime
        system += f"\n\n## Current Date\nToday is {datetime.now().strftime('%B %d, %Y')} ({datetime.now().strftime('%A')})."

        # Inject capability awareness — tell the agent what it CAN do
        # AND which function-calling tool to invoke for each. Avoids
        # the past failure mode where the agent said "I'll install a
        # skill" without realising that, until Phase Q, no install
        # tool was registered — so it would later apologise for not
        # being able to follow through. Now those tools (manage_skill /
        # manage_mcp) ARE registered, and we name them explicitly.
        capabilities = [
            "You have access to web search, URL reading, and file generation tools via function calling.",
            "You can generate files (HTML, markdown, CSV, JSON) using the generate_file tool. "
            "For documents like reports or articles, generate a styled HTML file. "
            "The user can view and download generated files directly in the chat.",
            "You can search and install new Anthropic-style skills via the manage_skill tool. "
            "It queries FOUR marketplaces by default with synonym expansion (e.g. 'pdf' is "
            "auto-expanded to 'pdf, pdf reader, pdf extract, pypdf, document parser'): "
            "(1) anthropics/skills — canonical Anthropic catalog, includes pdf/docx/xlsx/pptx/"
            "skill-creator. (2) google-gemini/gemini-skills — Google's official skills. "
            "(3) LobeHub community catalog (~100K skills, via npx CLI). "
            "(4) GitHub claude-skills topic — third-party skills tagged with that topic. "
            "manage_skill(action='search', query='...') returns interleaved matches with a "
            "'source' field on each row. Anthropic + Gemini results are prioritised at the "
            "top because they're highest-signal canonical sources. "
            "manage_skill(action='install', identifier='...') adds the chosen one. Identifier "
            "formats: 'anthropic:<name>' (e.g. 'anthropic:pdf'), 'gemini:<name>', LobeHub bare "
            "slugs, or full https://github.com/... URLs (multi-skill repos are auto-detected and "
            "the install path drills into the right subfolder). "
            "Use this when the user asks for a capability you don't already have "
            "(reading PDFs, building presentations, rendering diagrams, etc).",
            "You can search and install MCP (Model Context Protocol) servers via the manage_mcp tool: "
            "manage_mcp(action='search', query='...') finds servers in the LobeHub MCP Marketplace, "
            "manage_mcp(action='install', identifier='...') adds one. MCP servers expose new "
            "backend integrations (Slack, GitHub, GDrive, databases, …) as additional tools you can call.",
            "Always actually CALL manage_skill / manage_mcp via function calling — never just claim "
            "you've installed something. If install fails, surface the error verbatim and offer alternatives.",
            "CRITICAL — CAPABILITY-GAP REFLEX: when the user asks for something you don't currently "
            "have a tool for, your FIRST move (NOT web_search, NOT 'I don't have that ability') MUST "
            "be to search the skill + MCP marketplaces:\n"
            "  1. manage_mcp(action='search', query='<topic>')\n"
            "  2. manage_skill(action='search', query='<topic>')\n"
            "If a relevant entry exists, install it (action='install', identifier='...') and use it "
            "in the same turn. The whole point of this agent is that you can grow your own toolset "
            "at chat time without anyone redeploying code. Saying 'I'll continue to optimise my "
            "abilities in the future' is WRONG — the ability to grow is RIGHT NOW, via these tools, "
            "in this turn. web_search is a LAST resort, not a first.\n"
            "Concrete examples where this reflex MUST fire:\n"
            "  * 'What's the Starknet/Ethereum/Polygon/Arbitrum/Solana block height?' "
            "→ manage_mcp(search, '<chain>') first. (BSC is the one exception — use bsc_query.)\n"
            "  * 'Send a Slack message to #general' → manage_mcp(search, 'slack')\n"
            "  * 'Query my Postgres database' → manage_mcp(search, 'postgres')\n"
            "  * 'Render this CSV as a chart' → manage_skill(search, 'chart')\n"
            "  * 'Translate to French' → manage_skill(search, 'translate')\n"
            "  * 'Generate a QR code' → manage_skill(search, 'qrcode')\n"
            "If both searches return nothing usable, THEN web_search; if web_search also doesn't "
            "help, THEN politely admit you can't do it. NEVER skip steps 1-2.",
            "CRITICAL — chain queries: pick the right RPC tool by chain.\n"
            "  * BSC (Binance Smart Chain) → bsc_query. Actions: block_number, "
            "balance, tx_receipt, block, code. Default network=mainnet.\n"
            "  * Ethereum / Polygon / Arbitrum / Optimism / Base → chain_query "
            "with network='ethereum'/'polygon'/'arbitrum'/'optimism'/'base'. "
            "Same actions as bsc_query.\n"
            "  * Starknet / Solana / non-EVM → manage_mcp(action='search', "
            "query='<chain>'); the curated catalog has mcp-server-starknet, "
            "mcp-server-solana, etc. Install + use.\n"
            "NEVER use web_search for any of these. Search engines confuse "
            "chains (return Bitcoin's height for BSC, return ETH gas for "
            "Polygon, etc) and scrape stale block-explorer pages. "
            "Authoritative chain data only comes from RPC.",
            "CRITICAL — using an installed skill: the system prompt only carries each skill's name "
            "and an 80-character blurb, NOT its operations. When the user asks you to do something a "
            "skill covers (e.g. 'analyze this PDF', 'render a chart', 'summarise the deck'), your "
            "FIRST step must be manage_skill(action='show', name='<skill_name>') to load the full "
            "SKILL.md instructions. Only after reading those instructions can you confidently say "
            "what the skill can do. Do not answer 'this skill doesn't have an X function' until "
            "you've actually called show and read the operations.",
            "CRITICAL — how attachments arrive: when the user uploads a file in the SAME turn, "
            "the user message you see contains a `[Attachments]` block at the top with a "
            "DISTILLED summary of every file (filename, mime, size, key points). That summary IS "
            "the file's content for this turn — treat it as if the user pasted those bullets "
            "directly. NEVER reply 'please upload the file' when an `[Attachments]` block is "
            "present; it means the file IS attached and you're already looking at it. If you "
            "need MORE detail than the summary (e.g. a specific quote, a table, page N), call "
            "read_uploaded_file(filename) to pull the full extracted text on demand.",
            "CRITICAL — file presence: if the user's message references a file ('this PDF', 'the "
            "doc', 'attached') AND there's NO `[Attachments]` block in this turn AND "
            "read_uploaded_file() returns no matching file, only THEN politely ask them to "
            "attach or paste the file. Don't hallucinate a missing file when the attachments "
            "block is right there.",
            "You remember everything from previous conversations via your event log.",
            "Files uploaded by the user are stored in your memory across turns. The full extracted "
            "text of every uploaded PDF / docx / txt remains queryable even AFTER the original "
            "attachment turn — call read_uploaded_file(filename) to read it on demand. Call "
            "read_uploaded_file() with no args to LIST all files the user has ever uploaded in "
            "this conversation; never claim a file 'isn't available' without first calling that "
            "list. For large files, use offset/limit to read specific sections, or "
            "search='keyword' to jump to a match.",
            "CRITICAL — skill instructions vs in-context attachments: when you load a skill "
            "(e.g. via manage_skill('show', 'pdf')), the SKILL.md may describe a generic "
            "workflow that assumes the file is on disk. If the file is ALREADY in the "
            "`[Attachments]` block or readable via read_uploaded_file, USE THE IN-CONTEXT "
            "DATA — don't ask the user to re-upload just because the skill's example shows a "
            "file path. The skill's prompts are guidance for general use; the live conversation "
            "context wins.",
            "CRITICAL — workflows are function calls, not narration: if you have run_workflow "
            "available AND the user's request matches an installed workflow (e.g. 'write me a "
            "Twitter thread' → Content Studio, 'review this PR' → Code Review, 'help me research "
            "X' → Research Brief, 'polish my paper' → Paper Polish), you MUST emit a function "
            "call to run_workflow. Saying '我将启动 Content Studio' / 'I'll run the pipeline' "
            "in plain text WITHOUT an actual function_call is a BUG — the workflow never starts "
            "and the user sees nothing happen. Check the run_workflow tool's INSTALLED WORKFLOWS "
            "block before deciding; if the request matches, the call is mandatory. After a "
            "successful run_workflow function_call, your text reply must be at most one short "
            "sentence (the inline progress card carries everything the user needs to see).",
        ]
        installed = self.skills.names
        if installed:
            capabilities.append(f"Currently installed skills: {', '.join(installed)}")
        system += "\n\n## Your Capabilities\n" + "\n".join(f"- {c}" for c in capabilities)

        # Inject on-chain identity so the twin knows its own registration details
        identity_ctx = self._build_identity_context()
        if identity_ctx:
            system += f"\n\n{identity_ctx}"

        # DPM: Inject projected memory (or curated fallback)
        if evo_context:
            system += f"\n\n## Memory (projected from event log)\n{evo_context[:3000]}"

        # Inject installed skill INDEX (names + descriptions only, not full instructions)
        skill_context = self.skills.get_prompt_context()
        if skill_context:
            system += skill_context

        # When tools are registered, add explicit instructions so the LLM
        # uses function calling instead of generating text about tools.
        active_tools = self.tools if self.tools else None
        if active_tools:
            tool_list = ", ".join(self.tools.tool_names)
            system += (
                f"\n\n## Tool Use Instructions\n"
                f"You have access to the following tools via function calling: {tool_list}.\n"
                f"When you need to search the web, read a URL, or use any tool, "
                f"you MUST invoke the tool function — do NOT generate text describing "
                f"what you would search for or pretend to have search results. "
                f"Call the tool and wait for its response."
            )

        # ── Append user msg to in-memory chat context ────────────────
        # We use ``folded_user_message`` (text + distilled attachment
        # summaries) for the LLM call when present. Right after the
        # call returns we REPLACE that entry with the chip-prefixed
        # version so subsequent turns don't re-send the attachment
        # summaries (token leak) but still see "user attached X" as
        # natural-language context. Persistence to event_log already
        # happened above with the BARE text + structured metadata.
        llm_input_msg = (
            folded_user_message if folded_user_message is not None
            else chip_prefixed_user_msg
        )
        # #123 — vision parts (image_base64 bytes) ride alongside the
        # text on the same user dict. The SDK's
        # _messages_to_gemini_contents picks them up and emits an
        # inline_data Blob part per image. Stripped from the dict
        # below right after self.llm.chat(...) returns so they don't
        # leak into subsequent turn contexts (token cost + Gemini
        # gets confused by stale visual material).
        user_msg_entry: dict = {"role": "user", "content": llm_input_msg}
        if images:
            user_msg_entry["images"] = list(images)
        self._messages.append(user_msg_entry)

        # Live "drafting reply" cursor — flips to "replied" once the
        # LLM call returns. The desktop animates the in-progress dot
        # while this is the latest event.
        self.thinking.emit(
            "replying", "Drafting reply",
            content="streaming…",
            metadata={
                "context_messages": len(self._messages[-20:]),
                "tools_available": len(self.tools.tool_names) if self.tools else 0,
            },
        )

        llm_t0 = time.time()
        response = await self.llm.chat(
            messages=self._messages[-20:],
            system=system,
            tools=active_tools,
            # Pass the thinking emitter through so providers (Gemini)
            # can stream their own "thinking tokens" upstream as
            # ``reasoning`` events without twin having to know the
            # provider-specific shape. Providers that don't support
            # this just ignore the kwarg.
            thinking_emitter=self.thinking,
        )
        llm_duration_ms = int((time.time() - llm_t0) * 1000)

        # Phase Q fix: replace the folded user message in _messages
        # with the chip-prefixed version. Keeps the rolling LLM
        # context tight — without this, every subsequent turn for
        # the next 20 messages would re-send the attachment summaries
        # (token leak). The chip-only version still tells the LLM
        # an attachment WAS in play.
        if folded_user_message is not None and self._messages:
            for i in range(len(self._messages) - 1, -1, -1):
                if self._messages[i].get("role") == "user":
                    self._messages[i]["content"] = chip_prefixed_user_msg
                    break

        # #123 — strip image parts off the most recent user entry now
        # that the LLM call is done. Images are deliberately one-shot:
        # leaving them on _messages would mean every subsequent turn
        # in this conversation re-uploads ~1 MB of pixels to Gemini
        # for no benefit, and Gemini gets confused by visual context
        # the user isn't referring to anymore. Runs even when there
        # were no images on this turn (cheap no-op).
        for i in range(len(self._messages) - 1, -1, -1):
            if self._messages[i].get("role") == "user":
                self._messages[i].pop("images", None)
                break

        self._messages.append({"role": "assistant", "content": response})
        self._turn_count += 1

        # ABC: Post-check (invariants + governance)
        post = self.contract.post_check(response)
        if post.hard_violation:
            # Hard violation — log and regenerate (simplified: append warning)
            self.event_log.append("contract_violation", f"Hard: {post.reason}", session_id=self._thread_id)
            self.thinking.emit(
                "insight", "Contract post-check fired",
                content=post.reason or "",
                metadata={"phase": "post_check", "hard_violation": True},
            )
            response = f"{response}\n\n⚠️ [Contract notice: {post.reason}]"

        # Update drift score
        hard_score = post.details.get("hard_compliance", 1.0)
        soft_score = post.details.get("soft_compliance", 1.0)
        self.drift.update(hard_score, soft_score, "chat")

        # DPM: Append assistant response to event log (instant, no LLM).
        # #128: stamp referenced_file_ids on the assistant_response so
        # downstream feedback / search can bind this reply back to the
        # specific attachments the user just sent. Without this, an
        # "✗ correct" gesture later has to guess which file it's
        # correcting — we'd be back to fragile time-window heuristics.
        # The list comes straight from the chat-request attachments
        # (text + image alike); empty/None leaves the metadata absent.
        assistant_meta: Optional[dict] = None
        if referenced_file_ids:
            assistant_meta = {"referenced_file_ids": list(referenced_file_ids)}
        assistant_msg_idx = self.event_log.append(
            "assistant_response",
            response,
            session_id=self._thread_id,
            metadata=assistant_meta,
        )

        # #136 — fire-and-forget embed of this turn's chunks.
        # We send three categories to the vector index:
        #   1. user_message  → source_kind="chat", source_id=f"event-{idx}"
        #   2. assistant_response → same shape
        #   3. each attachment_distilled → source_kind="caption" for
        #      images (more specific so semantic_search can scope to
        #      images only), "attachment" otherwise. source_id is
        #      the file_id when available — stable across sessions so
        #      re-seeing the same file dedupes correctly.
        #
        # Wrapped in a fire-and-forget asyncio task because (a) we
        # never want to block the chat response on an embedding API
        # round trip, (b) failures are fine: search_chunks would just
        # return fewer hits, and the lexical fallback still works.
        try:
            self._enqueue_vector_embed(
                user_id=self.config.owner or self.config.agent_id,
                user_msg_idx=user_msg_idx,
                user_msg_text=persisted_user_msg,
                assistant_msg_idx=assistant_msg_idx,
                assistant_msg_text=response,
                distilled=distilled_attachments or [],
            )
        except Exception as e:  # noqa: BLE001
            logger.debug("vector embed enqueue failed: %s", e)

        # Phase J episodic memory — keep the EpisodesStore in sync
        # with the active thread so the desktop "Episodes" namespace
        # pill and chat-time autobiographical recall actually work.
        # Best-effort, never blocks the response: failures are
        # logged and swallowed (the EventLog is still source of
        # truth for the same data).
        try:
            self._upsert_active_episode(user_message, response)
        except Exception as e:  # noqa: BLE001
            logger.debug("episodes upsert failed: %s", e)

        # Final "replied" event closes the turn card on the desktop.
        # ``duration_ms`` is the wall time from when the user sent the
        # message — what they'd want to read off a stopwatch.
        self.thinking.emit(
            "replied", "Replied",
            content=(response[:160] + "…") if len(response) > 160 else response,
            metadata={
                "response_chars": len(response),
                "llm_call_ms": llm_duration_ms,
                "turn_count": self._turn_count,
            },
            duration_ms=int((time.time() - turn_start_wall) * 1000),
        )

        # ── Post-response work runs in background — user sees response immediately ──
        # With DPM, this is much lighter: just session save + optional skill detection
        self._bg_task(
            f"post-turn-{self._turn_count}",
            self._post_response_work(),
        )

        return response

    def _upsert_active_episode(
        self, user_message: str, response: str,
    ) -> None:
        """Roll a running :class:`Episode` for the current
        ``self._thread_id`` after every turn.

        Why per-turn upsert instead of "write at session end":
          * Avalonia desktop polls the namespace endpoint every 2 s
            and users want to see the episode counter increment
            live as they chat — not "wait until you start a new
            session".
          * Sessions in Nexus don't have a clean "ended" signal;
            the user might just close the app. A running upsert
            with mid-session summary still gives downstream
            consumers (chat-time autobiographical recall, verdict
            scoring, audit) the data they need.
          * EpisodesStore.upsert keys on session_id so this is
            idempotent — no row inflation per turn.

        The summary stays cheap: bare counters + last user prompt
        snippet. The full per-turn content already lives in
        ``self.event_log`` if a richer summary is wanted later
        (e.g., a background task can re-summarise via LLM and
        replace the cheap one).
        """
        store = getattr(self, "episodes", None)
        if store is None:
            return
        # No session id (synthetic-default thread, ``""`` empty string)
        # → fold under the literal "default" id so the namespace
        # endpoint shows a single Episode row instead of dropping
        # the data on the floor.
        sid = self._thread_id or "default"
        existing = None
        try:
            for e in store.all():
                if e.session_id == sid:
                    existing = e
                    break
        except Exception:
            existing = None
        # Cheap heuristic for "ongoing" vs first turn: started_at
        # comes from the existing row when present, otherwise NOW.
        started_at = existing.started_at if existing else time.time()
        prior_turns = int(
            (existing.extra or {}).get("turn_count", 0)
        ) if existing else 0
        snippet = user_message.strip().splitlines()[0] if user_message else ""
        ep = self._Episode(
            session_id=sid,
            started_at=started_at,
            ended_at=time.time(),  # rolling — user is still active
            summary=(
                # Keep summary short and human-readable; richer prose
                # synthesis is the EventLogCompactor's job (it runs
                # on a different cadence and can afford an LLM call).
                f"{prior_turns + 1} turn(s); last user said: "
                f"{snippet[:120]}"
            ),
            topics=existing.topics if existing else [],
            key_event_ids=existing.key_event_ids if existing else [],
            outcome=None,  # not classified until session ends / verdict
            mood="",
            extra={
                "turn_count": prior_turns + 1,
                "last_response_chars": len(response or ""),
            },
        )
        store.upsert(ep)

    async def _auto_compact(self) -> None:
        """Background: delegate to SDK's EventLogCompactor.

        Compact result is appended to EventLog (persisted + snapshotted)
        and updates local CuratedMemory (derived view).

        We surface the curated snapshot text in the emit's ``content``
        field so server-side mirrors (and any other on_event consumer)
        can render it as a "memory" entry without re-reading the SDK
        EventLog. ``memory_count`` / ``user_count`` are kept for
        backward compatibility.
        """
        if self._compactor:
            ok = await self._compactor.compact(session_id=self._thread_id)
            if ok:
                snapshot = ""
                try:
                    snapshot = self.curated_memory.get_prompt_context() or ""
                except Exception:
                    snapshot = ""
                self._emit("memory_compact", {
                    "memory_count": self.curated_memory.memory_count,
                    "user_count": self.curated_memory.user_count,
                    "content": snapshot,
                    "summary": snapshot,
                    "char_count": len(snapshot),
                })

                # Phase A+ task #155: piggyback an EventLog snapshot
                # onto each compaction. Compaction is a natural
                # quiescent moment — the projection just ran, the
                # event log is in a consistent shape, and the
                # Backend is already firing a write-behind
                # for the memory_compact event itself. Snapshot
                # cadence ≈ compact_interval × ~20 turns, so a
                # reasonable bound on bytes-on-chain even for
                # high-traffic agents. Best-effort: failure leaves
                # the previous snapshot in place — no data loss.
                backend = getattr(self.rune, "_backend", None)
                if backend is not None and hasattr(backend, "store_json"):
                    try:
                        await self.event_log.snapshot_to(backend)
                    except Exception as e:  # noqa: BLE001
                        logger.debug(
                            "EventLog snapshot_to skipped: %s", e,
                        )

                # Phase O.5: each compaction round is a natural verdict
                # boundary — scan unsettled evolution_proposal events
                # whose windows have now elapsed, score them, and
                # rollback any that observed regressions. Best-effort:
                # any failure here is logged but never re-raised so
                # the compaction path keeps its quiet semantics.
                try:
                    runner = getattr(self, "verdict_runner", None)
                    if runner is not None:
                        verdicts = runner.score_pending()
                        for v in verdicts:
                            self._emit("evolution_verdict", {
                                "edit_id": v.edit_id,
                                "decision": v.decision,
                                "fix_score": round(v.fix_score, 4),
                                "regression_score": round(v.regression_score, 4),
                                "abc_drift_delta": round(v.abc_drift_delta, 4),
                            })
                except Exception as e:  # noqa: BLE001
                    logger.warning("verdict_runner.score_pending failed: %s", e)

    async def _post_response_work(self) -> None:
        """Background: memory extraction, session save, reflection. Never blocks chat.

        IMPORTANT: Memory extraction runs FIRST, session save runs AFTER.
        Previous ordering (save → extract) had a fatal race: if a crash occurred
        between save and extract, the session checkpoint was persisted but the
        memories from that turn were permanently lost. By extracting first, we
        ensure memories are stored before the session checkpoint references them.
        """
        storage = "local"

        # ── 1. Extract and store memories FIRST ──
        self._emit("memory_extract", {"turn": self._turn_count})
        try:
            evo_result = await self.evolution.after_conversation_turn(
                self._messages,
                max_memories=self.config.max_memories_per_conversation,
            )
            if evo_result.get("actions"):
                for action in evo_result["actions"]:
                    if action["type"] == "memory_extraction":
                        self._emit("memory_stored", {
                            "count": action["count"],
                            "items": action["items"],
                            "storage": storage,
                        })
                    elif action["type"] == "skill_learning":
                        for skill_detail in action.get("details", []):
                            self._emit("skill_learned", {
                                "skill": skill_detail.get("skill_name", ""),
                                "lesson": skill_detail.get("lesson", ""),
                                "source": skill_detail.get("source", "conversation"),
                                "storage": storage,
                            })
            # Also update curated memory with extracted insights
            if evo_result.get("actions"):
                for action in evo_result["actions"]:
                    if action["type"] == "memory_extraction":
                        for item in action.get("items", []):
                            if isinstance(item, str):
                                cat = "memory"
                                content = item
                            elif isinstance(item, dict):
                                cat = item.get("category", "fact")
                                content = item.get("content", str(item))
                            else:
                                continue
                            if cat in ("preference", "style", "relationship"):
                                self.curated_memory.add_user_info(content)
                            else:
                                self.curated_memory.add_memory(content)
        except Exception as e:
            logger.warning("Background memory extraction failed: %s", e)

        # ── 2. Persist session checkpoint AFTER memories are stored ──
        self._emit("session_save", {
            "thread_id": self._thread_id,
            "turn": self._turn_count,
            "messages": len(self._messages),
            "storage": storage,
        })
        try:
            await self._save_session()
        except Exception as e:
            logger.warning("Background session save failed: %s", e)

        # ── Self-reflection & persona evolution ──
        if (
            self.config.persona_evolution_enabled
            and self._turn_count > 0
            and self._turn_count % self.config.reflection_after_every_n_turns == 0
        ):
            self._emit("persona_reflect", {
                "turn": self._turn_count,
                "trigger": f"every {self.config.reflection_after_every_n_turns} turns",
            })
            try:
                reflection = await self.evolution.trigger_reflection()
                pe = reflection.get("persona_evolution", {})
                if pe.get("version"):
                    self._emit("persona_evolved", {
                        "version": pe["version"],
                        "changes": pe.get("changes", ""),
                        "confidence": pe.get("confidence", 0),
                        "storage": storage,
                    })
            except Exception as e:
                logger.warning("Background reflection failed: %s", e)

    async def _handle_command(self, message: str) -> Optional[str]:
        # Phase I: slash-command dispatch lives in twin_commands.py.
        from . import twin_commands
        return await twin_commands.handle_command(self, message)

    # ── Session Management ───────────────────────────────────────

    async def _save_session(self):
        cp = Checkpoint(
            thread_id=self._thread_id,
            agent_id=self.config.agent_id,
            state={
                "messages": self._messages[-50:],
                "turn_count": self._turn_count,
            },
            metadata={
                "twin_name": self.config.name,
                "persona_version": self.evolution.persona.persona_store.current_version(),
                "erc8004_agent_id": None,
                "storage_mode": "local",
            },
        )
        await self.rune.sessions.save_checkpoint(cp)

    async def _switch_session(self, new_session_id: str) -> None:
        """Hot-swap the active thread from outside.

        Multi-session support (Phase Q): the server's chat handler can
        pass a ``session_id`` to ``chat()`` to route a turn to a specific
        thread without restarting the twin. We save a checkpoint of the
        outgoing thread, then re-load the in-memory ``_messages`` ring
        from event_log filtered by ``new_session_id`` so the LLM sees
        only that thread's history.

        ``_messages`` shape: list of ``{"role", "content"}`` dicts.
        EventLog rows have ``event_type`` ∈ {user_message, assistant_response}
        which we map to roles user/assistant respectively.

        Best-effort throughout — a failed checkpoint write or history
        reload doesn't abort the switch (we'd rather chat in the new
        thread than refuse). Errors are logged.
        """
        old_id = self._thread_id
        if new_session_id == old_id:
            return

        # 1. Persist current thread so the user can come back to it.
        try:
            await self._save_session()
        except Exception as e:
            logger.warning(
                "switch_session: save outgoing checkpoint failed: %s", e
            )

        # 2. Adopt the new id BEFORE we touch _messages so any concurrent
        #    emit during the swap stamps with the new id.
        self._thread_id = new_session_id

        # 3. Re-load _messages from the event_log filtered by the new
        #    session_id. ``EventLog.recent`` returns Event dataclasses
        #    oldest-first within the requested window; we filter to
        #    just chat events and project to the LLM message shape.
        try:
            recent_events = self.event_log.recent(
                limit=120, session_id=new_session_id,
            )
        except Exception as e:
            logger.warning(
                "switch_session: history reload failed: %s", e
            )
            recent_events = []

        new_messages: list[dict] = []
        for ev in recent_events:
            if ev.event_type == "user_message":
                new_messages.append({"role": "user", "content": ev.content or ""})
            elif ev.event_type == "assistant_response":
                new_messages.append({"role": "assistant", "content": ev.content or ""})
        # Cap at 50 messages — same ring _save_session persists, so
        # the LLM context stays bounded and predictable.
        if len(new_messages) > 50:
            new_messages = new_messages[-50:]

        self._messages = new_messages
        self._turn_count = sum(
            1 for m in new_messages if m.get("role") == "user"
        )

        logger.info(
            "twin.switch_session: %s → %s (%d messages restored)",
            old_id, new_session_id, len(new_messages),
        )

        # Audit-trail event so the timeline / thinking panel show the
        # boundary between threads.
        try:
            self.event_log.append(
                "session_switched",
                f"{old_id} -> {new_session_id}",
                session_id=new_session_id,
            )
        except Exception as exc:
            logger.debug("session_switched event append failed: %s", exc)

    async def new_session(self) -> str:
        from . import twin_commands
        return await twin_commands.new_session(self)

    async def delete_session(self, session_id: str) -> dict:
        """Hard-delete a session everywhere we can reach.

        Cleanup matrix:
          * EventLog (local SQLite) — rows for this session_id are
            DROPPED (irreversible).
          * Backend objects (if chain mode) — best-effort delete of
            objects under ``agents/{user}/.../sessions/{session_id}/``.
            Failures here don't abort the whole delete; the object
            store is treated as cache.
          * BSC state-root anchors — IMMUTABLE. We can't and don't
            attempt to alter them. The audit trail "session existed
            up to anchor N, deleted at block M" stands by design.
          * In-memory thread state — if we're currently active in this
            session, we reset _thread_id / _messages so the next chat
            starts a fresh thread.

        We emit a final ``session_deleted`` event BEFORE the SQL
        delete so it lands in the OUTGOING anchor (the deletion is
        itself part of the durable audit trail). Returns a summary
        dict the server returns to the desktop client.
        """
        if not session_id:
            raise ValueError("delete_session needs an explicit session_id")

        # Snapshot counts before we delete so the response reports
        # what was removed (handy for the desktop's "deleted N
        # messages" toast).
        before_count = 0
        try:
            before_count = self.event_log.count(session_id=session_id)
        except Exception as exc:
            logger.debug("counting session events failed: %s", exc)

        # Audit-trail event. We intentionally tag it with session_id
        # so it shows up in twin_event_log queries scoped to this
        # session — a forensic reader scanning the timeline knows
        # exactly when the deletion happened.
        try:
            self.event_log.append(
                "session_deleted",
                f"session {session_id} deleted ({before_count} events removed)",
                session_id=session_id,
                metadata={
                    "deleted_event_count": before_count,
                    "deleted_at": datetime.now(timezone.utc).isoformat(),
                },
            )
        except Exception as e:
            logger.warning("session_deleted audit event failed: %s", e)

        # Drop SQLite rows.
        deleted = 0
        try:
            deleted = self.event_log.delete_session(session_id)
        except Exception as e:
            logger.warning(
                "EventLog.delete_session failed for %s: %s", session_id, e,
            )

        # Best-effort backend object cleanup.
        # ``delete_session_objects`` when chain mode is active; in
        # local mode there's nothing to clean up.
        gf_result: dict = {"attempted": False}
        backend = getattr(self.rune, "_backend", None) or getattr(self, "_chain_backend", None)
        if backend is not None and hasattr(backend, "delete_session_objects"):
            try:
                gf_result = await backend.delete_session_objects(session_id)
                gf_result["attempted"] = True
            except Exception as e:
                logger.warning(
                    "Backend object cleanup failed for session %s: %s",
                    session_id, e,
                )
                gf_result = {"attempted": True, "error": str(e)}

        # If we just nuked the active thread, fall back to a fresh
        # one so the next chat doesn't try to append against a
        # session id whose context we already wiped.
        if self._thread_id == session_id:
            self._thread_id = f"session_{uuid.uuid4().hex[:8]}"
            self._messages = []
            self._turn_count = 0
            logger.info(
                "twin: active session deleted; rotated to fresh thread %s",
                self._thread_id,
            )

        return {
            "session_id": session_id,
            "deleted_event_count": deleted,
            "audit_event_recorded": True,
            "storage": gf_result,
            "storage_immutable_note": (
                "Storage records that reference deleted sessions are immutable."
                "and cannot be deleted. The deletion event is itself "
                "part of the audit trail."
            ),
        }

    # ── Task Delegation ──────────────────────────────────────────

    async def close(self):
        # ── 1. Commit pending facts to chain + save session ──
        # Phase D 续: facts_store writes through to disk synchronously
        # but commit() pins a new VersionedStore version + queues a
        # chain mirror — that's the durable shutdown promise.
        try:
            self.facts.commit()
        except (asyncio.CancelledError, Exception) as e:
            logger.debug("Facts commit during shutdown: %s", e)
        try:
            await self._save_session()
        except (asyncio.CancelledError, Exception) as e:
            logger.debug("Session save during shutdown: %s", e)

        # ── 2. Wait for background tasks (memory extraction, session sync) ──
        # The _post_response_work tasks fire backend writes inside them,
        # so we need to wait for those to complete first, then let
        # ChainBackend.close() drain its own pending write queue.
        if hasattr(self, "_bg_tasks") and self._bg_tasks:
            n = len(self._bg_tasks)
            grace = 15.0  # generous: post-turn work latency
            self._emit("shutdown_sync", {
                "pending": n,
                "grace_seconds": grace,
            })
            logger.info(
                "Graceful shutdown: waiting up to %.0fs for %d background task(s)...",
                grace, n,
            )
            try:
                done, pending = await asyncio.wait(
                    self._bg_tasks, timeout=grace,
                )
                if done:
                    logger.info("%d background task(s) completed", len(done))
                if pending:
                    logger.warning(
                        "%d task(s) still pending after %.0fs — cancelling",
                        len(pending), grace,
                    )
                    for task in pending:
                        task.cancel()
                    await asyncio.gather(*pending, return_exceptions=True)
            except Exception:
                # Fallback: cancel everything
                for task in list(self._bg_tasks):
                    task.cancel()
                await asyncio.gather(*self._bg_tasks, return_exceptions=True)
            self._bg_tasks.clear()

        # ── 3. Close Rune provider (drains ChainBackend pending writes) ──
        # ChainBackend.close() has its own grace period for background tasks
        # that were fired by the tasks we just waited for.
        try:
            await self.rune.close()
        except (asyncio.CancelledError, Exception) as e:
            logger.debug("Rune close during shutdown: %s", e)

        try:
            await self.llm.close()
        except Exception as exc:
            logger.debug("LLM close during shutdown: %s", exc)

        # ── 4. Close MCP server connections ──
        if self.tools:
            try:
                await self.tools.close()
            except Exception as e:
                logger.debug("MCP close during shutdown: %s", e)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        await self.close()
