# services/logic.py
import logging
import random
from flask import session
from sqlalchemy import func
from models import User, Rubro, Sugerencia, Conversacion 
from extensions import db
import re 
import json 

logger = logging.getLogger(__name__) # Logger para este módulo

# --- Clases para usuarios anónimos (definidas a nivel de módulo) ---
class _BaseAnonUser:
    nombre_empresa = "la tienda"
    plan = "anonimo"
    rubro_id = None
    link_web = ""
    telefono = ""
    direccion = ""
    horario = "horario de atención habitual" 
    horario_json = '[]' # String JSON representando una lista vacía
    ciudad = ""
    provincia = ""
    pais = "Argentina" 
    latitud = None
    longitud = None
    id = None 

class AnonUserPymeDemo(_BaseAnonUser):
    def __init__(self, preguntas_realizadas_sesion):
        super().__init__() # Llamar al init de la clase base si define alguno, o simplemente para heredar
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

# --- Funciones Auxiliares (sugerencias_por_rubro, formatear_numero_whatsapp_simple, reemplazar_placeholders) ---
# (Estas funciones se mantienen como las tenías en tu archivo logic.py, 
#  asegúrate de que usen el logger de este módulo si es necesario: logger.info(...) etc.
#  y que `reemplazar_placeholders` maneje bien el `user_obj.horario_json` como lo discutimos:
#  si es un User de BD, `user_obj.horario_json` es un dict/None (de la @property).
#  si es AnonUser*, `user_obj.horario_json` es un string JSON.)
#
#  Por brevedad, no las repito aquí, pero deben estar presentes en tu archivo.
#  La versión de reemplazar_placeholders que te di anteriormente ya consideraba esto.
#  Asegúrate que esa versión esté aquí.

# Re-incluyo una versión de reemplazar_placeholders aquí para completitud,
# similar a la que te di para este archivo, adaptada para estar aquí.
def formatear_numero_whatsapp_simple(telefono_str: str, codigo_pais: str = "54") -> str:
    # ... (tu lógica de formatear_numero_whatsapp_simple)
    if not telefono_str: return ""
    numeros = re.sub(r'\D', '', str(telefono_str))
    if numeros.startswith(codigo_pais + "9") and len(numeros) == (len(codigo_pais) + 1 + 10): return numeros
    if numeros.startswith(codigo_pais) and not numeros.startswith(codigo_pais + "9") and len(numeros) == (len(codigo_pais) + 10): return codigo_pais + "9" + numeros[len(codigo_pais):]
    if len(numeros) == 10: return f"{codigo_pais}9{numeros}"
    logger.warning(f"Número '{telefono_str}' no formateado claramente a WhatsApp, devolviendo limpios: '{numeros}'")
    return numeros

