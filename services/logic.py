import os
import logging
import cohere
from flask import session
from models import User, QA, Rubro, Sugerencia
from services.faq_matcher_spacy import buscar_en_faq_spacy
from services.intent_matcher import buscar_en_intents
from extensions import db
import random

# 🔐 Configurar cliente de Cohere
cohere_api_key = os.getenv("COHERE_API_KEY")
co = cohere.Client(cohere_api_key)

def obtener_sugerencias_por_rubro(rubro_id):
    sugerencias = Sugerencia.query.filter_by(rubro_id=rubro_id).all()
    if not sugerencias:
        sugerencias = Sugerencia.query.filter_by(rubro_id=1).all()  # fallback a 'general'

    todas = [s.texto for s in sugerencias]
    seleccionadas = random.sample(todas, min(5, len(todas)))  # máximo 5 random
    return seleccionadas

def responder_chatboc(pregunta, token, rubro_nombre_frontend=None):
    if not pregunta:
        return {"error": "Falta la pregunta"}

    # 🔓 MODO DEMO ANÓNIMO
    if token.startswith("demo-anon"):
        if "anon_preguntas" not in session:
            session["anon_preguntas"] = 0

        if session["anon_preguntas"] >= 15:
            return {
                "respuesta": "🔒 Alcanzaste el límite de 15 preguntas en modo demo. Registrate gratis para seguir probando.",
                "fuente": "sistema"
            }

        session["anon_preguntas"] += 1

        class AnonUser:
            nombre_empresa = "Demo Anónimo"
            plan = "demo"
            preguntas_usadas = session["anon_preguntas"]
            limite_preguntas = 15
            rubro_id = None
            rubro_nombre = rubro_nombre_frontend or "general"

        user = AnonUser()

    else:
        user = User.query.filter_by(token=token).first()
        if not user:
            return {"error": "Usuario no autenticado"}

        if user.preguntas_usadas >= user.limite_preguntas:
            return {
                "respuesta": "🔒 Alcanzaste el límite de tu plan. Actualizá para más preguntas.",
                "fuente": "sistema"
            }

    # 📚 Determinar rubro real (prioridad al rubro del usuario)
    rubro_id = getattr(user, "rubro_id", None)
    rubro_nombre = "general"

    if rubro_id:
        rubro = Rubro.query.get(rubro_id)
        if rubro:
            rubro_nombre = rubro.nombre.lower().strip()

    # Si viene rubro manual desde frontend (modo demo), se usa solo si no hay uno real
    if not rubro_id and rubro_nombre_frontend:
        rubro_obj = Rubro.query.filter(db.func.lower(Rubro.nombre) == rubro_nombre_frontend.lower().strip()).first()
        if rubro_obj:
            rubro_id = rubro_obj.id
            rubro_nombre = rubro_obj.nombre.lower().strip()

    logging.info(f"🧠 Buscando respuesta para: '{pregunta}' | Rubro: {rubro_nombre}")

    # Paso 1: FAQ
    faq_match = buscar_en_faq_spacy(pregunta, rubro_id)
    if faq_match:
        if not token.startswith("demo-anon"):
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
        if not token.startswith("demo-anon"):
            user.preguntas_usadas += 1
            db.session.commit()
        return {
            "respuesta": intent_respuesta,
            "nivel_usado": rubro_nombre,
            "fuente": "intents"
        }

    # Paso 3: Cohere
    try:
        nombre_empresa = getattr(user, "nombre_empresa", "la empresa")
        prompt = (
            f"Sos Chatboc, el asistente virtual oficial de la empresa '{nombre_empresa}', que trabaja en el rubro '{rubro_nombre}'.\n"
            f"Respondé las consultas de los clientes de forma clara, profesional y útil.\n"
            f"Usá frases cortas y naturales. Evitá rodeos, tecnicismos innecesarios y no aclares que sos un asistente virtual ni que la respuesta fue generada con IA.\n"
            f"Respondé siempre como si fueras parte del equipo de la empresa. Si no sabés algo, pedí más detalles o derivá con amabilidad.\n"
            f"Plan actual del cliente: {user.plan}.\n\n"
            f"Consulta: \"{pregunta}\""
        )

        cohere_response = co.generate(
            model="command",
            prompt=prompt,
            max_tokens=500,
            temperature=0.5,
        )
        generated_text = cohere_response.generations[0].text.strip()

        if any(word in generated_text.lower() for word in ["the", "you can", "hospital", "insurance", "thank you"]):
            raise ValueError("Respuesta en inglés detectada")

        if "nft" in pregunta.lower() and "token" not in generated_text.lower():
            raise ValueError("Respuesta incoherente para NFT")

    except Exception as e:
        logging.error(f"❌ Error en Cohere: {e}")
        sugerencias = obtener_sugerencias_por_rubro(rubro_id or 1)
        texto = "No encontré una respuesta directa. Pero podés preguntar algo como: " + " · ".join(f"“{s}”" for s in sugerencias)
        return {
            "respuesta": texto,
            "fuente": "sugerencia"
        }

    if not token.startswith("demo-anon"):
        user.preguntas_usadas += 1
        db.session.commit()

    return {
        "respuesta": generated_text,
        "nivel_usado": rubro_nombre,
        "fuente": "cohere"
    }
