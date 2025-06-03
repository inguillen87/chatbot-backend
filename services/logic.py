# services/logic.py
import logging
import random
from flask import session
from sqlalchemy import func
from models import User, Rubro, Sugerencia, Conversacion, db 
import re 
import json 
from typing import Any, Optional, Dict, List
import urllib.parse 

from .utils import limpiar_texto_base 

logger = logging.getLogger(__name__)

# --- Clases Anónimas (como las tenías) ---
class _BaseAnonUser:
    nombre_empresa: str = "la tienda"; plan: str = "anonimo"; rubro_id: Optional[int] = None; link_web: str = ""; telefono: str = ""; direccion: str = "horario de atención habitual"; horario_json: str = '[]'; ciudad: str = ""; provincia: str = ""; pais: str = "Argentina"; latitud: Optional[float] = None; longitud: Optional[float] = None; id: Optional[int] = None 
class AnonUserPymeDemo(_BaseAnonUser):
    def __init__(self, preguntas_realizadas_sesion: int):
        super().__init__(); self.nombre_empresa = "Chatboc Demostración"; self.plan = "demo_pyme"; self.preguntas_usadas = preguntas_realizadas_sesion; self.limite_preguntas = 15; self.link_web = "https://www.chatboc.ar"; self.telefono = "+549111234567"; self.direccion = "Av. Corrientes 1234, CABA"; self.ciudad = "CABA"; self.provincia = "CABA"; self.horario = "Lunes a Viernes de 9hs a 18hs. Sábados de 9hs a 13hs."; self.horario_json = json.dumps([{"dia": "Lunes", "abre": "09:00", "cierra": "18:00", "cerrado": False},{"dia": "Martes", "abre": "09:00", "cierra": "18:00", "cerrado": False},{"dia": "Miércoles", "abre": "09:00", "cierra": "18:00", "cerrado": False},{"dia": "Jueves", "abre": "09:00", "cierra": "18:00", "cerrado": False},{"dia": "Viernes", "abre": "09:00", "cierra": "18:00", "cerrado": False},{"dia": "Sábado", "abre": "09:00", "cierra": "13:00", "cerrado": False},{"dia": "Domingo", "abre": "", "cierra": "", "cerrado": True}]); self.latitud = -34.6037; self.longitud = -58.3816 
class GenericAnonUser(_BaseAnonUser):
    def __init__(self):
        super().__init__(); self.plan = "anonimo_general"; self.preguntas_usadas = session.get("generic_anon_preguntas", 0); self.limite_preguntas = 5
# --- Fin Clases Anónimas ---

def detectar_objecion(pregunta: str) -> Optional[str]:
    objeciones_precio = ["caro", "muy caro", "más barato", "barato", "precio alto", "está elevado", "no tengo tanto presupuesto"]
    indecision = ["lo voy a pensar", "más tarde", "después veo", "no sé", "tengo que ver", "no estoy seguro", "voy a consultar"]
    texto = pregunta.lower()
    if any(o in texto for o in objeciones_precio):
        return "Entiendo, trabajamos solo con productos de máxima calidad. ¿Querés que te pase una promo exclusiva por WhatsApp?"
    if any(i in texto for i in indecision):
        return "¡Perfecto! Si más adelante necesitás una oferta especial, avisame. ¿Te mando promos por WhatsApp?"
    return None

def es_pregunta_insolita(pregunta: str) -> bool:
    palabras_clave = ["precio", "comprar", "producto", "catálogo", "horario", "envío", "pago", "promoción"]
    return not any(palabra in pregunta.lower() for palabra in palabras_clave) and len(pregunta.strip()) > 4

