"""Fixture: SEC-012 — JWT created without an exp (expiry) claim.

This is intentionally vulnerable for Saturnday proof tests.
jwt.encode is called with a payload that contains no 'exp', 'expires',
or 'expiresIn' key, so the token is valid indefinitely.
"""

import os
import jwt

SECRET = os.environ["JWT_SECRET"]
ALGORITHM = "HS256"


def create_access_token(user_id: int) -> str:
    """Create a JWT with no expiry."""
    # Payload deliberately lacks exp — SEC-012.
    return jwt.encode({"sub": str(user_id), "iat": 1700000000}, SECRET, algorithm=ALGORITHM)


def verify_token(token: str) -> dict:
    return jwt.decode(token, SECRET, algorithms=[ALGORITHM])
