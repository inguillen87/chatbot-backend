# routes/chat.py
import logging
import random
from flask import Blueprint, request, jsonify, current_app
from sqlalchemy import func, desc
from models import User, Rubro, Conversacion, db
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
    """Obtiene y valida los campos comunes del cuerpo JSON."""
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

        contexto_previo = data.get("contexto_previo")  # Leemos la mochila
        rubro_id = data.get("rubro_id")
        rubro_clave = data.get("rubro_clave")
        uploaded_file_info = data.get("uploaded_file_info") # Extract uploaded file info
        archivo_adjunto_id = data.get("archivo_adjunto_id") # Nuevo: ID del ArchivoAdjunto si ya se creó

        # Validate uploaded_file_info structure if present
        if uploaded_file_info and not (
            isinstance(uploaded_file_info, dict) and
            "url" in uploaded_file_info and
            "name" in uploaded_file_info # Podríamos añadir 'id' aquí si el frontend lo manda
        ):
            raise ValueError("El campo 'uploaded_file_info' es inválido.")

        if archivo_adjunto_id and not isinstance(archivo_adjunto_id, int):
            raise ValueError("El campo 'archivo_adjunto_id' debe ser un entero.")

        return pregunta, contexto_previo, tipo_chat, rubro_id, rubro_clave, uploaded_file_info, archivo_adjunto_id, None

    except (TypeError, ValueError) as e:
        current_app.logger.warning(f"Error al parsear /ask: {e}")
        return (
            None,
            None,
            None,
            None,
            None,
            None, # para archivo_adjunto_id
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
            None, # para archivo_adjunto_id
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
    """Procesa una pregunta garantizando coherencia entre rubro y tipo_chat.

    Se usa en /ask, /ask/pyme y /ask/municipio. La estética y la lógica se
    determinan exclusivamente por el rubro y nunca se mezclan las de pyme con
    las de municipio. Si el rubro indica un tipo diferente al recibido, se
    ajusta y se registra un log; si la información es inconsistente, se
    devuelve un error claro.
    """
    try:
        (
            pregunta,
            contexto_previo,
            tipo_chat,
            rubro_id,
            rubro_clave,
            uploaded_file_info,
            archivo_adjunto_id, # Añadido
            error_response,
        ) = _parse_request(tipo_chat_fijo)
        if error_response:
            return error_response, 400

        # --- Autenticación y obtención de usuario ---
        # owner_obj es el dueño del bot/configuración (ej. la PYME o el Municipio)
        # viewer_obj es el usuario que está chateando (puede ser el mismo owner, un empleado, o un anónimo)
        owner_obj = owner_user or current_user # Si hay token de "owner" (ej. widget embebido con clave API de pyme)
        viewer_obj = current_user # El usuario autenticado por token JWT estándar, o None si es anónimo

        # Si no hay owner_user (clave API específica del bot) Y no hay current_user (sesión JWT),
        # Y además no hay anon_id, entonces no hay forma de identificar al que pregunta.
        # El decorador @anon_o_token_requerido ya debería manejar que current_user o anon_id existan.
        # La lógica de owner_obj es más para cuando el "bot" mismo es el dueño (ej. una PYME interactuando con su propio bot)
        # o cuando se usa una clave API específica del "bot" para identificarlo.

        # Simplificación: si hay owner_user (pasado por el decorador si el token es de un User que es owner), usarlo.
        # Si no, usar current_user (si está autenticado).
        # Si es anónimo, owner_obj será None aquí, lo cual es correcto.
        # El `responder_chatboc` y otros servicios deben manejar owner_obj=None para anónimos.

        # El owner_obj real (dueño del rubro/configuración) se determinará más adelante basado en rubro_id/clave,
        # o si el current_user tiene un rubro propio.
        # Por ahora, 'actor_principal' es quien realiza la acción o a quien se le atribuye (si está logueado).
        actor_principal = owner_obj or viewer_obj


        if not actor_principal and not anon_id: # Doble chequeo, aunque @anon_o_token_requerido debería cubrirlo
            return jsonify({"error": "No autenticado o identificado."}), 401

        # --- CONTROL DE LÍMITES PARA USUARIOS ANÓNIMOS ---
        if anon_id and not actor_principal: # Es anónimo
            max_messages = current_app.config.get("ANONYMOUS_MAX_MESSAGES_PER_SESSION", 10)
            session_timeout_minutes = current_app.config.get("ANONYMOUS_SESSION_TIMEOUT_MINUTES", 15)

            # Verificar timeout de sesión
            last_message_time = db.session.query(func.max(Conversacion.timestamp))\
                .filter(Conversacion.session_id == anon_id)\
                .scalar() # Usamos session_id para anon_id como se discutió

            session_expired = False
            if last_message_time:
                if datetime.utcnow() - last_message_time > timedelta(minutes=session_timeout_minutes):
                    session_expired = True
                    current_app.logger.info(f"Sesión anónima {anon_id} expirada. Reiniciando conteo de mensajes.")

            # Contar mensajes en la sesión actual (o todos si no hay timeout estricto por ahora)
            # Si la sesión expiró, el conteo efectivo es 0 para la nueva "sesión" (aunque el anon_id sea el mismo).
            if not session_expired:
                message_count_this_session = Conversacion.query\
                    .filter(Conversacion.session_id == anon_id)\
                    .filter(Conversacion.timestamp >= datetime.utcnow() - timedelta(minutes=session_timeout_minutes))\
                    .count() # Contamos cada entrada en Conversacion como un mensaje (pregunta o respuesta)
                             # O podríamos contar solo las preguntas (user_id es None)

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
            # Nota: El incremento de `preguntas_usadas` para anónimos (si se implementa un contador global)
            # o el registro de la `Conversacion` (que implícitamente cuenta) ocurrirá después de que `responder_chatboc` tenga éxito.

        # --- Determinación del Rubro y Owner real del Bot ---
        # El `owner_del_bot` es el User (PYME o Municipio) cuya configuración se usa.
        # Puede ser encontrado por `rubro_id`, `rubro_clave`, o si el `actor_principal` (usuario logueado) tiene un rubro.
        rubro_obj_global = None # Rubro que se usará para la lógica del bot.
        owner_del_bot = None    # Usuario dueño de ese rubro/bot.
        rubro_para_log = None   # For logging the rubro name/clave

        if rubro_id: # Frontend explicitly provided a rubro_id
            rubro_obj_global = Rubro.query.get(rubro_id)
            if rubro_obj_global:
                rubro_para_log = rubro_obj_global.nombre or rubro_obj_global.clave
                # Find the User who owns this rubro (typically an admin user with empresa_id=None)
                owner_del_bot = User.query.filter_by(rubro_id=rubro_obj_global.id, empresa_id=None).first()
                if not owner_del_bot:
                    # Fallback: maybe an admin user is linked via rol='admin' if empresa_id logic isn't strict for owners
                    owner_del_bot = User.query.filter_by(rubro_id=rubro_obj_global.id, rol='admin').first()
        elif rubro_clave: # Frontend explicitly provided a rubro_clave
            rubro_obj_global = Rubro.query.filter(func.lower(Rubro.clave) == func.lower(rubro_clave)).first() # Ensure rubro_clave is lowercased for query
            if rubro_obj_global:
                rubro_para_log = rubro_obj_global.nombre or rubro_obj_global.clave
                owner_del_bot = User.query.filter_by(rubro_id=rubro_obj_global.id, empresa_id=None).first()
                if not owner_del_bot:
                    owner_del_bot = User.query.filter_by(rubro_id=rubro_obj_global.id, rol='admin').first()
        elif actor_principal and actor_principal.rubro_id: # User is logged in and has an associated rubro_id
            # It's better to query Rubro again using actor_principal.rubro_id to ensure rubro_obj_global is a Rubro instance
            rubro_obj_global = Rubro.query.get(actor_principal.rubro_id)
            if rubro_obj_global:
                 rubro_para_log = rubro_obj_global.nombre or rubro_obj_global.clave
            # If the logged-in user has a rubro, they are considered the owner of that bot interaction
            # if their empresa_id is None (they are the company/admin account itself)
            if actor_principal.empresa_id is None:
                owner_del_bot = actor_principal
            else:
                # If logged-in user is an employee/client, find the actual owner of their rubro
                if rubro_obj_global: # Ensure rubro_obj_global was found
                    owner_del_bot = User.query.filter_by(rubro_id=rubro_obj_global.id, empresa_id=None).first()
                    if not owner_del_bot:
                        owner_del_bot = User.query.filter_by(rubro_id=rubro_obj_global.id, rol='admin').first()
        
        if not owner_del_bot and rubro_obj_global : # Rubro was found, but no specific owner user for it.
             current_app.logger.warning(f"Rubro ID {rubro_obj_global.id} ('{rubro_para_log}') encontrado pero sin User owner asociado (empresa_id=None o rol=admin). Se continuará sin owner específico si el rubro es público.")
             # For public rubros, owner_del_bot might remain None, and responder_chatboc should handle this.

        if rubro_obj_global:
            nombre_rubro_log = rubro_para_log or getattr(rubro_obj_global, "nombre", None) or getattr(rubro_obj_global, "clave", "N/A")
            owner_id_log = getattr(owner_del_bot, "id", "N/A")
            current_app.logger.info(f"Usando Rubro ID {rubro_obj_global.id} ('{nombre_rubro_log}') perteneciente a User ID {owner_id_log} para la lógica del bot.")
        else:
            current_app.logger.info("No se pudo determinar un rubro/owner específico para la lógica del bot. Se usará lógica genérica si aplica (ej. para rubros públicos por defecto).")

        # --- CONTROL DE PLAN (si el bot tiene un owner y éste tiene plan) ---
        if owner_del_bot: # Solo aplicar límites si el bot pertenece a un User específico
            from utils.plan_limits import limite_para_usuario
            limite = limite_para_usuario(owner_del_bot)
            if limite is not None and owner_del_bot.preguntas_usadas >= limite:
                return jsonify({
                    "error": f"El bot ha alcanzado el límite de preguntas de su plan ({limite})."
                }), 403

        # --- MANEJO DE IMAGEN ADJUNTA ---
        interpretacion_imagen_resultado = None
        if uploaded_file_info and archivo_adjunto_id: # Asumimos que el frontend ya subió el archivo y nos pasa el ID
            from models import ArchivoAdjunto # Import local para evitar ciclos
            from services.interpretacion_imagen_service import interpretar_imagen_para_chat # Import local

            archivo_obj = db.session.get(ArchivoAdjunto, archivo_adjunto_id)
            if archivo_obj:
                current_app.logger.info(f"Procesando imagen adjunta ID: {archivo_adjunto_id} para chat tipo: {tipo_chat}")

                tipo_interpretacion_img = None
                pyme_owner_para_pedido = None

                if tipo_chat == "pyme":
                    tipo_interpretacion_img = "pedido_pyme"
                    pyme_owner_para_pedido = owner_del_bot # La PYME dueña del bot/rubro
                    if not pyme_owner_para_pedido:
                         current_app.logger.warning(f"No se pudo determinar el owner PYME para interpretar pedido de imagen {archivo_adjunto_id}. Se intentará con el usuario actual si es PYME.")
                         # Si el que chatea (viewer_obj) es una pyme y tiene catálogo, podría ser él.
                         # Esto es menos común, usualmente el cliente de la pyme sube la imagen.
                         # Por ahora, se requiere que el `owner_del_bot` (la pyme a la que se le habla) esté definido.
                         # Si no, la interpretación de pedido no funcionará bien.
                elif tipo_chat == "municipio":
                    tipo_interpretacion_img = "reclamo_municipal"

                if tipo_interpretacion_img:
                    # Pasar owner_del_bot como pyme_user si es un pedido pyme
                    interpretacion_imagen_resultado = interpretar_imagen_para_chat(
                        archivo_obj,
                        tipo_interpretacion_img,
                        pyme_user=pyme_owner_para_pedido if tipo_interpretacion_img == "pedido_pyme" else None
                    )
                    current_app.logger.info(f"Resultado interpretación imagen: {interpretacion_imagen_resultado}")
                    # Este resultado se pasará a `responder_chatboc` o se usará para modificar la respuesta.
                else:
                    current_app.logger.warning(f"Tipo de chat '{tipo_chat}' no tiene interpretación de imagen definida.")
            else:
                current_app.logger.error(f"No se encontró ArchivoAdjunto con ID {archivo_adjunto_id} en la DB.")

        # --- OBTENER SESSION CHAT ID ---
        from flask import session as flask_request_session
        import uuid
        session_chat_id = flask_request_session.get('chat_session_uuid')
        if not session_chat_id:
            session_chat_id = str(uuid.uuid4())
            flask_request_session['chat_session_uuid'] = session_chat_id
        current_app.logger.info(f"Chat Session ID: {session_chat_id}")

        # --- LLAMADA A LA LÓGICA CENTRAL DEL CHATBOT ---
        # `owner_del_bot` es el User dueño de la configuración del bot (PYME o Municipio).
        # `viewer_obj` es el User que está chateando (puede ser None si es anónimo).
        resultado = responder_chatboc(
            pregunta=pregunta,
            owner_user=owner_del_bot,
            current_user=viewer_obj,
            rubro_obj=rubro_obj_global,
            rubro_nombre_frontend=rubro_clave, # El que mandó el frontend, para consistencia
            tipo_chat=tipo_chat, # El que mandó el frontend
            contexto_previo=contexto_previo,
            anon_id=anon_id,
            chat_session_uuid=session_chat_id,
            uploaded_file_info=uploaded_file_info, # Información original del archivo
            interpretacion_imagen_data=interpretacion_imagen_resultado # Nuevo: resultado del análisis de imagen
        )

        # --- Determinar si la conversación debe considerarse pública ---
        # Usar el rubro_obj_global que se determinó como el rubro efectivo para esta interacción.
        es_publico = es_rubro_publico(rubro_obj_global)
        nombre_rubro_log = getattr(rubro_obj_global, "clave", "N/A") if rubro_obj_global else "N/A"

        current_app.logger.info(
            f"[RUBROS] Rubro efectivo: '{nombre_rubro_log}' (ID: {getattr(rubro_obj_global, 'id', 'N/A')}), esPublico={es_publico}"
        )

        # --- INCREMENTAR CONTADOR DE PREGUNTAS (si el bot tiene owner) ---
        if owner_del_bot:
            owner_del_bot.preguntas_usadas += 1
            # Nota: el commit de la sesión de DB se hace al final, o podría hacerse aquí
            # si es crítico que se guarde incluso si `responder_chatboc` falla después.
            # Por ahora, se asume que si `responder_chatboc` tiene éxito, la pregunta cuenta.

        # --- DEVOLVER RESULTADO ---
        if isinstance(resultado, dict):
            resultado["es_publico"] = es_publico
            if owner_del_bot: # Si el bot tiene un owner específico
                from utils.plan_limits import limite_para_usuario
                resultado["preguntas_usadas"] = owner_del_bot.preguntas_usadas
                resultado["limite_preguntas"] = limite_para_usuario(owner_del_bot)

            # Si hubo interpretación de imagen, añadirla a la respuesta para el frontend
            if interpretacion_imagen_resultado and not interpretacion_imagen_resultado.get("error"):
                resultado["interpretacion_adjunto"] = interpretacion_imagen_resultado
                # Ejemplo: si es un pedido Pyme y se detectaron items, el frontend puede usar esto
                # para mostrar los items o preguntar si se añaden al carrito.
                # Si es un reclamo, podría mostrar un resumen de lo detectado.

        db.session.commit() # Commit de cambios (ej. preguntas_usadas)
        return jsonify(resultado), 200

    except Exception as e:
        db.session.rollback() # Rollback en caso de error antes del commit final
        # Log more details to help pinpoint the 500 error
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
    """Endpoint genérico que delega según el rubro.

    El frontend debe enviar siempre el rubro y el tipo de chat correctos.
    Si llegan cruzados, el backend ajustará o lanzará error para evitar
    mezclar la estética de pymes con la de municipios.
    """
    user = owner_user or current_user
    return _procesar_chat(current_user=current_user, owner_user=user, anon_id=anon_id)


@chat_bp.route("/ask/pyme", methods=["POST", "OPTIONS"])
@anon_o_token_requerido
def ask_pyme(current_user=None, anon_id=None, owner_user=None):
    """Procesa preguntas para pymes.

    Aunque el endpoint fije el tipo 'pyme', se verificará el rubro para evitar
    mezclar respuestas de municipio. Cualquier inconsistencia se registra y se
    corrige o se devuelve error.
    """
    user = owner_user or current_user
    return _procesar_chat("pyme", current_user=current_user, owner_user=user, anon_id=anon_id)


@chat_bp.route("/ask/municipio", methods=["POST", "OPTIONS"])
@anon_o_token_requerido
def ask_municipio(current_user=None, anon_id=None, owner_user=None):
    """Procesa preguntas para municipios.

    Se valida que el rubro corresponda a un ente público y, de no ser así,
    se registrará un error. Esto previene mezclar lógicas de pyme y municipio.
    """
    user = owner_user or current_user
    return _procesar_chat("municipio", current_user=current_user, owner_user=user, anon_id=anon_id)


@chat_bp.route("/widget/attention", methods=["GET"])
def widget_attention():
    """Devuelve un mensaje breve para mostrar en el globito del chat."""
    opciones = current_app.config.get("ATTENTION_BUBBLE_CHOICES")
    if opciones:
        mensaje = random.choice(opciones)
    else:
        mensaje = current_app.config.get(
            "ATTENTION_BUBBLE_TEXT", "¡Hola! ¿Necesitas ayuda?"
        )
    return jsonify({"mensaje": mensaje})
