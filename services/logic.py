# services/logic.py
import logging
import random
from flask import session
from sqlalchemy import func
from models import User, Rubro, Sugerencia, Conversacion 
from extensions import db
import re 
import json 

logger = logging.getLogger(__name__)

# --- Clases para usuarios anónimos (definidas a nivel de módulo) ---
class _BaseAnonUser:
    nombre_empresa = "la tienda"
    plan = "anonimo"
    rubro_id = None # Se intentará determinar
    link_web = ""
    telefono = ""
    direccion = ""
    horario = "horario de atención habitual" 
    horario_json = '[]' 
    ciudad = ""
    provincia = ""
    pais = "Argentina" 
    latitud = None
    longitud = None
    id = None # Anónimos no tienen ID de BD

class AnonUserPymeDemo(_BaseAnonUser):
    def __init__(self, preguntas_realizadas_sesion):
        super().__init__()
        self.nombre_empresa = "Chatboc Demostración"
        self.plan = "demo_pyme"
        self.preguntas_usadas = preguntas_realizadas_sesion
        self.limite_preguntas = 15
        self.link_web = "https://www.chatboc.ar" 
        self.telefono = "+549111234567" 
        self.direccion = "Av. Corrientes 1234, CABA"
        self.ciudad = "CABA"
        self.provincia = "CABA"
        self.horario = "Lunes a Viernes de 9hs a 18hs. Sábados de 9hs a 13hs." # String simple
        # String JSON que tu frontend y backend esperan para el horario estructurado
        self.horario_json = json.dumps([
            {"dia": "Lunes", "abre": "09:00", "cierra": "18:00", "cerrado": False},
            {"dia": "Martes", "abre": "09:00", "cierra": "18:00", "cerrado": False},
            {"dia": "Miércoles", "abre": "09:00", "cierra": "18:00", "cerrado": False},
            {"dia": "Jueves", "abre": "09:00", "cierra": "18:00", "cerrado": False},
            {"dia": "Viernes", "abre": "09:00", "cierra": "18:00", "cerrado": False},
            {"dia": "Sábado", "abre": "09:00", "cierra": "13:00", "cerrado": False},
            {"dia": "Domingo", "abre": "", "cierra": "", "cerrado": True}
        ])
        self.latitud = -34.6037 
        self.longitud = -58.3816 

class GenericAnonUser(_BaseAnonUser):
    def __init__(self):
        super().__init__()
        self.plan = "anonimo_general"
        # Este contador de sesión es para anónimos que no son el "demo-anon" específico del widget
        self.preguntas_usadas = session.get("generic_anon_preguntas", 0) 
        self.limite_preguntas = 5 
# --- Fin Clases Anónimas ---

# --- Funciones Auxiliares ---
def sugerencias_por_rubro(rubro_id: int) -> list:
    # ... (Tu función sugerencias_por_rubro, que ya estaba bien) ...
    try:
        sugerencias_obj = Sugerencia.query.filter_by(rubro_id=rubro_id).all()
        if sugerencias_obj:
            todas = [s.texto for s in sugerencias_obj]
            logger.info(f"Sugerencias para rubro {rubro_id}: {len(todas)}")
            return random.sample(todas, min(5, len(todas)))
        if rubro_id != 1: 
            fallback_obj = Sugerencia.query.filter_by(rubro_id=1).all()
            if fallback_obj:
                logger.info(f"No se encontraron sugerencias para rubro {rubro_id}, usando fallback general")
                return random.sample([s.texto for s in fallback_obj], min(5, len(fallback_obj)))
        return ["¿En qué más te puedo ayudar?", "Consulta nuestros productos principales.", "Háblame un poco más sobre lo que buscas."]
    except Exception as e:
        logger.error(f"Error buscando sugerencias: {e}", exc_info=True)
        return ["Disculpa, tuve un problema al buscar sugerencias en este momento."]

def formatear_numero_whatsapp_simple(telefono_str: str, codigo_pais: str = "54") -> str:
    # ... (Tu función formatear_numero_whatsapp_simple, que ya estaba bien) ...
    if not telefono_str: return ""
    numeros = re.sub(r'\D', '', str(telefono_str))
    if numeros.startswith(codigo_pais + "9") and len(numeros) == (len(codigo_pais) + 1 + 10): return numeros
    if numeros.startswith(codigo_pais) and not numeros.startswith(codigo_pais + "9") and len(numeros) == (len(codigo_pais) + 10): return codigo_pais + "9" + numeros[len(codigo_pais):]
    if len(numeros) == 10: return f"{codigo_pais}9{numeros}"
    logger.warning(f"Número '{telefono_str}' no formateado claramente a WhatsApp, devolviendo limpios: '{numeros}'")
    return numeros

