"""Stripe billing HTTP routes.

Four endpoints wired under /api/v1/billing:

  POST /checkout    — start Stripe Checkout. Returns a URL the desktop
                      opens in the system browser.
  POST /portal      — open Stripe Billing Portal for managing the
                      existing subscription (card / cancel / invoices).
  POST /webhook     — Stripe → us. Signature-verified; mutates users
                      table via billing.handle_webhook_event.
  GET  /subscription — read current subscription state for the
                      authenticated user. Powers the desktop's
                      "Plan" surface and trial-countdown banners.

All routes return 501 when STRIPE_SECRET_KEY isn't configured, except
the webhook which 400s (Stripe will retry on 5xx so we avoid that).
"""
from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

from nexus_server import billing, config

# get_current_user re-exported from nexus_server.auth (auth/__init__.py).
# Returns the user_id string, not an ORM object — every route looks up
# whatever fields it needs via get_db_connection().
from nexus_server.auth import get_current_user
from nexus_server.database import get_db_connection


def _lookup_user_email(user_id: str) -> Optional[str]:
    """Pull the user's email out of the users table. None when the
    column is empty (legacy users predating the billing migration) —
    Stripe Checkout handles a missing email by prompting the user to
    type it in during the flow."""
    with get_db_connection() as conn:
        row = conn.execute(
            "SELECT email FROM users WHERE id = ?", (user_id,),
        ).fetchone()
    return row["email"] if row else None

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/billing", tags=["billing"])


# ── Request / response shapes ────────────────────────────────────────

class CheckoutRequest(BaseModel):
    tier: str = Field(..., pattern="^(pro|pro_plus|radiology)$")
    cadence: str = Field("monthly", pattern="^(monthly|yearly)$")


class CheckoutResponse(BaseModel):
    url: str


class PortalResponse(BaseModel):
    url: str


class SubscriptionStatus(BaseModel):
    tier: str
    subscription_state: Optional[str]
    trial_ends_at: Optional[str]
    renews_at: Optional[str]
    has_payment_method: bool
    # `manage_url` is non-null only when the user already has a Stripe
    # customer record. New users get `checkout_required=true` and the
    # frontend should call /checkout instead.
    manage_url_available: bool


# ── Routes ───────────────────────────────────────────────────────────

@router.post("/checkout", response_model=CheckoutResponse)
def create_checkout(
    req: CheckoutRequest,
    user_id: str = Depends(get_current_user),
) -> CheckoutResponse:
    """Start a Stripe Checkout session. Returns a one-time URL the
    user's browser should open. The URL expires in 24 hours."""
    if not config.config.billing_enabled:
        raise HTTPException(
            status.HTTP_501_NOT_IMPLEMENTED,
            "Billing is not configured on this server.",
        )

    url = billing.create_checkout_session(
        user_id=user_id,
        email=_lookup_user_email(user_id),
        tier=req.tier,
        cadence=req.cadence,
    )
    if not url:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"No Stripe price configured for tier={req.tier} cadence={req.cadence}.",
        )
    return CheckoutResponse(url=url)


@router.post("/portal", response_model=PortalResponse)
def create_portal(user_id: str = Depends(get_current_user)) -> PortalResponse:
    """Open Stripe Billing Portal for an existing customer. 404 if the
    user has never completed checkout (no stripe_customer_id yet)."""
    if not config.config.billing_enabled:
        raise HTTPException(status.HTTP_501_NOT_IMPLEMENTED)

    with get_db_connection() as conn:
        row = conn.execute(
            "SELECT stripe_customer_id FROM users WHERE id = ?", (user_id,),
        ).fetchone()
    customer_id = row["stripe_customer_id"] if row else None
    if not customer_id:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            "No active subscription. Use /checkout to start one.",
        )

    url = billing.create_portal_session(customer_id)
    if not url:
        raise HTTPException(status.HTTP_501_NOT_IMPLEMENTED)
    return PortalResponse(url=url)


@router.post("/webhook")
async def webhook(request: Request) -> dict:
    """Stripe → us. Verifies signature with STRIPE_WEBHOOK_SECRET and
    dispatches to billing.handle_webhook_event, which mutates users.

    Returns 200 on accepted events. Returns 400 on bad-signature /
    malformed body — NOT 5xx, because Stripe interprets 5xx as
    "retry me" and we don't want infinite retries for a permanent
    misconfig.
    """
    payload = await request.body()
    sig_header = request.headers.get("stripe-signature", "")
    try:
        result = billing.handle_webhook_event(payload, sig_header)
    except ValueError as e:
        logger.warning("Stripe webhook rejected: %s", e)
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e)) from e
    return result


@router.get("/subscription", response_model=SubscriptionStatus)
def get_subscription(user_id: str = Depends(get_current_user)) -> SubscriptionStatus:
    """Return the authenticated user's subscription snapshot. The
    desktop polls this to render the "Plan" tab + the trial-expiring
    banner. Cheap — single users-table row read."""
    with get_db_connection() as conn:
        row = conn.execute(
            """SELECT tier, subscription_state, trial_ends_at,
                      subscription_renews_at, stripe_customer_id,
                      stripe_subscription_id
               FROM users WHERE id = ?""",
            (user_id,),
        ).fetchone()
    if not row:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found")

    return SubscriptionStatus(
        tier=row["tier"] or "beta",
        subscription_state=row["subscription_state"],
        trial_ends_at=row["trial_ends_at"],
        renews_at=row["subscription_renews_at"],
        has_payment_method=bool(row["stripe_subscription_id"]),
        manage_url_available=bool(row["stripe_customer_id"]),
    )
