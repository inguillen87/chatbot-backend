import logging
from flask import Blueprint, request, jsonify
from services.logic import responder_chatboc
from models import User  # ✅ Este era el que faltaba

chat_bp = Blueprint("chat_bp", __name__)

@chat_bp.route("/ask", methods=["POST"])
def responder():
    data = request.get_json()
    pregunta = data.get("question") or data.get("pregunta")
    token = request.headers.get("Authorization", "").replace("Bearer ", "").strip()
    rubro_nombre = data.get("rubro_nombre", None)
    historial = data.get("historial", [])

    logging.info(f"🎯 Pregunta recibida: {pregunta}")

    try:
        resultado = responder_chatboc(pregunta, token, rubro_nombre_frontend=rubro_nombre, historial=historial)
        return jsonify(resultado), 200
    except Exception as e:
        logging.error(f"❌ Error en /ask: {e}")
        return jsonify({"error": "Ocurrió un error al procesar tu pregunta."}), 500


@chat_bp.after_request
def apply_cors(response):
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type,Authorization"
    response.headers["Access-Control-Allow-Methods"] = "GET,POST,OPTIONS"
    return response
