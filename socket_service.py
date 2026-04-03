from flask_socketio import SocketIO, join_room, emit
from flask import current_app, request
from config import ALLOWED_ORIGINS
from models import User, TenantProfile, db, TicketComentario, MunicipioTicket, PymeTicket
import jwt
from services.ticket_service import servicio_tickets # Reutilizamos el servicio de tickets
from services.tts_orchestrator import generar_audio
from services.conversation_stream import build_realtime_envelope
from utils.response_utils import ensure_buttons_compatibility
from typing import Any, Optional, Set
import importlib.util
import os

SOCKET_CORS_ORIGINS = list(
    dict.fromkeys(list(ALLOWED_ORIGINS) + ["https://chatboc.ar", "https://www.chatboc.ar"])
)

def _resolve_socket_async_mode() -> str:
    """Prefer eventlet when available, fallback to threading for stability."""
    forced_mode = (os.getenv("SOCKETIO_ASYNC_MODE") or "").strip().lower()
    if forced_mode:
        return forced_mode

    if importlib.util.find_spec("eventlet"):
        return "eventlet"
    return "threading"


socketio = SocketIO(
    cors_allowed_origins=SOCKET_CORS_ORIGINS,
    cookie=True,
    async_mode=_resolve_socket_async_mode(),
    path="/api/socket.io",
)


def _get_owner_user(user: Optional[User]) -> Optional[User]:
    if not user:
        return None

    empresa_id = getattr(user, "empresa_id", None)
    if empresa_id:
        owner = User.query.get(empresa_id)
        if owner:
            return owner

    return user


def _get_rooms_for_user(user: Optional[User]) -> list[str]:
    rooms: Set[str] = set()
    if not user:
        return []

    owner = _get_owner_user(user)

    usuario_tipo = getattr(user, "tipo_chat", None)
    owner_tipo = getattr(owner, "tipo_chat", None)

    municipio_id = (
        getattr(user, "municipio_id", None)
        or getattr(owner, "municipio_id", None)
    )
    if not municipio_id and (owner_tipo == "municipio"):
        municipio_id = getattr(owner, "id", None)
    if (usuario_tipo == "municipio" or owner_tipo == "municipio") and municipio_id:
        rooms.add(f"municipio_{municipio_id}")

    rubro_id = getattr(user, "rubro_id", None) or getattr(owner, "rubro_id", None)
    pyme_id = getattr(user, "pyme_id", None) or getattr(owner, "pyme_id", None)
    if not pyme_id and owner_tipo == "pyme":
        pyme_id = getattr(owner, "id", None)

    if (usuario_tipo == "pyme" or owner_tipo == "pyme"):
        if rubro_id:
            rooms.add(f"pyme_{rubro_id}")
        elif pyme_id:
            rooms.add(f"pyme_{pyme_id}")

    return list(rooms)

def _get_rooms_for_tenant_slug(tenant_slug: Optional[str]) -> list[str]:
    if not tenant_slug:
        return []

    tenant = TenantProfile.query.filter_by(slug=str(tenant_slug).strip()).first()
    if not tenant:
        return []

    rooms: Set[str] = set()
    if tenant.municipio_id:
        rooms.add(f"municipio_{tenant.municipio_id}")
    if tenant.pyme_id:
        rooms.add(f"pyme_{tenant.pyme_id}")
    return list(rooms)



def _merge_rooms_for_subscription(user: User, tenant_slug: Optional[str]) -> list[str]:
    """Merge user-derived and tenant-derived rooms without dropping either scope."""

    rooms = list(_get_rooms_for_user(user))
    for room in _get_rooms_for_tenant_slug(tenant_slug):
        if room not in rooms:
            rooms.append(room)
    return rooms

