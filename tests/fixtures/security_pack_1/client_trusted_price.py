"""Fixture: SEC-016 — Server trusts client-supplied price/amount.

This is intentionally vulnerable for Saturnday proof tests.
The checkout endpoint reads 'price' directly from request.json and uses
it to charge the customer, allowing a client to set their own price.
"""

from flask import Flask, request, jsonify

app = Flask(__name__)


@app.post("/api/checkout")
def checkout():
    """Process payment using client-supplied price — SEC-016.

    request.json["price"] is read directly and used to charge the customer.
    An attacker can send price=1 to buy anything for $0.01.
    """
    user_id = request.json.get("user_id")
    product_id = request.json.get("product_id")
    # SEC-016: trusts the price from the client body.
    price = request.json.get("price")

    charge_result = {"charged": price, "user": user_id, "product": product_id}
    return jsonify(charge_result)


@app.post("/api/upgrade")
def upgrade_plan():
    """Upgrade user to premium plan — trusts client-supplied amount."""
    # SEC-016: amount from request body used to charge.
    amount = request.json.get("amount")
    role = request.json.get("role")
    return jsonify({"charged": amount, "role": role})
