"""Scheduled-task storage + worker (Phase 1).

Persistence layer over the ``scheduled_tasks`` table (see Alembic
migration 0003). The worker is started from ``main.py`` lifespan and
polls every ``POLL_INTERVAL_SECONDS`` for any ``status='pending'``
row whose ``fire_at <= now()``, then dispatches to a kind-specific
executor.

Phase 1 ships ONE kind:
  * ``send_email`` — payload `{to: [...], cc?: [...], subject, body}`
                     dispatch calls ``email_send.send_email_async``.

Phase 2 (post-MVP) adds:
  * ``chat_brief`` — payload `{prompt, patient_hash?}` runs a chat turn
                     server-side and stores the answer in result_json.
  * ``reminder``   — payload `{text}` emits a TASK_DUE event polled
                     by the desktop's notification surface.

Audit / replay
──────────────
Every state-change (CREATED, FIRED, CANCELLED) writes to the v3 event
log via ``Store.emit_and_apply``. The projection table here is a
materialised view: a DROP + replay reconstructs byte-identical state
(per Rev-8 / R23). The projection is hot-path optimised; the event
log is the source of truth.

Safety
──────
The bundled-creds + recipient-allow-list guards in
``email_send.send_email_async`` apply on EVERY firing, not just at
``CREATED`` time. So if the operator narrows the allow-list between
the medic confirming a scheduled task and the task firing, the
send is blocked with a clear error.
"""
from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import time
import uuid
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────


POLL_INTERVAL_SECONDS    = 30
MAX_BATCH_PER_TICK       = 20      # don't try to fire >N tasks in one pass
MAX_PENDING_PER_USER     = 50      # per-user quota; 422 from /confirm

SUPPORTED_KINDS          = {"send_email"}


# ─────────────────────────────────────────────────────────────────────
# Data shape
# ─────────────────────────────────────────────────────────────────────


@dataclass
class ScheduledTask:
    """One row from ``scheduled_tasks``. Used by both router responses
    and the worker dispatcher."""

    task_id:        str
    user_id:        str
    patient_hash:   Optional[str]
    session_id:     Optional[str]
    kind:           str
    payload:        dict           # decoded JSON
    fire_at:        int
    user_tz:        str
    recurrence_cron: Optional[str]
    status:         str            # pending|running|done|error|cancelled
    last_run_at:    Optional[int]
    last_error:     Optional[str]
    result:         Optional[dict]   # decoded JSON or None
    created_at:     int
    updated_at:     int
    cancelled_at:   Optional[int]

    @classmethod
    def from_row(cls, row) -> "ScheduledTask":
        return cls(
            task_id        = row[0],
            user_id        = row[1],
            patient_hash   = row[2],
            session_id     = row[3],
            kind           = row[4],
            payload        = json.loads(row[5] or "{}"),
            fire_at        = int(row[6]),
            user_tz        = row[7] or "UTC",
            recurrence_cron= row[8],
            status         = row[9],
            last_run_at    = (int(row[10]) if row[10] is not None else None),
            last_error     = row[11],
            result         = (json.loads(row[12]) if row[12] else None),
            created_at     = int(row[13]),
            updated_at     = int(row[14]),
            cancelled_at   = (int(row[15]) if row[15] is not None else None),
        )

    def to_dict(self) -> dict:
        """JSON-serialisable shape returned by the list endpoint."""
        return {
            "task_id":         self.task_id,
            "user_id":         self.user_id,
            "patient_hash":    self.patient_hash,
            "session_id":      self.session_id,
            "kind":            self.kind,
            "payload":         self.payload,
            "fire_at":         self.fire_at,
            "user_tz":         self.user_tz,
            "recurrence_cron": self.recurrence_cron,
            "status":          self.status,
            "last_run_at":     self.last_run_at,
            "last_error":      self.last_error,
            "result":          self.result,
            "created_at":      self.created_at,
            "updated_at":      self.updated_at,
            "cancelled_at":    self.cancelled_at,
        }


_TASK_COLS = (
    "task_id, user_id, patient_hash, session_id, kind, payload_json, "
    "fire_at, user_tz, recurrence_cron, status, last_run_at, "
    "last_error, result_json, created_at, updated_at, cancelled_at"
)


