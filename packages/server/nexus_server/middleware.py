"""Shared middleware and utilities.

Rate limiting and request logging middleware.
"""

import logging
import uuid
from datetime import datetime, timedelta, timezone

from nexus_server.database import get_db_connection

logger = logging.getLogger(__name__)


def check_rate_limit(
    user_id: str,
    endpoint: str,
    limit_per_minute: int,
) -> bool:
    """Check if user has exceeded rate limit for an endpoint.

    Uses a sliding 1-minute window stored in SQLite rate_limits table.
    Creates a new window entry if none exists in the current minute.

    Args:
        user_id: Authenticated user ID
        endpoint: API endpoint path
        limit_per_minute: Max requests allowed per minute

    Returns:
        True if within limit, False if exceeded
    """
    with get_db_connection() as conn:
        cursor = conn.cursor()

        now = datetime.now(timezone.utc)
        window_start = now - timedelta(minutes=1)

        # Get current request count in the window
        cursor.execute(
            """
            SELECT request_count FROM rate_limits
            WHERE user_id = ? AND endpoint = ? AND window_start > ?
            """,
            (user_id, endpoint, window_start),
        )
        row = cursor.fetchone()

        if row:
            count = row[0]
            if count >= limit_per_minute:
                return False
            # Increment count
            cursor.execute(
                """
                UPDATE rate_limits SET request_count = request_count + 1
                WHERE user_id = ? AND endpoint = ? AND window_start > ?
                """,
                (user_id, endpoint, window_start),
            )
        else:
            # Create new window entry
            limit_id = str(uuid.uuid4())
            cursor.execute(
                """
                INSERT INTO rate_limits
                (id, user_id, endpoint, request_count, window_start)
                VALUES (?, ?, ?, 1, ?)
                """,
                (limit_id, user_id, endpoint, now),
            )

        conn.commit()
        return True
