# services/logic.py
import logging
import random
from flask import session
from sqlalchemy import func
from models import User, Rubro, Sugerencia, Conversacion, db 
from extensions import db as extensions_db # Si tienes dos 'db', asegúrate cuál usar o renombra uno. Usaré 'db' de models.
import re 
import json 
from typing import Any, Optional, Dict, List

logger = logging.getLogger(__name__)

# --- Clases para usuarios anónimos (definidas a nivel de módulo) ---
class _BaseAnonUser:
    nombre_empresa: str = "la tienda"; plan: str = "anonimo"; rubro_id: Optional[int] = None; link_web: str = ""; telefono: str = ""; direccion: str = ""; horario: str = "horario de atención habitual"; horario_json: str = '[]'; ciudad: str = ""; provincia: str = ""; pais: str = "Argentina"; latitud: Optional[float] = None; longitud: Optional[float] = None; id: Optional[int] = None 

class AnonUserPymeDemo(_BaseAnonUser):
    def __init__(self, preguntas_realizadas_sesion: int):
        super().__init__(); self.nombre_empresa = "Chatboc Demostración"; self.plan = "demo_pyme"; self.preguntas_usadas = preguntas_realizadas_sesion; self.limite_preguntas = 15; self.link_web = "https://www.chatboc.ar"; self.telefono = "+549111234567"; self.direccion = "Av. Corrientes 1234, CABA"; self.ciudad = "CABA"; self.provincia = "CABA"; self.horario = "Lunes a Viernes de 9hs a 18hs. Sábados de 9hs a 13hs."; self.horario_json = json.dumps([{"dia": "Lunes", "abre": "09:00", "cierra": "18:00", "cerrado": False},{"dia": "Martes", "abre": "09:00", "cierra": "18:00", "cerrado": False},{"dia": "Miércoles", "abre": "09:00", "cierra": "18:00", "cerrado": False},{"dia": "Jueves", "abre": "09:00", "cierra": "18:00", "cerrado": False},{"dia": "Viernes", "abre": "09:00", "cierra": "18:00", "cerrado": False},{"dia": "Sábado", "abre": "09:00", "cierra": "13:00", "cerrado": False},{"dia": "Domingo", "abre": "", "cierra": "", "cerrado": True}]); self.latitud = -34.6037; self.longitud = -58.3816 

class GenericAnonUser(_BaseAnonUser):
    def __init__(self):
        super().__init__(); self.plan = "anonimo_general"; self.preguntas_usadas = session.get("generic_anon_preguntas", 0); self.limite_preguntas = 5
# --- Fin Clases Anónimas ---

