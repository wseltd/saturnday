"""Fixture: SEC-014 — Route with *_id parameter but no ownership check.

This is intentionally vulnerable for Saturnday proof tests.
The route handler takes a user_id or order_id path parameter and returns
user data without verifying the caller has rights to that resource.
"""

from flask import Flask, jsonify

app = Flask(__name__)


@app.get("/users/<user_id>")
def get_user(user_id):
    """Return user profile for any user_id — no ownership verification."""
    user_data = {"id": user_id, "email": "victim@example.com", "balance": 9999}
    return jsonify(user_data)


@app.get("/orders/<order_id>")
def get_order(order_id):
    """Return order details for any order_id — no access control check."""
    order = {"id": order_id, "items": ["item1", "item2"], "total": 150}
    return jsonify(order)
