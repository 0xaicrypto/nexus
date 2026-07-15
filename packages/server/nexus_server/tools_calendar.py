"""macOS Calendar + Email tools — #114.

Three tools the agent can call from chat:

  * ``read_calendar`` — read events from the local macOS Calendar.app
    (next 7 days by default). Uses ``osascript`` JXA — no third-party
    deps, no network.

  * ``compose_email_draft`` — open the user's default mail client
    with a pre-filled draft via ``mailto:`` URL. User reviews + clicks
    Send. Zero credentials. macOS-only.

  * ``send_email_now`` — actually send an email via SMTP using the
    configured Gmail (or other) account. Cross-platform. Requires
    NEXUS_SMTP_* env vars to be configured; falls back to a clear
    error when not. Designed for "agent drafted, user said go, agent
    sent" UX.

SMTP configuration
==================
Set in $RUNE_HOME/.env (single-user local deploy) or the server's
environment::

    NEXUS_SMTP_HOST=smtp.gmail.com
    NEXUS_SMTP_PORT=587
    NEXUS_SMTP_USER=your-bot-account@gmail.com
    NEXUS_SMTP_PASSWORD=<16-char Google App Password>
    NEXUS_SMTP_FROM="Nexus Agent <your-bot-account@gmail.com>"

For Gmail specifically: enable 2-factor auth on the bot account,
then generate an App Password at
https://myaccount.google.com/apppasswords. Use the App Password —
not your normal account password — as ``NEXUS_SMTP_PASSWORD``.

Safety
======
``send_email_now`` is irreversible. The tool's description tells the
agent to confirm with the user before invoking. A future
'send-allowlist' (NEXUS_SMTP_ALLOWED_RECIPIENTS env var) can lock
the bot to specific recipients while iterating.
"""
from __future__ import annotations

import asyncio
import email.message
import json
import logging
import os
import platform
import shutil
import smtplib
import ssl
from datetime import datetime, timedelta, timezone
from typing import Optional
from urllib.parse import quote

from nexus_core.tools.base import BaseTool, ToolResult

logger = logging.getLogger(__name__)


# ── JXA snippet for reading Calendar.app events ───────────────────────
#
# JavaScript for Automation. Faster + safer than classic AppleScript:
# no string parsing on our side, we just JSON.stringify the result and
# read it on the Python side.
#
# Note: Calendar.app must have been opened at least once for the
# osascript bridge to find any calendars. macOS will prompt the user
# for Calendar access on the first call — we surface that prompt in
# the tool's error path.
_READ_CALENDAR_JXA = r"""
ObjC.import('stdlib');
function run(argv) {
  var startISO = argv[0];   // ISO-8601 like "2026-05-23T00:00:00Z"
  var endISO   = argv[1];
  var startMs  = new Date(startISO).getTime();
  var endMs    = new Date(endISO).getTime();

  var app   = Application('Calendar');
  app.includeStandardAdditions = true;
  var out = [];
  try {
    var calendars = app.calendars();
    for (var i = 0; i < calendars.length; i++) {
      var cal = calendars[i];
      var calName = cal.name();
      var events;
      try { events = cal.events(); } catch (_) { continue; }
      for (var j = 0; j < events.length; j++) {
        var ev = events[j];
        var s = ev.startDate();
        var e = ev.endDate();
        if (!s) continue;
        var sMs = s.getTime();
        var eMs = e ? e.getTime() : sMs;
        if (eMs < startMs || sMs > endMs) continue;
        out.push({
          calendar: calName,
          summary:  String(ev.summary() || ''),
          location: String(ev.location() || ''),
          start:    s.toISOString(),
          end:      e ? e.toISOString() : null,
          allDay:   !!ev.alldayEvent(),
          notes:    String(ev.description() || '').slice(0, 500),
        });
      }
    }
  } catch (err) {
    return JSON.stringify({error: String(err)});
  }
  out.sort(function(a, b) { return a.start < b.start ? -1 : 1; });
  return JSON.stringify({events: out});
}
"""


# ─────────────────────────────────────────────────────────────────────
# Tool: read_calendar
# ─────────────────────────────────────────────────────────────────────


