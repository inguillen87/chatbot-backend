import os
import logging
import cohere
from models import User, QA, Rubro, Sugerencia
from services.faq_matcher_spacy import buscar_en_faq_spacy
from services.intent_matcher import buscar_en_intents
from extensions import db

# 🔐 Configurar cliente de Cohere
cohere_api_key = os.getenv("COHERE_API_KEY")
co = cohere.Client(cohere_api_key)

def obtener_sugerencias_por_rubro(rubro_id):
    sugerencias = Sugerencia.query.filter_by(rubro_id=rubro_id).all()
    if not sugerencias:
        sugerencias = Sugerencia.query.filter_by(rubro_id=1).all()  # fallback a 'general'
    return [s.texto for s in sugerencias]

def responder_chatboc(pregunta, token):
    if not pregunta:
        return {"error": "Falta la pregunta"}

    # 🔑 Validación de usuario
    user = User.query.filter_by(token=token).first()
    if not user:
        return {"error": "Usuario no autenticado"}

    if user.preguntas_usadas >= user.limite_preguntas:
        return {
            "respuesta": "🔒 Alcanzaste el límite de tu plan. Actualizá para más preguntas.",
            "fuente": "sistema"
        }

    # 📚 Contexto del rubro
    rubro_id = user.rubro_id or 1
    rubro = Rubro.query.get(rubro_id)
    rubro_nombre = rubro.nombre.lower() if rubro and rubro.nombre else "general"
    logging.info(f"🧠 Buscando respuesta para: '{pregunta}' | Rubro: {rubro_nombre}")

    # Paso 1: FAQ
    faq_match = buscar_en_faq_spacy(pregunta, rubro_id)
    if faq_match:
        user.preguntas_usadas += 1
        db.session.commit()
        return {
            "respuesta": faq_match.answer,
            "nivel_usado": rubro_nombre,
            "fuente": "faq"
        }

    # Paso 2: INTENTS
    intent_respuesta = buscar_en_intents(pregunta, rubro_nombre)
    if intent_respuesta:
        user.preguntas_usadas += 1
        db.session.commit()
        return {
            "respuesta": intent_respuesta,
            "nivel_usado": rubro_nombre,
            "fuente": "intents"
        }

    # Paso 3: Cohere
    try:
        prompt = (
            f"Sos Chatboc, un chatbot experto en el rubro '{rubro_nombre}'. "
            f"Respondé en forma breve, clara y profesional. "
            f"No inventes información. Si no sabés, pedí más detalles al cliente.\n"
            f"Consulta del cliente: \"{pregunta}\""
        )

        cohere_response = co.generate(
            model="command",
            prompt=prompt,
            max_tokens=60,
            temperature=0.4,
        )
        generated_text = cohere_response.generations[0].text.strip()

        if any(word in generated_text.lower() for word in ["the", "you can", "hospital", "insurance", "thank you"]):
            raise ValueError("Respuesta en inglés detectada")

        if "nft" in pregunta.lower() and "token" not in generated_text.lower():
            raise ValueError("Respuesta incoherente para NFT")

    except Exception as e:
        logging.error(f"❌ Error en Cohere: {e}")
        sugerencias = obtener_sugerencias_por_rubro(rubro_id)
        texto = "⚠️ No encontré una respuesta directa. Podés intentar con temas como: " + ", ".join(f"“{s}”" for s in sugerencias)
        return {
            "respuesta": texto,
            "fuente": "sugerencia"
        }

    user.preguntas_usadas += 1
    db.session.commit()

    return {
        "respuesta": f"{generated_text} 🤖 (Respuesta generada con IA)",
        "nivel_usado": rubro_nombre,
        "fuente": "cohere"
    }
