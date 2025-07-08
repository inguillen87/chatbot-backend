import logging
import random
import uuid # Added for chat_session_id generation
from flask import Blueprint, request, jsonify, current_app
from sqlalchemy import func, desc
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

        if uploaded_file_info and not (
            isinstance(uploaded_file_info, dict) and
            "url" in uploaded_file_info and
            "name" in uploaded_file_info
        ):
            raise ValueError("El campo 'uploaded_file_info' es inválido.")

        if archivo_adjunto_id and not isinstance(archivo_adjunto_id, int):
            raise ValueError("El campo 'archivo_adjunto_id' debe ser un entero.")

        return pregunta, contexto_previo, tipo_chat, rubro_id, rubro_clave, uploaded_file_info, archivo_adjunto_id, None

    except (TypeError, ValueError) as e:
        current_app.logger.warning(f"Error al parsear /ask: {e}")
        return (
            None, None, None, None, None, None, None,
            jsonify({"error": str(e)}),
        )
    except Exception as e:
        current_app.logger.error(f"Error inesperado al parsear /ask: {e}")
        return (
            None, None, None, None, None, None, None,
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
            error_response,
        ) = _parse_request(tipo_chat_fijo)
        if error_response:
            return error_response, 400

        received_cookies = request.cookies
        current_app.logger.info(f"Received cookies: {received_cookies}")
        flask_session_cookie_name = current_app.config.get("SESSION_COOKIE_NAME", "session")
        if flask_session_cookie_name in received_cookies:
            current_app.logger.info(f"Flask session cookie '{flask_session_cookie_name}' received.")
        else:
            current_app.logger.warning(f"Flask session cookie '{flask_session_cookie_name}' NOT received.")

        owner_obj = owner_user or current_user
        viewer_obj = current_user
        actor_principal = owner_obj or viewer_obj

        if not actor_principal and not anon_id:
            return jsonify({"error": "No autenticado o identificado."}), 401

        if anon_id and not actor_principal:
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

        interpretacion_imagen_resultado = None
        if uploaded_file_info and archivo_adjunto_id:
            from models import ArchivoAdjunto
            from services.interpretacion_imagen_service import interpretar_imagen_para_chat

            archivo_obj = db.session.get(ArchivoAdjunto, archivo_adjunto_id)
            if archivo_obj:
                current_app.logger.info(f"Procesando imagen adjunta ID: {archivo_adjunto_id} para chat tipo: {tipo_chat}")

                tipo_interpretacion_img = None
                pyme_owner_para_pedido = None

                if tipo_chat == "pyme":
                    tipo_interpretacion_img = "pedido_pyme"
                    pyme_owner_para_pedido = owner_del_bot
                    if not pyme_owner_para_pedido:
                        current_app.logger.warning(f"No se pudo determinar el owner PYME para interpretar pedido de imagen {archivo_adjunto_id}. Se intentará con el usuario actual si es PYME.")
                elif tipo_chat == "municipio":
                    tipo_interpretacion_img = "reclamo_municipal"

                if tipo_interpretacion_img:
                    interpretacion_imagen_resultado = interpretar_imagen_para_chat(
                        archivo_obj,
                        tipo_interpretacion_img,
                        pyme_user=pyme_owner_para_pedido if tipo_interpretacion_img == "pedido_pyme" else None
                    )
                    current_app.logger.info(f"Resultado interpretación imagen: {interpretacion_imagen_resultado}")
                else:
                    current_app.logger.warning(f"Tipo de chat '{tipo_chat}' no tiene interpretación de imagen definida.")
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

        # El objeto `chat_context_obj.context_data` será el que se pase y modifique
        # en lugar de `flask_request_session` para el contexto específico del chat.

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
            uploaded_file_info=uploaded_file_info,
            interpretacion_imagen_data=interpretacion_imagen_resultado
        )

        # Después de que responder_chatboc y sus sub-funciones hayan modificado chat_context_obj.context_data,
        # lo persistimos.
        try:
            db.session.commit()
            current_app.logger.info(f"ChatSessionContext para {chat_session_id_header} guardado/actualizado en DB.")
        except Exception as e_commit:
            db.session.rollback()
            current_app.logger.error(f"Error al hacer commit de ChatSessionContext para {chat_session_id_header}: {e_commit}", exc_info=True)
            # Considerar si devolver un error al usuario o si el chat puede continuar con contexto en memoria
            # por esta vez. Por ahora, la respuesta del chat ya se formó, así que continuamos.

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

        db.session.commit()
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