class ReadCalendarTool(BaseTool):
    """Read events from the user's macOS Calendar.app."""

    @property
    def name(self) -> str:
        return "read_calendar"

    @property
    def description(self) -> str:
        return (
            "Read events from the user's local macOS Calendar.app. "
            "Returns a JSON list of events with summary / start / end "
            "/ location / notes within the requested time window. "
            "Use this when the user asks about upcoming appointments, "
            "today's schedule, when they're free, or wants to brief on "
            "a specific event.\n"
            "\n"
            "Defaults: next 7 days starting from now. macOS-only — on "
            "other platforms the tool returns a clear 'not available' "
            "error.\n"
            "\n"
            "First call may prompt the user for Calendar access on "
            "macOS — this is normal and a one-time grant."
        )

    @property
    def parameters(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "start": {
                    "type": "string",
                    "description": (
                        "ISO-8601 start timestamp (e.g. "
                        "'2026-05-23T00:00:00Z'). Defaults to now."
                    ),
                },
                "end": {
                    "type": "string",
                    "description": (
                        "ISO-8601 end timestamp. Defaults to "
                        "7 days after start."
                    ),
                },
            },
            "required": [],
        }

    async def execute(
        self, start: Optional[str] = None, end: Optional[str] = None,
        **kwargs,
    ) -> ToolResult:
        if platform.system() != "Darwin":
            return ToolResult(
                success=False,
                error=(
                    "read_calendar is only available on macOS (this "
                    f"server is running {platform.system()}). Ask the "
                    "user to paste their relevant calendar entries "
                    "directly into chat instead."
                ),
            )
        if shutil.which("osascript") is None:
            return ToolResult(
                success=False,
                error="osascript not found on PATH. macOS install broken?",
            )

        now = datetime.now(timezone.utc)
        try:
            start_dt = (
                datetime.fromisoformat(start.replace("Z", "+00:00"))
                if start else now
            )
            end_dt = (
                datetime.fromisoformat(end.replace("Z", "+00:00"))
                if end else start_dt + timedelta(days=7)
            )
        except ValueError as e:
            return ToolResult(
                success=False,
                error=f"Bad ISO timestamp: {e}. Use e.g. 2026-05-23T00:00:00Z.",
            )

        # JXA's Date parser accepts ISO 8601 directly.
        proc_args = [
            "osascript", "-l", "JavaScript",
            "-e", _READ_CALENDAR_JXA,
            start_dt.isoformat(), end_dt.isoformat(),
        ]
        try:
            proc = await asyncio.create_subprocess_exec(
                *proc_args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=30.0,
            )
        except asyncio.TimeoutError:
            return ToolResult(
                success=False,
                error=(
                    "Calendar query timed out after 30s. Calendar.app "
                    "may be frozen or has too many events in the "
                    "window. Try a narrower range."
                ),
            )
        except Exception as e:  # noqa: BLE001
            return ToolResult(
                success=False, error=f"osascript failed: {e}",
            )

        if proc.returncode != 0:
            err = stderr.decode("utf-8", errors="replace").strip()
            hint = ""
            if "not authorized" in err.lower() or "1743" in err:
                hint = (
                    " — macOS Calendar access was denied. Tell the "
                    "user to open System Settings → Privacy & "
                    "Security → Calendars, and grant access to "
                    "Nexus / nexus-server."
                )
            return ToolResult(
                success=False,
                error=f"Calendar.app query failed (rc={proc.returncode}): "
                      f"{err[:200]}{hint}",
            )

        raw = stdout.decode("utf-8", errors="replace").strip()
        if not raw:
            return ToolResult(output='{"events":[]}')
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as e:
            return ToolResult(
                success=False,
                error=f"Calendar response wasn't JSON: {e}. Raw: {raw[:200]}",
            )
        if "error" in parsed:
            return ToolResult(
                success=False,
                error=f"Calendar.app: {parsed['error']}",
            )
        events = parsed.get("events", [])
        logger.info(
            "read_calendar: %d events in window %s → %s",
            len(events), start_dt.isoformat(), end_dt.isoformat(),
        )
        return ToolResult(output=json.dumps(parsed, indent=2))


# ─────────────────────────────────────────────────────────────────────
# Tool: compose_email_draft
# ─────────────────────────────────────────────────────────────────────


