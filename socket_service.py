from flask_socketio import SocketIO, join_room, emit
from flask import current_app, request
from config import ALLOWED_ORIGINS
from models import User, db, TicketComentario
import jwt
from services.ticket_service import servicio_tickets # Reutilizamos el servicio de tickets
from services.tts_orchestrator import generar_audio
from utils.response_utils import ensure_buttons_compatibility

socketio = SocketIO(
    cors_allowed_origins=ALLOWED_ORIGINS,
    cookie=True,
    async_mode="eventlet"
)

def emit_ticket_update(data):
    socketio.emit('ticket_update', data)


def emit_new_chat_message(ticket_type: str, ticket_id: int, message_payload: dict) -> None:
    """Emit a Socket.IO event for a new chat message in a ticket room."""

    room_name = f"ticket_{ticket_type}_{ticket_id}"
    try:
        socketio.emit(
            'new_chat_message',
            {
                'ticket_id': ticket_id,
                'message': message_payload,
            },
            room=room_name,
        )
    except Exception as exc:  # pragma: no cover - defensive logging only
        current_app.logger.error(
            "Failed to emit live chat message for %s ticket %s: %s",
            ticket_type,
            ticket_id,
            exc,
            exc_info=True,
        )

def send_welcome_message(sid, auth):
    """Sends a welcome message to a newly connected anonymous client."""
    from services.municipio_responder import responder_municipio
    from models import User, ChatSessionContext, Rubro, db
    from uuid import uuid4
    from flask import g

    current_app.logger.info(f"Anonymous connection on web channel detected for sid: {sid}. Sending welcome message.")
    with current_app.app_context():
        owner_user = User.query.filter_by(tipo_chat='municipio', rol='admin').first()
        if not owner_user:
            current_app.logger.error("Default municipality user with role 'admin' and tipo_chat 'municipio' not found.")
            return

        rubro = owner_user.rubro
        if not rubro:
            current_app.logger.error(f"Rubro not found for user {owner_user.id}")
            return

        chat_session_uuid = str(uuid4())
        chat_db_context = ChatSessionContext(
            chat_session_id=chat_session_uuid,
            user_id=owner_user.id,
            context_data={}
        )
        db.session.add(chat_db_context)
        db.session.commit()

        anon_id = str(uuid4())
        if 'viewer' in g:
            del g.viewer

        respuesta = responder_municipio(
            pregunta_original="__INIT__",
            owner_user=owner_user,
            rubro_obj=rubro,
            viewer_user=None,
            chat_db_context=chat_db_context,
            anon_id=anon_id,
            channel='web',
            chat_session_uuid=chat_session_uuid
        )

        ensure_buttons_compatibility(respuesta)

        if respuesta.get("generar_audio"):
            try:
                audio_url = generar_audio(text=respuesta["message_body"])
                if audio_url:
                    respuesta["audio_url"] = audio_url
            except Exception as e:
                current_app.logger.error(f"Error generating welcome audio: {e}")

        emit('message', respuesta, room=sid)
        current_app.logger.info(f"Welcome message sent to sid: {sid}")

@socketio.on('connect')
def on_connect(auth):
    """
    Handles new Socket.IO connections.
    Authenticates the user if a token is provided.
    For anonymous web connections, sends a welcome message.
    """
    current_app.logger.info(f"Socket.IO client connected: {request.sid}")
    token = (auth or {}).get('token')
    channel = (auth or {}).get('channel')

    if token:
        try:
            jwt.decode(token, current_app.config['SECRET_KEY'], algorithms=["HS256"])
            current_app.logger.info(f"Socket.IO token validated successfully for sid: {request.sid}")
        except (jwt.ExpiredSignatureError, jwt.InvalidTokenError) as e:
            current_app.logger.warning(f"Socket.IO connection rejected for sid {request.sid} due to invalid token: {e}")
            return False
    elif channel == 'web':
        # Defer the welcome message to a separate thread to not block the connection
        socketio.start_background_task(send_welcome_message, request.sid, auth)

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
        db.session.commit()
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
    from services.municipio_responder import handle_location_update
    response = handle_location_update(data)
    socketio.emit('message', response)
