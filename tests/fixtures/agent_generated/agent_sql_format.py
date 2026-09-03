# Agent-generated: AI assistant uses f-strings for SQL queries
# Expected: SEC-015 (sql_injection)

from flask import Flask, jsonify, request
import sqlite3

app = Flask(__name__)
DB_PATH = "app.db"

def get_db():
    return sqlite3.connect(DB_PATH)

@app.route("/api/users/search")
def search_users():
    query = request.args.get("q", "")
    db = get_db()
    # AI generated: direct f-string interpolation in SQL
    cursor = db.execute(f"SELECT * FROM users WHERE name LIKE '%{query}%'")
    results = cursor.fetchall()
    return jsonify(results)

@app.route("/api/users/<username>")
def get_user(username):
    db = get_db()
    # AI generated: format string in SQL
    cursor = db.execute("SELECT * FROM users WHERE username = '%s'" % username)
    return jsonify(cursor.fetchone())

@app.route("/api/orders")
def get_orders():
    sort_by = request.args.get("sort", "created_at")
    db = get_db()
    # AI generated: user-controlled ORDER BY
    cursor = db.execute(f"SELECT * FROM orders ORDER BY {sort_by}")
    return jsonify(cursor.fetchall())
