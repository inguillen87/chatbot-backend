from flask import request, jsonify
from models import User, QA
from extensions import db
from services.faq_matcher_spacy import buscar_en_faq_spacy
from datetime import datetime

PLAN_LIMITES = {
    "free": 10,
    "pro": 50,
    "premium": 50  # + acceso OpenAI si está habilitado
}

def responder_chatboc():
    print("🚨 Entrando a responder_chatboc")
    data = request.get_json()
    print("📨 DATA:", data)

    pregunta = data.get("pregunta", "")
    print("🤔 Pregunta:", pregunta)

    token = request.headers.get("Authorization", "").replace("Bearer ", "")
    print("🔐 Token:", token)

    user = User.query.filter_by(token=token).first()
    if not user:
        return jsonify({"error": "Usuario no autenticado"}), 401

    # Reset mensual del contador de preguntas usadas
    if (datetime.utcnow() - user.last_reset).days > 30:
        user.preguntas_usadas = 0
        user.last_reset = datetime.utcnow()
        db.session.commit()

    # Control de plan y uso
    limite = PLAN_LIMITES.get(user.plan, 10)
    if user.preguntas_usadas >= limite:
        return jsonify({"respuesta": "Alcanzaste el límite de tu plan. Actualizá para más preguntas."})

    # Buscar por FAQ local del rubro
    match = buscar_en_faq_spacy(pregunta, rubro_id=user.rubro_id)
    if match:
        user.preguntas_usadas += 1
        db.session.commit()
        return jsonify({"respuesta": match.answer})

    # Escalar a OpenAI si es premium (opcional, depende si está habilitado)
    if user.plan == "premium":
        return jsonify({"respuesta": "No encontramos una respuesta exacta, pero estamos consultando con un experto..."})

    return jsonify({
        "respuesta": "No encontré una respuesta en tu pack actual. Actualizá tu plan para recibir atención personalizada."
    })
