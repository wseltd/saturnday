"""Fixture: SEC-010 — Login endpoint imports rate limiter but never applies it.

This is intentionally vulnerable for Saturnday proof tests.
flask_limiter is imported and Limiter is instantiated, but no route
decorator calls limit(). The limiter object is defined but idle.
"""

import os
from flask import Flask, request, jsonify
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

app = Flask(__name__)

# Limiter is created but never wired to any route.
limiter = Limiter(
    app=app,
    key_func=get_remote_address,
)


@app.post("/login")
def login():
    """Password check with no throttling applied."""
    email = request.json.get("email")
    password = request.json.get("password")
    if email == "admin@example.com" and password == "admin":
        return jsonify({"token": "some_jwt_here"})
    return jsonify({"error": "invalid credentials"}), 401
