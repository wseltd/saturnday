"""Vulnerable: insecure cookie settings."""
from flask import make_response


def set_session_cookie(response, token):
    """Bad: SameSite=None without Secure, no HttpOnly."""
    response.set_cookie(
        "session",
        token,
        samesite="None",
    )
    return response


def set_refresh_token(response, refresh):
    """Bad: no HttpOnly on refresh token cookie."""
    response.set_cookie(
        "refresh_token",
        refresh,
        domain=".example.com",
    )
    return response
