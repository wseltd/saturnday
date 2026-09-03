"""Fixture: SEC-007 — logout handler with no token revocation step.

This is intentionally vulnerable for Saturnday proof tests.
The logout function verifies the JWT signature but takes no action to
prevent the token from being reused — no blocklist, no session deletion.
"""

import os
import jwt
from flask import Flask, request, jsonify

app = Flask(__name__)
SECRET = os.environ["JWT_SECRET"]


def logout(token: str) -> dict:
    """Log out: verify JWT signature only.

    The caller's token remains usable after this function returns.
    """
    try:
        payload = jwt.decode(token, SECRET, algorithms=["HS256"])
        return {"status": "logged_out", "user": payload["sub"]}
    except jwt.InvalidTokenError:
        return {"error": "invalid token"}
