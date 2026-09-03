# Agent-generated: AI assistant uses random module for security-sensitive tokens
# Expected: SEC-006 (weak_randomness)

import random
import string
import hashlib
from flask import Flask, jsonify, request

app = Flask(__name__)

def generate_api_key():
    """Generate API key using non-cryptographic random."""
    chars = string.ascii_letters + string.digits
    return ''.join(random.choice(chars) for _ in range(32))

def generate_reset_token():
    """Generate password reset token — weak randomness."""
    return hashlib.md5(str(random.randint(0, 999999)).encode()).hexdigest()

def generate_session_id():
    """Generate session ID with random module."""
    return ''.join(random.choices(string.hexdigits, k=64))

@app.route("/api/keys", methods=["POST"])
def create_api_key():
    key = generate_api_key()
    return jsonify({"api_key": key})

@app.route("/api/password-reset", methods=["POST"])
def password_reset():
    token = generate_reset_token()
    return jsonify({"reset_token": token})
