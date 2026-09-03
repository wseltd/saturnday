"""Vulnerable: auth endpoints with no rate limiting."""
from flask import Flask, request, jsonify

app = Flask(__name__)


@app.route("/api/login", methods=["POST"])
def login():
    """Bad: login endpoint with no rate limit."""
    username = request.json.get("username")
    password = request.json.get("password")
    user = authenticate(username, password)
    if user:
        return jsonify({"token": generate_token(user)})
    return jsonify({"error": "Invalid credentials"}), 401


@app.route("/api/password-reset", methods=["POST"])
def password_reset():
    """Bad: password reset with no rate limit."""
    email = request.json.get("email")
    send_reset_email(email)
    return jsonify({"sent": True})


@app.route("/api/verify-token", methods=["POST"])
def verify_token_endpoint():
    """Bad: token verification with no rate limit."""
    token = request.json.get("token")
    valid = check_token(token)
    return jsonify({"valid": valid})
