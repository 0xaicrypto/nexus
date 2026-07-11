"""User profile router.

Reads / updates the user-facing profile fields. The schema lives in
`users` (extended by the gated-beta + billing migration) and now
contains both identity bits (id, display_name, created_at) AND signup
metadata (email, organization, intended_use, status, tier).

Endpoints:

  GET   /api/v1/user/profile   — full profile snapshot for the desktop.
  PUT   /api/v1/user/profile   — legacy "display_name only" update,
                                 preserved for older desktop builds.
  PATCH /api/v1/user/profile   — partial update of display_name,
                                 organization, intended_use. Email and
                                 status/tier remain server-managed.
"""

import logging
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from nexus_server.auth import get_current_user
from nexus_server.database import get_db_connection

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/user", tags=["user"])


# ───────────────────────────────────────────────────────────────────────────
# Request / Response models
# ───────────────────────────────────────────────────────────────────────────


class UserProfile(BaseModel):
    """User profile snapshot. All signup-metadata fields are Optional
    because they were added by a later migration — pre-migration rows
    return NULL for them, which Pydantic surfaces as None."""

    user_id: str
    display_name: str
    created_at: str
    updated_at: str
    # Signup metadata (added with the gated-beta + Stripe migration).
    email: Optional[str] = None
    organization: Optional[str] = None
    intended_use: Optional[str] = None
    status: Optional[str] = None
    tier: Optional[str] = None


class UserProfileUpdate(BaseModel):
    """Legacy PUT shape — display_name only. Kept so the previous
    desktop build (which hits PUT) doesn't break after a server
    upgrade."""

    display_name: Optional[str] = Field(
        None, min_length=1, max_length=255
    )


class UserProfilePatch(BaseModel):
    """Partial update. All fields optional — server applies only what
    the client sent. Empty strings ARE applied (you can clear an
    organization by sending ""); only `null` / missing keys are
    skipped."""

    display_name: Optional[str] = Field(None, max_length=255)
    organization: Optional[str] = Field(None, max_length=255)
    intended_use: Optional[str] = Field(None, max_length=2000)


# ───────────────────────────────────────────────────────────────────────────
# Helpers
# ───────────────────────────────────────────────────────────────────────────


def _row_to_profile(row) -> UserProfile:
    """Convert a sqlite3.Row from the users table into the response
    DTO. `row` must include id, display_name, created_at, updated_at
    and the four signup-metadata columns by name."""
    return UserProfile(
        user_id=row["id"],
        display_name=row["display_name"] or "",
        created_at=row["created_at"] or "",
        updated_at=row["updated_at"] or "",
        email=row["email"] if "email" in row.keys() else None,
        organization=row["organization"] if "organization" in row.keys() else None,
        intended_use=row["intended_use"] if "intended_use" in row.keys() else None,
        status=row["status"] if "status" in row.keys() else None,
        tier=row["tier"] if "tier" in row.keys() else None,
    )


def _fetch_user(conn, user_id: str):
    """Read the user row by id. Returns None on miss. We select * so
    callers don't need to know which columns exist on disk — the row
    factory exposes them by name. Robust against future columns being
    added without touching this helper."""
    return conn.execute(
        "SELECT * FROM users WHERE id = ?", (user_id,)
    ).fetchone()


# ───────────────────────────────────────────────────────────────────────────
# Routes
# ───────────────────────────────────────────────────────────────────────────


@router.get("/profile", response_model=UserProfile)
async def get_user_profile(
    current_user: str = Depends(get_current_user),
) -> UserProfile:
    """Return the authenticated user's profile."""
    try:
        with get_db_connection() as conn:
            row = _fetch_user(conn, current_user)
        if not row:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="User not found",
            )
        return _row_to_profile(row)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Get profile error: {e}")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Failed to retrieve profile",
        )


@router.put("/profile", response_model=UserProfile)
async def update_user_profile(
    request: UserProfileUpdate,
    current_user: str = Depends(get_current_user),
) -> UserProfile:
    """Legacy display-name-only update. New clients should use PATCH."""
    try:
        now = datetime.now(timezone.utc).isoformat()
        with get_db_connection() as conn:
            row = _fetch_user(conn, current_user)
            if not row:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="User not found",
                )
            display_name = request.display_name or row["display_name"]
            conn.execute(
                "UPDATE users SET display_name = ?, updated_at = ? WHERE id = ?",
                (display_name, now, current_user),
            )
            conn.commit()
            row = _fetch_user(conn, current_user)
        logger.info(f"User profile updated (PUT): {current_user}")
        return _row_to_profile(row)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Update profile error: {e}")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Failed to update profile",
        )


@router.patch("/profile", response_model=UserProfile)
async def patch_user_profile(
    request: UserProfilePatch,
    current_user: str = Depends(get_current_user),
) -> UserProfile:
    """Partial update — apply only the keys present in `request`.

    We dispatch dynamically because the columns being touched depend
    on which fields the client populated. SQLite doesn't have a true
    "set only if non-null" SQL primitive that's portable across our
    other backends, so we build the UPDATE statement explicitly.

    Email is intentionally NOT mutable here — it's bound to the
    account the user signed up with. Tier / status flow
    through the billing webhook + admin approval routes, not the
    user-facing profile endpoint.
    """
    # Build a column→value map from the patch body. `model_dump(
    # exclude_unset=True)` gives us only the keys the client sent
    # (so a missing key means "don't touch" while an explicit empty
    # string means "clear this field"). This is what Pydantic's
    # patch-style update is for.
    payload = request.model_dump(exclude_unset=True)
    if not payload:
        # Nothing to update; just echo back the current profile so the
        # client doesn't have to special-case empty-PATCH responses.
        with get_db_connection() as conn:
            row = _fetch_user(conn, current_user)
        if not row:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="User not found",
            )
        return _row_to_profile(row)

    # Whitelist the columns we'll touch — anything outside this set is
    # a no-op even if a future Pydantic schema accidentally exposes it.
    ALLOWED = {"display_name", "organization", "intended_use"}
    updates = {k: v for k, v in payload.items() if k in ALLOWED}
    if not updates:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No updatable fields in request.",
        )

    now = datetime.now(timezone.utc).isoformat()
    set_clause = ", ".join(f"{col} = ?" for col in updates.keys())
    params = list(updates.values()) + [now, current_user]
    sql = f"UPDATE users SET {set_clause}, updated_at = ? WHERE id = ?"

    try:
        with get_db_connection() as conn:
            row = _fetch_user(conn, current_user)
            if not row:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND, detail="User not found",
                )
            conn.execute(sql, params)
            conn.commit()
            # Re-read so the response carries the canonical updated_at.
            row = _fetch_user(conn, current_user)
        logger.info(
            "User profile patched user=%s fields=%s",
            current_user, list(updates.keys()),
        )
        return _row_to_profile(row)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Patch profile error: {e}")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Failed to update profile",
        )
