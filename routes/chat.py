from flask import Blueprint, request, jsonify
from services.logic import responder_chatboc

chat_bp = Blueprint("chat", __name__)

@chat_bp.route("/responder_chatboc", methods=["POST", "OPTIONS"])
def responder():
    if request.method == "OPTIONS":
        # Respuesta rápida para preflight
        return jsonify({"ok": True}), 200

    try:
        data = request.get_json()
        if not isinstance(data, dict):
            raise ValueError("Formato JSON inválido")

        pregunta = data.get("question") or data.get("pregunta")
        token = request.headers.get("Authorization", "").replace("Bearer ", "").strip()

        if not pregunta:
            return jsonify({"error": "Falta la pregunta"}), 400

        resultado = responder_chatboc(pregunta, token)

        if "error" in resultado:
            return jsonify(resultado), 401

        return jsonify(resultado), 200

    except Exception as e:
        return jsonify({"error": f"Error interno: {str(e)}"}), 500

# ✅ FIX CORS manual
@chat_bp.after_request
def apply_cors(response):
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type,Authorization"
    response.headers["Access-Control-Allow-Methods"] = "GET,POST,OPTIONS"
    return response
