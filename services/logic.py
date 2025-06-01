# logic.py
import logging
import random
from flask import session
from sqlalchemy import func # Importar func para búsquedas case-insensitive
from models import User, Rubro, Sugerencia, Conversacion # Asegúrate que Sugerencia esté definido en models.py
from extensions import db
import re 
import json # Necesario para parsear horario_json

# Configuración del logger para este módulo
logger = logging.getLogger(__name__)

# --- Funciones Auxiliares ---

def sugerencias_por_rubro(rubro_id: int) -> list:
    try:
        sugerencias_obj = Sugerencia.query.filter_by(rubro_id=rubro_id).all()
        if sugerencias_obj:
            todas = [s.texto for s in sugerencias_obj]
            logger.info(f"Sugerencias para rubro {rubro_id}: {len(todas)}")
            return random.sample(todas, min(5, len(todas)))
        
        # Fallback a rubro general (ID 1), solo si no es ya el rubro 1 para evitar bucles si 1 no tiene
        if rubro_id != 1: 
            rubro_general_id = 1 # Asumir que el ID 1 es 'general'
            logger.info(f"No se encontraron sugerencias para rubro {rubro_id}, intentando fallback a rubro general ID {rubro_general_id}")
            fallback_obj = Sugerencia.query.filter_by(rubro_id=rubro_general_id).all()
            if fallback_obj:
                return random.sample([s.texto for s in fallback_obj], min(5, len(fallback_obj)))
        
        logger.info(f"No se encontraron sugerencias para rubro {rubro_id} ni en fallback general. Usando defaults.")
        return ["¿En qué más te puedo ayudar?", "Consulta nuestros productos principales.", "Háblame un poco más sobre lo que buscas."]
    except Exception as e:
        logger.error(f"Error buscando sugerencias: {e}", exc_info=True)
        return ["Disculpa, tuve un problema al buscar sugerencias en este momento."]

def formatear_numero_whatsapp_simple(telefono_str: str, codigo_pais: str = "54") -> str:
    if not telefono_str:
        return ""
    numeros = re.sub(r'\D', '', str(telefono_str)) # Asegurar que es string y eliminar no dígitos

    # Caso 1: Ya tiene el formato internacional de móvil argentino completo (ej. 5492611234567)
    if numeros.startswith(codigo_pais + "9") and len(numeros) == (len(codigo_pais) + 1 + 10): # 54 + 9 + 10 digitos
        return numeros
    
    # Caso 2: Tiene código de país pero le falta el '9' de móvil (ej. 542611234567)
    if numeros.startswith(codigo_pais) and not numeros.startswith(codigo_pais + "9") and len(numeros) == (len(codigo_pais) + 10):
        return codigo_pais + "9" + numeros[len(codigo_pais):]

    # Caso 3: Número local de 10 dígitos (característica + número, ej. 2611234567)
    if len(numeros) == 10:
        return f"{codigo_pais}9{numeros}"
        
    logger.warning(f"Número de teléfono '{telefono_str}' no pudo ser formateado a un estándar de WhatsApp claro, devolviendo dígitos limpios: '{numeros}'")
    return numeros


