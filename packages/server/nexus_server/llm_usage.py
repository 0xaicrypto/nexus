"""LLM token spend metering — #113.

Per-user / per-day rollup of prompt + completion tokens, broken down
by provider + model. Feeds the desktop's "Today: 12,345 tokens · $0.18"
indicator and the future paid-tier billing path.

Public surface
==============
* :func:`record_usage(user_id, provider, model, prompt_tokens,
  completion_tokens)` — called from llm_gateway / SDK whenever a
  provider returns usage data. Best-effort: failure to record never
  breaks the chat path (the agent's reply is what matters).
* :func:`usage_summary(user_id, days)` — returns total + per-day +
  per-model rollups for the last ``days`` days. Used by the
  /usage_summary endpoint.
* :func:`estimate_cost_usd(provider, model, prompt_tokens,
  completion_tokens)` — rough $-estimate from public price tables
  (Gemini / Claude / OpenAI). Imperfect but useful for the meter
  UI; not used for actual billing.

Pricing reference (refreshed manually — update when providers shift)
================================================================
Gemini 2.5 Pro:    $1.25 / 1M input, $10.00 / 1M output
Gemini 2.5 Flash:  $0.30 / 1M input, $2.50  / 1M output
Claude Sonnet 4:   $3.00 / 1M input, $15.00 / 1M output
Claude Haiku 4.5:  $1.00 / 1M input, $5.00  / 1M output
GPT-4o:            $2.50 / 1M input, $10.00 / 1M output
GPT-4o mini:       $0.15 / 1M input, $0.60  / 1M output
"""
from __future__ import annotations

import logging
import sqlite3
from datetime import date, datetime, timedelta, timezone

logger = logging.getLogger(__name__)


# Rough public-pricing table, $ per million tokens.
# Keep model match LOOSE — providers add "-preview" / "-2025-05-13"
# suffixes that we don't want to chase. Match by prefix when possible.
_PRICING_USD_PER_M: dict[str, tuple[float, float]] = {
    # Gemini
    "gemini-2.5-pro":    (1.25, 10.00),
    "gemini-2.5-flash":  (0.30,  2.50),
    "gemini-1.5-pro":    (1.25,  5.00),
    "gemini-1.5-flash":  (0.075, 0.30),
    # Claude
    "claude-opus-4":     (15.00, 75.00),
    "claude-sonnet-4":   (3.00, 15.00),
    "claude-haiku-4":    (1.00,  5.00),
    "claude-3.5-sonnet": (3.00, 15.00),
    "claude-3.5-haiku":  (1.00,  5.00),
    # OpenAI
    "gpt-4o-mini":       (0.15,  0.60),
    "gpt-4o":            (2.50, 10.00),
    "gpt-4-turbo":       (10.00, 30.00),
}


def estimate_cost_usd(
    provider: str, model: str,
    prompt_tokens: int, completion_tokens: int,
) -> float:
    """Rough $-estimate. Returns 0.0 when the model isn't priced —
    callers can show "—" for "unknown" rather than a misleading $0."""
    if not model:
        return 0.0
    key = model.lower().strip()
    # Try exact then prefix match.
    entry = _PRICING_USD_PER_M.get(key)
    if entry is None:
        for prefix, prices in _PRICING_USD_PER_M.items():
            if key.startswith(prefix):
                entry = prices
                break
    if entry is None:
        return 0.0
    in_price, out_price = entry
    return (
        prompt_tokens     * in_price  / 1_000_000.0
        + completion_tokens * out_price / 1_000_000.0
    )


def _ensure_table(conn: sqlite3.Connection) -> None:
    """Create the usage table if it doesn't exist. Idempotent.
    Schema chosen so the rollup view (per-user / per-day) is a
    single GROUP BY without joins."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS nexus_llm_usage (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id         TEXT NOT NULL,
            ts              TEXT NOT NULL,        -- ISO date UTC, e.g. "2026-05-23"
            provider        TEXT NOT NULL,        -- "gemini" | "openai" | "anthropic"
            model           TEXT NOT NULL,
            prompt_tokens   INTEGER NOT NULL DEFAULT 0,
            completion_tokens INTEGER NOT NULL DEFAULT 0,
            cost_usd        REAL NOT NULL DEFAULT 0.0,
            created_at      TEXT NOT NULL         -- full ISO timestamp
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS nexus_llm_usage_user_day "
        "ON nexus_llm_usage(user_id, ts)"
    )


def record_usage(
    user_id: str,
    provider: str,
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
) -> None:
    """Append one usage row. Best-effort — caller path never aborts
    on a logging failure."""
    if not user_id:
        return
    try:
        from nexus_server.database import get_db_connection
        cost = estimate_cost_usd(provider, model, prompt_tokens, completion_tokens)
        now = datetime.now(timezone.utc)
        with get_db_connection() as conn:
            _ensure_table(conn)
            conn.execute(
                """
                INSERT INTO nexus_llm_usage
                    (user_id, ts, provider, model,
                     prompt_tokens, completion_tokens, cost_usd, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    user_id,
                    now.date().isoformat(),
                    provider.lower(),
                    model,
                    int(prompt_tokens),
                    int(completion_tokens),
                    float(cost),
                    now.isoformat(),
                ),
            )
            conn.commit()
    except Exception as e:  # noqa: BLE001
        logger.debug("record_usage failed for %s: %s", user_id, e)


