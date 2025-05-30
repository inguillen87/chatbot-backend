# logic.py

import logging
import random
from flask import session
from models import User, Rubro, Sugerencia, Conversacion # Asegúrate que Sugerencia esté definido en models.py
from extensions import db
import re # Asegúrate que re esté importado
import json # Necesario para parsear horario_json

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

def formatear_numero_whatsapp_simple(telefono_str: str, codigo_pais: str = "54") -> str:
    """
    Formato MUY SIMPLIFICADO para WhatsApp: elimina no dígitos y antepone prefijo.
    Asume que el número ya es local o viene con el '9' de móvil para Argentina.
    Ej: "2611234567" -> "5492611234567"
    Ej: "+54 9 261 123-4567" -> "5492611234567"
    """
    if not telefono_str:
        return ""
    numeros = re.sub(r'\D', '', telefono_str) # Eliminar todo lo que no sea dígito

    # Caso 1: Ya tiene el formato internacional de móvil argentino completo (ej. 5492611234567)
    if numeros.startswith(codigo_pais + "9") and len(numeros) == (len(codigo_pais) + 1 + 10): # 54 + 9 + 10 digitos
        return numeros
    
    # Caso 2: Tiene código de país pero le falta el '9' de móvil (ej. 542611234567)
    if numeros.startswith(codigo_pais) and not numeros.startswith(codigo_pais + "9") and len(numeros) == (len(codigo_pais) + 10):
        return codigo_pais + "9" + numeros[len(codigo_pais):]

    # Caso 3: Número local de 10 dígitos (característica + número, ej. 2611234567)
    if len(numeros) == 10:
        return f"{codigo_pais}9{numeros}"
        
    # Caso 4: Número de celular sin característica pero con el 15 (ej. 154123456) -> común en algunas regiones.
    # Esta es una heurística más específica y puede necesitar ajuste.
    # En Argentina, el "15" a veces se omite para WhatsApp, dependiendo de la agenda.
    # Si el número de la BD viene sin el "15", la lógica de 10 dígitos debería cubrirlo si se ingresa la característica.
    # Si es un número de 8 dígitos (ej. 4123456 tras quitar el 15) no hay suficiente info para un link de WhatsApp confiable sin la característica.

    # Fallback: devolver solo los dígitos limpios. El link podría no funcionar bien o el usuario tendrá que ajustarlo.
    logging.warning(f"Número de teléfono '{telefono_str}' no pudo ser formateado a un estándar de WhatsApp claro, devolviendo dígitos limpios: '{numeros}'")
    return numeros


