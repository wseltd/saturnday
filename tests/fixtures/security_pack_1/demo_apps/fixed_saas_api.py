"""Demo: fixed_saas_api.py — same API with all issues corrected.

Each fix is labelled with the rule it satisfies.
This is the secure counterpart to insecure_saas_api.py.
"""

import os
import secrets
import time

import jwt
from flask import Flask, jsonify, request, session
from flask_login import login_required, current_user

app = Flask(__name__)

# ── SEC-001 FIXED: Secret from env, no fallback literal ──────────────────────
JWT_SECRET = os.environ["JWT_SECRET"]
ALGORITHM = "HS256"
TOKEN_TTL = 3600

# ── SEC-FE-002 FIXED: Stripe key from env — no literal in source ─────────────
import stripe as _stripe
_stripe.api_key = os.environ["STRIPE_SECRET_KEY"]


# ── SEC-002 FIXED: All admin routes guarded by @login_required ───────────────
@app.route("/admin/users", methods=["GET"])
@login_required
def admin_list_users():
    return jsonify([{"id": current_user.id, "email": current_user.email}])


# ── SEC-003 FIXED: WebSocket on_connect validates JWT before accepting ────────
import socketio as _sio_module

_sio = _sio_module.Server()


@_sio.on("connect")
def on_connect(sid, environ):
    token = environ.get("HTTP_AUTHORIZATION", "").removeprefix("Bearer ")
    try:
        jwt.decode(token, JWT_SECRET, algorithms=[ALGORITHM])
    except jwt.InvalidTokenError:
        return False  # Reject connection — authenticate() equivalent
    print(f"authenticated connection: {sid}")


# ── SEC-009 FIXED: CSRF protection via flask-wtf CSRFProtect ─────────────────
from flask_wtf.csrf import CSRFProtect, validate_csrf  # noqa

csrf = CSRFProtect(app)


@app.post("/api/profile/update")
@login_required
def update_profile():
    data = request.json
    return jsonify({"updated": data})


# ── SEC-010 FIXED: @limiter.limit() applied to the login route ───────────────
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

limiter = Limiter(app=app, key_func=get_remote_address)


@app.post("/login")
@limiter.limit("10 per minute")
def login():
    email = request.json.get("email")
    password = request.json.get("password")
    # SEC-006 FIXED: secrets module for token generation
    session_token = secrets.token_urlsafe(32)
    return jsonify({"token": session_token})


# ── SEC-014 FIXED: Ownership check before returning user data ─────────────────
@app.get("/users/<user_id>")
@login_required
def get_user_profile(user_id):
    if str(current_user.id) != str(user_id):
        return jsonify({"error": "forbidden"}), 403
    return jsonify({"id": user_id, "email": current_user.email})


# ── SEC-016 FIXED: Price looked up server-side from product catalogue ─────────
PRODUCT_PRICES = {"prod_001": 2999, "prod_002": 999}


@app.post("/api/checkout")
@login_required
def checkout():
    product_id = request.json.get("product_id")
    # Price is never taken from request body — always server-authoritative.
    price = PRODUCT_PRICES.get(product_id)
    if price is None:
        return jsonify({"error": "unknown product"}), 400
    return jsonify({"charged": price, "product": product_id})


# ── SEC-004 FIXED: OAuth callback validates state parameter ──────────────────
def oauth_callback():
    code = request.args.get("code")
    state = request.args.get("state")
    expected_state = session.pop("oauth_state", None)
    if not state or state != expected_state:
        return jsonify({"error": "invalid state"}), 400
    return jsonify({"code": code})
