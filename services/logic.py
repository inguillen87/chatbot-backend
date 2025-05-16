from flask import request, jsonify
from models import User
from extensions import db
from datetime import datetime
from services.faq_matcher_spacy import buscar_en_faq_spacy
from services.cohere_ai import get_cohere_response

PLAN_LIMITES = {
    "free": 10,
    "pro": 50,
    "premium": 50
}

def responder_chatboc():
    print("🚨 Entrando a responder_chatboc")

    try:
        data = request.get_json()
        pregunta = data.get("pregunta") or data.get("question", "")
        token = request.headers.get("Authorization", "").replace("Bearer ", "")

        user = User.query.filter_by(token=token).first()
        if not user:
            return jsonify({"error": "Usuario no autenticado"}), 401

        # Reset mensual
        if (datetime.utcnow() - user.last_reset).days > 30:
            user.preguntas_usadas = 0
            user.last_reset = datetime.utcnow()
            db.session.commit()

        # Control de plan
        limite = PLAN_LIMITES.get(user.plan, 10)
        if user.preguntas_usadas >= limite:
            return jsonify({"respuesta": "🔒 Alcanzaste el límite de tu plan. Actualizá para más preguntas."})

        # Intento con spaCy (preguntas frecuentes)
        match = buscar_en_faq_spacy(pregunta, rubro_id=user.rubro_id)
        if match:
            user.preguntas_usadas += 1
            db.session.commit()
            return jsonify({"respuesta": match.answer})

        # Si no hay match y es premium, usar Cohere
        if user.plan == "premium":
            respuesta = get_cohere_response(pregunta)
            user.preguntas_usadas += 1
            db.session.commit()
            return jsonify({"respuesta": respuesta})

        # Si no hay respuesta ni acceso a IA
        return jsonify({
            "respuesta": "🤖 No encontré una respuesta exacta. Intentá reformular la pregunta o actualizá tu plan para asistencia avanzada."
        })

    except Exception as e:
        print("❌ EXCEPCIÓN DETECTADA:", str(e))
        return jsonify({"respuesta": "⚠️ Error interno del servidor."}), 500