# --- Funciones Auxiliares (Asegúrate que estas definiciones estén aquí o importadas correctamente) ---
# (Incluyo las versiones que ya trabajamos)
def sugerencias_por_rubro(rubro_id: int) -> list:
    try:
        sugerencias_obj = Sugerencia.query.filter_by(rubro_id=rubro_id).all()
        if sugerencias_obj:
            todas = [s.texto for s in sugerencias_obj]
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
        valor_para_reemplazo = defaults_textos.get(attr_key, f"") 
        
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
                        if not dia: continue
                        if h_dict.get('cerrado'): partes.append(f"{dia}: Cerrado")
                        else: partes.append(f"{dia}: de {h_dict.get('abre','--:--')} a {h_dict.get('cierra','--:--')}")
                    valor_para_reemplazo = ". ".join(p for p in partes if p) + ("." if partes else "")
                    if not valor_para_reemplazo.strip() or valor_para_reemplazo == ".": valor_para_reemplazo = defaults_textos.get(attr_key)
                else: 
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
def responder_chatboc(pregunta: str, token: str | None, rubro_nombre_frontend: str | None = None) -> Dict[str, Any]:
    # Convertir todos los logger.debug a logger.info para asegurar visibilidad en Render
    logger.info(f"▶️ [LOGIC] Inicio responder_chatboc: Pregunta='{pregunta[:100]}...' Token='{str(token)[:15] if token else 'N/A'}' RubroFrontend='{rubro_nombre_frontend}'")

    if not pregunta or not pregunta.strip():
        logger.warning("[LOGIC] Pregunta vacía recibida, no se procesará.")
        return {"error": "La pregunta no puede estar vacía"}

    NOMBRE_HISTORIAL_SESION = 'historial_chat_cliente'
    if NOMBRE_HISTORIAL_SESION not in session:
        session[NOMBRE_HISTORIAL_SESION] = []
        logger.info(f"[LOGIC] '{NOMBRE_HISTORIAL_SESION}' inicializado en flask.session.")

    user_obj: Any = None 
    is_usuario_registrado_real: bool = False
    is_pyme_demo_anon = token is not None and token.startswith("demo-anon-") 

    if is_pyme_demo_anon:
        logger.info(f"[LOGIC] Modo Demo PYME Anónimo (token: {token}) detectado.")
        session.setdefault("pyme_demo_anon_preguntas", 0)
        if session["pyme_demo_anon_preguntas"] >= 15: 
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
            limite_preguntas_user = int(user_obj.limite_preguntas or 50) 
            if preguntas_usadas_user >= limite_preguntas_user:
                logger.info(f"[LOGIC] Límite de preguntas alcanzado para usuario {user_obj.email}.")
                return {"respuesta": "🔒 Límite de preguntas alcanzado en tu plan. Actualizá para más.", "fuente": "sistema_limite"}
        else:
            logger.warning(f"[LOGIC] Token '{str(token)[:15]}...' proporcionado pero no válido.")
            
    if user_obj is None: 
        logger.info("[LOGIC] No se identificó usuario. Creando instancia de Anónimo Genérico.")
        session.setdefault("generic_anon_preguntas", 0)
        if session["generic_anon_preguntas"] >= 5:
             logger.info("[LOGIC] Límite de preguntas alcanzado para Anónimo Genérico.")
             return {"respuesta": "Alcanzaste el límite de preguntas para usuarios anónimos. ¡Registrate gratis para continuar!", "fuente": "sistema_limite"}
        session["generic_anon_preguntas"] += 1
        user_obj = GenericAnonUser()
    
    logger.info(f"[LOGIC] ==> User Obj Determinado: {type(user_obj).__name__}")
    logger.info(f"[LOGIC]     user_obj.nombre_empresa: {getattr(user_obj, 'nombre_empresa', 'N/A')}")
    logger.info(f"[LOGIC]     user_obj.horario (string crudo): '{getattr(user_obj, 'horario', 'N/A')}'")
    horario_json_val = getattr(user_obj, 'horario_json', 'N/A')
    logger.info(f"[LOGIC]     user_obj.horario_json (valor directo atributo/propiedad): {str(horario_json_val)[:250]}...")

    rubro_id_final: int = 1 
    rubro_nombre_final: str = "general"
    rubro_obj_final: Optional[Rubro] = None
    if rubro_nombre_frontend and isinstance(rubro_nombre_frontend, str) and rubro_nombre_frontend.strip():
        logger.info(f"[LOGIC] Intentando determinar rubro por parámetro frontend: '{rubro_nombre_frontend}'")
        rubro_obj_final = Rubro.query.filter(func.lower(Rubro.nombre) == rubro_nombre_frontend.lower().strip()).first()
        if rubro_obj_final:
            logger.info(f"[LOGIC] Rubro determinado por frontend: '{rubro_obj_final.nombre}' (ID: {rubro_obj_final.id})")
            if isinstance(user_obj, (GenericAnonUser, AnonUserPymeDemo)): 
                user_obj.nombre_empresa = getattr(rubro_obj_final, 'nombre_pyme_default_para_anon', f"Negocios de {rubro_obj_final.nombre}")
                user_obj.rubro_id = rubro_obj_final.id 
                logger.info(f"[LOGIC] Contexto de user_obj ({type(user_obj).__name__}) actualizado con info del rubro '{rubro_obj_final.nombre}'.")
        else: logger.warning(f"[LOGIC] Rubro '{rubro_nombre_frontend}' (frontend) no encontrado en BD.")
    if not rubro_obj_final and hasattr(user_obj, 'rubro_id') and user_obj.rubro_id:
        logger.info(f"[LOGIC] Rubro no determinado por frontend. Usando rubro_id del user_obj: {user_obj.rubro_id}")
        rubro_obj_final = db.session.get(Rubro, user_obj.rubro_id)
        if not rubro_obj_final: logger.warning(f"[LOGIC] Rubro ID {user_obj.rubro_id} (del user_obj) no encontrado.")
    if not rubro_obj_final: 
        logger.info(f"[LOGIC] No se pudo determinar rubro. Usando rubro general (ID 1).")
        rubro_obj_final = db.session.get(Rubro, 1) 
    if rubro_obj_final:
        rubro_id_final = rubro_obj_final.id
        rubro_nombre_final = rubro_obj_final.nombre.lower().strip()
    else: 
        logger.error(f"[LOGIC] ¡ERROR CRÍTICO! No se encontró el rubro general ID 1. Usando 'general'.")
    logger.info(f"[LOGIC] ==> Rubro Final para consulta: '{rubro_nombre_final}' (ID: {rubro_id_final})")

    horario_json_str_para_contexto = "[]"
    if isinstance(user_obj, User): horario_json_str_para_contexto = user_obj.horario if user_obj.horario and user_obj.horario.strip() else '[]' 
    elif hasattr(user_obj, 'horario_json') and isinstance(user_obj.horario_json, str): horario_json_str_para_contexto = user_obj.horario_json if user_obj.horario_json and user_obj.horario_json.strip() else '[]'
    
    user_profile_context = {
        "nombre_empresa": getattr(user_obj, "nombre_empresa", "la tienda"),
        "rubro_nombre": rubro_nombre_final,
        "telefono_raw": getattr(user_obj, "telefono", ""),
        "link_web": getattr(user_obj, "link_web", ""),
        "direccion_completa": f"{getattr(user_obj, 'direccion', '')}, {getattr(user_obj, 'ciudad', '')}, {getattr(user_obj, 'provincia', '')}".replace(" ,", "").strip(', ').strip(),
        "horario_str": getattr(user_obj, "horario", "nuestro horario de atención"),
        "horario_json_str": horario_json_str_para_contexto, 
    }
    # CAMBIO: logger.debug a logger.info
    logger.info(f"[LOGIC] ==> User Profile Context para LLM (horario_json_str='{user_profile_context['horario_json_str']}'): {json.dumps(user_profile_context, ensure_ascii=False)}")

    horarios_para_prompt = user_profile_context['horario_str'] 
    try:
        if user_profile_context['horario_json_str'] and user_profile_context['horario_json_str'].strip() not in ['[]', '{}', 'null', '']:
            horarios_data = json.loads(user_profile_context['horario_json_str'])
            partes_horario = []
            if isinstance(horarios_data, list) and all(isinstance(h, dict) for h in horarios_data):
                for h_dict in horarios_data:
                    dia = h_dict.get("dia", "")
                    if not dia: continue 
                    if h_dict.get("cerrado"): partes_horario.append(f"{dia}: Cerrado")
                    else: partes_horario.append(f"{dia}: de {h_dict.get('abre','--:--')} a {h_dict.get('cierra','--:--')}")
            if partes_horario: horarios_para_prompt = ". ".join(p for p in partes_horario if p) + ("." if partes_horario else "")
        logger.info(f"[LOGIC] ==> Horarios formateados para prompt: {horarios_para_prompt}")
    except Exception as e_json_h_prompt:
        logger.warning(f"[LOGIC] No se pudo parsear/formatear horario_json_str ('{user_profile_context['horario_json_str']}') para prompt: {e_json_h_prompt}. Usando horario_str simple.")
    
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
        f"\n- Horarios de Atención: {horarios_para_prompt if horarios_para_prompt and horarios_para_prompt.strip('.') else 'Consultar nuestros horarios.'}"
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

    contexto_catalogo = ""
    if is_usuario_registrado_real and user_obj.id is not None:
        try:
            from services.qdrant_search import buscar_catalogo_qdrant, armar_respuesta_legible 
            resultados_qdrant = buscar_catalogo_qdrant(user_obj.id, pregunta, limite=3, score_min=0.68) 
            contexto_catalogo = armar_respuesta_legible(resultados_qdrant)
            if contexto_catalogo: 
                logger.info(f"[LOGIC] Contexto Qdrant para LLM (parcial): {contexto_catalogo[:150]}...")
                prompt_sistema_texto += f"\n\nINFORMACIÓN DEL CATÁLOGO RELEVANTE:\n{contexto_catalogo}"
            else: 
                logger.info("[LOGIC] Sin contexto de catálogo Qdrant para esta pregunta (o score bajo).")
                prompt_sistema_texto += "\nNo se encontró información específica del catálogo."
        except ImportError: logger.error("[LOGIC] Módulo Qdrant (qdrant_search) no encontrado."); prompt_sistema_texto += "\n(Error interno: sistema de catálogo no disponible)."
        except Exception as e_qdrant: logger.error(f"[LOGIC] Error buscando en Qdrant: {e_qdrant}", exc_info=True); prompt_sistema_texto += "\n(Error interno: problema al buscar en catálogo)."
    else:
        logger.info("[LOGIC] Usuario no es PYME registrada o no tiene ID, no se buscará en catálogo Qdrant.")
        prompt_sistema_texto += "\nNo hay un catálogo de productos específico para este modo. Responde con conocimiento general del rubro y la información de la empresa."

    prompt_sistema_texto += "\n\nInicia tu respuesta directamente al cliente, continuando la conversación de forma natural y concisa."
    # CAMBIO: logger.debug a logger.info
    logger.info(f"[LOGIC] ==> Prompt Sistema FINAL para Cohere (longitud: {len(prompt_sistema_texto)}): {prompt_sistema_texto[:500]}...")


    respuesta_obtenida_llm = "" 
    fuente_respuesta = "no_especificada_aun"
    try:
        from services.cohere_ai import get_cohere_response 
        
        MAX_MENSAJES_HISTORIAL_PARA_LLM = 6 
        historial_llm_raw = session.get(NOMBRE_HISTORIAL_SESION, [])
        historial_para_api = []
        for msg in historial_llm_raw[-(MAX_MENSAJES_HISTORIAL_PARA_LLM*2):]:
            role_api = "USER" if msg.get("role") == "user" else "CHATBOT"
            historial_para_api.append({"role": role_api, "message": msg.get("content","")})
        
        logger.info(f"[LOGIC] Enviando a Cohere: Pregunta='{pregunta[:50]}...'. Historial API: {len(historial_para_api)} mensajes. Preamble usado.")
        
        respuesta_obtenida_llm = get_cohere_response(
            current_message=pregunta,
            chat_history_for_api=historial_para_api,
            system_prompt_for_api=prompt_sistema_texto,
            rubro_id=rubro_id_final, 
            user_context=user_profile_context
        )
        logger.info(f"[LOGIC] Respuesta CRUDA de Cohere: '{respuesta_obtenida_llm}'")

        if respuesta_obtenida_llm and isinstance(respuesta_obtenida_llm, str) and len(respuesta_obtenida_llm.strip()) > 2:
            fuente_respuesta = "cohere"
            logger.info(f"[LOGIC] Cohere dio respuesta válida (longitud > 2).")
        else:
            logger.warning(f"[LOGIC] Respuesta de Cohere fue vacía o muy corta: '{respuesta_obtenida_llm}'. Se considera inválida.")
            respuesta_obtenida_llm = "" 
    except ImportError: 
        logger.error("[LOGIC] Módulo Cohere (services.cohere_ai) no encontrado.")
    except Exception as e_cohere: 
        logger.error(f"[LOGIC] Error al llamar a Cohere: {e_cohere}", exc_info=True)
        respuesta_obtenida_llm = ""

    respuesta_final_procesada = ""
    if respuesta_obtenida_llm:
        respuesta_final_procesada = reemplazar_placeholders(respuesta_obtenida_llm, user_obj)
    else: 
        logger.info("[LOGIC] Cohere no dio respuesta válida. Intentando FAQ Matcher...")
        try:
            from services.faq_matcher_spacy import buscar_en_faq_spacy
            faq_match_obj = buscar_en_faq_spacy(pregunta, rubro_id_final) 
            if faq_match_obj and faq_match_obj.answer:
                respuesta_final_procesada = reemplazar_placeholders(faq_match_obj.answer, user_obj)
                fuente_respuesta = "faq"
                logger.info(f"[LOGIC] Respuesta desde FAQ: '{respuesta_final_procesada[:100]}...'")
        except ImportError: logger.error("[LOGIC] Módulo FAQ (faq_matcher_spacy) no encontrado.")
        except Exception as e_faq: logger.warning(f"[LOGIC] Error en FAQ backup: {e_faq}", exc_info=True)

        if not respuesta_final_procesada:
            logger.info("[LOGIC] FAQ no dio respuesta. Intentando Intent Matcher...")
            try:
                from services.intent_matcher import buscar_en_intents
                intent_match_respuesta = buscar_en_intents(pregunta, rubro_nombre_final) 
                if intent_match_respuesta:
                    respuesta_final_procesada = reemplazar_placeholders(intent_match_respuesta, user_obj)
                    fuente_respuesta = "intent"
                    logger.info(f"[LOGIC] Respuesta desde Intents: '{respuesta_final_procesada[:100]}...'")
            except ImportError: logger.error("[LOGIC] Módulo Intent Matcher (intent_matcher) no encontrado.")
            except Exception as e_intent: logger.warning(f"[LOGIC] Error en Intents backup: {e_intent}", exc_info=True)

    if respuesta_final_procesada and respuesta_final_procesada.strip():
        session[NOMBRE_HISTORIAL_SESION].append({"role": "user", "content": pregunta})
        session[NOMBRE_HISTORIAL_SESION].append({"role": "assistant", "content": respuesta_final_procesada})
        session.modified = True
        logger.info(f"[LOGIC] Historial de sesión actualizado. Nuevo tamaño: {len(session[NOMBRE_HISTORIAL_SESION])}")

        if is_usuario_registrado_real and user_obj.id:
            try:
                user_obj.preguntas_usadas = (user_obj.preguntas_usadas or 0) + 1
                db.session.add(Conversacion(user_id=user_obj.id, pregunta=pregunta, respuesta=respuesta_final_procesada, fuente=fuente_respuesta, rubro=rubro_nombre_final))
                db.session.commit()
                logger.info(f"[LOGIC] Conversación guardada en BD para user {user_obj.id}.")
            except Exception as e_db_conv:
                logger.error(f"[LOGIC] Error guardando conversación en DB para user {user_obj.id}: {e_db_conv}", exc_info=True)
                db.session.rollback()
        
        respuesta_para_frontend = respuesta_final_procesada
        pyme_link_web_actual = getattr(user_obj, "link_web", "")
        if pyme_link_web_actual and fuente_respuesta not in ["faq", "sugerencia_sistema"]: 
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
        
        logger.info(f"✅ [LOGIC] Respuesta final (fuente: {fuente_respuesta}): '{respuesta_para_frontend[:100]}...'")
        return {"respuesta": respuesta_para_frontend, "nivel_usado": rubro_nombre_final, "fuente": fuente_respuesta}
    else: 
        logger.info("[LOGIC] Fallback final: Todos los sistemas no dieron respuesta útil. Usando sugerencias predefinidas del rubro.")
        sugerencias = sugerencias_por_rubro(rubro_id_final)
        if sugerencias:
            respuesta_sugerencias_base = "No encontré una respuesta directa para tu consulta. Quizás puedas intentar preguntando algo como: " + " · ".join(f"“{s}”" for s in sugerencias if s)
        else: 
            respuesta_sugerencias_base = "Lo siento, no pude entender bien tu pregunta. ¿Podrías intentar reformularla o preguntar sobre nuestros productos y servicios?"
        
        respuesta_final_procesada = reemplazar_placeholders(respuesta_sugerencias_base, user_obj)
        
        pyme_link_web_actual_fallback = getattr(user_obj, "link_web", "")
        if pyme_link_web_actual_fallback:
            link_absoluto_sug = pyme_link_web_actual_fallback
            if not link_absoluto_sug.startswith("http"): link_absoluto_sug = "https://" + link_absoluto_sug
            link_html_sug = (
                f'\n<div style="margin-top: 10px; font-size: 0.9em;">'
                f'También puedes <a href="{link_absoluto_sug}" target="_blank">visitar nuestra tienda online</a> para más información.'
                '</div>'
            )
            respuesta_final_procesada += link_html_sug

        session[NOMBRE_HISTORIAL_SESION].append({"role": "user", "content": pregunta}) 
        session.modified = True 
        logger.info(f"[LOGIC] Enviando respuesta de fallback con sugerencias.")
        return {"respuesta": respuesta_final_procesada, "fuente": "sugerencia_sistema"}

# --- FIN DE responder_chatboc ---