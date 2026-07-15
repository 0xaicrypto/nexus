"""Stripe billing wrapper.

Thin layer over the Stripe SDK so the rest of the app talks to one
clean module instead of importing ``stripe`` everywhere. Keeps three
operations behind a single import:

  * :func:`create_checkout_session` — builds the URL the desktop opens
    in the system browser when the user clicks "Upgrade".
  * :func:`create_portal_session` — builds the URL for "Manage
    subscription" (Stripe-hosted: invoices, card change, cancel).
  * :func:`handle_webhook_event` — verifies + dispatches Stripe
    webhooks into our user/subscription state machine.

All three are no-ops (raise / return None) when billing is disabled
via empty STRIPE_SECRET_KEY. That mode is what unit tests + local
dev use.

Why a separate module?
──────────────────────
* Routes (billing_routes.py) stay thin — they just translate HTTP
  to/from these functions.
* Webhook event dispatch is the only place that writes
  users.subscription_state etc. Centralising the mutations here makes
  the state machine auditable in one file.
* Stripe SDK is an optional dep (pyproject.toml has it as a soft
  install); the imports here are deferred so a deployment without
  stripe installed still boots.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from nexus_server import config
from nexus_server.database import get_db_connection

logger = logging.getLogger(__name__)

# Lazy import so unit-test / no-billing deployments don't need the
# `stripe` package on PYTHONPATH.
_stripe = None


def _get_stripe():
    """Return the configured ``stripe`` module, or None if billing is
    disabled / the SDK isn't installed."""
    global _stripe
    if not config.config.billing_enabled:
        return None
    if _stripe is not None:
        return _stripe
    try:
        import stripe  # type: ignore
    except ImportError:
        logger.error(
            "STRIPE_SECRET_KEY is set but the `stripe` Python package is "
            "not installed. Add `stripe>=7.0` to packages/server/pyproject.toml "
            "and re-run setup.sh."
        )
        return None
    stripe.api_key = config.config.STRIPE_SECRET_KEY
    # Pin a sane API version so an SDK upgrade doesn't silently change
    # webhook payload shapes. Bump when you've audited the diff.
    stripe.api_version = "2024-11-20.acacia"
    _stripe = stripe
    return _stripe


# ── Checkout session ────────────────────────────────────────────────

def create_checkout_session(
    user_id: str,
    email: Optional[str],
    tier: str,
    cadence: str = "monthly",
) -> Optional[str]:
    """Create a Stripe Checkout session for ``user`` and return the URL.

    The session is wired so:
      * `customer_email` is pre-filled (Stripe will create / reuse a
        Customer keyed on that email).
      * `client_reference_id` is our user_id so the webhook can match
        events back to our row.
      * `metadata.tier` records which tier the user picked.
      * Success / cancel URLs come from config — both point at our
        local server which then closes the browser tab.

    Returns ``None`` when billing is disabled or the requested tier
    isn't configured (caller should respond 501 / 400).
    """
    stripe = _get_stripe()
    if stripe is None:
        return None

    price_id = config.config.stripe_price_id(tier, cadence)
    if not price_id:
        logger.warning(
            "create_checkout_session: no price id for tier=%s cadence=%s",
            tier, cadence,
        )
        return None

    session = stripe.checkout.Session.create(
        mode="subscription",
        line_items=[{"price": price_id, "quantity": 1}],
        customer_email=email,
        client_reference_id=user_id,
        success_url=config.config.STRIPE_SUCCESS_URL + "?session_id={CHECKOUT_SESSION_ID}",
        cancel_url=config.config.STRIPE_CANCEL_URL,
        metadata={
            "user_id": user_id,
            "tier": tier,
            "cadence": cadence,
        },
        # 14-day trial without requiring card upfront. Toggle off in
        # Stripe Dashboard when you want strict pay-up-front.
        subscription_data={
            "trial_period_days": 14,
            "metadata": {"user_id": user_id, "tier": tier},
        },
        # Save customer payment method for future renewals.
        payment_method_collection="if_required",
    )
    return session.url


# ── Customer Portal (manage subscription) ───────────────────────────

def create_portal_session(stripe_customer_id: str) -> Optional[str]:
    """Create a Stripe Billing Portal session for an existing customer.
    The user lands on Stripe-hosted pages for invoices, card change,
    upgrade/downgrade, and cancellation."""
    stripe = _get_stripe()
    if stripe is None:
        return None

    session = stripe.billing_portal.Session.create(
        customer=stripe_customer_id,
        return_url=config.config.STRIPE_SUCCESS_URL,
    )
    return session.url


# ── Webhook dispatch ────────────────────────────────────────────────

