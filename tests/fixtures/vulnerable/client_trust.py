"""Vulnerable: server accepts client-supplied authority fields."""
from flask import Flask, request

app = Flask(__name__)


@app.route("/api/score", methods=["POST"])
def update_score():
    """Bad: score from client request body."""
    score = request.json["score"]
    save_score(current_user.id, score)
    return {"status": "ok"}


@app.route("/api/user/role", methods=["PUT"])
def set_role():
    """Bad: role from client."""
    role = request.json.get("role")
    current_user.role = role
    current_user.save()
    return {"status": "ok"}
