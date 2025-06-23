import logging
import re
import random
import json
import difflib
from datetime import datetime
from enum import Enum, auto
from flask import session as flask_session

# --- Importaciones ---
# MODIFICACIÓN 1: Agregamos SitioWebInfo a la lista de importaciones de modelos
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
from services.herramientas_municipio import normalizar_texto
import unicodedata
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


def serialize_state(state: PymeConversationState | None) -> str | None:
    return state.name if state else None


def deserialize_state(value: str | None) -> PymeConversationState | None:
    if not value:
        return None
    try:
        return PymeConversationState[value]
    except KeyError:
        return None


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

# MODIFICACIÓN 2: Definimos el nuevo prompt inteligente para Pymes, que usará el contexto de la DB
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

# --- PROMPT PARA CLASIFICACIÓN DE INTENCIÓN DE PYME ---
PROMPT_CLASIFICACION_INTENCION_PYME = """
Analiza la siguiente PREGUNTA DEL USUARIO y clasifica su INTENCIÓN en el contexto de una PYME.
Si la pregunta no encaja en ninguna de las categorías, clasifícala como 'general_pyme'.

INTENCIONES POSIBLES:
- iniciar_pedido: El usuario quiere hacer un pedido, solicitar un producto, cotización, o información para compra. (ej. "quiero pedir 5 cajas de vino", "cotización de este producto", "cómo compro", "quiero encargar")
- consultar_estado_pedido: El usuario quiere saber el estado de un pedido existente. (ej. "estado de mi reclamo", "cómo va mi ticket 12345")
- consultar_stock: El usuario pregunta sobre la disponibilidad de un producto o stock.
- consultar_horario: El usuario pregunta sobre horarios de atención.
- consultar_ubicacion: El usuario pregunta por la dirección física.
- hablar_con_agente_pyme: El usuario quiere hablar con una persona de la empresa (ej. "necesito hablar con alguien", "quiero hablar con una persona", "pasame con un humano", "me pasas con un operador", "quiero un representante")
- general_pyme: Cualquier otra consulta que no encaje en las anteriores.

PREGUNTA DEL USUARIO: "{pregunta_usuario}"

Tu respuesta debe ser SÓLO una de las INTENCIONES POSIBLES.
"""

# --- Función para clasificar intención específica de Pyme ---
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

# --- Utilidad para decidir herramientas con LLM ---
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

# --- Funciones Auxiliares (sin cambios) ---
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
    Tu tarea es analizar la respuesta de un cliente y extraer los productos y cantidades que solicita, basándote en la lista de PRODUCTOS DISPONIBLES.
    Si el cliente menciona "cada variedad" o "todos los que me mostraste", debes incluir TODOS los productos de la lista de PRODUCTOS DISPONIBLES con la cantidad especificada.
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
                cantidad = item_llam.get('cantidad', 1)
                unidad = item_llam.get('unidad', 'unidad')

                matched_product = None
                for p_raw in productos_disponibles_raw:
                    nombre_completo = p_raw.get('nombre', '')
                    sku = p_raw.get('sku', '')
                    if identificador == nombre_completo or identificador == sku:
                        matched_product = p_raw
                        break
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
                respuesta += f"  _{desc[:60]}_\n"
        if len(items_ordenados) > max_por_categoria:
            respuesta += f"  ...y {len(items_ordenados) - max_por_categoria} más en esta categoría.\n"
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

# --- ARQUITECTURA DE HANDLERS (sin cambios en la mayoría) ---

class BaseHandler:
    def __init__(self, context):
        self.context = context
    def handle(self, pregunta: str) -> dict | None:
        raise NotImplementedError

    # -- utilidades de coincidencia robusta --
    def _normalize(self, text: str) -> str:
        if not isinstance(text, str):
            return ""
        return normalizar_texto(text)

    def _has_keyword(self, text: str, keywords: list[str]) -> bool:
        texto_norm = self._normalize(text)
        return any(kw in texto_norm for kw in keywords)


