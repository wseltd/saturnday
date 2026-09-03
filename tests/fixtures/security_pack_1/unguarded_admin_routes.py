"""Fixture: SEC-002 — Flask admin routes with no authentication decorator.

This is intentionally vulnerable for Saturnday proof tests.
All three routes handle sensitive operations but have no @login_required,
@jwt_required, or equivalent auth guard.
"""

from flask import Flask, jsonify, request

app = Flask(__name__)


@app.route("/admin/users", methods=["GET"])
def list_users():
    """Admin-only: list all users — no auth guard."""
    users = [{"id": 1, "email": "alice@example.com"}, {"id": 2, "email": "bob@example.com"}]
    return jsonify(users)


@app.route("/admin/delete", methods=["POST"])
def delete_user():
    """Admin-only: delete a user — no auth guard."""
    user_id = request.json.get("user_id")
    return jsonify({"deleted": user_id})


@app.route("/api/payments", methods=["GET"])
def list_payments():
    """Sensitive: payment records — no auth guard."""
    payments = [{"id": "pay_001", "amount": 2000}]
    return jsonify(payments)