# ─────────────────────────────────────────────────────────────────────
# Storage helpers (raw SQL — projection table)
# ─────────────────────────────────────────────────────────────────────


def create_task(
    conn: sqlite3.Connection,
    *,
    user_id: str,
    kind: str,
    payload: dict,
    fire_at: int,
    user_tz: str,
    recurrence_cron: Optional[str] = None,
    session_id: Optional[str] = None,
    patient_hash: Optional[str] = None,
) -> ScheduledTask:
    """Insert a new pending task. Caller is responsible for emitting
    the SCHEDULED_TASK_CREATED event around this call — this function
    is the projection step.

    Raises ValueError on:
      - kind not in SUPPORTED_KINDS
      - fire_at not in (now - 60, now + 366 days)  (sanity)
      - per-user pending quota exceeded
    """
    if kind not in SUPPORTED_KINDS:
        raise ValueError(
            f"unsupported task kind {kind!r}. "
            f"Phase 1 supports: {sorted(SUPPORTED_KINDS)}"
        )
    now = int(time.time())
    if fire_at < now - 60:
        raise ValueError(
            f"fire_at {fire_at} is in the past (now={now}). "
            "Schedule a future timestamp; the worker won't catch up "
            "more than 60 s historical."
        )
    if fire_at > now + 366 * 86400:
        raise ValueError(
            f"fire_at {fire_at} is more than 1 year out. "
            "Phase 1 caps far-future scheduling — most likely a parse error."
        )

    pending_count = conn.execute(
        "SELECT COUNT(*) FROM scheduled_tasks "
        "WHERE user_id = ? AND status = 'pending'",
        (user_id,),
    ).fetchone()[0]
    if pending_count >= MAX_PENDING_PER_USER:
        raise ValueError(
            f"user has {pending_count} pending tasks (max "
            f"{MAX_PENDING_PER_USER}). Cancel some before adding more."
        )

    task_id = str(uuid.uuid4())
    conn.execute(
        f"INSERT INTO scheduled_tasks ({_TASK_COLS}) VALUES "
        "(?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', NULL, NULL, NULL, ?, ?, NULL)",
        (
            task_id, user_id, patient_hash, session_id, kind,
            json.dumps(payload, sort_keys=True),
            int(fire_at), user_tz, recurrence_cron,
            now, now,
        ),
    )
    conn.commit()
    return get_task(conn, task_id)


def get_task(conn: sqlite3.Connection, task_id: str) -> ScheduledTask:
    row = conn.execute(
        f"SELECT {_TASK_COLS} FROM scheduled_tasks WHERE task_id = ?",
        (task_id,),
    ).fetchone()
    if row is None:
        raise KeyError(f"task {task_id} not found")
    return ScheduledTask.from_row(row)


def list_tasks(
    conn: sqlite3.Connection,
    *,
    user_id: str,
    status: Optional[str] = None,
    limit: int = 100,
) -> list[ScheduledTask]:
    """Newest-fire-at-first list for the desktop's Calendar list view.
    ``status=None`` returns every status; status='pending' is the most
    common query."""
    if status is not None:
        rows = conn.execute(
            f"SELECT {_TASK_COLS} FROM scheduled_tasks "
            "WHERE user_id = ? AND status = ? "
            "ORDER BY fire_at ASC LIMIT ?",
            (user_id, status, int(limit)),
        ).fetchall()
    else:
        rows = conn.execute(
            f"SELECT {_TASK_COLS} FROM scheduled_tasks "
            "WHERE user_id = ? "
            "ORDER BY fire_at ASC LIMIT ?",
            (user_id, int(limit)),
        ).fetchall()
    return [ScheduledTask.from_row(r) for r in rows]


def cancel_task(
    conn: sqlite3.Connection,
    *,
    user_id: str,
    task_id: str,
) -> ScheduledTask:
    """Soft-cancel. Status → 'cancelled'; cancelled_at set. Caller emits
    SCHEDULED_TASK_CANCELLED around this. Idempotent: cancelling an
    already-cancelled task is a no-op."""
    now = int(time.time())
    row = conn.execute(
        "SELECT status FROM scheduled_tasks "
        "WHERE task_id = ? AND user_id = ?",
        (task_id, user_id),
    ).fetchone()
    if row is None:
        raise KeyError(f"task {task_id} not found for user {user_id}")
    if row[0] == "cancelled":
        return get_task(conn, task_id)
    conn.execute(
        "UPDATE scheduled_tasks SET status='cancelled', cancelled_at=?, "
        "updated_at=? WHERE task_id = ? AND user_id = ?",
        (now, now, task_id, user_id),
    )
    conn.commit()
    return get_task(conn, task_id)


