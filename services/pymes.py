# services/pymes.py

import logging
import random
import re
import json
import urllib.parse
from flask import session as flask_session
from models import Conversacion, User, Rubro, Sugerencia, db
from services.utils import (
    reemplazar_placeholders,
    formatear_numero_whatsapp_simple,
    sugerencias_por_rubro
)

logger = logging.getLogger(__name__)

NOMBRE_HISTORIAL_SESION = "historial_chat_cliente"
MAX_HISTORIAL_CHAT = 12

def limpiar_historial_sesion(nombre_historial=NOMBRE_HISTORIAL_SESION):
    try:
        if len(flask_session[nombre_historial]) > MAX_HISTORIAL_CHAT:
            flask_session[nombre_historial] = flask_session[nombre_historial][-MAX_HISTORIAL_CHAT:]
            flask_session.modified = True
    except Exception as e:
        logger.warning(f"[PYMES] Error limpiando historial: {e}")

def guardar_conversacion_y_contador(user_obj, pregunta, respuesta, fuente, rubro_nombre_final):
    if not user_obj or not hasattr(user_obj, "id") or not user_obj.id:
        return
    try:
        db_user_to_update = db.session.get(User, user_obj.id)
        if db_user_to_update:
            db_user_to_update.preguntas_usadas = (db_user_to_update.preguntas_usadas or 0) + 1
            db.session.add(Conversacion(
                user_id=user_obj.id,
                pregunta=pregunta,
                respuesta=respuesta,
                fuente=fuente,
                rubro=rubro_nombre_final
            ))
            db.session.commit()
    except Exception as e_db:
        logger.error(f"[PYMES] Error guardando conversación: {e_db}", exc_info=True)
        db.session.rollback()

def responder_pyme(pregunta, user_obj, rubro_obj, session_obj=None, **kwargs):
    session = session_obj if session_obj is not None else flask_session
    session.setdefault(NOMBRE_HISTORIAL_SESION, [])
    mensajes_previos = session[NOMBRE_HISTORIAL_SESION][-8:]
    user_id = getattr(user_obj, "id", None)

    # --- Lógica de límite de preguntas (demo, anónimos, plan, etc)
    plan = getattr(user_obj, "plan", "anonimo")
    preguntas_usadas = getattr(user_obj, "preguntas_usadas", 0)
    limite_preguntas = getattr(user_obj, "limite_preguntas", 5)
    if preguntas_usadas >= limite_preguntas:
        return {"respuesta": "🔒 Límite de preguntas alcanzado en tu plan. Registrate o actualizá para más.", "fuente": "sistema_limite"}

    # --- Info base para prompts
    nombre_pyme = getattr(user_obj, "nombre_empresa", "la empresa")
    telefono = getattr(user_obj, "telefono", "")
    link_web = getattr(user_obj, "link_web", "")
    direccion = getattr(user_obj, "direccion", "")
    ciudad = getattr(user_obj, "ciudad", "")
    provincia = getattr(user_obj, "provincia", "")
    direccion_completa = f"{direccion}, {ciudad}, {provincia}".replace(" ,", "").strip(", ")
    horario = getattr(user_obj, "horario", "Consultar horarios en la web")
    horario_json_str = getattr(user_obj, "horario_json", "[]")

    # --- Armar prompt e historial
    prompt_pyme = (
        f"Sos Chatboc, el agente de atención de {nombre_pyme}. Tu objetivo es ayudar al cliente a resolver dudas y llevarlo a la acción (comprar, consultar catálogo, agendar visita, etc.) en el menor número de intercambios. "
        "Respondé de manera profesional y persuasiva, como un vendedor humano, nunca como IA. "
        f"Datos útiles: Tel: {telefono} | Dirección: {direccion_completa} | Horario: {horario} | Web: {link_web}."
    )

    for msg in mensajes_previos:
        prompt_pyme += f"\n- {msg.get('role', 'user')}: {msg.get('content','')}"

    prompt_pyme += f"\n- Cliente: {pregunta}\n- Agente:"

    # --- Integración catálogo (Qdrant, etc.) – opcional
    # contexto_catalogo = ""  # Podés agregar lógica Qdrant aquí

    # --- Llamada a Cohere (o el LLM que uses)
    try:
        from services.cohere_ai import get_cohere_response
        respuesta_llm = get_cohere_response(
            message=pregunta,
            chat_history=[{"role": m.get("role", "user"), "message": m.get("content", "")} for m in mensajes_previos],
            preamble=prompt_pyme,
            rubro_id=getattr(rubro_obj, "id", 1),
            user_context={
                "nombre_empresa": nombre_pyme,
                "telefono": telefono,
                "link_web": link_web,
                "direccion": direccion_completa,
                "horario": horario
            }
        )
        respuesta_llm = reemplazar_placeholders(respuesta_llm, user_obj)
        fuente = "cohere"
    except Exception as e:
        logger.exception("Error en Cohere para PyME")
        respuesta_llm = ""
        fuente = "error_llm"

    # --- Si no hay respuesta, intentá FAQ/Intents
    if not respuesta_llm:
        try:
            from services.faq_matcher_spacy import buscar_en_faq_spacy
            faq = buscar_en_faq_spacy(pregunta, getattr(rubro_obj, "id", 1))
            if faq and faq.answer:
                respuesta_llm = reemplazar_placeholders(faq.answer, user_obj)
                fuente = "faq"
        except Exception as e:
            logger.warning(f"[PYMES] Error en FAQ: {e}")

    if not respuesta_llm:
        try:
            from services.intent_matcher import buscar_en_intents
            intent_resp = buscar_en_intents(pregunta, getattr(rubro_obj, "nombre", "general"))
            if intent_resp:
                respuesta_llm = reemplazar_placeholders(intent_resp, user_obj)
                fuente = "intent"
        except Exception as e:
            logger.warning(f"[PYMES] Error en Intents: {e}")

    # --- Fallback: sugerencias
    if not respuesta_llm:
        sugs = sugerencias_por_rubro(getattr(rubro_obj, "id", 1))
        respuesta_llm = "No encontré una respuesta directa. Probá con: " + " · ".join(f"“{s}”" for s in sugs if s)
        fuente = "sugerencia_sistema"

    # --- Guardar conversación e historial
    session[NOMBRE_HISTORIAL_SESION].extend([
        {"role": "user", "content": pregunta},
        {"role": "assistant", "content": respuesta_llm}
    ])
    limpiar_historial_sesion(NOMBRE_HISTORIAL_SESION)
    session.modified = True
    if user_id:
        guardar_conversacion_y_contador(user_obj, pregunta, respuesta_llm, fuente, getattr(rubro_obj, "nombre", "general"))

    # --- Botón de WhatsApp (solo si la respuesta lo justifica)
    # Usá lógica como la que tenías: solo agrega el botón si corresponde (por ejemplo, contacto, comprar, etc.)

    return {
        "respuesta": respuesta_llm,
        "fuente": fuente
    }
