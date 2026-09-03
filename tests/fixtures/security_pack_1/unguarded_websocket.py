"""Fixture: SEC-003 — WebSocket handler with no auth on connect.

This is intentionally vulnerable for Saturnday proof tests.
The on_connect handler does not check credentials; any connection is accepted.
"""

import socketio

sio = socketio.Server()


@sio.on("connect")
def on_connect(sid, environ):
    """Accept all connections with no authentication check."""
    print(f"Client connected: {sid}")


@sio.on("message")
def on_message(sid, data):
    """Handle messages from an unverified sender."""
    sio.emit("response", {"echo": data}, to=sid)


@sio.on("disconnect")
def on_disconnect(sid):
    print(f"Client disconnected: {sid}")
