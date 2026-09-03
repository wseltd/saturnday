"""Tricky: intentionally public health endpoint — must NOT flag auth_bypass."""
from flask import Flask

app = Flask(__name__)


@app.route("/health")
def health():
    """Public health check — no auth needed."""
    return {"status": "ok"}


@app.route("/readiness")
def readiness():
    """Public readiness probe — no auth needed."""
    return {"status": "ready"}
