"""Integration test app — intentionally has both secure and vulnerable endpoints.

Used by test_security_integration.py to validate structural controls.
This is NOT a real app — it's a test fixture with deliberate flaws.
"""
import hashlib
import json
import logging
import os
import random
import secrets
import time

import jwt

logger = logging.getLogger(__name__)

# ── Config ──────────────────────────────────────────────────────────────────

JWT_SECRET = os.environ.get("JWT_SECRET", "insecure-fallback-key")  # SEC-001: fallback
ALGORITHM = "HS256"
REFRESH_SECRET = "refresh-secret-hardcoded"  # SEC-001: hardcoded


# ── Auth helpers ────────────────────────────────────────────────────────────

def create_access_token(user_id):
    """SEC-012 FAIL: no exp claim."""
    return jwt.encode({"user_id": user_id}, JWT_SECRET)


def create_refresh_token(user_id):
    """Correct: has expiry."""
    return jwt.encode({"user_id": user_id, "exp": time.time() + 86400}, REFRESH_SECRET, algorithm=ALGORITHM)


def verify_access_token(token):
    """SEC-008 FAIL: no algorithms pinned."""
    return jwt.decode(token, JWT_SECRET)


def verify_refresh_token(token):
    """Correct: algorithms pinned."""
    return jwt.decode(token, REFRESH_SECRET, algorithms=["HS256"])


# ── Route handlers (Flask-style) ────────────────────────────────────────────

# Simulated framework decorators for static analysis
class _App:
    def route(self, path, **kw):
        def decorator(fn):
            fn._route = path
            fn._methods = kw.get("methods", ["GET"])
            return fn
        return decorator

    def get(self, path):
        def decorator(fn):
            fn._route = path
            return fn
        return decorator

    def post(self, path):
        def decorator(fn):
            fn._route = path
            return fn
        return decorator


app = _App()


# ── Public routes (should NOT flag auth_bypass) ─────────────────────────────

@app.route("/health")
def health():
    return {"status": "ok"}


@app.route("/login", methods=["POST"])
def login(request):
    """SEC-017 FAIL: differentiated error messages."""
    user = find_user(request.json["username"])
    if not user:
        return {"error": "User not found"}, 404
    if not check_password(user, request.json["password"]):
        logger.info("login failed")  # SEC-OPS-001 FAIL: bare logging
        return {"error": "Wrong password"}, 401
    token = create_access_token(user["id"])
    return {"token": token}


# ── Protected routes (SEC-002 FAIL: no auth) ────────────────────────────────

@app.get("/api/users/<user_id>")
def get_user(user_id):
    """SEC-002 FAIL: no auth decorator. SEC-014 FAIL: no ownership check."""
    return get_user_data(user_id)


@app.post("/api/admin/settings")
def update_settings():
    """SEC-002 FAIL: admin route without auth."""
    return {"status": "updated"}


# ── Cookie handling ─────────────────────────────────────────────────────────

def set_session_cookie(response, token):
    """SEC-005 FAIL: SameSite=None without Secure, no HttpOnly."""
    response.set_cookie("session", token, samesite="None")


def set_refresh_cookie(response, token):
    """SEC-011 FAIL: missing explicit SameSite, broad domain."""
    response.set_cookie("refresh_token", token, domain=".example.com")


# ── CSRF-vulnerable endpoint ───────────────────────────────────────────────

from flask import session  # noqa: for cookie auth detection

@app.route("/api/transfer", methods=["POST"])
def transfer(request):
    """SEC-009 FAIL: cookie-backed state change without CSRF."""
    session["last_transfer"] = time.time()
    return {"status": "transferred"}


# ── OAuth callback ──────────────────────────────────────────────────────────

@app.route("/auth/callback")
def oauth_callback(request):
    """SEC-004 FAIL: no state validation, redirect from input."""
    code = request.args["code"]
    token = exchange_code(code)
    profile = get_profile(token)
    user = find_or_create_by_email(profile["email"])  # unsafe auto-link
    redirect_uri = request.args.get("redirect_uri", "/")  # SEC-004: from input
    return redirect(redirect_uri)


# ── WebSocket handlers ──────────────────────────────────────────────────────

class socketio:
    @staticmethod
    def on(event):
        def decorator(fn):
            fn._ws_event = event
            return fn
        return decorator


@socketio.on("join_room")
def ws_join(data):
    """SEC-003 FAIL: no auth, no origin check."""
    room = data["room"]
    join_room(room)


# ── Rate limiting ──────────────────────────────────────────────────────────

from flask_limiter import Limiter  # noqa: imported but never applied (SEC-010)


# ── Token lifecycle ─────────────────────────────────────────────────────────

def logout(request):
    """SEC-007 FAIL: no token invalidation."""
    return {"message": "Logged out"}


def reset_password(request):
    """SEC-007 FAIL: no token invalidation."""
    new_password = request.json["new_password"]
    user = get_user(request.user_id)
    user["password"] = hash_password(new_password)
    save_user(user)
    return {"message": "Password reset"}


# ── Weak randomness ────────────────────────────────────────────────────────

def generate_invite_code():
    """SEC-006 FAIL: random for invite token."""
    return str(random.randint(100000, 999999))


def generate_reset_token():
    """Correct: uses secrets."""
    return secrets.token_urlsafe(32)


# ── Client-trusted logic ───────────────────────────────────────────────────

@app.route("/api/score", methods=["POST"])
def update_score(request):
    """SEC-016 FAIL: score from client."""
    score = request.json["score"]
    save_score(current_user_id, score)
    return {"status": "ok"}


# ── SQL injection ──────────────────────────────────────────────────────────

def search_users(query):
    """SEC-015 FAIL: f-string SQL."""
    sql = f"SELECT * FROM users WHERE name LIKE '%{query}%'"
    return execute(sql)


# ── XSS ────────────────────────────────────────────────────────────────────

from markupsafe import Markup  # noqa

def render_greeting(name):
    """SEC-013 FAIL: Markup with user data."""
    return Markup(f"<h1>Hello {name}</h1>")


# ── Stubs for functions referenced above ────────────────────────────────────

def find_user(username): pass
def check_password(user, pw): pass
def get_user_data(uid): pass
def exchange_code(code): pass
def get_profile(token): pass
def find_or_create_by_email(email): pass
def redirect(url): pass
def join_room(room): pass
def hash_password(pw): pass
def save_user(u): pass
def save_score(uid, s): pass
def execute(sql): pass
current_user_id = 1
