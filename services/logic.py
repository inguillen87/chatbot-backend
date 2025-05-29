import logging
import random
from flask import session
from models import User, Rubro, Sugerencia, Conversacion
from extensions import db

def sugerencias_por_rubro(rubro_id):
    try:
        sugerencias = Sugerencia.query.filter_by(rubro_id=rubro_id).all()
        if sugerencias:
            todas = [s.texto for s in sugerencias]
            logging.info(f"Sugerencias encontradas para rubro {rubro_id}: {len(todas)}")
            return random.sample(todas, min(5, len(todas)))
        fallback = Sugerencia.query.filter_by(rubro_id=1).all()
        logging.info(f"No se encontraron sugerencias para rubro {rubro_id}, usando fallback general")
        return random.sample([s.texto for s in fallback], min(5, len(fallback))) if fallback else ["No tengo sugerencias en este momento."]
    except Exception as e:
        logging.error(f"Error buscando sugerencias: {e}", exc_info=True)
        return ["Lo siento, ocurrió un error al buscar sugerencias."]

def reemplazar_placeholders(texto, user):
    try:
        # Ejemplo básico, adaptá a tu necesidad
        texto = texto.replace("[nombreEmpresa]", getattr(user, "nombre_empresa", "tu empresa"))
        texto = texto.replace("[linkWeb]", getattr(user, "link_web", ""))
        return texto
    except Exception as e:
        logging.error(f"Error reemplazando placeholders: {e}", exc_info=True)
        return texto

