"""#169 — Long-running background task substrate.

Motivating UX: the medic asks the agent for something that's going to
take a while (run a 7-step research workflow, segment all 1134 slices,
draft + cross-check a treatment plan). Instead of blocking the chat
window for 5 minutes, the agent calls ``defer_to_background(...)``,
which:

  1. Persists the task as ``queued`` in this module's SQLite table.
  2. Returns IMMEDIATELY to the agent with a confirmation it can
     paraphrase to the medic ("Working on it — I'll email you").
  3. A single asyncio worker loop on the server picks up the task,
     drives it to completion via twin.chat (re-using the same agent
     loop the medic would have driven manually).
  4. On completion: formats result → emails the medic → writes a
     ``task_completed`` event into twin.event_log so the next desktop
     refresh surfaces "✅ Done — emailed you" as an assistant card in
     the same session.

Why a SQL table rather than an in-memory queue:
  - Server restart shouldn't drop tasks the medic is waiting on.
  - Cross-process visibility: the future scheduled-tasks daemon
    (or a load-balanced server) can dequeue from the same table.
  - Audit: every long-task gets a permanent row with status timeline
    + which email it ended up in, recoverable months later.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


# ── Status state machine ────────────────────────────────────────────
STATUS_QUEUED    = "queued"
STATUS_RUNNING   = "running"
STATUS_DONE      = "done"
STATUS_EMAILED   = "emailed"
STATUS_FAILED    = "failed"


def _db_path() -> Path:
    """Mirror dicom._index_db_path's convention — same data dir, own
    file. Separate from nexus_server.db so a corrupted async_tasks
    table doesn't take down chat."""
    import os
    home = Path(
        os.environ.get(
            "RUNE_HOME_EXPORT",
            Path.home() / "Library" / "Application Support" / "RuneProtocol",
        )
    )
    home.mkdir(parents=True, exist_ok=True)
    p = home / "async_tasks.db"
    return p


def _init_db() -> None:
    """Idempotent schema creation. Safe to call on every server boot."""
    conn = sqlite3.connect(_db_path())
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS async_tasks (
                task_id              TEXT PRIMARY KEY,
                user_id              TEXT NOT NULL,
                session_id           TEXT NOT NULL DEFAULT '',
                description          TEXT NOT NULL,
                action_prompt        TEXT NOT NULL,
                eta_seconds          INTEGER NOT NULL DEFAULT 0,
                status               TEXT NOT NULL,
                email_to             TEXT NOT NULL DEFAULT '',
                email_subject        TEXT NOT NULL DEFAULT '',
                result_text          TEXT NOT NULL DEFAULT '',
                error                TEXT NOT NULL DEFAULT '',
                created_at           INTEGER NOT NULL,
                started_at           INTEGER NOT NULL DEFAULT 0,
                completed_at         INTEGER NOT NULL DEFAULT 0,
                emailed_at           INTEGER NOT NULL DEFAULT 0
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_async_status "
            "ON async_tasks(status, created_at)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_async_user "
            "ON async_tasks(user_id, created_at DESC)"
        )
        conn.commit()
    finally:
        conn.close()


