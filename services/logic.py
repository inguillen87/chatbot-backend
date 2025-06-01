# services/logic.py
import logging
import random
from flask import session
from sqlalchemy import func # Para búsquedas case-insensitive en Rubro
from models import User, Rubro, Sugerencia, Conversacion # Asegúrate que todos los modelos estén correctamente importados
from extensions import db
import re 
import json 
from typing import Any # Para tipado más genérico de user_obj

# Configuración del logger para este módulo. 
# Asegúrate de que el nivel de logging de tu app Flask en Render esté en DEBUG para ver todos estos mensajes.
logger = logging.getLogger(__name__)
# Si quieres forzar la aparición de todos estos logs sin cambiar la config general de Flask:
# logger.setLevel(logging.DEBUG) # Opcional: Descomenta para forzar DEBUG solo para este módulo
# logging.basicConfig(level=logging.DEBUG) # Otra opción, pero puede afectar otros loggers

# --- Clases para usuarios anónimos (definidas a nivel de módulo) ---
class _BaseAnonUser:
    nombre_empresa: str = "la tienda"
    plan: str = "anonimo"
    rubro_id: Optional[int] = None
    link_web: str = ""
    telefono: str = ""
    direccion: str = ""
    horario: str = "horario de atención habitual" 
    horario_json: str = '[]' # String JSON representando una lista vacía
    ciudad: str = ""
    provincia: str = ""
    pais: str = "Argentina" 
    latitud: Optional[float] = None
    longitud: Optional[float] = None
    id: Optional[int] = None 

class AnonUserPymeDemo(_BaseAnonUser):
    def __init__(self, preguntas_realizadas_sesion: int):
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
        self.horario = "Lunes a Viernes de 9hs a 18hs. Sábados de 9hs a 13hs."
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
        self.preguntas_usadas = session.get("generic_anon_preguntas", 0)
        self.limite_preguntas = 5
# --- Fin Clases Anónimas ---

# --- Funciones Auxiliares ---
def sugerencias_por_rubro(rubro_id: int) -> list:
    # ... (Tu función sugerencias_por_rubro) ...
    # (Asegúrate que use logger.info, logger.error como en mis ejemplos anteriores si quieres logs aquí)
    try:
        sugerencias_obj = Sugerencia.query.filter_by(rubro_id=rubro_id).all()
        if sugerencias_obj:
            todas = [s.texto for s in sugerencias_obj]
            logger.debug(f"Sugerencias para rubro {rubro_id} (total: {len(todas)}): {todas[:5]}") # DEBUG para no llenar logs
            return random.sample(todas, min(3, len(todas))) # Reducido a 3 para brevedad en fallback
        if rubro_id != 1: 
            fallback_obj = Sugerencia.query.filter_by(rubro_id=1).all()
            if fallback_obj:
                logger.info(f"No se encontraron sugerencias para rubro {rubro_id}, usando fallback general.")
                return random.sample([s.texto for s in fallback_obj], min(3, len(fallback_obj)))
        logger.info(f"No se encontraron sugerencias para rubro {rubro_id} ni en fallback general. Usando defaults predefinidos.")
        return ["¿Cuáles son sus horarios de atención?", "¿Qué productos ofrecen?", "¿Cómo puedo realizar una compra?"]
    except Exception as e:
        logger.error(f"Error buscando sugerencias para rubro_id {rubro_id}: {e}", exc_info=True)
        return ["Disculpa, tuve un problema al buscar sugerencias en este momento."]


def formatear_numero_whatsapp_simple(telefono_str: str, codigo_pais: str = "54") -> str:
    # ... (Tu función formatear_numero_whatsapp_simple) ...
    if not telefono_str: return ""
    numeros = re.sub(r'\D', '', str(telefono_str))
    if numeros.startswith(codigo_pais + "9") and len(numeros) == (len(codigo_pais) + 1 + 10): return numeros
    if numeros.startswith(codigo_pais) and not numeros.startswith(codigo_pais + "9") and len(numeros) == (len(codigo_pais) + 10): return codigo_pais + "9" + numeros[len(codigo_pais):]
    if len(numeros) == 10: return f"{codigo_pais}9{numeros}"
    logger.debug(f"Número '{telefono_str}' no pudo ser formateado a un estándar de WhatsApp claro, devolviendo solo dígitos: '{numeros}'")
    return numeros

