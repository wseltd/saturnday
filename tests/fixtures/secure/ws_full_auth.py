"""Secure: WebSocket with handshake auth + origin check."""
from flask_socketio import SocketIO, emit, disconnect

socketio = SocketIO()

ALLOWED_ORIGINS = ["https://app.example.com"]


@socketio.on("connect")
def handle_connect(auth):
    """Correct: auth on connect + origin check."""
    token = auth.get("token")
    if not verify_token(token):
        disconnect()
        return False
    origin = request.origin
    if origin not in ALLOWED_ORIGINS:
        disconnect()
        return False
    return True
