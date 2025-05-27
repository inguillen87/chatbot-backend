import os
import logging
from flask import session
from models import User, QA, Rubro, Sugerencia, Conversacion, CatalogoItem
from services.faq_matcher_spacy import buscar_en_faq_spacy
from services.intent_matcher import buscar_en_intents
from services.cohere_ai import get_cohere_response, embed_textos  # ✅ Uso modular
from services.vector_search import buscar_item_vectorizado  # 🔁 necesario para responder

from extensions import db
import random


def reemplazar_placeholders(texto: str, user) -> str:
    def safe(val, fallback=""): return str(val or fallback)

    return (
        texto
        .replace("[nombreEmpresa]", safe(getattr(user, "nombre_empresa", "nuestra empresa")))
        .replace("[linkWeb]", safe(getattr(user, "link_web", "https://tusitioweb.com")))
        .replace("[telefono]", f"https://wa.me/{safe(getattr(user, 'telefono', ''))}")
        .replace("[direccion]", safe(getattr(user, "direccion", "dirección no informada")))
        .replace("[horario]", safe(getattr(user, "horario", "horario no disponible")))
        .replace("[ubicacion]", safe(getattr(user, "ubicacion", "")))
        .replace("[rubroNombre]", safe(getattr(user, "rubro_nombre", "empresa")))
    )


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


def responder_chatboc(pregunta, token, rubro_nombre_frontend=None, historial=[]):
    if not pregunta:
        return {"error": "Falta la pregunta"}

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

    historial_chat = []
    try:
        if not is_demo:
            historial_chat = Conversacion.query.filter_by(user_id=user.id).order_by(Conversacion.timestamp.desc()).limit(5).all()
    except Exception as e:
        logging.warning(f"⚠️ No se pudo obtener historial de conversación: {e}")

    historial_texto = []
    for conv in reversed(historial_chat):
        historial_texto.append({"role": "user", "content": conv.pregunta})
        historial_texto.append({"role": "assistant", "content": conv.respuesta})
    historial_texto.append({"role": "user", "content": pregunta})

    # Paso 1: búsqueda vectorizada
    if not is_demo:
        try:
            respuesta_vector = buscar_item_vectorizado(pregunta, user.id)
            if respuesta_vector:
                user.preguntas_usadas += 1
                db.session.commit()
                db.session.add(Conversacion(user_id=user.id, pregunta=pregunta, respuesta=respuesta_vector, fuente="vector", rubro=rubro_nombre))
                db.session.commit()
                return {"respuesta": respuesta_vector, "nivel_usado": rubro_nombre, "fuente": "vector"}
        except Exception as e:
            logging.warning(f"❌ Error al usar vector embedding: {e}")

    # Paso 2: FAQ
    try:
        faq_match = buscar_en_faq_spacy(pregunta, rubro_id)
    except Exception as e:
        faq_match = None
        logging.warning(f"⚠️ Error buscando en FAQ: {e}")

    if faq_match:
        if not is_demo:
            try:
                user.preguntas_usadas += 1
                db.session.commit()
                db.session.add(Conversacion(user_id=user.id, pregunta=pregunta, respuesta=faq_match.answer, fuente="faq", rubro=rubro_nombre))
                db.session.commit()
            except Exception as e:
                logging.warning(f"⚠️ Error guardando conversación FAQ: {e}")
        return {"respuesta": faq_match.answer, "nivel_usado": rubro_nombre, "fuente": "faq"}

    # Paso 3: Intents
    try:
        intent_respuesta = buscar_en_intents(pregunta, rubro_nombre)
    except Exception as e:
        intent_respuesta = None
        logging.warning(f"⚠️ Error buscando en intents: {e}")

    if intent_respuesta:
        intent_respuesta = reemplazar_placeholders(intent_respuesta, user)
        if not is_demo:
            try:
                user.preguntas_usadas += 1
                db.session.commit()
                db.session.add(Conversacion(user_id=user.id, pregunta=pregunta, respuesta=intent_respuesta, fuente="intents", rubro=rubro_nombre))
                db.session.commit()
            except Exception as e:
                logging.warning(f"⚠️ Error guardando conversación intent: {e}")
        return {"respuesta": intent_respuesta, "nivel_usado": rubro_nombre, "fuente": "intents"}

    # Paso 3.5: Catálogo de productos
    try:
        catalogo_respuesta = None
        if not is_demo:
            from services.catalogo_matcher import buscar_en_catalogo
            respuesta_vector = buscar_item_vectorizado(pregunta, user)

        if catalogo_respuesta:
            user.preguntas_usadas += 1
            db.session.commit()
            db.session.add(Conversacion(user_id=user.id, pregunta=pregunta, respuesta=catalogo_respuesta, fuente="catalogo", rubro=rubro_nombre))
            db.session.commit()
            return {"respuesta": catalogo_respuesta, "nivel_usado": rubro_nombre, "fuente": "catalogo"}

    except Exception as e:
        logging.warning(f"⚠️ Error en catálogo matcher: {e}")

    # Paso 4: Cohere
    try:
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

        system_prompt = (
            f"Sos Chatboc, el asistente comercial oficial de {user_context['nombre_empresa']}, "
            f"dedicado a ayudar a clientes en el rubro {user_context['rubro_nombre']}. "
            f"Tu objetivo es asistir, recomendar productos, resolver dudas y guiar al usuario hacia una compra o contacto real. "
            f"Estás ubicado en {user_context['ubicacion']} y tenés tienda en {user_context['link_web']}. "
            f"Tu horario es: {user_context['horario']}. "
            f"Respondé siempre de forma amable, conversacional, directa y en español. "
            f"Si hay un link, dirección o WhatsApp, usalo. Nunca digas que sos una inteligencia artificial."
        )

        generated_text = get_cohere_response(historial_texto, rubro_id=rubro_id, user_context=user_context, system_prompt=system_prompt)
        respuesta_final = reemplazar_placeholders(generated_text, user)

    except Exception as e:
        logging.error(f"❌ Error al generar respuesta con Cohere: {e}")
        sugerencias = obtener_sugerencias_por_rubro(rubro_id)
        return {
            "respuesta": "No encontré una respuesta directa. Podés probar preguntando algo como: " + " · ".join(f"“{s}”" for s in sugerencias),
            "fuente": "sugerencia"
        }

    if not is_demo:
        try:
            user.preguntas_usadas += 1
            db.session.commit()
            nueva = Conversacion(user_id=user.id, pregunta=pregunta, respuesta=respuesta_final, fuente="cohere", rubro=rubro_nombre)
            db.session.add(nueva)
            db.session.commit()
            logging.info(f"💬 Conversación guardada: {pregunta[:50]}...")
        except Exception as e:
            logging.warning(f"⚠️ No se pudo guardar la conversación: {e}")

    return {"respuesta": respuesta_final, "nivel_usado": rubro_nombre, "fuente": "cohere"}
