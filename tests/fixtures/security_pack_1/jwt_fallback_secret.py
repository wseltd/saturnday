"""Fixture: SEC-001 — JWT secret with a hardcoded fallback default.

This is intentionally vulnerable for Saturnday proof tests.
os.environ.get with a string fallback is flagged by _check_hardcoded_jwt
as an env_fallback_secret because the fallback embeds a literal secret
into source code.
"""

import os
import jwt

# SEC-001: os.environ.get with a non-None literal fallback for a JWT secret.
SECRET = os.environ.get("JWT_SECRET", "default-secret-change-me")

ALGORITHM = "HS256"


def create_token(user_id: int) -> str:
    payload = {"sub": str(user_id)}
    return jwt.encode(payload, SECRET, algorithm=ALGORITHM)


def verify_token(token: str) -> dict:
    return jwt.decode(token, SECRET, algorithms=[ALGORITHM])