def reemplazar_placeholders(texto: str, user_obj) -> str:
    # ... (Tu función reemplazar_placeholders, con el manejo de horario_json que discutimos) ...
    # (Asegúrate de que esta sea la versión completa y correcta que te pasé anteriormente)
    if not texto or not isinstance(texto, str): return ""
    placeholders_conocidos = {
        "[nombreEmpresa]": "nombre_empresa", "[linkWeb]": "link_web", "[telefono]": "telefono",
        "[direccion]": "direccion", "[horario]": "horario", "[ciudad]" : "ciudad",
        "[provincia]": "provincia", "[pais]": "pais", "[horarioDetallado]": "horario_json",
    }
    defaults_textos = {
        "nombre_empresa": "nuestra empresa", "link_web": "nuestro sitio web", 
        "telefono": "nuestro número de contacto", "direccion": "nuestra dirección",
        "horario": "nuestro horario de atención", "ciudad": "nuestra ciudad", 
        "provincia": "nuestra provincia", "pais": "nuestro país",
        "horario_json": "consultar nuestros horarios detallados", # Default si el parseo falla o no hay datos
    }
    texto_procesado = texto
    for ph_template, attr_key in placeholders_conocidos.items():
        valor_atributo = None
        valor_para_reemplazo = defaults_textos.get(attr_key, "")
        if user_obj:
            if attr_key == "horario_json": 
                if isinstance(user_obj, User): 
                    valor_atributo = user_obj.horario_json # Llama a la @property -> dict o None
                else: 
                    valor_atributo = getattr(user_obj, attr_key, None) # De AnonUser* es un string JSON
            elif attr_key == "horario" and isinstance(user_obj, User):
                 valor_atributo = user_obj.horario 
            else:
                valor_atributo = getattr(user_obj, attr_key, None)

        if valor_atributo is not None:
            if ph_template == "[telefono]":
                if str(valor_atributo).strip():
                    numero_wsp = formatear_numero_whatsapp_simple(str(valor_atributo))
                    valor_para_reemplazo = f'{str(valor_atributo)} (<a href="https://wa.me/{numero_wsp}" target="_blank" style="color: green; text-decoration: underline; font-weight:bold;">Contactar por WhatsApp</a>)' if numero_wsp else str(valor_atributo)
                else: valor_para_reemplazo = defaults_textos.get(attr_key, "nuestro número de contacto")
            elif ph_template == "[linkWeb]":
                if str(valor_atributo).strip():
                    link_abs = str(valor_atributo)
                    if not link_abs.startswith("http"): link_abs = "https://" + link_abs
                    valor_para_reemplazo = link_abs
                else: valor_para_reemplazo = defaults_textos.get(attr_key, "nuestro sitio web")
            elif ph_template == "[horarioDetallado]":
                horarios_data_parseada = None
                if isinstance(valor_atributo, dict): # Si ya es un dict (de User.horario_json)
                    horarios_data_parseada = valor_atributo
                elif isinstance(valor_atributo, str) and valor_atributo.strip() and valor_atributo not in ['[]', '{}']:
                    try: horarios_data_parseada = json.loads(valor_atributo) # Parsear el string JSON
                    except json.JSONDecodeError: 
                        logger.warning(f"Error parseando string de horario_json ('{valor_atributo}') para placeholder [horarioDetallado]. User type: {type(user_obj).__name__}")
                
                if horarios_data_parseada and isinstance(horarios_data_parseada, list) and all(isinstance(h, dict) for h in horarios_data_parseada):
                    partes = []
                    for h_dict in horarios_data_parseada:
                        dia = h_dict.get('dia', 'Día')
                        if h_dict.get('cerrado'):
                            partes.append(f"{dia}: Cerrado")
                        else:
                            abre = h_dict.get('abre','--:--')
                            cierra = h_dict.get('cierra','--:--')
                            partes.append(f"{dia}: de {abre} a {cierra}")
                    valor_para_reemplazo = ". ".join(partes) + "." if partes else defaults_textos.get(attr_key)
                else: 
                    logger.debug(f"No se pudo formatear horario detallado para {ph_template}. Valor atributo: {valor_atributo}. Usando fallback.")
                    valor_para_reemplazo = str(getattr(user_obj, "horario", defaults_textos.get("horario"))) # Fallback al string simple
            elif isinstance(valor_atributo, str) and valor_atributo.strip() == "":
                valor_para_reemplazo = defaults_textos.get(attr_key, "")
            elif not isinstance(valor_atributo, (dict, list)): # Evitar convertir dicts/listas a string directamente si no son manejados
                valor_para_reemplazo = str(valor_atributo)
            # else: si es un dict/list no manejado, se mantiene el default o no se reemplaza si no hay default

        texto_procesado = texto_procesado.replace(ph_template, valor_para_reemplazo)
    
    def reemplazar_desconocido_callback(match):
        placeholder_interno = match.group(1)
        logger.warning(f"Placeholder desconocido encontrado y eliminado: [{placeholder_interno}]")
        # Puedes añadir lógica más específica aquí si quieres
        return "" # Eliminar placeholders desconocidos por defecto
    texto_procesado = re.sub(r"\[([^\]\[\s]+?)\]", reemplazar_desconocido_callback, texto_procesado) # Regex un poco más robusta
    return texto_procesado