# Maps Stripe Subscription.status → our users.subscription_state.
# Identity for the common ones; we keep Stripe's vocabulary so when
# we read `subscription_state` we already speak Stripe. The one
# rename is "canceled" → we keep that spelling for parity (US English,
# as Stripe uses) rather than re-spelling "cancelled".
_STATUS_MAP = {
    "trialing":          "trialing",
    "active":            "active",
    "past_due":          "past_due",
    "canceled":          "canceled",
    "unpaid":            "unpaid",
    "incomplete":        "incomplete",
    "incomplete_expired":"incomplete_expired",
    "paused":            "paused",
}


def handle_webhook_event(payload: bytes, signature: str) -> dict:
    """Verify a Stripe webhook payload + dispatch to a state-machine
    update. Returns a small dict the route handler can echo as the
    response body for debugging.

    Raises:
        ValueError — bad signature, malformed payload, billing disabled.
    """
    stripe = _get_stripe()
    if stripe is None:
        raise ValueError("billing disabled")
    if not config.config.STRIPE_WEBHOOK_SECRET:
        raise ValueError("STRIPE_WEBHOOK_SECRET not set")

    try:
        event = stripe.Webhook.construct_event(
            payload, signature, config.config.STRIPE_WEBHOOK_SECRET
        )
    except (ValueError, stripe.error.SignatureVerificationError) as e:
        raise ValueError(f"webhook verification failed: {e}") from e

    etype = event["type"]
    obj = event["data"]["object"]
    logger.info("Stripe webhook: %s id=%s", etype, obj.get("id"))

    # We care about three families:
    #   * checkout.session.completed — first-time link Customer ↔ user.
    #   * customer.subscription.{created,updated,deleted} — lifecycle.
    #   * invoice.payment_{succeeded,failed} — billing health.
    if etype == "checkout.session.completed":
        _on_checkout_completed(obj)
    elif etype.startswith("customer.subscription."):
        _on_subscription_changed(obj)
    elif etype == "invoice.payment_failed":
        _on_payment_failed(obj)
    # Other event types (invoice.paid, customer.updated, etc.) we
    # don't act on — they're noise from our perspective but logging
    # the type at INFO above gives us a forensic trail anyway.

    return {"received": True, "type": etype}


def _on_checkout_completed(session: dict) -> None:
    """Link a Stripe Customer to our user row. This is the FIRST
    event after a successful checkout."""
    user_id = (session.get("client_reference_id")
               or session.get("metadata", {}).get("user_id"))
    customer_id = session.get("customer")
    subscription_id = session.get("subscription")
    tier = session.get("metadata", {}).get("tier", "pro")
    if not user_id or not customer_id:
        logger.warning("checkout.session.completed missing user_id/customer_id: %s", session.get("id"))
        return

    with get_db_connection() as conn:
        conn.execute(
            """
            UPDATE users SET
              stripe_customer_id = ?,
              stripe_subscription_id = ?,
              tier = ?,
              subscription_state = ?,
              updated_at = ?
            WHERE id = ?
            """,
            (customer_id, subscription_id, tier, "trialing",
             datetime.now(timezone.utc).isoformat(), user_id),
        )
        conn.commit()
    logger.info("Linked user %s → customer %s tier=%s", user_id, customer_id, tier)


def _on_subscription_changed(sub: dict) -> None:
    """Sync subscription state from Stripe to our users.subscription_state.
    Fired on subscription.created / .updated / .deleted."""
    customer_id = sub.get("customer")
    if not customer_id:
        return
    state = _STATUS_MAP.get(sub.get("status", ""), sub.get("status"))
    renews_at_ts = sub.get("current_period_end")
    renews_at = (
        datetime.fromtimestamp(renews_at_ts, tz=timezone.utc).isoformat()
        if renews_at_ts else None
    )

    with get_db_connection() as conn:
        conn.execute(
            """
            UPDATE users SET
              subscription_state = ?,
              subscription_renews_at = ?,
              stripe_subscription_id = ?,
              updated_at = ?
            WHERE stripe_customer_id = ?
            """,
            (state, renews_at, sub.get("id"),
             datetime.now(timezone.utc).isoformat(), customer_id),
        )
        conn.commit()
    logger.info("Customer %s subscription state → %s", customer_id, state)


def _on_payment_failed(invoice: dict) -> None:
    """Mark the subscription past_due. Stripe will retry per the
    Dashboard's smart-retry settings; if all retries fail it fires
    customer.subscription.deleted which our subscription handler
    catches."""
    customer_id = invoice.get("customer")
    if not customer_id:
        return
    with get_db_connection() as conn:
        conn.execute(
            """UPDATE users SET subscription_state = 'past_due', updated_at = ?
               WHERE stripe_customer_id = ?""",
            (datetime.now(timezone.utc).isoformat(), customer_id),
        )
        conn.commit()
    logger.warning("Payment failed for customer %s", customer_id)
