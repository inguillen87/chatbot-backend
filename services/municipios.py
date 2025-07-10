import logging
import re
import json
import os
from enum import Enum, auto
import unicodedata
import difflib
from flask import current_app, has_app_context  # Ensure current_app is imported directly
from sqlalchemy.orm.attributes import flag_modified # Import for flag_modified
from models import MunicipioTicket, TicketComentario, db, SitioWebInfo, Conversacion # Added Conversacion
from services.cohere_ai import get_cohere_response
from services.ticket_service import servicio_tickets
from .logic import (
    _clasificar_intencion_con_llm,
    detectar_small_talk_con_llm,
    generar_respuesta_small_talk,
)
from twilio.rest import Client
from datetime import datetime, timedelta
from services.utils_placeholders import (
    reemplazar_placeholders,
    obtener_respuesta_municipio,
)
from services.config_loader import cargar_configuracion_municipio
from .herramientas_municipio import (
    consultar_recoleccion_por_direccion,
    categorizar_reclamo_por_palabra_clave,
    sugerir_categorias_relevantes,
    parse_direccion_completa,
    normalizar_texto,
    direccion_es_valida,
    TOOL_REGISTRY,
    KEYWORD_TO_CATEGORY_MAP,
)
from .common_utils import (
    validar_email,
    validar_telefono,
    formatear_telefono_e164,
    construir_respuesta_sugerir_registro
)
from .llm_utils import extract_complaint_details_llm, extract_multiple_contact_details_llm
import math

try:
    from flask import current_app, session as flask_session, has_app_context
except ImportError:
    current_app = None
    flask_session = {}

logger = logging.getLogger(__name__)

CONTEXTO_MUNICIPIO = "contexto_municipio_v2"

# Keywords for confirming actions, especially in the reclamo (complaint) flow
PALABRAS_CLAVE_CONFIRMACION = {
    "confirmar_reclamo", "confirmar", "confirmo", "confirmado",
    "si", "sí", "afirmativo", "dale", "ok", "proceder", "aceptar",
    "confirmar_reclamo_final", "si, confirmar reclamo", "sí, confirmar reclamo", # From button texts
    "yes" # English just in case
}

# Keywords for requesting to edit information during a flow
EDIT_KEYWORDS = {
    "editar", "cambiar", "corregir", "modificar", 
    "no era asi", "me equivoque", "error", "equivocado",
    "editar datos", "editar_reclamo_datos", "quiero editar", "necesito cambiar"
}

URL_REGEX = re.compile(r"https?://\S+")

def agregar_botones_para_links(texto: str, botones: list) -> list:
    if not texto:
        return botones
    urls = re.findall(URL_REGEX, texto)
    for url in urls:
        if not any(b.get("url") == url for b in botones):
            botones.append({"texto": "Abrir enlace", "url": url})
    return botones

TWILIO_ACCOUNT_SID = os.environ.get("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN")
TWILIO_PHONE_NUMBER = os.environ.get("TWILIO_PHONE_NUMBER")
TWILIO_WHATSAPP_NUMBER = "whatsapp:+14155238886"
TWILIO_WHATSAPP_CONTENT_SID = os.environ.get("TWILIO_WHATSAPP_CONTENT_SID")

MUNICIPIO_ID = os.environ.get("MUNICIPIO_ID", "default")
CONFIG_MUNICIPIO = cargar_configuracion_municipio(MUNICIPIO_ID, "config.json")

TODAS_LAS_CATEGORIAS_UNICAS = sorted(list(set(KEYWORD_TO_CATEGORY_MAP.values())))
BOTONES_TODAS_CATEGORIAS = [{"texto": cat} for cat in TODAS_LAS_CATEGORIAS_UNICAS]

MINI_FAQ_TRAMITES = cargar_configuracion_municipio(
    MUNICIPIO_ID, "mini_faq_tramites.json"
)

_TRAMITES_CACHE = None
_TRAMITES_MTIME = None

def cargar_tramites_info():
    global _TRAMITES_CACHE, _TRAMITES_MTIME
    ruta = os.path.join(
        os.path.dirname(__file__), "..", "data", "municipios", MUNICIPIO_ID, "tramites.json"
    )
    try:
        mtime = os.path.getmtime(ruta)
    except OSError as e:
        logger.error(f"[TRAMITES] No se pudo acceder a {ruta}: {e}")
        _TRAMITES_CACHE = {}
        _TRAMITES_MTIME = None
        return _TRAMITES_CACHE
    if _TRAMITES_CACHE is None or _TRAMITES_MTIME != mtime:
        try:
            with open(ruta, "r", encoding="utf-8") as f:
                _TRAMITES_CACHE = json.load(f)
            logger.info(f"✅ Trámites cargados desde {ruta}")
            _TRAMITES_MTIME = mtime
        except Exception as e:
            logger.error(f"[TRAMITES] No se pudo cargar {ruta}: {e}", exc_info=True)
            _TRAMITES_CACHE = {}
            _TRAMITES_MTIME = mtime
    return _TRAMITES_CACHE

def get_tramites_info() -> dict:
    return cargar_tramites_info()

DEFAULT_TRAMITES_WEB_URL = CONFIG_MUNICIPIO.get(
    "tramites_web_url", "https://www.ejemplo.gob.ar/tramites/"
)
MUNICIPIO_DIRECCION = CONFIG_MUNICIPIO.get("direccion", "Dirección del municipio")
EJEMPLO_DIRECCION = CONFIG_MUNICIPIO.get("ejemplo_direccion", "Avenida Siempreviva 123")

class ConversationState(Enum):
    ESPERANDO_CONFIRMACION_CIERRE = auto()
    ESPERANDO_CALIFICACION = auto()
    ESPERANDO_NUMERO_TICKET = auto()
    ESPERANDO_PARAM_RECOLECCION = auto()
    ESPERANDO_CATEGORIA_RECLAMO = auto()
    ESPERANDO_DIRECCION_RECLAMO = auto()
    ESPERANDO_NOMBRE_VECINO = auto()
    ESPERANDO_TELEFONO_VECINO = auto()
    ESPERANDO_EMAIL_VECINO = auto()
    ESPERANDO_DESCRIPCION_RECLAMO = auto()
    ESPERANDO_ADJUNTOS_RECLAMO = auto()
    ESPERANDO_CONFIRMACION_RECLAMO = auto()
    ESPERANDO_SELECCION_TRAMITE = auto()
    ESPERANDO_PREGUNTA_CURSO_LICENCIA = auto()
    ESPERANDO_TEXTO_SUGERENCIA = auto()
    ESPERANDO_PRODUCTO_PARA_CONSULTA = auto()
    MOSTRANDO_PRODUCTOS = auto()
    ESPERANDO_CONFIRMACION_AGREGAR_CARRITO = auto()
    ESPERANDO_OPCION_CARRITO = auto()
    ESPERANDO_DETALLES_CHECKOUT = auto()
    ESPERANDO_CONFIRMACION_PEDIDO = auto()
    ESPERANDO_UBICACION_PANICO = auto()

_PRODUCT_CATALOG_CACHE = None
def cargar_catalogo_productos():
    global _PRODUCT_CATALOG_CACHE
    if _PRODUCT_CATALOG_CACHE is None:
        try:
            catalog_file_path = os.path.join(os.path.dirname(__file__), "..", "data", "product_catalog.json")
            with open(catalog_file_path, "r", encoding="utf-8") as f: _PRODUCT_CATALOG_CACHE = json.load(f)
            logger.info(f"✅ Catálogo de productos cargado desde {catalog_file_path}")
        except FileNotFoundError: logger.warning(f"[CATALOGO] Archivo no encontrado: {catalog_file_path}, se usa lista vacía"); _PRODUCT_CATALOG_CACHE = []
        except Exception as e: logger.error(f"❌ Error al cargar product_catalog.json: {e}", exc_info=True); _PRODUCT_CATALOG_CACHE = []
    return _PRODUCT_CATALOG_CACHE
PRODUCT_CATALOG = cargar_catalogo_productos()

_COMMERCE_LOCATIONS_CACHE = None
def cargar_ubicaciones_comercios():
    global _COMMERCE_LOCATIONS_CACHE
    if _COMMERCE_LOCATIONS_CACHE is None:
        try:
            loc_file_path = os.path.join(os.path.dirname(__file__), "..", "data", "commerce_locations.json")
            with open(loc_file_path, "r", encoding="utf-8") as f: _COMMERCE_LOCATIONS_CACHE = json.load(f)
            logger.info(f"✅ Ubicaciones de comercios cargadas desde {loc_file_path}")
        except FileNotFoundError: logger.warning(f"[COMERCIOS] Archivo no encontrado: {loc_file_path}, se usa lista vacía"); _COMMERCE_LOCATIONS_CACHE = []
        except Exception as e: logger.error(f"❌ Error al cargar commerce_locations.json: {e}", exc_info=True); _COMMERCE_LOCATIONS_CACHE = []
    return _COMMERCE_LOCATIONS_CACHE
COMMERCE_LOCATIONS = cargar_ubicaciones_comercios()

def enviar_notificacion_sms(numero_destino: str, mensaje: str):
    if not all([TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_PHONE_NUMBER]): print("[NOTIFICACION SMS] Faltan credenciales de Twilio SMS."); return
    try:
        client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
        message = client.messages.create(body=mensaje, from_=TWILIO_PHONE_NUMBER, to=numero_destino)
        print(f"[NOTIFICACION SMS] SMS enviado SID: {message.sid}")
    except Exception as e: print(f"[NOTIFICACION SMS] Error al enviar SMS: {e}")

def enviar_notificacion_whatsapp_con_plantilla(numero_destino: str, nombre: str, nro_ticket: str, categoria: str):
    if not all([TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_WHATSAPP_NUMBER, TWILIO_WHATSAPP_CONTENT_SID]): logger.error("[NOTIFICACION WHATSAPP] Faltan credenciales de Twilio WhatsApp (SID/Token/Number/Content_SID)."); return
    destinatario_whatsapp = f"whatsapp:{numero_destino}"
    try:
        client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
        variables_plantilla = {"1": nombre, "2": f"M-{nro_ticket}", "3": categoria}
        message = client.messages.create(from_=TWILIO_WHATSAPP_NUMBER, to=destinatario_whatsapp, content_sid=TWILIO_WHATSAPP_CONTENT_SID, content_variables=json.dumps(variables_plantilla))
        logger.info(f"[NOTIFICACION WHATSAPP] Plantilla enviada, SID: {message.sid}")
    except Exception as e: logger.error(f"[NOTIFICACION WHATSAPP] Error al enviar plantilla: {e}", exc_info=True)

def es_pregunta_nueva(texto_usuario: str, tipo_esperado: str, categorias_validas=None) -> bool:
    texto = texto_usuario.strip().lower()
    if texto in {"ok", "gracias"}: return True
    texto_norm = normalizar_texto(texto_usuario) # Normalize once

    UNIVERSAL_INTERRUPTS = {"cancelar", "salir", "menu", "menú", "ayuda", "inicio"}
    COMMON_GREETINGS = {"hola", "buen día", "buen dia", "buenas tardes", "buenas noches", "hey", "que tal", "buenas"}

    if texto_norm in UNIVERSAL_INTERRUPTS:
        logger.info(f"[Guardián de Flujo] Universal interrupt detected: '{texto_norm}'")
        return True

    if texto_norm in COMMON_GREETINGS and len(texto_norm.split()) <= 2:
        logger.info(f"[Guardián de Flujo] Common greeting detected: '{texto_norm}'")
        return True
        
    if texto_norm == "gracias" or "muchas gracias" in texto_norm:
        logger.info(f"[Guardián de Flujo] Thanks detected: '{texto_norm}'")
        return True

    # Specific rules to pass through valid-looking inputs to the handler
    if tipo_esperado == "una confirmación (sí o no)":
        if texto_norm in {"si", "sí", "no", "afirmativo", "negativo"}: return False
    if tipo_esperado == "una calificación del 1 al 5":
        if re.fullmatch(r"[1-5]", texto_norm): return False
    if tipo_esperado == "un número de ticket":
        # Allow M-12345, 12345, or even just a number if context is strong
        if re.fullmatch(r"m?\-?\d{4,}", texto_norm) or (tipo_esperado == "un número de ticket" and texto_norm.isdigit()):
             return False

    prompt = f"""
    Evalúa la RESPUESTA DEL USUARIO en el contexto de que el chatbot esperaba: '{tipo_esperado}'.
    RESPUESTA DEL USUARIO: "{texto_usuario}"

    Considera lo siguiente:
    - Si la RESPUESTA DEL USUARIO es un intento de proveer la información esperada (aunque sea parcial o malformada), es 'RESPUESTA_VALIDA'.
    - Si la RESPUESTA DEL USUARIO es una pregunta diferente, un saludo (ej: 'hola', 'buenas tardes'), una despedida, una solicitud de cancelación (ej: 'cancelar', 'salir'), o un cambio claro de tema, es 'PREGUNTA_NUEVA'.
    - Si la RESPUESTA DEL USUARIO es una expresión de frustración o confusión pero aún relacionada al flujo, considérala 'RESPUESTA_VALIDA' (el bot necesitará manejar la frustración).
    - Frases cortas como 'ok', 'bueno', 'dale' son ambiguas. Si el chatbot esperaba datos complejos (ej. una dirección completa, una descripción detallada) y recibe solo 'ok', es más probable que sea 'PREGUNTA_NUEVA' o un intento de resetear el flujo. Si esperaba una simple confirmación (sí/no), 'ok' puede ser 'RESPUESTA_VALIDA'.

    Basado en esto, ¿la RESPUESTA DEL USUARIO es una continuación del flujo actual o es una PREGUNTA_NUEVA/cambio de tema?
    Responde únicamente con 'RESPUESTA_VALIDA' o 'PREGUNTA_NUEVA'.
    """
    try:
        decision = get_cohere_response(message=prompt, preamble="Eres un clasificador experto en diálogos. Solo respondé 'RESPUESTA_VALIDA' o 'PREGUNTA_NUEVA'.")
        decision_clean = decision.strip().upper()
        logger.info(f"[Guardián de Flujo] LLM Input: '{texto_usuario}', Esperado: '{tipo_esperado}', Decisión LLM: '{decision_clean}'")
        return "PREGUNTA_NUEVA" in decision_clean
    except Exception as e:
        logger.error(f"[Guardián de Flujo] Error al clasificar pregunta nueva con LLM: {e}", exc_info=True)
        # Fallback strategy: if LLM fails, be conservative.
        # If text is very short (1-2 words) and not a clear "yes/no" when that's expected, assume new.
        if len(texto_norm.split()) <= 2 and texto_norm not in {"si", "sí", "no"}:
            # Also check if it's not a number if expecting a number (like rating)
            if tipo_esperado == "una calificación del 1 al 5" and texto_norm.isdigit() and re.fullmatch(r"[1-5]", texto_norm):
                return False # It's a valid rating
            logger.warning(f"[Guardián de Flujo] LLM error, fallback determined it's a new question for short input: '{texto_norm}'")
            return True 
        logger.warning(f"[Guardián de Flujo] LLM error, fallback determined it's NOT a new question: '{texto_norm}'")
        return False

PROMPT_MUNICIPIO_CON_CONTEXTO = """
Sos el asistente digital del municipio. Respondé la PREGUNTA DEL USUARIO usando solo la INFORMACIÓN DE CONTEXTO.
Si no tenés info suficiente, decilo y sugerí contactar al municipio.
--- CONTEXTO ---
{contexto_scraped}
-----------------
PREGUNTA: "{pregunta_usuario}"
Respuesta:
"""

class BaseMunicipioHandler:
    def __init__(self, context): self.context = context
    def handle(self, payload: dict) -> dict | None: raise NotImplementedError
    def build_detalles_memoria(self, memoria: dict) -> str:
        partes = []
        if memoria.get("categoria_reclamo"): partes.append(f"Categoría: {memoria['categoria_reclamo']}")
        direccion_estructurada = memoria.get("direccion_estructurada_reclamo")
        if direccion_estructurada and isinstance(direccion_estructurada, dict):
            dir_parts = []
            if direccion_estructurada.get("calle"): dir_parts.append(direccion_estructurada["calle"])
            if direccion_estructurada.get("numero"): dir_parts.append(direccion_estructurada["numero"])
            calle_numero_str = " ".join(filter(None, [direccion_estructurada.get("calle", ""), direccion_estructurada.get("numero", "")])).strip()
            if calle_numero_str:
                full_address_str = calle_numero_str
                if direccion_estructurada.get("localidad"): full_address_str += f", {direccion_estructurada['localidad']}"
                if direccion_estructurada.get("provincia"): full_address_str += f", {direccion_estructurada['provincia']}"
                if direccion_estructurada.get("barrio"): full_address_str += f" (Barrio: {direccion_estructurada['barrio']})"
                if direccion_estructurada.get("codigo_postal"): full_address_str += f" - CP: {direccion_estructurada['codigo_postal']}"
                if direccion_estructurada.get("otros_detalles"): full_address_str += f" - Detalles: {direccion_estructurada['otros_detalles']}"
                partes.append(f"Dirección: {full_address_str}")
            else: partes.append(f"Dirección: {memoria.get('direccion_reclamo', 'No especificada')}")
        elif memoria.get("direccion_reclamo"): partes.append(f"Dirección: {memoria['direccion_reclamo']}")
        if memoria.get("nombre_vecino"): partes.append(f"Nombre: {memoria['nombre_vecino']}")
        if memoria.get("telefono_vecino"): partes.append(f"Teléfono: {memoria['telefono_vecino']}")
        if memoria.get("email_vecino"): partes.append(f"Email: {memoria['email_vecino']}")
        if memoria.get("descripcion_reclamo"): partes.append(f"Descripción: {memoria['descripcion_reclamo']}")
        if memoria.get("ubicacion_gps"): lat = memoria['ubicacion_gps'].get('lat', 'N/A'); lon = memoria['ubicacion_gps'].get('lon', 'N/A'); partes.append(f"Ubicación GPS: Lat {lat}, Lon {lon}")
        if memoria.get("foto_url"): partes.append("Foto adjunta: Sí")
        return "\n".join(partes)

class GreetingHandler(BaseMunicipioHandler):
    def handle(self, payload: dict) -> dict | None:
        pregunta_str = payload.get("pregunta", ""); memoria = self.context[CONTEXTO_MUNICIPIO]; texto = normalizar_texto(pregunta_str.strip("!.,?"))
        saludos = ["hola", "buenos dias", "buenas tardes", "buenas noches", "hey", "que tal", "buenas"]; tokens = re.sub(r"[!.,?]", "", texto).split(); set_saludo = {"hola", "buenos", "dias", "buenas", "tardes", "noches", "hey", "que", "tal"}
        if texto in saludos or (0 < len(tokens) <= 3 and all(t in set_saludo for t in tokens)): # Simple greeting matches the whole input
            if len(pregunta_str.split()) > 5 and any(kw in texto_normalizado for kw in IntentClassifierHandler.KEYWORDS_RECLAMO + IntentClassifierHandler.KEYWORDS_TRAMITE + IntentClassifierHandler.KEYWORDS_SUGERENCIA):
                 # If the original message was long AND contains keywords for other intents,
                 # despite the simple greeting match, let other handlers try.
                logger.info("[GreetingHandler] Simple greeting detected in a longer, intentful message. Allowing other handlers to process details first.")
                memoria["saludo_detectado_en_largo_mensaje"] = True 
                return None # Let other handlers try to parse the details

            # Standard greeting response for short/simple greetings
            memoria.clear() 
            greeting_body = "¡Hola! 👋 Soy tu asistente digital del Municipio. Estoy aquí para ayudarte. Podés consultarme sobre trámites, hacer un reclamo, dejar una sugerencia o resolver alguna duda que tengas. ¡Contame en qué te puedo colaborar hoy!"
            options = [
                {"id": "iniciar_reclamo", "texto": "Hacer un reclamo"},
                {"id": "hacer_sugerencia", "texto": "Dejar una sugerencia"},
                {"id": "consultar_tramite", "texto": "Consultar un trámite"},
                {"id": "consultar_estado_ticket", "texto": "Estado de mi ticket"}
            ]
            message_type = 'interactive_buttons' if len(options) <= 3 else 'interactive_list'
            if len(options) > 10: 
                logger.warning("GreetingHandler: Too many options for WhatsApp list.")

            return {
                "message_body": greeting_body, "options_list": options,
                "message_type": message_type, "fuente": "saludo_municipio_interactivo_v2"
            }
        
        # For greetings at the start of longer sentences like "hola, quiero hacer un reclamo..."
        # The original loop `for saludo in saludos: if (texto.startswith...): memoria["saludo_detectado"] = True; break`
        # only sets a flag but doesn't return. This is good, it lets other handlers proceed.
        # We'll rely on ReclamoInteligenteMunicipioHandler or IntentClassifierHandler to pick up the rest.
        for saludo_kw in saludos: # Use a different variable name to avoid conflict
            if (texto.startswith(saludo_kw + " ") or texto.startswith(saludo_kw + ",") or texto.startswith(saludo_kw + ".")):
                memoria["saludo_detectado_en_largo_mensaje"] = True # Flag that a greeting was seen
                logger.info(f"[GreetingHandler] Leading greeting '{saludo_kw}' detected in longer message. Letting other handlers proceed.")
                break # No need to check other greeting keywords
        return None # Always return None if it's not a simple, standalone greeting, to allow other handlers.

class SugerenciasVecinoHandler(BaseMunicipioHandler):
    def handle(self, payload: dict) -> dict | None:
        pregunta_str = payload.get("pregunta", "").strip(); memoria = self.context[CONTEXTO_MUNICIPIO]; estado_conversacion_str = memoria.get("estado_conversacion"); estado_conversacion = ConversationState[estado_conversacion_str] if isinstance(estado_conversacion_str, str) else estado_conversacion_str; intencion = self.context.get("intencion");
        sugerencia_texto_directo = "" # Inicializar aquí

        if intencion == "hacer_sugerencia":
            texto_normalizado_input = normalizar_texto(pregunta_str)

            # Lista de frases introductorias comunes (ya normalizadas)
            # Ordenadas por longitud descendente para matchear la más larga primero
            frases_intro_norm = sorted([
                normalizar_texto("quiero hacer una sugerencia"),
                normalizar_texto("quiero dejar una sugerencia"),
                normalizar_texto("me gustaria sugerir"),
                normalizar_texto("tengo una sugerencia"),
                normalizar_texto("quisiera proponer una idea"),
                normalizar_texto("quisiera proponer"),
                normalizar_texto("mi sugerencia es"),
                normalizar_texto("mi idea es"),
                normalizar_texto("sugerencia"),
                normalizar_texto("proponer"),
                normalizar_texto("sugerir"),
                normalizar_texto("idea"),
                normalizar_texto("propuesta")
            ], key=len, reverse=True)

            texto_procesado_para_sugerencia = texto_normalizado_input
            prefijo_quitado = False
            for frase_intro in frases_intro_norm:
                if texto_procesado_para_sugerencia.startswith(frase_intro):
                    texto_procesado_para_sugerencia = texto_procesado_para_sugerencia[len(frase_intro):].strip()
                    prefijo_quitado = True
                    # Quitar conectores comunes si están al principio del texto restante
                    conectores_a_quitar = [":", ",", "que", "es que", "es"]
                    for conector in conectores_a_quitar:
                        # Normalizar también el conector para la comparación
                        conector_norm_espacio = normalizar_texto(conector + " ")
                        conector_norm_solo = normalizar_texto(conector)
                        if texto_procesado_para_sugerencia.startswith(conector_norm_espacio):
                            texto_procesado_para_sugerencia = texto_procesado_para_sugerencia[len(conector_norm_espacio):].strip()
                            break
                        elif texto_procesado_para_sugerencia.startswith(conector_norm_solo):
                             texto_procesado_para_sugerencia = texto_procesado_para_sugerencia[len(conector_norm_solo):].strip()
                             break
                    break

            # Si se quitó un prefijo y quedó algo, o si no se quitó nada pero el original no era solo una keyword de la lista.
            # La validación de longitud se hará después sobre `sugerencia_final`.
            if texto_procesado_para_sugerencia:
                 # Si el texto original era solo una de las frases introductorias (ej. "sugerencia"),
                 # entonces texto_procesado_para_sugerencia será vacío. En ese caso, no lo tomamos como sugerencia directa.
                if not (prefijo_quitado and not texto_procesado_para_sugerencia and texto_normalizado_input in frases_intro_norm):
                    sugerencia_texto_directo = texto_procesado_para_sugerencia


            if not sugerencia_texto_directo and estado_conversacion != ConversationState.ESPERANDO_TEXTO_SUGERENCIA:
                memoria["estado_conversacion"] = ConversationState.ESPERANDO_TEXTO_SUGERENCIA.name
                body = "Bueno, a continuación escribe todo lo que quieras sugerir. Nuestro bot interpretará tu mensaje y lo guardará en nuestra base de datos como un ticket para que nuestro equipo lo estudie."
                options = [{"id": "cancelar_sugerencia", "texto": "Cancelar sugerencia"}]
                return {
                    "message_body": body,
                    "options_list": options,
                    "message_type": 'interactive_buttons',
                    "fuente": "sugerencia_pedir_texto_v2"
                }

        if estado_conversacion == ConversationState.ESPERANDO_TEXTO_SUGERENCIA or sugerencia_texto_directo:
            sugerencia_para_validar = ""
            texto_original_sugerencia = pregunta_str # Guardar el input original para el ticket

            if sugerencia_texto_directo: # Vino del primer mensaje (ya está normalizado y procesado por la lógica anterior)
                sugerencia_para_validar = sugerencia_texto_directo
                # Si la sugerencia directa vino del primer mensaje, el texto original para guardar es pregunta_str
                # que contiene esa frase inicial (ej. "sugerencia: que pongan luces").
                # La lógica de extracción ya quitó "sugerencia:", así que `sugerencia_texto_directo` es "que pongan luces".
                # Si queremos guardar el texto original completo, `pregunta_str` es la fuente.
                # Si queremos guardar solo la parte extraída, necesitamos reconstruirla o usar `sugerencia_texto_directo`.
                # Por simplicidad y para asegurar que no perdemos nada, si `sugerencia_texto_directo` existe,
                # significa que `pregunta_str` contenía la sugerencia. `texto_original_sugerencia` ya es `pregunta_str`.
            elif estado_conversacion == ConversationState.ESPERANDO_TEXTO_SUGERENCIA:
                sugerencia_para_validar = normalizar_texto(pregunta_str) # Normalizar el input actual para validación
                # texto_original_sugerencia ya es pregunta_str.

            if pregunta_str and not sugerencia_texto_directo and \
               estado_conversacion == ConversationState.ESPERANDO_TEXTO_SUGERENCIA and \
               es_pregunta_nueva(pregunta_str, "el texto de tu sugerencia", memoria): # Solo chequear si es pregunta nueva si estamos esperando texto
                logger.info(f"[SugerenciasVecinoHandler] '{pregunta_str}' detectada como pregunta nueva mientras se esperaba texto de sugerencia. Limpiando.")
                memoria.clear()
                self.context["intencion"] = None
                return None

            keywords_genericas_sugerencia_norm = sorted(list(set([normalizar_texto(s) for s in [
                "sugerencia", "sugerencias", "idea", "propuesta", "proponer", "sugerir", "mejorar",
                "mi sugerencia", "mi idea", "una propuesta", "tengo una idea", "tengo una sugerencia"
            ]])))

            MIN_LEN_SUGERENCIA = 15
            sugerencia_valida_contenido = True
            if not sugerencia_para_validar or len(sugerencia_para_validar) < MIN_LEN_SUGERENCIA:
                sugerencia_valida_contenido = False
            # Verificar si la sugerencia (después de normalizar) es solo una keyword genérica
            # Esto es para el caso en que el usuario responda "sugerencia" cuando se le pide el detalle.
            if sugerencia_para_validar in keywords_genericas_sugerencia_norm and len(sugerencia_para_validar.split()) <= 2 : # ej. "sugerencia" o "mi idea"
                sugerencia_valida_contenido = False

            if not sugerencia_valida_contenido:
                memoria["estado_conversacion"] = ConversationState.ESPERANDO_TEXTO_SUGERENCIA.name # Mantener estado
                body_reprompt = "Por favor, danos más detalles sobre tu sugerencia para que podamos entenderla y procesarla correctamente. Necesitamos al menos algunas palabras que describan tu idea."
                if sugerencia_para_validar and sugerencia_para_validar in keywords_genericas_sugerencia_norm and len(sugerencia_para_validar.split()) <= 2:
                    body_reprompt = f"'{pregunta_str.strip()}' es un poco genérico. ¿Podrías describir tu sugerencia con más detalle, por favor?"
                elif sugerencia_para_validar and len(sugerencia_para_validar) < MIN_LEN_SUGERENCIA:
                     body_reprompt = "Tu sugerencia es un poco corta. Para entenderla bien, ¿podrías detallarla un poco más?"

                options_reprompt = [{"id": "cancelar_sugerencia_detalle", "texto": "Cancelar sugerencia"}]
                return {
                    "message_body": body_reprompt,
                    "options_list": options_reprompt,
                    "message_type": 'interactive_buttons',
                    "fuente": "sugerencia_pedir_mas_detalle_v3"
                }

            # Si la validación pasa, usamos el texto original que el usuario ingresó para el ticket
            sugerencia_a_guardar = texto_original_sugerencia.strip()

            try:
                ticket_data = {
                    "asunto": "Nueva Sugerencia/Mejora del Vecino",
                    "categoria": "Sugerencia",
                    "detalles": sugerencia_a_guardar, # Guardar el texto completo y original
                    "pregunta": sugerencia_a_guardar, # También en pregunta por consistencia
                    "estado": "nueva_sugerencia",
                    "user_id": self.context.get("cliente_id"),
                    "anon_id": self.context.get("anon_id") if not self.context.get("cliente_id") else None,
                    "municipio_id": getattr(self.context.get("user_obj"), "municipio_id", None)
                }
                tipo_ticket_para_sugerencia = "municipio" # Las sugerencias ciudadanas son para municipios
                ticket = servicio_tickets.crear_nuevo_ticket(tipo_ticket=tipo_ticket_para_sugerencia, ticket_data=ticket_data)
                if ticket:
                    nro_ticket_str = f"M-{ticket.nro_ticket}" # Asumiendo que siempre es municipio para sugerencia
                    logger.info(f"Sugerencia registrada como ticket {nro_ticket_str}."); memoria.clear() # CORREGIDO: logger_actual -> logger
                    body_exito = f"¡Muchas gracias por tu sugerencia! La hemos registrado y será revisada por nuestro equipo. Tu número de registro es {nro_ticket_str}. Valoramos mucho tu aporte."
                    options_exito = [
                        {"id": "hacer_otra_consulta_sug", "texto": "Hacer otra consulta"},
                        {"id": "volver_inicio_sug", "texto": "Volver al inicio"}
                    ]
                    return {
                        "message_body": body_exito,
                        "options_list": options_exito,
                        "message_type": 'interactive_buttons',
                        "fuente": "sugerencia_registrada_exito_v2"
                    }
                else: raise Exception("La creación del ticket de sugerencia retornó None.")
            except Exception as e: 
                logger.error(f"[SugerenciasVecinoHandler] Error al guardar sugerencia como ticket: {e}", exc_info=True)
                # Simple text response for error, no buttons needed here by default
                return {"message_body": "Hubo un problema al registrar tu sugerencia. Por favor, intentá de nuevo en un momento.", "options_list": [], "message_type": "text", "fuente": "sugerencia_error_registro_v2"}
        return None

class CancelHandler(BaseMunicipioHandler):
    CANCEL_KEYWORDS = ["cancelar", "olvidalo", "no importa", "volver", "salir", "cancelar reclamo", "no quiero continuar", "parar", "detener"]
    def handle(self, payload: dict) -> dict | None:
        pregunta_str = payload.get("pregunta", ""); texto = normalizar_texto(pregunta_str)
        if any(kw in texto for kw in self.CANCEL_KEYWORDS) or payload.get("action") == "cancelar":
            self.context[CONTEXTO_MUNICIPIO].clear()
            body = "Operación cancelada. ¿Necesitás ayuda con otro trámite o reclamo?"
            options = [
                {"id": "iniciar_reclamo", "texto": "Hacer un reclamo"},
                {"id": "consultar_estado_ticket", "texto": "Consultar estado de ticket"},
                {"id": "hablar_con_agente", "texto": "Hablar con un agente"}
            ]
            return {
                "message_body": body,
                "options_list": options,
                "message_type": 'interactive_buttons',
                "fuente": "cancel_handler_interactivo_v2"
            }
        return None

class PoliteHandler(BaseMunicipioHandler):
    KEYWORDS = {"gracias", "ok", "ok gracias", "muchas gracias", "dale", "perfecto", "genial"}
    def handle(self, payload: dict) -> dict | None:
        pregunta_str = payload.get("pregunta", ""); texto = normalizar_texto(pregunta_str)
        if texto in self.KEYWORDS:
            if not self.context[CONTEXTO_MUNICIPIO].get("estado_conversacion"): self.context[CONTEXTO_MUNICIPIO].clear()
            body = "¡De nada! ¿Necesitás ayuda con algo más?"
            options = [
                {"id": "iniciar_reclamo", "texto": "Hacer un reclamo"},
                {"id": "consultar_tramite", "texto": "Consultar estado de un trámite"},
                {"id": "hablar_con_agente", "texto": "Hablar con un agente"}
            ]
            return {
                "message_body": body,
                "options_list": options,
                "message_type": 'interactive_buttons',
                "fuente": "polite_handler_interactivo_v2"
            }
        return None

class SmallTalkHandler(BaseMunicipioHandler):
    def handle(self, payload: dict) -> dict | None:
        pregunta_str = payload.get("pregunta", "")
        if detectar_small_talk_con_llm(pregunta_str):
            respuesta_text = generar_respuesta_small_talk(pregunta_str)
            return {
                "message_body": respuesta_text,
                "options_list": [],
                "message_type": "text",
                "fuente": "smalltalk_municipio_llm_v2"
            }
        return None

