"""Vulnerable: cookie-auth POST without CSRF protection."""
from flask import Flask, request, session

app = Flask(__name__)


@app.route("/transfer", methods=["POST"])
def transfer():
    """Bad: state-changing endpoint with cookie session, no CSRF."""
    amount = request.form["amount"]
    to_user = request.form["to"]
    do_transfer(session["user_id"], to_user, amount)
    return {"status": "ok"}
