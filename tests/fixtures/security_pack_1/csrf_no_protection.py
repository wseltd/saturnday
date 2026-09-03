"""Fixture: SEC-009 — POST endpoint with cookie-based auth and no anti-forgery tokens.

This is intentionally vulnerable for Saturnday proof tests.
The file uses session (cookie-based auth indicator) and has state-changing
POST route decorators, but no synchronizer token or double-submit cookie anywhere.
"""

from flask import Flask, request, jsonify, session

app = Flask(__name__)
app.secret_key = "flask-session-secret"


@app.post("/api/account/email")
def update_email():
    """Change user email — cookie-backed, no anti-forgery token."""
    user_id = session.get("user_id")
    if not user_id:
        return jsonify({"error": "not authenticated"}), 401
    new_email = request.json.get("email")
    return jsonify({"updated": new_email})


@app.post("/api/account/password")
def change_password():
    """Change user password — cookie-backed, no anti-forgery token."""
    user_id = session.get("user_id")
    if not user_id:
        return jsonify({"error": "not authenticated"}), 401
    new_password = request.json.get("password")
    return jsonify({"ok": True})
