# services/pymes.py

import logging
from flask import session as flask_session
from models import Conversacion, User, db
from services.utils_placeholders import reemplazar_placeholders
from services.utils import sugerencias_por_rubro
from urllib.parse import quote

NOMBRE_HISTORIAL_SESION = "historial_chat_cliente"
MAX_HISTORIAL_CHAT = 12

def limpiar_historial_sesion(nombre_historial=NOMBRE_HISTORIAL_SESION):
    try:
        if len(flask_session[nombre_historial]) > MAX_HISTORIAL_CHAT:
            flask_session[nombre_historial] = flask_session[nombre_historial][-MAX_HISTORIAL_CHAT:]
            flask_session.modified = True
    except Exception as e:
        logging.warning(f"[PYMES] Error limpiando historial: {e}")

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
        logging.error(f"[PYMES] Error guardando conversación: {e_db}", exc_info=True)
        db.session.rollback()

def responder_pyme(pregunta, user_obj, rubro_obj, session_obj=None, **kwargs):
    from services.webinfo import obtener_info_web
    from services.cohere_ai import get_cohere_response
    from services.faq_matcher_spacy import buscar_en_faq_spacy
    from services.intent_matcher import buscar_en_intents
    from services.vector_search import buscar_item_vectorizado

    session = session_obj if session_obj is not None else flask_session
    session.setdefault(NOMBRE_HISTORIAL_SESION, [])
    mensajes_previos = session[NOMBRE_HISTORIAL_SESION][-8:]
    user_id = getattr(user_obj, "id", None)

    # --- Límite de preguntas ---
    plan = getattr(user_obj, "plan", "anonimo")
    preguntas_usadas = getattr(user_obj, "preguntas_usadas", 0)
    limite_preguntas = getattr(user_obj, "limite_preguntas", 5)
    if preguntas_usadas >= limite_preguntas:
        return {"respuesta": "🔒 Límite de preguntas alcanzado en tu plan. Registrate o actualizá para más.", "fuente": "sistema_limite"}

    def seguro(val, fallback):
        return val.strip() if val and isinstance(val, str) else fallback

    nombre_pyme = seguro(getattr(user_obj, "nombre_empresa", ""), "la empresa")
    telefono = seguro(getattr(user_obj, "telefono", ""), "")
    link_web = seguro(getattr(user_obj, "link_web", ""), "")
    direccion = seguro(getattr(user_obj, "direccion", ""), "")
    ciudad = seguro(getattr(user_obj, "ciudad", ""), "")
    provincia = seguro(getattr(user_obj, "provincia", ""), "")
    direccion_completa = ', '.join(filter(None, [direccion, ciudad, provincia]))
    horario = seguro(getattr(user_obj, "horario", ""), "Consultar horarios en la web")
    rubro_nombre = getattr(rubro_obj, "nombre", "empresa")

    # --- Info extra de web (scraping) ---
    prompt_extra = ""
    info_web = {}
    whatsapp_link = ""
    if link_web:
        try:
            info_web = obtener_info_web(user_id, link_web)
        except Exception as e:
            logging.warning(f"[PYMES] Error scraping web: {e}")
            info_web = {}
    if info_web and not info_web.get("error"):
        prompt_extra = "\n\nInformación extra del sitio web de la empresa:"
        if info_web.get('emails'):
            mails = ', '.join(info_web['emails'])
            if mails: prompt_extra += f"\n- Emails: {mails}"
        if info_web.get('telefonos'):
            tels = ', '.join(info_web['telefonos'])
            if tels: prompt_extra += f"\n- Teléfonos: {tels}"
        if info_web.get('direcciones'):
            dirs = ', '.join(info_web['direcciones'])
            if dirs: prompt_extra += f"\n- Direcciones: {dirs}"
        if info_web.get('noticias'):
            noticias = '; '.join(info_web['noticias'][:5])
            if noticias: prompt_extra += f"\n- Noticias: {noticias}"
        if info_web.get('scrap_fecha'):
            prompt_extra += f"\n(Datos extraídos el {info_web['scrap_fecha'][:10]})"
        # Detectar link de WhatsApp preferido
        if info_web.get("whatsapp_links"):
            whatsapp_link = info_web["whatsapp_links"][0]

    # --- 1. Catálogo vectorizado (Qdrant) ---
    respuesta_llm = ""
    fuente = ""
    try:
        resp_vector = buscar_item_vectorizado(pregunta, user_obj)
        if resp_vector:
            respuesta_llm = resp_vector
            fuente = "catalogo_vector"
    except Exception as e:
        logging.warning(f"[PYMES] Error búsqueda catálogo vectorial: {e}")

    # --- 2. LLM Cohere ---
    if not respuesta_llm:
        try:
            prompt_pyme = (
                f"Sos Chatboc, agente comercial oficial de {nombre_pyme} ({rubro_nombre}), orientado a ventas y atención al cliente en LATAM."
                " Tu tarea es responder consultas y persuadir para comprar, consultar catálogo, reservar, pedir presupuesto o dejar datos de contacto."
                " Siempre respondé como un vendedor humano profesional (nunca digas que sos IA)."
                " Respondé al grano, llevá siempre a la acción y usá datos REALES del sistema: catálogo, scraping, historial y contexto."
                " Si la consulta es sobre productos, stock, precios, envíos, medios de pago, respondé con los datos cargados en la empresa (PDF/Excel o los que scrapearon)."
                " Si no encontrás la info exacta, orientá al usuario a consultar WhatsApp, web o dejar su contacto."
                f"\n- Teléfono: {telefono or 'No informado'}"
                f"\n- Dirección: {direccion_completa or 'No informada'}"
                f"\n- Horario: {horario or 'No informado'}"
                f"\n- Web: {link_web or 'No informada'}"
                f"{prompt_extra or ''}"
            )

            # Si hay catálogo cargado, avisar
            if getattr(user_obj, "catalogo_pdf_url", None) or getattr(user_obj, "catalogo_excel_url", None):
                prompt_pyme += "\nLa empresa tiene un catálogo digital cargado (PDF o Excel). Si preguntan por productos, ofertas o precios, respondé usando esos datos."

            # Historial de chat
            for msg in mensajes_previos:
                prompt_pyme += f"\n- {msg.get('role', 'user')}: {msg.get('content','')}"
            prompt_pyme += f"\n- Cliente: {pregunta}\n- Agente:"

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
            logging.exception("[PYMES] Error en Cohere para PyME")
            respuesta_llm = ""
            fuente = "error_llm"

    # --- 3. FAQ matcher ---
    if not respuesta_llm:
        try:
            faq = buscar_en_faq_spacy(pregunta, getattr(rubro_obj, "id", 1))
            if faq and faq.answer:
                respuesta_llm = reemplazar_placeholders(faq.answer, user_obj)
                fuente = "faq"
        except Exception as e:
            logging.warning(f"[PYMES] Error en FAQ: {e}")

    # --- 4. Intent matcher ---
    if not respuesta_llm:
        try:
            intent_resp = buscar_en_intents(pregunta, getattr(rubro_obj, "nombre", "general"))
            if intent_resp:
                respuesta_llm = reemplazar_placeholders(intent_resp, user_obj)
                fuente = "intent"
        except Exception as e:
            logging.warning(f"[PYMES] Error en Intents: {e}")

    # --- 5. Sugerencias por rubro ---
    if not respuesta_llm:
        rubro_key = None
        if rubro_obj and hasattr(rubro_obj, "nombre"):
            rubro_key = rubro_obj.nombre.lower().replace(" ", "_")
        elif isinstance(rubro_obj, str):
            rubro_key = rubro_obj.lower().replace(" ", "_")
        elif isinstance(rubro_obj, int):
            rubro_key = rubro_obj
        else:
            rubro_key = "bodega"
        sugs = sugerencias_por_rubro(rubro_key)
        respuesta_llm = "No encontré una respuesta directa. Probá con: " + " · ".join(f"“{s}”" for s in sugs if s)
        fuente = "sugerencia_sistema"

    # --- Guardar conversación e historial ---
    session[NOMBRE_HISTORIAL_SESION].extend([
        {"role": "user", "content": pregunta},
        {"role": "assistant", "content": respuesta_llm}
    ])
    limpiar_historial_sesion(NOMBRE_HISTORIAL_SESION)
    session.modified = True
    if user_id:
        guardar_conversacion_y_contador(user_obj, pregunta, respuesta_llm, fuente, getattr(rubro_obj, "nombre", "general"))

    # --- Botón WhatsApp: SIEMPRE el que sale del scraping, si no el teléfono ---
    def render_boton_whatsapp(telefono, pregunta, respuesta_llm, whatsapp_link=None):
        palabras_clave = ["comprar", "contactar", "hablar", "asesor", "whatsapp", "pedido", "presupuesto", "catálogo"]
        if (whatsapp_link or telefono) and any(x in respuesta_llm.lower() for x in palabras_clave):
            msg = quote(f"Hola, consulto por: '{pregunta}'")
            url = whatsapp_link if whatsapp_link else f"https://wa.me/{telefono}?text={msg}"
            return f'<div style="margin-top:7px;text-align:center;"><a href="{url}" target="_blank" style="display:inline-block;background:#25d366;color:#fff;padding:6px 14px;text-decoration:none;border-radius:5px;font-size:0.95em;font-weight:500;">💬 WhatsApp</a></div>'
        return ""

    respuesta_final = respuesta_llm + render_boton_whatsapp(telefono, pregunta, respuesta_llm, whatsapp_link=whatsapp_link)

    return {
        "respuesta": respuesta_final,
        "fuente": fuente
    }