def responder_chatboc(pregunta, token, rubro_nombre_frontend=None, historial=[]):
    logging.info(f"Inicio responder_chatboc con pregunta: {pregunta} | token: {token} | rubro frontend: {rubro_nombre_frontend}")

    if not pregunta:
        logging.warning("Falta la pregunta")
        return {"error": "Falta la pregunta"}

    is_demo = False
    user = None
    rubro_id = 1
    rubro_nombre = "general"

    try:
        is_demo = token.startswith("demo-anon")
    except Exception as e:
        logging.warning(f"Token inválido o no string: {token} | Error: {e}")

    if is_demo:
        logging.info("Modo demo anónimo detectado")
        session.setdefault("anon_preguntas", 0)
        if session["anon_preguntas"] >= 15:
            return {"respuesta": "🔒 Alcanzaste el límite de 15 preguntas en modo demo. Registrate gratis para seguir probando.", "fuente": "sistema"}
        session["anon_preguntas"] += 1
        class AnonUser:
            nombre_empresa = "Demo Anónimo"
            plan = "demo"
            preguntas_usadas = session["anon_preguntas"]
            limite_preguntas = 15
            rubro_id = None
        user = AnonUser()
    else:
        try:
            user = User.query.filter_by(token=token).first()
            if not user:
                logging.warning("Usuario no autenticado")
                return {"error": "Usuario no autenticado"}
            if user.preguntas_usadas >= user.limite_preguntas:
                return {"respuesta": "🔒 Alcanzaste el límite de tu plan. Actualizá para más preguntas.", "fuente": "sistema"}
        except Exception as e:
            logging.error(f"Error obteniendo usuario: {e}", exc_info=True)
            return {"error": "Error interno al obtener usuario"}

    try:
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
        logging.info(f"Usuario: {getattr(user, 'nombre_empresa', 'demo')} | Rubro: {rubro_nombre} (ID {rubro_id})")
    except Exception as e:
        logging.error(f"Error obteniendo rubro: {e}", exc_info=True)

    # Historial
    historial_chat = []
    if not is_demo and hasattr(user, "id"):
        try:
            historial_chat = Conversacion.query.filter_by(user_id=user.id).order_by(Conversacion.timestamp.desc()).limit(10).all()
            logging.info(f"Historial recuperado: {len(historial_chat)} mensajes")
        except Exception as e:
            logging.warning(f"No se pudo obtener historial: {e}")

    mensajes = []
    for conv in reversed(historial_chat):
        mensajes.append({"role": "user", "content": conv.pregunta})
        mensajes.append({"role": "assistant", "content": conv.respuesta})
    mensajes.append({"role": "user", "content": pregunta})

    # Intentar Qdrant
    resultados_qdrant = []
    contexto_catalogo = ""
    if not is_demo and hasattr(user, "id"):
        try:
            from services.qdrant_search import buscar_catalogo_qdrant, armar_respuesta_legible
            logging.info("Buscando en Qdrant...")
            resultados_qdrant = buscar_catalogo_qdrant(user.id, pregunta, limite=5)
            logging.info(f"Resultados Qdrant: {resultados_qdrant}")
            contexto_catalogo = armar_respuesta_legible(resultados_qdrant)
        except Exception as e:
            logging.error(f"Error al buscar en Qdrant: {e}", exc_info=True)

    # Intentar Cohere AI
    respuesta_final = ""
    try:
        from services.cohere_ai import get_cohere_response
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
        logging.info(f"Enviando mensaje a Cohere con prompt de largo {len(prompt)} y {len(mensajes)} mensajes de historial")

        respuesta_final = get_cohere_response(messages, rubro_id=rubro_id, user_context=user_context)
        logging.info(f"Respuesta de Cohere recibida: {respuesta_final[:200]}...")
        respuesta_final = reemplazar_placeholders(respuesta_final, user)

    except Exception as e:
        logging.error(f"Error al generar respuesta con Cohere: {e}", exc_info=True)

    # Guardar y devolver respuesta de Cohere si es válida
    if respuesta_final and len(respuesta_final) > 5:
        try:
            if not is_demo and hasattr(user, "id"):
                user.preguntas_usadas += 1
                db.session.commit()
                db.session.add(Conversacion(user_id=user.id, pregunta=pregunta, respuesta=respuesta_final, fuente="cohere", rubro=rubro_nombre))
                db.session.commit()
            logging.info("Respuesta final enviada desde Cohere")
            return {"respuesta": respuesta_final, "nivel_usado": rubro_nombre, "fuente": "cohere"}
        except Exception as e:
            logging.error(f"Error guardando conversación en DB: {e}", exc_info=True)

    # Backup: FAQ
    try:
        from services.faq_matcher_spacy import buscar_en_faq_spacy
        faq_match = buscar_en_faq_spacy(pregunta, rubro_id)
        if faq_match:
            if not is_demo and hasattr(user, "id"):
                user.preguntas_usadas += 1
                db.session.commit()
                db.session.add(Conversacion(user_id=user.id, pregunta=pregunta, respuesta=faq_match.answer, fuente="faq", rubro=rubro_nombre))
                db.session.commit()
            logging.info("Respuesta enviada desde FAQ")
            return {"respuesta": faq_match.answer, "nivel_usado": rubro_nombre, "fuente": "faq"}
    except Exception as e:
        logging.warning(f"Error buscando en FAQ: {e}", exc_info=True)

    # Backup: Intents
    try:
        from services.intent_matcher import buscar_en_intents
        intent_respuesta = buscar_en_intents(pregunta, rubro_nombre)
        if intent_respuesta:
            intent_respuesta = reemplazar_placeholders(intent_respuesta, user)
            if not is_demo and hasattr(user, "id"):
                user.preguntas_usadas += 1
                db.session.commit()
                db.session.add(Conversacion(user_id=user.id, pregunta=pregunta, respuesta=intent_respuesta, fuente="intents", rubro=rubro_nombre))
                db.session.commit()
            logging.info("Respuesta enviada desde intents")
            return {"respuesta": intent_respuesta, "nivel_usado": rubro_nombre, "fuente": "intents"}
    except Exception as e:
        logging.warning(f"Error buscando en intents: {e}", exc_info=True)

    sugerencias = sugerencias_por_rubro(rubro_id)
    logging.info("No se encontró respuesta directa, enviando sugerencias")
    return {
        "respuesta": "No encontré una respuesta directa. Probá preguntando: " + " · ".join(f"“{s}”" for s in sugerencias),
        "fuente": "sugerencia"
    }
