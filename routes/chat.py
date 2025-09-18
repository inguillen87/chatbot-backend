import sys
import os
import logging
import random
import uuid  # Added for chat_session_id generation
from copy import deepcopy
from urllib.parse import urljoin
from typing import Dict, List, Optional, Tuple

# Add project root to sys.path for this routes file
project_root_chat_routes = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if project_root_chat_routes not in sys.path:
    sys.path.insert(0, project_root_chat_routes)

from flask import Blueprint, request, jsonify, current_app
from sqlalchemy import func, desc
from sqlalchemy.orm.attributes import flag_modified # Importado para flag_modified
from models import User, Rubro, Conversacion, db, ChatSessionContext # Added ChatSessionContext
from utils.db_utils import commit_with_retry
from socket_service import socketio # Import socketio
from services.logic import (
    responder_chatboc,
    RUBROS_PUBLICOS,
    normalizar_rubro,
    es_rubro_publico,
)
from services.demo_registry import load_demo_rubros
from utils.auth_helpers import (
    anon_o_token_requerido,
    obtener_token,
    user_from_token,
)
from utils.response_utils import ensure_buttons_compatibility
from datetime import datetime, timedelta

chat_bp = Blueprint("chat_bp", __name__)

DEMO_ACTION_PREFIX = "demo_select_rubro"


def _load_demo_rubros() -> List[Dict[str, Optional[str]]]:
    """Recupera la configuración de demos y la enriquece con datos reales."""

    opciones: List[Dict[str, Optional[str]]] = []
    for demo in load_demo_rubros():
        opciones.append(demo.to_internal_dict())
    return opciones


def _extract_demo_key(action_id: Optional[str]) -> Optional[str]:
    if not action_id:
        return None
    value = str(action_id).strip()
    if not value:
        return None

    if ":" in value:
        prefix, candidate = value.split(":", 1)
        if prefix != DEMO_ACTION_PREFIX:
            return None
    elif value.startswith(f"{DEMO_ACTION_PREFIX}_"):
        candidate = value[len(f"{DEMO_ACTION_PREFIX}_"):]
    elif value.startswith(DEMO_ACTION_PREFIX):
        candidate = value[len(DEMO_ACTION_PREFIX):]
        candidate = candidate.lstrip(":_")
    else:
        return None

    candidate = candidate.strip().lower()
    return candidate or None


def _is_init_payload(payload) -> bool:
    if payload is None:
        return True
    if isinstance(payload, str):
        return payload.strip() in ("", "__INIT__")
    if isinstance(payload, dict):
        inner = payload.get("pregunta")
        if inner is None:
            return True
        if isinstance(inner, str) and inner.strip() in ("", "__INIT__"):
            return True
    return False


def _build_demo_selector_payload(opciones: List[Dict[str, Optional[str]]]) -> Dict[str, object]:
    mensaje = current_app.config.get(
        "DEMO_WELCOME_MESSAGE",
        "👋 ¡Bienvenido a la demo de Chatboc! Elegí la experiencia que querés probar:",
    )
    botones: List[Dict[str, object]] = []
    for opcion in opciones:
        action_value = f"{DEMO_ACTION_PREFIX}:{opcion['key']}"
        boton = {
            "texto": opcion["label"],
            "action_id": action_value,
            "action": action_value,
            "id": action_value,
        }
        if opcion.get("descripcion"):
            boton["descripcion"] = opcion["descripcion"]
        botones.append(boton)

    message_type = "interactive_list" if len(botones) > 1 else "interactive_buttons"
    return {
        "message_body": mensaje,
        "options_list": botones,
        "botones": botones,
        "message_type": message_type,
        "fuente": "demo_selector",
        "generar_audio": True,
    }


def _build_demo_limit_response(limite: int) -> Dict[str, object]:
    mensaje = (
        "¡Gracias por probar Chatboc! Llegaste al límite de "
        f"{limite} consultas de la demo interactiva. "
        "Agendá una reunión con nuestro equipo para conocer el potencial completo de la plataforma."
    )
    botones = [
        {
            "texto": "Solicitar Demo Personalizada",
            "action": "open_demo_form",
            "action_id": "open_demo_form",
            "id": "open_demo_form",
        },
        {"texto": "Iniciar Sesión", "action": "login", "action_id": "login", "id": "login"},
        {
            "texto": "Registrarme Gratis",
            "action": "register",
            "action_id": "register",
            "id": "register",
        },
    ]
    return {
        "error": "demo_limit_reached",
        "respuesta": mensaje,
        "message_body": mensaje,
        "options_list": botones,
        "botones": botones,
        "message_type": "interactive_buttons",
        "fuente": "demo_limit",
        "generar_audio": True,
    }


def _absolute_demo_url(path: Optional[str]) -> Optional[str]:
    """Devuelve una URL absoluta para recursos de la demo."""
    if not path:
        return None

    value = str(path).strip()
    if not value:
        return None

    if value.startswith(("http://", "https://", "data:")):
        return value

    base_url = current_app.config.get("BACKEND_URL") or request.host_url
    if not base_url.endswith("/"):
        base_url = f"{base_url}/"

    return urljoin(base_url, value.lstrip("/"))


def _format_demo_resources(
    resources: List[Dict[str, object]] | None,
) -> Tuple[str, List[Dict[str, object]], List[Dict[str, object]]]:
    """Genera texto, botones y adjuntos a partir de la configuración de recursos."""

    if not resources:
        return "", [], []

    icon_map = {
        "pdf": "📄",
        "document": "📄",
        "image": "🖼️",
        "video": "🎬",
        "spreadsheet": "📊",
        "pricing": "💰",
        "link": "🔗",
    }

    lines: List[str] = []
    buttons: List[Dict[str, object]] = []
    attachments: List[Dict[str, object]] = []

    for idx, raw in enumerate(resources):
        if not isinstance(raw, dict):
            continue

        title_raw = raw.get("title") or raw.get("nombre") or raw.get("label")
        description_raw = raw.get("description") or raw.get("descripcion")
        resource_type = str(raw.get("type") or raw.get("tipo") or "document").strip().lower() or "document"
        icon = icon_map.get(resource_type, "📎")

        title = str(title_raw).strip() if title_raw else None
        description = str(description_raw).strip() if description_raw else None
        cta_text = str(raw.get("cta_text") or title or "Ver recurso").strip()

        absolute_url = _absolute_demo_url(raw.get("url") or raw.get("href"))
        thumbnail_url = _absolute_demo_url(raw.get("thumbnail") or raw.get("image"))

        label_for_text = title or cta_text or f"Recurso {idx + 1}"

        line = f"• {icon} {label_for_text}"
        if description:
            line += f" – {description}"
        if absolute_url:
            line += f" → {absolute_url}"
        lines.append(line)

        if absolute_url:
            buttons.append(
                {
                    "id": f"demo_resource_{idx}",
                    "texto": f"{icon} {cta_text}",
                    "type": "url",
                    "url": absolute_url,
                    "description": description,
                }
            )

        attachment_entry: Dict[str, object] = {
            "titulo": label_for_text,
            "descripcion": description,
            "tipo": resource_type,
        }
        if absolute_url:
            attachment_entry["url"] = absolute_url
        if thumbnail_url:
            attachment_entry["thumbnail"] = thumbnail_url
        attachments.append(attachment_entry)

    formatted_text = "\n".join(lines) if lines else ""
    return formatted_text, buttons, attachments


