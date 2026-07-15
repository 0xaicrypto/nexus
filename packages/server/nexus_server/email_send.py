"""Email send transport for v2.

Ported from v1's ``tools_calendar.py`` (which combined SMTP transport,
recipient gating, and LLM tool-call wrappers in one ~850-line module).
v2 splits the transport off into this dedicated module — it's needed by
the new REST router (``email_router.py``) which the desktop hits when
the medic clicks "Send" in the Compose dialog. The LLM tool-call layer
is intentionally NOT ported: the v2 chat path (``chat_router`` +
``retrieval_tiers``) is SSE streaming over a single Gemini call and
doesn't expose function-calling. If/when we add tool-calling back we
can layer the LLM tool on top of these primitives without duplicating
the SMTP/relay code.

Transport precedence (matches v1 §"#116" comment):

    1. Hosted relay      — when NEXUS_RELAY_URL + NEXUS_RELAY_API_KEY
                           are both set. Sends go to the Fly.io relay
                           (``packages/relay/main.py``) which enforces
                           recipient allow-list + daily rate limit +
                           audit log centrally. This is the prod path:
                           the .dmg ships the URL + key baked in.

    2. Direct SMTP       — when NEXUS_SMTP_HOST + NEXUS_SMTP_USER +
                           NEXUS_SMTP_PASSWORD are all set. Dev / power
                           user only — the password sits on the
                           medic's machine. No central rate limit.

    3. None configured   — caller gets an "EMAIL_NOT_CONFIGURED"
                           error. Refuses silently-failing fallback.

The bundled-credentials guard (#115 in v1) is preserved: if
``NEXUS_SMTP_BUNDLED_CREDS=1`` (set by build-macos.sh when the .dmg
ships a shared SMTP password), unbounded sends are refused. Operator
must specify ``NEXUS_SMTP_ALLOWED_RECIPIENTS`` as a comma-list of
addresses we're allowed to send to.

The env is re-read on every call (``_live_smtp_config()`` does
``os.environ.get`` fresh) so the medic can drop new creds into
``$RUNE_HOME/.env`` and the next /api/v1/email/send picks them up
without a sidecar restart — same pattern Quick scan uses for
GEMINI_API_KEY (see ``quick_scan._live_gemini_api_key``).
"""
from __future__ import annotations

import asyncio
import email.message
import logging
import os
import smtplib
import ssl
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────
# Result shapes — small dataclasses instead of tuples so callers don't
# get the field order wrong (ok, message, status_code is easy to mis-
# remember). The router serialises these to JSON.
# ─────────────────────────────────────────────────────────────────────


@dataclass
class SendResult:
    ok: bool
    transport: str  # 'relay' | 'smtp' | 'none'
    message: str
    status_code: int = 0  # HTTP-ish; 0 means "no HTTP involved" (SMTP path)
    sent_to: list[str] = field(default_factory=list)


@dataclass
class TransportStatus:
    """Snapshot of what the server CAN do right now. Returned by the
    /transport endpoint so the desktop's Compose dialog can pre-flight
    the configuration without attempting a real send."""
    relay_configured: bool
    smtp_configured:  bool
    bundled_creds:    bool
    default_from:     str  # "" when nothing configured
    allowed_recipients: list[str]
    relay_url_host:   str  # public so the UI can show "via relay.nexus.io"
                           # without leaking the full URL/key

    def to_dict(self) -> dict:
        return {
            "relay_configured":   self.relay_configured,
            "smtp_configured":    self.smtp_configured,
            "bundled_creds":      self.bundled_creds,
            "default_from":       self.default_from,
            "allowed_recipients": self.allowed_recipients,
            "relay_url_host":     self.relay_url_host,
            "configured": self.relay_configured or self.smtp_configured,
        }


# ─────────────────────────────────────────────────────────────────────
# Config probes (read env fresh on every call so .env edits hot-load)
# ─────────────────────────────────────────────────────────────────────


def _live_relay() -> Optional[tuple[str, str]]:
    """Return (url, api_key) or None when relay isn't configured.

    Validates the .env-template sentinel REPLACE_WITH_ — when the
    bundled template ships placeholders, we treat them as unconfigured
    rather than POSTing garbage to nothing.
    """
    url = os.environ.get("NEXUS_RELAY_URL", "").strip()
    key = os.environ.get("NEXUS_RELAY_API_KEY", "").strip()
    if not (url and key):
        return None
    if "REPLACE_WITH_" in url or "REPLACE_WITH_" in key:
        return None
    return (url, key)


