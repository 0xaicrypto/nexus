"""Per-user Nexus DigitalTwin lifecycle.

Server owns one DigitalTwin instance per logged-in user, lazy-created
on first chat request and idle-evicted after a configurable timeout.
Replaces the direct LLM gateway path with the full Nexus 9-step flow
(contract pre/post check, EventLog, projection, drift score, background
evolution).

Operating modes (decided per-user at twin creation):

  * **Chain mode** — entered when ALL of the following hold:
      - ``SERVER_PRIVATE_KEY`` is configured (the custodial signing key
        the server uses to sponsor on-chain writes for Web2 users).
      - The user has a ``chain_agent_id`` (ERC-8004 token id) persisted
        in the ``users`` table — typically populated by the existing
        ``/api/v1/chain/register-agent`` endpoint on first signup.
      - A BSC RPC URL is resolvable from config.
    When in chain mode, twin's own ChainBackend is active: every
    event_log append lands in the durable local store and anchors a
    state-root update on BSC.

  * **Local mode** — fallback when chain prereqs are missing. Twin still
    works (DPM event log + projection + memory evolution all run), it
    just doesn't talk to the chain. Useful for fresh signups before
    registration completes, and for offline dev.

Coexistence with legacy server data plane (transitional):

  * Every event twin appends is mirrored to ``sync_events`` via the
    ``on_event`` callback so the existing ``/agent/timeline`` and
    ``/agent/memories`` endpoints keep working without changes. S5
    will retire that mirror once those endpoints read from twin.

  * ``sync_anchor`` and ``chain_proxy`` will be removed in S4/S6 once
    every chat goes through a chain-mode twin (S2 + S6 together — twin
    auto-registers identity in background, removing the need for a
    pre-chat /chain/register-agent round-trip).

Eviction:
  * Default 30 min idle → ``twin.close()`` and remove from registry.
  * Background task is started in main.lifespan, stopped on shutdown.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from nexus_server.config import get_config
from nexus_server.database import get_db_connection

logger = logging.getLogger(__name__)
config = get_config()


# ── Tunables ──────────────────────────────────────────────────────────

# How long a twin can idle in memory before we close it. The next chat
# from that user incurs a cold start (~5-10 sec).
TWIN_IDLE_SECONDS = int(getattr(config, "TWIN_IDLE_SECONDS", 30 * 60))

# How often the eviction task wakes up to check.
TWIN_REAPER_INTERVAL = 60.0

# Where each user's twin stores its private state (event_log SQLite,
# curated memory MD, contracts dir, etc). Per-user subdir.
TWIN_BASE_DIR = Path(
    getattr(config, "TWIN_BASE_DIR",
            Path.home() / ".nexus_server" / "twins")
)


# ── In-memory registry ────────────────────────────────────────────────


@dataclass
class _TwinSession:
    twin: object  # DigitalTwin — but typed as object to keep this module
                  # importable in environments where nexus isn't installed
    last_used: float = field(default_factory=time.time)
    user_id: str = ""

    def touch(self) -> None:
        self.last_used = time.time()


# Module-level state is fine — only one TwinManager per process.
_sessions: dict[str, _TwinSession] = {}
_lock = asyncio.Lock()
_reaper_task: Optional[asyncio.Task] = None
_test_override: Optional[object] = None  # let unit tests inject a fake twin


# ── twin event mirror (deleted in Phase B) ────────────────────────────
#
# Pre-S5 the server mirrored every twin emit into the ``sync_events``
# SQLite table so legacy /agent/timeline and /agent/memories endpoints
# could read events without poking into twin's per-user EventLog.
# After S5 those endpoints opened twin's EventLog directly via
# ``twin_event_log`` (read-only sqlite3 URI mode), making the mirror
# write-only — no production read path consulted it.
#
# Phase B drops both the mirror writes AND the ``sync_events`` table
# itself. The ``twin.on_event`` hook is no longer assigned — twin emits
# nothing into the server's SQLite. Bug 3's chain-activity log handler
# (``twin_chain_events`` table) is unaffected; that data stream lives
# in its own table for a different reason.


# ── Lazy create / cache ───────────────────────────────────────────────


_bootstrap_lock = asyncio.Lock()
# In-process bootstrap mutex per user_id. Threading.Lock would also work,
# but the bootstrap function is called from both async (TwinManager) and
# sync (chain_proxy endpoint) paths; an asyncio.Lock + a process-level
# dict gives us the same protection without inventing a new sync primitive.
_user_bootstrap_locks: dict[str, asyncio.Lock] = {}


def _user_lock(user_id: str) -> asyncio.Lock:
    """Per-user mutex that serialises bootstrap_chain_identity calls.

    Without this, the desktop's POST /chain/register-agent and twin's
    background bootstrap can race for the same user, both call
    ``client.register_agent``, and end up with two distinct ERC-8004
    token ids. The user lands in DB with whichever finished last, the
    bucket name is locked to that, but the on-chain identity twin
    actually owns is the OTHER one — bucket / identity divergence.
    """
    lock = _user_bootstrap_locks.get(user_id)
    if lock is None:
        lock = asyncio.Lock()
        _user_bootstrap_locks[user_id] = lock
    return lock


def bootstrap_chain_identity(user_id: str) -> Optional[int]:
    """Register the user's ERC-8004 identity on BSC if chain is configured
    and they don't already have one.

    Returns the user's ``chain_agent_id`` after the call — either the
    cached value or the newly-registered token id, or ``None`` if chain
    isn't configured / registration failed (in which case the twin runs
    in local mode for this user).

    Concurrency: the check-then-register sequence is wrapped in a
    SQLite ``BEGIN IMMEDIATE`` transaction so two callers racing for
    the same user can't both proceed past the cache check. The first
    one in acquires a write lock on ``users``, sees no chain_agent_id,
    runs the registration, persists, commits. The second one waits on
    the lock, then re-reads inside the same transaction and short-
    circuits with the cached id.

    S6 architecture: twin auto-registers on first start. Until S6 the
    desktop's onboarding flow called ``POST /api/v1/chain/register-agent``
    explicitly; that endpoint now delegates here so registration logic
    lives in exactly one place. Once Round 2-C lands and the desktop
    stops calling /chain/register-agent, this function is the only
    remaining caller.
    """
    cached = _read_chain_agent_id(user_id)
    if cached is not None:
        return cached

    if not config.SERVER_PRIVATE_KEY or not config.chain_active_rpc:
        return None

    # Lazy import: chain_proxy pulls in web3 / BSCClient and we
    # don't want to force that on local-mode setups.
    try:
        from nexus_server import chain_proxy as cp
    except Exception as e:
        logger.warning(
            "bootstrap_chain_identity: chain_proxy import failed: %s", e,
        )
        return None

    client = cp._get_chain_client()
    if client is None:
        return None

    # Resolve a name the contract is happy with (some implementations
    # revert on empty URI). Mirrors chain_proxy.register_chain_agent.
    try:
        with get_db_connection() as conn:
            row = conn.execute(
                "SELECT display_name FROM users WHERE id = ?", (user_id,),
            ).fetchone()
        candidate = (row[0] if row and row[0] else "").strip()
    except Exception:
        candidate = ""
    agent_name = candidate or f"rune-user-{user_id[:8]}"

    # ── Race-safe check-and-register ─────────────────────────────────
    # SQLite BEGIN IMMEDIATE acquires a reserved lock right away,
    # serialising any other writer that's about to do the same. The
    # second caller will wait here, then re-read inside the txn and
    # short-circuit with the row the first caller just wrote.
    with get_db_connection() as conn:
        try:
            conn.execute("BEGIN IMMEDIATE")
        except Exception as e:
            logger.warning("bootstrap: could not acquire write lock: %s", e)
            return None
        try:
            re_check = conn.execute(
                "SELECT chain_agent_id FROM users WHERE id = ?", (user_id,),
            ).fetchone()
            if re_check and re_check[0] is not None:
                conn.rollback()
                logger.info(
                    "bootstrap: another writer registered %s as %s — using cached",
                    user_id, re_check[0],
                )
                return int(re_check[0])

            try:
                token_id = int(client.register_agent(agent_name))
            except Exception as e:
                conn.rollback()
                logger.warning(
                    "bootstrap_chain_identity: register_agent failed for %s: %s",
                    user_id, e,
                )
                return None

            from datetime import datetime, timezone
            now_iso = datetime.now(timezone.utc).isoformat()
            conn.execute(
                "UPDATE users SET chain_agent_id = ?, "
                "chain_register_tx = COALESCE(chain_register_tx, ''), "
                "updated_at = ? WHERE id = ?",
                (token_id, now_iso, user_id),
            )
            conn.commit()
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass
            raise

    logger.info(
        "Twin auto-registered chain identity for %s: token_id=%s",
        user_id, token_id,
    )
    return token_id


def _read_chain_agent_id(user_id: str) -> Optional[int]:
    """Look up the user's ERC-8004 token id from the ``users`` table.

    Populated by /api/v1/chain/register-agent today. Returns ``None``
    if the user hasn't registered yet (in which case the twin will
    fall back to local mode).
    """
    try:
        with get_db_connection() as conn:
            row = conn.execute(
                "SELECT chain_agent_id FROM users WHERE id = ?",
                (user_id,),
            ).fetchone()
    except Exception as e:
        logger.warning("read_chain_agent_id failed for %s: %s", user_id, e)
        return None
    if not row or row[0] is None:
        return None
    try:
        return int(row[0])
    except (TypeError, ValueError):
        return None


def _resolve_chain_kwargs(user_id: str) -> dict:
    """Decide whether this user's twin enters chain mode and, if so,
    return the kwargs to pass to ``DigitalTwin.create``.

    Returns an empty dict when any prerequisite is missing — twin will
    then start in local mode (still fully functional, just doesn't
    anchor to BSC).
    """
    if not config.SERVER_PRIVATE_KEY:
        logger.debug(
            "Twin local mode for %s: SERVER_PRIVATE_KEY not configured",
            user_id,
        )
        return {}
    rpc = config.chain_active_rpc
    if not rpc:
        logger.debug(
            "Twin local mode for %s: no RPC configured for %s",
            user_id, config.NEXUS_NETWORK,
        )
        return {}
    token_id = _read_chain_agent_id(user_id)
    if token_id is None:
        # S6: twin auto-registers identity on first start. If chain is
        # configured, attempt the bootstrap inline before deciding on
        # local mode — this is the path that lets us delete the
        # /chain/register-agent endpoint without leaving users stuck in
        # local mode forever. If bootstrap fails (RPC down, contract
        # revert, etc.) we still fall through to local mode and the
        # twin works without chain writes.
        token_id = bootstrap_chain_identity(user_id)
        if token_id is None:
            logger.info(
                "Twin local mode for %s: no chain_agent_id (auto-register failed or chain disabled)",
                user_id,
            )
            return {}

    net_short = config.network_short
    net_prefix = "MAINNET" if net_short == "mainnet" else "TESTNET"

    return {
        "private_key": config.SERVER_PRIVATE_KEY,
        "network": net_short,
        "rpc_url": rpc or "",
        "agent_state_address": (
            getattr(config, f"NEXUS_{net_prefix}_AGENT_STATE_ADDRESS", "") or ""
        ),
        "task_manager_address": (
            getattr(config, f"NEXUS_{net_prefix}_TASK_MANAGER_ADDRESS", "") or ""
        ),
        "identity_registry_address": (
            getattr(config, f"NEXUS_{net_prefix}_IDENTITY_REGISTRY", "") or ""
        ),
    }


async def _create_twin(user_id: str):
    """Build a fresh DigitalTwin for ``user_id``.

    Chain mode is auto-decided by ``_resolve_chain_kwargs`` based on
    server-wide config + the user's registration state. See module
    docstring for the full state machine.
    """
    # Defer import: nexus_core + nexus pull a lot of optional
    # deps; we want twin_manager to be importable even if they're not.
    from nexus.twin import DigitalTwin

    user_dir = TWIN_BASE_DIR / user_id
    user_dir.mkdir(parents=True, exist_ok=True)

    api_key = config.GEMINI_API_KEY or ""
    if not api_key:
        raise RuntimeError(
            "TwinManager: GEMINI_API_KEY not configured — twin chat path needs it"
        )

    chain_kwargs = _resolve_chain_kwargs(user_id)
    if chain_kwargs:
        logger.info(
            "TwinManager: chain mode for user %s (network=%s)",
            user_id,
            chain_kwargs.get("network"),
        )
        # Bug 2 fix: pass the cached token_id so twin's _initialize
        # pre-seeds its identity cache and skips the background
        # _register_identity task that would otherwise mint a second
        # ERC-8004 token.
        cached_token_id = _read_chain_agent_id(user_id)
        if cached_token_id is not None:
            chain_kwargs["cached_agent_id"] = cached_token_id
    else:
        logger.info("TwinManager: local mode for user %s", user_id)

    twin = await DigitalTwin.create(
        name="Nexus Agent",
        owner=user_id,
        agent_id=f"user-{user_id[:8]}",
        llm_provider="gemini",
        llm_api_key=api_key,
        base_dir=str(user_dir),
        enable_tools=True,
        tavily_api_key=config.TAVILY_API_KEY or "",
        **chain_kwargs,
    )

    # ── Wire the user-scoped file resolver onto twin's file reader.
    # The SDK's ReadUploadedFileTool was constructed in legacy
    # in-memory mode (no resolver) — we now point it at the SQL
    # store so cross-turn / cross-eviction / cross-restart reads
    # all work without the tool ever holding bytes itself. The
    # legacy ``store()`` API stays available for unit tests that
    # haven't been migrated yet (they don't go through twin_manager
    # so they never trigger this branch).
    try:
        from nexus_server.files import (
            resolve_file_text as _resolve_for_user,
            list_user_files as _list_for_user,
        )
        if getattr(twin, "_file_reader", None) is not None:
            twin._file_reader._resolver = (
                lambda fname, _uid=user_id:
                    _resolve_for_user(_uid, fname)
            )
            twin._file_reader._lister = (
                lambda _uid=user_id: _list_for_user(_uid)
            )
            logger.debug(
                "ReadUploadedFileTool resolver bound for user %s", user_id,
            )
    except Exception as e:  # noqa: BLE001
        logger.warning(
            "Could not wire SQL-backed file resolver for %s: %s",
            user_id, e,
        )
    # Phase B: no on_event hook installed — sync_events mirror table
    # was deleted. Twin's emits propagate only to its own EventLog
    # (the canonical store) and to the chain-activity log handler
    # (Bug 3 visibility, separate twin_chain_events table).

    # ── Session metadata replay (#159 续) ──
    # Re-apply any session_metadata events from twin's EventLog to the
    # nexus_sessions SQL table. Covers the case where the SQL DB is
    # fresh (server migrated, volume restored from backup, etc) but
    # the EventLog carries the title/archive history. Idempotent —
    # safe to run on every twin construction.
    try:
        from nexus_server.session_sync import replay_session_metadata
        replay_session_metadata(user_id, twin)
    except Exception as e:  # noqa: BLE001
        logger.debug(
            "session_metadata replay skipped for %s: %s", user_id, e,
        )

    # ── User-scoped tool registration ──
    # Each entry is (module_path, register_fn_name, human_label). The
    # register_fn is expected to take (twin, user_id) and attach its
    # tools to the twin's ToolRegistry. We iterate so adding a 4th
    # tool category is one line — and a single failure doesn't stop
    # the others from registering.
    _USER_SCOPED_TOOL_REGISTRARS = (
        # Phase B: chat-first workflow invocation (list_workflows).
        ("nexus_server.tools_workflow", "register_workflow_tools", "Workflow"),
        # Phase C-1: cross-session memory search (search_past_chats).
        ("nexus_server.tools_memory",   "register_memory_tools",   "Memory"),
        # D-2: ad-hoc subagent delegation (delegate).
        ("nexus_server.tools_subagent", "register_subagent_tools", "Subagent"),
        # #114: macOS Calendar + Mail bridges (read_calendar + compose_email_draft).
        ("nexus_server.tools_calendar", "register_calendar_tools", "Calendar"),
        # #119: SkillOpt-style validation-gated skill evolver.
        ("nexus_server.tools_evolve",   "register_evolve_tools",   "Evolve"),
        # #126: OCR via tesseract for narrow text-extraction needs
        # (vision still default for general image understanding).
        ("nexus_server.tools_ocr",      "register_ocr_tools",      "OCR"),
        # #169: defer_to_background — schedule long-running tasks for
        # async execution + email notification.
        ("nexus_server.tools_async",    "register_async_tools",    "AsyncTasks"),
    )
    for module_path, fn_name, label in _USER_SCOPED_TOOL_REGISTRARS:
        try:
            module = __import__(module_path, fromlist=[fn_name])
            getattr(module, fn_name)(twin, user_id)
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "%s tools not registered for %s: %s", label, user_id, e,
            )
    return twin


async def get_twin(user_id: str):
    """Return a (cached or freshly created) DigitalTwin for ``user_id``.

    Tests can short-circuit by setting :data:`_test_override` to a stub
    that exposes ``async chat(message) -> str`` and ``async close()``.
    """
    if _test_override is not None:
        return _test_override

    async with _lock:
        sess = _sessions.get(user_id)
        if sess is not None:
            sess.touch()
            return sess.twin

        logger.info("TwinManager: cold-starting twin for user %s", user_id)
        twin = await _create_twin(user_id)
        _sessions[user_id] = _TwinSession(twin=twin, user_id=user_id)
        return twin


async def close_user(user_id: str) -> None:
    """Close + drop one user's twin (e.g. on logout, idle eviction)."""
    async with _lock:
        sess = _sessions.pop(user_id, None)
    if sess is None:
        return
    try:
        if hasattr(sess.twin, "close"):
            await sess.twin.close()
    except Exception as e:
        logger.warning("twin.close() failed for %s: %s", user_id, e)


