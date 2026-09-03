"""Fixture: SEC-004 — OAuth callback handler with no state validation.

This is intentionally vulnerable for Saturnday proof tests.
The oauth_callback function reads 'code' from the request but never
generates, stores, or compares a 'state' parameter, making it
vulnerable to CSRF on the OAuth flow.
"""

import requests
from flask import Flask, request, redirect, session

app = Flask(__name__)
app.secret_key = "flask-session-key"

GITHUB_CLIENT_ID = "github_oauth_client_id_here"
GITHUB_CLIENT_SECRET = "github_oauth_client_secret_here"


def oauth_callback():
    """GitHub OAuth callback — reads code, ignores state.

    SEC-004: No state generation, storage, or validation present.
    """
    code = request.args.get("code")
    # 'state' is never checked — CSRF on the OAuth flow.

    token_response = requests.post(
        "https://github.com/login/oauth/access_token",
        data={
            "client_id": GITHUB_CLIENT_ID,
            "client_secret": GITHUB_CLIENT_SECRET,
            "code": code,
        },
        headers={"Accept": "application/json"},
    )
    access_token = token_response.json().get("access_token")
    session["github_token"] = access_token
    return redirect("/dashboard")