def _resolve_ticket_room(payload: Any) -> Optional[str]:
    if not isinstance(payload, dict):
        return None

    explicit_room = payload.get("socket_room")
    if explicit_room:
        return explicit_room

    tenant_type = payload.get("tenant_type") or payload.get("tipo")
    tenant_id = payload.get("tenant_id")

    if tenant_type == "municipio":
        municipio_id = payload.get("municipio_id") or tenant_id
        if municipio_id:
            return f"municipio_{municipio_id}"
    elif tenant_type == "pyme":
        rubro_id = payload.get("rubro_id")
        if rubro_id:
            return f"pyme_{rubro_id}"
        fallback_id = tenant_id or payload.get("pyme_id")
        if fallback_id:
            return f"pyme_{fallback_id}"

    ticket_id = payload.get("id") or payload.get("ticket_id")
    if ticket_id and tenant_type in {"municipio", "pyme"}:
        try:
            if tenant_type == "municipio":
                ticket_obj = db.session.get(MunicipioTicket, ticket_id)
                municipio_id = getattr(ticket_obj, "municipio_id", None) if ticket_obj else None
                if municipio_id:
                    return f"municipio_{municipio_id}"
            elif tenant_type == "pyme":
                ticket_obj = db.session.get(PymeTicket, ticket_id)
                rubro_id = getattr(ticket_obj, "rubro_id", None) if ticket_obj else None
                if rubro_id:
                    return f"pyme_{rubro_id}"
        except Exception:
            current_app.logger.exception(
                "Error resolving socket room for ticket %s of type %s", ticket_id, tenant_type
            )

    return None


def _emit_to_ticket_room(event_name: str, data: Any) -> None:
    """Emit an event to the room associated with the ticket payload."""
    room = _resolve_ticket_room(data)
    if room:
        socketio.emit(event_name, data, room=room)
    else:
        socketio.emit(event_name, data)


def _emit_standard_ticket_event(event_name: str, data: Any) -> None:
    """Emit normalized enterprise-style events alongside legacy socket payloads."""
    payload = data if isinstance(data, dict) else {"payload": data}
    room = _resolve_ticket_room(payload)
    envelope = build_realtime_envelope(event_name=event_name, payload=payload, room=room)
    if room:
        socketio.emit(event_name, envelope, room=room)
    else:
        socketio.emit(event_name, envelope)


def emit_ticket_update(data: Any) -> None:
    """Broadcast generic ticket updates to subscribed admin clients."""
    _emit_to_ticket_room('ticket_update', data)


def emit_ticket_status_changed(data: Any) -> None:
    """Broadcast a normalized status event while preserving legacy consumers."""
    _emit_standard_ticket_event('ticket.status.changed', data)
    emit_ticket_update(data)


def emit_ticket_assignment_changed(data: Any) -> None:
    """Broadcast assignment changes with a normalized contract for new clients."""
    _emit_standard_ticket_event('ticket.assignment.changed', data)
    emit_ticket_update(data)


def emit_ticket_presence_changed(data: Any) -> None:
    """Broadcast ticket presence updates for collaborative inbox experiences."""
    _emit_standard_ticket_event('ticket.presence.changed', data)


def emit_conversation_message_read(data: Any) -> None:
    """Broadcast read-state updates for enterprise inbox clients."""
    _emit_standard_ticket_event('conversation.message.read', data)

def emit_conversation_linked(data: Any) -> None:
    """Broadcast omnichannel link events."""
    _emit_standard_ticket_event('conversation.linked', data)


def emit_notification_status_changed(data: Any) -> None:
    """Broadcast normalized notification lifecycle events."""
    event_name = data.get("event") if isinstance(data, dict) else None
    if event_name not in {"notification.sent", "notification.failed"}:
        event_name = "notification.updated"
    _emit_standard_ticket_event(event_name, data)


def emit_ticket_unread_changed(data: Any) -> None:
    """Broadcast unread-summary deltas for inbox list reconciliation."""
    _emit_standard_ticket_event('ticket.unread.changed', data)


def emit_tenant_update(tenant_slug: str, event_name: str, data: Any = None) -> None:
    """Emit an event to the tenant's specific room for real-time portal updates."""
    if tenant_slug:
        # Emit generic content update signal
        socketio.emit('tenant_content_update', {'type': event_name}, room=tenant_slug)
        # Emit specific event
        if data:
            socketio.emit(event_name, data, room=tenant_slug)


