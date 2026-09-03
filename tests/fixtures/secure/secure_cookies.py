"""Secure: proper cookie settings."""
from flask import make_response


def set_session_cookie(response, token):
    """Correct: all flags set."""
    response.set_cookie(
        "session",
        token,
        secure=True,
        httponly=True,
        samesite="Lax",
    )
    return response


def set_non_sensitive_cookie(response):
    """Non-sensitive cookie — should not flag."""
    response.set_cookie("theme", "dark")
    return response
