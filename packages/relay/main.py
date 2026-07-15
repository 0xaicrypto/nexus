"""Nexus Email Relay — #116.

Tiny FastAPI service. Sits between Nexus desktop clients and the
shared-support-email Gmail account. Holds the real SMTP password
(only in the hosting environment, never shipped to clients) and
enforces hard limits:

  * API-key auth (one secret, bundled with the Nexus .dmg)
  * Per-user rate limit (configurable, default 10/day)
  * Recipient allow-list (env or per-user)
  * Audit log of every send (SQLite, queryable via /audit endpoint)

Why this exists
===============
Bundling SMTP passwords in client distributables is the cardinal
mistake of "support email" features. A relay lets you:
  - rotate creds without rebuilding client (fly secrets set ...)
  - revoke individual users without re-issuing keys to everyone
  - audit every send (catches abuse immediately)
  - enforce policy that the SMTP server itself can't (rate, content)

Run locally
===========
    uvicorn main:app --port 8443

Set env vars first — see .env.example.

Deploy to Fly
=============
    flyctl launch          # one-time, creates fly.toml
    flyctl secrets set GMAIL_APP_PASSWORD=... RELAY_API_KEY=...
    flyctl deploy
"""
from __future__ import annotations

import email.message
import logging
import os
import smtplib
import sqlite3
import ssl
from contextlib import asynccontextmanager
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Header, HTTPException, Request, status
from pydantic import BaseModel, EmailStr, Field

logger = logging.getLogger("nexus.relay")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


# ─────────────────────────────────────────────────────────────────────
# Config (env-driven, never hardcoded)
# ─────────────────────────────────────────────────────────────────────


def _env(name: str, default: str = "") -> str:
    v = os.environ.get(name, default)
    return v.strip()


def _env_int(name: str, default: int) -> int:
    try:
        return int(_env(name, "") or default)
    except ValueError:
        return default


RELAY_API_KEY        = _env("RELAY_API_KEY")
SMTP_HOST            = _env("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT            = _env_int("SMTP_PORT", 587)
SMTP_USER            = _env("SMTP_USER")
SMTP_PASSWORD        = _env("SMTP_PASSWORD")
SMTP_FROM            = _env("SMTP_FROM") or SMTP_USER
DAILY_LIMIT_PER_USER = _env_int("DAILY_LIMIT_PER_USER", 10)
ALLOWED_RECIPIENTS   = [
    r.strip().lower()
    for r in _env("ALLOWED_RECIPIENTS", "").split(",")
    if r.strip()
]
# Restrict to specific @domains (e.g. "hospital.org,clinic.com").
# Empty = no domain restriction.
ALLOWED_DOMAINS      = [
    d.strip().lower().lstrip("@")
    for d in _env("ALLOWED_DOMAINS", "").split(",")
    if d.strip()
]
AUDIT_DB_PATH        = _env("AUDIT_DB_PATH", "/data/audit.db")
DEBUG_MODE           = _env("DEBUG_MODE", "0") == "1"


# ─────────────────────────────────────────────────────────────────────
# Audit DB (SQLite — Fly.io volume mount, falls back to local file)
# ─────────────────────────────────────────────────────────────────────


def _ensure_db() -> None:
    """Create the audit + rate-limit tables. Idempotent. Tolerates
    missing parent directory by falling back to /tmp — handy for
    `uvicorn main:app` local dev without a Fly volume."""
    global AUDIT_DB_PATH
    p = Path(AUDIT_DB_PATH)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
    except (PermissionError, OSError):
        logger.warning("Cannot write to %s; falling back to /tmp/audit.db", p)
        AUDIT_DB_PATH = "/tmp/audit.db"
        p = Path(AUDIT_DB_PATH)

    conn = sqlite3.connect(str(p))
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS sends (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                ts            TEXT NOT NULL,
                user_id       TEXT NOT NULL,
                recipient_to  TEXT NOT NULL,
                recipient_cc  TEXT NOT NULL DEFAULT '',
                subject       TEXT NOT NULL,
                body_bytes    INTEGER NOT NULL,
                status        TEXT NOT NULL,            -- ok | rate_limited | blocked | smtp_error
                client_ip     TEXT NOT NULL DEFAULT '',
                detail        TEXT NOT NULL DEFAULT ''
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS sends_user_day ON sends(user_id, ts)"
        )
        conn.commit()
    finally:
        conn.close()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    _ensure_db()
    logger.info(
        "Nexus Email Relay booted. user=%s, daily_limit=%d, "
        "allowed_recipients=%d, allowed_domains=%d, audit_db=%s, debug=%s",
        SMTP_USER or "(unset)",
        DAILY_LIMIT_PER_USER,
        len(ALLOWED_RECIPIENTS),
        len(ALLOWED_DOMAINS),
        AUDIT_DB_PATH,
        DEBUG_MODE,
    )
    if not RELAY_API_KEY:
        logger.warning(
            "RELAY_API_KEY not set — every request will 401. "
            "Set it via `fly secrets set RELAY_API_KEY=...`",
        )
    if not (SMTP_USER and SMTP_PASSWORD):
        logger.warning(
            "SMTP_USER / SMTP_PASSWORD not set — sends will fail with "
            "5xx. Set them via `fly secrets set SMTP_USER=... SMTP_PASSWORD=...`",
        )
    yield


app = FastAPI(
    title="Nexus Email Relay",
    version="0.1.0",
    lifespan=lifespan,
)


# ─────────────────────────────────────────────────────────────────────
# Models
# ─────────────────────────────────────────────────────────────────────


class SendEmailRequest(BaseModel):
    """Body for POST /api/send-email.

    ``nexus_user_id`` is the calling Nexus instance's user_id (the
    Nexus desktop client extracts it from its local JWT and passes
    it along). Used for per-user rate limiting + audit attribution.
    We do NOT cryptographically verify it — the API key already gates
    access; nexus_user_id is informational. If you want stronger
    binding, sign the JWT on the Nexus server side using a shared
    key and have the relay verify it.
    """
    nexus_user_id: str = Field(..., min_length=1, max_length=128)
    to:        str = Field(..., min_length=3, max_length=512)
    subject:   str = Field(..., min_length=1, max_length=512)
    body:      str = Field(..., min_length=1, max_length=200_000)
    cc:        Optional[str] = Field(default=None, max_length=512)


class SendEmailResponse(BaseModel):
    status: str                  # "sent"
    sent_to: list[str]
    daily_quota_remaining: int


# ─────────────────────────────────────────────────────────────────────
# Auth + rate limit
# ─────────────────────────────────────────────────────────────────────


def _require_api_key(provided: Optional[str]) -> None:
    if not RELAY_API_KEY:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Relay is not configured (RELAY_API_KEY unset). "
                   "Operator: `fly secrets set RELAY_API_KEY=...`",
        )
    if not provided:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing X-Nexus-Relay-Key header.",
        )
    # constant-time compare to avoid leaking key prefix via timing
    import hmac
    if not hmac.compare_digest(provided, RELAY_API_KEY):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid relay API key.",
        )