class RecoleccionHandler(BaseMunicipioHandler):
    KEYWORDS = ["basura", "recoleccion", "residuos", "basurero"]
    def handle(self, payload: dict) -> dict | None:
        pregunta_str = payload.get("pregunta", ""); memoria = self.context[CONTEXTO_MUNICIPIO]
        estado_str = memoria.get("estado_conversacion"); estado = ConversationState[estado_str] if isinstance(estado_str, str) else estado_str
        texto = normalizar_texto(pregunta_str)
        if estado == ConversationState.ESPERANDO_PARAM_RECOLECCION:
            if es_pregunta_nueva(pregunta_str, "una dirección"): memoria.clear(); return None
            if not direccion_es_valida(pregunta_str): 
                return {"message_body": f"La dirección '{pregunta_str}' no parece completa o válida. ¿Podrías verificarla e ingresar calle y número?", "options_list": [], "message_type": "text", "fuente": "recoleccion_direccion_invalida_v2"}
            resultado = consultar_recoleccion_por_direccion(direccion=pregunta_str); memoria.clear()
            if not resultado or "No" in resultado:
                body = "No encontré información de recolección para esa dirección. Podés verificar en la web municipal o intentar con otra dirección."
                options = [
                    {"id": "consultar_otra_direccion_recoleccion", "texto": "Consultar otra dirección"},
                    {"id": "hablar_con_agente", "texto": "Hablar con un agente"}
                ]
                return {
                    "message_body": body,
                    "options_list": options,
                    "message_type": 'interactive_buttons',
                    "fuente": "recoleccion_sin_resultado_opciones_v2"
                }
            body_res = f"{resultado}\n¿Consultás otra dirección o hacés otro trámite?"
            options_res = [
                {"id": "consultar_otra_direccion_recoleccion", "texto": "Consultar otra dirección"},
                {"id": "iniciar_reclamo", "texto": "Hacer un reclamo"}
            ]
            return {
                "message_body": body_res,
                "options_list": options_res,
                "message_type": 'interactive_buttons',
                "fuente": "recoleccion_resultado_opciones_v2"
            }
        if any(kw in texto for kw in self.KEYWORDS) and not estado:
            if direccion_es_valida(pregunta_str):
                resultado = consultar_recoleccion_por_direccion(direccion=pregunta_str)
                if not resultado or "No" in resultado:
                    body_no_res_directo = "No encontré información de recolección para esa dirección. Revisá si está bien escrita, o consultá al municipio."
                    options_no_res_directo = [
                        {"id": "reintentar_direccion_recoleccion", "texto": "Reintentar"}, # Needs state handling or clear context
                        {"id": "hablar_con_agente", "texto": "Hablar con un agente"}
                    ]
                    return {
                        "message_body": body_no_res_directo,
                        "options_list": options_no_res_directo,
                        "message_type": 'interactive_buttons',
                        "fuente": "recoleccion_sin_resultado_directo_opciones_v2"
                    }
                body_res_directo = f"{resultado}\n¿Consultás otra dirección o hacés otro trámite?"
                options_res_directo = [
                    {"id": "consultar_otra_direccion_recoleccion", "texto": "Consultar otra dirección"},
                    {"id": "iniciar_reclamo", "texto": "Hacer un reclamo"}
                ]
                return {
                    "message_body": body_res_directo,
                    "options_list": options_res_directo,
                    "message_type": 'interactive_buttons',
                    "fuente": "recoleccion_resultado_directo_opciones_v2"
                }
            else:
                memoria["estado_conversacion"] = ConversationState.ESPERANDO_PARAM_RECOLECCION.name
                return {"message_body": f"¿La dirección para consultar el horario de recolección?\nPor ejemplo: {EJEMPLO_DIRECCION}", "options_list": [], "message_type": "text", "fuente": "recoleccion_pedir_direccion_v2"}
        return None

class IntentClassifierHandler(BaseMunicipioHandler):
    KEYWORDS_AGENTE = ["agente", "humano", "persona", "representante", "operador", "empleado", "atención", "real", "chat real", "soporte", "ayuda humana", "hablar con alguien", "asesor", "consultor", "soporte técnico", "atender", "personal", "comunicarme", "llamar", "contacto", "quiero hablar", "hablame con"]
    KEYWORDS_RECLAMO = ["reclamo", "reclamos", "queja", "quejas", "problema", "problemas", "denuncia", "denuncias", "reportar", "arbol caido", "árbol caído", "arbol", "caido"]
    KEYWORDS_TRAMITE = ["trámite", "tramite", "trámites", "tramites", "gestión", "gestiones", "consulta de trámite", "turno", "certificado", "licencia", "renovar", "sacar"]
    KEYWORDS_TICKET_STATUS = ["ticket", "estado", "seguimiento", "número de ticket"]
    KEYWORDS_SUGERENCIA = ["sugerencia", "sugerencias", "idea", "propuesta", "proponer", "mejorar"]
    KEYWORDS_INICIAR_COMPRA = ["comprar", "compra", "pedido", "productos", "catálogo", "catalogo", "tienda", "venden"]
    KEYWORDS_VER_CARRITO = ["carrito", "bolsa", "mi compra", "mi pedido"]
    KEYWORDS_PAGAR = ["pagar", "checkout", "finalizar compra", "cobrar"]
    KEYWORDS_UBICACION_TIENDA = ["ubicación", "dirección", "local", "tienda física", "sucursal", "mapa"]
    KEYWORDS_RECLAMO_PEDIDO = ["pedido mal", "problema compra", "producto roto", "pedido incorrecto"]
    KEYWORDS_PANICO = ["ayuda urgente", "emergencia", "sos", "necesito ayuda inmediata", "panico", "pánico", "boton de panico", "botón de pánico", "peligro"]
    def handle(self, payload: dict) -> dict | None:
        pregunta_str = payload.get("pregunta", "").strip(); logger.info(f"[INTENT] Analizando intención para: '{pregunta_str}'"); memoria = self.context[CONTEXTO_MUNICIPIO]; texto_normalizado = normalizar_texto(pregunta_str)

        # ---- INICIO NUEVA LÓGICA PARA NO SOBREESCRIBIR INTENCIÓN DE IMAGEN ----
        # Si ya hay una intención de 'iniciar_reclamo' (establecida por responder_municipio debido a media)
        # y el texto actual está vacío, no dejar que el LLM la cambie a 'general'.
        # self.context.get("es_foto") se establece DESPUÉS del bloque de análisis de imagen en responder_municipio.
        # Usaremos la presencia de 'uploaded_file_info_whatsapp' o 'uploaded_file_info' en el payload original.

        # Accedemos a la info original de la imagen desde el payload que recibió el handler.
        # `self.context` es el contexto global de `responder_municipio`.
        # `payload` es `received_payload` que también tiene esta info.

        _initial_media_info = self.context.get("uploaded_file_info_whatsapp_for_intent_classifier") or \
                              self.context.get("uploaded_file_info_for_intent_classifier")
                              # Estas claves se establecerían en responder_municipio si se decide pasar la info de esta forma

        # Mejor: usar context["es_foto"] que ya se setea en responder_municipio si hubo una imagen procesada
        if self.context.get("intencion") == "iniciar_reclamo" and \
           not pregunta_str and \
           self.context.get("es_foto"): # "es_foto" se establece en responder_municipio si se procesó una imagen
            logger.info(f"[INTENT] Intención 'iniciar_reclamo' por imagen previa y texto vacío. Manteniendo intención actual.")
            return None # No cambiar la intención, ReclamoHandler debería actuar.
        # ---- FIN NUEVA LÓGICA ----

        current_context_state_val = memoria.get("estado_conversacion")
        active_state_is_reclamo = False
        current_state_enum = None

        if isinstance(current_context_state_val, ConversationState):
            current_state_enum = current_context_state_val
        elif isinstance(current_context_state_val, str):
            try:
                current_state_enum = ConversationState[current_context_state_val]
            except KeyError:
                pass # current_state_enum remains None

        if current_state_enum and current_state_enum in RECLAMO_STATES:
            active_state_is_reclamo = True

        if active_state_is_reclamo:
            # If a reclamo is active, and this IntentClassifierHandler is called,
            # it means the ReclamoHandler (owner) decided not to handle the input (returned None).
            # We should not try to classify intent for simple inputs like "sí" or "ok" as a new general intent.
            # Let the ReclamoHandler get another chance in the remaining_handlers loop, or let it fall through to a generic "didn't understand".
            logger.info(f"[INTENT_CLASSIFIER] Reclamo en curso (estado activo: {current_state_enum.name if current_state_enum else current_context_state_val}). IntentClassifier cede el control y no clasificará nueva intención.")
            # Allow interruption keywords even if a reclamo is active
            if any(kw in texto_normalizado for kw in self.KEYWORDS_AGENTE):
                self.context["intencion"] = "hablar_con_agente"; memoria.clear(); logger.info(f"[MUNICIPIO] Intención: hablar_con_agente (por keyword, interrumpe flujo de reclamo)"); return None
            if any(kw in texto_normalizado for kw in self.KEYWORDS_PANICO):
                self.context["intencion"] = "activar_panico"; memoria.clear(); logger.info(f"[MUNICIPIO] Intención: activar_panico (por keyword, interrumpe flujo de reclamo)"); return None
            return None # Cede control

        # If not a reclamo state, or no state at all, proceed with normal intent classification
        if memoria.get("estado_conversacion"): # Handles non-reclamo active states
            if any(kw in texto_normalizado for kw in self.KEYWORDS_AGENTE): self.context["intencion"] = "hablar_con_agente"; memoria.clear(); logger.info(f"[MUNICIPIO] Intención: hablar_con_agente (por keyword, interrumpe flujo)"); return None
            if any(kw in texto_normalizado for kw in self.KEYWORDS_PANICO): self.context["intencion"] = "activar_panico"; memoria.clear(); logger.info(f"[MUNICIPIO] Intención: activar_panico (por keyword, interrumpe flujo)"); return None

            self.context["intencion"] = "continuar_flujo";
            active_state_log = memoria.get('estado_conversacion')
            if isinstance(active_state_log, Enum): active_state_log = active_state_log.name
            logger.info(f"[MUNICIPIO] Intención: continuar_flujo (estado activo no-reclamo: {active_state_log})")
            return None

        # No active state, classify intent from scratch
        for kw in self.KEYWORDS_PANICO:
            if kw in texto_normalizado: self.context["intencion"] = "activar_panico"; memoria.clear(); logger.info(f"[MUNICIPIO] Intención: activar_panico (por keyword '{kw}')"); return None
        for kw in self.KEYWORDS_AGENTE:
            if kw in texto_normalizado: self.context["intencion"] = "hablar_con_agente"; memoria.clear(); logger.info(f"[MUNICIPIO] Intención: hablar_con_agente (por keyword '{kw}')"); return None
        for kw in self.KEYWORDS_INICIAR_COMPRA:
            if kw in texto_normalizado: self.context["intencion"] = "iniciar_compra"; logger.info(f"[COMERCIO] Intención: iniciar_compra (por keyword '{kw}')"); return None
        for kw in self.KEYWORDS_VER_CARRITO:
            if kw in texto_normalizado: self.context["intencion"] = "ver_carrito"; logger.info(f"[COMERCIO] Intención: ver_carrito (por keyword '{kw}')"); return None
        for kw in self.KEYWORDS_PAGAR:
            if kw in texto_normalizado: self.context["intencion"] = "proceder_al_pago"; logger.info(f"[COMERCIO] Intención: proceder_al_pago (por keyword '{kw}')"); return None
        for kw in self.KEYWORDS_UBICACION_TIENDA:
            if kw in texto_normalizado: self.context["intencion"] = "solicitar_ubicacion_tienda"; logger.info(f"[COMERCIO] Intención: solicitar_ubicacion_tienda (por keyword '{kw}')"); return None
        for kw in self.KEYWORDS_RECLAMO_PEDIDO:
            if kw in texto_normalizado: self.context["intencion"] = "reclamo_pedido"; logger.info(f"[COMERCIO] Intención: reclamo_pedido (por keyword '{kw}')"); return None
        for kw in self.KEYWORDS_TICKET_STATUS:
            if kw in texto_normalizado: self.context["intencion"] = "consultar_estado_ticket"; logger.info(f"[MUNICIPIO] Intención: consultar_estado_ticket (por keyword '{kw}')"); return None
        for kw in self.KEYWORDS_RECLAMO:
            if kw in texto_normalizado: self.context["intencion"] = "iniciar_reclamo"; logger.info(f"[MUNICIPIO] Intención: iniciar_reclamo (por palabra clave '{kw}')"); return None
        for kw in self.KEYWORDS_TRAMITE:
            if kw in texto_normalizado: self.context["intencion"] = "consultar_tramite"; logger.info(f"[MUNICIPIO] Intención: consultar_tramite (por palabra clave '{kw}')"); return None
        for kw in self.KEYWORDS_SUGERENCIA:
            if kw in texto_normalizado: self.context["intencion"] = "hacer_sugerencia"; logger.info(f"[MUNICIPIO] Intención: hacer_sugerencia (por palabra clave '{kw}')"); return None

        intencion_llm = _clasificar_intencion_con_llm(pregunta_str) 
        self.context["intencion"] = intencion_llm
        logger.info(f"[MUNICIPIO] Intención (final por LLM): {self.context.get('intencion')}"); return None

class TicketStatusHandler(BaseMunicipioHandler):
    def handle(self, payload: dict) -> dict | None:
        pregunta_str = payload.get("pregunta", ""); memoria = self.context[CONTEXTO_MUNICIPIO]; estado_conversacion_str = memoria.get("estado_conversacion"); estado_conversacion = ConversationState[estado_conversacion_str] if isinstance(estado_conversacion_str, str) else estado_conversacion_str;
        if estado_conversacion and estado_conversacion in RECLAMO_STATES: return None
        if estado_conversacion == ConversationState.ESPERANDO_CONFIRMACION_CIERRE:
            # Si la pregunta está vacía (ej. usuario envió solo una imagen en WhatsApp),
            # no lo consideramos una "pregunta nueva" que limpie el contexto, sino que reiteramos.
            if not pregunta_str.strip():
                ticket_id = memoria.get("ticket_id_activo")
                # Re-obtener el ticket para construir el mensaje de reiteración.
                ticket = db.session.get(MunicipioTicket, ticket_id) if ticket_id else None
                asunto_ticket = ticket.asunto if ticket else "tu reclamo"
                estado_ticket = ticket.estado.replace('_', ' ').title() if ticket else "actual"

                body_reiteracion = f"Sigo esperando tu confirmación para el ticket sobre '{asunto_ticket}' (estado: {estado_ticket}). ¿Se resolvió tu problema?"
                options_reiteracion = [
                    {"id": "ticket_solucionado_reit", "texto": "Sí, solucionado"},
                    {"id": "ticket_no_solucionado_reit", "texto": "No, aún no"}
                ]
                # No cambiar estado_conversacion aquí, sigue esperando la confirmación.
                return {
                    "message_body": body_reiteracion,
                    "options_list": options_reiteracion,
                    "message_type": 'interactive_buttons',
                    "fuente": "ticket_status_reiterar_confirmacion_vacio_v2"
                }

            if es_pregunta_nueva(pregunta_str, "una confirmación (sí o no)"): memoria.clear(); return None
            ticket_id = memoria.get("ticket_id_activo"); ticket = db.session.get(MunicipioTicket, ticket_id)
            if not ticket: memoria.clear(); return {"message_body": "No pude encontrar el ticket activo. ¿Necesitás ayuda con algo más?", "options_list": [], "message_type": "text", "fuente": "ticket_status_no_ticket_activo_v2"}

            accion_confirmacion = normalizar_texto(pregunta_str)
            # Usar IDs de botones para mayor robustez si es posible, o keywords
            if accion_confirmacion == "ticket_solucionado" or "si" in accion_confirmacion:
                ticket.estado = "resuelto"; db.session.commit()
                memoria["estado_conversacion"] = ConversationState.ESPERANDO_CALIFICACION.name
                return {"message_body": "¡Excelente! ¿Podés calificar la atención recibida del 1 al 5?", "options_list": [], "message_type": "text", "fuente": "ticket_status_pedir_calificacion_v2"}
            elif accion_confirmacion == "ticket_no_solucionado" or "no" in accion_confirmacion:
                memoria.clear();
                return {"message_body": "Entendido. Dejamos el ticket abierto para que el equipo continúe con el seguimiento. ¿Necesitás algo más?", "options_list": [], "message_type": "text", "fuente": "ticket_status_no_resuelto_v2"}
            else: # Si no es "si" ni "no" claro, y tampoco era vacío (manejado arriba), podría ser una pregunta nueva (manejado por es_pregunta_nueva) o algo no entendido.
                  # Si es_pregunta_nueva retornó False, significa que LLM consideró que NO es una pregunta nueva.
                  # En este caso, reiterar la pregunta de confirmación es una buena estrategia.
                ticket_id = memoria.get("ticket_id_activo")
                ticket = db.session.get(MunicipioTicket, ticket_id) if ticket_id else None
                asunto_ticket = ticket.asunto if ticket else "tu reclamo"
                estado_ticket = ticket.estado.replace('_', ' ').title() if ticket else "actual"
                body_reiteracion_no_entendido = f"No entendí bien tu respuesta para el ticket sobre '{asunto_ticket}' (estado: {estado_ticket}). Por favor, confirmame: ¿Se resolvió tu problema?"
                options_reiteracion_no_entendido = [
                    {"id": "ticket_solucionado_reit_ne", "texto": "Sí, solucionado"},
                    {"id": "ticket_no_solucionado_reit_ne", "texto": "No, aún no"}
                ]
                return {
                    "message_body": body_reiteracion_no_entendido,
                    "options_list": options_reiteracion_no_entendido,
                    "message_type": 'interactive_buttons',
                    "fuente": "ticket_status_reiterar_confirmacion_no_entendido_v2"
                }

        elif estado_conversacion == ConversationState.ESPERANDO_CALIFICACION:
            if not pregunta_str.strip(): # Si la entrada es vacía
                return {"message_body": "Aún espero tu calificación del 1 al 5 para la atención recibida. ¿Podrías indicármela?", "options_list": [], "message_type": "text", "fuente": "ticket_status_reiterar_calificacion_vacio_v2"}

            if es_pregunta_nueva(pregunta_str, "una calificación del 1 al 5"): memoria.clear(); return None
            if not re.fullmatch(r"[1-5]", pregunta_str.strip()): 
                return {"message_body": "Por favor, ingresa una calificación del 1 al 5.", "options_list": [], "message_type": "text", "fuente": "ticket_status_pedir_calificacion_invalida_v2"}
            ticket_id = memoria.get("ticket_id_activo")
            if ticket_id: servicio_tickets.crear_comentario(ticket_id=ticket_id, tipo_ticket="municipio", comentario_data={"comentario": f"Calificación: {pregunta_str}", "es_admin": False, "anon_id": self.context.get("anon_id")})
            memoria.clear()
            body_post_calificacion = "¡Gracias por tu calificación! ¿Te ayudo con otro trámite o reclamo?"
            options_post_calificacion = [
                {"id": "iniciar_reclamo", "texto": "Nuevo reclamo"},
                {"id": "consultar_estado_ticket", "texto": "Consultar otro ticket"},
                {"id": "hablar_con_agente", "texto": "Hablar con un agente"}
            ]
            return {
                "message_body": body_post_calificacion,
                "options_list": options_post_calificacion,
                "message_type": 'interactive_buttons',
                "fuente": "ticket_status_post_calificacion_v2"
            }
        elif estado_conversacion == ConversationState.ESPERANDO_NUMERO_TICKET:
            if es_pregunta_nueva(pregunta_str, "un número de ticket"): memoria.clear(); return None
            match = re.search(r"\d{5,}", pregunta_str)
            if not match: 
                return {"message_body": "No entendí el número de ticket. ¿Podés repetirlo? Debe ser un número de al menos 5 dígitos.", "options_list": [], "message_type": "text", "fuente": "ticket_status_numero_invalido_v2"}
            numero = int(match.group(0)); ticket = MunicipioTicket.query.filter_by(nro_ticket=numero).first(); memoria.pop("estado_conversacion", None)
            if not ticket: 
                return {"message_body": f"No encontré ticket M-{match.group(0)}. Por favor, verificá el número.", "options_list": [], "message_type": "text", "fuente": "ticket_status_no_encontrado_num_v2"}
            respuesta = f"El ticket **M-{ticket.nro_ticket}** sobre '{ticket.asunto}' está en estado: **{ticket.estado.replace('_', ' ').title()}**."
            ultimo_comentario = TicketComentario.query.filter_by(municipio_ticket_id=ticket.id, es_admin=True).order_by(TicketComentario.fecha.desc()).first()
            if ultimo_comentario: respuesta += f"\nÚltima actualización: *{ultimo_comentario.comentario}*"
            if ticket.estado == "en_proceso":
                memoria["estado_conversacion"] = ConversationState.ESPERANDO_CONFIRMACION_CIERRE.name; memoria["ticket_id_activo"] = ticket.id
                body = respuesta + "\n¿Se resolvió tu problema?"
                options = [
                    {"id": "ticket_solucionado", "texto": "Sí, solucionado"},
                    {"id": "ticket_no_solucionado", "texto": "No, aún no"}
                ]
                return {
                    "message_body": body,
                    "options_list": options,
                    "message_type": 'interactive_buttons',
                    "fuente": "ticket_status_confirm_resolucion_v2"
                }
            return {"message_body": respuesta, "options_list": [], "message_type": "text", "fuente": "ticket_status_info_v2"}
        if self.context.get("intencion") == "consultar_estado_ticket":
            match = re.search(r"\d{5,}", pregunta_str)
            if not match: 
                memoria["estado_conversacion"] = ConversationState.ESPERANDO_NUMERO_TICKET.name
                return {"message_body": "Para consultar el estado de un ticket, decime el número de ticket por favor.", "options_list": [], "message_type": "text", "fuente": "ticket_status_pedir_numero_v2"}
            numero = int(match.group(0)); ticket = MunicipioTicket.query.filter_by(nro_ticket=numero).first()
            if not ticket: 
                return {"message_body": f"No encontré ticket M-{match.group(0)}. Por favor, verificá el número.", "options_list": [], "message_type": "text", "fuente": "ticket_status_no_encontrado_v2"}
            respuesta = f"El ticket **M-{ticket.nro_ticket}** sobre '{ticket.asunto}' está en estado: **{ticket.estado.replace('_', ' ').title()}**."
            ultimo_comentario = TicketComentario.query.filter_by(municipio_ticket_id=ticket.id, es_admin=True).order_by(TicketComentario.fecha.desc()).first()
            if ultimo_comentario: respuesta += f"\nÚltima actualización: *{ultimo_comentario.comentario}*"
            if ticket.estado == "en_proceso":
                memoria["estado_conversacion"] = ConversationState.ESPERANDO_CONFIRMACION_CIERRE.name; memoria["ticket_id_activo"] = ticket.id
                body_en_proceso = respuesta + "\n¿Se resolvió tu problema?"
                options_en_proceso = [
                    {"id": "ticket_solucionado", "texto": "Sí, solucionado"},
                    {"id": "ticket_no_solucionado", "texto": "No, aún no"}
                ]
                return {
                    "message_body": body_en_proceso,
                    "options_list": options_en_proceso,
                    "message_type": 'interactive_buttons',
                    "fuente": "ticket_status_confirm_resolucion_intencion_v2"
                }
            return {"message_body": respuesta, "options_list": [], "message_type": "text", "fuente": "ticket_status_info_intencion_v2"}
        return None

class ReclamoInteligenteMunicipioHandler(BaseMunicipioHandler):
    CAMPOS_RECLAMO = ["categoria", "direccion", "nombre", "telefono", "email", "descripcion"]

    # Definición simplificada para usar en ReclamoInteligenteMunicipioHandler
    _HANDLER_GENERIC_RECLAMO_KEYWORDS = {
        "reclamo", "reclamos", "queja", "quejas", "problema", "problemas",
        "denuncia", "denuncias", "reportar", "reporte",
        "hacer", "quiero", "necesito", "deseo", "ayuda", "consultar", "solicitar"
    }
    _HANDLER_FRASES_GENERICAS_RECLAMO = {
        normalizar_texto("hacer un reclamo"),
        normalizar_texto("quiero hacer un reclamo"),
        normalizar_texto("iniciar reclamo"),
        normalizar_texto("nuevo reclamo"),
        normalizar_texto("generar reclamo"),
        normalizar_texto("un reclamo")
    }
    _HANDLER_FILLER_WORDS = {"querer", "queria", "necesitar", "gustaria", "poder", "un", "una", "el", "la", "de", "del", "para", "mi", "yo", "tu", "quisiera", "me", "por", "favor", "podria", "podrias"}


    def handle(self, payload: dict) -> dict | None:
        pregunta_str = payload.get("pregunta", ""); memoria = self.context[CONTEXTO_MUNICIPIO]

        current_state_obj_str = memoria.get("estado_conversacion")
        current_state_obj = None
        if isinstance(current_state_obj_str, str):
            try: current_state_obj = ConversationState[current_state_obj_str]
            except KeyError: pass
        elif isinstance(current_state_obj_str, ConversationState):
            current_state_obj = current_state_obj_str

        if current_state_obj and current_state_obj in RECLAMO_STATES: return None

        if self.context.get("intencion") != "iniciar_reclamo": return None

        # --- NUEVA CONDICIÓN PARA EVITAR PROCESAR FRASES GENÉRICAS ---
        texto_normalizado_pregunta = normalizar_texto(pregunta_str)
        palabras_pregunta = texto_normalizado_pregunta.split()

        es_muy_generico = False
        if texto_normalizado_pregunta in self._HANDLER_FRASES_GENERICAS_RECLAMO:
            es_muy_generico = True
        else:
            # Quitar palabras de relleno y ver si solo quedan keywords de reclamo
            palabras_significativas = [p for p in palabras_pregunta if p not in self._HANDLER_FILLER_WORDS]

            if not palabras_significativas:
                pass # No hacer nada si solo eran fillers, no es genérico de reclamo per se

            elif palabras_significativas and all(p in self._HANDLER_GENERIC_RECLAMO_KEYWORDS for p in palabras_significativas):
                if len(palabras_pregunta) <= 5: # Umbral para frases como "queria hacer un reclamo" (4)
                    es_muy_generico = True

            if not es_muy_generico and len(palabras_pregunta) <= 3:
                if palabras_pregunta and all(palabra in self._HANDLER_GENERIC_RECLAMO_KEYWORDS for palabra in palabras_pregunta):
                    es_muy_generico = True

        if es_muy_generico:
            logger.info(f"[ReclamoInteligenteHandler] Pregunta '{pregunta_str}' es demasiado genérica (lógica mejorada). Cediendo a ReclamoHandler.")
            return None
        # --- FIN LÓGICA MEJORADA ---

        # La limpieza de memoria se hace DESPUÉS de la verificación de frase genérica,
        # solo si la frase NO es genérica y se va a proceder con la extracción inteligente.
        if not (current_state_obj and current_state_obj in RECLAMO_STATES):
            memoria.clear()
            logger.info("[ReclamoInteligenteHandler] Memoria limpiada para nuevo intento de reclamo inteligente (pregunta no genérica).")
        else:
            logger.info("[ReclamoInteligenteHandler] Reclamo ya en curso, no se limpiará la memoria globalmente aquí.")


        if not memoria.get("categoria_reclamo") and not memoria.get("direccion_reclamo"): # Only run if no claim data already in memory
            prompt = f"""
            Extraé del siguiente mensaje los siguientes datos si están presentes, si algún campo no está presente, simplemente omitilo:
            - categoria (motivo del reclamo, ej: basura, agua, semáforo, etc. Usá las categorías: {", ".join(CATEGORIAS_RECLAMO)})
            - direccion (ej: Av. San Martín 123)
            - nombre (nombre y apellido del reclamante)
            - telefono (número de teléfono con código de área)
            - email (dirección de correo electrónico)
            - descripcion (detalle del problema)
            Mensaje: "{pregunta_str}"
            Devolvé solo JSON con esos campos. Ejemplo: {{"categoria": "luminaria", "direccion": "Av. San Martín 500", "nombre": "Luis Pérez", "telefono": "2613334444", "email": "luis@gmail.com", "descripcion": "La luz del poste está apagada hace días."}}
            """
            datos_extraidos_reclamo_inteligente = {} # Initialize as empty dict
            try:
                resp = get_cohere_response(message=prompt, preamble="Extraé los campos y devolvé solo JSON.")
                if resp and resp.strip(): # Ensure response is not empty or just whitespace
                    datos_extraidos_reclamo_inteligente = json.loads(resp)
                    logger.info(f"[ReclamoInteligenteHandler] Datos extraídos por LLM: {datos_extraidos_reclamo_inteligente}")
                else:
                    logger.warning(f"[ReclamoInteligenteHandler] Respuesta vacía o solo espacios de Cohere para prompt: {prompt}")
            except json.JSONDecodeError as e: # Catch only JSONDecodeError specifically
                logger.error(f"[ReclamoInteligenteMunicipioHandler] Error Cohere/JSON al decodificar: {e}. Respuesta LLM: '{resp}'", exc_info=True)
                # datos_extraidos_reclamo_inteligente remains {}
            except Exception as e: # Catch other potential errors from get_cohere_response or other issues
                logger.error(f"[ReclamoInteligenteMunicipioHandler] Error inesperado en extracción Cohere: {e}", exc_info=True)
                # datos_extraidos_reclamo_inteligente remains {}
            
            # It's important to NOT clear the whole memoria here if the intent is 'iniciar_reclamo'
            # but we are already in a RECLAMO_STATE. This handler (ReclamoInteligenteMunicipioHandler)
            # should only run if no reclamo is active.
            # The check `if current_state_obj and current_state_obj in RECLAMO_STATES: return None`
            # at the beginning of the handle method should prevent this.
            # If it's truly a new reclamo (no relevant state, intent is iniciar_reclamo), then clear is fine.
            if not (current_state_obj and current_state_obj in RECLAMO_STATES):
                memoria.clear() 
                logger.info("[ReclamoInteligenteHandler] Memoria limpiada para nuevo intento de reclamo inteligente.")
            else:
                logger.info("[ReclamoInteligenteHandler] Reclamo ya en curso, no se limpiará la memoria globalmente aquí.")


            direccion_texto_original = datos_extraidos_reclamo_inteligente.get("direccion", "")
            if direccion_texto_original:
                config_muni_para_parseo = self.context.get("municipio_config") or CONFIG_MUNICIPIO
                parsed_address = parse_direccion_completa(direccion_texto_original, config_muni_para_parseo)
                if parsed_address and parsed_address.get("calle") and parsed_address.get("localidad"):
                    memoria["direccion_estructurada_reclamo"] = parsed_address
                    memoria["direccion_reclamo"] = f"{parsed_address['calle']} {parsed_address.get('numero', '')}, {parsed_address['localidad']}".replace(" ,", ",").strip()
                    logger.info(f"[ReclamoInteligenteHandler] Dirección parseada y guardada: {memoria['direccion_estructurada_reclamo']}")
                else:
                    if direccion_es_valida(direccion_texto_original): memoria["direccion_reclamo"] = direccion_texto_original.strip(); logger.warning(f"[ReclamoInteligenteHandler] Dirección '{direccion_texto_original}' no pudo ser parseada estructuradamente pero pasó validación básica.")
                    else: logger.warning(f"[ReclamoInteligenteHandler] Dirección '{direccion_texto_original}' no válida o no parseable. Se pedirá.")
            # Populate non-category fields first
            for campo in self.CAMPOS_RECLAMO:
                if campo == "direccion" or campo == "categoria": continue # Handle category and address separately
                valor_campo = datos_extraidos_reclamo_inteligente.get(campo, "")
                if valor_campo:
                    if campo == "telefono":
                        if validar_telefono(valor_campo):
                            memoria["telefono_vecino"] = formatear_telefono_e164(valor_campo)
                            logger.info(f"[ReclamoInteligenteHandler] LLM Teléfono: {memoria['telefono_vecino']}")
                        else: logger.warning(f"[ReclamoInteligenteHandler] LLM Teléfono '{valor_campo}' no válido.")
                    elif campo == "email":
                        if validar_email(valor_campo):
                            memoria["email_vecino"] = valor_campo.strip().lower()
                            logger.info(f"[ReclamoInteligenteHandler] LLM Email: {memoria['email_vecino']}")
                        else: logger.warning(f"[ReclamoInteligenteHandler] LLM Email '{valor_campo}' no válido.")
                    elif campo == "nombre":
                        memoria["nombre_vecino"] = valor_campo.strip()
                        logger.info(f"[ReclamoInteligenteHandler] LLM Nombre: {memoria['nombre_vecino']}")
                    elif campo == "descripcion": # Prioritize LLM description
                        desc_val = valor_campo.strip()
                        if len(desc_val) >= 10 : # Basic check for meaningful description
                             memoria["descripcion_reclamo"] = desc_val
                             logger.info(f"[ReclamoInteligenteHandler] LLM Descripción: {desc_val[:50]}")
                        else:
                             logger.info(f"[ReclamoInteligenteHandler] LLM Descripción '{desc_val}' muy corta, se pedirá si es necesario.")
                    else: # Should not happen with current CAMPOS_RECLAMO
                        memoria[campo] = valor_campo.strip()
            
            # Refined Category Logic
            llm_category_raw = datos_extraidos_reclamo_inteligente.get("categoria", "").strip()
            # Use description from memoria (which might be from LLM) or fallback to pregunta_str for keyword categorization
            current_description_for_cat = memoria.get("descripcion_reclamo", pregunta_str) 

            if llm_category_raw:
                matched_category_from_llm = next((c for c in CATEGORIAS_RECLAMO if normalizar_texto(c) == normalizar_texto(llm_category_raw)), None)
                if not matched_category_from_llm: # Try fuzzy match if exact fails
                    close_matches_llm = difflib.get_close_matches(normalizar_texto(llm_category_raw), categorias_normalizadas, n=1, cutoff=0.7)
                    if close_matches_llm:
                        idx = categorias_normalizadas.index(close_matches_llm[0])
                        matched_category_from_llm = CATEGORIAS_RECLAMO[idx]
                
                if matched_category_from_llm and matched_category_from_llm != "otro motivo":
                    memoria["categoria_reclamo"] = matched_category_from_llm
                    logger.info(f"[ReclamoInteligenteHandler] Categoría por LLM: {matched_category_from_llm}")
                else: # LLM category is "otro motivo" or not matched well, try keywords
                    keyword_category = categorizar_reclamo_por_palabra_clave(current_description_for_cat)
                    if keyword_category and keyword_category != "otro motivo":
                        memoria["categoria_reclamo"] = keyword_category
                        logger.info(f"[ReclamoInteligenteHandler] Categoría por keywords de descripción ('{current_description_for_cat[:30]}...'): {keyword_category}")
                    elif matched_category_from_llm: # LLM said "otro motivo" and keywords found nothing better
                         memoria["categoria_reclamo"] = matched_category_from_llm 
                         logger.info(f"[ReclamoInteligenteHandler] Categoría por LLM fue '{matched_category_from_llm}', keywords no mejoraron.")
                    # If category is still not set (e.g. LLM no dio, keywords no dio), se pedirá en paso a paso
            else: # LLM did not provide any category, rely on keywords from description/pregunta
                keyword_category = categorizar_reclamo_por_palabra_clave(current_description_for_cat)
                if keyword_category: # This can be "otro motivo" if keywords map to it
                    memoria["categoria_reclamo"] = keyword_category
                    logger.info(f"[ReclamoInteligenteHandler] Sin categoría LLM, usando categoría por keywords ('{current_description_for_cat[:30]}...'): {keyword_category}")

            # Ensure 'descripcion_reclamo' is set if LLM provided it and it wasn't set by the loop above
            # This handles if "descripcion" was not in CAMPOS_RECLAMO loop explicitly for some reason, or if it was empty there.
            llm_description_check = datos_extraidos_reclamo_inteligente.get("descripcion", "").strip()
            if llm_description_check and not memoria.get("descripcion_reclamo"):
                if len(llm_description_check) >= 10:
                    memoria["descripcion_reclamo"] = llm_description_check
                    logger.info(f"[ReclamoInteligenteHandler] Descripción (re-check) por LLM: {llm_description_check[:50]}")

            if any(memoria.get(key) for key in ["categoria_reclamo", "direccion_reclamo", "nombre_vecino", "telefono_vecino", "email_vecino", "descripcion_reclamo"]): # If any field was extracted by LLM or parsing
                if all(memoria.get(key) for key in ["categoria_reclamo", "direccion_reclamo", "nombre_vecino", "telefono_vecino", "email_vecino", "descripcion_reclamo"]): # If all fields extracted
                    memoria["estado_conversacion"] = ConversationState.ESPERANDO_CONFIRMACION_RECLAMO.name
                    resumen = self.build_detalles_memoria(memoria)
                    body = f"Parece que tenemos todos los datos. ¿Confirmás el reclamo con estos datos?\n{resumen}"
                    options = [
                        {"id": "confirmar_reclamo_inteligente", "texto": "Confirmar reclamo"},
                        {"id": "editar_reclamo_inteligente", "texto": "Editar datos"}
                    ]
                    return {
                        "message_body": body,
                        "options_list": options,
                        "message_type": 'interactive_buttons',
                        "fuente": "reclamo_inteligente_confirmacion_v2"
                    }
                else:
                    # Determine the first missing field and set state accordingly
                    campos_requeridos_orden = [
                        ("categoria_reclamo", ConversationState.ESPERANDO_CATEGORIA_RECLAMO),
                        ("direccion_reclamo", ConversationState.ESPERANDO_DIRECCION_RECLAMO),
                        ("descripcion_reclamo", ConversationState.ESPERANDO_DESCRIPCION_RECLAMO),
                        ("nombre_vecino", ConversationState.ESPERANDO_NOMBRE_VECINO),
                        ("telefono_vecino", ConversationState.ESPERANDO_TELEFONO_VECINO),
                        ("email_vecino", ConversationState.ESPERANDO_EMAIL_VECINO)
                    ]
                    proximo_estado_a_pedir = ConversationState.ESPERANDO_CATEGORIA_RECLAMO # Default
                    for campo_memoria, estado_enum in campos_requeridos_orden:
                        if not memoria.get(campo_memoria):
                            proximo_estado_a_pedir = estado_enum
                            break
                    
                    memoria["estado_conversacion"] = proximo_estado_a_pedir.name
                    logger.info(f"[ReclamoInteligenteHandler] Datos parciales extraídos. Próximo estado para ReclamoHandler: {proximo_estado_a_pedir.name}.")
                    return None # Let ReclamoHandler ask the question for the new state

                    # The following lines from the original SEARCH block are now effectively replaced by 'return None':
                    # sugeridas_data_intel = sugerir_categorias_relevantes(pregunta_str)
                    options_data_intel = sugeridas_data_intel if sugeridas_data_intel else CATEGORIAS_RECLAMO
                    options_intel = [{"id": normalizar_texto(c), "texto": c.title()} for c in options_data_intel]

                    primera_pregunta = ("Para tu reclamo, ¿podrías ayudarme seleccionando una categoría, "
                                       "o describiendo brevemente de qué se trata?")
                    if sugeridas_data_intel:
                        primera_pregunta = "Detecté que podría ser sobre algunos de estos temas. Para tu reclamo, ¿cuál sería la categoría?"
                    
                    logger.info(f"[ReclamoInteligenteHandler] No todos los datos extraídos. Iniciando reclamo paso a paso con pregunta de categoría.")
                    
                    message_type_intel = 'interactive_list' # Categories can be many
                    if len(options_intel) > 10:
                        logger.warning(f"ReclamoInteligenteHandler: Too many options ({len(options_intel)}) for WhatsApp list. Formatter will truncate.")
                    
                    return {
                        "message_body": primera_pregunta,
                        "options_list": options_intel,
                        "message_type": message_type_intel,
                        "fuente": "solicitud_categoria_reclamo_inteligente_interactivo_v2"
                    }
        return None # Only returns None if no claim fields were extracted at all initially, or if it's not its turn.

