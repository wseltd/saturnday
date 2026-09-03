# Agent-generated: AI assistant renders user input without escaping
# Expected: SEC-013 (xss_check)

from flask import Flask, request, make_response, Markup
from jinja2 import Template

app = Flask(__name__)

@app.route("/search")
def search():
    query = request.args.get("q", "")
    # AI generated: Markup() wrapping user input
    html = Markup(f"<h1>Search results for: {query}</h1>")
    return make_response(html)

@app.route("/profile/<username>")
def profile(username):
    # AI generated: |safe filter in Jinja template with user data
    template = Template("{{ name | safe }}")
    return template.render(name=username)

@app.route("/error")
def error_page():
    msg = request.args.get("message", "Unknown error")
    # AI generated: Markup() wrapping error message
    return Markup(f"<div class='error'>{msg}</div>"), 400