def usage_summary(user_id: str, days: int = 7) -> dict:
    """Return rollups for the meter UI:

        {
          "total":        {"prompt_tokens": N, "completion_tokens": N,
                           "cost_usd": $, "call_count": N},
          "today":        {...},
          "by_day":       [{"date": "...", "prompt_tokens": ..., ...}],
          "by_model":     [{"model": "...", "calls": N, "cost_usd": $}],
        }
    """
    if days <= 0 or days > 90:
        days = 7
    try:
        from nexus_server.database import get_db_connection
        cutoff = (date.today() - timedelta(days=days - 1)).isoformat()
        today_iso = date.today().isoformat()
        with get_db_connection() as conn:
            _ensure_table(conn)
            conn.row_factory = sqlite3.Row
            total = conn.execute(
                """
                SELECT
                    COALESCE(SUM(prompt_tokens), 0)     AS prompt_tokens,
                    COALESCE(SUM(completion_tokens), 0) AS completion_tokens,
                    COALESCE(SUM(cost_usd), 0.0)        AS cost_usd,
                    COUNT(*)                            AS call_count
                FROM nexus_llm_usage
                WHERE user_id = ? AND ts >= ?
                """,
                (user_id, cutoff),
            ).fetchone()
            today = conn.execute(
                """
                SELECT
                    COALESCE(SUM(prompt_tokens), 0)     AS prompt_tokens,
                    COALESCE(SUM(completion_tokens), 0) AS completion_tokens,
                    COALESCE(SUM(cost_usd), 0.0)        AS cost_usd,
                    COUNT(*)                            AS call_count
                FROM nexus_llm_usage
                WHERE user_id = ? AND ts = ?
                """,
                (user_id, today_iso),
            ).fetchone()
            by_day_rows = conn.execute(
                """
                SELECT ts AS day,
                       SUM(prompt_tokens)     AS prompt_tokens,
                       SUM(completion_tokens) AS completion_tokens,
                       SUM(cost_usd)          AS cost_usd
                FROM nexus_llm_usage
                WHERE user_id = ? AND ts >= ?
                GROUP BY ts
                ORDER BY ts ASC
                """,
                (user_id, cutoff),
            ).fetchall()
            by_model_rows = conn.execute(
                """
                SELECT model,
                       COUNT(*)                        AS calls,
                       SUM(prompt_tokens)              AS prompt_tokens,
                       SUM(completion_tokens)          AS completion_tokens,
                       SUM(cost_usd)                   AS cost_usd
                FROM nexus_llm_usage
                WHERE user_id = ? AND ts >= ?
                GROUP BY model
                ORDER BY cost_usd DESC
                """,
                (user_id, cutoff),
            ).fetchall()
        return {
            "total": dict(total) if total else {},
            "today": dict(today) if today else {},
            "by_day":   [dict(r) for r in by_day_rows],
            "by_model": [dict(r) for r in by_model_rows],
            "window_days": days,
        }
    except Exception as e:  # noqa: BLE001
        logger.warning("usage_summary failed for %s: %s", user_id, e)
        return {
            "total": {}, "today": {}, "by_day": [], "by_model": [],
            "window_days": days,
            "error": str(e),
        }


# ── Provider-response extractors ───────────────────────────────────


def extract_gemini_usage(response) -> tuple[int, int]:
    """Gemini's google-genai response carries usage_metadata with
    prompt_token_count + candidates_token_count. Returns
    ``(prompt, completion)`` — zeros if absent."""
    try:
        meta = getattr(response, "usage_metadata", None)
        if meta is None:
            return 0, 0
        return (
            int(getattr(meta, "prompt_token_count", 0) or 0),
            int(getattr(meta, "candidates_token_count", 0) or 0),
        )
    except Exception:
        return 0, 0


def extract_openai_usage(response) -> tuple[int, int]:
    """OpenAI response.usage has prompt_tokens + completion_tokens."""
    try:
        usage = getattr(response, "usage", None)
        if usage is None:
            return 0, 0
        return (
            int(getattr(usage, "prompt_tokens", 0) or 0),
            int(getattr(usage, "completion_tokens", 0) or 0),
        )
    except Exception:
        return 0, 0


def extract_anthropic_usage(response) -> tuple[int, int]:
    """Anthropic response.usage has input_tokens + output_tokens."""
    try:
        usage = getattr(response, "usage", None)
        if usage is None:
            return 0, 0
        return (
            int(getattr(usage, "input_tokens", 0) or 0),
            int(getattr(usage, "output_tokens", 0) or 0),
        )
    except Exception:
        return 0, 0