# --- Funciones Auxiliares ---
def sugerencias_por_rubro(rubro_id: int) -> List[str]:
    try:
        sugs_obj = Sugerencia.query.filter_by(rubro_id=rubro_id).all()
        if sugs_obj: todas = [s.texto for s in sugs_obj if s.texto and s.texto.strip()]
        else: todas = []
        if not todas and rubro_id != 1:
            logger.info(f"[LOGIC-SUG] No sugerencias para rubro {rubro_id}, usando fallback general.")
            sugs_obj = Sugerencia.query.filter_by(rubro_id=1).all()
            if sugs_obj: todas = [s.texto for s in sugs_obj if s.texto and s.texto.strip()]
        if todas:
            logger.info(f"[LOGIC-SUG] Sugerencias para rubro (final): {len(todas)} encontradas.")
            return random.sample(todas, min(3, len(todas)))
        logger.info(f"[LOGIC-SUG] No sugerencias válidas. Usando defaults.")
        return ["¿Cuáles son sus horarios de atención?", "¿Qué productos ofrecen?", "¿Cómo puedo realizar una compra?"]
    except Exception as e:
        logger.error(f"[LOGIC-SUG] Error buscando sugerencias: {e}", exc_info=True)
        return ["Disculpa, tuve un problema al buscar sugerencias."]

