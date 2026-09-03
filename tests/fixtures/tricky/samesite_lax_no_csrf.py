"""Tricky: SameSite=Lax without CSRF token — must fire SEC-009."""
from flask import Flask, request, session

app = Flask(__name__)
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"


@app.route("/update-profile", methods=["POST"])
def update_profile():
    """SameSite=Lax alone is not sufficient CSRF protection per OWASP."""
    session["name"] = request.form["name"]
    return {"status": "ok"}
