import logging
from flask import Blueprint, request, jsonify
from services.logic import responder_chatboc

# 👇 Importarlo después, no al tope del archivo
from models import User

chat_bp = Blueprint("chat_bp", __name__)

@chat_bp.route("/ask", methods=["POST", "OPTIONS"])
def ask():
    if request.method == "OPTIONS":
        return jsonify({"ok": True}), 200

    try:
        data = request.get_json()
        pregunta = data.get("question") or data.get("pregunta")
        rubro_nombre = data.get("rubro", "").strip().lower()
        token = request.headers.get("Authorization", "").replace("Bearer ", "").strip()

        if not pregunta:
            return jsonify({"error": "Falta la pregunta"}), 400

        resultado = responder_chatboc(pregunta, token, rubro_nombre_frontend=rubro_nombre)
        return jsonify(resultado), 200

    except Exception as e:
        logging.error(f"❌ Error en /ask: {e}")
        return jsonify({"error": "Error interno al procesar tu pregunta."}), 500

@chat_bp.after_request
def apply_cors(response):
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type,Authorization"
    response.headers["Access-Control-Allow-Methods"] = "GET,POST,OPTIONS"
    return response
