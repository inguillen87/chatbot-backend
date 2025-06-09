# routes/chat.py

import logging
from flask import Blueprint, request, jsonify, current_app
from models import Rubro, User
from services.logic import responder_chatboc
from services.municipios import responder_municipio

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
        rubro_enviado = data.get("rubro")
        
        return pregunta, contexto_previo, rubro_enviado, None
    except Exception as e:
        current_app.logger.warning(f"Error al parsear el cuerpo de /ask: {e}")
        error_msg = str(e) if isinstance(e, (ValueError, TypeError)) else "Formato JSON inválido."
        return None, None, None, jsonify({"error": error_msg})

def _authenticate_and_get_user():
    """Autentica al usuario por token si se provee."""
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        token = auth_header.split(" ")[1]
        if token:
            return User.query.filter_by(token=token).first()
    return None

@chat_bp.route("/ask", methods=["POST"])
def ask():
    """
    Endpoint principal, refactorizado para ser robusto y claro.
    Maneja la autenticación por token y el ruteo de la lógica.
    """
    try:
        pregunta, contexto_previo, rubro_enviado, error_response = _get_request_data()
        if error_response:
            return error_response, 400

        user_obj = _authenticate_and_get_user()
        
        # Determinar el rubro: primero el del usuario logueado, sino el enviado en el body.
        rubro_obj = None
        if user_obj and user_obj.rubro:
            rubro_obj = user_obj.rubro
        elif rubro_enviado:
            rubro_obj = Rubro.query.filter(Rubro.nombre.ilike(rubro_enviado.strip().lower())).first()

        # Decidir a qué lógica principal llamar (Municipio o Pyme/General)
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
        
        # SIEMPRE devolvemos un JSON, eliminando el TypeError
        return jsonify(resultado), 200

    except Exception as e:
        current_app.logger.error(f"❌ Error crítico en el endpoint /ask: {e}", exc_info=True)
        return jsonify({"error": "Error interno del servidor al procesar tu pregunta."}), 500

@chat_bp.route("/ping", methods=["GET"])
def ping():
    """Endpoint de prueba para verificar que el servicio está activo."""
    return jsonify({"status": "ok", "message": "pong"}), 200