def formatear_numero_whatsapp_simple(telefono_str: Optional[str], codigo_pais: str = "54") -> str:
    if not telefono_str: return ""
    numeros = re.sub(r'\D', '', str(telefono_str))
    if numeros.startswith(codigo_pais + "9") and len(numeros) == (len(codigo_pais) + 1 + 10): return numeros
    if numeros.startswith(codigo_pais) and not numeros.startswith(codigo_pais + "9") and len(numeros) == (len(codigo_pais) + 10): return codigo_pais + "9" + numeros[len(codigo_pais):]
    if len(numeros) == 10: return f"{codigo_pais}9{numeros}"
    logger.info(f"[LOGIC-WSP] Número '{telefono_str}' no formateado a WhatsApp, usando: '{numeros}'")
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
                valor_para_reemplazo = str(valor_atributo).strip() if str(valor_atributo).strip() else defaults_textos.get(attr_key, "")
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
    session.setdefault(NOMBRE_HISTORIAL_SESION, [])
    logger.info(f"[LOGIC] Historial de sesión actual (antes de añadir pregunta): {len(session[NOMBRE_HISTORIAL_SESION])} mensajes.")

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
                logger.info(f"[LOGIC] Límite preguntas ({preg_usadas}/{lim_preg}) alcanzado para {user_obj.email}.")
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
    
    logger.info(f"[LOGIC] ==> User Obj Determinado: {type(user_obj).__name__}, Plan: {getattr(user_obj, 'plan', 'N/A')}")
    logger.info(f"[LOGIC]     user_obj.nombre_empresa: '{getattr(user_obj, 'nombre_empresa', 'N/A')}'")
    logger.info(f"[LOGIC]     user_obj.horario (raw string): '{getattr(user_obj, 'horario', 'N/A')}'")
    logger.info(f"[LOGIC]     user_obj.horario_json (prop/attr): {str(getattr(user_obj, 'horario_json', 'N/A'))[:250]}...")

    rubro_id_final: int = 1; rubro_nombre_final: str = "general"; rubro_obj_final: Optional[Rubro] = None
    if rubro_nombre_frontend and isinstance(rubro_nombre_frontend, str) and rubro_nombre_frontend.strip() and rubro_nombre_frontend != "general":
        logger.info(f"[LOGIC] Intentando rubro por parámetro frontend: '{rubro_nombre_frontend}'")
        rubro_obj_final = Rubro.query.filter(func.lower(Rubro.nombre) == rubro_nombre_frontend.lower().strip()).first()
        if rubro_obj_final: 
            logger.info(f"[LOGIC] Rubro por frontend: '{rubro_obj_final.nombre}' (ID: {rubro_obj_final.id})")
            if isinstance(user_obj, (GenericAnonUser, AnonUserPymeDemo)): 
                user_obj.nombre_empresa = getattr(rubro_obj_final, 'nombre_pyme_default_para_anon', f"Negocios de {rubro_obj_final.nombre}")
                user_obj.rubro_id = rubro_obj_final.id 
                logger.info(f"[LOGIC] Contexto de user_obj ({type(user_obj).__name__}) actualizado con rubro '{rubro_obj_final.nombre}'.")
        else: logger.warning(f"[LOGIC] Rubro '{rubro_nombre_frontend}' (frontend) no encontrado.")
    if not rubro_obj_final and hasattr(user_obj, 'rubro_id') and user_obj.rubro_id:
        logger.info(f"[LOGIC] Usando rubro_id del user_obj: {user_obj.rubro_id}")
        rubro_obj_final = db.session.get(Rubro, user_obj.rubro_id)
        if not rubro_obj_final: logger.warning(f"[LOGIC] Rubro ID {user_obj.rubro_id} (del user_obj) no encontrado.")
    if not rubro_obj_final: 
        rubro_obj_final = db.session.get(Rubro, 1); logger.info(f"[LOGIC] Usando rubro general (ID 1).")
    if rubro_obj_final: 
        rubro_id_final = rubro_obj_final.id; rubro_nombre_final = rubro_obj_final.nombre.lower().strip()
    else: 
        logger.error(f"[LOGIC] ¡ERROR CRÍTICO! Rubro general ID 1 no encontrado."); rubro_id_final = 1; rubro_nombre_final = "general"
    logger.info(f"[LOGIC] ==> Rubro Final: '{rubro_nombre_final}' (ID: {rubro_id_final})")

    horario_json_str_ctx = getattr(user_obj, 'horario', '[]') if isinstance(user_obj, User) else getattr(user_obj, 'horario_json', '[]')
    if not horario_json_str_ctx or not isinstance(horario_json_str_ctx, str) or not horario_json_str_ctx.strip(): horario_json_str_ctx = '[]'
    
    user_profile_context = {
        "nombre_empresa": getattr(user_obj, "nombre_empresa", "esta tienda"), "rubro_nombre": rubro_nombre_final,
        "telefono_raw": getattr(user_obj, "telefono", ""), "link_web": getattr(user_obj, "link_web", ""),
        "direccion_completa": f"{getattr(user_obj, 'direccion', '')}, {getattr(user_obj, 'ciudad', '')}, {getattr(user_obj, 'provincia', '')}".replace(" ,", "").strip(', ').strip(),
        "horario_str": getattr(user_obj, "horario", "Consultar nuestros horarios"), 
        "horario_json_str": horario_json_str_ctx, 
    }
    logger.info(f"[LOGIC] ==> User Profile Context para LLM: {json.dumps(user_profile_context, ensure_ascii=False, indent=2)}")

    horarios_para_prompt = user_profile_context['horario_str'] 
    try:
        if user_profile_context['horario_json_str'] and user_profile_context['horario_json_str'].strip().startswith('['):
            horarios_data = json.loads(user_profile_context['horario_json_str'])
            partes_horario = [f"{h.get('dia','')}: " + (f"de {h.get('abre','--:--')} a {h.get('cierra','--:--')}" if not h.get('cerrado') else "Cerrado") for h in horarios_data if isinstance(h,dict) and h.get('dia')]
            if partes_horario: horarios_para_prompt = ". ".join(partes_horario) + "."
        logger.info(f"[LOGIC] ==> Horarios formateados para prompt: {horarios_para_prompt}")
    except Exception as e_json_h_p: 
        logger.warning(f"[LOGIC] Error parseando horario_json_str para prompt: {e_json_h_p}. Se usará horario_str original.")
    
    numero_intercambios_previos = len(session[NOMBRE_HISTORIAL_SESION]) // 2
    
    # --- PROMPT DEL SISTEMA (igual que antes) ---
    prompt_sistema_texto = (
        f"Sos Chatboc, un asistente de ventas experto para {user_profile_context['nombre_empresa']} (rubro: {user_profile_context['rubro_nombre']}), ubicada en {user_profile_context['direccion_completa']}. "
        f"Tu principal objetivo es entender las necesidades del cliente y guiarlo hacia una compra o una acción concreta (como visitar la tienda online: {user_profile_context['link_web'] if user_profile_context['link_web'] else 'nuestro sitio web'}) en pocos intercambios (idealmente 2-4). "
        f"Ya has tenido {numero_intercambios_previos} interacciones previas con este cliente en esta sesión (revisa el historial de conversación que te proveo para mantener el contexto y evitar repetir información). "
        "Actúa como un vendedor humano profesional: sé amable, muy proactivo, resolutivo y persuasivo. Tus respuestas deben ser concisas, directas al grano y siempre aportando valor. Evita rellenos o introducciones innecesarias. "
        "Si necesitas clarificar, haz preguntas puntuales y claras. "
        "Si el cliente muestra interés en un producto o servicio, intenta facilitar el siguiente paso: ofrece añadir al carrito, llévalo a la página del producto en la tienda online, o describe cómo proceder. "
        "No te presentes como una IA o 'asistente virtual'. Eres un experto de {user_profile_context['nombre_empresa']}. "
        f"\nINFORMACIÓN CLAVE DE {user_profile_context['nombre_empresa']} (Usa esta información para responder preguntas relacionadas):"
        f"\n- Teléfono de Contacto: {user_profile_context['telefono_raw'] if user_profile_context['telefono_raw'] else 'No especificado. Sugiere contactar por otros medios o visitar la web.'}"
        f"\n- Dirección de la Tienda: {user_profile_context['direccion_completa'] if user_profile_context['direccion_completa'].strip(', ') else 'Consulta nuestra ubicación en la web o redes sociales.'}"
        f"\n- Horarios de Atención: {horarios_para_prompt if horarios_para_prompt and horarios_para_prompt.strip('.') and 'consultar' not in horarios_para_prompt.lower() else 'Por favor, consulta nuestros horarios actualizados en la web o redes sociales.'}"
        f"\n- Sitio Web / Tienda Online: {user_profile_context['link_web'] if user_profile_context['link_web'] else 'Puedes encontrar más información en nuestras redes sociales.'}"
        "\nINSTRUCCIONES DE COMPORTAMIENTO Y RESPUESTA:"
        "\n1. Saludo Inicial: Si es el primer mensaje del cliente y es un saludo, responde amablemente y pregunta en qué puedes ayudarlo sobre los productos o servicios de la empresa."
        "\n2. Preguntas Confusas/Spam: Si la pregunta del cliente es confusa, incomprensible, aleatoria (ej. 'asdfgh') o claramente no relacionada con el negocio, responde amablemente: 'Disculpa, no entendí bien tu consulta. Te puedo ayudar con información sobre nuestros productos, precios, horarios o cómo realizar una compra. ¿En qué te puedo asistir hoy?'"
        "\n3. Horarios: Si te preguntan '¿Están abiertos ahora?' o sobre horarios para un día específico, usa la información de 'Horarios de Atención' para dar la respuesta más precisa posible. Si es necesario, indica si está abierto o cerrado en el momento de la consulta (asume zona horaria Argentina, GMT-3). Si la información de horarios no es clara, dirige al cliente a la web o teléfono."
        "\n4. Información No Disponible: Si te piden información que NO tienes (no está en este prompt ni en el catálogo), indica amablemente: 'No dispongo de ese detalle específico en este momento, pero puedo ayudarte con otros temas. También puedes visitar nuestra tienda online en [linkWeb] o contactarnos al [telefono] para más información.'"
        "\nINSTRUCCIONES PARA USAR EL CATÁLOGO DE PRODUCTOS (si se provee información abajo):"
        "\n5. Presentación de Productos: Si el cliente pregunta por un tipo de producto (ej. 'vinos malbec') y el catálogo recuperado tiene opciones, presenta CLARAMENTE las más relevantes (máximo 2-3) con 'Nombre', 'Precio EXACTO del catálogo', 'Moneda' (ej. ARS o USD) y 'Presentación/Unidad' (ej. 'caja x6', '750ml'). Sé conciso. Ejemplo: 'Claro, tenemos: [Nombre Producto A] a [Moneda] [Precio A] (Presentación: [Unidad A]), y [Nombre Producto B] a [Moneda] [Precio B] (Presentación: [Unidad B]). ¿Te interesa alguno en particular?'"
        "\n6. Precio Específico: Si el cliente pregunta por el precio de un producto específico y lo encuentras, da el 'Precio EXACTO del catálogo' y la 'Moneda'."
        "\n7. Cálculo de Totales: Si el cliente pide varias unidades de un producto y el catálogo tiene un precio numérico claro, calcula el total y preséntalo. Ejemplo: 'Perfecto, 3 unidades de [Producto X] serían [Moneda] [Total Calculado].'"
        "\n8. Precio 'Consultar': Si el precio en el catálogo dice 'Consultar' o no está claro, indícalo y sugiere alternativas (ver online, contactar)."
        "\n9. Descripción del Catálogo: Si la descripción del catálogo es muy larga, resume los puntos clave para el cliente."
        "\n10. MUY IMPORTANTE (No Inventar): Si el cliente pregunta por un producto específico y NO encuentras información relevante en el catálogo que te proporcioné para esta consulta, NO INVENTES DETALLES COMO PRECIOS O CARACTERÍSTICAS ESPECÍFICAS. En su lugar, puedes decir algo como: 'No tengo el detalle o precio exacto de [producto consultado] en este momento, pero tenemos una amplia variedad de [categoría general del producto, ej: medias deportivas]. Puedes ver todas nuestras opciones en la tienda online en [linkWeb] o puedo ayudarte con otro tipo de producto.' O bien, 'Para ese producto específico, te recomiendo visitar nuestra tienda online ([linkWeb]) o contactarnos al [telefono] para darte la información más actualizada.'"
        "\n11. MONEDA: Si el catálogo recuperado indica una moneda (ej. USD o ARS junto al precio), USA ESA MONEDA en tu respuesta. Si no se especifica moneda en el catálogo y das un precio, asume que es en Pesos Argentinos (ARS) y menciónalo si es relevante (ej. 'El precio es $XXXX ARS'). NO inventes el tipo de moneda."
        "\n12. Cierre de Venta / Siguiente Paso: Siempre intenta facilitar el siguiente paso. Si el cliente muestra interés, pregunta si quiere añadir al carrito, visitar la página del producto, o cómo prefiere proceder."
        "\nREGLAS DE VENDEDOR EXPERTO Y SEGUIMIENTO:"
        "\n- No te presentes nunca como asistente virtual ni como inteligencia artificial. Sos un vendedor real y experto de la empresa."
        "\n- Si el cliente objeta el precio ('muy caro', 'vi más barato', 'caro', 'me parece caro'), destaca la calidad, el valor agregado, el servicio postventa o promociones. Ejemplo: 'Entiendo tu observación sobre el precio. Nuestros productos son de alta calidad y ofrecemos [menciona un beneficio, ej: envío rápido, garantía, atención personalizada]. ¿Te gustaría que exploremos alguna opción que se ajuste mejor o alguna promoción vigente?'"
        "\n- Siempre terminá tu respuesta con una pregunta de seguimiento o una propuesta concreta que invite a la acción: '¿Te gustaría avanzar con la compra?', '¿Querés que te contacte un asesor?', '¿Te puedo ayudar con algo más para completar tu pedido?'"
        "\n- Si el cliente dice frases tipo 'lo voy a pensar', 'más tarde', 'después veo', responde: '¡Entendido! Tómate tu tiempo. Si más adelante tienes alguna duda o necesitas una oferta especial, no dudes en consultarme por aquí o por WhatsApp. ¿Hay algo más en lo que te pueda asistir hoy?'"
        "\n- Nunca respondas 'depende' de forma vaga. Sé concreto o sugerí alternativas claras."
        "\n- Si el cliente pide algo similar a lo anterior ('¿tenés algo parecido?', 'otras opciones'), sugerí productos relacionados del catálogo o del mismo rubro si tienes esa información."
    )

    contexto_catalogo = ""; 
    if is_usuario_registrado_real and user_obj.id is not None:
        logger.info(f"[LOGIC] Usuario PYME {user_obj.id}. Intentando búsqueda en Qdrant para pregunta: '{pregunta[:50]}...'")
        try:
            from services.qdrant_search import buscar_catalogo_qdrant, armar_respuesta_legible 
            resultados_qdrant = buscar_catalogo_qdrant(user_obj.id, pregunta, limite=3, score_min=0.60)
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
        historial_para_api = [{"role": "USER" if msg.get("role") == "user" else "CHATBOT", "message": msg.get("content","")} for msg in session.get(NOMBRE_HISTORIAL_SESION, [])[-(6*2):]]
        
        logger.info(f"[LOGIC] Enviando a Cohere: Pregunta='{pregunta[:50]}...'. Historial API: {len(historial_para_api)}.")
        
        respuesta_obtenida_llm = get_cohere_response(
            message=pregunta,
            chat_history=historial_para_api,
            preamble=prompt_sistema_texto, 
            rubro_id=rubro_id_final, 
            user_context=user_profile_context
        )
        logger.info(f"[LOGIC] Respuesta CRUDA de Cohere: '{respuesta_obtenida_llm}'")
        mensaje_persuasion = detectar_objecion(pregunta)
        if mensaje_persuasion and respuesta_obtenida_llm:
            respuesta_obtenida_llm += f"\n\n{mensaje_persuasion}"

        mensajes_error_cohere_conocidos = [
            "Error interno: Asistente IA no disponible en este momento (C01).",
            "Por favor, escribe una pregunta o consulta más clara.",
            "Error interno: No se pudo inicializar el asistente IA (C02).",
            "Nuestro asistente IA está experimentando una alta demanda. Por favor, intenta nuevamente en unos momentos.",
            "Lo siento, no pude procesar tu solicitud en este momento con el asistente IA (E01).",
            "Lo siento, tuve un problema inesperado al intentar generar una respuesta (E02).",
            "Lo siento, hubo un problema técnico con nuestro asistente IA (TE01)."
        ]
        if respuesta_obtenida_llm and isinstance(respuesta_obtenida_llm, str) and \
           respuesta_obtenida_llm not in mensajes_error_cohere_conocidos and \
           len(respuesta_obtenida_llm.strip()) > 2:
            fuente_respuesta = "cohere"; 
            logger.info(f"[LOGIC] Cohere dio respuesta aparentemente válida.")
        else: 
            logger.warning(f"[LOGIC] Respuesta Cohere fue vacía, muy corta o un error conocido: '{respuesta_obtenida_llm}'.")
            respuesta_obtenida_llm = "" 
    except Exception as e_cohere_call: 
        logger.error(f"[LOGIC] Error crítico al llamar o procesar respuesta de Cohere: {e_cohere_call}", exc_info=True)
        respuesta_obtenida_llm = ""

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

    respuesta_para_frontend = respuesta_final_procesada

    # --- LÓGICA DE BOTONES "INTELIGENTE" MEJORADA VISUAL ---
    pyme_link_web = getattr(user_obj, "link_web", "")
    pyme_telefono = getattr(user_obj, "telefono", "")

    # Añadir botón de tienda online
    anadir_boton_tienda = False
    if pyme_link_web and fuente_respuesta not in ["faq", "sugerencia_sistema"]:
        keywords_venta_producto = ["comprar", "pedido", "tienda online", "ver más", "catálogo", "productos", "opciones", "precio de", "modelo", "tipo", "variedad", "cajas", "ofertas"]
        if (fuente_respuesta == "cohere" and any(kw in respuesta_obtenida_llm.lower() for kw in keywords_venta_producto)) or \
           any(kw in pregunta.lower() for kw in keywords_venta_producto):
            anadir_boton_tienda = True

    if anadir_boton_tienda:
        link_absoluto_tienda = pyme_link_web
        if not link_absoluto_tienda.startswith("http"): link_absoluto_tienda = "https://" + link_absoluto_tienda
        boton_tienda_html = (
            f'''
<div style="margin-top: 22px; padding-top: 12px; border-top: 1px solid #eaeaea; text-align: center;">
  <a href="{link_absoluto_tienda}" target="_blank"
     style="
        display: inline-block;
        background: linear-gradient(90deg, #2980f3 60%, #7dd3fc 100%);
        color: #fff;
        padding: 14px 32px;
        text-align: center;
        text-decoration: none;
        border-radius: 12px;
        font-size: 1.07em;
        font-family: 'Inter', 'Segoe UI', Arial, sans-serif;
        font-weight: 700;
        box-shadow: 0 4px 18px 0 rgba(0,50,255,0.08), 0 1.5px 7px 0 rgba(0,0,0,0.04);
        transition: background 0.2s, box-shadow 0.2s;
        margin: 0 auto;
        min-width: 200px;
     "
     onmouseover="this.style.background='linear-gradient(90deg,#005acb 70%,#3ab3fc 100%)'; this.style.boxShadow='0 6px 22px 0 rgba(10,60,200,0.16)';"
     onmouseout="this.style.background='linear-gradient(90deg,#2980f3 60%,#7dd3fc 100%)'; this.style.boxShadow='0 4px 18px 0 rgba(0,50,255,0.08), 0 1.5px 7px 0 rgba(0,0,0,0.04)';"
     >
    <span style="display:inline-block;vertical-align:middle;margin-right:7px;font-size:1.15em;">🛒</span>
    Visitar Tienda Online
  </a>
</div>
''')
        respuesta_para_frontend += boton_tienda_html
        logger.info("[LOGIC] Botón de Tienda Online añadido.")

    # Añadir botón de WhatsApp
    anadir_boton_whatsapp = False
    if pyme_telefono and fuente_respuesta != "sugerencia_sistema":
        keywords_contacto_pedido = ["contactar", "teléfono", "whatsapp", "pedido", "comprar", "reservar", "hablar con alguien", "asesor", "consulta", "ayuda"]
        if any(kw in pregunta.lower() for kw in keywords_contacto_pedido) or \
           (fuente_respuesta == "cohere" and any(kw in respuesta_obtenida_llm.lower() for kw in keywords_contacto_pedido)):
            anadir_boton_whatsapp = True

    if anadir_boton_whatsapp:
        numero_wsp = formatear_numero_whatsapp_simple(pyme_telefono)
        if numero_wsp:
            mensaje_whatsapp_predefinido = f"Hola {user_profile_context['nombre_empresa']}, tengo una consulta sobre: \"{pregunta}\". La respuesta que me dieron fue: \"{respuesta_final_procesada[:70]}...\" ¿Podrían ayudarme?"
            if "comprar" in pregunta.lower() or "pedido" in pregunta.lower():
                 mensaje_whatsapp_predefinido = f"Hola {user_profile_context['nombre_empresa']}, quisiera hacer un pedido o consulta sobre: \"{pregunta}\". El bot me dijo: \"{respuesta_final_procesada[:70]}...\""
            
            mensaje_whatsapp_encoded = urllib.parse.quote(mensaje_whatsapp_predefinido)
            boton_wsp_html = (
                f'''
<div style="margin-top: 14px; text-align: center;">
  <a href="https://wa.me/{numero_wsp}?text={mensaje_whatsapp_encoded}" target="_blank"
     style="
        display: inline-block;
        background: linear-gradient(90deg, #25d366 85%, #059669 100%);
        color: #fff;
        padding: 14px 32px;
        text-align: center;
        text-decoration: none;
        border-radius: 12px;
        font-size: 1.07em;
        font-family: 'Inter', 'Segoe UI', Arial, sans-serif;
        font-weight: 700;
        box-shadow: 0 4px 18px 0 rgba(27,205,96,0.09), 0 1.5px 7px 0 rgba(0,0,0,0.05);
        transition: background 0.2s, box-shadow 0.2s;
        margin: 0 auto;
        min-width: 200px;
     "
     onmouseover="this.style.background='linear-gradient(90deg,#15ad42 90%,#0dd9a7 100%)'; this.style.boxShadow='0 6px 22px 0 rgba(27,205,96,0.15)';"
     onmouseout="this.style.background='linear-gradient(90deg,#25d366 85%,#059669 100%)'; this.style.boxShadow='0 4px 18px 0 rgba(27,205,96,0.09), 0 1.5px 7px 0 rgba(0,0,0,0.05)';"
     >
    <span style="display:inline-block;vertical-align:middle;margin-right:7px;font-size:1.22em;">💬</span>
    Contactar por WhatsApp
  </a>
</div>
''')
            respuesta_para_frontend += boton_wsp_html
            logger.info(f"[LOGIC] Botón de WhatsApp añadido. Mensaje predefinido (parcial): {mensaje_whatsapp_predefinido[:70]}...")

    if respuesta_final_procesada and respuesta_final_procesada.strip(): 
        session[NOMBRE_HISTORIAL_SESION].extend([{"role": "user", "content": pregunta}, {"role": "assistant", "content": respuesta_final_procesada}])
        session.modified = True; logger.info(f"[LOGIC] Historial actualizado. Tamaño: {len(session[NOMBRE_HISTORIAL_SESION])}")
        if is_usuario_registrado_real and user_obj.id:
            try:
                db_user_to_update = db.session.get(User, user_obj.id)
                if db_user_to_update:
                    db_user_to_update.preguntas_usadas = (db_user_to_update.preguntas_usadas or 0) + 1
                    db.session.add(Conversacion(user_id=user_obj.id, pregunta=pregunta, respuesta=respuesta_final_procesada, fuente=fuente_respuesta, rubro=rubro_nombre_final))
                    db.session.commit()
                    logger.info(f"[LOGIC] Conversación y contador de preguntas guardado para user {user_obj.id}.")
            except Exception as e_db: logger.error(f"[LOGIC] Error guardando conversación: {e_db}", exc_info=True); db.session.rollback()
        
        logger.info(f"✅ [LOGIC] Respuesta final (fuente: {fuente_respuesta}): '{respuesta_para_frontend[:100]}...'")
        return {"respuesta": respuesta_para_frontend, "nivel_usado": rubro_nombre_final, "fuente": fuente_respuesta}
    else: 
        logger.info("[LOGIC] Fallback final definitivo: Todos los sistemas no dieron respuesta útil. Usando sugerencias.")
        sugs = sugerencias_por_rubro(rubro_id_final)
        if sugs: resp_sug_base = "No encontré una respuesta directa. Quizás puedas intentar: " + " · ".join(f"“{s}”" for s in sugs if s)
        else: resp_sug_base = "Lo siento, no entendí bien. ¿Podrías reformularlo?"
        resp_final_sug = reemplazar_placeholders(resp_sug_base, user_obj)
        
        pyme_link_web_fallback = getattr(user_obj, "link_web", "")
        if pyme_link_web_fallback:
            link_abs_sug = pyme_link_web_fallback
            if not link_abs_sug.startswith("http"): link_abs_sug = "https://" + link_abs_sug
            resp_final_sug += (
                f'''
<div style="margin-top: 10px; font-size: 0.97em; text-align: center;">
  También puedes <a href="{link_abs_sug}" target="_blank" style="color: #2980f3; text-decoration: underline; font-weight: 600;">visitar nuestra tienda online</a>.
</div>
''')
        session[NOMBRE_HISTORIAL_SESION].append({"role": "user", "content": pregunta}); session.modified = True
        logger.info(f"[LOGIC] Enviando fallback con sugerencias: '{resp_final_sug[:100]}...'")
        return {"respuesta": resp_final_sug, "fuente": "sugerencia_sistema"}
# --- FIN DE responder_chatboc ---