# ── Reaper task (idle eviction) ───────────────────────────────────────


async def _reaper_loop(stop_event: asyncio.Event) -> None:
    logger.info(
        "TwinManager reaper: interval=%.0fs idle_cap=%ds",
        TWIN_REAPER_INTERVAL, TWIN_IDLE_SECONDS,
    )
    while not stop_event.is_set():
        try:
            now = time.time()
            stale_uids: list[str] = []
            async with _lock:
                for uid, sess in _sessions.items():
                    if now - sess.last_used > TWIN_IDLE_SECONDS:
                        stale_uids.append(uid)
            for uid in stale_uids:
                logger.info("TwinManager: evicting idle twin user=%s", uid)
                await close_user(uid)
        except Exception as e:
            logger.warning("Twin reaper tick failed: %s", e)

        try:
            await asyncio.wait_for(
                stop_event.wait(), timeout=TWIN_REAPER_INTERVAL
            )
            return  # stop signalled
        except asyncio.TimeoutError:
            continue


def start_reaper() -> tuple[asyncio.Task, asyncio.Event]:
    """Spin up the eviction loop. Call once from main.lifespan startup."""
    stop_event = asyncio.Event()
    task = asyncio.create_task(_reaper_loop(stop_event), name="twin-reaper")
    return task, stop_event