def _live_smtp_config() -> Optional[dict]:
    """Same shape as v1's ``_smtp_config``. Returns None when the
    required trio host/user/password isn't fully populated."""
    host     = os.environ.get("NEXUS_SMTP_HOST", "").strip()
    user     = os.environ.get("NEXUS_SMTP_USER", "").strip()
    password = os.environ.get("NEXUS_SMTP_PASSWORD", "").strip()
    if not (host and user and password):
        return None
    if "REPLACE_WITH_" in user or "REPLACE_WITH_" in password:
        return None
    try:
        port = int(os.environ.get("NEXUS_SMTP_PORT", "587"))
    except ValueError:
        port = 587
    allowed_raw = os.environ.get("NEXUS_SMTP_ALLOWED_RECIPIENTS", "")
    allowed = [r.strip().lower() for r in allowed_raw.split(",") if r.strip()]
    return {
        "host":     host,
        "port":     port,
        "user":     user,
        "password": password,
        "from":     (os.environ.get("NEXUS_SMTP_FROM", "").strip() or user),
        "allowed":  allowed,
        "bundled":  os.environ.get("NEXUS_SMTP_BUNDLED_CREDS", "").strip() == "1",
    }


def transport_status() -> TransportStatus:
    """Return what's configured. UI consumes via GET /api/v1/email/transport."""
    relay = _live_relay()
    smtp  = _live_smtp_config()

    relay_url_host = ""
    if relay:
        from urllib.parse import urlparse
        try:
            relay_url_host = urlparse(relay[0]).hostname or ""
        except Exception:  # noqa: BLE001
            relay_url_host = ""

    default_from = ""
    if smtp:
        default_from = smtp["from"]
    elif relay:
        # Relay path uses the relay's own FROM (operator-side). Surface
        # the host so UI shows "via {host}" — the actual envelope FROM
        # lives in relay env, not exposed here.
        default_from = f"(relay · {relay_url_host or '?'})"

    return TransportStatus(
        relay_configured = relay is not None,
        smtp_configured  = smtp  is not None,
        bundled_creds    = bool(smtp and smtp.get("bundled")),
        default_from     = default_from,
        allowed_recipients = list(smtp["allowed"]) if smtp else [],
        relay_url_host   = relay_url_host,
    )


# ─────────────────────────────────────────────────────────────────────
# Recipient gating
# ─────────────────────────────────────────────────────────────────────


def _validate_recipients(
    addrs: list[str], allowed: list[str],
) -> Optional[str]:
    """Empty allow-list = no restriction. With an allow-list, every
    recipient must be on it. Returns error string or None."""
    if not allowed:
        return None
    bad = [a for a in addrs if a.lower() not in allowed]
    if bad:
        return (
            f"Recipient(s) not in NEXUS_SMTP_ALLOWED_RECIPIENTS "
            f"allow-list: {', '.join(bad)}. Currently permitted: "
            f"{', '.join(allowed)}."
        )
    return None


# ─────────────────────────────────────────────────────────────────────
# SMTP (sync — blocking)
# ─────────────────────────────────────────────────────────────────────


