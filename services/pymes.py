import logging
import re
import random
import json
import difflib
from datetime import datetime
from enum import Enum, auto
from flask import session as flask_session

# --- Importaciones ---
from models import Conversacion, PymeTicket, TicketComentario, PymePedido, Rubro, SitioWebInfo, db
from services.utils_placeholders import reemplazar_placeholders
from services.utils import sugerencias_por_rubro, calcular_monto_total_items
from utils.plan_limits import limite_para_usuario
from services.cohere_ai import get_cohere_response
from services.vector_search import buscar_item_vectorizado
from services.qdrant_search import (
    buscar_catalogo_qdrant,
    armar_respuesta_legible,
    DEFAULT_SEARCH_LIMIT,
)
from services.faq_matcher_spacy import buscar_en_faq_spacy
from services.intent_matcher import buscar_en_intents
from services.ticket_service import servicio_tickets
from services.email_service import (
    enviar_email_ticket_admin,
    enviar_email_ticket_cliente,
    enviar_email_pedido_cliente,
    enviar_sms,
    enviar_whatsapp,
)
from services.webinfo import obtener_info_web, guardar_info_web
from services.scraper_avanzado import extraer_productos_de_url
from services.pedido_service import servicio_pedidos
from services.herramientas_pyme import TOOL_REGISTRY_PYME
from services.herramientas_municipio import normalizar_texto # Usamos esta función de normalización
from .logic import (
    detectar_small_talk_con_llm,
    generar_respuesta_small_talk,
)

logger = logging.getLogger(__name__)

# --- Constantes ---
NOMBRE_HISTORIAL_SESION = "historial_chat_cliente_pyme"
CONTEXTO_PYME_SESION = "contexto_pyme"
MAX_HISTORIAL_CHAT = 30
LAST_OWNER_SESION = "pyme_last_owner_id"


class PymeConversationState(Enum):
    ESPERANDO_DETALLES_PEDIDO = auto()
    CONFIRMANDO_PEDIDO_TEMP = auto()
    CONFIRMANDO_PEDIDO_FINAL_PASO_2 = auto()
    ESPERANDO_NUMERO_PEDIDO = auto()
    ESPERANDO_NUMERO_TICKET = auto()
    ESPERANDO_DETALLES_RECLAMO = auto()
    ESPERANDO_DATOS_RECLAMO_ROTO = auto()
    ESPERANDO_CIUDAD_ENVIO = auto()
    ESPERANDO_NOMBRE_PRODUCTO_STOCK = auto()
    ESPERANDO_ADJUNTOS_RECLAMO_PYME = auto() 
    ESPERANDO_CONFIRMACION_RECLAMO_PYME = auto()


def serialize_state(state: PymeConversationState | None) -> str | None:
    return state.name if state else None


def deserialize_state(value: str | None) -> PymeConversationState | None:
    if not value:
        return None
    try:
        return PymeConversationState[value]
    except KeyError:
        return None

# --- Funciones Auxiliares (que no dependen de Handlers específicos) ---
def es_pregunta_nueva(texto_usuario: str, tipo_esperado: str) -> bool:
    """Determina si el usuario cambió de tema cuando se esperaba un dato."""
    texto = texto_usuario.strip().lower()
    if tipo_esperado == "el dato solicitado" and re.search(r"\d", texto):
        return False
    prompt = (
        f"Analiza la RESPUESTA DEL USUARIO. El chatbot esperaba algo relacionado a: '{tipo_esperado}'.\n"
        f"RESPUESTA DEL USUARIO: '{texto_usuario}'\n"
        "Si responde lo que esperabas, contestá 'RESPUESTA_VALIDA'."
        " Si cambia de tema, contestá 'PREGUNTA_NUEVA'."
    )
    try:
        decision = get_cohere_response(message=prompt, preamble="Sos un clasificador. Solo respondé 'RESPUESTA_VALIDA' o 'PREGUNTA_NUEVA'.")
        return "PREGUNTA_NUEVA" in decision
    except Exception:
        return False


PROMPT_PYME_CON_CONTEXTO = """
Eres "Chatboc", un agente de ventas y atención al cliente experto para la empresa "{nombre_pyme}".
Tu tarea principal es responder la PREGUNTA DEL USUARIO de forma clara y útil, basándote ESTRICTAMENTE en la INFORMACIÓN DE CONTEXTO extraída de la página web oficial de la empresa.
No inventes detalles, precios o políticas que no estén explícitamente mencionadas en el contexto. Si la información no está disponible, indícalo amablemente y ofrece ayuda para contactar a un representante.

--- INFORMACIÓN DE CONTEXTO (Extraída de la web de {nombre_pyme}) ---
{contexto_scraped}
--------------------------------------------------------------------

PREGUNTA DEL USUARIO: "{pregunta_usuario}"

Respuesta:
"""

PROMPT_CLASIFICACION_INTENCION_PYME = """
Analiza la siguiente PREGUNTA DEL USUARIO y clasifica su INTENCIÓN en el contexto de una PYME.
Si la pregunta no encaja en ninguna de las categorías, clasifícala como 'general_pyme'.

INTENCIONES POSIBLES:
- iniciar_pedido: El usuario quiere hacer un pedido, solicitar un producto, cotización, o información para compra. (ej. "quiero pedir 5 cajas de vino", "cotización de este producto", "cómo compro", "quiero encargar")
- consultar_estado_pedido: El usuario quiere saber el estado de un pedido existente. (ej. "estado de mi reclamo", "cómo va mi pedido 12345")
- consultar_stock: El usuario pregunta sobre la disponibilidad de un producto o stock.
- consultar_horario: El usuario pregunta sobre horarios de atención.
- consultar_ubicacion: El usuario pregunta por la dirección física.
- hablar_con_agente_pyme: El usuario quiere hablar con una persona de la empresa (ej. "necesito hablar con alguien", "quiero hablar con una persona", "pasame con un humano", "me pasas con un operador", "quiero un representante")
- iniciar_reclamo_pyme: El usuario quiere hacer un reclamo o queja.
- general_pyme: Cualquier otra consulta que no encaje en las anteriores.

PREGUNTA DEL USUARIO: "{pregunta_usuario}"

Tu respuesta debe ser SÓLO una de las INTENCIONES POSIBLES.
"""

def _clasificar_intencion_pyme_con_llm(pregunta: str) -> str:
    """Clasifica la intención de una consulta en el contexto de una PyME."""
    logger.info(f"[PYME_CLF] Clasificando intención para: '{pregunta}'")
    prompt = PROMPT_CLASIFICACION_INTENCION_PYME.format(pregunta_usuario=pregunta)
    try:
        intencion = get_cohere_response(
            message=prompt,
            preamble="Sos un clasificador de intención de usuario para una pyme. Responde solo con la intención clasificada.",
        )
        return intencion.strip().lower()
    except Exception as e:
        logger.error(f"[PYME_CLF] Error al clasificar intención: {e}")
        return "general_pyme"


def crear_prompt_decision_herramienta_pyme(pregunta_usuario: str) -> str:
    descripcion_herramientas = {}
    for nombre, det in TOOL_REGISTRY_PYME.items():
        descripcion_herramientas[nombre] = {
            "descripcion": det.get("descripcion", ""),
            "parametros": det.get("parametros", {})
        }
    prompt = f"""
Sos un despachador de herramientas para PYMES. Analizá la PREGUNTA DEL USUARIO y decidí si alguna herramienta puede resolverla.
HERRAMIENTAS DISPONIBLES:
{json.dumps(descripcion_herramientas, indent=2, ensure_ascii=False)}
PREGUNTA: "{pregunta_usuario}"
- Si coincide y hay parámetros, devolvé JSON: {{"herramienta": "nombre", "parametros": {{"nombre_param": "valor"}}}}
- Si faltan parámetros, devolvé JSON: {{"herramienta": "nombre", "faltan_parametros": ["nombre_param"]}}
- Si ninguna aplica, devolvé 'null'.
"""
    return prompt


def _generar_asunto_con_llm(pregunta: str) -> str:
    try:
        prompt = f"Resume la siguiente consulta de un cliente en un título breve de 4 a 8 palabras para un ticket de soporte. La consulta es: '{pregunta}'"
        asunto = get_cohere_response(message=prompt, chat_history=[], preamble="Eres un experto en resumir consultas de clientes.")
        return asunto.strip().replace('"', '')
    except Exception as e:
        logger.error(f"[PYME] Error generando asunto con LLM: {e}")
        return (pregunta[:75] + '...') if len(pregunta) > 75 else pregunta

def _extraer_cantidades_con_llm(pregunta_cliente: str, productos_disponibles_raw: list) -> list:
    nombres_y_sku = []
    for p in productos_disponibles_raw:
        nombre_completo = p.get('nombre', '')
        sku = p.get('sku', '')
        display_name = nombre_completo
        
        if sku and sku != nombre_completo and sku != "N/A":
            display_name = f"{nombre_completo} (SKU: {sku})"
        elif p.get('descripcion'):
            desc_para_display = p['descripcion'][:30].replace('\n', ' ').strip()
            display_name = f"{nombre_completo} ({desc_para_display}...)" 
        nombres_y_sku.append(display_name)

    prompt = f"""
    Tu tarea es analizar la respuesta de un cliente y extraer los productos y cantidades que solicita, basándote en la lista de PRODUCTOS_DISPONIBLES.
    Si el cliente menciona "cada variedad" o "todos los que me mostraste", debes incluir TODOS los productos de la lista de PRODUCTOS_DISPONIBLES con la cantidad especificada.
    Tu respuesta DEBE SER ÚNICAMENTE un objeto JSON en formato de lista. Cada objeto debe tener "producto_identificador", "cantidad" y "unidad".
    "producto_identificador" debe ser el nombre exacto o el nombre con SKU que se te proporcionó en la lista PRODUCTOS_DISPONIBLES para una identificación precisa.
    Si no se especifica unidad (como 'caja', 'botella'), usa 'unidad'. Si no se puede determinar la cantidad, asume '1'.
    Si el producto no está en la lista de PRODUCTOS_DISPONIBLES, NO lo incluyas.

    PRODUCTOS_DISPONIBLES: {json.dumps(nombres_y_sku, ensure_ascii=False)}

    RESPUESTA DEL CLIENTE: "{pregunta_cliente}"

    JSON de Salida:
    """
    try:
        respuesta_llm = get_cohere_response(message=prompt, chat_history=[], preamble="Eres un asistente experto en procesar pedidos en formato JSON.")
        json_limpio = respuesta_llm.strip().replace("```json", "").replace("```", "")
        parsed_json = json.loads(json_limpio)
        
        productos_parseados = []
        pedio_todos = "cada variedad" in pregunta_cliente.lower() or "todos los que me mostraste" in pregunta_cliente.lower()

        if pedio_todos and productos_disponibles_raw:
            cantidad_general = 1
            match_cantidad_general = re.search(r'(\d+)\s*(?:caj(?:a|as)|botell(?:a|as)|unidad(?:es)?)', pregunta_cliente, re.IGNORECASE)
            if match_cantidad_general:
                cantidad_general = int(match_cantidad_general.group(1))

            for p_raw in productos_disponibles_raw:
                productos_parseados.append({
                    "nombre": p_raw.get('nombre'),
                    "sku": p_raw.get('sku', 'N/A'),
                    "precio": p_raw.get('precio', 0.0),
                    "precio_str": p_raw.get('precio_str', 'Consultar'),
                    "cantidad": cantidad_general, 
                    "unidad": p_raw.get('unidad', 'unidad'),
                    "detalles_originales": p_raw 
                })
        else: 
            for item_llm in parsed_json:
                identificador = item_llm.get('producto_identificador', '').strip()
                cantidad = item_llm.get('cantidad', 1)
                unidad = item_llm.get('unidad', 'unidad')

                matched_product = None
                for p_raw in productos_disponibles_raw:
                    nombre_completo = p_raw.get('nombre', '')
                    sku = p_raw.get('sku', '')
                    if identificador == nombre_completo or identificador == sku:
                        matched_product = p_raw
                        break
                    # Coincidencia difusa para nombres si no hay coincidencia exacta de identificador
                    if identificador.lower() in nombre_completo.lower():
                        matched_product = p_raw
                        break

                if matched_product:
                    productos_parseados.append({
                        "nombre": matched_product.get('nombre'),
                        "sku": matched_product.get('sku', 'N/A'),
                        "precio": matched_product.get('precio', 0.0), 
                        "precio_str": matched_product.get('precio_str', 'Consultar'),
                        "cantidad": cantidad,
                        "unidad": unidad,
                        "detalles_originales": matched_product 
                    })
        return productos_parseados
    except Exception as e:
        logger.error(f"[PYMES] Error al extraer cantidades con LLM: {e}", exc_info=True)
        return []