class ReclamoHandler(BaseMunicipioHandler):
    EDIT_KEYWORDS = ["editar", "cambiar", "corregir", "modificar", "no era asi", "me equivoque", "error"]
    def handle(self, payload: dict) -> dict | None:
        pregunta_str = payload.get("pregunta", "") or ""; memoria = self.context[CONTEXTO_MUNICIPIO]

        estado_str = memoria.get("estado_conversacion")
        estado = None # Current state as Enum
        if estado_str:
            if isinstance(estado_str, str):
                try: estado = ConversationState[estado_str]
                except KeyError: logger.warning(f"[ReclamoHandler] Invalid state string '{estado_str}' found in memory. Clearing state."); memoria.pop("estado_conversacion", None)
            elif isinstance(estado_str, ConversationState): estado = estado_str # Already an Enum

        intencion = self.context.get("intencion")

        # Log entry point
        logger.info(f"[ReclamoHandler.handle ENTRY] Pregunta: '{pregunta_str[:100]}...', Estado Memoria: {estado.name if estado else 'None'}, Intención: {intencion}")

        if intencion == "iniciar_reclamo" and estado is None:
            logger.info("[ReclamoHandler] Intención 'iniciar_reclamo' y sin estado previo.")

            interacciones_previas = memoria.get("interacciones_anon_sesion")
            # Limpiar solo campos relevantes al reclamo, no todo el contexto del municipio
            campos_a_limpiar_reclamo = [
                "categoria_reclamo", "descripcion_reclamo", "direccion_reclamo",
                "nombre_vecino", "telefono_vecino", "email_vecino",
                "foto_url", "ubicacion_gps", "direccion_estructurada_reclamo",
                "mensaje_adjunto_recibido", "analisis_imagen_reclamo_auto",
                "estado_conversacion" # Limpiar el estado también para empezar de cero el flujo de reclamo
            ]
            for campo_limpiar in campos_a_limpiar_reclamo:
                memoria.pop(campo_limpiar, None)

            if interacciones_previas is not None: # Restaurar si existía
                memoria["interacciones_anon_sesion"] = interacciones_previas
            logger.info(f"[ReclamoHandler] Memoria de reclamo limpiada. Contexto actual: {memoria}")

            if self.context.get("es_foto") and self.context.get("foto_url"): # No chequear memoria.get("foto_url") aquí
                memoria["foto_url"] = self.context.get("foto_url")
                memoria["mensaje_adjunto_recibido"] = "Veo que adjuntaste una foto. "
                logger.info(f"[ReclamoHandler] Foto {memoria['foto_url']} reconocida del contexto global.")

            if self.context.get("analisis_imagen_reclamo_auto"):
                analisis_img = self.context.get("analisis_imagen_reclamo_auto")
                if analisis_img.get("categoria") and not memoria.get("categoria_reclamo"): # Solo si no está ya seteada
                    memoria["categoria_reclamo"] = analisis_img["categoria"]
                    logger.info(f"[ReclamoHandler] Categoría pre-llenada por análisis de imagen: {memoria['categoria_reclamo']}")
                if analisis_img.get("descripcion") and not memoria.get("descripcion_reclamo"):
                    memoria["descripcion_reclamo"] = analisis_img["descripcion"]
                    logger.info(f"[ReclamoHandler] Descripción pre-llenada por análisis de imagen: {memoria['descripcion_reclamo'][:50]}")

            if memoria.get("categoria_reclamo"):
                logger.info(f"[ReclamoHandler] Categoría '{memoria['categoria_reclamo']}' ya en memoria. Avanzando a pedir dirección.")
                memoria["estado_conversacion"] = ConversationState.ESPERANDO_DIRECCION_RECLAMO.name
                estado = ConversationState.ESPERANDO_DIRECCION_RECLAMO
            else:
                memoria["estado_conversacion"] = ConversationState.ESPERANDO_CATEGORIA_RECLAMO.name

                mensaje_adjunto = memoria.pop("mensaje_adjunto_recibido", "")
                texto_para_sugerir_categorias = memoria.get("descripcion_reclamo", "")

                sugeridas_data = sugerir_categorias_relevantes(texto_para_sugerir_categorias)
                options_data = sugeridas_data if (sugeridas_data and len(sugeridas_data) > 0) else CATEGORIAS_RECLAMO
                options_cat = [{"id": normalizar_texto(c), "texto": c.title()} for c in options_data]
                if not any(opt['id'] == "otro motivo" for opt in options_cat) and "otro motivo" in CATEGORIAS_RECLAMO:
                    options_cat.append({"id": "otro motivo", "texto": "Otro Motivo"})

                respuesta_texto_cat = f"{mensaje_adjunto}Para tu reclamo, ¿podrías ayudarme seleccionando una categoría, o describiendo brevemente de qué se trata?"
                if sugeridas_data:
                    respuesta_texto_cat = f"{mensaje_adjunto}Detecté que podría ser sobre algunos de estos temas. Para tu reclamo, ¿cuál sería la categoría?"

                message_type_cat = 'interactive_list' if len(options_cat) > 3 else 'interactive_buttons'
                if len(options_cat) > 10: logger.warning(f"ReclamoHandler (inicio): Too many options for WhatsApp list.")

                logger.info("[ReclamoHandler] Retornando solicitud de categoría (inicio de flujo).")
                return {"message_body": respuesta_texto_cat, "options_list": options_cat, "message_type": message_type_cat, "fuente": "solicitud_categoria_reclamo_inicio_v4"}

        if not estado or estado not in RECLAMO_STATES:
            if intencion == "iniciar_reclamo" and estado is None:
                 logger.error("[ReclamoHandler] Lógica de inicio de reclamo no retornó como se esperaba (estado aún None). Abortando.")
                 return {"message_body":"Error al iniciar el reclamo. Por favor, intente de nuevo.", "options_list":[], "message_type":"text", "fuente":"reclamo_error_inicio_inesperado"}
            logger.debug(f"[ReclamoHandler] Intención '{intencion}' o estado '{estado.name if estado else 'None'}' no son para este handler en este punto. No se maneja aquí.")
            return None

        is_simple_confirmation = pregunta_str.lower() in ["si", "sí", "no", "ok", "dale", "cancelar"]
        is_known_action_button = payload.get("action") in ["adjuntar_foto", "compartir_ubicacion", "sin_adjuntos", "confirmar_reclamo", "editar_reclamo"]
        llm_extraction_beneficial_states = [ConversationState.ESPERANDO_CATEGORIA_RECLAMO, ConversationState.ESPERANDO_DIRECCION_RECLAMO, ConversationState.ESPERANDO_NOMBRE_VECINO, ConversationState.ESPERANDO_TELEFONO_VECINO, ConversationState.ESPERANDO_EMAIL_VECINO, ConversationState.ESPERANDO_DESCRIPCION_RECLAMO]

        # Enhanced LLM extraction at the beginning of relevant states or for general input during reclamo
        # Only run LLM if pregunta_str is not a simple confirmation, not a known action, and is not empty.
        if estado in llm_extraction_beneficial_states and \
           pregunta_str and \
           not is_simple_confirmation and \
           not is_known_action_button:

            logger.info(f"[ReclamoHandler_LLM_ENHANCED] Attempting LLM extraction for state {estado.name if isinstance(estado, Enum) else estado} with input: '{pregunta_str}'")
            # Use extract_multiple_contact_details_llm for broader extraction
            # Define fields relevant to the current state or all reclamo fields
            potential_fields_for_llm = [
                "tipo_problema", "ubicacion_problema", "descripcion_problema",
                "nombre_cliente", "telefono_cliente", "email_cliente"
            ]
            # extracted_details = extract_complaint_details_llm(pregunta_str) # Original
            extracted_details = extract_multiple_contact_details_llm(pregunta_str, potential_fields_for_llm)


            if extracted_details:
                logger.info(f"[ReclamoHandler_LLM_ENHANCED] LLM Extracted: {extracted_details}")
                llm_updated_any_field_in_this_pass = False

                # Populate memoria based on extracted_details, checking if field already exists
                if 'tipo_problema' in extracted_details and extracted_details['tipo_problema'] and not memoria.get("categoria_reclamo"):
                    cat_text = extracted_details['tipo_problema']
                    matched_category = next((c for c in CATEGORIAS_RECLAMO if normalizar_texto(c) == normalizar_texto(cat_text)), None)
                    if not matched_category:
                        close_matches = difflib.get_close_matches(normalizar_texto(cat_text), categorias_normalizadas, n=1, cutoff=0.7)
                        if close_matches: idx = categorias_normalizadas.index(close_matches[0]); matched_category = CATEGORIAS_RECLAMO[idx]
                    if matched_category:
                        memoria["categoria_reclamo"] = matched_category; llm_updated_any_field_in_this_pass = True; logger.info(f"LLM set categoria_reclamo: {matched_category}")

                if 'ubicacion_problema' in extracted_details and extracted_details['ubicacion_problema'] and not memoria.get("direccion_reclamo"):
                    addr_text = extracted_details['ubicacion_problema'].strip()
                    config_muni_llm_addr = self.context.get("municipio_config") or CONFIG_MUNICIPIO
                    parsed_llm_address = parse_direccion_completa(addr_text, config_muni_llm_addr)
                    if parsed_llm_address and parsed_llm_address.get("calle") and parsed_llm_address.get("localidad"):
                        memoria["direccion_estructurada_reclamo"] = parsed_llm_address
                        direccion_llm_confirmacion = f"{parsed_llm_address['calle']} {parsed_llm_address.get('numero', '')}, {parsed_llm_address['localidad']}".replace(" ,", ",").strip()
                        memoria["direccion_reclamo"] = direccion_llm_confirmacion
                        llm_updated_any_field_in_this_pass = True; logger.info(f"LLM set direccion_reclamo (structured): {direccion_llm_confirmacion}")
                    elif direccion_es_valida(addr_text): # Fallback if structured parse fails but basic valid
                        memoria["direccion_reclamo"] = addr_text; llm_updated_any_field_in_this_pass = True; logger.info(f"LLM set direccion_reclamo (basic valid): {addr_text}")

                if 'descripcion_problema' in extracted_details and extracted_details['descripcion_problema'] and not memoria.get("descripcion_reclamo"):
                    desc_text = extracted_details['descripcion_problema'].strip()
                    if len(desc_text) > 10 :
                        memoria["descripcion_reclamo"] = desc_text; llm_updated_any_field_in_this_pass = True; logger.info(f"LLM set descripcion_reclamo: {desc_text[:50]}...")

                if 'nombre_cliente' in extracted_details and extracted_details['nombre_cliente'] and not memoria.get("nombre_vecino"):
                    nombre_val = extracted_details['nombre_cliente'].strip()
                    if len(nombre_val.split()) >= 1: # Allow single name if LLM provides it. Step validation will ask for full if needed.
                         memoria["nombre_vecino"] = nombre_val; llm_updated_any_field_in_this_pass = True; logger.info(f"LLM set nombre_vecino: {nombre_val}")

                if 'telefono_cliente' in extracted_details and extracted_details['telefono_cliente'] and not memoria.get("telefono_vecino"):
                    telefono_input_llm = extracted_details['telefono_cliente']
                    if validar_telefono(telefono_input_llm):
                        telefono_normalizado_llm = formatear_telefono_e164(telefono_input_llm)
                        memoria["telefono_vecino"] = telefono_normalizado_llm
                        llm_updated_any_field_in_this_pass = True
                        logger.info(f"LLM set telefono_vecino (normalizado E.164): {telefono_normalizado_llm}")
                    else:
                        logger.warning(f"[ReclamoHandler_LLM_ENHANCED] Teléfono '{telefono_input_llm}' extraído por LLM no pasó la validación.")

                if 'email_cliente' in extracted_details and extracted_details['email_cliente'] and not memoria.get("email_vecino"):
                    email_input_llm = extracted_details['email_cliente']
                    if validar_email(email_input_llm):
                        email_normalizado_llm = email_input_llm.strip().lower()
                        memoria["email_vecino"] = email_normalizado_llm
                        llm_updated_any_field_in_this_pass = True
                        logger.info(f"LLM set email_vecino (normalizado): {email_normalizado_llm}")
                    else:
                        logger.warning(f"[ReclamoHandler_LLM_ENHANCED] Email '{email_input_llm}' extraído por LLM no pasó la validación.")
                
                if llm_updated_any_field_in_this_pass:
                    logger.info(f"[ReclamoHandler_LLM_ENHANCED] LLM pre-filled data. Memoria actual: {memoria}")
                    # Check if all required fields are now filled to jump to confirmation
                    todos_los_datos_requeridos = ["categoria_reclamo", "direccion_reclamo", "nombre_vecino", "telefono_vecino", "email_vecino", "descripcion_reclamo"]
                    if all(memoria.get(campo) for campo in todos_los_datos_requeridos):
                        logger.info("[ReclamoHandler_LLM_ENHANCED] Todos los datos requeridos fueron completados por LLM. Saltando a confirmación.")
                        memoria["estado_conversacion"] = ConversationState.ESPERANDO_CONFIRMACION_RECLAMO.name
                        estado = ConversationState.ESPERANDO_CONFIRMACION_RECLAMO # Update local state for current pass
                        # The loop will break, and the ESPERANDO_CONFIRMACION_RECLAMO logic will handle the response
                    # No explicit 'continue' here, the while loop will re-evaluate with the updated memoria
            else:
                logger.info(f"[ReclamoHandler_LLM_ENHANCED] LLM returned no structured details for: '{pregunta_str}'")


        # Main loop to gather information step-by-step
        # This loop will advance the state if a field is filled (either by LLM or user input)
        # and then re-evaluate. If all fields are filled, it will eventually reach ESPERANDO_CONFIRMACION_RECLAMO.
        MAX_ITERATIONS_RECLAMO_LOOP = 7 # Safety break for the while loop
        iterations_count = 0
        while iterations_count < MAX_ITERATIONS_RECLAMO_LOOP :
            iterations_count += 1
            current_state_for_logic = estado # estado is already an Enum here

            logger.debug(f"[ReclamoHandler.while_loop_iter_{iterations_count}] Processing state: {current_state_for_logic.name if current_state_for_logic else 'None'}. Pregunta actual en bucle: '{pregunta_str[:50]}...'")

            # 1. ESPERANDO_CATEGORIA_RECLAMO
            if current_state_for_logic == ConversationState.ESPERANDO_CATEGORIA_RECLAMO:
                categoria_desde_input = None
                # Definir una lista de IDs de acción que no son categorías y no deben usarse para sugerencias
                action_ids_no_categoria = ["iniciarreclamo", "hacer_reclamo", "hacer un reclamo"]
                texto_normalizado_pregunta_actual = normalizar_texto(pregunta_str)

                is_action_id_input = texto_normalizado_pregunta_actual in action_ids_no_categoria

                if pregunta_str and not is_action_id_input:  # Intentar extraer categoría solo si pregunta_str no es un ID de acción
                    # texto_normalizado_cat es el mismo que texto_normalizado_pregunta_actual
                    if texto_normalizado_pregunta_actual in categorias_normalizadas:
                        idx = categorias_normalizadas.index(texto_normalizado_pregunta_actual)
                        categoria_desde_input = CATEGORIAS_RECLAMO[idx]
                    else:
                        from difflib import get_close_matches
                        matches = get_close_matches(texto_normalizado_pregunta_actual, categorias_normalizadas, n=1, cutoff=0.7)
                        if matches:
                            idx = categorias_normalizadas.index(matches[0])
                            categoria_desde_input = CATEGORIAS_RECLAMO[idx]

                    if not categoria_desde_input: # Try LLM if keyword/fuzzy failed for current input
                        try:
                            respuesta_llm_cat = _clasificar_intencion_con_llm(pregunta_str, opciones=CATEGORIAS_RECLAMO, tipo="categoría")
                            if respuesta_llm_cat and respuesta_llm_cat in CATEGORIAS_RECLAMO:
                                categoria_desde_input = respuesta_llm_cat
                        except Exception: pass

                if categoria_desde_input:
                    memoria["categoria_reclamo"] = categoria_desde_input
                    logger.info(f"[ReclamoHandler] Categoría establecida/actualizada a: {categoria_desde_input} desde input '{pregunta_str}'.")
                    memoria["estado_conversacion"] = ConversationState.ESPERANDO_DIRECCION_RECLAMO.name
                    estado = ConversationState.ESPERANDO_DIRECCION_RECLAMO
                    if all(memoria.get(fld) for fld in ["direccion_reclamo", "nombre_vecino", "telefono_vecino", "email_vecino", "descripcion_reclamo"]):
                        memoria["estado_conversacion"] = ConversationState.ESPERANDO_CONFIRMACION_RECLAMO.name
                        estado = ConversationState.ESPERANDO_CONFIRMACION_RECLAMO
                    pregunta_str = ""
                    continue

                elif memoria.get("categoria_reclamo"):
                    logger.debug(f"[ReclamoHandler] Categoría ya en memoria: '{memoria['categoria_reclamo']}' y no se actualizó con input actual ('{pregunta_str}'). Avanzando.")
                    memoria["estado_conversacion"] = ConversationState.ESPERANDO_DIRECCION_RECLAMO.name
                    estado = ConversationState.ESPERANDO_DIRECCION_RECLAMO
                    if all(memoria.get(fld) for fld in ["direccion_reclamo", "nombre_vecino", "telefono_vecino", "email_vecino", "descripcion_reclamo"]):
                        memoria["estado_conversacion"] = ConversationState.ESPERANDO_CONFIRMACION_RECLAMO.name
                        estado = ConversationState.ESPERANDO_CONFIRMACION_RECLAMO
                    continue

                else: # Pedir categoría (porque no se extrajo, no estaba en memoria, o el input era un ID de acción)
                    mensaje_adjunto = memoria.pop("mensaje_adjunto_recibido", "")

                    # Usar descripción_reclamo (si existe, ej. de análisis de imagen) para sugerir categorías,
                    # o nada si el input fue un ID de acción.
                    texto_para_sugerir_categorias = memoria.get("descripcion_reclamo", "")
                    if pregunta_str and not is_action_id_input: # Si el usuario escribió algo que no es ID de acción
                        texto_para_sugerir_categorias = pregunta_str

                    sugeridas_data = sugerir_categorias_relevantes(texto_para_sugerir_categorias)
                    options_data = sugeridas_data if (sugeridas_data and len(sugeridas_data) > 0) else CATEGORIAS_RECLAMO
                    options = [{"id": normalizar_texto(c), "texto": c.title()} for c in options_data]
                    if not any(opt['id'] == "otro motivo" for opt in options) and "otro motivo" in CATEGORIAS_RECLAMO:
                        options.append({"id": "otro motivo", "texto": "Otro Motivo"})


                    respuesta_texto = ""
                    # Si el input fue un ID de acción o no hubo input (ej. imagen sola), pedir categoría directamente.
                    if is_action_id_input or not pregunta_str.strip():
                        if sugeridas_data:
                            respuesta_texto = f"{mensaje_adjunto}Detecté que podría ser sobre algunos de estos temas. Para tu reclamo, ¿cuál sería la categoría?"
                        else:
                            respuesta_texto = f"{mensaje_adjunto}Para tu reclamo, ¿podrías ayudarme seleccionando una categoría, o describiendo brevemente de qué se trata?"
                    else: # El input no fue ID de acción, no se reconoció como categoría, y no estaba vacío.
                        respuesta_texto = f"{mensaje_adjunto}¡Ups! No reconocí '{pregunta_str}' como una categoría válida."
                        if sugeridas_data:
                             respuesta_texto += " Quizás quisiste decir alguna de estas:"
                        else:
                             respuesta_texto += " Por favor, elegí una de las siguientes opciones o describila mejor:"

                    message_type = 'interactive_list' if len(options) > 3 else 'interactive_buttons'
                    if len(options) > 10: logger.warning(f"ReclamoHandler Esperando Categoria: Too many options ({len(options)}) for WhatsApp list.")
                    return {"message_body": respuesta_texto, "options_list": options, "message_type": message_type, "fuente": "solicitud_categoria_reclamo_interactivo_v3"}

            # 2. ESPERANDO_DIRECCION_RECLAMO
            elif current_state_for_logic == ConversationState.ESPERANDO_DIRECCION_RECLAMO:
                if memoria.get("direccion_reclamo"): # Address already known
                    logger.debug(f"[ReclamoHandler] Dirección ya en memoria: '{memoria['direccion_reclamo']}'. Avanzando.")
                    memoria["estado_conversacion"] = ConversationState.ESPERANDO_NOMBRE_VECINO.name; estado = ConversationState.ESPERANDO_NOMBRE_VECINO
                    if all(memoria.get(campo) for campo in ["nombre_vecino", "telefono_vecino", "email_vecino", "descripcion_reclamo"]):
                        logger.info("[ReclamoHandler] Dirección y todos los demás datos ya en memoria. Saltando a confirmación.")
                        memoria["estado_conversacion"] = ConversationState.ESPERANDO_CONFIRMACION_RECLAMO.name
                        estado = ConversationState.ESPERANDO_CONFIRMACION_RECLAMO
                    pregunta_str = ""
                    continue

                # Address not known yet.
                if pregunta_str: # User has provided some input, try to parse it as address
                    if es_pregunta_nueva(pregunta_str, "una dirección", memoria):
                        logger.info(f"[ReclamoHandler] '{pregunta_str}' detectada como pregunta nueva. Limpiando reclamo.")
                        for key in list(memoria.keys()):
                            if key.endswith(('_reclamo', '_vecino')) or key in ['foto_url', 'ubicacion_gps', 'direccion_estructurada_reclamo']: memoria.pop(key, None)
                        memoria["estado_conversacion"] = None; self.context["intencion"] = None; return None
                
                    if payload.get("es_foto") and payload.get("es_ubicacion"):
                        # If user sent a photo/location when address was expected.
                        categoria_mem_for_msg = memoria.get('categoria_reclamo', 'el reclamo')
                        cat_title_for_msg = categoria_mem_for_msg.title() if isinstance(categoria_mem_for_msg, str) else "El Reclamo"
                        # Mantener botones para compartir GPS si es relevante
                        options_adj_gps = []
                        allow_gps_for_this_user_adj = True
                        if self.context.get("anon_id") and not self.context.get("cliente_id"):
                            if has_app_context() and not current_app.config.get("ALLOW_ANON_GPS", False):
                                allow_gps_for_this_user_adj = False
                        if allow_gps_for_this_user_adj:
                            options_adj_gps.append({"id": "accion_compartir_ubicacion_adj", "texto": "📍 Compartir Ubicación GPS"})

                        return {
                            "message_body": f"Entendido lo del adjunto. Para el reclamo de **{cat_title_for_msg}**, primero necesito la dirección escrita del problema (ej. 'Av. San Martín 123'). ¿Me la decís?",
                            "options_list": options_adj_gps,
                            "message_type": 'interactive_buttons' if options_adj_gps else 'text',
                            "fuente": "reclamo_ack_adjunto_pide_direccion_v2"
                        }

                    logger.info(f"[ReclamoHandler] Estado: ESPERANDO_DIRECCION_RECLAMO. Input: '{pregunta_str}'.")
                    # La lógica de procesar pregunta_str como dirección textual se mueve más abajo,
                    # para ser usada si no se pide la dirección por primera vez o si no es GPS.

                # Si llegamos aquí, es porque:
                # 1. No había ubicación GPS en el payload actual que se procesó arriba.
                # 2. No había dirección en memoria (ya que el `if memoria.get("direccion_reclamo")` no continuó).
                # 3. O bien pregunta_str estaba vacía (hay que pedir dirección con opciones),
                #    o pregunta_str tenía texto pero aún no se ha procesado como dirección textual en este paso.

                if not pregunta_str: # Solo mostrar opciones si no hay texto que procesar (primera vez que se pide dir)
                    categoria_mem = memoria.get('categoria_reclamo', '')
                    cat_title = categoria_mem.title() if categoria_mem and isinstance(categoria_mem, str) else "el reclamo"
                    # Usar mensaje_adjunto_recibido si fue establecido al inicio del handler
                    ack_adjunto = memoria.get("mensaje_adjunto_recibido", "")

                    body_pedir_direccion = f"{ack_adjunto}Entendido, categoría: **{cat_title}**. Ahora, ¿la **dirección exacta** del problema, por favor?\n(Ej: {EJEMPLO_DIRECCION}, Localidad). También podés compartir tu ubicación GPS."

                    options_pedir_direccion = []
                    allow_gps_for_this_user = True
                    if self.context.get("anon_id") and not self.context.get("cliente_id"):
                        if has_app_context() and not current_app.config.get("ALLOW_ANON_GPS", False):
                            allow_gps_for_this_user = False
                            body_pedir_direccion += "\n(Para compartir GPS necesitarás estar registrado)."

                    if allow_gps_for_this_user:
                         options_pedir_direccion.append({"id": "accion_compartir_ubicacion", "texto": "📍 Compartir Ubicación GPS"})

                    return {
                        "message_body": body_pedir_direccion,
                        "options_list": options_pedir_direccion,
                        "message_type": 'interactive_buttons' if options_pedir_direccion else 'text',
                        "fuente": "reclamo_pedir_direccion_con_opcion_gps_v2"
                    }
                else: # pregunta_str tiene texto, procesarlo como dirección (lógica original adaptada)
                    config_muni_parseo = self.context.get("municipio_config") or CONFIG_MUNICIPIO
                    parsed_address = parse_direccion_completa(pregunta_str, config_muni_parseo)

                    if parsed_address and parsed_address.get("calle") and parsed_address.get("localidad"):
                        memoria["direccion_estructurada_reclamo"] = parsed_address
                        dir_confirm_text = f"{parsed_address['calle']} {parsed_address.get('numero', '')}, {parsed_address['localidad']}".replace(" ,",",").strip()
                        memoria["direccion_reclamo"] = dir_confirm_text
                        logger.info(f"[ReclamoHandler] Dirección guardada: {dir_confirm_text}.")
                        memoria["estado_conversacion"] = ConversationState.ESPERANDO_NOMBRE_VECINO.name
                        estado = ConversationState.ESPERANDO_NOMBRE_VECINO
                        if all(memoria.get(campo) for campo in ["nombre_vecino", "telefono_vecino", "email_vecino", "descripcion_reclamo"]):
                             memoria["estado_conversacion"] = ConversationState.ESPERANDO_CONFIRMACION_RECLAMO.name
                             estado = ConversationState.ESPERANDO_CONFIRMACION_RECLAMO
                        if memoria.get("nombre_vecino"):
                            pregunta_str = ""
                            continue
                        else:
                            return {"message_body": f"¡Perfecto! Dirección registrada como: **{memoria['direccion_reclamo']}**. Ahora, ¿podrías decirme tu **nombre completo**?", "options_list": [], "message_type": "text", "fuente": "reclamo_direccion_ok_pide_nombre_v2"}
                    else: # Dirección textual no válida
                        respuesta_dir_inv = f"La dirección '{pregunta_str}' no parece completa o válida. ¿Podrías verificarla? Necesito algo como '{EJEMPLO_DIRECCION}, Localidad'."
                        options_dir_inv = []
                        allow_gps_for_this_user_invalida = True
                        if self.context.get("anon_id") and not self.context.get("cliente_id"):
                            if has_app_context() and not current_app.config.get("ALLOW_ANON_GPS", False):
                                allow_gps_for_this_user_invalida = False
                                respuesta_dir_inv += "\n(Para compartir GPS necesitarás estar registrado)."

                        if allow_gps_for_this_user_invalida:
                            respuesta_dir_inv += "\nTambién podés intentar compartir tu ubicación GPS."
                            options_dir_inv.append({"id": "accion_compartir_ubicacion_reintento", "texto": "📍 Compartir Ubicación GPS"})

                        return {
                            "message_body": respuesta_dir_inv,
                            "options_list": options_dir_inv,
                            "message_type": 'interactive_buttons' if options_dir_inv else 'text',
                            "fuente": "reclamo_direccion_invalida_con_opcion_gps_v2"
                        }

            # 3. ESPERANDO_NOMBRE_VECINO
            elif current_state_for_logic == ConversationState.ESPERANDO_NOMBRE_VECINO:
                if memoria.get("nombre_vecino"):
                    logger.debug(f"[ReclamoHandler] Nombre ya en memoria: '{memoria['nombre_vecino']}'. Avanzando.")
                    memoria["estado_conversacion"] = ConversationState.ESPERANDO_TELEFONO_VECINO.name; estado = ConversationState.ESPERANDO_TELEFONO_VECINO
                    if all(memoria.get(campo) for campo in ["telefono_vecino", "email_vecino", "descripcion_reclamo"]):
                        memoria["estado_conversacion"] = ConversationState.ESPERANDO_CONFIRMACION_RECLAMO.name
                        estado = ConversationState.ESPERANDO_CONFIRMACION_RECLAMO
                    pregunta_str = ""
                    continue

                if not pregunta_str: # If pregunta_str is empty, we must ask for the name
                     return {"respuesta": "Para continuar, necesitaría tu **nombre y apellido**. ¿Podrías ingresarlos?"}

                # Check for new question only if pregunta_str is not empty
                if es_pregunta_nueva(pregunta_str, "tu nombre completo", memoria):
                    logger.info(f"[ReclamoHandler] '{pregunta_str}' detectada como pregunta nueva mientras se esperaba nombre. Limpiando reclamo.")
                    # Conservar datos ya recolectados si es posible, solo limpiar estado para re-clasificar intención
                    # memoria.clear() # Demasiado agresivo, comentado para prueba
                    memoria.pop("estado_conversacion", None) # Limpiar solo el estado para permitir re-clasificación
                    self.context["intencion"] = None # Forzar re-clasificación de intención
                    # No retornar None inmediatamente, dejar que la cadena de handlers intente clasificar la "nueva pregunta"
                    # Si nada más lo maneja, el fallback general actuará.
                    # Esto es un cambio de estrategia para preservar datos.
                    logger.warning(f"[ReclamoHandler] Intento de pregunta nueva '{pregunta_str}' en ESPERANDO_NOMBRE_VECINO. Se limpió estado y se intentará reclasificar. Datos en memoria: {memoria}")
                    # Return None to allow IntentClassifier to run again.
                    # This means the current ReclamoHandler pass stops here.
                    return None


                nombre_input = pregunta_str.strip()
                logger.info(f"[ReclamoHandler] Estado: ESPERANDO_NOMBRE_VECINO. Input: '{nombre_input}'.")
                
                config_muni_parseo_nombre = self.context.get("municipio_config") or CONFIG_MUNICIPIO
                potential_address_parts = parse_direccion_completa(nombre_input, config_muni_parseo_nombre)
                
                # NUEVA LÓGICA DE DESAMBIGUACIÓN:
                is_actually_a_name = False
                if potential_address_parts and isinstance(potential_address_parts, dict):
                    parsed_calle = potential_address_parts.get("calle")
                    parsed_numero = potential_address_parts.get("numero")

                    if parsed_calle == nombre_input and \
                       not any(char.isdigit() for char in parsed_calle) and \
                       parsed_numero is None:

                        other_significant_details = False
                        if potential_address_parts.get("otros_detalles") and \
                           any(kword in potential_address_parts["otros_detalles"].lower() for kword in ["esquina", "entre", "frente a", "piso", "depto", "departamento"]):
                            other_significant_details = True

                        # Considerar solo las claves que NO son las esperadas como defaults o calle/numero(None)
                        # Si solo quedan defaults (localidad, provincia) o ninguna otra clave significativa, es un nombre.
                        keys_in_parsed_with_values = {k for k, v in potential_address_parts.items() if v is not None}
                        non_default_or_simple_street_keys = keys_in_parsed_with_values - {"calle", "localidad", "provincia", "numero"}

                        if not other_significant_details and not non_default_or_simple_street_keys:
                             is_actually_a_name = True
                             logger.info(f"[ReclamoHandler] Heurística: Input '{nombre_input}' parece nombre a pesar de parseo con defaults. Parsed: {potential_address_parts}")

                if not is_actually_a_name:
                    is_likely_address = False
                    if potential_address_parts and isinstance(potential_address_parts, dict):
                        has_street = bool(potential_address_parts.get("calle"))
                        has_number = bool(potential_address_parts.get("numero"))
                        has_locality = bool(potential_address_parts.get("localidad"))
                        has_province = bool(potential_address_parts.get("provincia"))

                        if has_street and (has_number or has_locality):
                            is_likely_address = True
                        elif has_locality and has_province and len(nombre_input.split()) >= 2:
                            is_likely_address = True
                        elif has_street and has_province and len(nombre_input.split()) >= 2: # e.g. "Calle Falsa, Mendoza"
                            is_likely_address = True
                        elif has_street and not (has_number or has_locality or has_province):
                            common_street_indicators = ["calle", "avenida", "avda", "av.", "av ", "pasaje", "psje", "ruta", "bv.", "bv ", "bulevar", "diag.", "diag ", "diagonal"]
                            normalized_input_lower = nombre_input.lower()
                            if any(normalized_input_lower.startswith(indicator) for indicator in common_street_indicators):
                                is_likely_address = True
                            elif potential_address_parts.get("calle") and any(indicator in potential_address_parts.get("calle").lower() for indicator in common_street_indicators):
                                 is_likely_address = True
                            elif potential_address_parts.get("calle") and any(char.isdigit() for char in potential_address_parts.get("calle")):
                                is_likely_address = True
                            elif len(potential_address_parts) == 1 and potential_address_parts.get("calle") == nombre_input and not any(char.isdigit() for char in nombre_input):
                                 is_likely_address = False

                        # Adicional: si tiene "otros_detalles" como "esquina", "piso", "depto", es probable dirección
                        if potential_address_parts.get("otros_detalles") and \
                           any(kword in potential_address_parts["otros_detalles"].lower() for kword in ["esquina", "entre", "frente a", "piso", "depto", "departamento"]):
                            if has_street : # solo si tambien tiene calle
                                is_likely_address = True


                    if is_likely_address: # Solo si la lógica original lo marca como dirección
                        logger.warning(f"[ReclamoHandler] Input '{nombre_input}' para NOMBRE parece una dirección (lógica original). Parsed: {potential_address_parts}. Repreguntando nombre.")
                        return {"respuesta": "Estaba esperando tu nombre y apellido, pero parece que ingresaste una dirección. ¿Podrías decirme tu nombre, por favor?"}

                # Si is_actually_a_name es True, O si la lógica original de is_likely_address resultó False,
                # entonces NO se considera dirección y continúa el flujo normal.
                
                if validar_telefono(nombre_input):
                    logger.warning(f"[ReclamoHandler] Input '{nombre_input}' para NOMBRE parece un teléfono. Repreguntando nombre sin perder contexto.")
                    return {"respuesta": "Estaba esperando tu nombre y apellido, pero eso parece un número de teléfono. ¿Podrías decírmelos, por favor?"}

                cleaned_name = nombre_input
                prefixes_to_remove = [
                    "es un nombre y un apellido real.. ",
                    "mi nombre completo es ", # More specific
                    "mi nombre es ",
                    "me llamo ",
                    "soy ",
                    "me dicen ",
                    "puede llamarme ",
                    "registrame como ",
                ]
                temp_cleaned_name = cleaned_name.lower() # For matching prefixes
                for prefix in prefixes_to_remove:
                    if temp_cleaned_name.startswith(prefix):
                        cleaned_name = cleaned_name[len(prefix):].strip()
                        break # Remove only the first matching prefix
                
                # Further clean common interjections if they are now at the start
                interjections_to_remove_at_start = ["bueno ", "dale ", "ok ", "listo "]
                temp_cleaned_name_for_interjection = cleaned_name.lower()
                for interjection in interjections_to_remove_at_start:
                    if temp_cleaned_name_for_interjection.startswith(interjection):
                        cleaned_name = cleaned_name[len(interjection):].strip()
                        break


                if not cleaned_name or len(cleaned_name.split()) < 1 or len(cleaned_name) < 2: # Added min length for name
                    return {"respuesta": "Para continuar, necesitaría tu **nombre y apellido** (o al menos un nombre válido). ¿Podrías ingresarlos?"}

                memoria["nombre_vecino"] = cleaned_name 
                logger.info(f"[ReclamoHandler] Nombre guardado (cleaned): '{cleaned_name}'.")
                
                # Prepare greeting name (first word of cleaned name)
                greeting_name = cleaned_name.split()[0] if cleaned_name else "tú"


                if all(memoria.get(campo) for campo in ["telefono_vecino", "email_vecino", "descripcion_reclamo"]):
                    memoria["estado_conversacion"] = ConversationState.ESPERANDO_CONFIRMACION_RECLAMO.name
                    estado = ConversationState.ESPERANDO_CONFIRMACION_RECLAMO
                else:
                    memoria["estado_conversacion"] = ConversationState.ESPERANDO_TELEFONO_VECINO.name
                    estado = ConversationState.ESPERANDO_TELEFONO_VECINO
                
                if pregunta_str == payload.get("pregunta",""): # Check if the original input was processed for this step
                    if memoria.get("telefono_vecino"): 
                        pregunta_str = "" 
                        continue
                    else:
                        # Use the cleaned first name for the greeting
                        return {"respuesta": f"¡Gracias, {greeting_name.title()}! Ahora, ¿me pasarías tu **número de teléfono con código de área**?"}
                pregunta_str = "" 
                continue
            
            # 4. ESPERANDO_TELEFONO_VECINO
            elif current_state_for_logic == ConversationState.ESPERANDO_TELEFONO_VECINO:
                if memoria.get("telefono_vecino") and not pregunta_str: # Phone in memory and no new input for this field
                    logger.debug(f"[ReclamoHandler] Teléfono ya en memoria: '{memoria['telefono_vecino']}'. Avanzando.")
                    memoria["estado_conversacion"] = ConversationState.ESPERANDO_EMAIL_VECINO.name
                    estado = ConversationState.ESPERANDO_EMAIL_VECINO
                    if all(memoria.get(campo) for campo in ["email_vecino", "descripcion_reclamo"]):
                        memoria["estado_conversacion"] = ConversationState.ESPERANDO_CONFIRMACION_RECLAMO.name
                        estado = ConversationState.ESPERANDO_CONFIRMACION_RECLAMO
                    # No 'pregunta_str' to consume, so just continue
                    continue
                
                if pregunta_str and es_pregunta_nueva(pregunta_str, "tu número de teléfono", memoria):
                    logger.info(f"[ReclamoHandler] '{pregunta_str}' detectada como pregunta nueva en ESPERANDO_TELEFONO_VECINO. Limpiando reclamo.")
                    for key in list(memoria.keys()):
                        if key.endswith(('_reclamo', '_vecino')) or key in ['foto_url', 'ubicacion_gps', 'direccion_estructurada_reclamo']: memoria.pop(key, None)
                    memoria["estado_conversacion"] = None; self.context["intencion"] = None
                    return None # Allow IntentClassifier to re-run

                processed_phone_input = ""
                phone_source = ""

                if pregunta_str: # Only process if there's new input
                    extracted_details = extract_multiple_contact_details_llm(pregunta_str, ["telefono_cliente", "email_cliente"]) # Try to get email too
                    if extracted_details and extracted_details.get("telefono_cliente"):
                        processed_phone_input = extracted_details["telefono_cliente"].strip()
                        phone_source = "LLM"
                        logger.info(f"[ReclamoHandler] Teléfono extraído por LLM para estado TELEFONO ('{pregunta_str}'): '{processed_phone_input}'")
                        # If LLM also extracted email and it's not already in memoria, save it
                        if extracted_details.get("email_cliente") and not memoria.get("email_vecino"):
                            llm_email = extracted_details["email_cliente"].strip()
                            if validar_email(llm_email):
                                memoria["email_vecino"] = llm_email.lower()
                                logger.info(f"[ReclamoHandler] Email también extraído por LLM en TELEFONO: '{memoria['email_vecino']}'")
                    else:
                        processed_phone_input = pregunta_str.strip()
                        phone_source = "direct input"
                    
                    logger.info(f"[ReclamoHandler] Estado: ESPERANDO_TELEFONO_VECINO. Procesando '{processed_phone_input}' (fuente: {phone_source}).")
                    if validar_telefono(processed_phone_input):
                        telefono_normalizado = formatear_telefono_e164(processed_phone_input)
                        memoria["telefono_vecino"] = telefono_normalizado
                        logger.info(f"[ReclamoHandler] Teléfono guardado (normalizado E.164): '{telefono_normalizado}'.")
                        
                        # Determine next state
                        if memoria.get("email_vecino"): # Check if email is now filled (either previously or by LLM in this step)
                            if memoria.get("descripcion_reclamo"):
                                memoria["estado_conversacion"] = ConversationState.ESPERANDO_CONFIRMACION_RECLAMO.name
                                estado = ConversationState.ESPERANDO_CONFIRMACION_RECLAMO
                            else:
                                memoria["estado_conversacion"] = ConversationState.ESPERANDO_DESCRIPCION_RECLAMO.name
                                estado = ConversationState.ESPERANDO_DESCRIPCION_RECLAMO
                        else: # Email still missing
                            memoria["estado_conversacion"] = ConversationState.ESPERANDO_EMAIL_VECINO.name
                            estado = ConversationState.ESPERANDO_EMAIL_VECINO

                        pregunta_str = "" # Consumed current input

                        # Ask for the next piece of information or break to confirm
                        if estado == ConversationState.ESPERANDO_EMAIL_VECINO:
                             return {"respuesta": "¡Excelente! Casi terminamos. ¿Cuál es tu **dirección de correo electrónico**?"}
                        elif estado == ConversationState.ESPERANDO_DESCRIPCION_RECLAMO:
                             return {"respuesta": "¡Bárbaro! Ahora, por favor, contame con un poco más de detalle **cuál es el problema**. Luego podrás adjuntar foto/ubicación si querés."}
                        elif estado == ConversationState.ESPERANDO_CONFIRMACION_RECLAMO:
                            break # All data filled, exit while loop
                        else: # Should not happen
                            continue
                    else: # Phone input (either from LLM or direct) is not valid
                        error_msg = f"El teléfono '{processed_phone_input}' no parece válido. ¿Podrías revisarlo (solo números con código de área)?"
                        if phone_source == "LLM" and pregunta_str != processed_phone_input:
                             error_msg = f"Entendí que tu teléfono podría ser '{processed_phone_input}', pero no parece válido. ¿Podrías ingresarlo nuevamente (solo números con código de área)?"
                        
                        config_muni_parseo_tel = self.context.get("municipio_config") or CONFIG_MUNICIPIO
                        if parse_direccion_completa(processed_phone_input, config_muni_parseo_tel) and len(processed_phone_input.split()) > 1:
                            return {"message_body": "Estaba esperando un teléfono, pero eso parece una dirección. ¿Tu teléfono, por favor?", "options_list": [], "message_type": "text", "fuente": "reclamo_telefono_como_direccion_v2"}
                        if validar_email(processed_phone_input): # Check if it was an email
                             return {"message_body": "Estaba esperando un teléfono, pero eso parece un email. ¿Tu número de teléfono, por favor?", "options_list": [], "message_type": "text", "fuente": "reclamo_telefono_como_email_v2"}
                        return {"message_body": error_msg, "options_list": [], "message_type": "text", "fuente": "reclamo_telefono_invalido_v3"} # Re-prompt for phone
                else: # No pregunta_str, must ask for phone
                     nombre_mem = memoria.get("nombre_vecino", "tú").split(" ")[0].title()
                     return {"message_body": f"¡Gracias, {nombre_mem}! Ahora, ¿me pasarías tu **número de teléfono con código de área**?", "options_list": [], "message_type": "text", "fuente": "reclamo_pedir_telefono_v3"}

            # 5. ESPERANDO_EMAIL_VECINO
            elif current_state_for_logic == ConversationState.ESPERANDO_EMAIL_VECINO:
                if memoria.get("email_vecino") and not pregunta_str: # Email in memory and no new input for this field
                    logger.debug(f"[ReclamoHandler] Email ya en memoria: '{memoria['email_vecino']}'. Avanzando.")
                    memoria["estado_conversacion"] = ConversationState.ESPERANDO_DESCRIPCION_RECLAMO.name
                    estado = ConversationState.ESPERANDO_DESCRIPCION_RECLAMO
                    if memoria.get("descripcion_reclamo"):
                        memoria["estado_conversacion"] = ConversationState.ESPERANDO_CONFIRMACION_RECLAMO.name
                        estado = ConversationState.ESPERANDO_CONFIRMACION_RECLAMO
                    # No 'pregunta_str' to consume, so just continue to re-evaluate loop with new state
                    continue

                if pregunta_str and es_pregunta_nueva(pregunta_str, "tu correo electrónico", memoria):
                    logger.info(f"[ReclamoHandler] '{pregunta_str}' detectada como pregunta nueva en ESPERANDO_EMAIL_VECINO. Limpiando reclamo.")
                    for key in list(memoria.keys()):
                        if key.endswith(('_reclamo', '_vecino')) or key in ['foto_url', 'ubicacion_gps', 'direccion_estructurada_reclamo']: memoria.pop(key, None)
                    memoria["estado_conversacion"] = None; self.context["intencion"] = None
                    return None # Allow IntentClassifier to re-run

                processed_email_input = ""
                email_source = ""

                if pregunta_str: # Only process if there's new input from the user for this turn
                    # Try LLM on the current input, focusing on email
                    extracted_details = extract_multiple_contact_details_llm(pregunta_str, ["email_cliente"])
                    if extracted_details and extracted_details.get("email_cliente"):
                        processed_email_input = extracted_details["email_cliente"].strip()
                        email_source = "LLM"
                        logger.info(f"[ReclamoHandler] Email extraído por LLM ('{pregunta_str}'): '{processed_email_input}'")
                        # Potentially save other details extracted by LLM if any
                        # (This part should be a common function to update memoria from LLM results)
                    else:
                        processed_email_input = pregunta_str.strip()
                        email_source = "direct input"
                    
                    logger.info(f"[ReclamoHandler] Estado: ESPERANDO_EMAIL_VECINO. Procesando '{processed_email_input}' (fuente: {email_source}).")
                    if validar_email(processed_email_input):
                        email_normalizado = processed_email_input.lower()
                        memoria["email_vecino"] = email_normalizado
                        logger.info(f"[ReclamoHandler] Email guardado (normalizado): '{email_normalizado}'.")
                        
                        # Determine next state
                        if memoria.get("descripcion_reclamo"):
                            memoria["estado_conversacion"] = ConversationState.ESPERANDO_CONFIRMACION_RECLAMO.name
                            estado = ConversationState.ESPERANDO_CONFIRMACION_RECLAMO
                        else:
                            memoria["estado_conversacion"] = ConversationState.ESPERANDO_DESCRIPCION_RECLAMO.name
                            estado = ConversationState.ESPERANDO_DESCRIPCION_RECLAMO
                        
                        pregunta_str = "" # Consumed current input

                        # Ask for the next piece of information or break to confirm
                        if estado == ConversationState.ESPERANDO_DESCRIPCION_RECLAMO: # This means description is missing
                             return {"respuesta": "¡Bárbaro! Ahora, por favor, contame con un poco más de detalle **cuál es el problema**. Luego podrás adjuntar foto/ubicación si querés."}
                        elif estado == ConversationState.ESPERANDO_CONFIRMACION_RECLAMO: # All data filled
                            break # Exit while loop to proceed to confirmation logic below
                        else: # Should not happen if logic is correct, but safeguard
                            continue # Re-evaluate loop for next state.
                    else: # Email input (either from LLM or direct) is not valid
                        error_msg = f"El correo electrónico '{processed_email_input}' no parece tener el formato correcto. ¿Podrías revisarlo?"
                        if email_source == "LLM" and pregunta_str != processed_email_input: 
                            error_msg = f"Entendí que tu email podría ser '{processed_email_input}', pero no parece tener el formato correcto. ¿Podrías ingresarlo nuevamente?"
                        
                        # Check for common misinterpretations again before generic error
                        config_muni_parseo_email = self.context.get("municipio_config") or CONFIG_MUNICIPIO
                        if parse_direccion_completa(processed_email_input, config_muni_parseo_email) and len(processed_email_input.split()) > 1 :
                             return {"message_body": "Estaba esperando un email, pero eso parece una dirección. ¿Tu email, por favor?", "options_list": [], "message_type": "text", "fuente": "reclamo_email_como_direccion_v2"}
                        if validar_telefono(processed_email_input):
                             return {"message_body": "Estaba esperando un email, pero eso parece un teléfono. ¿Tu email, por favor?", "options_list": [], "message_type": "text", "fuente": "reclamo_email_como_telefono_v2"}
                        return {"message_body": error_msg, "options_list": [], "message_type": "text", "fuente": "reclamo_email_invalido_v3"} # Re-prompt for email
                else: # No pregunta_str, must ask for email
                    # This path is taken if previous state advanced to ESPERANDO_EMAIL_VECINO and pregunta_str was consumed/empty
                    nombre_mem = memoria.get("nombre_vecino", "tú").split(" ")[0].title()
                    return {"message_body": f"¡Excelente {nombre_mem}! Casi terminamos. ¿Cuál es tu **dirección de correo electrónico**?", "options_list": [], "message_type": "text", "fuente": "reclamo_pedir_email_v3"}
            
            # 6. ESPERANDO_DESCRIPCION_RECLAMO
            elif current_state_for_logic == ConversationState.ESPERANDO_DESCRIPCION_RECLAMO:
                if memoria.get("descripcion_reclamo"):
                    logger.debug(f"[ReclamoHandler] Descripción ya en memoria: '{memoria['descripcion_reclamo'][:50]}...'. Avanzando a adjuntos.")
                    memoria["estado_conversacion"] = ConversationState.ESPERANDO_ADJUNTOS_RECLAMO.name; estado = ConversationState.ESPERANDO_ADJUNTOS_RECLAMO
                    # If desc was pre-filled by LLM, and the current input (pregunta_str) is different,
                    # it means we've advanced state based on pre-fill. We shouldn't ask for adjuntos yet
                    # if the current input was the one that filled the description.
                    # So, only 'continue' if the current pregunta_str is NOT what filled the description.
                    if memoria.get("descripcion_reclamo") != pregunta_str.strip():
                        pregunta_str = "" # Clear to avoid processing for adjuntos in this turn
                        continue
                    # If desc was filled by current input, fall through to ask about adjuntos.

                if pregunta_str and es_pregunta_nueva(pregunta_str, "una descripción del problema", memoria):
                    logger.info(f"[ReclamoHandler] '{pregunta_str}' detectada como pregunta nueva. Limpiando reclamo."); # ... (clear logic)
                    for key in list(memoria.keys()):
                        if key.endswith(('_reclamo', '_vecino')) or key in ['foto_url', 'ubicacion_gps', 'direccion_estructurada_reclamo']: memoria.pop(key, None)
                    memoria["estado_conversacion"] = None; self.context["intencion"] = None; return None

                descripcion_final = ""
                # ... (logic for getting description from payload['datos'] or pregunta_str)
                descripcion_input = ""
                datos_sub_payload = payload.get("datos", {}); campo_descripcion = "descripcion_reclamo"

                if campo_descripcion in datos_sub_payload and isinstance(datos_sub_payload[campo_descripcion], str) and len(datos_sub_payload[campo_descripcion].strip()) >= 10:
                    descripcion_input = datos_sub_payload[campo_descripcion].strip()
                elif pregunta_str:
                    # Check if pregunta_str is likely a button ID or too generic
                    normalized_input_desc = normalizar_texto(pregunta_str)
                    # Combine known confirmation/edit keywords with typical action prefixes
                    KNOWN_BUTTON_LIKE_PHRASES = PALABRAS_CLAVE_CONFIRMACION.union(EDIT_KEYWORDS).union({
                        "adjuntar_foto", "compartir_ubicacion", "sin_adjuntos", 
                        "confirmarreclamofinal", "editarreclamodatos", "arreglodecalle", "iniciarreclamo" # Added common action IDs
                    })
                    
                    is_likely_button_id_or_action = normalized_input_desc in KNOWN_BUTTON_LIKE_PHRASES or \
                                                 (any(btn_id_part in normalized_input_desc for btn_id_part in [
                                                     "confirmar", "editar", "adjuntar", "seleccionar", "opcion", # Generic button actions
                                                     "reclamo", "calle", "arbol", "agua", "luz", "limpieza", "iniciar" # Keywords often in button IDs/short commands
                                                     ]) and len(normalized_input_desc.split()) <= 3) # Short phrases

                    if not is_likely_button_id_or_action:
                        descripcion_input = pregunta_str.strip()
                    else:
                        logger.warning(f"Input '{pregunta_str}' for description seems like a button ID/action or too generic. Normalized: '{normalized_input_desc}'. Re-prompting.")
                        # descripcion_input remains empty, will trigger re-prompt below
                
                if not descripcion_input or len(descripcion_input) < 10: # Min length for a meaningful description
                    return {"respuesta": "Para entender mejor, necesitaría una breve **descripción del problema**. ¿Podrías contarme más?"}
                
                memoria["descripcion_reclamo"] = descripcion_input
                logger.info(f"Descripción guardada: '{descripcion_input[:50]}...'.")
                memoria["estado_conversacion"] = ConversationState.ESPERANDO_ADJUNTOS_RECLAMO.name
                estado = ConversationState.ESPERANDO_ADJUNTOS_RECLAMO
                logger.info("[ReclamoHandler] Nuevo estado: ESPERANDO_ADJUNTOS_RECLAMO.")
                # This response is triggered if the current input (pregunta_str or from payload.datos) just filled the description
                body_adj = "¡Gracias por la descripción! ¿Querés **adjuntar una foto o compartir tu ubicación GPS**? (Opcional)"
                options_adj = [{"id": "adjuntar_foto", "texto": "Adjuntar foto"}, {"id": "compartir_ubicacion", "texto": "Compartir ubicación"}, {"id": "sin_adjuntos", "texto": "Continuar sin adjuntos"}]
                return {"message_body": body_adj, "options_list": options_adj, "message_type": 'interactive_buttons', "fuente": "reclamo_pedir_adjuntos_v2"}
            
            # 7. If all data is filled, this state will be reached
            elif current_state_for_logic == ConversationState.ESPERANDO_CONFIRMACION_RECLAMO:
                logger.info("[ReclamoHandler] Todos los datos necesarios están en memoria. Procediendo a la confirmación final del reclamo.")
                # The logic for ESPERANDO_CONFIRMACION_RECLAMO is outside this while loop.
                break # Exit while loop to proceed to confirmation logic.

            # Break if state hasn't changed, to prevent infinite loop if a field isn't getting filled.
            # This shouldn't happen if each state correctly asks or validates.
            # Or if iterations are too high.
            if estado == current_state_for_logic and iterations_count > 1 and pregunta_str : # State didn't advance despite input
                logger.warning(f"[ReclamoHandler] El estado {estado.name} no avanzó después de la entrada '{pregunta_str}'. Rompiendo bucle para evitar ciclo infinito.")
                break
            if iterations_count >= MAX_ITERATIONS_RECLAMO_LOOP:
                logger.error(f"[ReclamoHandler] Se alcanzó el número máximo de iteraciones ({MAX_ITERATIONS_RECLAMO_LOOP}) en el bucle de recopilación de datos del reclamo. Estado actual: {estado.name}. Abortando para evitar ciclo infinito.")
                memoria.clear() # Clear context to reset
                return {"respuesta": "Parece que tuvimos un problema recopilando todos los datos. Por favor, intentá iniciar el reclamo de nuevo."}

            # If we are here, it means a field was filled (e.g. by LLM or previous step) and we are continuing the loop
            # to the next state. `pregunta_str` should have been cleared if it was consumed by a previous step in this iteration.
            if not pregunta_str and estado not in [ConversationState.ESPERANDO_ADJUNTOS_RECLAMO, ConversationState.ESPERANDO_CONFIRMACION_RECLAMO]:
                # This means we advanced state based on pre-filled data (likely from LLM or earlier user multi-input)
                # and now need to *ask* for the current state's data if it's not already filled.
                if estado == ConversationState.ESPERANDO_DIRECCION_RECLAMO and not memoria.get("direccion_reclamo"):
                    cat_mem = memoria.get('categoria_reclamo','N/A')
                    return { "respuesta": f"Perfecto, categoría: **{cat_mem.title() if cat_mem else 'N/A'}**. ¿La **dirección exacta** del problema?\nPor ejemplo: {EJEMPLO_DIRECCION}"}
                elif estado == ConversationState.ESPERANDO_NOMBRE_VECINO and not memoria.get("nombre_vecino"):
                    dir_mem = memoria.get('direccion_reclamo','N/A')
                    return {"respuesta": f"¡Perfecto! Dirección registrada como: **{dir_mem if dir_mem else 'N/A'}**. Ahora, ¿podrías decirme tu **nombre completo**?"}
                elif estado == ConversationState.ESPERANDO_TELEFONO_VECINO and not memoria.get("telefono_vecino"):
                    nom_mem = memoria.get('nombre_vecino','Vecino')
                    return {"respuesta": f"¡Gracias, {(nom_mem.split()[0] if nom_mem else 'Vecino')}! Ahora, ¿me pasarías tu **número de teléfono con código de área**?"}
                elif estado == ConversationState.ESPERANDO_EMAIL_VECINO and not memoria.get("email_vecino"):
                    return {"respuesta": "¡Excelente! Casi terminamos. ¿Cuál es tu **dirección de correo electrónico**?"}
                elif estado == ConversationState.ESPERANDO_DESCRIPCION_RECLAMO and not memoria.get("descripcion_reclamo"):
                    return {"respuesta": "¡Bárbaro! Ahora, por favor, contame con un poco más de detalle **cuál es el problema**. Luego podrás adjuntar foto/ubicación si querés."}
                
            elif pregunta_str and estado == current_state_for_logic and iterations_count > 1 : 
                logger.warning(f"[ReclamoHandler] Pregunta '{pregunta_str}' no fue consumida y el estado {estado.name} no avanzó. Rompiendo bucle para evitar ciclo.")
                break 
            elif estado == ConversationState.ESPERANDO_ADJUNTOS_RECLAMO or estado == ConversationState.ESPERANDO_CONFIRMACION_RECLAMO:
                break


        # After the while loop, check the state. It should be ADJUNTOS or CONFIRMACION, or an error occurred.
        estado_str_after_loop = memoria.get("estado_conversacion")
        estado_after_loop = None # Initialize to None
        # Ensure estado_after_loop is an Enum for comparison, or None
        estado_after_loop = None
        if isinstance(estado_str_after_loop, str):
            try:
                estado_after_loop = ConversationState[estado_str_after_loop]
            except KeyError:
                logger.error(
                    f"[ReclamoHandler] Estado inválido '{estado_str_after_loop}' en memoria tras bucle. Limpiando."
                )
                memoria.clear()
                return {"respuesta": "Hubo un error procesando tu reclamo. Por favor, intentá de nuevo."}
        elif isinstance(estado_str_after_loop, ConversationState):
            estado_after_loop = estado_str_after_loop
        elif estado_str_after_loop is not None:
            logger.error(
                f"[ReclamoHandler] Tipo de estado inesperado '{type(estado_str_after_loop)}' en memoria tras bucle. Limpiando."
            )
            memoria.clear()
            return {"respuesta": "Hubo un error procesando tu reclamo. Por favor, intentá de nuevo."}
        
        if estado_after_loop == ConversationState.ESPERANDO_ADJUNTOS_RECLAMO:
            accion = payload.get("action", "").lower() or normalizar_texto(pregunta_str)
            SIN_ADJUNTOS_KEYWORDS = ["sin_adjuntos", "no, continuar", "no", "no gracias", "no, gracias", "completar", "completar reclamo", "completar el reclamo", "terminar", "terminar reclamo", "quiero completar", "quiero terminar"]
            if any(kw in accion for kw in SIN_ADJUNTOS_KEYWORDS):
                memoria["estado_conversacion"] = ConversationState.ESPERANDO_CONFIRMACION_RECLAMO.name
                resumen = self.build_detalles_memoria(memoria)
                body_confirm = f"Perfecto, continuamos sin adjuntos. Por favor, revisá si todos los datos son correctos antes de confirmar:\n\n{resumen}\n\n¿Está todo bien para generar el reclamo?"
                options_confirm = [
                    {"id": "confirmar_reclamo_final", "texto": "Sí, confirmar reclamo"},
                    {"id": "editar_reclamo_datos", "texto": "No, quiero editar algo"}
                ]
                return {
                    "message_body": body_confirm,
                    "options_list": options_confirm,
                    "message_type": 'interactive_buttons',
                    "fuente": "reclamo_confirmar_sin_adjuntos_v2"
                }
            if accion == "adjuntar_foto":
                if self.context.get("anon_id") and not self.context.get("user_id"):
                    body_anon_foto = "Para adjuntar una foto, necesitás iniciar sesión o registrarte. ¿Te gustaría hacerlo ahora?"
                    options_anon_foto = [
                        {"id": "login_anon_adjunto", "texto": "Iniciar Sesión"},
                        {"id": "register_anon_adjunto", "texto": "Registrarme Gratis"},
                        {"id": "sin_adjuntos_anon", "texto": "Continuar sin adjuntar"}
                    ]
                    return {"message_body": body_anon_foto, "options_list": options_anon_foto, "message_type": 'interactive_buttons', "fuente": "reclamo_adjuntar_foto_anon_v2"}
                return {"message_body": "¡Entendido! Podés enviarme la foto ahora. Cuando la vea, la adjuntaré al reclamo. Si preferís no adjuntar nada, simplemente decime 'continuar'.", "options_list": [{"id": "sin_adjuntos_post_foto_prompt", "texto": "No adjuntar y continuar"}], "message_type": "interactive_buttons", "fuente": "reclamo_esperando_foto_v2"}
            if accion == "compartir_ubicacion":
                allow_anon_gps_for_sharing = False
                if has_app_context(): allow_anon_gps_for_sharing = current_app.config.get("ALLOW_ANON_GPS", False) # type: ignore
                if (self.context.get("anon_id") and not self.context.get("user_id") and not (has_app_context() and current_app.config.get("ALLOW_ANON_GPS", False))): # type: ignore
                    body_anon_gps = "Para compartir tu ubicación GPS de forma precisa para el reclamo, necesitás iniciar sesión o registrarte. ¿Te gustaría hacerlo ahora?"
                    options_anon_gps = [
                        {"id": "login_anon_adjunto_gps", "texto": "Iniciar Sesión"},
                        {"id": "register_anon_adjunto_gps", "texto": "Registrarme Gratis"},
                        {"id": "sin_adjuntos_anon_gps", "texto": "Continuar sin compartir ubicación"}
                    ]
                    return {"message_body": body_anon_gps, "options_list": options_anon_gps, "message_type": 'interactive_buttons', "fuente": "reclamo_compartir_gps_anon_v2"}
                return {"message_body": "¡Claro! Podés compartir tu ubicación actual usando el botón del clip 📎 en tu WhatsApp o la opción de compartir ubicación de la web. Si preferís no hacerlo, solo decime 'continuar'.", "options_list": [{"id": "sin_adjuntos_post_gps_prompt", "texto": "No compartir y continuar"}], "message_type": "interactive_buttons", "fuente": "reclamo_esperando_gps_v2"}
            adjunto_recibido_msg = ""
            if payload.get("es_foto") and payload.get("archivo_url"): memoria["foto_url"] = payload.get("archivo_url"); adjunto_recibido_msg = "¡Foto recibida y adjuntada!"
            if payload.get("es_ubicacion") and payload.get("ubicacion_usuario"): memoria["ubicacion_gps"] = payload.get("ubicacion_usuario"); adjunto_recibido_msg = "¡Ubicación GPS recibida y adjuntada!" if not adjunto_recibido_msg else "¡Foto y ubicación GPS recibidas y adjuntadas!"
            if memoria.get("foto_url") or memoria.get("ubicacion_gps"):
                memoria["estado_conversacion"] = ConversationState.ESPERANDO_CONFIRMACION_RECLAMO.name
                resumen = self.build_detalles_memoria(memoria)
                body_confirm_adj = f"{adjunto_recibido_msg}\n\nExcelente. Ahora, por favor, revisá si todos los datos son correctos antes de confirmar:\n\n{resumen}\n\n¿Está todo bien para generar el reclamo?"
                options_confirm_adj = [
                    {"id": "confirmar_reclamo_final", "texto": "Sí, confirmar reclamo"},
                    {"id": "editar_reclamo_datos", "texto": "No, quiero editar algo"}
                ]
                return {
                    "message_body": body_confirm_adj,
                    "options_list": options_confirm_adj,
                    "message_type": 'interactive_buttons',
                    "fuente": "reclamo_confirmar_con_adjuntos_v2"
                }
            CONFIRMACION_DIRECTA_KEYWORDS_ADJUNTOS = ["confirmar reclamo", "confirmar", "confirmo", "confirmado", "si confirmo", "sí confirmo", "finalizar reclamo", "si", "sí", "confirmarreclamo", "si confirmar reclamo", "sí confirmar reclamo"]
            if any(kw in accion for kw in CONFIRMACION_DIRECTA_KEYWORDS_ADJUNTOS):
                logger.info(f"[ReclamoHandler] Detectada confirmación directa ('{accion}') en ESPERANDO_ADJUNTOS_RECLAMO. Transicionando a confirmación final.")
                memoria["estado_conversacion"] = ConversationState.ESPERANDO_CONFIRMACION_RECLAMO.name
                resumen = self.build_detalles_memoria(memoria)
                body_skip_adj = f"Entendido, salteamos los adjuntos. Por favor, revisá si todos los datos son correctos antes de confirmar:\n\n{resumen}\n\n¿Está todo bien para generar el reclamo?"
                options_skip_adj = [
                    {"id": "confirmar_reclamo_final", "texto": "Sí, confirmar reclamo"},
                    {"id": "editar_reclamo_datos", "texto": "No, quiero editar algo"}
                ]
                return {
                    "message_body": body_skip_adj,
                    "options_list": options_skip_adj,
                    "message_type": 'interactive_buttons',
                    "fuente": "reclamo_skip_adjuntos_confirm_v2"
                }
            try: # type: ignore
                respuesta_llm = _clasificar_intencion_con_llm(pregunta_str, opciones=["adjuntar foto", "compartir ubicacion", "completar", "ninguno"], tipo="adjunto") # type: ignore
                if respuesta_llm and "completar" in respuesta_llm.lower():
                    memoria["estado_conversacion"] = ConversationState.ESPERANDO_CONFIRMACION_RECLAMO.name
                    resumen = self.build_detalles_memoria(memoria)
                    body_llm_skip_adj = f"Entendido, continuamos sin adjuntos. Por favor, revisá si todos los datos son correctos antes de confirmar:\n\n{resumen}\n\n¿Está todo bien para generar el reclamo?"
                    options_llm_skip_adj = [
                        {"id": "confirmar_reclamo_final", "texto": "Sí, confirmar reclamo"},
                        {"id": "editar_reclamo_datos", "texto": "No, quiero editar algo"}
                    ]
                    return {
                        "message_body": body_llm_skip_adj,
                        "options_list": options_llm_skip_adj,
                        "message_type": 'interactive_buttons',
                        "fuente": "reclamo_llm_skip_adjuntos_confirm_v2"
                    }
            except Exception: pass
            body_adj_fallback = "No estoy seguro de haber recibido un adjunto. ¿Querés intentar adjuntar una foto o compartir tu ubicación? También podés elegir continuar sin adjuntos."
            options_adj_fallback = [
                {"id": "adjuntar_foto", "texto": "Adjuntar foto"},
                {"id": "compartir_ubicacion", "texto": "Compartir ubicación"},
                {"id": "sin_adjuntos", "texto": "Continuar sin adjuntos"}
            ]
            return {
                "message_body": body_adj_fallback, 
                "options_list": options_adj_fallback, 
                "message_type": 'interactive_buttons',
                "fuente": "reclamo_adjuntos_fallback_v2"
            }

        estado_str_confirm = memoria.get("estado_conversacion")
        estado_confirm = ConversationState[estado_str_confirm] if isinstance(estado_str_confirm, str) else estado_str_confirm

        if estado_confirm == ConversationState.ESPERANDO_CONFIRMACION_RECLAMO:
            # Determine a robust key for idempotency
            idempotency_payload_key = payload.get("idempotency_key")
            action_key = payload.get("action") # This should be 'confirmarreclamofinal' if it's from the button action
            chat_session_uuid = self.context.get("chat_session_uuid")
            chat_session_data = self.context.get("chat_db_context_data") # This is the raw dict from chat_db_context.context_data

            effective_idempotency_key = idempotency_payload_key

            normalized_action_key_for_idempotency = ""
            if action_key and isinstance(action_key, str):
                 normalized_action_key_for_idempotency = action_key.lower().replace("_", "").replace("-", "").strip()

            FINAL_CONFIRM_ACTION_NORMALIZED = "confirmarreclamofinal" # Lo que parece llegar del frontend

            if not effective_idempotency_key and \
               normalized_action_key_for_idempotency == FINAL_CONFIRM_ACTION_NORMALIZED and \
               chat_session_uuid:
                effective_idempotency_key = f"{chat_session_uuid}_{FINAL_CONFIRM_ACTION_NORMALIZED}"
                logger.info(f"[ReclamoHandler] No 'idempotency_key' en payload para acción '{normalized_action_key_for_idempotency}', usando generado: {effective_idempotency_key}")
            
            if effective_idempotency_key and chat_session_data is not None: # chat_session_data is critical
                processed_keys = chat_session_data.get("processed_idempotency_keys", {})
                if effective_idempotency_key in processed_keys:
                    existing_ticket_nro = processed_keys[effective_idempotency_key]
                    logger.info(f"[ReclamoHandler] Effective idempotency key '{effective_idempotency_key}' ya procesada. Ticket existente: M-{existing_ticket_nro}.")
                    
                    memoria.clear() # Clear the current attempt's data from handler's context
                    # Also update the main chat_db_context's municipio part to reflect this clearance
                    if CONTEXTO_MUNICIPIO not in chat_session_data: # Should exist, but defensive
                        chat_session_data[CONTEXTO_MUNICIPIO] = {}
                    chat_session_data[CONTEXTO_MUNICIPIO].clear() 
                    chat_session_data[CONTEXTO_MUNICIPIO]["estado_conversacion"] = None # Explicitly nullify state for DB
                    # flag_modified(chat_db_context, "context_data") will be called at the end of responder_municipio

                    body_idempotency = f"Este reclamo ({memoria.get('descripcion_reclamo_original_para_idempotencia', 'confirmado previamente')}) ya fue registrado con el número de ticket: **M-{existing_ticket_nro}**. No se ha creado un nuevo ticket. ¡Gracias!"
                    options_idempotency = [
                        {"id": "iniciar_reclamo_nuevo_post_idem", "texto": "Hacer un nuevo reclamo"},
                        {"id": "consultar_estado_ticket_existente_idem", "texto": "Consultar otro ticket"}
                    ]
                    return {
                        "message_body": body_idempotency,
                        "options_list": options_idempotency,
                        "message_type": "interactive_buttons",
                        "fuente": "reclamo_idempotencia_detectada_v2",
                        "ticket_id": None # No new ticket created
                    }
            elif effective_idempotency_key and chat_session_data is None: # Should not happen if chat_db_context is always loaded
                logger.warning("[ReclamoHandler] chat_db_context_data no disponible para idempotencia, aunque se esperaba.")

            # Store original description for potential idempotency message if needed later
            if not memoria.get("descripcion_reclamo_original_para_idempotencia") and memoria.get("descripcion_reclamo"):
                memoria["descripcion_reclamo_original_para_idempotencia"] = memoria.get("descripcion_reclamo")


            if self.context.get("anon_id") and not self.context.get("cliente_id"):
                if has_app_context(): max_tickets_anon = current_app.config.get("ANONYMOUS_MAX_TICKETS_PER_SESSION", 1); session_timeout_minutes_config = current_app.config.get("ANONYMOUS_SESSION_TIMEOUT_MINUTES", 15) # type: ignore
                else: max_tickets_anon = 1; session_timeout_minutes_config = 15
                anon_tickets_count = MunicipioTicket.query.filter_by(anon_id=self.context["anon_id"]).filter(MunicipioTicket.fecha >= datetime.utcnow() - timedelta(minutes=session_timeout_minutes_config)).count()
                if has_app_context(): current_app.logger.info(f"Usuario anónimo {self.context['anon_id']} (Municipio): {anon_tickets_count} tickets en la sesión actual (límite: {max_tickets_anon}).") # type: ignore
                else: logger.info(f"Usuario anónimo {self.context['anon_id']} (Municipio): {anon_tickets_count} tickets en la sesión actual (límite: {max_tickets_anon}).")
                if anon_tickets_count >= max_tickets_anon:
                    memoria.clear()
                    # Original: return {"respuesta": "Alcanzaste el límite...", "botones": [...]}
                    body_anon_limit = "Alcanzaste el límite de reclamos para usuarios invitados en esta sesión. Para continuar, por favor inicia sesión o regístrate."
                    options_anon_limit = [
                        {"id": "login_anon_limit", "texto": "Iniciar Sesión"},
                        {"id": "register_anon_limit", "texto": "Registrarme Gratis"}
                    ]
                    return {
                        "message_body": body_anon_limit,
                        "options_list": options_anon_limit,
                        "message_type": 'interactive_buttons',
                        "fuente": "reclamo_anon_limit_v2"
                    }

            texto_normalizado_accion = normalizar_texto(pregunta_str)
            accion = payload.get("action", "").lower() or texto_normalizado_accion
            # PALABRAS_CLAVE_CONFIRMACION includes "confirmar_reclamo", "si", etc.
            # Let's refine action IDs for clarity, e.g. "confirmar_reclamo_final" used above
            if accion == "confirmar_reclamo_final" or (accion != "editar_reclamo_datos" and any(kw in accion for kw in PALABRAS_CLAVE_CONFIRMACION)):
                if not all(memoria.get(f"{campo}_reclamo" if campo not in ["nombre", "telefono", "email"] else f"{campo}_vecino") for campo in ["categoria", "direccion", "nombre", "telefono", "email", "descripcion"]):
                     logger.error("[ReclamoHandler] Faltan datos críticos para la creación del ticket."); memoria.clear()
                     # Original: return {"respuesta": "Hubo un problema...", "botones": [{"texto": "Hacer un reclamo"}]}
                     return {
                         "message_body": "Hubo un problema al recopilar toda la información necesaria. Por favor, intentemos de nuevo. ¿Querés hacer un reclamo?",
                         "options_list": [{"id": "iniciar_reclamo_error_datos", "texto": "Hacer un reclamo"}],
                         "message_type": 'interactive_buttons',
                         "fuente": "reclamo_error_faltan_datos_v2"
                     }
                try:
                    categoria = memoria.get("categoria_reclamo", "General"); nombre = memoria.get("nombre_vecino", ""); telefono_raw = memoria.get("telefono_vecino", ""); email = memoria.get("email_vecino", "")
                    ticket_data = {
                        "asunto": f"Reclamo de {categoria}",
                        "categoria": categoria,
                        "detalles": memoria.get("descripcion_reclamo", ""),
                        "direccion": memoria.get("direccion_reclamo", ""),
                        "nombre_vecino": nombre,
                        "telefono_vecino": telefono_raw,
                        "email_vecino": email,  # Using email_vecino to match model field
                        "estado": "nuevo",
                        "user_id": self.context.get("cliente_id"),
                        "anon_id": self.context.get("anon_id") if not self.context.get("cliente_id") else None,
                        "municipio_id": getattr(self.context.get("user_obj"), "municipio_id", None),
                        # "ubicacion": memoria.get("ubicacion_gps"), # Old composite key
                        "foto_url_directa": memoria.get("foto_url") # Using foto_url_directa to match model
                    }
                    
                    # Add latitud and longitud if available from ubicacion_gps
                    ubicacion_gps_data = memoria.get("ubicacion_gps")
                    if ubicacion_gps_data and isinstance(ubicacion_gps_data, dict):
                        ticket_data["latitud"] = ubicacion_gps_data.get("lat")
                        ticket_data["longitud"] = ubicacion_gps_data.get("lon")
                    
                    ticket = servicio_tickets.crear_nuevo_ticket(tipo_ticket="municipio", ticket_data=ticket_data)
                    # ... (idempotency and file association logic remains the same) ...
                    if ticket:
                        if effective_idempotency_key and chat_session_uuid and chat_session_data is not None:
                            processed_keys = chat_session_data.get("processed_idempotency_keys", {})
                            processed_keys[effective_idempotency_key] = ticket.nro_ticket
                            chat_session_data["processed_idempotency_keys"] = processed_keys
                            # flag_modified will be called at the end of responder_municipio
                            logger.info(f"[ReclamoHandler] Effective idempotency key '{effective_idempotency_key}' asociada al ticket M-{ticket.nro_ticket} y guardada.")
                        
                        archivo_id_a_vincular = self.context.get("archivo_id_para_asociar"); chat_session_uuid_actual = self.context.get("chat_session_uuid"); user_id_actual_context = self.context.get("user_id") 
                        if archivo_id_a_vincular or chat_session_uuid_actual:
                            from services.archivo_service import archivo_service
                            criterio_asociacion = {}
                            if archivo_id_a_vincular: criterio_asociacion["ids_archivos"] = [archivo_id_a_vincular]; logger.info(f"[ReclamoHandler] Intentando asociar ArchivoAdjunto ID {archivo_id_a_vincular} a Ticket M-{ticket.nro_ticket}")
                            elif chat_session_uuid_actual:
                                criterio_asociacion["session_id"] = chat_session_uuid_actual
                                viewer_user_id_for_file = self.context.get("cliente_id")
                                if viewer_user_id_for_file: criterio_asociacion["user_id"] = viewer_user_id_for_file
                                logger.info(f"[ReclamoHandler] Intentando asociar archivos por session_id {chat_session_uuid_actual} (ViewerUser: {viewer_user_id_for_file}) a Ticket M-{ticket.nro_ticket}")
                            if criterio_asociacion:
                                asociacion_exitosa = archivo_service.asociar_archivos_a_ticket(ticket_id=ticket.id, tipo_ticket="municipio", **criterio_asociacion)
                                if asociacion_exitosa:
                                    logger.info(f"[ReclamoHandler] Archivos asociados exitosamente a Ticket M-{ticket.nro_ticket} usando: {criterio_asociacion}")
                                    if self.context[CONTEXTO_MUNICIPIO] and "archivo_id_para_asociar" in self.context[CONTEXTO_MUNICIPIO]:
                                        del self.context[CONTEXTO_MUNICIPIO]["archivo_id_para_asociar"]
                                else: logger.warning(f"[ReclamoHandler] No se pudieron asociar archivos a Ticket M-{ticket.nro_ticket} usando: {criterio_asociacion}")
                        else: logger.info(f"[ReclamoHandler] No hay archivo_id específico ni session_id para asociar al Ticket M-{ticket.nro_ticket}.")
                    else: logger.error(f"[ReclamoHandler] No se pudo crear el ticket, no se intentará asociar archivos ni guardar idempotency key.")

                    telefono_e164 = formatear_telefono_e164(telefono_raw)
                    if telefono_e164:
                        logger.info(f"[ReclamoHandler] Intentando enviar notificaciones para Ticket M-{ticket.nro_ticket} a {telefono_e164}. Nombre: {nombre}, Categoria: {categoria}")
                        try:
                            enviar_notificacion_whatsapp_con_plantilla(telefono_e164, nombre, str(ticket.nro_ticket), categoria)
                        except Exception as e_whatsapp:
                            logger.error(f"[ReclamoHandler] Error al intentar enviar notificación WhatsApp para Ticket M-{ticket.nro_ticket}: {e_whatsapp}", exc_info=True)
                        try:
                            enviar_notificacion_sms(telefono_e164, f"Hola {nombre}! Tu reclamo M-{ticket.nro_ticket} ({categoria}) fue generado.")
                        except Exception as e_sms:
                            logger.error(f"[ReclamoHandler] Error al intentar enviar notificación SMS para Ticket M-{ticket.nro_ticket}: {e_sms}", exc_info=True)
                    else:
                        logger.warning(f"[ReclamoHandler] No se pudo formatear el teléfono '{telefono_raw}' a E.164 para Ticket M-{ticket.nro_ticket}. No se enviarán notificaciones.")
                    memoria.clear()

                    body_exito = f"¡Excelente! Tu reclamo ha sido registrado con el número de ticket: **M-{ticket.nro_ticket}**. Guardalo para futuras consultas. Te mantendremos informado sobre su progreso por email o WhatsApp. ¡Gracias por ayudarnos a mejorar nuestro municipio!"
                    options_exito = [
                        {"id": "iniciar_reclamo_nuevo", "texto": "Hacer un nuevo reclamo"},
                        {"id": "consultar_estado_ticket_nuevo", "texto": "Consultar estado de un ticket"},
                        {"id": "volver_inicio", "texto": "Volver al inicio"}
                    ]
                    return {
                        "message_body": body_exito,
                        "options_list": options_exito,
                        "message_type": 'interactive_buttons',
                        "fuente": "reclamo_creado_exito_v2",
                        "ticket_id": ticket.id
                    }
                except Exception as e:
                    logger.error(f"[ReclamoHandler] Error al crear ticket: {e}", exc_info=True); memoria.clear()
                    # Original: return {"respuesta": ("¡Oh, parece que tuvimos un pequeño problema técnico..."), "botones": [...]}
                    body_error_creacion = "¡Oh, parece que tuvimos un pequeño problema técnico al registrar tu reclamo! Lamento mucho las molestias. ¿Podrías intentarlo de nuevo en unos minutos? Si el problema continúa, el equipo del municipio estará contento de ayudarte por otros medios."
                    options_error_creacion = [
                        {"id": "reintentar_crear_reclamo", "texto": "Intentar de nuevo"},
                        {"id": "hablar_con_agente_error_reclamo", "texto": "Hablar con un agente"}
                    ]
                    return {
                        "message_body": body_error_creacion,
                        "options_list": options_error_creacion,
                        "message_type": 'interactive_buttons',
                        "fuente": "reclamo_error_creacion_v2"
                    }
            elif accion == "editar_reclamo_datos" or any(kw in accion for kw in EDIT_KEYWORDS) or "editar" in accion or "cambiar" in accion :
                memoria["estado_conversacion"] = ConversationState.ESPERANDO_CATEGORIA_RECLAMO.name
                
                current_summary = self.build_detalles_memoria(memoria)
                current_category_display = memoria.get('categoria_reclamo')
                if current_category_display:
                    current_category_display = current_category_display.replace("_", " ").title()
                else:
                    current_category_display = "No definida"

                body_editar = (f"Entendido, vamos a revisar los datos. Actualmente tenemos esto:\n\n{current_summary}\n\n"
                               f"Empecemos por la categoría. La categoría actual es **{current_category_display}**. "
                               "¿Es correcta o querés cambiarla? Podés seleccionar una nueva de la lista o escribirla.")

                current_cat_norm = normalizar_texto(memoria.get("categoria_reclamo", ""))
                # Use a relevant text for category suggestion if available (e.g., description)
                text_for_edit_cat_suggestion = memoria.get("descripcion_reclamo", pregunta_str) # Fallback to current input if no description
                sugeridas_data_edit = sugerir_categorias_relevantes(text_for_edit_cat_suggestion)
                options_data_edit = sugeridas_data_edit if sugeridas_data_edit and len(sugeridas_data_edit) > 0 else CATEGORIAS_RECLAMO
                
                options_editar = []
                for c in options_data_edit:
                    text = c.title()
                    is_current = (normalizar_texto(c) == current_cat_norm)
                    options_editar.append({"id": normalizar_texto(c), "texto": f"{text}{' (Actual)' if is_current else ''}"})
                
                # Ensure "Otro Motivo" is an option if not already suggested and different from current (and not the only option)
                if "otro motivo" not in [normalizar_texto(o['id']) for o in options_editar] and \
                   (current_cat_norm != "otro motivo" or len(options_editar) == 0) :
                     options_editar.append({"id": "otro motivo", "texto": "Otro Motivo"})
                # Ensure there's at least one option if options_data_edit was empty and "otro motivo" was current
                if not options_editar: 
                    options_editar = [{"id": normalizar_texto(cat), "texto": cat.title()} for cat in CATEGORIAS_RECLAMO]


                return {
                    "message_body": body_editar,
                    "options_list": options_editar,
                    "message_type": 'interactive_list',
                    "fuente": "reclamo_editar_iniciar_desde_categoria_v2"
                }
            else: # Fallback for ESPERANDO_CONFIRMACION_RECLAMO
                try:
                    respuesta_llm = _clasificar_intencion_con_llm(pregunta_str, opciones=["confirmar", "editar"], tipo="confirmacion")
                    if respuesta_llm and "confirm" in respuesta_llm.lower(): payload2 = payload.copy(); payload2["action"] = "confirmar_reclamo_final"; return self.handle(payload2) # Use specific action ID
                    elif respuesta_llm and "edit" in respuesta_llm.lower(): payload2 = payload.copy(); payload2["action"] = "editar_reclamo_datos"; return self.handle(payload2) # Use specific action ID
                except Exception: pass
                resumen = self.build_detalles_memoria(memoria)
                # Original: return {"respuesta": f"No estoy seguro de qué quisiste decir...\n\n{resumen}\n\n¿Confirmamos o editamos?", "botones": [...]}
                body_fallback_confirm = f"No estoy seguro de qué quisiste decir. Por favor, confirmá si los datos son correctos o si querés editar algo:\n\n{resumen}\n\n¿Confirmamos o editamos?"
                options_fallback_confirm = [
                    {"id": "confirmar_reclamo_final", "texto": "Sí, confirmar reclamo"},
                    {"id": "editar_reclamo_datos", "texto": "No, quiero editar algo"}
                ]
                return {
                    "message_body": body_fallback_confirm,
                    "options_list": options_fallback_confirm,
                    "message_type": 'interactive_buttons',
                    "fuente": "reclamo_confirm_fallback_v2"
                }
        return None # Exit point if not in ESPERANDO_CONFIRMACION_RECLAMO or ESPERANDO_ADJUNTOS_RECLAMO

