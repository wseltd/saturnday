"""Vulnerable: Flask routes without auth decorators."""
from flask import Flask, jsonify

app = Flask(__name__)


@app.route("/api/users/<user_id>")
def get_user(user_id):
    """Bad: no auth on sensitive route."""
    return jsonify(get_user_data(user_id))


@app.route("/api/admin/settings", methods=["POST"])
def update_settings():
    """Bad: admin route without auth."""
    return jsonify({"status": "updated"})


@app.route("/health")
def health():
    """OK: public health endpoint."""
    return {"status": "ok"}
