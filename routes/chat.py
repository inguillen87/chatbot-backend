# routes/chat.py

import logging
from flask import Blueprint, request, jsonify, session, current_app
from models import User, Rubro
from services.logic import responder_chatboc
from services.municipios import responder_municipio

# --- Blueprint Definition ---
chat_bp = Blueprint("chat_bp", __name__)

# --- Helper Functions (para un código más limpio) ---

def _get_request_data():
    """Parsea el cuerpo de la solicitud y extrae la pregunta."""
    try:
        data = request.get_json(force=True)
        if not isinstance(data, dict):
            raise TypeError("El cuerpo de la solicitud debe ser un objeto JSON.")
        
        pregunta = data.get("pregunta") or data.get("question")
        if not pregunta:
            raise ValueError("Falta el campo 'pregunta' o 'question' en la solicitud.")
            
        # Extraemos el contexto manual (la "mochila")
        contexto_previo = data.get("contexto_previo")
        
        return pregunta, contexto_previo, None
    except Exception as e:
        current_app.logger.warning(f"Error al parsear el cuerpo de /ask: {e}")
        error_msg = str(e) if isinstance(e, (ValueError, TypeError)) else "Formato JSON inválido."
        return None, None, jsonify({"error": error_msg})

def _authenticate_and_get_context():
    """Autentica al usuario por token y obtiene su rubro."""
    auth_header = request.headers.get("Authorization", "")
    token = None
    if auth_header.startswith("Bearer "):
        token = auth_header.split(" ")[1]
    
    if not token:
        # Permite uso anónimo, pero user_obj será None
        return None, None

    user_obj = User.query.filter_by(token=token).first()
    if not user_obj:
        # El token existe pero es inválido, esto es un error de autorización
        return None, jsonify({"error": "Token inválido o sesión expirada."})
    
    rubro_obj = user_obj.rubro if user_obj.rubro else None
    return user_obj, rubro_obj

# --- Main Endpoint ---

@chat_bp.route("/ask", methods=["POST"])
def ask():
    """
    Endpoint principal, ahora refactorizado para mayor claridad.
    Enruta la pregunta al servicio de lógica correcto.
    """
    try:
        # 1. Obtener y validar datos de la solicitud
        pregunta, contexto_previo, error_response = _get_request_data()
        if error_response:
            return error_response, 400

        # 2. Autenticar usuario y obtener su contexto (user, rubro)
        user_obj, auth_error_response = _authenticate_and_get_context()
        if auth_error_response:
            return auth_error_response, 401
        
        rubro_obj = user_obj.rubro if user_obj else None
        
        # Como fallback, si el usuario no tiene rubro, lo buscamos en el body
        if not rubro_obj and (contexto_previo and contexto_previo.get("rubro")):
            rubro_name = contexto_previo.get("rubro").strip().lower()
            rubro_obj = Rubro.query.filter(Rubro.nombre.ilike(rubro_name)).first()
            
        # 3. Decidir a qué lógica principal llamar (Municipio o Pyme/General)
        if rubro_obj and rubro_obj.nombre.lower().strip() == 'municipios':
            # Llamamos a la lógica de Municipio
            resultado = responder_municipio(
                pregunta=pregunta, 
                user_obj=user_obj, 
                rubro_obj=rubro_obj,
                # session_obj=session, # <-- Dejado comentado para el futuro
                contexto_previo=contexto_previo
            )
        else:
            # Llamamos a la lógica de Pyme (Chatboc general)
            resultado = responder_chatboc(
                pregunta=pregunta, 
                user_obj=user_obj, 
                rubro_obj=rubro_obj,
                # session_obj=session, # <-- Dejado comentado para el futuro
                contexto_previo=contexto_previo
            )
        
        return jsonify(resultado), 200

    except Exception as e:
        current_app.logger.error(f"❌ Error crítico en el endpoint /ask: {e}", exc_info=True)
        return jsonify({"error": "Error interno del servidor al procesar tu pregunta."}), 500

@chat_bp.route("/ping", methods=["GET"])
def ping():
    """Endpoint de prueba para verificar que el servicio está activo."""
    return jsonify({"status": "ok", "message": "pong"}), 200