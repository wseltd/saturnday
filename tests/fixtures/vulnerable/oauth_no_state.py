"""Vulnerable: OAuth callback without state validation."""
from flask import Flask, request

app = Flask(__name__)


@app.route("/auth/callback")
def oauth_callback():
    """Bad: no state validation, auto-links on email match."""
    code = request.args["code"]
    token = exchange_code(code)
    profile = get_profile(token)
    # Auto-link on email match without verification
    user = find_or_create_by_email(profile["email"])
    redirect_uri = request.args.get("redirect_uri", "/")
    return redirect(redirect_uri)
