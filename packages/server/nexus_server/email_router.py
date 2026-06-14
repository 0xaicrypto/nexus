"""REST endpoints for the v2 email-send capability.

  GET  /api/v1/email/transport
      → reports which transport(s) are configured (relay, SMTP, both,
        neither). Compose dialog hits this on open so the Send button
        can be disabled with a clear "no transport configured" hint
        instead of letting the medic type a draft they can never send.

  POST /api/v1/email/send
      → validates input, dispatches via ``email_send.send_email_async``
        (relay-first, SMTP-fallback). Returns the SendResult dict; UI
        surfaces ``message`` verbatim on both success and failure.

Auth: both endpoints require a valid JWT (Depends(get_current_user)).
The same user_id is forwarded into the relay payload so the relay's
audit log records WHICH medic kicked off the send — important for
rate-limit accounting (per-user quota) and for forensics if a send
ever needs to be traced back.

Notes on scope
──────────────
This is a deliberately thin router. We don't accept attachments
(v1 never supported them; the relay's Pydantic schema rejects extra
fields), and we don't allow per-user FROM overrides (the relay uses a
single audited mailbox; SMTP path uses whatever ``NEXUS_SMTP_FROM``
the operator configured). Keeping the wire format flat means the UI
side can stay one form + one POST.
"""
from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from nexus_server.auth import get_current_user
from nexus_server import email_send

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/email", tags=["email"])


# ─────────────────────────────────────────────────────────────────────
# Wire shapes
# ─────────────────────────────────────────────────────────────────────


class SendEmailRequest(BaseModel):
    """Compose dialog → server. Fields mirror what v1's
    ``SendEmailNowTool.parameters`` accepted, except ``to`` / ``cc``
    are list[str] here (the UI already splits on commas client-side
    so a typed list is cleaner over the wire)."""

    to:      list[str] = Field(..., min_length=1)
    subject: str       = Field(..., min_length=1, max_length=998)
    body:    str       = Field(..., min_length=1, max_length=200_000)
    cc:      list[str] = Field(default_factory=list)


class SendEmailResponse(BaseModel):
    ok:          bool
    transport:   str            # 'relay' | 'smtp' | 'none'
    message:     str
    sent_to:     list[str] = Field(default_factory=list)
    status_code: int       = 0


class TransportStatusResponse(BaseModel):
    """Returned by GET /api/v1/email/transport. ``configured`` is the
    boolean the UI flips off the Send button on. The verbose fields
    (relay_url_host, allowed_recipients, etc.) feed Settings · Email's
    "current status" card so the medic can sanity-check what the
    operator configured."""

    configured:         bool
    relay_configured:   bool
    smtp_configured:    bool
    bundled_creds:      bool
    default_from:       str
    allowed_recipients: list[str]
    relay_url_host:     str


# ─────────────────────────────────────────────────────────────────────
# Routes
# ─────────────────────────────────────────────────────────────────────


@router.get("/transport", response_model=TransportStatusResponse)
async def get_transport_status(
    _: str = Depends(get_current_user),
) -> TransportStatusResponse:
    """Read-only probe — what can the server do right now?

    Cheap (env-var reads only, no network) so the desktop polls it
    every time the Compose dialog opens. We don't cache because
    operator might have just dropped fresh creds into $RUNE_HOME/.env
    and the next call must pick them up (same hot-reload contract as
    the LLM keys).
    """
    s = email_send.transport_status()
    return TransportStatusResponse(**s.to_dict())


@router.post(
    "/send", response_model=SendEmailResponse,
    status_code=status.HTTP_200_OK,
)
async def send_email(
    req: SendEmailRequest,
    user_id: str = Depends(get_current_user),
) -> SendEmailResponse:
    """Dispatch one outbound email.

    Returns 200 + ``ok=False`` on send-level failures (relay rejected,
    SMTP auth bad, recipient blocked, etc.) so the UI can surface
    ``message`` directly without parsing a 4xx error envelope. We
    reserve actual HTTP errors for:

      - 401: auth missing / expired (handled by Depends)
      - 422: schema rejection by FastAPI (empty to / oversize body)
      - 503: transport not configured at all
    """
    # Reject early if NOTHING is set up — UI shows a clearer error this
    # way (it can branch on the 503 to render "Set up SMTP/relay first").
    status_dc = email_send.transport_status()
    if not (status_dc.relay_configured or status_dc.smtp_configured):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "Email transport not configured on this server. Configure "
                "either NEXUS_RELAY_URL + NEXUS_RELAY_API_KEY (recommended) "
                "or NEXUS_SMTP_HOST + NEXUS_SMTP_USER + NEXUS_SMTP_PASSWORD "
                "in $RUNE_HOME/.env, then retry."
            ),
        )

    result = await email_send.send_email_async(
        user_id = user_id,
        to      = req.to,
        cc      = req.cc,
        subject = req.subject,
        body    = req.body,
    )
    return SendEmailResponse(
        ok          = result.ok,
        transport   = result.transport,
        message     = result.message,
        sent_to     = result.sent_to,
        status_code = result.status_code,
    )