def _today_iso() -> str:
    return date.today().isoformat()


def _count_today(user_id: str) -> int:
    conn = sqlite3.connect(AUDIT_DB_PATH)
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM sends "
            "WHERE user_id = ? AND ts LIKE ? AND status = 'ok'",
            (user_id, f"{_today_iso()}%"),
        ).fetchone()
        return int(row[0]) if row else 0
    finally:
        conn.close()


def _log_send(
    user_id: str, to_list: list[str], cc_list: list[str],
    subject: str, body_bytes: int, status_str: str,
    client_ip: str, detail: str = "",
) -> None:
    """Append one row to the audit table. Best-effort — never raises."""
    try:
        conn = sqlite3.connect(AUDIT_DB_PATH)
        try:
            conn.execute(
                "INSERT INTO sends "
                "(ts, user_id, recipient_to, recipient_cc, subject, "
                " body_bytes, status, client_ip, detail) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    datetime.now(timezone.utc).isoformat(),
                    user_id,
                    ", ".join(to_list),
                    ", ".join(cc_list),
                    subject[:512],
                    body_bytes,
                    status_str,
                    client_ip,
                    detail[:512],
                ),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception as e:  # noqa: BLE001
        logger.warning("audit log failed: %s", e)


# ─────────────────────────────────────────────────────────────────────
# Recipient policy
# ─────────────────────────────────────────────────────────────────────


def _check_recipients(addrs: list[str]) -> Optional[str]:
    """Return None when all addresses pass policy, else an error string."""
    bad: list[str] = []
    for a in addrs:
        a_lc = a.lower()
        if "@" not in a_lc:
            bad.append(a)
            continue
        if ALLOWED_RECIPIENTS and a_lc not in ALLOWED_RECIPIENTS:
            # If both lists are set, must match either.
            domain = a_lc.split("@", 1)[1]
            if not ALLOWED_DOMAINS or domain not in ALLOWED_DOMAINS:
                bad.append(a)
            continue
        if ALLOWED_DOMAINS and not ALLOWED_RECIPIENTS:
            domain = a_lc.split("@", 1)[1]
            if domain not in ALLOWED_DOMAINS:
                bad.append(a)
    if bad:
        return (
            f"Recipient(s) blocked by relay policy: {', '.join(bad)}. "
            "Operator: extend ALLOWED_RECIPIENTS or ALLOWED_DOMAINS "
            "via fly secrets to permit additional addresses."
        )
    return None


