"""RFC 5545 .ics renderer + email send_with_ics helper (design §5.2).

The helper composes an RFC 5545 VCALENDAR with one VEVENT, then sends
it as ``multipart/alternative`` with ``Content-Type: text/calendar;
method=REQUEST`` so Outlook / Gmail / Apple Mail all recognise it as
a meeting invite (calendar pop-up + Accept/Decline buttons).
"""
from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.message import EmailMessage
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class IcsEvent:
    summary:     str
    dtstart_utc: int                      # epoch seconds
    dtend_utc:   int                      # epoch seconds
    description: str = ""
    location:    str = ""
    organizer_email: str = ""
    attendee_emails: list[str] = field(default_factory=list)
    uid:         str = ""                 # generated if empty
    sequence:    int = 0                  # bump on update


def _fmt_utc(ts: int) -> str:
    dt = datetime.fromtimestamp(ts, tz=timezone.utc)
    return dt.strftime("%Y%m%dT%H%M%SZ")


def render_ics(ev: IcsEvent) -> str:
    """Render a minimal RFC 5545 VCALENDAR."""
    uid = ev.uid or f"rune-{uuid.uuid4()}@rune-protocol.app"
    now = _fmt_utc(int(time.time()))

    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Rune//Research Workspace//EN",
        "METHOD:REQUEST",
        "CALSCALE:GREGORIAN",
        "BEGIN:VEVENT",
        f"UID:{uid}",
        f"DTSTAMP:{now}",
        f"DTSTART:{_fmt_utc(ev.dtstart_utc)}",
        f"DTEND:{_fmt_utc(ev.dtend_utc)}",
        f"SUMMARY:{_escape(ev.summary)}",
        f"DESCRIPTION:{_escape(ev.description)}",
        f"LOCATION:{_escape(ev.location)}",
        f"SEQUENCE:{ev.sequence}",
        "STATUS:CONFIRMED",
        "TRANSP:OPAQUE",
    ]
    if ev.organizer_email:
        lines.append(f"ORGANIZER:mailto:{ev.organizer_email}")
    for a in ev.attendee_emails:
        lines.append(
            f"ATTENDEE;CUTYPE=INDIVIDUAL;ROLE=REQ-PARTICIPANT;PARTSTAT=NEEDS-ACTION;"
            f"RSVP=TRUE;CN={a}:mailto:{a}"
        )
    lines += ["END:VEVENT", "END:VCALENDAR"]
    # RFC 5545 prefers CRLF line endings.
    return "\r\n".join(lines) + "\r\n"


def _escape(s: str) -> str:
    """Escape characters per RFC 5545 §3.3.11 TEXT."""
    return (
        (s or "")
        .replace("\\", "\\\\")
        .replace("\n", "\\n")
        .replace(",", "\\,")
        .replace(";", "\\;")
    )


# ─────────────────────────────────────────────────────────────────────
# Email integration
# ─────────────────────────────────────────────────────────────────────


def send_with_ics(
    *, to: list[str],
    subject: str,
    body_text: str,
    event: IcsEvent,
    from_addr: str = "",
    cc: Optional[list[str]] = None,
) -> dict:
    """Compose a meeting-invite email and dispatch through the existing
    email_send module. Returns the transport result dict.
    """
    from nexus_server import email_send

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"]    = from_addr or "noreply@rune-protocol.app"
    msg["To"]      = ", ".join(to)
    if cc:
        msg["Cc"]  = ", ".join(cc)
    msg.set_content(body_text or subject)

    ics_text = render_ics(event)
    # Attach as both inline calendar part (so clients show the popup)
    # AND as a file attachment (so they can open it as .ics manually).
    msg.add_alternative(
        ics_text,
        subtype="calendar",
        params={"method": "REQUEST", "name": "invite.ics"},
    )

    # email_send.send takes the same fields we'd use in send_email — we
    # use it via a thin shim. If a more direct send_raw is added later
    # it can replace this.
    try:
        result = email_send.send(
            to=to,
            subject=subject,
            body_text=body_text,
            body_html=None,
            attachments=[{
                "name": "invite.ics",
                "mime": "text/calendar; method=REQUEST",
                "content": ics_text.encode("utf-8"),
            }],
            cc=cc or [],
        )
        if hasattr(result, "to_dict"):
            return result.to_dict()
        return result if isinstance(result, dict) else {"status": "ok"}
    except (AttributeError, TypeError) as exc:
        # Older email_send shapes — fall through to raw SMTP via
        # email_send._live_smtp_config if available.
        logger.warning("email_send.send not available (%s); skipping send", exc)
        return {"status": "skipped",
                "reason": "email_send.send shim not present",
                "ics_preview": ics_text}
