# Agent-generated: AI assistant creates state-changing POST routes without CSRF
# Expected: SEC-009 (csrf_state_change)

from flask import Flask, jsonify, request

app = Flask(__name__)

@app.route("/api/transfer", methods=["POST"])
def transfer_funds():
    """Transfer money — no CSRF token validation."""
    data = request.json
    from_account = data.get("from")
    to_account = data.get("to")
    amount = data.get("amount")
    # Process transfer without CSRF protection
    return jsonify({"status": "transferred", "amount": amount})

@app.route("/api/settings", methods=["POST"])
def update_settings():
    """Update user settings — no CSRF protection."""
    settings = request.form.to_dict()
    return jsonify({"status": "updated"})

@app.route("/api/password", methods=["POST"])
def change_password():
    """Change password — no CSRF token check."""
    old_pw = request.form.get("old_password")
    new_pw = request.form.get("new_password")
    return jsonify({"status": "password changed"})
