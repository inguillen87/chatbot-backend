import logging
import random
from flask import session
from models import User, Rubro, Sugerencia, Conversacion
from extensions import db
import re # Asegúrate que re esté importado


# --- Funciones Auxiliares ---
def sugerencias_por_rubro(rubro_id: int) -> list:
    # ... (esta función se mantiene como la versión anterior que te di, es robusta) ...
    try:
        sugerencias_obj = Sugerencia.query.filter_by(rubro_id=rubro_id).all()
        if sugerencias_obj:
            todas = [s.texto for s in sugerencias_obj]
            logging.info(f"Sugerencias para rubro {rubro_id}: {len(todas)}")
            return random.sample(todas, min(5, len(todas)))
        if rubro_id != 1:
            fallback_obj = Sugerencia.query.filter_by(rubro_id=1).all()
            if fallback_obj:
                logging.info(f"No se encontraron sugerencias para rubro {rubro_id}, usando fallback general")
                return random.sample([s.texto for s in fallback_obj], min(5, len(fallback_obj)))
        return ["¿En qué más te puedo ayudar?", "Consulta nuestros productos principales.", "Háblame un poco más sobre lo que buscas."]
    except Exception as e:
        logging.error(f"Error buscando sugerencias: {e}", exc_info=True)
        return ["Disculpa, tuve un problema al buscar sugerencias en este momento."]

def formatear_numero_whatsapp_simple(telefono_str: str, codigo_pais: str = "54") -> str:
    """
    Formato MUY SIMPLIFICADO para WhatsApp: elimina no dígitos y antepone prefijo.
    Asume que el número ya es local o viene con el '9' de móvil para Argentina.
    Ej: "2611234567" -> "5492611234567"
    Ej: "+54 9 261 123-4567" -> "5492611234567"
    """
    if not telefono_str:
        return ""
    numeros = re.sub(r'\D', '', telefono_str)

    # Si ya empieza con el código de país y el 9 de móvil (ej. 549...)
    if numeros.startswith(codigo_pais + "9"):
        return numeros
    # Si empieza con el código de país pero sin el 9 (ej. 54261...)
    elif numeros.startswith(codigo_pais) and not numeros.startswith(codigo_pais + "9"):
        # Suponemos que es un fijo o un móvil al que le falta el 9 después del código de país.
        # Para WhatsApp, generalmente se necesita el 9 para móviles.
        # Esta es una heurística y podría no ser siempre correcta.
        return codigo_pais + "9" + numeros[len(codigo_pais):]
    # Si es un número local (ej. 10 dígitos como 2611234567 para Mendoza)
    elif len(numeros) == 10:
        return f"{codigo_pais}9{numeros}"
    # Si es un número más corto (podría ser un celular sin código de área como 15XXXXXXX)
    # o un formato no esperado, devolver los números limpios. El link podría no funcionar.
    # Es mejor que los números en la BD estén lo más completos posible.
    return numeros # Fallback a solo los dígitos si no cumple los patrones anteriores


