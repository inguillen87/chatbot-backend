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
from services.logic import (
    responder_chatboc,
    RUBROS_PUBLICOS,
    normalizar_rubro,
    es_rubro_publico,
)
from .auth import anon_o_token_requerido
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
        if not pregunta:
            raise ValueError("Falta el campo 'pregunta'")

        if tipo_chat_fijo:
            tipo_chat = tipo_chat_fijo
        else:
            tipo_chat = _normalizar_tipo_chat(data.get("tipo_chat"))
            if tipo_chat not in ("pyme", "municipio"):
                raise ValueError("'tipo_chat' debe ser 'pyme' o 'municipio'")

        contexto_previo = data.get("contexto_previo")
        rubro_id = data.get("rubro_id")
        rubro_clave = data.get("rubro_clave")
        uploaded_file_info = data.get("uploaded_file_info")
        archivo_adjunto_id = data.get("archivo_adjunto_id")
        location = data.get("location")

        if uploaded_file_info and not (
            isinstance(uploaded_file_info, dict) and
            "url" in uploaded_file_info and
            "name" in uploaded_file_info
        ):
            raise ValueError("El campo 'uploaded_file_info' es inválido.")

        if archivo_adjunto_id and not isinstance(archivo_adjunto_id, int):
            raise ValueError("El campo 'archivo_adjunto_id' debe ser un entero.")

        if location and not (
            isinstance(location, dict) and
            "lat" in location and
            "lon" in location
        ):
            raise ValueError("El campo 'location' es inválido.")

        return pregunta, contexto_previo, tipo_chat, rubro_id, rubro_clave, uploaded_file_info, archivo_adjunto_id, location, None

    except (TypeError, ValueError) as e:
        current_app.logger.warning(f"Error al parsear /ask: {e}")
        return (
            None, None, None, None, None, None, None, None,
            jsonify({"error": str(e)}),
        )
    except Exception as e:
        current_app.logger.error(f"Error inesperado al parsear /ask: {e}")
        return (
            None, None, None, None, None, None, None, None,
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
    try:
        (
            pregunta,
            contexto_previo,
            tipo_chat,
            rubro_id,
            rubro_clave,
            uploaded_file_info,
            archivo_adjunto_id,
            location,
            error_response,
        ) = _parse_request(tipo_chat_fijo)
        if error_response:
            return error_response, 400

        # Determinar el actor principal y el tipo de usuario
        actor_principal = owner_user or current_user
        is_anonymous = not actor_principal
        viewer_obj = current_user # El que mira

        if is_anonymous and not anon_id:
            return jsonify({"error": "No autenticado o identificado."}), 401

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

        analisis_archivo_resultado = None
        if uploaded_file_info and archivo_adjunto_id:
            from models import ArchivoAdjunto
            from services.analisis_archivo_service import tarea_analizar_contenido_archivo
            from services.image_processing_service import image_processing_service
            import requests

            archivo_obj = db.session.get(ArchivoAdjunto, archivo_adjunto_id)
            if archivo_obj:
                current_app.logger.info(f"Iniciando análisis de archivo adjunto ID: {archivo_adjunto_id} para chat tipo: {tipo_chat}")

                if uploaded_file_info.get("mime_type", "").startswith("image/"):
                    try:
                        response = requests.get(uploaded_file_info["url"])
                        response.raise_for_status()
                        image_content = response.content
                        analisis_archivo_resultado = image_processing_service.analyze_image(image_content)
                    except Exception as e:
                        current_app.logger.error(f"Error al procesar la imagen: {e}", exc_info=True)
                else:
                    # Llamar a la tarea de Celery de forma asíncrona para otros tipos de archivo
                    tarea_analizar_contenido_archivo.delay(archivo_adjunto_id)
                    current_app.logger.info(f"Tarea de análisis para archivo {archivo_adjunto_id} encolada.")
            else:
                current_app.logger.error(f"No se encontró ArchivoAdjunto con ID {archivo_adjunto_id} en la DB.")

        # Import uuid al inicio del archivo si no está ya
        import uuid
        from models import ChatSessionContext

        # Leer el X-Chat-Session-Id del header
        chat_session_id_header = request.headers.get("X-Chat-Session-Id")

        if not chat_session_id_header:
            # Fallback: Generar un nuevo ID si no viene en el header.
            # Idealmente, el frontend SIEMPRE debería enviarlo.
            chat_session_id_header = str(uuid.uuid4())
            current_app.logger.warning(f"X-Chat-Session-Id no encontrado en headers. Generando uno nuevo: {chat_session_id_header}")

        current_app.logger.info(f"Usando Chat Session ID (from header or generated): {chat_session_id_header}")

        # Cargar o crear el contexto de la base de datos
        chat_context_obj = ChatSessionContext.query.get(chat_session_id_header)
        if not chat_context_obj:
            current_app.logger.info(f"No se encontró ChatSessionContext para {chat_session_id_header}. Creando uno nuevo.")
            chat_context_obj = ChatSessionContext(
                chat_session_id=chat_session_id_header,
                user_id=getattr(actor_principal, 'id', None), # Asociar con usuario logueado si existe
                anon_id=anon_id if not actor_principal else None, # Asociar con anon_id si no hay usuario logueado
                context_data={} # Inicializar con datos vacíos
            )
            db.session.add(chat_context_obj)
            # No hacer commit aquí todavía, se hará después de procesar el chat
        else:
            current_app.logger.info(f"ChatSessionContext cargado para {chat_session_id_header}. User_id: {chat_context_obj.user_id}, Anon_id: {chat_context_obj.anon_id}")
            # Actualizar user_id o anon_id si es necesario (ej. usuario anónimo inicia sesión)
            if actor_principal and chat_context_obj.user_id != actor_principal.id:
                current_app.logger.info(f"Actualizando user_id en ChatSessionContext {chat_session_id_header} de {chat_context_obj.user_id} a {actor_principal.id}")
                chat_context_obj.user_id = actor_principal.id
                chat_context_obj.anon_id = None # Limpiar anon_id si se asocia a un usuario
            elif not actor_principal and anon_id and chat_context_obj.anon_id != anon_id:
                current_app.logger.info(f"Actualizando anon_id en ChatSessionContext {chat_session_id_header} de {chat_context_obj.anon_id} a {anon_id}")
                chat_context_obj.anon_id = anon_id
                # No limpiar user_id aquí, podría ser un usuario que cerró sesión y sigue como anónimo con el mismo session_id


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
            uploaded_file_info=uploaded_file_info,
            interpretacion_imagen_data=analisis_archivo_resultado,
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

        # Format the response for the web channel using the formatter
        from services.response_formatter import build_interactive_response

        # Ensure 'respuesta' and 'botones' are correctly populated in 'resultado'
        # by the formatter, using the new structured fields.
        web_body = resultado.get('message_body', resultado.get('respuesta', 'Error al procesar')) # Fallback
        web_options = resultado.get('options_list', resultado.get('botones', [])) # Fallback
        web_message_type = resultado.get('message_type', 'text')
        if web_options and web_message_type == 'text': # If options are present, it should be an interactive type
            web_message_type = 'interactive_buttons' # Default for web if options exist

        formatted_web_response = build_interactive_response(
            options=web_options,
            body_text=web_body,
            channel="web",
            message_type=web_message_type,
            original_bot_response=resultado # Pass the full dict from responder_chatboc
        )
        if isinstance(resultado, dict) and "fuente" in resultado:
            formatted_web_response["fuente"] = resultado["fuente"]

        # Guardar datos del último mensaje para evitar duplicados
        if chat_context_obj:
            chat_context_obj.context_data["last_user_message"] = pregunta
            chat_context_obj.context_data["last_user_message_time"] = datetime.utcnow().isoformat()
            chat_context_obj.context_data["last_bot_response"] = formatted_web_response
            flag_modified(chat_context_obj, "context_data")

        # This commit is for User.preguntas_usadas primarily, and any other DB changes by responder_chatboc
        db.session.commit()
        return jsonify(formatted_web_response), 200

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