def reemplazar_placeholders(texto: str, user_obj) -> str:
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
        valor_para_reemplazo = defaults_textos.get(attr_key, "")
        if user_obj:
            if attr_key == "horario_json": # Caso especial para horario_json
                if isinstance(user_obj, User): # Modelo User de BD
                    valor_atributo = user_obj.horario_json # Llama a la @property -> dict o None
                else: # AnonUserPymeDemo o GenericAnonUser
                    valor_atributo = getattr(user_obj, attr_key, None) # Debería ser un string JSON
            elif attr_key == "horario" and isinstance(user_obj, User):
                 valor_atributo = user_obj.horario # El string JSON original desde la BD
            else:
                valor_atributo = getattr(user_obj, attr_key, None)

        if valor_atributo is not None:
            if ph_template == "[telefono]":
                # ... (tu lógica de teléfono con formatear_numero_whatsapp_simple) ...
                if str(valor_atributo).strip():
                    numero_wsp = formatear_numero_whatsapp_simple(str(valor_atributo))
                    valor_para_reemplazo = f'{str(valor_atributo)} (<a href="https://wa.me/{numero_wsp}" target="_blank" style="color: green; text-decoration: underline; font-weight:bold;">Contactar</a>)' if numero_wsp else str(valor_atributo)
                else: valor_para_reemplazo = defaults_textos.get(attr_key, "nuestro número")

            elif ph_template == "[linkWeb]":
                # ... (tu lógica de linkWeb) ...
                if str(valor_atributo).strip():
                    link_abs = str(valor_atributo)
                    if not link_abs.startswith("http"): link_abs = "https://" + link_abs
                    valor_para_reemplazo = link_abs
                else: valor_para_reemplazo = defaults_textos.get(attr_key, "nuestro sitio")

            elif ph_template == "[horarioDetallado]":
                horarios_data = None
                if isinstance(valor_atributo, dict): horarios_data = valor_atributo # Ya es dict
                elif isinstance(valor_atributo, str) and valor_atributo.strip() and valor_atributo not in ['[]', '{}']:
                    try: horarios_data = json.loads(valor_atributo)
                    except json.JSONDecodeError: logger.warning(f"Error parseando string horario_json para placeholder: {valor_atributo}")
                
                if horarios_data and isinstance(horarios_data, list):
                    # ... (tu lógica de formateo de horarios_data a string)
                    partes = [f"{h.get('dia', 'Día')}: {h.get('abre','--')}-{h.get('cierra','--')}" if not h.get('cerrado') else f"{h.get('dia', 'Día')}: Cerrado" for h in horarios_data]
                    valor_para_reemplazo = ". ".join(partes) + "." if partes else defaults_textos.get(attr_key)
                else: # Fallback al string simple de horario
                    valor_para_reemplazo = str(getattr(user_obj, "horario", defaults_textos.get("horario")))
            
            elif isinstance(valor_atributo, str) and valor_atributo.strip() == "":
                valor_para_reemplazo = defaults_textos.get(attr_key, "")
            elif not isinstance(valor_atributo, (dict, list)):
                valor_para_reemplazo = str(valor_atributo)
            # else: no se reemplaza si es un dict/list no manejado específicamente
            
        texto_procesado = texto_procesado.replace(ph_template, valor_para_reemplazo)
    
    # ... (tu callback de reemplazar_desconocido_callback y re.sub) ...
    def reemplazar_desconocido_callback(match):
        # ... (tu lógica actual)
        placeholder_interno = match.group(1)
        logger.warning(f"Placeholder desconocido encontrado: [{placeholder_interno}]")
        if "precio" in placeholder_interno.lower() : return "(precio a consultar)"
        return ""
    texto_procesado = re.sub(r"\[([^\]\[]+)\]", reemplazar_desconocido_callback, texto_procesado)
    return texto_procesado

# --- FIN Funciones Auxiliares ---


