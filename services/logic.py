import logging
from flask import session
from models import User, Rubro, Conversacion
from extensions import db

def responder_chatboc(pregunta, token, rubro_nombre_frontend=None):
    logging.info(f"Inicio responder_chatboc: pregunta='{pregunta}' token='{token}' rubro_frontend='{rubro_nombre_frontend}'")

    if not pregunta:
        logging.warning("Pregunta vacía recibida")
        return {"error": "Falta la pregunta"}

    # Inicializar historial en sesión si no existe
    if 'historial_sesion' not in session:
        session['historial_sesion'] = []
        logging.info("Inicializando historial_sesion en flask.session")

    is_demo = False
    user = None
    rubro_id = 1
    rubro_nombre = "general"

    # Detectar modo demo anónimo
    try:
        is_demo = token.startswith("demo-anon")
    except Exception as e:
        logging.warning(f"Token inválido: {token} | Error: {e}")

    if is_demo:
        logging.info("Modo demo anónimo detectado")
        session.setdefault("anon_preguntas", 0)
        if session["anon_preguntas"] >= 15:
            return {"respuesta": "🔒 Límite de 15 preguntas en modo demo alcanzado. Registrate para seguir.", "fuente": "sistema"}
        session["anon_preguntas"] += 1

        class AnonUser:
            nombre_empresa = "Demo Anónimo"
            plan = "demo"
            preguntas_usadas = session["anon_preguntas"]
            limite_preguntas = 15
            rubro_id = None
            link_web = ""
            telefono = ""
            direccion = ""
            horario = ""
            ubicacion = ""
        user = AnonUser()
    else:
        # Autenticación usuario real
        try:
            user = User.query.filter_by(token=token).first()
            if not user:
                logging.warning("Usuario no autenticado")
                return {"error": "Usuario no autenticado"}
            if user.preguntas_usadas >= user.limite_preguntas:
                return {"respuesta": "🔒 Límite de preguntas alcanzado en tu plan. Actualizá para más.", "fuente": "sistema"}
        except Exception as e:
            logging.error(f"Error obteniendo usuario: {e}", exc_info=True)
            return {"error": "Error interno al obtener usuario"}

    # Determinar rubro
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

    # Construir mensajes para Cohere desde historial_sesion + nueva pregunta
    mensajes_para_cohere = session['historial_sesion'][:]  # copia
    mensajes_para_cohere.append({"role": "user", "content": pregunta})
    logging.info(f"Mensajes para Cohere construidos: {len(mensajes_para_cohere)}")

    # Agregar contexto catálogo con Qdrant
    contexto_catalogo = ""
    if not is_demo and hasattr(user, "id"):
        try:
            from services.qdrant_search import buscar_catalogo_qdrant, armar_respuesta_legible
            logging.info("Buscando catálogo en Qdrant...")
            resultados_qdrant = buscar_catalogo_qdrant(user.id, pregunta, limite=5)
            logging.info(f"Resultados Qdrant: {resultados_qdrant}")
            contexto_catalogo = armar_respuesta_legible(resultados_qdrant)
        except Exception as e:
            logging.error(f"Error buscando catálogo en Qdrant: {e}", exc_info=True)

    # Preparar prompt para Cohere con instrucciones claras para guiar la venta
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

    prompt_base = (
        f"Sos Chatboc, asistente digital de ventas de {user_context['nombre_empresa']} (rubro {user_context['rubro_nombre']}).\n"
        "Tu objetivo es entender al cliente, ofrecer productos o servicios, y llevarlo en 5-6 intercambios a concretar la compra o visitar la tienda online.\n"
        "Usá lenguaje amable, profesional, y nunca digas que sos IA.\n"
        f"Datos: dirección: {user_context['direccion']}, web: {user_context['link_web']}, teléfono: {user_context['telefono']}, horario: {user_context['horario']}.\n"
    )

    if contexto_catalogo:
        prompt_base += f"Contexto catálogo productos:\n{contexto_catalogo}\n"

    prompt_base += "Respondé la conversación basándote en el historial y el contexto.\n"

    system_message = {"role": "system", "content": prompt_base}

    mensajes_finales = [system_message] + mensajes_para_cohere

    # Llamar a Cohere
    try:
        from services.cohere_ai import get_cohere_response
        logging.info(f"Enviando prompt a Cohere con longitud {len(prompt_base)} y {len(mensajes_para_cohere)} mensajes en historial")
        respuesta_final = get_cohere_response(mensajes_finales, rubro_id=rubro_id, user_context=user_context)
        logging.info(f"Respuesta recibida de Cohere: {respuesta_final[:200]}...")
    except Exception as e:
        logging.error(f"Error al llamar Cohere: {e}", exc_info=True)
        respuesta_final = ""

    # Reemplazar placeholders
    try:
        from logic import reemplazar_placeholders
        respuesta_final = reemplazar_placeholders(respuesta_final, user)
    except Exception as e:
        logging.error(f"Error reemplazando placeholders: {e}", exc_info=True)

    # Si respuesta válida, actualizar historial sesión y guardar en DB
    if respuesta_final and len(respuesta_final) > 5:
        # Actualizar historial de sesión
        session['historial_sesion'].append({"role": "user", "content": pregunta})
        session['historial_sesion'].append({"role": "assistant", "content": respuesta_final})

        MAX_HISTORIAL = 20
        if len(session['historial_sesion']) > MAX_HISTORIAL:
            session['historial_sesion'] = session['historial_sesion'][-MAX_HISTORIAL:]
        session.modified = True

        # Guardar en DB si usuario registrado
        if not is_demo and hasattr(user, "id"):
            try:
                user.preguntas_usadas += 1
                db.session.add(Conversacion(user_id=user.id, pregunta=pregunta, respuesta=respuesta_final, fuente="cohere", rubro=rubro_nombre))
                db.session.commit()
                logging.info("Conversación guardada en base de datos")
            except Exception as e:
                logging.error(f"Error guardando conversación en DB: {e}", exc_info=True)

        return {"respuesta": respuesta_final, "nivel_usado": rubro_nombre, "fuente": "cohere"}

    # Backup FAQ
    try:
        from services.faq_matcher_spacy import buscar_en_faq_spacy
        faq_match = buscar_en_faq_spacy(pregunta, rubro_id)
        if faq_match:
            if not is_demo and hasattr(user, "id"):
                user.preguntas_usadas += 1
                db.session.add(Conversacion(user_id=user.id, pregunta=pregunta, respuesta=faq_match.answer, fuente="faq", rubro=rubro_nombre))
                db.session.commit()
            logging.info("Respuesta desde FAQ")
            return {"respuesta": faq_match.answer, "nivel_usado": rubro_nombre, "fuente": "faq"}
    except Exception as e:
        logging.warning(f"Error en FAQ backup: {e}", exc_info=True)

    # Backup Intents
    try:
        from services.intent_matcher import buscar_en_intents
        intent_respuesta = buscar_en_intents(pregunta, rubro_nombre)
        if intent_respuesta:
            intent_respuesta = reemplazar_placeholders(intent_respuesta, user)
            if not is_demo and hasattr(user, "id"):
                user.preguntas_usadas += 1
                db.session.add(Conversacion(user_id=user.id, pregunta=pregunta, respuesta=intent_respuesta, fuente="intents", rubro=rubro_nombre))
                db.session.commit()
            logging.info("Respuesta desde intents")
            return {"respuesta": intent_respuesta, "nivel_usado": rubro_nombre, "fuente": "intents"}
    except Exception as e:
        logging.warning(f"Error en intents backup: {e}", exc_info=True)

    # Si no hay respuesta directa, mandar sugerencias
    try:
        from logic import sugerencias_por_rubro
        sugerencias = sugerencias_por_rubro(rubro_id)
    except Exception as e:
        logging.error(f"Error obteniendo sugerencias: {e}", exc_info=True)
        sugerencias = ["No tengo sugerencias disponibles ahora."]

    logging.info("No se encontró respuesta directa. Enviando sugerencias.")
    return {
        "respuesta": "No encontré una respuesta directa. Probá preguntando: " + " · ".join(f"“{s}”" for s in sugerencias),
        "fuente": "sugerencia"
    }
