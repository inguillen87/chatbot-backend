# logic.py

import logging
import random
from flask import session
from models import User, Rubro, Sugerencia, Conversacion # Asegúrate que Sugerencia esté definido en models.py
from extensions import db

# --- Funciones Auxiliares ---
def sugerencias_por_rubro(rubro_id: int) -> list:
    try:
        sugerencias_obj = Sugerencia.query.filter_by(rubro_id=rubro_id).all()
        if sugerencias_obj:
            todas = [s.texto for s in sugerencias_obj]
            logging.info(f"Sugerencias para rubro {rubro_id}: {len(todas)}")
            return random.sample(todas, min(5, len(todas)))

        if rubro_id != 1: # Evitar recursión infinita si el rubro 1 no tiene sugerencias
            fallback_obj = Sugerencia.query.filter_by(rubro_id=1).all() # Fallback a rubro general (ID 1)
            if fallback_obj:
                logging.info(f"No se encontraron sugerencias para rubro {rubro_id}, usando fallback general")
                return random.sample([s.texto for s in fallback_obj], min(5, len(fallback_obj)))

        return ["¿En qué más te puedo ayudar?", "Consulta nuestros productos principales.", "Háblame un poco más sobre lo que buscas."]
    except Exception as e:
        logging.error(f"Error buscando sugerencias: {e}", exc_info=True)
        return ["Disculpa, tuve un problema al buscar sugerencias en este momento."]

def reemplazar_placeholders(texto: str, user_obj) -> str:
    if user_obj is None:
        empresa = "la empresa"
        link = ""
    else:
        empresa = getattr(user_obj, "nombre_empresa", "la empresa")
        link = getattr(user_obj, "link_web", "")

    try:
        texto_reemplazado = texto.replace("[nombreEmpresa]", empresa)
        if link:
            texto_reemplazado = texto_reemplazado.replace("[linkWeb]", link)
        else:
            texto_reemplazado = texto_reemplazado.replace("[linkWeb]", "nuestra tienda online")
        return texto_reemplazado
    except Exception as e:
        logging.error(f"Error en reemplazando placeholders: {e}", exc_info=True)
        return texto