def buscar_en_faqs(pregunta: str, contexto_faq: str) -> dict | None:
    logger.info(f"[buscar_en_faqs] Buscando '{pregunta}' en el contexto '{contexto_faq}'. Implementación pendiente.")
    return None

class TramitesHandler(BaseMunicipioHandler):
    def handle(self, payload: dict) -> dict | None:
        pregunta_str = payload.get("pregunta", "")
        memoria = self.context[CONTEXTO_MUNICIPIO]
        estado_str = memoria.get("estado_conversacion"); estado = ConversationState[estado_str] if isinstance(estado_str, str) else estado_str
        if estado and estado in RECLAMO_STATES: return None
        intencion = self.context.get("intencion")
        if intencion == "consultar_tramite" and not estado:
            memoria.clear(); memoria["estado_conversacion"] = ConversationState.ESPERANDO_SELECCION_TRAMITE.name
            tramites_keys = list(get_tramites_info().keys())
            options = [{"id": normalizar_texto(t), "texto": t.title()} for t in tramites_keys]
            body = "¿Sobre qué trámite necesitás información?"
            message_type = 'interactive_list' if len(options) > 3 else 'interactive_buttons'
            if len(options) > 10:
                logger.warning("TramitesHandler: Too many tramites for a single WhatsApp list. Formatter will truncate.")
            return {
                "message_body": body,
                "options_list": options,
                "message_type": message_type,
                "fuente": "tramites_solicitar_seleccion_v2"
            }
        if estado == ConversationState.ESPERANDO_SELECCION_TRAMITE:
            if pregunta_str and es_pregunta_nueva(pregunta_str, "el nombre de un trámite", memoria):
                logger.info(f"[TramitesHandler] '{pregunta_str}' detectada como pregunta nueva mientras se esperaba trámite. Limpiando memoria.")
                memoria.clear()
                self.context["intencion"] = None # Allow re-classification
                return None

            from .sinonimos import aplicar_sinonimos, TRAMITE_SYNONYMS, fuzzy_match
            texto = normalizar_texto(pregunta_str); texto_con_sinonimos = aplicar_sinonimos(texto, TRAMITE_SYNONYMS)
            # Ensure texto_usuario_lower is defined if used, or use 'texto' (normalized pregunta_str)
            texto_usuario_lower_for_id_check = normalizar_texto(pregunta_str) # Use normalized input for ID check

            clave_tramite = next((k for k in get_tramites_info().keys() if normalizar_texto(k) == texto_con_sinonimos), None)
            if not clave_tramite: # Try to find by ID if user clicked a button
                clave_tramite = next((k for k in get_tramites_info().keys() if normalizar_texto(k) == texto_usuario_lower_for_id_check), None)

            if not clave_tramite: # Fuzzy match if still not found
                all_tramite_names = list(get_tramites_info().keys()) + list(TRAMITE_SYNONYMS.keys())
                best_match_key = fuzzy_match(all_tramite_names, texto)
                if best_match_key and best_match_key in get_tramites_info(): clave_tramite = best_match_key
                elif best_match_key and best_match_key in TRAMITE_SYNONYMS: clave_tramite = TRAMITE_SYNONYMS[best_match_key]
            
            if clave_tramite:
                memoria.clear(); info = get_tramites_info()[clave_tramite]; user_obj = self.context.get("user_obj")
                link_web_placeholder = (getattr(user_obj, "link_web", None) or DEFAULT_TRAMITES_WEB_URL)
                direccion_placeholder = getattr(user_obj, "direccion", None) or MUNICIPIO_DIRECCION
                data_placeholders = {"linkWeb": link_web_placeholder, "direccion": direccion_placeholder}
                
                descripcion_tramite = reemplazar_placeholders(info.get("descripcion", "No hay descripción disponible."), data_placeholders)
                # Botones de la info del trámite (ej. links) se pondrán en el cuerpo del mensaje para WhatsApp.
                # Para web, se pueden mantener como botones si el frontend los maneja.
                # Por ahora, simplificamos: la descripción contendrá todo, y las opciones serán genéricas.
                
                options_post_info = [
                    {"id": "consultar_otro_tramite", "texto": "Consultar otro trámite"},
                    {"id": "volver_inicio_tramites", "texto": "Volver al inicio"}
                ]
                # Agregar URLs como texto en la descripción para WhatsApp
                # Para web, los botones originales de info.get("botones") podrían usarse si el formatter los soporta.
                # Esta parte necesita más refinamiento si los botones de info son cruciales y variados.
                # For now, URLs from info.get("botones") will be appended to description text if channel is WhatsApp.
                # This is a simplification. A more robust solution would involve the formatter handling these.
                
                original_info_buttons = info.get("botones", [])
                if self.context.get("channel") == "whatsapp" and original_info_buttons:
                    links_texto = "\n\nEnlaces relevantes:\n"
                    for btn_info in original_info_buttons:
                        if btn_info.get("url"):
                            links_texto += f"- {btn_info.get('texto', 'Abrir enlace')}: {btn_info['url']}\n"
                    if links_texto.strip() != "Enlaces relevantes:":
                         descripcion_tramite += links_texto

                return {
                    "message_body": descripcion_tramite, 
                    "options_list": options_post_info, # Generic options after info
                    "message_type": "interactive_buttons", # Assuming few generic options
                    "fuente": f"tramite_info_{normalizar_texto(clave_tramite)}_v2"
                    # "original_buttons_from_config": original_info_buttons # For web channel to potentially use
                }

            if "conducir" in texto and ("licencia" in texto or "carnet" in texto):
                memoria["estado_conversacion"] = ConversationState.ESPERANDO_PREGUNTA_CURSO_LICENCIA.name
                info_curso = obtener_respuesta_municipio("curso_licencia_info")
                body_curso = f"{info_curso}\nSi necesitas sacar turno, puedes hacerlo en https://tlc.mendoza.gov.ar/turnos (este enlace se abrirá en tu navegador)."
                options_curso = [
                    {"id": "donde_hacer_curso_licencia", "texto": "¿Dónde hacer el curso?"},
                    {"id": "requisitos_licencia_conducir", "texto": "Ver requisitos generales"},
                    {"id": "consultar_otro_tramite_lic", "texto": "Consultar otro trámite"}
                ]
                return {
                    "message_body": body_curso,
                    "options_list": options_curso,
                    "message_type": 'interactive_buttons', # Max 3
                    "fuente": "tramites_info_curso_licencia_v2"
                }

            memoria.clear()
            tramites_keys_nf = list(get_tramites_info().keys())
            options_nf = [{"id": normalizar_texto(t), "texto": t.title()} for t in tramites_keys_nf]
            body_nf = obtener_respuesta_municipio("tramite_no_encontrado")
            message_type_nf = 'interactive_list' if len(options_nf) > 3 else 'interactive_buttons'
            if len(options_nf) > 10:
                logger.warning("TramitesHandler (not found): Too many tramites for a single WhatsApp list.")
            return {
                "message_body": body_nf,
                "options_list": options_nf,
                "message_type": message_type_nf,
                "fuente": "tramites_no_encontrado_relistar_v2"
            }
        elif estado == ConversationState.ESPERANDO_PREGUNTA_CURSO_LICENCIA:
            if es_pregunta_nueva(pregunta_str, "una pregunta sobre el curso de licencia"): memoria.clear(); return None
            # buscar_en_faqs no está implementado, así que esta rama no se ejecutará como antes.
            # respuesta_faq = buscar_en_faqs(pregunta_str, "licencia_de_conducir")
            # if respuesta_faq:
            #     memoria.clear();
            #     # Adaptar respuesta_faq si tiene botones
            #     return {"message_body": respuesta_faq["a"], ... } 
            memoria.clear()
            body_curso_fallback = obtener_respuesta_municipio("curso_licencia_info")
            # Adding some generic follow-up options
            options_curso_fallback = [
                {"id": "consultar_otro_tramite_curso_fallback", "texto": "Consultar otro trámite"},
                {"id": "volver_inicio_curso_fallback", "texto": "Volver al inicio"}
            ]
            return {
                "message_body": body_curso_fallback, 
                "options_list": options_curso_fallback, 
                "message_type": "interactive_buttons", 
                "fuente": "tramites_curso_licencia_info_fallback_v2"
            }
        return None

