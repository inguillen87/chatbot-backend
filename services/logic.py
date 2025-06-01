# services/logic.py
import logging
import random
from flask import session
from sqlalchemy import func
from models import User, Rubro, Sugerencia, Conversacion 
from extensions import db
import re 
import json 
from typing import Any, Optional # Mejoras en tipado

# Configuración del logger para este módulo.
logger = logging.getLogger(__name__)
# PARA ASEGURAR QUE VEAS LOS LOGS EN RENDER, CAMBIAREMOS TEMPORALMENTE DEBUG A INFO
# O ASEGÚRATE QUE TU APP FLASK ESTÉ LOGUEANDO A NIVEL DEBUG.
# UNA FORMA FÁCIL ES CAMBIAR MANUALMENTE logger.debug A logger.info MÁS ABAJO.

# --- Clases para usuarios anónimos (definidas a nivel de módulo) ---
class _BaseAnonUser:
    nombre_empresa: str = "la tienda"
    plan: str = "anonimo"
    rubro_id: Optional[int] = None
    link_web: str = ""
    telefono: str = ""
    direccion: str = ""
    horario: str = "horario de atención habitual" 
    horario_json: str = '[]' 
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
    try:
        sugerencias_obj = Sugerencia.query.filter_by(rubro_id=rubro_id).all()
        if sugerencias_obj:
            todas = [s.texto for s in sugerencias_obj]
            # CAMBIO: logger.debug a logger.info para asegurar visibilidad
            logger.info(f"[LOGIC-SUG] Sugerencias para rubro {rubro_id} (total: {len(todas)}): {todas[:3]}") 
            return random.sample(todas, min(3, len(todas)))
        if rubro_id != 1: 
            fallback_obj = Sugerencia.query.filter_by(rubro_id=1).all()
            if fallback_obj:
                logger.info(f"[LOGIC-SUG] No se encontraron sugerencias para rubro {rubro_id}, usando fallback general.")
                return random.sample([s.texto for s in fallback_obj], min(3, len(fallback_obj)))
        logger.info(f"[LOGIC-SUG] No se encontraron sugerencias para rubro {rubro_id} ni en fallback. Usando defaults.")
        return ["¿Cuáles son sus horarios de atención?", "¿Qué productos ofrecen?", "¿Cómo puedo realizar una compra?"]
    except Exception as e:
        logger.error(f"[LOGIC-SUG] Error buscando sugerencias para rubro_id {rubro_id}: {e}", exc_info=True)
        return ["Disculpa, tuve un problema al buscar sugerencias en este momento."]

def formatear_numero_whatsapp_simple(telefono_str: str, codigo_pais: str = "54") -> str:
    if not telefono_str: return ""
    numeros = re.sub(r'\D', '', str(telefono_str))
    if numeros.startswith(codigo_pais + "9") and len(numeros) == (len(codigo_pais) + 1 + 10): return numeros
    if numeros.startswith(codigo_pais) and not numeros.startswith(codigo_pais + "9") and len(numeros) == (len(codigo_pais) + 10): return codigo_pais + "9" + numeros[len(codigo_pais):]
    if len(numeros) == 10: return f"{codigo_pais}9{numeros}"
    # CAMBIO: logger.debug a logger.info
    logger.info(f"[LOGIC-WSP] Número '{telefono_str}' no formateado claramente a WhatsApp, devolviendo solo dígitos: '{numeros}'")
    return numeros