def ordenar_productos_para_venta(productos: list[dict]) -> list[dict]:
    """Ordena productos priorizando destacados, relevancia y mejor precio."""
    def _key(p):
        destacado = 0 if p.get("destacado") else 1
        relevancia = -float(p.get("relevancia", 0))
        precio = float(p.get("precio_float") or p.get("precio") or 0)
        return (destacado, relevancia, precio)

    return sorted(productos, key=_key)

def armar_respuesta_catalogo_agrupado(productos: list, max_por_categoria=5) -> str:
    """
    Organiza y presenta productos agrupados por categoría, mostrando los más relevantes primero.
    """
    if not productos:
        return "No se encontraron productos en el catálogo."

    # Agrupar por categoría
    categorias = {}
    for p in productos:
        cat = p.get("categoria_qdrant") or p.get("categoria") or "Otros"
        categorias.setdefault(cat, []).append(p)

    respuesta = ""
    for categoria, items in categorias.items():
        respuesta += f"\n🗂️ **{categoria.title()}**\n"
        # Ordenar por precio si existe, sino por nombre
        items_ordenados = sorted(
            items, key=lambda x: float(x.get("precio_float") or x.get("precio") or 0)
        )
        for p in items_ordenados[:max_por_categoria]:
            nombre = p.get("nombre", "Producto")
            precio = p.get("precio_str") or f"${p.get('precio', 'Consultar')}"
            unidad = p.get("unidad", "unidad")
            desc = p.get("descripcion", "")
            respuesta += f"- **{nombre}** ({unidad}) — {precio}\n"
            if desc:
                respuesta += f"    _{desc[:60]}_\n"
        if len(items_ordenados) > max_por_categoria:
            respuesta += f"    ...y {len(items_ordenados) - max_por_categoria} más en esta categoría.\n"
    return respuesta.strip()

def armar_tabla_comparativa(productos: list) -> str:
    if not productos:
        return ""
    header = "| Producto | Precio | Unidad | Descripción |\n|---|---|---|---|\n"
    filas = []
    for p in productos[:10]:
        nombre = p.get("nombre", "")
        precio = p.get("precio_str") or f"${p.get('precio', 'Consultar')}"
        unidad = p.get("unidad", "unidad")
        desc = (p.get("descripcion", "") or "")[:40]
        filas.append(f"| {nombre} | {precio} | {unidad} | {desc} |")
    return header + "\n".join(filas)

# --- ARQUITECTURA DE HANDLERS (TODAS LAS CLASES DEFINIDAS AQUÍ PRIMERO) ---

class BaseHandler:
    def __init__(self, context):
        self.context = context
    # MODIFICADO: Ahora acepta un 'payload' completo como diccionario
    def handle(self, payload: dict) -> dict | None:
        raise NotImplementedError

    # -- utilidades de coincidencia robusta --
    def _normalize(self, text: str) -> str:
        if not isinstance(text, str):
            return ""
        return normalizar_texto(text)

    def _has_keyword(self, text: str, keywords: list[str]) -> bool:
        texto_norm = self._normalize(text)
        return any(kw in texto_norm for kw in keywords)

    def build_detalles_memoria(self, memoria: dict) -> str:
        """Arma un pequeño resumen de los datos del reclamo/pedido."""
        partes = []
        if memoria.get("categoria_reclamo"):
            partes.append(f"Categoría: {memoria['categoria_reclamo']}")
        if memoria.get("descripcion_reclamo"):
            partes.append(f"Descripción: {memoria['descripcion_reclamo']}")
        if memoria.get("foto_url"):
            partes.append("Foto adjunta: Sí")
        if memoria.get("ubicacion_gps"):
            lat = memoria['ubicacion_gps'].get('lat', 'N/A')
            lon = memoria['ubicacion_gps'].get('lon', 'N/A')
            partes.append(f"Ubicación GPS: Lat {lat}, Lon {lon}")
        
        # Para pedidos, si hay items temporales
        if memoria.get("items_temporales"):
            items_str = "\n".join([f"- {item['cantidad']} {item['unidad']} de {item['nombre']} ({item['precio_str']})" for item in memoria['items_temporales']])
            partes.append(f"Ítems del pedido:\n{items_str}")
            total = calcular_monto_total_items(memoria['items_temporales'])
            partes.append(f"Total estimado: ${total:.2f}")

        return "\n".join(partes)


class GreetingHandler(BaseHandler):
    # MODIFICADO: acepta payload
    def handle(self, payload: dict) -> dict | None:
        pregunta_str = payload.get("pregunta", "") # Extrae el string de la pregunta
        memoria = self.context.get("contexto_pyme", {}) # Accede al contexto pyme
        texto = self._normalize(pregunta_str.strip("!.,?")) # Usa _normalize del BaseHandler
        saludos = [
            "hola", "buenos dias", "buenas tardes", "buenas noches", "hey",
            "que tal", "buenas", "buen dia", "hola hola", "hola que tal", "saludos",
        ]
        tokens = texto.split()
        set_saludo = {
            "hola", "buenos", "dias", "buenas", "tardes", "noches", "hey",
            "que", "tal", "saludos", "dia",
        }

        def token_es_saludo(tok: str) -> bool:
            if tok in set_saludo:
                return True
            return any(difflib.SequenceMatcher(None, tok, s).ratio() >= 0.7 for s in set_saludo)

        es_saludo = texto in saludos or (
            0 < len(tokens) <= 3 and all(token_es_saludo(t) for t in tokens)
        )

        if not es_saludo:
            for saludo in saludos:
                if difflib.SequenceMatcher(None, texto, saludo).ratio() >= 0.85:
                    es_saludo = True
                    break

        if es_saludo:
            nombre_pyme = self.context.get('nombre_pyme')
            if nombre_pyme:
                return {
                    "respuesta": f"¡Hola! Soy Chatboc, tu asistente para {nombre_pyme}. ¿En qué puedo ayudarte hoy? ¿Buscás algún producto o necesitás asesoramiento?",
                    "fuente": "saludo_pyme",
                    "botones": [
                        {"texto": "Ver catálogo", "action": "ver_catalogo"},
                        {"texto": "Hablar con un agente", "action": "escalar"},
                    ]
                }
            return {
                "respuesta": "¡Hola! Soy Chatboc. ¿En qué puedo ayudarte hoy?",
                "fuente": "saludo_pyme",
                "botones": [
                    {"texto": "Ver catálogo", "action": "ver_catalogo"},
                    {"texto": "Hablar con un agente", "action": "escalar"},
                ]
            }
        return None


class CancelHandler(BaseHandler):
    """Permite cancelar el flujo actual si el usuario lo solicita."""

    CANCEL_KEYWORDS = [
        "cancelar", "olvidalo", "deja", "no importa", "volver", "salir",
        "cancelar pedido", "no quiero continuar", "parar", "detener",
        "finalizar", "terminar" # Añadidos para capturar intentos de finalizar el reclamo/pedido
    ]

    # MODIFICADO: acepta payload
    def handle(self, payload: dict) -> dict | None:
        pregunta_str = payload.get("pregunta", "") # Extrae el string de la pregunta
        texto = self._normalize(pregunta_str)
        # Si la acción es "cancelar" (desde un botón) o la frase contiene keywords de cancelación
        if payload.get("action") == "cancelar" or any(kw in texto for kw in self.CANCEL_KEYWORDS): 
            self.context.get("contexto_pyme", {}).clear() # Limpia completamente el contexto del flujo
            return {
                "respuesta": "Operación cancelada. ¿Necesitás ayuda con otra cosa?",
                "fuente": "cancelar_pyme",
                "botones": [
                    {"texto": "Ver catálogo", "action": "ver_catalogo"},
                    {"texto": "Hacer un pedido", "action": "iniciar_pedido"},
                    {"texto": "Hablar con un agente", "action": "escalar"},
                ],
            }
        return None


class PoliteHandler(BaseHandler):
    """Responde brevemente ante agradecimientos u otras expresiones corteses."""

    KEYWORDS = {"gracias", "ok", "ok gracias", "muchas gracias", "dale", "perfecto", "genial"}

    # MODIFICADO: acepta payload
    def handle(self, payload: dict) -> dict | None:
        pregunta_str = payload.get("pregunta", "") # Extrae el string de la pregunta
        texto = self._normalize(pregunta_str)
        if texto in self.KEYWORDS:
            # No necesariamente limpiar toda la memoria, si está en medio de un flujo.
            # Solo si es un agradecimiento simple fuera de un flujo directo.
            if not self.context.get("contexto_pyme", {}).get("estado_conversacion"):
                self.context.get("contexto_pyme", {}).clear()
            return {
                "respuesta": "¡De nada! ¿Necesitás ayuda con algo más?",
                "fuente": "cortesia_pyme",
                "botones": [
                    {"texto": "Ver catálogo", "action": "ver_catalogo"},
                    {"texto": "Hacer un pedido", "action": "iniciar_pedido"},
                    {"texto": "Hablar con un agente", "action": "escalar"},
                ],
            }
        return None


class SmallTalkHandler(BaseHandler):
    """Detecta small talk con LLM y responde de forma cordial."""
    # MODIFICADO: acepta payload
    def handle(self, payload: dict) -> dict | None:
        pregunta_str = payload.get("pregunta", "") # Extrae el string de la pregunta
        # Solo responde small talk si no hay una acción o adjunto explícito
        if payload.get("action") or payload.get("es_foto") or payload.get("es_ubicacion"):
            return None # No interceptar si hay una acción o adjunto real

        if detectar_small_talk_con_llm(pregunta_str):
            respuesta = generar_respuesta_small_talk(pregunta_str)
            return {
                "respuesta": respuesta,
                "fuente": "smalltalk_pyme_llm",
            }
        return None


class SentimentHandler(BaseHandler):
    NEGATIVE_KEYWORDS = [
        "pesimo", "pésimo", "horrible", "desastre", "engaño", "estafa", "odio", "malisimo", "malo",
    ]
    POSITIVE_KEYWORDS = [
        "excelente", "buen servicio", "muy bueno", "genial", "gracias", "felicitaciones",
    ]
    # MODIFICADO: acepta payload
    def handle(self, payload: dict) -> dict | None:
        pregunta_str = payload.get("pregunta", "") # Extrae el string de la pregunta
        texto = self._normalize(pregunta_str)
        
        # Solo analiza sentimiento si no es una acción de botón o un adjunto
        if payload.get("action") or payload.get("es_foto") or payload.get("es_ubicacion"):
            return None

        if any(kw in texto for kw in self.POSITIVE_KEYWORDS):
            sentimiento = "positivo"
        elif any(kw in texto for kw in self.NEGATIVE_KEYWORDS):
            sentimiento = "negativo"
        else:
            sentimiento = analizar_sentimiento_con_llm(pregunta_str)

        if sentimiento == "negativo":
            self.context["intencion"] = "hablar_con_agente_pyme"
            return {
                "respuesta": "Lamento la mala experiencia. ¿Querés hablar con un agente o preferís que te recomiende productos con mejor valoración?",
                "fuente": "sentimiento_negativo",
                "botones": [
                    {"texto": "Hablar con un agente", "action": "escalar"},
                    {"texto": "Ver productos recomendados", "action": "recomendar"},
                ],
            }
        if sentimiento == "positivo":
            return {
                "respuesta": "¡Gracias por tu comentario! ¿Te gustaría aprovechar una oferta especial o recibir recomendaciones personalizadas?",
                "fuente": "sentimiento_positivo",
                "botones": [
                    {"texto": "Ver ofertas", "action": "ofertas"},
                    {"texto": "Recomiéndame productos", "action": "recomendar"},
                ]
            }
        return None

