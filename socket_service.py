from flask_socketio import SocketIO, join_room, emit
from flask import current_app
from models import User, db, TicketComentario
import jwt
from services.ticket_service import servicio_tickets # Reutilizamos el servicio de tickets

socketio = SocketIO(cors_allowed_origins="*")

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

@socketio.on('send_chat_message')
def handle_send_chat_message(data):
    """
    Manejador para cuando un agente envía un mensaje en el chat de un ticket.
    """
    token = data.get('token')
    room = data.get('room')
    ticket_id = data.get('ticket_id')
    ticket_type = data.get('ticket_type')
    message_text = data.get('message')

    if not all([token, room, ticket_id, ticket_type, message_text]):
        # No emitir error al cliente para no interrumpir su flujo, pero loguear.
        current_app.logger.error(f"Socket 'send_chat_message' recibió datos incompletos: {data}")
        return

    try:
        token_data = jwt.decode(token, current_app.config['SECRET_KEY'], algorithms=["HS256"])
        current_user = User.query.get(token_data['user_id'])
        if not current_user or current_user.rol not in ['admin', 'empleado']:
            current_app.logger.warning(f"Intento de envío de mensaje de chat por usuario no autorizado: {token_data.get('user_id')}")
            return
    except (jwt.ExpiredSignatureError, jwt.InvalidTokenError) as e:
        current_app.logger.error(f"Token inválido en 'send_chat_message': {e}")
        return

    # Guardar el comentario en la base de datos
    nuevo_comentario = servicio_tickets.crear_comentario(
        ticket_id=ticket_id,
        tipo_ticket=ticket_type,
        comentario_data={
            "comentario": message_text,
            "user_id": current_user.id,
            "es_admin": True # Los mensajes desde el panel siempre son de un admin/empleado
        }
    )

    if nuevo_comentario:
        # Emitir el nuevo mensaje a todos en la sala.
        emit('new_chat_message', {
            'ticket_id': ticket_id,
            'message': nuevo_comentario.to_dict() # Usamos el método to_dict() del modelo
        }, room=room)
    else:
        current_app.logger.error(f"No se pudo guardar el comentario para el ticket {ticket_type} {ticket_id}")


@socketio.on('location')
def on_location(data):
    """
    Handles a location update from the client.
    The data is expected to be a dictionary with 'lat' and 'lon' keys.
    e.g., {'lat': -34.6037, 'lon': -58.3816}
    """
    from services.municipios import handle_location_update
    response = handle_location_update(data)
    socketio.emit('message', response)
