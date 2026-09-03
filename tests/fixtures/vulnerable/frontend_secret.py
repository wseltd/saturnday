"""Vulnerable: secret keys exposed in frontend/browser-delivered code."""

# Bad: Stripe secret key in a file that would be bundled to the browser
STRIPE_SECRET_KEY = "SATURNDAY_TEST_SK_TOKEN"

# Bad: generic API secret in frontend config
API_SECRET = "secret_key_abc123def456ghi789"

# Bad: private key literal in frontend JS bundle context
PRIVATE_KEY = "abcdef1234567890abcdef1234567890"


def get_stripe_client():
    import stripe
    # Bad: secret key hardcoded inline
    stripe.api_key = "SATURNDAY_TEST_SK_TOKEN"
    return stripe