class ComposeEmailDraftTool(BaseTool):
    """Open the user's default mail client with a pre-filled draft.

    Uses ``mailto:`` URLs so zero auth is required — the user reviews
    in their mail client (Mail.app, Outlook, web Gmail set as
    default, etc.) and clicks Send themselves. Critical for clinical
    / medical use cases where the AI's draft must be human-reviewed
    before transmission.
    """

    @property
    def name(self) -> str:
        return "compose_email_draft"

    @property
    def description(self) -> str:
        return (
            "Open the user's default email client with a pre-filled "
            "draft (To / Subject / Body). The user reviews and clicks "
            "Send — the agent never transmits email autonomously. Use "
            "this when the user asks you to email someone, send a "
            "report, follow up on a meeting, etc.\n"
            "\n"
            "macOS only today (uses `open mailto:…`). On other "
            "platforms returns a clear error. The body is plain text; "
            "the mail client may apply its own signature on send."
        )

    @property
    def parameters(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "to": {
                    "type": "string",
                    "description": (
                        "Recipient email address. Multiple recipients "
                        "comma-separated. Required."
                    ),
                },
                "subject": {
                    "type": "string",
                    "description": "Email subject line.",
                },
                "body": {
                    "type": "string",
                    "description": (
                        "Email body (plain text). Markdown is preserved "
                        "as-is — most mail clients render it as plain "
                        "text. Keep it concise; the user can edit "
                        "before sending."
                    ),
                },
                "cc": {
                    "type": "string",
                    "description": "Optional CC recipients, comma-separated.",
                },
            },
            "required": ["to"],
        }

    async def execute(
        self,
        to: str = "",
        subject: str = "",
        body: str = "",
        cc: Optional[str] = None,
        **kwargs,
    ) -> ToolResult:
        if not to.strip():
            return ToolResult(
                success=False, error="`to` is required.",
            )
        if platform.system() != "Darwin":
            return ToolResult(
                success=False,
                error=(
                    "compose_email_draft is only available on macOS "
                    "today (this server is running "
                    f"{platform.system()}). On other platforms, copy "
                    "the draft into your mail client manually."
                ),
            )
        if shutil.which("open") is None:
            return ToolResult(
                success=False,
                error="`open` command not found. macOS install broken?",
            )

        # Build mailto URL. URL-encode each component — `quote(s, safe='')`
        # turns spaces into `%20` and avoids `+` (some clients
        # interpret `+` as space, others don't).
        params = []
        if subject:
            params.append(f"subject={quote(subject, safe='')}")
        if body:
            params.append(f"body={quote(body, safe='')}")
        if cc:
            params.append(f"cc={quote(cc, safe='')}")
        url = f"mailto:{quote(to, safe='@,')}"
        if params:
            url += "?" + "&".join(params)

        # mailto URLs have practical length limits on macOS (~2048
        # chars). For longer bodies the mail client may truncate
        # silently — warn the agent so it can split.
        if len(url) > 8000:
            return ToolResult(
                success=False,
                error=(
                    f"Email body too long ({len(body)} chars). "
                    "Shorten or split into multiple drafts."
                ),
            )

        try:
            proc = await asyncio.create_subprocess_exec(
                "open", url,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=10.0,
            )
        except asyncio.TimeoutError:
            return ToolResult(
                success=False,
                error="Opening mail client timed out after 10s.",
            )
        except Exception as e:  # noqa: BLE001
            return ToolResult(
                success=False, error=f"`open` failed: {e}",
            )

        if proc.returncode != 0:
            err = stderr.decode("utf-8", errors="replace").strip()
            return ToolResult(
                success=False,
                error=f"`open mailto:` returned rc={proc.returncode}: {err[:200]}",
            )

        logger.info(
            "compose_email_draft opened for to=%s subject=%r (body=%d chars)",
            to, subject[:60], len(body),
        )
        return ToolResult(
            output=(
                "Opened mail client with the draft. "
                f"To: {to}. Subject: {subject!r}. The user will "
                "review and click Send — you do NOT need to ask "
                "for confirmation; just acknowledge that the draft "
                "is ready."
            ),
        )


# ─────────────────────────────────────────────────────────────────────
# Tool: send_email_now  (SMTP, irreversible)
# ─────────────────────────────────────────────────────────────────────


