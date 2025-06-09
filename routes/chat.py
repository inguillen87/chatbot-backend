# routes/chat.py

import logging
from flask import Blueprint, request, jsonify, session as flask_session, current_app
from models import Rubro, User
from services.logic import responder_chatboc
from services.municipios import responder_municipio
from flask_login import current_user, login_required

chat_bp = Blueprint("chat_bp", __name__)

def _get_request_data():
    """Parsea el cuerpo de la solicitud, extrae la pregunta y el contexto."""
    try:
        data = request.get_json()
        if not isinstance(data, dict):
            raise TypeError("El cuerpo de la solicitud debe ser un objeto JSON.")
        
        pregunta = data.get("pregunta") or data.get("question")
        if not pregunta:
            raise ValueError("Falta el campo 'pregunta' en la solicitud.")
            
        contexto_previo = data.get("contexto_previo")
        return pregunta, contexto_previo, None
    except Exception as e:
        current_app.logger.warning(f"Error al parsear el cuerpo de /ask: {e}")
        error_msg = str(e) if isinstance(e, (ValueError, TypeError)) else "Formato JSON inválido."
        return None, None, jsonify({"error": error_msg})

@chat_bp.route("/ask", methods=["POST"])
@login_required
def ask():
    """
    Endpoint principal, refactorizado para ser más robusto y claro.
    """
    try:
        pregunta, contexto_previo, error_response = _get_request_data()
        if error_response:
            return error_response, 400

        # El decorador @login_required ya nos da el user_obj en `current_user`
        user_obj = current_user
        rubro_obj = user_obj.rubro if user_obj and user_obj.rubro else None
        
        # Como fallback, si el usuario no tiene rubro, lo buscamos en el payload
        if not rubro_obj and (contexto_previo and contexto_previo.get("rubro")):
             rubro_name = contexto_previo.get("rubro").strip().lower()
             rubro_obj = Rubro.query.filter(Rubro.nombre.ilike(rubro_name)).first()

        if rubro_obj and rubro_obj.nombre.lower().strip() == 'municipios':
            resultado = responder_municipio(
                pregunta=pregunta, 
                user_obj=user_obj, 
                rubro_obj=rubro_obj,
                contexto_previo=contexto_previo
            )
        else:
            resultado = responder_chatboc(
                pregunta=pregunta, 
                user_obj=user_obj, 
                rubro_obj=rubro_obj,
                contexto_previo=contexto_previo
            )
        
        # Esta es la línea clave: SIEMPRE devolvemos un JSON.
        return jsonify(resultado), 200

    except Exception as e:
        current_app.logger.error(f"❌ Error crítico en el endpoint /ask: {e}", exc_info=True)
        return jsonify({"error": "Error interno del servidor al procesar tu pregunta."}), 500

@chat_bp.route("/ping", methods=["GET"])
def ping():
    """Endpoint de prueba para verificar que el servicio está activo."""
    return jsonify({"status": "ok", "message": "pong"}), 200