class LimitHandler(BaseHandler):
    # MODIFICADO: acepta payload
    def handle(self, payload: dict) -> dict | None:
        # pregunta_str = payload.get("pregunta", "") # No se usa directamente aquí, solo se chequea el límite
        limite = self.context.get('limite_preguntas')
        if limite is not None and self.context.get('preguntas_usadas', 0) >= limite:
            return {
                "respuesta": "🔒 Límite de preguntas alcanzado. Actualizá tu plan para continuar.",
                "fuente": "sistema_limite",
                "estado_respuesta": "limite_alcanzado",
                "botones": [
                    # Aquí podrías añadir botones para ir a la página de planes, por ejemplo
                ]
            }
        return None


class FaqHandler(BaseHandler):
    # MODIFICADO: acepta payload
    def handle(self, payload: dict) -> dict | None:
        pregunta_str = payload.get("pregunta", "")
        respuesta = buscar_en_faq_spacy(pregunta_str, self.context.get('user_id'))
        if respuesta:
            return {
                "respuesta": respuesta,
                "fuente": "faq_pyme",
                "botones": [
                    {"texto": "Ver catálogo", "action": "ver_catalogo"},
                    {"texto": "Hablar con un agente", "action": "escalar"},
                ]
            }
        return None

class ToolHandlerPyme(BaseHandler):
    # MODIFICADO: acepta payload
    def handle(self, payload: dict) -> dict | None:
        pregunta_str = payload.get("pregunta", "")
        # Aquí podrías usar crear_prompt_decision_herramienta_pyme para que el LLM decida la herramienta
        # Por ahora, un simple chequeo de keyword
        if self.context.get('contexto_pyme',{}).get('estado_conversacion') == PymeConversationState.ESPERANDO_NOMBRE_PRODUCTO_STOCK:
            # Flujo de stock: el usuario dio el nombre del producto
            from services.herramientas_pyme import verificar_stock_producto
            resultado = verificar_stock_producto(nombre=pregunta_str, user_id=self.context.get('user_id'))
            self.context['contexto_pyme'].pop('estado_conversacion', None) # Limpiar estado
            return json.loads(resultado) # La herramienta ya devuelve un dict para el frontend

        if "stock" in pregunta_str.lower() or payload.get("action") == "consultar_stock":
            # Esto inicia el sub-flujo que pide el nombre del producto
            self.context['contexto_pyme']['estado_conversacion'] = serialize_state(PymeConversationState.ESPERANDO_NOMBRE_PRODUCTO_STOCK)
            return {
                "respuesta": "Puedo ayudarte a verificar el stock. ¿Qué producto te interesa?",
                "fuente": "tool_pyme_stock",
                "botones": [
                    {"texto": "Ver catálogo", "action": "ver_catalogo"},
                    {"texto": "Hablar con un agente", "action": "escalar"},
                ]
            }
        return None

class HumanEscalationPymeHandler(BaseHandler):
    # MODIFICADO: acepta payload
    def handle(self, payload: dict) -> dict | None:
        pregunta_str = payload.get("pregunta", "")
        # Priorizar la acción explícita de un botón
        if payload.get("action") == "escalar" or "agente" in pregunta_str.lower() or "humano" in pregunta_str.lower():
            user_id = self.context.get('user_id')
            cliente_id = self.context.get('cliente_id') # user_id del usuario final
            
            if not user_id and not cliente_id:
                return {
                    "respuesta": "Para conectarte con un agente, por favor regístrate o inicia sesión.",
                    "fuente": "escalamiento_anonimo",
                    "botones": [
                        {"texto": "Registrarme", "action": "register"},
                        {"texto": "Iniciar sesión", "action": "login"},
                    ]
                }
            
            # Crear un ticket de chat en vivo
            asunto = _generar_asunto_con_llm(pregunta_str)
            ticket_data = {
                "asunto": asunto or "Solicitud de Chat en Vivo",
                "categoria": "Atención en Vivo",
                "detalles": f"El cliente solicitó chat en vivo: '{pregunta_str}'",
                "user_id": cliente_id if cliente_id else user_id, # Usar cliente_id si existe
                "pyme_id": user_id, # ID del dueño del bot (la PYME)
                "estado": "esperando_agente_en_vivo",
                # Puedes añadir más campos como ubicación, foto si los tienes en el contexto
            }
            try:
                ticket = servicio_tickets.crear_nuevo_ticket(tipo_ticket="pyme", ticket_data=ticket_data)
                # Al igual que en Municipio, limpiar contexto para iniciar el chat en vivo
                self.context['contexto_pyme'].clear() 
                return {
                    "respuesta": f"Te conecto con un agente humano. Tu número de chat es **P-{ticket.nro_ticket}**. Por favor, aguardá un momento.",
                    "fuente": "escalamiento_humano_pyme",
                    "ticket_id": ticket.id, # IMPORTANTE: Pasar el ticket_id al frontend
                    "botones": [
                        {"texto": "Ver estado del chat", "action": "consultar_estado_ticket", "ticket_id": ticket.id},
                    ]
                }
            except Exception as e:
                logger.error(f"[PYME] Error al crear ticket de chat en vivo: {e}", exc_info=True)
                return {
                    "respuesta": "Hubo un problema al intentar conectar con un agente. Por favor, intenta más tarde o llama a la empresa.",
                    "fuente": "error_escalamiento",
                    "botones": [
                        {"texto": "Volver al chat", "action": "chat_normal"},
                    ]
                }
        return None

class LLMHandler(BaseHandler):
    """Fallback de IA generativa para consultas generales."""
    # MODIFICADO: acepta payload
    def handle(self, payload: dict) -> dict | None:
        pregunta_str = payload.get("pregunta", "") # Extrae el string de la pregunta
        # Intenta obtener información específica de la web de la pyme si está scrapeada
        user_id = self.context.get('user_id')
        nombre_pyme = self.context.get('nombre_pyme', 'la empresa')
        contexto_scraped = ""
        if user_id:
            try:
                # Asumo que tienes una forma de obtener el contenido web relevante de la PYME por user_id
                # (ej. guardado previamente con obtener_info_web o subido)
                sitio_info = SitioWebInfo.query.filter_by(user_id=user_id, tipo="contenido_general_pyme").first()
                if sitio_info and sitio_info.datos_json:
                    contexto_scraped = json.loads(sitio_info.datos_json).get("contenido", "")
                if not contexto_scraped: # Si no hay contenido específico, busca más amplio
                    # Esto es un placeholder. La lógica real podría ser buscar en web, si se permite.
                    # Por ahora, si no hay info específica, el LLM responderá de forma más general.
                    pass 
            except Exception as e:
                logger.error(f"[LLMHandler] Error al obtener info web para {user_id}: {e}", exc_info=True)
                contexto_scraped = "" # Limpiar contexto en caso de error

        prompt_final = PROMPT_PYME_CON_CONTEXTO.format(
            nombre_pyme=nombre_pyme,
            contexto_scraped=contexto_scraped,
            pregunta_usuario=pregunta_str
        )
        
        respuesta = get_cohere_response(
            message=prompt_final,
            preamble=f"Eres Chatboc, un agente de ventas y atención al cliente para {nombre_pyme}. Responde concisa y útilmente.",
        )
        
        if respuesta and "no tengo información suficiente" not in respuesta.lower() and "no puedo responder" not in respuesta.lower():
            return {
                "respuesta": respuesta,
                "fuente": "llm_pyme_con_contexto",
            }
        
        return None # No responde si la IA no tiene información específica

class IntentHandler(BaseHandler):
    """Maneja intenciones específicas no cubiertas por otros handlers."""
    # MODIFICADO: acepta payload
    def handle(self, payload: dict) -> dict | None:
        pregunta_str = payload.get("pregunta", "") # Extrae el string de la pregunta
        # Ejemplo: si la intención es 'consultar_horario'
        if self.context.get('intencion') == 'consultar_horario':
            # Llamar a la herramienta consultar_horario_actual
            from services.herramientas_pyme import consultar_horario_actual
            resultado_herramienta = consultar_horario_actual(self.context.get('user_obj'))
            respuesta_dict = json.loads(resultado_herramienta)
            return {
                "respuesta": respuesta_dict.get("respuesta", "No puedo consultar el horario en este momento."),
                "fuente": "intencion_horario",
            }
        
        if self.context.get('intencion') == 'consultar_ubicacion':
            # Asumo que el user_obj tiene latitud/longitud
            lat = getattr(self.context.get('user_obj'), 'latitud', None)
            lon = getattr(self.context.get('user_obj'), 'longitud', None)
            direccion = getattr(self.context.get('user_obj'), 'direccion', 'nuestra dirección')
            
            if lat is not None and lon is not None:
                map_url = f"https://www.google.com/maps/search/?api=1&query={lat},{lon}"
                return {
                    "respuesta": f"Nuestra dirección es: {direccion}. Puedes verla en el mapa aquí: {map_url}",
                    "fuente": "intencion_ubicacion",
                    "botones": [{"texto": "Ver en Mapa", "url": map_url}]
                }
            return {
                "respuesta": f"Nuestra dirección es: {direccion}. Visítanos cuando quieras.",
                "fuente": "intencion_ubicacion",
            }

        return None

class EngancheAnonimoHandler(BaseHandler):
    # MODIFICADO: acepta payload
    def handle(self, payload: dict) -> dict | None:
        pregunta_str = payload.get("pregunta", "") # Extrae el string de la pregunta
        
        # Si ya hay un user_id, no se aplica este enganche
        if self.context.get('user_id'):
            return None

        # Si el usuario es anónimo y NO tiene anon_id (es la primera interacción real)
        # o si la acción es "continuar_anonimo"
        if not self.context.get('anon_id') or payload.get("action") == "continuar_anonimo":
            # El backend debe generar un anon_id si no existe
            if not self.context.get('anon_id'):
                # Idealmente, el anon_id se crea en el endpoint /ask si es anónimo
                # Aquí solo confirmamos si existe para el mensaje de enganche
                pass # No generamos anon_id aquí, se genera en routes/chat.py
            
            # Si el usuario ya está logueado o ya tiene un anon_id, este handler no debería responder.
            if self.context.get('user_id') or self.context.get('anon_id'):
                return None

            return {
                "respuesta": "¡Hola! Soy Chatboc, tu asistente de {nombre_pyme}. Para ver precios exclusivos y hacer pedidos, por favor regístrate o ingresá tus datos. ¿Querés continuar como invitado o iniciar sesión?".format(nombre_pyme=self.context.get('nombre_pyme')),
                "fuente": "enganche_anonimo",
                "botones": [
                    {"texto": "Registrarme", "action": "register"},
                    {"texto": "Iniciar sesión", "action": "login"},
                    {"texto": "Continuar como invitado", "action": "continuar_anonimo"}, # Nuevo botón para continuar
                ]
            }
        return None

class SalesEngageHandler(BaseHandler):
    """Ofrece sugerencias comerciales inteligentes y personalizadas."""
    # MODIFICADO: acepta payload
    def handle(self, payload: dict) -> dict | None:
        pregunta_str = payload.get("pregunta", "")
        sugerencias = sugerencias_por_rubro(self.context.get('rubro_nombre'))
        historial = self.context.get('mensajes_previos', [])
        ultima_interaccion_text = historial[-2]['content'] if len(historial) > 1 else "" # Accede al contenido de la interacción previa

        # Si la última interacción del bot fue mostrar un catálogo/producto
        if "producto" in ultima_interaccion_text.lower() or "catálogo" in ultima_interaccion_text.lower() or "catalogo_vector" == self.context.get("fuente_respuesta_anterior"): # Chequea la fuente anterior si la guardas
            return {
                "respuesta": "¿Te gustaría que te ayude a armar tu pedido? Puedo recomendarte los productos más elegidos o ayudarte a comparar opciones.",
                "fuente": "venta_sugerencia_accion",
                "botones": [
                    {"texto": "Quiero recomendaciones", "action": "recomendar"},
                    {"texto": "Agregar al pedido", "action": "add_to_cart"},
                    {"texto": "Hablar con un agente", "action": "escalar"},
                ]
            }
        
        if sugerencias:
            texto = reemplazar_placeholders(random.choice(sugerencias), self.context.get('user_obj'))
            return {
                "respuesta": texto + "\n¿Te gustaría recibir una oferta personalizada o ayuda para tu compra?",
                "fuente": "sugerencia_venta",
                "botones": [
                    {"texto": "Ver ofertas", "action": "ofertas"},
                    {"texto": "Hablar con un agente", "action": "escalar"},
                ]
            }
        return None