class ProductCatalogHandler(BaseMunicipioHandler):
    def handle(self, payload: dict) -> dict | None:
        pregunta_str = payload.get("pregunta", "").strip(); memoria = self.context[CONTEXTO_MUNICIPIO]; intencion = self.context.get("intencion")
        if intencion != "iniciar_compra": return None
        if not PRODUCT_CATALOG: memoria.clear(); return {"respuesta": "Lo sentimos, nuestro catálogo de productos no está disponible en este momento."}
        categories = sorted(list(set(p.get("category", "Otros") for p in PRODUCT_CATALOG))); botones_categorias = []
        for cat in categories: botones_categorias.append({"texto": cat})
        respuesta_texto = "¡Excelente! Tenemos varios productos que podrían interesarte. ¿Qué tipo de producto estás buscando? Aquí tienes nuestras categorías:"
        memoria["estado_conversacion"] = ConversationState.ESPERANDO_PRODUCTO_PARA_CONSULTA.name; memoria.pop("last_found_products", None); memoria.pop("last_discussed_product", None)
        return {"respuesta": respuesta_texto, "botones": botones_categorias}

class ProductInquiryHandler(BaseMunicipioHandler):
    def _buscar_productos(self, texto_busqueda: str) -> list:
        if not texto_busqueda: return []
        texto_busqueda_norm = normalizar_texto(texto_busqueda); palabras_busqueda = set(texto_busqueda_norm.split()); productos_encontrados = []
        for prod in PRODUCT_CATALOG:
            nombre_norm = normalizar_texto(prod.get("name", "")); desc_norm = normalizar_texto(prod.get("description", "")); cat_norm = normalizar_texto(prod.get("category", ""))
            if texto_busqueda_norm in nombre_norm: productos_encontrados.append({"producto": prod, "score": 10}); continue
            if palabras_busqueda.issubset(nombre_norm.split()): productos_encontrados.append({"producto": prod, "score": 8}); continue
            score = 0
            if any(palabra in cat_norm for palabra in palabras_busqueda): score +=3
            palabras_en_nombre = sum(1 for palabra in palabras_busqueda if palabra in nombre_norm); palabras_en_desc = sum(1 for palabra in palabras_busqueda if palabra in desc_norm)
            score += palabras_en_nombre * 2; score += palabras_en_desc * 1
            if score > 2: productos_encontrados.append({"producto": prod, "score": score})
        productos_encontrados.sort(key=lambda x: x["score"], reverse=True)
        final_list_with_scores = []; seen_ids = set()
        for item in productos_encontrados:
            if item["producto"]["id"] not in seen_ids: final_list_with_scores.append(item); seen_ids.add(item["producto"]["id"])
        if not final_list_with_scores or final_list_with_scores[0]["score"] < 5:
            all_product_names = {prod.get("id"): normalizar_texto(prod.get("name", "")) for prod in PRODUCT_CATALOG}
            fuzzy_matches_names = difflib.get_close_matches(texto_busqueda_norm, all_product_names.values(), n=3, cutoff=0.7)
            if fuzzy_matches_names:
                logger.info(f"[ProductInquiryHandler] Fuzzy matches encontrados: {fuzzy_matches_names}")
                for name_match in fuzzy_matches_names:
                    for prod_id, norm_name in all_product_names.items():
                        if norm_name == name_match:
                            original_prod = next((p for p in PRODUCT_CATALOG if p["id"] == prod_id), None)
                            if original_prod and original_prod["id"] not in seen_ids: final_list_with_scores.append({"producto": original_prod, "score": 2}); seen_ids.add(original_prod["id"]); break
        final_list_with_scores.sort(key=lambda x: x["score"], reverse=True)
        final_products_list = []; final_seen_ids = set()
        for item in final_list_with_scores:
            if item["producto"]["id"] not in final_seen_ids: final_products_list.append(item["producto"]); final_seen_ids.add(item["producto"]["id"])
        return final_products_list
    def handle(self, payload: dict) -> dict | None:
        pregunta_str = payload.get("pregunta", "").strip(); memoria = self.context[CONTEXTO_MUNICIPIO]; estado_str = memoria.get("estado_conversacion"); estado = ConversationState[estado_str] if isinstance(estado_str, str) else estado_str; intencion = self.context.get("intencion")
        if not (estado == ConversationState.ESPERANDO_PRODUCTO_PARA_CONSULTA or (intencion == "consultar_producto" and estado != ConversationState.ESPERANDO_CONFIRMACION_AGREGAR_CARRITO) ): return None
        if not PRODUCT_CATALOG: return {"respuesta": "Nuestro catálogo de productos no está disponible en este momento. Intenta más tarde, por favor."}
        logger.info(f"[ProductInquiryHandler] Procesando consulta de producto: '{pregunta_str}'")
        productos = self._buscar_productos(pregunta_str); memoria.pop("last_discussed_product", None)
        if not productos:
            categoria_match = next((cat for cat in set(p.get("category") for p in PRODUCT_CATALOG) if normalizar_texto(pregunta_str) == normalizar_texto(cat)), None)
            if categoria_match:
                productos_categoria = [p for p in PRODUCT_CATALOG if p.get("category") == categoria_match]
                if productos_categoria:
                    memoria["last_found_products"] = productos_categoria; memoria["estado_conversacion"] = ConversationState.MOSTRANDO_PRODUCTOS.name
                    nombres_productos = [f"{p['name']} (${p['price']:.2f})" for p in productos_categoria[:5]]
                    respuesta_str = f"Encontré estos productos en la categoría '{categoria_match}':\n" + "\n".join(f"- {nombre}" for nombre in nombres_productos)
                    if len(productos_categoria) > 5: respuesta_str += f"\n... y {len(productos_categoria) - 5} más."
                    respuesta_str += "\n¿Te interesa alguno en particular?"
                    return {"respuesta": respuesta_str, "botones": [{"texto": p['name']} for p in productos_categoria[:3]]}
            return {"respuesta": "No encontré productos que coincidan con tu búsqueda. ¿Querés intentar con otras palabras o ver nuestras categorías?", "botones": [{"texto": "Ver categorías"}, {"texto": "Cancelar compra"}]}
        if len(productos) == 1:
            prod = productos[0]; memoria["last_discussed_product"] = prod; memoria["estado_conversacion"] = ConversationState.ESPERANDO_CONFIRMACION_AGREGAR_CARRITO.name
            respuesta = (f"Encontré esto: **{prod['name']}**\n{prod['description']}\nPrecio: ${prod['price']:.2f}\n¿Te gustaría agregarlo al carrito?")
            return {"respuesta": respuesta, "botones": [{"texto": "Sí, agregar al carrito"}, {"texto": "No, gracias"}, {"texto": "Buscar otro producto"}]}
        else:
            memoria["last_found_products"] = productos; memoria["estado_conversacion"] = ConversationState.MOSTRANDO_PRODUCTOS.name
            nombres_productos = [f"{p['name']} (${p['price']:.2f})" for p in productos[:5]]
            respuesta_str = "Encontré varios productos que podrían interesarte:\n" + "\n".join(f"- {nombre}" for nombre in nombres_productos)
            if len(productos) > 5: respuesta_str += f"\n... y {len(productos) - 5} más."
            respuesta_str += "\n¿Cuál de estos te interesa? O puedes refinar tu búsqueda."
            botones = [{"texto": p['name']} for p in productos[:3]]; botones.append({"texto": "Buscar de nuevo"})
            return {"respuesta": respuesta_str, "botones": botones}

