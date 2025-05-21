import os
import logging
import cohere
from flask import session
from models import User, QA, Rubro, Sugerencia
from services.faq_matcher_spacy import buscar_en_faq_spacy
from services.intent_matcher import buscar_en_intents
from extensions import db
import random

# Inicializar cliente Cohere
cohere_api_key = os.getenv("COHERE_API_KEY")
co = cohere.Client(cohere_api_key)

def obtener_sugerencias_por_rubro(rubro_id):
    try:
        sugerencias = Sugerencia.query.filter_by(rubro_id=rubro_id).all()
        if sugerencias:
            logging.info(f"✅ {len(sugerencias)} sugerencias encontradas para rubro_id={rubro_id}")
            todas = [s.texto for s in sugerencias]
            return random.sample(todas, min(5, len(todas)))
        logging.warning(f"⚠️ Sin sugerencias para rubro_id={rubro_id}. Usando rubro_id=1 (general)")
        fallback = Sugerencia.query.filter_by(rubro_id=1).all()
        return random.sample([s.texto for s in fallback], min(5, len(fallback))) if fallback else ["Lo siento, no tengo sugerencias disponibles en este momento."]
    except Exception as e:
        logging.error(f"❌ Error al obtener sugerencias: {e}")
        return ["Lo siento, ocurrió un error al buscar sugerencias."]

def responder_chatboc(pregunta, token, rubro_nombre_frontend=None):
    if not pregunta:
        return {"error": "Falta la pregunta"}

    # =====================
    # 🔓 Determinar usuario
    # =====================
    is_demo = token.startswith("demo-anon")
    user = None
    rubro_id = 1
    rubro_nombre = "general"

    if is_demo:
        session.setdefault("anon_preguntas", 0)
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

    # ===========================
    # 🧠 Determinar rubro válido
    # ===========================
    if user.rubro_id:
        rubro = Rubro.query.get(user.rubro_id)
        if rubro:
            rubro_id = rubro.id
            rubro_nombre = rubro.nombre.lower().strip()
    elif rubro_nombre_frontend:
        rubro_obj = Rubro.query.filter(db.func.lower(Rubro.nombre) == rubro_nombre_frontend.lower().strip()).first()
        if rubro_obj:
            rubro_id = rubro_obj.id
            rubro_nombre = rubro_obj.nombre.lower().strip()

    logging.info(f"📌 Usuario: {getattr(user, 'nombre_empresa', 'demo')} | Rubro: {rubro_nombre} (ID {rubro_id})")

    # ====================
    # ✅ Paso 1: FAQs spaCy
    # ====================
    faq_match = buscar_en_faq_spacy(pregunta, rubro_id)
    if faq_match:
        if not is_demo:
            user.preguntas_usadas += 1
            db.session.commit()
        return {
            "respuesta": faq_match.answer,
            "nivel_usado": rubro_nombre,
            "fuente": "faq"
        }

    # =====================
    # ✅ Paso 2: Intents JSON
    # =====================
    intent_respuesta = buscar_en_intents(pregunta, rubro_nombre)
    if intent_respuesta:
        if not is_demo:
            user.preguntas_usadas += 1
            db.session.commit()
        return {
            "respuesta": intent_respuesta,
            "nivel_usado": rubro_nombre,
            "fuente": "intents"
        }

    # =====================
    # ✅ Paso 3: Cohere GPT
    # =====================
    try:
        prompt = (
            f"Sos Chatboc, el asistente virtual oficial de '{user.nombre_empresa}', empresa del rubro '{rubro_nombre}'.\n"
            f"Respondé con claridad, profesionalismo y estilo humano.\n"
            f"No digas que sos IA ni menciones que es una respuesta generada.\n"
            f"Plan del cliente: {user.plan}.\n"
            f"Consulta: \"{pregunta}\""
        )

        cohere_response = co.generate(
            model="command",
            prompt=prompt,
            max_tokens=500,
            temperature=0.5,
        )
        generated_text = cohere_response.generations[0].text.strip()

        # Validaciones básicas para evitar errores de idioma o contexto
        if any(w in generated_text.lower() for w in ["the", "you can", "insurance", "hospital"]):
            raise ValueError("Respuesta en inglés detectada.")
        if "nft" in pregunta.lower() and "token" not in generated_text.lower():
            raise ValueError("Respuesta incoherente para NFT.")

    except Exception as e:
        logging.error(f"❌ Error en Cohere: {e}")
        sugerencias = obtener_sugerencias_por_rubro(rubro_id)
        return {
            "respuesta": "No encontré una respuesta directa. Podés probar preguntando algo como: " + " · ".join(f"“{s}”" for s in sugerencias),
            "fuente": "sugerencia"
        }

    if not is_demo:
        user.preguntas_usadas += 1
        db.session.commit()

    return {
        "respuesta": generated_text,
        "nivel_usado": rubro_nombre,
        "fuente": "cohere"
    }
