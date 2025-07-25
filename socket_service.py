from flask_socketio import SocketIO, join_room

socketio = SocketIO()

def emit_ticket_update(data):
    socketio.emit('ticket_update', data)

@socketio.on('join')
def on_join(data):
    room = data['room']
    join_room(room)
    socketio.emit('status', {'msg': 'Conectado a la sala ' + room}, room=room)

@socketio.on('new_chat')
def on_new_chat(data):
    room = data['room']
    socketio.emit('new_chat', data, room=room)
