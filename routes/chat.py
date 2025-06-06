# Contenido Final y Correcto para: routes/chat.py

from flask import Blueprint, request, jsonify, session, current_app

# Importamos los modelos y los servicios de lógica que sí existen
from models import Rubro, User
from services.logic import responder_chatboc
from services.municipios import responder_municipio

chat_bp = Blueprint("chat_bp", __name__)

@chat_bp.route("/ask", methods=["POST"])
def ask():
    """
    Endpoint principal que enruta la pregunta al servicio de lógica correcto.
    """
    try:
        data = request.get_json()
        if not data or not data.get("question"):
            current_app.logger.warning("Solicitud a /ask inválida (sin JSON o sin 'question').")
            return jsonify({"error": "Falta la pregunta en la solicitud."}), 400

        pregunta = data.get("question")
        auth_header = request.headers.get("Authorization", "")
        token = auth_header.split(" ")[1] if auth_header.startswith("Bearer ") else None
        
        user_obj = User.query.filter_by(token=token).first() if token else None
        rubro_autoritativo = user_obj.rubro if user_obj and user_obj.rubro else None

        # Si no hay rubro en el perfil del usuario, intenta tomarlo del frontend
        if not rubro_autoritativo and data.get("rubro"):
             rubro_autoritativo = Rubro.query.filter(Rubro.nombre.ilike(data.get("rubro").strip().lower())).first()

        # Enrutamiento inteligente al servicio correcto
        if rubro_autoritativo and rubro_autoritativo.nombre.lower().strip() == 'municipios':
            resultado = responder_municipio(
                pregunta=pregunta, user_obj=user_obj, rubro_obj=rubro_autoritativo, session_obj=session
            )
        else:
            resultado = responder_chatboc(
                pregunta=pregunta, user_obj=user_obj, rubro_obj=rubro_autoritativo, session_obj=session
            )
            
        return jsonify(resultado), 200

    except Exception as e:
        current_app.logger.error(f"❌ Error crítico en el endpoint /ask: {e}", exc_info=True)
        return jsonify({"error": "Error interno del servidor al procesar tu pregunta."}), 500

@chat_bp.route("/ping", methods=["GET"])
def ping():
    """Endpoint de prueba para verificar que el servicio está activo."""
    return jsonify({"status": "ok", "message": "pong"}), 200