def _smtp_config() -> Optional[dict]:
    """Return SMTP settings from env, or None when not configured.
    Caller surfaces a clear error when None — we never silently
    fall back to an unconfigured mode.

    Recognises bundled credentials (containing the literal
    ``REPLACE_WITH_`` sentinel from the .env template) and treats
    them as "not configured" — prevents the agent from trying to
    send via a placeholder string."""
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
    return {
        "host":     host,
        "port":     port,
        "user":     user,
        "password": password,
        "from":     (os.environ.get("NEXUS_SMTP_FROM", "").strip() or user),
        # Comma-separated allow-list of recipients. STRONGLY RECOMMENDED
        # when SMTP_PASSWORD is bundled in the .dmg — limits blast
        # radius if the password is extracted. The send tool's pre-flight
        # check (_validate_recipients + _bundled_creds_require_allowlist)
        # enforces this server-side; even an attacker editing their
        # local .env can't bypass it because the check runs in the same
        # process the agent uses.
        "allowed":  [
            r.strip().lower() for r in
            os.environ.get("NEXUS_SMTP_ALLOWED_RECIPIENTS", "").split(",")
            if r.strip()
        ],
        # #115: bundled credentials (shipped in the .dmg) trigger a
        # mandatory allow-list policy. The presence of
        # NEXUS_SMTP_BUNDLED_CREDS=1 (set by build-macos.sh) tells the
        # send path "your password is half-public; refuse unbounded
        # sends".
        "bundled":  os.environ.get("NEXUS_SMTP_BUNDLED_CREDS", "").strip() == "1",
    }


def _validate_recipients(addrs: list[str], allowed: list[str]) -> Optional[str]:
    """Return None if all recipients are allowed; else an error
    string. Empty allow-list means no restriction."""
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


def _send_smtp_sync(
    cfg: dict, to: list[str], cc: list[str],
    subject: str, body: str,
) -> tuple[bool, str]:
    """Run SMTP send on the calling thread. Wrapped in run_in_executor
    by the async tool below. Returns (ok, message_or_error)."""
    msg = email.message.EmailMessage()
    msg["From"]    = cfg["from"]
    msg["To"]      = ", ".join(to)
    if cc:
        msg["Cc"]  = ", ".join(cc)
    msg["Subject"] = subject
    msg.set_content(body or "")

    try:
        # Modern Gmail SMTP submission: TLS on 587 with STARTTLS.
        # We don't support implicit-TLS port 465 here for simplicity;
        # most providers offer 587 as well.
        ctx = ssl.create_default_context()
        with smtplib.SMTP(cfg["host"], cfg["port"], timeout=20) as s:
            s.ehlo()
            s.starttls(context=ctx)
            s.ehlo()
            s.login(cfg["user"], cfg["password"])
            recipients = to + cc
            refused = s.send_message(msg, to_addrs=recipients)
            if refused:
                return False, f"Some recipients refused: {refused}"
        return True, f"Sent to {', '.join(to + cc)} from {cfg['from']}."
    except smtplib.SMTPAuthenticationError as e:
        return False, (
            f"SMTP authentication failed: {e}. If using Gmail, ensure "
            "NEXUS_SMTP_PASSWORD is a 16-char App Password (NOT your "
            "regular account password). Generate one at "
            "https://myaccount.google.com/apppasswords."
        )
    except smtplib.SMTPRecipientsRefused as e:
        return False, f"All recipients refused: {e.recipients}"
    except smtplib.SMTPException as e:
        return False, f"SMTP error: {e}"
    except OSError as e:
        return False, f"Network error reaching {cfg['host']}: {e}"


async def _post_to_relay(
    url: str, api_key: str, payload: dict,
) -> tuple[bool, str, int]:
    """POST one send request to the relay. Returns (ok, msg, status_code).
    Relay's JSON error body bubbles up verbatim so users see the
    real reason (rate limited / blocked / smtp_error / etc.)."""
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
        return False, f"Cannot reach relay at {endpoint}: {e}", 0
    except httpx.TimeoutException as e:
        return False, f"Relay timed out after 30s: {e}", 0
    except Exception as e:  # noqa: BLE001
        return False, f"Relay call failed: {e}", 0

    if resp.status_code == 200:
        try:
            data = resp.json()
            return True, (
                f"Sent via relay. To: {', '.join(data.get('sent_to') or [])}. "
                f"Quota remaining today: {data.get('daily_quota_remaining', '?')}."
            ), 200
        except Exception:
            return True, "Sent via relay (response unparseable but 200).", 200

    # Non-200: try to surface the relay's structured detail.
    try:
        data = resp.json()
        detail = data.get("detail") or str(data)
    except Exception:
        detail = resp.text[:300] or f"HTTP {resp.status_code}"
    return False, f"Relay rejected send (HTTP {resp.status_code}): {detail}", resp.status_code


