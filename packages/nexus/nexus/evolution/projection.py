"""ProjectionMemory — task-conditioned projection over EventLog.

Two modes (selected via ``mode=`` in the constructor or per-call):

* **single_call** (DPM canonical, default for short logs) — one LLM
  call over the full event-log trajectory. Lossy-compaction style.
  Fast and cheap when the log fits in the LLM's context, degrades
  as the log grows past the effective context window.

* **rlm** (Phase P) — Recursive Language Model. Load events as a
  REPL variable, let the root LLM write Python to slice / regex /
  sub-LM-call snippets, then commit a final string via
  ``_set_result``. Handles arbitrarily long logs at higher cost
  variance. Inspired by Zhang, Kraska & Khattab,
  *Recursive Language Models* (arXiv:2512.24601).
  Implementation: :class:`nexus_core.RLMRunner`.

Based on DPM (arXiv:2604.20158):
  π(E, T, B) → M
  E = event log, T = task/query, B = budget (chars)

Output (both modes) extracts:
  - FACTS: concrete verifiable claims with [event_index] citations
  - CONTEXT: relevant background for the current query
  - USER_PROFILE: known preferences and style

Phase P note: RLM mode is opt-in via ``TwinConfig.chat_projection_mode``;
default stays ``single_call`` until quality dogfooding signs off.
"""

from __future__ import annotations

import logging
from typing import Awaitable, Callable, Optional

from nexus_core.memory import EventLog
from nexus_core.rlm import RLMConfig, RLMRunner

logger = logging.getLogger(__name__)

PROJECTION_PROMPT = """You are producing a memory view from an event log for the current conversation.

EVENTS:
{events}

CURRENT QUERY: {query}
BUDGET: {budget} characters maximum

Extract the most relevant information for answering the current query.
Output three sections in this exact order:

FACTS
- Key facts from the events, each citing [event_index]. Be specific.

CONTEXT
- Background context relevant to the current query.

USER_PROFILE
- Known user preferences, communication style, interests.

Stay within the budget. Prioritize facts that directly relate to the query.
If the log is empty or has no relevant content, output "No relevant context found."
"""


_RLM_PROJECTION_TASK = """\
You are projecting a memory view from a long EventLog for a chat agent.

The full event log is loaded as the REPL variable ``trajectory`` (a string).
The current user query is in the variable ``query``. Budget is in
``budget`` (max chars for your final output).

Goal: build the most useful FACTS + CONTEXT + USER_PROFILE summary that
helps the chat LLM answer the user's query.

Patterns that work well here:
  - Use ``import re`` to find references to topics in the query.
  - Slice ``trajectory[start:end]`` to focus on relevant ranges.
  - For dense reasoning, ``await _sub_llm("...")`` over individual slices
    instead of feeding the whole trajectory.
  - Stitch findings into the three-section format below.

Final output format (commit via _set_result):

FACTS
- Specific facts from events, each citing [event_index] when possible.

CONTEXT
- Background relevant to the current query.

USER_PROFILE
- Known preferences, style, interests.

If the log has nothing relevant, _set_result("No relevant context found.").
"""


