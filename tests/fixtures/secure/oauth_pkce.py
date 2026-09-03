"""Secure: full OAuth PKCE + state flow."""
import secrets
from flask import Flask, request, session

app = Flask(__name__)


@app.route("/auth/login")
def oauth_login():
    state = secrets.token_urlsafe(32)
    code_verifier = secrets.token_urlsafe(64)
    session["oauth_state"] = state
    session["code_verifier"] = code_verifier
    code_challenge = compute_challenge(code_verifier)
    return redirect(build_auth_url(state=state, code_challenge=code_challenge))


@app.route("/auth/callback")
def oauth_callback():
    """Correct: validates state, uses PKCE, no auto-link."""
    if request.args["state"] != session.pop("oauth_state", None):
        return "Invalid state", 403
    code_verifier = session.pop("code_verifier")
    token = exchange_code(request.args["code"], code_verifier=code_verifier)
    profile = get_profile(token)
    user = verify_and_link_account(profile)
    return redirect("/dashboard")