def reemplazar_placeholders(texto: str, user_obj) -> str:
    if not texto or not isinstance(texto, str): # Asegurar que texto sea un string
        return ""

    # Placeholders estándar
    placeholders_conocidos = {
        "[nombreEmpresa]": "nombre_empresa",
        "[linkWeb]": "link_web",
        "[telefono]": "telefono",
        "[direccion]": "direccion",
        "[horario]": "horario",      # String simple de horario
        "[ubicacion]": "ubicacion",  # Podría ser redundante si usamos provincia/ciudad
        "[ciudad]" : "ciudad",
        "[provincia]": "provincia",
        "[pais]": "pais",
        "[horarioDetallado]": "horario_json", # Para el horario estructurado
    }

    defaults_textos = {
        "nombre_empresa": "nuestra empresa", "link_web": "nuestro sitio web",
        "telefono": "nuestro número de contacto", "direccion": "nuestra dirección",
        "horario": "nuestro horario de atención", "ubicacion": "nuestra área de servicio",
        "ciudad": "nuestra ciudad", "provincia": "nuestra provincia", "pais": "nuestro país",
        "horario_json": "consultar nuestros horarios detallados",
    }

    texto_procesado = texto

    for ph_template, attr_key in placeholders_conocidos.items():
        valor_atributo = None
        if user_obj:
            # Para horario_json, si user_obj es un modelo User, la @property ya lo parsea.
            # Si es AnonUser/GenericAnonUser, 'horario_json' es un string.
            # El campo 'horario' (simple string) se obtiene directamente.
            if attr_key == "horario_json" and isinstance(user_obj, User): # Modelo User de BD
                valor_atributo = user_obj.horario_json # Esto llama a la @property -> devuelve dict o None
            elif attr_key == "horario" and isinstance(user_obj, User): # Modelo User de BD
                 valor_atributo = user_obj.horario # El string JSON original
            else: # Para AnonUser, GenericAnonUser, u otros atributos
                valor_atributo = getattr(user_obj, attr_key, None)
        
        valor_para_reemplazo = defaults_textos.get(attr_key, "")

        if valor_atributo is not None:
            if ph_template == "[telefono]":
                if str(valor_atributo).strip():
                    numero_wsp = formatear_numero_whatsapp_simple(str(valor_atributo))
                    if numero_wsp:
                        valor_para_reemplazo = f'{str(valor_atributo)} (<a href="https://wa.me/{numero_wsp}" target="_blank" style="color: green; text-decoration: underline; font-weight:bold;">Contactar por WhatsApp</a>)'
                    else:
                        valor_para_reemplazo = str(valor_atributo) # Solo el número si no se pudo formatear
                else: # Telefono está vacío
                     valor_para_reemplazo = defaults_textos.get(attr_key, "nuestro número de contacto")
            
            elif ph_template == "[linkWeb]":
                if str(valor_atributo).strip():
                    link_abs = str(valor_atributo)
                    if not link_abs.startswith("http://") and not link_abs.startswith("https://"):
                        link_abs = "https://" + link_abs
                    valor_para_reemplazo = link_abs
                else: # LinkWeb está vacío
                    valor_para_reemplazo = defaults_textos.get(attr_key, "nuestro sitio web")

            elif ph_template == "[horarioDetallado]":
                # 'valor_atributo' aquí puede ser un dict (de User.horario_json) o un string JSON (de AnonUser.horario_json)
                horarios_data = None
                if isinstance(valor_atributo, dict): # Ya parseado por la @property de User
                    horarios_data = valor_atributo
                elif isinstance(valor_atributo, str) and valor_atributo.strip() and valor_atributo.strip() != '[]':
                    try:
                        horarios_data = json.loads(valor_atributo)
                    except json.JSONDecodeError:
                        logger.warning(f"Error parseando horario_json (string) para placeholder: {valor_atributo}")
                        horarios_data = None
                
                if horarios_data and isinstance(horarios_data, list): # Asegurar que sea una lista
                    texto_horario_formateado = ""
                    # Asumimos que horarios_data es una lista de dicts con 'dia', 'abre', 'cierra', 'cerrado'
                    # como se envía desde el frontend Perfil.tsx
                    for dia_data in horarios_data:
                        dia_nombre = dia_data.get("dia", "Día desconocido")
                        if dia_data.get("cerrado"):
                            texto_horario_formateado += f"{dia_nombre}: Cerrado. "
                        else:
                            abre = dia_data.get('abre','--:--')
                            cierra = dia_data.get('cierra','--:--')
                            texto_horario_formateado += f"{dia_nombre}: {abre} - {cierra}. "
                    valor_para_reemplazo = texto_horario_formateado.strip() if texto_horario_formateado else defaults_textos.get(attr_key)
                else: # No hay datos de horario o no es lista, usar el string simple o default
                    texto_fallback_horario = getattr(user_obj, "horario", defaults_textos.get("horario")) # String simple
                    valor_para_reemplazo = str(texto_fallback_horario) if texto_fallback_horario else defaults_textos.get(attr_key)
            
            elif isinstance(valor_atributo, str) and valor_atributo.strip() == "":
                # Si el atributo existe pero es un string vacío, usar el default
                valor_para_reemplazo = defaults_textos.get(attr_key, "")
            elif not isinstance(valor_atributo, (dict, list)): # Para otros atributos que no son dict/list
                valor_para_reemplazo = str(valor_atributo)
            # Si es dict o list y no es un placeholder especial, no se reemplaza (o se define cómo)
            
        texto_procesado = texto_procesado.replace(ph_template, valor_para_reemplazo)

    # Fallback para placeholders desconocidos [AlgoEntreCorchetes]
    def reemplazar_desconocido_callback(match):
        placeholder_interno = match.group(1)
        logger.warning(f"Placeholder desconocido encontrado: [{placeholder_interno}]")
        if "precio" in placeholder_interno.lower() or "costo" in placeholder_interno.lower():
            return "(precio a consultar)"
        elif "link" in placeholder_interno.lower() or "url" in placeholder_interno.lower():
            link_web_general = getattr(user_obj, "link_web", "") if user_obj else ""
            if link_web_general and not str(link_web_general).startswith("http"):
                link_web_general = "https://" + str(link_web_general)
            return f"(visita nuestro sitio web{': ' + str(link_web_general) if link_web_general else ''} para más información)"
        return "" 
    texto_procesado = re.sub(r"\[([^\]\[]+)\]", reemplazar_desconocido_callback, texto_procesado)
    return texto_procesado

