import logging
import random
from flask import session
from models import User, Rubro, Sugerencia, Conversacion
from extensions import db

from services.qdrant_search import buscar_catalogo_qdrant, armar_respuesta_legible
from services.cohere_ai import get_cohere_response
from services.faq_matcher_spacy import buscar_en_faq_spacy
from services.intent_matcher import buscar_en_intents

def reemplazar_placeholders(texto, user):
    # Stub simple, reemplazá con tu función real si tenés
    if not texto:
        return texto
    if hasattr(user, "nombre_empresa"):
        texto = texto.replace("[nombreEmpresa]", user.nombre_empresa)
    return texto

def sugerencias_por_rubro(rubro_id):
    try:
        sugerencias = Sugerencia.query.filter_by(rubro_id=rubro_id).all()
        if sugerencias:
            todas = [s.texto for s in sugerencias]
            return random.sample(todas, min(5, len(todas)))
        # fallback si no hay para ese rubro
        fallback = Sugerencia.query.filter_by(rubro_id=1).all()
        return random.sample([s.texto for s in fallback], min(5, len(fallback))) if fallback else ["No tengo sugerencias en este momento."]
    except Exception as e:
        logging.error(f"❌ Error buscando sugerencias: {e}")
        return ["Lo siento, ocurrió un error al buscar sugerencias."]

def responder_chatboc(pregunta, token, rubro_nombre_frontend=None, historial=[]):
    if not pregunta:
        return {"error": "Falta la pregunta"}

    # --- Usuario y límites ---
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

    # --- Rubro ---
    if hasattr(user, "rubro_id") and user.rubro_id:
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

    # --- Historial conversacional ---
    historial_chat = []
    if not is_demo and hasattr(user, "id"):
        try:
            historial_chat = Conversacion.query.filter_by(user_id=user.id).order_by(Conversacion.timestamp.desc()).limit(10).all()
        except Exception as e:
            logging.warning(f"⚠️ No se pudo obtener historial de conversación: {e}")

    mensajes = []
    for conv in reversed(historial_chat):
        mensajes.append({"role": "user", "content": conv.pregunta})
        mensajes.append({"role": "assistant", "content": conv.respuesta})
    mensajes.append({"role": "user", "content": pregunta})

    # --- 1. Qdrant: buscar catálogo relevante ---
    resultados_qdrant = []
    contexto_catalogo = ""
    if not is_demo and hasattr(user, "id"):
        try:
            logging.info("Antes de buscar en Qdrant")
            resultados_qdrant = buscar_catalogo_qdrant(user.id, pregunta, limite=5)
            logging.info(f"Resultados Qdrant: {resultados_qdrant}")
            contexto_catalogo = armar_respuesta_legible(resultados_qdrant)
        except Exception as e:
            logging.warning(f"❌ Error al buscar en Qdrant: {e}")

    # --- 2. Cohere: generar respuesta inteligente ---
    user_context = {
        "nombre_empresa": getattr(user, "nombre_empresa", "la empresa"),
        "rubro_nombre": rubro_nombre,
        "plan": getattr(user, "plan", "demo"),
        "telefono": getattr(user, "telefono", ""),
        "link_web": getattr(user, "link_web", ""),
        "direccion": getattr(user, "direccion", ""),
        "horario": getattr(user, "horario", ""),
        "ubicacion": getattr(user, "ubicacion", "")
    }

    if contexto_catalogo:
        prompt = (
            f"Contexto del catálogo extraído automáticamente (productos relevantes):\n{contexto_catalogo}\n"
            f"Sos Chatboc, el agente comercial digital de {user_context['nombre_empresa']} (rubro: {user_context['rubro_nombre']}). "
            f"Tu objetivo es vender, sugerir productos, mostrar promociones, responder con info precisa y guiar la conversación a una acción (ejemplo: compra, reserva, pedir más info, mandar link, enviar WhatsApp, etc). "
            f"Nunca digas que sos IA. Usá siempre un tono humano, amable, directo, y ofrecé ayuda para cerrar una venta o resolver la consulta.\n"
            f"Datos útiles: dirección {user_context['direccion']}, link {user_context['link_web']}, tel {user_context['telefono']}, horario {user_context['horario']}.\n"
            f"Respondé la siguiente conversación como si fueras un vendedor profesional y digital."
        )
    else:
        prompt = (
            f"Sos Chatboc, el agente comercial digital de {user_context['nombre_empresa']} (rubro: {user_context['rubro_nombre']}). "
            f"Tu objetivo es vender, sugerir productos, mostrar promociones, responder con info precisa y guiar la conversación a una acción (ejemplo: compra, reserva, pedir más info, mandar link, enviar WhatsApp, etc). "
            f"Nunca digas que sos IA. Usá siempre un tono humano, amable, directo, y ofrecé ayuda para cerrar una venta o resolver la consulta.\n"
            f"Datos útiles: dirección {user_context['direccion']}, link {user_context['link_web']}, tel {user_context['telefono']}, horario {user_context['horario']}.\n"
            f"Respondé la siguiente conversación como si fueras un vendedor profesional y digital."
        )
    messages = [{"role": "system", "content": prompt}] + mensajes

    respuesta_final = ""
    try:
        respuesta_final = get_cohere_response(messages, rubro_id=rubro_id, user_context=user_context)
        respuesta_final = reemplazar_placeholders(respuesta_final, user)
    except Exception as e:
        logging.error(f"❌ Error al generar respuesta con Cohere: {e}")

    if respuesta_final and len(respuesta_final) > 5:
        if not is_demo and hasattr(user, "id"):
            user.preguntas_usadas += 1
            db.session.commit()
            db.session.add(Conversacion(user_id=user.id, pregunta=pregunta, respuesta=respuesta_final, fuente="cohere", rubro=rubro_nombre))
            db.session.commit()
        return {"respuesta": respuesta_final, "nivel_usado": rubro_nombre, "fuente": "cohere"}

    # --- 3. FAQs como backup ---
    try:
        faq_match = buscar_en_faq_spacy(pregunta, rubro_id)
        if faq_match:
            if not is_demo and hasattr(user, "id"):
                user.preguntas_usadas += 1
                db.session.commit()
                db.session.add(Conversacion(user_id=user.id, pregunta=pregunta, respuesta=faq_match.answer, fuente="faq", rubro=rubro_nombre))
                db.session.commit()
            return {"respuesta": faq_match.answer, "nivel_usado": rubro_nombre, "fuente": "faq"}
    except Exception as e:
        logging.warning(f"⚠️ Error buscando en FAQ: {e}")

    # --- 4. Intents como backup ---
    try:
        intent_respuesta = buscar_en_intents(pregunta, rubro_nombre)
        if intent_respuesta:
            intent_respuesta = reemplazar_placeholders(intent_respuesta, user)
            if not is_demo and hasattr(user, "id"):
                user.preguntas_usadas += 1
                db.session.commit()
                db.session.add(Conversacion(user_id=user.id, pregunta=pregunta, respuesta=intent_respuesta, fuente="intents", rubro=rubro_nombre))
                db.session.commit()
            return {"respuesta": intent_respuesta, "nivel_usado": rubro_nombre, "fuente": "intents"}
    except Exception as e:
        logging.warning(f"⚠️ Error buscando en intents: {e}")

    # --- 5. Sugerencias como último recurso ---
    sugerencias = sugerencias_por_rubro(rubro_id)
    return {
        "respuesta": "No encontré una respuesta directa. Probá preguntando: " + " · ".join(f"“{s}”" for s in sugerencias),
        "fuente": "sugerencia"
    }
