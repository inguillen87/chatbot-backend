# routes/chat.py
import logging
from flask import Blueprint, request, jsonify, current_app
from sqlalchemy import func
from models import User, Rubro
from services.logic import responder_chatboc
from .auth import anon_o_token_requerido

chat_bp = Blueprint("chat_bp", __name__)


def _parse_request(tipo_chat_fijo: str | None = None):
    """Obtiene y valida los campos comunes del cuerpo JSON."""
    try:
        data = request.get_json()
        if not isinstance(data, dict):
            raise TypeError("El cuerpo debe ser JSON.")

        pregunta = data.get("pregunta")
        if not pregunta:
            raise ValueError("Falta el campo 'pregunta'.")

        if tipo_chat_fijo:
            tipo_chat = tipo_chat_fijo
        else:
            tipo_chat = data.get("tipo_chat")
            if tipo_chat not in ("pyme", "municipio"):
                raise ValueError("'tipo_chat' debe ser 'pyme' o 'municipio'.")

        contexto_previo = data.get("contexto_previo")  # Leemos la mochila
        rubro_id = data.get("rubro_id")
        rubro_clave = data.get("rubro_clave")

        return pregunta, contexto_previo, tipo_chat, rubro_id, rubro_clave, None
    except Exception as e:
        current_app.logger.warning(f"Error al parsear /ask: {e}")
        return (
            None,
            None,
            None,
            None,
            None,
            jsonify({"error": "Formato JSON inválido."}),
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
    anon_id: str | None = None,
):
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

        user_obj = current_user if current_user is not None else _authenticate_and_get_user()
        if not user_obj and not anon_id:
            return jsonify({"error": "No autenticado."}), 401

        if rubro_id:
            rubro_obj = Rubro.query.get(rubro_id)
        elif rubro_clave:
            rubro_obj = Rubro.query.filter(
                func.lower(Rubro.clave) == rubro_clave.lower()
            ).first()
        else:
            rubro_obj = user_obj.rubro if user_obj and user_obj.rubro else None

        # Logueamos qué rubro se está usando para procesar la pregunta
        if rubro_obj:
            nombre_rubro = getattr(rubro_obj, "nombre", None) or getattr(rubro_obj, "clave", None)
            current_app.logger.info(
                f"Usando rubro ID {rubro_obj.id} - {nombre_rubro}"
            )
        else:
            current_app.logger.info("Sin rubro asociado al usuario o en la petición")

        # --- CONTROL DE PLAN SOLO PARA USUARIOS AUTENTICADOS ---
        if user_obj:
            if (
                user_obj.plan != "full"
                and user_obj.preguntas_usadas >= user_obj.limite_preguntas
            ):
                return (
                    jsonify(
                        {
                            "error": f"Alcanzaste el límite de preguntas de tu plan ({user_obj.limite_preguntas}). Mejorá tu plan para seguir consultando."
                        }
                    ),
                    403,
                )

        # Usamos la lógica centralizada que decide según el rubro
        resultado = responder_chatboc(
            pregunta,
            user_obj=user_obj,
            rubro_obj=rubro_obj,
            rubro_nombre_frontend=rubro_clave,
            tipo_chat=tipo_chat,
            contexto_previo=contexto_previo,
        )

        # --- INCREMENTAR CONTADOR SOLO SI TODO ESTÁ OK ---
        if user_obj:
            user_obj.preguntas_usadas += 1
            try:
                from extensions import db

                db.session.commit()
            except Exception as e:
                current_app.logger.error(
                    f"Error al actualizar preguntas_usadas: {e}"
                )
                # NO frena el flujo del bot, pero loguea

        # --- OPCIONAL: DEVOLVER CONTADOR ACTUALIZADO ---
        if isinstance(resultado, dict) and user_obj:
            resultado["preguntas_usadas"] = user_obj.preguntas_usadas
            resultado["limite_preguntas"] = user_obj.limite_preguntas

        return jsonify(resultado), 200

    except Exception as e:
        current_app.logger.error(f"❌ Error crítico en /ask: {e}", exc_info=True)
        return jsonify({"error": "Error interno del servidor."}), 500


# Handler para el preflight de CORS de /ask
@chat_bp.route("/ask", methods=["OPTIONS"])
def ask_options():
    return "", 200


@chat_bp.route("/ask", methods=["POST"])
@anon_o_token_requerido
def ask(current_user=None, anon_id=None):
    return _procesar_chat(current_user=current_user, anon_id=anon_id)


# Handler para el preflight de CORS de /ask/pyme
@chat_bp.route("/ask/pyme", methods=["OPTIONS"])
def ask_pyme_options():
    return "", 200


@chat_bp.route("/ask/pyme", methods=["POST"])
@anon_o_token_requerido
def ask_pyme(current_user=None, anon_id=None):
    return _procesar_chat("pyme", current_user=current_user, anon_id=anon_id)


# Preflight CORS handler for /ask/municipio
@chat_bp.route("/ask/municipio", methods=["OPTIONS"])
def ask_municipio_options():
    return "", 200

@chat_bp.route("/ask/municipio", methods=["POST"])
@anon_o_token_requerido
def ask_municipio(current_user=None, anon_id=None):
    return _procesar_chat("municipio", current_user=current_user, anon_id=anon_id)
