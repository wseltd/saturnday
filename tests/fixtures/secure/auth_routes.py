"""Secure: all routes with auth middleware."""
from flask import Flask, jsonify
from flask_login import login_required

app = Flask(__name__)


@app.route("/api/users/<user_id>")
@login_required
def get_user(user_id):
    """Correct: auth decorator present."""
    return jsonify(get_user_data(user_id))


@app.route("/health")
def health():
    """OK: intentionally public."""
    return {"status": "ok"}
