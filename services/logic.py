# logic.py

import logging
import random # Necesario para sugerencias_por_rubro
from flask import session
from models import User, Rubro, Sugerencia, Conversacion # Asumo que Sugerencia es necesaria para sugerencias_por_rubro
from extensions import db

# Si estas funciones están en este mismo archivo, no necesitas "from logic import ..."
# Simplemente llámalas directamente.

def sugerencias_por_rubro(rubro_id): # Esta función estaba en tu código original del primer mensaje
    try:
        sugerencias_obj = Sugerencia.query.filter_by(rubro_id=rubro_id).all()
        if sugerencias_obj:
            todas = [s.texto for s in sugerencias_obj]
            logging.info(f"Sugerencias para rubro {rubro_id}: {len(todas)}")
            return random.sample(todas, min(5, len(todas)))
        
        # Fallback a rubro general si no hay específicas y no es ya el general
        if rubro_id != 1:
            fallback_obj = Sugerencia.query.filter_by(rubro_id=1).all()
            if fallback_obj:
                logging.info(f"No se encontraron sugerencias para rubro {rubro_id}, usando fallback general")
                return random.sample([s.texto for s in fallback_obj], min(5, len(fallback_obj)))
        
        return ["Consulta nuestros productos.", "Háblame más de lo que buscas."] # Fallback muy genérico
    except Exception as e:
        logging.error(f"Error buscando sugerencias: {e}", exc_info=True)
        return ["Disculpa, tuve un problema al buscar sugerencias."]

def reemplazar_placeholders(texto, user_obj): # user_obj puede ser User o AnonUser
    # Asegurarse de que user_obj no sea None para evitar errores con getattr
    if user_obj is None:
        # Fallback para cuando no hay objeto user (ej. error muy temprano)
        # O considera si esto debería generar un error o usar valores por defecto muy genéricos.
        empresa = "la empresa"
        link = ""
    else:
        empresa = getattr(user_obj, "nombre_empresa", "la empresa")
        link = getattr(user_obj, "link_web", "")

    try:
        texto = texto.replace("[nombreEmpresa]", empresa)
        texto = texto.replace("[linkWeb]", link)
        # Puedes añadir más placeholders aquí si los necesitas
        return texto
    except Exception as e:
        logging.error(f"Error en placeholders: {e}", exc_info=True)
        return texto


