from flask import Blueprint, request, jsonify
from twilio.twiml.messaging_response import MessagingResponse  # type: ignore
from services.nlp import get_gpt_response
from models import QA, User
from extensions import db
from datetime import datetime
import logging
import os

chat_bp = Blueprint('chat', __name__)

# ✅ RUTA PARA WEB FRONTEND
@chat_bp.route('/ask', methods=['POST'])
def ask():
    token = request.headers.get("Authorization", "")
    if not token:
        logging.warning("Token no enviado")
        return jsonify({"error": "No autorizado"}), 401

    data = request.get_json()
    question = data.get("question", "").strip()
    user_id = data.get("user_id")  # Puede ser None si no hay login

    if not question:
        return jsonify({"answer": "Por favor escribí una consulta válida."})

    try:
        user = User.query.get(user_id) if user_id else None

        # ✅ Control de límite por plan
        if user:
            plan_limits = {
                "free": 10,
                "starter": 300,
                "pro": 1000,
                "enterprise": float("inf")
            }

            limit = plan_limits.get(user.plan, 10)

            # Reset mensual
            if (datetime.utcnow() - user.last_reset).days > 30:
                user.preguntas_usadas = 0
                user.last_reset = datetime.utcnow()
                db.session.commit()

            # ✅ Aplicar límites también en modo DEMO
            if user.preguntas_usadas >= limit:
                return jsonify({
                    "answer": f"Has alcanzado el límite de tu plan ({user.plan}). Actualizá tu suscripción para seguir usando Chatboc."
                })

        # ✅ Mostrar info en consola
        print("📥 Pregunta recibida:", question)
        print("👤 Usuario:", user.name if user else "anónimo")
        print("📊 Plan:", user.plan if user else "sin plan")

        # ✅ Obtener respuesta (OpenAI o demo)
        answer = get_gpt_response(question, user)
        print("🧠 Respuesta generada:", answer)

        # ✅ Guardar en base y sumar contador
        if user:
            new_qa = QA(user_id=user.id, question=question, answer=answer)
            db.session.add(new_qa)
            user.preguntas_usadas += 1
            db.session.commit()

        return jsonify({"answer": answer})

    except Exception as e:
        logging.error(f"❌ Error procesando la consulta: {str(e)}")
        return jsonify({"answer": "Ocurrió un error procesando tu consulta."}), 500

# ✅ RUTA PARA WHATSAPP (Twilio)
@chat_bp.route('/whatsapp', methods=['POST'])
def whatsapp_reply():
    incoming_msg = request.form.get('Body')
    logging.info(f"Mensaje recibido por WhatsApp: {incoming_msg}")

    respuesta = get_gpt_response(incoming_msg)

    resp = MessagingResponse()
    resp.message(respuesta)

    return str(resp)