def _send_smtp_sync(
    cfg: dict, to: list[str], cc: list[str],
    subject: str, body: str,
) -> SendResult:
    """STARTTLS submission on the configured port. Caller wraps in
    run_in_executor.

    Error messages preserve v1's actionable phrasing — when a Gmail
    user pastes their account password instead of an App Password, the
    error tells them where to generate the App Password."""
    msg = email.message.EmailMessage()
    msg["From"]    = cfg["from"]
    msg["To"]      = ", ".join(to)
    if cc:
        msg["Cc"]  = ", ".join(cc)
    msg["Subject"] = subject
    msg.set_content(body or "")

    try:
        ctx = ssl.create_default_context()
        with smtplib.SMTP(cfg["host"], cfg["port"], timeout=20) as s:
            s.ehlo()
            s.starttls(context=ctx)
            s.ehlo()
            s.login(cfg["user"], cfg["password"])
            recipients = to + cc
            refused = s.send_message(msg, to_addrs=recipients)
            if refused:
                return SendResult(
                    ok=False, transport="smtp",
                    message=f"Some recipients refused: {refused}",
                )
        return SendResult(
            ok=True, transport="smtp",
            message=f"Sent to {', '.join(to + cc)} from {cfg['from']}.",
            sent_to=to + cc,
        )
    except smtplib.SMTPAuthenticationError as e:
        return SendResult(
            ok=False, transport="smtp",
            message=(
                f"SMTP authentication failed: {e}. If using Gmail, "
                "ensure NEXUS_SMTP_PASSWORD is a 16-char App Password "
                "(NOT your regular account password). Generate one at "
                "https://myaccount.google.com/apppasswords."
            ),
        )
    except smtplib.SMTPRecipientsRefused as e:
        return SendResult(
            ok=False, transport="smtp",
            message=f"All recipients refused: {e.recipients}",
        )
    except smtplib.SMTPException as e:
        return SendResult(
            ok=False, transport="smtp", message=f"SMTP error: {e}",
        )
    except OSError as e:
        return SendResult(
            ok=False, transport="smtp",
            message=f"Network error reaching {cfg['host']}: {e}",
        )


# ─────────────────────────────────────────────────────────────────────
# Relay (async — httpx)
# ─────────────────────────────────────────────────────────────────────


async def _post_to_relay(
    url: str, api_key: str, payload: dict,
) -> SendResult:
    """POST one send request to the Fly.io relay. The relay's JSON
    error body bubbles up verbatim so users see the real reason
    (rate limited / blocked / smtp_error / etc.)."""
    import httpx
    endpoint = url.rstrip("/") + "/api/send-email"
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                endpoint,
                headers={"X-Nexus-Relay-Key": api_key},
                json=payload,
            )
    except httpx.ConnectError as e:
        return SendResult(
            ok=False, transport="relay",
            message=f"Cannot reach relay at {endpoint}: {e}",
        )
    except httpx.TimeoutException as e:
        return SendResult(
            ok=False, transport="relay",
            message=f"Relay timed out after 30s: {e}",
        )
    except Exception as e:  # noqa: BLE001
        return SendResult(
            ok=False, transport="relay",
            message=f"Relay call failed: {e}",
        )

    if resp.status_code == 200:
        try:
            data = resp.json()
        except Exception:
            return SendResult(
                ok=True, transport="relay", status_code=200,
                message="Sent via relay (response unparseable but 200).",
            )
        sent_to = data.get("sent_to") or []
        quota   = data.get("daily_quota_remaining", "?")
        # Be honest with the medic about what "sent" means here:
        # relay's SMTP server returned 250-Accepted — but acceptance ≠
        # delivery. Gmail in particular can accept then silently
        # quarantine messages from accounts it's rate-limiting or
        # flagging. Telling the medic "Sent" full stop sets them up
        # to be confused when zhao@gmail.com calls saying "nothing
        # arrived". Surface the 1-5 min delivery window + spam-check
        # hint up front.
        return SendResult(
            ok=True, transport="relay", status_code=200,
            message=(
                f"已交付 relay (To: {', '.join(sent_to)}). "
                f"今日剩余配额: {quota}. "
                f"投递通常在 1-5 分钟到达;若 10 分钟仍未收到,"
                f"请收件人检查垃圾邮件夹,或联系运维查 relay 日志。"
            ),
            sent_to=sent_to,
        )

    # Non-200: surface structured detail.
    try:
        data = resp.json()
        detail = data.get("detail") or str(data)
    except Exception:
        detail = resp.text[:300] or f"HTTP {resp.status_code}"
    return SendResult(
        ok=False, transport="relay", status_code=resp.status_code,
        message=f"Relay rejected send (HTTP {resp.status_code}): {detail}",
    )


# ─────────────────────────────────────────────────────────────────────
# High-level orchestrator — single entry point for the router
# ─────────────────────────────────────────────────────────────────────


def _parse_addr_list(raw: str | list[str] | None) -> list[str]:
    """Normalise the To/Cc input into a list of trimmed addresses.
    Accepts either a comma-separated string or a list (the router
    surface uses list[str], but the LLM-tool path used to pass strings,
    keeping both for flexibility)."""
    if raw is None:
        return []
    if isinstance(raw, list):
        items = raw
    else:
        items = raw.split(",")
    return [a.strip() for a in items if isinstance(a, str) and a.strip()]