def enqueue_task(
    *,
    user_id: str,
    session_id: str,
    description: str,
    action_prompt: str,
    eta_seconds: int,
    email_to: str,
) -> str:
    """Insert a new task in ``queued`` state. Returns the task_id.

    ``action_prompt`` is the literal text the worker will pass back
    to twin.chat as a fresh user-message turn — the worker doesn't
    interpret it, it just re-enters the agent loop with it. The agent
    on that fresh turn does the heavy work (workflow, delegation,
    multi-step reasoning) without the medic's window being blocked.
    """
    task_id = uuid.uuid4().hex
    now = int(time.time())
    _init_db()
    conn = sqlite3.connect(_db_path())
    try:
        pending = conn.execute(
            "SELECT COUNT(*) FROM async_tasks WHERE user_id = ? AND status IN (?, ?)",
            (user_id, STATUS_QUEUED, STATUS_RUNNING),
        ).fetchone()[0]
        if pending >= 50:
            raise RuntimeError(
                f"async_tasks: user {user_id} already has {pending} pending/running "
                "tasks; refusing to enqueue more"
            )
        conn.execute(
            """
            INSERT INTO async_tasks
            (task_id, user_id, session_id, description, action_prompt,
             eta_seconds, status, email_to, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (task_id, user_id, session_id, description, action_prompt,
             int(eta_seconds), STATUS_QUEUED, email_to, now),
        )
        conn.commit()
    finally:
        conn.close()
    logger.info(
        "async_task enqueued — user=%s task=%s eta=%ds desc=%r",
        user_id, task_id[:8], eta_seconds, description[:60],
    )
    return task_id


def _claim_next_queued() -> Optional[dict]:
    """Atomically dequeue one task: SELECT + UPDATE status=running.

    Uses a transaction so two worker passes can't pick the same task
    in a multi-worker future. Returns a dict snapshot of the row or
    None if the queue is empty.
    """
    _init_db()
    conn = sqlite3.connect(_db_path())
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT task_id, user_id, session_id, description, "
            "action_prompt, email_to "
            "FROM async_tasks WHERE status = ? "
            "ORDER BY created_at ASC LIMIT 1",
            (STATUS_QUEUED,),
        ).fetchone()
        if not row:
            conn.commit()
            return None
        conn.execute(
            "UPDATE async_tasks SET status = ?, started_at = ? "
            "WHERE task_id = ?",
            (STATUS_RUNNING, int(time.time()), row[0]),
        )
        conn.commit()
        return {
            "task_id":       row[0],
            "user_id":       row[1],
            "session_id":    row[2],
            "description":   row[3],
            "action_prompt": row[4],
            "email_to":      row[5],
        }
    finally:
        conn.close()


def _mark_done(task_id: str, result_text: str, email_subject: str) -> None:
    conn = sqlite3.connect(_db_path())
    try:
        conn.execute(
            "UPDATE async_tasks SET status = ?, result_text = ?, "
            "email_subject = ?, completed_at = ? WHERE task_id = ?",
            (STATUS_DONE, result_text, email_subject,
             int(time.time()), task_id),
        )
        conn.commit()
    finally:
        conn.close()


def _mark_failed(task_id: str, error: str) -> None:
    conn = sqlite3.connect(_db_path())
    try:
        conn.execute(
            "UPDATE async_tasks SET status = ?, error = ?, "
            "completed_at = ? WHERE task_id = ?",
            (STATUS_FAILED, error, int(time.time()), task_id),
        )
        conn.commit()
    finally:
        conn.close()


def _mark_emailed(task_id: str) -> None:
    conn = sqlite3.connect(_db_path())
    try:
        conn.execute(
            "UPDATE async_tasks SET status = ?, emailed_at = ? "
            "WHERE task_id = ?",
            (STATUS_EMAILED, int(time.time()), task_id),
        )
        conn.commit()
    finally:
        conn.close()


def list_user_tasks(user_id: str, limit: int = 50) -> list[dict]:
    """Diagnostic / future UI surface — return recent tasks for a
    user, newest first. Not used by the worker itself; here so the
    desktop can show a "background tasks" panel later."""
    _init_db()
    conn = sqlite3.connect(_db_path())
    try:
        rows = conn.execute(
            "SELECT task_id, description, status, eta_seconds, "
            "result_text, error, created_at, completed_at, emailed_at "
            "FROM async_tasks WHERE user_id = ? "
            "ORDER BY created_at DESC LIMIT ?",
            (user_id, int(limit)),
        ).fetchall()
    finally:
        conn.close()
    return [
        {
            "task_id":      r[0],
            "description":  r[1],
            "status":       r[2],
            "eta_seconds":  int(r[3]),
            "result_text":  r[4] or "",
            "error":        r[5] or "",
            "created_at":   int(r[6]),
            "completed_at": int(r[7]),
            "emailed_at":   int(r[8]),
        }
        for r in rows
    ]


# ── Action-prompt sanitiser ─────────────────────────────────────────


# Defer / email phrases that would otherwise re-trigger
# defer_to_background inside the worker's own twin.chat call. We strip
# them from the medic's original message before feeding it back, OR
# wrap with an explicit "execute now" override so the agent inside
# the worker doesn't recurse.
_DEFER_PHRASES_TO_STRIP = (
    # Chinese
    "做完邮件通知我", "做完邮件通知你", "做完后邮件通知",
    "完成后邮件通知", "邮件通知我", "邮件通知你",
    "我去查房", "我去开会", "我先离开",
    "做完通知我", "做完了通知我", "完成后通知我",
    "后台运行", "异步处理", "后台跑",
    # English
    "email me when done", "email me when finished",
    "I'll be afk", "i'm afk", "i'm going afk",
    "in the background", "run this in background",
    "notify me when done", "notify me by email",
)


def _wrap_action_prompt(action_prompt: str) -> str:
    """Build the prompt the worker actually hands to twin.chat.

    Two transforms:
      1. Strip phrases that say "do this later / email me" — those
         would re-trigger defer_to_background inside the worker's
         own twin.chat call (the worker IS the deferred execution;
         we don't want infinite recursion).
      2. Prepend an explicit "execute now, inline, return the
         synthesis text" instruction so the agent doesn't half-
         finish + return empty.

    The worker only ever runs in the "do the work" context, so the
    wrapper is unambiguous about that.
    """
    cleaned = action_prompt or ""
    for phrase in _DEFER_PHRASES_TO_STRIP:
        cleaned = cleaned.replace(phrase, "")
    # Common trailing punctuation left over from phrase removal.
    cleaned = cleaned.replace("，，", "，").replace(",,", ",").strip()
    if not cleaned:
        cleaned = "(empty task body — see the description above)"
    return (
        "You are executing this task in the background NOW. "
        "Do NOT call defer_to_background. Do NOT promise to email "
        "the user separately — the worker will email the synthesis "
        "text you return. Run any required tools (delegate, "
        "read_uploaded_file, web_search, etc.) inline, then "
        "produce the final synthesis text as your reply. The text "
        "you return WILL be the email body, so write it as a "
        "complete answer the medic can act on without context.\n\n"
        "Task:\n"
        f"{cleaned}"
    )


# ── Worker loop ─────────────────────────────────────────────────────


async def _execute_one(task: dict) -> None:
    """Run a single task end-to-end: re-enter agent loop, capture
    result, send email, write completion event into twin event_log.

    Failure handling: every stage is logged; the task lands in
    STATUS_FAILED on any unhandled exception so the medic gets a
    failure email rather than silent loss.
    """
    user_id = task["user_id"]
    task_id = task["task_id"]
    description = task["description"]
    action_prompt = task["action_prompt"]
    session_id = task["session_id"]
    email_to = task["email_to"]

    logger.info(
        "async_task running — user=%s task=%s desc=%r",
        user_id, task_id[:8], description[:60],
    )

    # ── Step 1: invoke the agent on a fresh turn with the action
    #            prompt. We use twin.chat directly so the agent loop's
    #            full tool set (delegate, workflows, file_reader,
    #            ...) is available — same as if the medic had typed
    #            the prompt themselves.
    #
    # CRITICAL — sanitise the action_prompt before re-driving twin.chat.
    # The medic's original message ("用 X workflow 跑 Y，做完邮件通知
    # 我") contains the very phrases that trigger defer_to_background.
    # If we feed it back verbatim, the agent calls defer_to_background
    # AGAIN (or hallucinates another "我会跑" without calling) — net
    # result: worker's twin.chat returns no text → email body =
    # "(agent returned no text)" — which is what bit us in production.
    #
    # The fix is to wrap the action_prompt in an explicit instruction
    # that bans deferral + makes the synthesis target unambiguous.
    sanitised_prompt = _wrap_action_prompt(action_prompt)
    logger.debug(
        "async_task wrapping action_prompt: %r → %r (head)",
        action_prompt[:80], sanitised_prompt[:120],
    )

    result_text = ""
    try:
        from nexus_server.twin_manager import get_twin
        twin = await get_twin(user_id)
        result_text = await twin.chat(
            sanitised_prompt,
            session_id=session_id or None,
        )
        if not result_text:
            logger.warning(
                "async_task %s: twin.chat returned empty text. "
                "action_prompt=%r",
                task_id[:8], sanitised_prompt[:200],
            )
            result_text = (
                "(The agent ran but returned no synthesis text. "
                "This usually means a tool loop completed without "
                "producing a final reply — check server.log for "
                "tool errors, or rephrase the task and try again.)"
            )
    except Exception as e:  # noqa: BLE001
        logger.exception("async_task agent loop failed: %s", e)
        _mark_failed(task_id, f"{type(e).__name__}: {e}")
        # Still attempt email + chat notification so medic isn't
        # left hanging — covers cases where the task partially
        # produced useful artefacts (file generation, etc.).
        try:
            await _send_failure_email(
                user_id=user_id, to=email_to,
                description=description,
                error=f"{type(e).__name__}: {e}",
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("failure email send failed: %s", exc)
        try:
            await _emit_completion_event(
                user_id, session_id, task_id, description,
                ok=False, error=f"{type(e).__name__}: {e}",
                email_to=email_to,
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("completion event emit failed: %s", exc)
        return

    subject = _make_subject(description)
    _mark_done(task_id, result_text, subject)

    # ── Step 2: email the result.
    try:
        await _send_success_email(
            user_id=user_id, to=email_to, subject=subject,
            description=description, result_text=result_text,
        )
        _mark_emailed(task_id)
    except Exception as e:  # noqa: BLE001
        logger.exception("async_task email send failed: %s", e)
        # Don't fail the whole task — result is persisted, medic can
        # see it in the next chat refresh even without email.

    # ── Step 3: post a completion card into the chat session.
    try:
        await _emit_completion_event(
            user_id, session_id, task_id, description,
            ok=True, result_text=result_text, email_to=email_to,
        )
    except Exception as e:  # noqa: BLE001
        logger.exception("async_task chat event emit failed: %s", e)


def _make_subject(description: str) -> str:
    """Email subject line — keep <80 chars, prepend a Nexus marker so
    a future mail filter can sort these into a folder."""
    base = (description or "Background task").strip()
    if len(base) > 70:
        base = base[:70].rstrip() + "…"
    return f"[Nexus] {base}"


async def _send_success_email(
    *, user_id: str, to: str, subject: str,
    description: str, result_text: str,
) -> None:
    """Send the completion email via the relay (or direct SMTP
    fallback). user_id is passed through to the relay for quota +
    allow-list lookup — same identity the interactive
    send_email_now tool uses."""
    if not to:
        logger.warning("async_task: no email_to configured — skipping send")
        return
    body = (
        f"Nexus finished the background task you asked about:\n\n"
        f"  {description}\n\n"
        f"=========================== RESULT ===========================\n\n"
        f"{result_text}\n\n"
        f"==============================================================\n\n"
        f"You can also see this in the Nexus chat — open the app and the "
        f"completion card will be in the same conversation.\n\n"
        f"— Nexus"
    )
    await _send_email_async(
        user_id=user_id, to=[to], cc=[], subject=subject, body=body,
    )


async def _send_failure_email(
    *, user_id: str, to: str, description: str, error: str,
) -> None:
    if not to:
        return
    subject = f"[Nexus] Task failed: {description[:50]}"
    body = (
        f"Nexus tried to run a background task you asked about, "
        f"but hit an error:\n\n"
        f"  {description}\n\n"
        f"Error: {error}\n\n"
        f"Open the Nexus chat — the failure card has more detail and "
        f"you can retry or refine the request from there.\n\n"
        f"— Nexus"
    )
    await _send_email_async(
        user_id=user_id, to=[to], cc=[], subject=subject, body=body,
    )


async def _send_email_async(
    *, user_id: str, to: list[str], cc: list[str],
    subject: str, body: str,
) -> None:
    """Send an email using the SAME precedence as the agent's
    interactive send_email_now tool (#115/#116):

      1. Hosted Fly.io relay (NEXUS_RELAY_URL + NEXUS_RELAY_API_KEY)
         — production path, holds shared SMTP creds server-side,
         enforces per-user daily quota + allow-list. This is what
         the medic actually configured yesterday.
      2. Direct SMTP (NEXUS_SMTP_HOST + NEXUS_SMTP_USER + NEXUS_SMTP_PASSWORD)
         — local dev fallback only.

    Either way: we never raise on send failure — log + return so the
    background task still posts its chat completion card. The medic
    sees "✅ Done (email failed: <reason>)" and can retry from chat.
    """
    import os
    relay_url     = os.environ.get("NEXUS_RELAY_URL", "").strip()
    relay_api_key = os.environ.get("NEXUS_RELAY_API_KEY", "").strip()

    if relay_url and relay_api_key:
        from nexus_server.tools_calendar import _post_to_relay
        payload = {
            "nexus_user_id": user_id,
            "to":      ", ".join(to),
            "subject": subject,
            "body":    body,
        }
        if cc:
            payload["cc"] = ", ".join(cc)
        ok, msg, code = await _post_to_relay(relay_url, relay_api_key, payload)
        if not ok:
            raise RuntimeError(f"relay send failed (HTTP {code}): {msg}")
        logger.info(
            "async_task email sent via relay to %s: %s", to, msg,
        )
        return

    # Direct SMTP fallback for local dev.
    from nexus_server.tools_calendar import _send_smtp_sync, _smtp_config
    cfg = _smtp_config()
    if not cfg:
        logger.warning(
            "async_task: neither NEXUS_RELAY_URL nor NEXUS_SMTP_HOST "
            "is configured. Skipping email; medic still sees the chat "
            "completion card."
        )
        return
    loop = asyncio.get_running_loop()
    ok, msg = await loop.run_in_executor(
        None, _send_smtp_sync, cfg, to, cc, subject, body,
    )
    if not ok:
        raise RuntimeError(f"smtp send failed: {msg}")
    logger.info("async_task email sent via direct SMTP to %s: %s", to, msg)


async def _emit_completion_event(
    user_id: str, session_id: str, task_id: str, description: str,
    *,
    ok: bool, result_text: str = "", error: str = "", email_to: str = "",
) -> None:
    """Append an ``assistant_response`` event into the user's twin
    event_log so the desktop's next /agent/messages poll renders a
    completion card in the same session. We piggy-back on the
    existing assistant_response shape (rather than introducing a new
    event_type) so the desktop UI handles it without changes."""
    from nexus_server.twin_manager import get_twin
    twin = await get_twin(user_id)
    if ok:
        snippet = (result_text or "").strip()
        if len(snippet) > 600:
            snippet = snippet[:600].rstrip() + "…"
        body = (
            f"✅ Background task done — {description}\n\n"
            + (f"📧 Emailed full result to {email_to}.\n\n" if email_to else "")
            + (f"Summary:\n\n{snippet}" if snippet else "")
        )
    else:
        body = (
            f"❌ Background task failed — {description}\n\n"
            f"Error: {error}\n\n"
            + (f"📧 Sent failure notice to {email_to}." if email_to else "")
        )
    try:
        twin.event_log.append(
            "assistant_response",
            body,
            metadata={
                "async_task_id": task_id,
                "kind": "task_completion",
                "ok": bool(ok),
                "session_id": session_id or "",
            },
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("async_task event_log append failed: %s", e)


# ── Top-level worker loop (started by main.py at server boot) ───────


_worker_task: Optional[asyncio.Task] = None


async def _worker_loop() -> None:
    """Single asyncio coroutine that drains the queue forever. Wakes
    every 3 s — cheap polling sized to "responsive enough for a
    medic glance" without hammering SQLite. A higher-volume future
    can swap this for a condition variable + notify on enqueue.
    """
    logger.info("async_tasks worker started")
    # Crash recovery: reset any tasks left 'running' from a previous
    # server run — they were in-flight when the process died.
    try:
        _init_db()
        _conn = sqlite3.connect(_db_path())
        try:
            _conn.execute(
                "UPDATE async_tasks SET status = ? WHERE status = ?",
                (STATUS_QUEUED, STATUS_RUNNING),
            )
            _conn.commit()
        finally:
            _conn.close()
    except Exception as _e:  # noqa: BLE001
        logger.warning("async_tasks: crash-recovery reset failed: %s", _e)
    while True:
        try:
            claimed = _claim_next_queued()
            if claimed is not None:
                await _execute_one(claimed)
                # Tight loop — drain anything else queued before
                # going back to sleep.
                continue
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            logger.exception("async_tasks worker tick crashed: %s", e)
        await asyncio.sleep(3.0)


def start_worker() -> None:
    """Idempotent worker spawn — called from server startup. Safe to
    call multiple times; second + later calls no-op."""
    global _worker_task
    if _worker_task is not None and not _worker_task.done():
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        # No loop yet (e.g. boot path before uvicorn started). The
        # caller should retry from inside an async startup hook.
        logger.warning("async_tasks.start_worker: no running event loop")
        return
    _worker_task = loop.create_task(_worker_loop())
    logger.info("async_tasks worker task spawned")


# ── HTTP API surface ────────────────────────────────────────────────


from fastapi import APIRouter, Depends  # noqa: E402
from pydantic import BaseModel  # noqa: E402

from nexus_server.auth import get_current_user  # noqa: E402

router = APIRouter(prefix="/api/v1/async-tasks", tags=["async-tasks"])


class AsyncTaskInfo(BaseModel):
    """Wire shape for one task in the desktop's task list UI."""
    task_id:        str
    description:    str
    status:         str        # queued|running|done|emailed|failed
    eta_seconds:    int
    result_text:    str        # snippet — full text in email
    error:          str
    created_at:     int
    completed_at:   int
    emailed_at:     int


class AsyncTaskListResponse(BaseModel):
    tasks: list[AsyncTaskInfo]
    # Convenience counters so the UI can render a "3 running, 1 just
    # finished" badge without re-tallying.
    active_count:    int
    finished_count:  int


@router.get("", response_model=AsyncTaskListResponse)
async def list_my_async_tasks(
    limit: int = 30,
    current_user: str = Depends(get_current_user),
) -> AsyncTaskListResponse:
    """#172 — return this user's recent background tasks for the
    desktop's task-list panel.

    Newest first. Includes still-running, recently-emailed, and
    failed within the past hour. Older finished tasks are clipped
    by ``limit``.
    """
    rows = list_user_tasks(current_user, limit=limit)
    out = [
        AsyncTaskInfo(
            task_id=r["task_id"],
            description=r["description"],
            status=r["status"],
            eta_seconds=r["eta_seconds"],
            # Cap result text to a snippet — the UI only renders a
            # one-line preview anyway; full text in email body.
            result_text=(r["result_text"][:600] + "…"
                         if len(r["result_text"]) > 600
                         else r["result_text"]),
            error=r["error"],
            created_at=r["created_at"],
            completed_at=r["completed_at"],
            emailed_at=r["emailed_at"],
        )
        for r in rows
    ]
    active = sum(1 for t in out if t.status in ("queued", "running"))
    finished = sum(1 for t in out if t.status in ("done", "emailed", "failed"))
    return AsyncTaskListResponse(
        tasks=out, active_count=active, finished_count=finished,
    )