def _format_demo_faq_preview(faq_preview: List[Dict[str, object]] | None) -> str:
    if not faq_preview:
        return ""

    lines: List[str] = []
    for raw in faq_preview:
        if not isinstance(raw, dict):
            continue

        question = str(raw.get("pregunta") or raw.get("question") or "").strip()
        if not question:
            continue
        answer = str(raw.get("respuesta") or raw.get("answer") or "").strip()
        line = f"• ❓ {question}"
        if answer:
            line += f" → {answer}"
        lines.append(line)

    return "\n".join(lines)

def _parse_request(tipo_chat_fijo: str | None = None):
    def _normalizar_tipo_chat(valor: str | None) -> str | None:
        if not valor:
            return None
        valor = str(valor).strip().lower()
        sinonimos = {
            "pymes": "pyme",
            "pyme": "pyme",
            "municipios": "municipio",
            "municipio": "municipio",
            "muni": "municipio",
        }
        return sinonimos.get(valor)

    try:
        data = request.get_json()
        if not isinstance(data, dict):
            raise TypeError("El cuerpo debe ser JSON")

        pregunta = data.get("pregunta")
        location = data.get("location")

        # If the question is empty (or not provided) and there's no location,
        # it's the initial message from the widget.
        if not location and (pregunta is None or str(pregunta).strip() == ""):
            pregunta = "__INIT__"

        if tipo_chat_fijo:
            tipo_chat = tipo_chat_fijo
        else:
            tipo_chat = _normalizar_tipo_chat(data.get("tipo_chat"))

        contexto_previo = data.get("contexto_previo")
        rubro_id = data.get("rubro_id")
        rubro_clave = data.get("rubro_clave") or data.get("rubro")

        if tipo_chat not in ("pyme", "municipio"):
            rubro_obj_tmp = None
            try:
                if rubro_id:
                    rubro_obj_tmp = Rubro.query.get(int(rubro_id))
                elif rubro_clave:
                    rubro_obj_tmp = Rubro.query.filter(func.lower(Rubro.clave) == func.lower(str(rubro_clave))).first()
            except Exception:
                rubro_obj_tmp = None

            if rubro_obj_tmp:
                tipo_chat = "municipio" if es_rubro_publico(rubro_obj_tmp) else "pyme"
            else:
                raise ValueError("'tipo_chat' debe ser 'pyme' o 'municipio'")

        attachment_info = data.get("attachmentInfo") or data.get("attachment_info")
        location = data.get("location")
        ticket_id = data.get("ticket_id")
        tipo_ticket = data.get("tipo_ticket")
        profile_name = data.get("nombre_usuario") or data.get("profile_name")
        action_id = data.get("action") or data.get("action_id")

        if isinstance(pregunta, dict):
            action_id = action_id or pregunta.get("action") or pregunta.get("action_id")


        if attachment_info:
            if not isinstance(attachment_info, dict) or not all(k in attachment_info for k in ['id', 'url', 'name', 'mimeType', 'size']):
                raise ValueError("El campo 'attachmentInfo' es inválido o le faltan campos requeridos.")

        normalized_location = None
        if location:
            if not isinstance(location, dict):
                raise ValueError("El campo 'location' es inválido.")

            lat_value = location.get("lat")
            lon_value = location.get("lon")
            if lat_value is None:
                lat_value = location.get("latitude")
            if lon_value is None:
                lon_value = location.get("longitude")
            if lon_value is None:
                lon_value = location.get("lng")

            if lat_value is None or lon_value is None:
                raise ValueError("El campo 'location' es inválido.")

            try:
                lat_float = float(lat_value)
                lon_float = float(lon_value)
            except (TypeError, ValueError):
                raise ValueError("El campo 'location' es inválido.")

            normalized_location = {
                "latitude": lat_float,
                "longitude": lon_float,
                "lat": lat_float,
                "lon": lon_float,
            }

            accuracy = location.get("accuracy")
            if accuracy is not None:
                try:
                    normalized_location["accuracy"] = float(accuracy)
                except (TypeError, ValueError):
                    normalized_location["accuracy"] = accuracy

            address = location.get("address") or location.get("label")
            if address:
                normalized_location["address"] = address

            source = location.get("source")
            if source:
                normalized_location["source"] = source

            for extra_key in ("name", "description"):
                if location.get(extra_key):
                    normalized_location[extra_key] = location[extra_key]

            normalized_location.update({
                key: value
                for key, value in location.items()
                if key not in normalized_location and value is not None
            })

            location = normalized_location

        return (
            pregunta,
            contexto_previo,
            tipo_chat,
            rubro_id,
            rubro_clave,
            attachment_info,
            location,
            ticket_id,
            tipo_ticket,
            profile_name,
            action_id,
            None,
        )

    except (TypeError, ValueError) as e:
        current_app.logger.warning(f"Error al parsear /ask: {e}")
        return (
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            jsonify({"error": str(e)}),
        )
    except Exception as e:
        current_app.logger.error(f"Error inesperado al parsear /ask: {e}")
        return (
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            jsonify({"error": "Formato JSON inválido"}),
        )

def _authenticate_and_get_user():
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        token = auth_header.split(" ")[1]
        if token:
            return User.query.filter_by(token=token).first()
    return None

