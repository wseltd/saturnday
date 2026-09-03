"""Fixture: SECURE — JWT with env-sourced secret, expiry set, httponly cookies.

This is the secure counterpart to jwt_fallback_secret.py and jwt_no_expiry.py.
- _check_hardcoded_jwt: PASS — os.environ['KEY'] (no fallback literal).
- _check_token_expiry: PASS — exp claim present in context window of jwt.encode call.
- _check_cookie_security_hard: PASS — httponly=True, secure=True, samesite='Strict'.
"""

import os
import time
import jwt
from flask import Flask, make_response

app = Flask(__name__)

# Secure: raises KeyError if missing — no literal fallback.
SECRET = os.environ["JWT_SECRET"]
ALGORITHM = "HS256"
TOKEN_TTL_SECONDS = 3600  # 1 hour


def create_token(user_id: int) -> str:
    """Create a JWT with a proper exp claim — SEC-012 compliant."""
    now = int(time.time())
    return jwt.encode(
        {"sub": str(user_id), "iat": now, "exp": now + TOKEN_TTL_SECONDS},
        SECRET,
        algorithm=ALGORITHM,
    )


def verify_token(token: str) -> dict:
    """Decode JWT with pinned algorithm list — SEC-008 compliant."""
    return jwt.decode(token, SECRET, algorithms=[ALGORITHM])


@app.post("/auth/token")
def issue_token():
    """Issue a refresh token with all security flags — SEC-005 compliant."""
    token = create_token(user_id=1)
    response = make_response({"ok": True})
    response.set_cookie(
        "refresh_token",
        token,
        httponly=True,
        secure=True,
        samesite="Strict",
        max_age=TOKEN_TTL_SECONDS,
    )
    return response