class CrossSellHandler(BaseHandler):
    """Sugiere productos complementarios o más vendidos para aumentar el ticket promedio."""
    # MODIFICADO: acepta payload
    def handle(self, payload: dict) -> dict | None:
        pregunta_str = payload.get("pregunta", "")
        productos = self.context['contexto_pyme'].get('productos_mostrados_catalogo', [])
        if productos:
            # Sugiere productos de otra categoría o los más vendidos
            sugeridos = [p for p in productos if p.get("destacado")] or productos[:3]
            sugeridos = ordenar_productos_para_venta(sugeridos)
            if sugeridos:
                texto = "Muchos clientes también llevan estos productos complementarios. ¡Aprovechá para agregarlos a tu compra!"
                lista = "\n".join([f"- {p.get('nombre')} ({p.get('precio_str','Consultar')})" for p in sugeridos[:3]])
                return {
                    "respuesta": f"{texto}\n{lista}\n¿Te gustaría conocer más o agregarlos al pedido?",
                    "fuente": "cross_sell_pyme",
                    "botones": [
                        {"texto": "Agregar al pedido", "action": "add_to_cart"},
                        {"texto": "Ver más accesorios", "action": "ver_mas"},
                    ]
                }
        return None

class OfertaPersonalizadaHandler(BaseHandler):
    # MODIFICADO: acepta payload
    def handle(self, payload: dict) -> dict | None:
        pregunta_str = payload.get("pregunta", "")
        if self.context.get('user_obj') and self.context['user_obj'].plan != "anonimo":
            return {
                "respuesta": "¡Por ser cliente frecuente, tenés un 10% de descuento en tu próxima compra! ¿Querés aprovecharlo ahora?",
                "fuente": "oferta_personalizada",
                "botones": [
                    {"texto": "Sí, quiero el descuento", "action": "usar_descuento"},
                    {"texto": "Ver catálogo", "action": "ver_catalogo"},
                ]
            }
        return None

class EnvioResumenHandler(BaseHandler):
    """
    Ofrece y realiza el envío del resumen de pedido por WhatsApp, email o SMS,
    reutilizando la lógica existente y guiando al usuario a concretar la compra.
    Ahora también genera y envía un link de pago si el pedido está listo.
    """
    # MODIFICADO: acepta payload
    def handle(self, payload: dict) -> dict | None:
        pregunta_str = payload.get("pregunta", "")
        texto = pregunta_str.lower()
        user = self.context.get('user_obj')
        pedido = self.context['contexto_pyme'].get('pedido_actual')  # Debes guardar el pedido en contexto cuando se genera

        # Simulación de generación de link de pago (puedes reemplazar por tu integración real)
        def generar_link_pago(pedido):
            if not pedido:
                return None
            # Aquí deberías integrar con tu pasarela de pagos real
            return f"https://pagos.tupyme.com/pagar?pedido={pedido.get('id', 'demo')}"

        # Detecta preferencia de canal
        if "whatsapp" in texto or "wasap" in texto or payload.get("action") == "enviar_whatsapp": # Check action
            if user and user.telefono:
                try:
                    resumen_pedido_str = json.dumps(pedido, ensure_ascii=False, indent=2) # Convertir pedido a string para envío
                    enviar_whatsapp(user.telefono, f"Resumen de tu pedido: {resumen_pedido_str}")
                    link_pago = generar_link_pago(pedido)
                    respuesta = "¡Listo! Te envié el resumen de tu pedido por WhatsApp."
                    if link_pago:
                        enviar_whatsapp(user.telefono, f"Podés pagar tu pedido aquí: {link_pago}")
                        respuesta += " Además, te envié el link de pago para que puedas abonar de forma segura."
                    respuesta += " ¿Querés finalizar la compra o agregar algo más?"
                except Exception as e:
                    logger.error(f"[PYME] Error enviando WhatsApp: {e}")
                    respuesta = "Hubo un problema enviando el WhatsApp. ¿Querés que lo intente por email o SMS?"
            else:
                respuesta = "No tengo tu número de WhatsApp registrado. ¿Querés ingresarlo o prefieres recibir el resumen por email?"
            return {
                "respuesta": respuesta,
                "fuente": "envio_resumen_pyme",
                "botones": [
                    {"texto": "Finalizar compra", "action": "finalizar_pedido"},
                    {"texto": "Agregar productos", "action": "ver_catalogo"},
                    {"texto": "Recibir por email", "action": "enviar_email"},
                    {"texto": "Pagar ahora", "action": "link_pago"},
                ]
            }

        if "email" in texto or "correo" in texto or payload.get("action") == "enviar_email": # Check action
            if user and user.email:
                try:
                    enviar_email_pedido_cliente(user.email, pedido)
                    link_pago = generar_link_pago(pedido)
                    respuesta = "¡Listo! Te envié el resumen de tu pedido por email."
                    if link_pago:
                        enviar_email_pedido_cliente(user.email, {"mensaje": f"Podés pagar tu pedido aquí: {link_pago}"})
                        respuesta += " Además, te envié el link de pago para que puedas abonar de forma segura."
                    respuesta += " ¿Querés finalizar la compra o agregar algo más?"
                except Exception as e:
                    logger.error(f"[PYME] Error enviando email: {e}")
                    respuesta = "Hubo un problema enviando el email. ¿Querés que lo intente por WhatsApp o SMS?"
            else:
                respuesta = "No tengo tu email registrado. ¿Querés ingresarlo o prefieres recibir el resumen por WhatsApp?"
            return {
                "respuesta": respuesta,
                "fuente": "envio_resumen_pyme",
                "botones": [
                    {"texto": "Finalizar compra", "action": "finalizar_pedido"},
                    {"texto": "Agregar productos", "action": "ver_catalogo"},
                    {"texto": "Recibir por WhatsApp", "action": "enviar_whatsapp"},
                    {"texto": "Pagar ahora", "action": "link_pago"},
                ]
            }

        if "sms" in texto or "mensaje" in texto or payload.get("action") == "enviar_sms": # Check action
            if user and user.telefono:
                try:
                    enviar_sms(user.telefono, f"Resumen de tu pedido: {json.dumps(pedido, ensure_ascii=False)}")
                    link_pago = generar_link_pago(pedido)
                    respuesta = "¡Listo! Te envié el resumen de tu pedido por SMS."
                    if link_pago:
                        enviar_sms(user.telefono, f"Podés pagar tu pedido aquí: {link_pago}")
                        respuesta += " Además, te envié el link de pago para que puedas abonar de forma segura."
                    respuesta += " ¿Querés finalizar la compra o agregar algo más?"
                except Exception as e:
                    logger.error(f"[PYME] Error enviando SMS: {e}")
                    respuesta = "Hubo un problema enviando el SMS. ¿Querés que lo intente por WhatsApp o email?"
            else:
                respuesta = "No tengo tu número de teléfono registrado. ¿Querés ingresarlo o prefieres recibir el resumen por email?"
            return {
                "respuesta": respuesta,
                "fuente": "envio_resumen_pyme",
                "botones": [
                    {"texto": "Finalizar compra", "action": "finalizar_pedido"},
                    {"texto": "Agregar productos", "action": "ver_catalogo"},
                    {"texto": "Recibir por email", "action": "enviar_email"},
                    {"texto": "Pagar ahora", "action": "link_pago"},
                ]
            }

        # Si solo pregunta por resumen, ofrece todos los canales disponibles y el link de pago si existe
        if "resumen" in texto or "pedido" in texto or "pagar" in texto or payload.get("action") == "link_pago": # Check action
            link_pago = generar_link_pago(pedido)
            botones = [
                {"texto": "WhatsApp", "action": "enviar_whatsapp"},
                {"texto": "Email", "action": "enviar_email"},
                {"texto": "SMS", "action": "enviar_sms"},
            ]
            if link_pago:
                botones.append({"texto": "Pagar ahora", "action": "link_pago", "url": link_pago})
            return {
                "respuesta": "¿Por qué canal preferís recibir el resumen de tu pedido? Y si querés, podés pagar ahora mismo con el link seguro.",
                "fuente": "envio_resumen_pyme",
                "botones": botones
            }

        return None

class PostVentaHandler(BaseHandler):
    # MODIFICADO: acepta payload
    def handle(self, payload: dict) -> dict | None:
        pregunta_str = payload.get("pregunta", "")
        if any(kw in pregunta_str.lower() for kw in ["gracias", "recibí", "llegó mi pedido"]):
            return {
                "respuesta": "¡Nos alegra que hayas recibido tu pedido! ¿Quedaste conforme? Si necesitas ayuda o querés hacer otro pedido, estoy para ayudarte. ¿Te gustaría responder una breve encuesta de satisfacción?",
                "fuente": "post_venta_pyme",
                "botones": [
                    {"texto": "Hacer otro pedido", "action": "iniciar_pedido"},
                    {"texto": "Responder encuesta", "action": "encuesta_satisfaccion"},
                    {"texto": "Hablar con un agente", "action": "escalar"},
                ]
            }
        return None

# --- HANDLERS ESPECÍFICOS DE FLUJOS ---
# Voy a definir los handlers que faltan o son más complejos aquí para el flujo de PYME

class FollowUpHandler(BaseHandler):
    """
    Handler de seguimiento inteligente: detecta si el usuario está en medio de un flujo (pedido, reclamo, etc.)
    y lo guía para concretar la acción, cerrar ventas o resolver dudas, evitando que abandone el proceso.
    """
    # MODIFICADO: acepta payload
    def handle(self, payload: dict) -> dict | None:
        pregunta_str = payload.get("pregunta", "")
        contexto_pyme = self.context.get('contexto_pyme', {})
        estado = deserialize_state(contexto_pyme.get('estado_conversacion'))

        # Si el usuario está confirmando un pedido
        if estado == PymeConversationState.CONFIRMANDO_PEDIDO_TEMP:
            if "cancelar" in pregunta_str.lower() or "no quiero" in pregunta_str.lower() or payload.get("action") == "cancelar_pedido": # Check action
                contexto_pyme.clear()
                return {
                    "respuesta": "Entendido, no se generará el pedido. ¿Te gustaría ver otros productos o recibir asesoramiento?",
                    "fuente": "pedido_cancelado",
                    "botones": [
                        {"texto": "Ver catálogo", "action": "ver_catalogo"},
                        {"texto": "Hablar con un agente", "action": "escalar"},
                    ]
                }
            # Si la acción es confirmar pedido, lo gestiona el PedidoHandler
            if payload.get("action") == "confirmar_pedido":
                return PedidoHandler(self.context).handle(payload) # Pasa el payload al PedidoHandler para la confirmación final

            return {
                "respuesta": "¿Confirmás tu pedido? Si necesitas modificarlo o agregar productos, avisame. También podés hablar con un agente.",
                "fuente": "seguimiento_confirmacion_pedido",
                "botones": [
                    {"texto": "Confirmar pedido", "action": "confirmar_pedido"},
                    {"texto": "Agregar productos", "action": "ver_catalogo"},
                    {"texto": "Hablar con un agente", "action": "escalar"},
                ]
            }

        # Si el usuario está en otro flujo, puedes agregar más lógica aquí
        if estado == PymeConversationState.ESPERANDO_DETALLES_PEDIDO:
            # Esto puede ser manejado por PedidoHandler también
            return PedidoHandler(self.context).handle(payload)

        # Si el usuario está en espera de número de pedido o ticket
        if estado in [PymeConversationState.ESPERANDO_NUMERO_PEDIDO, PymeConversationState.ESPERANDO_NUMERO_TICKET]:
            return {
                "respuesta": "Por favor, indicame el número correspondiente para poder ayudarte.",
                "fuente": "seguimiento_numero_pedido_ticket",
                "botones": [
                    {"texto": "No lo tengo", "action": "no_tengo_numero"},
                    {"texto": "Hablar con un agente", "action": "escalar"},
                ]
            }
        
        # Nuevos estados para reclamos PYME
        if estado == PymeConversationState.ESPERANDO_DETALLES_RECLAMO:
            return ClaimHandler(self.context).handle(payload)
        
        if estado == PymeConversationState.ESPERANDO_ADJUNTOS_RECLAMO_PYME:
            return ClaimHandler(self.context).handle(payload) # ClaimHandler manejará adjuntos
        
        if estado == PymeConversationState.ESPERANDO_CONFIRMACION_RECLAMO_PYME:
            return ClaimHandler(self.context).handle(payload) # ClaimHandler manejará confirmación

        # Si no hay flujo activo, no responde
        return None