# --- Clases para usuarios anónimos (definidas a nivel de módulo o al inicio de la función) ---
class _BaseAnonUser:
    """Clase base para usuarios anónimos para compartir atributos default."""
    nombre_empresa = "la tienda"
    plan = "anonimo"
    rubro_id = None
    link_web = ""
    telefono = ""
    direccion = ""
    horario = "horario de atención habitual" # String simple por defecto
    horario_json = '[]' # String JSON vacío por defecto (lista vacía)
    ubicacion = "" # Considerar si es necesario o se usan ciudad/provincia
    ciudad = ""
    provincia = ""
    pais = "Argentina" # Default
    latitud = None
    longitud = None
    id = None # MUY IMPORTANTE: usuarios anónimos no tienen ID de BD
    # Atributos necesarios para la @property horario_json si se llamara desde AnonUser
    # (aunque reemplazar_placeholders ya lo maneja)
    # def horario_json(self): return json.loads(self.horario) if self.horario else None

class AnonUserPymeDemo(_BaseAnonUser):
    def __init__(self, preguntas_realizadas_sesion):
        self.nombre_empresa = "Chatboc Demostración"
        self.plan = "demo_pyme"
        self.preguntas_usadas = preguntas_realizadas_sesion
        self.limite_preguntas = 15 # Límite específico para demo de PYME anónima
        # Datos de ejemplo más específicos para la demo:
        self.link_web = "https://www.chatboc.ar" 
        self.telefono = "+549111234567" 
        self.direccion = "Av. Corrientes 1234, CABA"
        self.ciudad = "CABA"
        self.provincia = "CABA"
        self.horario = "Lunes a Viernes de 9hs a 18hs. Sábados de 9hs a 13hs."
        self.horario_json = json.dumps([ # Estructura como la espera el frontend y reemplazar_placeholders
            {"dia": "Lunes", "abre": "09:00", "cierra": "18:00", "cerrado": False},
            {"dia": "Martes", "abre": "09:00", "cierra": "18:00", "cerrado": False},
            {"dia": "Miércoles", "abre": "09:00", "cierra": "18:00", "cerrado": False},
            {"dia": "Jueves", "abre": "09:00", "cierra": "18:00", "cerrado": False},
            {"dia": "Viernes", "abre": "09:00", "cierra": "18:00", "cerrado": False},
            {"dia": "Sábado", "abre": "09:00", "cierra": "13:00", "cerrado": False},
            {"dia": "Domingo", "abre": "", "cierra": "", "cerrado": True}
        ])
        self.latitud = -34.6037 # Buenos Aires
        self.longitud = -58.3816 # Buenos Aires
        # rubro_id se determinará por rubro_nombre_frontend

class GenericAnonUser(_BaseAnonUser):
    def __init__(self):
        super().__init__() # Hereda los defaults de _BaseAnonUser
        # GenericAnonUser puede tener menos overrides o ser más general
        self.plan = "anonimo_general"
        self.preguntas_usadas = session.get("generic_anon_preguntas", 0) # Podría tener su propio contador
        self.limite_preguntas = 5 # Límite más bajo para anónimos genéricos


