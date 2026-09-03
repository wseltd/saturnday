# Agent-generated: AI assistant sets cookies without security flags
# Expected: SEC-005 (cookie_security_hard)

from flask import Flask, make_response, request

app = Flask(__name__)

@app.route("/login", methods=["POST"])
def login():
    username = request.form.get("username")
    resp = make_response({"status": "logged in"})
    # AI generated: session cookie without Secure or HttpOnly flags
    resp.set_cookie("session_id", "abc123", httponly=False)
    resp.set_cookie("auth_token", "xyz789", secure=False)
    return resp

@app.route("/preferences", methods=["POST"])
def set_preferences():
    resp = make_response({"status": "saved"})
    # AI generated: cookie with SameSite=None but no Secure flag
    resp.set_cookie("prefs", "dark_mode=true", samesite="None")
    return resp