def responder_chatboc(pregunta, token, rubro_nombre_frontend=None):
    logging.info(f"▶️ Inicio responder_chatboc: pregunta='{pregunta}' token='{token}' rubro_frontend='{rubro_nombre_frontend}'")

    if not pregunta:
        logging.warning("Pregunta vacía recibida")
        return {"error": "Falta la pregunta"}

    # Usar un nombre de variable de sesión específico para el historial de chat del cliente
    NOMBRE_HISTORIAL_SESION = 'historial_chat_cliente'
    if NOMBRE_HISTORIAL_SESION not in session:
        session[NOMBRE_HISTORIAL_SESION] = []
        logging.info(f"Inicializando '{NOMBRE_HISTORIAL_SESION}' en flask.session")

    is_demo = False
    user = None # Este será el objeto User de la PYME o AnonUser
    rubro_id = 1
    rubro_nombre = "general"

    try:
        is_demo = token.startswith("demo-anon")
    except Exception as e:
        logging.warning(f"Token inválido o ausente: {token} | Error: {e}")
        # Considerar si para tokens no-demo pero inválidos se debería abortar o tratar como anónimo/general
        # Por ahora, el flujo original parece que continuaría como no-demo y fallaría en la query de User.

    if is_demo:
        logging.info("Modo demo anónimo detectado")
        session.setdefault("anon_preguntas", 0)
        if session["anon_preguntas"] >= 15:
            return {"respuesta": "🔒 Límite de 15 preguntas en modo demo alcanzado. Registrate para seguir.", "fuente": "sistema"}
        session["anon_preguntas"] += 1

        class AnonUser: # Definición de AnonUser como en tu código
            nombre_empresa = "Demo Anónimo"
            plan = "demo"
            preguntas_usadas = session["anon_preguntas"]
            limite_preguntas = 15
            rubro_id = None # O un ID de rubro demo general si lo tienes
            link_web = "tu-tienda-online.com" # Link genérico para demo
            telefono = "123-456-7890"
            direccion = "Calle Falsa 123"
            horario = "Lunes a Viernes de 9 a 18hs"
            ubicacion = "" # Podría ser un mapa genérico o nada
            id = None # Importante para no intentar operaciones de BD
        user = AnonUser()
    else:
        try:
            db_user = User.query.filter_by(token=token).first() # Renombrado a db_user para evitar confusión con 'user'
            if not db_user:
                logging.warning(f"Usuario no autenticado o token inválido: {token}")
                # Decidir si devolver error o tratar como un usuario genérico no demo
                # Por ahora, mantenemos el error para proteger endpoints no-demo.
                return {"error": "Usuario no autenticado"}
            if db_user.preguntas_usadas >= db_user.limite_preguntas:
                return {"respuesta": "🔒 Límite de preguntas alcanzado en tu plan. Actualizá para más.", "fuente": "sistema"}
            user = db_user # Ahora user es el objeto User de la BD
        except Exception as e:
            logging.error(f"Error obteniendo usuario de BD: {e}", exc_info=True)
            return {"error": "Error interno al obtener usuario"}

    # Determinar rubro (usando el objeto 'user' que puede ser AnonUser o User de BD)
    try:
        if hasattr(user, "rubro_id") and user.rubro_id: # Chequea si user (AnonUser o User) tiene rubro_id
            rubro_obj_db = Rubro.query.get(user.rubro_id)
            if rubro_obj_db:
                rubro_id = rubro_obj_db.id
                rubro_nombre = rubro_obj_db.nombre.lower().strip()
        elif rubro_nombre_frontend: # Si el frontend envía un rubro (ej. para usuarios anónimos no-demo)
            rubro_obj_db = Rubro.query.filter(db.func.lower(Rubro.nombre) == rubro_nombre_frontend.lower().strip()).first()
            if rubro_obj_db:
                rubro_id = rubro_obj_db.id
                rubro_nombre = rubro_obj_db.nombre.lower().strip()
        logging.info(f"Empresa: {getattr(user, 'nombre_empresa', 'N/A')} | Rubro: {rubro_nombre} (ID {rubro_id})")
    except Exception as e:
        logging.error(f"Error obteniendo rubro: {e}", exc_info=True)
        # Se mantiene el rubro general por defecto
    
    # Construir mensajes para Cohere desde el historial de sesión + nueva pregunta
    # Limitar el número de mensajes del historial enviados a Cohere
    MAX_MENSAJES_HISTORIAL_PARA_LLM = 10 # 5 intercambios (user + assistant)
    
    historial_actual_cliente = session[NOMBRE_HISTORIAL_SESION][:] # Copia
    mensajes_para_llm = []
    if len(historial_actual_cliente) > MAX_MENSAJES_HISTORIAL_PARA_LLM:
        mensajes_para_llm = historial_actual_cliente[-MAX_MENSAJES_HISTORIAL_PARA_LLM:]
    else:
        mensajes_para_llm = historial_actual_cliente
    
    mensajes_para_llm.append({"role": "user", "content": pregunta})
    logging.info(f"Mensajes para Cohere (incluyendo actual): {len(mensajes_para_llm)}")

    contexto_catalogo = ""
    # La búsqueda en Qdrant debe usar user.id (el ID de la PYME, no del cliente final)
    # Para AnonUser, user.id es None, así que no buscará.
    if not is_demo and hasattr(user, "id") and user.id is not None:
        try:
            from services.qdrant_search import buscar_catalogo_qdrant, armar_respuesta_legible #
            logging.info(f"Buscando catálogo en Qdrant para user_id: {user.id}...")
            resultados_qdrant = buscar_catalogo_qdrant(user.id, pregunta, limite=5) #
            contexto_catalogo = armar_respuesta_legible(resultados_qdrant) #
            if contexto_catalogo:
                 logging.info(f"Contexto de catálogo Qdrant (primeros 100 chars): {contexto_catalogo[:100]}")
        except ImportError:
            logging.error("Módulo Qdrant no encontrado. Asegúrate de que services.qdrant_search esté disponible.")
        except Exception as e:
            logging.error(f"Error buscando catálogo en Qdrant: {e}", exc_info=True)

    user_profile_context = { # Información de la PYME para el LLM
        "nombre_empresa": getattr(user, "nombre_empresa", "la empresa"),
        "rubro_nombre": rubro_nombre,
        "telefono": getattr(user, "telefono", "no disponible"),
        "link_web": getattr(user, "link_web", "no disponible"),
        "direccion": getattr(user, "direccion", "no disponible"),
        "horario": getattr(user, "horario", "no disponible"),
    }

    # Ingeniería del Prompt del Sistema para Cohere
    # Contar intercambios (1 pregunta de usuario + 1 respuesta de asistente = 1 intercambio)
    numero_intercambios_previos = len(session[NOMBRE_HISTORIAL_SESION]) // 2

    prompt_sistema_texto = (
        f"Sos Chatboc, un asistente comercial experto de {user_profile_context['nombre_empresa']} (del rubro: {user_profile_context['rubro_nombre']}). "
        f"Tu principal objetivo es entender rápidamente las necesidades del cliente y guiarlo hacia una compra o una visita a la tienda online ({user_profile_context['link_web']}) en los próximos 2-4 intercambios. "
        f"Ya has tenido {numero_intercambios_previos} intercambios con este cliente (revisa el historial de conversación que te proveo). "
        "Sé amable, muy proactivo, resolutivo y persuasivo. Haz preguntas claras si necesitas más información para ayudarle. "
        "Si el cliente muestra interés en un producto o servicio, intenta cerrar la venta ofreciendo añadirlo al carrito, llevarlo a la página del producto en la tienda online, o facilitando el siguiente paso. "
        "No menciones que eres una IA ni un 'asistente virtual'. Habla como un vendedor humano y entusiasta. "
        f"Si es relevante, puedes usar estos datos de la empresa: Teléfono: {user_profile_context['telefono']}, Dirección: {user_profile_context['direccion']}, Horario: {user_profile_context['horario']}. "
    )

    if contexto_catalogo:
        prompt_sistema_texto += f"\n\nBasándote en la pregunta del cliente, aquí tienes información relevante de nuestro catálogo que podría ser útil:\n---\n{contexto_catalogo}\n---\nUsa esta información para responder y ofrecer productos específicos. Si hay precios, menciónalos."
    else:
        prompt_sistema_texto += "\nNo encontré información específica en el catálogo para esta consulta, pero intenta ayudar al cliente con tu conocimiento general sobre los productos/servicios del rubro y la empresa."

    prompt_sistema_texto += "\n\nInicia tu respuesta directamente al cliente, continuando la conversación de forma natural."

    mensajes_finales_para_llm = [{"role": "system", "content": prompt_sistema_texto}] + mensajes_para_llm
    
    respuesta_obtenida = ""
    fuente_respuesta = "desconocida"

    # Llamada a Cohere (LLM Principal)
    try:
        from services.cohere_ai import get_cohere_response # (Asumo que esta es la función que llama a la API de Cohere para generar chat)
        logging.info(f"Enviando {len(mensajes_finales_para_llm)} mensajes a Cohere. Prompt sistema longitud: {len(prompt_sistema_texto)}. Historial para LLM: {len(mensajes_para_llm)-1} mensajes.")
        
        # La función get_cohere_response debería tomar la lista de mensajes.
        # El user_context aquí podría ser para pasar datos adicionales a la función Cohere si los necesita internamente,
        # pero el prompt principal ya tiene la info de la PYME.
        llm_response_text = get_cohere_response(mensajes_finales_para_llm, rubro_id=rubro_id, user_context=user_profile_context)
        
        if llm_response_text and len(llm_response_text) > 3: # Umbral mínimo para una respuesta válida
            respuesta_obtenida = llm_response_text
            fuente_respuesta = "cohere"
            logging.info(f"Respuesta recibida de Cohere (primeros 200 chars): {respuesta_obtenida[:200]}...")
        else:
            logging.warning("Respuesta de Cohere vacía o muy corta.")
            respuesta_obtenida = "" # Asegurar que esté vacía si no es válida

    except ImportError:
        logging.error("Módulo Cohere no encontrado. Asegúrate de que services.cohere_ai y la función get_cohere_response estén disponibles.")
        respuesta_obtenida = "" # Fallback si no se puede importar
    except Exception as e:
        logging.error(f"Error al llamar a Cohere: {e}", exc_info=True)
        respuesta_obtenida = "" # Fallback en caso de error

    # Aplicar reemplazo de placeholders a la respuesta obtenida (si la hay)
    if respuesta_obtenida:
        respuesta_obtenida = reemplazar_placeholders(respuesta_obtenida, user)
        # Considerar añadir el botón de compra aquí si es pertinente
        # Ejemplo: if user_profile_context['link_web'] and ("tienda online" in respuesta_obtenida.lower() or "comprar ahora" in respuesta_obtenida.lower()):
        # respuesta_obtenida += f'\n\n<a href="{user_profile_context["link_web"]}" target="_blank">🛒 Visita nuestra tienda</a>'


    # Si Cohere falló o no dio una respuesta válida, intentar backups
    if not respuesta_obtenida:
        logging.info("Cohere no dio respuesta válida. Intentando backups (FAQ, Intents)...")
        # Backup FAQ
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

    if not respuesta_obtenida: # Si aún no hay respuesta, intentar Intents
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


    # Procesamiento final de la respuesta y guardado
    if respuesta_obtenida:
        # Actualizar historial de sesión del cliente con la pregunta y la respuesta final
        session[NOMBRE_HISTORIAL_SESION].append({"role": "user", "content": pregunta})
        session[NOMBRE_HISTORIAL_SESION].append({"role": "assistant", "content": respuesta_obtenida})

        # Limitar tamaño del historial en sesión
        MAX_HISTORIAL_EN_SESION = 20 # (10 intercambios)
        if len(session[NOMBRE_HISTORIAL_SESION]) > MAX_HISTORIAL_EN_SESION:
            session[NOMBRE_HISTORIAL_SESION] = session[NOMBRE_HISTORIAL_SESION][-MAX_HISTORIAL_EN_SESION:]
        session.modified = True
        logging.info(f"Historial de sesión actualizado. Tamaño actual: {len(session[NOMBRE_HISTORIAL_SESION])}")

        # Guardar en DB si es un usuario PYME autenticado (no demo, no anónimo)
        if not is_demo and hasattr(user, "id") and user.id is not None:
            try:
                user.preguntas_usadas += 1 # Incrementar contador
                db.session.add(Conversacion(user_id=user.id, pregunta=pregunta, respuesta=respuesta_obtenida, fuente=fuente_respuesta, rubro=rubro_nombre))
                db.session.commit()
                logging.info("Conversación guardada en base de datos para usuario PYME.")
            except Exception as e:
                logging.error(f"Error guardando conversación en DB: {e}", exc_info=True)
                db.session.rollback() # Importante hacer rollback en caso de error de guardado

        return {"respuesta": respuesta_obtenida, "nivel_usado": rubro_nombre, "fuente": fuente_respuesta}
    else:
        # Si después de Cohere y todos los backups no hay respuesta, enviar sugerencias
        sugerencias_generadas = sugerencias_por_rubro(rubro_id)
        respuesta_sugerencias = "No encontré una respuesta directa para tu consulta. Quizás puedas intentar preguntando algo como: " + " · ".join(f"“{s}”" for s in sugerencias_generadas)
        
        # No guardamos esta respuesta de "no entendí" en el historial del LLM como una respuesta de 'assistant',
        # pero sí podrías querer guardarla en la sesión del cliente si quieres rastrear cuándo ocurren.
        # Por ahora, solo la devolvemos.
        logging.info("No se encontró respuesta directa. Enviando sugerencias al cliente.")
        return {"respuesta": respuesta_sugerencias, "fuente": "sugerencia_sistema"}