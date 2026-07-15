"""FastAPI application assembly and entry point.

Creates and configures the main FastAPI application with:
  - Routers (auth, llm_gateway, chain_proxy, agent_state, files,
    user_profile) — note ``sync_hub`` was retired in
    Phase B when the desktop became a thin client.
  - CORS middleware
  - Exception handlers
  - Health check endpoint
  - Lifecycle management (startup/shutdown)
"""

import argparse
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path


# Load .env before anything reads os.getenv.
#
# Lookup order (first to set a key wins; later files only fill blanks):
#   1. cwd .env                — operator/CI override
#   2. packages/server/.env    — server-specific (SERVER_PRIVATE_KEY, JWT, …)
#   3. packages/sdk/.env       — network-level fallback (NEXUS_TESTNET_RPC,
#                                contract addresses) so chain_proxy can find
#                                network config without duplicating it.
#
# Custodial signing key (SERVER_PRIVATE_KEY) is server-only and never read
# from sdk/.env; sdk/.env is only used here as a network/contract config
# source. SDK's NEXUS_PRIVATE_KEY may also be present — we let it through
# into os.environ because SDK code may consult it, but chain_proxy treats
# it as ignored.
def _load_dotenv():
    server_pkg = Path(__file__).parent.parent
    sdk_env = server_pkg.parent / "sdk" / ".env"
    candidates = [
        Path(".env"),
        server_pkg / ".env",
        sdk_env,
    ]
    for p in candidates:
        if not p.exists():
            continue
        for line in p.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key, val = key.strip(), val.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = val
        # Note: we keep walking — earlier files take precedence via the
        # `key not in os.environ` guard, while later files fill in any
        # leftover blanks (e.g. sdk/.env supplies NEXUS_TESTNET_RPC).

_load_dotenv()

# Configure file + console logging.
#
# Log file location is resolved to an *always-writable* directory so
# nexus_server doesn't crash when its source tree happens to be on a
# read-only filesystem — which is the case when the server is launched
# from inside a packaged macOS .app bundle (Contents/Resources/ is
# read-only). Resolution order:
#   1. NEXUS_LOG_DIR env var if set
#   2. RUNE_HOME_EXPORT env var (set by the desktop's start.sh)
#   3. ~/Library/Application Support/RuneProtocol (mac default)
#   4. Current working directory (legacy fallback for plain `dotnet run`
#      and pytest runs where cwd is the repo root and writable)
def _resolve_log_path() -> str:
    candidates = [
        os.environ.get("NEXUS_LOG_DIR"),
        os.environ.get("RUNE_HOME_EXPORT"),
        os.path.expanduser("~/Library/Application Support/RuneProtocol"),
        os.getcwd(),
    ]
    for c in candidates:
        if not c:
            continue
        try:
            os.makedirs(c, exist_ok=True)
            # Verify writability with a touch — `os.access(W_OK)` lies
            # on some macOS filesystems (e.g. read-only DMG mounts
            # report writable until the open() call).
            probe = os.path.join(c, ".nexus_log_probe")
            with open(probe, "a"):
                pass
            os.remove(probe)
            return os.path.join(c, "nexus_server.log")
        except OSError:
            continue
    # Last-resort: log only to stderr.
    return ""

