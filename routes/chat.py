from flask import Blueprint, request, jsonify, session, current_app
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
        # 1. Body robusto: siempre parsea a dict
        try:
            data = request.get_json(force=True)
            if isinstance(data, str):  # Por si alguna vez llega como string
                import json
                data = json.loads(data)
        except Exception as e:
            current_app.logger.warning(f"JSON inválido recibido en /ask: {e}")
            return jsonify({"error": "Solicitud inválida, formato JSON incorrecto."}), 400

        if not isinstance(data, dict):
            current_app.logger.warning("El cuerpo recibido en /ask no es un dict.")
            return jsonify({"error": "El cuerpo debe ser un JSON (objeto), no texto plano."}), 400

        # 2. Chequeo de campo: acepta 'pregunta' o 'question'
        pregunta = data.get("pregunta") or data.get("question")
        if not pregunta:
            current_app.logger.warning("Solicitud a /ask sin campo 'pregunta' ni 'question'.")
            return jsonify({"error": "Falta la pregunta en la solicitud ('pregunta' o 'question')."}), 400

        # 3. Token de autenticación
        auth_header = request.headers.get("Authorization", "")
        token = None
        if auth_header.startswith("Bearer "):
            token = auth_header.split(" ")[1]
        elif auth_header:
            token = auth_header
        user_obj = User.query.filter_by(token=token).first() if token else None
        rubro_autoritativo = user_obj.rubro if user_obj and user_obj.rubro else None

        # 4. Si no hay rubro de usuario, usá el del body
        if not rubro_autoritativo and data.get("rubro"):
            rubro_name = data.get("rubro").strip().lower()
            rubro_autoritativo = Rubro.query.filter(Rubro.nombre.ilike(rubro_name)).first()

        # 5. Ruteo: municipios o general
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
