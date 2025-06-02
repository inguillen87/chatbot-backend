# services/logic.py
import logging
import random
from flask import session
from sqlalchemy import func
from models import User, Rubro, Sugerencia, Conversacion, db 
import re 
import json 
from typing import Any, Optional, Dict, List

# Asegúrate de que utils.py esté en el mismo directorio 'services' o ajusta la ruta
from .utils import limpiar_texto_base, parse_precio_flexible 
# extraer_unidades_y_tipos_precio se usa en google_docai.py y procesar_catalogo_excel.py,
# no directamente aquí en logic.py, pero es bueno tenerlo en utils.py

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

# --- Funciones Auxiliares ---
def sugerencias_por_rubro(rubro_id: int) -> List[str]:
    try:
        sugerencias_obj = Sugerencia.query.filter_by(rubro_id=rubro_id).all()
        if sugerencias_obj:
            todas = [s.texto for s in sugerencias_obj if s.texto and s.texto.strip()] # Filtrar vacías
            if todas:
                logger.info(f"[LOGIC-SUG] Sugerencias para rubro {rubro_id} (total: {len(todas)}): {todas[:3]}") 
                return random.sample(todas, min(3, len(todas)))
        if rubro_id != 1: 
            fallback_obj = Sugerencia.query.filter_by(rubro_id=1).all()
            if fallback_obj:
                todas_fallback = [s.texto for s in fallback_obj if s.texto and s.texto.strip()]
                if todas_fallback:
                    logger.info(f"[LOGIC-SUG] No se encontraron sugerencias para rubro {rubro_id}, usando fallback general.")
                    return random.sample(todas_fallback, min(3, len(todas_fallback)))
        logger.info(f"[LOGIC-SUG] No se encontraron sugerencias válidas para rubro {rubro_id} ni en fallback. Usando defaults.")
        return ["¿Cuáles son sus horarios de atención?", "¿Qué productos ofrecen?", "¿Cómo puedo realizar una compra?"]
    except Exception as e:
        logger.error(f"[LOGIC-SUG] Error buscando sugerencias para rubro_id {rubro_id}: {e}", exc_info=True)
        return ["Disculpa, tuve un problema al buscar sugerencias en este momento."]

def formatear_numero_whatsapp_simple(telefono_str: Optional[str], codigo_pais: str = "54") -> str:
    if not telefono_str: return ""
    numeros = re.sub(r'\D', '', str(telefono_str))
    if numeros.startswith(codigo_pais + "9") and len(numeros) == (len(codigo_pais) + 1 + 10): return numeros
    if numeros.startswith(codigo_pais) and not numeros.startswith(codigo_pais + "9") and len(numeros) == (len(codigo_pais) + 10): return codigo_pais + "9" + numeros[len(codigo_pais):]
    if len(numeros) == 10: return f"{codigo_pais}9{numeros}"
    logger.info(f"[LOGIC-WSP] Número '{telefono_str}' no formateado claramente a WhatsApp, devolviendo solo dígitos: '{numeros}'")
    return numeros