_log_path = _resolve_log_path()
_handlers: list[logging.Handler] = [logging.StreamHandler()]
if _log_path:
    _handlers.insert(0, logging.FileHandler(_log_path, mode="a"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=_handlers,
)
# Suppress noisy HTTP debug logs
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("httpx").setLevel(logging.WARNING)

from typing import AsyncGenerator

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from nexus_server import (
    agent_state,
    auth,
    billing_routes,
    chain_proxy,
    files,
    llm_gateway,
    sessions_router,
    thinking_stream,
    user_profile,
    workflows_router,
)

# Phase B: ``sync_hub`` is gone (raises ImportError). /sync/push and
# /sync/pull retired after Round 2 made the desktop a thin client.
from nexus_server.config import get_config
from nexus_server.database import init_db

logger = logging.getLogger(__name__)
config = get_config()


# ───────────────────────────────────────────────────────────────────────────
# F26.3 — Soft-deleted identity GC
# ───────────────────────────────────────────────────────────────────────────

_SOFT_DELETE_RETENTION_DAYS = 90


def _gc_soft_deleted_identities() -> None:
    """Hard-delete users + every user-scoped projection where the row
    was soft-deleted more than ``_SOFT_DELETE_RETENTION_DAYS`` ago.

    Runs at server boot. Idempotent — no rows over the threshold ⇒
    no-op. The 90-day window is the medic's grace period to undelete
    (see §6.4); after that we're contractually committed to actually
    forget the data.
    """
    import sqlite3
    from datetime import datetime, timedelta, timezone

    from nexus_server.database import get_db_connection

    cutoff = (datetime.now(timezone.utc)
              - timedelta(days=_SOFT_DELETE_RETENTION_DAYS)).isoformat()

    with get_db_connection() as conn:
        try:
            stale = conn.execute(
                "SELECT id FROM users "
                "WHERE deleted_at IS NOT NULL AND deleted_at < ?",
                (cutoff,),
            ).fetchall()
        except sqlite3.Error:
            # users.deleted_at may not exist on a partial migration
            # (first boot on an old DB). The init_db ALTER above
            # adds it idempotently, but if we got here before that
            # ran, just skip — next boot will catch it.
            return

        if not stale:
            return

        user_ids = [r[0] for r in stale]
        logger.warning(
            "identity GC: hard-deleting %d users past %d-day retention",
            len(user_ids), _SOFT_DELETE_RETENTION_DAYS,
        )
        for uid in user_ids:
            # Mirror the cascade in routes.wipe_identity. Wrap each
            # table delete so one missing table doesn't abort the
            # whole sweep (partial schema during migration).
            for table in (
                "clinical_graph_nodes", "clinical_graph_edges",
                "node_provenance", "practitioner_observations",
                "practitioner_facts", "chat_takeaways",
                "patient_memory", "patients", "uploads",
                "twin_event_log",
            ):
                try:
                    conn.execute(
                        f"DELETE FROM {table} WHERE user_id = ?", (uid,),
                    )
                except sqlite3.Error as e:
                    logger.debug("delete from %s failed: %s", table, e)
            try:
                conn.execute(
                    "UPDATE sessions SET patient_hash = '' "
                    "WHERE user_id = ?", (uid,),
                )
            except sqlite3.Error as e:
                logger.debug("clearing session patient_hash failed: %s", e)
            conn.execute("DELETE FROM users WHERE id = ?", (uid,))
        conn.commit()
        logger.info("identity GC: complete (%d users gone)", len(user_ids))


# ───────────────────────────────────────────────────────────────────────────
# Response Models
# ───────────────────────────────────────────────────────────────────────────


def _read_build_info() -> dict[str, str]:
    """Read the BUILD_INFO file stamped by build-macos.sh at package
    time. Returns ``{version, build, built_at}`` or sensible "dev"
    defaults when the file is absent (e.g. during ``pytest`` runs in
    the source tree, where there's no .dmg build step).
    """
    info_path = Path(__file__).parent / "BUILD_INFO"
    out = {"version": "dev", "build": "0", "built_at": "unknown"}
    try:
        if info_path.exists():
            for line in info_path.read_text().splitlines():
                line = line.strip()
                if not line or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                out[k.strip()] = v.strip()
    except Exception as e:  # noqa: BLE001
        logger.debug("reading build info failed: %s", e)
    return out


BUILD_INFO = _read_build_info()


class HealthCheckResponse(BaseModel):
    """Health check response.

    ``version`` / ``build`` / ``built_at`` reflect the code build that
    shipped this server. ``api_version`` / ``min_client_api_version``
    are the protocol-level compatibility gates the desktop checks on
    every launch to decide whether to show an upgrade banner.
    """

    status: str
    timestamp: str
    version: str = BUILD_INFO["version"]
    build: str = BUILD_INFO["build"]
    built_at: str = BUILD_INFO["built_at"]
    # Protocol version — increment when making breaking API changes.
    api_version: int = config.API_VERSION
    # Oldest client the server still accepts without a warning.
    min_client_api_version: int = config.MIN_CLIENT_API_VERSION


# ───────────────────────────────────────────────────────────────────────────
# Lifecycle
# ───────────────────────────────────────────────────────────────────────────


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Manage application lifecycle (startup/shutdown).

    Spins up the TwinManager idle-eviction reaper + chain-activity log
    handler on startup, drains them on shutdown so the process exits
    cleanly. Phase B removed the legacy anchor retry daemon — see
    sync_anchor.py for the tombstone explanation.
    """
    import asyncio as _asyncio
    import os as _os

    # F-bundled-sidecar-silent-exit — Tauri pipes our stderr to the
    # desktop's diag panel via a non-blocking IPC. If the FastAPI
    # lifespan raises BEFORE the first chat round, we've seen the
    # traceback occasionally drop on the floor (pipe buffer never
    # flushed before the process exits). Belt-and-suspenders: also
    # write any startup failure to a file in $RUNE_HOME so the medic
    # can grep it post-mortem. The path is opportunistic — if
    # $RUNE_HOME isn't writable we silently continue with stderr only.
    _crash_dump_path = None
    try:
        # The Tauri sidecar exports RUNE_HOME_EXPORT (same var the log
        # path resolver uses); plain RUNE_HOME kept as a fallback for
        # manual runs. Reading only RUNE_HOME here meant the dump never
        # fired inside the bundle.
        _rune_home = (_os.environ.get("RUNE_HOME_EXPORT")
                      or _os.environ.get("RUNE_HOME"))
        if _rune_home:
            _crash_dir = Path(_rune_home) / "logs"
            _crash_dir.mkdir(parents=True, exist_ok=True)
            _crash_dump_path = _crash_dir / "sidecar-startup-failure.log"
    except Exception:
        _crash_dump_path = None

    def _dump_startup_failure(stage: str, exc: BaseException) -> None:
        import datetime as _dt
        import traceback as _tb
        if _crash_dump_path is None:
            return
        try:
            with _crash_dump_path.open("a", encoding="utf-8") as f:
                f.write(
                    f"\n=== {_dt.datetime.now(_dt.timezone.utc).isoformat()} "
                    f"startup failed at: {stage} ===\n"
                )
                f.write(f"build={BUILD_INFO.get('build')} "
                        f"version={BUILD_INFO.get('version')}\n")
                _tb.print_exception(
                    type(exc), exc, exc.__traceback__, file=f,
                )
                f.write("=== end ===\n")
        except Exception:
            pass

    # Startup
    logger.info(
        "Starting Nexus API Server (build %s, version %s, built_at %s)",
        BUILD_INFO["build"], BUILD_INFO["version"], BUILD_INFO["built_at"],
    )
    config.validate()
    # U3.4 — Alembic migrations BEFORE init_db, because the runner's
    # 0001_initial migration delegates back to init_db. Calling
    # alembic upgrade head first means:
    #   * Fresh install   → 0001 runs init_db + all init_*_table, sets
    #                       alembic_version = "0001".
    #   * Upgrade install → 0001 already applied (alembic_version says
    #                       so) → skipped. Any new 0002/3/... ALTERs
    #                       or data backfills run now.
    # On failure we ABORT startup — broken schema is worse than a
    # missing server (the medic gets a clear "backend down" banner).
    try:
        from nexus_server.migrations.runner import run_migrations
        head = run_migrations()
        logger.info("DB migrations applied; head=%s", head)
    except Exception as exc:
        logger.exception("DB migration failed — refusing to start: %s", exc)
        _dump_startup_failure("run_migrations", exc)
        raise
    # init_db remains as a belt-and-suspenders idempotent call for
    # any tables not yet captured in 0001 (e.g. modules that lazy-init
    # on first request). Once every init_*_table is mirrored in a
    # migration, this line can be removed.
    init_db()

    # Hydrate any DB-persisted LLM settings (API keys, provider, model)
    # into os.environ + ServerConfig BEFORE anything that reads
    # `config.GEMINI_API_KEY` etc. runs. .env values that were exported
    # by the Tauri sidecar at launch still win (we don't overwrite
    # non-empty env keys); the DB only fills the gaps. This is what
    # lets a freshly-installed bundle pick up keys the medic saved
    # months ago without forcing them to retype.
    try:
        from nexus_server.settings_router import hydrate_env_from_db
        hydrate_env_from_db()
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "hydrate_env_from_db failed: %s — falling back to .env only",
            exc,
        )

    # F26.3 — soft-deleted users 90-day GC. Idempotent + cheap (one
    # SQL DELETE bounded by deleted_at). Run on every boot rather
    # than scheduling a cron — boots are infrequent enough (~1/day
    # per medic) and the cost of running on every boot is negligible.
    try:
        _gc_soft_deleted_identities()
    except Exception as exc:  # noqa: BLE001
        logger.warning("identity GC failed: %s", exc)

    # #135 — semantic memory index. Idempotent; safe to call every boot.
    # If sqlite-vec isn't installed (broken venv) we log and continue —
    # search_chunks raising EmbeddingUnavailable later is a better
    # failure mode than refusing to start the server.
    try:
        from nexus_server.vector_index import init_vector_index
        init_vector_index()
    except Exception as e:  # noqa: BLE001
        logger.warning(
            "vector_index init failed: %s — semantic search will be "
            "unavailable until this is fixed", e,
        )

    # #140 — DICOM study / series index. Same defensive pattern as
    # vector_index — schema setup must never block server boot
    # (a broken pydicom install would otherwise lock the medic out
    # of every other feature).
    try:
        from nexus_server.dicom import init_dicom_index
        init_dicom_index()
        # #144 — RT contour tables sit alongside dicom_studies in the
        # same DB. Init right after so a fresh install gets everything.
        from nexus_server.dicom_router import init_rt_tables
        init_rt_tables()
        # #181 — manually-registered patient roster table. Lives in
        # the same DB as dicom_studies so /patients/full can JOIN.
        from nexus_server.patients_router import init_patients_table
        init_patients_table()
    except Exception as e:  # noqa: BLE001
        logger.warning(
            "dicom_index init failed: %s — medical imaging will be "
            "unavailable until this is fixed", e,
        )

    # #169 — async background task queue + worker. Spawns a single
    # asyncio task that drains the async_tasks SQLite queue forever:
    # picks up queued tasks, runs them through twin.chat, emails the
    # result, posts a completion card into the chat session. Defensive
    # init pattern (table create + worker spawn) matches the other
    # background components above.
    try:
        from nexus_server import async_tasks
        async_tasks._init_db()
        async_tasks.start_worker()
    except Exception as e:  # noqa: BLE001
        logger.warning(
            "async_tasks init failed: %s — defer_to_background tool "
            "will accept calls but no worker will execute them",
            e,
        )

    # #195 / ADR-002 Rev-8 — event-sourcing foundation. Brings up the
    # canonical twin_event_log + projection_state + all v3 projection
    # tables (clinical_graph_nodes/edges, node_provenance, cached_views,
    # practitioner_facts/observations, reference_knowledge). Idempotent;
    # safe to call every boot. Defensive: failure here would block all
    # M0+ memory features but must not block server start (auth + DICOM
    # path stays usable).
    try:
        from nexus_server.database import get_db_connection
        from nexus_server.event_sourcing import init_event_sourcing_schema
        with get_db_connection() as _es_conn:
            init_event_sourcing_schema(_es_conn)
            # #213 — apply any pending schema migrations (Rev-7 §16.6).
            from nexus_server.es_migrations import apply_pending
            applied = apply_pending(_es_conn)
            if applied:
                logger.info("schema migrations applied: %s", applied)
        logger.info("event-sourcing schema initialised (Rev-8 SSOT contract)")
    except Exception as e:  # noqa: BLE001
        logger.warning(
            "event_sourcing init failed: %s — memory_router_v2 endpoints "
            "will be unavailable until this is fixed", e,
        )

    # #213 — D1 daily snapshot scheduler (Rev-7 / §16.3 Tier 2).
    # Fires once per ~24h; rolling 30 daily / 12 weekly / 24 monthly
    # retention. Snapshot files land in ~/Documents/Nexus Archive/.
    try:
        import pathlib as _pl

        from nexus_server.persistence import start_snapshot_scheduler
        _db_path = _pl.Path(config.DATABASE_URL.replace("sqlite:///", ""))
        if _db_path.exists():
            start_snapshot_scheduler(_db_path)
            logger.info("daily snapshot scheduler started (Rev-7 D1)")
    except Exception as e:  # noqa: BLE001
        logger.warning("snapshot scheduler failed to start: %s", e)

    # Phase B: the anchor retry daemon was removed entirely. After S4
    # nothing in production created retryable rows (twin's ChainBackend
    # owns anchoring directly), and the daemon stayed opt-in for an
    # operator-on-demand drain. With Phase B's full sync_anchor cleanup
    # the daemon is gone — the read-only ``list_anchors_for_user`` view
    # remains for legacy history.
    daemon_task = None
    stop_event = None

    # Phase D: TwinManager idle reaper. Only spin it up when twin is
    # enabled, so the legacy LLM gateway path doesn't pay the import
    # cost of nexus.
    twin_reaper_task = None
    twin_stop_event = None
    if config.USE_TWIN and _os.environ.get("NEXUS_DISABLE_TWIN_REAPER") != "1":
        try:
            from nexus_server import twin_manager
            twin_reaper_task, twin_stop_event = twin_manager.start_reaper()
            # Bug 3: capture SDK chain activity into twin_chain_events
            # so /agent/state and /agent/timeline can surface anchor
            # successes / failures to the desktop sidebar.
            twin_manager.install_chain_activity_handler()
        except Exception as e:
            logger.warning(
                "TwinManager reaper failed to start (twin path disabled): %s", e
            )

    # Scheduled Tasks Phase 1 — spawn the worker that polls
    # scheduled_tasks every 30s for due rows. Best-effort init: if
    # the table is missing (migration didn't run) the worker logs +
    # keeps polling, and the next migration will unblock it.
    scheduler_task = None
    scheduler_stop = None
    try:
        from nexus_server import scheduler as _sched
        scheduler_task, scheduler_stop = _sched.start_worker()
        logger.info("scheduled-tasks worker started")
    except Exception as e:  # noqa: BLE001
        logger.warning(
            "scheduled-tasks worker failed to start: %s — confirm/list "
            "endpoints still work but no task will ever fire", e,
        )

    try:
        yield
    finally:
        # Stop the scheduled-tasks worker cleanly.
        if scheduler_stop is not None and scheduler_task is not None:
            scheduler_stop.set()
            try:
                await _asyncio.wait_for(scheduler_task, timeout=5.0)
            except _asyncio.TimeoutError:
                logger.warning(
                    "scheduled-tasks worker did not stop in 5s; cancelling."
                )
                scheduler_task.cancel()
                try:
                    await scheduler_task
                except _asyncio.CancelledError as err:
                    logger.debug("scheduler task cancelled during shutdown: %s", err)
        # Shutdown
        logger.info("Shutting down Nexus API Server")
        if daemon_task is not None and stop_event is not None:
            stop_event.set()
            try:
                await _asyncio.wait_for(daemon_task, timeout=5.0)
            except _asyncio.TimeoutError:
                logger.warning(
                    "Anchor retry daemon did not stop in 5s; cancelling."
                )
                daemon_task.cancel()
                try:
                    await daemon_task
                except _asyncio.CancelledError as err:
                    logger.debug("anchor retry daemon cancelled during shutdown: %s", err)

        if twin_stop_event is not None:
            try:
                from nexus_server import twin_manager
                twin_manager.uninstall_chain_activity_handler()
                await twin_manager.shutdown_all(twin_stop_event, twin_reaper_task)
            except Exception as e:
                logger.warning("TwinManager shutdown failed: %s", e)


# ───────────────────────────────────────────────────────────────────────────
# Exception Handlers
# ───────────────────────────────────────────────────────────────────────────


async def http_exception_handler(
    request: Request,
    exc: HTTPException,
) -> JSONResponse:
    """Handle HTTP exceptions with consistent format.

    Args:
        request: Request object
        exc: HTTPException raised

    Returns:
        JSONResponse with error details
    """
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "error": exc.detail,
            "status_code": exc.status_code,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        },
    )


async def generic_exception_handler(
    request: Request,
    exc: Exception,
) -> JSONResponse:
    """Handle unexpected exceptions.

    Args:
        request: Request object
        exc: Exception raised

    Returns:
        JSONResponse with error details
    """
    logger.error(f"Unhandled exception: {exc}", exc_info=True)
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={
            "error": "Internal server error",
            "status_code": 500,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        },
    )


# ───────────────────────────────────────────────────────────────────────────
# Application Factory
# ───────────────────────────────────────────────────────────────────────────


def create_app() -> FastAPI:
    """Create and configure FastAPI application.

    Returns:
        Configured FastAPI application instance
    """
    app = FastAPI(
        title="Nexus API",
        description=(
            "Modular FastAPI server: LLM Gateway, Auth Provider, "
            "Data Sync Hub, and Chain Proxy"
        ),
        version="0.1.0",
        lifespan=lifespan,
    )

    # CORS middleware.
    #
    # Wildcard handling: CORS spec forbids `Access-Control-Allow-Origin: *`
    # combined with `Access-Control-Allow-Credentials: true` — browsers
    # reject the preflight. We rely on Bearer JWT in the Authorization
    # header (not cookies), so credentials=true is not actually required
    # for our auth flow. When CORS_ALLOW_ORIGINS="*" we disable
    # allow_credentials so the wildcard works correctly. This is what
    # the bundled desktop sets (the webview origin is tauri://localhost
    # or asset://localhost depending on macOS version — wildcard side-
    # steps the version difference, and the backend is bound to 127.0.0.1
    # so off-host requests can't reach it anyway).
    is_wildcard = config.CORS_ALLOW_ORIGINS == "*"
    cors_origins = ["*"] if is_wildcard else config.CORS_ALLOW_ORIGINS.split(",")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=cors_origins,
        allow_credentials=not is_wildcard,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Exception handlers
    app.add_exception_handler(HTTPException, http_exception_handler)
    app.add_exception_handler(Exception, generic_exception_handler)

    # Health check endpoint. Both /health and /healthz are supported —
    # /health is the FastAPI convention, /healthz is the Kubernetes
    # convention. Docker HEALTHCHECK + Caddy upstream probe + the
    # desktop's pre-flight ping all hit /healthz, so we keep both.
    async def _health() -> HealthCheckResponse:
        return HealthCheckResponse(
            status="healthy",
            timestamp=datetime.now(timezone.utc).isoformat(),
        )

    @app.get("/health", response_model=HealthCheckResponse, tags=["health"])
    async def health_check() -> HealthCheckResponse:
        return await _health()

    @app.get("/healthz", response_model=HealthCheckResponse, tags=["health"],
             include_in_schema=False)
    async def healthz() -> HealthCheckResponse:
        return await _health()

    # Build identity endpoint — frontend hits this on launch and compares
    # to its own bundled VITE_NEXUS_BUILD_ID. A mismatch means the user
    # has a stale .app talking to a fresh backend (or vice versa).
    @app.get("/api/v1/build", tags=["health"])
    async def build_info():
        try:
            from nexus_server.__build_info__ import (
                BUILD_ID,
                BUILD_TIME,
                GIT_SHA,
                VERSION,
            )
        except Exception:
            BUILD_ID = BUILD_TIME = GIT_SHA = VERSION = "unknown"
        return {
            "version":    VERSION,
            "build_id":   BUILD_ID,
            "build_time": BUILD_TIME,
            "git_sha":    GIT_SHA,
        }

    # Include routers with API prefixes
    app.include_router(auth.router)
    # Admin console — user list / disable / enable / reset-password.
    # Every route requires role='admin' via require_admin.
    from nexus_server import admin_router as _admin_router
    app.include_router(_admin_router.router)
    app.include_router(llm_gateway.router)
    app.include_router(chain_proxy.router)
    # Stripe billing — checkout / portal / webhook / status. Routes
    # gracefully return 501 when STRIPE_SECRET_KEY isn't set, so the
    # router is always wired regardless of deployment mode.
    app.include_router(billing_routes.router)
    app.include_router(user_profile.router)
    app.include_router(agent_state.router)
    # /api/v1/files/upload — desktop streams attachments here so the
    # next /llm/chat call can reference them by file_id without
    # re-uploading bytes. Round 2-B feature; the router was somehow
    # never wired into the app on the rename, which is why uploads
    # have been 404'ing in the desktop ("Skipped: file.pdf 404").
    app.include_router(files.router)
    # F-unified-chat-files — per-chat file library (patient / research /
    # cross-research / assistant), all 4 chat surfaces share the same
    # REST shape. See docs/design/UNIFIED_CHAT_FILES.md.
    from nexus_server import chat_files_router
    app.include_router(chat_files_router.router)
    # Phase B: legacy /api/v1/sync/anchors read endpoint moved out of
    # the deleted sync_hub into agent_state.sync_router. Same path,
    # different module.
    app.include_router(agent_state.sync_router)
    # Multi-session support: list / create / rename / archive chat threads.
    # Lives at /api/v1/sessions; the desktop sidebar lists these so users
    # can hold multiple parallel conversations with the same agent.
    app.include_router(sessions_router.router)
    app.include_router(workflows_router.router)
    # NOTE: the legacy /api/v1/agent/memory router (memory_router.py, v1)
    # was retired — no frontend/CLI callers; memory_router_v2 is canonical.
    # Live agent thinking stream (Server-Sent Events). The desktop's
    # cognition panel opens a long-lived connection and renders typed
    # reasoning steps (memory recall, tool calls, Gemini thinking
    # tokens, evolution proposals) as the agent runs each chat turn.
    app.include_router(thinking_stream.router)
    # #130 — Expert feedback loop: medic clicks ✓ Accept / ✗ Correct
    # on an assistant message; we persist to per-skill feedback.jsonl
    # which #131 vision-grounded skill evolution consumes as its
    # training corpus.
    from nexus_server import feedback as _fb
    app.include_router(_fb.router)
    # #172 — async tasks list endpoint for the desktop task-list UI.
    # Worker spawn happens in lifespan() above; this just exposes
    # GET /api/v1/async-tasks so the desktop can poll for status.
    from nexus_server import async_tasks as _async_tasks
    app.include_router(_async_tasks.router)
    # #176 — per-patient memory CRUD (get/put/append). Schema
    # migration is idempotent and runs on first request.
    from nexus_server import patient_memory as _pmem
    app.include_router(_pmem.router)
    # #195 / ADR-002 Rev-8 — v3 memory layer HTTP surface. Layer 1
    # projection reads (findings/medications/timeline/conflicts),
    # provenance drill-down for CitationChip, Layer 2 practitioner
    # candidates + confirm/reject, audit log slice. All endpoints
    # auth-gated via Depends(get_current_user); user_id closed over
    # server-side so an LLM-controlled call cannot pivot to another
    # medic's data.
    from nexus_server import memory_router_v2 as _memory_v2
    app.include_router(_memory_v2.router)
    # #208 — Tier-classified SSE chat endpoint backed by retrieval_tiers.
    # POST /api/v1/agent/chat returns Server-Sent Events with the
    # tier_classified / reasoning_chunk / final_answer_chunk / citations /
    # turn_complete sequence the desktop's Encounter mode subscribes to.
    from nexus_server import chat_router as _chat_v2
    app.include_router(_chat_v2.router)
    # #213 — MONAI Label OHIF bridge (Rev-6/Rev-9). Captures medic
    # corrections from OHIF viewer annotations into event_log.
    from nexus_server.monai_runtime import ohif_label_bridge as _ohif
    app.include_router(_ohif.router)
    # #142/#144/#146 — DICOM HTTP API: render slices/MIP/grid, ROI
    # CRUD, RTSTRUCT import/export, SAM auto-segment endpoint.
    # Hosts the API the Cornerstone3D WebView in the desktop viewer
    # talks to.
    from nexus_server import dicom_router as _dcm_router
    app.include_router(_dcm_router.router)
    # #181 — manual patient registration + full roster.
    from nexus_server import patients_router as _patients_router
    app.include_router(_patients_router.router)
    # #191 — DICOM Quick scan endpoint (Gemini Flash triage).
    from nexus_server import quick_scan as _quick_scan
    app.include_router(_quick_scan.router)
    # U3.3 — Settings · Data export. /api/v1/export/archive_path + /bundle.
    # Builds a zip of the user's twin_event_log + manifest under
    # ~/Documents/Nexus Archive/. Restore endpoints land alongside in
    # M3.3 finalize (destructive replace requires extra Rev-19 guards).
    from nexus_server import export_router as _export_router
    app.include_router(_export_router.router)
    # U3.3 — Settings · LLM. Lets the desktop write GEMINI/OPENAI/
    # ANTHROPIC keys + provider/model selection to $RUNE_HOME/.env and
    # picks up the change in-process (no restart). v1-parity since v1's
    # start.sh seeded the same file; the Tauri sidecar in lib.rs reads
    # it back on every launch.
    from nexus_server import settings_router as _settings_router
    app.include_router(_settings_router.router)
    # v2 email-send capability (relay-first, SMTP-fallback). Ported
    # from v1's tools_calendar.py SendEmailNowTool. The desktop's
    # Compose dialog hits POST /api/v1/email/send; GET .../transport
    # tells it whether the operator has configured a backend.
    from nexus_server import email_router as _email_router
    app.include_router(_email_router.router)
    # ReportMode PDF export — POST /api/v1/report/pdf builds a clinical
    # report via reportlab Platypus, writes it to $ARCHIVE_DIR/Reports/,
    # returns the path so the UI's "Last report" card can show it +
    # an Open Folder button. Replaces the broken window.print()
    # mechanism that produced no file and no path feedback in WKWebView.
    from nexus_server import report_pdf_router as _report_pdf_router
    app.include_router(_report_pdf_router.router)
    # Scheduled Tasks Phase 1 — POST /api/v1/schedule/extract picks
    # schedule intent out of chat text (heuristic-only); /confirm
    # lands a row in scheduled_tasks; /list + DELETE serve the
    # Calendar UI. The worker that fires tasks at fire_at is started
    # below in lifespan().
    from nexus_server import scheduler_router as _scheduler_router
    app.include_router(_scheduler_router.router)
    # Research Workspace — /api/v1/research/* and the Patient →
    # Studies derived endpoint /api/v1/patients/{hash}/studies (D18).
    # See docs/design/RESEARCH_WORKSPACE_DESIGN.md.
    from nexus_server import research_router as _research_router
    app.include_router(_research_router.router)
    app.include_router(_research_router.patients_studies_router)
    # Writing Studio (P1) — /api/v1/docs/*: documents, version
    # snapshots, de-identified data reference chips, selection polish
    # (SSE), PHI scan and the docx export gate.
    from nexus_server import writing_router as _writing_router
    app.include_router(_writing_router.router)
    # Skills management — /api/v1/skills: list / search / install /
    # uninstall / toggle. Skills live in the user's twin dir
    # ({TWIN_BASE_DIR}/{user_id}/skills/); enabled/auto_apply prefs in
    # user_skill_prefs. Enabled skills flow into BOTH chat paths — the
    # v2 tiered chat via skills_router.build_skills_block (see
    # chat_router) and the legacy twin path via the live-twin cache
    # sync + apply_disabled_overlay in twin_manager.
    from nexus_server import skills_router as _skills_router
    app.include_router(_skills_router.router)

    # #143 — serve the Cornerstone3D viewer.html as a static page.
    # Desktop launches the user's default browser at
    # http://localhost:8001/dicom-viewer/?studyId=…&token=… when the
    # medic clicks Open Viewer; the page then talks to /api/v1/dicom/*
    # via fetch(). Mounting static is the simplest deploy — no
    # WebView packaging, no extra runtime, no .NET 10 compat risk.
    from pathlib import Path as _Path

    from fastapi.staticfiles import StaticFiles as _StaticFiles
    _static_dir = _Path(__file__).parent / "static"
    if _static_dir.is_dir():
        app.mount(
            "/dicom-viewer",
            _StaticFiles(directory=str(_static_dir), html=True),
            name="dicom-viewer",
        )

    return app


# ───────────────────────────────────────────────────────────────────────────
# Entry Point
# ───────────────────────────────────────────────────────────────────────────


def run_server() -> None:
    """Entry point for rune-server CLI command.

    Parses command-line arguments and starts uvicorn server.

    HTTPS
    -----
    Remote (non-localhost) access should run over HTTPS. Two ways to
    give this server a TLS certificate:

      1. **Self-signed** — `./scripts/generate_self_signed_cert.sh`
         produces ``cert.pem`` + ``key.pem``. Quick, works for any IP /
         hostname. Browsers show a "Not Secure" warning that the user
         must click through; the desktop's WebView prompts to trust the
         cert (one-time accept).

      2. **Let's Encrypt** — `./scripts/setup_letsencrypt.sh` provisions
         a real cert against your domain (or nip.io subdomain). No
         browser warning; auto-renews via cron.

    Either way, point the entry-point at the resulting files via
    ``--ssl-certfile`` / ``--ssl-keyfile`` (or env vars
    ``SSL_CERTFILE`` / ``SSL_KEYFILE``). Uvicorn handles the TLS
    handshake natively — no Caddy / nginx required.
    """
    parser = argparse.ArgumentParser(
        description="Nexus API Server"
    )
    parser.add_argument(
        "--port",
        type=int,
        default=config.SERVER_PORT,
        help=f"Server port (default: {config.SERVER_PORT})",
    )
    parser.add_argument(
        "--host",
        type=str,
        default=config.SERVER_HOST,
        help=f"Server host (default: {config.SERVER_HOST})",
    )
    parser.add_argument(
        "--reload",
        action="store_true",
        help="Enable auto-reload on file changes",
    )
    # TLS args — both optional. If you pass one you MUST pass the other,
    # or uvicorn errors out at startup. Env vars SSL_CERTFILE /
    # SSL_KEYFILE are the same thing for systemd / docker contexts where
    # plumbing CLI args is awkward.
    parser.add_argument(
        "--ssl-certfile",
        type=str,
        default=os.environ.get("SSL_CERTFILE"),
        help="PEM-encoded TLS certificate file (enables HTTPS).",
    )
    parser.add_argument(
        "--ssl-keyfile",
        type=str,
        default=os.environ.get("SSL_KEYFILE"),
        help="PEM-encoded TLS private key file (enables HTTPS).",
    )

    args = parser.parse_args()

    # Validation: if either TLS arg is set, both must be set + readable.
    # Fail loudly at startup rather than letting uvicorn print a less
    # actionable stack trace 200ms later.
    tls_kwargs = {}
    if args.ssl_certfile or args.ssl_keyfile:
        if not (args.ssl_certfile and args.ssl_keyfile):
            raise SystemExit(
                "✗ --ssl-certfile and --ssl-keyfile must both be set "
                "(or neither). Got cert=%r key=%r"
                % (args.ssl_certfile, args.ssl_keyfile),
            )
        for name, path in (("cert", args.ssl_certfile),
                           ("key",  args.ssl_keyfile)):
            if not os.path.isfile(path):
                raise SystemExit(
                    f"✗ TLS {name} file not found: {path}\n"
                    "  Generate one with ./scripts/generate_self_signed_cert.sh"
                )
        tls_kwargs["ssl_certfile"] = args.ssl_certfile
        tls_kwargs["ssl_keyfile"] = args.ssl_keyfile
        scheme = "https"
    else:
        scheme = "http"

    logger.info(
        f"Starting Nexus API Server on {scheme}://{args.host}:{args.port}"
    )
    if scheme == "http" and args.host not in ("127.0.0.1", "localhost"):
        logger.warning(
            "Server is bound to %s on plain HTTP. Credentials will be "
            "sent in cleartext to any client that's not on the same "
            "machine. Generate a TLS cert (see --ssl-certfile / scripts/"
            "generate_self_signed_cert.sh) for remote access.",
            args.host,
        )

    import uvicorn
    uvicorn.run(
        "nexus_server.main:create_app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        factory=True,
        log_level=config.LOG_LEVEL.lower(),
        **tls_kwargs,
    )


if __name__ == "__main__":
    import uvicorn

    app = create_app()

    parser = argparse.ArgumentParser(
        description="Nexus API Server"
    )
    parser.add_argument(
        "--port",
        type=int,
        default=config.SERVER_PORT,
        help=f"Server port (default: {config.SERVER_PORT})",
    )
    parser.add_argument(
        "--host",
        type=str,
        default=config.SERVER_HOST,
        help=f"Server host (default: {config.SERVER_HOST})",
    )
    parser.add_argument(
        "--reload",
        action="store_true",
        help="Enable auto-reload on file changes",
    )

    args = parser.parse_args()

    logger.info(
        f"Starting Nexus API Server on {args.host}:{args.port}"
    )

    uvicorn.run(
        app,
        host=args.host,
        port=args.port,
        reload=args.reload,
        log_level=config.LOG_LEVEL.lower(),
    )