def _mark_running(conn: sqlite3.Connection, task_id: str) -> None:
    conn.execute(
        "UPDATE scheduled_tasks SET status='running', last_run_at=?, "
        "updated_at=? WHERE task_id = ?",
        (int(time.time()), int(time.time()), task_id),
    )
    conn.commit()


def _mark_fired(
    conn: sqlite3.Connection,
    *,
    task_id: str,
    status: str,            # 'done' | 'error' | 'pending' (recurring)
    result: Optional[dict],
    error: Optional[str],
    next_fire_at: Optional[int],
) -> None:
    now = int(time.time())
    sets = ["status=?", "updated_at=?"]
    args: list = [status, now]
    if result is not None:
        sets.append("result_json=?")
        args.append(json.dumps(result, sort_keys=True))
    if error is not None:
        sets.append("last_error=?")
        args.append(error)
    if next_fire_at is not None:
        sets.append("fire_at=?")
        args.append(int(next_fire_at))
    args.extend([task_id])
    conn.execute(
        f"UPDATE scheduled_tasks SET {', '.join(sets)} WHERE task_id = ?",
        args,
    )
    conn.commit()


# ─────────────────────────────────────────────────────────────────────
# Worker dispatch
# ─────────────────────────────────────────────────────────────────────


async def _execute_send_email(task: ScheduledTask) -> tuple[str, dict, Optional[str]]:
    """Dispatch one send_email task. Returns (status, result, error).

    Re-validates recipients at fire time — the operator may have
    tightened the allow-list since the task was confirmed."""
    from nexus_server import email_send
    p = task.payload or {}
    try:
        result = await email_send.send_email_async(
            user_id=task.user_id,
            to=p.get("to") or [],
            cc=p.get("cc") or [],
            subject=p.get("subject") or "(no subject)",
            body=p.get("body") or "(no body)",
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception(
            "scheduled send_email crashed: task=%s user=%s",
            task.task_id[:8], task.user_id,
        )
        return ("error", {}, f"{type(exc).__name__}: {exc}")

    result_dict = {
        "transport":   result.transport,
        "message":     result.message,
        "sent_to":     list(result.sent_to),
        "status_code": result.status_code,
    }
    if result.ok:
        return ("done", result_dict, None)
    return ("error", result_dict, result.message)


_EXECUTORS = {
    "send_email": _execute_send_email,
}


async def fire_task(task: ScheduledTask, conn: sqlite3.Connection) -> dict:
    """Execute one task. Updates row status. Returns the dict that
    SCHEDULED_TASK_FIRED's payload should carry."""
    exec_fn = _EXECUTORS.get(task.kind)
    if exec_fn is None:
        msg = f"no executor for kind {task.kind!r}"
        _mark_fired(
            conn, task_id=task.task_id, status="error",
            result=None, error=msg, next_fire_at=None,
        )
        return {"task_id": task.task_id, "status": "error", "error": msg}

    _mark_running(conn, task.task_id)
    started = time.monotonic()
    # Executors are SUPPOSED to catch their own exceptions and return
    # ('error', {}, msg) — see _execute_send_email — but a stub /
    # mock / future executor that raises must not crash the worker
    # tick. Belt-and-suspenders: convert any uncaught exception into
    # an error result so the row's status updates correctly and the
    # next tick can proceed.
    try:
        status, result, error = await exec_fn(task)
    except Exception as exc:  # noqa: BLE001
        logger.exception(
            "executor for kind=%s raised uncaught: task=%s",
            task.kind, task.task_id[:8],
        )
        status = "error"
        result = {}
        error = f"{type(exc).__name__}: {exc}"
    elapsed_ms = int((time.monotonic() - started) * 1000)

    # Phase 1: no recurrence support yet — recurrence_cron is stored
    # but always treated as one-shot. Phase 2 plugs croniter here.
    next_fire_at = None

    _mark_fired(
        conn, task_id=task.task_id,
        status=status, result=result, error=error,
        next_fire_at=next_fire_at,
    )

    fired_payload: dict = {
        "task_id":    task.task_id,
        "status":     status,
        "elapsed_ms": elapsed_ms,
    }
    if result:
        fired_payload["result_json"] = result
    if error:
        fired_payload["error"] = error
    if next_fire_at is not None:
        fired_payload["next_fire_at"] = int(next_fire_at)
    return fired_payload


# ─────────────────────────────────────────────────────────────────────
# Worker loop
# ─────────────────────────────────────────────────────────────────────


def _due_tasks(conn: sqlite3.Connection, now: int, limit: int) -> list[ScheduledTask]:
    rows = conn.execute(
        f"SELECT {_TASK_COLS} FROM scheduled_tasks "
        "WHERE status = 'pending' AND fire_at <= ? "
        "ORDER BY fire_at ASC LIMIT ?",
        (now, limit),
    ).fetchall()
    return [ScheduledTask.from_row(r) for r in rows]


async def _tick(get_conn) -> int:
    """One worker iteration. ``get_conn`` is a callable returning a
    fresh sqlite3 connection — kept as a parameter so tests can drive
    against an in-memory DB. Returns number of tasks fired."""
    from nexus_server.event_sourcing import EventKind, Store

    now = int(time.time())
    fired = 0
    with get_conn() as conn:
        due = _due_tasks(conn, now, MAX_BATCH_PER_TICK)
    for task in due:
        try:
            with get_conn() as conn:
                fired_payload = await fire_task(task, conn)
                # Audit-trail: emit SCHEDULED_TASK_FIRED. Best-effort —
                # a missing event_log shouldn't lose the projection
                # update we already committed above.
                try:
                    store = Store(conn)
                    store.emit_and_apply(
                        kind=EventKind.SCHEDULED_TASK_FIRED,
                        payload=fired_payload,
                        apply_fn=lambda *_a, **_k: None,  # projection done above
                        user_id=task.user_id,
                        patient_hash=task.patient_hash,
                    )
                    conn.commit()
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "scheduled_task FIRED event_log emit failed "
                        "(projection still ok): %s", exc,
                    )
            fired += 1
        except Exception as exc:  # noqa: BLE001
            logger.exception(
                "worker tick: task=%s crashed: %s",
                task.task_id[:8], exc,
            )
    return fired


