"""Vulnerable: WebSocket handlers with no auth or handshake verification."""
from flask_socketio import SocketIO

socketio = SocketIO()


@socketio.on("connect")
def handle_connect():
    """Bad: WebSocket connect with no auth check."""
    pass


@socketio.on("message")
def handle_message(data):
    """Bad: message handler with no token verification."""
    process(data)


@socketio.on("admin_action")
def handle_admin(data):
    """Bad: admin socket event, no auth."""
    do_admin_action(data)