def emit_new_ticket(data: Any) -> None:
    """Broadcast a newly created ticket and mirror a generic update for legacy clients."""
    _emit_to_ticket_room('new_ticket', data)
    emit_ticket_update(data)


def emit_ticket_comment(data: Any) -> None:
    """Broadcast a new comment without altering the legacy ticket_update payloads."""
    _emit_to_ticket_room('new_comment', data)
    _emit_standard_ticket_event('conversation.message.created', data)

def emit_new_chat_message(data: Any) -> None:
    """Broadcast a new chat message to the live chat room."""
    _emit_to_ticket_room('new_chat_message', data)
    _emit_standard_ticket_event('conversation.message.created', data)


def emit_survey_update(slug_publico: str, data: Any) -> None:
    """Emit a live update for a specific survey/poll."""
    room = f"encuesta_{slug_publico}"
    socketio.emit('survey_update', data, room=room)


def emit_survey_comment(slug_publico: str, data: Any) -> None:
    """Emit a live comment for a specific survey/poll."""
    room = f"encuesta_{slug_publico}"
    socketio.emit('survey_comment', data, room=room)




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
            payload = jwt.decode(token, current_app.config['SECRET_KEY'], algorithms=["HS256"])
            user_id = payload.get('user_id')
            tenant_slug = payload.get('tenant_slug')
            user = User.query.get(user_id) if user_id else None
            if not user:
                current_app.logger.warning(
                    "Socket.IO connection rejected for sid %s due to unknown user in token.",
                    request.sid,
                )
                return False

            rooms = _merge_rooms_for_subscription(user, tenant_slug)
            for room in rooms:
                join_room(room)
                current_app.logger.debug(
                    "Socket.IO sid %s joined room %s for user %s", request.sid, room, user.id
                )

            current_app.logger.info(
                "Socket.IO token validated successfully for sid: %s (rooms=%s)",
                request.sid,
                rooms,
            )
        except (jwt.ExpiredSignatureError, jwt.InvalidTokenError) as e:
            current_app.logger.warning(f"Socket.IO connection rejected for sid {request.sid} due to invalid token: {e}")
            return False
    elif channel == 'web':
        # Defer the welcome message to a separate thread to not block the connection
        socketio.start_background_task(send_welcome_message, request.sid, auth)


@socketio.on('subscribe_ticket_updates')
def on_subscribe_ticket_updates(data):
    token = (data or {}).get('token')
    tenant_slug = (data or {}).get('tenant_slug')
    if not token:
        emit('subscription_error', {'error': 'missing_token'})
        return

    try:
        payload = jwt.decode(token, current_app.config['SECRET_KEY'], algorithms=["HS256"])
    except (jwt.ExpiredSignatureError, jwt.InvalidTokenError) as exc:
        current_app.logger.warning("Socket subscribe rejected for sid %s: %s", request.sid, exc)
        emit('subscription_error', {'error': 'invalid_token'})
        return

    user_id = payload.get('user_id')
    user = User.query.get(user_id) if user_id else None
    if not user:
        emit('subscription_error', {'error': 'unknown_user'})
        return

    rooms = _merge_rooms_for_subscription(user, tenant_slug)
    for room in rooms:
        join_room(room)
    emit('subscribed_ticket_updates', {'rooms': rooms or []})

@socketio.on('join')
def on_join(data):
    room = data['room']
    join_room(room)
    # Support for survey rooms
    if room.startswith("encuesta_"):
        current_app.logger.debug(f"Client joined survey room: {room}")
    # Remove 'status' emit to prevent annoying "pip" sound on frontend
    # socketio.emit('status', {'msg': 'Conectado a la sala ' + room}, room=room)

@socketio.on('new_chat')
def on_new_chat(data):
    room = data['room']
    socketio.emit('new_chat', data, room=room)

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
                if ticket_type == "municipio" or current_app.config.get("ENABLE_PYME_WHATSAPP_CHAT", True):
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
