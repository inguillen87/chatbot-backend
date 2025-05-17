from flask import request, jsonify
from models import User, Rubro
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
        if not user.last_reset or (datetime.utcnow() - user.last_reset).days > 30:
            user.preguntas_usadas = 0
            user.last_reset = datetime.utcnow()
            db.session.commit()

        # Control de plan
        limite = PLAN_LIMITES.get(user.plan, 10)
        if user.preguntas_usadas >= limite:
            return jsonify({"respuesta": "🔒 Alcanzaste el límite de tu plan. Actualizá para más preguntas."})

        # Buscar en rubro del usuario y, si es necesario, subir al padre
        rubro_actual = Rubro.query.get(user.rubro_id)
        match = None
        niveles_intentados = []

        while rubro_actual and not match:
            niveles_intentados.append(rubro_actual.clave)
            print(f"🔎 Buscando en rubro: {rubro_actual.clave}")
            match = buscar_en_faq_spacy(pregunta, rubro_id=rubro_actual.id)
            rubro_actual = rubro_actual.parent

        if match:
            user.preguntas_usadas += 1
            db.session.commit()
            return jsonify({
                "respuesta": match.answer,
                "nivel_usado": niveles_intentados[0]
            })

        # Si no hay match y es premium, usar Cohere
        if user.plan == "premium":
            messages = [{"role": "user", "content": pregunta}]
            respuesta = get_cohere_response(messages, rubro_id=user.rubro_id)
            user.preguntas_usadas += 1
            db.session.commit()
            return jsonify({
                "respuesta": respuesta,
                "fuente": "cohere"
            })

        # Sin respuesta válida
        return jsonify({
            "respuesta": "🤖 No encontré una respuesta exacta. Intentá reformular la pregunta o actualizá tu plan para asistencia avanzada.",
            "nivel_usado": niveles_intentados
        })

    except Exception as e:
        print("❌ EXCEPCIÓN DETECTADA:", str(e))
        return jsonify({"respuesta": "⚠️ Error interno del servidor."}), 500
