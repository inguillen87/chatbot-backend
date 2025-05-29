import logging
import random
from flask import session
from models import User, Rubro, Sugerencia, Conversacion
from extensions import db

def sugerencias_por_rubro(rubro_id):
    try:
        sugerencias_obj = Sugerencia.query.filter_by(rubro_id=rubro_id).all()
        if sugerencias_obj:
            todas = [s.texto for s in sugerencias_obj]
            logging.info(f"Sugerencias para rubro {rubro_id}: {len(todas)}")
            return random.sample(todas, min(5, len(todas)))
        if rubro_id != 1:
            fallback_obj = Sugerencia.query.filter_by(rubro_id=1).all()
            if fallback_obj:
                logging.info(f"Fallback general para rubro {rubro_id}")
                return random.sample([s.texto for s in fallback_obj], min(5, len(fallback_obj)))
        return ["Consulta nuestros productos.", "Háblame más de lo que buscas."]
    except Exception as e:
        logging.error(f"Error en sugerencias: {e}", exc_info=True)
        return ["Disculpa, tuve un problema buscando sugerencias."]

def reemplazar_placeholders(texto, user_obj):
    if user_obj is None:
        empresa = "la empresa"
        link = ""
    else:
        empresa = getattr(user_obj, "nombre_empresa", "la empresa")
        link = getattr(user_obj, "link_web", "")
    try:
        texto = texto.replace("[nombreEmpresa]", empresa)
        texto = texto.replace("[linkWeb]", link)
        return texto
    except Exception as e:
        logging.error(f"Error en placeholders: {e}", exc_info=True)
        return texto