class IntentClassifierPymeHandler(BaseHandler):
    """
    Clasifica la intención del usuario usando LLM y contexto, para enrutar la consulta al handler adecuado.
    Potenciado para ventas, reclamos, consultas y acciones comerciales.
    """
    # MODIFICADO: acepta payload
    def handle(self, payload: dict) -> dict | None:
        pregunta_str = payload.get("pregunta", "")
        contexto_pyme = self.context.get('contexto_pyme', {})
        memoria = contexto_pyme # Referencia para claridad, es lo mismo que contexto_pyme
        texto_normalizado = normalizar_texto(pregunta_str)

        # Si hay una acción de botón, usarla como intención principal si no hay estado activo
        if payload.get("action") and not memoria.get("estado_conversacion"):
            self.context["intencion"] = payload["action"]
            logger.info(f"[PYME] Intención: {payload['action']} (desde action del botón)")
            return None # Dejar que el siguiente handler en la cadena lo procese

        # Palabras clave para agente humano
        KEYWORDS_AGENTE = [
            "agente", "humano", "persona", "representante", "operador", "hablar con alguien", "atención humana", "escalar"
        ]
        if any(kw in texto_normalizado for kw in KEYWORDS_AGENTE):
            self.context["intencion"] = "hablar_con_agente_pyme"
            memoria.clear() # Limpiar memoria si el usuario quiere hablar con agente
            logger.info(f"[PYME] Intención: hablar_con_agente_pyme (por palabra clave)")
            return None

        # Palabras clave para pedido
        KEYWORDS_PEDIDO = [
            "comprar", "pedido", "cotización", "encargar", "quiero pedir", "quiero comprar", "ordenar", "solicitar", "add_to_cart" # Añadir action
        ]
        if any(kw in texto_normalizado for kw in KEYWORDS_PEDIDO):
            self.context["intencion"] = "iniciar_pedido"
            # No limpiar memoria aquí si es parte de un flujo de pedido existente
            logger.info(f"[PYME] Intención: iniciar_pedido (por palabra clave)")
            return None

        # Palabras clave para estado de pedido/ticket
        KEYWORDS_ESTADO = [
            "estado", "seguimiento", "dónde está mi pedido", "cómo va mi pedido", "ticket", "reclamo", "consultar_estado_pedido", "consultar_estado_ticket"
        ]
        if any(kw in texto_normalizado for kw in KEYWORDS_ESTADO):
            self.context["intencion"] = "consultar_estado_pedido" # Unificar a este para PYME
            # No limpiar memoria aquí si es parte de un flujo existente
            logger.info(f"[PYME] Intención: consultar_estado_pedido (por palabra clave)")
            return None

        # Palabras clave para stock
        KEYWORDS_STOCK = [
            "stock", "hay", "disponible", "queda", "tienen", "disponibilidad", "consultar_stock" # Añadir action
        ]
        if any(kw in texto_normalizado for kw in KEYWORDS_STOCK):
            self.context["intencion"] = "consultar_stock"
            # No limpiar memoria aquí si es parte de un flujo existente
            logger.info(f"[PYME] Intención: consultar_stock (por palabra clave)")
            return None

        # Palabras clave para horarios
        KEYWORDS_HORARIO = [
            "horario", "a qué hora", "cuándo abren", "cuándo cierran", "horarios", "consultar_horario"
        ]
        if any(kw in texto_normalizado for kw in KEYWORDS_HORARIO):
            self.context["intencion"] = "consultar_horario"
            # No limpiar memoria aquí
            logger.info(f"[PYME] Intención: consultar_horario (por palabra clave)")
            return None

        # Palabras clave para ubicación
        KEYWORDS_UBICACION = [
            "dónde están", "dirección", "ubicación", "cómo llego", "dónde queda", "consultar_ubicacion"
        ]
        if any(kw in texto_normalizado for kw in KEYWORDS_UBICACION):
            self.context["intencion"] = "consultar_ubicacion"
            # No limpiar memoria aquí
            logger.info(f"[PYME] Intención: consultar_ubicacion (por palabra clave)")
            return None
        
        # Palabras clave para reclamo (iniciar)
        KEYWORDS_INICIAR_RECLAMO = [
            "reclamo", "queja", "problema", "reportar", "iniciar_reclamo_pyme" # Añadir action
        ]
        if any(kw in texto_normalizado for kw in KEYWORDS_INICIAR_RECLAMO):
            self.context["intencion"] = "iniciar_reclamo_pyme"
            memoria.clear() # Limpiar memoria si inicia un reclamo
            logger.info(f"[PYME] Intención: iniciar_reclamo_pyme (por palabra clave)")
            return None


        # Si no hubo match con keywords y no hay estado activo, usa LLM para clasificar intención
        if not memoria.get("estado_conversacion"):
            intencion_llm = _clasificar_intencion_pyme_con_llm(pregunta_str)
            self.context["intencion"] = intencion_llm
        else:
            self.context["intencion"] = "continuar_flujo"

        logger.info(f"[PYME] Intención (final): {self.context.get('intencion')}")
        return None

class VectorCatalogHandler(BaseHandler):
    """
    Muestra productos agrupados, ordenados y con botones de acción para maximizar ventas.
    Usa contexto, historial y preferencias si están disponibles.
    """
    # MODIFICADO: acepta payload
    def handle(self, payload: dict) -> dict | None:
        pregunta_str = payload.get("pregunta", "")
        user_id = self.context.get('user_id')
        if not user_id:
            return None

        contexto_pyme = self.context.get('contexto_pyme', {})
        estado = deserialize_state(contexto_pyme.get('estado_conversacion'))
        
        # Si la intención es iniciar pedido o estamos en un flujo de pedido/reclamo, no debería responder el catálogo general
        if self.context.get('intencion') in ['iniciar_pedido', 'iniciar_reclamo_pyme'] or estado in {
            PymeConversationState.ESPERANDO_DETALLES_PEDIDO,
            PymeConversationState.CONFIRMANDO_PEDIDO_TEMP,
            PymeConversationState.CONFIRMANDO_PEDIDO_FINAL_PASO_2,
            PymeConversationState.ESPERANDO_DETALLES_RECLAMO, # Bloquear si estamos en un reclamo
            PymeConversationState.ESPERANDO_ADJUNTOS_RECLAMO_PYME,
            PymeConversationState.ESPERANDO_CONFIRMACION_RECLAMO_PYME,
        }:
            return None

        # Si la acción explícita es ver_catalogo, forzar búsqueda de catálogo
        if payload.get("action") == "ver_catalogo" or self._has_keyword(pregunta_str, ["catalogo", "productos", "ver", "lista", "muestrame"]): # Add more keywords
            pass # Continúa para buscar catálogo
        else:
            # Si no hay intención de catálogo y no hay estado activo, no se dispara este handler por defecto
            if not self.context.get('intencion') == "general_pyme" and not estado:
                return None


        resultados = buscar_catalogo_qdrant(
            user_id=user_id,
            pregunta=pregunta_str,
            limite=DEFAULT_SEARCH_LIMIT,
            categoria=self.context.get('rubro_nombre'),
        )
        productos_mostrados = []
        if resultados:
            for hit in resultados:
                if hasattr(hit, 'payload') and isinstance(hit.payload, dict):
                    productos_mostrados.append(hit.payload)

        if productos_mostrados:
            # Ordena usando un algoritmo que prioriza destacados y relevancia
            productos_mostrados = ordenar_productos_para_venta(productos_mostrados)
            self.context['contexto_pyme']['productos_mostrados_catalogo'] = productos_mostrados

            # Agrupa por categoría si hay muchas opciones
            categorias = {}
            for p in productos_mostrados:
                cat = p.get("categoria_qdrant") or p.get("categoria") or "Otros"
                categorias.setdefault(cat, []).append(p)

            respuesta = "Estos son los productos más relevantes que encontré para vos:\n"
            for categoria, items in categorias.items():
                respuesta += f"\n🗂️ **{categoria.title()}**\n"
                for idx, p in enumerate(items[:3], 1): # Limitar a 3 por categoría en la lista inicial
                    nombre = p.get("nombre", "Producto")
                    precio = p.get("precio_str") or f"${p.get('precio', 'Consultar')}"
                    desc = p.get("descripcion", "")
                    respuesta += f"{idx}. **{nombre}** — {precio}\n"
                    if desc:
                        respuesta += f"     _{desc[:60]}_\n"
                if len(items) > 3:
                    respuesta += f"...y {len(items)-3} más en esta categoría.\n"

            respuesta += "\n¿Te gustaría agregar alguno al pedido? Decime el número o nombre del producto y la cantidad. Si querés ver más opciones, decímelo o pedí ayuda."

            return {
                'respuesta': respuesta,
                'fuente': 'catalogo_vector',
                'estado_respuesta': 'mostrar_catalogo',
                'botones': [
                    {"texto": "Agregar al pedido", "action": "add_to_cart"},
                    {"texto": "Ver más productos", "action": "ver_mas"}, # Nuevo botón para ver más
                    {"texto": "Consultar stock", "action": "consultar_stock"},
                    {"texto": "Hablar con un agente", "action": "escalar"},
                ]
            }

        return None

