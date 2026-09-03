"""Fixture: SECURE — Stripe key loaded from env var on server, never in frontend.

This is the secure counterpart to stripe_secret_in_react.jsx.
check_payment_secret_in_frontend should PASS on this file because
os.environ is used (the _SAFE_ENV_CONFIG guard fires).
"""

import os
import stripe

# Loaded securely from environment — not embedded as a literal.
stripe.api_key = os.environ["STRIPE_SECRET_KEY"]


def create_charge(amount_cents: int, source_token: str) -> dict:
    """Create a Stripe charge server-side with the env-sourced secret key."""
    return stripe.Charge.create(
        amount=amount_cents,
        currency="usd",
        source=source_token,
    )


def issue_refund(charge_id: str, amount_cents: int) -> dict:
    """Issue a refund server-side."""
    return stripe.Refund.create(charge=charge_id, amount=amount_cents)