def responder_chatboc(pregunta, token, rubro_nombre_frontend=None):
    logging.info(f"▶️ Inicio responder_chatboc: '{pregunta}' token='{token}' rubro='{rubro_nombre_frontend}'")

    if not pregunta:
        return {"error": "Falta la pregunta"}

    HISTORIAL_SESION = 'historial_chat_cliente'
    if HISTORIAL_SESION not in session:
        session[HISTORIAL_SESION] = []
        logging.info(f"Inicializando historial en sesión")

    if "contador_interacciones" not in session:
        session["contador_interacciones"] = 0

    is_demo = False
    user = None
    rubro_id = 1
    rubro_nombre = "general"

    try:
        is_demo = token.startswith("demo-anon")
    except Exception as e:
        logging.warning(f"Token inválido: {e}")

    if is_demo:
        session.setdefault("anon_preguntas", 0)
        if session["anon_preguntas"] >= 15:
            return {"respuesta": "🔒 Límite demo alcanzado. Registrate para seguir.", "fuente": "sistema"}
        session["anon_preguntas"] += 1

        class AnonUser:
            nombre_empresa = "Demo Anónimo"
            plan = "demo"
            preguntas_usadas = session["anon_preguntas"]
            limite_preguntas = 15
            rubro_id = None
            link_web = "tu-tienda-online.com"
            telefono = "123-456-7890"
            direccion = "Calle Falsa 123"
            horario = "Lun-Vie 9-18"
            ubicacion = ""
            id = None
        user = AnonUser()
    else:
        try:
            db_user = User.query.filter_by(token=token).first()
            if not db_user:
                return {"error": "Usuario no autenticado"}
            if db_user.preguntas_usadas >= db_user.limite_preguntas:
                return {"respuesta": "🔒 Límite de preguntas alcanzado. Actualizá para más.", "fuente": "sistema"}
            user = db_user
        except Exception as e:
            logging.error(f"Error usuario BD: {e}", exc_info=True)
            return {"error": "Error interno al obtener usuario"}

    # Rubro
    try:
        if hasattr(user, "rubro_id") and user.rubro_id:
            rubro_obj = Rubro.query.get(user.rubro_id)
            if rubro_obj:
                rubro_id = rubro_obj.id
                rubro_nombre = rubro_obj.nombre.lower().strip()
        elif rubro_nombre_frontend:
            rubro_obj = Rubro.query.filter(db.func.lower(Rubro.nombre) == rubro_nombre_frontend.lower().strip()).first()
            if rubro_obj:
                rubro_id = rubro_obj.id
                rubro_nombre = rubro_obj.nombre.lower().strip()
    except Exception as e:
        logging.error(f"Error en rubro: {e}")

    # Incrementar contador de interacciones
    session["contador_interacciones"] += 1
    interacciones = session["contador_interacciones"]

    # Historial para LLM (máx 8 mensajes)
    historial = session[HISTORIAL_SESION]
    max_historial = 8
    mensajes_para_llm = historial[-max_historial:] if len(historial) > max_historial else historial[:]
    mensajes_para_llm.append({"role": "user", "content": pregunta})

    # Buscar catálogo con Qdrant + Document AI
    contexto_catalogo = ""
    if not is_demo and hasattr(user, "id") and user.id is not None:
        try:
            from services.qdrant_search import buscar_catalogo_qdrant, armar_respuesta_legible
            resultados_qdrant = buscar_catalogo_qdrant(user.id, pregunta, limite=5)
            contexto_catalogo = armar_respuesta_legible(resultados_qdrant)
        except Exception as e:
            logging.error(f"Error Qdrant: {e}", exc_info=True)

    user_profile = {
        "nombre_empresa": getattr(user, "nombre_empresa", "la empresa"),
        "rubro_nombre": rubro_nombre,
        "telefono": getattr(user, "telefono", "no disponible"),
        "link_web": getattr(user, "link_web", "no disponible"),
        "direccion": getattr(user, "direccion", "no disponible"),
        "horario": getattr(user, "horario", "no disponible"),
    }

    # Prompt para Cohere
    prompt = (
        f"Sos Chatboc, asistente comercial experto de {user_profile['nombre_empresa']} (rubro: {user_profile['rubro_nombre']}).\n"
        f"Tu objetivo es entender rápido las necesidades del cliente y guiarlo a una compra o visita a la tienda online ({user_profile['link_web']}) en los próximos 2-4 intercambios.\n"
        f"Llevamos {interacciones} intercambios. Sé amable, proactivo y persuasivo. Haz preguntas claras.\n"
        "Si el cliente muestra interés, intenta cerrar la venta ofreciendo añadir al carrito, mostrar link o siguiente paso.\n"
        "No digas que eres IA.\n"
        f"Datos de contacto: Tel: {user_profile['telefono']}, Dirección: {user_profile['direccion']}, Horario: {user_profile['horario']}.\n"
    )
    if contexto_catalogo:
        prompt += f"\nInformación relevante del catálogo:\n---\n{contexto_catalogo}\n---\nUsá esta info para ofrecer productos y precios.\n"
    else:
        prompt += "\nNo encontré info del catálogo, usa conocimiento general.\n"
    prompt += "\nInicia tu respuesta directamente al cliente.\n"

    mensajes_finales = [{"role": "system", "content": prompt}] + mensajes_para_llm

    respuesta_final = ""
    fuente = "desconocida"

    # Llamar a Cohere
    try:
        from services.cohere_ai import get_cohere_response
        respuesta = get_cohere_response(mensajes_finales, rubro_id=rubro_id, user_context=user_profile)
        if respuesta and len(respuesta) > 3:
            respuesta_final = respuesta
            fuente = "cohere"
        else:
            respuesta_final = ""
    except Exception as e:
        logging.error(f"Error Cohere: {e}", exc_info=True)
        respuesta_final = ""

    # Si no respondió, intentar backup FAQ
    if not respuesta_final:
        try:
            from services.faq_matcher_spacy import buscar_en_faq_spacy
            faq = buscar_en_faq_spacy(pregunta, rubro_id)
            if faq and hasattr(faq, 'answer'):
                respuesta_final = faq.answer
                fuente = "faq"
        except Exception as e:
            logging.warning(f"Error FAQ: {e}", exc_info=True)

    # Si no, intentar intents
    if not respuesta_final:
        try:
            from services.intent_matcher import buscar_en_intents
            intent_respuesta = buscar_en_intents(pregunta, rubro_nombre)
            if intent_respuesta:
                respuesta_final = intent_respuesta
                fuente = "intents"
        except Exception as e:
            logging.warning(f"Error intents: {e}", exc_info=True)

    # Reemplazar placeholders
    if respuesta_final:
        respuesta_final = reemplazar_placeholders(respuesta_final, user)

    # Añadir llamado a acción si pasamos umbral
    if interacciones >= 8:
        respuesta_final += "\n\n👉 ¿Querés que te pase el link para comprar o el contacto para ayudarte?"

    # Guardar en historial y DB
    if respuesta_final:
        session[HISTORIAL_SESION].append({"role": "user", "content": pregunta})
        session[HISTORIAL_SESION].append({"role": "assistant", "content": respuesta_final})
        MAX_HIST = 20
        if len(session[HISTORIAL_SESION]) > MAX_HIST:
            session[HISTORIAL_SESION] = session[HISTORIAL_SESION][-MAX_HIST:]
        session.modified = True

        if not is_demo and hasattr(user, "id") and user.id is not None:
            try:
                user.preguntas_usadas += 1
                db.session.add(Conversacion(user_id=user.id, pregunta=pregunta, respuesta=respuesta_final, fuente=fuente, rubro=rubro_nombre))
                db.session.commit()
            except Exception as e:
                logging.error(f"Error guardando DB: {e}", exc_info=True)
                db.session.rollback()

        return {"respuesta": respuesta_final, "nivel_usado": rubro_nombre, "fuente": fuente}

    # Sugerencias fallback
    sugerencias = sugerencias_por_rubro(rubro_id)
    return {
        "respuesta": "No encontré respuesta directa. Probá: " + " · ".join(f"“{s}”" for s in sugerencias),
        "fuente": "sugerencia"
    }