def reemplazar_placeholders(texto: str, user_obj) -> str:
    if not texto:
        return ""

    # Placeholders estándar que SÍ esperamos obtener del user_obj (perfil de la PYME)
    placeholders_conocidos = {
        "[nombreEmpresa]": "nombre_empresa",
        "[linkWeb]": "link_web",        # Manejo especial abajo
        "[telefono]": "telefono",      # Manejo especial para WhatsApp link
        "[direccion]": "direccion",
        "[horario]": "horario",        # Para el string simple de horario. Ver [horarioDetallado]
        "[ubicacion]": "ubicacion",    # Usualmente la provincia
        "[ciudad]" : "ciudad",         # Nuevo campo
        "[provincia]": "provincia",     # Nuevo campo
        "[pais]": "pais",             # Nuevo campo
        # Placeholder para los horarios estructurados (si decides usarlo en FAQs/Intents)
        "[horarioDetallado]": "horario_json", # Asume que 'horario_json' es el atributo en User
    }

    # Valores por defecto si user_obj es None o el atributo no existe
    defaults_textos = {
        "nombre_empresa": "nuestra empresa",
        "link_web": "nuestro sitio web",
        "telefono": "nuestro número de contacto",
        "direccion": "nuestra dirección",
        "horario": "nuestro horario de atención", # Default para el string simple de horario
        "ubicacion": "nuestra área de servicio",
        "ciudad": "nuestra ciudad",
        "provincia": "nuestra provincia",
        "pais": "nuestro país",
        "horario_json": "consultar nuestros horarios detallados",
    }

    texto_procesado = texto

    for ph_template, user_attr_name in placeholders_conocidos.items():
        valor_atributo_original = None
        if user_obj:
            valor_atributo_original = getattr(user_obj, user_attr_name, None)
        
        valor_para_texto = str(valor_atributo_original) if valor_atributo_original is not None and str(valor_atributo_original).strip() != "" else defaults_textos.get(user_attr_name, "")

        if ph_template == "[telefono]":
            if valor_atributo_original: # Si hay un número de teléfono real
                numero_telefono_str = str(valor_atributo_original)
                numero_wsp_formateado = formatear_numero_whatsapp_simple(numero_telefono_str)
                if numero_wsp_formateado:
                    link_wsp_html = f'{numero_telefono_str} (<a href="https://wa.me/{numero_wsp_formateado}" target="_blank" style="color: green; text-decoration: underline; font-weight:bold;">Contactar por WhatsApp</a>)'
                    texto_procesado = texto_procesado.replace(ph_template, link_wsp_html)
                else: # Si no se pudo formatear bien, solo poner el número original
                    texto_procesado = texto_procesado.replace(ph_template, numero_telefono_str)
            else: # No hay número, usar default
                texto_procesado = texto_procesado.replace(ph_template, defaults_textos.get(user_attr_name, "nuestro número de contacto"))
        
        elif ph_template == "[linkWeb]":
            # Para el texto de [linkWeb], si el link está vacío, usamos el default.
            # El botón HTML "Ir a la Tienda Online" tiene su propia lógica para el href.
            if valor_atributo_original and str(valor_atributo_original).strip():
                # Asegurar que el link sea absoluto para mostrar en texto también
                link_abs = str(valor_atributo_original)
                if not link_abs.startswith("http://") and not link_abs.startswith("https://"):
                    link_abs = "https://" + link_abs
                texto_procesado = texto_procesado.replace(ph_template, link_abs)
            else:
                texto_procesado = texto_procesado.replace(ph_template, defaults_textos.get(user_attr_name, "nuestro sitio web"))
        
        elif ph_template == "[horarioDetallado]":
            if valor_atributo_original: # valor_atributo_original sería el string JSON de user.horario_json
                try:
                    horarios_data = json.loads(str(valor_atributo_original)) # Parsear el JSON
                    texto_horario_formateado = ""
                    dias_semana_es = ["Lunes", "Martes", "Miércoles", "Jueves", "Viernes", "Sábado", "Domingo"]
                    for i, dia_data in enumerate(horarios_data):
                        if i < len(dias_semana_es):
                            dia_nombre = dias_semana_es[i]
                            if dia_data.get("cerrado"):
                                texto_horario_formateado += f"{dia_nombre}: Cerrado. "
                            else:
                                abre = dia_data.get('abre','--:--')
                                cierra = dia_data.get('cierra','--:--')
                                texto_horario_formateado += f"{dia_nombre}: {abre} - {cierra}. "
                    texto_procesado = texto_procesado.replace(ph_template, texto_horario_formateado.strip())
                except (TypeError, json.JSONDecodeError) as e_json:
                    logging.warning(f"Error parseando horario_json para PYME {getattr(user_obj, 'id', 'N/A')}: {valor_atributo_original}. Error: {e_json}")
                    # Fallback al campo 'horario' de texto simple si existe, o al default
                    texto_fallback_horario = getattr(user_obj, "horario", defaults_textos.get("horario", "consultar horario"))
                    texto_procesado = texto_procesado.replace(ph_template, texto_fallback_horario)
            else: # No hay horario_json, usar default
                texto_procesado = texto_procesado.replace(ph_template, defaults_textos.get("horario_json", "consultar nuestros horarios"))
        else: # Para otros placeholders conocidos
            texto_procesado = texto_procesado.replace(ph_template, valor_para_texto)

    # Fallback para placeholders desconocidos [AlgoEntreCorchetes]
    def reemplazar_desconocido_callback(match):
        placeholder_interno = match.group(1)
        # Evitar recursión si el reemplazo mismo contiene un placeholder (poco probable aquí)
        if placeholder_interno in placeholders_conocidos:
             return match.group(0) # Devolver el placeholder original si es uno conocido que no se reemplazó (no debería pasar)

        logging.warning(f"Placeholder desconocido encontrado: [{placeholder_interno}] en texto: \"{texto_procesado[:150].replace(match.group(0), '')}...\"") # Loguear el contexto
        if "precio" in placeholder_interno.lower() or "costo" in placeholder_interno.lower():
            return "(precio a consultar)"
        elif "link" in placeholder_interno.lower() or "url" in placeholder_interno.lower():
            link_web_general = ""
            if user_obj and hasattr(user_obj, "link_web"):
                link_web_general = getattr(user_obj, "link_web", "")
                if link_web_general and not link_web_general.startswith("http"):
                    link_web_general = "https://" + link_web_general
            return f"(visita nuestro sitio web{': ' + link_web_general if link_web_general else ''} para más información)"
        return "" # Por defecto, eliminar placeholders desconocidos

    texto_procesado = re.sub(r"\[([^\]\[]+)\]", reemplazar_desconocido_callback, texto_procesado)
    return texto_procesado

