# services/logic.py
import logging
import random
from flask import session
from sqlalchemy import func
from models import User, Rubro, Sugerencia, Conversacion, db 
# from extensions import db # Decide cuál 'db' usar. Si el de models.py está bien configurado, es suficiente.
import re 
import json 
from typing import Any, Optional, Dict, List

logger = logging.getLogger(__name__)

# --- Clases Anónimas (como estaban, ya corregidas) ---
class _BaseAnonUser:
    nombre_empresa: str = "la tienda"; plan: str = "anonimo"; rubro_id: Optional[int] = None; link_web: str = ""; telefono: str = ""; direccion: str = ""; horario: str = "horario de atención habitual"; horario_json: str = '[]'; ciudad: str = ""; provincia: str = ""; pais: str = "Argentina"; latitud: Optional[float] = None; longitud: Optional[float] = None; id: Optional[int] = None 
class AnonUserPymeDemo(_BaseAnonUser):
    def __init__(self, preguntas_realizadas_sesion: int):
        super().__init__(); self.nombre_empresa = "Chatboc Demostración"; self.plan = "demo_pyme"; self.preguntas_usadas = preguntas_realizadas_sesion; self.limite_preguntas = 15; self.link_web = "https://www.chatboc.ar"; self.telefono = "+549111234567"; self.direccion = "Av. Corrientes 1234, CABA"; self.ciudad = "CABA"; self.provincia = "CABA"; self.horario = "Lunes a Viernes de 9hs a 18hs. Sábados de 9hs a 13hs."; self.horario_json = json.dumps([{"dia": "Lunes", "abre": "09:00", "cierra": "18:00", "cerrado": False},{"dia": "Martes", "abre": "09:00", "cierra": "18:00", "cerrado": False},{"dia": "Miércoles", "abre": "09:00", "cierra": "18:00", "cerrado": False},{"dia": "Jueves", "abre": "09:00", "cierra": "18:00", "cerrado": False},{"dia": "Viernes", "abre": "09:00", "cierra": "18:00", "cerrado": False},{"dia": "Sábado", "abre": "09:00", "cierra": "13:00", "cerrado": False},{"dia": "Domingo", "abre": "", "cierra": "", "cerrado": True}]); self.latitud = -34.6037; self.longitud = -58.3816 
class GenericAnonUser(_BaseAnonUser):
    def __init__(self):
        super().__init__(); self.plan = "anonimo_general"; self.preguntas_usadas = session.get("generic_anon_preguntas", 0); self.limite_preguntas = 5
# --- Fin Clases Anónimas ---

# --- Funciones Auxiliares (como estaban, ya corregidas) ---
def sugerencias_por_rubro(rubro_id: int) -> list:
    # ... (tu código de sugerencias_por_rubro) ...
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
    # ... (tu código de formatear_numero_whatsapp_simple) ...
    if not telefono_str: return ""
    numeros = re.sub(r'\D', '', str(telefono_str))
    if numeros.startswith(codigo_pais + "9") and len(numeros) == (len(codigo_pais) + 1 + 10): return numeros
    if numeros.startswith(codigo_pais) and not numeros.startswith(codigo_pais + "9") and len(numeros) == (len(codigo_pais) + 10): return codigo_pais + "9" + numeros[len(codigo_pais):]
    if len(numeros) == 10: return f"{codigo_pais}9{numeros}"
    logger.info(f"[LOGIC-WSP] Número '{telefono_str}' no formateado claramente a WhatsApp, devolviendo solo dígitos: '{numeros}'")
    return numeros

