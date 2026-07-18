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
            except Exception as exc:
                logger.debug("rollback failed: %s", exc)
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
            except asyncio.CancelledError as e:
                logger.debug("reaper task cancelled during shutdown: %s", e)

    async with _lock:
        uids = list(_sessions.keys())
    for uid in uids:
        await close_user(uid)


# ── Introspection (used by /agent/twin-status) ────────────────────────


def is_active(user_id: str) -> bool:
    return user_id in _sessions


def session_count() -> int:
    return len(_sessions)
