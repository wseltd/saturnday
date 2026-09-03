"""Vulnerable: rate limiter with in-memory store."""
from flask import Flask
from flask_limiter import Limiter

app = Flask(__name__)
limiter = Limiter(app, default_limits=["100 per hour"])
