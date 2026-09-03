"""Vulnerable: Socket.IO handlers without auth."""
from flask_socketio import SocketIO, emit

socketio = SocketIO()


@socketio.on("join_room")
def handle_join(data):
    """Bad: no auth check, no origin validation."""
    room = data["room"]
    join_room(room)
    emit("joined", {"room": room}, room=room)


@socketio.on("send_message")
def handle_message(data):
    """Bad: no per-message auth."""
    emit("message", data, room=data["room"])
