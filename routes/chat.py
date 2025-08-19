import sys
import os
import logging
import random
import uuid  # Added for chat_session_id generation

# Add project root to sys.path for this routes file
project_root_chat_routes = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if project_root_chat_routes not in sys.path:
    sys.path.insert(0, project_root_chat_routes)

from flask import Blueprint, request, jsonify, current_app
from sqlalchemy import func, desc
from sqlalchemy.orm.attributes import flag_modified # Importado para flag_modified
from models import User, Rubro, Conversacion, db, ChatSessionContext # Added ChatSessionContext
from socket_service import socketio # Import socketio
from services.logic import (
    responder_chatboc,
    RUBROS_PUBLICOS,
    normalizar_rubro,
    es_rubro_publico,
)
from utils.auth_helpers import anon_o_token_requerido
from datetime import datetime, timedelta

chat_bp = Blueprint("chat_bp", __name__)

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
        # Si no hay pregunta pero sí ubicación, es válido. Se generará una pregunta sintética más adelante.
        if not pregunta and not location:
            raise ValueError("La solicitud debe contener al menos un campo 'pregunta' o 'location'.")

        if tipo_chat_fijo:
            tipo_chat = tipo_chat_fijo
        else:
            tipo_chat = _normalizar_tipo_chat(data.get("tipo_chat"))
            if tipo_chat not in ("pyme", "municipio"):
                raise ValueError("'tipo_chat' debe ser 'pyme' o 'municipio'")

        contexto_previo = data.get("contexto_previo")
        rubro_id = data.get("rubro_id")
        rubro_clave = data.get("rubro_clave")
        attachment_info = data.get("attachment_info")
        location = data.get("location")
        ticket_id = data.get("ticket_id")
        tipo_ticket = data.get("tipo_ticket")


        if attachment_info:
            if not isinstance(attachment_info, dict) or not all(k in attachment_info for k in ['id', 'url', 'name', 'mimeType', 'size']):
                raise ValueError("El campo 'attachment_info' es inválido o le faltan campos requeridos.")

        if location and not (
            isinstance(location, dict) and
            "lat" in location and
            "lon" in location
        ):
            raise ValueError("El campo 'location' es inválido.")

        return pregunta, contexto_previo, tipo_chat, rubro_id, rubro_clave, attachment_info, location, ticket_id, tipo_ticket, None

    except (TypeError, ValueError) as e:
        current_app.logger.warning(f"Error al parsear /ask: {e}")
        return (
            None, None, None, None, None, None, None, None, None,
            jsonify({"error": str(e)}),
        )
    except Exception as e:
        current_app.logger.error(f"Error inesperado al parsear /ask: {e}")
        return (
            None, None, None, None, None, None, None, None, None,
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
    # --- Session and Context Initialization ---
    chat_session_id_header = request.headers.get("X-Chat-Session-Id")
    if not chat_session_id_header:
        chat_session_id_header = str(uuid.uuid4())
        current_app.logger.warning(f"X-Chat-Session-Id not found. Generated new: {chat_session_id_header}")

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
        else:
            return jsonify({"error": "Audio file is empty."}), 400
    else:
        chat_context_obj.context_data.pop('source_is_audio', None) # Remove flag if it's a text message
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
                error_response,
            ) = _parse_request(tipo_chat_fijo)
            if error_response:
                return error_response, 400

        # Si la solicitud solo contenía una ubicación, creamos una pregunta sintética para que el backend la procese.
        if not pregunta and location:
            pregunta = "[Ubicación compartida por el usuario]"
        except Exception as e:
            return jsonify({"error": f"Invalid request format: {e}"}), 400

        current_app.logger.debug(
            "Parsed request data",
            extra={
                "pregunta": pregunta,
                "tipo_chat": tipo_chat,
                "rubro_id": rubro_id,
                "rubro_clave": rubro_clave,
                "attachment_info": attachment_info,
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
                db.session.commit() # Commit the new comment
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

        # The logic for file analysis has been moved to the upload endpoint.
        # The chat endpoint is only responsible for passing the attachment_info.
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
                        return jsonify(last_resp), 200
            except Exception:
                pass

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
            user_data={
                "name": actor_principal.name,
                "email": actor_principal.email,
                "telefono": actor_principal.telefono
            } if actor_principal else None
        )

        # Después de que responder_chatboc y sus sub-funciones hayan modificado chat_context_obj.context_data,
        # lo persistimos.
        
        # Marcar explícitamente context_data como modificado para SQLAlchemy
        if chat_context_obj:
            # Importar la función de serialización
            from services.municipios import serializar_enum, CONTEXTO_MUNICIPIO # CONTEXTO_MUNICIPIO for logging clarity

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
            db.session.commit() # Commit principal para ChatSessionContext y User.preguntas_usadas
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
        resultado["es_publico"] = es_publico
        if owner_del_bot:
            from utils.plan_limits import limite_para_usuario
            resultado["preguntas_usadas"] = owner_del_bot.preguntas_usadas
            resultado["limite_preguntas"] = limite_para_usuario(owner_del_bot)

        if interpretacion_imagen_resultado and not interpretacion_imagen_resultado.get("error"):
            resultado["interpretacion_adjunto"] = interpretacion_imagen_resultado

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
            db.session.commit()
        except Exception as e:
            db.session.rollback()
            current_app.logger.error(f"Error during final commit: {e}", exc_info=True)
            return jsonify({"error": "Error interno del servidor al guardar la sesión."}), 500

        # Emit the result via Socket.IO if the channel is web
        if channel == "web" and chat_session_id_header:
            # FIX: Ensure 'botones' key is present for the frontend if 'options_list' exists.
            # The frontend widget expects 'botones', but many backend handlers generate 'options_list'.
            if resultado and isinstance(resultado, dict) and 'options_list' in resultado and 'botones' not in resultado:
                resultado['botones'] = resultado['options_list']
                current_app.logger.info("Copiando 'options_list' a 'botones' para compatibilidad con el frontend.")

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

@chat_bp.route("/config/google-maps-key", methods=["GET"])
def google_maps_key():
    return jsonify({"google_maps_key": current_app.config.get("GOOGLE_MAPS_API_KEY")})
