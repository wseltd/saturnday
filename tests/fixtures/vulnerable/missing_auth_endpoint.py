"""Vulnerable: sensitive endpoints with no auth guard."""
from flask import Flask, jsonify, request

app = Flask(__name__)


@app.route("/api/admin/users", methods=["GET"])
def list_all_users():
    """Bad: admin endpoint with no auth decorator."""
    return jsonify(get_all_users())


@app.route("/api/payments/refund", methods=["POST"])
def process_refund():
    """Bad: payment mutation with no auth check."""
    amount = request.json.get("amount")
    return jsonify({"refunded": amount})


@app.route("/api/user/delete", methods=["DELETE"])
def delete_user():
    """Bad: destructive action with no auth."""
    user_id = request.json.get("user_id")
    do_delete(user_id)
    return jsonify({"deleted": True})


@app.route("/health")
def health():
    """OK: public health endpoint."""
    return {"status": "ok"}
