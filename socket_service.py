from flask_socketio import SocketIO

socketio = SocketIO()

def emit_ticket_update(data):
    socketio.emit('ticket_update', data)