class PedidoHandler(BaseHandler):
    # MODIFICADO: acepta payload
    def handle(self, payload: dict) -> dict | None:
        pregunta_str = payload.get("pregunta", "")
        contexto_pyme = self.context.get('contexto_pyme', {})
        estado = deserialize_state(contexto_pyme.get('estado_conversacion'))
        user_id = self.context.get('user_id')

        # Acción: Iniciar pedido
        if self.context.get('intencion') == 'iniciar_pedido' and not estado:
            contexto_pyme.clear() # Limpiar memoria para nuevo pedido
            contexto_pyme['estado_conversacion'] = serialize_state(PymeConversationState.ESPERANDO_DETALLES_PEDIDO)
            
            # Buscar productos relevantes en el catálogo directamente
            productos_encontrados = buscar_catalogo_qdrant(
                user_id=user_id,
                pregunta=pregunta_str, # Usar la pregunta inicial
                limite=DEFAULT_SEARCH_LIMIT,
                categoria=self.context.get('rubro_nombre')
            )
            contexto_pyme['productos_mostrados_catalogo'] = productos_encontrados if productos_encontrados else []
            
            if productos_encontrados:
                respuesta_catalogo = armar_respuesta_catalogo_agrupado(productos_encontrados, max_por_categoria=3)
                return {
                    "respuesta": f"Claro, para iniciar tu pedido, estos son algunos productos que encontré:\n{respuesta_catalogo}\n\nDecime qué productos y cantidades te gustaría agregar.",
                    "fuente": "pedido_iniciado_con_sugerencia",
                    "estado_respuesta": "mostrar_catalogo_pedido",
                    "botones": [
                        {"texto": "Agregar al pedido", "action": "add_to_cart"},
                        {"texto": "Ver más productos", "action": "ver_mas"},
                        {"texto": "Finalizar pedido", "action": "finalizar_pedido"},
                    ]
                }
            return {
                "respuesta": "Claro, para iniciar tu pedido. ¿Qué productos te gustaría agregar? Si necesitas, puedo mostrarte nuestro catálogo.",
                "fuente": "pedido_iniciado",
                "estado_respuesta": "esperando_productos_pedido",
                "botones": [
                    {"texto": "Ver catálogo", "action": "ver_catalogo"},
                    {"texto": "Hablar con un agente", "action": "escalar"},
                ]
            }

        # Estado: Esperando detalles del pedido
        if estado == PymeConversationState.ESPERANDO_DETALLES_PEDIDO:
            productos_mostrados = contexto_pyme.get('productos_mostrados_catalogo', [])
            productos_con_cantidades = _extraer_cantidades_con_llm(pregunta_str, productos_mostrados)
            
            if not productos_con_cantidades:
                return {
                    "respuesta": "No entendí los productos o cantidades. Por favor, decime qué productos de la lista y qué cantidad deseas (ej. '2 unidades de vino tinto').",
                    "fuente": "pedido_error_cantidad",
                    "botones": [
                        {"texto": "Ver catálogo", "action": "ver_catalogo"},
                        {"texto": "Hablar con un agente", "action": "escalar"},
                    ]
                }
            
            contexto_pyme['items_temporales'] = productos_con_cantidades
            contexto_pyme['estado_conversacion'] = serialize_state(PymeConversationState.CONFIRMANDO_PEDIDO_TEMP)
            
            resumen_items = "\n".join([
                f"- {p['cantidad']} {p['unidad']} de {p['nombre']} ({p['precio_str']})" 
                for p in productos_con_cantidades
            ])
            monto_total = calcular_monto_total_items(productos_con_cantidades)

            return {
                "respuesta": f"Confirmá los ítems para tu pedido:\n{resumen_items}\n\nTotal estimado: ${monto_total:.2f}\n\n¿Es correcto?",
                "fuente": "pedido_resumen_temp",
                "botones": [
                    {"texto": "Confirmar pedido", "action": "confirmar_pedido"},
                    {"texto": "Agregar más productos", "action": "ver_catalogo"},
                    {"texto": "Cancelar pedido", "action": "cancelar_pedido"},
                ]
            }

        # Acción: Confirmar pedido (primer paso, desde botón)
        if payload.get("action") == "confirmar_pedido" and estado == PymeConversationState.CONFIRMANDO_PEDIDO_TEMP:
            # Aquí generas el pedido en la DB.
            items_pedido = contexto_pyme.get('items_temporales', [])
            if not items_pedido:
                contexto_pyme.clear()
                return {
                    "respuesta": "No hay ítems en tu pedido para confirmar. ¿Te gustaría empezar un pedido nuevo?",
                    "fuente": "pedido_error_no_items",
                    "botones": [
                        {"texto": "Iniciar pedido", "action": "iniciar_pedido"}
                    ]
                }
            
            # Crear el pedido real en la DB
            try:
                pedido_id = servicio_pedidos.crear_pedido(
                    user_id=user_id,
                    items=items_pedido,
                    total=calcular_monto_total_items(items_pedido)
                )
                contexto_pyme['pedido_actual'] = {"id": pedido_id, "items": items_pedido, "total": calcular_monto_total_items(items_pedido)} # Guarda el pedido final
                contexto_pyme['estado_conversacion'] = serialize_state(PymeConversationState.CONFIRMANDO_PEDIDO_FINAL_PASO_2) # Siguiente paso
                
                # Enviar notificaciones al admin y cliente
                admin_email = getattr(self.context.get('user_obj'), 'email', None)
                if admin_email:
                    enviar_email_ticket_admin(admin_email, pedido_id, items_pedido) # Asumo que esta función existe
                
                cliente_email = getattr(self.context.get('viewer_user_obj'), 'email', None) # Asumo viewer_user_obj en contexto
                if cliente_email:
                    enviar_email_pedido_cliente(cliente_email, pedido_id, items_pedido) # Asumo que esta función existe
                
                cliente_tel = getattr(self.context.get('viewer_user_obj'), 'telefono', None)
                if cliente_tel:
                    enviar_sms(cliente_tel, f"Tu pedido P-{pedido_id} ha sido confirmado por {self.context.get('nombre_pyme')}.")


                memoria.clear() # Limpiar memoria después de finalizar el pedido
                return {
                    "respuesta": f"¡Excelente! Tu pedido #{pedido_id} ha sido confirmado. ¿Por qué canal te gustaría recibir el resumen y el link de pago?",
                    "fuente": "pedido_confirmado",
                    "pedido_data": contexto_pyme['pedido_actual'], # Envía los datos del pedido al frontend
                    "botones": [
                        {"texto": "WhatsApp", "action": "enviar_whatsapp"},
                        {"texto": "Email", "action": "enviar_email"},
                        {"texto": "SMS", "action": "enviar_sms"},
                        {"texto": "Pagar ahora", "action": "link_pago"},
                    ]
                }
            except Exception as e:
                logger.error(f"[PYME] Error al crear pedido: {e}", exc_info=True)
                contexto_pyme.clear()
                return {
                    "respuesta": "Hubo un error al procesar tu pedido. Por favor, intenta de nuevo o contacta a un agente.",
                    "fuente": "pedido_error_db",
                    "botones": [
                        {"texto": "Hablar con un agente", "action": "escalar"}
                    ]
                }

        return None

class BrokenProductHandler(BaseHandler):
    # MODIFICADO: acepta payload
    def handle(self, payload: dict) -> dict | None:
        pregunta_str = payload.get("pregunta", "")
        # Esto es un placeholder. Debería manejar productos que no se encuentran.
        return None # Por ahora, no hace nada

class ClaimHandler(BaseHandler):
    """
    Gestiona el flujo de reclamos de PYME, solicitando detalles, adjuntos y confirmación.
    """
    CAMPOS_RECLAMO_PYME = ["categoria", "descripcion", "foto_url", "ubicacion_gps"]

    # MODIFICADO: acepta payload
    def handle(self, payload: dict) -> dict | None:
        pregunta_str = payload.get("pregunta", "")
        memoria = self.context.get("contexto_pyme", {})
        estado = deserialize_state(memoria.get("estado_conversacion"))
        user_id = self.context.get('user_id')
        cliente_id = self.context.get('cliente_id')
        nombre_pyme = self.context.get('nombre_pyme')

        # Iniciar reclamo
        if self.context.get('intencion') == 'iniciar_reclamo_pyme' and not estado:
            memoria.clear() # Limpiar memoria para nuevo reclamo
            memoria['estado_conversacion'] = serialize_state(PymeConversationState.ESPERANDO_DETALLES_RECLAMO)
            
            # Intentar extraer datos desde el primer mensaje si es un reclamo "inteligente"
            prompt = f"""
            Extrae del siguiente mensaje del cliente los siguientes datos si están presentes:
            - categoria (motivo del reclamo, ej: envío, producto, servicio, pago, etc.)
            - descripcion (detalle del problema)

            Mensaje: "{pregunta_str}"

            Devuelve solo JSON con esos campos. Ejemplo:
            {{ "categoria": "envío", "descripcion": "mi pedido llegó roto" }}
            """
            try:
                resp = get_cohere_response(message=prompt, preamble="Extrae los campos y devuelve solo JSON.")
                datos_llm = json.loads(resp) if resp else {}
                memoria['categoria_reclamo'] = datos_llm.get('categoria')
                memoria['descripcion_reclamo'] = datos_llm.get('descripcion')
            except Exception as e:
                logger.error(f"[CLAIM] Error LLM extrayendo reclamo: {e}", exc_info=True)

            if memoria.get('descripcion_reclamo'):
                memoria['estado_conversacion'] = serialize_state(PymeConversationState.ESPERANDO_ADJUNTOS_RECLAMO_PYME)
                return {
                    "respuesta": f"Entendido. Tu reclamo es sobre: {memoria['descripcion_reclamo']}. ¿Querés adjuntar una foto o compartir tu ubicación?",
                    "fuente": "reclamo_iniciado",
                    "botones": [
                        {"texto": "Adjuntar foto", "action": "adjuntar_foto"},
                        {"texto": "Compartir ubicación", "action": "compartir_ubicacion"},
                        {"texto": "No, continuar", "action": "sin_adjuntos"}
                    ]
                }

            return {
                "respuesta": "¿Sobre qué es tu reclamo? Por favor, describímelo brevemente.",
                "fuente": "reclamo_iniciado",
            }

        # Estado: Esperando detalles del reclamo
        if estado == PymeConversationState.ESPERANDO_DETALLES_RECLAMO:
            if not pregunta_str.strip():
                return {"respuesta": "Por favor, describí el problema para tu reclamo."}
            
            memoria['descripcion_reclamo'] = pregunta_str.strip()
            memoria['estado_conversacion'] = serialize_state(PymeConversationState.ESPERANDO_ADJUNTOS_RECLAMO_PYME)
            return {
                "respuesta": "¿Querés adjuntar una foto o compartir tu ubicación?",
                "fuente": "reclamo_detalles",
                "botones": [
                    {"texto": "Adjuntar foto", "action": "adjuntar_foto"},
                    {"texto": "Compartir ubicación", "action": "compartir_ubicacion"},
                    {"texto": "No, continuar", "action": "sin_adjuntos"}
                ]
            }

        # Estado: Esperando adjuntos (foto/ubicación)
        if estado == PymeConversationState.ESPERANDO_ADJUNTOS_RECLAMO_PYME:
            accion = payload.get("action", "").lower() or normalizar_texto(pregunta_str)
            
            # PROCESAR ADJUNTOS REALES
            if payload.get("es_foto") and payload.get("archivo_url"):
                memoria["foto_url"] = payload.get("archivo_url")
                logger.info(f"[CLAIM] Foto adjunta para PYME: {memoria['foto_url']}")
                memoria["estado_conversacion"] = serialize_state(PymeConversationState.ESPERANDO_CONFIRMACION_RECLAMO_PYME)
                resumen = self.build_detalles_memoria(memoria)
                return {
                    "respuesta": f"¡Foto recibida! ¿Confirmás el reclamo con estos datos?\n{resumen}",
                    "fuente": "reclamo_foto_recibida",
                    "botones": [
                        {"texto": "Confirmar reclamo", "action": "confirmar_reclamo"},
                        {"texto": "Editar datos", "action": "editar_reclamo"}
                    ]
                }
            
            if payload.get("es_ubicacion") and payload.get("ubicacion_usuario"):
                memoria["ubicacion_gps"] = payload.get("ubicacion_usuario")
                logger.info(f"[CLAIM] Ubicación adjunta para PYME: {memoria['ubicacion_gps']}")
                memoria["estado_conversacion"] = serialize_state(PymeConversationState.ESPERANDO_CONFIRMACION_RECLAMO_PYME)
                resumen = self.build_detalles_memoria(memoria)
                return {
                    "respuesta": f"¡Ubicación recibida! ¿Confirmás el reclamo con estos datos?\n{resumen}",
                    "fuente": "reclamo_ubicacion_recibida",
                    "botones": [
                        {"texto": "Confirmar reclamo", "action": "confirmar_reclamo"},
                        {"texto": "Editar datos", "action": "editar_reclamo"}
                    ]
                }

            # ACCIONES DE BOTONES
            if accion == "sin_adjuntos": # El botón "No, continuar"
                memoria["estado_conversacion"] = serialize_state(PymeConversationState.ESPERANDO_CONFIRMACION_RECLAMO_PYME)
                resumen = self.build_detalles_memoria(memoria)
                return {
                    "respuesta": f"¿Confirmás el reclamo con estos datos?\n{resumen}",
                    "fuente": "reclamo_sin_adjuntos",
                    "botones": [
                        {"texto": "Confirmar reclamo", "action": "confirmar_reclamo"},
                        {"texto": "Editar datos", "action": "editar_reclamo"}
                    ]
                }
            
            if accion in ["adjuntar_foto", "compartir_ubicacion"]:
                # El frontend ya maneja la UX de abrir el selector/pedir GPS. 
                # El backend simplemente espera el siguiente mensaje que contendrá el adjunto real.
                return None # No responder, solo esperar el siguiente input

            # FALLBACK si no es adjunto real ni acción reconocida
            return {
                "respuesta": "Todavía no recibí ningún adjunto válido. ¿Querés adjuntar una foto o compartir tu ubicación, o continuar sin adjuntos?",
                "fuente": "reclamo_adjunto_invalido_reintento",
                "botones": [
                    {"texto": "Adjuntar foto", "action": "adjuntar_foto"},
                    {"texto": "Compartir ubicación", "action": "compartir_ubicacion"},
                    {"texto": "No, continuar", "action": "sin_adjuntos"}
                ]
            }

        # Estado: Esperando confirmación final del reclamo
        if estado == PymeConversationState.ESPERANDO_CONFIRMACION_RECLAMO_PYME:
            accion = payload.get("action", "").lower() or normalizar_texto(pregunta_str)

            if accion == "confirmar_reclamo":
                # Validar que los datos mínimos estén
                if not memoria.get('descripcion_reclamo'):
                    memoria.clear()
                    return {"respuesta": "Parece que falta la descripción del reclamo. ¿Querés empezar de nuevo?", "botones":[{"texto": "Iniciar reclamo", "action": "iniciar_reclamo_pyme"}]}
                
                # Crear el ticket de reclamo final
                try:
                    ticket_data = {
                        "asunto": memoria.get('categoria_reclamo', 'Reclamo General') + ": " + memoria['descripcion_reclamo'][:50],
                        "categoria": memoria.get('categoria_reclamo', 'General'),
                        "detalles": self.build_detalles_memoria(memoria), # Reutiliza la función para el resumen
                        "user_id": cliente_id if cliente_id else user_id,
                        "pyme_id": user_id,
                        "estado": "nuevo",
                        "ubicacion": memoria.get('ubicacion_gps'),
                        "foto_url": memoria.get('foto_url'),
                        "anon_id": self.context.get('anon_id')
                    }
                    ticket = servicio_tickets.crear_nuevo_ticket(tipo_ticket="pyme", ticket_data=ticket_data)
                    
                    # Enviar notificaciones (asumo que tienes email/telefono en user_obj/viewer_user_obj)
                    if user_id:
                        admin_email = getattr(self.context.get('user_obj'), 'email', None)
                        if admin_email:
                            enviar_email_ticket_admin(admin_email, ticket.nro_ticket, ticket_data['detalles'])
                    
                    cliente_email = getattr(self.context.get('viewer_user_obj'), 'email', None) # Asumo viewer_user_obj
                    if cliente_email:
                        enviar_email_pedido_cliente(cliente_email, ticket.nro_ticket, ticket_data['detalles'])
                    
                    cliente_tel = getattr(self.context.get('viewer_user_obj'), 'telefono', None)
                    if cliente_tel:
                        enviar_sms(cliente_tel, f"Tu reclamo P-{ticket.nro_ticket} ha sido recibido por {nombre_pyme}.")


                    memoria.clear() # Limpiar memoria después de finalizar el reclamo
                    return {
                        "respuesta": f"¡Listo! Tu reclamo #{ticket.nro_ticket} ha sido registrado exitosamente. Te mantendremos informado sobre el estado.",
                        "fuente": "reclamo_confirmado",
                        "ticket_id": ticket.id,
                        "botones": [
                            {"texto": "Consultar estado de ticket", "action": "consultar_estado_ticket"},
                            {"texto": "Hacer otro reclamo", "action": "iniciar_reclamo_pyme"},
                            {"texto": "Hablar con un agente", "action": "escalar"}
                        ]
                    }
                except Exception as e:
                    logger.error(f"[CLAIM] Error al crear ticket de reclamo: {e}", exc_info=True)
                    memoria.clear()
                    return {"respuesta": "Hubo un error al registrar tu reclamo. Por favor, intenta más tarde o contacta a un agente.", "fuente": "reclamo_error"}
            
            elif accion == "editar_datos":
                memoria['estado_conversacion'] = serialize_state(PymeConversationState.ESPERANDO_DETALLES_RECLAMO)
                return {"respuesta": "¿Qué parte del reclamo quieres editar? Describe los cambios o ingresa nuevamente los detalles.", "fuente": "reclamo_editar"}
            
            # Si no es confirmar ni editar, repite la pregunta
            return {
                "respuesta": "¿Confirmás tu reclamo con los datos ingresados o querés editarlos?",
                "fuente": "reclamo_confirmacion_reintento",
                "botones": [
                    {"texto": "Confirmar reclamo", "action": "confirmar_reclamo"},
                    {"texto": "Editar datos", "action": "editar_reclamo"}
                ]
            }
        return None