class GreetingHandler(BaseHandler):
    def handle(self, pregunta: str) -> dict | None:
        def _normalize_for_greeting(text: str) -> str:
            text = "".join(
                c for c in unicodedata.normalize("NFD", text) if not unicodedata.combining(c)
            )
            text = text.lower()
            text = re.sub(r"[!.,?]", "", text)
            text = re.sub(r"(\w)\1+", r"\1", text)
            return re.sub(r"\s+", " ", text).strip()

        texto = _normalize_for_greeting(pregunta)
        saludos = [
            "hola",
            "buenos dias",
            "buenas tardes",
            "buenas noches",
            "hey",
            "que tal",
            "buenas",
            "buen dia",
            "hola hola",
            "hola que tal",
            "saludos",
        ]
        tokens = texto.split()
        set_saludo = {
            "hola",
            "buenos",
            "dias",
            "buenas",
            "tardes",
            "noches",
            "hey",
            "que",
            "tal",
            "saludos",
            "dia",
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
            nombre = getattr(self.context.get('user_obj'), 'nombre_empresa', None)
            if nombre:
                return {
                    "respuesta": f"¡Hola! Soy Chatboc, tu asistente para {nombre}. ¿En qué puedo ayudarte hoy? ¿Buscás algún producto o necesitás asesoramiento?",
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

class SmallTalkHandler(BaseHandler):
    """Detecta small talk con LLM y responde de forma cordial."""

    def handle(self, pregunta: str) -> dict | None:
        if detectar_small_talk_con_llm(pregunta):
            respuesta = generar_respuesta_small_talk(pregunta)
            return {
                "respuesta": respuesta,
                "fuente": "smalltalk_pyme_llm",
            }
        return None

PROMPT_ANALISIS_SENTIMIENTO = """
Analiza la FRASE y respondé solo 'positivo', 'negativo' o 'neutro'.

FRASE: "{frase}"
"""


def analizar_sentimiento_con_llm(frase: str) -> str:
    try:
        decision = get_cohere_response(
            message=PROMPT_ANALISIS_SENTIMIENTO.format(frase=frase),
            preamble="Sos un analizador de sentimiento. Respondé solo con positivo, negativo o neutro.",
        )
        return decision.strip().lower()
    except Exception as e:
        logger.error(f"[SENTIMIENTO] Error analizando: {e}")
        return "neutro"


class SentimentHandler(BaseHandler):
    NEGATIVE_KEYWORDS = [
        "pesimo", "pésimo", "horrible", "desastre", "engaño", "estafa", "odio", "malisimo", "malo",
    ]
    POSITIVE_KEYWORDS = [
        "excelente", "buen servicio", "muy bueno", "genial", "gracias", "felicitaciones",
    ]

    def handle(self, pregunta: str) -> dict | None:
        texto = self._normalize(pregunta)
        if any(kw in texto for kw in self.POSITIVE_KEYWORDS):
            sentimiento = "positivo"
        elif any(kw in texto for kw in self.NEGATIVE_KEYWORDS):
            sentimiento = "negativo"
        else:
            sentimiento = analizar_sentimiento_con_llm(pregunta)

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
    def handle(self, pregunta: str) -> dict | None:
        limite = self.context.get('limite_preguntas')
        if limite is not None and self.context.get('preguntas_usadas', 0) >= limite:
            return {
                "respuesta": "🔒 Límite de preguntas alcanzado. Actualizá tu plan para continuar.",
                "fuente": "sistema_limite",
                "estado_respuesta": "limite_alcanzado",
            }
        return None

# --- HANDLERS FALTANTES: PLANTILLAS INTELIGENTES ---

class FaqHandler(BaseHandler):
    def handle(self, pregunta: str) -> dict | None:
        respuesta = buscar_en_faq_spacy(pregunta, self.context.get('user_id'))
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
    def handle(self, pregunta: str) -> dict | None:
        if "stock" in pregunta.lower():
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
    def handle(self, pregunta: str) -> dict | None:
        if "agente" in pregunta.lower() or "humano" in pregunta.lower():
            return {
                "respuesta": "Te comunico con un agente humano. Por favor, aguardá un momento.",
                "fuente": "escalamiento_humano_pyme",
                "botones": [
                    {"texto": "Cancelar", "action": "cancelar"},
                ]
            }
        return None

class LLMHandler(BaseHandler):
    """Fallback de IA generativa para consultas generales."""
    def handle(self, pregunta: str) -> dict | None:
        respuesta = get_cohere_response(message=pregunta)
        if respuesta:
            return {
                "respuesta": respuesta,
                "fuente": "llm_pyme",
            }
        return None

class IntentHandler(BaseHandler):
    """Maneja intenciones específicas no cubiertas por otros handlers."""
    def handle(self, pregunta: str) -> dict | None:
        # Ejemplo: si la intención es 'consultar_horario'
        if self.context.get('intencion') == 'consultar_horario':
            return {
                "respuesta": "Nuestro horario de atención es de lunes a viernes de 9 a 18 hs.",
                "fuente": "intencion_horario",
            }
        return None

class EngancheAnonimoHandler(BaseHandler):
    def handle(self, pregunta: str) -> dict | None:
        if not self.context.get('user_id'):
            return {
                "respuesta": "Para ver precios exclusivos y hacer pedidos, por favor registrate o ingresá tus datos.",
                "fuente": "enganche_anonimo",
                "botones": [
                    {"texto": "Registrarme", "action": "registrar"},
                    {"texto": "Ver catálogo", "action": "ver_catalogo"},
                ]
            }
        return None

class SalesEngageHandler(BaseHandler):
    """Ofrece sugerencias comerciales inteligentes y personalizadas."""
    def handle(self, pregunta: str) -> dict | None:
        sugerencias = sugerencias_por_rubro(self.context.get('rubro_nombre'))
        historial = self.context.get('mensajes_previos', [])
        ultima_interaccion = historial[-2]['content'] if len(historial) > 1 else ""
        if "producto" in ultima_interaccion.lower() or "catálogo" in ultima_interaccion.lower():
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
    def handle(self, pregunta: str) -> dict | None:
        productos = self.context['contexto_pyme'].get('productos_mostrados_catalogo', [])
        if productos:
            # Ejemplo: sugiere productos de otra categoría o los más vendidos
            sugeridos = [p for p in productos if p.get("destacado") or p.get("categoria") == "Accesorios"]
            if sugeridos:
                texto = "¿Te interesan también estos productos que suelen comprar otros clientes?"
                lista = "\n".join([f"- {p.get('nombre')} ({p.get('precio_str','Consultar')})" for p in sugeridos[:3]])
                return {
                    "respuesta": f"{texto}\n{lista}",
                    "fuente": "cross_sell_pyme",
                    "botones": [
                        {"texto": "Agregar al pedido", "action": "add_to_cart"},
                        {"texto": "Ver más accesorios", "action": "ver_mas"},
                    ]
                }
        return None

class OfertaPersonalizadaHandler(BaseHandler):
    def handle(self, pregunta: str) -> dict | None:
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
    def handle(self, pregunta: str) -> dict | None:
        texto = pregunta.lower()
        user = self.context.get('user_obj')
        pedido = self.context['contexto_pyme'].get('pedido_actual')  # Debes guardar el pedido en contexto cuando se genera

        # Simulación de generación de link de pago (puedes reemplazar por tu integración real)
        def generar_link_pago(pedido):
            if not pedido:
                return None
            # Aquí deberías integrar con tu pasarela de pagos real
            return f"https://pagos.tupyme.com/pagar?pedido={pedido.get('id', 'demo')}"

        # Detecta preferencia de canal
        if "whatsapp" in texto or "wasap" in texto:
            if user and user.telefono:
                try:
                    enviar_whatsapp(user.telefono, f"Resumen de tu pedido: {pedido}")
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

        if "email" in texto or "correo" in texto:
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

        if "sms" in texto or "mensaje" in texto:
            if user and user.telefono:
                try:
                    enviar_sms(user.telefono, f"Resumen de tu pedido: {pedido}")
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
        if "resumen" in texto or "pedido" in texto or "pagar" in texto:
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
    def handle(self, pregunta: str) -> dict | None:
        if any(kw in pregunta.lower() for kw in ["gracias", "recibí", "llegó mi pedido"]):
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

# --- FUNCIÓN ORQUESTADORA PRINCIPAL MEJORADA ---
def responder_pyme(pregunta, owner_user, rubro_obj, viewer_user=None, anon_id=None, **kwargs):
    contexto_previo = kwargs.get('contexto_previo', {})
    contexto_previo_valido = contexto_previo if contexto_previo is not None else {}

    if owner_user:
        last_id = flask_session.get(LAST_OWNER_SESION)
        if last_id != owner_user.id:
            flask_session[LAST_OWNER_SESION] = owner_user.id
            flask_session[NOMBRE_HISTORIAL_SESION] = []
            flask_session[CONTEXTO_PYME_SESION] = {}
            contexto_previo_valido = {}
    else:
        flask_session.pop(LAST_OWNER_SESION, None)

    contexto_pyme = contexto_previo_valido.get(CONTEXTO_PYME_SESION, {})

    context = {
        "contexto_pyme": contexto_pyme,
        "user_obj": owner_user,
        "rubro_obj": rubro_obj,
        "user_id": getattr(owner_user, "id", None),
        "cliente_id": getattr(viewer_user, "id", None),
        "anon_id": anon_id,
        "nombre_pyme": getattr(owner_user, "nombre_empresa", "la empresa") if owner_user else "la empresa",
        "telefono": getattr(owner_user, "telefono", "") if owner_user else "",
        "direccion": getattr(owner_user, "direccion", "") if owner_user else "",
        "email": getattr(owner_user, "email", "") if owner_user else "",
        "plan": getattr(owner_user, "plan", "anonimo") if owner_user else "anonimo",
        "preguntas_usadas": getattr(owner_user, "preguntas_usadas", 0) if owner_user else 0,
        "limite_preguntas": limite_para_usuario(owner_user) if owner_user else None,
        "rubro_nombre": getattr(rubro_obj, "nombre", "empresa").lower() if rubro_obj else "desconocido",
        "mensajes_previos": flask_session.get(NOMBRE_HISTORIAL_SESION, [])
    }

    handler_chain = [
        LimitHandler,
        GreetingHandler,
        SmallTalkHandler,
        SentimentHandler,
        FollowUpHandler,
        IntentClassifierPymeHandler,
        VectorCatalogHandler,
        PedidoHandler,
        BrokenProductHandler,
        ClaimHandler,
        TicketStatusHandler,
        FaqHandler,
        ToolHandlerPyme,
        HumanEscalationPymeHandler,
        SalesEngageHandler,
        CrossSellHandler,
        RecomendacionHandler,   # <--- nuevo
        UpsellHandler,          # <--- nuevo
        PostVentaHandler,       # <--- nuevo
        LLMHandler,
        IntentHandler,
        EngancheAnonimoHandler,
    ]

    respuesta_final = None
    for handler_class in handler_chain:
        handler_instance = handler_class(context)
        respuesta_final = handler_instance.handle(pregunta)
        if respuesta_final:
            if not isinstance(respuesta_final, dict):
                logging.error(f"[HANDLER_ERROR] Handler '{handler_class.__name__}' devolvió tipo incorrecto: {type(respuesta_final)}")
                respuesta_final = None
            else:
                break

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

    historial = flask_session.get(NOMBRE_HISTORIAL_SESION, [])
    historial.append({"role": "user", "content": pregunta})
    asistente_content = str(respuesta_final.get('respuesta',''))
    historial.append({"role": "assistant", "content": asistente_content})
    flask_session[NOMBRE_HISTORIAL_SESION] = historial[-MAX_HISTORIAL_CHAT:]

    try:
        if context['user_id']:
            db.session.add(Conversacion(
                user_id=context['user_id'], pregunta=pregunta,
                respuesta=respuesta_final.get('respuesta', ''),
                fuente=respuesta_final.get('fuente', 'desconocida'),
                rubro=context['rubro_nombre']
            ))
            db.session.commit()
    except Exception as e:
        logging.error(f"[PYMES] Error guardando conversación en DB: {e}", exc_info=True)
        db.session.rollback()

    return {
        "respuesta": respuesta_final.get('respuesta', "Error: respuesta mal formada."),
        "fuente": respuesta_final.get('fuente', 'desconocida'),
        "contexto_actualizado": {CONTEXTO_PYME_SESION: contexto_pyme},
        "estado_respuesta": respuesta_final.get('estado_respuesta', 'no_entendido'),
        "pedido_data": respuesta_final.get('pedido_data', None),
        "botones": respuesta_final.get('botones', []),
    }

class VectorCatalogHandler(BaseHandler):
    """
    Muestra productos agrupados, ordenados y con botones de acción para maximizar ventas.
    Usa contexto, historial y preferencias si están disponibles.
    """
    def handle(self, pregunta: str) -> dict | None:
        user_id = self.context.get('user_id')
        if not user_id:
            return None

        contexto_pyme = self.context.get('contexto_pyme', {})
        estado = deserialize_state(contexto_pyme.get('estado_conversacion'))
        if self.context.get('intencion') == 'iniciar_pedido' or estado in {
            PymeConversationState.ESPERANDO_DETALLES_PEDIDO,
            PymeConversationState.CONFIRMANDO_PEDIDO_TEMP,
            PymeConversationState.CONFIRMANDO_PEDIDO_FINAL_PASO_2,
        }:
            return None

        resultados = buscar_catalogo_qdrant(
            user_id=user_id,
            pregunta=pregunta,
            limite=DEFAULT_SEARCH_LIMIT,
            categoria=self.context.get('rubro_nombre'),
        )
        productos_mostrados = []
        if resultados:
            for hit in resultados:
                if hasattr(hit, 'payload') and isinstance(hit.payload, dict):
                    productos_mostrados.append(hit.payload)

        if productos_mostrados:
            # Ordena primero por relevancia (si existe), luego por precio
            productos_mostrados.sort(key=lambda p: (p.get("relevancia", 0), float(p.get("precio_float") or p.get("precio") or 0)), reverse=True)
            self.context['contexto_pyme']['productos_mostrados_catalogo'] = productos_mostrados

            # Agrupa por categoría si hay muchas opciones
            categorias = {}
            for p in productos_mostrados:
                cat = p.get("categoria_qdrant") or p.get("categoria") or "Otros"
                categorias.setdefault(cat, []).append(p)

            respuesta = "Estos son los productos más relevantes que encontré para vos:\n"
            for categoria, items in categorias.items():
                respuesta += f"\n🗂️ **{categoria.title()}**\n"
                for idx, p in enumerate(items[:3], 1):
                    nombre = p.get("nombre", "Producto")
                    precio = p.get("precio_str") or f"${p.get('precio', 'Consultar')}"
                    desc = p.get("descripcion", "")
                    respuesta += f"{idx}. **{nombre}** — {precio}\n"
                    if desc:
                        respuesta += f"    _{desc[:60]}_\n"
                if len(items) > 3:
                    respuesta += f"...y {len(items)-3} más en esta categoría.\n"

            respuesta += "\n¿Te gustaría agregar alguno al pedido? Decime el número o nombre del producto y la cantidad. Si querés ver más opciones, decímelo o pedí ayuda."

            return {
                'respuesta': respuesta,
                'fuente': 'catalogo_vector',
                'estado_respuesta': 'mostrar_catalogo',
                'botones': [
                    {"texto": "Agregar al pedido", "action": "add_to_cart"},
                    {"texto": "Ver más productos", "action": "ver_mas"},
                    {"texto": "Consultar stock", "action": "consultar_stock"},
                    {"texto": "Hablar con un agente", "action": "escalar"},
                ]
            }

        return None

class PedidoHandler(BaseHandler):
    def handle(self, pregunta: str) -> dict | None:
        if self.context.get('intencion') == 'iniciar_pedido':
            productos = self.context['contexto_pyme'].get('productos_mostrados_catalogo', [])
            seleccionados = []
            for p in productos:
                nombre = p.get("nombre", "").lower()
                sku = p.get("sku", "").lower()
                if nombre in pregunta.lower() or sku in pregunta.lower():
                    seleccionados.append(p)
            if seleccionados:
                nombres = ", ".join([p.get("nombre") for p in seleccionados])
                return {
                    "respuesta": f"¡Genial! Seleccionaste: {nombres}. ¿Cuántas unidades querés de cada uno? Decime el número o escribí '1' si es solo uno. Cuando termines, podés finalizar el pedido o agregar más productos.",
                    "fuente": "pedido_pyme_seleccion",
                    "botones": [
                        {"texto": "Agregar más productos", "action": "ver_catalogo"},
                        {"texto": "Finalizar pedido", "action": "finalizar_pedido"},
                        {"texto": "Hablar con un agente", "action": "escalar"},
                    ]
                }
            if productos:
                lista = "\n".join([f"{idx+1}. {p.get('nombre')}" for idx, p in enumerate(productos[:7])])
                return {
                    "respuesta": f"¿Qué producto y cantidad te gustaría pedir? Elegí de los siguientes o decime el nombre/código:\n{lista}",
                    "fuente": "pedido_pyme",
                    "botones": [
                        {"texto": "Ver catálogo", "action": "ver_catalogo"},
                        {"texto": "Hablar con un agente", "action": "escalar"},
                    ]
                }
            return {
                "respuesta": "¿Qué producto te gustaría pedir? Puedes ver nuestro catálogo para elegir.",
                "fuente": "pedido_pyme",
                "botones": [
                    {"texto": "Ver catálogo", "action": "ver_catalogo"},
                    {"texto": "Hablar con un agente", "action": "escalar"},
                ]
            }
        return None

class BrokenProductHandler(BaseHandler):
    def handle(self, pregunta: str) -> dict | None:
        if self.context.get('intencion') == 'consultar_estado_pedido':
            return {
                "respuesta": "Para consultar el estado de tu pedido, por favor proporcioname el número de pedido.",
                "fuente": "estado_pedido_pyme",
                "botones": [
                    {"texto": "Consultar estado", "action": "consultar_estado_pedido"},
                    {"texto": "Hablar con un agente", "action": "escalar"},
                ]
            }
        return None

class ClaimHandler(BaseHandler):
    def handle(self, pregunta: str) -> dict | None:
        if self.context.get('intencion') == 'consultar_estado_pedido':
            return {
                "respuesta": "Si tenés un reclamo, por favor indicame el número de ticket o pedido relacionado.",
                "fuente": "reclamo_pyme",
                "botones": [
                    {"texto": "Consultar reclamo", "action": "consultar_reclamo"},
                    {"texto": "Hablar con un agente", "action": "escalar"},
                ]
            }
        return None

class TicketStatusHandler(BaseHandler):
    def handle(self, pregunta: str) -> dict | None:
        if self.context.get('intencion') == 'consultar_estado_pedido':
            return {
                "respuesta": "Para verificar el estado de tu ticket, por favor proporcioname el número de ticket.",
                "fuente": "estado_ticket_pyme",
                "botones": [
                    {"texto": "Consultar estado de ticket", "action": "consultar_estado_ticket"},
                    {"texto": "Hablar con un agente", "action": "escalar"},
                ]
            }
        return None

class FollowUpHandler(BaseHandler):
    """
    Handler de seguimiento inteligente: detecta si el usuario está en medio de un flujo (pedido, reclamo, etc.)
    y lo guía para concretar la acción, cerrar ventas o resolver dudas, evitando que abandone el proceso.
    """
    def handle(self, pregunta: str) -> dict | None:
        contexto_pyme = self.context.get('contexto_pyme', {})
        estado = deserialize_state(contexto_pyme.get('estado_conversacion'))

        # Si el usuario está confirmando un pedido
        if estado == PymeConversationState.CONFIRMANDO_PEDIDO_TEMP:
            if "cancelar" in pregunta.lower() or "no quiero" in pregunta.lower():
                contexto_pyme.clear()
                return {
                    "respuesta": "Entendido, no se generará el pedido. ¿Te gustaría ver otros productos o recibir asesoramiento?",
                    "fuente": "pedido_cancelado",
                    "botones": [
                        {"texto": "Ver catálogo", "action": "ver_catalogo"},
                        {"texto": "Hablar con un agente", "action": "escalar"},
                    ]
                }
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
            return {
                "respuesta": "¿Qué producto y cantidad te gustaría pedir? Si necesitas ayuda, puedo recomendarte los más vendidos.",
                "fuente": "seguimiento_detalles_pedido",
                "botones": [
                    {"texto": "Ver productos recomendados", "action": "recomendar"},
                    {"texto": "Ver catálogo", "action": "ver_catalogo"},
                    {"texto": "Hablar con un agente", "action": "escalar"},
                ]
            }

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

        # Si no hay flujo activo, no responde
        return None

class IntentClassifierPymeHandler(BaseHandler):
    """
    Clasifica la intención del usuario usando LLM y contexto, para enrutar la consulta al handler adecuado.
    Potenciado para ventas, reclamos, consultas y acciones comerciales.
    """
    def handle(self, pregunta: str) -> dict | None:
        contexto_pyme = self.context.get('contexto_pyme', {})
        memoria = contexto_pyme
        texto_normalizado = normalizar_texto(pregunta)

        # Palabras clave para agente humano
        KEYWORDS_AGENTE = [
            "agente", "humano", "persona", "representante", "operador", "hablar con alguien", "atención humana"
        ]
        if any(kw in texto_normalizado for kw in KEYWORDS_AGENTE):
            self.context["intencion"] = "hablar_con_agente_pyme"
            memoria.clear()
            logger.info(f"[PYME] Intención: hablar_con_agente_pyme (por palabra clave)")
            return None

        # Palabras clave para pedido
        KEYWORDS_PEDIDO = [
            "comprar", "pedido", "cotización", "encargar", "quiero pedir", "quiero comprar", "ordenar", "solicitar"
        ]
        if any(kw in texto_normalizado for kw in KEYWORDS_PEDIDO):
            self.context["intencion"] = "iniciar_pedido"
            memoria.clear()
            logger.info(f"[PYME] Intención: iniciar_pedido (por palabra clave)")
            return None

        # Palabras clave para estado de pedido/ticket
        KEYWORDS_ESTADO = [
            "estado", "seguimiento", "dónde está mi pedido", "cómo va mi pedido", "ticket", "reclamo"
        ]
        if any(kw in texto_normalizado for kw in KEYWORDS_ESTADO):
            self.context["intencion"] = "consultar_estado_pedido"
            memoria.clear()
            logger.info(f"[PYME] Intención: consultar_estado_pedido (por palabra clave)")
            return None

        # Palabras clave para stock
        KEYWORDS_STOCK = [
            "stock", "hay", "disponible", "queda", "tienen", "disponibilidad"
        ]
        if any(kw in texto_normalizado for kw in KEYWORDS_STOCK):
            self.context["intencion"] = "consultar_stock"
            memoria.clear()
            logger.info(f"[PYME] Intención: consultar_stock (por palabra clave)")
            return None

        # Palabras clave para horarios
        KEYWORDS_HORARIO = [
            "horario", "a qué hora", "cuándo abren", "cuándo cierran", "horarios"
        ]
        if any(kw in texto_normalizado for kw in KEYWORDS_HORARIO):
            self.context["intencion"] = "consultar_horario"
            memoria.clear()
            logger.info(f"[PYME] Intención: consultar_horario (por palabra clave)")
            return None

        # Palabras clave para ubicación
        KEYWORDS_UBICACION = [
            "dónde están", "dirección", "ubicación", "cómo llego", "dónde queda"
        ]
        if any(kw in texto_normalizado for kw in KEYWORDS_UBICACION):
            self.context["intencion"] = "consultar_ubicacion"
            memoria.clear()
            logger.info(f"[PYME] Intención: consultar_ubicacion (por palabra clave)")
            return None

        # Si no hubo match, usa LLM para clasificar intención
        if not memoria.get("estado_conversacion"):
            intencion_llm = _clasificar_intencion_pyme_con_llm(pregunta)
            self.context["intencion"] = intencion_llam
        else:
            self.context["intencion"] = "continuar_flujo"

        logger.info(f"[PYME] Intención (final): {self.context.get('intencion')}")
        return None

class RecomendacionHandler(BaseHandler):
    """Sugiere productos recomendados según historial, ticket promedio y catálogo."""
    def handle(self, pregunta: str) -> dict | None:
        productos = self.context['contexto_pyme'].get('productos_mostrados_catalogo', [])
        if not productos:
            return None
        # Ejemplo: sugiere los más vendidos o destacados
        recomendados = [p for p in productos if p.get("destacado")] or productos[:3]
        if recomendados:
            texto = "Te recomiendo estos productos que eligen otros clientes:"
            lista = "\n".join([f"- {p.get('nombre')} ({p.get('precio_str','Consultar')})" for p in recomendados])
            return {
                "respuesta": f"{texto}\n{lista}\n¿Te gustaría agregarlos al pedido o ver más detalles?",
                "fuente": "recomendacion_pyme",
                "botones": [
                    {"texto": "Agregar recomendados", "action": "add_to_cart"},
                    {"texto": "Ver más productos", "action": "ver_mas"},
                    {"texto": "Hablar con un agente", "action": "escalar"},
                ]
            }
        return None

class UpsellHandler(BaseHandler):
    """Sugiere versiones premium, packs o mayores cantidades para aumentar el ticket."""
    def handle(self, pregunta: str) -> dict | None:
        productos = self.context['contexto_pyme'].get('productos_mostrados_catalogo', [])
        if not productos:
            return None
        # Ejemplo: si el usuario pide un producto barato, sugiere el premium
        baratos = [p for p in productos if float(p.get("precio_float", 0)) < 5000]
        premium = [p for p in productos if float(p.get("precio_float", 0)) > 10000]
        if baratos and premium:
            texto = "¿Sabías que tenemos versiones premium o packs con mejor precio por unidad?"
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