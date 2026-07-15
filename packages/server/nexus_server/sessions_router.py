"""HTTP routes for multi-session management.

Mounts under ``/api/v1/sessions``. The desktop's sidebar uses these to
list, create, rename, and archive chat threads.

Why a separate file from sessions.py?
-------------------------------------
``sessions.py`` is the data layer (DB CRUD, business rules). This file
is the transport layer (FastAPI routing, auth, error mapping). Keeping
them apart so the data layer is unit-testable without spinning up a
TestClient — and so the router can grow auth / rate-limit decorators
without polluting the storage code.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import Response

from nexus_server import sessions
from nexus_server.auth import get_current_user
from nexus_server.session_sync import emit_session_metadata

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/sessions", tags=["sessions"])


async def _emit_to_twin(
    user_id: str,
    *,
    session_id: str,
    action: str,
    title=None,
    archived=None,
) -> None:
    """Best-effort: route the session metadata change into twin's
    EventLog so it's chain-mirrored alongside chat messages. Failure
    here doesn't fail the API call — the SQL mutation already
    happened, and the next emit succeeds will heal the durability gap.
    """
    try:
        from nexus_server.twin_manager import get_twin
        twin = await get_twin(user_id)
        emit_session_metadata(
            twin,
            session_id=session_id,
            action=action,
            title=title,
            archived=archived,
        )
    except Exception as e:  # noqa: BLE001
        logger.debug("session_metadata emit skipped (%s)", e)


@router.get("", response_model=sessions.SessionListResponse)
async def list_sessions_endpoint(
    include_archived: bool = False,
    current_user: str = Depends(get_current_user),
) -> sessions.SessionListResponse:
    """Return all sessions for the current user, newest activity first.

    The synthetic "Default chat" session is appended automatically if
    the user has any pre-sessions chat history (events tagged with
    empty session_id). The desktop renders both the same way; the
    ``is_default`` flag is informational so the UI can hide
    rename/archive controls for it.
    """
    items = sessions.list_sessions(current_user, include_archived=include_archived)
    return sessions.SessionListResponse(sessions=items)


@router.post("", response_model=sessions.SessionInfo, status_code=status.HTTP_201_CREATED)
async def create_session_endpoint(
    body: sessions.CreateSessionRequest,
    current_user: str = Depends(get_current_user),
) -> sessions.SessionInfo:
    """Create a new chat session. Returns the new SessionInfo.

    The id is server-issued in twin's ``_thread_id`` format
    (``session_xxxxxxxx``) so it lines up with what twin's EventLog
    stamps onto each event row.
    """
    info = sessions.create_session(current_user, title=body.title)
    await _emit_to_twin(
        current_user, session_id=info.id, action="create", title=info.title,
    )
    return info


@router.patch("/{session_id}", response_model=sessions.SessionInfo)
async def update_session_endpoint(
    session_id: str,
    body: sessions.UpdateSessionRequest,
    current_user: str = Depends(get_current_user),
) -> sessions.SessionInfo:
    """Rename or (un)archive a session.

    Returns the updated row. 404 if the session doesn't exist or
    belongs to another user. 400 if the caller passed no fields and
    the row is the synthetic default session (which can't be edited).
    """
    if session_id == sessions.DEFAULT_SESSION_ID:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="The default session is synthetic and cannot be renamed or archived.",
        )
    info = sessions.update_session(
        current_user, session_id,
        title=body.title, archived=body.archived,
    )
    if info is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Session {session_id} not found",
        )
    # Pick the most descriptive action label for the EventLog so the
    # raw audit-trail dump reads naturally. Renames take precedence
    # over archive flips because the archive flag is a 1-bit toggle
    # while the title can be arbitrary user text.
    if body.title is not None:
        action = "rename"
    elif body.archived is True:
        action = "archive"
    elif body.archived is False:
        action = "unarchive"
    else:
        action = "update"
    await _emit_to_twin(
        current_user,
        session_id=session_id,
        action=action,
        title=body.title,
        archived=body.archived,
    )
    return info


@router.delete("/{session_id}")
async def delete_session_endpoint(
    session_id: str,
    hard: bool = False,
    current_user: str = Depends(get_current_user),
):
    """Archive or hard-delete a session.

    Two modes:
      * ``hard=false`` (default) — soft archive. Twin's event_log keeps
        every message for audit / anchor history. Setting ``archived=1``
        hides it from the sidebar's default list; PATCH archived=False
        brings it back. Returns 204.
      * ``hard=true`` — full delete. Tells twin to wipe SQLite rows for
        this session_id, drop stored session objects, and clear the
        ``nexus_sessions`` metadata row. BSC state-root anchors are
        immutable on chain and stay; the response notes the orphan count
        and the immutability of historic anchors. Returns 200 + summary.
    """
    if session_id == sessions.DEFAULT_SESSION_ID:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="The default session is synthetic and cannot be deleted.",
        )

    info = sessions.get_session(current_user, session_id)
    if info is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Session {session_id} not found",
        )

    if not hard:
        ok = sessions.archive_session(current_user, session_id)
        if not ok:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Session {session_id} not found",
            )
        await _emit_to_twin(
            current_user, session_id=session_id,
            action="archive", archived=True,
        )
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    # ── Hard delete ──────────────────────────────────────────────
    # Order matters: twin runs cleanup FIRST so the audit trail event
    # ('session_deleted') gets written into the still-existing
    # event_log + still-existing sessions row before we drop them.
    twin_result = {"deleted_event_count": 0, "storage": {}, "bsc_anchors_immutable_note": ""}
    try:
        from nexus_server.twin_manager import get_twin
        twin = await get_twin(current_user)
        if hasattr(twin, "delete_session"):
            twin_result = await twin.delete_session(session_id)
    except Exception as e:
        logger.warning(
            "twin.delete_session failed for user=%s session=%s: %s",
            current_user, session_id, e,
        )
        twin_result["error"] = str(e)

    sessions.delete_session_row(current_user, session_id)
    await _emit_to_twin(
        current_user, session_id=session_id, action="delete",
    )

    return {
        "session_id": session_id,
        "hard_deleted": True,
        "deleted_event_count": twin_result.get("deleted_event_count", 0),
        "storage": twin_result.get("storage", {}),
        "bsc_note": twin_result.get(
            "bsc_anchors_immutable_note",
            "BSC state-root anchors are immutable on chain — historic "
            "anchors are not deleted, but the agent no longer surfaces "
            "this session's content from any read path.",
        ),
    }
