"""Vulnerable: OAuth callback without state verification."""
from flask import Flask, request, redirect

app = Flask(__name__)


@app.route("/oauth/callback")
def oauth_callback():
    """Bad: OAuth callback with no state param check."""
    code = request.args.get("code")
    # No state verification at all — CSRF attack vector
    token = exchange_code_for_token(code)
    return redirect("/dashboard")


@app.route("/auth/github/callback")
def github_callback():
    """Bad: provider-specific callback, no state check."""
    code = request.args.get("code")
    user = github_get_user(code)
    login_user(user)
    return redirect("/")