def reemplazar_placeholders(texto: str, user_obj: Any) -> str:
    if not texto or not isinstance(texto, str): return ""
    # ... (Definición de placeholders_conocidos y defaults_textos como antes) ...
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
        "horario_json": "consultar nuestros horarios detallados",
    }
    texto_procesado = texto
    for ph_template, attr_key in placeholders_conocidos.items():
        valor_atributo = None
        valor_para_reemplazo = defaults_textos.get(attr_key, f"[{attr_key}]") # Devolver el placeholder si no hay default
        
        if user_obj:
            if attr_key == "horario_json": 
                if isinstance(user_obj, User): 
                    valor_atributo = user_obj.horario_json # Llama a la @property -> dict o None
                else: 
                    valor_atributo = getattr(user_obj, attr_key, None) # De AnonUser* es un string JSON
            elif attr_key == "horario" and isinstance(user_obj, User): # Si específicamente se pide [horario] para un User de BD
                 valor_atributo = user_obj.horario # Este es el string JSON original de la BD
            else: # Para otros atributos o para AnonUsers pidiendo [horario]
                valor_atributo = getattr(user_obj, attr_key, None)

        if valor_atributo is not None:
            # Lógica específica de reemplazo para cada placeholder
            if ph_template == "[telefono]":
                # ... (lógica de formateo y link de WhatsApp) ...
                tel_str = str(valor_atributo).strip()
                if tel_str:
                    numero_wsp = formatear_numero_whatsapp_simple(tel_str)
                    valor_para_reemplazo = f'{tel_str} (<a href="https://wa.me/{numero_wsp}" target="_blank" style="color: green; text-decoration: underline; font-weight:bold;">Contactar por WhatsApp</a>)' if numero_wsp else tel_str
                else: valor_para_reemplazo = defaults_textos.get(attr_key, "")


            elif ph_template == "[linkWeb]":
                # ... (lógica de linkWeb) ...
                link_str = str(valor_atributo).strip()
                if link_str:
                    link_abs = link_str
                    if not link_abs.startswith("http"): link_abs = "https://" + link_abs
                    valor_para_reemplazo = link_abs
                else: valor_para_reemplazo = defaults_textos.get(attr_key, "")


            elif ph_template == "[horarioDetallado]":
                horarios_data_parseada = None
                if isinstance(valor_atributo, dict) or isinstance(valor_atributo, list): # Si ya es un dict o list (de User.horario_json o si el mock user ya lo tiene parseado)
                    horarios_data_parseada = valor_atributo
                elif isinstance(valor_atributo, str) and valor_atributo.strip() and valor_atributo not in ['[]', '{}', 'null']:
                    try: horarios_data_parseada = json.loads(valor_atributo)
                    except json.JSONDecodeError: 
                        logger.warning(f"Error parseando string de '{attr_key}' ('{valor_atributo}') para placeholder [horarioDetallado]. User type: {type(user_obj).__name__}")
                
                if horarios_data_parseada and isinstance(horarios_data_parseada, list) and all(isinstance(h, dict) for h in horarios_data_parseada):
                    partes = []
                    for h_dict in horarios_data_parseada:
                        dia = h_dict.get('dia', '') # Esperar 'dia'
                        if h_dict.get('cerrado'):
                            partes.append(f"{dia}: Cerrado")
                        else:
                            abre = h_dict.get('abre','--:--')
                            cierra = h_dict.get('cierra','--:--')
                            partes.append(f"{dia}: de {abre} a {cierra}")
                    valor_para_reemplazo = ". ".join(p for p in partes if p) + ("." if partes else "")
                    if not valor_para_reemplazo.strip() or valor_para_reemplazo == ".": valor_para_reemplazo = defaults_textos.get(attr_key)
                else: 
                    logger.debug(f"No se pudo formatear horario detallado para {ph_template} con valor '{valor_atributo}'. Usando fallback al campo 'horario' simple.")
                    valor_para_reemplazo = str(getattr(user_obj, "horario", defaults_textos.get("horario"))) # Fallback al string simple de horario

            elif isinstance(valor_atributo, str) and valor_atributo.strip() == "": # Atributo existe pero es string vacío
                valor_para_reemplazo = defaults_textos.get(attr_key, "") # Usar default si existe
            elif not isinstance(valor_atributo, (dict, list)): # Para otros atributos que no son dict/list
                valor_para_reemplazo = str(valor_atributo)
            # Nota: si valor_atributo es un dict/list no manejado específicamente, se usa el default.

        texto_procesado = texto_procesado.replace(ph_template, valor_para_reemplazo)
    
    def reemplazar_desconocido_callback(match):
        placeholder_interno = match.group(1)
        logger.warning(f"Placeholder desconocido encontrado y eliminado: [{placeholder_interno}]")
        return "" 
    texto_procesado = re.sub(r"\[([^\]\[\s]+?)\]", reemplazar_desconocido_callback, texto_procesado)
    return texto_procesado