def responder_chatboc(pregunta: str, token: str | None, rubro_nombre_frontend: str | None = None):
    logger.info(f"▶️ Inicio responder_chatboc: pregunta='{pregunta[:100]}...' token='{str(token)[:10]}...' rubro_frontend='{rubro_nombre_frontend}'")

    if not pregunta or not pregunta.strip():
        logger.warning("Pregunta vacía recibida.")
        return {"error": "Falta la pregunta"}

    NOMBRE_HISTORIAL_SESION = 'historial_chat_cliente'
    if NOMBRE_HISTORIAL_SESION not in session:
        session[NOMBRE_HISTORIAL_SESION] = []
        logger.info(f"Inicializando '{NOMBRE_HISTORIAL_SESION}' en flask.session.")

    user_obj = None # Cambiado nombre de variable para claridad
    rubro_id_final = 1 
    rubro_nombre_final = "general" # Default
    is_usuario_registrado_real = False # Para diferenciar User de BD de los anónimos

    is_pyme_demo_anon = token is not None and token.startswith("demo-anon-")

    if is_pyme_demo_anon:
        logger.info(f"Modo Demo PYME Anónimo (token: {token}) detectado.")
        session.setdefault("pyme_demo_anon_preguntas", 0)
        if session["pyme_demo_anon_preguntas"] >= 15:
            return {"respuesta": "🔒 Límite de 15 preguntas en modo demo PYME alcanzado. Registrate para seguir.", "fuente": "sistema"}
        session["pyme_demo_anon_preguntas"] += 1
        user_obj = AnonUserPymeDemo(session["pyme_demo_anon_preguntas"])
    
    elif token: 
        db_user_found = User.query.filter_by(token=token).first()
        if db_user_found:
            is_usuario_registrado_real = True
            user_obj = db_user_found
            logger.info(f"Usuario PYME registrado autenticado: {user_obj.email}")
            # Convertir preguntas_usadas y limite_preguntas a int, con defaults si son None
            preguntas_usadas_user = user_obj.preguntas_usadas if user_obj.preguntas_usadas is not None else 0
            limite_preguntas_user = user_obj.limite_preguntas if user_obj.limite_preguntas is not None else 50 # O un default de plan
            if preguntas_usadas_user >= limite_preguntas_user:
                return {"respuesta": "🔒 Límite de preguntas alcanzado en tu plan. Actualizá para más.", "fuente": "sistema"}
        else:
            logger.warning(f"Token '{str(token)[:15]}...' proporcionado pero no válido. Tratando como Anónimo Genérico.")
            # user_obj se quedará None y se creará GenericAnonUser abajo
            
    if user_obj is None: 
        logger.info("Creando instancia de Anónimo Genérico.")
        session.setdefault("generic_anon_preguntas", 0)
        if session["generic_anon_preguntas"] >= 5:
             return {"respuesta": "Alcanzaste el límite de preguntas para usuarios anónimos. ¡Registrate gratis para continuar!", "fuente": "sistema"}
        session["generic_anon_preguntas"] += 1
        user_obj = GenericAnonUser()

    # --- Determinar Rubro Final ---
    # (Tu lógica de determinación de rubro aquí, usando db.session.get(Rubro, id_rubro) donde sea posible)
    # ...
    # Ejemplo simplificado (DEBES USAR TU LÓGICA COMPLETA AQUÍ):
    rubro_obj_final = None
    if rubro_nombre_frontend:
        rubro_obj_final = Rubro.query.filter(func.lower(Rubro.nombre) == rubro_nombre_frontend.lower().strip()).first()
    if not rubro_obj_final and hasattr(user_obj, 'rubro_id') and user_obj.rubro_id:
        rubro_obj_final = db.session.get(Rubro, user_obj.rubro_id)
    if not rubro_obj_final:
        rubro_obj_final = db.session.get(Rubro, 1) # Fallback a general
    
    if rubro_obj_final:
        rubro_id_final = rubro_obj_final.id
        rubro_nombre_final = rubro_obj_final.nombre.lower().strip()
        # Si user_obj es GenericAnonUser y se encontró un rubro por frontend, actualizar su contexto:
        if isinstance(user_obj, GenericAnonUser) and rubro_nombre_frontend and rubro_obj_final.nombre.lower().strip() == rubro_nombre_frontend.lower().strip():
            user_obj.nombre_empresa = getattr(rubro_obj_final, 'nombre_pyme_default_para_anon', f"la sección de {rubro_obj_final.nombre}")
            user_obj.rubro_id = rubro_obj_final.id
            # ... (copiar más atributos del Rubro al GenericAnonUser si es necesario) ...
    else: # No debería pasar si el rubro ID 1 (general) existe
        logger.error(f"Rubro ID {rubro_id_final} (general) no encontrado en la BD!")
    # --- Fin Determinar Rubro Final ---

    logger.info(f"Contexto User final: {type(user_obj).__name__}, Empresa: '{getattr(user_obj, 'nombre_empresa', 'N/A')}', Rubro: '{rubro_nombre_final}' (ID: {rubro_id_final})")

    # --- Construcción de User Profile Context para LLM ---
    # (Asegúrate de que esta lógica obtiene correctamente el string JSON del horario)
    # ... (Tu lógica para pyme_nombre_empresa, link_web, telefono, etc.)
    pyme_horario_json_str = ""
    if isinstance(user_obj, User): # Usuario de BD
        pyme_horario_json_str = user_obj.horario if user_obj.horario else '[]'
    else: # AnonUserPymeDemo o GenericAnonUser
        pyme_horario_json_str = getattr(user_obj, "horario_json", "[]")

    user_profile_context = {
        "nombre_empresa": getattr(user_obj, "nombre_empresa", "la tienda"),
        "rubro_nombre": rubro_nombre_final,
        "telefono_raw": getattr(user_obj, "telefono", ""),
        "link_web": getattr(user_obj, "link_web", ""),
        "direccion_completa": f"{getattr(user_obj, 'direccion', '')}, {getattr(user_obj, 'ciudad', '')}, {getattr(user_obj, 'provincia', '')}".replace(" ,", "").strip(', '),
        "horario_str": getattr(user_obj, "horario", "nuestro horario de atención"), # El string simple
        "horario_json_str": pyme_horario_json_str, # El string JSON
    }
    # ... (el resto de user_profile_context)

    # --- Formatear horarios detallados para el prompt ---
    # (Tu lógica para formatear horarios_para_prompt usando user_profile_context['horario_json_str'])
    # ...

    # --- Construcción del Prompt del Sistema ---
    # (Tu lógica para prompt_sistema_texto)
    # ...

    # --- Búsqueda en Catálogo Qdrant ---
    contexto_catalogo = ""
    if is_usuario_registrado_real and user_obj.id is not None:
        try:
            from services.qdrant_search import buscar_catalogo_qdrant, armar_respuesta_legible # Mover imports al top si es posible
            resultados_qdrant = buscar_catalogo_qdrant(user_obj.id, pregunta, limite=3) # Límite ajustado
            contexto_catalogo = armar_respuesta_legible(resultados_qdrant)
            if contexto_catalogo: logger.info(f"Contexto Qdrant (parcial): {contexto_catalogo[:150]}...")
            else: logger.info("Sin contexto de catálogo Qdrant para esta pregunta.")
        except ImportError: logger.error("Módulo Qdrant (qdrant_search) no encontrado.")
        except Exception as e_qdrant: logger.error(f"Error buscando en Qdrant: {e_qdrant}", exc_info=True)
    
    # --- Preparar mensajes y llamar a Cohere ---
    # (Tu lógica para mensajes_finales_para_llm y llamada a get_cohere_response)
    # ...

    # --- Fallbacks (FAQ, Intents) ---
    # (Tu lógica de fallbacks)
    # ...

    # --- Guardado y respuesta final ---
    # (Tu lógica de guardado de conversación y adición de botón)
    # ...

    # Este es un esqueleto, debes rellenar las partes omitidas (...) con tu lógica completa.
    # La clave era definir AnonUser* fuera de los if/else.
    # Y asegurar que `user_obj` se use consistentemente.

    # Ejemplo de respuesta si todo lo demás falla (DEBES REEMPLAZAR ESTO)
    # respuesta_final_procesada = "Lo siento, no pude procesar tu solicitud en este momento."
    # fuente_respuesta = "error_interno"
    # ... (resto del código de tu función)
    
    # Al final, después de tener `respuesta_final_procesada` y `fuente_respuesta`:
    # if respuesta_final_procesada:
    #     session[NOMBRE_HISTORIAL_SESION].append({"role": "user", "content": pregunta})
    #     session[NOMBRE_HISTORIAL_SESION].append({"role": "assistant", "content": respuesta_final_procesada})
    #     session.modified = True
    #     if is_usuario_registrado_real and user_obj.id:
    #         try:
    #             user_obj.preguntas_usadas = (user_obj.preguntas_usadas or 0) + 1
    #             db.session.add(Conversacion(user_id=user_obj.id, pregunta=pregunta, respuesta=respuesta_final_procesada, fuente=fuente_respuesta, rubro=rubro_nombre_final))
    #             db.session.commit()
    #         except Exception as e_db_conv:
    #             logger.error(f"Error guardando conversación en DB: {e_db_conv}", exc_info=True)
    #             db.session.rollback()
    #     # ... (añadir botón HTML si hay link web) ...
    #     return {"respuesta": respuesta_final_procesada_con_boton_si_aplica, "nivel_usado": rubro_nombre_final, "fuente": fuente_respuesta}
    # else:
    #     # ... (tu lógica de fallback final a sugerencias del sistema) ...
    #     return {"respuesta": respuesta_sugerencias_procesada_con_boton_si_aplica, "fuente": "sugerencia_sistema"}

    # !! ESTA ES UNA RESPUESTA DE EMERGENCIA HASTA QUE COMPLETES LA LÓGICA !!
    logger.error("LOGICA INCOMPLETA EN RESPONDER_CHATBOC - SE DEVOLVERA ERROR GENERICO")
    return {"error": "Error procesando la solicitud en responder_chatboc"}