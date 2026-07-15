"""Expert feedback loop — #130.

Captures the medic's (or any expert's) ✓ Accept / ✗ Correct gesture
on an assistant message and writes a feedback record to the relevant
skill's ``feedback.jsonl``. This is the training corpus for #131
vision-grounded skill evolution.

Wire shape
==========
POST /api/v1/feedback ::

    {
      "assistant_event_idx": 47,
      "kind": "accept" | "correct",
      "correction_text": "实际是钙化点，HU > 400",   # required when kind="correct"
      "skill_name": "chest-ct-reader",               # optional; defaults to "main-agent"
      "tag": "钙化_vs_结节"                          # optional short tag
    }

Persistence shape (one line per feedback) ::

    {
      "ts": "2026-06-07T14:32:11Z",
      "skill_name": "chest-ct-reader",
      "kind": "correct",
      "context": {
        "assistant_event_idx": 47,
        "agent_output": "...verbatim...",
        "referenced_file_ids": ["file-aaa"],
        "session_id": "session_20260607"
      },
      "feedback": {
        "kind": "correct",
        "expert_text": "实际是钙化点，HU > 400",
        "tag": "钙化_vs_结节"
      }
    }

The shape is intentionally additive — when #131 evolver consumes this
file it ignores fields it doesn't know, and new fields can be added
without a migration.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from nexus_server.auth import get_current_user

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1", tags=["feedback"])


# ── Storage location ──────────────────────────────────────────────────


def _skills_dir() -> Path:
    """Resolve the user-scoped skills directory.

    Mirrors ``tools_evolve._user_skills_dir`` — cwd-relative
    ``.nexus/skills``. The server is launched with cwd=$RUNE_HOME
    so feedback ends up at $RUNE_HOME/.nexus/skills/<name>/feedback.jsonl.
    """
    return Path.cwd() / ".nexus" / "skills"


def _feedback_path(skill_name: str) -> Path:
    """Per-skill feedback log. Folder + file are created on demand
    because some skills (the "main-agent" pseudo-skill) don't have a
    pre-existing folder under .nexus/skills."""
    folder = _skills_dir() / skill_name
    folder.mkdir(parents=True, exist_ok=True)
    return folder / "feedback.jsonl"


# ── Request / response shapes ─────────────────────────────────────────


class FeedbackRequest(BaseModel):
    assistant_event_idx: int = Field(..., ge=0)
    kind: str = Field(..., description="'accept' or 'correct'")
    correction_text: Optional[str] = Field(
        None, description="The medic's correction. Required when kind='correct'.",
    )
    skill_name: Optional[str] = Field(
        None,
        description=(
            "The skill whose protocol the correction targets. Default "
            "'main-agent' for top-level conversation; specific reader "
            "names like 'chest-ct-reader' for sub-agent responses."
        ),
    )
    tag: Optional[str] = Field(
        None,
        max_length=80,
        description="Optional short tag for categorising the feedback "
                    "('钙化_vs_结节', 'subpleural_omission', etc.).",
    )


class FeedbackResponse(BaseModel):
    ok: bool
    skill_name: str
    feedback_count: int = Field(
        ...,
        description=(
            "How many feedback entries are now in this skill's bucket. "
            "Lets the desktop show 'evolution will trigger at N' progress."
        ),
    )
    feedback_path: str


# ── Event log lookup ──────────────────────────────────────────────────


def _read_assistant_event(
    user_id: str, event_idx: int,
) -> tuple[Optional[str], Optional[dict], Optional[str]]:
    """Pull the assistant_response row identified by ``event_idx``.

    Returns ``(content, metadata, session_id)`` or ``(None, None, None)``
    if not found / wrong type. The metadata pulls
    ``referenced_file_ids`` (set by #128) so we can record which
    attachments this reply was about.
    """
    try:
        from nexus_server.twin_event_log import _open_readonly
    except ImportError:
        return None, None, None
    try:
        conn = _open_readonly(user_id)
    except Exception as e:  # noqa: BLE001
        logger.warning("event log open failed for %s: %s", user_id, e)
        return None, None, None
    try:
        row = conn.execute(
            "SELECT event_type, content, metadata, session_id "
            "FROM events WHERE idx = ?",
            (event_idx,),
        ).fetchone()
    except Exception as e:  # noqa: BLE001
        logger.warning("event log lookup failed: %s", e)
        return None, None, None
    finally:
        try:
            conn.close()
        except Exception as exc:  # noqa: BLE001
            logger.debug("closing connection failed: %s", exc)

    if not row:
        return None, None, None
    event_type, content, meta_json, sid = row
    if event_type != "assistant_response":
        return None, None, None
    try:
        meta = json.loads(meta_json) if meta_json else {}
    except Exception:  # noqa: BLE001
        meta = {}
    return content, meta, sid


# ── Append helper ─────────────────────────────────────────────────────


def _append_feedback(
    skill_name: str,
    kind: str,
    expert_text: Optional[str],
    tag: Optional[str],
    assistant_event_idx: int,
    agent_output: Optional[str],
    referenced_file_ids: list[str],
    session_id: Optional[str],
) -> Path:
    """Write one JSONL row. Atomic per-row (append-only file)."""
    record = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "skill_name": skill_name,
        "kind": kind,
        "context": {
            "assistant_event_idx": assistant_event_idx,
            "agent_output": agent_output or "",
            "referenced_file_ids": list(referenced_file_ids or []),
            "session_id": session_id or "",
        },
        "feedback": {
            "kind": kind,
            "expert_text": expert_text or "",
            "tag": tag or "",
        },
    }
    path = _feedback_path(skill_name)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    return path


def _count_lines(path: Path) -> int:
    """Cheap count for the response payload. Empty file → 0."""
    if not path.exists():
        return 0
    try:
        with path.open("rb") as fh:
            return sum(1 for _ in fh)
    except Exception:  # noqa: BLE001
        return 0


# ── Endpoint ──────────────────────────────────────────────────────────


@router.post("/feedback", response_model=FeedbackResponse)
async def submit_feedback(
    req: FeedbackRequest,
    current_user: str = Depends(get_current_user),
) -> FeedbackResponse:
    """Record an Accept / Correct gesture against an assistant message.

    Validates that the referenced event is an ``assistant_response``;
    400s otherwise. ``correction_text`` is required when
    ``kind='correct'`` — otherwise the feedback row would be
    information-free.
    """
    kind = (req.kind or "").strip().lower()
    if kind not in ("accept", "correct"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="`kind` must be 'accept' or 'correct'.",
        )
    if kind == "correct" and not (req.correction_text or "").strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "`correction_text` is required when kind='correct'. "
                "Skip the feedback if the medic didn't actually type a "
                "correction."
            ),
        )

    # Pull the assistant message + its referenced_file_ids so we don't
    # trust the client to claim what they were correcting. The event
    # log is the source of truth.
    content, meta, sid = _read_assistant_event(
        current_user, req.assistant_event_idx,
    )
    if content is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=(
                f"No assistant_response event at idx "
                f"{req.assistant_event_idx} for this user."
            ),
        )

    skill_name = (req.skill_name or "main-agent").strip() or "main-agent"
    # Defensive: skill name must be a single path segment. Otherwise
    # a malicious client could write feedback into ../../etc.
    if "/" in skill_name or "\\" in skill_name or skill_name.startswith("."):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="`skill_name` must be a single path segment.",
        )

    referenced_file_ids: list[str] = []
    if isinstance(meta, dict):
        rfi = meta.get("referenced_file_ids")
        if isinstance(rfi, list):
            referenced_file_ids = [str(x) for x in rfi if x]

    path = _append_feedback(
        skill_name=skill_name,
        kind=kind,
        expert_text=req.correction_text,
        tag=req.tag,
        assistant_event_idx=req.assistant_event_idx,
        agent_output=content,
        referenced_file_ids=referenced_file_ids,
        session_id=sid,
    )

    count = _count_lines(path)
    logger.info(
        "feedback recorded user=%s skill=%s kind=%s count=%d",
        current_user, skill_name, kind, count,
    )
    return FeedbackResponse(
        ok=True,
        skill_name=skill_name,
        feedback_count=count,
        feedback_path=str(path),
    )


# ── Stats endpoint (lets desktop show progress toward evolution trigger) ─


class FeedbackStatsResponse(BaseModel):
    counts_by_skill: dict
    total: int


@router.get("/feedback/stats", response_model=FeedbackStatsResponse)
async def feedback_stats(
    current_user: str = Depends(get_current_user),
) -> FeedbackStatsResponse:
    """Return per-skill feedback counts. Lets the desktop UI render
    a 'corrections recorded: 3/5 — evolution triggers at 5' chip
    on the skill / pack page."""
    # Reading is cheap; we walk every feedback.jsonl under .nexus/skills.
    counts: dict[str, int] = {}
    base = _skills_dir()
    if base.exists():
        for skill_folder in base.iterdir():
            if not skill_folder.is_dir():
                continue
            f = skill_folder / "feedback.jsonl"
            if f.exists():
                counts[skill_folder.name] = _count_lines(f)
    return FeedbackStatsResponse(
        counts_by_skill=counts, total=sum(counts.values()),
    )
