import os
import logging
import cohere
from models import User, QA, Rubro
from services.faq_matcher_spacy import buscar_en_faq_spacy
from services.intent_matcher import buscar_en_intents  # 👈 Integramos esto
from extensions import db

cohere_api_key = os.getenv("COHERE_API_KEY")
co = cohere.Client(cohere_api_key)

def responder_chatboc(pregunta, token):
    if not pregunta:
        return {"error": "Falta la pregunta"}

    user = User.query.filter_by(token=token).first()
    if not user and token == "demo-token":
        user = User.query.filter_by(token="demo-token").first()

    if not user:
        return {"error": "Usuario no autenticado"}

    if user.preguntas_usadas >= user.limite_preguntas:
        return {
            "respuesta": "🔒 Alcanzaste el límite de tu plan. Actualizá para más preguntas.",
            "fuente": "sistema"
        }

    rubro_id = user.rubro_id or 1
    rubro = Rubro.query.get(rubro_id)
    rubro_nombre = rubro.nombre if rubro else "general"
    logging.info(f"🧠 Buscando respuesta para: '{pregunta}' | Rubro: {rubro_nombre}")

    # Paso 1: Buscar en INTENTS
    intent_respuesta = buscar_en_intents(pregunta, rubro_nombre)
    if intent_respuesta:
        user.preguntas_usadas += 1
        db.session.commit()
        return {
            "respuesta": intent_respuesta,
            "nivel_usado": rubro_nombre,
            "fuente": "intents"
        }

    # Paso 2: Buscar en FAQs
    faq_match = buscar_en_faq_spacy(pregunta, rubro_id)
    if faq_match:
        user.preguntas_usadas += 1
        db.session.commit()
        return {
            "respuesta": faq_match.answer,
            "nivel_usado": rubro_nombre,
            "fuente": "faq"
        }

    # Paso 3: Fallback con Cohere
    try:
        cohere_response = co.generate(
            model="command",
            prompt=f"Respondé de forma clara y profesional esta consulta para una empresa del rubro {rubro_nombre}: {pregunta}",
            max_tokens=100,
            temperature=0.6,
        )
        generated_text = cohere_response.generations[0].text.strip()
    except Exception as e:
        logging.error(f"❌ Error en Cohere: {e}")
        return {
            "respuesta": "⚠️ No se pudo generar una respuesta automática por ahora.",
            "fuente": "error"
        }

    user.preguntas_usadas += 1
    db.session.commit()

    return {
        "respuesta": f"{generated_text} 🤖 (Respuesta generada con IA)",
        "nivel_usado": rubro_nombre,
        "fuente": "cohere"
    }
