"""Unified LLM context assembly — four ordered layers with token budgets.

Context-management redesign, phase 1. This module is THE single place
that assembles the context for an LLM chat turn. Callers hand it the
pieces; it orders them, estimates token cost, trims to budget, and
returns a :class:`ContextBundle` ready for ``llm_gateway.call_llm``.

Layer model (in final-prompt order)
-----------------------------------
* **S — system/stable**: persona / system prompt + auto-apply skills
  block. Caller-provided. NEVER trimmed. Physically the skills block
  rides as ``system_tail`` appended after the R layer so skill
  instructions keep their "last word" override semantics, but for
  budgeting purposes it belongs to S (untrimmable).
* **M — memory/projection**: cross-session memory, read from the
  user's twin CuratedMemory snapshot (``MEMORY.md`` / ``USER.md``).
  Capped at its layer fraction; trimmed tail-first only after H and R
  are exhausted. Empty / missing → layer skipped entirely.
* **R — retrieval/turn data**: caller-provided :class:`RetrievalBlock`
  list (tier retrieval results, doc reference snapshots, attachment
  excerpts …), each tagged with an integer priority. Lowest priority
  dropped first when over budget. Original caller order is preserved
  in the rendered prompt regardless of priority.
* **H — history/session**: the messages array (see
  :func:`get_session_history`, which also splices in the rolling
  summary). Oldest messages dropped first — but the synthetic summary
  message and the last two turns (last 4 messages) are protected.

The final prompt puts S + M first — they are stable across the turns
of a session, so providers with prompt caching get a cacheable stable
prefix — then R, then H as the messages array (followed by the
current user message, which is never trimmed).

Token estimation
----------------
:func:`estimate_tokens` is a cheap heuristic, NOT a tokenizer:

    tokens ≈ sum(3.5 if CJK else 1 for each char) / 3.5

i.e. ASCII ≈ len/3.5 (close to the familiar len/4 rule of thumb, a
little conservative) and CJK chars (codepoint > U+2E80) ≈ 1 token
each, which matches how most BPE vocabularies treat Chinese clinical
text. Good to ±20% — plenty for budgeting, useless for billing.

Budget
------
Default overall input budget is 32 000 tokens, overridable via the
``NEXUS_CONTEXT_BUDGET`` env var. Layer fractions:
S=0.15, M=0.15, R=0.35, H=0.25, reserve=0.10 (the reserve is simply
never allocated — trimming targets ``budget * 0.90``).

Trimming order when over budget:
  1. oldest H messages (never the summary message or the last 2 turns)
  2. lowest-priority R blocks
  3. M, truncated tail-first
S and the current user message are never trimmed.

Rolling session summary
-----------------------
:func:`get_session_history` returns the last ``window`` messages of a
session PLUS — when the session is longer than the window and a
summary row exists in ``chat_session_summaries`` — a synthetic first
message ``{role:'user', content:'[对话早前内容摘要]\\n…'}``. Summary
generation is async / out-of-band (:func:`maybe_update_session_summary`
fired from the chat_router post-turn hook via
:func:`schedule_session_summary_update`); a stale or missing summary
just means the window behaves exactly like the pre-redesign fixed
window. Turn latency never waits on summarisation.
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────
# Budget constants
# ─────────────────────────────────────────────────────────────────────

DEFAULT_BUDGET_TOKENS = 32_000

#: Model-aware default budgets. Matched by substring against the
#: active DEFAULT_LLM_MODEL (first hit wins, ordered most-specific
#: first). Budgets are deliberately far below each model's context
#: window: the window is a ceiling for rare large payloads, the budget
#: is per-turn discipline (cost: input tokens bill every turn; latency:
#: prefill scales with input; quality: retrieval beats needle-in-a-
#: haystack). Larger windows earn proportionally larger budgets, not
#: window-sized ones. NEXUS_CONTEXT_BUDGET env always overrides.
MODEL_BUDGET_TABLE: list[tuple[str, int]] = [
    ("kimi-k2.7", 64_000),      # 256K window
    ("kimi-k2.6", 64_000),      # 256K window
    ("kimi", 48_000),           # other kimi models (128K+)
    ("gemini-2.5", 64_000),     # 1M window
    ("gemini", 48_000),
    ("claude", 64_000),         # 200K window
    ("gpt-4o", 40_000),         # 128K window
    ("gpt", 40_000),
]


def _budget_for_model(model: str | None) -> int:
    """Model-aware default budget (substring match, first hit wins)."""
    if model:
        m = model.lower()
        for needle, budget in MODEL_BUDGET_TABLE:
            if needle in m:
                return budget
    return DEFAULT_BUDGET_TOKENS

#: Layer fractions of the overall budget. ``reserve`` is head-room that
#: is never allocated (trim target = budget * (1 - reserve)).
BUDGETS = {
    "S": 0.15,
    "M": 0.15,
    "R": 0.35,
    "H": 0.25,
    "reserve": 0.10,
}

#: Prefix of the synthetic rolling-summary history message. Also used
#: to recognise (and protect) it during trimming.
SUMMARY_PREFIX = "[对话早前内容摘要]"

#: The last N history messages (= last 2 user/assistant turns) are
#: never dropped by budget trimming.
PROTECTED_RECENT_MESSAGES = 4

#: Per-message overhead added to the content estimate (role tokens,
#: message framing).
_PER_MESSAGE_OVERHEAD = 4

#: Default history window (messages), matching the legacy fixed window.
DEFAULT_HISTORY_WINDOW = 12

#: Summary regeneration cadence: at least this many messages must have
#: fallen out of the window — and be uncovered by the stored summary —
#: before we spend an LLM call refreshing it.
SUMMARY_MIN_STALE = 6

#: How many messages (newest-first) the summariser fetches at most.
#: Sessions longer than this lose their oldest tail from the summary —
#: an accepted phase-1 approximation.
SUMMARY_MAX_FETCH = 400


def _budget_from_env() -> int:
    """Resolve the effective budget: NEXUS_CONTEXT_BUDGET env wins;
    otherwise a model-aware default from MODEL_BUDGET_TABLE based on
    the active DEFAULT_LLM_MODEL; otherwise DEFAULT_BUDGET_TOKENS."""
    raw = os.environ.get("NEXUS_CONTEXT_BUDGET", "")
    try:
        v = int(raw)
        if v > 0:
            return v
    except (TypeError, ValueError):
        pass
    try:
        from nexus_server.config import get_config
        return _budget_for_model(get_config().DEFAULT_LLM_MODEL)
    except Exception:  # noqa: BLE001 — config unavailable in some tests
        return DEFAULT_BUDGET_TOKENS


# ─────────────────────────────────────────────────────────────────────
# Token estimation
# ─────────────────────────────────────────────────────────────────────


def estimate_tokens(text: str) -> int:
    """Cheap token-count heuristic (see module docstring).

    ASCII/latin chars weigh 1, CJK-range chars (codepoint > U+2E80)
    weigh 3.5; the weighted sum is divided by 3.5. Net effect:
    ASCII ≈ len/3.5, CJK ≈ 1 token per char. Returns 0 for empty
    input, at least 1 for any non-empty input.
    """
    if not text:
        return 0
    weighted = 0.0
    for ch in text:
        weighted += 3.5 if ord(ch) > 0x2E80 else 1.0
    return max(1, int(weighted / 3.5))


def _estimate_message(msg: dict) -> int:
    return estimate_tokens(str(msg.get("content") or "")) + _PER_MESSAGE_OVERHEAD


def _truncate_tail_to_tokens(text: str, max_tokens: int) -> str:
    """Cut ``text`` from the tail until it fits ``max_tokens``.

    Tail-first per the trimming spec: the head of the memory snapshot
    holds the oldest, most-established facts — we prefer keeping those.
    """
    if max_tokens <= 0:
        return ""
    while text and estimate_tokens(text) > max_tokens:
        est = estimate_tokens(text)
        keep = int(len(text) * max_tokens / est * 0.98)
        keep = min(len(text) - 1, max(0, keep))
        if keep <= 0:
            return ""
        text = text[:keep]
    return text.rstrip()


# ─────────────────────────────────────────────────────────────────────
# Data shapes
# ─────────────────────────────────────────────────────────────────────


@dataclass
class RetrievalBlock:
    """One R-layer block. Higher ``priority`` survives trimming longer."""
    text: str
    priority: int = 0
    tag: str = ""


@dataclass
class ContextBundle:
    """Assembled context, ready for ``llm_gateway.call_llm``.

    * ``system_text`` — S + M + R (+ system_tail) composed.
    * ``messages`` — H (with optional summary message) + the current
      user message appended last.
    * ``dropped`` — ``{"history_msgs": int, "retrieval_blocks": int}``;
      feeds the ``context_info`` SSE debug frame.
    * ``token_estimate`` — heuristic total input tokens after trimming.
    * ``summary_included`` — whether the synthetic rolling-summary
      message is present in ``messages``.
    """
    system_text: str
    messages: list[dict]
    dropped: dict = field(default_factory=dict)
    token_estimate: int = 0
    summary_included: bool = False


# ─────────────────────────────────────────────────────────────────────
# Layer M — cross-session memory projection
# ─────────────────────────────────────────────────────────────────────


_CURATED_ENTRY_DELIMITER = "\n§\n"  # mirrors nexus_core.memory.curated


def _twin_base_dir() -> Path:
    """Same path contract as twin_manager / twin_event_log."""
    return Path(
        os.environ.get(
            "NEXUS_TWIN_BASE_DIR",
            os.path.expanduser("~/.nexus_server/twins"),
        )
    )


def _read_curated_file(path: Path) -> list[str]:
    """Parse a CuratedMemory markdown file into its entry list.

    Mirrors ``nexus_core.memory.curated.CuratedMemory._read_file``
    (``\\n§\\n``-delimited entries, order-preserving dedupe) WITHOUT
    instantiating a twin — reading the files directly costs one stat +
    one read instead of the full DigitalTwin bring-up (LLM client init,
    chain backend, session restore: seconds, not microseconds).
    """
    try:
        if not path.is_file():
            return []
        text = path.read_text(encoding="utf-8").strip()
    except OSError as e:
        logger.debug("curated memory read failed (%s): %s", path, e)
        return []
    if not text:
        return []
    entries = [e.strip() for e in text.split(_CURATED_ENTRY_DELIMITER) if e.strip()]
    seen: set[str] = set()
    return [e for e in entries if not (e in seen or seen.add(e))]


def get_memory_projection(user_id: str, scope: str = "user") -> str:
    """Layer M — the user's cross-session curated-memory snapshot.

    Reads ``{TWIN_BASE_DIR}/{user_id}/curated_memory/MEMORY.md`` and
    ``USER.md`` directly from disk (the derived views the twin's
    CuratedMemory maintains) and renders them in the exact shape
    ``CuratedMemory.get_prompt_context()`` produces, so the projection
    is byte-identical to what the legacy twin path injects.

    ``scope`` is reserved for future per-patient / per-study memory
    scoping; only ``"user"`` is implemented in phase 1.

    Returns ``""`` when the user has no curated memory (fresh user,
    missing dir, unreadable files) — callers skip the layer.
    """
    if not user_id or scope != "user":
        return ""
    d = _twin_base_dir() / user_id / "curated_memory"
    memory_entries = _read_curated_file(d / "MEMORY.md")
    user_entries = _read_curated_file(d / "USER.md")
    parts: list[str] = []
    if memory_entries:
        parts.append("## Your Memory\n- " + "\n- ".join(memory_entries))
    if user_entries:
        parts.append("## About This User\n- " + "\n- ".join(user_entries))
    return "\n\n".join(parts)


# ─────────────────────────────────────────────────────────────────────
# Layer H — session history + rolling summary
# ─────────────────────────────────────────────────────────────────────


#: fetcher(user_id, session_id, limit) -> (messages_oldest_first, total)
#: where each message is at least {"role": ..., "content": ...}.
HistoryFetcher = Callable[[str, str, int], tuple[list[dict], int]]


def _default_history_fetcher(
    user_id: str, session_id: str, limit: int,
) -> tuple[list[dict], int]:
    """Default fetcher — the shared twin_event_log chat history."""
    from nexus_server import twin_event_log
    raw, total = twin_event_log.list_messages(
        user_id, limit, before_idx=None, session_id=session_id,
    )
    return raw, int(total)


def _clean_history(raw: list[dict]) -> list[dict]:
    """Keep only user/assistant messages with non-empty content —
    identical filter to the legacy ``_recent_history_messages``."""
    out: list[dict] = []
    for m in raw:
        role = m.get("role") or ""
        if role not in ("user", "assistant"):
            continue
        content = (m.get("content") or "").strip()
        if not content:
            continue
        out.append({"role": role, "content": content})
    return out


def _ensure_summary_schema(conn) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS chat_session_summaries (
            user_id    TEXT NOT NULL,
            session_id TEXT NOT NULL,
            upto_idx   INTEGER NOT NULL DEFAULT 0,
            summary    TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL DEFAULT '',
            PRIMARY KEY (user_id, session_id)
        )
        """
    )