class TicketStatusHandler(BaseHandler):
    # MODIFICADO: acepta payload
    def handle(self, payload: dict) -> dict | None:
        pregunta_str = payload.get("pregunta", "")
        memoria = self.context.get("contexto_pyme", {})
        estado = deserialize_state(memoria.get("estado_conversacion"))
        user_id = self.context.get('user_id')
        cliente_id = self.context.get('cliente_id')
        
        # Iniciar consulta de estado de pedido/ticket
        if self.context.get('intencion') == 'consultar_estado_pedido' and not estado:
            memoria.clear()
            memoria['estado_conversacion'] = serialize_state(PymeConversationState.ESPERANDO_NUMERO_PEDIDO)
            return {"respuesta": "Por favor, indicame el número de tu pedido para consultar su estado.", "fuente": "estado_pedido_solicitar_numero"}
        
        # Esperando número de pedido
        if estado == PymeConversationState.ESPERANDO_NUMERO_PEDIDO:
            match_num = re.search(r'\d+', pregunta_str)
            if not match_num:
                return {"respuesta": "No reconocí un número de pedido válido. Por favor, ingresá solo los dígitos.", "fuente": "estado_pedido_error_numero"}
            
            nro_pedido = int(match_num.group(0))
            pedido = PymePedido.query.filter_by(nro_pedido=nro_pedido, user_id=user_id).first() # Buscar por nro y pyme_id
            
            if not pedido:
                return {"respuesta": f"No encontré el pedido #{nro_pedido}. Verificá si lo escribiste bien o si fue creado con este usuario.", "fuente": "estado_pedido_no_encontrado"}
            
            memoria.clear()
            return {
                "respuesta": f"El pedido #{pedido.nro_pedido} está en estado: **{pedido.estado}**.\nTotal: ${pedido.total:.2f}. Detalles: {pedido.detalles_json}",
                "fuente": "estado_pedido_encontrado",
                "botones": [
                    {"texto": "Ver ítems", "action": "ver_pedido_items", "pedido_id": pedido.id},
                    {"texto": "Contactar por este pedido", "action": "escalar_por_pedido", "pedido_id": pedido.id},
                ]
            }

        # Iniciar consulta de estado de ticket
        if self.context.get('intencion') == 'consultar_estado_ticket' and not estado:
            memoria.clear()
            memoria['estado_conversacion'] = serialize_state(PymeConversationState.ESPERANDO_NUMERO_TICKET)
            return {"respuesta": "Por favor, indicame el número de tu ticket para consultar su estado.", "fuente": "estado_ticket_solicitar_numero"}

        # Esperando número de ticket
        if estado == PymeConversationState.ESPERANDO_NUMERO_TICKET:
            match_num = re.search(r'\d+', pregunta_str)
            if not match_num:
                return {"respuesta": "No reconocí un número de ticket válido. Por favor, ingresá solo los dígitos.", "fuente": "estado_ticket_error_numero"}
            
            nro_ticket = int(match_num.group(0))
            ticket = PymeTicket.query.filter_by(nro_ticket=nro_ticket, user_id=user_id).first() # Buscar por nro y pyme_id
            
            if not ticket:
                return {"respuesta": f"No encontré el ticket #{nro_ticket}. Verificá si lo escribiste bien o si fue creado con este usuario.", "fuente": "estado_ticket_no_encontrado"}
            
            memoria.clear()
            return {
                "respuesta": f"El ticket #{ticket.nro_ticket} (Asunto: {ticket.asunto}) está en estado: **{ticket.estado}**.\nDetalles: {ticket.detalles}",
                "fuente": "estado_ticket_encontrado",
                "botones": [
                    {"texto": "Contactar por este ticket", "action": "escalar_por_ticket", "ticket_id": ticket.id},
                ]
            }
        return None

# --- Otros handlers si los tienes definidos o quieres que los creemos ---
# Puedes añadir aquí los handlers para RecomendacionHandler, UpsellHandler,
# BrokenProductHandler, ClaimHandler (si no es el mismo que TicketStatusHandler), etc.
# Asegúrate de que todos sigan el patrón handle(self, payload: dict).

class RecomendacionHandler(BaseHandler):
    """Sugiere productos recomendados según historial, ticket promedio y catálogo."""
    # MODIFICADO: acepta payload
    def handle(self, payload: dict) -> dict | None:
        pregunta_str = payload.get("pregunta", "")
        productos = self.context['contexto_pyme'].get('productos_mostrados_catalogo', [])
        if not productos:
            return None
        # Ejemplo: sugiere los más vendidos o destacados
        recomendados = [p for p in productos if p.get("destacado")] or productos[:3]
        recomendados = ordenar_productos_para_venta(recomendados)
        if recomendados:
            texto = "Nuestros clientes aman estos productos. ¿Te gustaría probarlos?"
            lista = "\n".join([f"- {p.get('nombre')} ({p.get('precio_str','Consultar')})" for p in recomendados])
            return {
                "respuesta": f"{texto}\n{lista}\n¿Te gustaría agregarlos al pedido o ver más detalles?",
                "fuente": "recomendacion_pyme",
                "botones": [
                    {"texto": "Agregar recomendados", "action": "add_to_cart"},
                    {"texto": "Ver más productos", "action": "ver_mas"},
                    {"texto": "Consultar stock", "action": "consultar_stock"},
                    {"texto": "Hablar con un agente", "action": "escalar"},
                ]
            }
        return None

class UpsellHandler(BaseHandler):
    """Sugiere versiones premium, packs o mayores cantidades para aumentar el ticket."""
    # MODIFICADO: acepta payload
    def handle(self, payload: dict) -> dict | None:
        pregunta_str = payload.get("pregunta", "")
        productos = self.context['contexto_pyme'].get('productos_mostrados_catalogo', [])
        if not productos:
            return None
        # Ejemplo: si el usuario pide un producto barato, sugiere el premium
        baratos = [p for p in productos if float(p.get("precio_float", 0)) < 5000]
        premium = [p for p in productos if float(p.get("precio_float", 0)) > 10000]
        premium = ordenar_productos_para_venta(premium)
        if baratos and premium:
            texto = "Tenemos opciones premium y packs con mayor valor agregado. ¡Ideal para sacar el máximo provecho!"
            lista = "\n".join([f"- {p.get('nombre')} ({p.get('precio_str','Consultar')})" for p in premium[:2]])
            return {
                "respuesta": f"{texto}\n{lista}\n¿Te gustaría conocer más o agregarlos al pedido?",
                "fuente": "upsell_pyme",
                "botones": [
                    {"texto": "Ver packs premium", "action": "ver_premium"},
                    {"texto": "Agregar al pedido", "action": "add_to_cart"},
                ]
            }
        return None

class BrokenProductHandler(BaseHandler):
    # MODIFICADO: acepta payload
    def handle(self, payload: dict) -> dict | None:
        pregunta_str = payload.get("pregunta", "")
        # Esto es un placeholder. Debería manejar productos que no se encuentran.
        return None # Por ahora, no hace nada