async def _tick_research_assessments(get_conn) -> int:
    """Research Workspace Phase 3 tick. Scan ``study_assessments`` for
    rows whose ``due_at`` is within the next REMINDER_LEAD_MS window
    AND haven't had a reminder fired yet. Fires a .ics meeting invite
    to the doctor + (if consented) the patient.

    Returns the count of reminders sent this tick.
    """
    from nexus_server.research.calendar_ics import IcsEvent, send_with_ics
    REMINDER_LEAD_MS  = 24 * 3600 * 1000           # 24 h ahead
    REMINDER_GRACE_MS = 7 * 24 * 3600 * 1000       # mark missed after 7d
    now_ms = int(time.time() * 1000)
    fired  = 0

    # 1) Upcoming reminders
    with get_conn() as conn:
        try:
            rows = conn.execute(
                """
                SELECT user_id, study_id, patient_hash, visit_id,
                       assessment_kind, due_at
                FROM study_assessments
                WHERE status = 'planned'
                  AND due_at BETWEEN ? AND ?
                  AND (notes IS NULL OR notes NOT LIKE 'reminder_fired:%')
                LIMIT 50
                """,
                (now_ms, now_ms + REMINDER_LEAD_MS),
            ).fetchall()
        except sqlite3.Error:
            rows = []   # legacy install — study_assessments not yet created

    for row in rows:
        user_id, study_id, patient_hash, visit_id, akind, due_at = row
        try:
            with get_conn() as conn:
                study_row = conn.execute(
                    "SELECT display_name, short_code FROM research_studies "
                    "WHERE user_id = ? AND study_id = ?",
                    (user_id, study_id),
                ).fetchone()
                pat_row = conn.execute(
                    "SELECT email_address, email_reminder_consent, initials "
                    "FROM patients WHERE user_id = ? AND patient_hash = ?",
                    (user_id, patient_hash),
                ).fetchone()
                doctor_email = ""
                if _users_has_email_column(conn):
                    d = conn.execute(
                        "SELECT email_address FROM users WHERE user_id = ?",
                        (user_id,),
                    ).fetchone()
                    doctor_email = (d[0] if d else "") or ""

            study_name       = study_row[0] if study_row else study_id
            short_code       = study_row[1] if study_row else study_id
            patient_email    = ((pat_row[0] if pat_row else "") or "")
            patient_consent  = bool(pat_row[1]) if pat_row else False
            patient_initials = (pat_row[2] if pat_row else "") or "—"

            to_addrs: list[str] = []
            if doctor_email:                          to_addrs.append(doctor_email)
            if patient_email and patient_consent:     to_addrs.append(patient_email)

            event = IcsEvent(
                summary=f"{short_code} · {patient_initials} · {visit_id} · {akind}",
                dtstart_utc=int(due_at / 1000),
                dtend_utc=int(due_at / 1000) + 30 * 60,
                description=(
                    f"Research visit per protocol {study_name}.\n"
                    f"Visit: {visit_id}\nAssessment: {akind}\n"
                    "Reply Accept / Decline to update your calendar."
                ),
                location="Outpatient",
                organizer_email=doctor_email,
                attendee_emails=to_addrs,
            )
            if to_addrs:
                send_with_ics(
                    to=to_addrs,
                    subject=(f"[Research] {short_code} · "
                             f"复诊提醒 · {patient_initials}"),
                    body_text=event.description,
                    event=event,
                    from_addr=doctor_email or "",
                )

            with get_conn() as conn:
                conn.execute(
                    """
                    UPDATE study_assessments SET notes = ?
                    WHERE user_id=? AND study_id=? AND patient_hash=?
                      AND visit_id=? AND assessment_kind=?
                    """,
                    (f"reminder_fired:{now_ms}",
                     user_id, study_id, patient_hash, visit_id, akind),
                )
                conn.commit()
            fired += 1
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "assessment reminder fire failed (study=%s visit=%s): %s",
                study_id[:8], visit_id, exc,
            )

    # 2) Overdue → mark missed
    try:
        with get_conn() as conn:
            conn.execute(
                "UPDATE study_assessments SET status = 'missed' "
                "WHERE status = 'planned' AND due_at < ?",
                (now_ms - REMINDER_GRACE_MS,),
            )
            conn.commit()
    except sqlite3.Error:
        pass

    return fired