class CartHandler(BaseMunicipioHandler):
    def _initialize_cart(self, memoria: dict): memoria.setdefault('shopping_cart', [])
    def _add_to_cart(self, memoria: dict, product_to_add: dict, quantity: int = 1) -> bool:
        self._initialize_cart(memoria); cart = memoria['shopping_cart']
        if product_to_add.get('stock', float('inf')) < quantity: return False
        for item in cart:
            if item['id'] == product_to_add['id']: item['quantity'] += quantity; return True
        cart.append({'id': product_to_add['id'], 'name': product_to_add['name'], 'price': product_to_add['price'], 'quantity': quantity})
        return True
    def _remove_from_cart(self, memoria: dict, product_id_to_remove: str) -> bool:
        self._initialize_cart(memoria); cart = memoria['shopping_cart']; original_length = len(cart)
        memoria['shopping_cart'] = [item for item in cart if item['id'] != product_id_to_remove]
        return len(memoria['shopping_cart']) < original_length
    def _format_cart_view(self, memoria: dict) -> str:
        self._initialize_cart(memoria); cart = memoria['shopping_cart']
        if not cart: return "Tu carrito de compras está vacío."
        respuesta = "🛒 **Tu Carrito de Compras:**\n"; total_general = 0.0
        for i, item in enumerate(cart):
            subtotal = item['price'] * item['quantity']
            respuesta += f"{i+1}. **{item['name']}**\n   Cantidad: {item['quantity']} x ${item['price']:.2f} c/u = ${subtotal:.2f}\n"
            total_general += subtotal
        respuesta += f"\n✨ **Total General: ${total_general:.2f}**"; memoria['last_cart_total'] = total_general
        return respuesta
    def handle(self, payload: dict) -> dict | None:
        pregunta_str = payload.get("pregunta", "").strip(); action = payload.get("action", normalizar_texto(pregunta_str)); memoria = self.context[CONTEXTO_MUNICIPIO]; estado_str = memoria.get("estado_conversacion"); estado = ConversationState[estado_str] if isinstance(estado_str, str) else estado_str; intencion = self.context.get("intencion")
        self._initialize_cart(memoria)
        if (intencion == "agregar_al_carrito" or (estado == ConversationState.ESPERANDO_CONFIRMACION_AGREGAR_CARRITO and any(kw in action for kw in ["si", "sí", "agregar", "dale", "quiero"]))):
            product_to_add = memoria.get("last_discussed_product")
            if not product_to_add: return {"respuesta": "No estoy seguro de qué producto querés agregar. ¿Podrías mostrarme de nuevo?"}
            if self._add_to_cart(memoria, product_to_add):
                memoria.pop("last_discussed_product", None); memoria.pop("last_found_products", None); memoria["estado_conversacion"] = ConversationState.ESPERANDO_OPCION_CARRITO.name
                cart_summary = self._format_cart_view(memoria)
                return {"respuesta": f"✅ ¡{product_to_add['name']} agregado al carrito!\n\n{cart_summary}", "botones": [{"texto": "Seguir comprando"}, {"texto": "Finalizar Compra"}, {"texto": "Quitar un producto"}]}
            else: return {"respuesta": f"Lo siento, parece que no tenemos suficiente stock de {product_to_add['name']} en este momento.", "botones": [{"texto": "Buscar otro producto"}, {"texto": "Ver carrito"}]}
        if estado == ConversationState.ESPERANDO_CONFIRMACION_AGREGAR_CARRITO and any(kw in action for kw in ["no", "cancelar"]):
            memoria.pop("last_discussed_product", None); memoria.pop("estado_conversacion", None)
            return {"respuesta": "Entendido. ¿Querés buscar otro producto o ver nuestras categorías?", "botones": [{"texto":"Buscar otro producto"}, {"texto": "Ver categorías"}]}
        if intencion == "ver_carrito" or action == "ver carrito":
            cart_view = self._format_cart_view(memoria); botones = []
            if memoria['shopping_cart']: botones = [{"texto": "Finalizar Compra"}, {"texto": "Seguir comprando"}, {"texto": "Quitar un producto"}]
            else: botones = [{"texto": "Ver productos"}]
            memoria["estado_conversacion"] = ConversationState.ESPERANDO_OPCION_CARRITO.name if memoria['shopping_cart'] else None
            return {"respuesta": cart_view, "botones": botones}
        if intencion == "eliminar_del_carrito" or (estado == ConversationState.ESPERANDO_OPCION_CARRITO and "quitar" in action):
            if not memoria['shopping_cart']: return {"respuesta": "Tu carrito ya está vacío.", "botones": [{"texto": "Ver productos"}]}
            producto_a_quitar_nombre = None
            if "quitar" in pregunta_str:
                partes = pregunta_str.split("quitar", 1)
                if len(partes) > 1 and partes[1].strip(): producto_a_quitar_nombre = normalizar_texto(partes[1].strip())
            if producto_a_quitar_nombre:
                item_id_to_remove = None
                for item in memoria['shopping_cart']:
                    if normalizar_texto(item['name']) == producto_a_quitar_nombre: item_id_to_remove = item['id']; break
                if item_id_to_remove:
                    self._remove_from_cart(memoria, item_id_to_remove); cart_view = self._format_cart_view(memoria)
                    respuesta_msg = f"'{producto_a_quitar_nombre.title()}' eliminado del carrito.\n\n{cart_view}"
                    if not memoria['shopping_cart']: memoria.pop("estado_conversacion", None)
                    return {"respuesta": respuesta_msg, "botones": [{"texto": "Seguir comprando"}, {"texto": "Finalizar Compra"}]}
                else: return {"respuesta": f"No encontré '{producto_a_quitar_nombre.title()}' en tu carrito. ¿Querés ver el carrito para verificar?", "botones": [{"texto":"Ver carrito"}]}
            else:
                memoria["estado_conversacion"] = ConversationState.ESPERANDO_OPCION_CARRITO.name
                botones_productos_carrito = [{"texto": f"Quitar: {item['name']}"} for item in memoria['shopping_cart'][:3]]
                return {"respuesta": "OK. ¿Qué producto te gustaría quitar de tu carrito?", "botones": botones_productos_carrito + [{"texto": "Ver carrito completo"}, {"texto": "Cancelar"}]}
        if action.startswith("quitar:"):
            nombre_a_quitar = action.split("quitar:", 1)[1].strip(); item_id_to_remove = None
            for item in memoria['shopping_cart']:
                if item['name'] == nombre_a_quitar: item_id_to_remove = item['id']; break
            if item_id_to_remove:
                self._remove_from_cart(memoria, item_id_to_remove); cart_view = self._format_cart_view(memoria)
                respuesta_msg = f"'{nombre_a_quitar}' eliminado del carrito.\n\n{cart_view}"
                if not memoria['shopping_cart']: memoria.pop("estado_conversacion", None)
                return {"respuesta": respuesta_msg, "botones": [{"texto": "Seguir comprando"}, {"texto": "Finalizar Compra"}]}
        if estado == ConversationState.ESPERANDO_OPCION_CARRITO and "seguir comprando" in action:
            memoria.pop("estado_conversacion", None)
            return ProductCatalogHandler(self.context).handle({"pregunta": "ver productos", "action":"ver productos", "intencion": "iniciar_compra"})
        return None

class CheckoutHandler(BaseMunicipioHandler):
    def handle(self, payload: dict) -> dict | None:
        pregunta_str = payload.get("pregunta", "").strip(); action = payload.get("action", normalizar_texto(pregunta_str)); memoria = self.context[CONTEXTO_MUNICIPIO]; estado = memoria.get("estado_conversacion"); intencion = self.context.get("intencion")
        cart_handler_instance = CartHandler(self.context)
        if intencion == "proceder_al_pago" or (estado == ConversationState.ESPERANDO_OPCION_CARRITO and "finalizar compra" in action):
            cart_handler_instance._initialize_cart(memoria)
            if not memoria.get('shopping_cart'): return {"respuesta": "Tu carrito está vacío. ¿Querés ver nuestros productos para agregar algo?", "botones": [{"texto": "Ver productos"}]}
            cart_view = cart_handler_instance._format_cart_view(memoria); memoria["estado_conversacion"] = ConversationState.ESPERANDO_CONFIRMACION_PEDIDO.name
            return {"respuesta": f"Estás por finalizar tu compra. Por favor, revisá tu pedido:\n\n{cart_view}\n\n¿Confirmás este pedido?", "botones": [{"texto": "Sí, confirmar pedido"}, {"texto": "Modificar carrito"}, {"texto": "Cancelar"}]}

        estado_str_confirm = memoria.get("estado_conversacion")
        estado_confirm = ConversationState[estado_str_confirm] if isinstance(estado_str_confirm, str) else estado_str_confirm

        if estado_confirm == ConversationState.ESPERANDO_CONFIRMACION_PEDIDO:
            if any(kw in action for kw in ["si", "sí", "confirmar", "confirmar pedido"]):
                cart_handler_instance._initialize_cart(memoria); shopping_cart = memoria.get('shopping_cart', [])
                if not shopping_cart: memoria.clear(); return {"respuesta": "Parece que tu carrito está vacío. Volvamos a empezar.", "botones": [{"texto": "Ver productos"}]}
                try:
                    user_name = getattr(self.context.get("user_obj"), "nombre", "Cliente Chat") or getattr(self.context.get("viewer_user"), "nombre", "Cliente Chat")
                    detalles_pedido_str = "Productos:\n";
                    for item in shopping_cart: detalles_pedido_str += f"- {item['name']} (x{item['quantity']}) - ${item['price']:.2f} c/u\n"
                    detalles_pedido_str += f"\nTotal: ${memoria.get('last_cart_total', 0.0):.2f}"
                    ticket_data = {"asunto": f"Nuevo Pedido Web/Chat - {user_name}", "categoria": "Pedido Online", "detalles": detalles_pedido_str, "pregunta": f"Pedido confirmado por {user_name}.", "estado": "pedido_confirmado", "user_id": self.context.get("cliente_id"), "anon_id": self.context.get("anon_id") if not self.context.get("cliente_id") else None, "municipio_id": getattr(self.context.get("user_obj"), "municipio_id", None)}
                    tipo_ticket_pedido = "pyme" if hasattr(self.context.get("rubro_obj"), "id") else "municipio"
                    pedido_ticket = servicio_tickets.crear_nuevo_ticket(tipo_ticket=tipo_ticket_pedido, ticket_data=ticket_data)
                    if pedido_ticket:
                        logger.info(f"Pedido confirmado y registrado como ticket #{pedido_ticket.nro_ticket}.")
                        memoria.pop('shopping_cart', None); memoria.pop('last_cart_total', None); memoria.pop('last_discussed_product', None); memoria.pop('last_found_products', None); memoria.pop("estado_conversacion", None)
                        return {"respuesta": (f"¡Excelente! Tu pedido ha sido confirmado con el número de referencia: **{pedido_ticket.nro_ticket}**. Nos pondremos en contacto contigo a la brevedad para coordinar los detalles del pago y la entrega. ¡Gracias por tu compra!"), "botones": [{"texto": "Ver más productos"}, {"texto": "Necesito ayuda"}]}
                    else: raise Exception("La creación del ticket de pedido retornó None.")
                except Exception as e:
                    logger.error(f"[CheckoutHandler] Error al finalizar pedido y crear ticket: {e}", exc_info=True)
                    memoria["estado_conversacion"] = ConversationState.ESPERANDO_OPCION_CARRITO.name
                    return {"respuesta": "Hubo un problema al procesar tu pedido. Por favor, intentá confirmar nuevamente en unos momentos. Tu carrito sigue guardado.", "botones": [{"texto": "Reintentar confirmar"}, {"texto": "Ver carrito"}]}
            elif any(kw in action for kw in ["modificar", "modificar carrito"]):
                memoria["estado_conversacion"] = ConversationState.ESPERANDO_OPCION_CARRITO.name; cart_view = cart_handler_instance._format_cart_view(memoria)
                return {"respuesta": "Ok, volvemos a tu carrito para que puedas modificarlo:\n\n" + cart_view, "botones": [{"texto": "Finalizar Compra"}, {"texto": "Seguir comprando"}, {"texto": "Quitar un producto"}]}
            elif any(kw in action for kw in ["cancelar", "no"]):
                 memoria.pop("estado_conversacion", None)
                 return {"respuesta": "Pedido cancelado. ¿Querés seguir viendo productos o necesitas ayuda con algo más?", "botones": [{"texto": "Seguir comprando"}, {"texto": "Ver carrito"}, {"texto": "Hablar con un agente"}]}
            else:
                cart_view = cart_handler_instance._format_cart_view(memoria)
                return {"respuesta": f"No entendí tu respuesta. Por favor, confirmá si querés finalizar este pedido:\n\n{cart_view}\n\n", "botones": [{"texto": "Sí, confirmar pedido"}, {"texto": "Modificar carrito"}, {"texto": "Cancelar"}]}
        return None

class StoreLocationHandler(BaseMunicipioHandler):
    def _calculate_distance_sq(self, lat1, lon1, lat2, lon2): return (lat1 - lat2)**2 + (lon1 - lon2)**2
    def handle(self, payload: dict) -> dict | None:
        pregunta_str = payload.get("pregunta", "").strip(); memoria = self.context[CONTEXTO_MUNICIPIO]; intencion = self.context.get("intencion"); user_location = self.context.get("ubicacion_usuario")
        if intencion != "solicitar_ubicacion_tienda": return None
        if not COMMERCE_LOCATIONS: return {"respuesta": "Lo siento, no tengo información sobre la ubicación de nuestras tiendas o sucursales en este momento."}
        if not user_location:
            memoria["estado_conversacion"] = "ESPERANDO_UBICACION_PARA_TIENDAS"; memoria["intencion_pendiente_ubicacion"] = "solicitar_ubicacion_tienda"
            return {"respuesta": "Para encontrar las sucursales más cercanas, necesito tu ubicación. ¿Podrías compartirla?", "botones": [{"texto": "Compartir mi ubicación", "action": "compartir_ubicacion_para_tiendas"}, {"texto": "No, gracias"}]}
        if memoria.get("estado_conversacion") == "ESPERANDO_UBICACION_PARA_TIENDAS":
            memoria.pop("estado_conversacion", None); memoria.pop("intencion_pendiente_ubicacion", None)
        locations_with_distance = []
        for loc in COMMERCE_LOCATIONS:
            if loc.get("latitude") is not None and loc.get("longitude") is not None:
                dist_sq = self._calculate_distance_sq(user_location['lat'], user_location['lon'], loc['latitude'], loc['longitude'])
                locations_with_distance.append({**loc, "distance_sq": dist_sq})
        locations_with_distance.sort(key=lambda x: x["distance_sq"]); nearest_locations = locations_with_distance[:3]
        if not nearest_locations: return {"respuesta": "No encontré tiendas o sucursales cercanas a tu ubicación actual."}
        respuesta_str = "Aquí están las tiendas/sucursales más cercanas que encontré:\n"; botones = []
        for i, loc in enumerate(nearest_locations):
            respuesta_str += (f"\n{i+1}. **{loc['name']}**\n   Dirección: {loc['address']}\n")
            if loc.get('hours'): respuesta_str += f"   Horario: {loc['hours']}\n"
            if loc.get('phone'): respuesta_str += f"   Teléfono: {loc['phone']}\n"
            map_url = f"https://www.google.com/maps/search/?api=1&query={loc['latitude']},{loc['longitude']}"
            botones.append({"texto": f"Ver mapa: {loc['name']}", "url": map_url})
        respuesta_str += "\nEspero que esta información te sea útil."
        memoria.pop("last_found_products", None); memoria.pop("last_discussed_product", None); memoria.pop("shopping_cart", None)
        return {"respuesta": respuesta_str, "botones": botones}

class PanicButtonHandler(BaseMunicipioHandler):
    def handle(self, payload: dict) -> dict | None:
        pregunta_str = payload.get("pregunta", "").strip(); memoria = self.context[CONTEXTO_MUNICIPIO]; estado = memoria.get("estado_conversacion"); intencion = self.context.get("intencion"); user_location = self.context.get("ubicacion_usuario")
        if not (intencion == "activar_panico" or estado == ConversationState.ESPERANDO_UBICACION_PANICO): return None
        logger.warning(f"[PANIC_HANDLER] Pánico activado. Intención: {intencion}, Estado: {estado}, Ubicación: {user_location}")
        if self.context.get("anon_id") and not self.context.get("user_id"):
            body_anon_panic = "🚨 **EMERGENCIA DETECTADA** 🚨\nPara enviar ayuda de forma efectiva, necesitamos tu ubicación. Compartir tu ubicación precisa requiere que inicies sesión o te registres. **Si estás en peligro inmediato y no puedes/quieres registrarte, llamá directamente al 911 o al número de emergencia local.**\n\nSi deseas continuar por aquí y compartir tu ubicación (requiere registro/login):"
            options_anon_panic = [
                {"id": "login_panic_anon", "texto": "Iniciar Sesión para Emergencia"},
                {"id": "register_panic_anon", "texto": "Registrarme para Emergencia"},
                {"id": "cancelar_alerta_panic_anon", "texto": "Cancelar Alerta (error mío)"}
            ]
            return {
                "message_body": body_anon_panic,
                "options_list": options_anon_panic,
                "message_type": 'interactive_buttons', # Could be list if text is long for buttons
                "fuente": "panic_anon_login_required_v2"
            }
        if not user_location and estado != ConversationState.ESPERANDO_UBICACION_PANICO:
            memoria["estado_conversacion"] = ConversationState.ESPERANDO_UBICACION_PANICO.name
            memoria["intencion_pendiente_ubicacion"] = "activar_panico"
            if intencion == "activar_panico": memoria["mensaje_original_panico"] = pregunta_str
            body_pide_ubicacion = "¡EMERGENCIA! Para ayudarte de inmediato, COMPARTÍ TU UBICACIÓN AHORA. Es crucial para enviar ayuda.\nSi no puedes compartirla, intentaremos ayudarte igualmente, pero la ubicación acelera la respuesta."
            options_pide_ubicacion = [
                {"id": "compartir_ubicacion_urgente_panic", "texto": "🚨 COMPARTIR UBICACIÓN URGENTE"},
                {"id": "no_compartir_ubicacion_panic", "texto": "No puedo compartir ubicación"}
            ]
            return {
                "message_body": body_pide_ubicacion,
                "options_list": options_pide_ubicacion,
                "message_type": 'interactive_buttons',
                "fuente": "panic_pide_ubicacion_v2"
            }
        if memoria.get("estado_conversacion") == ConversationState.ESPERANDO_UBICACION_PANICO:
            memoria.pop("estado_conversacion", None); memoria.pop("intencion_pendiente_ubicacion", None)
        mensaje_original_guardado = memoria.pop("mensaje_original_panico", pregunta_str)
        try:
            detalles_alerta = f"Botón de pánico activado por el usuario. Mensaje original: '{mensaje_original_guardado}'."
            if user_location: detalles_alerta += f" Ubicación compartida: Lat {user_location.get('lat')}, Lon {user_location.get('lon')}."
            else: detalles_alerta += " Ubicación NO compartida por el usuario."
            ticket_data = {"asunto": "¡¡¡ALERTA DE PÁNICO ACTIVADA!!!", "categoria": "Emergencia Pánico", "detalles": detalles_alerta, "pregunta": mensaje_original_guardado, "estado": "ALERTA_PANICO_ACTIVA", "user_id": self.context.get("cliente_id"), "anon_id": self.context.get("anon_id") if not self.context.get("cliente_id") else None, "municipio_id": getattr(self.context.get("user_obj"), "municipio_id", None), "latitud": user_location.get("lat") if user_location else None, "longitud": user_location.get("lon") if user_location else None}
            ticket_panico = servicio_tickets.crear_nuevo_ticket(tipo_ticket="municipio", ticket_data=ticket_data)
            if not ticket_panico: raise Exception("La creación del ticket de pánico retornó None.")
            logger.critical(f"[PANIC_HANDLER] Ticket de pánico M-{ticket_panico.nro_ticket} CREADO. {detalles_alerta}")
            respuesta_usuario = ""
            if user_location: respuesta_usuario = ("Tu ALERTA DE PÁNICO y ubicación han sido ENVIADAS a los servicios de emergencia. La ayuda está en camino. Mantené la calma y seguí las instrucciones de las autoridades si te contactan.")
            else: respuesta_usuario = ("Tu ALERTA DE PÁNICO ha sido ENVIADA. No se pudo obtener tu ubicación. Si es posible, informala cuando te contacten. Mantené la calma.")
            memoria.pop("shopping_cart", None); memoria.pop("last_discussed_product", None)
            return {"respuesta": respuesta_usuario}
        except Exception as e:
            logger.error(f"[PanicButtonHandler] Error crítico al procesar pánico: {e}", exc_info=True)
            return {"respuesta": ("Estamos intentando procesar tu alerta de emergencia. Si estás en peligro inmediato, por favor contacta directamente a los servicios de emergencia locales (ej: 911).")}

class ImpuestosHandler(BaseMunicipioHandler):
    def handle(self, payload: dict) -> dict | None:
        pregunta_str = payload.get("pregunta", ""); memoria = self.context[CONTEXTO_MUNICIPIO]; estado = memoria.get("estado_conversacion")
        if estado and estado in RECLAMO_STATES: return None
        intencion = self.context.get("intencion")
        if intencion == "consultar_impuestos":
            self.context[CONTEXTO_MUNICIPIO].clear()
            body = obtener_respuesta_municipio("impuestos_info")
            raw_options_data = obtener_respuesta_municipio("impuestos_botones")
            
            options = []
            if isinstance(raw_options_data, list):
                for btn_data in raw_options_data:
                    option_text = btn_data.get("texto", "Opción")
                    option_id = btn_data.get("action", normalizar_texto(option_text)) # Default ID
                    
                    current_option = {"id": option_id, "texto": option_text}
                    if "url" in btn_data:
                        current_option["type"] = "url"
                        current_option["url"] = btn_data["url"]
                        if self.context.get("channel") == "whatsapp":
                            body += f"\n\n{option_text}: {btn_data['url']}"
                            # For WhatsApp, we don't add URL buttons to the interactive list itself
                            # The URL is in the text. We might offer a generic button like "Siguiente".
                            continue # Skip adding this as an interactive option for WhatsApp if it's purely a URL
                    options.append(current_option)
            
            # Filter out URL-only options for WhatsApp interactive count
            interactive_options_count = sum(1 for opt in options if opt.get("type") != "url")
            message_type = 'text'
            if interactive_options_count == 1: message_type = 'interactive_buttons'
            elif 1 < interactive_options_count <= 3: message_type = 'interactive_buttons'
            elif interactive_options_count > 3: message_type = 'interactive_list'
            
            # If all original buttons were URLs and it's WhatsApp, options list might be empty for interactive part
            if self.context.get("channel") == "whatsapp" and interactive_options_count == 0:
                message_type = 'text' # Body contains the info and URLs

            return {
                "message_body": body,
                "options_list": options, # Formatter will handle web URL buttons
                "message_type": message_type,
                "fuente": "impuestos_info_interactivo_v2"
            }
        return None

class GeneralHandler(BaseMunicipioHandler):
    def handle(self, payload: dict) -> dict | None:
        pregunta_str = payload.get("pregunta", ""); memoria = self.context[CONTEXTO_MUNICIPIO]; estado = memoria.get("estado_conversacion")
        logger.info("[GeneralHandler] Consulta general con contexto de DB."); user_obj = self.context.get("user_obj")
        if estado == ConversationState.ESPERANDO_PREGUNTA_CURSO_LICENCIA:
            respuesta_faq = buscar_en_faqs(pregunta_str, "licencia_de_conducir")
            if respuesta_faq:
                respuesta = {"respuesta": respuesta_faq["a"]}
                if "botones" in respuesta_faq: respuesta["botones"] = respuesta_faq["botones"]
                return respuesta
            logger.info("[GeneralHandler] En estado ESPERANDO_PREGUNTA_CURSO_LICENCIA, pero FAQ no encontró nada. Dejando a LLM general.")
        if not user_obj: logger.warning("[GeneralHandler] No hay user_obj (dueño del bot) en contexto. No se puede buscar en SitioWebInfo."); return None
        contexto_scraped = ""
        try:
            query_filter = {"user_id": user_obj.id}
            contenidos = SitioWebInfo.query.filter_by(**query_filter).all()
            textos_relevantes = [json.loads(item.datos_json).get("contenido", "") for item in contenidos if json.loads(item.datos_json).get("tipo") == "contenido_general"]
            contexto_scraped = " ".join(filter(None, textos_relevantes))
            if not contexto_scraped: logger.info(f"[GeneralHandler] No se encontró contenido 'contenido_general' en SitioWebInfo para user_id {user_obj.id}."); contexto_scraped = "No hay información general disponible del municipio en este momento."
        except Exception as e: logger.error(f"[GeneralHandler] Error al obtener contenido SitioWebInfo: {e}", exc_info=True); contexto_scraped = "Hubo un error al cargar la información general del municipio."
        prompt_final = PROMPT_MUNICIPIO_CON_CONTEXTO.format(contexto_scraped=contexto_scraped, pregunta_usuario=pregunta_str)
        respuesta_llm = safe_llm_call(prompt=prompt_final, preamble="Sos un asistente municipal que responde basado en info oficial.", fallback=("No encontré información específica para tu consulta en la base de datos del municipio. Te puedo ayudar con reclamos, trámites, o intentar conectar con un agente."))
        if len(respuesta_llm.split()) < 7 and ("no puedo" in respuesta_llm.lower() or "no sé" in respuesta_llm.lower()):
             logger.info(f"[GeneralHandler] Respuesta LLM corta o evasiva no detectada por safe_llm_call: '{respuesta_llm}'. Usando botones de fallback.")
             body = respuesta_llm + "\n\nQuizás estas opciones te sirvan:"
             options = [
                 {"id": "iniciar_reclamo_general_fallback", "texto": "Hacer un reclamo"},
                 {"id": "consultar_estado_ticket_general_fallback", "texto": "Consultar estado de ticket"},
                 {"id": "hablar_con_agente_general_fallback", "texto": "Hablar con un agente"}
             ]
             return {
                 "message_body": body,
                 "options_list": options,
                 "message_type": 'interactive_buttons',
                 "fuente": "general_handler_fallback_opciones_v2"
             }
        return {"message_body": respuesta_llm, "options_list": [], "message_type": "text", "fuente": "general_handler_respuesta_directa_v2"}

class EngancheAnonimoMunicipioHandler(BaseMunicipioHandler):
    def handle(self, payload: dict) -> dict | None:
        if self.context.get("user_id"): return None # Already logged in, not for this handler
        # Allow greeting/polite/smalltalk to pass through even if anon, they might respond before this handler.
        if GreetingHandler(self.context).handle(payload) or PoliteHandler(self.context).handle(payload) or SmallTalkHandler(self.context).handle(payload):
            # If these handlers respond, their response will be used.
            # This Enganche handler should only act if those didn't, or if a specific risky intent is detected.
            pass

        intencion = self.context.get("intencion")
        # Ensure this handler only triggers if no other handler has already formed a response for the current input.
        # This check might be implicitly handled by the main loop, but good to be mindful.

        es_anonimo_real = self.context.get("anon_id") and not self.context.get("user_id") and not self.context.get("cliente_id")

        if es_anonimo_real:
            risky_intents = ["iniciar_reclamo", "hablar_con_agente", "consultar_estado_ticket", "activar_panico", "iniciar_compra", "proceder_al_pago"]
            if intencion in risky_intents:
                # Store the pending intent and current state (if any)
                self.context[CONTEXTO_MUNICIPIO]["accion_pendiente_post_login"] = intencion
                current_state_for_saving = self.context[CONTEXTO_MUNICIPIO].get("estado_conversacion")
                if isinstance(current_state_for_saving, Enum): # Ensure state is saved as string
                    self.context[CONTEXTO_MUNICIPIO]["estado_conversacion_pre_login"] = current_state_for_saving.name
                elif isinstance(current_state_for_saving, str):
                    self.context[CONTEXTO_MUNICIPIO]["estado_conversacion_pre_login"] = current_state_for_saving
                elif current_state_for_saving is None:
                     self.context[CONTEXTO_MUNICIPIO].pop("estado_conversacion_pre_login", None)

                logger.info(f"[EngancheAnonimo] Usuario anónimo intentando acción riesgosa: {intencion}. Guardando acción pendiente y estado pre-login: {self.context[CONTEXTO_MUNICIPIO].get('estado_conversacion_pre_login')}")

                body = "Para esta acción (como registrar reclamos, chatear con un agente, activar alertas, o realizar compras) necesitás iniciar sesión o registrarte. ¿Te gustaría hacerlo ahora?"
                options = [
                    {"id": "login_enganche_risky", "texto": "Iniciar Sesión"},
                    {"id": "register_enganche_risky", "texto": "Registrarme Gratis"},
                    {"id": "continuar_invitado_enganche_risky", "texto": "No, gracias"} # This option might lead to them being stuck if the action is mandatory for logged-in users.
                ]
                return {
                    "message_body": body,
                    "options_list": options,
                    "message_type": 'interactive_buttons',
                    "fuente": "enganche_anon_risky_intent_v2"
                }
            
            # If it's a general greeting or very early interaction for an anonymous user, suggest login/register more softly.
            # This part might need careful placement in the handler chain or more context from `responder_municipio`
            # (e.g., if it's the very first interaction).
            # For now, let's assume if it reaches here without a risky intent, it's a general engagement.
            # This could be redundant if GreetingHandler already offered options.
            # Let's make it conditional on no state being active.
            if not self.context[CONTEXTO_MUNICIPIO].get("estado_conversacion"):
                body_general_anon = "¡Hola! Soy tu asistente digital. Para darte una atención más completa y personalizada, te recomiendo registrarte o iniciar sesión. ¿Querés continuar como invitado y solo consultar información general por ahora?"
                options_general_anon = [
                    {"id": "login_enganche_general", "texto": "Iniciar Sesión"},
                    {"id": "register_enganche_general", "texto": "Registrarme Gratis"},
                    {"id": "continuar_invitado_general", "texto": "Continuar como invitado"}
                ]
                return {
                    "message_body": body_general_anon,
                    "options_list": options_general_anon,
                    "message_type": 'interactive_buttons',
                    "fuente": "enganche_anon_general_v2"
                }
        return None

def crear_prompt_decision_herramienta(pregunta_usuario: str) -> str:
    descripcion_herramientas_json = {}
    for nombre, detalles in TOOL_REGISTRY.items(): descripcion_herramientas_json[nombre] = {"descripcion": detalles["descripcion"], "parametros": detalles["parametros"]}
    prompt = f"""
Sos un despachador de herramientas inteligente. Analizá la PREGUNTA DEL USUARIO y decidí si alguna herramienta puede resolverla.
HERRAMIENTAS DISPONIBLES:
{json.dumps(descripcion_herramientas_json, indent=2)}
PREGUNTA: "{pregunta_usuario}"
- Si coincide y hay parámetros, devolvé JSON: {{"herramienta": "nombre_herramienta", "parametros": {{"nombre_param": "valor"}}}}
- Si faltan parámetros, devolvé JSON: {{"herramienta": "nombre_herramienta", "faltan_parametros": ["nombre_param"]}}
- Si no aplica, devolvé 'null'.
"""
    return prompt

class ToolHandler(BaseMunicipioHandler):
    def handle(self, payload: dict) -> dict | None:
        pregunta_str = payload.get("pregunta", ""); memoria = self.context[CONTEXTO_MUNICIPIO]; estado = memoria.get("estado_conversacion")
        if estado and estado in RECLAMO_STATES: return None
        if estado == ConversationState.ESPERANDO_PARAM_RECOLECCION: return RecoleccionHandler(self.context).handle(payload)
        prompt = crear_prompt_decision_herramienta(pregunta_str)
        try:
            respuesta_llm_str = get_cohere_response(message=prompt, preamble="Sos experto en decidir si una pregunta requiere una herramienta. Respondé JSON o 'null'.")
            logger.info(f"[ToolHandler] Decisión LLM Herramienta: {respuesta_llm_str.strip()}")
            if not respuesta_llm_str or respuesta_llm_str.strip().lower() == "null": return None
            decision = json.loads(respuesta_llm_str); nombre_herramienta = decision.get("herramienta")
            if not nombre_herramienta or nombre_herramienta not in TOOL_REGISTRY: return None
            if "faltan_parametros" in decision:
                param_faltante = decision["faltan_parametros"][0]
                if param_faltante == "direccion" and nombre_herramienta == "consultar_recoleccion_por_direccion":
                    memoria["estado_conversacion"] = ConversationState.ESPERANDO_PARAM_RECOLECCION.name
                    return {"respuesta": (f"¡Perfecto! Decime la dirección completa donde querés consultar el servicio municipal.\n{EJEMPLO_DIRECCION}")}
                else: return {"respuesta": f"Necesito más información para usar la herramienta de {nombre_herramienta.replace('_', ' ')}. ¿Podrías proveer el dato: {param_faltante}?"}
            elif "parametros" in decision:
                parametros = decision["parametros"]; funcion_a_ejecutar = TOOL_REGISTRY[nombre_herramienta]["funcion"]
                logger.info(f"[ToolHandler] Ejecutando herramienta '{nombre_herramienta}' con parámetros: {parametros}")
                resultado = funcion_a_ejecutar(**parametros)
                try: return json.loads(resultado)
                except (json.JSONDecodeError, TypeError): return {"respuesta": resultado}
        except json.JSONDecodeError: logger.error(f"[ToolHandler] Error al parsear JSON de respuesta LLM: {respuesta_llm_str}", exc_info=True); return None
        except Exception as e:
            logger.error(f"[ToolHandler] Error general en ToolHandler: {e}", exc_info=True)
            return {"respuesta": "Hubo un error técnico al intentar usar una herramienta. Por favor, probá de nuevo o comunicate con el municipio.", "botones": [{"texto": "Hablar con un agente"}]}
        return None

