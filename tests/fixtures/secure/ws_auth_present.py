"""Secure: WebSocket handlers with auth verification."""
from flask_socketio import SocketIO, disconnect
from flask_login import current_user

socketio = SocketIO()


@socketio.on("connect")
def handle_connect():
    """Good: auth check on connect."""
    token = request.headers.get("Authorization")
    if not token or not verify_token(token):
        disconnect()
        return False


@socketio.on("message")
def handle_message(data):
    """Good: user must be authenticated."""
    if not current_user.is_authenticated:
        disconnect()
        return
    process(data)