def reemplazar_placeholders(texto: str, user_obj: Any) -> str:
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
        "horario_json": "consultar nuestros horarios detallados",
    }
    texto_procesado = texto
    for ph_template, attr_key in placeholders_conocidos.items():
        valor_atributo = None
        valor_para_reemplazo = defaults_textos.get(attr_key, f"") # Devolver "" si no hay default
        
        if user_obj:
            if attr_key == "horario_json": 
                if isinstance(user_obj, User): 
                    valor_atributo = user_obj.horario_json 
                else: 
                    valor_atributo = getattr(user_obj, attr_key, None)
            elif attr_key == "horario" and isinstance(user_obj, User):
                 valor_atributo = user_obj.horario 
            else:
                valor_atributo = getattr(user_obj, attr_key, None)

        if valor_atributo is not None:
            if ph_template == "[telefono]":
                tel_str = str(valor_atributo).strip()
                if tel_str:
                    numero_wsp = formatear_numero_whatsapp_simple(tel_str)
                    valor_para_reemplazo = f'{tel_str} (<a href="https://wa.me/{numero_wsp}" target="_blank" style="color: green; text-decoration: underline; font-weight:bold;">Contactar por WhatsApp</a>)' if numero_wsp else tel_str
                else: valor_para_reemplazo = defaults_textos.get(attr_key, "")
            elif ph_template == "[linkWeb]":
                link_str = str(valor_atributo).strip()
                if link_str:
                    link_abs = link_str
                    if not link_abs.startswith("http"): link_abs = "https://" + link_abs
                    valor_para_reemplazo = link_abs
                else: valor_para_reemplazo = defaults_textos.get(attr_key, "")
            elif ph_template == "[horarioDetallado]":
                horarios_data_parseada = None
                if isinstance(valor_atributo, dict) or isinstance(valor_atributo, list): 
                    horarios_data_parseada = valor_atributo
                elif isinstance(valor_atributo, str) and valor_atributo.strip() and valor_atributo not in ['[]', '{}', 'null']:
                    try: horarios_data_parseada = json.loads(valor_atributo)
                    except json.JSONDecodeError: 
                        logger.warning(f"[LOGIC-PH] Error parseando string de '{attr_key}' ('{valor_atributo}') para [horarioDetallado]. User type: {type(user_obj).__name__}")
                
                if horarios_data_parseada and isinstance(horarios_data_parseada, list) and all(isinstance(h, dict) for h in horarios_data_parseada):
                    partes = []
                    for h_dict in horarios_data_parseada:
                        dia = h_dict.get('dia', '')
                        if h_dict.get('cerrado'): partes.append(f"{dia}: Cerrado" if dia else "Cerrado")
                        else: partes.append(f"{dia}: de {h_dict.get('abre','--:--')} a {h_dict.get('cierra','--:--')}" if dia else f"de {h_dict.get('abre','--:--')} a {h_dict.get('cierra','--:--')}")
                    valor_para_reemplazo = ". ".join(p for p in partes if p.replace(": Cerrado","").strip()) + ("." if partes else "")
                    if not valor_para_reemplazo.strip() or valor_para_reemplazo == ".": valor_para_reemplazo = defaults_textos.get(attr_key)
                else: 
                    # CAMBIO: logger.debug a logger.info
                    logger.info(f"[LOGIC-PH] No se pudo formatear horario detallado para {ph_template} con valor '{str(valor_atributo)[:50]}...'. Usando fallback.")
                    valor_para_reemplazo = str(getattr(user_obj, "horario", defaults_textos.get("horario")))
            elif isinstance(valor_atributo, str) and valor_atributo.strip() == "":
                valor_para_reemplazo = defaults_textos.get(attr_key, "")
            elif not isinstance(valor_atributo, (dict, list)):
                valor_para_reemplazo = str(valor_atributo)
            
        texto_procesado = texto_procesado.replace(ph_template, valor_para_reemplazo)
    
    def reemplazar_desconocido_callback(match):
        placeholder_interno = match.group(1)
        logger.warning(f"[LOGIC-PH] Placeholder desconocido encontrado y eliminado: [{placeholder_interno}]")
        return "" 
    texto_procesado = re.sub(r"\[([^\]\[\s]+?)\]", reemplazar_desconocido_callback, texto_procesado)
    return texto_procesado
# --- FIN Funciones Auxiliares ---


