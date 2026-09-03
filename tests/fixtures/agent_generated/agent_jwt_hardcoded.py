# Agent-generated: common pattern where AI assistant embeds JWT secret directly
# Expected: SEC-001 (hardcoded_jwt)

from flask import Flask, jsonify, request
import jwt

app = Flask(__name__)

# AI assistant generated this "for development" — a real secret in code
JWT_SECRET = "super_secret_key_do_not_share_2024"

@app.route("/api/login", methods=["POST"])
def login():
    username = request.json.get("username")
    password = request.json.get("password")
    # Simplified auth check
    if username == "admin" and password == "password":
        token = jwt.encode({"user": username}, JWT_SECRET, algorithm="HS256")
        return jsonify({"token": token})
    return jsonify({"error": "Invalid credentials"}), 401

@app.route("/api/protected")
def protected():
    token = request.headers.get("Authorization", "").replace("Bearer ", "")
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=["HS256"])
        return jsonify({"message": f"Hello {payload['user']}"})
    except jwt.InvalidTokenError:
        return jsonify({"error": "Invalid token"}), 401