def _procesar_chat(
    tipo_chat_fijo: str | None = None,
    current_user=None,
    owner_user=None,
    anon_id: str | None = None,
): 
    channel = "web"  # Define channel for this processing function
    original_user_payload = None
    # --- Session and Context Initialization ---
    chat_session_id_header = request.headers.get("X-Chat-Session-Id")
    if not chat_session_id_header:
        chat_session_id_header = str(uuid.uuid4())
        current_app.logger.warning(f"X-Chat-Session-Id not found. Generated new: {chat_session_id_header}")

    def _emit_socket_payload(payload: object) -> None:
        """Emite un mensaje por Socket.IO si hay una sesión web activa."""

        if channel == "web" and chat_session_id_header:
            ensure_buttons_compatibility(payload)

            socketio.emit('message', payload, room=chat_session_id_header)
            current_app.logger.debug(
                "Emitting socket message (early return)",
                extra={"room": chat_session_id_header, "payload": payload},
            )

    actor_principal = owner_user or current_user
    chat_context_obj = ChatSessionContext.query.filter_by(chat_session_id=chat_session_id_header).first()

    if not chat_context_obj:
        current_app.logger.info(f"No ChatSessionContext found for {chat_session_id_header}. Creating new one.")
        chat_context_obj = ChatSessionContext(
            chat_session_id=chat_session_id_header,
            user_id=getattr(actor_principal, 'id', None),
            anon_id=anon_id if not actor_principal else None,
            context_data={}
        )
        db.session.add(chat_context_obj)
        try:
            commit_with_retry(db.session)
            current_app.logger.info(
                f"ChatSessionContext inicial guardado para {chat_session_id_header} (commit temprano)."
            )
        except Exception as e:
            db.session.rollback()
            current_app.logger.error(
                f"Error guardando ChatSessionContext inicial para {chat_session_id_header}: {e}",
                exc_info=True,
            )
            return (
                jsonify({"error": "Error de base de datos"}),
                500,
            )

    # --- Request Parsing (Audio or JSON) ---
    if 'audio_file' in request.files:
        audio_file = request.files['audio_file']
        if audio_file.filename != '':
            from services.google_speech_to_text import SpeechToTextService
            import tempfile

            chat_context_obj.context_data['source_is_audio'] = True

            # Use a more unique filename to avoid collisions
            temp_filename = f"{uuid.uuid4()}_{audio_file.filename}"
            temp_path = os.path.join(tempfile.gettempdir(), temp_filename)
            audio_file.save(temp_path)

            stt_service = SpeechToTextService()
            pregunta = stt_service.transcribe_audio_file(file_path=temp_path, mime_type=audio_file.mimetype)

            os.remove(temp_path)

            if not pregunta:
                return jsonify({
                    "message_body": "Lo siento, no pude entender lo que dijiste en el audio. ¿Podrías intentarlo de nuevo o escribir tu consulta?",
                    "message_type": "text",
                    "fuente": "audio_transcription_failed"
                }), 400

            # Set default values for other parameters when processing audio
            contexto_previo = None
            tipo_chat = tipo_chat_fijo or 'municipio'
            rubro_id = request.form.get('rubro_id')
            rubro_clave = request.form.get('rubro_clave')
            uploaded_file_info = None
            archivo_adjunto_id = None
            location = None
            action_id = None
            original_user_payload = pregunta
        else:
            return jsonify({"error": "Audio file is empty."}), 400
    else:
        # chat_context_obj.context_data.pop('source_is_audio', None) # This was moved to after the check
        try:
            (
                pregunta,
                contexto_previo,
                tipo_chat,
                rubro_id,
                rubro_clave,
                attachment_info,
                location,
                ticket_id,
                tipo_ticket,
                profile_name,
                action_id,
                error_response,
            ) = _parse_request(tipo_chat_fijo)
            if error_response:
                return error_response, 400
            original_user_payload = pregunta

            # Fallback to cookies or stored session context if profile name not provided in JSON
            if not profile_name:
                profile_name = request.cookies.get("nombre_usuario") or request.cookies.get("profile_name")
            if not profile_name and chat_context_obj and chat_context_obj.context_data:
                profile_name = chat_context_obj.context_data.get("profile_name")

            # Si la solicitud solo contenía una ubicación, creamos una pregunta sintética para que el backend la procese.
            if not pregunta and location:
                pregunta = "[Ubicación compartida por el usuario]"
        except Exception as e:
            current_app.logger.error(f"Error parsing request in _procesar_chat: {e}", exc_info=True)
            return jsonify({"error": f"Invalid request format: {e}"}), 400

        current_app.logger.debug(
            "Parsed request data",
            extra={
                "pregunta": pregunta,
                "tipo_chat": tipo_chat,
                "rubro_id": rubro_id,
                "rubro_clave": rubro_clave,
                "attachmentInfo": attachment_info,
                "location": location,
                "ticket_id": ticket_id,
                "tipo_ticket": tipo_ticket,
            },
        )

    # --- Intercept messages for active live chats ---
    if ticket_id and tipo_ticket and pregunta:
        from models import MunicipioTicket, PymeTicket
        from services.ticket_service import servicio_tickets

        TicketModel = MunicipioTicket if tipo_ticket == "municipio" else PymeTicket
        # Use with_for_update to lock the row during the check and update
        ticket = db.session.query(TicketModel).filter_by(id=ticket_id).with_for_update().first()

        if ticket and ticket.estado in ["esperando_agente_en_vivo", "en_proceso", "en_vivo"]:
            comentario_data = {
                "comentario": pregunta,
                "user_id": getattr(current_user, "id", None),
                "anon_id": anon_id if not current_user else None,
                "es_admin": False
            }

            if attachment_info:
                archivo_id = attachment_info.get('id')
                comentario_data['archivo_adjunto_id'] = archivo_id
                if not pregunta.strip():
                    comentario_data['comentario'] = f"[Archivo adjunto: {attachment_info.get('name', 'archivo')}]"
                else:
                    comentario_data['comentario'] += f" [Archivo: {attachment_info.get('name', 'archivo')}]"

            nuevo_comentario = servicio_tickets.crear_comentario(
                ticket_id=ticket_id,
                tipo_ticket=tipo_ticket,
                comentario_data=comentario_data
            )

            if nuevo_comentario:
                commit_with_retry(db.session) # Commit the new comment
                room_name = f"ticket_{tipo_ticket}_{ticket_id}"
                socketio.emit('new_chat_message', {
                    'ticket_id': ticket_id,
                    'message': nuevo_comentario.to_dict()
                }, room=room_name)
                current_app.logger.info(f"User message for active ticket {ticket_id} sent to room {room_name}")
                return jsonify({"status": "message_sent_to_live_chat"}), 200
            else:
                db.session.rollback()
                return jsonify({"error": "Failed to save user message for live chat"}), 500

    try:
        # --- User and Role Determination ---
        is_anonymous = not actor_principal
        viewer_obj = current_user # El que mira

        if is_anonymous and not anon_id:
            # This case should ideally not be reached if anon_o_token_requerido is working correctly,
            # as it should have generated an anon_id. This is a safeguard.
            current_app.logger.warning("anon_id no fue provisto a _procesar_chat para un usuario anónimo. El decorador podría no estar funcionando como se espera.")
            return jsonify({"error": "No se pudo identificar la sesión anónima."}), 401

        if is_anonymous:
            # Lógica para usuarios anónimos
            max_messages = current_app.config.get("ANONYMOUS_MAX_MESSAGES_PER_SESSION", 10)
            session_timeout_minutes = current_app.config.get("ANONYMOUS_SESSION_TIMEOUT_MINUTES", 15)

            last_message_time = db.session.query(func.max(Conversacion.timestamp)) \
                .filter(Conversacion.session_id == anon_id) \
                .scalar()

            session_expired = False
            if last_message_time:
                if datetime.utcnow() - last_message_time > timedelta(minutes=session_timeout_minutes):
                    session_expired = True
                    current_app.logger.info(f"Sesión anónima {anon_id} expirada. Reiniciando conteo de mensajes.")

            if not session_expired:
                message_count_this_session = Conversacion.query \
                    .filter(Conversacion.session_id == anon_id) \
                    .filter(Conversacion.timestamp >= datetime.utcnow() - timedelta(minutes=session_timeout_minutes)) \
                    .count()

                current_app.logger.info(f"Usuario anónimo {anon_id}: {message_count_this_session} mensajes en la sesión actual (límite: {max_messages}).")

                if message_count_this_session >= max_messages:
                    return jsonify({
                        "error": "Alcanzaste el límite de mensajes para usuarios invitados.",
                        "respuesta": "Alcanzaste el límite de mensajes para usuarios invitados. Para continuar, por favor inicia sesión o regístrate.",
                        "botones": [
                            {"texto": "Iniciar Sesión", "action": "login"},
                            {"texto": "Registrarme Gratis", "action": "register"}
                        ]
                    }), 403
        else:
            # Lógica para usuarios autenticados
            current_app.logger.info(f"Usuario autenticado: {actor_principal.email} (ID: {actor_principal.id})")
            # No se aplican límites de mensajes para usuarios autenticados
            # Si el usuario está logueado, usar su ubicación guardada si no se proporciona una nueva
            if not location and actor_principal.latitud and actor_principal.longitud:
                location = {"lat": actor_principal.latitud, "lon": actor_principal.longitud}

        rubro_obj_global = None
        owner_del_bot = None
        rubro_para_log = None

        if chat_context_obj and chat_context_obj.context_data is None:
            chat_context_obj.context_data = {}

        contexto_chat = chat_context_obj.context_data if chat_context_obj else {}
        if not isinstance(contexto_chat, dict):
            contexto_chat = {}
            if chat_context_obj:
                chat_context_obj.context_data = contexto_chat

        tipo_chat_normalized = (tipo_chat or "").strip().lower()
        is_municipal_request = tipo_chat_normalized == "municipio"

        if is_municipal_request and isinstance(contexto_chat, dict):
            demo_keys_to_clear = (
                "demo_session",
                "demo_owner_user_id",
                "demo_rubro_id",
                "demo_tipo_chat",
                "demo_key",
                "demo_prompt_context",
                "demo_display_name",
                "demo_description",
                "demo_welcome_message",
                "demo_resources",
                "demo_faq_preview",
                "demo_intro_sent",
                "demo_message_count",
            )
            cleared_demo_state = False
            for key in demo_keys_to_clear:
                if key in contexto_chat:
                    contexto_chat.pop(key, None)
                    cleared_demo_state = True
            if cleared_demo_state and chat_context_obj:
                flag_modified(chat_context_obj, "context_data")

        is_demo_selection_event = False
        demo_options: Optional[List[Dict[str, Optional[str]]]] = None

        if rubro_id:
            rubro_obj_global = Rubro.query.get(rubro_id)
            if rubro_obj_global:
                rubro_para_log = rubro_obj_global.nombre or rubro_obj_global.clave
                owner_del_bot = User.query.filter_by(rubro_id=rubro_obj_global.id, empresa_id=None).first()
                if not owner_del_bot:
                    owner_del_bot = User.query.filter_by(rubro_id=rubro_obj_global.id, rol='admin').first()
        elif rubro_clave:
            rubro_obj_global = Rubro.query.filter(func.lower(Rubro.clave) == func.lower(rubro_clave)).first()
            if rubro_obj_global:
                rubro_para_log = rubro_obj_global.nombre or rubro_obj_global.clave
                owner_del_bot = User.query.filter_by(rubro_id=rubro_obj_global.id, empresa_id=None).first()
                if not owner_del_bot:
                    owner_del_bot = User.query.filter_by(rubro_id=rubro_obj_global.id, rol='admin').first()
        elif actor_principal and actor_principal.rubro_id:
            rubro_obj_global = Rubro.query.get(actor_principal.rubro_id)
            if rubro_obj_global:
                rubro_para_log = rubro_obj_global.nombre or rubro_obj_global.clave
            if actor_principal.empresa_id is None:
                owner_del_bot = actor_principal
            else:
                if rubro_obj_global:
                    owner_del_bot = User.query.filter_by(rubro_id=rubro_obj_global.id, empresa_id=None).first()
                    if not owner_del_bot:
                        owner_del_bot = User.query.filter_by(rubro_id=rubro_obj_global.id, rol='admin').first()

        if not owner_del_bot and owner_user:
            owner_del_bot = owner_user
            if not rubro_obj_global:
                rubro_obj_global = getattr(owner_user, "rubro", None)
                if rubro_obj_global:
                    rubro_para_log = rubro_para_log or getattr(rubro_obj_global, "nombre", None) or getattr(rubro_obj_global, "clave", None)
                    if not rubro_id:
                        rubro_id = rubro_obj_global.id
                    if not rubro_clave and getattr(rubro_obj_global, "clave", None):
                        rubro_clave = rubro_obj_global.clave

        # Recuperar el owner de una demo previamente seleccionada si no vino en la request
        if (
            not is_municipal_request
            and not owner_del_bot
            and isinstance(contexto_chat, dict)
        ):
            stored_owner_id = contexto_chat.get("demo_owner_user_id")
            if stored_owner_id:
                potencial_owner = User.query.get(stored_owner_id)
                if potencial_owner:
                    owner_del_bot = potencial_owner
                    rubro_obj_global = Rubro.query.get(contexto_chat.get("demo_rubro_id")) or potencial_owner.rubro
                    if rubro_obj_global:
                        rubro_para_log = rubro_para_log or getattr(rubro_obj_global, "nombre", None) or getattr(rubro_obj_global, "clave", None)
                        if not rubro_id:
                            rubro_id = rubro_obj_global.id
                        if not rubro_clave and getattr(rubro_obj_global, "clave", None):
                            rubro_clave = rubro_obj_global.clave
                    tipo_chat = contexto_chat.get("demo_tipo_chat", tipo_chat)

        if not is_municipal_request:
            demo_key = _extract_demo_key(action_id)
            if not demo_key and isinstance(original_user_payload, dict):
                demo_key = _extract_demo_key(original_user_payload.get("action") or original_user_payload.get("action_id"))

            if not demo_key and isinstance(original_user_payload, str):
                user_text = original_user_payload.strip().lower()
                if user_text:
                    demo_options = _load_demo_rubros()
                    for opcion in demo_options:
                        candidatos = {
                            opcion["key"],
                            opcion["label"].strip().lower(),
                            (opcion.get("rubro_clave") or "").strip().lower(),
                        }
                        if user_text in candidatos:
                            demo_key = opcion["key"]
                            break

            if demo_key:
                demo_options = demo_options or _load_demo_rubros()
                selected_demo = next((opt for opt in demo_options if opt["key"] == demo_key), None)
                if not selected_demo:
                    selector_payload = _build_demo_selector_payload(demo_options)
                    selector_payload["message_body"] = (
                        "No pude reconocer esa demo. Elegí una de las opciones disponibles para continuar."
                    )
                    _emit_socket_payload(selector_payload)
                    return jsonify(selector_payload), 200

                owner_del_bot = User.query.get(selected_demo["owner_user_id"])
                rubro_obj_global = Rubro.query.get(selected_demo["rubro_id"]) if selected_demo.get("rubro_id") else None
                if owner_del_bot and not rubro_obj_global:
                    rubro_obj_global = owner_del_bot.rubro

                if not owner_del_bot or not rubro_obj_global:
                    current_app.logger.error(
                        f"[demo] La demo '{demo_key}' no cuenta con usuario o rubro configurado correctamente."
                    )
                    demo_options = demo_options or _load_demo_rubros()
                    selector_payload = _build_demo_selector_payload(demo_options)
                    selector_payload["message_body"] = (
                        "La demo seleccionada no está disponible en este momento. Elegí otra opción para continuar."
                    )
                    _emit_socket_payload(selector_payload)
                    return jsonify(selector_payload), 200

                rubro_para_log = selected_demo["label"]
                tipo_chat = selected_demo["tipo_chat"]
                rubro_id = getattr(rubro_obj_global, "id", rubro_id)
                if getattr(rubro_obj_global, "clave", None):
                    rubro_clave = rubro_obj_global.clave

                contexto_chat["demo_session"] = True
                contexto_chat["demo_owner_user_id"] = owner_del_bot.id
                contexto_chat["demo_rubro_id"] = rubro_obj_global.id if rubro_obj_global else None
                contexto_chat["demo_tipo_chat"] = tipo_chat
                contexto_chat["demo_key"] = selected_demo["key"]
                contexto_chat["demo_prompt_context"] = selected_demo.get("prompt_context") or selected_demo.get("descripcion")
                contexto_chat["demo_display_name"] = selected_demo.get("label")
                contexto_chat["demo_description"] = selected_demo.get("descripcion")
                contexto_chat["demo_welcome_message"] = selected_demo.get("welcome_message")
                contexto_chat["demo_message_count"] = 0
                contexto_chat["demo_resources"] = deepcopy(selected_demo.get("resources") or [])
                contexto_chat["demo_faq_preview"] = deepcopy(selected_demo.get("faq_preview") or [])
                contexto_chat["demo_intro_sent"] = False
                flag_modified(chat_context_obj, "context_data")

                pregunta = "__INIT__"
                original_user_payload = "__INIT__"
                action_id = None
                is_demo_selection_event = True

            if not owner_del_bot:
                demo_options = demo_options or _load_demo_rubros()
                if demo_options:
                    contexto_chat["demo_session"] = True
                    contexto_chat.pop("demo_owner_user_id", None)
                    contexto_chat.pop("demo_rubro_id", None)
                    contexto_chat.pop("demo_tipo_chat", None)
                    contexto_chat.pop("demo_key", None)
                    contexto_chat.pop("demo_prompt_context", None)
                    contexto_chat.pop("demo_display_name", None)
                    contexto_chat.pop("demo_description", None)
                    contexto_chat.pop("demo_welcome_message", None)
                    contexto_chat.pop("demo_resources", None)
                    contexto_chat.pop("demo_faq_preview", None)
                    contexto_chat.pop("demo_intro_sent", None)
                    flag_modified(chat_context_obj, "context_data")
                    selector_payload = _build_demo_selector_payload(demo_options)
                    try:
                        commit_with_retry(db.session)
                    except Exception as e_commit:
                        db.session.rollback()
                        current_app.logger.error(
                            f"Error guardando la selección de demo para la sesión {chat_session_id_header}: {e_commit}",
                            exc_info=True,
                        )
                    _emit_socket_payload(selector_payload)
                    return jsonify(selector_payload), 200

        if not owner_del_bot and rubro_obj_global:
            current_app.logger.warning(
                f"Rubro ID {rubro_obj_global.id} ('{rubro_para_log}') encontrado pero sin User owner asociado (empresa_id=None o rol=admin). Se continuará sin owner específico si el rubro es público.")

        if rubro_obj_global:
            nombre_rubro_log = rubro_para_log or getattr(rubro_obj_global, "nombre", None) or getattr(rubro_obj_global, "clave", "N/A")
            owner_id_log = getattr(owner_del_bot, "id", "N/A")
            current_app.logger.info(f"Usando Rubro ID {rubro_obj_global.id} ('{nombre_rubro_log}') perteneciente a User ID {owner_id_log} para la lógica del bot.")
        else:
            current_app.logger.info("No se pudo determinar un rubro/owner específico para la lógica del bot. Se usará lógica genérica si aplica (ej. para rubros públicos por defecto).")

        if owner_del_bot:
            from utils.plan_limits import limite_para_usuario
            limite = limite_para_usuario(owner_del_bot)
            if limite is not None and owner_del_bot.preguntas_usadas >= limite:
                return jsonify({
                    "error": f"El bot ha alcanzado el límite de preguntas de su plan ({limite})."
                }), 403

        demo_limit = current_app.config.get("DEMO_MAX_MESSAGES_PER_SESSION", 0)
        demo_session_activa = bool(isinstance(contexto_chat, dict) and contexto_chat.get("demo_session"))
        incrementar_demo = (
            demo_session_activa
            and demo_limit
            and demo_limit > 0
            and not is_demo_selection_event
            and not _is_init_payload(original_user_payload)
        )

        if incrementar_demo:
            conteo_actual = int(contexto_chat.get("demo_message_count", 0)) + 1
            contexto_chat["demo_message_count"] = conteo_actual
            flag_modified(chat_context_obj, "context_data")
            if conteo_actual > demo_limit:
                respuesta_limite = _build_demo_limit_response(demo_limit)
                try:
                    commit_with_retry(db.session)
                except Exception as e_commit:
                    db.session.rollback()
                    current_app.logger.error(
                        f"Error al guardar el límite de la demo para la sesión {chat_session_id_header}: {e_commit}",
                        exc_info=True,
                    )
                _emit_socket_payload(respuesta_limite)
                return jsonify(respuesta_limite), 403

        # The logic for file analysis has been moved to the upload endpoint.
        # The chat endpoint is only responsible for passing the attachmentInfo.
        analisis_archivo_resultado = None

        # Leer el X-Chat-Session-Id del header
        chat_session_id_header = request.headers.get("X-Chat-Session-Id")

        if not chat_session_id_header:
            # Fallback: Generar un nuevo ID si no viene en el header.
            # Idealmente, el frontend SIEMPRE debería enviarlo.
            chat_session_id_header = str(uuid.uuid4())
            current_app.logger.warning(f"X-Chat-Session-Id no encontrado en headers. Generando uno nuevo: {chat_session_id_header}")

        current_app.logger.info(f"Usando Chat Session ID (from header or generated): {chat_session_id_header}")

        # Cargar o crear el contexto de la base de datos
        chat_context_obj = ChatSessionContext.query.filter_by(chat_session_id=chat_session_id_header).first()

        if not chat_context_obj:
            current_app.logger.info(f"No se encontró ChatSessionContext. Creando uno nuevo.")
            chat_context_obj = ChatSessionContext(
                chat_session_id=chat_session_id_header,
                user_id=getattr(actor_principal, 'id', None), # Asociar con usuario logueado si existe
                anon_id=anon_id if not actor_principal else None, # Asociar con anon_id si no hay usuario logueado
                context_data={} # Inicializar con datos vacíos
            )
            db.session.add(chat_context_obj)
            # No hacer commit aquí todavía, se hará después de procesar el chat
        else:
            current_app.logger.info(f"ChatSessionContext cargado. User_id: {chat_context_obj.user_id}, Anon_id: {chat_context_obj.anon_id}")


            # Detect if user just logged in with this session
            if actor_principal and chat_context_obj.user_id == actor_principal.id and not chat_context_obj.context_data.get("user_was_present_before", False):
                chat_context_obj.context_data["just_logged_in_flag"] = True
                current_app.logger.info(f"User {actor_principal.id} just logged in with session {chat_session_id_header}. Setting just_logged_in_flag.")

            # This flag should be set to True if an authenticated user is present.
            chat_context_obj.context_data["user_was_present_before"] = bool(actor_principal)

        if chat_context_obj and chat_context_obj.context_data.get('just_logged_in_flag'):
            current_app.logger.info(f"User {actor_principal.id} just logged in. Clearing flag.")
            # Welcome back message or other logic can be triggered here.
            # For now, just clearing the flag.
            chat_context_obj.context_data.pop('just_logged_in_flag', None)
            flag_modified(chat_context_obj, "context_data")


        # El objeto `chat_context_obj.context_data` será el que se pase y modifique
        # en lugar de `flask_request_session` para el contexto específico del chat.

        interpretacion_imagen_resultado = None
        # --- Deduplicar mensajes rápidos idénticos ---
        last_msg = chat_context_obj.context_data.get("last_user_message")
        last_time_str = chat_context_obj.context_data.get("last_user_message_time")
        if last_msg == pregunta and last_time_str:
            try:
                last_dt = datetime.fromisoformat(last_time_str)
                if datetime.utcnow() - last_dt < timedelta(seconds=2):
                    current_app.logger.info("Mensaje duplicado detectado; reenviando última respuesta.")
                    last_resp = chat_context_obj.context_data.get("last_bot_response")
                    if last_resp:
                        ensure_buttons_compatibility(last_resp)
                        return jsonify(last_resp), 200
            except Exception:
                pass

        demo_metadata = None
        if isinstance(contexto_chat, dict) and contexto_chat.get("demo_session"):
            demo_metadata = {
                "key": contexto_chat.get("demo_key"),
                "prompt_context": contexto_chat.get("demo_prompt_context") or contexto_chat.get("demo_description"),
                "display_name": contexto_chat.get("demo_display_name"),
                "description": contexto_chat.get("demo_description"),
                "welcome_message": contexto_chat.get("demo_welcome_message"),
                "resources": deepcopy(contexto_chat.get("demo_resources") or []),
                "faq_preview": deepcopy(contexto_chat.get("demo_faq_preview") or []),
            }

        responder_extra_kwargs = {}
        if location:
            responder_extra_kwargs["es_ubicacion"] = True
            responder_extra_kwargs["ubicacion_usuario"] = location

        resultado = responder_chatboc(
            pregunta=pregunta,
            owner_user=owner_del_bot,
            current_user=viewer_obj, # El usuario que está viendo/interactuando
            rubro_obj=rubro_obj_global,
            rubro_nombre_frontend=rubro_clave,
            tipo_chat=tipo_chat,
            contexto_previo=contexto_previo, # Este 'contexto_previo' del request original podría necesitar ser integrado o reemplazado por el de la DB
            anon_id=anon_id, # El anon_id de la cabecera, para lógica de límites de mensajes anónimos, etc.
            chat_session_uuid=chat_session_id_header, # El ID de sesión único, ahora desde el header
            chat_db_context=chat_context_obj, # Pasar el objeto de contexto de DB
            channel="web", # Set channel to web
            attachment_info=attachment_info,
            location=location,
            profile_name=profile_name,
            user_data={
                "name": actor_principal.name,
                "email": actor_principal.email,
                "telefono": actor_principal.telefono
            } if actor_principal else None,
            demo_metadata=demo_metadata,
            **responder_extra_kwargs,
        )

        recursos_demo: List[Dict[str, object]] = []
        faq_preview_data: List[Dict[str, object]] = []
        demo_description: Optional[str] = None
        demo_welcome: Optional[str] = None
        if isinstance(contexto_chat, dict):
            recursos_demo = contexto_chat.get("demo_resources") or []
            faq_preview_data = contexto_chat.get("demo_faq_preview") or []
            demo_description = contexto_chat.get("demo_description")
            demo_welcome = contexto_chat.get("demo_welcome_message")

        has_intro_content = bool(
            recursos_demo or faq_preview_data or demo_description or demo_welcome
        )

        should_apply_intro = (
            demo_session_activa
            and has_intro_content
            and isinstance(resultado, dict)
            and not contexto_chat.get("demo_intro_sent")
            and (is_demo_selection_event or _is_init_payload(original_user_payload))
        )

        if should_apply_intro:
            resources_text, resource_buttons, resource_attachments = _format_demo_resources(recursos_demo)
            display_name = contexto_chat.get("demo_display_name") or contexto_chat.get("demo_key") or "esta demo"
            faq_preview_text = _format_demo_faq_preview(faq_preview_data)

            base_message = (
                contexto_chat.get("demo_welcome_message")
                or resultado.get("message_body")
                or resultado.get("respuesta")
            )
            description = contexto_chat.get("demo_description")
            original_message = resultado.get("message_body")

            segments: List[str] = []
            if base_message:
                segments.append(str(base_message).strip())
            if description:
                description_text = str(description).strip()
                if description_text and description_text not in segments:
                    segments.append(description_text)
            if faq_preview_text:
                segments.append(f"❓ Preguntas frecuentes destacadas:\n{faq_preview_text}")
            if resources_text:
                segments.append(f"📎 Material destacado de {display_name}:\n{resources_text}")
            if original_message:
                original_text = str(original_message).strip()
                if original_text and original_text not in segments:
                    segments.append(original_text)

            message_text = "\n\n".join([seg for seg in segments if seg])
            if message_text:
                resultado["message_body"] = message_text
                resultado["respuesta"] = message_text

            if resource_buttons:
                existing_options = resultado.get("options_list") or resultado.get("botones") or []
                resultado["options_list"] = resource_buttons + existing_options
                total_botones = len(resultado["options_list"])
                if total_botones:
                    if total_botones <= 3:
                        resultado["message_type"] = "interactive_buttons"
                    else:
                        resultado["message_type"] = "interactive_list"
                existing_botones = resultado.get("botones") or []
                if existing_botones:
                    resultado["botones"] = resource_buttons + existing_botones
                else:
                    resultado["botones"] = resultado["options_list"]

            if resource_attachments:
                existing_adjuntos = resultado.get("adjuntos") or []
                resultado["adjuntos"] = existing_adjuntos + resource_attachments

            contexto_chat["demo_intro_sent"] = True
            flag_modified(chat_context_obj, "context_data")

        # Después de que responder_chatboc y sus sub-funciones hayan modificado chat_context_obj.context_data,
        # lo persistimos.
        
        # Marcar explícitamente context_data como modificado para SQLAlchemy
        if chat_context_obj:
            # Importar la función de serialización
            from services.municipio_responder import serializar_enum, CONTEXTO_MUNICIPIO # CONTEXTO_MUNICIPIO for logging clarity

            # Serializar el context_data COMPLETO antes de marcarlo como modificado y hacer commit
            if chat_context_obj.context_data:
                # Log an example of what's in 'estado_conversacion' before and after, if it exists
                # This is for debugging the specific issue observed.
                raw_municipio_context = chat_context_obj.context_data.get(CONTEXTO_MUNICIPIO, {})
                state_before_global_serialization = raw_municipio_context.get("estado_conversacion", "N/A_in_sub_context")

                # Also check top-level estado_conversacion if it exists
                top_level_state_before = chat_context_obj.context_data.get("estado_conversacion", "N/A_top_level")

                chat_context_obj.context_data = serializar_enum(chat_context_obj.context_data)

                # Log after serialization
                serialized_municipio_context = chat_context_obj.context_data.get(CONTEXTO_MUNICIPIO, {})
                state_after_global_serialization = serialized_municipio_context.get("estado_conversacion", "N/A_in_sub_context_after")
                top_level_state_after = chat_context_obj.context_data.get("estado_conversacion", "N/A_top_level_after")

                current_app.logger.info(f"ChatSessionContext Serialization: TopLevelState before='{top_level_state_before}', after='{top_level_state_after}'. SubContextState before='{state_before_global_serialization}', after='{state_after_global_serialization}'.")

            flag_modified(chat_context_obj, "context_data")
            current_app.logger.info(f"ChatSessionContext.context_data (post-serialization) marcado como modificado para {chat_session_id_header}.")

        try:
            commit_with_retry(db.session) # Commit principal para ChatSessionContext y User.preguntas_usadas
            current_app.logger.info(f"ChatSessionContext para {chat_session_id_header} guardado/actualizado en DB (Commit Principal).")
        except Exception as e_commit:
            db.session.rollback()
            current_app.logger.error(f"Error en Commit Principal (ChatSessionContext) para {chat_session_id_header}: {e_commit}", exc_info=True)
            # La respuesta al usuario ya se formó, pero el contexto no se guardó.

        es_publico = es_rubro_publico(rubro_obj_global)
        nombre_rubro_log = getattr(rubro_obj_global, "clave", "N/A") if rubro_obj_global else "N/A"

        current_app.logger.info(
            f"[RUBROS] Rubro efectivo: '{nombre_rubro_log}' (ID: {getattr(rubro_obj_global, 'id', 'N/A')}), esPublico={es_publico}"
        )

        if owner_del_bot:
            owner_del_bot.preguntas_usadas += 1

        if isinstance(resultado, dict):
            message_body = resultado.get("message_body")
            respuesta = resultado.get("respuesta")
            if message_body and not respuesta:
                resultado["respuesta"] = message_body
            elif respuesta and not message_body:
                resultado["message_body"] = respuesta

            resultado["es_publico"] = es_publico
            if owner_del_bot:
                from utils.plan_limits import limite_para_usuario
                resultado["preguntas_usadas"] = owner_del_bot.preguntas_usadas
                resultado["limite_preguntas"] = limite_para_usuario(owner_del_bot)
            if interpretacion_imagen_resultado and not interpretacion_imagen_resultado.get("error"):
                resultado["interpretacion_adjunto"] = interpretacion_imagen_resultado

        # ... (previous commit for ChatSessionContext) ...

        # The 'resultado' dictionary from responder_chatboc is now structured
        # exactly as the LLM specified, which is what the frontend expects.

        # --- Audio Synthesis Step ---
        # If the response indicates that audio should be generated, do it now.
        if isinstance(resultado, dict) and resultado.get("generar_audio"):
            from services.google_text_to_speech import TextToSpeechService
            tts_service = TextToSpeechService()
            # The text to synthesize can be in 'message_body' (municipio) or 'respuesta' (pyme)
            text_to_synthesize = resultado.get("message_body") or resultado.get("respuesta")
            if text_to_synthesize:
                try:
                    audio_url = tts_service.synthesize_speech(text_to_synthesize)
                    if audio_url:
                        resultado["audio_url"] = audio_url
                        current_app.logger.info(f"Audio generado y añadido a la respuesta: {audio_url}")
                except Exception as e:
                    # Log the error, but don't crash the main response flow
                    current_app.logger.error(f"Error durante la síntesis de voz: {e}", exc_info=True)

        # We just need to pass it through after adding any necessary metadata.

        if isinstance(resultado, tuple):
            # Handle error cases where responder_chatboc returns a tuple
            error_message, status_code = resultado
            return jsonify(error_message), status_code

        if not isinstance(resultado, dict):
            # Fallback for unexpected response types
            current_app.logger.error(f"Unexpected response type from responder_chatboc: {type(resultado)}")
            resultado = {"message_body": "Ocurrió un error inesperado en el servidor."}


        # Add metadata to the response
        message_body = resultado.get("message_body") if isinstance(resultado, dict) else None
        respuesta = resultado.get("respuesta") if isinstance(resultado, dict) else None
        if message_body and not respuesta:
            resultado["respuesta"] = message_body
        elif respuesta and not message_body:
            resultado["message_body"] = respuesta

        resultado["es_publico"] = es_publico
        if owner_del_bot:
            from utils.plan_limits import limite_para_usuario
            resultado["preguntas_usadas"] = owner_del_bot.preguntas_usadas
            resultado["limite_preguntas"] = limite_para_usuario(owner_del_bot)

        if interpretacion_imagen_resultado and not interpretacion_imagen_resultado.get("error"):
            resultado["interpretacion_adjunto"] = interpretacion_imagen_resultado

        if isinstance(resultado, dict):
            audio_url = resultado.get("audio_url")
            if audio_url and channel == "web" and "audio" not in resultado:
                resultado["audio"] = {"link": audio_url}

        ensure_buttons_compatibility(resultado)

        # Si el usuario es anónimo y la acción requiere datos personales, pedirlos
        if is_anonymous and resultado and resultado.get("accion_backend") in ["crear_reclamo", "iniciar_reclamo"] and not (resultado.get("datos_estructura", {}).get("nombre_usuario_detectado") and resultado.get("datos_estructura", {}).get("telefono_detectado") and resultado.get("datos_estructura", {}).get("email_detectado")):
            resultado['pedir_info'] = ["nombre", "telefono", "email"]

        # Guardar datos del último mensaje para evitar duplicados
        if chat_context_obj:
            chat_context_obj.context_data["last_user_message"] = pregunta
            chat_context_obj.context_data["last_user_message_time"] = datetime.utcnow().isoformat()
            chat_context_obj.context_data["last_bot_response"] = resultado
            flag_modified(chat_context_obj, "context_data")

        # This commit is for User.preguntas_usadas and ChatSessionContext primarily
        try:
            commit_with_retry(db.session)
        except Exception as e:
            db.session.rollback()
            current_app.logger.error(f"Error during final commit: {e}", exc_info=True)
            return jsonify({"error": "Error interno del servidor al guardar la sesión."}), 500

        # Emit the result via Socket.IO if the channel is web
        if channel == "web" and chat_session_id_header:
            ensure_buttons_compatibility(resultado)

            socketio.emit('message', resultado, room=chat_session_id_header)
            current_app.logger.debug(
                "Emitting socket message",
                extra={"room": chat_session_id_header, "payload": resultado},
            )
            current_app.logger.info(
                f"Emitted socket event 'message' to room {chat_session_id_header}"
            )

        current_app.logger.debug(
            "Returning HTTP response", extra={"payload": resultado}
        )
        return jsonify(resultado), 200

    except Exception as e:
        db.session.rollback()
        error_details = {
            "pregunta": pregunta if 'pregunta' in locals() else 'N/A',
            "tipo_chat": tipo_chat if 'tipo_chat' in locals() else 'N/A',
            "rubro_id": rubro_id if 'rubro_id' in locals() else 'N/A',
            "rubro_clave": rubro_clave if 'rubro_clave' in locals() else 'N/A',
            "actor_principal_id": actor_principal.id if 'actor_principal' in locals() and actor_principal else 'N/A',
            "owner_del_bot_id": owner_del_bot.id if 'owner_del_bot' in locals() and owner_del_bot else 'N/A',
            "viewer_obj_id": viewer_obj.id if 'viewer_obj' in locals() and viewer_obj else 'N/A',
            "anon_id": anon_id if 'anon_id' in locals() else 'N/A',
            "archivo_adjunto_id": archivo_adjunto_id if 'archivo_adjunto_id' in locals() else 'N/A',
            "uploaded_file_info": uploaded_file_info if 'uploaded_file_info' in locals() else 'N/A',
            "session_chat_id": session_chat_id if 'session_chat_id' in locals() else 'N/A'
        }
        current_app.logger.error(
            f"❌ Error crítico en _procesar_chat. Details: {error_details}. Exception: {e}",
            exc_info=True
        )
        return jsonify({"error": "Error interno del servidor."}), 500