# --- COMIENZO DE responder_chatboc ---
def responder_chatboc(pregunta: str, token: str, rubro_nombre_frontend: str = None):
    logger.info(f"▶️ Inicio responder_chatboc: pregunta='{pregunta}' token='{token}' rubro_frontend='{rubro_nombre_frontend}'")

    if not pregunta or not pregunta.strip():
        logger.warning("Pregunta vacía recibida")
        return {"error": "Falta la pregunta"}

    NOMBRE_HISTORIAL_SESION = 'historial_chat_cliente'
    if NOMBRE_HISTORIAL_SESION not in session:
        session[NOMBRE_HISTORIAL_SESION] = []
        logger.info(f"Inicializando '{NOMBRE_HISTORIAL_SESION}' en flask.session")

    user = None 
    rubro_id_final = 1 # Default a rubro general (ID 1)
    rubro_nombre_final = "general"
    
    # Determinar tipo de usuario
    is_pyme_demo_anon = token is not None and token.startswith("demo-anon") # Token específico para la demo del widget en chatboc.ar
    is_usuario_registrado = False

    if is_pyme_demo_anon:
        logger.info("Modo Demo PYME Anónimo (token demo-anon) detectado.")
        session.setdefault("pyme_demo_anon_preguntas", 0)
        if session["pyme_demo_anon_preguntas"] >= 15: # Límite para este tipo de demo
            return {"respuesta": "🔒 Límite de 15 preguntas en modo demo PYME alcanzado. Registrate para seguir.", "fuente": "sistema"}
        session["pyme_demo_anon_preguntas"] += 1
        user = AnonUserPymeDemo(session["pyme_demo_anon_preguntas"]) # Instancia de la clase definida arriba
        # El rubro para AnonUserPymeDemo se intentará tomar de rubro_nombre_frontend
    
    elif token: # Hay un token, podría ser un usuario registrado
        db_user = User.query.filter_by(token=token).first()
        if db_user:
            is_usuario_registrado = True
            user = db_user
            logger.info(f"Usuario PYME registrado autenticado: {user.email}")
            if user.preguntas_usadas >= user.limite_preguntas:
                return {"respuesta": "🔒 Límite de preguntas alcanzado en tu plan. Actualizá para más.", "fuente": "sistema"}
        else:
            logger.warning(f"Token '{token[:15]}...' proporcionado pero no corresponde a un usuario registrado ni a demo-anon. Tratando como anónimo general.")
            # Si el token no es válido, se procede como anónimo general (user sigue None por ahora)
            pass # Se instanciará GenericAnonUser más abajo si user sigue None

    if user is None: # Si no es pyme_demo_anon ni usuario registrado válido, es anónimo genérico
        logger.info("Usuario es Anónimo Genérico (sin token válido o sin token).")
        session.setdefault("generic_anon_preguntas", 0) # Contador para anónimos genéricos
        if session["generic_anon_preguntas"] >= 5:
             return {"respuesta": "Alcanzaste el límite de preguntas para usuarios anónimos. ¡Registrate gratis para continuar!", "fuente": "sistema"}
        session["generic_anon_preguntas"] += 1
        user = GenericAnonUser() # Instancia de la clase definida arriba

    # Determinar Rubro Final a usar para la consulta
    rubro_obj_seleccionado = None
    if rubro_nombre_frontend:
        logger.info(f"Intentando determinar rubro por parámetro frontend: '{rubro_nombre_frontend}'")
        rubro_obj_seleccionado = Rubro.query.filter(func.lower(Rubro.nombre) == rubro_nombre_frontend.lower().strip()).first()
        if rubro_obj_seleccionado:
            logger.info(f"Rubro determinado por frontend: {rubro_obj_seleccionado.nombre} (ID: {rubro_obj_seleccionado.id})")
            # Si el usuario es anónimo genérico, actualizamos su contexto con datos del rubro si es posible
            if isinstance(user, GenericAnonUser):
                user.nombre_empresa = getattr(rubro_obj_seleccionado, 'nombre_pyme_default_para_anon', f"la sección de {rubro_obj_seleccionado.nombre}")
                user.rubro_id = rubro_obj_seleccionado.id # Aunque es anónimo, podemos saber el rubro_id del contexto
                # Aquí podrías cargar más datos default del Rubro al GenericAnonUser si los tuvieras en el modelo Rubro
        else:
            logger.warning(f"Rubro '{rubro_nombre_frontend}' (frontend) no encontrado. Se usará el rubro del usuario o general.")

    if not rubro_obj_seleccionado and hasattr(user, "rubro_id") and user.rubro_id: # Si no se determinó por frontend, usar el del User (registrado o AnonUserPymeDemo si tuviera rubro_id)
        logger.info(f"Usando rubro_id del objeto User: {user.rubro_id}")
        rubro_obj_seleccionado = db.session.get(Rubro, user.rubro_id) # Usar db.session.get para PK
        if not rubro_obj_seleccionado:
            logger.warning(f"Rubro ID {user.rubro_id} del usuario no encontrado. Se usará general.")
            # rubro_obj_seleccionado se quedará como None, y se usará el default general

    if rubro_obj_seleccionado:
        rubro_id_final = rubro_obj_seleccionado.id
        rubro_nombre_final = rubro_obj_seleccionado.nombre.lower().strip()
    else: # Fallback final a rubro general si todo lo demás falla
        logger.info(f"No se pudo determinar un rubro específico. Usando rubro general ID {rubro_id_final}.")
        rubro_general_obj_default = db.session.get(Rubro, rubro_id_final) # Usar db.session.get
        if rubro_general_obj_default:
            rubro_nombre_final = rubro_general_obj_default.nombre.lower().strip()
        else: # Esto no debería pasar si el rubro general ID 1 existe
            logger.error(f"¡ERROR CRÍTICO! No se encontró el rubro general ID {rubro_id_final}.")
            rubro_nombre_final = "general" # Super fallback

    logger.info(f"Contexto PYME final para esta solicitud: Empresa: {getattr(user, 'nombre_empresa', 'N/A')} | Rubro: {rubro_nombre_final} (ID: {rubro_id_final})")
    
    # ----- Construcción de Mensajes y Contexto para LLM -----
    # (El resto de la lógica de mensajes_para_llm, contexto_catalogo, user_profile_context, y prompt_sistema_texto)
    # ...
    
    # Importante: Ajustar cómo se obtiene `pyme_horario_json_str` en user_profile_context
    # porque `user.horario_json` (la @property) devuelve un dict o None, no un string JSON.
    # Necesitamos el string JSON original que está en `user.horario` para el modelo User de BD.

    pyme_nombre_empresa = getattr(user, "nombre_empresa", "la tienda")
    pyme_link_web = getattr(user, "link_web", "")
    pyme_telefono = getattr(user, "telefono", "") 
    pyme_direccion = getattr(user, "direccion", "")
    pyme_ciudad = getattr(user, "ciudad", "")
    pyme_provincia = getattr(user, "provincia", getattr(user, "ubicacion", "")) 
    pyme_horario_str_simple = getattr(user, "horario", "nuestro horario de atención") 
    
    # Obtener el string JSON para el horario:
    if isinstance(user, User): # Si es un usuario de la BD
        pyme_horario_json_str = user.horario if user.horario else '[]' # Tomar el string directamente de la BD
    else: # Para AnonUserPymeDemo o GenericAnonUser
        pyme_horario_json_str = getattr(user, "horario_json", "[]") # Estos ya tienen un string JSON

    user_profile_context = {
        "nombre_empresa": pyme_nombre_empresa,
        "rubro_nombre": rubro_nombre_final, # Usar el rubro_nombre_final determinado
        "telefono_raw": pyme_telefono,
        "link_web": pyme_link_web,
        "direccion_completa": f"{pyme_direccion}, {pyme_ciudad}, {pyme_provincia}".strip(', ').strip(),
        "horario_str": pyme_horario_str_simple,
        "horario_json_str": pyme_horario_json_str, 
    }
    
    # (El resto de tu lógica para construir el prompt_sistema_texto, llamar a Cohere, etc.)
    # ... esta parte del código parece extensa y no la has pegado completa, pero la idea general es:
    # Tu lógica de formatear horarios para el prompt, construir el prompt_sistema_texto,
    # llamar a Cohere, y luego los fallbacks a FAQ e Intents.
    
    # Ejemplo de cómo podría continuar (simplificado):
    MAX_MENSAJES_HISTORIAL_PARA_LLM = 10 
    historial_actual_cliente = session.get(NOMBRE_HISTORIAL_SESION, [])[:]
    
    mensajes_para_llm = []
    if len(historial_actual_cliente) > MAX_MENSAJES_HISTORIAL_PARA_LLM:
        mensajes_para_llm = historial_actual_cliente[-MAX_MENSAJES_HISTORIAL_PARA_LLM:]
    else:
        mensajes_para_llm = historial_actual_cliente
    mensajes_para_llm.append({"role": "user", "content": pregunta})

    contexto_catalogo = ""
    if is_usuario_registrado and user.id is not None: # Solo buscar catálogo para usuarios PYME registrados
        try:
            from services.qdrant_search import buscar_catalogo_qdrant, armar_respuesta_legible
            logger.info(f"Buscando catálogo en Qdrant para user_id (PYME): {user.id}...")
            resultados_qdrant = buscar_catalogo_qdrant(user.id, pregunta, limite=5)
            contexto_catalogo = armar_respuesta_legible(resultados_qdrant)
            if contexto_catalogo:
                logger.info(f"Contexto Qdrant (primeros 150 chars): {contexto_catalogo[:150]}...")
            else:
                logger.info("No se encontró contexto de catálogo en Qdrant para esta pregunta.")
        except ImportError:
            logger.error("Módulo Qdrant (services.qdrant_search) no encontrado.")
        except Exception as e_qdrant:
            logger.error(f"Error buscando catálogo en Qdrant: {e_qdrant}", exc_info=True)
    
    # --- Formatear horarios detallados para el prompt (si existen y son válidos) ---
    horarios_para_prompt = user_profile_context['horario_str'] # Default al string simple
    try:
        if user_profile_context['horario_json_str'] and user_profile_context['horario_json_str'] != '[]':
            # La validación de la estructura del JSON ya se hizo en el frontend y al guardar en /perfil.
            # Aquí confiamos un poco más o hacemos una validación más ligera si es necesario.
            horarios_data = json.loads(user_profile_context['horario_json_str']) # Debería ser una lista de dicts
            partes_horario = []
            if isinstance(horarios_data, list):
                for dia_data in horarios_data: # Asumimos que dia_data es un dict
                    dia_nombre = dia_data.get("dia", "Día") # El frontend ahora envía "dia"
                    if dia_data.get("cerrado"):
                        partes_horario.append(f"{dia_nombre}: Cerrado")
                    else:
                        abre = dia_data.get('abre','--:--')
                        cierra = dia_data.get('cierra','--:--')
                        partes_horario.append(f"{dia_nombre}: de {abre} a {cierra}")
            if partes_horario:
                horarios_para_prompt = ". ".join(partes_horario) + "."
                logger.info(f"Horarios formateados para prompt: {horarios_para_prompt}")
    except Exception as e_json_horario_prompt:
        logger.warning(f"No se pudo parsear o formatear horario_json para el prompt: {e_json_horario_prompt}. Usando horario_str.")
    
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
        f"\n- Horarios de Atención: {horarios_para_prompt if horarios_para_prompt.strip('.') else 'Consultar nuestros horarios.'}"
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
    if contexto_catalogo:
        prompt_sistema_texto += f"\n\nINFORMACIÓN DEL CATÁLOGO PARA ESTA CONSULTA (usa solo lo relevante y sé breve):\n---\n{contexto_catalogo}\n---\nUsa esta información del catálogo para responder, siguiendo las instrucciones sobre productos, precios y brevedad que te di."
    else:
        prompt_sistema_texto += "\nNo encontré información específica en el catálogo para esta consulta. Intenta ayudar al cliente de forma concisa con tu conocimiento general sobre los productos/servicios del rubro, o pide más detalles."
    prompt_sistema_texto += "\n\nInicia tu respuesta directamente al cliente, continuando la conversación de forma natural y concisa."

    mensajes_finales_para_llm = [{"role": "system", "content": prompt_sistema_texto}] + mensajes_para_llm
    
    respuesta_obtenida_llm = "" 
    fuente_respuesta = "desconocida"

    try:
        from services.cohere_ai import get_cohere_response
        logger.info(f"Enviando {len(mensajes_finales_para_llm)} mensajes a Cohere. Prompt sistema longitud: {len(prompt_sistema_texto)}.")
        respuesta_obtenida_llm = get_cohere_response(mensajes_finales_para_llm, rubro_id=rubro_id_final, user_context=user_profile_context)
        if respuesta_obtenida_llm and len(respuesta_obtenida_llm.strip()) > 3:
            fuente_respuesta = "cohere"
            logger.info(f"Respuesta de Cohere (cruda, antes de placeholders): {respuesta_obtenida_llm[:200]}...")
        else:
            logger.warning("Respuesta de Cohere vacía o muy corta.")
            respuesta_obtenida_llm = "" 
    except ImportError:
        logger.error("Módulo Cohere (services.cohere_ai.get_cohere_response) no encontrado.")
    except Exception as e_cohere:
        logger.error(f"Error al llamar a Cohere: {e_cohere}", exc_info=True)

    respuesta_final_procesada = ""

    if respuesta_obtenida_llm:
        respuesta_final_procesada = reemplazar_placeholders(respuesta_obtenida_llm, user)
    else: 
        logger.info("Cohere no dio respuesta o fue inválida. Intentando backups (FAQ, Intents)...")
        # ... (tu lógica de fallback a FAQ e Intents) ...
        # Asegúrate que esta parte también use 'user' (que es el objeto User, AnonUserPymeDemo o GenericAnonUser)
        # para reemplazar_placeholders.
        # Ejemplo:
        # if not respuesta_final_procesada:
        #     from services.faq_matcher_spacy import buscar_en_faq_spacy
        #     # ... buscar_en_faq_spacy ...
        #     if faq_match:
        #         respuesta_final_procesada = reemplazar_placeholders(faq_match.answer, user) 
        #         fuente_respuesta = "faq"
        pass # Placeholder para tu lógica de fallback

    if respuesta_final_procesada:
        session[NOMBRE_HISTORIAL_SESION].append({"role": "user", "content": pregunta})
        session[NOMBRE_HISTORIAL_SESION].append({"role": "assistant", "content": respuesta_final_procesada})
        
        MAX_HISTORIAL_EN_SESION = 20 
        if len(session[NOMBRE_HISTORIAL_SESION]) > MAX_HISTORIAL_EN_SESION:
            session[NOMBRE_HISTORIAL_SESION] = session[NOMBRE_HISTORIAL_SESION][-MAX_HISTORIAL_EN_SESION:]
        session.modified = True
        logger.info(f"Historial de sesión actualizado. Tamaño: {len(session[NOMBRE_HISTORIAL_SESION])}")

        if is_usuario_registrado: # Solo para usuarios PYME reales
            try:
                user.preguntas_usadas = (user.preguntas_usadas or 0) + 1 # Asegurar que no sea None
                db.session.add(Conversacion(user_id=user.id, pregunta=pregunta, respuesta=respuesta_final_procesada, fuente=fuente_respuesta, rubro=rubro_nombre_final))
                db.session.commit()
                logger.info("Conversación guardada en BD para usuario PYME.")
            except Exception as e_db_conv:
                logger.error(f"Error guardando conversación en DB: {e_db_conv}", exc_info=True)
                db.session.rollback()
        
        respuesta_para_frontend = respuesta_final_procesada
        pyme_link_web_actual = getattr(user, "link_web", "")
        if pyme_link_web_actual:
            link_absoluto = pyme_link_web_actual
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
        
        return {"respuesta": respuesta_para_frontend, "nivel_usado": rubro_nombre_final, "fuente": fuente_respuesta}
    else: 
        sugerencias_generadas = sugerencias_por_rubro(rubro_id_final)
        # ... (tu lógica de fallback final a sugerencias del sistema) ...
        # Asegúrate que reemplazar_placeholders aquí también use 'user'.
        respuesta_sugerencias_base = "No encontré una respuesta directa para tu consulta. Quizás puedas intentar preguntando algo como: " + " · ".join(f"“{s}”" for s in sugerencias_generadas)
        respuesta_sugerencias_procesada = reemplazar_placeholders(respuesta_sugerencias_base, user)

        # ... (añadir link a tienda online si existe) ...
        session[NOMBRE_HISTORIAL_SESION].append({"role": "user", "content": pregunta})
        session.modified = True
        return {"respuesta": respuesta_sugerencias_procesada, "fuente": "sugerencia_sistema"}

# --- FIN DE responder_chatboc ---