def reemplazar_placeholders(texto: Optional[str], user_obj: Any) -> str:
    if not texto or not isinstance(texto, str): return ""
    placeholders_conocidos = {
        "[nombreEmpresa]": "nombre_empresa", "[linkWeb]": "link_web", "[telefono]": "telefono",
        "[direccion]": "direccion", "[horario]": "horario", "[ciudad]" : "ciudad",
        "[provincia]": "provincia", "[pais]": "pais", "[horarioDetallado]": "horario_json",
    }
    defaults_textos = { "nombre_empresa": "nuestra empresa", "link_web": "nuestro sitio web", "telefono": "nuestro número de contacto", "direccion": "nuestra dirección", "horario": "nuestro horario de atención", "ciudad": "nuestra ciudad", "provincia": "nuestra provincia", "pais": "nuestro país", "horario_json": "consultar nuestros horarios detallados",}
    texto_procesado = texto
    for ph_template, attr_key in placeholders_conocidos.items():
        valor_atributo = None; valor_para_reemplazo = defaults_textos.get(attr_key, f"") 
        if user_obj:
            if attr_key == "horario_json": 
                if isinstance(user_obj, User): valor_atributo = user_obj.horario_json 
                else: valor_atributo = getattr(user_obj, attr_key, None)
            elif attr_key == "horario" and isinstance(user_obj, User): valor_atributo = user_obj.horario 
            else: valor_atributo = getattr(user_obj, attr_key, None)
        if valor_atributo is not None:
            if ph_template == "[telefono]":
                tel_str = str(valor_atributo).strip()
                if tel_str: numero_wsp = formatear_numero_whatsapp_simple(tel_str); valor_para_reemplazo = f'{tel_str} (<a href="https://wa.me/{numero_wsp}" target="_blank" style="color: green; text-decoration: underline; font-weight:bold;">Contactar por WhatsApp</a>)' if numero_wsp else tel_str
                else: valor_para_reemplazo = defaults_textos.get(attr_key, "")
            elif ph_template == "[linkWeb]":
                link_str = str(valor_atributo).strip()
                if link_str: link_abs = link_str;_ = link_abs.startswith("http") or (link_abs := "https://" + link_abs); valor_para_reemplazo = link_abs
                else: valor_para_reemplazo = defaults_textos.get(attr_key, "")
            elif ph_template == "[horarioDetallado]":
                horarios_data_parseada = None
                if isinstance(valor_atributo, dict) or isinstance(valor_atributo, list): horarios_data_parseada = valor_atributo
                elif isinstance(valor_atributo, str) and valor_atributo.strip() and valor_atributo not in ['[]', '{}', 'null']:
                    try: horarios_data_parseada = json.loads(valor_atributo)
                    except json.JSONDecodeError: logger.warning(f"[LOGIC-PH] Error parseando '{attr_key}' ('{valor_atributo}') para [horarioDetallado]. User: {type(user_obj).__name__}")
                if horarios_data_parseada and isinstance(horarios_data_parseada, list) and all(isinstance(h, dict) for h in horarios_data_parseada):
                    partes = [f"{h.get('dia','')}: Cerrado" if h.get('cerrado') else f"{h.get('dia','')}: de {h.get('abre','--:--')} a {h.get('cierra','--:--')}" for h in horarios_data_parseada if h.get('dia')]
                    valor_para_reemplazo = ". ".join(p for p in partes if p) + ("." if partes else "");_ = (not valor_para_reemplazo.strip() or valor_para_reemplazo == ".") and (valor_para_reemplazo := defaults_textos.get(attr_key))
                else: logger.info(f"[LOGIC-PH] No se pudo formatear horario para {ph_template} con valor '{str(valor_atributo)[:50]}...'. Fallback."); valor_para_reemplazo = str(getattr(user_obj, "horario", defaults_textos.get("horario")))
            elif isinstance(valor_atributo, str) and valor_atributo.strip() == "": valor_para_reemplazo = defaults_textos.get(attr_key, "")
            elif not isinstance(valor_atributo, (dict, list)): valor_para_reemplazo = str(valor_atributo)
        texto_procesado = texto_procesado.replace(ph_template, valor_para_reemplazo)
    def reemplazar_desconocido_callback(match): logger.warning(f"[LOGIC-PH] Placeholder desconocido: [{match.group(1)}]"); return "" 
    texto_procesado = re.sub(r"\[([^\]\[\s]+?)\]", reemplazar_desconocido_callback, texto_procesado)
    return texto_procesado
# --- FIN Funciones Auxiliares ---

