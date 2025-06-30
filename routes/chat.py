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

        return pregunta, contexto_previo, tipo_chat, rubro_id, rubro_clave, None

    except (TypeError, ValueError) as e:
        current_app.logger.warning(f"Error al parsear /ask: {e}")
        return (
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
            error_response,
        ) = _parse_request(tipo_chat_fijo)
        if error_response:
            return error_response, 400

        owner_obj = owner_user or current_user or _authenticate_and_get_user()
        viewer_obj = current_user
        if not owner_obj and not anon_id:
            return jsonify({"error": "No autenticado."}), 401

        # --- CONTROL DE LÍMITES PARA USUARIOS ANÓNIMOS ---
        if anon_id and not owner_obj: # Es anónimo
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

        if rubro_id:
            rubro_obj = Rubro.query.get(rubro_id)
        elif rubro_clave:
            rubro_obj = Rubro.query.filter(
                func.lower(Rubro.clave) == rubro_clave.lower()
            ).first()
        else:
            rubro_obj = owner_obj.rubro if owner_obj and owner_obj.rubro else None

        # Logueamos qué rubro se está usando para procesar la pregunta
        if rubro_obj:
            nombre_rubro = getattr(rubro_obj, "nombre", None) or getattr(rubro_obj, "clave", None)
            current_app.logger.info(
                f"Usando rubro ID {rubro_obj.id} - {nombre_rubro}"
            )
        else:
            current_app.logger.info("Sin rubro asociado al usuario o en la petición")

        # --- CONTROL DE PLAN SOLO PARA USUARIOS AUTENTICADOS ---
        if owner_obj:
            from utils.plan_limits import limite_para_usuario
            limite = limite_para_usuario(owner_obj)
            if limite is not None and owner_obj.preguntas_usadas >= limite:
                return (
                    jsonify(
                        {
                            "error": f"Alcanzaste el límite de preguntas de tu plan ({limite}). Mejorá tu plan para seguir consultando."
                        }
                    ),
                    403,
                )

        from flask import session as flask_request_session # Renombrar para evitar conflicto con session_obj
        import uuid

        session_chat_id = flask_request_session.get('chat_session_uuid')
        if not session_chat_id:
            session_chat_id = str(uuid.uuid4())
            flask_request_session['chat_session_uuid'] = session_chat_id

        current_app.logger.info(f"Chat Session ID: {session_chat_id}")

        # Usamos la lógica centralizada que decide según el rubro
        resultado = responder_chatboc(
            pregunta,
            owner_user=owner_obj,
            current_user=viewer_obj,
            rubro_obj=rubro_obj,
            rubro_nombre_frontend=rubro_clave,
            tipo_chat=tipo_chat,
            contexto_previo=contexto_previo, # Esto es el contexto_pyme o contexto_municipio de la sesión
            anon_id=anon_id,
            chat_session_uuid=session_chat_id # Pasar el session_uuid
        )

        # --- Determinar si la conversación debe considerarse pública ---
        rubro_seleccionado = (
            rubro_obj
            or rubro_clave
            or (owner_obj.rubro if owner_obj and getattr(owner_obj, "rubro", None) else None)
        )
        rubro_nombre = normalizar_rubro(rubro_seleccionado)
        es_publico = es_rubro_publico(rubro_seleccionado)

        current_app.logger.info(
            f"[RUBROS] user.rubro={getattr(owner_obj, 'rubro', None)} "
            f"rubroSeleccionado={rubro_seleccionado} "
            f"rubroNormalizado={rubro_nombre} esRubroPublico={es_publico}"
        )


        # --- INCREMENTAR CONTADOR SOLO SI TODO ESTÁ OK ---
        if owner_obj:
            owner_obj.preguntas_usadas += 1
            try:
                from extensions import db

                db.session.commit()
            except Exception as e:
                current_app.logger.error(
                    f"Error al actualizar preguntas_usadas: {e}"
                )
                # NO frena el flujo del bot, pero loguea

        # --- OPCIONAL: DEVOLVER CONTADOR ACTUALIZADO ---
        if isinstance(resultado, dict):
            resultado["es_publico"] = es_publico
            if owner_obj:
                from utils.plan_limits import limite_para_usuario
                resultado["preguntas_usadas"] = owner_obj.preguntas_usadas
                resultado["limite_preguntas"] = limite_para_usuario(owner_obj)

        return jsonify(resultado), 200

    except Exception as e:
        current_app.logger.error(f"❌ Error crítico en /ask: {e}", exc_info=True)
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
