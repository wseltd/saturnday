"""Tricky: __Host- prefix with Path=/ — MUST PASS."""
from flask import make_response


def set_host_cookie(response, token):
    """Correct: __Host- prefix requires Secure, no Domain, Path=/."""
    response.set_cookie(
        "__Host-session",
        token,
        secure=True,
        httponly=True,
        samesite="Strict",
        path="/",
    )
    return response