def _users_has_email_column(conn: sqlite3.Connection) -> bool:
    try:
        rows = conn.execute("PRAGMA table_info(users)").fetchall()
        return any(r[1] == "email_address" for r in rows)
    except sqlite3.Error:
        return False


async def worker_loop(get_conn, stop_event: asyncio.Event) -> None:
    """Forever loop — sleeps ``POLL_INTERVAL_SECONDS`` between ticks.
    Driven by ``main.py`` lifespan; cancellation via ``stop_event``
    or task cancellation."""
    logger.info("scheduler worker started (poll=%ds)", POLL_INTERVAL_SECONDS)
    while not stop_event.is_set():
        try:
            n = await _tick(get_conn)
            if n:
                logger.info("scheduler tick: fired %d task(s)", n)
        except Exception as exc:  # noqa: BLE001
            logger.exception("scheduler tick crashed: %s", exc)

        # Research Workspace tick — assessment reminders.
        try:
            n2 = await _tick_research_assessments(get_conn)
            if n2:
                logger.info(
                    "research scheduler tick: fired %d reminder(s)", n2,
                )
        except Exception as exc:  # noqa: BLE001
            logger.exception("research scheduler tick crashed: %s", exc)

        try:
            await asyncio.wait_for(
                stop_event.wait(), timeout=POLL_INTERVAL_SECONDS,
            )
        except asyncio.TimeoutError:
            continue
    logger.info("scheduler worker stopped")


def start_worker() -> tuple[asyncio.Task, asyncio.Event]:
    """Spawn the worker as an asyncio task. Returns (task, stop_event).
    Caller (main.py lifespan) must keep both alive for the app's
    lifetime and signal stop_event at shutdown."""
    from nexus_server.database import get_db_connection

    stop = asyncio.Event()
    task = asyncio.create_task(worker_loop(get_db_connection, stop))
    return task, stop
