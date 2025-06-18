# routes/chat.py
import logging
from flask import Blueprint, request, jsonify, current_app
from models import User

chat_bp = Blueprint("chat_bp", __name__)

def _get_request_data():
    try:
        data = request.get_json()
        if not isinstance(data, dict):
            raise TypeError("El cuerpo debe ser JSON.")
        pregunta = data.get("pregunta")
        if not pregunta:
            raise ValueError("Falta el campo 'pregunta'.")
        contexto_previo = data.get("contexto_previo")  # Leemos la mochila
        tipo_chat = data.get("tipo_chat", "pyme")
        return pregunta, contexto_previo, tipo_chat, None
    except Exception as e:
        current_app.logger.warning(f"Error al parsear /ask: {e}")
        return None, None, None, jsonify({"error": "Formato JSON inválido."})

def _authenticate_and_get_user():
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        token = auth_header.split(" ")[1]
        if token:
            return User.query.filter_by(token=token).first()
    return None

@chat_bp.route("/ask", methods=["POST"])
def ask():
    try:
        pregunta, contexto_previo, tipo_chat, error_response = _get_request_data()
        if error_response:
            return error_response, 400

        user_obj = _authenticate_and_get_user()
        if not user_obj:
            return jsonify({"error": "No autenticado."}), 401

        rubro_obj = user_obj.rubro if user_obj and user_obj.rubro else None

        # --- CONTROL DE PLAN ---
        if user_obj.plan != "full" and user_obj.preguntas_usadas >= user_obj.limite_preguntas:
            return jsonify({
                "error": f"Alcanzaste el límite de preguntas de tu plan ({user_obj.limite_preguntas}). Mejorá tu plan para seguir consultando."
            }), 403

        if tipo_chat == "municipio":
            from services.municipios import responder_municipio
            handler_fn = responder_municipio
        else:
            from services.pymes import responder_pyme
            handler_fn = responder_pyme

        resultado = handler_fn(
            pregunta,
            user_obj,
            rubro_obj,
            contexto_previo=contexto_previo,
        )

        # --- INCREMENTAR CONTADOR SOLO SI TODO ESTÁ OK ---
        user_obj.preguntas_usadas += 1
        try:
            from extensions import db
            db.session.commit()
        except Exception as e:
            current_app.logger.error(f"Error al actualizar preguntas_usadas: {e}")
            # NO frena el flujo del bot, pero loguea

        # --- OPCIONAL: DEVOLVER CONTADOR ACTUALIZADO ---
        if isinstance(resultado, dict):
            resultado["preguntas_usadas"] = user_obj.preguntas_usadas
            resultado["limite_preguntas"] = user_obj.limite_preguntas

        return jsonify(resultado), 200

    except Exception as e:
        current_app.logger.error(f"❌ Error crítico en /ask: {e}", exc_info=True)
        return jsonify({"error": "Error interno del servidor."}), 500