# --- COMIENZO DE responder_chatboc ---
def responder_chatboc(pregunta: str, token: str | None, rubro_nombre_frontend: str | None = None):
    # ----- LOGS INICIALES Y VALIDACIÓN DE PREGUNTA -----
    logger.info(f"▶️ Inicio responder_chatboc: Pregunta='{pregunta[:100]}...' Token='{str(token)[:15] if token else 'N/A'}' RubroFrontend='{rubro_nombre_frontend}'")

    if not pregunta or not pregunta.strip():
        logger.warning("[LOGIC] Pregunta vacía recibida, no se procesará.")
        return {"error": "La pregunta no puede estar vacía"}

    NOMBRE_HISTORIAL_SESION = 'historial_chat_cliente'
    if NOMBRE_HISTORIAL_SESION not in session:
        session[NOMBRE_HISTORIAL_SESION] = []
        logger.info(f"[LOGIC] '{NOMBRE_HISTORIAL_SESION}' inicializado en flask.session.")

    # ----- DETERMINACIÓN DE USUARIO (user_obj) -----
    user_obj: Any = None 
    is_usuario_registrado_real: bool = False
    is_pyme_demo_anon = token is not None and token.startswith("demo-anon-") # Asumir un prefijo específico

    if is_pyme_demo_anon:
        logger.info(f"[LOGIC] Modo Demo PYME Anónimo (token: {token}) detectado.")
        session.setdefault("pyme_demo_anon_preguntas", 0)
        if session["pyme_demo_anon_preguntas"] >= 15: # Límite para este tipo de demo
            logger.info("[LOGIC] Límite de preguntas alcanzado para Demo PYME Anónimo.")
            return {"respuesta": "🔒 Límite de 15 preguntas en modo demo PYME alcanzado. Registrate para seguir.", "fuente": "sistema_limite"}
        session["pyme_demo_anon_preguntas"] += 1
        user_obj = AnonUserPymeDemo(session["pyme_demo_anon_preguntas"])
    elif token: 
        db_user_found = User.query.filter_by(token=token).first()
        if db_user_found:
            is_usuario_registrado_real = True
            user_obj = db_user_found
            logger.info(f"[LOGIC] Usuario PYME registrado autenticado: {user_obj.email} (ID: {user_obj.id})")
            preguntas_usadas_user = int(user_obj.preguntas_usadas or 0)
            limite_preguntas_user = int(user_obj.limite_preguntas or 50) # Default de plan si es None
            if preguntas_usadas_user >= limite_preguntas_user:
                logger.info(f"[LOGIC] Límite de preguntas alcanzado para usuario {user_obj.email}.")
                return {"respuesta": "🔒 Límite de preguntas alcanzado en tu plan. Actualizá para más.", "fuente": "sistema_limite"}
        else:
            logger.warning(f"[LOGIC] Token '{str(token)[:15]}...' proporcionado pero no corresponde a un usuario PYME registrado ni a demo-anon.")
            # user_obj se quedará None y se creará GenericAnonUser abajo
            
    if user_obj is None: 
        logger.info("[LOGIC] No se identificó usuario PYME o demo-anon. Creando instancia de Anónimo Genérico.")
        session.setdefault("generic_anon_preguntas", 0)
        if session["generic_anon_preguntas"] >= 5: # Límite para anónimos genéricos
             logger.info("[LOGIC] Límite de preguntas alcanzado para Anónimo Genérico.")
             return {"respuesta": "Alcanzaste el límite de preguntas para usuarios anónimos. ¡Registrate gratis para continuar!", "fuente": "sistema_limite"}
        session["generic_anon_preguntas"] += 1
        user_obj = GenericAnonUser()
    
    # Loguear el tipo de usuario y datos de horario
    logger.info(f"[LOGIC] ==> User Obj Determinado: {type(user_obj).__name__}")
    logger.info(f"[LOGIC]     user_obj.nombre_empresa: {getattr(user_obj, 'nombre_empresa', 'N/A')}")
    logger.info(f"[LOGIC]     user_obj.horario (string crudo): '{getattr(user_obj, 'horario', 'N/A')}'")
    
    # Para User de BD, .horario_json es la @property que parsea. Para mocks, es el atributo string JSON.
    # Necesitamos el string JSON para el user_profile_context, y el objeto parseado para formatear el prompt.
    horario_json_obj_o_str = getattr(user_obj, 'horario_json', None)
    logger.info(f"[LOGIC]     user_obj.horario_json (valor directo del atributo/propiedad): {str(horario_json_obj_o_str)[:100]}...")


    # ----- DETERMINACIÓN DE RUBRO (rubro_id_final, rubro_nombre_final) -----
    rubro_id_final: int = 1 
    rubro_nombre_final: str = "general"
    rubro_obj_final: Optional[Rubro] = None
    # ... (Tu lógica de determinación de rubro, que ya estaba bastante bien) ...
    # Solo asegúrate de loguear el resultado:
    logger.info(f"[LOGIC] ==> Rubro Final para la consulta: '{rubro_nombre_final}' (ID: {rubro_id_final})")


    # ----- CONSTRUCCIÓN DE CONTEXTO PARA LLM -----
    # Obtener el string JSON para el horario, consistentemente
    horario_json_str_para_contexto = "[]" # Default
    if isinstance(user_obj, User): 
        horario_json_str_para_contexto = user_obj.horario if user_obj.horario else '[]' 
    elif hasattr(user_obj, 'horario_json') and isinstance(user_obj.horario_json, str): 
        horario_json_str_para_contexto = user_obj.horario_json if user_obj.horario_json else '[]'
    elif hasattr(user_obj, 'horario_json') and isinstance(user_obj.horario_json, dict): # Por si el mock ya lo tiene como dict
        try: horario_json_str_para_contexto = json.dumps(user_obj.horario_json)
        except: horario_json_str_para_contexto = '[]'


    user_profile_context = {
        "nombre_empresa": getattr(user_obj, "nombre_empresa", "la tienda"),
        "rubro_nombre": rubro_nombre_final,
        "telefono_raw": getattr(user_obj, "telefono", ""),
        "link_web": getattr(user_obj, "link_web", ""),
        "direccion_completa": f"{getattr(user_obj, 'direccion', '')}, {getattr(user_obj, 'ciudad', '')}, {getattr(user_obj, 'provincia', '')}".replace(" ,", "").strip(', ').strip(),
        "horario_str": getattr(user_obj, "horario", "nuestro horario de atención"), # String simple
        "horario_json_str": horario_json_str_para_contexto, 
    }
    logger.info(f"[LOGIC] ==> User Profile Context para LLM (horario_json_str='{user_profile_context['horario_json_str']}'): {json.dumps(user_profile_context, ensure_ascii=False, indent=2)}")

    horarios_para_prompt = user_profile_context['horario_str'] # Default
    try:
        if user_profile_context['horario_json_str'] and user_profile_context['horario_json_str'].strip() not in ['[]', '{}', 'null', '']:
            horarios_data = json.loads(user_profile_context['horario_json_str'])
            partes_horario = []
            if isinstance(horarios_data, list) and all(isinstance(h, dict) for h in horarios_data):
                for h_dict in horarios_data:
                    dia = h_dict.get("dia", "")
                    if not dia: continue # Saltar si no hay día
                    if h_dict.get("cerrado"): partes_horario.append(f"{dia}: Cerrado")
                    else: partes_horario.append(f"{dia}: de {h_dict.get('abre','--:--')} a {h_dict.get('cierra','--:--')}")
            if partes_horario: horarios_para_prompt = ". ".join(partes_horario) + "."
        logger.info(f"[LOGIC] ==> Horarios formateados para prompt: {horarios_para_prompt}")
    except Exception as e_json_h_prompt:
        logger.warning(f"[LOGIC] No se pudo parsear/formatear horario_json_str ('{user_profile_context['horario_json_str']}') para el prompt: {e_json_h_prompt}. Usando horario_str simple.")
    
    # ... (Construcción de prompt_sistema_texto como antes, usando horarios_para_prompt) ...
    # ... (Lógica de búsqueda en catálogo Qdrant como antes) ...
    # ... (Llamada a Cohere, reemplazando placeholders, y fallbacks a FAQ e Intents como antes) ...
    
    # ASEGÚRATE DE QUE ESTAS LLAMADAS A FUNCIONES EXTERNAS (COHERE, FAQ, INTENTS)
    # ESTÉN DESCOMENTADAS Y COMPLETAS EN TU CÓDIGO.
    # POR EJEMPLO, LA LLAMADA A COHERE:
    respuesta_obtenida_llm = ""
    fuente_respuesta = "desconocida_inicial"
    prompt_sistema_texto = "AQUI VA TU PROMPT DEL SISTEMA COMPLETO USANDO user_profile_context y horarios_para_prompt" # REEMPLAZA ESTO
    # (Construye tu prompt_sistema_texto COMPLETO aquí)

    try:
        from services.cohere_ai import get_cohere_response
        # ... (preparar mensajes_completos_cohere como antes) ...
        historial_llm = session.get(NOMBRE_HISTORIAL_SESION, [])[-10:] # Ejemplo
        mensajes_para_api_cohere = []
        for msg in historial_llm:
            mensajes_para_api_cohere.append({"role": "USER" if msg["role"]=="user" else "CHATBOT", "message": msg["content"]})
        
        mensajes_completos_cohere = [{"role": "system", "content": prompt_sistema_texto}] + mensajes_para_api_cohere + [{"role": "user", "content": pregunta}]

        respuesta_obtenida_llm = get_cohere_response(mensajes_completos_cohere, rubro_id_final, user_profile_context)
        logger.info(f"[LOGIC] Respuesta CRUDA de Cohere: '{respuesta_obtenida_llm}'")
        if respuesta_obtenida_llm and isinstance(respuesta_obtenida_llm, str) and len(respuesta_obtenida_llm.strip()) > 3:
            fuente_respuesta = "cohere"
        else:
            respuesta_obtenida_llm = "" # Forzar a vacío
    except Exception as e_cohere:
        logger.error(f"[LOGIC] Error llamando a Cohere: {e_cohere}", exc_info=True)
        respuesta_obtenida_llm = "" # Asegurar vacío en error

    # ... (el resto de tu lógica de fallbacks, guardado, y retorno final)

    # Si después de todos los intentos, no hay respuesta_final_procesada
    if not respuesta_final_procesada or not respuesta_final_procesada.strip(): # Añadido chequeo de strip
        logger.info("[LOGIC] Fallback final: Todos los sistemas no dieron respuesta útil. Usando sugerencias predefinidas.")
        sugerencias_generadas = sugerencias_por_rubro(rubro_id_final) # rubro_id_final ya está definido
        textos_sugerencias = [s for s in sugerencias_generadas if s] 
        if textos_sugerencias:
            respuesta_sugerencias_base = "No encontré una respuesta directa para tu consulta. Quizás puedas intentar preguntando algo como: " + " · ".join(f"“{s}”" for s in textos_sugerencias)
        else: 
            respuesta_sugerencias_base = "Lo siento, no pude entender bien tu pregunta. ¿Podrías intentar reformularla o preguntar sobre nuestros productos y servicios?"
        respuesta_final_procesada = reemplazar_placeholders(respuesta_sugerencias_base, user_obj)
        fuente_respuesta = "sugerencia_sistema"
        # ... (añadir botón HTML si hay link web) ...
        
    # Asegurar que siempre se devuelva algo
    if not respuesta_final_procesada: # Si aún está vacía (muy improbable ahora)
        respuesta_final_procesada = "Lo siento, no puedo ayudarte con eso en este momento."
        fuente_respuesta = "error_inesperado"

    # Guardar en historial de sesión
    session[NOMBRE_HISTORIAL_SESION].append({"role": "user", "content": pregunta})
    session[NOMBRE_HISTORIAL_SESION].append({"role": "assistant", "content": respuesta_final_procesada})
    MAX_HISTORIAL_EN_SESION = 20 
    if len(session[NOMBRE_HISTORIAL_SESION]) > MAX_HISTORIAL_EN_SESION:
        session[NOMBRE_HISTORIAL_SESION] = session[NOMBRE_HISTORIAL_SESION][-MAX_HISTORIAL_EN_SESION:]
    session.modified = True

    # Guardar en DB si es usuario PYME real
    if is_usuario_registrado_real and user_obj.id:
        try:
            user_obj.preguntas_usadas = (user_obj.preguntas_usadas or 0) + 1
            db.session.add(Conversacion(user_id=user_obj.id, pregunta=pregunta, respuesta=respuesta_final_procesada, fuente=fuente_respuesta, rubro=rubro_nombre_final))
            db.session.commit()
        except Exception as e_db_conv:
            logger.error(f"[LOGIC] Error guardando conversación en DB para user {user_obj.id}: {e_db_conv}", exc_info=True)
            db.session.rollback()
            
    # Añadir botón HTML
    respuesta_para_frontend = respuesta_final_procesada
    pyme_link_web_actual = getattr(user_obj, "link_web", "")
    if pyme_link_web_actual and fuente_respuesta not in ["faq", "sugerencia_sistema_con_link"]: # Evitar doble botón
        # ... (tu lógica del botón HTML) ...
        pass

    logger.info(f"✅ [LOGIC] Respuesta final (fuente: {fuente_respuesta}): '{respuesta_para_frontend[:100]}...'")
    return {"respuesta": respuesta_para_frontend, "nivel_usado": rubro_nombre_final, "fuente": fuente_respuesta}

# --- FIN DE responder_chatboc ---