class HumanEscalationHandler(BaseMunicipioHandler):
    def handle(self, payload: dict) -> dict | None:
        pregunta_str = payload.get("pregunta", "")
        if self.context.get("intencion") != "hablar_con_agente": return None
        
        if self.context.get("anon_id") and not self.context.get("cliente_id"):
            body_anon_escal = "Para hablar con un agente y que podamos dar seguimiento personalizado a tu consulta, necesitás iniciar sesión o registrarte. ¿Te gustaría hacerlo ahora?"
            options_anon_escal = [
                {"id": "login_escalation_anon", "texto": "Iniciar Sesión"},
                {"id": "register_escalation_anon", "texto": "Registrarme Gratis"},
                {"id": "no_gracias_escalation_anon", "texto": "No, gracias"}
            ]
            return {
                "message_body": body_anon_escal,
                "options_list": options_anon_escal,
                "message_type": 'interactive_buttons',
                "fuente": "escalation_anon_sugerir_registro_v2"
            }
        elif not self.context.get("cliente_id"): # Not anonymous but not logged in (should not happen if anon_id is always set for non-logged-in)
             body_login_req = "Para hablar con un agente, por favor inicia sesión."
             options_login_req = [{"id": "login_escalation_req", "texto": "Iniciar Sesión"}]
             return {
                "message_body": body_login_req,
                "options_list": options_login_req,
                "message_type": 'interactive_buttons',
                "fuente": "escalation_login_requerido_v2"
            }

        logger.info(f"[HumanEscalationHandler] Usuario {self.context.get('cliente_id') or self.context.get('anon_id')} pide agente.")
        
        memoria = self.context.get(CONTEXTO_MUNICIPIO, {})
        pregunta_original_escalation = payload.get("pregunta", "") # The message that triggered escalation

        escalation_details_parts = ["El vecino solicitó chat en vivo."]
        if pregunta_original_escalation and pregunta_original_escalation.lower() not in ["hablar con un agente", "agente", "humano", "hablar con alguien", "asesor"]:
            escalation_details_parts.append(f"Mensaje original: '{pregunta_original_escalation}'.")
        
        claim_description = memoria.get("descripcion_reclamo")
        if claim_description:
            escalation_details_parts.append(f"Descripción previa del problema: {claim_description}")
        
        original_claim_category = memoria.get("categoria_reclamo")
        if original_claim_category and original_claim_category != "Atención en Vivo": # "Atención en Vivo" is the category of the live chat ticket itself
            escalation_details_parts.append(f"Categoría del reclamo original: {original_claim_category.replace('_', ' ').title()}")

        final_escalation_details = " ".join(escalation_details_parts)

        nombre = memoria.get("nombre_vecino", "")
        telefono = memoria.get("telefono_vecino", "") 
        email = memoria.get("email_vecino", "")
        direccion_mem = memoria.get("direccion_reclamo", "")
        
        # Fallback to user profile data if details are missing from memoria
        viewer_user = self.context.get("viewer_user_obj")
        if not nombre and viewer_user and getattr(viewer_user, "name", None):
            nombre = viewer_user.name
            logger.info(f"[HumanEscalationHandler] Usando nombre del perfil de usuario: {nombre}")
        if not telefono and viewer_user and getattr(viewer_user, "telefono", None):
            # Asegurarse que el teléfono del perfil esté en formato E.164 si es posible, o usar como está.
            # La lógica de formateo de teléfono podría ser necesaria aquí si el perfil no lo garantiza.
            telefono = viewer_user.telefono 
            logger.info(f"[HumanEscalationHandler] Usando teléfono del perfil de usuario: {telefono}")
        if not email and viewer_user and getattr(viewer_user, "email", None):
            email = viewer_user.email
            logger.info(f"[HumanEscalationHandler] Usando email del perfil de usuario: {email}")
        
        lat_mem, lon_mem = None, None
        ubicacion_payload = self.context.get("ubicacion_usuario") # GPS from current payload/turn (e.g. user clicks "Share Location" then "Talk to agent")
        if ubicacion_payload and isinstance(ubicacion_payload, dict):
            lat_mem = ubicacion_payload.get("lat")
            lon_mem = ubicacion_payload.get("lon")
            logger.info(f"[HumanEscalationHandler] Usando ubicación GPS del payload actual: Lat {lat_mem}, Lon {lon_mem}")
        elif memoria.get("ubicacion_gps") and isinstance(memoria.get("ubicacion_gps"), dict): # GPS from prior reclamo context
            lat_mem = memoria.get("ubicacion_gps", {}).get("lat")
            lon_mem = memoria.get("ubicacion_gps", {}).get("lon")
            logger.info(f"[HumanEscalationHandler] Usando ubicación GPS de memoria de reclamo: Lat {lat_mem}, Lon {lon_mem}")
        
        # If still no address/location, try from user profile (textual address only)
        if not direccion_mem and not (lat_mem and lon_mem) and viewer_user and getattr(viewer_user, "direccion", None):
            direccion_mem = viewer_user.direccion
            logger.info(f"[HumanEscalationHandler] Usando dirección de texto del perfil de usuario: {direccion_mem}")


        ticket_data = {
            "asunto": f"Solicitud de Chat en Vivo por: {nombre if nombre else 'Vecino'}",
            "categoria": "Atención en Vivo",
            "pregunta": memoria.get("descripcion_reclamo", pregunta_original_escalation), # Use current problem description if available
            "detalles": final_escalation_details,
            "user_id": self.context.get("cliente_id"),
            "anon_id": self.context.get("anon_id") if not self.context.get("cliente_id") else None,
            "municipio_id": getattr(self.context.get("user_obj"), "municipio_id", None),
            "estado": "esperando_agente_en_vivo",
            "nombre_vecino": nombre if nombre else None, # Ensure None if empty string
            "telefono_vecino": telefono if telefono else None,
            "email_vecino": email if email else None,
            "direccion": direccion_mem if direccion_mem else None,
            "latitud": lat_mem,
            "longitud": lon_mem
        }
        ticket_data_cleaned = {k: v for k, v in ticket_data.items() if v is not None} # Remove None values

        try:
            sala_de_chat = servicio_tickets.crear_nuevo_ticket(tipo_ticket="municipio", ticket_data=ticket_data_cleaned)
            if not sala_de_chat: raise Exception("No se pudo crear el ticket de sala de chat.")
            servicio_tickets.crear_comentario(ticket_id=sala_de_chat.id, tipo_ticket="municipio", comentario_data={"comentario": pregunta_str, "es_admin": False, "user_id": self.context.get("cliente_id"), "anon_id": self.context.get("anon_id")}) # anon_id might be None here if cliente_id exists
            logger.info(f"[HumanEscalationHandler] Sala de chat #{sala_de_chat.nro_ticket} creada.")
            self.context[CONTEXTO_MUNICIPIO].clear()
            
            body_sala_creada = f"Hemos recibido tu solicitud para hablar con un agente. Estamos notificando al equipo. Tu número de chat es **M-{sala_de_chat.nro_ticket}**. Un agente se unirá tan pronto como esté disponible. Si la espera se prolonga, puedes intentar nuevamente o dejarnos un mensaje más detallado sobre tu consulta."
            options_sala_creada = [
                {"id": f"dejar_mensaje_detallado_chat_{sala_de_chat.nro_ticket}", "texto": "Dejar un mensaje detallado"},
                {"id": f"ver_estado_solicitud_chat_{sala_de_chat.nro_ticket}", "texto": f"Ver estado (M-{sala_de_chat.nro_ticket})"}
            ]
            return {
                "message_body": body_sala_creada,
                "options_list": options_sala_creada,
                "message_type": 'interactive_buttons',
                "fuente": "escalation_sala_creada_v2",
                "ticket_id": sala_de_chat.id
            }
        except Exception as e:
            logger.error(f"[HumanEscalationHandler] Error al escalar a agente: {e}", exc_info=True)
            body_error_escal = "Tuvimos un inconveniente al intentar conectar con un agente en este momento. Por favor, ¿podrías intentar nuevamente en unos minutos? Si prefieres, puedes dejarnos un mensaje con tu consulta y te contactaremos a la brevedad." # Removed placeholder for phone
            options_error_escal = [
                {"id": "reintentar_escalation", "texto": "Intentar de nuevo"},
                {"id": "dejar_mensaje_error_escalation", "texto": "Dejar un mensaje"}
            ]
            return {
                "message_body": body_error_escal,
                "options_list": options_error_escal,
                "message_type": 'interactive_buttons',
                "fuente": "escalation_error_v2"
            }
        return None

class VectorMunicipioCatalogHandler(BaseMunicipioHandler):
    def handle(self, payload: dict) -> dict | None:
        pregunta_str = payload.get("pregunta", ""); memoria = self.context[CONTEXTO_MUNICIPIO]; estado = memoria.get("estado_conversacion")
        if estado and estado in RECLAMO_STATES: return None
        user_obj = self.context.get("user_obj");
        if not user_obj: return None
        keywords_catalogo = ["oficina", "dependencia", "servicio", "centro", "hospital", "salud", "atención", "punto", "ubicación", "dónde queda", "cómo llego", "mapa", "dirección", "municipalidad", "delegación", "horario", "contacto"]
        if not any(kw in normalizar_texto(pregunta_str) for kw in keywords_catalogo) and not estado: return None
        try:
            query_filter = {"user_id": user_obj.id}
            if hasattr(user_obj, "municipio_id") and user_obj.municipio_id: query_filter["municipio_id"] = user_obj.municipio_id
            contenidos = SitioWebInfo.query.filter_by(**query_filter).all(); dependencias = []
            for item in contenidos:
                datos = json.loads(item.datos_json)
                if datos.get("tipo") in ("dependencias", "oficinas", "servicios", "puntos_atencion"): dependencias.extend(datos.get("items", []))
            if not dependencias: return None
            ubicacion_usuario = self.context.get("ubicacion_usuario")
            if ubicacion_usuario and isinstance(ubicacion_usuario, dict) and 'lat' in ubicacion_usuario and 'lon' in ubicacion_usuario:
                def distancia(dep):
                    lat, lon = dep.get("lat"), dep.get("lon")
                    if lat is not None and lon is not None: return (lat - ubicacion_usuario["lat"])**2 + (lon - ubicacion_usuario["lon"])**2
                    return float("inf")
                dependencias.sort(key=distancia)
            else: dependencias.sort(key=lambda d: d.get("nombre", ""))
            agrupadas = {};
            for dep in dependencias: cat = dep.get("categoria") or dep.get("tipo") or "Otros"; agrupadas.setdefault(cat, []).append(dep)
            respuesta = ""
            for cat, deps in agrupadas.items():
                respuesta += f"\n🏢 **{cat.title()}**\n"
                for i, d in enumerate(deps):
                    if i >= 5: break
                    nombre = d.get("nombre", "Dependencia sin nombre"); direccion = d.get("direccion", "Dirección no informada"); tel = d.get("telefono", ""); horario = d.get("horario", ""); ubicacion_coords = f"({d.get('lat', '')}, {d.get('lon', '')})" if d.get("lat") and d.get("lon") else ""
                    respuesta += f"- **{nombre}** — {direccion} {ubicacion_coords}\n"
                    if tel: respuesta += f"  Tel: {tel}\n"
                    if horario: respuesta += f"  Horario: {horario}\n"
                if len(deps) > 5: respuesta += f"  ...y {len(deps)-5} más en esta categoría. Podés preguntar por ellos.\n"
            
            body = respuesta.strip() + "\n\n¿Necesitás más detalles o ver esto en un mapa?"
            options = []
            if ubicacion_usuario:
                # For WhatsApp, "Ver en mapa" could trigger a flow to send a map image or link.
                # For web, it might be a specific frontend action.
                options.append({"id": "vector_ver_en_mapa", "texto": "Ver en mapa"})
            options.append({"id": "vector_contactar_municipio", "texto": "Contactar municipio"})
            
            return {
                "message_body": body,
                "options_list": options,
                "message_type": 'interactive_buttons',
                "fuente": "vector_catalogo_opciones_v2",
                "estado_respuesta": "mostrar_dependencias" # Keep custom fields
            }
        except Exception as e: logger.error(f"[VectorMunicipioCatalogHandler] Error: {e}", exc_info=True); return None

class TramiteInteligenteHandler(BaseMunicipioHandler):
    def handle(self, payload: dict) -> dict | None:
        pregunta_str = payload.get("pregunta", ""); memoria = self.context[CONTEXTO_MUNICIPIO]; estado = memoria.get("estado_conversacion")
        if estado and estado in RECLAMO_STATES: return None
        intencion = self.context.get("intencion"); keywords_tramite = ["requisito", "documento", "necesito", "cómo hago", "pasos", "turno", "costo", "precio", "arancel", "dónde", "lugar", "horario", "duración", "tramite", "trámite", "solicitar", "pedir", "obtener", "gestionar"]
        if not (intencion == "consultar_tramite" or any(kw in normalizar_texto(pregunta_str) for kw in keywords_tramite)) or estado: return None
        user_obj = self.context.get("user_obj");
        if not user_obj: return None
        try:
            query_filter = {"user_id": user_obj.id}
            if hasattr(user_obj, "municipio_id") and user_obj.municipio_id: query_filter["municipio_id"] = user_obj.municipio_id
            contenidos = SitioWebInfo.query.filter_by(**query_filter).all(); tramites_scraped = []
            for item in contenidos:
                datos = json.loads(item.datos_json)
                if datos.get("tipo") == "tramites": tramites_scraped.extend(datos.get("tramites", []))
            for k, v in get_tramites_info().items():
                if not any(t.get("nombre") == k for t in tramites_scraped): tramites_scraped.append({"nombre": k, **v})
            if not tramites_scraped: return None
            pregunta_norm = normalizar_texto(pregunta_str); nombres_tramites = [t.get("nombre", "").lower() for t in tramites_scraped]
            from .sinonimos import TRAMITE_SYNONYMS
            for syn, real_name in TRAMITE_SYNONYMS.items():
                if real_name not in nombres_tramites: nombres_tramites.append(syn.lower())
            mejor_match = difflib.get_close_matches(pregunta_norm, nombres_tramites, n=1, cutoff=0.6)
            tramite_encontrado = None
            if mejor_match:
                matched_name = mejor_match[0]
                for t in tramites_scraped:
                    if normalizar_texto(t.get("nombre", "")) == matched_name: tramite_encontrado = t; break
                if not tramite_encontrado:
                    real_name = next((v for k, v in TRAMITE_SYNONYMS.items() if normalizar_texto(k) == matched_name), None)
                    if real_name:
                        for t in tramites_scraped:
                            if normalizar_texto(t.get("nombre", "")) == normalizar_texto(real_name): tramite_encontrado = t; break
            if not tramite_encontrado:
                for t in tramites_scraped:
                    if any(kw in normalizar_texto(t.get("nombre", "")) for kw in pregunta_norm.split() if len(kw) > 2): tramite_encontrado = t; break
            if not tramite_encontrado: return None
            nombre = tramite_encontrado.get("nombre", "Trámite"); requisitos = tramite_encontrado.get("requisitos", "No informados"); pasos = tramite_encontrado.get("pasos", ""); costo = tramite_encontrado.get("costo", "Consultar"); lugar = tramite_encontrado.get("lugar", ""); horario = tramite_encontrado.get("horario", ""); link = tramite_encontrado.get("link", "")
            
            # Construct the main body text
            body = f"**Información sobre {nombre.title()}**\n"
            if requisitos: body += f"\n**Requisitos:** {requisitos}\n"
            if pasos: body += f"\n**Pasos a seguir:** {pasos}\n"
            if costo: body += f"\n**Costo:** {costo}\n"
            if lugar: body += f"\n**Lugar:** {lugar}\n"
            if horario: body += f"\n**Horario:** {horario}\n"
            body = body.strip()

            options = []
            if link:
                if self.context.get("channel") == "whatsapp":
                    body += f"\n\nPara más información, visitá: {link}"
                else: # For web, suggest a URL button
                    options.append({
                        "id": f"tramite_intel_link_{normalizar_texto(nombre)}",
                        "texto": "Más información",
                        "url": link,
                        "type": "url" 
                    })

            if "turno" in requisitos.lower() or "turno" in pasos.lower() or "turno" in pregunta_norm:
                options.append({
                    "id": f"tramite_intel_sacar_turno_{normalizar_texto(nombre)}",
                    "texto": "Sacar turno"
                })
            
            options.append({"id": "tramite_intel_ver_todos", "texto": "Ver todos los trámites"})
            
            # Determine message_type based on actual interactive options for WhatsApp
            interactive_options_count = sum(1 for opt in options if opt.get("type") != "url")
            message_type = 'text'
            if interactive_options_count == 1: message_type = 'interactive_buttons'
            elif 1 < interactive_options_count <= 3: message_type = 'interactive_buttons'
            elif interactive_options_count > 3: message_type = 'interactive_list'
            
            # If only a URL button was added for web, and no other interactive options, type remains text for WhatsApp
            if interactive_options_count == 0 and any(opt.get("type") == "url" for opt in options):
                message_type = 'text' # As WhatsApp won't show URL button from here

            return {
                "message_body": body,
                "options_list": options, # Send all, formatter will handle based on channel
                "message_type": message_type,
                "fuente": "tramite_inteligente_info_v2",
                "estado_respuesta": "mostrar_tramite" 
            }
        except Exception as e: logger.error(f"[TramiteInteligenteHandler] Error: {e}", exc_info=True); return None

class ReclamoGeoHandler(BaseMunicipioHandler):
    def handle(self, payload: dict) -> dict | None:
        pregunta_str = payload.get("pregunta", ""); memoria = self.context[CONTEXTO_MUNICIPIO]; estado_str = memoria.get("estado_conversacion"); estado = ConversationState[estado_str] if isinstance(estado_str, str) else estado_str
        if estado != ConversationState.ESPERANDO_ADJUNTOS_RECLAMO: return None
        return ReclamoHandler(self.context).handle(payload)

def safe_llm_call(prompt, preamble, fallback=None):
    logger.debug(f"[LLM_CALL_PROMPT] Enviando prompt a LLM. Preamble: '{preamble}'. Prompt: '{prompt[:500]}...'")
    try:
        resp = get_cohere_response(message=prompt, preamble=preamble)
        logger.debug(f"[LLM_CALL_RESPONSE] Respuesta LLM recibida: '{resp[:500]}...'")
        generic_phrases = ["no tengo información", "lo siento", "no puedo ayudarte con eso", "no lo sé", "esa información no está disponible", "como modelo de lenguaje", "no tengo acceso a internet", "no puedo realizar esa acción"]
        if not resp: logger.warning("[LLM_FALLBACK] Respuesta vacía del LLM."); raise ValueError("Respuesta vacía del LLM")
        resp_lower = resp.lower()
        for phrase in generic_phrases:
            if phrase in resp_lower: logger.warning(f"[LLM_FALLBACK] Respuesta genérica del LLM detectada (contiene: '{phrase}'). Respuesta completa: '{resp}'"); raise ValueError(f"Respuesta genérica del LLM (contiene: '{phrase}')")
        return resp
    except ValueError as ve: logger.error(f"[LLM_FALLBACK] Problema con la respuesta del LLM: {ve}"); return fallback or "No tengo información específica en este momento. ¿Te puedo ayudar con algo más?"
    except Exception as e: logger.error(f"[LLM_FALLBACK] Error general en llamada a LLM: {e}", exc_info=True); return fallback or "Hubo un inconveniente al procesar tu solicitud en este momento. Intenta de nuevo más tarde."

CATEGORIAS_RECLAMO = ["arbol caido", "arreglo de calle", "castracion de mascota", "falta de agua, rotura de caño", "fumigacion", "inspeccion de comercio", "limpieza", "luminaria", "riego de calle", "rotura de semaforo", "tramites de obras privadas", "incendio", "otro motivo"]
categorias_normalizadas = [normalizar_texto(c) for c in CATEGORIAS_RECLAMO]
RECLAMO_STATES = [ConversationState.ESPERANDO_CATEGORIA_RECLAMO, ConversationState.ESPERANDO_DIRECCION_RECLAMO, ConversationState.ESPERANDO_NOMBRE_VECINO, ConversationState.ESPERANDO_TELEFONO_VECINO, ConversationState.ESPERANDO_EMAIL_VECINO, ConversationState.ESPERANDO_DESCRIPCION_RECLAMO, ConversationState.ESPERANDO_ADJUNTOS_RECLAMO, ConversationState.ESPERANDO_CONFIRMACION_RECLAMO]

def serializar_enum(obj):
    if isinstance(obj, Enum): return obj.name
    elif isinstance(obj, dict): return {k: serializar_enum(v) for k, v in obj.items()}
    elif isinstance(obj, list): return [serializar_enum(v) for v in obj]
    else: return obj

BOTONES_COMANDOS_MUNICIPIO = {"Hacer un reclamo": "iniciar_reclamo", "Consultar estado de un trámite": "consultar_estado_ticket", "Consultar estado de ticket": "consultar_estado_ticket", "Consultar otro ticket": "consultar_estado_ticket", "Hablar con un agente": "hablar_con_agente", "Nuevo reclamo": "iniciar_reclamo", "Adjuntar foto": "adjuntar_foto", "Compartir ubicación": "compartir_ubicacion", "Foto": "adjuntar_foto", "Ubicación": "compartir_ubicacion", "No, continuar": "sin_adjuntos", "Completar reclamo": "sin_adjuntos", "Sí, confirmar reclamo": "confirmar_reclamo", "Si, confirmar reclamo": "confirmar_reclamo", "Confirmar reclamo": "confirmar_reclamo", "Finalizar": "confirmar_reclamo", "Finalizar reclamo": "confirmar_reclamo", "Confirmar": "confirmar_reclamo", "Confirmado": "confirmar_reclamo", "Si confirmo": "confirmar_reclamo", "Sí confirmo": "confirmar_reclamo", "Editar datos": "editar_reclamo", "Sí, solucionado": "confirmar_cierre_ticket", "No, aún no": "no_cerrar_ticket"}
OWNER_HANDLERS_FOR_STATE = {ConversationState.ESPERANDO_CATEGORIA_RECLAMO: ReclamoHandler, ConversationState.ESPERANDO_DIRECCION_RECLAMO: ReclamoHandler, ConversationState.ESPERANDO_NOMBRE_VECINO: ReclamoHandler, ConversationState.ESPERANDO_TELEFONO_VECINO: ReclamoHandler, ConversationState.ESPERANDO_EMAIL_VECINO: ReclamoHandler, ConversationState.ESPERANDO_DESCRIPCION_RECLAMO: ReclamoHandler, ConversationState.ESPERANDO_ADJUNTOS_RECLAMO: ReclamoHandler, ConversationState.ESPERANDO_CONFIRMACION_RECLAMO: ReclamoHandler, ConversationState.ESPERANDO_NUMERO_TICKET: TicketStatusHandler, ConversationState.ESPERANDO_CONFIRMACION_CIERRE: TicketStatusHandler, ConversationState.ESPERANDO_CALIFICACION: TicketStatusHandler, ConversationState.ESPERANDO_PARAM_RECOLECCION: RecoleccionHandler, ConversationState.ESPERANDO_SELECCION_TRAMITE: TramitesHandler, ConversationState.ESPERANDO_PREGUNTA_CURSO_LICENCIA: TramitesHandler, ConversationState.ESPERANDO_TEXTO_SUGERENCIA: SugerenciasVecinoHandler, ConversationState.ESPERANDO_PRODUCTO_PARA_CONSULTA: ProductInquiryHandler, ConversationState.MOSTRANDO_PRODUCTOS: ProductInquiryHandler, ConversationState.ESPERANDO_CONFIRMACION_AGREGAR_CARRITO: ProductInquiryHandler, ConversationState.ESPERANDO_OPCION_CARRITO: CartHandler, ConversationState.ESPERANDO_DETALLES_CHECKOUT: CheckoutHandler, ConversationState.ESPERANDO_CONFIRMACION_PEDIDO: CheckoutHandler, ConversationState.ESPERANDO_UBICACION_PANICO: PanicButtonHandler}