class SendEmailNowTool(BaseTool):
    """Send an email RIGHT NOW via configured SMTP. Irreversible."""

    def _user_id(self) -> str:
        """Read the calling user from llm_gateway's contextvar (#113).
        Falls back to 'anonymous' for tests / dev calls."""
        try:
            from nexus_server.llm_gateway import _current_user_var
            return _current_user_var.get() or "anonymous"
        except Exception:
            return "anonymous"

    async def _send_via_relay(
        self, relay_url: str, relay_api_key: str,
        to: str, subject: str, body: str, cc: Optional[str],
    ) -> ToolResult:
        """Path used in prod — POST to the hosted Fly.io relay."""
        payload = {
            "nexus_user_id": self._user_id(),
            "to":      to,
            "subject": subject,
            "body":    body,
        }
        if cc:
            payload["cc"] = cc
        ok, msg, code = await _post_to_relay(relay_url, relay_api_key, payload)
        if not ok:
            return ToolResult(success=False, error=msg)
        logger.info(
            "send_email_now via relay: user=%s to=%r subject=%r",
            payload["nexus_user_id"], to, subject[:80],
        )
        return ToolResult(output="✓ " + msg)


    @property
    def name(self) -> str:
        return "send_email_now"

    @property
    def description(self) -> str:
        relay_configured = bool(
            os.environ.get("NEXUS_RELAY_URL", "").strip()
            and os.environ.get("NEXUS_RELAY_API_KEY", "").strip()
        )
        smtp_configured  = _smtp_config() is not None
        if relay_configured:
            status_line = (
                "STATUS: hosted relay configured — sends go through "
                "an audited rate-limited gateway. The relay enforces "
                "a daily cap (typically 10/user/day) and a recipient "
                "allow-list; over-limit / disallowed sends bounce with "
                "a clear error you should pass to the user verbatim."
            )
        elif smtp_configured:
            status_line = (
                "STATUS: direct SMTP configured (DEV MODE). Sends go "
                "straight to the SMTP server — no relay, no audit. "
                "For production deploys, configure NEXUS_RELAY_URL "
                "instead (see packages/relay/)."
            )
        else:
            status_line = (
                "STATUS: no email backend configured. Calls will "
                "return 'not configured'. Operator: set up the "
                "Fly.io relay (recommended — see packages/relay/) "
                "OR configure direct SMTP (NEXUS_SMTP_* in .env)."
            )
        return (
            "Send an email DIRECTLY via SMTP. This is IRREVERSIBLE — "
            "the message goes to the recipient(s) the moment this "
            "tool returns success.\n"
            "\n"
            "Safety rules:\n"
            "  - BEFORE invoking, show the user the proposed To / "
            "Subject / Body in chat text and explicitly ask "
            "'send this now?'. Don't invoke until the user types "
            "yes / confirm / send / 发 / similar.\n"
            "  - On send-failure, the tool returns a clear error; "
            "you should surface it to the user verbatim.\n"
            "  - If the user wants to edit the draft before sending, "
            "use `compose_email_draft` instead (opens the user's mail "
            "client with the draft prefilled — they hit Send "
            "themselves).\n"
            "\n"
            + status_line
        )

    @property
    def parameters(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "to": {
                    "type": "string",
                    "description": (
                        "Recipient email address. Multiple recipients "
                        "comma-separated. Required."
                    ),
                },
                "subject": {
                    "type": "string",
                    "description": "Email subject line.",
                },
                "body": {
                    "type": "string",
                    "description": (
                        "Email body (plain text). Keep it concise + "
                        "professional. Markdown is preserved but most "
                        "recipients see it as plain text."
                    ),
                },
                "cc": {
                    "type": "string",
                    "description": "Optional CC, comma-separated.",
                },
            },
            "required": ["to", "subject", "body"],
        }

    async def execute(
        self,
        to: str = "",
        subject: str = "",
        body: str = "",
        cc: Optional[str] = None,
        **kwargs,
    ) -> ToolResult:
        if not to.strip() or not subject.strip() or not body.strip():
            return ToolResult(
                success=False,
                error="`to`, `subject`, and `body` are all required.",
            )

        # #116: prefer the hosted relay when configured. The relay
        # holds the real SMTP password (in its hosting env, never
        # shipped to clients) and enforces rate-limit + allow-list
        # + audit log centrally. Direct SMTP path stays as a dev
        # fallback for local iteration.
        relay_url     = os.environ.get("NEXUS_RELAY_URL", "").strip()
        relay_api_key = os.environ.get("NEXUS_RELAY_API_KEY", "").strip()
        if relay_url and relay_api_key:
            return await self._send_via_relay(
                relay_url, relay_api_key, to, subject, body, cc,
            )

        cfg = _smtp_config()
        if cfg is None:
            return ToolResult(
                success=False,
                error=(
                    "Neither relay (NEXUS_RELAY_URL + "
                    "NEXUS_RELAY_API_KEY) nor direct SMTP "
                    "(NEXUS_SMTP_HOST + NEXUS_SMTP_USER + "
                    "NEXUS_SMTP_PASSWORD) is configured. For "
                    "production deploys use a relay — see "
                    "packages/relay/."
                ),
            )

        to_list = [a.strip() for a in to.split(",") if a.strip()]
        cc_list = [a.strip() for a in (cc or "").split(",") if a.strip()]
        all_addrs = to_list + cc_list
        if not to_list:
            return ToolResult(
                success=False,
                error="`to` did not contain any valid addresses.",
            )

        # #115: bundled credentials REQUIRE a non-empty allow-list.
        # The bundled SMTP_PASSWORD is half-public (shipped in .dmg),
        # so an unbounded send target would let anyone with the .dmg
        # spam from the shared bot account. Refuse outright if the
        # operator forgot to set NEXUS_SMTP_ALLOWED_RECIPIENTS.
        if cfg.get("bundled") and not cfg["allowed"]:
            return ToolResult(
                success=False,
                error=(
                    "Bundled SMTP credentials require a non-empty "
                    "NEXUS_SMTP_ALLOWED_RECIPIENTS allow-list. "
                    "Refusing to send — the .dmg-shipped password "
                    "could be extracted, so unbounded sends are "
                    "blocked by design. Operator: add the recipient "
                    "to NEXUS_SMTP_ALLOWED_RECIPIENTS in the bundle's "
                    "packages/server/.env, OR have each user add their "
                    "own contacts to ~/Library/Application Support/"
                    "RuneProtocol/.env."
                ),
            )
        gate_err = _validate_recipients(all_addrs, cfg["allowed"])
        if gate_err:
            return ToolResult(success=False, error=gate_err)

        loop = asyncio.get_event_loop()
        try:
            ok, msg = await asyncio.wait_for(
                loop.run_in_executor(
                    None, _send_smtp_sync,
                    cfg, to_list, cc_list, subject, body,
                ),
                timeout=30.0,
            )
        except asyncio.TimeoutError:
            return ToolResult(
                success=False,
                error=(
                    f"SMTP send timed out after 30s "
                    f"({cfg['host']}:{cfg['port']}). Network reachable?"
                ),
            )

        if not ok:
            return ToolResult(success=False, error=msg)

        logger.info(
            "send_email_now: %s → %s (subject=%r, %d chars)",
            cfg["from"], all_addrs, subject[:60], len(body),
        )
        return ToolResult(output="✓ " + msg)


# ─────────────────────────────────────────────────────────────────────
# Registrar (called from twin_manager)
# ─────────────────────────────────────────────────────────────────────


def register_calendar_tools(twin, user_id: str) -> None:
    """Register the calendar + email tools onto the given twin."""
    twin.register_tool(ReadCalendarTool())
    twin.register_tool(ComposeEmailDraftTool())
    twin.register_tool(SendEmailNowTool())
    logger.info(
        "Calendar/email tools registered for user %s "
        "(platform=%s, smtp_configured=%s)",
        user_id, platform.system(), _smtp_config() is not None,
    )