# --- FIN Funciones Auxiliares ---


def responder_chatboc(pregunta: str, token: str | None, rubro_nombre_frontend: str | None = None):
    logger.info(f"▶️ Inicio responder_chatboc: Pregunta='{pregunta[:100]}...' Token='{str(token)[:10]}...' RubroFrontend='{rubro_nombre_frontend}'")

    if not pregunta or not pregunta.strip():
        logger.warning("Pregunta vacía recibida.")
        return {"error": "Falta la pregunta"}

    NOMBRE_HISTORIAL_SESION = 'historial_chat_cliente'
    if NOMBRE_HISTORIAL_SESION not in session:
        session[NOMBRE_HISTORIAL_SESION] = []
        logger.info(f"Inicializando '{NOMBRE_HISTORIAL_SESION}' en flask.session.")

    user_obj: Any = None # Tipado más genérico para el objeto usuario
    rubro_id_final: int = 1 
    rubro_nombre_final: str = "general" 
    is_usuario_registrado_real: bool = False

    is_pyme_demo_anon = token is not None and token.startswith("demo-anon-")

    if is_pyme_demo_anon:
        logger.info(f"Modo Demo PYME Anónimo (token: {token}) detectado.")
        session.setdefault("pyme_demo_anon_preguntas", 0)
        if session["pyme_demo_anon_preguntas"] >= 15:
            return {"respuesta": "🔒 Límite de 15 preguntas en modo demo PYME alcanzado. Registrate para seguir.", "fuente": "sistema_limite"}
        session["pyme_demo_anon_preguntas"] += 1
        user_obj = AnonUserPymeDemo(session["pyme_demo_anon_preguntas"])
    
    elif token: 
        db_user_found = User.query.filter_by(token=token).first()
        if db_user_found:
            is_usuario_registrado_real = True
            user_obj = db_user_found
            logger.info(f"Usuario PYME registrado autenticado: {user_obj.email} (ID: {user_obj.id})")
            preguntas_usadas_user = user_obj.preguntas_usadas if user_obj.preguntas_usadas is not None else 0
            limite_preguntas_user = user_obj.limite_preguntas if user_obj.limite_preguntas is not None else 50 
            if preguntas_usadas_user >= limite_preguntas_user:
                return {"respuesta": "🔒 Límite de preguntas alcanzado en tu plan. Actualizá para más.", "fuente": "sistema_limite"}
        else:
            logger.warning(f"Token '{str(token)[:15]}...' proporcionado pero no válido. Tratando como Anónimo Genérico.")
            
    if user_obj is None: 
        logger.info("Creando instancia de Anónimo Genérico.")
        session.setdefault("generic_anon_preguntas", 0)
        if session["generic_anon_preguntas"] >= 5:
             return {"respuesta": "Alcanzaste el límite de preguntas para usuarios anónimos. ¡Registrate gratis para continuar!", "fuente": "sistema_limite"}
        session["generic_anon_preguntas"] += 1
        user_obj = GenericAnonUser()

    logger.info(f"Tipo de user_obj determinado: {type(user_obj).__name__}")
    logger.debug(f"Atributos user_obj.horario: {getattr(user_obj, 'horario', 'N/A')}")
    logger.debug(f"Atributos user_obj.horario_json (si es mock, es string; si es User de BD, es @property que devuelve dict/None): {getattr(user_obj, 'horario_json', 'N/A')}")

    # --- Determinar Rubro Final ---
    rubro_obj_final = None
    if rubro_nombre_frontend and isinstance(rubro_nombre_frontend, str) and rubro_nombre_frontend.strip():
        logger.info(f"Intentando determinar rubro por parámetro frontend: '{rubro_nombre_frontend}'")
        rubro_obj_final = Rubro.query.filter(func.lower(Rubro.nombre) == rubro_nombre_frontend.lower().strip()).first()
        if rubro_obj_final:
            logger.info(f"Rubro determinado por frontend: {rubro_obj_final.nombre} (ID: {rubro_obj_final.id})")
            if isinstance(user_obj, GenericAnonUser): # Si es anónimo genérico, actualizar su contexto
                user_obj.nombre_empresa = getattr(rubro_obj_final, 'nombre_pyme_default_para_anon', f"la sección de {rubro_obj_final.nombre}")
                user_obj.rubro_id = rubro_obj_final.id
                # Aquí podrías cargar más datos default del Rubro al GenericAnonUser si los tuvieras en el modelo Rubro
                # Ejemplo: user_obj.horario = getattr(rubro_obj_final, 'horario_default_rubro', user_obj.horario)
        else:
            logger.warning(f"Rubro '{rubro_nombre_frontend}' (frontend) no encontrado. Se usará el rubro del usuario o general.")

    if not rubro_obj_final and hasattr(user_obj, 'rubro_id') and user_obj.rubro_id:
        logger.info(f"Rubro no determinado por frontend. Usando rubro_id del objeto User: {user_obj.rubro_id}")
        rubro_obj_final = db.session.get(Rubro, user_obj.rubro_id)
        if not rubro_obj_final:
            logger.warning(f"Rubro ID {user_obj.rubro_id} del usuario no encontrado. Se usará general.")
    
    if not rubro_obj_final: # Fallback final a rubro general si todo lo demás falla
        logger.info(f"No se pudo determinar un rubro específico por frontend o usuario. Usando rubro general ID 1.")
        rubro_obj_final = db.session.get(Rubro, 1) 
    
    if rubro_obj_final:
        rubro_id_final = rubro_obj_final.id
        rubro_nombre_final = rubro_obj_final.nombre.lower().strip()
    else: 
        logger.error(f"¡ERROR CRÍTICO! No se encontró el rubro general ID 1 en la BD! Usando 'general' por defecto.")
        rubro_id_final = 1 # Mantener ID 1 como default
        rubro_nombre_final = "general"

    logger.info(f"Contexto PYME final para esta solicitud: Empresa: '{getattr(user_obj, 'nombre_empresa', 'N/A')}', Rubro: '{rubro_nombre_final}' (ID: {rubro_id_final})")

    # --- Construcción de User Profile Context para LLM ---
    raw_horario_str_from_user_obj = getattr(user_obj, 'horario', "[]") # Para User de BD, este es el JSON string. Para mocks, es un string simple o JSON string.
    
    # Si user_obj es User (de BD), user_obj.horario_json es la propiedad que devuelve el dict parseado.
    # Si es mock, user_obj.horario_json es un atributo que contiene el string JSON.
    horario_json_para_llm_str = "[]" # Default a string de lista vacía
    if isinstance(user_obj, User): # Usuario de BD
        horario_json_para_llm_str = user_obj.horario if user_obj.horario else '[]' # Usar el string JSON de la BD
    elif hasattr(user_obj, 'horario_json'): # AnonUserPymeDemo o GenericAnonUser
        horario_json_para_llm_str = user_obj.horario_json if user_obj.horario_json else '[]'


    user_profile_context = {
        "nombre_empresa": getattr(user_obj, "nombre_empresa", "la tienda"),
        "rubro_nombre": rubro_nombre_final,
        "telefono_raw": getattr(user_obj, "telefono", ""),
        "link_web": getattr(user_obj, "link_web", ""),
        "direccion_completa": f"{getattr(user_obj, 'direccion', '')}, {getattr(user_obj, 'ciudad', '')}, {getattr(user_obj, 'provincia', '')}".replace(" ,", "").strip(', ').strip(),
        "horario_str": raw_horario_str_from_user_obj, # El string simple o el string JSON de User.horario
        "horario_json_str": horario_json_para_llm_str, 
    }
    logger.debug(f"User Profile Context para LLM: {user_profile_context}")

    # --- Formatear horarios detallados para el prompt ---
    horarios_para_prompt = user_profile_context['horario_str'] # Default al string simple
    try:
        # Usar el horario_json_str que ya preparamos para el contexto
        if user_profile_context['horario_json_str'] and user_profile_context['horario_json_str'] != '[]':
            horarios_data = json.loads(user_profile_context['horario_json_str'])
            partes_horario = []
            if isinstance(horarios_data, list) and all(isinstance(h, dict) for h in horarios_data):
                for h_dict in horarios_data:
                    dia = h_dict.get("dia", "Día")
                    if h_dict.get("cerrado"):
                        partes_horario.append(f"{dia}: Cerrado")
                    else:
                        abre = h_dict.get('abre','--:--')
                        cierra = h_dict.get('cierra','--:--')
                        partes_horario.append(f"{dia}: de {abre} a {cierra}")
            if partes_horario:
                horarios_para_prompt = ". ".join(partes_horario) + "."
        logger.info(f"Horarios formateados para prompt: {horarios_para_prompt}")
    except Exception as e_json_horario_prompt:
        logger.warning(f"No se pudo parsear o formatear horario_json_str ('{user_profile_context['horario_json_str']}') para el prompt: {e_json_horario_prompt}. Usando horario_str simple.")
    
    numero_intercambios_previos = len(session.get(NOMBRE_HISTORIAL_SESION, [])) // 2
    prompt_sistema_texto = (
        f"Sos Chatboc, un asistente comercial experto de {user_profile_context['nombre_empresa']} (rubro: {user_profile_context['rubro_nombre']}), ubicada en {user_profile_context['direccion_completa']}. "
        f"Tu principal objetivo es entender rápidamente las necesidades del cliente y guiarlo hacia una compra o una visita a la tienda online ({user_profile_context['link_web'] if user_profile_context['link_web'] else 'nuestra página web'}) en los próximos 2-4 intercambios. "
        f"Ya has tenido {numero_intercambios_previos} intercambios con este cliente (revisa el historial de conversación que te proveo). "
        "Sé amable, muy proactivo, resolutivo y persuasivo. Tus respuestas deben ser breves, directas y valiosas. Ve al grano. Evita el texto de relleno o introducciones innecesarias. Proporciona la información clave de forma concisa. "
        "Haz preguntas claras si necesitas más información para ayudarle. "
        "Si el cliente muestra interés en un producto o servicio, intenta cerrar la venta ofreciendo añadirlo al carrito, llevarlo a la página del producto en la tienda online, o facilitando el siguiente paso de forma clara y simple. "
        "No menciones que eres una IA ni un 'asistente virtual'. Habla como un vendedor humano y entusiasta. "
        f"\nINFORMACIÓN DE CONTACTO Y UBICACIÓN DE {user_profile_context['nombre_empresa']}:"
        f"\n- Teléfono (para llamadas o WhatsApp): {user_profile_context['telefono_raw'] if user_profile_context['telefono_raw'] else 'No disponible'}"
        f"\n- Dirección: {user_profile_context['direccion_completa'] if user_profile_context['direccion_completa'].strip(', ') else 'Consultar por nuestra ubicación.'}"
        f"\n- Horarios de Atención: {horarios_para_prompt if horarios_para_prompt and horarios_para_prompt.strip('.') else 'Consultar nuestros horarios.'}" # Chequeo extra para prompt vacío
        f"\n- Sitio Web: {user_profile_context['link_web'] if user_profile_context['link_web'] else 'No disponible'}"
        "\nSI LA PREGUNTA DEL CLIENTE NO TIENE SENTIDO, es incomprensible o solo son caracteres al azar, NO intentes responderla directamente. En su lugar, responde amablemente que no entendiste la consulta y ofrece ayuda general. Ejemplo: 'Disculpa, no entendí bien tu consulta. Puedo ayudarte con información sobre nuestros productos, precios, horarios o cómo comprar. ¿En qué te puedo asistir hoy?'"
        "\nIMPORTANTE SOBRE PRODUCTOS Y PRECIOS DEL CATÁLOGO QUE TE PROVEERÉ:"
        "\n1. Cuando el cliente pregunte por un tipo de producto (ej. 'vinos malbec'), y si el catálogo recuperado contiene múltiples opciones, PRESENTA CLARAMENTE LAS OPCIONES MÁS RELEVANTES (máximo 2-3) con su 'Nombre' y 'Precio' exactos. Sé conciso. Ejemplo: 'Tenemos: Vino Malbec A a [Precio A], y Vino Malbec B Reserva a [Precio B].'"
        "\n2. Si el cliente pregunta por el precio de un producto específico y lo encuentras en el catálogo, da el 'Precio' indicado de forma directa."
        "\n3. Si el cliente pide varias unidades de un producto con precio, y el precio es numérico, calcula el total y ofréceselo directamente. Ejemplo: '3 unidades de [Producto X] serían $[Total]'."
        "\n4. Si la información del catálogo no es clara sobre un precio, o dice 'Consultar precio', indícalo brevemente y sugiere consultar en la tienda online o contactar."
        "\n5. Si la información del catálogo es extensa para un producto, resume los puntos más importantes para el cliente o enfócate en lo que preguntó. No copies grandes bloques de texto."
        "\n6. Si no hay información del catálogo relevante, responde concisamente con conocimiento general o pide más detalles."
        "\n7. Si te preguntan '¿Están abiertos ahora?' o sobre horarios específicos, utiliza la información de 'Horarios de Atención' que te proporcioné para responder lo más precisamente posible."
    )

    # --- Búsqueda en Catálogo Qdrant ---
    contexto_catalogo = ""
    if is_usuario_registrado_real and user_obj.id is not None: # Solo PYMEs registradas tienen catálogo Qdrant
        try:
            from services.qdrant_search import buscar_catalogo_qdrant, armar_respuesta_legible 
            resultados_qdrant = buscar_catalogo_qdrant(user_obj.id, pregunta, limite=3, score_min=0.68) # Ajustar límite y score_min según necesidad
            contexto_catalogo = armar_respuesta_legible(resultados_qdrant)
            if contexto_catalogo: 
                logger.info(f"Contexto Qdrant para LLM (parcial): {contexto_catalogo[:150]}...")
                prompt_sistema_texto += f"\n\nINFORMACIÓN DEL CATÁLOGO PARA ESTA CONSULTA (usa solo lo relevante y sé breve):\n---\n{contexto_catalogo}\n---\nUsa esta información del catálogo para responder, siguiendo las instrucciones sobre productos, precios y brevedad que te di."
            else: 
                logger.info("Sin contexto de catálogo Qdrant para esta pregunta (o score bajo).")
                prompt_sistema_texto += "\nNo encontré información específica en el catálogo para esta consulta. Intenta ayudar al cliente de forma concisa con tu conocimiento general sobre los productos/servicios del rubro, o pide más detalles."
        except ImportError: 
            logger.error("Módulo Qdrant (qdrant_search) no encontrado.")
            prompt_sistema_texto += "\n(Error interno: no se pudo acceder al sistema de búsqueda de catálogo)."
        except Exception as e_qdrant: 
            logger.error(f"Error buscando en Qdrant: {e_qdrant}", exc_info=True)
            prompt_sistema_texto += "\n(Error interno: problema al buscar en catálogo)."
    else: # Para usuarios anónimos o demo, no buscar en catálogo Qdrant personalizado
        logger.info("Usuario no es PYME registrada o no tiene ID, no se buscará en catálogo Qdrant.")
        prompt_sistema_texto += "\nNo hay un catálogo de productos específico para este modo de demostración o usuario. Responde con conocimiento general del rubro y la información de la empresa."

    prompt_sistema_texto += "\n\nInicia tu respuesta directamente al cliente, continuando la conversación de forma natural y concisa."
    logger.debug(f"Prompt del sistema final para Cohere (primeros 500 chars): {prompt_sistema_texto[:500]}")
    
    # --- Preparar mensajes y llamar a Cohere ---
    MAX_MENSAJES_HISTORIAL_PARA_LLM = 10 
    historial_actual_cliente = session.get(NOMBRE_HISTORIAL_SESION, [])[:]
    
    mensajes_historial_api = []
    for msg in historial_actual_cliente[-MAX_MENSAJES_HISTORIAL_PARA_LLM:]: # Tomar solo los últimos N
        api_role = "USER" if msg["role"].lower() == "user" else "CHATBOT"
        mensajes_historial_api.append({"role": api_role, "message": msg["content"]})

    # messages_for_llm para la API de Cohere ahora es solo el historial.
    # El prompt del sistema se pasa como 'preamble' y la pregunta actual como 'message'.
    
    respuesta_obtenida_llm = "" 
    fuente_respuesta = "desconocida"

    try:
        from services.cohere_ai import get_cohere_response
        logger.info(f"Enviando pregunta '{pregunta[:50]}...' a Cohere. Historial para API: {len(mensajes_historial_api)} mensajes.")
        
        # La función get_cohere_response debería manejar la estructura de messages_for_llm
        # que ahora le pasamos como:
        # [{"role": "system", "content": prompt_sistema_texto}, ...historial..., {"role": "user", "content": pregunta}]
        # O, si tu get_cohere_response usa el SDK nuevo con `preamble`:
        # co_client.chat(message=pregunta, chat_history=mensajes_historial_api, preamble=prompt_sistema_texto)
        
        # Alinear con la estructura que `get_cohere_response` espera (previamente era una lista completa)
        # Si `get_cohere_response` espera el system prompt como primer elemento:
        mensajes_completos_cohere = [{"role": "system", "content": prompt_sistema_texto}] + mensajes_historial_api + [{"role": "user", "content": pregunta}]
        
        # O si tu get_cohere_response ya maneja system_prompt y chat_history por separado:
        # respuesta_obtenida_llm = get_cohere_response(
        #     current_message=pregunta,
        #     chat_history_for_api=mensajes_historial_api, # Solo historial user/chatbot
        #     system_prompt_for_api=prompt_sistema_texto, # Preamble/System
        #     rubro_id=rubro_id_final, 
        #     user_context=user_profile_context
        # )
        # Por ahora, asumo que get_cohere_response toma la lista completa como antes:
        respuesta_obtenida_llm = get_cohere_response(mensajes_completos_cohere, rubro_id=rubro_id_final, user_context=user_profile_context)

        logger.info(f"Respuesta CRUDA de Cohere: '{respuesta_obtenida_llm}'")
        if respuesta_obtenida_llm and len(respuesta_obtenida_llm.strip()) > 3: # Umbral pequeño
            fuente_respuesta = "cohere"
            logger.info(f"Respuesta de Cohere (cruda, antes de placeholders): {respuesta_obtenida_llm[:200]}...")
        else:
            logger.warning(f"Respuesta de Cohere fue vacía o muy corta: '{respuesta_obtenida_llm}'. Se intentarán fallbacks.")
            respuesta_obtenida_llm = "" # Asegurar que esté vacía para proceder a fallbacks
    except ImportError: 
        logger.error("Módulo Cohere (services.cohere_ai.get_cohere_response) no encontrado.")
    except Exception as e_cohere: 
        logger.error(f"Error al llamar a Cohere: {e_cohere}", exc_info=True)

    # --- Fallbacks ---
    respuesta_final_procesada = ""
    if respuesta_obtenida_llm:
        respuesta_final_procesada = reemplazar_placeholders(respuesta_obtenida_llm, user_obj)
    else: 
        logger.info("Cohere no dio respuesta válida. Intentando FAQ Matcher...")
        try:
            from services.faq_matcher_spacy import buscar_en_faq_spacy
            faq_match = buscar_en_faq_spacy(pregunta, rubro_id_final) # Usar rubro_id_final
            if faq_match and hasattr(faq_match, 'answer') and faq_match.answer:
                respuesta_final_procesada = reemplazar_placeholders(faq_match.answer, user_obj)
                fuente_respuesta = "faq"
                logger.info(f"Respuesta desde FAQ (procesada): {respuesta_final_procesada[:100]}...")
        except ImportError: logger.error("Módulo FAQ (faq_matcher_spacy) no encontrado.")
        except Exception as e_faq: logger.warning(f"Error en FAQ backup: {e_faq}", exc_info=True)

        if not respuesta_final_procesada:
            logger.info("FAQ no dio respuesta. Intentando Intent Matcher...")
            try:
                from services.intent_matcher import buscar_en_intents
                intent_match_text = buscar_en_intents(pregunta, rubro_nombre_final) # Usar rubro_nombre_final
                if intent_match_text:
                    respuesta_final_procesada = reemplazar_placeholders(intent_match_text, user_obj)
                    fuente_respuesta = "intent"
                    logger.info(f"Respuesta desde Intents (procesada): {respuesta_final_procesada[:100]}...")
            except ImportError: logger.error("Módulo Intent Matcher (intent_matcher) no encontrado.")
            except Exception as e_intent: logger.warning(f"Error en Intents backup: {e_intent}", exc_info=True)

    # --- Guardado y respuesta final ---
    if respuesta_final_procesada and respuesta_final_procesada.strip():
        session[NOMBRE_HISTORIAL_SESION].append({"role": "user", "content": pregunta})
        session[NOMBRE_HISTORIAL_SESION].append({"role": "assistant", "content": respuesta_final_procesada})
        
        MAX_HISTORIAL_EN_SESION = 20 
        if len(session[NOMBRE_HISTORIAL_SESION]) > MAX_HISTORIAL_EN_SESION:
            session[NOMBRE_HISTORIAL_SESION] = session[NOMBRE_HISTORIAL_SESION][-MAX_HISTORIAL_EN_SESION:]
        session.modified = True
        # logger.info(f"Historial de sesión actualizado. Tamaño: {len(session[NOMBRE_HISTORIAL_SESION])}")

        if is_usuario_registrado_real and user_obj.id: # Solo para usuarios PYME reales
            try:
                user_obj.preguntas_usadas = (user_obj.preguntas_usadas or 0) + 1
                db.session.add(Conversacion(user_id=user_obj.id, pregunta=pregunta, respuesta=respuesta_final_procesada, fuente=fuente_respuesta, rubro=rubro_nombre_final))
                db.session.commit()
                # logger.info("Conversación guardada en BD para usuario PYME.")
            except Exception as e_db_conv:
                logger.error(f"Error guardando conversación en DB para user {user_obj.id}: {e_db_conv}", exc_info=True)
                db.session.rollback()
        
        respuesta_para_frontend = respuesta_final_procesada
        pyme_link_web_actual = getattr(user_obj, "link_web", "")
        if pyme_link_web_actual and fuente_respuesta != "faq": # No añadir botón si la respuesta ya es de FAQ (podría tener su propio formato)
            link_absoluto = pyme_link_web_actual
            if not link_absoluto.startswith("http"): link_absoluto = "https://" + link_absoluto
            boton_html = (
                f'\n<div style="margin-top: 15px; padding-top: 10px; border-top: 1px solid #eee;">'
                f'<a href="{link_absoluto}" target="_blank" '
                f'style="display: inline-block; background-color: #007bff; color: white; padding: 10px 20px; '
                f'text-align: center; text-decoration: none; border-radius: 5px; font-size: 16px; font-weight: bold;">'
                'Ir a la Tienda Online'
                '</a></div>'
            )
            respuesta_para_frontend += boton_html
        
        logger.info(f"✅ Respuesta final enviada (fuente: {fuente_respuesta}): '{respuesta_para_frontend[:100]}...'")
        return {"respuesta": respuesta_para_frontend, "nivel_usado": rubro_nombre_final, "fuente": fuente_respuesta}
    else: 
        logger.info("Todos los sistemas (LLM, FAQ, Intents) fallaron en dar una respuesta. Usando sugerencias de fallback.")
        sugerencias_generadas = sugerencias_por_rubro(rubro_id_final)
        respuesta_sugerencias_base = "No encontré una respuesta directa para tu consulta. Quizás puedas intentar preguntando algo como: " + " · ".join(f"“{s}”" for s in sugerencias_generadas if s)
        respuesta_sugerencias_procesada = reemplazar_placeholders(respuesta_sugerencias_base, user_obj)

        pyme_link_web_actual_fallback = getattr(user_obj, "link_web", "")
        if pyme_link_web_actual_fallback:
            link_absoluto_sug = pyme_link_web_actual_fallback
            if not link_absoluto_sug.startswith("http"): link_absoluto_sug = "https://" + link_absoluto_sug
            link_html_sug = (
                f'\n<div style="margin-top: 10px; font-size: 0.9em;">'
                f'También puedes <a href="{link_absoluto_sug}" target="_blank">visitar nuestra tienda online</a> para más información.'
                '</div>'
            )
            respuesta_sugerencias_procesada += link_html_sug

        session[NOMBRE_HISTORIAL_SESION].append({"role": "user", "content": pregunta}) 
        session.modified = True # Guardar la pregunta del usuario incluso si el bot da sugerencias
        return {"respuesta": respuesta_sugerencias_procesada, "fuente": "sugerencia_sistema"}

# --- FIN DE responder_chatboc ---