# ─────────────────────────────────────────────────────────────────────
# SMTP send
# ─────────────────────────────────────────────────────────────────────


def _send_via_smtp(
    to_list: list[str], cc_list: list[str],
    subject: str, body: str,
) -> dict:
    """Send and return a diagnostic blob.

    Returns ``{"ehlo_response", "auth_response", "send_message_refused"}``
    so the caller can stash it in the audit log. ``smtplib.send_message``
    returning an empty dict means the SMTP server accepted ALL
    recipients — but acceptance is NOT delivery. Gmail in particular
    often returns 250-Accepted and then silently quarantines the
    message (rate-limit, "suspicious activity", reputation downgrade).
    The diagnostic blob is the most we can capture at the relay tier;
    real delivery confirmation requires postmaster bounce processing
    on the operator side.

    Throws on outright failure. Caller's try/except converts to HTTP
    error.
    """
    msg = email.message.EmailMessage()
    msg["From"]    = SMTP_FROM
    msg["To"]      = ", ".join(to_list)
    if cc_list:
        msg["Cc"]  = ", ".join(cc_list)
    msg["Subject"] = subject
    msg.set_content(body)

    diag: dict = {"ehlo_response": "", "auth_response": "", "send_message_refused": {}}
    ctx = ssl.create_default_context()
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=20) as s:
        ehlo_code, ehlo_msg = s.ehlo()
        s.starttls(context=ctx)
        ehlo_code2, ehlo_msg2 = s.ehlo()
        diag["ehlo_response"] = f"{ehlo_code} → {ehlo_code2}"
        # Login returns a (code, msg) tuple on success; we log it so
        # operators can see Gmail's exact auth-accept message.
        try:
            auth_code, auth_msg = s.login(SMTP_USER, SMTP_PASSWORD)
            diag["auth_response"] = f"{auth_code} {auth_msg!r}"
        except smtplib.SMTPAuthenticationError:
            raise
        refused = s.send_message(msg, to_addrs=to_list + cc_list)
        diag["send_message_refused"] = refused or {}
        if refused:
            raise smtplib.SMTPRecipientsRefused(refused)
        # Final NOOP — gives Gmail one last chance to surface a transient
        # issue (rare but seen for accounts being throttled).
        try:
            noop_code, noop_msg = s.noop()
            diag["noop_response"] = f"{noop_code} {noop_msg!r}"
        except smtplib.SMTPException as exc:
            diag["noop_response"] = f"noop_error: {exc}"
    return diag


def _smtp_preflight() -> dict:
    """Lightweight verification that SMTP login currently works.

    Connects + STARTTLS + LOGIN + NOOP, then closes — does NOT send a
    real message. Used by the /preflight endpoint so operators (and
    the desktop's debug pane in a later phase) can verify the relay
    is healthy without burning quota or polluting the audit log.
    """
    out: dict = {"smtp_configured": bool(SMTP_USER and SMTP_PASSWORD)}
    if not (SMTP_USER and SMTP_PASSWORD):
        out["ok"] = False
        out["error"] = "SMTP_USER / SMTP_PASSWORD not configured on relay"
        return out
    ctx = ssl.create_default_context()
    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=15) as s:
            s.ehlo()
            s.starttls(context=ctx)
            s.ehlo()
            code, msg = s.login(SMTP_USER, SMTP_PASSWORD)
            out["login"] = f"{code} {msg!r}"
            code, msg = s.noop()
            out["noop"]  = f"{code} {msg!r}"
        out["ok"] = True
    except smtplib.SMTPAuthenticationError as exc:
        out["ok"] = False
        out["error"] = (
            f"SMTP_AUTH_FAILED — Gmail rejected the App Password "
            f"(rotate via Google Account → Security → App passwords): {exc}"
        )
    except smtplib.SMTPException as exc:
        out["ok"] = False
        out["error"] = f"SMTP_EXCEPTION: {exc}"
    except OSError as exc:
        out["ok"] = False
        out["error"] = f"network: {exc}"
    return out


# ─────────────────────────────────────────────────────────────────────
# Endpoints
# ─────────────────────────────────────────────────────────────────────


@app.get("/healthz")
async def healthz() -> dict:
    """Liveness probe. Does NOT confirm SMTP creds are correct — that
    only happens on actual send. Use ``/preflight`` for that."""
    return {
        "status":               "ok",
        "smtp_configured":      bool(SMTP_USER and SMTP_PASSWORD),
        "auth_configured":      bool(RELAY_API_KEY),
        "daily_limit_per_user": DAILY_LIMIT_PER_USER,
        "allowed_domains":      ALLOWED_DOMAINS,
        "allowed_recipients_count": len(ALLOWED_RECIPIENTS),
    }


