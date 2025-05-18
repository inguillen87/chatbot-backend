import os
import logging
import cohere
from models import User, QA, Rubro
from services.faq_matcher_spacy import buscar_en_faq_spacy
from services.intent_matcher import buscar_en_intents
from extensions import db

# 🔐 Configurar cliente de Cohere
cohere_api_key = os.getenv("COHERE_API_KEY")
co = cohere.Client(cohere_api_key)

def responder_chatboc(pregunta, token):
    if not pregunta:
        return {"error": "Falta la pregunta"}

    # 🔑 Validación de usuario o modo demo
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

    # 📚 Preparar contexto del rubro
    rubro_id = user.rubro_id or 1
    rubro = Rubro.query.get(rubro_id)
    rubro_nombre = rubro.nombre.lower() if rubro and rubro.nombre else "general"
    logging.info(f"🧠 Buscando respuesta para: '{pregunta}' | Rubro: {rubro_nombre}")

    # 🔍 Paso 1: Buscar en INTENTS
    intent_respuesta = buscar_en_intents(pregunta, rubro_nombre)
    if intent_respuesta:
        user.preguntas_usadas += 1
        db.session.commit()
        return {
            "respuesta": intent_respuesta,
            "nivel_usado": rubro_nombre,
            "fuente": "intents"
        }

    # 📘 Paso 2: Buscar en FAQs
    faq_match = buscar_en_faq_spacy(pregunta, rubro_id)
    if faq_match:
        user.preguntas_usadas += 1
        db.session.commit()
        return {
            "respuesta": faq_match.answer,
            "nivel_usado": rubro_nombre,
            "fuente": "faq"
        }

    # 🤖 Paso 3: Generación con Cohere si no hay match
    try:
        prompt = (
            f"Actuá como un asistente virtual especializado en el rubro '{rubro_nombre}'. "
            f"Respondé de forma breve, profesional y clara la siguiente consulta: {pregunta}. "
            f"Respondé en español neutro. No respondas en inglés ni inventes información si no estás seguro. "
            f"Si no entendés la consulta, pedí más detalles de forma educada."
        )

        cohere_response = co.generate(
            model="command",
            prompt=prompt,
            max_tokens=60,
            temperature=0.4,
        )
        generated_text = cohere_response.generations[0].text.strip()

        # 🚨 Validación de idioma y coherencia
        if any(word in generated_text.lower() for word in ["the", "you can", "hospital", "insurance", "thank you"]):
            raise ValueError("Respuesta en inglés detectada")

        if "nft" in pregunta.lower() and "token" not in generated_text.lower():
            raise ValueError("Respuesta incoherente para NFT")

    except Exception as e:
        logging.error(f"❌ Error en Cohere: {e}")
        return {
            "respuesta": "⚠️ No pude responder con suficiente precisión. ¿Podés reformular tu consulta?",
            "fuente": "error"
        }

    # 🧾 Registrar uso
    user.preguntas_usadas += rubro
    db.session.commit()

    return {
        "respuesta": f"{generated_text} 🤖 (Respuesta generada con IA)",
        "nivel_usado": rubro_nombre,
        "fuente": "cohere"
    }
