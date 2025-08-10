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

from models import MunicipioTicket, PymeTicket

@socketio.on('send_chat_message')
def handle_send_chat_message(data):
    """
    Manejador para cuando un agente envía un mensaje en el chat de un ticket.
    Guarda el mensaje, lo emite por socket y envía notificaciones a otros canales (Email, SMS, WhatsApp).
    """
    token = data.get('token')
    room = data.get('room')
    ticket_id = data.get('ticket_id')
    ticket_type = data.get('ticket_type')
    message_text = data.get('message')

    if not all([token, room, ticket_id, ticket_type, message_text]):
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
        # 1. Emitir el nuevo mensaje a todos en la sala del chat en vivo.
        emit('new_chat_message', {
            'ticket_id': ticket_id,
            'message': nuevo_comentario.to_dict()
        }, room=room)

        # 2. Enviar notificaciones a otros canales (Email, SMS, WhatsApp)
        try:
            TicketModel = MunicipioTicket if ticket_type == "municipio" else PymeTicket
            ticket_obj = db.session.get(TicketModel, ticket_id)
            if ticket_obj:
                from services.email_service import (
                    enviar_email_ticket_novedad,
                    enviar_sms_ticket_novedad,
                    enviar_whatsapp_ticket_novedad,
                )
                mensaje_notificacion = f"Un agente ha respondido a tu ticket #{ticket_obj.nro_ticket}: \"{message_text}\""

                enviar_email_ticket_novedad(ticket_obj, mensaje_notificacion)
                enviar_sms_ticket_novedad(ticket_obj, mensaje_notificacion)
                if ticket_type == "municipio":
                    enviar_whatsapp_ticket_novedad(ticket_obj, mensaje_notificacion)

                current_app.logger.info(f"Notificaciones por respuesta de agente enviadas para ticket {ticket_id} (tipo {ticket_type}).")
            else:
                current_app.logger.error(f"No se encontró el ticket {ticket_id} (tipo {ticket_type}) para enviar notificaciones.")
        except Exception as e_notif:
            current_app.logger.error(f"Error durante el envío de notificaciones para respuesta de agente en ticket {ticket_id}: {e_notif}", exc_info=True)
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
