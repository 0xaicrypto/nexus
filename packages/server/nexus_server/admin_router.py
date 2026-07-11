"""Admin router — basic multi-user management.

All endpoints require role='admin' (enforced by ``require_admin``,
which layers on top of ``get_current_user`` so disabled/deleted
accounts are already filtered out before the role check).

Endpoints:
  GET  /api/v1/admin/users                          — list users
  POST /api/v1/admin/users/{user_id}/disable        — lock account
  POST /api/v1/admin/users/{user_id}/enable         — unlock account
  POST /api/v1/admin/users/{user_id}/reset-password — set new password
"""

import logging
import sqlite3
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field, field_validator

from nexus_server.auth import get_current_user
from nexus_server.auth.routes import _validate_password, hash_password
from nexus_server.database import get_db_connection

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/admin", tags=["admin"])


# ───────────────────────────────────────────────────────────────────────────
# Dependency
# ───────────────────────────────────────────────────────────────────────────


async def require_admin(
    current_user: str = Depends(get_current_user),
) -> str:
    """Dependency: the authenticated user must have role='admin'."""
    with get_db_connection() as conn:
        row = conn.execute(
            "SELECT role FROM users WHERE id = ? AND deleted_at IS NULL",
            (current_user,),
        ).fetchone()
    if row is None or (row["role"] or "user") != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": "admin_required",
                    "message": "Admin privileges required."},
        )
    return current_user


# ───────────────────────────────────────────────────────────────────────────
# Models
# ───────────────────────────────────────────────────────────────────────────


class AdminUserInfo(BaseModel):
    user_id: str
    username: str                       # = users.display_name
    role: str
    created_at: Optional[str] = None
    disabled_at: Optional[str] = None
    last_login_at: Optional[str] = None
    has_password: bool = False


class AdminUsersListResponse(BaseModel):
    users: list[AdminUserInfo]


class AdminUserActionResponse(BaseModel):
    user_id: str
    disabled_at: Optional[str] = None
    ok: bool = True


class AdminResetPasswordRequest(BaseModel):
    new_password: str = Field(..., min_length=8, max_length=256)

    @field_validator("new_password")
    @classmethod
    def validate_password(cls, v: str) -> str:
        return _validate_password(v)


# ───────────────────────────────────────────────────────────────────────────
# Helpers
# ───────────────────────────────────────────────────────────────────────────


def _fetch_user(conn, user_id: str):
    conn.row_factory = sqlite3.Row
    return conn.execute(
        "SELECT id, display_name, role, created_at, disabled_at, "
        "       last_login_at, password_hash "
        "FROM users WHERE id = ? AND deleted_at IS NULL",
        (user_id,),
    ).fetchone()


def _not_found() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail={"code": "user_not_found", "message": "No such user."},
    )


# ───────────────────────────────────────────────────────────────────────────
# Routes
# ───────────────────────────────────────────────────────────────────────────


@router.get("/users", response_model=AdminUsersListResponse)
async def list_users(
    admin_id: str = Depends(require_admin),
) -> AdminUsersListResponse:
    """List all live (non-deleted) users for the admin console."""
    with get_db_connection() as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT id, display_name, role, created_at, disabled_at, "
            "       last_login_at, password_hash "
            "FROM users WHERE deleted_at IS NULL "
            "ORDER BY created_at ASC"
        ).fetchall()
    return AdminUsersListResponse(users=[
        AdminUserInfo(
            user_id=r["id"],
            username=r["display_name"] or "",
            role=r["role"] or "user",
            created_at=r["created_at"],
            disabled_at=r["disabled_at"],
            last_login_at=r["last_login_at"],
            has_password=r["password_hash"] is not None,
        )
        for r in rows
    ])


@router.post("/users/{user_id}/disable",
             response_model=AdminUserActionResponse)
async def disable_user(
    user_id: str,
    admin_id: str = Depends(require_admin),
) -> AdminUserActionResponse:
    """Disable an account. Its JWTs stop working immediately (checked
    in get_current_user) and /login returns 403. Admins cannot
    disable themselves — that would brick the deployment."""
    if user_id == admin_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "cannot_disable_self",
                    "message": "Admins cannot disable their own account."},
        )
    now = datetime.now(timezone.utc).isoformat()
    with get_db_connection() as conn:
        if _fetch_user(conn, user_id) is None:
            raise _not_found()
        conn.execute(
            "UPDATE users SET disabled_at = COALESCE(disabled_at, ?), "
            "updated_at = ? WHERE id = ?",
            (now, now, user_id),
        )
        conn.commit()
        row = _fetch_user(conn, user_id)
    logger.info("admin %s disabled user %s", admin_id[:8], user_id[:8])
    return AdminUserActionResponse(
        user_id=user_id, disabled_at=row["disabled_at"],
    )


@router.post("/users/{user_id}/enable",
             response_model=AdminUserActionResponse)
async def enable_user(
    user_id: str,
    admin_id: str = Depends(require_admin),
) -> AdminUserActionResponse:
    """Re-enable a previously disabled account."""
    now = datetime.now(timezone.utc).isoformat()
    with get_db_connection() as conn:
        if _fetch_user(conn, user_id) is None:
            raise _not_found()
        conn.execute(
            "UPDATE users SET disabled_at = NULL, updated_at = ? "
            "WHERE id = ?",
            (now, user_id),
        )
        conn.commit()
    logger.info("admin %s enabled user %s", admin_id[:8], user_id[:8])
    return AdminUserActionResponse(user_id=user_id, disabled_at=None)


@router.post("/users/{user_id}/reset-password",
             response_model=AdminUserActionResponse)
async def reset_password(
    user_id: str,
    body: AdminResetPasswordRequest,
    admin_id: str = Depends(require_admin),
) -> AdminUserActionResponse:
    """Admin sets a new password on any account (including legacy
    passwordless ones — this also 'claims' the account)."""
    now = datetime.now(timezone.utc).isoformat()
    password_hash = hash_password(body.new_password)
    with get_db_connection() as conn:
        if _fetch_user(conn, user_id) is None:
            raise _not_found()
        conn.execute(
            "UPDATE users SET password_hash = ?, updated_at = ? "
            "WHERE id = ?",
            (password_hash, now, user_id),
        )
        conn.commit()
        row = _fetch_user(conn, user_id)
    logger.info("admin %s reset password for user %s",
                admin_id[:8], user_id[:8])
    return AdminUserActionResponse(
        user_id=user_id, disabled_at=row["disabled_at"],
    )