# ── Chain activity log capture (Bug 3 visibility) ─────────────────────
#
# After S4 the UI's anchor counters (read from sync_anchors) became
# permanently 0 for chat-mode users — twin's ChainBackend writes BSC
# anchors directly, no longer touching that table. Failures landed
# only in stderr, invisible to the operator.
#
# We add a logging.Handler that subscribes to the SDK's
# ``rune.backend.chain`` logger, parses the success/failure messages,
# identifies the user via agent_id prefix, and persists one row per
# attempt into ``twin_chain_events``. The /agent/state and
# /agent/timeline endpoints then count + render those rows so the
# desktop sidebar reflects what's actually happening on chain.
#
# Why log scraping instead of an in-process callback? The SDK's
# ChainBackend is a generic library; threading an ``on_event`` hook
# through every put/get/anchor call is a large API change. Logs are
# already structured ("[WRITE][BSC] Anchor OK: ...") and stable; this
# handler is a 50-line bridge that costs nothing on the hot path. If
# we ever want richer signals we can graduate to a proper SDK callback.


import re as _re


# Pre-compiled regexes match the exact format strings in
# nexus_core.backends.chain. If you change those format strings,
# update these — there's a regression test that injects synthetic
# LogRecords to keep the pair in lockstep.
_RE_BSC_OK = _re.compile(
    r"\[WRITE\]\[BSC\] Anchor OK: agent=(?P<agent>[\w-]+) "
    r"hash=(?P<hash>[0-9a-fA-F]+) tx=(?P<tx>[0-9a-fA-F]+)"
    r"(?: \((?P<dur>[\d.]+)s\))?"
)
# BSC anchor failure — emitted by chain.py's anchor except branch.
# Structured fields so we can record a failure chain event with the
# actual revert reason instead of just a generic warning. Without
# this, BSC failures were utterly invisible to the desktop UI.
_RE_BSC_FAIL = _re.compile(
    r"\[FALLBACK\]\[BSC\] Anchor failed for (?P<agent>[\w-]+) (?:— )?"
    r"agent=\S+ hash=(?P<hash>[0-9a-fA-F]+) reason=(?P<reason>.+)"
)


