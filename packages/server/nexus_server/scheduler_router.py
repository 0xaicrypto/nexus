"""REST endpoints for scheduled tasks (Phase 1).

  POST   /api/v1/schedule/confirm      — user confirms a proposal
  POST   /api/v1/schedule/extract      — heuristic extractor preview
                                         (used in tests + dev UI; chat
                                         router fires this implicitly)
  GET    /api/v1/schedule/list         — list this user's tasks
  DELETE /api/v1/schedule/{task_id}    — cancel one task

Auth: every endpoint Depends(get_current_user). The user_id baked
into auth is the row's owner; no cross-user reads/writes possible.
"""
from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from nexus_server import schedule_intent, scheduler
from nexus_server.auth import get_current_user
from nexus_server.database import get_db_connection
from nexus_server.event_sourcing import EventKind, Store

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/schedule", tags=["schedule"])


# ─────────────────────────────────────────────────────────────────────
# Wire shapes
# ─────────────────────────────────────────────────────────────────────


class ConfirmRequest(BaseModel):
    """User confirmed a proposal (or composed a task from scratch in
    the UI). All fields are required since the medic just clicked
    Confirm on a fully-formed card."""

    kind:            str
    payload:         dict
    fire_at:         int                      # unix seconds UTC
    user_tz:         str                      = "UTC"
    recurrence_cron: Optional[str]            = None
    session_id:      Optional[str]            = None
    patient_hash:    Optional[str]            = None
    proposal_id:     Optional[str]            = None  # for audit


class TaskView(BaseModel):
    """Serialised ScheduledTask for the JSON wire."""

    task_id:         str
    user_id:         str
    patient_hash:    Optional[str]
    session_id:      Optional[str]
    kind:            str
    payload:         dict
    fire_at:         int
    user_tz:         str
    recurrence_cron: Optional[str]
    status:          str
    last_run_at:     Optional[int]
    last_error:      Optional[str]
    result:          Optional[dict]
    created_at:      int
    updated_at:      int
    cancelled_at:    Optional[int]


class TaskListResponse(BaseModel):
    tasks: list[TaskView]


class ExtractRequest(BaseModel):
    """For the heuristic-extractor preview endpoint."""

    text:         str
    user_tz:      str = "UTC"
    session_id:   Optional[str] = None
    patient_hash: Optional[str] = None


class ExtractResponse(BaseModel):
    """Null result = no schedule intent detected. Otherwise carries
    the same fields the UI confirmation card needs."""

    proposal: Optional[dict] = None


# ─────────────────────────────────────────────────────────────────────
# Routes
# ─────────────────────────────────────────────────────────────────────


@router.post("/confirm", response_model=TaskView)
async def confirm_task(
    req: ConfirmRequest,
    user_id: str = Depends(get_current_user),
) -> TaskView:
    """Medic confirmed a scheduling proposal — persist + start the
    countdown.

    Errors:
      422 — unsupported kind, fire_at out of range, per-user quota
    """
    try:
        with get_db_connection() as conn:
            task = scheduler.create_task(
                conn,
                user_id=user_id,
                kind=req.kind,
                payload=req.payload,
                fire_at=req.fire_at,
                user_tz=req.user_tz,
                recurrence_cron=req.recurrence_cron,
                session_id=req.session_id,
                patient_hash=req.patient_hash,
            )
            # Best-effort SCHEDULED_TASK_CREATED audit event — the
            # projection row above is the source of truth for the
            # worker; the event log is for replay / audit.
            try:
                store = Store(conn)
                store.emit_and_apply(
                    kind=EventKind.SCHEDULED_TASK_CREATED,
                    payload={
                        "task_id":         task.task_id,
                        "kind":            task.kind,
                        "payload_json":    task.payload,
                        "fire_at":         task.fire_at,
                        "user_tz":         task.user_tz,
                        "session_id":      task.session_id or "",
                        "patient_hash":    task.patient_hash or "",
                        "recurrence_cron": task.recurrence_cron or "",
                        "proposal_id":     req.proposal_id or "",
                    },
                    apply_fn=lambda *_a, **_k: None,
                    user_id=user_id,
                    patient_hash=task.patient_hash,
                )
                conn.commit()
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "SCHEDULED_TASK_CREATED audit emit failed "
                    "(projection ok): %s", exc,
                )
    except ValueError as ve:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(ve),
        ) from ve
    except Exception as exc:  # noqa: BLE001
        logger.exception("confirm_task failed: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"{type(exc).__name__}: {exc}",
        ) from exc

    return TaskView(**task.to_dict())


@router.post("/extract", response_model=ExtractResponse)
async def extract_proposal_route(
    req: ExtractRequest,
    _: str = Depends(get_current_user),
) -> ExtractResponse:
    """Probe a piece of chat text for schedule intent.

    Returned `proposal` is None when nothing was detected. Otherwise
    it's the JSON shape the UI confirmation card needs (proposal_id +
    fire_at + payload + summary + needs_user_input)."""
    p = schedule_intent.extract_proposal(
        user_text=req.text,
        user_tz=req.user_tz,
        session_id=req.session_id,
        patient_hash=req.patient_hash,
    )
    if p is None:
        return ExtractResponse(proposal=None)
    return ExtractResponse(proposal={
        "proposal_id":      p.proposal_id,
        "kind":             p.kind,
        "fire_at":          p.fire_at,
        "user_tz":          p.user_tz,
        "summary":          p.summary,
        "payload":          p.payload,
        "recurrence_cron":  p.recurrence_cron,
        "session_id":       p.session_id,
        "patient_hash":     p.patient_hash,
        "needs_user_input": list(p.needs_user_input),
    })


@router.get("/list", response_model=TaskListResponse)
async def list_route(
    status_filter: Optional[str] = None,
    limit:         int = 100,
    user_id:       str = Depends(get_current_user),
) -> TaskListResponse:
    """Newest-fire-at first. status_filter ∈ {None, pending, done,
    error, cancelled, running}."""
    if limit <= 0 or limit > 500:
        limit = 100
    with get_db_connection() as conn:
        tasks = scheduler.list_tasks(
            conn,
            user_id=user_id,
            status=status_filter,
            limit=limit,
        )
    return TaskListResponse(
        tasks=[TaskView(**t.to_dict()) for t in tasks],
    )


@router.delete("/{task_id}", response_model=TaskView)
async def cancel_route(
    task_id: str,
    user_id: str = Depends(get_current_user),
) -> TaskView:
    """Cancel a pending task. Idempotent. Soft-delete (status →
    'cancelled'); the row + event-log entry are retained."""
    try:
        with get_db_connection() as conn:
            task = scheduler.cancel_task(
                conn, user_id=user_id, task_id=task_id,
            )
            try:
                store = Store(conn)
                store.emit_and_apply(
                    kind=EventKind.SCHEDULED_TASK_CANCELLED,
                    payload={"task_id": task_id, "reason": "user_request"},
                    apply_fn=lambda *_a, **_k: None,
                    user_id=user_id,
                    patient_hash=task.patient_hash,
                )
                conn.commit()
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "SCHEDULED_TASK_CANCELLED audit emit failed: %s", exc,
                )
    except KeyError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"task {task_id} not found",
        )
    return TaskView(**task.to_dict())