@chat_bp.route("/ask", methods=["POST", "OPTIONS"])
@anon_o_token_requerido
def ask(current_user=None, anon_id=None, owner_user=None):
    user = owner_user or current_user
    return _procesar_chat(current_user=current_user, owner_user=user, anon_id=anon_id)

@chat_bp.route("/ask/pyme", methods=["POST", "OPTIONS"])
@anon_o_token_requerido
def ask_pyme(current_user=None, anon_id=None, owner_user=None):
    user = owner_user or current_user
    return _procesar_chat("pyme", current_user=current_user, owner_user=user, anon_id=anon_id)

@chat_bp.route("/ask/municipio", methods=["POST", "OPTIONS"])
@anon_o_token_requerido
def ask_municipio(current_user=None, anon_id=None, owner_user=None):
    user = owner_user or current_user
    return _procesar_chat("municipio", current_user=current_user, owner_user=user, anon_id=anon_id)


@chat_bp.route("/profile-name", methods=["POST", "OPTIONS"])
def set_profile_name():
    """Store the visitor's profile name in cookies and session context."""
    if request.method == "OPTIONS":
        return jsonify({}), 200

    data = request.get_json(silent=True) or {}
    profile_name = data.get("nombre_usuario") or data.get("profile_name")
    if not profile_name:
        return jsonify({"error": "'nombre_usuario' requerido"}), 400

    resp = jsonify({"profile_name": profile_name})
    resp.set_cookie(
        "nombre_usuario",
        profile_name,
        max_age=60 * 60 * 24 * 30,
        samesite=current_app.config.get("SESSION_COOKIE_SAMESITE", "None"),
        secure=current_app.config.get("SESSION_COOKIE_SECURE", True),
    )

    chat_session_id = request.headers.get("X-Chat-Session-Id")
    if chat_session_id:
        chat_context_obj = ChatSessionContext.query.filter_by(chat_session_id=chat_session_id).first()
        if not chat_context_obj:
            chat_context_obj = ChatSessionContext(chat_session_id=chat_session_id, context_data={})
            db.session.add(chat_context_obj)
        if chat_context_obj.context_data is None:
            chat_context_obj.context_data = {}
        chat_context_obj.context_data["profile_name"] = profile_name
        flag_modified(chat_context_obj, "context_data")
        commit_with_retry(db.session)

    return resp, 200