def _user_id_for_agent(agent_id_str: str) -> Optional[str]:
    """Reverse the ``user-{user_id[:8]}`` derivation.

    twin_manager._create_twin builds agent_id as ``user-{user_id[:8]}``,
    so we look up users whose id starts with the 8 chars. This is a
    lossy mapping — there's a 1-in-2^32 chance of collision — but for
    the scale we care about (small operator deployments) it's fine.
    Multi-tenant correctness for SaaS would want a dedicated mapping
    table.
    """
    if not agent_id_str.startswith("user-"):
        return None
    prefix = agent_id_str[len("user-"):]
    if not prefix or len(prefix) > 64:
        return None
    try:
        with get_db_connection() as conn:
            row = conn.execute(
                "SELECT id FROM users WHERE id LIKE ? LIMIT 2",
                (prefix + "%",),
            ).fetchall()
    except Exception:
        return None
    if len(row) != 1:
        # Either no match or ambiguous — don't guess.
        return None
    return row[0][0]


def _record_chain_event(
    user_id: str,
    kind: str,
    status: str,
    summary: str = "",
    tx_hash: Optional[str] = None,
    content_hash: Optional[str] = None,
    object_path: Optional[str] = None,
    error: Optional[str] = None,
    duration_ms: Optional[int] = None,
) -> None:
    """Insert a row into twin_chain_events. Best-effort — never raises."""
    from datetime import datetime, timezone
    now_iso = datetime.now(timezone.utc).isoformat()
    try:
        with get_db_connection() as conn:
            conn.execute(
                """
                INSERT INTO twin_chain_events
                (user_id, kind, status, summary, tx_hash, content_hash,
                 object_path, error, duration_ms, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    user_id, kind, status, summary or None,
                    tx_hash, content_hash, object_path,
                    (error[:512] if error else None),
                    duration_ms, now_iso,
                ),
            )
            conn.commit()
    except Exception as e:
        logger.debug("twin_chain_events insert failed: %s", e)


class _ChainActivityLogHandler(logging.Handler):
    """Watch the ``rune.backend.chain`` logger for chain write
    activity and persist rows into twin_chain_events.

    Multi-tenant attribution works by matching the agent_id token in
    the BSC anchor message to a user row.
    """

    def __init__(self) -> None:
        super().__init__(level=logging.WARNING)
        # Most recently attributed twin user — kept for best-effort
        # attribution of log lines that don't carry agent_id.
        self._last_user: Optional[str] = None

    def emit(self, record: logging.LogRecord) -> None:  # noqa: D401
        try:
            self._dispatch(record)
        except Exception:
            # Never let a logging handler crash the producing logger.
            pass

    def _dispatch(self, record: logging.LogRecord) -> None:
        msg = record.getMessage()
        name = record.name

        if name == "nexus_core.backend.chain":
            m = _RE_BSC_OK.search(msg)
            if m:
                uid = _user_id_for_agent(m.group("agent"))
                if uid:
                    self._last_user = uid
                    dur = m.group("dur")
                    duration_ms = int(float(dur) * 1000) if dur else None
                    _record_chain_event(
                        uid,
                        kind="bsc_anchor",
                        status="ok",
                        summary=f"Anchored on BSC (tx {m.group('tx')[:10]}…)",
                        tx_hash=m.group("tx"),
                        content_hash=m.group("hash"),
                        duration_ms=duration_ms,
                    )
                return

            # BSC anchor failure: same dispatch logic as the OK branch,
            # but records `status=failed` so the desktop's chain-events
            # log can render the row in red. Try the FAIL pattern AFTER
            # the OK pattern (OK is the common path — checking it first
            # is a tiny perf win; the regexes are mutually exclusive).
            m = _RE_BSC_FAIL.search(msg)
            if m:
                uid = _user_id_for_agent(m.group("agent"))
                if uid:
                    self._last_user = uid
                    _record_chain_event(
                        uid,
                        kind="bsc_anchor",
                        status="failed",
                        summary="BSC anchor reverted — local fallback only",
                        content_hash=m.group("hash"),
                        error=m.group("reason"),
                    )
                return


_chain_log_handler: Optional[_ChainActivityLogHandler] = None


def install_chain_activity_handler() -> None:
    """Attach :class:`_ChainActivityLogHandler` to the SDK loggers.

    Idempotent: calling twice replaces the handler so a hot-reload in
    development doesn't end up with two duplicates writing the same
    rows. Should be called once from main.lifespan startup, after
    init_db (so the target table exists).
    """
    global _chain_log_handler
    if _chain_log_handler is not None:
        # Detach previous instance first so we don't double-write.
        logging.getLogger("nexus_core.backend.chain").removeHandler(_chain_log_handler)
    _chain_log_handler = _ChainActivityLogHandler()
    logging.getLogger("nexus_core.backend.chain").addHandler(_chain_log_handler)
    logger.info("Chain activity log handler installed (twin_chain_events)")


def uninstall_chain_activity_handler() -> None:
    """Detach the handler. Used in tests + on shutdown."""
    global _chain_log_handler
    if _chain_log_handler is None:
        return
    logging.getLogger("nexus_core.backend.chain").removeHandler(_chain_log_handler)
    _chain_log_handler = None


async def shutdown_all(stop_event: asyncio.Event,
                       reaper_task: asyncio.Task | None) -> None:
    """Stop the reaper and close every active twin. Lifespan teardown."""
    stop_event.set()
    if reaper_task is not None:
        try:
            await asyncio.wait_for(reaper_task, timeout=5.0)
        except asyncio.TimeoutError:
            reaper_task.cancel()
            try:
                await reaper_task
            except asyncio.CancelledError:
                pass

    async with _lock:
        uids = list(_sessions.keys())
    for uid in uids:
        await close_user(uid)


# ── Introspection (used by /agent/twin-status) ────────────────────────


def is_active(user_id: str) -> bool:
    return user_id in _sessions


def session_count() -> int:
    return len(_sessions)
