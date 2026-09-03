"""Secure: CSRF protection with double-submit cookie."""
from flask import Flask, request, session
from flask_wtf.csrf import CSRFProtect

app = Flask(__name__)
csrf = CSRFProtect(app)


@app.route("/transfer", methods=["POST"])
def transfer():
    """Correct: CSRFProtect applied globally."""
    amount = request.form["amount"]
    to_user = request.form["to"]
    do_transfer(session["user_id"], to_user, amount)
    return {"status": "ok"}
