# En routes/chat.py
from flask import Blueprint, request, jsonify, current_app # current_app no se usa aquí
import logging
from services.logic import responder_chatboc
# from .auth import token_requerido # Si token_requerido está en auth.py y necesitas importarlo

chat_bp = Blueprint("chat_bp", __name__)
logger = logging.getLogger(__name__) # Usar logger del módulo es buena práctica

@chat_bp.route("/ask", methods=["POST"]) # <-- SOLO "POST"
# @token_requerido # Si esta ruta debe estar protegida por token
def ask():
    # Ya no necesitas el bloque if request.method == "OPTIONS"
    try:
        data = request.get_json()
        if not data: # Chequeo adicional si el JSON está vacío
            logger.warning("Solicitud a /ask sin datos JSON o JSON vacío.")
            return jsonify({"error": "Falta el cuerpo de la solicitud o está vacío."}), 400

        pregunta = data.get("question") or data.get("pregunta")
        rubro_nombre = data.get("rubro", "").strip().lower()

        # El token se maneja en el decorador @token_requerido o se pasa a responder_chatboc
        # Si @token_requerido inyecta 'user', entonces responder_chatboc ya no necesitaría 'token' como argumento directo
        # sino el objeto 'user'. Por ahora, mantengo la lógica de pasar el token.
        auth_header = request.headers.get("Authorization", "")
        token = None
        if auth_header.startswith("Bearer "):
            token = auth_header.split(" ")[1].strip()

        if not pregunta:
            logger.warning("Pregunta faltante en la solicitud a /ask.")
            return jsonify({"error": "Falta la pregunta"}), 400

        logger.info(f"Procesando /ask para pregunta: '{pregunta[:50]}...' (Token presente: {'Sí' if token else 'No'})")
        # 1. Traé el rubro_obj real antes de llamar a responder_chatboc
        rubro_obj = Rubro.query.filter_by(nombre=rubro_nombre).first() if rubro_nombre else None

        # 2. Llamá correctamente a la función
        resultado = responder_chatboc(pregunta, token, rubro_nombre_frontend=rubro_nombre, rubro_obj=rubro_obj)
        return jsonify(resultado), 200

    except Exception as e:
        logger.error(f"❌ Error crítico en /ask: {e}", exc_info=True) # exc_info=True para la traza completa
        return jsonify({"error": "Error interno al procesar tu pregunta."}), 500

@chat_bp.route("/ping", methods=["GET"])
def ping():
    return jsonify({"msg": "pong"}), 200

# ELIMINA EL DECORADOR @chat_bp.after_request COMPLETAMENTE