@chat_bp.route("/widget/attention", methods=["GET"])
def widget_attention():
    opciones = current_app.config.get("ATTENTION_BUBBLE_CHOICES")
    if opciones:
        mensaje = random.choice(opciones)
    else:
        mensaje = current_app.config.get(
            "ATTENTION_BUBBLE_TEXT", "¡Hola! ¿Necesitas ayuda?"
        )
    return jsonify({"mensaje": mensaje})


@chat_bp.route("/widget/config", methods=["GET"])
def widget_config():
    token = obtener_token()
    if not token:
        return jsonify({"error": "Token requerido"}), 400

    user = user_from_token(token)
    if not user:
        user = User.query.filter_by(token=token).first()
    if not user:
        return jsonify({"error": "Token inválido"}), 404

    config = {
        "nombre_empresa": user.nombre_empresa or user.name or "",
        "logo_url": user.logo_url or "",
        "color_primario": user.color_primario or "#000000",
        "color_secundario": user.color_secundario or "#FFFFFF",
        "badge_tipo": user.badge_tipo or "",
        "widget_icon_url": user.widget_icon_url or "",
        "widget_animation": user.widget_animation or "",
    }

    return jsonify(config)

@chat_bp.route("/config/google-maps-key", methods=["GET"])
def google_maps_key():
    return jsonify({"google_maps_key": current_app.config.get("GOOGLE_MAPS_API_KEY")})