# --- FUNCIÓN ORQUESTADORA PRINCIPAL MEJORADA ---
def responder_pyme(pregunta_original, owner_user, rubro_obj, viewer_user=None, anon_id=None, **kwargs):
    logger.info(f"[INICIO] Pregunta recibida: '{pregunta_original}'")
    
    # --- MODIFICACIÓN CLAVE: Preparar el payload ---
    received_payload = {}
    if isinstance(pregunta_original, dict): # Si el frontend envió un objeto JSON completo en el campo 'pregunta'
        received_payload = pregunta_original
        pregunta_str = received_payload.get("pregunta", "")
    else: # Si el frontend envió un string simple
        pregunta_str = pregunta_original
        received_payload["pregunta"] = pregunta_original
    
    # Combinar los kwargs adicionales de la llamada API con el payload recibido
    for key, value in kwargs.items():
        received_payload[key] = value

    # Obtener el contexto de la sesión
    contexto_previo = received_payload.get('contexto_previo', {})
    contexto_pyme = contexto_previo.get(CONTEXTO_PYME_SESION, {})

    # Deserializar el estado de conversación
    estado_guardado_str = contexto_pyme.get('estado_conversacion')
    if estado_guardado_str and isinstance(estado_guardado_str, str):
        contexto_pyme['estado_conversacion'] = deserialize_state(estado_guardado_str)
    else:
        contexto_pyme['estado_conversacion'] = None

    # Inicializar el diccionario 'context'
    context = {
        "contexto_pyme": contexto_pyme,
        "user_obj": owner_user,
        "rubro_obj": rubro_obj,
        "user_id": getattr(owner_user, "id", None),
        "cliente_id": getattr(viewer_user, "id", None), # El usuario final interactuando
        "anon_id": anon_id,
        "nombre_pyme": getattr(owner_user, "nombre_empresa", "la empresa") if owner_user else "la empresa",
        "telefono": getattr(owner_user, "telefono", "") if owner_user else "",
        "direccion": getattr(owner_user, "direccion", "") if owner_user else "",
        "email": getattr(owner_user, "email", "") if owner_user else "",
        "plan": getattr(owner_user, "plan", "anonimo") if owner_user else "anonimo",
        "preguntas_usadas": getattr(owner_user, "preguntas_usadas", 0) if owner_user else 0,
        "limite_preguntas": limite_para_usuario(owner_user) if owner_user else None,
        "rubro_nombre": getattr(rubro_obj, "nombre", "empresa").lower() if rubro_obj else "desconocido",
        "mensajes_previos": flask_session.get(NOMBRE_HISTORIAL_SESION, []),
        # Incluir datos de adjuntos y acciones directamente en el contexto para fácil acceso de los handlers
        "ubicacion_usuario": received_payload.get("ubicacion_usuario"),
        "foto_url": received_payload.get("archivo_url") if received_payload.get("es_foto") else None,
        "es_foto": received_payload.get("es_foto", False),
        "es_ubicacion": received_payload.get("es_ubicacion", False),
        "es_archivo": received_payload.get("es_archivo", False),
        "action": received_payload.get("action"),
        "intencion": None, # La intención se clasificará o se recuperará de la memoria
    }

    # Lógica para cambio de dueño de sesión (para limpiar historial si cambia de pyme)
    if owner_user:
        last_id = flask_session.get(LAST_OWNER_SESION)
        if last_id != owner_user.id:
            flask_session[LAST_OWNER_SESION] = owner_user.id
            flask_session[NOMBRE_HISTORIAL_SESION] = []
            flask_session[CONTEXTO_PYME_SESION] = {}
            context["contexto_pyme"] = {} # Reiniciar contexto si cambia de dueño
            context["mensajes_previos"] = []
    else:
        flask_session.pop(LAST_OWNER_SESION, None)


    # --- INICIO DE CADENA DE HANDLERS ---
    # TODAS LAS CLASES DE HANDLERS DEBEN ESTAR DEFINIDAS ARRIBA DE ESTE PUNTO
    handler_chain = [
        # Prioridad alta para interrupciones y small talk
        LimitHandler, # Chequea límite de preguntas primero
        CancelHandler, # Cancelar cualquier flujo
        GreetingHandler, # Saludos
        SmallTalkHandler, # Conversación casual
        SentimentHandler, # Análisis de sentimiento
        
        # Handlers de flujos específicos (si hay un estado de conversación activo)
        FollowUpHandler, # Guía al usuario en flujos activos (pedido, reclamo, etc.)
        PedidoHandler, # Gestión de creación de pedidos (iniciar, agregar items, confirmar)
        TicketStatusHandler, # Consulta de estado de pedidos/tickets
        ClaimHandler, # Gestión de reclamos
        
        # Clasificación de intención general para enrutar a herramientas o flujos
        IntentClassifierPymeHandler, 
        
        # Herramientas y búsqueda de catálogo
        ToolHandlerPyme, # Herramientas simples (stock, horario, etc.)
        VectorCatalogHandler, # Búsqueda y muestra de catálogo por búsqueda vectorial
        
        # Lógicas de venta y marketing (engagement)
        SalesEngageHandler, # Sugerencias de venta
        CrossSellHandler, # Venta cruzada
        RecomendacionHandler, # Recomendaciones de productos
        UpsellHandler, # Sugerencias de upgrade
        OfertaPersonalizadaHandler, # Ofertas a clientes
        EnvioResumenHandler, # Envío de resumen de pedido
        PostVentaHandler, # Post-venta (encuestas, feedback)

        # Fallback de IA generativa y enganches
        LLMHandler, # Preguntas generales (contexto web de la PYME)
        IntentHandler, # Manejo de intenciones generales sin lógica de flujo compleja
        EngancheAnonimoHandler, # Enganche para usuarios anónimos al final
    ]

    respuesta_final = None
    
    # Intenta clasificar la intención principal ANTES de pasar por la cadena de handlers
    # Esto permite que los handlers internos confíen en `context['intencion']`
    if not context["contexto_pyme"].get("estado_conversacion"): # Solo clasifica si no hay un flujo activo
        # Si hay un action desde el frontend, úsalo como intención
        if context.get("action"):
            context["intencion"] = context["action"]
            logger.info(f"[INTENT] Intención inicial: '{context['intencion']}' (desde action)")
        else:
            # Si no hay action, usa el LLM para clasificar
            intencion_clasificada = _clasificar_intencion_pyme_con_llm(pregunta_str)
            context["intencion"] = intencion_clasificada
            logger.info(f"[INTENT] Intención inicial: '{context['intencion']}' (clasificada por LLM)")

    # Iterar sobre la cadena de handlers
    for handler_class in handler_chain:
        handler_instance = handler_class(context)
        
        # Especial: Los handlers de interrupción o saludo siempre se evalúan primero
        if handler_class in [CancelHandler, GreetingHandler, SmallTalkHandler, SentimentHandler, LimitHandler]:
            respuesta_parcial = handler_instance.handle(received_payload)
            if respuesta_parcial:
                respuesta_final = respuesta_parcial
                break 
            continue 

        # Lógica para que solo el handler del estado activo (o que coincida con la intención) responda
        current_state_in_context = context["contexto_pyme"].get("estado_conversacion")
        current_intencion = context.get("intencion")

        is_owner_of_flow = False
        if current_state_in_context:
            # Si hay un estado activo, el handler debe ser el dueño de ese estado para procesar
            if isinstance(handler_instance, FollowUpHandler) and current_state_in_context in [
                PymeConversationState.ESPERANDO_DETALLES_PEDIDO, PymeConversationState.CONFIRMANDO_PEDIDO_TEMP,
                PymeConversationState.CONFIRMANDO_PEDIDO_FINAL_PASO_2, PymeConversationState.ESPERANDO_NUMERO_PEDIDO,
                PymeConversationState.ESPERANDO_NUMERO_TICKET, PymeConversationState.ESPERANDO_DETALLES_RECLAMO,
                PymeConversationState.ESPERANDO_ADJUNTOS_RECLAMO_PYME, PymeConversationState.ESPERANDO_CONFIRMACION_RECLAMO_PYME,
                PymeConversationState.ESPERANDO_CIUDAD_ENVIO, PymeConversationState.ESPERANDO_NOMBRE_PRODUCTO_STOCK # Añadidos para FollowUp si cubre esos estados
            ]:
                is_owner_of_flow = True
            elif isinstance(handler_instance, PedidoHandler) and current_state_in_context in [
                PymeConversationState.ESPERANDO_DETALLES_PEDIDO, PymeConversationState.CONFIRMANDO_PEDIDO_TEMP, PymeConversationState.CONFIRMANDO_PEDIDO_FINAL_PASO_2
            ]:
                is_owner_of_flow = True
            elif isinstance(handler_instance, ClaimHandler) and current_state_in_context in [
                PymeConversationState.ESPERANDO_DETALLES_RECLAMO, PymeConversationState.ESPERANDO_ADJUNTOS_RECLAMO_PYME, PymeConversationState.ESPERANDO_CONFIRMACION_RECLAMO_PYME
            ]:
                is_owner_of_flow = True
            elif isinstance(handler_instance, TicketStatusHandler) and current_state_in_context in [
                PymeConversationState.ESPERANDO_NUMERO_PEDIDO, PymeConversationState.ESPERANDO_NUMERO_TICKET
            ]:
                is_owner_of_flow = True
            elif isinstance(handler_instance, ToolHandlerPyme) and current_state_in_context == PymeConversationState.ESPERANDO_NOMBRE_PRODUCTO_STOCK:
                is_owner_of_flow = True
            # Añadir otras condiciones si hay handlers que "poseen" un estado específico

        elif not current_state_in_context: # Si no hay un flujo activo, el handler puede ser el que inicia un flujo basado en la intención
            if (isinstance(handler_instance, PedidoHandler) and current_intencion == 'iniciar_pedido') or \
               (isinstance(handler_instance, VectorCatalogHandler) and (current_intencion == 'general_pyme' or payload.get("action") == "ver_catalogo" )) or \
               (isinstance(handler_instance, HumanEscalationPymeHandler) and current_intencion == 'hablar_con_agente_pyme') or \
               (isinstance(handler_instance, TicketStatusHandler) and current_intencion in ['consultar_estado_pedido', 'consultar_estado_ticket']) or \
               (isinstance(handler_instance, ToolHandlerPyme) and current_intencion == 'consultar_stock') or \
               (isinstance(handler_instance, ClaimHandler) and current_intencion == 'iniciar_reclamo_pyme') or \
               (isinstance(handler_instance, SalesEngageHandler) and current_intencion in ['recomendar', 'ofertas', 'add_to_cart']) or \
               (isinstance(handler_instance, CrossSellHandler) and current_intencion == 'cross_sell') or \
               (isinstance(handler_instance, RecomendacionHandler) and current_intencion == 'recomendar') or \
               (isinstance(handler_instance, UpsellHandler) and current_intencion == 'upsell') or \
               (isinstance(handler_instance, OfertaPersonalizadaHandler) and current_intencion == 'ofertas') or \
               (isinstance(handler_instance, EnvioResumenHandler) and current_intencion in ['enviar_whatsapp', 'enviar_email', 'enviar_sms', 'link_pago']) or \
               (isinstance(handler_instance, PostVentaHandler) and current_intencion == 'post_venta') or \
               (isinstance(handler_instance, FaqHandler) and current_intencion == 'faq') or \
               (isinstance(handler_instance, LLMHandler) and current_intencion == 'general_pyme') or \
               (isinstance(handler_instance, IntentHandler) and current_intencion in ['consultar_horario', 'consultar_ubicacion']):
                is_owner_of_flow = True

        if is_owner_of_flow:
            logger.info(f"[HANDLER] Procesando con handler de flujo/intención: {handler_class.__name__} (Estado: {current_state_in_context.name if current_state_in_context else 'None'}, Intención: {current_intencion})")
            respuesta_parcial = handler_instance.handle(received_payload)
            if respuesta_parcial:
                respuesta_final = respuesta_parcial
                break
            else:
                logger.warning(f"[HANDLER] Handler {handler_class.__name__} (flujo/intención) no generó respuesta válida.")
                if current_state_in_context and not (payload.get("es_foto") or payload.get("es_ubicacion") or payload.get("action")):
                    if es_pregunta_nueva(pregunta_str, "el dato solicitado"):
                        logger.info("[GUARDIAN] Detectada PREGUNTA_NUEVA. Limpiando estado y re-evaluando intención.")
                        context["contexto_pyme"].clear()
                        context["intencion"] = None
                        respuesta_final = None 
                        break
                continue 

        logger.info(f"[HANDLER] Saltando {handler_class.__name__} (no es dueño del flujo/intención actual).")


    if not respuesta_final:
        respuesta_final = {
            "respuesta": "Disculpa, no pude procesar tu solicitud. Por favor, intenta de nuevo o consulta nuestro catálogo.",
            "fuente": "error_no_handler",
            "estado_respuesta": "error_critico",
            "botones": [
                {"texto": "Ver catálogo", "action": "ver_catalogo"},
                {"texto": "Hablar con un agente", "action": "escalar"},
            ]
        }
    
    # Guardar historial de conversación
    historial = flask_session.get(NOMBRE_HISTORIAL_SESION, [])
    historial.append({"role": "user", "content": pregunta_str}) 
    asistente_content = str(respuesta_final.get('respuesta',''))
    historial.append({"role": "assistant", "content": asistente_content})
    flask_session[NOMBRE_HISTORIAL_SESION] = historial[-MAX_HISTORIAL_CHAT:]

    # Guardar conversación en DB (solo si hay user_id)
    try:
        if context['user_id']:
            db.session.add(Conversacion(
                user_id=context['user_id'], 
                pregunta=pregunta_str, 
                respuesta=respuesta_final.get('respuesta', ''),
                fuente=respuesta_final.get('fuente', 'desconocida'),
                rubro=context['rubro_nombre']
            ))
            db.session.commit()
    except Exception as e:
        logging.error(f"[PYMES] Error guardando conversación en DB: {e}", exc_info=True)
        db.session.rollback()

    # Devolver respuesta final al frontend
    return {
        "respuesta": respuesta_final.get('respuesta', "Error: respuesta mal formada."),
        "fuente": respuesta_final.get('fuente', 'desconocida'),
        "contexto_actualizado": {CONTEXTO_PYME_SESION: contexto_pyme}, 
        "estado_respuesta": respuesta_final.get('estado_respuesta', 'no_entendido'),
        "pedido_data": respuesta_final.get('pedido_data', None), 
        "botones": respuesta_final.get('botones', []),
        "media_url": context.get('foto_url'), 
        "location_data": context.get('ubicacion_usuario') 
    }