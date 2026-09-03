"""Secure: secrets come from environment, no payment keys in frontend."""
import os


# Good: secret from environment
STRIPE_SECRET_KEY = os.environ["STRIPE_SECRET_KEY"]

# Good: publishable key is intended for frontend use
STRIPE_PUBLISHABLE_KEY = "pk_live_4eC39HqLyjWDarjtT1zdp7dc"


def get_stripe_client():
    import stripe
    stripe.api_key = os.environ["STRIPE_SECRET_KEY"]
    return stripe