# --- FIN Funciones Auxiliares ---


# --- COMIENZO DE responder_chatboc ---
def responder_chatboc(pregunta: str, token: str | None, rubro_nombre_frontend: str | None = None):
    # ----- LOGS INICIALES -----
    logger.info(f"▶️ Inicio responder_chatboc: Pregunta='{pregunta[:100]}...' Token='{str(token)[:15] if token else 'N/A'}' RubroFrontend='{rubro_nombre_frontend}'")

    if not pregunta or not pregunta.strip():
        logger.warning("Pregunta vacía recibida, no se procesará.")
        return {"error": "La pregunta no puede estar vacía."}

    NOMBRE_HISTORIAL_SESION = 'historial_chat_cliente'
    if NOMBRE_HISTORIAL_SESION not in session:
        session[NOMBRE_HISTORIAL_SESION] = []
        logger.info(f"'{NOMBRE_HISTORIAL_SESION}' inicializado en flask.session.")

    # ----- DETERMINACIÓN DE USUARIO (user_obj) -----
    user_obj: Any = None 
    is_usuario_registrado_real: bool = False
    is_pyme_demo_anon = token is not None and token.startswith("demo-anon-")

    if is_pyme_demo_anon:
        logger.info(f"Modo Demo PYME Anónimo (token: {token}) detectado.")
        session.setdefault("pyme_demo_anon_preguntas", 0)
        if session["pyme_demo_anon_preguntas"] >= 15:
            logger.info("Límite de preguntas alcanzado para Demo PYME Anónimo.")
            return {"respuesta": "🔒 Límite de 15 preguntas en modo demo PYME alcanzado. Registrate para seguir.", "fuente": "sistema_limite"}
        session["pyme_demo_anon_preguntas"] += 1
        user_obj = AnonUserPymeDemo(session["pyme_demo_anon_preguntas"])
    elif token: 
        db_user_found = User.query.filter_by(token=token).first()
        if db_user_found:
            is_usuario_registrado_real = True
            user_obj = db_user_found
            logger.info(f"Usuario PYME registrado autenticado: {user_obj.email} (ID: {user_obj.id})")
            preguntas_usadas_user = int(user_obj.preguntas_usadas or 0)
            limite_preguntas_user = int(user_obj.limite_preguntas or 50)
            if preguntas_usadas_user >= limite_preguntas_user:
                logger.info(f"Límite de preguntas alcanzado para usuario {user_obj.email}.")
                return {"respuesta": "🔒 Límite de preguntas alcanzado en tu plan. Actualizá para más.", "fuente": "sistema_limite"}
        else:
            logger.warning(f"Token '{str(token)[:15]}...' proporcionado pero no corresponde a un usuario PYME registrado ni a demo-anon.")
            # Se tratará como Anónimo Genérico si user_obj sigue None
            
    if user_obj is None: 
        logger.info("No se identificó usuario PYME o demo-anon. Creando instancia de Anónimo Genérico.")
        session.setdefault("generic_anon_preguntas", 0)
        if session["generic_anon_preguntas"] >= 5: # Límite para anónimos genéricos
             logger.info("Límite de preguntas alcanzado para Anónimo Genérico.")
             return {"respuesta": "Alcanzaste el límite de preguntas para usuarios anónimos. ¡Registrate gratis para continuar!", "fuente": "sistema_limite"}
        session["generic_anon_preguntas"] += 1
        user_obj = GenericAnonUser()
    
    logger.info(f"==> User Obj Determinado: {type(user_obj).__name__}")
    logger.debug(f"    user_obj.nombre_empresa: {getattr(user_obj, 'nombre_empresa', 'N/A')}")
    logger.debug(f"    user_obj.horario (string): {getattr(user_obj, 'horario', 'N/A')}")
    logger.debug(f"    user_obj.horario_json (string JSON en mock, @property en User): {getattr(user_obj, 'horario_json', 'N/A')}")


    # ----- DETERMINACIÓN DE RUBRO (rubro_id_final, rubro_nombre_final) -----
    rubro_id_final: int = 1 
    rubro_nombre_final: str = "general"
    rubro_obj_final: Optional[Rubro] = None

    if rubro_nombre_frontend and isinstance(rubro_nombre_frontend, str) and rubro_nombre_frontend.strip():
        logger.info(f"Intentando determinar rubro por parámetro frontend: '{rubro_nombre_frontend}'")
        rubro_obj_final = Rubro.query.filter(func.lower(Rubro.nombre) == rubro_nombre_frontend.lower().strip()).first()
        if rubro_obj_final:
            logger.info(f"Rubro determinado por frontend: '{rubro_obj_final.nombre}' (ID: {rubro_obj_final.id})")
            if isinstance(user_obj, GenericAnonUser) or isinstance(user_obj, AnonUserPymeDemo): # Actualizar contexto si es anónimo o demo y rubro_frontend es válido
                user_obj.nombre_empresa = getattr(rubro_obj_final, 'nombre_pyme_default_para_anon', f"Negocios de {rubro_obj_final.nombre}")
                user_obj.rubro_id = rubro_obj_final.id # Guardar el ID del rubro en el objeto anónimo/demo
                # Aquí podrías copiar más atributos default del Rubro al user_obj si los tienes en el modelo Rubro
                # ej. user_obj.horario = getattr(rubro_obj_final, 'horario_default_rubro', user_obj.horario)
                logger.info(f"Contexto de user_obj ({type(user_obj).__name__}) actualizado con info del rubro '{rubro_obj_final.nombre}'.")
        else:
            logger.warning(f"Rubro '{rubro_nombre_frontend}' (enviado por frontend) no encontrado en BD. Se procederá con rubro del usuario o general.")

    if not rubro_obj_final and hasattr(user_obj, 'rubro_id') and user_obj.rubro_id:
        logger.info(f"Rubro no (o no válidamente) determinado por frontend. Usando rubro_id del user_obj: {user_obj.rubro_id}")
        rubro_obj_final = db.session.get(Rubro, user_obj.rubro_id)
        if not rubro_obj_final:
            logger.warning(f"Rubro ID {user_obj.rubro_id} (del user_obj) no encontrado. Se usará rubro general.")
    
    if not rubro_obj_final: # Si sigue sin rubro, usar el general por defecto (ID 1)
        logger.info(f"No se pudo determinar un rubro específico por frontend o user_obj. Usando rubro general (ID 1).")
        rubro_obj_final = db.session.get(Rubro, 1) 
    
    if rubro_obj_final:
        rubro_id_final = rubro_obj_final.id
        rubro_nombre_final = rubro_obj_final.nombre.lower().strip()
    else: 
        logger.error(f"¡ERROR CRÍTICO! No se encontró el rubro general ID 1 en la BD. Usando 'general' por defecto.")
        # rubro_id_final ya es 1, rubro_nombre_final ya es "general"
    
    logger.info(f"==> Rubro Final para la consulta: '{rubro_nombre_final}' (ID: {rubro_id_final})")


    # ----- CONSTRUCCIÓN DE CONTEXTO PARA LLM -----
    # Priorizar user.horario (string JSON de la BD) para User real, o user.horario_json (string JSON de mock) para anónimos
    horario_json_str_para_contexto = "[]"
    if isinstance(user_obj, User): 
        horario_json_str_para_contexto = user_obj.horario if user_obj.horario else '[]' 
    elif hasattr(user_obj, 'horario_json'): 
        horario_json_str_para_contexto = user_obj.horario_json if user_obj.horario_json else '[]'

    user_profile_context = {
        "nombre_empresa": getattr(user_obj, "nombre_empresa", "la tienda"),
        "rubro_nombre": rubro_nombre_final,
        "telefono_raw": getattr(user_obj, "telefono", ""),
        "link_web": getattr(user_obj, "link_web", ""),
        "direccion_completa": f"{getattr(user_obj, 'direccion', '')}, {getattr(user_obj, 'ciudad', '')}, {getattr(user_obj, 'provincia', '')}".replace(" ,", "").strip(', ').strip(),
        "horario_str": getattr(user_obj, "horario", "nuestro horario de atención"), # String simple para referencia
        "horario_json_str": horario_json_str_para_contexto, # El string JSON que se parseará
    }
    logger.debug(f"==> User Profile Context para LLM (antes de formatear horario): {json.dumps(user_profile_context, ensure_ascii=False, indent=2)}")

    horarios_para_prompt = user_profile_context['horario_str'] # Default
    try:
        if user_profile_context['horario_json_str'] and user_profile_context['horario_json_str'] != '[]':
            horarios_data = json.loads(user_profile_context['horario_json_str'])
            partes_horario = []
            if isinstance(horarios_data, list) and all(isinstance(h, dict) for h in horarios_data):
                for h_dict in horarios_data:
                    dia = h_dict.get("dia", "Día") # Espera que 'dia' venga del JSON
                    if h_dict.get("cerrado"): partes_horario.append(f"{dia}: Cerrado")
                    else: partes_horario.append(f"{dia}: de {h_dict.get('abre','--:--')} a {h_dict.get('cierra','--:--')}")
            if partes_horario: horarios_para_prompt = ". ".join(partes_horario) + "."
        logger.info(f"==> Horarios formateados para prompt: {horarios_para_prompt}")
    except Exception as e_json_h_prompt:
        logger.warning(f"No se pudo parsear/formatear horario_json_str ('{user_profile_context['horario_json_str']}') para prompt: {e_json_h_prompt}. Usando horario_str simple.")
    
    numero_intercambios_previos = len(session.get(NOMBRE_HISTORIAL_SESION, [])) // 2
    
    # (Tu prompt_sistema_texto se mantiene igual, solo asegúrate que use las variables actualizadas)
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
        f"\n- Horarios de Atención: {horarios_para_prompt if horarios_para_prompt and horarios_para_prompt.strip('.') else 'Consultar nuestros horarios.'}"
        f"\n- Sitio Web: {user_profile_context['link_web'] if user_profile_context['link_web'] else 'No disponible'}"
        "\nSI LA PREGUNTA DEL CLIENTE NO TIENE SENTIDO, es incomprensible o solo son caracteres al azar, NO intentes responderla directamente. En su lugar, responde amablemente que no entendiste la consulta y ofrece ayuda general. Ejemplo: 'Disculpa, no entendí bien tu consulta. Puedo ayudarte con información sobre nuestros productos, precios, horarios o cómo comprar. ¿En qué te puedo asistir hoy?'"
        "\nIMPORTANTE SOBRE PRODUCTOS Y PRECIOS DEL CATÁLOGO QUE TE PROVEERÉ:"
        # ... (resto de tus instrucciones para el LLM sobre el catálogo) ...
        "\n1. Cuando el cliente pregunte por un tipo de producto (ej. 'vinos malbec'), y si el catálogo recuperado contiene múltiples opciones, PRESENTA CLARAMENTE LAS OPCIONES MÁS RELEVANTES (máximo 2-3) con su 'Nombre' y 'Precio' exactos. Sé conciso. Ejemplo: 'Tenemos: Vino Malbec A a [Precio A], y Vino Malbec B Reserva a [Precio B].'"
        "\n7. Si te preguntan '¿Están abiertos ahora?' o sobre horarios específicos, utiliza la información de 'Horarios de Atención' que te proporcioné para responder lo más precisamente posible."
    )
    # ... (Lógica de añadir contexto_catalogo al prompt_sistema_texto)
    logger.debug(f"==> Prompt del sistema para Cohere (primeros 500 chars): {prompt_sistema_texto[:500]}")

    # --- LLAMADA AL LLM (COHERE) ---
    respuesta_obtenida_llm = "" 
    fuente_respuesta = "desconocida_inicial"
    try:
        from services.cohere_ai import get_cohere_response # Mover imports al top si es posible
        
        MAX_HISTORIAL_PARA_LLM = 10 
        historial_llm = session.get(NOMBRE_HISTORIAL_SESION, [])[-MAX_HISTORIAL_PARA_LLM:]
        mensajes_para_api_cohere = []
        for msg in historial_llm:
            mensajes_para_api_cohere.append({"role": "USER" if msg["role"]=="user" else "CHATBOT", "message": msg["content"]})

        logger.info(f"Enviando pregunta '{pregunta[:50]}...' a Cohere. Historial API: {len(mensajes_para_api_cohere)} mensajes. Preamble (System Prompt) será usado.")
        # La función get_cohere_response en cohere_ai.py usa preamble para el system_prompt
        respuesta_obtenida_llm = get_cohere_response(
            current_message=pregunta, # Asumiendo que tu get_cohere_response toma current_message
            chat_history_for_api=mensajes_para_api_cohere,
            system_prompt_for_api=prompt_sistema_texto,
            rubro_id=rubro_id_final, # Estos eran los args que pasabas antes
            user_context=user_profile_context
        )
        logger.info(f"Respuesta CRUDA de Cohere: '{respuesta_obtenida_llm}'")

        if respuesta_obtenida_llm and isinstance(respuesta_obtenida_llm, str) and len(respuesta_obtenida_llm.strip()) > 3:
            fuente_respuesta = "cohere"
            logger.info(f"Respuesta de Cohere considerada válida (longitud > 3).")
        else:
            logger.warning(f"Respuesta de Cohere fue vacía o muy corta: '{respuesta_obtenida_llm}'. Se considera inválida para respuesta directa.")
            respuesta_obtenida_llm = "" # Forzar a vacío para activar fallbacks
    except ImportError: 
        logger.error("Módulo Cohere (services.cohere_ai) no encontrado.")
    except Exception as e_cohere: 
        logger.error(f"Error al llamar a Cohere: {e_cohere}", exc_info=True)

    # --- FALLBACKS (FAQ, Intents) ---
    respuesta_final_procesada = ""
    if respuesta_obtenida_llm:
        respuesta_final_procesada = reemplazar_placeholders(respuesta_obtenida_llm, user_obj)
    else: 
        logger.info("Cohere no dio respuesta válida. Intentando FAQ Matcher...")
        try:
            from services.faq_matcher_spacy import buscar_en_faq_spacy
            faq_match_obj = buscar_en_faq_spacy(pregunta, rubro_id_final) 
            if faq_match_obj and faq_match_obj.answer: # faq_match_obj es el objeto QA
                respuesta_final_procesada = reemplazar_placeholders(faq_match_obj.answer, user_obj)
                fuente_respuesta = "faq"
                logger.info(f"Respuesta desde FAQ: '{respuesta_final_procesada[:100]}...'")
        except ImportError: logger.error("Módulo FAQ (faq_matcher_spacy) no encontrado.")
        except Exception as e_faq: logger.warning(f"Error en FAQ backup: {e_faq}", exc_info=True)

        if not respuesta_final_procesada: # Si FAQ tampoco dio respuesta
            logger.info("FAQ no dio respuesta. Intentando Intent Matcher...")
            try:
                from services.intent_matcher import buscar_en_intents
                intent_match_respuesta = buscar_en_intents(pregunta, rubro_nombre_final) 
                if intent_match_respuesta:
                    respuesta_final_procesada = reemplazar_placeholders(intent_match_respuesta, user_obj)
                    fuente_respuesta = "intent"
                    logger.info(f"Respuesta desde Intents: '{respuesta_final_procesada[:100]}...'")
            except ImportError: logger.error("Módulo Intent Matcher (intent_matcher) no encontrado.")
            except Exception as e_intent: logger.warning(f"Error en Intents backup: {e_intent}", exc_info=True)

    # --- RESPUESTA FINAL Y GUARDADO ---
    if respuesta_final_procesada and respuesta_final_procesada.strip():
        session[NOMBRE_HISTORIAL_SESION].append({"role": "user", "content": pregunta})
        session[NOMBRE_HISTORIAL_SESION].append({"role": "assistant", "content": respuesta_final_procesada})
        # ... (lógica de MAX_HISTORIAL_EN_SESION y session.modified) ...
        session.modified = True

        if is_usuario_registrado_real and user_obj.id:
            try:
                user_obj.preguntas_usadas = (user_obj.preguntas_usadas or 0) + 1
                db.session.add(Conversacion(user_id=user_obj.id, pregunta=pregunta, respuesta=respuesta_final_procesada, fuente=fuente_respuesta, rubro=rubro_nombre_final))
                db.session.commit()
            except Exception as e_db_conv:
                logger.error(f"Error guardando conversación en DB para user {user_obj.id}: {e_db_conv}", exc_info=True)
                db.session.rollback()
        
        respuesta_para_frontend = respuesta_final_procesada
        # ... (lógica para añadir botón HTML si hay link web) ...
        logger.info(f"✅ Respuesta final (fuente: {fuente_respuesta}): '{respuesta_para_frontend[:100]}...'")
        return {"respuesta": respuesta_para_frontend, "nivel_usado": rubro_nombre_final, "fuente": fuente_respuesta}
    else: 
        logger.info("Fallback final: Todos los sistemas (LLM, FAQ, Intents) no dieron respuesta útil. Usando sugerencias predefinidas.")
        sugerencias_generadas = sugerencias_por_rubro(rubro_id_final)
        textos_sugerencias = [s for s in sugerencias_generadas if s] # Filtrar Nones o vacíos
        if textos_sugerencias:
            respuesta_sugerencias_base = "No encontré una respuesta directa para tu consulta. Quizás puedas intentar preguntando algo como: " + " · ".join(f"“{s}”" for s in textos_sugerencias)
        else: # Si incluso las sugerencias por rubro fallan o están vacías
            respuesta_sugerencias_base = "Lo siento, no pude entender bien tu pregunta. ¿Podrías intentar reformularla o preguntar sobre nuestros productos y servicios?"

        respuesta_sugerencias_procesada = reemplazar_placeholders(respuesta_sugerencias_base, user_obj)
        # ... (lógica para añadir botón HTML a sugerencias si hay link web) ...
        session[NOMBRE_HISTORIAL_SESION].append({"role": "user", "content": pregunta}) 
        session.modified = True 
        return {"respuesta": respuesta_sugerencias_procesada, "fuente": "sugerencia_sistema"}

# --- FIN DE responder_chatboc ---