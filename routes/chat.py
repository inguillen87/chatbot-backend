# routes/chat.py
from flask import Blueprint, request, jsonify
import logging
from models import Rubro, User  # Asegurate de importar Rubro y User
from services.logic import responder_chatboc

chat_bp = Blueprint("chat_bp", __name__)
logger = logging.getLogger(__name__)

@chat_bp.route("/ask", methods=["POST"])
def ask():
    try:
        data = request.get_json()
        if not data:
            logger.warning("Solicitud a /ask sin datos JSON o JSON vacío.")
            return jsonify({"error": "Falta el cuerpo de la solicitud o está vacío."}), 400

        pregunta = data.get("question") or data.get("pregunta")
        rubro_nombre = (data.get("rubro") or "").strip().lower()

        auth_header = request.headers.get("Authorization", "")
        token = None
        if auth_header.startswith("Bearer "):
            token = auth_header.split(" ")[1].strip()

        if not pregunta:
            logger.warning("Pregunta faltante en la solicitud a /ask.")
            return jsonify({"error": "Falta la pregunta"}), 400

        # Trata de encontrar el usuario si existe (solo si token no es demo)
        user_obj = None
        if token and not token.startswith("demo"):
            user_obj = User.query.filter_by(token=token).first()

        # Trata de encontrar el rubro en BD si lo mandaron (siempre por nombre normalizado)
        rubro_obj = None
        if rubro_nombre:
            rubro_obj = Rubro.query.filter(Rubro.nombre.ilike(rubro_nombre)).first()

        logger.info(f"Procesando /ask para pregunta: '{pregunta[:50]}...' (Token: {token}, Rubro: {rubro_nombre})")

        # Llama robusto, pasando todo lo que tengas (funciona aunque falte user o rubro)
        resultado = responder_chatboc(
            pregunta,
            user_obj=user_obj,
            rubro_obj=rubro_obj,
            rubro_nombre_frontend=rubro_nombre
        )
        return jsonify(resultado), 200

    except Exception as e:
        logger.error(f"❌ Error crítico en /ask: {e}", exc_info=True)
        return jsonify({"error": "Error interno al procesar tu pregunta."}), 500

@chat_bp.route("/ping", methods=["GET"])
def ping():
    return jsonify({"msg": "pong"}), 200
