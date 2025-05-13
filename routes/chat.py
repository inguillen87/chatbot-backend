from flask import Blueprint, request, jsonify
from twilio.twiml.messaging_response import MessagingResponse  # type: ignore
from services.faq_matcher_spacy import buscar_en_faq_spacy
from services.nlp import get_gpt_response
from models import QA, User
from extensions import db
from datetime import datetime
import logging
import os

chat_bp = Blueprint('chat', __name__)

# ✅ Ruta principal para usuarios logueados
@chat_bp.route('/ask', methods=['POST'])
def ask():
    token = request.headers.get("Authorization", "")
    if not token:
        logging.warning("Token no enviado")
        return jsonify({"error": "No autorizado"}), 401

    data = request.get_json()
    question = data.get("question", "").strip()
    user_id = data.get("user_id")

    if not question:
        return jsonify({"answer": "Por favor escribí una consulta válida."})

    try:
        user = User.query.get(user_id) if user_id else None

        print("📥 Pregunta recibida:", question)
        print("👤 Usuario:", user.name if user else "anónimo")
        print("📊 Plan:", user.plan if user else "sin plan")

        # 1. Intentar responder con FAQs locales
        answer = buscar_en_faq_spacy(question)

        # 2. Si no hay respuesta local, controlar el plan y usar GPT
        if not answer:
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

                if user.preguntas_usadas >= limit:
                    return jsonify({
                        "answer": f"🛑 Alcanzaste el límite de tu plan ({user.plan}). Actualizá tu suscripción para seguir usando Chatboc."
                    })

            # Usar GPT si no se encontró en FAQ
            answer = get_gpt_response(question, user)
            print("🧠 Respuesta generada por GPT:", answer)

            # Guardar la consulta si es GPT
            if user:
                new_qa = QA(user_id=user.id, question=question, answer=answer)
                db.session.add(new_qa)
                user.preguntas_usadas += 1
                db.session.commit()

        return jsonify({"answer": answer})

    except Exception as e:
        logging.error(f"❌ Error procesando la consulta: {str(e)}")
        return jsonify({"answer": "Ocurrió un error procesando tu consulta."}), 500


# ✅ Ruta abierta para demo
@chat_bp.route("/demo-chat", methods=["POST"])
def demo_chat():
    try:
        data = request.get_json()
        messages = data.get("messages", [])

        last_user_msg = ""
        for m in reversed(messages):
            if m["role"] == "user":
                last_user_msg = m["content"]
                break

        if not last_user_msg:
            return jsonify({"content": "Por favor escribí una consulta válida."})

        print("🧪 DEMO - pregunta:", last_user_msg)

        class FakeUser:
            name = "Usuario demo"
            industry = "Pyme demo"

        answer = get_gpt_response(last_user_msg, FakeUser())
        print("🧪 DEMO - respuesta:", answer)

        return jsonify({"content": answer})

    except Exception as e:
        logging.error(f"❌ Error en demo_chat: {e}")
        return jsonify({"content": "⚠️ No se pudo generar una respuesta en este momento."}), 500


# ✅ Ruta para WhatsApp
@chat_bp.route('/whatsapp', methods=['POST'])
def whatsapp_reply():
    incoming_msg = request.form.get('Body')
    logging.info(f"Mensaje recibido por WhatsApp: {incoming_msg}")

    respuesta = get_gpt_response(incoming_msg)

    resp = MessagingResponse()
    resp.message(respuesta)

    return str(resp)
