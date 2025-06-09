# routes/chat.py

import logging
from flask import Blueprint, request, jsonify, current_app
from models import Rubro, User
from services.logic import responder_chatboc
from services.municipios import responder_municipio # <-- Esta línea necesita que la función exista
from flask_login import current_user

chat_bp = Blueprint("chat_bp", __name__)

def _get_request_data():
    try:
        data = request.get_json()
        if not isinstance(data, dict): raise TypeError("El cuerpo debe ser JSON.")
        pregunta = data.get("pregunta") or data.get("question")
        if not pregunta: raise ValueError("Falta el campo 'pregunta'.")
        contexto_previo = data.get("contexto_previo")
        rubro_enviado = data.get("rubro")
        return pregunta, contexto_previo, rubro_enviado, None
    except Exception as e:
        current_app.logger.warning(f"Error al parsear /ask: {e}")
        return None, None, None, jsonify({"error": "Formato JSON inválido."})

def _authenticate_and_get_user():
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        token = auth_header.split(" ")[1]
        if token:
            return User.query.filter_by(token=token).first()
    return None

@chat_bp.route("/ask", methods=["POST"])
def ask():
    try:
        pregunta, contexto_previo, rubro_enviado, error_response = _get_request_data()
        if error_response:
            return error_response, 400

        user_obj = _authenticate_and_get_user()
        
        rubro_obj = None
        if user_obj and user_obj.rubro:
            rubro_obj = user_obj.rubro
        elif rubro_enviado:
            rubro_obj = Rubro.query.filter(Rubro.nombre.ilike(rubro_enviado.strip().lower())).first()

        if rubro_obj and rubro_obj.nombre.lower().strip() == 'municipios':
            resultado = responder_municipio(pregunta=pregunta, user_obj=user_obj, rubro_obj=rubro_obj, contexto_previo=contexto_previo)
        else:
            resultado = responder_chatboc(pregunta=pregunta, user_obj=user_obj, rubro_obj=rubro_obj, contexto_previo=contexto_previo)
        
        return jsonify(resultado), 200
    except Exception as e:
        current_app.logger.error(f"❌ Error crítico en /ask: {e}", exc_info=True)
        return jsonify({"error": "Error interno del servidor."}), 500