def _read_summary_row(user_id: str, session_id: str) -> tuple[str, int]:
    """Return (summary, upto_idx); ("", 0) when absent / on error."""
    try:
        from nexus_server.database import get_db_connection
        with get_db_connection() as conn:
            _ensure_summary_schema(conn)
            row = conn.execute(
                "SELECT summary, upto_idx FROM chat_session_summaries "
                "WHERE user_id = ? AND session_id = ?",
                (user_id, session_id),
            ).fetchone()
        if row:
            return str(row[0] or ""), int(row[1] or 0)
    except Exception as e:  # noqa: BLE001
        logger.debug("summary read failed for %s/%s: %s", user_id, session_id, e)
    return "", 0


def get_session_history(
    user_id: str,
    session_id: Optional[str],
    window: int = DEFAULT_HISTORY_WINDOW,
    *,
    fetcher: Optional[HistoryFetcher] = None,
) -> list[dict]:
    """Layer H — last ``window`` messages of a session, oldest-first,
    plus the rolling summary when the session outgrew the window.

    When the session holds more than ``window`` messages AND a summary
    row exists in ``chat_session_summaries``, a synthetic first message
    ``{role:'user', content:'[对话早前内容摘要]\\n<summary>'}`` is
    prepended so the LLM sees a compressed view of what fell out of
    the window. Missing / stale summary → plain window (legacy
    behaviour).

    ``fetcher`` lets non-event-log surfaces (e.g. the writing studio's
    ``doc_chat_messages``) reuse the same window + summary mechanics;
    see :data:`HistoryFetcher` for the contract. Best-effort: any
    fetch failure degrades to "no history".
    """
    if not session_id:
        return []
    fetch = fetcher or _default_history_fetcher
    try:
        raw, total = fetch(user_id, session_id, window)
    except Exception as exc:  # noqa: BLE001
        logger.debug("history fetch failed for session=%s: %s", session_id, exc)
        return []
    out = _clean_history(raw)
    if total > window:
        summary, _upto = _read_summary_row(user_id, session_id)
        if summary.strip():
            out.insert(0, {
                "role": "user",
                "content": SUMMARY_PREFIX + "\n" + summary.strip(),
            })
    return out