# --- COMIENZO DE responder_chatboc ---
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
    except Exception: # Captura más genérica si token no es string
        logging.warning(f"Error al verificar si el token es demo (token podría no ser string): {token}")
        pass # is_demo seguirá False

    if is_demo:
        logging.info("Modo demo anónimo detectado")
        session.setdefault("anon_preguntas", 0)
        if session["anon_preguntas"] >= 15:
            return {"respuesta": "🔒 Límite de 15 preguntas en modo demo alcanzado. Registrate para seguir.", "fuente": "sistema"}
        session["anon_preguntas"] += 1

        class AnonUser: # Definición de AnonUser como la tenías
            nombre_empresa = "Demo Anónimo"
            plan = "demo"
            preguntas_usadas = session["anon_preguntas"]
            limite_preguntas = 15
            rubro_id = None 
            link_web = "tu-tienda-online.com" 
            telefono = "5491112345678" # Ejemplo de número para demo
            direccion = "Calle Falsa 123, Ciudad Demo"
            horario = "Lunes a Viernes de 9 a 18hs" # String simple para el default
            horario_json = '[{"abre":"09:00","cierra":"18:00","cerrado":false},{"abre":"09:00","cierra":"18:00","cerrado":false},{"abre":"09:00","cierra":"18:00","cerrado":false},{"abre":"09:00","cierra":"18:00","cerrado":false},{"abre":"09:00","cierra":"18:00","cerrado":false},{"abre":"09:00","cierra":"13:00","cerrado":false},{"abre":"","cierra":"","cerrado":true}]'
            ubicacion = "Ciudad Demo"
            ciudad = "Ciudad Demo"
            provincia = "Provincia Demo"
            pais = "País Demo"
            latitud = -32.8895  # Mendoza Coords
            longitud = -68.8458 # Mendoza Coords
            id = None
        user = AnonUser()
    else: # No es demo-anon
        if token: # Si hay token, es un usuario real o un token demo de PYME
            try:
                db_user = User.query.filter_by(token=token).first()
                if not db_user:
                    # Si el token no es "demo-anon" y no se encuentra en la BD, podría ser un error o un anónimo general.
                    # Por ahora, si hay token pero no es válido, devolvemos error.
                    # Si el token es "demo-token" (genérico de ChatPage.tsx), aquí también daría error.
                    # Se podría añadir una lógica para un "demo-token" genérico si es necesario.
                    logging.warning(f"Usuario no autenticado o token inválido (no demo-anon): {token}")
                    return {"error": "Usuario no autenticado"}
                if db_user.preguntas_usadas >= db_user.limite_preguntas:
                    return {"respuesta": "🔒 Límite de preguntas alcanzado en tu plan. Actualizá para más.", "fuente": "sistema"}
                user = db_user 
            except Exception as e:
                logging.error(f"Error obteniendo usuario de BD: {e}", exc_info=True)
                return {"error": "Error interno al obtener usuario"}
        else: # No hay token y no es demo-anon => Anónimo general
            logging.info("Sin token y no es demo-anon. Tratando como anónimo general.")
            class GenericAnonUser:
                nombre_empresa = "la empresa" # Default, se puede sobreescribir por rubro_frontend
                plan = "anonimo"
                rubro_id = None # Se intentará determinar por rubro_frontend
                link_web = "" 
                telefono = ""
                direccion = ""
                horario = "horario de atención habitual" # String simple
                horario_json = '[]' # JSON vacío por defecto
                ubicacion = ""
                ciudad = ""
                provincia = ""
                pais = ""
                latitud = None
                longitud = None
                id = None # Muy importante para no intentar operaciones de BD que requieran ID
            user = GenericAnonUser()

    # Determinar rubro (Prioridad para rubro_nombre_frontend si se proporciona y es válido)
    rubro_determinado_por_frontend = False
    if rubro_nombre_frontend:
        logging.info(f"Intentando determinar rubro por frontend: '{rubro_nombre_frontend}'")
        rubro_obj_frontend = Rubro.query.filter(db.func.lower(Rubro.nombre) == rubro_nombre_frontend.lower().strip()).first()
        if rubro_obj_frontend:
            rubro_id = rubro_obj_frontend.id
            rubro_nombre = rubro_obj_frontend.nombre.lower().strip()
            rubro_determinado_por_frontend = True
            logging.info(f"Rubro determinado por frontend: {rubro_nombre} (ID: {rubro_id})")
            if isinstance(user, GenericAnonUser): # Si es anónimo genérico, actualizar su contexto
                user.nombre_empresa = getattr(rubro_obj_frontend, 'nombre_pyme_asociada', rubro_obj_frontend.nombre) # Asumir un campo o usar el nombre del rubro
                user.rubro_id = rubro_id
                # Idealmente, el objeto Rubro podría tener datos default de la PYME para ese rubro
                user.link_web = getattr(rubro_obj_frontend, 'link_web_default', "") 
                user.telefono = getattr(rubro_obj_frontend, 'telefono_default', "")
                user.direccion = getattr(rubro_obj_frontend, 'direccion_default', "") 
                user.horario = getattr(rubro_obj_frontend, 'horario_default', "horario de atención habitual")
                user.horario_json = getattr(rubro_obj_frontend, 'horario_json_default', '[]')
                user.ubicacion = getattr(rubro_obj_frontend, 'ubicacion_default', "")
                # ... y para los nuevos campos de dirección si los tienes en el modelo Rubro
                user.ciudad = getattr(rubro_obj_frontend, 'ciudad_default', "")
                user.provincia = getattr(rubro_obj_frontend, 'provincia_default', "")
                # user.latitud = getattr(rubro_obj_frontend, 'latitud_default', None)
                # user.longitud = getattr(rubro_obj_frontend, 'longitud_default', None)
        else:
            logging.warning(f"Rubro '{rubro_nombre_frontend}' enviado por frontend no encontrado en BD. Usando rubro del usuario o general.")

    if not rubro_determinado_por_frontend and user and hasattr(user, "rubro_id") and user.rubro_id:
        logging.info(f"Rubro no determinado por frontend o no válido. Usando rubro_id del objeto User: {user.rubro_id}")
        rubro_obj_user = Rubro.query.get(user.rubro_id)
        if rubro_obj_user:
            rubro_id = rubro_obj_user.id
            rubro_nombre = rubro_obj_user.nombre.lower().strip()
        else: # Rubro_id del usuario no es válido, usar general
            logging.warning(f"Rubro ID {user.rubro_id} del usuario no encontrado. Usando general.")
            rubro_id = 1 
            rubro_general_obj = Rubro.query.get(rubro_id)
            rubro_nombre = rubro_general_obj.nombre.lower().strip() if rubro_general_obj else "general"
    elif not rubro_determinado_por_frontend : # Si no hay rubro_frontend ni rubro_id en user, usar general
        logging.info("Ni rubro_frontend ni rubro_id en user. Usando rubro general.")
        rubro_id = 1
        rubro_general_obj = Rubro.query.get(rubro_id)
        rubro_nombre = rubro_general_obj.nombre.lower().strip() if rubro_general_obj else "general"

    # Final check para asegurar que rubro_id y rubro_nombre sean válidos
    if not Rubro.query.get(rubro_id): 
        logging.error(f"Rubro ID {rubro_id} final sigue siendo inválido. Forzando a general (ID 1). ¡Revisar lógica de rubros!")
        rubro_id = 1 
        rubro_general_obj = Rubro.query.get(rubro_id)
        rubro_nombre = rubro_general_obj.nombre.lower().strip() if rubro_general_obj else "general"
    
    logging.info(f"Contexto PYME final para esta solicitud: Empresa: {getattr(user, 'nombre_empresa', 'N/A')} | Rubro: {rubro_nombre} (ID {rubro_id})")

    # ----- Construcción de Mensajes y Contexto para LLM -----
    MAX_MENSAJES_HISTORIAL_PARA_LLM = 10 # Últimos 5 intercambios
    historial_actual_cliente = session.get(NOMBRE_HISTORIAL_SESION, [])[:] # Obtener copia o lista vacía
    
    mensajes_para_llm = []
    if len(historial_actual_cliente) > MAX_MENSAJES_HISTORIAL_PARA_LLM:
        mensajes_para_llm = historial_actual_cliente[-MAX_MENSAJES_HISTORIAL_PARA_LLM:]
    else:
        mensajes_para_llm = historial_actual_cliente
    mensajes_para_llm.append({"role": "user", "content": pregunta})

    contexto_catalogo = ""
    # Solo buscar en catálogo si no es demo Y si user es una instancia de User (tiene id)
    # y no GenericAnonUser (que tiene id=None)
    if not is_demo and hasattr(user, 'id') and user.id is not None:
        try:
            from services.qdrant_search import buscar_catalogo_qdrant, armar_respuesta_legible
            logging.info(f"Buscando catálogo en Qdrant para user_id (PYME): {user.id}...")
            resultados_qdrant = buscar_catalogo_qdrant(user.id, pregunta, limite=5) # Límite de resultados de Qdrant
            contexto_catalogo = armar_respuesta_legible(resultados_qdrant)
            if contexto_catalogo:
                 logging.info(f"Contexto Qdrant (primeros 150 chars): {contexto_catalogo[:150]}...")
            else:
                 logging.info("No se encontró contexto de catálogo en Qdrant para esta pregunta.")
        except ImportError:
            logging.error("Módulo Qdrant (services.qdrant_search) no encontrado.")
        except Exception as e:
            logging.error(f"Error buscando catálogo en Qdrant: {e}", exc_info=True)

    # Perfil de la PYME para el prompt (usando los nuevos campos si existen)
    pyme_nombre_empresa = getattr(user, "nombre_empresa", "la tienda")
    pyme_link_web = getattr(user, "link_web", "")
    pyme_telefono = getattr(user, "telefono", "") # Se formateará después
    pyme_direccion = getattr(user, "direccion", "")
    pyme_ciudad = getattr(user, "ciudad", "")
    pyme_provincia = getattr(user, "provincia", getattr(user, "ubicacion", "")) # Fallback a ubicacion si provincia no existe
    pyme_horario_str_simple = getattr(user, "horario", "nuestro horario de atención") # String simple
    pyme_horario_json_str = getattr(user, "horario_json", "[]") # String JSON de horarios

    user_profile_context = {
        "nombre_empresa": pyme_nombre_empresa,
        "rubro_nombre": rubro_nombre,
        "telefono_raw": pyme_telefono, # Teléfono crudo para el prompt, se formatea en reemplazar_placeholders
        "link_web": pyme_link_web,
        "direccion_completa": f"{pyme_direccion}, {pyme_ciudad}, {pyme_provincia}".strip(', '),
        "horario_str": pyme_horario_str_simple, # Horario como string simple
        "horario_json_str": pyme_horario_json_str, # String JSON para posible uso avanzado por LLM o formateo
        # Podrías añadir latitud y longitud si el LLM pudiera usarlos para algo (ej. "cerca de...")
    }
    
    numero_intercambios_previos = len(session.get(NOMBRE_HISTORIAL_SESION, [])) // 2
    
    # --- Formatear horarios detallados para el prompt (si existen y son válidos) ---
    horarios_para_prompt = user_profile_context['horario_str'] # Default al string simple
    try:
        if user_profile_context['horario_json_str'] and user_profile_context['horario_json_str'] != '[]':
            horarios_data = json.loads(user_profile_context['horario_json_str'])
            dias_semana_es = ["Lunes", "Martes", "Miércoles", "Jueves", "Viernes", "Sábado", "Domingo"]
            partes_horario = []
            for i, dia_data in enumerate(horarios_data):
                if i < len(dias_semana_es):
                    dia_nombre = dias_semana_es[i]
                    if dia_data.get("cerrado"):
                        partes_horario.append(f"{dia_nombre}: Cerrado")
                    else:
                        abre = dia_data.get('abre','--:--')
                        cierra = dia_data.get('cierra','--:--')
                        partes_horario.append(f"{dia_nombre}: de {abre} a {cierra}")
            if partes_horario:
                horarios_para_prompt = ". ".join(partes_horario) + "."
                logging.info(f"Horarios formateados para prompt: {horarios_para_prompt}")
    except Exception as e_json_horario:
        logging.warning(f"No se pudo parsear o formatear horario_json para el prompt: {e_json_horario}. Usando horario_str.")
    # --- Fin formateo de horarios ---
    prompt_sistema_texto = (
        f"Sos Chatboc, un asistente comercial experto de {user_profile_context['nombre_empresa']} (rubro: {user_profile_context['rubro_nombre']}), ubicada en {user_profile_context['direccion_completa']}. " # Añadida ubicación al inicio
        f"Tu principal objetivo es entender rápidamente las necesidades del cliente y guiarlo hacia una compra o una visita a la tienda online ({user_profile_context['link_web'] if user_profile_context['link_web'] else 'nuestra página web'}) en los próximos 2-4 intercambios. "
        f"Ya has tenido {numero_intercambios_previos} intercambios con este cliente (revisa el historial de conversación que te proveo). "
        "Sé amable, muy proactivo, resolutivo y persuasivo. Tus respuestas deben ser breves, directas y valiosas. Ve al grano. Evita el texto de relleno o introducciones innecesarias. Proporciona la información clave de forma concisa. "
        "Haz preguntas claras si necesitas más información para ayudarle. "
        "Si el cliente muestra interés en un producto o servicio, intenta cerrar la venta ofreciendo añadirlo al carrito, llevarlo a la página del producto en la tienda online, o facilitando el siguiente paso de forma clara y simple. "
        "No menciones que eres una IA ni un 'asistente virtual'. Habla como un vendedor humano y entusiasta. "
        
        # --- Uso Mejorado de Datos de la Empresa ---
        f"\nINFORMACIÓN DE CONTACTO Y UBICACIÓN DE {user_profile_context['nombre_empresa']}:"
        f"\n- Teléfono (para llamadas o WhatsApp): {user_profile_context['telefono_raw'] if user_profile_context['telefono_raw'] else 'No disponible'}"
        f"\n- Dirección: {user_profile_context['direccion_completa'] if user_profile_context['direccion_completa'].strip() else 'Consultar por nuestra ubicación.'}"
        f"\n- Horarios de Atención: {horarios_para_prompt if horarios_para_prompt else 'Consultar nuestros horarios.'}" # Usa los horarios formateados
        f"\n- Sitio Web: {user_profile_context['link_web'] if user_profile_context['link_web'] else 'No disponible'}"
        # --- Fin Uso Mejorado de Datos ---

        "\nSI LA PREGUNTA DEL CLIENTE NO TIENE SENTIDO, es incomprensible o solo son caracteres al azar, NO intentes responderla directamente. En su lugar, responde amablemente que no entendiste la consulta y ofrece ayuda general. Ejemplo: 'Disculpa, no entendí bien tu consulta. Puedo ayudarte con información sobre nuestros productos, precios, horarios o cómo comprar. ¿En qué te puedo asistir hoy?'"
        
        "\nIMPORTANTE SOBRE PRODUCTOS Y PRECIOS DEL CATÁLOGO QUE TE PROVEERÉ:"
        "\n1. Cuando el cliente pregunte por un tipo de producto (ej. 'vinos malbec'), y si el catálogo recuperado contiene múltiples opciones, PRESENTA CLARAMENTE LAS OPCIONES MÁS RELEVANTES (máximo 2-3) con su 'Nombre' y 'Precio' exactos. Sé conciso. Ejemplo: 'Tenemos: Vino Malbec A a [Precio A], y Vino Malbec B Reserva a [Precio B].'"
        "\n2. Si el cliente pregunta por el precio de un producto específico y lo encuentras en el catálogo, da el 'Precio' indicado de forma directa."
        "\n3. Si el cliente pide varias unidades de un producto con precio, y el precio es numérico, calcula el total y ofréceselo directamente. Ejemplo: '3 unidades de [Producto X] serían $[Total]'."
        "\n4. Si la información del catálogo no es clara sobre un precio, o dice 'Consultar precio', indícalo brevemente y sugiere consultar en la tienda online o contactar."
        "\n5. Si la información del catálogo es extensa para un producto, resume los puntos más importantes para el cliente o enfócate en lo que preguntó. No copies grandes bloques de texto."
        "\n6. Si no hay información del catálogo relevante, responde concisamente con conocimiento general o pide más detalles."
        "\n7. Si te preguntan '¿Están abiertos ahora?' o sobre horarios específicos, utiliza la información de 'Horarios de Atención' que te proporcioné para responder lo más precisamente posible." # Nueva instrucción para horarios
    )
    if contexto_catalogo:
        prompt_sistema_texto += f"\n\nINFORMACIÓN DEL CATÁLOGO PARA ESTA CONSULTA (usa solo lo relevante y sé breve):\n---\n{contexto_catalogo}\n---\nUsa esta información del catálogo para responder, siguiendo las instrucciones sobre productos, precios y brevedad que te di."
    else:
        prompt_sistema_texto += "\nNo encontré información específica en el catálogo para esta consulta. Intenta ayudar al cliente de forma concisa con tu conocimiento general sobre los productos/servicios del rubro, o pide más detalles."
    prompt_sistema_texto += "\n\nInicia tu respuesta directamente al cliente, continuando la conversación de forma natural y concisa."

    # ... (el resto de la función responder_chatboc se mantiene igual)

    mensajes_finales_para_llm = [{"role": "system", "content": prompt_sistema_texto}] + mensajes_para_llm
    
    respuesta_obtenida_llm = "" # Respuesta cruda del LLM
    fuente_respuesta = "desconocida"

    try:
        from services.cohere_ai import get_cohere_response
        logging.info(f"Enviando {len(mensajes_finales_para_llm)} mensajes a Cohere. Prompt sistema longitud: {len(prompt_sistema_texto)}.")
        respuesta_obtenida_llm = get_cohere_response(mensajes_finales_para_llm, rubro_id=rubro_id, user_context=user_profile_context)
        if respuesta_obtenida_llm and len(respuesta_obtenida_llm) > 3:
            fuente_respuesta = "cohere"
            logging.info(f"Respuesta de Cohere (cruda, antes de placeholders): {respuesta_obtenida_llm[:200]}...")
        else:
            logging.warning("Respuesta de Cohere vacía o muy corta.")
            respuesta_obtenida_llm = "" # Asegurar que esté vacía
    except ImportError:
        logging.error("Módulo Cohere (services.cohere_ai.get_cohere_response) no encontrado.")
    except Exception as e:
        logging.error(f"Error al llamar a Cohere: {e}", exc_info=True)

    # Procesar respuesta (si la hubo del LLM) o buscar en fallbacks
    respuesta_final_procesada = ""

    if respuesta_obtenida_llm:
        respuesta_final_procesada = reemplazar_placeholders(respuesta_obtenida_llm, user)
    else: # Si Cohere NO dio respuesta, intentar backups
        logging.info("Cohere no dio respuesta o fue inválida. Intentando backups (FAQ, Intents)...")
        try:
            from services.faq_matcher_spacy import buscar_en_faq_spacy
            faq_match = buscar_en_faq_spacy(pregunta, rubro_id)
            if faq_match and hasattr(faq_match, 'answer'):
                respuesta_final_procesada = reemplazar_placeholders(faq_match.answer, user)
                fuente_respuesta = "faq"
                logging.info(f"Respuesta desde FAQ (procesada): {respuesta_final_procesada}")
        except ImportError: logging.error("Módulo FAQ (spaCy) no encontrado.")
        except Exception as e: logging.warning(f"Error en FAQ backup: {e}", exc_info=True)

        if not respuesta_final_procesada:
            try:
                from services.intent_matcher import buscar_en_intents
                intent_match_text = buscar_en_intents(pregunta, rubro_nombre)
                if intent_match_text:
                    respuesta_final_procesada = reemplazar_placeholders(intent_match_text, user)
                    fuente_respuesta = "intents"
                    logging.info(f"Respuesta desde Intents (procesada): {respuesta_final_procesada}")
            except ImportError: logging.error("Módulo Intent Matcher no encontrado.")
            except Exception as e: logging.warning(f"Error en Intents backup: {e}", exc_info=True)

    # Guardado y adición del botón
    if respuesta_final_procesada:
        session[NOMBRE_HISTORIAL_SESION].append({"role": "user", "content": pregunta})
        session[NOMBRE_HISTORIAL_SESION].append({"role": "assistant", "content": respuesta_final_procesada}) # Guardar respuesta ya procesada
        
        # Limitar tamaño del historial
        MAX_HISTORIAL_EN_SESION = 20 
        if len(session[NOMBRE_HISTORIAL_SESION]) > MAX_HISTORIAL_EN_SESION:
            session[NOMBRE_HISTORIAL_SESION] = session[NOMBRE_HISTORIAL_SESION][-MAX_HISTORIAL_EN_SESION:]
        session.modified = True
        logging.info(f"Historial de sesión actualizado. Tamaño: {len(session[NOMBRE_HISTORIAL_SESION])}")

        # Guardar en DB si es un usuario PYME real
        if hasattr(user, "id") and user.id is not None and not is_demo :
            try:
                user.preguntas_usadas += 1
                db.session.add(Conversacion(user_id=user.id, pregunta=pregunta, respuesta=respuesta_final_procesada, fuente=fuente_respuesta, rubro=rubro_nombre))
                db.session.commit()
                logging.info("Conversación guardada en BD para usuario PYME.")
            except Exception as e:
                logging.error(f"Error guardando conversación en DB: {e}", exc_info=True)
                db.session.rollback()
        
        respuesta_para_frontend = respuesta_final_procesada
        # Añadir botón HTML "Ir a la Tienda Online"
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
            respuesta_para_frontend += boton_html
        
        return {"respuesta": respuesta_para_frontend, "nivel_usado": rubro_nombre, "fuente": fuente_respuesta}

    else: # Fallback final a sugerencias del sistema
        sugerencias_generadas = sugerencias_por_rubro(rubro_id)
        respuesta_sugerencias_base = "No encontré una respuesta directa para tu consulta. Quizás puedas intentar preguntando algo como: " + " · ".join(f"“{s}”" for s in sugerencias_generadas)
        respuesta_sugerencias_procesada = reemplazar_placeholders(respuesta_sugerencias_base, user)
        
        if pyme_link_web: # Añadir link a la tienda también en las sugerencias
            link_absoluto_sug = pyme_link_web
            if not link_absoluto_sug.startswith("http://") and not link_absoluto_sug.startswith("https://"):
                link_absoluto_sug = "https://" + link_absoluto_sug
            link_html_sug = (
                f'\n<div style="margin-top: 10px; font-size: 0.9em;">'
                f'También puedes <a href="{link_absoluto_sug}" target="_blank">visitar nuestra tienda online</a> para más información.'
                '</div>'
            )
            respuesta_sugerencias_procesada += link_html_sug

        logging.info("No se encontró respuesta directa. Enviando sugerencias al cliente.")
        session[NOMBRE_HISTORIAL_SESION].append({"role": "user", "content": pregunta}) # Guardar pregunta para contexto
        session.modified = True
        return {"respuesta": respuesta_sugerencias_procesada, "fuente": "sugerencia_sistema"}

# --- FIN DE responder_chatboc ---