# --- Función Principal del Chatbot ---
def responder_chatboc(pregunta: str, token: str, rubro_nombre_frontend: str = None):
    logging.info(f"▶️ Inicio responder_chatboc: pregunta='{pregunta}' token='{token}' rubro_frontend='{rubro_nombre_frontend}'")

    if not pregunta:
        logging.warning("Pregunta vacía recibida")
        return {"error": "Falta la pregunta"}

    NOMBRE_HISTORIAL_SESION = 'historial_chat_cliente'
    if NOMBRE_HISTORIAL_SESION not in session:
        session[NOMBRE_HISTORIAL_SESION] = []
        logging.info(f"Inicializando '{NOMBRE_HISTORIAL_SESION}' en flask.session")

    is_demo = False
    user = None
    rubro_id = 1
    rubro_nombre = "general"

    try:
        is_demo = token is not None and token.startswith("demo-anon")
    except Exception:
        pass

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
            link_web = "tu-tienda-online.com"
            telefono = "123-456-7890"
            direccion = "Calle Falsa 123"
            horario = "Lunes a Viernes de 9 a 18hs"
            ubicacion = ""
            id = None
        user = AnonUser()
    else:
        if token:
            try:
                db_user = User.query.filter_by(token=token).first()
                if not db_user:
                    logging.warning(f"Usuario no autenticado o token inválido: {token}")
                    return {"error": "Usuario no autenticado"}
                if db_user.preguntas_usadas >= db_user.limite_preguntas:
                    return {"respuesta": "🔒 Límite de preguntas alcanzado en tu plan. Actualizá para más.", "fuente": "sistema"}
                user = db_user
            except Exception as e:
                logging.error(f"Error obteniendo usuario de BD: {e}", exc_info=True)
                return {"error": "Error interno al obtener usuario"}
        else:
            logging.info("Sin token y no es demo-anon. Tratando como anónimo general.")
            class GenericAnonUser:
                nombre_empresa = "la empresa"
                plan = "anonimo"
                rubro_id = None
                link_web = ""
                telefono = ""
                direccion = ""
                horario = ""
                id = None
            user = GenericAnonUser()

    # Determinar rubro (Prioridad para rubro_frontend si se proporciona y es válido)
    rubro_determinado_por_frontend = False
    if rubro_nombre_frontend:
        logging.info(f"Intentando determinar rubro por frontend: '{rubro_nombre_frontend}'")
        rubro_obj_frontend = Rubro.query.filter(db.func.lower(Rubro.nombre) == rubro_nombre_frontend.lower().strip()).first()
        if rubro_obj_frontend:
            rubro_id = rubro_obj_frontend.id
            rubro_nombre = rubro_obj_frontend.nombre.lower().strip()
            rubro_determinado_por_frontend = True
            logging.info(f"Rubro determinado por frontend: {rubro_nombre} (ID: {rubro_id})")
            if isinstance(user, GenericAnonUser):
                user.nombre_empresa = rubro_obj_frontend.nombre # Asignar nombre de empresa si es genérico
                user.rubro_id = rubro_id
                # Aquí podrías intentar cargar link_web, telefono, etc., si están asociados al objeto Rubro
                # y 'user' (GenericAnonUser) no los tiene.
                # Ejemplo: user.link_web = getattr(rubro_obj_frontend, 'default_link_web', "")
        else:
            logging.warning(f"Rubro '{rubro_nombre_frontend}' enviado por frontend no encontrado en BD.")

    if not rubro_determinado_por_frontend:
        logging.info("Rubro no determinado por frontend, usando lógica de usuario/token.")
        if hasattr(user, "rubro_id") and user.rubro_id: # Si el usuario (PYME o Demo con rubro_id) tiene un rubro
            rubro_obj_db = Rubro.query.get(user.rubro_id)
            if rubro_obj_db:
                rubro_id = rubro_obj_db.id
                rubro_nombre = rubro_obj_db.nombre.lower().strip()
        # Si no, se mantienen los defaults (rubro_id=1, rubro_nombre="general")

    # Asegurar que rubro_nombre y rubro_id siempre tengan un valor, default a general si es necesario.
    if not Rubro.query.get(rubro_id): # Si el rubro_id final no es válido por alguna razón
        logging.warning(f"Rubro ID {rubro_id} inválido o no encontrado, usando rubro general por defecto.")
        rubro_id = 1 # ID del rubro "general"
        rubro_general_obj = Rubro.query.get(rubro_id)
        rubro_nombre = rubro_general_obj.nombre.lower().strip() if rubro_general_obj else "general"
    
    logging.info(f"Contexto PYME final para esta solicitud: Empresa: {getattr(user, 'nombre_empresa', 'N/A')} | Rubro: {rubro_nombre} (ID {rubro_id})")

    MAX_MENSAJES_HISTORIAL_PARA_LLM = 10
    historial_actual_cliente = session[NOMBRE_HISTORIAL_SESION][:]
    mensajes_para_llm = []
    if len(historial_actual_cliente) > MAX_MENSAJES_HISTORIAL_PARA_LLM:
        mensajes_para_llm = historial_actual_cliente[-MAX_MENSAJES_HISTORIAL_PARA_LLM:]
    else:
        mensajes_para_llm = historial_actual_cliente
    mensajes_para_llm.append({"role": "user", "content": pregunta})

    contexto_catalogo = ""
    if hasattr(user, "id") and user.id is not None:
        try:
            from services.qdrant_search import buscar_catalogo_qdrant, armar_respuesta_legible #
            logging.info(f"Buscando catálogo en Qdrant para user_id (PYME): {user.id}...")
            resultados_qdrant = buscar_catalogo_qdrant(user.id, pregunta, limite=5) # Aumentado a 5 para más contexto de producto
            contexto_catalogo = armar_respuesta_legible(resultados_qdrant) #
            if contexto_catalogo:
                 logging.info(f"Contexto Qdrant (primeros 100 chars): {contexto_catalogo[:100]}")
        except ImportError:
            logging.error("Módulo Qdrant (services.qdrant_search) no encontrado.")
        except Exception as e:
            logging.error(f"Error buscando catálogo en Qdrant: {e}", exc_info=True)

    pyme_nombre_empresa = getattr(user, "nombre_empresa", "la tienda")
    pyme_link_web = getattr(user, "link_web", "")
    pyme_telefono = getattr(user, "telefono", "nuestro teléfono de contacto")
    pyme_direccion = getattr(user, "direccion", "nuestra dirección")
    pyme_horario = getattr(user, "horario", "nuestro horario de atención")

    user_profile_context = {
        "nombre_empresa": pyme_nombre_empresa,
        "rubro_nombre": rubro_nombre,
        "telefono": pyme_telefono,
        "link_web": pyme_link_web,
        "direccion": pyme_direccion,
        "horario": pyme_horario,
    }

    numero_intercambios_previos = len(session[NOMBRE_HISTORIAL_SESION]) // 2
    prompt_sistema_texto = (
        f"Sos Chatboc, un asistente comercial experto de {user_profile_context['nombre_empresa']} (del rubro: {user_profile_context['rubro_nombre']}). "
        f"Tu principal objetivo es entender rápidamente las necesidades del cliente y guiarlo hacia una compra o una visita a la tienda online ({user_profile_context['link_web'] if user_profile_context['link_web'] else 'nuestra página web'}) en los próximos 2-4 intercambios. "
        f"Ya has tenido {numero_intercambios_previos} intercambios con este cliente (revisa el historial de conversación que te proveo). "
        "Sé amable, muy proactivo, resolutivo y persuasivo. Haz preguntas claras si necesitas más información para ayudarle. "
        "Si el cliente muestra interés en un producto o servicio, intenta cerrar la venta ofreciendo añadirlo al carrito, llevarlo a la página del producto en la tienda online, o facilitando el siguiente paso. "
        "No menciones que eres una IA ni un 'asistente virtual'. Habla como un vendedor humano y entusiasta. "
        f"Si es relevante, puedes usar estos datos de la empresa: Teléfono: {user_profile_context['telefono']}, Dirección: {user_profile_context['direccion']}, Horario: {user_profile_context['horario']}. "
        "\nIMPORTANTE SOBRE PRODUCTOS Y PRECIOS DEL CATÁLOGO QUE TE PROVEERÉ:"
        "\n1. Cuando el cliente pregunte por un tipo de producto (ej. 'vinos malbec', 'medias talle L'), y si el catálogo recuperado contiene múltiples opciones, PRESENTA CLARAMENTE AL MENOS 2-3 OPCIONES relevantes con su 'Nombre' y 'Precio' exactos tal como aparecen en la información del catálogo. Ejemplo: 'Claro, tenemos estos Malbecs: [Nombre Malbec A] a [Precio A], [Nombre Malbec B Reserva] a [Precio B].'"
        "\n2. Si el cliente pregunta por el precio de un producto específico y lo encuentras en el catálogo, da el 'Precio' indicado."
        "\n3. Si el cliente pide varias unidades de un producto con precio, y el precio es numérico, calcula el total y ofréceselo (ej. '3 unidades de [Producto X] a $[Precio Y] serían $[Total]')."
        "\n4. Si la información del catálogo no es clara sobre un precio para un producto específico que el cliente menciona, o si el precio dice 'Consultar precio', indica que pueden consultarlo en la tienda online o que te pidan más detalles para verificarlo."
        "\n5. Si no hay información del catálogo, o no es relevante para la pregunta del cliente, responde con conocimiento general o pide más detalles."
    )
    if contexto_catalogo: # contexto_catalogo es generado por armar_respuesta_legible
        prompt_sistema_texto += f"\n\nINFORMACIÓN DEL CATÁLOGO PARA ESTA CONSULTA:\n---\n{contexto_catalogo}\n---\nUsa esta información del catálogo para responder, siguiendo las instrucciones sobre productos y precios que te di."
    else:
        prompt_sistema_texto += "\nNo encontré información específica en el catálogo para esta consulta, intenta ayudar al cliente con tu conocimiento general sobre los productos/servicios del rubro y la empresa, o pide más detalles."
    prompt_sistema_texto += "\n\nInicia tu respuesta directamente al cliente, continuando la conversación de forma natural."

    if contexto_catalogo:
       prompt_sistema_texto += f"\n\n{contexto_catalogo}\nUsa la información del catálogo anterior para responder y ofrecer productos específicos, prestando especial atención a listar nombres y precios correctamente como te indiqué."
    else:
        prompt_sistema_texto += "\nNo encontré información específica en el catálogo para esta consulta, pero intenta ayudar al cliente con tu conocimiento general sobre los productos/servicios del rubro y la empresa."
    prompt_sistema_texto += "\n\nInicia tu respuesta directamente al cliente, continuando la conversación de forma natural."

    mensajes_finales_para_llm = [{"role": "system", "content": prompt_sistema_texto}] + mensajes_para_llm

    respuesta_obtenida = ""
    fuente_respuesta = "desconocida"

    try:
        from services.cohere_ai import get_cohere_response
        llm_response_text = get_cohere_response(mensajes_finales_para_llm, rubro_id=rubro_id, user_context=user_profile_context)
        if llm_response_text and len(llm_response_text) > 3:
            respuesta_obtenida = llm_response_text
            fuente_respuesta = "cohere"
            logging.info(f"Respuesta de Cohere (primeros 200 chars): {respuesta_obtenida[:200]}...")
        else:
            logging.warning("Respuesta de Cohere vacía o muy corta.")
    except ImportError:
        logging.error("Módulo Cohere (services.cohere_ai.get_cohere_response) no encontrado.")
    except Exception as e:
        logging.error(f"Error al llamar a Cohere: {e}", exc_info=True)

    if respuesta_obtenida:
        respuesta_obtenida = reemplazar_placeholders(respuesta_obtenida, user)

    if not respuesta_obtenida:
        logging.info("Cohere no dio respuesta. Intentando backups (FAQ, Intents)...")
        try:
            from services.faq_matcher_spacy import buscar_en_faq_spacy #
            faq_match = buscar_en_faq_spacy(pregunta, rubro_id) #
            if faq_match and hasattr(faq_match, 'answer'):
                respuesta_obtenida = faq_match.answer
                fuente_respuesta = "faq"
                respuesta_obtenida = reemplazar_placeholders(respuesta_obtenida, user)
                logging.info(f"Respuesta desde FAQ: {respuesta_obtenida}")
        except ImportError:
            logging.error("Módulo FAQ (spaCy) no encontrado.")
        except Exception as e:
            logging.warning(f"Error en FAQ backup: {e}", exc_info=True)

    if not respuesta_obtenida:
        try:
            from services.intent_matcher import buscar_en_intents #
            intent_match_text = buscar_en_intents(pregunta, rubro_nombre) #
            if intent_match_text:
                respuesta_obtenida = intent_match_text
                fuente_respuesta = "intents"
                respuesta_obtenida = reemplazar_placeholders(respuesta_obtenida, user)
                logging.info(f"Respuesta desde Intents: {respuesta_obtenida}")
        except ImportError:
            logging.error("Módulo Intent Matcher no encontrado.")
        except Exception as e:
            logging.warning(f"Error en Intents backup: {e}", exc_info=True)

    # Procesamiento final de la respuesta, guardado y añadido del botón
    if respuesta_obtenida:
        session[NOMBRE_HISTORIAL_SESION].append({"role": "user", "content": pregunta})
        session[NOMBRE_HISTORIAL_SESION].append({"role": "assistant", "content": respuesta_obtenida}) # Guardar respuesta original ANTES de añadir botón
        MAX_HISTORIAL_EN_SESION = 20
        if len(session[NOMBRE_HISTORIAL_SESION]) > MAX_HISTORIAL_EN_SESION:
            session[NOMBRE_HISTORIAL_SESION] = session[NOMBRE_HISTORIAL_SESION][-MAX_HISTORIAL_EN_SESION:]
        session.modified = True
        logging.info(f"Historial de sesión actualizado. Tamaño: {len(session[NOMBRE_HISTORIAL_SESION])}")

        if hasattr(user, "id") and user.id is not None and not is_demo :
            try:
                user.preguntas_usadas += 1 # Corregido para instanciar correctamente
                db.session.add(Conversacion(user_id=user.id, pregunta=pregunta, respuesta=respuesta_obtenida, fuente=fuente_respuesta, rubro=rubro_nombre))
                db.session.commit()
                logging.info("Conversación guardada en BD para usuario PYME.")
            except Exception as e:
                logging.error(f"Error guardando conversación en DB: {e}", exc_info=True)
                db.session.rollback()
        
        # --- Añadir Botón HTML ---
        if pyme_link_web:
            boton_html = (
                f'\n<div style="margin-top: 15px; padding-top: 10px; border-top: 1px solid #eee;">'
                f'<a href="{pyme_link_web}" target="_blank" '
                f'style="display: inline-block; background-color: #007bff; color: white; padding: 10px 20px; '
                f'text-align: center; text-decoration: none; border-radius: 5px; font-size: 16px; font-weight: bold;">'
                'Ir a la Tienda Online'
                '</a></div>'
            )
            respuesta_obtenida_con_boton = respuesta_obtenida + boton_html
            return {"respuesta": respuesta_obtenida_con_boton, "nivel_usado": rubro_nombre, "fuente": fuente_respuesta}
        else: # Si no hay link_web, devolver la respuesta sin botón
            return {"respuesta": respuesta_obtenida, "nivel_usado": rubro_nombre, "fuente": fuente_respuesta}

    else: # Fallback final a sugerencias del sistema
        sugerencias_generadas = sugerencias_por_rubro(rubro_id)
        respuesta_sugerencias_texto = "No encontré una respuesta directa para tu consulta. Quizás puedas intentar preguntando algo como: " + " · ".join(f"“{s}”" for s in sugerencias_generadas)
        
        # Añadir link genérico a la web si existe, incluso en sugerencias
        if pyme_link_web:
            link_html_sugerencia = (
                f'\n<div style="margin-top: 10px; font-size: 0.9em;">'
                f'También puedes <a href="{pyme_link_web}" target="_blank">visitar nuestra tienda online</a> para más información.'
                '</div>'
            )
            respuesta_sugerencias_texto += link_html_sugerencia

        logging.info("No se encontró respuesta directa. Enviando sugerencias al cliente.")
        session[NOMBRE_HISTORIAL_SESION].append({"role": "user", "content": pregunta})
        session.modified = True
        return {"respuesta": respuesta_sugerencias_texto, "fuente": "sugerencia_sistema"}