"""Vulnerable: weak JWT configuration patterns."""
import jwt
import os

# Bad: hardcoded JWT secret (also caught by SEC-001 / hardcoded_jwt)
JWT_SECRET = "mysecretkey"

# Bad: no expiry set
def create_token_no_expiry(user_id):
    payload = {"sub": user_id}
    return jwt.encode(payload, JWT_SECRET, algorithm="HS256")


# Bad: far-future expiry (essentially no expiry)
def create_token_unsafe_expiry(user_id):
    from datetime import datetime, timedelta
    payload = {
        "sub": user_id,
        "exp": datetime.utcnow() + timedelta(days=3650),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm="HS256")


# Bad: refresh token in a non-httponly, non-secure cookie
def set_refresh_cookie(response, token):
    response.set_cookie("refresh_token", token)  # no httponly=True, no secure=True


# Bad: default fallback secret
JWT_SIGNING_KEY = os.environ.get("JWT_SECRET", "fallback_default_secret")
