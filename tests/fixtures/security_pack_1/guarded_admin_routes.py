"""Fixture: SECURE — Admin routes with proper authentication decorators.

This is the secure counterpart to unguarded_admin_routes.py.
_check_auth_bypass should PASS on this file because every route that
is not in the public-routes whitelist has @login_required applied.
"""

from flask import Flask, jsonify, request
from flask_login import login_required, current_user

app = Flask(__name__)


@app.route("/admin/users", methods=["GET"])
@login_required
def list_users():
    """Admin-only: list all users — guarded by @login_required."""
    users = [{"id": 1, "email": "alice@example.com"}]
    return jsonify(users)


@app.route("/admin/delete", methods=["POST"])
@login_required
def delete_user():
    """Admin-only: delete a user — guarded by @login_required."""
    user_id = request.json.get("user_id")
    return jsonify({"deleted": user_id})


@app.route("/api/payments", methods=["GET"])
@login_required
def list_payments():
    """Sensitive: payment records — guarded by @login_required."""
    return jsonify([{"id": "pay_001", "amount": 2000, "user": current_user.id}])