def responder_chatboc(pregunta: str, token: str | None, rubro_nombre_frontend: str | None = None) -> Dict[str, Any]:
    logger.info(f"▶️ [LOGIC] Inicio responder_chatboc: Pregunta='{pregunta[:100]}...' Token='{str(token)[:15] if token else 'N/A'}' RubroFrontend='{rubro_nombre_frontend}'")

    if not pregunta or not pregunta.strip():
        logger.warning("[LOGIC] Pregunta vacía recibida.")
        return {"error": "La pregunta no puede estar vacía"}

    NOMBRE_HISTORIAL_SESION = 'historial_chat_cliente'
    if NOMBRE_HISTORIAL_SESION not in session:
        session[NOMBRE_HISTORIAL_SESION] = []
        logger.info(f"[LOGIC] '{NOMBRE_HISTORIAL_SESION}' inicializado.")

    user_obj: Any = None 
    is_usuario_registrado_real: bool = False
    is_pyme_demo_anon = token is not None and token.startswith("demo-anon-") 

    if is_pyme_demo_anon:
        logger.info(f"[LOGIC] Modo Demo PYME Anónimo (token: {token}).")
        session.setdefault("pyme_demo_anon_preguntas", 0)
        if session["pyme_demo_anon_preguntas"] >= 15: 
            logger.info("[LOGIC] Límite Demo PYME Anónimo alcanzado.")
            return {"respuesta": "🔒 Límite de 15 preguntas en modo demo PYME alcanzado. Registrate para seguir.", "fuente": "sistema_limite"}
        session["pyme_demo_anon_preguntas"] += 1
        user_obj = AnonUserPymeDemo(session["pyme_demo_anon_preguntas"])
    elif token: 
        db_user_found = User.query.filter_by(token=token).first()
        if db_user_found:
            is_usuario_registrado_real = True; user_obj = db_user_found
            logger.info(f"[LOGIC] Usuario PYME autenticado: {user_obj.email} (ID: {user_obj.id})")
            preg_usadas = int(user_obj.preguntas_usadas or 0); lim_preg = int(user_obj.limite_preguntas or 50)
            if preg_usadas >= lim_preg:
                logger.info(f"[LOGIC] Límite preguntas alcanzado para {user_obj.email}.")
                return {"respuesta": "🔒 Límite de preguntas alcanzado en tu plan. Actualizá para más.", "fuente": "sistema_limite"}
        else: logger.warning(f"[LOGIC] Token '{str(token)[:15]}...' no válido.")
            
    if user_obj is None: 
        logger.info("[LOGIC] Usuario Anónimo Genérico.")
        session.setdefault("generic_anon_preguntas", 0)
        if session["generic_anon_preguntas"] >= 5:
             logger.info("[LOGIC] Límite preguntas Anónimo Genérico alcanzado.")
             return {"respuesta": "Alcanzaste el límite de preguntas para usuarios anónimos. ¡Registrate gratis para continuar!", "fuente": "sistema_limite"}
        session["generic_anon_preguntas"] += 1
        user_obj = GenericAnonUser()
    
    logger.info(f"[LOGIC] ==> User Obj Determinado: {type(user_obj).__name__}")
    logger.info(f"[LOGIC]     user_obj.horario (raw): '{getattr(user_obj, 'horario', 'N/A')}'")
    logger.info(f"[LOGIC]     user_obj.horario_json (prop/attr): {str(getattr(user_obj, 'horario_json', 'N/A'))[:250]}...")

    rubro_id_final: int = 1; rubro_nombre_final: str = "general"; rubro_obj_final: Optional[Rubro] = None
    if rubro_nombre_frontend and isinstance(rubro_nombre_frontend, str) and rubro_nombre_frontend.strip():
        logger.info(f"[LOGIC] Intentando rubro por frontend: '{rubro_nombre_frontend}'")
        rubro_obj_final = Rubro.query.filter(func.lower(Rubro.nombre) == rubro_nombre_frontend.lower().strip()).first()
        if rubro_obj_final: logger.info(f"[LOGIC] Rubro por frontend: '{rubro_obj_final.nombre}' (ID: {rubro_obj_final.id})")
        else: logger.warning(f"[LOGIC] Rubro '{rubro_nombre_frontend}' (frontend) no encontrado.")
    if not rubro_obj_final and hasattr(user_obj, 'rubro_id') and user_obj.rubro_id:
        logger.info(f"[LOGIC] Usando rubro_id del user_obj: {user_obj.rubro_id}")
        rubro_obj_final = db.session.get(Rubro, user_obj.rubro_id)
        if not rubro_obj_final: logger.warning(f"[LOGIC] Rubro ID {user_obj.rubro_id} (del user_obj) no encontrado.")
    if not rubro_obj_final: rubro_obj_final = db.session.get(Rubro, 1); logger.info(f"[LOGIC] Usando rubro general (ID 1).")
    if rubro_obj_final: rubro_id_final = rubro_obj_final.id; rubro_nombre_final = rubro_obj_final.nombre.lower().strip()
    else: logger.error(f"[LOGIC] ¡ERROR CRÍTICO! Rubro general ID 1 no encontrado.")
    logger.info(f"[LOGIC] ==> Rubro Final: '{rubro_nombre_final}' (ID: {rubro_id_final})")

    horario_json_str_ctx = getattr(user_obj, 'horario', '[]') if isinstance(user_obj, User) else getattr(user_obj, 'horario_json', '[]')
    if not horario_json_str_ctx or not horario_json_str_ctx.strip(): horario_json_str_ctx = '[]'
    
    user_profile_context = {
        "nombre_empresa": getattr(user_obj, "nombre_empresa", ""), "rubro_nombre": rubro_nombre_final,
        "telefono_raw": getattr(user_obj, "telefono", ""), "link_web": getattr(user_obj, "link_web", ""),
        "direccion_completa": f"{getattr(user_obj, 'direccion', '')}, {getattr(user_obj, 'ciudad', '')}, {getattr(user_obj, 'provincia', '')}".replace(" ,", "").strip(', '),
        "horario_str": getattr(user_obj, "horario", "Consultar horario"), # Para el placeholder [horario]
        "horario_json_str": horario_json_str_ctx, # Para el prompt y [horarioDetallado]
    }
    logger.info(f"[LOGIC] ==> User Profile Context para LLM (previo a formatear horario para prompt): {json.dumps(user_profile_context, ensure_ascii=False, indent=2)}")

    horarios_para_prompt = user_profile_context['horario_str'] # Default al string simple o JSON string de User.horario
    try:
        # Solo intentar parsear y formatear si horario_json_str es un string JSON válido y no vacío
        if user_profile_context['horario_json_str'] and user_profile_context['horario_json_str'].strip().startswith('['):
            horarios_data = json.loads(user_profile_context['horario_json_str'])
            partes_horario = []
            if isinstance(horarios_data, list) and all(isinstance(h, dict) for h in horarios_data):
                for h_dict in horarios_data:
                    dia = h_dict.get("dia", "")
                    if not dia: continue 
                    if h_dict.get("cerrado"): partes_horario.append(f"{dia}: Cerrado")
                    else: partes_horario.append(f"{dia}: de {h_dict.get('abre','--:--')} a {h_dict.get('cierra','--:--')}")
            if partes_horario: horarios_para_prompt = ". ".join(partes_horario) + "."
        logger.info(f"[LOGIC] ==> Horarios formateados para prompt (usado en system_prompt): {horarios_para_prompt}")
    except Exception as e_json_h_p: logger.warning(f"[LOGIC] Error parseando horario_json_str para prompt: {e_json_h_p}. Se usará horario_str original.")
    
    numero_intercambios_previos = len(session.get(NOMBRE_HISTORIAL_SESION, [])) // 2
    
    # --- CONSTRUCCIÓN DEL PROMPT DEL SISTEMA ---
    prompt_sistema_texto = (
        f"Sos Chatboc, un asistente comercial experto de {user_profile_context['nombre_empresa']} (rubro: {user_profile_context['rubro_nombre']}), ubicada en {user_profile_context['direccion_completa']}. "
        f"Tu principal objetivo es entender rápidamente las necesidades del cliente y guiarlo hacia una compra o una visita a la tienda online ({user_profile_context['link_web'] if user_profile_context['link_web'] else 'nuestra página web'}) en los próximos 2-4 intercambios. "
        f"Ya has tenido {numero_intercambios_previos} intercambios con este cliente (revisa el historial de conversación que te proveo más abajo si es necesario). "
        "Sé amable, muy proactivo, resolutivo y persuasivo. Tus respuestas deben ser breves (máximo 2-3 frases), directas y valiosas. Ve al grano. Evita el texto de relleno o introducciones innecesarias. Proporciona la información clave de forma concisa. "
        "Haz preguntas claras si necesitas más información para ayudarle. "
        "Si el cliente muestra interés en un producto o servicio, intenta cerrar la venta ofreciendo añadirlo al carrito, llevarlo a la página del producto en la tienda online, o facilitando el siguiente paso de forma clara y simple. "
        "No menciones que eres una IA ni un 'asistente virtual'. Habla como un vendedor humano y entusiasta. "
        f"\nINFORMACIÓN CLAVE DE {user_profile_context['nombre_empresa']}:"
        f"\n- Teléfono de Contacto: {user_profile_context['telefono_raw'] if user_profile_context['telefono_raw'] else 'No disponible en este momento.'}"
        f"\n- Dirección Física: {user_profile_context['direccion_completa'] if user_profile_context['direccion_completa'].strip(', ') else 'Consultar por nuestra ubicación.'}"
        f"\n- Horarios de Atención: {horarios_para_prompt if horarios_para_prompt and horarios_para_prompt.strip('.') and 'consultar' not in horarios_para_prompt.lower() else 'Por favor, consulta nuestros horarios.'}"
        f"\n- Sitio Web / Tienda Online: {user_profile_context['link_web'] if user_profile_context['link_web'] else 'No disponible en este momento.'}"
        "\nINSTRUCCIONES ADICIONALES:"
        "\n- Si la pregunta del cliente es incomprensible o solo son caracteres al azar, responde amablemente que no entendiste la consulta y ofrece ayuda general. Ejemplo: 'Disculpa, no entendí bien tu consulta. Puedo ayudarte con información sobre nuestros productos, precios, horarios o cómo comprar. ¿En qué te puedo asistir hoy?'"
        "\n- Si te preguntan '¿Están abiertos ahora?' o sobre horarios específicos, BASA tu respuesta ÚNICAMENTE en la información de 'Horarios de Atención' proporcionada arriba. Calcula si está abierto o cerrado según la hora actual si es necesario (asume zona horaria local de Argentina, GMT-3)."
        "\n- Si se te pide información que NO está en este prompt (ej. detalles muy específicos de un producto no listado en el catálogo o historia de la empresa), indica amablemente que no tienes esa información específica pero puedes ayudar con otros temas o dirigirlos al sitio web."
    )
    
    contexto_catalogo = ""; 
    if is_usuario_registrado_real and user_obj.id is not None:
        logger.info(f"[LOGIC] Usuario PYME {user_obj.id}. Intentando búsqueda en Qdrant para pregunta: '{pregunta[:50]}...'")
        try:
            from services.qdrant_search import buscar_catalogo_qdrant, armar_respuesta_legible 
            resultados_qdrant = buscar_catalogo_qdrant(user_obj.id, pregunta, limite=3, score_min=0.65) # Ajustado score_min
            contexto_catalogo = armar_respuesta_legible(resultados_qdrant)
            if contexto_catalogo: 
                logger.info(f"[LOGIC] Contexto Qdrant encontrado: {contexto_catalogo[:100]}...")
                prompt_sistema_texto += f"\n\nINFORMACIÓN DE NUESTRO CATÁLOGO QUE PODRÍA SER RELEVANTE (USA ESTO SI CORRESPONDE A LA PREGUNTA DEL CLIENTE):\n{contexto_catalogo}"
            else: 
                logger.info("[LOGIC] Sin contexto relevante de catálogo Qdrant para esta pregunta (o score bajo).")
                prompt_sistema_texto += "\n(No se encontró información de productos específicos en el catálogo para esta consulta. Responde basándote en la información general de la empresa o pide más detalles al cliente si es sobre un producto)."
        except Exception as e_q: 
            logger.error(f"[LOGIC] Error buscando o armando contexto de Qdrant: {e_q}", exc_info=True)
            prompt_sistema_texto += "\n(Hubo un problema técnico al intentar buscar en nuestro catálogo de productos en este momento)."
    else:
        logger.info("[LOGIC] Sin búsqueda en catálogo Qdrant (usuario anónimo, demo, o sin ID).")
        prompt_sistema_texto += "\n(No hay un catálogo de productos específico para este modo de chat. Responde con conocimiento general del rubro y la información de la empresa ya proporcionada)."

    prompt_sistema_texto += "\n\nTU RESPUESTA DIRECTA AL CLIENTE (sé breve y ve al grano):"
    logger.info(f"[LOGIC] ==> Prompt Sistema FINAL para Cohere (longitud: {len(prompt_sistema_texto)}). Primeros 400 chars: {prompt_sistema_texto[:400]}...")
    logger.info(f"[LOGIC] ... Últimos 300 chars del prompt: ...{prompt_sistema_texto[-300:]}")


    respuesta_obtenida_llm = ""; fuente_respuesta = "no_especificada"
    try:
        from services.cohere_ai import get_cohere_response 
        historial_para_api = [{"role": "USER" if msg.get("role") == "user" else "CHATBOT", "message": msg.get("content","")} for msg in session.get(NOMBRE_HISTORIAL_SESION, [])[-(6*2):]] # Últimos 6 intercambios
        
        logger.info(f"[LOGIC] Enviando a Cohere: Pregunta='{pregunta[:50]}...'. Historial API: {len(historial_para_api)}.")
        
        respuesta_obtenida_llm = get_cohere_response(
            message=pregunta,
            chat_history=historial_para_api,
            preamble=prompt_sistema_texto, # Este es el system_prompt
            # rubro_id y user_context se pasan para que get_cohere_response los tenga disponibles si los usa internamente
            rubro_id=rubro_id_final, 
            user_context=user_profile_context 
        )
        logger.info(f"[LOGIC] Respuesta CRUDA de Cohere: '{respuesta_obtenida_llm}'")

        # MEJORA: Verificar si la respuesta de Cohere es uno de nuestros mensajes de error internos
        mensajes_error_cohere_interno = [
            "Error interno: Asistente IA no disponible en este momento (C01).",
            "Error: Mensaje de usuario inválido para el asistente IA.",
            "Error interno: No se pudo inicializar el asistente IA (C02).",
            "Lo siento, no pude procesar tu solicitud en este momento con el asistente IA (E01).",
            "Lo siento, tuve un problema inesperado al intentar generar una respuesta (E02)."
        ]
        if respuesta_obtenida_llm in mensajes_error_cohere_interno:
            logger.error(f"[LOGIC] Cohere devolvió un mensaje de error interno: '{respuesta_obtenida_llm}'")
            respuesta_obtenida_llm = "" # Forzar a vacío para que no se muestre al usuario y se usen fallbacks
        elif respuesta_obtenida_llm and isinstance(respuesta_obtenida_llm, str) and len(respuesta_obtenida_llm.strip()) > 2:
            fuente_respuesta = "cohere"; logger.info(f"[LOGIC] Cohere dio respuesta aparentemente válida.")
        else: 
            logger.warning(f"[LOGIC] Respuesta Cohere vacía/corta tras chequeo de errores: '{respuesta_obtenida_llm}'."); respuesta_obtenida_llm = "" 
    except Exception as e_cohere: 
        logger.error(f"[LOGIC] Error crítico al llamar o procesar respuesta de Cohere: {e_cohere}", exc_info=True); respuesta_obtenida_llm = ""

    respuesta_final_procesada = ""
    if respuesta_obtenida_llm:
        respuesta_final_procesada = reemplazar_placeholders(respuesta_obtenida_llm, user_obj)
    else: 
        logger.info("[LOGIC] Cohere no dio respuesta útil. Intentando FAQ...")
        try:
            from services.faq_matcher_spacy import buscar_en_faq_spacy
            faq = buscar_en_faq_spacy(pregunta, rubro_id_final) 
            if faq and faq.answer: 
                respuesta_final_procesada = reemplazar_placeholders(faq.answer, user_obj)
                fuente_respuesta = "faq"; 
                logger.info(f"[LOGIC] Respuesta FAQ: {respuesta_final_procesada[:100]}...")
        except Exception as e_faq: logger.warning(f"[LOGIC] Error FAQ: {e_faq}", exc_info=True)
        
        if not respuesta_final_procesada:
            logger.info("[LOGIC] FAQ no dio respuesta. Intentando Intents...")
            try:
                from services.intent_matcher import buscar_en_intents
                intent_resp = buscar_en_intents(pregunta, rubro_nombre_final) 
                if intent_resp: 
                    respuesta_final_procesada = reemplazar_placeholders(intent_resp, user_obj)
                    fuente_respuesta = "intent"; 
                    logger.info(f"[LOGIC] Respuesta Intent: {respuesta_final_procesada[:100]}...")
            except Exception as e_intent: logger.warning(f"[LOGIC] Error Intents: {e_intent}", exc_info=True)

    if respuesta_final_procesada and respuesta_final_procesada.strip():
        session[NOMBRE_HISTORIAL_SESION].extend([{"role": "user", "content": pregunta}, {"role": "assistant", "content": respuesta_final_procesada}])
        session.modified = True; logger.info(f"[LOGIC] Historial actualizado. Tamaño: {len(session[NOMBRE_HISTORIAL_SESION])}")
        if is_usuario_registrado_real and user_obj.id:
            try:
                # Asegurarse que user_obj sea la instancia de la BD para actualizar
                db_user_to_update = db.session.get(User, user_obj.id)
                if db_user_to_update:
                    db_user_to_update.preguntas_usadas = (db_user_to_update.preguntas_usadas or 0) + 1
                    db.session.add(Conversacion(user_id=user_obj.id, pregunta=pregunta, respuesta=respuesta_final_procesada, fuente=fuente_respuesta, rubro=rubro_nombre_final))
                    db.session.commit()
                    logger.info(f"[LOGIC] Conversación y contador de preguntas guardado para user {user_obj.id}.")
                else:
                    logger.error(f"[LOGIC] No se pudo encontrar al usuario {user_obj.id} en la BD para actualizar preguntas_usadas.")
            except Exception as e_db: logger.error(f"[LOGIC] Error guardando conversación/actualizando user: {e_db}", exc_info=True); db.session.rollback()
        
        respuesta_para_frontend = respuesta_final_procesada
        # --- LÓGICA DE BOTONES "INTELIGENTE" ---
        pyme_link_web_actual = getattr(user_obj, "link_web", "")
        # Añadir botón de tienda online si la respuesta NO es de FAQ (que podría tener sus propios links),
        # NO es una sugerencia del sistema (que podría tener su propio link),
        # Y si la respuesta de Cohere parece orientada a la venta o información de productos.
        deberia_anadir_boton_tienda = False
        if fuente_respuesta == "cohere":
            # Palabras clave que sugieren que un botón de tienda sería útil
            keywords_venta = ["comprar", "pedido", "tienda online", "ver más", "catálogo", "productos", "opciones", "precio de"]
            if any(kw in respuesta_obtenida_llm.lower() for kw in keywords_venta) or \
               any(kw in pregunta.lower() for kw in keywords_venta): # También si la pregunta original lo sugiere
                deberia_anadir_boton_tienda = True
        
        if pyme_link_web_actual and deberia_anadir_boton_tienda:
            link_absoluto = pyme_link_web_actual
            if not link_absoluto.startswith("http"): link_absoluto = "https://" + link_absoluto
            boton_html = (
                f'\n<div style="margin-top: 15px; padding-top: 10px; border-top: 1px solid #eee;">'
                f'<a href="{link_absoluto}" target="_blank" '
                f'style="display: inline-block; background-color: #007bff; color: white; padding: 10px 20px; '
                f'text-align: center; text-decoration: none; border-radius: 5px; font-size: 16px; font-weight: bold;">'
                'Visitar Tienda Online' # Texto del botón
                '</a></div>'
            )
            respuesta_para_frontend += boton_html
            logger.info("[LOGIC] Botón de Tienda Online añadido a la respuesta.")
        # El botón de WhatsApp se añade a través del placeholder [telefono] si está en la respuesta.

        logger.info(f"✅ [LOGIC] Respuesta final (fuente: {fuente_respuesta}): '{respuesta_para_frontend[:100]}...'")
        return {"respuesta": respuesta_para_frontend, "nivel_usado": rubro_nombre_final, "fuente": fuente_respuesta}
    else: 
        logger.info("[LOGIC] Fallback final: Todos los sistemas no dieron respuesta útil. Usando sugerencias del rubro.")
        sugs = sugerencias_por_rubro(rubro_id_final)
        if sugs:
            respuesta_sugerencias_base = "No encontré una respuesta directa para tu consulta. Quizás puedas intentar preguntando algo como: " + " · ".join(f"“{s}”" for s in sugs if s)
        else: 
            respuesta_sugerencias_base = "Lo siento, no pude entender bien tu pregunta. ¿Podrías intentar reformularla o preguntar sobre nuestros productos y servicios?"
        
        respuesta_final_procesada = reemplazar_placeholders(respuesta_sugerencias_base, user_obj)
        
        pyme_link_web_actual_fallback = getattr(user_obj, "link_web", "")
        if pyme_link_web_actual_fallback: # Añadir siempre el link a la tienda en el fallback si existe
            link_absoluto_sug = pyme_link_web_actual_fallback
            if not link_absoluto_sug.startswith("http"): link_absoluto_sug = "https://" + link_absoluto_sug
            link_html_sug = (
                f'\n<div style="margin-top: 10px; font-size: 0.9em;">'
                f'También puedes <a href="{link_absoluto_sug}" target="_blank" style="color: #60a5fa; text-decoration: underline;">visitar nuestra tienda online</a> para más información.'
                '</div>'
            )
            respuesta_final_procesada += link_html_sug

        session[NOMBRE_HISTORIAL_SESION].append({"role": "user", "content": pregunta}); session.modified = True
        logger.info(f"[LOGIC] Enviando respuesta de fallback con sugerencias: '{respuesta_final_procesada[:100]}...'")
        return {"respuesta": respuesta_final_procesada, "fuente": "sugerencia_sistema"}

# --- FIN DE responder_chatboc ---