# Very loose RFC-5322 sanity check. We're not trying to validate every
# valid form (Display Name <addr@x.com>, IDN, etc.) — just block obvious
# junk before paying the SMTP roundtrip cost. The transport layer will
# reject anything that slips through.
import re as _re

_ADDR_RE = _re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


def _looks_like_email(addr: str) -> bool:
    return bool(_ADDR_RE.match(addr))


async def send_email_async(
    *,
    user_id: str,
    to: str | list[str],
    subject: str,
    body: str,
    cc: str | list[str] | None = None,
) -> SendResult:
    """Send an email through the best available transport.

    Caller is responsible for auth — the router does this before
    invoking us. ``user_id`` is passed through to the relay payload
    (the relay's audit log keys on it) and surfaced in our own log
    line so we can attribute SMTP-path sends to a medic when
    reviewing nexus_server.log.
    """
    to_list = _parse_addr_list(to)
    cc_list = _parse_addr_list(cc)

    # Pre-flight: at least one TO, all addresses look like email-ish.
    if not to_list:
        return SendResult(
            ok=False, transport="none",
            message="`to` is required and must contain at least one address.",
        )
    if not subject or not subject.strip():
        return SendResult(
            ok=False, transport="none", message="`subject` is required.",
        )
    if not body or not body.strip():
        return SendResult(
            ok=False, transport="none", message="`body` is required.",
        )
    bad = [a for a in to_list + cc_list if not _looks_like_email(a)]
    if bad:
        return SendResult(
            ok=False, transport="none",
            message=f"Not a valid email address: {', '.join(bad)}",
        )

    # Preference: relay > smtp > error.
    relay = _live_relay()
    if relay is not None:
        url, key = relay
        payload: dict = {
            "nexus_user_id": user_id,
            "to":      ",".join(to_list),
            "subject": subject,
            "body":    body,
        }
        if cc_list:
            payload["cc"] = ",".join(cc_list)
        result = await _post_to_relay(url, key, payload)
        if result.ok:
            logger.info(
                "email send via relay: user=%s to=%s subject=%r",
                user_id, to_list, subject[:80],
            )
        else:
            logger.warning(
                "email send via relay FAILED: user=%s to=%s err=%s",
                user_id, to_list, result.message[:200],
            )
        return result

    cfg = _live_smtp_config()
    if cfg is None:
        return SendResult(
            ok=False, transport="none",
            message=(
                "Email transport not configured. Either set NEXUS_RELAY_URL + "
                "NEXUS_RELAY_API_KEY (recommended — see packages/relay/), or "
                "configure direct SMTP via NEXUS_SMTP_HOST + NEXUS_SMTP_USER + "
                "NEXUS_SMTP_PASSWORD in $RUNE_HOME/.env."
            ),
        )

    # Bundled-credentials guard (v1 #115).
    if cfg.get("bundled") and not cfg["allowed"]:
        return SendResult(
            ok=False, transport="smtp",
            message=(
                "Bundled SMTP credentials require a non-empty "
                "NEXUS_SMTP_ALLOWED_RECIPIENTS allow-list. The .dmg-shipped "
                "password could be extracted, so unbounded sends are blocked "
                "by design. Add the recipient to NEXUS_SMTP_ALLOWED_RECIPIENTS "
                "in $RUNE_HOME/.env."
            ),
        )
    gate_err = _validate_recipients(to_list + cc_list, cfg["allowed"])
    if gate_err:
        return SendResult(
            ok=False, transport="smtp", message=gate_err,
        )

    loop = asyncio.get_event_loop()
    try:
        result = await asyncio.wait_for(
            loop.run_in_executor(
                None, _send_smtp_sync,
                cfg, to_list, cc_list, subject, body,
            ),
            timeout=30.0,
        )
    except asyncio.TimeoutError:
        return SendResult(
            ok=False, transport="smtp",
            message=(
                f"SMTP send timed out after 30s ({cfg['host']}:{cfg['port']}). "
                "Network reachable?"
            ),
        )

    if result.ok:
        logger.info(
            "email send via SMTP: user=%s from=%s to=%s subject=%r",
            user_id, cfg["from"], to_list + cc_list, subject[:80],
        )
    else:
        logger.warning(
            "email send via SMTP FAILED: user=%s to=%s err=%s",
            user_id, to_list + cc_list, result.message[:200],
        )
    return result