# ── Rolling-summary generation (async, out-of-band) ──────────────────


_SUMMARIZER_SYSTEM_PROMPT = (
    "你是一个严谨的医疗对话摘要器。把提供的医生-助手对话内容压缩成一段"
    "不超过 300 token 的要点摘要。\n"
    "硬性规则：\n"
    "1. 只使用对话中明确出现的信息。绝对不要编造、推断或补充任何事实——"
    "宁可省略，不可虚构。\n"
    "2. 临床数值必须逐字保留（剂量、日期、病灶大小、化验值、分期、"
    "百分比等），不得四舍五入或改写。\n"
    "3. 保留仍未解决的问题与正在进行的任务。\n"
    "4. 已有摘要中的事实应保留（可精简措辞），除非新对话明确推翻。\n"
    "只输出摘要正文本身，不要任何前言、标题或解释。"
)

_SUMMARY_MSG_CHAR_CAP = 1200   # per-message transcript cap fed to the LLM


async def maybe_update_session_summary(
    user_id: str,
    session_id: str,
    *,
    window: int = DEFAULT_HISTORY_WINDOW,
    min_stale: int = SUMMARY_MIN_STALE,
    fetcher: Optional[HistoryFetcher] = None,
) -> bool:
    """Refresh the rolling summary for one session, if warranted.

    Cadence gates (both must hold, otherwise no-op → ``False``):
      * ``messages_beyond_window >= min_stale`` — the session must have
        outgrown the window by enough to be worth summarising;
      * the stored summary's ``upto_idx`` is stale by >= ``min_stale``
        messages.

    On refresh: summarises [existing summary + the messages that have
    newly fallen out of the window] into <=300 tokens via
    ``llm_gateway.call_llm`` (configured default model, max_tokens 512)
    and upserts ``chat_session_summaries``. Returns ``True`` when the
    row was written.

    Designed to run out-of-band (see
    :func:`schedule_session_summary_update`) — the chat turn never
    waits on it, and any failure here just leaves the previous (or no)
    summary in place.
    """
    if not user_id or not session_id:
        return False
    fetch = fetcher or _default_history_fetcher
    try:
        raw, _total = fetch(user_id, session_id, SUMMARY_MAX_FETCH)
    except Exception as exc:  # noqa: BLE001
        logger.debug("summary fetch failed for %s/%s: %s", user_id, session_id, exc)
        return False
    msgs = _clean_history(raw)
    beyond = len(msgs) - window
    if beyond < min_stale:
        return False

    prev_summary, covered = _read_summary_row(user_id, session_id)
    covered = max(0, min(covered, beyond))
    if beyond - covered < min_stale:
        return False   # summary fresh enough

    fallen_out = msgs[covered:beyond]
    if not fallen_out:
        return False

    transcript_lines = [
        f"{m['role']}: {m['content'][:_SUMMARY_MSG_CHAR_CAP]}"
        for m in fallen_out
    ]
    user_prompt = (
        "已有摘要（可能为空）：\n"
        + (prev_summary.strip() or "（无）")
        + "\n\n需要并入摘要的新对话内容：\n"
        + "\n".join(transcript_lines)
    )

    try:
        from nexus_server import llm_gateway
        content, model, _stop, _tools = await llm_gateway.call_llm(
            messages=[{"role": "user", "content": user_prompt}],
            system_prompt=_SUMMARIZER_SYSTEM_PROMPT,
            model=None,          # configured default (cheap) model
            temperature=0.2,
            max_tokens=512,
            tools=None,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("session summary LLM call failed (%s/%s): %s",
                       user_id, session_id, exc)
        return False
    summary = (content or "").strip()
    if not summary:
        return False

    try:
        from nexus_server.database import get_db_connection
        with get_db_connection() as conn:
            _ensure_summary_schema(conn)
            conn.execute(
                "INSERT INTO chat_session_summaries "
                "(user_id, session_id, upto_idx, summary, updated_at) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(user_id, session_id) DO UPDATE SET "
                "  upto_idx = excluded.upto_idx, "
                "  summary = excluded.summary, "
                "  updated_at = excluded.updated_at",
                (user_id, session_id, beyond, summary,
                 datetime.now(timezone.utc).isoformat()),
            )
            conn.commit()
    except Exception as exc:  # noqa: BLE001
        logger.warning("session summary upsert failed (%s/%s): %s",
                       user_id, session_id, exc)
        return False
    logger.info(
        "session summary updated: user=%s session=%s upto_idx=%d chars=%d model=%s",
        user_id, session_id, beyond, len(summary), model,
    )
    return True


def schedule_session_summary_update(user_id: str, session_id: str) -> None:
    """Post-turn hook — fire-and-forget summary refresh.

    Does a cheap synchronous staleness pre-check (one COUNT query, no
    LLM) so short sessions never even spawn a task, then schedules
    :func:`maybe_update_session_summary` on the running event loop.
    Never raises; turn latency never waits on the summary.
    """
    if not user_id or not session_id:
        return
    try:
        # Cheap pre-check: total message count only.
        _msgs, total = _default_history_fetcher(user_id, session_id, 1)
        if total - DEFAULT_HISTORY_WINDOW < SUMMARY_MIN_STALE:
            return
    except Exception as exc:  # noqa: BLE001
        logger.debug("summary pre-check failed (%s/%s): %s",
                     user_id, session_id, exc)
        return

    async def _run() -> None:
        try:
            await maybe_update_session_summary(user_id, session_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("background summary task failed (%s/%s): %s",
                           user_id, session_id, exc)

    try:
        loop = asyncio.get_running_loop()
        loop.create_task(_run())
    except RuntimeError:
        # No running loop (sync caller, e.g. a script) — run inline.
        try:
            asyncio.run(_run())
        except Exception as exc:  # noqa: BLE001
            logger.warning("inline summary run failed (%s/%s): %s",
                           user_id, session_id, exc)


# ─────────────────────────────────────────────────────────────────────
# build() — the assembler
# ─────────────────────────────────────────────────────────────────────


def _first_droppable_history_index(history: list[dict]) -> Optional[int]:
    """Oldest history index that trimming may remove.

    Protected: the synthetic summary message (identified by
    :data:`SUMMARY_PREFIX`) and the last :data:`PROTECTED_RECENT_MESSAGES`
    messages (= last 2 turns).
    """
    limit = len(history) - PROTECTED_RECENT_MESSAGES
    for i in range(len(history)):
        if i >= limit:
            return None
        content = str(history[i].get("content") or "")
        if content.startswith(SUMMARY_PREFIX):
            continue
        return i
    return None


def build(
    *,
    system_text: str,
    memory_text: Optional[str] = None,
    user_id: Optional[str] = None,
    retrieval_blocks: Optional[list[RetrievalBlock]] = None,
    history: Optional[list[dict]] = None,
    current_user_message: Optional[str] = None,
    system_tail: str = "",
    budget: Optional[int] = None,
) -> ContextBundle:
    """Assemble the four layers into a :class:`ContextBundle`.

    * ``system_text`` — Layer S body (persona / rules). Never trimmed.
    * ``memory_text`` — Layer M. When ``None`` and ``user_id`` is
      given, read via :func:`get_memory_projection`. Pass ``""`` to
      explicitly skip the memory layer.
    * ``retrieval_blocks`` — Layer R, trimmed lowest-priority-first.
      Rendered in caller order.
    * ``history`` — Layer H messages (oldest-first, may start with the
      synthetic summary message). Trimmed oldest-first with the summary
      and the last 2 turns protected.
    * ``current_user_message`` — appended as the final user message;
      never trimmed.
    * ``system_tail`` — untrimmable text appended AFTER the R layer
      (the skills block: budget-wise part of S, physically last so
      skill instructions keep override semantics).
    * ``budget`` — overall input-token budget; default from
      ``NEXUS_CONTEXT_BUDGET`` env or 32 000.
    """
    budget = budget or _budget_from_env()
    usable = int(budget * (1.0 - BUDGETS["reserve"]))

    if memory_text is None:
        memory_text = get_memory_projection(user_id) if user_id else ""
    memory_text = (memory_text or "").strip()
    # Layer cap for M — applied up front regardless of overall usage.
    m_cap = int(budget * BUDGETS["M"])
    if memory_text and estimate_tokens(memory_text) > m_cap:
        memory_text = _truncate_tail_to_tokens(memory_text, m_cap)

    kept_blocks: list[RetrievalBlock] = [
        b for b in (retrieval_blocks or []) if (b.text or "").strip()
    ]
    hist: list[dict] = list(history or [])

    def _total() -> int:
        t = estimate_tokens(system_text) + estimate_tokens(system_tail)
        t += estimate_tokens(memory_text)
        t += sum(estimate_tokens(b.text) for b in kept_blocks)
        t += sum(_estimate_message(m) for m in hist)
        if current_user_message is not None:
            t += estimate_tokens(current_user_message) + _PER_MESSAGE_OVERHEAD
        return t

    dropped_history = 0
    dropped_blocks = 0

    while _total() > usable:
        # 1. Oldest droppable history message.
        idx = _first_droppable_history_index(hist)
        if idx is not None:
            hist.pop(idx)
            dropped_history += 1
            continue
        # 2. Lowest-priority retrieval block.
        if kept_blocks:
            lowest = min(
                range(len(kept_blocks)),
                key=lambda i: kept_blocks[i].priority,
            )
            kept_blocks.pop(lowest)
            dropped_blocks += 1
            continue
        # 3. Memory, truncated tail-first to exactly fit.
        if memory_text:
            overshoot = _total() - usable
            new_cap = estimate_tokens(memory_text) - overshoot
            memory_text = (
                _truncate_tail_to_tokens(memory_text, new_cap)
                if new_cap > 0 else ""
            )
            # memory shrank (or emptied) → loop re-evaluates and exits
            # via one of the branches or the final break.
            if not memory_text:
                continue
            break
        # Nothing left to trim — S and the current user message are
        # protected by design; accept the overshoot.
        break

    # Compose: S + M (stable prefix) → R → tail. H rides as messages.
    parts: list[str] = []
    if (system_text or "").strip():
        parts.append(system_text)
    if memory_text:
        parts.append(memory_text)
    for b in kept_blocks:
        parts.append(b.text.strip())
    if (system_tail or "").strip():
        parts.append(system_tail.strip())
    final_system = "\n\n".join(parts)

    messages = list(hist)
    if current_user_message is not None:
        messages.append({"role": "user", "content": current_user_message})

    summary_included = bool(
        hist and str(hist[0].get("content") or "").startswith(SUMMARY_PREFIX)
    )

    return ContextBundle(
        system_text=final_system,
        messages=messages,
        dropped={
            "history_msgs": dropped_history,
            "retrieval_blocks": dropped_blocks,
        },
        token_estimate=_total(),
        summary_included=summary_included,
    )
