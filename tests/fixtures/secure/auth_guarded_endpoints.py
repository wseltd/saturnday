"""Secure: sensitive endpoints with proper auth guards."""
from flask import Flask, jsonify, request
from flask_login import login_required, current_user

app = Flask(__name__)


@app.route("/api/admin/users", methods=["GET"])
@login_required
def list_all_users():
    """Good: admin endpoint protected by login_required."""
    return jsonify(get_all_users())


@app.route("/api/payments/refund", methods=["POST"])
@login_required
def process_refund():
    """Good: payment endpoint with auth guard."""
    amount = request.json.get("amount")
    return jsonify({"refunded": amount})


@app.route("/health")
def health():
    """OK: public health endpoint."""
    return {"status": "ok"}