def reemplazar_placeholders(texto: str, user_obj) -> str:
    if not texto:
        return ""

    placeholders_conocidos = {
        "[nombreEmpresa]": "nombre_empresa",
        "[linkWeb]": "link_web",
        "[telefono]": "telefono",
        "[direccion]": "direccion",
        "[horario]": "horario",
        "[ubicacion]": "ubicacion",
    }
    defaults_textos = {
        "nombre_empresa": "nuestra empresa",
        "link_web": "nuestro sitio web",
        "telefono": "nuestro número de contacto",
        "direccion": "nuestra dirección",
        "horario": "nuestro horario de atención",
        "ubicacion": "nuestra área de servicio",
    }

    texto_procesado = texto

    for ph_template, user_attr_name in placeholders_conocidos.items():
        valor_atributo_original = None
        if user_obj:
            valor_atributo_original = getattr(user_obj, user_attr_name, None)
        
        valor_para_texto = str(valor_atributo_original) if valor_atributo_original else defaults_textos.get(user_attr_name, "")

        if ph_template == "[telefono]":
            if valor_atributo_original:
                numero_wsp_formateado = formatear_numero_whatsapp_simple(str(valor_atributo_original))
                if numero_wsp_formateado:
                    # Se muestra el número original, y el link de WhatsApp
                    link_wsp_html = f'{str(valor_atributo_original)} (<a href="https://wa.me/{numero_wsp_formateado}" target="_blank" style="color: green; text-decoration: underline; font-weight:bold;">Contactar por WhatsApp</a>)'
                    texto_procesado = texto_procesado.replace(ph_template, link_wsp_html)
                else: # Si no se pudo formatear, solo poner el número original
                    texto_procesado = texto_procesado.replace(ph_template, str(valor_atributo_original))
            else: # No hay número, usar default
                texto_procesado = texto_procesado.replace(ph_template, defaults_textos.get(user_attr_name, ""))
        elif ph_template == "[linkWeb]":
            # El reemplazo de [linkWeb] en el texto es solo el nombre genérico.
            # El botón HTML usa el link real y se añade después.
            if valor_atributo_original:
                texto_procesado = texto_procesado.replace(ph_template, valor_para_texto) # o un texto como "nuestra tienda online"
            else:
                texto_procesado = texto_procesado.replace(ph_template, "nuestra tienda online")
        else:
            texto_procesado = texto_procesado.replace(ph_template, valor_para_texto)

    # Fallback para placeholders desconocidos [AlgoEntreCorchetes]
    def reemplazar_desconocido_callback(match):
        placeholder_interno = match.group(1)
        logging.warning(f"Placeholder desconocido encontrado y reemplazado genéricamente: [{placeholder_interno}] en texto: \"{texto[:100]}...\"") # Loguear el contexto
        if "precio" in placeholder_interno.lower() or "costo" in placeholder_interno.lower():
            return "(precio a consultar)"
        elif "link" in placeholder_interno.lower() or "url" in placeholder_interno.lower():
            link_web_general = ""
            if user_obj and hasattr(user_obj, "link_web"):
                link_web_general = getattr(user_obj, "link_web", "")
                if link_web_general and not link_web_general.startswith("http"):
                    link_web_general = "https://" + link_web_general
            return f"(visita nuestro sitio web{': ' + link_web_general if link_web_general else ''} para más detalles)"
        return "" # Por defecto, eliminar placeholders desconocidos para una UI más limpia

    texto_procesado = re.sub(r"\[([^\]\[]+)\]", reemplazar_desconocido_callback, texto_procesado)
    return texto_procesado

# --- Función Principal del Chatbot (responder_chatboc) ---
# El resto de la función responder_chatboc se mantiene igual a la última versión que te di,
# la cual ya incluye:
# - La lógica de sesión.
# - El manejo de usuarios demo y autenticados.
# - La determinación de rubro priorizando rubro_frontend.
# - La construcción de mensajes_para_llm.
# - La llamada a Qdrant (usando armar_respuesta_legible que debe estar mejorado en qdrant_search.py).
# - La construcción del prompt_sistema_texto (con las instrucciones detalladas para precios).
# - La llamada a Cohere.
# - Los fallbacks a FAQ e Intents (asegúrate que estas respuestas usen placeholders que SÍ puedas rellenar).
# - El guardado en BD.
# - La adición del botón HTML "Ir a la Tienda Online" al final si pyme_link_web existe y es válido.

# Pega aquí el resto de tu función responder_chatboc desde la última versión que te proporcioné.
# Asegúrate de que la línea donde llamas a reemplazar_placeholders use esta nueva versión.
# Ejemplo:
# if respuesta_obtenida:
#     respuesta_obtenida = reemplazar_placeholders(respuesta_obtenida, user) # LLAMA A LA VERSIÓN ACTUALIZADA
#     # ... (resto de la lógica, añadir botón, etc.)

