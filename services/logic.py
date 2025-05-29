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
            logging.info(f"Sugerencias para rubro {rubro_id}: {len(todas)}")
            return random.sample(todas, min(5, len(todas)))
        fallback = Sugerencia.query.filter_by(rubro_id=1).all()
        logging.info(f"No se encontraron sugerencias para rubro {rubro_id}, usando fallback general")
        return random.sample([s.texto for s in fallback], min(5, len(fallback))) if fallback else ["No tengo sugerencias ahora."]
    except Exception as e:
        logging.error(f"Error buscando sugerencias: {e}", exc_info=True)
        return ["Ocurrió un error buscando sugerencias."]

def reemplazar_placeholders(texto, user):
    try:
        texto = texto.replace("[nombreEmpresa]", getattr(user, "nombre_empresa", "tu empresa"))
        texto = texto.replace("[linkWeb]", getattr(user, "link_web", ""))
        return texto
    except Exception as e:
        logging.error(f"Error en placeholders: {e}", exc_info=True)
        return texto

def responder_chatboc(pregunta, token, rubro_nombre_frontend=None):
    logging.info(f"▶️ Inicio responder_chatboc: pregunta='{pregunta}', token='{token}', rubro='{rubro_nombre_frontend}'")

    if not pregunta:
        logging.warning("❌ Pregunta vacía")
        return {"error": "Falta la pregunta"}

    is_demo = False
    user = None
    rubro_id = 1
    rubro_nombre = "general"

    try:
        is_demo = token.startswith("demo-anon")
    except Exception:
        pass

    if is_demo:
        logging.info("Modo demo anónimo")
        session.setdefault("anon_preguntas", 0)
        if session["anon_preguntas"] >= 15:
            return {"respuesta": "🔒 Alcanzaste el límite de preguntas en demo. Regístrate para seguir.", "fuente": "sistema"}
        session["anon_preguntas"] += 1

        class AnonUser:
            nombre_empresa = "Demo Anónimo"
            plan = "demo"
            preguntas_usadas = session["anon_preguntas"]
            limite_preguntas = 15
            rubro_id = None
            telefono = link_web = direccion = horario = ubicacion = ""

        user = AnonUser()
    else:
        try:
            user = User.query.filter_by(token=token).first()
            if not user:
                logging.warning("Usuario no autenticado")
                return {"error": "Usuario no autenticado"}
            if user.preguntas_usadas >= user.limite_preguntas:
                return {"respuesta": "🔒 Límite de preguntas alcanzado. Actualiza tu plan.", "fuente": "sistema"}
        except Exception as e:
            logging.error(f"Error al obtener usuario: {e}", exc_info=True)
            return {"error": "Error interno al obtener usuario"}

    # Rubro
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
        logging.info(f"Usuario '{getattr(user, 'nombre_empresa', 'demo')}' usa rubro '{rubro_nombre}' (id {rubro_id})")
    except Exception as e:
        logging.error(f"Error al obtener rubro: {e}", exc_info=True)

    # Historial
    historial_chat = []
    if not is_demo and hasattr(user, "id"):
        try:
            historial_chat = Conversacion.query.filter_by(user_id=user.id).order_by(Conversacion.timestamp.desc()).limit(10).all()
            logging.info(f"Historial recuperado: {len(historial_chat)} mensajes")
        except Exception as e:
            logging.warning(f"No se pudo obtener historial: {e}", exc_info=True)

    mensajes = []
    for conv in reversed(historial_chat):
        mensajes.append({"role": "user", "content": conv.pregunta})
        mensajes.append({"role": "assistant", "content": conv.respuesta})
    mensajes.append({"role": "user", "content": pregunta})

    # Qdrant
    contexto_catalogo = ""
    try:
        if not is_demo and hasattr(user, "id"):
            from services.qdrant_search import buscar_catalogo_qdrant, armar_respuesta_legible
            logging.info("Buscando catálogo en Qdrant...")
            resultados_qdrant = buscar_catalogo_qdrant(user.id, pregunta, limite=5)
            logging.info(f"Resultados Qdrant: {resultados_qdrant}")
            contexto_catalogo = armar_respuesta_legible(resultados_qdrant)
    except Exception as e:
        logging.error(f"Error en Qdrant: {e}", exc_info=True)

    # Cohere AI
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
                f"Contexto del catálogo:\n{contexto_catalogo}\n"
                f"Sos Chatboc, agente comercial digital de {user_context['nombre_empresa']} (rubro: {user_context['rubro_nombre']}). "
                f"Tu objetivo es vender, sugerir productos, mostrar promociones, responder con info precisa y guiar la conversación hacia una acción (compra, reserva, etc). "
                f"Nunca digas que sos IA. Usa tono humano, amable, directo, y ayuda a cerrar la venta.\n"
                f"Datos: dirección {user_context['direccion']}, link {user_context['link_web']}, tel {user_context['telefono']}, horario {user_context['horario']}.\n"
                f"Respondé la conversación como vendedor profesional."
            )
        else:
            prompt = (
                f"Sos Chatboc, agente comercial digital de {user_context['nombre_empresa']} (rubro: {user_context['rubro_nombre']}). "
                f"Tu objetivo es vender, sugerir productos, mostrar promociones, responder con info precisa y guiar la conversación hacia una acción (compra, reserva, etc). "
                f"Nunca digas que sos IA. Usa tono humano, amable, directo, y ayuda a cerrar la venta.\n"
                f"Datos: dirección {user_context['direccion']}, link {user_context['link_web']}, tel {user_context['telefono']}, horario {user_context['horario']}.\n"
                f"Respondé la conversación como vendedor profesional."
            )

        messages = [{"role": "system", "content": prompt}] + mensajes
        logging.info(f"Enviando mensaje a Cohere con prompt largo {len(prompt)} y {len(mensajes)} mensajes")

        respuesta_final = get_cohere_response(messages, rubro_id=rubro_id, user_context=user_context)
        logging.info(f"Respuesta Cohere: {respuesta_final[:200]}...")
        respuesta_final = reemplazar_placeholders(respuesta_final, user)

        # Agregar botón para terminar compra
        boton_compra = '\n\n<a href="https://salvadorpatti.com/carrito" target="_blank">🛒 Terminar compra</a>'
        respuesta_final += boton_compra

    except Exception as e:
        logging.error(f"Error en Cohere: {e}", exc_info=True)

    # Guardar respuesta y actualizar preguntas usadas
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
            logging.error(f"Error guardando conversación: {e}", exc_info=True)

    # Backup FAQ
    try:
        from services.faq_matcher_spacy import buscar_en_faq_spacy
        faq_match = buscar_en_faq_spacy(pregunta, rubro_id)
        if faq_match:
            if not is_demo and hasattr(user, "id"):
                user.preguntas_usadas += 1
                db.session.commit()
                db.session.add(Conversacion(user_id=user.id, pregunta=pregunta, respuesta=faq_match.answer, fuente="faq", rubro=rubro_nombre))
                db.session.commit()
            logging.info("Respuesta desde FAQ")
            return {"respuesta": faq_match.answer, "nivel_usado": rubro_nombre, "fuente": "faq"}
    except Exception as e:
        logging.warning(f"Error FAQ: {e}", exc_info=True)

    # Backup intents
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
            logging.info("Respuesta desde intents")
            return {"respuesta": intent_respuesta, "nivel_usado": rubro_nombre, "fuente": "intents"}
    except Exception as e:
        logging.warning(f"Error intents: {e}", exc_info=True)

    # Finalmente sugerencias
    sugerencias = sugerencias_por_rubro(rubro_id)
    logging.info("No se encontró respuesta directa, enviando sugerencias")
    return {
        "respuesta": "No encontré una respuesta directa. Probá preguntando: " + " · ".join(f"“{s}”" for s in sugerencias),
        "fuente": "sugerencia"
    }
