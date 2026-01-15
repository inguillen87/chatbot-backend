from flask_socketio import SocketIO, emit, join_room, leave_room
from flask import request, current_app
import logging
import json

logger = logging.getLogger(__name__)

socketio = SocketIO()

def init_socketio(app):
    """
    Initializes the SocketIO instance with the Flask app.
    """
    socketio.init_app(
        app,
        cors_allowed_origins="*",
        async_mode='eventlet',
        message_queue=app.config.get('SOCKETIO_MESSAGE_QUEUE'),
        manage_session=False
    )
    return socketio

@socketio.on('connect')
def handle_connect():
    """
    Handles a new client connection.
    """
    logger.info(f"Client connected: {request.sid}")
    emit('status', {'msg': 'Connected'})

@socketio.on('disconnect')
def handle_disconnect():
    """
    Handles client disconnection.
    """
    logger.info(f"Client disconnected: {request.sid}")

@socketio.on('join')
def on_join(data):
    """
    Joins a client to a specific room (e.g., 'municipio_1', 'ticket_123').
    """
    room = data.get('room')
    if room:
        join_room(room)
        logger.info(f"Client {request.sid} joined room: {room}")
        emit('status', {'msg': f'Joined room {room}'}, room=room)

@socketio.on('leave')
def on_leave(data):
    """
    Removes a client from a room.
    """
    room = data.get('room')
    if room:
        leave_room(room)
        logger.info(f"Client {request.sid} left room: {room}")
        emit('status', {'msg': f'Left room {room}'}, room=room)

def emit_new_ticket(ticket_data):
    """
    Emits a 'new_ticket' event to the relevant room (e.g., municipio_ID).
    Used when a new ticket is created (via WhatsApp, Web, etc.).
    """
    try:
        municipio_id = ticket_data.get('municipio_id')
        if municipio_id:
            room_name = f"municipio_{municipio_id}"
            socketio.emit('new_ticket', ticket_data, room=room_name)
            logger.info(f"Emitted new_ticket to {room_name}")

        # Also emit to a global admin room if applicable
        socketio.emit('new_ticket_global', ticket_data, room='super_admin')

    except Exception as e:
        logger.error(f"Error emitting new_ticket: {e}")

def emit_ticket_update(ticket_data):
    """
    Emits a 'ticket_update' event when a ticket status changes.
    """
    try:
        municipio_id = ticket_data.get('municipio_id')
        ticket_id = ticket_data.get('id')

        # Notify the municipality dashboard
        if municipio_id:
            room_name = f"municipio_{municipio_id}"
            socketio.emit('ticket_update', ticket_data, room=room_name)

        # Notify the specific ticket room (for live chat views)
        if ticket_id:
            ticket_room = f"ticket_{ticket_id}"
            socketio.emit('ticket_update', ticket_data, room=ticket_room)

    except Exception as e:
        logger.error(f"Error emitting ticket_update: {e}")

def emit_ticket_comment(comment_data):
    """
    Emits a 'ticket_comment' event when a new message/comment is added.
    """
    try:
        ticket_id = comment_data.get('ticket_id')
        if ticket_id:
            room_name = f"ticket_{ticket_id}"
            socketio.emit('ticket_comment', comment_data, room=room_name)
            logger.info(f"Emitted ticket_comment to {room_name}")
    except Exception as e:
        logger.error(f"Error emitting ticket_comment: {e}")
