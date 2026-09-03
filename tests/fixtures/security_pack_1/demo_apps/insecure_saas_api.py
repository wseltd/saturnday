"""Demo: insecure_saas_api.py — single-file "AI-built SaaS API" with all failure classes.

Each flaw is labelled with the Saturnday rule ID that catches it.
This file is intentionally vulnerable for Saturnday proof tests.
"""

import json
import random
import time
from flask import Flask, jsonify, request, session

app = Flask(__name__)

# ── SEC-001: Hardcoded JWT signing secret (assignment to JWT_SECRET literal) ──
JWT_SECRET = "hardcoded_jwt_signing_secret_do_not_ship"
ALGORITHM = "HS256"

# ── SEC-FE-002 proxy: Stripe secret in a config dict serialised to API response ──
# (SEC-FE-002 fires on sk_live_ literal in any source file)
PAYMENT_CONFIG = {
    "secret_key": "SATURNDAY_TEST_SK_TOKEN",
    "currency": "usd",
}


# ── SEC-002: Missing auth on /admin/users — not in public-routes whitelist ────
@app.route("/admin/users", methods=["GET"])
def admin_list_users():
    return jsonify([{"id": 1, "email": "admin@example.com"}])


# ── SEC-003: WebSocket handler on_connect with no auth ───────────────────────
import socketio as _sio_module

_sio = _sio_module.Server()


@_sio.on("connect")
def on_connect(sid, environ):
    """Accept all connections without checking credentials."""
    print(f"connection accepted: {sid}")


# ── SEC-009: POST route with session (cookie auth) and no anti-forgery token ──
@app.post("/api/profile/update")
def update_profile():
    user_id = session.get("user_id")
    data = request.json
    return jsonify({"updated": data})


# ── SEC-010: flask_limiter imported but no route has throttling applied ────────
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

limiter = Limiter(app=app, key_func=get_remote_address)


@app.post("/login")
def login():
    """Password check with no throttle applied to this route."""
    email = request.json.get("email")
    password = request.json.get("password")
    # SEC-006 bonus: random.randint used for session token generation
    token = random.randint(100000, 999999)  # noqa — insecure token generation
    return jsonify({"token": str(token)})


# ── SEC-014: IDOR — /users/<user_id> with no ownership check ─────────────────
@app.get("/users/<user_id>")
def get_user_profile(user_id):
    """Return any user's profile without access control."""
    return jsonify({"id": user_id, "email": "victim@example.com"})


# ── SEC-016: Client-trusted price on checkout ─────────────────────────────────
@app.post("/api/checkout")
def checkout():
    """Charge the amount the client claims."""
    price = request.json.get("price")
    amount = request.json.get("amount")
    return jsonify({"charged": price, "amount": amount})


# ── SEC-004: OAuth callback with no state validation ─────────────────────────
def oauth_callback():
    code = request.args.get("code")
    # state is never read or validated.
    return jsonify({"code": code})
