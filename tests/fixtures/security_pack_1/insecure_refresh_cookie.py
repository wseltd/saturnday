"""Fixture: SEC-005 — Refresh token cookie set without httponly/secure/samesite.

This is intentionally vulnerable for Saturnday proof tests.
response.set_cookie is called for a 'refresh_token' with none of
httponly=True, secure=True, or samesite='Strict' flags present.
"""

from flask import Flask, make_response

app = Flask(__name__)


@app.post("/auth/refresh")
def refresh():
    """Issue a new refresh token cookie with no security flags.

    SEC-005: set_cookie on 'refresh_token' without httponly, secure, or samesite.
    """
    token = "some_opaque_refresh_token_value"
    response = make_response({"ok": True})
    # Missing httponly=True, secure=True, samesite="Strict"
    response.set_cookie("refresh_token", token)
    return response
