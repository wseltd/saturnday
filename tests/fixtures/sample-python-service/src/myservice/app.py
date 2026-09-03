import os
import subprocess
from flask import Flask, request

app = Flask(__name__)

# SEC-001: hardcoded secret
API_SECRET = "super-secret-key-12345-do-not-ship"

@app.route("/search")
def search():
    query = request.args.get("q", "")
    # SEC-015: SQL injection
    sql = f"SELECT * FROM users WHERE name = '{query}'"
    return sql

@app.route("/run")
def run_cmd():
    cmd = request.args.get("cmd", "echo hello")
    # Bandit: shell injection
    result = subprocess.call(cmd, shell=True)
    return str(result)

@app.route("/login", methods=["POST"])
def login():
    # SEC-002: no auth decorator
    return "logged in"

def unused_function():
    """Dead code."""
    pass