# --- COMIENZO DE responder_chatboc (para asegurar que esté completa) ---
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
            telefono = "5492611234567" # Ejemplo de número para demo
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
                user.nombre_empresa = getattr(rubro_obj_frontend, 'nombre_empresa_asociada', rubro_obj_frontend.nombre) 
                user.rubro_id = rubro_id
                user.link_web = getattr(rubro_obj_frontend, 'link_web_asociado', "") 
                user.telefono = getattr(rubro_obj_frontend, 'telefono_asociado', "")
                user.direccion = getattr(rubro_obj_frontend, 'direccion_asociada', "") 
                user.horario = getattr(rubro_obj_frontend, 'horario_asociado', "")  
        else:
            logging.warning(f"Rubro '{rubro_nombre_frontend}' enviado por frontend no encontrado en BD.")

    if not rubro_determinado_por_frontend:
        logging.info("Rubro no determinado por frontend, usando lógica de usuario/token.")
        if hasattr(user, "rubro_id") and user.rubro_id: 
            rubro_obj_db = Rubro.query.get(user.rubro_id)
            if rubro_obj_db:
                rubro_id = rubro_obj_db.id
                rubro_nombre = rubro_obj_db.nombre.lower().strip()
    
    if not Rubro.query.get(rubro_id): 
        logging.warning(f"Rubro ID {rubro_id} inválido o no encontrado, usando rubro general por defecto.")
        rubro_id = 1 
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
            from services.qdrant_search import buscar_catalogo_qdrant, armar_respuesta_legible
            logging.info(f"Buscando catálogo en Qdrant para user_id (PYME): {user.id}...")
            resultados_qdrant = buscar_catalogo_qdrant(user.id, pregunta, limite=5)
            contexto_catalogo = armar_respuesta_legible(resultados_qdrant)
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
        "telefono": pyme_telefono, # Este se pasará a reemplazar_placeholders y se convertirá en link WSP
        "link_web": pyme_link_web,
        "direccion": pyme_direccion,
        "horario": pyme_horario,
    }
    
    numero_intercambios_previos = len(session[NOMBRE_HISTORIAL_SESION]) // 2
    prompt_sistema_texto = (
   # Dentro de la función responder_chatboc en logic.py

    # ... (user_profile_context y numero_intercambios_previos se definen antes) ...

    prompt_sistema_texto = (
        f"Sos Chatboc, un asistente comercial experto de {user_profile_context['nombre_empresa']} (del rubro: {user_profile_context['rubro_nombre']}). "
        f"Tu principal objetivo es entender rápidamente las necesidades del cliente y guiarlo hacia una compra o una visita a la tienda online ({user_profile_context['link_web'] if user_profile_context['link_web'] else 'nuestra página web'}) en los próximos 2-4 intercambios. "
        f"Ya has tenido {numero_intercambios_previos} intercambios con este cliente (revisa el historial de conversación que te proveo). "
        
        # --- NUEVAS INSTRUCCIONES PARA BREVEDAD Y DIRECTIVIDAD ---
        "Sé amable, muy proactivo, resolutivo y persuasivo. **Tus respuestas deben ser breves, directas y valiosas. Ve al grano. Evita el texto de relleno o introducciones innecesarias. Proporciona la información clave de forma concisa.** "
        # --- FIN NUEVAS INSTRUCCIONES ---
        
        "Haz preguntas claras si necesitas más información para ayudarle. "
        "Si el cliente muestra interés en un producto o servicio, intenta cerrar la venta ofreciendo añadirlo al carrito, llevarlo a la página del producto en la tienda online, o facilitando el siguiente paso de forma clara y simple. "
        "No menciones que eres una IA ni un 'asistente virtual'. Habla como un vendedor humano y entusiasta. "
        f"Si es relevante, puedes usar estos datos de la empresa: Teléfono: {user_profile_context['telefono']}, Dirección: {user_profile_context['direccion']}, Horario: {user_profile_context['horario']}. "
        
        "\nIMPORTANTE SOBRE PRODUCTOS Y PRECIOS DEL CATÁLOGO QUE TE PROVEERÉ:"
        "\n1. Cuando el cliente pregunte por un tipo de producto (ej. 'vinos malbec'), y si el catálogo recuperado contiene múltiples opciones, PRESENTA CLARAMENTE LAS OPCIONES MÁS RELEVANTES (máximo 2-3) con su 'Nombre' y 'Precio' exactos. Sé conciso. Ejemplo: 'Tenemos: Vino Malbec A a [Precio A], y Vino Malbec B Reserva a [Precio B].'"
        "\n2. Si el cliente pregunta por el precio de un producto específico y lo encuentras en el catálogo, da el 'Precio' indicado de forma directa."
        "\n3. Si el cliente pide varias unidades de un producto con precio, y el precio es numérico, calcula el total y ofréceselo directamente. Ejemplo: '3 unidades de [Producto X] serían $[Total]'."
        "\n4. Si la información del catálogo no es clara sobre un precio, o dice 'Consultar precio', indícalo brevemente y sugiere consultar en la tienda online o contactar."
        "\n5. Si la información del catálogo es extensa para un producto, resume los puntos más importantes para el cliente o enfócate en lo que preguntó. No copies grandes bloques de texto."
        "\n6. Si no hay información del catálogo relevante, responde concisamente con conocimiento general o pide más detalles."
    )
    if contexto_catalogo: # contexto_catalogo es generado por armar_respuesta_legible
        prompt_sistema_texto += f"\n\nINFORMACIÓN DEL CATÁLOGO PARA ESTA CONSULTA (usa solo lo relevante y sé breve):\n---\n{contexto_catalogo}\n---\nUsa esta información del catálogo para responder, siguiendo las instrucciones sobre productos, precios y brevedad que te di."
    else:
        prompt_sistema_texto += "\nNo encontré información específica en el catálogo para esta consulta. Intenta ayudar al cliente de forma concisa con tu conocimiento general sobre los productos/servicios del rubro, o pide más detalles."
    prompt_sistema_texto += "\n\nInicia tu respuesta directamente al cliente, continuando la conversación de forma natural y concisa."

    # ... el resto de la función responder_chatboc ...
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

    if respuesta_obtenida: # Si Cohere dio respuesta
        respuesta_procesada = reemplazar_placeholders(respuesta_obtenida, user) # Reemplazar placeholders en la respuesta de Cohere
    else: # Si Cohere NO dio respuesta, intentar backups
        logging.info("Cohere no dio respuesta. Intentando backups (FAQ, Intents)...")
        respuesta_procesada = "" # Para asegurar que entra a los if de abajo
        try:
            from services.faq_matcher_spacy import buscar_en_faq_spacy
            faq_match = buscar_en_faq_spacy(pregunta, rubro_id)
            if faq_match and hasattr(faq_match, 'answer'):
                respuesta_procesada = reemplazar_placeholders(faq_match.answer, user) # Reemplazar placeholders en respuesta de FAQ
                fuente_respuesta = "faq"
                logging.info(f"Respuesta desde FAQ: {respuesta_procesada}")
        except ImportError:
            logging.error("Módulo FAQ (spaCy) no encontrado.")
        except Exception as e:
            logging.warning(f"Error en FAQ backup: {e}", exc_info=True)

        if not respuesta_procesada: # Si FAQ tampoco dio respuesta
            try:
                from services.intent_matcher import buscar_en_intents
                intent_match_text = buscar_en_intents(pregunta, rubro_nombre)
                if intent_match_text:
                    respuesta_procesada = reemplazar_placeholders(intent_match_text, user) # Reemplazar en respuesta de Intent
                    fuente_respuesta = "intents"
                    logging.info(f"Respuesta desde Intents: {respuesta_procesada}")
            except ImportError:
                logging.error("Módulo Intent Matcher no encontrado.")
            except Exception as e:
                logging.warning(f"Error en Intents backup: {e}", exc_info=True)

    # Procesamiento final de la respuesta, guardado y añadido del botón
    if respuesta_procesada: # Si tenemos una respuesta de Cohere, FAQ, o Intents
        session[NOMBRE_HISTORIAL_SESION].append({"role": "user", "content": pregunta})
        # Guardar la respuesta YA PROCESADA CON PLACEHOLDERS en el historial de sesión
        session[NOMBRE_HISTORIAL_SESION].append({"role": "assistant", "content": respuesta_procesada})
        MAX_HISTORIAL_EN_SESION = 20
        if len(session[NOMBRE_HISTORIAL_SESION]) > MAX_HISTORIAL_EN_SESION:
            session[NOMBRE_HISTORIAL_SESION] = session[NOMBRE_HISTORIAL_SESION][-MAX_HISTORIAL_EN_SESION:]
        session.modified = True
        logging.info(f"Historial de sesión actualizado. Tamaño: {len(session[NOMBRE_HISTORIAL_SESION])}")

        if hasattr(user, "id") and user.id is not None and not is_demo :
            try:
                user.preguntas_usadas += 1
                # Guardar la respuesta YA PROCESADA en la DB
                db.session.add(Conversacion(user_id=user.id, pregunta=pregunta, respuesta=respuesta_procesada, fuente=fuente_respuesta, rubro=rubro_nombre))
                db.session.commit()
                logging.info("Conversación guardada en BD para usuario PYME.")
            except Exception as e:
                logging.error(f"Error guardando conversación en DB: {e}", exc_info=True)
                db.session.rollback()
        
        # --- Añadir Botón HTML ---
        # (pyme_link_web se define al principio de la función a partir de getattr(user, "link_web", ""))
        respuesta_final_con_boton = respuesta_procesada # Empezar con la respuesta ya procesada
        if pyme_link_web:
            link_absoluto = pyme_link_web
            if not link_absoluto.startswith("http://") and not link_absoluto.startswith("https://"):
                link_absoluto = "https://" + link_absoluto
            
            boton_html = (
                f'\n<div style="margin-top: 15px; padding-top: 10px; border-top: 1px solid #eee;">'
                f'<a href="{link_absoluto}" target="_blank" '
                f'style="display: inline-block; background-color: #007bff; color: white; padding: 10px 20px; '
                f'text-align: center; text-decoration: none; border-radius: 5px; font-size: 16px; font-weight: bold;">'
                'Ir a la Tienda Online'
                '</a></div>'
            )
            respuesta_final_con_boton += boton_html
        
        return {"respuesta": respuesta_final_con_boton, "nivel_usado": rubro_nombre, "fuente": fuente_respuesta}

    else: # Fallback final a sugerencias del sistema
        sugerencias_generadas = sugerencias_por_rubro(rubro_id)
        # Reemplazar placeholders también en el texto de sugerencias (si los hubiera, aunque no parece)
        respuesta_sugerencias_texto_base = "No encontré una respuesta directa para tu consulta. Quizás puedas intentar preguntando algo como: " + " · ".join(f"“{s}”" for s in sugerencias_generadas)
        respuesta_sugerencias_texto_procesada = reemplazar_placeholders(respuesta_sugerencias_texto_base, user)
        
        if pyme_link_web:
            link_absoluto_sugerencia = pyme_link_web
            if not link_absoluto_sugerencia.startswith("http://") and not link_absoluto_sugerencia.startswith("https://"):
                link_absoluto_sugerencia = "https://" + link_absoluto_sugerencia
            link_html_sugerencia = (
                f'\n<div style="margin-top: 10px; font-size: 0.9em;">'
                f'También puedes <a href="{link_absoluto_sugerencia}" target="_blank">visitar nuestra tienda online</a> para más información.'
                '</div>'
            )
            respuesta_sugerencias_texto_procesada += link_html_sugerencia

        logging.info("No se encontró respuesta directa. Enviando sugerencias al cliente.")
        session[NOMBRE_HISTORIAL_SESION].append({"role": "user", "content": pregunta})
        session.modified = True
        return {"respuesta": respuesta_sugerencias_texto_procesada, "fuente": "sugerencia_sistema"}