@app.post("/api/send-email", response_model=SendEmailResponse)
async def send_email_endpoint(
    req: SendEmailRequest,
    request: Request,
    x_nexus_relay_key: Optional[str] = Header(default=None),
) -> SendEmailResponse:
    _require_api_key(x_nexus_relay_key)

    if not (SMTP_USER and SMTP_PASSWORD):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Relay's upstream SMTP not configured. Contact operator.",
        )

    to_list = [a.strip() for a in req.to.split(",") if a.strip()]
    cc_list = [a.strip() for a in (req.cc or "").split(",") if a.strip()]
    all_addrs = to_list + cc_list
    if not to_list:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="`to` did not contain any valid addresses.",
        )

    client_ip = request.client.host if request.client else ""

    # Policy gate
    policy_err = _check_recipients(all_addrs)
    if policy_err:
        _log_send(
            req.nexus_user_id, to_list, cc_list,
            req.subject, len(req.body), "blocked",
            client_ip, policy_err,
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail=policy_err,
        )

    # Rate limit
    sent_today = _count_today(req.nexus_user_id)
    if sent_today >= DAILY_LIMIT_PER_USER:
        _log_send(
            req.nexus_user_id, to_list, cc_list,
            req.subject, len(req.body), "rate_limited",
            client_ip, f"{sent_today}/{DAILY_LIMIT_PER_USER}",
        )
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=(
                f"Daily send limit reached for this user "
                f"({sent_today}/{DAILY_LIMIT_PER_USER}). "
                f"Resets at UTC midnight."
            ),
        )

    # Actually send
    try:
        send_diag = _send_via_smtp(to_list, cc_list, req.subject, req.body)
    except smtplib.SMTPAuthenticationError as e:
        _log_send(
            req.nexus_user_id, to_list, cc_list,
            req.subject, len(req.body), "smtp_error",
            client_ip, f"auth: {e}",
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Upstream SMTP authentication failed. Operator: "
                   "check / rotate App Password.",
        )
    except smtplib.SMTPException as e:
        _log_send(
            req.nexus_user_id, to_list, cc_list,
            req.subject, len(req.body), "smtp_error",
            client_ip, str(e),
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"SMTP error: {e}",
        )
    except OSError as e:
        # Network-level failures (DNS, connection refused, TLS) are
        # OSError subclasses — they don't inherit from SMTPException.
        # Treat them as an upstream-unavailable condition.
        _log_send(
            req.nexus_user_id, to_list, cc_list,
            req.subject, len(req.body), "smtp_error",
            client_ip, f"network: {e}",
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Network error reaching SMTP: {e}",
        )

    diag_summary = (
        f"auth={send_diag.get('auth_response','')} "
        f"refused={send_diag.get('send_message_refused', {})} "
        f"noop={send_diag.get('noop_response','')}"
    )
    _log_send(
        req.nexus_user_id, to_list, cc_list,
        req.subject, len(req.body), "ok",
        client_ip, diag_summary,
    )
    logger.info(
        "send ok: user=%s to=%s subject=%r body=%dB ip=%s diag=%s",
        req.nexus_user_id, to_list, req.subject[:80], len(req.body),
        client_ip, diag_summary,
    )
    return SendEmailResponse(
        status="sent",
        sent_to=to_list + cc_list,
        daily_quota_remaining=max(0, DAILY_LIMIT_PER_USER - (sent_today + 1)),
    )


@app.get("/preflight")
async def preflight_endpoint(
    x_nexus_relay_key: Optional[str] = Header(default=None),
) -> dict:
    """Verify the relay can authenticate to its upstream SMTP server
    WITHOUT sending a real message. Useful when ``send-email`` keeps
    returning 200 but recipients aren't getting anything — preflight
    tells you whether the relay→Gmail link itself is healthy.

    Returns ``{"ok": bool, "error": str|null, "login": "<smtp resp>",
    "noop": "<smtp resp>"}``. Quota is NOT consumed.
    """
    _require_api_key(x_nexus_relay_key)
    return _smtp_preflight()


@app.get("/audit")
async def audit_endpoint(
    days: int = 7,
    x_nexus_relay_key: Optional[str] = Header(default=None),
) -> dict:
    """Operator-only view. Returns recent send rows for auditing."""
    _require_api_key(x_nexus_relay_key)
    days = max(1, min(days, 90))
    cutoff = datetime.now(timezone.utc).date().toordinal() - (days - 1)
    cutoff_iso = date.fromordinal(cutoff).isoformat()
    conn = sqlite3.connect(AUDIT_DB_PATH)
    try:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM sends WHERE ts >= ? ORDER BY id DESC LIMIT 500",
            (cutoff_iso,),
        ).fetchall()
    finally:
        conn.close()
    return {"count": len(rows), "rows": [dict(r) for r in rows]}