def reemplazar_placeholders(texto: str, user_obj: Any) -> str:
    # ... (tu código de reemplazar_placeholders, la versión completa y corregida que te pasé antes) ...
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

    horario_json_str_ctx = user_obj.horario if isinstance(user_obj, User) and user_obj.horario else getattr(user_obj, 'horario_json', '[]')
    if not horario_json_str_ctx or not horario_json_str_ctx.strip(): horario_json_str_ctx = '[]'
    
    user_profile_context = {
        "nombre_empresa": getattr(user_obj, "nombre_empresa", ""), "rubro_nombre": rubro_nombre_final,
        "telefono_raw": getattr(user_obj, "telefono", ""), "link_web": getattr(user_obj, "link_web", ""),
        "direccion_completa": f"{getattr(user_obj, 'direccion', '')}, {getattr(user_obj, 'ciudad', '')}, {getattr(user_obj, 'provincia', '')}".replace(" ,", "").strip(', '),
        "horario_str": getattr(user_obj, "horario", "Consultar horario"), "horario_json_str": horario_json_str_ctx, 
    }
    logger.info(f"[LOGIC] ==> User Profile Context para LLM: {json.dumps(user_profile_context, ensure_ascii=False, indent=2)}")

    horarios_para_prompt = user_profile_context['horario_str'] 
    try:
        if user_profile_context['horario_json_str'] and user_profile_context['horario_json_str'].strip() not in ['[]', '{}', 'null', '']:
            horarios_data = json.loads(user_profile_context['horario_json_str'])
            partes_horario = [f"{h.get('dia','')}: " + (f"de {h.get('abre','--:--')} a {h.get('cierra','--:--')}" if not h.get('cerrado') else "Cerrado") for h in horarios_data if isinstance(h,dict) and h.get('dia')]
            if partes_horario: horarios_para_prompt = ". ".join(partes_horario) + "."
        logger.info(f"[LOGIC] ==> Horarios formateados para prompt: {horarios_para_prompt}")
    except Exception as e_json_h_prompt: logger.warning(f"[LOGIC] Error parseando horario_json_str para prompt: {e_json_h_prompt}.")
    
    numero_intercambios_previos = len(session.get(NOMBRE_HISTORIAL_SESION, [])) // 2
    prompt_sistema_texto = ( f"Sos Chatboc, un asistente comercial experto de {user_profile_context['nombre_empresa']} (rubro: {user_profile_context['rubro_nombre']}), ubicada en {user_profile_context['direccion_completa']}. "
        # ... (resto de tu prompt_sistema_texto completo) ...
        f"\n- Horarios de Atención: {horarios_para_prompt if horarios_para_prompt and horarios_para_prompt.strip('.') else 'Consultar nuestros horarios.'}"
        # ...
    )
    contexto_catalogo = ""; # ... (Lógica Qdrant como antes) ...
    if is_usuario_registrado_real and user_obj.id is not None:
        try:
            from services.qdrant_search import buscar_catalogo_qdrant, armar_respuesta_legible 
            resultados_qdrant = buscar_catalogo_qdrant(user_obj.id, pregunta, limite=3); contexto_catalogo = armar_respuesta_legible(resultados_qdrant)
            if contexto_catalogo: logger.info(f"[LOGIC] Contexto Qdrant: {contexto_catalogo[:100]}..."); prompt_sistema_texto += f"\n\nINFO DEL CATÁLOGO:\n{contexto_catalogo}"
            else: logger.info("[LOGIC] Sin contexto Qdrant."); prompt_sistema_texto += "\nINFO DEL CATÁLOGO: No se encontró información específica del catálogo para esta consulta."
        except Exception as e_q: logger.error(f"[LOGIC] Error Qdrant: {e_q}"); prompt_sistema_texto += "\nINFO DEL CATÁLOGO: (Error al consultar catálogo)"
    else: prompt_sistema_texto += "\nINFO DEL CATÁLOGO: No hay catálogo específico para este modo."
    prompt_sistema_texto += "\n\nResponde directamente al cliente:"
    logger.info(f"[LOGIC] ==> Prompt Sistema FINAL para Cohere (longitud: {len(prompt_sistema_texto)}): {prompt_sistema_texto[:300]}...")

    respuesta_obtenida_llm = ""; fuente_respuesta = "no_especificada"
    try:
        from services.cohere_ai import get_cohere_response 
        historial_llm_raw = session.get(NOMBRE_HISTORIAL_SESION, [])
        historial_para_api = [{"role": "USER" if msg.get("role") == "user" else "CHATBOT", "message": msg.get("content","")} for msg in historial_llm_raw[-(6*2):]]
        logger.info(f"[LOGIC] Enviando a Cohere: Pregunta='{pregunta[:50]}...'. Historial API: {len(historial_para_api)}.")
        respuesta_obtenida_llm = get_cohere_response(message=pregunta, chat_history=historial_para_api, preamble=prompt_sistema_texto)
        logger.info(f"[LOGIC] Respuesta CRUDA de Cohere: '{respuesta_obtenida_llm}'")
        if respuesta_obtenida_llm and isinstance(respuesta_obtenida_llm, str) and len(respuesta_obtenida_llm.strip()) > 2:
            fuente_respuesta = "cohere"; logger.info(f"[LOGIC] Cohere dio respuesta válida.")
        else: logger.warning(f"[LOGIC] Respuesta Cohere vacía/corta: '{respuesta_obtenida_llm}'."); respuesta_obtenida_llm = "" 
    except Exception as e_cohere: logger.error(f"[LOGIC] Error llamando a Cohere: {e_cohere}", exc_info=True); respuesta_obtenida_llm = ""

    respuesta_final_procesada = ""
    if respuesta_obtenida_llm:
        respuesta_final_procesada = reemplazar_placeholders(respuesta_obtenida_llm, user_obj)
    else: 
        logger.info("[LOGIC] Cohere no dio respuesta. Intentando FAQ...")
        try:
            from services.faq_matcher_spacy import buscar_en_faq_spacy
            faq = buscar_en_faq_spacy(pregunta, rubro_id_final) 
            if faq and faq.answer: respuesta_final_procesada = reemplazar_placeholders(faq.answer, user_obj); fuente_respuesta = "faq"; logger.info(f"[LOGIC] Respuesta FAQ: {respuesta_final_procesada[:100]}...")
        except Exception as e_faq: logger.warning(f"[LOGIC] Error FAQ: {e_faq}", exc_info=True)
        if not respuesta_final_procesada:
            logger.info("[LOGIC] FAQ no dio respuesta. Intentando Intents...")
            try:
                from services.intent_matcher import buscar_en_intents
                intent_resp = buscar_en_intents(pregunta, rubro_nombre_final) 
                if intent_resp: respuesta_final_procesada = reemplazar_placeholders(intent_resp, user_obj); fuente_respuesta = "intent"; logger.info(f"[LOGIC] Respuesta Intent: {respuesta_final_procesada[:100]}...")
            except Exception as e_intent: logger.warning(f"[LOGIC] Error Intents: {e_intent}", exc_info=True)

    if respuesta_final_procesada and respuesta_final_procesada.strip():
        session[NOMBRE_HISTORIAL_SESION].extend([{"role": "user", "content": pregunta}, {"role": "assistant", "content": respuesta_final_procesada}])
        session.modified = True; logger.info(f"[LOGIC] Historial actualizado. Nuevo tamaño: {len(session[NOMBRE_HISTORIAL_SESION])}")
        if is_usuario_registrado_real and user_obj.id:
            try:
                user_obj.preguntas_usadas = (user_obj.preguntas_usadas or 0) + 1; db.session.add(Conversacion(user_id=user_obj.id, pregunta=pregunta, respuesta=respuesta_final_procesada, fuente=fuente_respuesta, rubro=rubro_nombre_final)); db.session.commit()
                logger.info(f"[LOGIC] Conversación guardada para user {user_obj.id}.")
            except Exception as e_db: logger.error(f"[LOGIC] Error guardando conversación: {e_db}", exc_info=True); db.session.rollback()
        
        respuesta_para_frontend = respuesta_final_procesada
        # ... (lógica botón HTML) ...
        logger.info(f"✅ [LOGIC] Respuesta final (fuente: {fuente_respuesta}): '{respuesta_para_frontend[:100]}...'")
        return {"respuesta": respuesta_para_frontend, "nivel_usado": rubro_nombre_final, "fuente": fuente_respuesta}
    else: 
        logger.info("[LOGIC] Fallback final. Usando sugerencias.")
        sugs = sugerencias_por_rubro(rubro_id_final)
        resp_sug = ("No encontré una respuesta directa. Quizás puedas intentar: " + " · ".join(f"“{s}”" for s in sugs if s)) if sugs else "Lo siento, no entendí bien. ¿Podrías reformularlo?"
        resp_final_sug = reemplazar_placeholders(resp_sug, user_obj)
        # ... (lógica botón HTML para sugerencias) ...
        session[NOMBRE_HISTORIAL_SESION].append({"role": "user", "content": pregunta}); session.modified = True
        logger.info(f"[LOGIC] Enviando fallback con sugerencias: '{resp_final_sug[:100]}...'")
        return {"respuesta": resp_final_sug, "fuente": "sugerencia_sistema"}
# --- FIN DE responder_chatboc ---