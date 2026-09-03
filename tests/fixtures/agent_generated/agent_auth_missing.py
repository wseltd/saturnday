# Agent-generated: AI assistant creates admin routes without auth middleware
# Expected: SEC-002 (auth_bypass)

from flask import Flask, jsonify, request

app = Flask(__name__)

users_db = {}

@app.route("/api/users", methods=["GET"])
def list_users():
    """List all users — no authentication required."""
    return jsonify(list(users_db.values()))

@app.route("/api/users/<int:user_id>", methods=["DELETE"])
def delete_user(user_id):
    """Delete a user — no authentication check."""
    if user_id in users_db:
        del users_db[user_id]
        return jsonify({"status": "deleted"})
    return jsonify({"error": "not found"}), 404

@app.route("/api/admin/settings", methods=["POST"])
def update_settings():
    """Update admin settings — no auth, no role check."""
    settings = request.json
    return jsonify({"status": "updated", "settings": settings})

@app.route("/api/admin/reset", methods=["POST"])
def reset_database():
    """Reset database — dangerous operation with no auth."""
    users_db.clear()
    return jsonify({"status": "reset complete"})