class ProjectionMemory:
    """Task-conditioned projection over an EventLog.

    Default mode (``single_call``) makes exactly ONE LLM call at
    decision time to extract relevant context — the canonical DPM
    π(E, T, B) function. Optional ``rlm`` mode delegates to
    :class:`nexus_core.RLMRunner` so the root LLM can navigate
    arbitrarily long logs at the cost of higher per-call variance.
    """

    def __init__(
        self,
        event_log: EventLog,
        llm_fn: Callable[..., Awaitable[str]],
        *,
        mode: str = "single_call",
        sub_llm_fn: Optional[Callable[[str], Awaitable[str]]] = None,
        rlm_config: Optional[RLMConfig] = None,
        fastpath_char_threshold: int = 16_000,
    ):
        """
        Args:
            event_log: The append-only event log.
            llm_fn: Async LLM completion (prompt, **kwargs) -> str.
                Used directly in single_call mode; wrapped as the
                root LLM in rlm mode.
            mode: ``"single_call"`` or ``"rlm"``.
            sub_llm_fn: Async (query) -> str cheap-LLM callable.
                Required if ``mode == "rlm"``; ignored otherwise.
                Pass ``None`` plus ``mode="rlm"`` to run RLM with
                no recursion (paper's "no sub-calls" ablation).
            rlm_config: Override default :class:`RLMConfig`. Sane
                defaults are picked from ``TwinConfig`` if absent.
            fastpath_char_threshold: When the trajectory is shorter
                than this many chars, fall back to single_call even
                if ``mode == "rlm"``. RLM overhead doesn't pay off
                on short logs (paper Observation 3).
        """
        if mode not in {"single_call", "rlm"}:
            raise ValueError(f"mode must be 'single_call' or 'rlm', got {mode!r}")
        self._log = event_log
        self._llm = llm_fn
        self._mode = mode
        self._sub_llm = sub_llm_fn
        self._rlm_config = rlm_config or RLMConfig(
            max_iterations=8, max_sub_calls=15, timeout_seconds=30.0,
        )
        self._fastpath_threshold = fastpath_char_threshold
        self._last_projection: str = ""
        self._last_query: str = ""
        self._last_mode_used: str = ""  # observability: which path actually ran

    async def project(self, query: str, budget: int = 3000,
                      session_id: str = None) -> str:
        """Project a memory view from the event log for the given query.

        This is the π(E, T, B) function from DPM.

        In single_call mode: one LLM call, deterministic.
        In rlm mode: stochastic RLM run, falls back to single_call
        when trajectory is shorter than ``fastpath_char_threshold``.

        Args:
            query: The current user query / task
            budget: Maximum characters for the projection output
            session_id: Optional session filter

        Returns:
            Structured memory view (FACTS + CONTEXT + USER_PROFILE).
        """
        # Get trajectory from event log
        trajectory = self._log.get_trajectory(session_id=session_id, max_chars=80000)

        if not trajectory.strip():
            self._last_projection = ""
            self._last_mode_used = "empty"
            return ""

        event_count = self._log.count(session_id=session_id)

        # Fast-path: short log → single_call regardless of configured mode.
        use_rlm = (
            self._mode == "rlm"
            and len(trajectory) >= self._fastpath_threshold
        )

        if use_rlm:
            return await self._project_rlm(query, budget, trajectory, event_count)
        return await self._project_single_call(query, budget, trajectory, event_count)

    async def _project_single_call(
        self, query: str, budget: int, trajectory: str, event_count: int,
    ) -> str:
        """Original DPM single-call projection — one LLM round-trip."""
        logger.info(
            "Projecting (single_call): %d events, query=%r, budget=%d",
            event_count, query[:50], budget,
        )
        prompt = PROJECTION_PROMPT.format(
            events=trajectory, query=query, budget=budget,
        )
        try:
            result = await self._llm(prompt, temperature=0.0)
            self._last_projection = result.strip()
            self._last_query = query
            self._last_mode_used = "single_call"
            logger.info("Projection complete: %d chars", len(self._last_projection))
            return self._last_projection
        except Exception as e:
            logger.warning("Projection (single_call) failed: %s", e)
            self._last_mode_used = "single_call_failed"
            return ""

    async def _project_rlm(
        self, query: str, budget: int, trajectory: str, event_count: int,
    ) -> str:
        """RLM projection — root LLM navigates trajectory via REPL.

        Builds a two-arg root_llm (messages, system) → str adapter
        on top of the single-arg ``self._llm`` so the existing
        TwinConfig wiring keeps working without changes.
        """
        logger.info(
            "Projecting (rlm): %d events, %d chars trajectory, query=%r, budget=%d",
            event_count, len(trajectory), query[:50], budget,
        )

        async def _root_llm(messages, system):
            """Adapt single-prompt LLMClient.complete to the
            RLM-runner's two-arg expectation by flattening messages
            into a single prompt string. Loses some structure but
            works with every existing LLMClient backend."""
            flat = system.rstrip() + "\n\n" if system else ""
            for m in messages:
                role = m.get("role", "user").upper()
                flat += f"[{role}]\n{m.get('content', '')}\n\n"
            return await self._llm(flat, temperature=0.0)

        runner = RLMRunner(
            root_llm=_root_llm,
            sub_llm=self._sub_llm,
            config=self._rlm_config,
        )
        try:
            result = await runner.run(
                task=_RLM_PROJECTION_TASK,
                context_vars={
                    "trajectory": trajectory,
                    "query": query,
                    "budget": budget,
                },
            )
        except Exception as e:
            logger.warning("Projection (rlm) failed: %s; falling back to single_call", e)
            out = await self._project_single_call(query, budget, trajectory, event_count)
            # Overwrite the inner call's "single_call" tag — what the
            # operator wants to see is *why* we ended up in single_call
            # this time (RLM error, not native single_call).
            self._last_mode_used = "rlm_failed_fallback"
            return out

        if result.truncated or not result.output:
            # RLM hit a hard limit without committing a result —
            # fall back so we don't silently degrade chat quality.
            logger.warning(
                "RLM projection truncated (iters=%d, sub_calls=%d); "
                "falling back to single_call",
                result.iterations_used, result.sub_calls_used,
            )
            out = await self._project_single_call(query, budget, trajectory, event_count)
            self._last_mode_used = "rlm_truncated_fallback"
            return out

        self._last_projection = result.output.strip()
        self._last_query = query
        self._last_mode_used = "rlm"
        logger.info(
            "Projection (rlm) complete: %d chars, %d iters, %d sub_calls, %.1fs",
            len(self._last_projection),
            result.iterations_used,
            result.sub_calls_used,
            result.elapsed_seconds,
        )
        return self._last_projection

    async def search(self, query: str, limit: int = 10) -> list[dict]:
        """Search the event log (FTS5) without LLM — for quick recall."""
        events = self._log.search(query, limit=limit)
        return [
            {"index": e.index, "type": e.event_type, "content": e.content[:200]}
            for e in events
        ]

    @property
    def last_projection(self) -> str:
        return self._last_projection

    @property
    def event_count(self) -> int:
        return self._log.count()