def responder_municipio(
    pregunta_original,
    owner_user,
    rubro_obj,
    viewer_user=None,
    chat_db_context=None,
    anon_id=None,
    channel: str = "web",
    **kwargs
):
    logger_actual = current_app.logger if has_app_context() else logger
    logger_actual.info(
        f"[RESPONDER_MUNICIPIO_START] Pregunta: '{pregunta_original}', UserMunicipio: {getattr(owner_user, 'id', 'N/A')}, ViewerCiudadano: {getattr(viewer_user, 'id', 'N/A')}, Anon: {anon_id}, Channel: {channel}, ChatSessionUUID: {kwargs.get('chat_session_uuid')}"
    )
    received_payload = {}
    pregunta_str = ""
    if isinstance(pregunta_original, dict):
        received_payload = pregunta_original
        pregunta_str = received_payload.get("pregunta", "")
    elif isinstance(pregunta_original, str):
        pregunta_str = pregunta_original
        received_payload["pregunta"] = pregunta_original
    else:
        logger_actual.warning(
            f"Tipo inesperado para pregunta_original: {type(pregunta_original)}. Contenido: {pregunta_original}"
        )
        pregunta_str = ""
        received_payload["pregunta"] = ""
    if kwargs:
        for key, value in kwargs.items():
            received_payload[key] = value
    if chat_db_context.context_data is None:
        chat_db_context.context_data = {}
    # Carga inicial del contexto específico del municipio
    contexto_municipio_data_from_db = chat_db_context.context_data.get(
        CONTEXTO_MUNICIPIO, {}
    )
    logger_actual.info(
        f"[CONTEXTO_MUNICIPIO_LOAD_RAW] Contexto crudo para '{CONTEXTO_MUNICIPIO}' desde DB: {contexto_municipio_data_from_db}"
    )

    # Crear una copia para modificar de forma segura para esta request.
    contexto_municipio_actual = dict(contexto_municipio_data_from_db)

    # --- Handle post-login resumption ---
    if viewer_user and chat_db_context.context_data.get("just_logged_in_flag"):
        logger_actual.info(f"User {viewer_user.id} identified as just logged in.")
        chat_db_context.context_data.pop("just_logged_in_flag") # Consume the flag

        accion_pendiente = contexto_municipio_actual.pop("accion_pendiente_post_login", None)
        estado_pre_login_str = contexto_municipio_actual.pop("estado_conversacion_pre_login", None)

        if accion_pendiente:
            logger_actual.info(f"Retomando acción pendiente post-login: {accion_pendiente}, estado pre-login: {estado_pre_login_str}")
            kwargs["intencion"] = accion_pendiente # Set intencion for current processing context
            if estado_pre_login_str:
                # Restore state directly into contexto_municipio_actual. It will be parsed to Enum later.
                contexto_municipio_actual["estado_conversacion"] = estado_pre_login_str

        # If the user's input is a generic acknowledgement of login, neutralize it
        # so it doesn't interfere with the resumed flow.
        generic_login_acks = ["ok", "listo", "ya está", "ya me loguee", "estoy logueado", "logged in", "continuar", "dale", "bueno"]
        if pregunta_str.strip().lower() in generic_login_acks:
            logger_actual.info(f"Input '{pregunta_str}' es un ack genérico post-login. Neutralizándolo.")
            pregunta_str = "" # Neutralize for current processing
            if "pregunta" in received_payload: # Ensure payload also reflects this
                received_payload["pregunta"] = ""

    # --- End Handle post-login resumption ---

    estado_guardado_raw = contexto_municipio_actual.get("estado_conversacion")
    logger_actual.info(
        f"[CONTEXTO_MUNICIPIO_LOAD_STATE_RAW] 'estado_conversacion' crudo extraído del contexto_municipio_actual: '{estado_guardado_raw}' (Tipo: {type(estado_guardado_raw)})"
    )

    if estado_guardado_raw and isinstance(estado_guardado_raw, str):
        logger_actual.info(
            f"[CONTEXTO_MUNICIPIO_LOAD_STATE] Intentando convertir estado string '{estado_guardado_raw}' a Enum ConversationState."
        )
        try:
            estado_enum = ConversationState[estado_guardado_raw]
            contexto_municipio_actual["estado_conversacion"] = estado_enum
            logger_actual.info(
                f"[CONTEXTO_MUNICIPIO_LOAD_STATE] Éxito. 'estado_conversacion' ahora es Enum: {estado_enum}"
            )
        except KeyError:
            logger_actual.error(
                f"[CONTEXTO_MUNICIPIO_LOAD_STATE] Falló conversión. String '{estado_guardado_raw}' no es un miembro válido de ConversationState. 'estado_conversacion' se establece a None."
            )
            contexto_municipio_actual["estado_conversacion"] = None
    elif estado_guardado_raw is None:
        logger_actual.info(
            "[CONTEXTO_MUNICIPIO_LOAD_STATE] 'estado_conversacion' es None en los datos crudos. Se mantiene como None."
        )
        contexto_municipio_actual["estado_conversacion"] = None  # Asegurar que sea None explícito
    elif isinstance(estado_guardado_raw, ConversationState):
        logger_actual.warning(
            f"[CONTEXTO_MUNICIPIO_LOAD_STATE] 'estado_conversacion' ya es un Enum ({estado_guardado_raw}) al cargar. Esto es inusual si se carga desde JSON/DB. Se usará tal cual."
        )
        contexto_municipio_actual["estado_conversacion"] = estado_guardado_raw  # Mantener el Enum
    else:  # Otros tipos inesperados
        logger_actual.error(
            f"[CONTEXTO_MUNICIPIO_LOAD_STATE] Tipo inesperado para 'estado_conversacion' ({type(estado_guardado_raw)}): '{estado_guardado_raw}'. Se establece a None."
        )
        contexto_municipio_actual["estado_conversacion"] = None

    # Log del estado final que se usará en esta petición
    final_loaded_state = contexto_municipio_actual.get("estado_conversacion")
    logger_actual.info(
        f"[CONTEXTO_MUNICIPIO_LOAD_FINAL] 'estado_conversacion' final para esta petición: '{final_loaded_state}' (Tipo: {type(final_loaded_state)})"
    )

    # --- Load instance-specific municipio_config ---
    # owner_user is the User object for the bot owner (municipality/pyme)
    specific_municipio_config = None
    owner_user_municipio_id_str = None # For logging
    if owner_user and hasattr(owner_user, 'municipio_id') and owner_user.municipio_id:
        # Assuming owner_user.municipio_id is the string key used for config folders (e.g., "junin", "concordia")
        owner_user_municipio_id_str = str(owner_user.municipio_id) # Ensure it's a string if it's an int
        specific_municipio_config = cargar_configuracion_municipio(owner_user_municipio_id_str, "config.json")
        if specific_municipio_config:
            logger_actual.info(f"Configuración específica cargada para municipio_id: {owner_user_municipio_id_str}")
        else:
            logger_actual.warning(f"No se encontró configuración específica para municipio_id: {owner_user_municipio_id_str}. Se usará la global.")
            specific_municipio_config = CONFIG_MUNICIPIO # Fallback to global
    elif owner_user and hasattr(owner_user, 'id') and not hasattr(owner_user, 'municipio_id'):
        # This case might apply if a User object can be a "municipality" itself,
        # and its ID is used as the key for its config.
        # This depends on how MUNICIPIO_ID is structured for different clients.
        # For now, we assume municipio_id on User is the primary way.
        # If not, this logic might need adjustment based on how different clients' configs are keyed.
        logger_actual.info(f"Owner user {owner_user.id} no tiene 'municipio_id', usando MUNICIPIO_ID global ('{MUNICIPIO_ID}') para config.")
        specific_municipio_config = CONFIG_MUNICIPIO # Fallback to global
    else:
        logger_actual.warning("No se pudo determinar un municipio_id específico del owner_user. Se usará la configuración global.")
        specific_municipio_config = CONFIG_MUNICIPIO # Fallback to global if owner_user is None or has no ID

        # Reconstructing the context dictionary to ensure clean syntax
    context = {}
    context[CONTEXTO_MUNICIPIO] = contexto_municipio_actual
    context["user_obj"] = owner_user
    context["user_id"] = getattr(owner_user, "id", None)
    context["cliente_id"] = getattr(viewer_user, "id", None)
    context["viewer_user_obj"] = viewer_user
    context["anon_id"] = anon_id
    context["intencion"] = None  # Initialize intencion
    context["rubro_obj"] = rubro_obj
    context["channel"] = channel
    context["municipio_config_actual"] = specific_municipio_config

    context["user_id"] = getattr(owner_user, "id", None)
    context["cliente_id"] = getattr(viewer_user, "id", None)
    context["viewer_user_obj"] = viewer_user
    context["anon_id"] = anon_id
    context["intencion"] = None  # Initialize intencion
    context["rubro_obj"] = rubro_obj
    context["channel"] = channel
    context["municipio_config_actual"] = specific_municipio_config
    # El resto de la inicialización del context que depende de received_payload
    context["ubicacion_usuario"] = received_payload.get("ubicacion_usuario")
    context["foto_url"] = None # Se poblará después del análisis de imagen si es necesario
    context["es_foto"] = False  # Se establecerá después del análisis de imagen
    context["es_ubicacion"] = received_payload.get("es_ubicacion", False)
    context["es_archivo"] = received_payload.get("es_archivo", False)
    context["action"] = received_payload.get("action")
    context["datos_interpretados_archivo"] = kwargs.get("datos_interpretados_archivo")
    context["archivo_id_para_asociar"] = kwargs.get("archivo_id_para_asociar")
    context["chat_session_uuid"] = kwargs.get("chat_session_uuid")
    context["chat_db_context_data"] = chat_db_context.context_data
    # End of reconstructed context dictionary. Ensuring no trailing braces here.

    # --- Image Analysis for New/Early Claims & Initial Intent Setting by Media ---
    uploaded_file_info_for_analysis = received_payload.get("uploaded_file_info") or \
                                      received_payload.get("uploaded_file_info_whatsapp")

    is_new_media_for_analysis = False
    if uploaded_file_info_for_analysis and isinstance(uploaded_file_info_for_analysis, dict):
        mime_type = uploaded_file_info_for_analysis.get("mime_type", "")
        if mime_type.startswith("image/"): # Process only images for now
            if uploaded_file_info_for_analysis.get("id") or uploaded_file_info_for_analysis.get("url"):
                is_new_media_for_analysis = True

                # Establish es_foto and foto_url in the main context immediately if an image is detected
                context["es_foto"] = True
                context["foto_url"] = uploaded_file_info_for_analysis.get("url")
                if uploaded_file_info_for_analysis.get("id") and uploaded_file_info_for_analysis.get("source") != "whatsapp":
                    context["archivo_id_para_asociar"] = uploaded_file_info_for_analysis.get("id")
                logger_actual.info(f"[RESPONDER_MUNICIPIO] Imagen detectada en payload (source: {uploaded_file_info_for_analysis.get('source', 'web')}). context['es_foto'] y context['foto_url'] actualizados.")

                # If image is present and text is empty, and no prior intent from kwargs, set intent to iniciar_reclamo
                if not pregunta_str.strip() and not kwargs.get("intencion") and not context.get("intencion"):
                    context["intencion"] = "iniciar_reclamo"
                    logger_actual.info(f"[RESPONDER_MUNICIPIO] Imagen sin texto y sin intención previa por kwargs. Intención fijada a 'iniciar_reclamo'.")

    if is_new_media_for_analysis: # This 'if' is now primarily for logging and triggering the analysis itself
        logger_actual.info(
            f"[RESPONDER_MUNICIPIO] Media (imagen) detectada para posible análisis. "
            f"Estado actual: {final_loaded_state}, Intención (pre-análisis): {context.get('intencion')}, Texto: '{pregunta_str[:30]}...'"
        )

        should_analyze_media_for_claim = False
        # Analyze if no conversation state or if waiting for category, AND if intent is (or becomes) iniciar_reclamo
        if not final_loaded_state or final_loaded_state == ConversationState.ESPERANDO_CATEGORIA_RECLAMO:
            # If no intent is set yet by text or previous logic, and we have media, assume it's for a claim.
            if not context.get("intencion"): # Check if it's still None
                 if kwargs.get("intencion"): # If intent was passed via kwargs (e.g. from a specific button action with media)
                    context["intencion"] = kwargs["intencion"]
                 else: # Default to iniciar_reclamo if media is present and no other intent source
                    context["intencion"] = "iniciar_reclamo"
                    logger_actual.info(f"[RESPONDER_MUNICIPIO] Análisis: Media presente, sin intención específica, asumiendo 'iniciar_reclamo'.")

            if context.get("intencion") == "iniciar_reclamo": # Only analyze if intent is indeed for a claim
                should_analyze_media_for_claim = True

        if should_analyze_media_for_claim: # No need to check intent again here, already done
            logger_actual.info(f"[RESPONDER_MUNICIPIO] Procediendo con análisis de imagen para intención '{context.get('intencion')}'.")
            try:
                from models import ArchivoAdjunto
                from services.interpretacion_imagen_service import interpretar_imagen_para_chat

                archivo_obj_for_analysis = None
                # If it's from web, it should have an ID to fetch from DB
                if uploaded_file_info_for_analysis.get("source") != "whatsapp" and uploaded_file_info_for_analysis.get("id"):
                    archivo_obj_for_analysis = db.session.get(ArchivoAdjunto, uploaded_file_info_for_analysis["id"])
                    if not archivo_obj_for_analysis:
                        logger_actual.warning(f"No se encontró ArchivoAdjunto con ID {uploaded_file_info_for_analysis['id']} para análisis.")

                # If it's from WhatsApp (no ID yet) or web object fetched, and we have a URL
                # The `interpretar_imagen_para_chat` needs to be robust to handle either
                # an ArchivoAdjunto object or a direct URL (if `archivo_obj_for_analysis` is None but URL is in `uploaded_file_info_for_analysis`).
                # For now, we assume `interpretar_imagen_para_chat` primarily works with an ArchivoAdjunto object.
                # If it's a WhatsApp image, we might need to create a temporary ArchivoAdjunto-like structure
                # or modify `interpretar_imagen_para_chat` to accept a URL.

                # Let's prepare a structure that `interpretar_imagen_para_chat` can use,
                # even if it's a temporary one for WhatsApp images not yet in DB.

                # This part needs careful implementation of how `interpretar_imagen_para_chat`
                # consumes `archivo_adjunto`. If it strictly needs a persisted DB object,
                # WhatsApp images would need to be saved first.
                # For now, we'll assume if `archivo_obj_for_analysis` is None but `uploaded_file_info_for_analysis` has a URL,
                # the service might handle it. This is a simplification.

                path_or_url_for_analysis = None
                if archivo_obj_for_analysis: # Web uploaded file, already in DB
                    path_or_url_for_analysis = archivo_obj_for_analysis.url
                    logger_actual.info(
                        f"[RESPONDER_MUNICIPIO] Analizando imagen desde ArchivoAdjunto ID {archivo_obj_for_analysis.id} ({archivo_obj_for_analysis.nombre_original})."
                    )
                elif uploaded_file_info_for_analysis.get("source") == "whatsapp" and uploaded_file_info_for_analysis.get("url"):
                    # This is a WhatsApp image URL. `interpretar_imagen_para_chat` needs to be able
                    # to handle this, perhaps by downloading it or passing the URL to Vision API.
                    # We will pass the dict `uploaded_file_info_for_analysis` itself as `archivo_adjunto` argument.
                    # `interpretar_imagen_para_chat` will need to be adapted.
                    archivo_obj_for_analysis = uploaded_file_info_for_analysis # Pass the dict
                    logger_actual.info(
                        f"[RESPONDER_MUNICIPIO] Analizando imagen desde URL de WhatsApp: {archivo_obj_for_analysis.get('url')}."
                    )

                if archivo_obj_for_analysis: # Either a DB object or the dict from WhatsApp
                    analisis_resultado = interpretar_imagen_para_chat(
                        archivo_adjunto=archivo_obj_for_analysis, # Can be DB object or dict
                        tipo_interpretacion="reclamo_auto_descripcion_categoria"
                    )
                    logger_actual.info(f"[RESPONDER_MUNICIPIO] Resultado análisis de imagen para reclamo: {analisis_resultado}")

                    if analisis_resultado and not analisis_resultado.get("error") and analisis_resultado.get('es_reclamo'):
                        sugerida_cat = analisis_resultado.get("categoria_sugerida")
                        sugerida_desc = analisis_resultado.get("descripcion_sugerida")

                        if sugerida_cat and (not contexto_municipio_actual.get("categoria_reclamo") or contexto_municipio_actual.get("categoria_reclamo") == "otro motivo"):
                            contexto_municipio_actual["categoria_reclamo"] = sugerida_cat
                            logger_actual.info(f"Categoría pre-llenada desde análisis de imagen: {sugerida_cat}")

                        if sugerida_desc and (not contexto_municipio_actual.get("descripcion_reclamo") or len(contexto_municipio_actual.get("descripcion_reclamo", "")) < 20):
                            contexto_municipio_actual["descripcion_reclamo"] = sugerida_desc
                            logger_actual.info(f"Descripción pre-llenada desde análisis de imagen: {sugerida_desc[:70]}...")

                        contexto_municipio_actual["analisis_imagen_reclamo_auto"] = {
                            "categoria": sugerida_cat, "descripcion": sugerida_desc,
                            "ocr_texto": analisis_resultado.get("texto_ocr", "")[:200],
                            "source": uploaded_file_info_for_analysis.get("source", "unknown")
                        }

                        if channel == "whatsapp" and (sugerida_cat or sugerida_desc) and not contexto_municipio_actual.get("telefono_vecino"):
                            # (Lógica de pre-llenado de teléfono para WhatsApp se mantiene igual)
                            whatsapp_phone_number = None
                            if viewer_user and getattr(viewer_user, "telefono", None):
                                whatsapp_phone_number = viewer_user.telefono
                            if whatsapp_phone_number and validar_telefono(whatsapp_phone_number):
                                contexto_municipio_actual["telefono_vecino"] = formatear_telefono_e164(whatsapp_phone_number)
                                logger_actual.info(f"WhatsApp Quick Claim: Teléfono pre-llenado: {contexto_municipio_actual['telefono_vecino']}")
                else:
                    logger_actual.warning("No se pudo obtener un objeto ArchivoAdjunto o URL válida para el análisis de imagen.")
            except Exception as e_img_analysis_main:
                logger_actual.error(f"Error durante el análisis de imagen en responder_municipio: {e_img_analysis_main}", exc_info=True)

            # Asegurar que el contexto general ('context' dict) refleje que se procesó una foto,
            # para que ReclamoHandler pueda usar su lógica de "mensaje_adjunto_recibido".
            if uploaded_file_info_for_analysis and uploaded_file_info_for_analysis.get("mime_type", "").startswith("image/"):
                context["es_foto"] = True # Informar al contexto general
                if uploaded_file_info_for_analysis.get("url"):
                    context["foto_url"] = uploaded_file_info_for_analysis.get("url")

                # Para archivos web que ya tienen un ID de ArchivoAdjunto en la DB
                if uploaded_file_info_for_analysis.get("id") and uploaded_file_info_for_analysis.get("source") != "whatsapp":
                     context["archivo_id_para_asociar"] = uploaded_file_info_for_analysis.get("id")
                     logger_actual.info(f"[RESPONDER_MUNICIPIO] Preparando archivo_id_para_asociar: {context['archivo_id_para_asociar']} para foto web.")
                # Para imágenes de WhatsApp, la URL está en context["foto_url"].
                # La asociación al ticket (guardar el ArchivoAdjunto y vincular) debería ocurrir
                # cuando el ticket se crea, si la URL de la foto está en la memoria del reclamo.

    # Context now contains pre-filled image data if analysis was run and successful.
    # Y context["es_foto"], context["foto_url"] también están seteados si hubo una imagen.
    # Proceed with standard context setup for handlers.
    # The 'intencion' for the context dictionary used by handlers will be set by IntentClassifierHandler later if not already set.
    # kwargs.get("intencion") was from the function call, context['intencion'] is for the handlers.
    # We must ensure context['intencion'] is correctly set before handlers that depend on it.
    # The image analysis block above now sets context["intencion"] = "iniciar_reclamo" if image is first input.

    # --- Logic for suggesting registration to anonymous users --- (This also uses context)

    estado_para_chequeo_sugerencia = contexto_municipio_actual.get("estado_conversacion")  # Enum or None

    if not viewer_user and anon_id and has_app_context():
        estados_a_evitar_sugerencia_para_anon = [
            ConversationState.ESPERANDO_DIRECCION_RECLAMO,
            ConversationState.ESPERANDO_NOMBRE_VECINO,
            ConversationState.ESPERANDO_TELEFONO_VECINO,
            ConversationState.ESPERANDO_EMAIL_VECINO,
            ConversationState.ESPERANDO_DESCRIPCION_RECLAMO,
            ConversationState.ESPERANDO_ADJUNTOS_RECLAMO,
            ConversationState.ESPERANDO_CONFIRMACION_RECLAMO,
            ConversationState.ESPERANDO_UBICACION_PANICO,
        ]

        if estado_para_chequeo_sugerencia not in estados_a_evitar_sugerencia_para_anon:
            interacciones_anon_sesion = contexto_municipio_actual.get("interacciones_anon_sesion", 0)
            if len(pregunta_str.split()) > 1 or pregunta_str.lower() not in ["si", "no", "ok", "dale", "bueno"]:
                interacciones_anon_sesion += 1
            contexto_municipio_actual["interacciones_anon_sesion"] = interacciones_anon_sesion

            umbral_sugerencia = (
                current_app.config.get("MUNICIPIO_UMBRAL_SUGERENCIA_REGISTRO", 3) if has_app_context() else 3
            )

            if umbral_sugerencia and umbral_sugerencia > 0 and interacciones_anon_sesion >= umbral_sugerencia:
                if not contexto_municipio_actual.get("sugerencia_registro_emitida_ronda", False):
                    logger_actual.info(
                        f"[RESPONDER_MUNICIPIO] Anon {anon_id} alcanzó umbral. Sugiriendo registro."
                    )
                    contexto_municipio_actual["sugerencia_registro_emitida_ronda"] = True

                    respuesta_sugerencia_obj = construir_respuesta_sugerir_registro(
                        mensaje_personalizado="Para ayudarte mejor con tus gestiones y reclamos.",
                        tipo_entidad="municipio",
                        channel=channel  # Pass the channel
                    )

                    sug_body = respuesta_sugerencia_obj.get(
                        "respuesta", "Te recomendamos registrarte para una mejor experiencia."
                    )
                    sug_options_raw = respuesta_sugerencia_obj.get("botones", [])
                    sug_options_list = [
                        {"id": btn.get("action", normalizar_texto(btn["texto"])), "texto": btn["texto"]}
                        for btn in sug_options_raw
                    ]
                    sug_message_type = "interactive_buttons" if sug_options_list else "text"

                    # Serializar contexto_municipio_actual ANTES de asignarlo a chat_db_context.context_data
                    contexto_municipio_serializado_para_sugerencia = serializar_enum(contexto_municipio_actual)
                    chat_db_context.context_data[CONTEXTO_MUNICIPIO] = contexto_municipio_serializado_para_sugerencia

                    if chat_db_context:  # Ensure flag_modified is called if context is updated
                        flag_modified(chat_db_context, "context_data")

                    if anon_id and not viewer_user:  # Log conversation for this early return
                        try:
                            db.session.add(
                                Conversacion(
                                    session_id=context.get("chat_session_uuid") or anon_id,
                                    pregunta=pregunta_str,
                                    respuesta=sug_body,
                                    fuente=respuesta_sugerencia_obj.get("fuente", "sugerencia_registro_municipio"),
                                    rubro=getattr(context.get("rubro_obj"), "nombre", "municipio_general"),
                                    user_id=None,
                                )
                            )
                            db.session.commit()
                        except Exception as e_conv_sug_muni:
                            logger_actual.error(
                                f"Error guardando Conversacion (sugerencia MUNICIPIO): {e_conv_sug_muni}"
                            )
                            db.session.rollback()

                    return {
                        # Return the new structure
                        "message_body": sug_body,
                        "options_list": sug_options_list,
                        "message_type": sug_message_type,
                        "fuente": respuesta_sugerencia_obj.get("fuente", "sugerencia_registro_municipio_v2"),
                        # El contexto para la respuesta HTTP también debe usar el serializado
                        "contexto_actualizado": {CONTEXTO_MUNICIPIO: contexto_municipio_serializado_para_sugerencia},
                    }
            else:  # Not reached umbral or umbral is 0/disabled
                contexto_municipio_actual.pop("sugerencia_registro_emitida_ronda", None)
                # Context will be saved at the end of responder_municipio if no early return.

    if context.get("datos_interpretados_archivo"):
        logger_actual.info(
            f"[MUNICIPIOS_HANDLER] Datos interpretados: {context['datos_interpretados_archivo']}"
        )
    if context.get("archivo_id_para_asociar"):
        logger_actual.info(
            f"[MUNICIPIOS_HANDLER] Archivo ID para asociar: {context['archivo_id_para_asociar']}"
        )

    comando_from_text = BOTONES_COMANDOS_MUNICIPIO.get(pregunta_str.strip())
    if comando_from_text and not context.get("action"):
        context["action"] = comando_from_text
        received_payload["action"] = comando_from_text
        logger_actual.info(f"[BOTON] Comando por texto: '{comando_from_text}'")
    elif context.get("action"):
        logger_actual.info(f"[BOTON] Comando por payload.action: '{context['action']}'")
    elif context.get("es_foto") or context.get("es_ubicacion"):
        logger_actual.info(
            f"[ADJUNTO] Detectado: foto={context['es_foto']}, ubicacion={context['es_ubicacion']}"
        )
        if context.get("es_ubicacion"):
            if (
                contexto_municipio_actual.get("intencion_pendiente_ubicacion") == "solicitar_ubicacion_tienda"
                and contexto_municipio_actual.get("estado_conversacion") == "ESPERANDO_UBICacion_PARA_TIENDAS"
            ):
                context["intencion"] = "solicitar_ubicacion_tienda"
                logger_actual.info(
                    f"[CONTEXTO] Ubicación para tiendas, re-evaluando con intención: {context['intencion']}"
                )
            elif (
                contexto_municipio_actual.get("intencion_pendiente_ubicacion") == "activar_panico"
                and contexto_municipio_actual.get("estado_conversacion") == ConversationState.ESPERANDO_UBICACION_PANICO
            ):
                context["intencion"] = "activar_panico"
                logger_actual.info(
                    f"[CONTEXTO] Ubicación para PÁNICO, re-evaluando con intención: {context['intencion']}"
                )

    estado_conversacion_actual = contexto_municipio_actual.get("estado_conversacion")  # This is now an Enum or None
    active_state_log_name = (
        estado_conversacion_actual.name
        if isinstance(estado_conversacion_actual, Enum)
        else str(estado_conversacion_actual)
    )
    logger_actual.info(
        f"[HANDLER_CHAIN_START] Estado en memoria: {active_state_log_name}. Intención previa: {context.get('intencion')}"
    )

    prioritized_handlers = [CancelHandler, PanicButtonHandler]
    if context.get("intencion") == "hablar_con_agente":
        prioritized_handlers.append(HumanEscalationHandler)

    respuesta_final = None
    dueño_handler_class = None
    for handler_class_iter in prioritized_handlers:
        handler_instance = handler_class_iter(context)
        respuesta_parcial = handler_instance.handle(received_payload)
        if respuesta_parcial:
            respuesta_final = respuesta_parcial
            logger_actual.info(
                f"[HANDLER_CHAIN] Prioritized handler {handler_class_iter.__name__} respondió."
            )
            break

    if not respuesta_final and estado_conversacion_actual:
        dueño_handler_class_actual = OWNER_HANDLERS_FOR_STATE.get(estado_conversacion_actual)
        if dueño_handler_class_actual:
            dueño_instance = dueño_handler_class_actual(context)  # Create instance
            active_state_name_log = (
                estado_conversacion_actual.name
                if isinstance(estado_conversacion_actual, Enum)
                else str(estado_conversacion_actual)
            )
            logger_actual.info(
                f"[HANDLER_CHAIN] Estado activo '{active_state_name_log}'. Dando prioridad a {dueño_instance.__class__.__name__}"
            )
            respuesta_parcial_dueño = dueño_instance.handle(received_payload)
            if respuesta_parcial_dueño:
                respuesta_final = respuesta_parcial_dueño
                logger_actual.info(
                    f"[HANDLER_CHAIN] Dueño del estado {dueño_instance.__class__.__name__} respondió."
                )
        else:
            logger_actual.info(
                f"[HANDLER_CHAIN] Dueño del estado ({dueño_instance.__class__.__name__}) no respondió. Re-evaluando intención."
            )
            IntentClassifierHandler(context).handle(received_payload)  # Re-classify intent
            logger_actual.info(
                f"[HANDLER_CHAIN] Nueva intención post-dueño: {context.get('intencion')}"
            )
    else:  # No owner handler for the current state
        active_state_name_log_no_owner = (
            estado_conversacion_actual.name
            if isinstance(estado_conversacion_actual, Enum)
            else str(estado_conversacion_actual)
        )
        logger_actual.warning(
            f"[HANDLER_CHAIN] Estado activo '{active_state_name_log_no_owner}' pero no se encontró handler dueño definido. Limpiando estado y re-clasificando."
        )
        contexto_municipio_actual.pop("estado_conversacion", None)  # Clear state
        IntentClassifierHandler(context).handle(received_payload)  # Re-classify intent
        logger_actual.info(
            f"[HANDLER_CHAIN] Nueva intención post-limpieza de estado sin dueño: {context.get('intencion')}"
        )

    if not respuesta_final:  # If no prioritized handler or owner handler responded
        # Ensure intent is classified if not already set or if state was cleared
        if not context.get("intencion") and not contexto_municipio_actual.get("estado_conversacion"):
            logger_actual.info(
                "[HANDLER_CHAIN] Ejecutando IntentClassifierHandler (sin estado activo, sin intención previa)."
            )
            IntentClassifierHandler(context).handle(received_payload)
            logger_actual.info(
                f"[HANDLER_CHAIN] Intención post-clasificación inicial: {context.get('intencion')}"
            )

        # Define the general sequence of handlers
        remaining_handlers = [
            GreetingHandler,
            PoliteHandler,
            SmallTalkHandler,
            HumanEscalationHandler,
            TicketStatusHandler,
            SugerenciasVecinoHandler,
            RecoleccionHandler,
            ReclamoInteligenteMunicipioHandler,
            ReclamoHandler,  # ReclamoInteligente first
            TramitesHandler,
            TramiteInteligenteHandler,
            ImpuestosHandler,
            ProductCatalogHandler,
            ProductInquiryHandler,
            CartHandler,
            CheckoutHandler,
            StoreLocationHandler,
            ToolHandler,
            VectorMunicipioCatalogHandler,
            GeneralHandler,  # General context-based answers
            EngancheAnonimoMunicipioHandler,  # Suggest login/register if still anonymous and no other handler took over
        ]

        for handler_class_iter_main in remaining_handlers:
            if respuesta_final:
                break  # If a handler in this loop responds, exit

            if (
                handler_class_iter_main in prioritized_handlers
                and handler_class_iter_main != HumanEscalationHandler
            ):
                logger_actual.debug(
                    f"[HANDLER_CHAIN] Saltando {handler_class_iter_main.__name__} (ya corrió como prioritario o no aplicó)."
                )
                continue

            if handler_class_iter_main == EngancheAnonimoMunicipioHandler and context.get("cliente_id"):
                logger_actual.debug(
                    f"[HANDLER_CHAIN] Saltando EngancheAnonimoMunicipioHandler (usuario logueado)."
                )
                continue

            current_memoria_state_for_handler_raw = context[CONTEXTO_MUNICIPIO].get("estado_conversacion")
            current_memoria_state_for_handler_enum = None
            if isinstance(current_memoria_state_for_handler_raw, str):
                try:
                    current_memoria_state_for_handler_enum = ConversationState[
                        current_memoria_state_for_handler_raw
                    ]
                except KeyError:
                    pass  # Keep as None if invalid string
            elif isinstance(current_memoria_state_for_handler_raw, ConversationState):
                current_memoria_state_for_handler_enum = current_memoria_state_for_handler_raw

            original_state_in_context_before_handler = context[CONTEXTO_MUNICIPIO].get("estado_conversacion")
            context[CONTEXTO_MUNICIPIO][
                "estado_conversacion"
            ] = current_memoria_state_for_handler_enum  # Set Enum for handler

            handler_instance = handler_class_iter_main(context)
            log_state_for_handler = (
                current_memoria_state_for_handler_enum.name if current_memoria_state_for_handler_enum else "None"
            )
            logger_actual.info(
                f"[HANDLER_CHAIN] Intentando con handler: {handler_class_iter_main.__name__} (Intención: {context.get('intencion')}, Estado para Handler: {log_state_for_handler})"
            )

            respuesta_parcial = handler_instance.handle(received_payload)

            # After handler execution, decide what state to persist.
            state_after_handler = context[CONTEXTO_MUNICIPIO].get("estado_conversacion")
            if isinstance(state_after_handler, ConversationState):
                context[CONTEXTO_MUNICIPIO]["estado_conversacion"] = state_after_handler.name  # Persist as string
            elif state_after_handler is None:
                context[CONTEXTO_MUNICIPIO].pop("estado_conversacion", None)  # Ensure it's None or key removed

            if respuesta_parcial:
                respuesta_final = respuesta_parcial
                logger_actual.info(
                    f"[HANDLER_CHAIN] Handler {handler_class_iter_main.__name__} respondió."
                )
                break  # Exit loop once a handler provides a response
            else:
                logger_actual.info(
                    f"[HANDLER_CHAIN] Handler {handler_class_iter_main.__name__} no respondió."
                )

    # --- Re-evaluación de intención si hay imagen sin texto y la intención es genérica ---
    if not respuesta_final and context.get("es_foto") and not pregunta_str.strip() and \
       context.get("intencion") in ["general", "pregunta_general", None]:
        logger_actual.info(f"[RE-ROUTE IMAGE INTENT] Imagen detectada sin texto y con intención débil ('{context.get('intencion')}'). "
                           f"Forzando 'iniciar_reclamo' y re-intentando con ReclamoHandler.")
        context["intencion"] = "iniciar_reclamo"

        # Limpiar estado si no es un estado de reclamo, para que ReclamoHandler empiece de cero.
        current_memoria_state_for_reroute_raw = contexto_municipio_actual.get("estado_conversacion")
        current_memoria_state_for_reroute_enum = None
        if isinstance(current_memoria_state_for_reroute_raw, str): # Puede ser string si ya se serializó
            try: current_memoria_state_for_reroute_enum = ConversationState[current_memoria_state_for_reroute_raw]
            except KeyError: pass
        elif isinstance(current_memoria_state_for_reroute_raw, ConversationState): # O Enum si no se serializó aún
            current_memoria_state_for_reroute_enum = current_memoria_state_for_reroute_raw

        if current_memoria_state_for_reroute_enum not in RECLAMO_STATES and \
           current_memoria_state_for_reroute_enum is not None:
            logger_actual.info(f"[RE-ROUTE IMAGE INTENT] Estado actual '{current_memoria_state_for_reroute_enum.name}' no es de reclamo. Limpiando contexto de municipio.")
            # Guardar interacciones_anon_sesion si existe, para no resetear el contador de sugerencia de registro
            interacciones_previas = contexto_municipio_actual.get("interacciones_anon_sesion")
            contexto_municipio_actual.clear()
            if interacciones_previas is not None:
                contexto_municipio_actual["interacciones_anon_sesion"] = interacciones_previas
            contexto_municipio_actual["estado_conversacion"] = None # Asegurar que esté explícitamente None como string o antes de serializar
            context[CONTEXTO_MUNICIPIO] = contexto_municipio_actual # Actualizar el 'context' que usa el handler

        # Re-intentar con ReclamoHandler (y ReclamoInteligente por si acaso)
        # Esto asume que ReclamoHandler no fue el que ya retornó None para esta misma situación.
        # Si el análisis de imagen llenó datos, ReclamoInteligente podría actuar.
        for handler_class_reroute in [ReclamoInteligenteMunicipioHandler, ReclamoHandler]:
            # Restaurar el estado de conversación a Enum para el handler si es necesario
            # (ya debería estar como Enum si no se ha serializado, o None)
            state_before_reroute_call_raw = context[CONTEXTO_MUNICIPIO].get("estado_conversacion")
            state_before_reroute_call_enum = None
            if isinstance(state_before_reroute_call_raw, str):
                try: state_before_reroute_call_enum = ConversationState[state_before_reroute_call_raw]
                except KeyError: pass
            elif isinstance(state_before_reroute_call_raw, ConversationState):
                 state_before_reroute_call_enum = state_before_reroute_call_raw
            context[CONTEXTO_MUNICIPIO]["estado_conversacion"] = state_before_reroute_call_enum


            handler_instance_reroute = handler_class_reroute(context)
            logger_actual.info(f"[RE-ROUTE IMAGE INTENT] Re-intentando con handler: {handler_class_reroute.__name__}")
            respuesta_reroute = handler_instance_reroute.handle(received_payload)

            state_after_reroute_handler = context[CONTEXTO_MUNICIPIO].get("estado_conversacion")
            if isinstance(state_after_reroute_handler, ConversationState): # Serializar para el contexto principal
                context[CONTEXTO_MUNICIPIO]["estado_conversacion"] = state_after_reroute_handler.name
            elif state_after_reroute_handler is None:
                context[CONTEXTO_MUNICIPIO].pop("estado_conversacion", None)


            if respuesta_reroute:
                respuesta_final = respuesta_reroute
                logger_actual.info(f"[RE-ROUTE IMAGE INTENT] Handler {handler_class_reroute.__name__} respondió en re-intento.")
                break


    if not respuesta_final:
        logger_actual.info(
            "[HANDLER_CHAIN_FALLBACK] Ningún handler respondió (incluso tras re-intento por imagen). Usando fallback general."
        )
        current_fallback_state_raw = contexto_municipio_actual.get("estado_conversacion")
        current_fallback_state_enum = None
        if isinstance(current_fallback_state_raw, str):
            try:
                current_fallback_state_enum = ConversationState[current_fallback_state_raw]
            except KeyError:
                pass
        elif isinstance(current_fallback_state_raw, ConversationState):
            current_fallback_state_enum = current_fallback_state_raw

        current_fallback_state = contexto_municipio_actual.get("estado_conversacion") # Ya debería ser string o None aquí

        options_fallback = [
            {"id": "iniciar_reclamo_fallback_main", "texto": "Hacer un reclamo"},
            {"id": "consultar_tramite_fallback_main", "texto": "Consultar un trámite"},
            {"id": "hablar_con_agente_fallback_main", "texto": "Hablar con un agente"},
        ]
        message_type_fallback = "interactive_buttons"

        if current_fallback_state: # Si AÚN hay un estado aquí (ej. si el re-intento de ReclamoHandler lo seteó pero retornó None)
            estado_log_val = current_fallback_state # Ya es string o None
            # No limpiar contexto aquí si el ReclamoHandler ya lo preparó para pedir algo.
            # El mensaje de fallback debería ser más genérico.
            logger_actual.warning(
                f"[FALLBACK_WARN] Fallback con estado activo '{estado_log_val}' (posiblemente del re-intento de ReclamoHandler)."
            )
            body_fallback = ( # Mensaje más genérico si hay un estado activo que no llevó a respuesta
                "No estoy seguro de cómo continuar desde aquí. ¿Podrías intentar reformular o elegir una opción?"
            )
        else: # No hay estado activo
            body_fallback = (
                "Disculpa, no estoy seguro de haber entendido bien tu consulta. ¿Podrías intentar reformular tu pregunta o elegir una de estas opciones?"
            )

        respuesta_final = {
            "message_body": body_fallback,
            "options_list": options_fallback,
            "message_type": message_type_fallback,
            "fuente": "municipio_fallback_general_v3", # v3 para diferenciar
        }

    # Ensure estado_conversacion within contexto_municipio_actual is a string name if it's an Enum,
    # or remove if None, before general serialization for DB.
    estado_final_en_memoria = contexto_municipio_actual.get("estado_conversacion")
    if isinstance(estado_final_en_memoria, ConversationState):
        contexto_municipio_actual["estado_conversacion"] = estado_final_en_memoria.name
        logger_actual.info(
            f"[CONTEXTO_MUNICIPIO_PRE_SERIALIZE_MAIN] Estado Enum '{estado_final_en_memoria.name}' convertido a string."
        )
    elif estado_final_en_memoria is None:
        contexto_municipio_actual.pop("estado_conversacion", None)
        logger_actual.info(f"[CONTEXTO_MUNICIPIO_PRE_SERIALIZE_MAIN] Estado es None.")
    # else: it's already a string or other non-Enum (but potentially non-JSON-serializable) type.
    # serializar_enum below will handle other nested Enums.

    # Apply the recursive serializar_enum to the whole contexto_municipio_actual
    # before assigning it to chat_db_context.context_data
    contexto_municipio_serializado_para_db = serializar_enum(contexto_municipio_actual)

    chat_db_context.context_data[CONTEXTO_MUNICIPIO] = contexto_municipio_serializado_para_db  # Use the fully serialized version
    logger_actual.info(
        f"[CONTEXTO_MUNICIPIO_POST_SAVE_IN_DB_CONTEXT] Contexto municipio (serializado para DB) asignado a chat_db_context.data: {contexto_municipio_serializado_para_db}"
    )

    if chat_db_context:
        flag_modified(chat_db_context, "context_data")

    # For the HTTP response, we can use the same serialized version
    # No need to call serializar_enum again if it was already done for DB.
    contexto_serializado_para_respuesta_http = contexto_municipio_serializado_para_db

    media_url_to_send = contexto_serializado_para_respuesta_http.get("foto_url") # Already serialized
    location_data_to_send = contexto_serializado_para_respuesta_http.get("ubicacion_gps") # Already serialized

    message_body_final = (
        respuesta_final.get("message_body") or respuesta_final.get("respuesta", "")
    )
    options_list_final = respuesta_final.get("options_list") or respuesta_final.get(
        "botones", []
    )

    message_type_final = "text"
    if isinstance(options_list_final, list) and options_list_final:
        num_options = len(options_list_final)
        if 0 < num_options <= 3:
            message_type_final = respuesta_final.get("message_type") or "interactive_buttons"
        elif num_options > 3:
            message_type_final = respuesta_final.get("message_type") or "interactive_list"
        if "message_type" in respuesta_final and respuesta_final["message_type"]:
            message_type_final = respuesta_final["message_type"]

    final_response_dict = {
        "message_body": message_body_final,
        "options_list": options_list_final,
        "message_type": message_type_final,
        "contexto_actualizado": {CONTEXTO_MUNICIPIO: contexto_serializado_para_respuesta_http},
        "ticket_id": respuesta_final.get("ticket_id", None),
        "media_url": media_url_to_send,
        "location_data": location_data_to_send,
        "adjuntos": [],
        "fuente": respuesta_final.get("fuente", "desconocida"),
    }

    uploaded_file_info = received_payload.get("uploaded_file_info")
    if uploaded_file_info and isinstance(uploaded_file_info, dict):
        if uploaded_file_info.get("url") and uploaded_file_info.get("name"):
            final_response_dict["adjuntos"].append(
                {
                    "nombre_original": uploaded_file_info["name"],
                    "url_descarga": uploaded_file_info["url"],
                    "tipo_mime": uploaded_file_info.get("type", "application/octet-stream"),
                }
            )
            logger_actual.info(f"Adjuntando info de archivo subido: {uploaded_file_info['name']}")

    respuesta_log = (final_response_dict.get("message_body") or "")[:100]
    adjuntos_len = len(final_response_dict.get("adjuntos", []))
    logger_actual.info(
        f"[RESPONDER_MUNICIPIO_END] Respuesta: '{respuesta_log}...', Opciones: {len(options_list_final)}, TipoMsg: {message_type_final}, Fuente: {final_response_dict['fuente']}, Adjuntos: {adjuntos_len}"
    )

    if anon_id and not viewer_user and respuesta_final and isinstance(respuesta_final, dict):
        try:
            db.session.add(
                Conversacion(
                    session_id=kwargs.get("chat_session_uuid") or anon_id,
                    pregunta=pregunta_str,
                    respuesta=final_response_dict.get("message_body"),
                    fuente=final_response_dict.get("fuente", "municipio_anon_respuesta"),
                    rubro=getattr(context.get("rubro_obj"), "nombre", "municipio_general"),
                    user_id=None,
                )
            )
            db.session.commit()
            logger_actual.info(
                f"Conversación (municipio) para anon_id {anon_id}/session {kwargs.get('chat_session_uuid')} guardada."
            )
        except Exception as e_conv_muni:
            logger_actual.error(
                f"Error guardando conversación de municipio para anon_id {anon_id}/session {kwargs.get('chat_session_uuid')}: {e_conv_muni}",
                exc_info=True,
            )
            db.session.rollback()

    return final_response_dict