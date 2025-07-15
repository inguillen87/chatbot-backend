import sys
import os
import logging
import re

project_root_municipios_svc = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if project_root_municipios_svc not in sys.path:
    sys.path.insert(0, project_root_municipios_svc)
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
# _clasificar_intencion_con_llm removed from logic.py, so remove import here
from .logic import (
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

def extract_description_and_check_confirmation(text: str, confirmation_keywords: set) -> tuple[str | None, bool]:
    """
    Extracts description from text and checks for a confirmation intent.
    Returns a tuple: (extracted_description, has_confirmation_intent).
    """
    if not text:
        return None, False

    normalized_text = normalizar_texto(text.strip())

    # Sort keywords by length to match longer phrases first (e.g., "confirmar reclamo" before "confirmar")
    sorted_confirmation_keywords = sorted(list(confirmation_keywords), key=len, reverse=True)

    extracted_description = normalized_text
    has_confirmation_intent = False

    for keyword in sorted_confirmation_keywords:
        # Check if the text ends with the keyword, possibly preceded by a space, comma, or period.
        # Example: "description keyword", "description, keyword", "description. keyword"
        # Or if the keyword itself is a multi-word phrase like "confirmar reclamo"

        # Pattern 1: "description [., ]keyword"
        # Regex to find keyword at the end, possibly preceded by common separators
        # This needs to be careful not to strip too much if keyword is part of a legit description.
        # Let's simplify: if a keyword is present AND the phrase is short, or keyword is at the end.

        # If the keyword is a multi-word phrase itself (e.g., "confirmar reclamo")
        if " " in keyword: # Multi-word keyword
            if normalized_text.endswith(keyword):
                # If a multi-word confirmation keyword is found at the end, it's a strong signal.
                extracted_description = normalized_text[:-len(keyword)].strip(" .,")
                if not extracted_description: # If original text was ONLY the multi-word keyword
                    extracted_description = None
                has_confirmation_intent = True
                break
        else: # Single-word keyword
            # Check if keyword is at the very end
            if normalized_text == keyword: # Input is ONLY the keyword
                extracted_description = None
                has_confirmation_intent = True
                break
            # Check if text ends with " keyword"
            if normalized_text.endswith(f" {keyword}"):
                potential_description = normalized_text[:-(len(keyword) + 1)].strip()
                # If description is short, or keyword is strong.
                if len(potential_description.split()) <= 4 or not potential_description: # Allow slightly longer desc like "nada de eso confirmar"
                    extracted_description = potential_description if potential_description else None
                    has_confirmation_intent = True
                    break
            # Check if text ends with ",keyword" or ".keyword" (less common for natural language confirmation)
            for separator in [",", "."]:
                if normalized_text.endswith(f"{separator}{keyword}"):
                    potential_description = normalized_text[:-(len(keyword) + 1)].strip()
                    if len(potential_description.split()) <= 4 or not potential_description:
                        extracted_description = potential_description if potential_description else None
                        has_confirmation_intent = True
                        break
            if has_confirmation_intent:
                break

    # If no specific pattern matched but a keyword is in a short text.
    # This is a bit risky as "yes" or "ok" can be part of a description.
    # Let's refine: if the *entire input* is very similar to a confirmation phrase or is short and contains one.
    if not has_confirmation_intent:
        # Check if the whole normalized_text is just a keyword or a keyword plus very little else
        # e.g. "confirmar el reclamo" "si confirmar" "listo ok"
        # This part needs to be more robust. For now, the endswith logic is primary.
        # The critical case "nada confirmar el reclamo" should be caught if "confirmar reclamo" is a keyword.
        # Let's add "confirmar reclamo" to PALABRAS_CLAVE_CONFIRMACION for this.
        # The sorted_confirmation_keywords will handle it.
        pass


    # If after stripping, the description is one of the keywords itself, it means the original was likely just "keyword keyword"
    # or the description part was empty.
    if extracted_description and extracted_description in confirmation_keywords and has_confirmation_intent:
        extracted_description = None # User likely meant only to confirm.

    # If the original text was short and contained a keyword, it's likely a confirmation.
    # The `endswith` logic handles this for clearer cases.
    # This is a fallback for inputs like "ok es todo" -> desc: "ok es todo", confirm: False (which is correct)
    # vs "es todo ok" -> desc: "es todo", confirm: True (if "ok" is a keyword)

    # If has_confirmation_intent is true, extracted_description is what remains.
    # If has_confirmation_intent is false, extracted_description is the original normalized_text.
    if not has_confirmation_intent:
        extracted_description = text.strip() # Return original cleaned text if no confirm intent

    # Final check: if description is empty and intent is true, set desc to a placeholder if needed by caller
    # For now, None is acceptable for an empty description part.
    # logger.info(f"[extract_description_and_check_confirmation] Input: '{text}', Normalized: '{normalized_text}', Extracted Desc: '{extracted_description}', Confirmed: {has_confirmation_intent}")
    return extracted_description, has_confirmation_intent

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
    ESPERANDO_INFO_RECLAMO_LLM = auto() # Nuevo estado para cuando el LLM está recopilando info para un reclamo
    CONVERSACION_GENERAL_LLM = auto() # Nuevo estado para cuando el LLM está en una conversación general

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

    # --- LLM Call Removed - Simplified Heuristic ---
    # The main Gemini call should handle intent changes. This function is now a simpler guard.
    # If the input is very short and not a clear expected simple response, assume it might be new.
    # For more complex cases, the main LLM (Gemini) should detect a change of topic/intent.

    palabras_clave_continuacion_simple = {"si", "sí", "no", "ok", "dale", "listo", "bueno", "afirmativo", "negativo"}
    palabras_clave_cancelacion = {"cancelar", "salir", "menu", "menú", "ayuda", "inicio"}
    palabras_clave_saludo = {"hola", "buen día", "buen dia", "buenas tardes", "buenas noches"}


    if texto_norm in palabras_clave_cancelacion:
        logger.info(f"[Guardián de Flujo Simplificado] Cancelación/Interrupción detectada: '{texto_norm}' -> PREGUNTA_NUEVA")
        return True

    if texto_norm in palabras_clave_saludo and len(texto_norm.split()) <= 2:
        logger.info(f"[Guardián de Flujo Simplificado] Saludo detectado: '{texto_norm}' -> PREGUNTA_NUEVA")
        return True

    # If expecting a simple confirmation and got one, it's NOT a new question.
    if tipo_esperado == "una confirmación (sí o no)" and texto_norm in palabras_clave_continuacion_simple:
        logger.info(f"[Guardián de Flujo Simplificado] Confirmación simple esperada y recibida: '{texto_norm}' -> NO ES PREGUNTA_NUEVA")
        return False

    # If expecting a rating and got a number 1-5, it's NOT a new question.
    if tipo_esperado == "una calificación del 1 al 5" and re.fullmatch(r"[1-5]", texto_norm):
        logger.info(f"[Guardián de Flujo Simplificado] Calificación esperada y recibida: '{texto_norm}' -> NO ES PREGUNTA_NUEVA")
        return False

    # If expecting a ticket number and got something that looks like one.
    if tipo_esperado == "un número de ticket" and (re.fullmatch(r"m?\-?\d{4,}", texto_norm) or texto_norm.isdigit()):
        logger.info(f"[Guardián de Flujo Simplificado] Número de ticket esperado y recibido: '{texto_norm}' -> NO ES PREGUNTA_NUEVA")
        return False

    # If the input is very short (1-2 words) and NOT one of the simple continuation keywords,
    # it's more likely a new question or an attempt to break flow, especially if complex data was expected.
    if len(texto_norm.split()) <= 2 and texto_norm not in palabras_clave_continuacion_simple:
        # Further check: if complex data like an address was expected, even "ok" could be a sign of breaking flow.
        # This heuristic is tricky. For now, a short, non-keyword response is considered new.
        logger.info(f"[Guardián de Flujo Simplificado] Input corto ('{texto_norm}') no es palabra clave de continuación -> PREGUNTA_NUEVA")
        return True

    # If the input is longer, assume it's an attempt to provide the expected data or continue.
    # The main LLM (Gemini) will be responsible for identifying if this longer input is off-topic.
    logger.info(f"[Guardián de Flujo Simplificado] Input ('{texto_norm}') no es una interrupción obvia. Asumiendo continuación -> NO ES PREGUNTA_NUEVA")
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
        pregunta_str = payload.get("pregunta", "").strip()
        memoria = self.context[CONTEXTO_MUNICIPIO]
        texto_normalizado = normalizar_texto(pregunta_str)

        logger.info(f"[IntentClassifierHandler ENTRY] Pregunta: '{pregunta_str[:100]}...', Intención en Contexto: {self.context.get('intencion')}, Estado Memoria: {memoria.get('estado_conversacion')}")

        # --- Helper function for clearing context while preserving image analysis ---
        # (Moved here for clarity as it's used multiple times in this handler)
        def clear_memoria_preserving_image_analysis(m):
            image_analysis_data = m.get("analisis_imagen_reclamo_auto_raw")
            categoria_prefill = m.get("categoria_reclamo")
            descripcion_prefill = m.get("descripcion_reclamo")
            is_prefill_from_image = image_analysis_data is not None
            m.clear()
            if image_analysis_data:
                m["analisis_imagen_reclamo_auto_raw"] = image_analysis_data
                if is_prefill_from_image:
                    if categoria_prefill: m["categoria_reclamo"] = categoria_prefill
                    if descripcion_prefill: m["descripcion_reclamo"] = descripcion_prefill
                logger.info("[IntentClassifier] Memoria limpiada, datos de análisis de imagen preservados.")
        # --- End Helper ---

        # 1. Prioritize LLM-derived intent if specific and actionable
        llm_intent = self.context.get("intencion")
        llm_datos_accion = self.context.get("datos_accion") # From main Gemini call

        # If LLM intent is to execute a tool, ensure it's passed to ToolHandler
        if llm_intent == "ejecutar_herramienta" and isinstance(llm_datos_accion, dict) and llm_datos_accion.get("nombre_herramienta"):
            logger.info(f"[IntentClassifierHandler] Intención LLM es '{llm_intent}' para herramienta '{llm_datos_accion.get('nombre_herramienta')}'. Cediendo a ToolHandler.")
            # No need to change context["intencion"] here, it's already set. ToolHandler will use it.
            return None # ToolHandler will be called later in the chain

        # Handle critical overrides (panic, agent) based on keywords, even if LLM intent exists,
        # unless an active state already handles them (e.g. ReclamoHandler in specific state).
        # This check is important for safety and immediate user needs.
        if any(kw in texto_normalizado for kw in self.KEYWORDS_PANICO):
            self.context["intencion"] = "activar_panico"
            clear_memoria_preserving_image_analysis(memoria)
            logger.info(f"[IntentClassifierHandler] PANICO detectado por keyword. Intención forzada a 'activar_panico'.")
            return None # PanicButtonHandler should take over

        if any(kw in texto_normalizado for kw in self.KEYWORDS_AGENTE):
            # Check if already in a flow that might have its own agent escalation
            current_state_val_for_agent = memoria.get("estado_conversacion")
            current_state_enum_for_agent = None
            if isinstance(current_state_val_for_agent, ConversationState): current_state_enum_for_agent = current_state_val_for_agent
            elif isinstance(current_state_val_for_agent, str):
                try: current_state_enum_for_agent = ConversationState[current_state_val_for_agent]
                except KeyError: pass

            if not (current_state_enum_for_agent and current_state_enum_for_agent in RECLAMO_STATES): # Avoid double handling if ReclamoHandler handles agent in-flow
                self.context["intencion"] = "hablar_con_agente"
                clear_memoria_preserving_image_analysis(memoria)
                logger.info(f"[IntentClassifierHandler] AGENTE detectado por keyword. Intención forzada a 'hablar_con_agente'.")
                return None # HumanEscalationHandler should take over

        # 2. Handle image-related intent priority
        # If intent is 'iniciar_reclamo' (possibly from image analysis in responder_municipio) AND it's an image
        if llm_intent == "iniciar_reclamo" and self.context.get("es_foto"):
            if not pregunta_str: # Image sent alone
                logger.info(f"[IntentClassifierHandler] Intención 'iniciar_reclamo' (por imagen sin texto) es prioritaria. Manteniendo.")
                return None # Let ReclamoHandler proceed
            else: # Image sent with text
                # Keywords for AGENTE or PANICO in the accompanying text can override 'iniciar_reclamo'
                # This is already handled by the critical override checks above.
                # If not overridden, 'iniciar_reclamo' from image + text remains.
                logger.info(f"[IntentClassifierHandler] Intención 'iniciar_reclamo' (por imagen CON texto) es prioritaria. Texto: '{pregunta_str}'. Manteniendo.")
                return None # Let ReclamoHandler proceed

        # 3. Respect active conversation states (especially multi-step flows like reclamo)
        current_context_state_val = memoria.get("estado_conversacion")
        current_state_enum = None

        if isinstance(current_context_state_val, ConversationState):
            current_state_enum = current_context_state_val
        elif isinstance(current_context_state_val, str):
            try:
                current_state_enum = ConversationState[current_context_state_val]
            except KeyError:
                pass # current_state_enum remains None

        current_context_state_val = memoria.get("estado_conversacion")
        current_state_enum = None
        if isinstance(current_context_state_val, ConversationState): current_state_enum = current_context_state_val
        elif isinstance(current_context_state_val, str):
            try: current_state_enum = ConversationState[current_context_state_val]
            except KeyError: pass

        # If there's an active state (e.g., multi-step reclamo), let its handler manage the flow.
        # Critical overrides (panic, agent) are checked before this.
        # This IntentClassifier should not override an ongoing, specific flow unless it's a critical interrupt.
        if current_state_enum:
            logger.info(f"[IntentClassifierHandler] Estado activo '{current_state_enum.name}'. Cediendo control al handler del estado.")
            # Set a generic 'continuar_flujo' if LLM didn't provide a more specific one,
            # to ensure the state's handler gets a chance.
            if not llm_intent or llm_intent in ["no_accion", "small_talk", "pregunta_general"]:
                self.context["intencion"] = "continuar_flujo" # Ensure the state handler runs
            return None # Let the state's owner handler proceed

        # 4. Use LLM-derived intent if it's specific and no active state is overriding.
        # Actionable intents that don't need further keyword processing here.
        actionable_llm_intents = [
            "iniciar_reclamo", "consultar_estado_ticket", "consultar_tramite",
            "hacer_sugerencia", "iniciar_compra", "ver_carrito", "proceder_al_pago",
            "solicitar_ubicacion_tienda", "reclamo_pedido"
            # "ejecutar_herramienta", "activar_panico", "hablar_con_agente" are handled earlier or by specific handlers.
        ]
        if llm_intent in actionable_llm_intents:
            logger.info(f"[IntentClassifierHandler] Usando intención LLM directa: '{llm_intent}'.")
            # The intent is already in self.context["intencion"].
            # Ensure memoria is clear if this is a new primary intent and not a continuation.
            # This is tricky; if LLM initiated a new flow, memoria should be clear.
            # If it's clarifying a previous generic query, memoria might have useful context.
            # For now, assume if LLM gives a strong new intent, prior generic context in memoria is less relevant.
            # However, clear_memoria_preserving_image_analysis might be too broad if not an image flow.
            # Let's be conservative: if LLM gives a new primary actionable intent, we assume it's a fresh start for that flow.
            # The specific handlers (ReclamoHandler, etc.) are responsible for their own memory initialization.
            # This handler's job is just to ensure the correct intent is set.
            return None # Let the corresponding handler (Reclamo, TicketStatus, etc.) pick it up.

        # 5. Fallback to keyword-based classification if LLM intent is generic or absent
        #    AND no active state is present.
        #    (Critical keyword overrides for panic/agent are already done above).
        
        logger.info(f"[IntentClassifierHandler] Intención LLM ('{llm_intent}') no es directamente accionable o está ausente. Procediendo con fallback a keywords.")

        # --- Keyword-based classification (as fallback) ---
        # Explicit RECLAMO phrases
        reclamo_phrases = [
            "quiero hacer un reclamo", "necesito hacer un reclamo", "vengo a reclamar", 
            "hacer un reclamo", "presentar una queja", "reportar un problema"
        ]
        for phrase in reclamo_phrases:
            if normalizar_texto(phrase) in texto_normalizado:
                self.context["intencion"] = "iniciar_reclamo"
                logger.info(f"[IntentClassifierHandler KW_FB] Intención: iniciar_reclamo (frase explícita '{phrase}')")
                return None

        # General RECLAMO keywords
        for kw in self.KEYWORDS_RECLAMO:
            if kw in texto_normalizado: 
                self.context["intencion"] = "iniciar_reclamo"
                logger.info(f"[IntentClassifierHandler KW_FB] Intención: iniciar_reclamo (keyword general '{kw}')")
                return None
        
        # TICKET STATUS
        for kw in self.KEYWORDS_TICKET_STATUS:
            if kw in texto_normalizado:
                self.context["intencion"] = "consultar_estado_ticket"
                logger.info(f"[IntentClassifierHandler KW_FB] Intención: consultar_estado_ticket (keyword '{kw}')")
                return None
        
        # TRAMITE
        for kw in self.KEYWORDS_TRAMITE:
            if kw in texto_normalizado:
                self.context["intencion"] = "consultar_tramite"
                logger.info(f"[IntentClassifierHandler KW_FB] Intención: consultar_tramite (keyword '{kw}')")
                return None
        
        # SUGERENCIA
        for kw in self.KEYWORDS_SUGERENCIA:
            if kw in texto_normalizado:
                self.context["intencion"] = "hacer_sugerencia"
                logger.info(f"[IntentClassifierHandler KW_FB] Intención: hacer_sugerencia (keyword '{kw}')")
                return None

        # PYME specific keywords (if target is pyme or ambiguous and these appear)
        # This part might need refinement based on how `target` from LLM is used.
        # For now, if LLM didn't set a strong municipal intent, check PYME keywords.
        if llm_datos_accion and llm_datos_accion.get("target") in ["pyme", "ambos", None]: # Or if target is not strictly municipio
            for kw_pyme, intent_pyme in [
                (self.KEYWORDS_INICIAR_COMPRA, "iniciar_compra"),
                (self.KEYWORDS_VER_CARRITO, "ver_carrito"),
                (self.KEYWORDS_PAGAR, "proceder_al_pago"),
                (self.KEYWORDS_UBICACION_TIENDA, "solicitar_ubicacion_tienda"),
                (self.KEYWORDS_RECLAMO_PEDIDO, "reclamo_pedido")
            ]:
                for kw in kw_pyme:
                    if kw in texto_normalizado:
                        self.context["intencion"] = intent_pyme
                        # memoria.clear() # PYME handlers usually manage their own context
                        logger.info(f"[IntentClassifierHandler KW_FB] Intención PYME: {intent_pyme} (keyword '{kw}')")
                        return None

        # Direct category match for RECLAMO (final keyword check)
        if not self.context.get("intencion") and len(texto_normalizado.split()) <= 3:
            if texto_normalizado in categorias_normalizadas:
                self.context["intencion"] = "iniciar_reclamo"
                logger.info(f"[IntentClassifierHandler KW_FB] Intención: iniciar_reclamo (match directo de categoría corta '{texto_normalizado}')")
                return None

        # If after all this, no specific intent is set by keywords,
        # and LLM intent was generic (like 'small_talk', 'no_accion', 'pregunta_general'),
        # we honor that generic LLM intent. If LLM intent was None, default to 'pregunta_general'.
        if not self.context.get("intencion"): # If keyword fallbacks didn't set anything
            if llm_intent and llm_intent not in ["ejecutar_herramienta", "activar_panico", "hablar_con_agente", "no_accion"]: # Don't override these critical ones if they somehow reached here
                self.context["intencion"] = llm_intent # Honor original generic LLM intent
                logger.info(f"[IntentClassifierHandler] No keyword match. Usando intención genérica original de LLM: '{llm_intent}'")
            else: # If LLM intent was also None or a critical one we shouldn't default to
                self.context["intencion"] = "pregunta_general" # Default fallback
                logger.info(f"[IntentClassifierHandler] No keyword match y sin intención LLM clara (o era 'no_accion'). Default a 'pregunta_general'.")

        return None # Let GeneralHandler or SmallTalkHandler pick up based on the (possibly now generic) intent.


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
                memoria.clear()
                return {"message_body": "¡Excelente! Me alegro de que tu problema se haya resuelto. Gracias por tu colaboración.", "options_list": [], "message_type": "text", "fuente": "ticket_status_resuelto_cierre_v2"}
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

    def handle(self, payload: dict) -> dict | None:
        pregunta_str = payload.get("pregunta", "")
        memoria = self.context[CONTEXTO_MUNICIPIO]

        if self.context.get("intencion") != "iniciar_reclamo" or memoria.get("estado_conversacion"):
            return None

        # Usar LLM para extraer detalles del reclamo de la pregunta del usuario
        datos_extraidos = extract_complaint_details_llm(pregunta_str)
        if not datos_extraidos:
            return None

        # Limpiar la memoria de reclamo antes de llenarla con nuevos datos
        for campo in self.CAMPOS_RECLAMO:
            memoria.pop(f"{campo}_reclamo", None)

        # Llenar la memoria con los datos extraídos
        if datos_extraidos.get("tipo_problema"):
            memoria["categoria_reclamo"] = datos_extraidos["tipo_problema"]
        if datos_extraidos.get("ubicacion_problema"):
            memoria["direccion_reclamo"] = datos_extraidos["ubicacion_problema"]
        if datos_extraidos.get("descripcion_problema"):
            memoria["descripcion_reclamo"] = datos_extraidos["descripcion_problema"]

        # Extraer también detalles de contacto si están presentes
        datos_contacto = extract_multiple_contact_details_llm(pregunta_str, ["nombre_cliente", "telefono_cliente", "email_cliente"])
        if datos_contacto.get("nombre_cliente"):
            memoria["nombre_vecino"] = datos_contacto["nombre_cliente"]
        if datos_contacto.get("telefono_cliente"):
            memoria["telefono_vecino"] = datos_contacto["telefono_cliente"]
        if datos_contacto.get("email_cliente"):
            memoria["email_vecino"] = datos_contacto["email_cliente"]

        # Determinar el siguiente paso
        campos_requeridos = ["categoria_reclamo", "direccion_reclamo", "descripcion_reclamo", "nombre_vecino", "telefono_vecino", "email_vecino"]
        for campo in campos_requeridos:
            if not memoria.get(campo):
                estado_str = f"ESPERANDO_{campo.upper()}"
                if hasattr(ConversationState, estado_str):
                    memoria["estado_conversacion"] = getattr(ConversationState, estado_str).name
                    return None  # Dejar que ReclamoHandler pida la información que falta

        # Si todos los datos están presentes, pasar a la confirmación
        memoria["estado_conversacion"] = ConversationState.ESPERANDO_CONFIRMACION_RECLAMO.name
        return None

class ReclamoHandler(BaseMunicipioHandler):
    EDIT_KEYWORDS = ["editar", "cambiar", "corregir", "modificar", "no era asi", "me equivoque", "error"]

    def handle(self, payload: dict) -> dict | None:
        pregunta_str = payload.get("pregunta", "") or ""
        memoria = self.context[CONTEXTO_MUNICIPIO]
        estado_str = memoria.get("estado_conversacion")
        estado = ConversationState[estado_str] if isinstance(estado_str, str) else estado_str

        if not estado or estado not in RECLAMO_STATES:
            return None

        # Si el estado es esperar categoría y ya la tenemos, avanzamos
        if estado == ConversationState.ESPERANDO_CATEGORIA_RECLAMO and memoria.get("categoria_reclamo"):
            estado = self.avanzar_estado(memoria)
        
        # Procesar el input del usuario para el estado actual
        if pregunta_str:
            self.procesar_input_para_estado(pregunta_str, estado, memoria)
            estado = self.avanzar_estado(memoria)

        # Generar respuesta basada en el estado actual
        return self.generar_respuesta_para_estado(estado, memoria)

    def avanzar_estado(self, memoria: dict) -> ConversationState:
        campos_requeridos = ["categoria_reclamo", "direccion_reclamo", "descripcion_reclamo", "nombre_vecino", "telefono_vecino", "email_vecino"]
        for campo in campos_requeridos:
            if not memoria.get(campo):
                estado_str = f"ESPERANDO_{campo.upper()}"
                nuevo_estado = getattr(ConversationState, estado_str)
                memoria["estado_conversacion"] = nuevo_estado.name
                return nuevo_estado

        memoria["estado_conversacion"] = ConversationState.ESPERANDO_CONFIRMACION_RECLAMO.name
        return ConversationState.ESPERANDO_CONFIRMACION_RECLAMO

    def procesar_input_para_estado(self, pregunta: str, estado: ConversationState, memoria: dict):
        if estado == ConversationState.ESPERANDO_CATEGORIA_RECLAMO:
            memoria["categoria_reclamo"] = pregunta
        elif estado == ConversationState.ESPERANDO_DIRECCION_RECLAMO:
            memoria["direccion_reclamo"] = pregunta
        elif estado == ConversationState.ESPERANDO_DESCRIPCION_RECLAMO:
            memoria["descripcion_reclamo"] = pregunta
        elif estado == ConversationState.ESPERANDO_NOMBRE_VECINO:
            memoria["nombre_vecino"] = pregunta
        elif estado == ConversationState.ESPERANDO_TELEFONO_VECINO:
            memoria["telefono_vecino"] = pregunta
        elif estado == ConversationState.ESPERANDO_EMAIL_VECINO:
            memoria["email_vecino"] = pregunta

    def generar_respuesta_para_estado(self, estado: ConversationState, memoria: dict) -> dict:
        if estado == ConversationState.ESPERANDO_CATEGORIA_RECLAMO:
            return {"message_body": "Por favor, indicá la categoría de tu reclamo."}
        elif estado == ConversationState.ESPERANDO_DIRECCION_RECLAMO:
            return {"message_body": "Por favor, indicá la dirección del reclamo."}
        elif estado == ConversationState.ESPERANDO_DESCRIPCION_RECLAMO:
            return {"message_body": "Por favor, describí el problema."}
        elif estado == ConversationState.ESPERANDO_NOMBRE_VECINO:
            return {"message_body": "Por favor, decime tu nombre completo."}
        elif estado == ConversationState.ESPERANDO_TELEFONO_VECINO:
            return {"message_body": "Por favor, decime tu número de teléfono."}
        elif estado == ConversationState.ESPERANDO_EMAIL_VECINO:
            return {"message_body": "Por favor, decime tu email."}
        elif estado == ConversationState.ESPERANDO_CONFIRMACION_RECLAMO:
            resumen = self.build_detalles_memoria(memoria)
            return {
                "message_body": f"Por favor, revisá si todos los datos son correctos:\n\n{resumen}\n\n¿Confirmás el reclamo?",
                "options_list": [
                    {"id": "confirmar_reclamo_final", "texto": "Sí, confirmar"},
                    {"id": "editar_reclamo_datos", "texto": "No, editar"}
                ],
                "message_type": 'interactive_buttons'
            }
        return {"message_body": "No estoy seguro de cómo proceder. ¿Podrías intentarlo de nuevo?"}

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
            texto = normalizar_texto(pregunta_str) # pregunta_str is 'forestales' from button payload
            texto_con_sinonimos = aplicar_sinonimos(texto, TRAMITE_SYNONYMS)

            # Ensure texto_usuario_lower is defined if used, or use 'texto' (normalized pregunta_str)
            texto_usuario_lower_for_id_check = normalizar_texto(pregunta_str)

            # --- DEBUG LOGGING START ---
            loaded_tramites_keys = list(get_tramites_info().keys())
            logger.info(f"[TramitesHandler DEBUG] Input pregunta_str: '{pregunta_str}'")
            logger.info(f"[TramitesHandler DEBUG] Normalized texto: '{texto}'")
            logger.info(f"[TramitesHandler DEBUG] Texto con sinonimos: '{texto_con_sinonimos}'")
            logger.info(f"[TramitesHandler DEBUG] Keys in _TRAMITES_CACHE: {loaded_tramites_keys}")
            normalized_cache_keys = [normalizar_texto(k) for k in loaded_tramites_keys]
            logger.info(f"[TramitesHandler DEBUG] Normalized keys in _TRAMITES_CACHE: {normalized_cache_keys}")
            # --- DEBUG LOGGING END ---

            clave_tramite = next((k for k in loaded_tramites_keys if normalizar_texto(k) == texto_con_sinonimos), None)

            if not clave_tramite: # Try to find by ID if user clicked a button (redundant if first check is good)
                logger.info(f"[TramitesHandler DEBUG] First key match failed. Trying by ID check with: '{texto_usuario_lower_for_id_check}'")
                clave_tramite = next((k for k in loaded_tramites_keys if normalizar_texto(k) == texto_usuario_lower_for_id_check), None)

            if not clave_tramite: # Fuzzy match if still not found
                logger.info(f"[TramitesHandler DEBUG] Second key match failed. Trying fuzzy match.")
                all_tramite_names = loaded_tramites_keys + list(TRAMITE_SYNONYMS.keys())
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

                # Clear state and intent to prevent duplication on next interaction
                logger.info(f"[TramitesHandler] Trámite '{clave_tramite}' encontrado. Limpiando estado e intención post-respuesta.")
                memoria.pop("estado_conversacion", None)
                self.context["intencion"] = None

                return {
                    "message_body": descripcion_tramite, 
                    "options_list": options_post_info, # Generic options after info
                    "message_type": "interactive_buttons", # Assuming few generic options
                    "fuente": f"tramite_info_{normalizar_texto(clave_tramite)}_v2"
                    # "original_buttons_from_config": original_info_buttons # For web channel to potentially use
                }

            if "conducir" in texto and ("licencia" in texto or "carnet" in texto):
                # This specific path for "curso_licencia_info" sets its own state.
                # It should also clear the broader 'consultar_tramite' intent if it proceeds.
                memoria["estado_conversacion"] = ConversationState.ESPERANDO_PREGUNTA_CURSO_LICENCIA.name
                self.context["intencion"] = None # Consume 'consultar_tramite' as we are going into a sub-flow
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
        if not user_obj:
            logger.warning("[GeneralHandler] No hay user_obj (dueño del bot) en contexto. No se puede buscar en SitioWebInfo.")
            return None
        contexto_scraped = ""
        try:
            query_filter = {"user_id": user_obj.id}
            contenidos = SitioWebInfo.query.filter_by(**query_filter).all()
            textos_relevantes = [json.loads(item.datos_json).get("contenido", "") for item in contenidos if json.loads(item.datos_json).get("tipo") == "contenido_general"]
            contexto_scraped = " ".join(filter(None, textos_relevantes))
            if not contexto_scraped:
                logger.info(f"[GeneralHandler] No se encontró contenido 'contenido_general' en SitioWebInfo para user_id {user_obj.id}.")
                contexto_scraped = "No hay información general disponible del municipio en este momento."
        except Exception as e:
            logger.error(f"[GeneralHandler] Error al obtener contenido SitioWebInfo: {e}", exc_info=True)
            contexto_scraped = "Hubo un error al cargar la información general del municipio."

        logger.info(f"[GeneralHandler] Contexto de scraping para la pregunta '{pregunta_str[:100]}...':\n{contexto_scraped[:500]}...")
        prompt_final = PROMPT_MUNICIPIO_CON_CONTEXTO.format(contexto_scraped=contexto_scraped, pregunta_usuario=pregunta_str)
        # --- Modified to use llamar_gemini ---
        # The JULES_SYSTEM_PROMPT already defines the persona.
        # We need to construct the 'usuario' and 'historial' arguments for llamar_gemini.

        # Construct 'usuario_info_for_gemini' based on available context
        usuario_info_for_gemini = {
            "nombre": self.context.get("nombre_vecino") or getattr(self.context.get("viewer_user_obj"), "name", "Vecino"),
            "tipo_entidad": "municipio", # Explicitly municipio for this handler
            "ubicacion_conocida": self.context.get("direccion_reclamo") or getattr(self.context.get("viewer_user_obj"), "direccion", None),
            "contacto": {
                "telefono": self.context.get("telefono_vecino") or getattr(self.context.get("viewer_user_obj"), "telefono", None),
                "email": self.context.get("email_vecino") or getattr(self.context.get("viewer_user_obj"), "email", None)
            }
        }
        # Filter out None values from usuario_info_for_gemini for cleaner prompt
        usuario_info_for_gemini = {k: v for k, v in usuario_info_for_gemini.items() if v is not None}
        if usuario_info_for_gemini.get("contacto"):
             usuario_info_for_gemini["contacto"] = {k: v for k,v in usuario_info_for_gemini["contacto"].items() if v is not None}
             if not usuario_info_for_gemini["contacto"]: del usuario_info_for_gemini["contacto"]


        historial_chat_for_gemini = self.context.get("mensajes_previos", []) # Assuming this is in the right format

        # Augment the user's question with the scraped context for GeneralHandler
        # The JULES_SYSTEM_PROMPT instructs Gemini to use context if provided.
        # We can prepend the scraped context to the user's question for this specific handler.
        mensaje_a_gemini = f"Contexto del sitio web del municipio:\n{contexto_scraped}\n\nPregunta del usuario: {pregunta_str}"

        try:
            llm_response_structured = llamar_gemini(
                mensaje_usuario=mensaje_para_gemini,
                usuario=usuario_info_for_gemini,
                historial=historial_chat_para_gemini
            )
        except TypeError as e:
            # This is a specific catch for the 'mensaje' vs 'mensaje_usuario' error.
            if "got an unexpected keyword argument 'mensaje'" in str(e):
                logger_actual.error(f"[RESPONDER_MUNICIPIO] TypeError por keyword 'mensaje'. Reintentando con 'mensaje_usuario'. Error: {e}")
                llm_response_structured = llamar_gemini(
                    mensaje_usuario=mensaje_para_gemini, # Corrected keyword
                    usuario=usuario_info_for_gemini,
                    historial=historial_chat_para_gemini
                )
            else:
                logger_actual.error(f"[RESPONDER_MUNICIPIO_LLM_ERROR] Error de TypeError no esperado en la llamada a Gemini: {e}", exc_info=True)
                raise e # Relanzar otras TypeErrors
        except Exception as e:
            logger_actual.error(f"[RESPOND_PYME_LLM_ERROR] Error general en la llamada a Gemini: {e}", exc_info=True)
            llm_response_structured = {
                "respuesta_usuario": "Lo siento, estoy teniendo problemas para conectarme con el asistente inteligente. Un agente humano revisará tu consulta.",
                "accion_backend": "derivar_humano",
                "datos_estructura": {"target": "pyme", "error_llm": True, "detalle_error": str(e)},
                "pedir_info": None,
                "botones": []
            }

        respuesta_texto_gemini = gemini_response_structured.get("respuesta_usuario")
        accion_gemini = gemini_response_structured.get("accion_backend")

        if not respuesta_texto_gemini or accion_gemini == "error_llm" or \
           (len(respuesta_texto_gemini.split()) < 7 and ("no puedo" in respuesta_texto_gemini.lower() or "no sé" in respuesta_texto_gemini.lower() or "no tengo información" in respuesta_texto_gemini.lower())) or \
           ("no pude encontrar una respuesta directa" in respuesta_texto_gemini.lower()) or \
           ("hubo un inconveniente al procesar tu solicitud" in respuesta_texto_gemini.lower()):

            logger.info(f"[GeneralHandler] Gemini no encontró respuesta específica o devolvió fallback. Respuesta Gemini: '{respuesta_texto_gemini}', Accion: {accion_gemini}. Construyendo fallback contextual.")
            body_contextual_fallback = "No encontré información específica para tu consulta."
            options_contextual_fallback = []

            # Check for context from memoria
            categoria_reclamo_activa = memoria.get("categoria_reclamo")
            estado_conversacion_actual = memoria.get("estado_conversacion") # This is Enum or None

            if estado_conversacion_actual in RECLAMO_STATES and categoria_reclamo_activa:
                body_contextual_fallback = f"No encontré información adicional sobre '{pregunta_str}', pero si te referías al reclamo sobre '{categoria_reclamo_activa.replace('_',' ').title()}', podemos continuar con eso."
                # TODO: Add specific buttons to continue the claim, e.g., "Sí, continuar reclamo"
                # This requires knowing what the next step for that claim would be.
                # For now, offering generic options or asking for clarification.
                options_contextual_fallback.extend([
                    {"id": "continuar_reclamo_contextual", "texto": f"Continuar reclamo ({categoria_reclamo_activa.replace('_',' ').title()})"},
                    {"id": "iniciar_nuevo_reclamo_contextual", "texto": "Iniciar nuevo reclamo"},
                    {"id": "hablar_agente_contextual_reclamo", "texto": "Hablar con un agente"}
                ])
            elif estado_conversacion_actual == ConversationState.ESPERANDO_SELECCION_TRAMITE or memoria.get("ultimo_tramite_consultado"):
                tramite_ref = memoria.get("ultimo_tramite_consultado", "trámites")
                body_contextual_fallback = f"No encontré información específica sobre tu consulta relacionada con '{tramite_ref}'. Puedo mostrarte la lista de trámites nuevamente si querés."
                options_contextual_fallback.extend([
                    {"id": "ver_lista_tramites_contextual", "texto": "Ver lista de trámites"},
                    {"id": "hablar_agente_contextual_tramite", "texto": "Hablar con un agente"}
                ])
            else: # Generic fallback if no strong context
                body_contextual_fallback = "No encontré información específica para tu consulta. ¿Quizás querías hacer un reclamo, consultar sobre un trámite, o necesitas hablar con un agente?"
                options_contextual_fallback.extend([
                    {"id": "iniciar_reclamo_general_fallback_v3", "texto": "Hacer un reclamo"},
                    {"id": "consultar_tramite_general_fallback_v3", "texto": "Consultar un trámite"},
                    {"id": "hablar_con_agente_general_fallback_v3", "texto": "Hablar con un agente"}
                ])

            message_type_fallback = 'interactive_buttons' if options_contextual_fallback else 'text'
            if len(options_contextual_fallback) > 3: message_type_fallback = 'interactive_list'

            return {
                "message_body": body_contextual_fallback,
                "options_list": options_contextual_fallback,
                "message_type": message_type_fallback,
                "fuente": "general_handler_contextual_fallback_v3"
            }

        # If LLM gave a good answer, return it
        return {"message_body": respuesta_texto_gemini, "options_list": [], "message_type": "text", "fuente": "general_handler_respuesta_directa_v2"}

class EngancheAnonimoMunicipioHandler(BaseMunicipioHandler):
    def handle(self, payload: dict) -> dict | None:
        if self.context.get("user_id"): return None # Already logged in, not for this handler

        intencion = self.context.get("intencion")
        # If the intent is already to start a claim (e.g., set by image analysis),
        # let that flow proceed without suggesting registration at this exact moment.
        if intencion == "iniciar_reclamo":
            logger.info("[EngancheAnonimo] Intención 'iniciar_reclamo' detectada. Omitiendo sugerencia de registro para este turno.")
            return None

        # Allow greeting/polite/smalltalk to pass through even if anon, they might respond before this handler.
        # This check should ideally be after the "iniciar_reclamo" check, so those handlers don't
        # prevent the "iniciar_reclamo" intent from being respected by this handler.
        # However, the main handler loop calls these before EngancheAnonimo if they are earlier in the list.
        # The current placement implies that if Greeting/Polite/SmallTalk respond, Enganche won't run.
        # If they don't respond, AND intent is not "iniciar_reclamo", then Enganche proceeds.
        if GreetingHandler(self.context).handle(payload) or \
           PoliteHandler(self.context).handle(payload) or \
           SmallTalkHandler(self.context).handle(payload):
            # If these handlers respond, their response will be used by the main loop,
            # and EngancheAnonimoMunicipioHandler might not be called or its response ignored.
            # For this specific handler, we are interested in what happens if THEY DON'T respond.
            # The logic here is more about whether *this handler* should proceed.
            # The check above for "iniciar_reclamo" is the more direct control for this handler.
            pass # This pass means if those handlers *would* have responded, this handler won't actively do anything *different* yet.

        # Re-fetch intencion as it might have been cleared or changed by Greeting/Polite/Smalltalk if they modified context
        # though they typically don't clear intent if they are just providing a simple response.
        # For safety, could re-fetch: intencion = self.context.get("intencion")

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

        # If already in a claim flow or waiting for specific param for a tool (like recoleccion), let that proceed.
        if estado and estado in RECLAMO_STATES: return None
        if estado == ConversationState.ESPERANDO_PARAM_RECOLECCION:
            return RecoleccionHandler(self.context).handle(payload)

        # ToolHandler now expects intent and data from the main Gemini call (Orchestrator)
        # Example: self.context["intencion"] == "ejecutar_herramienta"
        #          self.context["datos_accion"] == {"nombre_herramienta": "...", "parametros": {...}} or {"faltan_parametros": [...]}

        if self.context.get("intencion") != "ejecutar_herramienta":
            logger.debug(f"[ToolHandler] Intención no es 'ejecutar_herramienta' (es '{self.context.get('intencion')}'). Cediendo.")
            return None

        decision = self.context.get("datos_accion") # This should be the structured data from Gemini
        if not isinstance(decision, dict):
            logger.warning(f"[ToolHandler] 'datos_accion' no es un diccionario válido para ejecutar herramienta. Datos: {decision}")
            return None # Cannot proceed

        nombre_herramienta = decision.get("nombre_herramienta")
        logger.info(f"[ToolHandler] Intentando ejecutar herramienta basada en datos de Gemini: '{nombre_herramienta}'")

        if not nombre_herramienta or nombre_herramienta not in TOOL_REGISTRY:
            logger.warning(f"[ToolHandler] Herramienta '{nombre_herramienta}' no reconocida o no en TOOL_REGISTRY.")
            return None # Or return an error message to the user

        if "faltan_parametros" in decision:
            # This logic implies Gemini can identify missing parameters for a tool.
            param_faltante = decision["faltan_parametros"][0] if decision["faltan_parametros"] else "información adicional"

            # Special handling for 'consultar_recoleccion_por_direccion' if 'direccion' is missing
            if param_faltante == "direccion" and nombre_herramienta == "consultar_recoleccion_por_direccion":
                memoria["estado_conversacion"] = ConversationState.ESPERANDO_PARAM_RECOLECCION.name
                # Store the tool info so RecoleccionHandler knows it's a tool-driven request
                memoria["herramienta_pendiente"] = {"nombre": nombre_herramienta, "contexto_original": decision}
                logger.info(f"[ToolHandler] Falta dirección para '{nombre_herramienta}'. Cambiando estado a ESPERANDO_PARAM_RECOLECCION.")
                return {"respuesta": (f"Para consultar el servicio de recolección, necesito la dirección completa.\nPor ejemplo: {EJEMPLO_DIRECCION}")}

            logger.info(f"[ToolHandler] Faltan parámetros para '{nombre_herramienta}': {decision['faltan_parametros']}.")
            return {"message_body": f"Necesito más información para usar la herramienta de {nombre_herramienta.replace('_', ' ')}. ¿Podrías proveer el dato: {param_faltante}?"}

        elif "parametros" in decision:
            parametros = decision.get("parametros", {})
            if not isinstance(parametros, dict):
                 logger.error(f"[ToolHandler] Parámetros para herramienta '{nombre_herramienta}' no son un diccionario: {parametros}")
                 return {"message_body": f"Hubo un problema con los parámetros para la herramienta {nombre_herramienta}."}

            funcion_a_ejecutar = TOOL_REGISTRY[nombre_herramienta]["funcion"]
            logger.info(f"[ToolHandler] Ejecutando herramienta '{nombre_herramienta}' con parámetros: {parametros}")
            try:
                resultado = funcion_a_ejecutar(**parametros)
                # El resultado de la herramienta puede ser un string o un dict (para JSON response)
                if isinstance(resultado, dict): # Si la herramienta ya devuelve la estructura de respuesta completa
                    return resultado
                return {"message_body": str(resultado)} # Si devuelve solo el texto
            except Exception as e_tool_exec:
                logger.error(f"[ToolHandler] Error ejecutando herramienta '{nombre_herramienta}': {e_tool_exec}", exc_info=True)
                return {"message_body": "Hubo un error al intentar usar la herramienta solicitada."}
        else:
            logger.warning(f"[ToolHandler] Decisión de Gemini para herramienta '{nombre_herramienta}' no contiene 'faltan_parametros' ni 'parametros'. Datos: {decision}")
            return {"message_body": f"No pude determinar cómo proceder con la herramienta {nombre_herramienta}."}

        # return None # Should have returned from one of the branches above.

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
    except ValueError as ve: logger.error(f"[LLM_FALLBACK] Problema con la respuesta del LLM: {ve}"); return fallback or "No pude encontrar una respuesta directa a tu consulta. ¿Podrías reformularla o preferís que te muestre opciones generales como hacer un reclamo o consultar trámites?"
    except Exception as e: logger.error(f"[LLM_FALLBACK] Error general en llamada a LLM: {e}", exc_info=True); return fallback or "Hubo un inconveniente al procesar tu solicitud en este momento. ¿Podrías reformularla o preferís que te muestre opciones generales como hacer un reclamo o consultar trámites?"

CATEGORIAS_RECLAMO = ["arbol caido", "arreglo de calle", "castracion de mascota", "falta de agua, rotura de caño", "fumigacion", "inspeccion de comercio", "limpieza", "luminaria", "riego de calle", "rotura de semaforo", "tramites de obras privadas", "incendio", "otro motivo"]
categorias_normalizadas = [normalizar_texto(c) for c in CATEGORIAS_RECLAMO]
RECLAMO_STATES = [ConversationState.ESPERANDO_CATEGORIA_RECLAMO, ConversationState.ESPERANDO_DIRECCION_RECLAMO, ConversationState.ESPERANDO_NOMBRE_VECINO, ConversationState.ESPERANDO_TELEFONO_VECINO, ConversationState.ESPERANDO_EMAIL_VECINO, ConversationState.ESPERANDO_DESCRIPCION_RECLAMO, ConversationState.ESPERANDO_ADJUNTOS_RECLAMO, ConversationState.ESPERANDO_CONFIRMACION_RECLAMO]

def serializar_enum(obj):
    if isinstance(obj, Enum): return obj.name
    elif isinstance(obj, dict): return {k: serializar_enum(v) for k, v in obj.items()}
    elif isinstance(obj, list): return [serializar_enum(v) for v in obj]
    else: return obj

BOTONES_COMANDOS_MUNICIPIO = {"Hacer un reclamo": "iniciar_reclamo", "Consultar estado de un trámite": "consultar_estado_ticket", "Consultar estado de ticket": "consultar_estado_ticket", "Consultar otro ticket": "consultar_estado_ticket", "Hablar con un agente": "hablar_con_agente", "Nuevo reclamo": "iniciar_reclamo", "Adjuntar foto": "adjuntar_foto", "Compartir ubicación": "compartir_ubicacion", "Foto": "adjuntar_foto", "Ubicación": "compartir_ubicacion", "No, continuar": "sin_adjuntos", "Completar reclamo": "sin_adjuntos", "Sí, confirmar reclamo": "confirmar_reclamo", "Si, confirmar reclamo": "confirmar_reclamo", "Confirmar reclamo": "confirmar_reclamo", "Finalizar": "confirmar_reclamo", "Finalizar reclamo": "confirmar_reclamo", "Confirmar": "confirmar_reclamo", "Confirmado": "confirmar_reclamo", "Si confirmo": "confirmar_reclamo", "Sí confirmo": "confirmar_reclamo", "Editar datos": "editar_reclamo", "Sí, solucionado": "confirmar_cierre_ticket", "No, aún no": "no_cerrar_ticket"}

import random # Asegurar que random está importado para el mock_ticket_nro
from services.gemini_bridge import llamar_gemini # Asegurar import

# Imports necesarios para la función accion_crear_reclamo_municipio
# (Algunos pueden estar ya importados globalmente en el archivo)
# from models import MunicipioTicket, db as global_db, User, ArchivoAdjunto, AnalisisArchivo # db ya está como global_db
# from services.ticket_service import servicio_tickets # Ya importado
# from .herramientas_municipio import parse_direccion_completa, direccion_es_valida # Ya importados globalmente
# from .common_utils import validar_telefono, formatear_telefono_e164, validar_email # Ya importados globalmente
# from .config_loader import CONFIG_MUNICIPIO # Ya importado globalmente
# from services.municipios import enviar_notificacion_whatsapp_con_plantilla # Esta función está en este mismo archivo.

# Definición completa de accion_crear_reclamo_municipio
def accion_crear_reclamo_municipio(datos_llm: dict, context: dict) -> dict:
    logger_func = current_app.logger if has_app_context() else logging.getLogger(__name__)
    logger_func.info(f"[ACCION_CREAR_RECLAMO_MUNICIPIO] Datos LLM: {datos_llm}")

    # --- 1. Extracción y Validación de Datos ---
    categoria = datos_llm.get("categoria", "Reclamo General")
    descripcion = datos_llm.get("descripcion")
    ubicacion_llm = datos_llm.get("ubicacion")
    coordenadas_llm = datos_llm.get("coordenadas") 
    nombre_vecino_llm = datos_llm.get("usuario") 
    telefono_llm = datos_llm.get("telefono")
    email_llm = datos_llm.get("email")
    foto_url_llm = datos_llm.get("foto_url_adjunta") 

    if not descripcion:
        return {
            "message_body": "No pude entender la descripción del reclamo. Por favor, intenta describirlo de nuevo.",
            "options_list": [], "fuente": "accion_crear_reclamo_error_sin_descripcion"
        }
    if not ubicacion_llm and not coordenadas_llm:
        return {
            "message_body": "No pude entender la ubicación del reclamo. Por favor, especifica dónde es el problema.",
            "options_list": [], "fuente": "accion_crear_reclamo_error_sin_ubicacion"
        }

    # --- 2. Recopilación de Información del Contexto ---
    viewer_user = context.get("viewer_user_obj")
    owner_user = context.get("user_obj") 
    
    user_id_db = getattr(viewer_user, "id", None)
    anon_id_db = context.get("anon_id") if not user_id_db else None
    municipio_config_actual = context.get("municipio_config_actual", CONFIG_MUNICIPIO) 
    municipio_db_id_para_ticket = getattr(owner_user, "municipio_id", None)
    # chat_session_uuid = context.get("chat_session_uuid") # Descomentar si se usa para idempotencia
    # chat_db_context_data = context.get("chat_db_context_data", {}) 

    nombre_vecino_final = nombre_vecino_llm or getattr(viewer_user, "nombre", None) or "Ciudadano Anónimo"
    
    telefono_final_validado_e164 = None
    temp_phone_str = str(telefono_llm or getattr(viewer_user, "telefono", ""))
    if temp_phone_str and validar_telefono(temp_phone_str): # common_utils.validar_telefono
        telefono_final_validado_e164 = formatear_telefono_e164(temp_phone_str) # common_utils.formatear_telefono_e164

    email_final_validado = None
    temp_email_str = str(email_llm or getattr(viewer_user, "email", ""))
    if temp_email_str and validar_email(temp_email_str): # common_utils.validar_email
        email_final_validado = temp_email_str.lower()

    direccion_final_txt = ubicacion_llm
    latitud_final = coordenadas_llm.get("lat") if isinstance(coordenadas_llm, dict) else None
    longitud_final = coordenadas_llm.get("lon") if isinstance(coordenadas_llm, dict) else None

    if ubicacion_llm and not (latitud_final and longitud_final): # Si tenemos texto de dirección pero no coords del LLM
        # herramientas_municipio.parse_direccion_completa
        parsed_address = parse_direccion_completa(ubicacion_llm, municipio_config_actual)
        if parsed_address and parsed_address.get("calle") and parsed_address.get("localidad"):
            direccion_final_txt = f"{parsed_address['calle']} {parsed_address.get('numero', '')}, {parsed_address['localidad']}".replace(" ,", ",").strip()
            logger_func.info(f"Dirección parseada de LLM: {direccion_final_txt}")
        elif not direccion_es_valida(ubicacion_llm): # herramientas_municipio.direccion_es_valida
             return {
                "message_body": f"La ubicación '{ubicacion_llm}' no parece válida. ¿Podrías verificarla?",
                "options_list": [], "fuente": "accion_crear_reclamo_direccion_invalida_llm"
            }
    
    ticket_data = {
        "asunto": f"Reclamo (LLM): {categoria}", "categoria": categoria, "detalles": descripcion,
        "direccion": direccion_final_txt, "nombre_vecino": nombre_vecino_final,
        "telefono_vecino": telefono_final_validado_e164, "email_vecino": email_final_validado,
        "estado": "nuevo", "user_id": user_id_db, "anon_id": anon_id_db,
        "municipio_id": municipio_db_id_para_ticket, "latitud": latitud_final, "longitud": longitud_final,
        "origen_reclamo": "LLM_CHATBOT"
    }
    if context.get("foto_url"): 
        ticket_data["foto_url_directa"] = context.get("foto_url")
    elif foto_url_llm: 
        ticket_data["foto_url_directa"] = foto_url_llm

    ticket_data_cleaned = {k: v for k, v in ticket_data.items() if v is not None}
    logger_func.info(f"[ACCION_CREAR_RECLAMO_MUNICIPIO] Datos para ticket: {ticket_data_cleaned}")

    try:
        ticket_creado = servicio_tickets.crear_nuevo_ticket(tipo_ticket="municipio", ticket_data=ticket_data_cleaned)
        if not ticket_creado:
            raise Exception("servicio_tickets.crear_nuevo_ticket retornó None")
        
        nro_ticket_str = f"M-{ticket_creado.nro_ticket}"
        logger_func.info(f"Ticket {nro_ticket_str} creado exitosamente vía LLM.")

        archivo_id_a_vincular = context.get("archivo_id_para_asociar")
        if archivo_id_a_vincular:
            from services.archivo_service import archivo_service 
            asociacion_exitosa = archivo_service.asociar_archivos_a_ticket(ticket_id=ticket_creado.id, tipo_ticket="municipio", ids_archivos=[archivo_id_a_vincular])
            if asociacion_exitosa: logger_func.info(f"Archivo ID {archivo_id_a_vincular} asociado a ticket {nro_ticket_str}.")
            else: logger_func.warning(f"No se pudo asociar archivo ID {archivo_id_a_vincular} a ticket {nro_ticket_str}.")
            
            # Consumir del contexto. CONTEXTO_MUNICIPIO es el sub-diccionario.
            if CONTEXTO_MUNICIPIO in context and isinstance(context[CONTEXTO_MUNICIPIO], dict) and "archivo_id_para_asociar" in context[CONTEXTO_MUNICIPIO]:
                 del context[CONTEXTO_MUNICIPIO]["archivo_id_para_asociar"] 
            elif "archivo_id_para_asociar" in context: 
                 context.pop("archivo_id_para_asociar", None)


        if telefono_final_validado_e164:
            try:
                enviar_notificacion_whatsapp_con_plantilla(telefono_final_validado_e164, nombre_vecino_final, str(ticket_creado.nro_ticket), categoria)
                logger_func.info(f"Notificación WhatsApp enviada para ticket {nro_ticket_str}")
            except Exception as e_notify_wp:
                logger_func.error(f"Error enviando notificación WhatsApp para {nro_ticket_str}: {e_notify_wp}")
        
        return {
            "message_body": f"¡Gracias {nombre_vecino_final}! Tu reclamo sobre '{categoria}' ha sido registrado con el número {nro_ticket_str}. Te mantendremos informado.",
            "options_list": [
                {"id": f"consultar_estado_ticket_{ticket_creado.nro_ticket}", "texto": "Consultar estado"},
                {"id": "iniciar_otro_reclamo_llm", "texto": "Hacer otro reclamo"}
            ],
            "fuente": "accion_crear_reclamo_llm_exito",
            "ticket_id": ticket_creado.id 
        }
    except Exception as e:
        logger_func.error(f"[ACCION_CREAR_RECLAMO_MUNICIPIO] Error al crear ticket: {e}", exc_info=True)
        # Asegurar que db es accesible (puede ser global_db si se renombró en imports)
        if hasattr(global_db, 'session') and hasattr(global_db.session, 'rollback'):
            global_db.session.rollback()
        elif hasattr(db, 'session') and hasattr(db.session, 'rollback'): # Fallback si db no fue renombrado
            db.session.rollback()
        return {
            "message_body": "Hubo un problema al intentar registrar tu reclamo. Por favor, intenta de nuevo más tarde o contacta al municipio directamente.",
            "options_list": [],
            "fuente": "accion_crear_reclamo_llm_error_creacion"
        }

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
    
    USAR_LLM_PARA_RECLAMOS = True # Feature flag para la nueva lógica LLM
    respuesta_manejada_por_llm = False # Flag para indicar si el LLM ya manejó la respuesta

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

    contexto_municipio_data_from_db = {}
    chat_db_context_live_data = {}

    if chat_db_context:
        if chat_db_context.context_data is None:
            chat_db_context.context_data = {}
        chat_db_context_live_data = chat_db_context.context_data # Reference to the live dict
        contexto_municipio_data_from_db = chat_db_context_live_data.get(
            CONTEXTO_MUNICIPIO, {}
        )
    else:
        logger_actual.warning("[RESPONDER_MUNICIPIO] chat_db_context is None. Municipio context will be empty for this request.")
        # contexto_municipio_data_from_db remains {}
        # chat_db_context_live_data remains {}

    logger_actual.info(
        f"[CONTEXTO_MUNICIPIO_LOAD_RAW] Contexto crudo para '{CONTEXTO_MUNICIPIO}' desde DB: {contexto_municipio_data_from_db}"
    )

    # Crear una copia para modificar de forma segura para esta request.
    contexto_municipio_actual = dict(contexto_municipio_data_from_db)

    # --- 2. CONSTRUCT THE 'context' DICTIONARY FOR HANDLERS (EARLY INITIALIZATION) ---
    # This dictionary is passed to handlers and used throughout this function.
    context = {
        CONTEXTO_MUNICIPIO: contexto_municipio_actual, # The specific state for municipio flow
        "user_obj": owner_user, # The User object of the bot instance (e.g., the Municipality)
        "viewer_user_obj": viewer_user, # The User object of the end-user (vecino/ciudadano)
        "cliente_id": getattr(viewer_user, "id", None),
        "anon_id": anon_id,
        "rubro_obj": rubro_obj,
        "channel": channel,
        "municipio_config_actual": CONFIG_MUNICIPIO, # Use the correct global constant here
        "chat_session_uuid": kwargs.get("chat_session_uuid"),
        "chat_db_context_data": chat_db_context_live_data, # Use the safely accessed live data dict
        # Fields to be populated by payload/kwargs or later logic:
        "intencion": kwargs.get("intencion"), # Initial intent from Orchestrator/kwargs
        "ubicacion_usuario": received_payload.get("ubicacion_usuario"),
        "es_foto": False, "foto_url": None, # Defaults, will be updated after inspecting payload
        "es_ubicacion": received_payload.get("es_ubicacion", False),
        "es_archivo": received_payload.get("es_archivo", False),
        "action": received_payload.get("action"), # From button clicks, etc.
        "datos_interpretados_archivo": kwargs.get("datos_interpretados_archivo"),
        "archivo_id_para_asociar": kwargs.get("archivo_id_para_asociar"),
    }
    if not (chat_db_context and hasattr(chat_db_context, 'context_data')):
        logger_actual.critical("chat_db_context.context_data no disponible al inicializar 'context'. Usando dict vacío. Esto es problemático.")


    logger_actual.info(
        f"[RESPONDER_MUNICIPIO_START_CONTEXT_INIT] Context inicializado. UserMunicipio: {context['user_obj'].id if context['user_obj'] else 'N/A'}, "
        f"ViewerCiudadano: {context['cliente_id'] or context['anon_id']}"
    )
    logger_actual.info(f"[CONTEXTO_MUNICIPIO_LOAD_RAW] Contexto DB para {CONTEXTO_MUNICIPIO}: {contexto_municipio_data_from_db}")


    # --- Handle post-login resumption (modifies context[CONTEXTO_MUNICIPIO] and context["intencion"]) ---
    # Ensure to check within context["chat_db_context_data"] which is the live dict from the ORM object
    if viewer_user and context["chat_db_context_data"].get("just_logged_in_flag"):
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

    # --- LLM Integration for Reclamos (and potentially other intents later) ---
    if USAR_LLM_PARA_RECLAMOS and not respuesta_manejada_por_llm: # Check flag here
        estado_conversacion_para_llm = contexto_municipio_actual.get("estado_conversacion") # Enum or None
        invocar_llm = False
        if estado_conversacion_para_llm == ConversationState.ESPERANDO_INFO_RECLAMO_LLM or \
           estado_conversacion_para_llm == ConversationState.CONVERSACION_GENERAL_LLM:
            invocar_llm = True
            logger_actual.info(f"[RESPONDER_MUNICIPIO_LLM_CHECK] Estado LLM activo: {estado_conversacion_para_llm}. Invocando LLM.")
        elif not estado_conversacion_para_llm or contexto_municipio_actual.get("saludo_detectado_en_largo_mensaje"):
            # Heurística para invocar LLM: texto sustantivo o imagen sin texto.
            # El context["es_foto"] se setea en el bloque "EARLY_IMG_PROC" que está más abajo ahora.
            # Para que el LLM use la foto en el primer turno, EARLY_IMG_PROC debe correr ANTES de este bloque LLM.
            # Por ahora, el LLM se basará en `pregunta_str` y `contexto_municipio_actual` que podría tener info de imagen de un turno ANTERIOR.
            # Si es una imagen NUEVA en ESTE turno, el EARLY_IMG_PROC más abajo la procesará para el *siguiente* turno del LLM,
            # o para los handlers tradicionales si el LLM no maneja este turno.
            # Esto es un punto a refinar: idealmente el análisis de imagen de ESTE turno debería estar disponible para el LLM en ESTE turno.
            
            # Para que el LLM pueda usar la imagen en el *mismo turno* que se envía,
            # la detección de `context["es_foto"]` y `context["foto_url"]` debe ocurrir *antes* de este bloque.
            # El bloque "EARLY_IMG_PROC" (que está más abajo) se encarga de esto.
            # Entonces, aquí `context.get("es_foto")` reflejará si una imagen fue detectada en este turno.
            if len(pregunta_str.strip().split()) > 1 or \
               (context.get("es_foto") and not pregunta_str.strip()):
                invocar_llm = True
                logger_actual.info(f"[RESPONDER_MUNICIPIO_LLM_CHECK] Estado inicial/saludo. Pregunta: '{pregunta_str[:50]}...', es_foto: {context.get('es_foto')}. Invocando LLM.")

        if invocar_llm:
            logger_actual.info(f"[RESPONDER_MUNICIPIO_LLM_INVOKE] Invocando LLM. Estado actual para LLM: {estado_conversacion_para_llm}")
            usuario_info_llm = {
                "nombre": getattr(viewer_user, "nombre", "Vecino/a") if viewer_user else "Vecino/a",
                "tipo_entidad": "municipio", 
                "ubicacion": getattr(viewer_user, "direccion", None) if viewer_user else None,
                "contacto": { "telefono": getattr(viewer_user, "telefono", None) if viewer_user else None, "email": getattr(viewer_user, "email", None) if viewer_user else None }
            }
            historial_para_llm = []
            if estado_conversacion_para_llm == ConversationState.ESPERANDO_INFO_RECLAMO_LLM: historial_para_llm = contexto_municipio_actual.get("historial_llm_reclamo", [])
            elif estado_conversacion_para_llm == ConversationState.CONVERSACION_GENERAL_LLM: historial_para_llm = contexto_municipio_actual.get("historial_conversacion_general_llm", [])

            try:
                mensaje_completo_para_llm = {"texto": pregunta_str}
                if context.get("es_foto") and context.get("foto_url"):
                    mensaje_completo_para_llm["imagen_url"] = context.get("foto_url")
                    if contexto_municipio_actual.get("analisis_imagen_reclamo_auto_raw"): 
                        analisis_previo = contexto_municipio_actual.get("analisis_imagen_reclamo_auto_raw")
                        if isinstance(analisis_previo, dict):
                             resumen_analisis = {k: analisis_previo.get(k) for k in ["categoria_sugerida", "descripcion_sugerida", "texto_ocr"] if analisis_previo.get(k)}
                             if resumen_analisis: mensaje_completo_para_llm["analisis_previo_imagen"] = resumen_analisis
                
                respuesta_llm_dict = llamar_gemini(mensaje_usuario=json.dumps(mensaje_completo_para_llm), usuario=usuario_info_llm, historial=historial_para_llm)
                logger_actual.info(f"[RESPONDER_MUNICIPIO_LLM_RESP] Respuesta LLM: {respuesta_llm_dict}")

                respuesta_usuario_llm = respuesta_llm_dict.get("respuesta_usuario")
                accion_backend_llm = respuesta_llm_dict.get("accion_backend")
                datos_estructura_llm = respuesta_llm_dict.get("datos_estructura")
                pedir_info_llm = respuesta_llm_dict.get("pedir_info")
                botones_llm = respuesta_llm_dict.get("botones", [])

                if respuesta_usuario_llm:
                    nuevo_turno_historial = {"pregunta_usuario": pregunta_str, "respuesta_ia": respuesta_usuario_llm}
                    hist_key = None
                    if accion_backend_llm == "crear_reclamo" and datos_estructura_llm and datos_estructura_llm.get("target") == "municipio":
                        hist_key = "historial_llm_reclamo"
                        if not pedir_info_llm: # Acción completa, se creará ticket
                            respuesta_accion = accion_crear_reclamo_municipio(datos_estructura_llm, context)
                            respuesta_final = respuesta_accion
                            for k in ["historial_llm_reclamo", "datos_parciales_llm_reclamo", "esperando_info_llm_reclamo"]: contexto_municipio_actual.pop(k, None)
                            contexto_municipio_actual["estado_conversacion"] = None
                            respuesta_manejada_por_llm = True
                        else: # Pide más info para reclamo
                            contexto_municipio_actual["datos_parciales_llm_reclamo"] = datos_estructura_llm
                            contexto_municipio_actual["estado_conversacion"] = ConversationState.ESPERANDO_INFO_RECLAMO_LLM
                            contexto_municipio_actual["esperando_info_llm_reclamo"] = pedir_info_llm
                            respuesta_final = {"message_body": respuesta_usuario_llm, "options_list": botones_llm, "message_type": "interactive_buttons" if botones_llm else "text", "fuente": "llm_pide_info_reclamo"}
                            respuesta_manejada_por_llm = True
                    elif accion_backend_llm == "derivar_humano":
                        context["intencion"] = "hablar_con_agente"
                        contexto_municipio_actual["mensaje_previo_llm_para_escalamiento"] = respuesta_usuario_llm
                        respuesta_manejada_por_llm = False # Deja a HumanEscalationHandler construir la respuesta
                        logger_actual.info(f"[RESPONDER_MUNICIPIO_LLM] LLM derivó a humano.")
                    elif respuesta_usuario_llm: # Respuesta general
                        hist_key = "historial_conversacion_general_llm"
                        contexto_municipio_actual["estado_conversacion"] = ConversationState.CONVERSACION_GENERAL_LLM
                        if pedir_info_llm: contexto_municipio_actual["esperando_info_general_llm"] = pedir_info_llm
                        else: contexto_municipio_actual.pop("esperando_info_general_llm", None)
                        respuesta_final = {"message_body": respuesta_usuario_llm, "options_list": botones_llm, "message_type": "interactive_buttons" if botones_llm else "text", "fuente": "llm_respuesta_general"}
                        respuesta_manejada_por_llm = True
                    
                    if hist_key and respuesta_manejada_por_llm : # Solo agregar a historial si el LLM efectivamente manejó la respuesta y es un flujo continuo
                        contexto_municipio_actual.setdefault(hist_key, []).append(nuevo_turno_historial)
                
                if not respuesta_usuario_llm and not accion_backend_llm : # LLM no dio nada útil
                    logger_actual.warning("[RESPONDER_MUNICIPIO_LLM] LLM no devolvió respuesta_usuario ni acción_backend.")
                    respuesta_manejada_por_llm = False # Fallback

            except Exception as e_llm:
                logger_actual.error(f"[RESPONDER_MUNICIPIO_LLM_ERROR] Error: {e_llm}", exc_info=True)
                respuesta_manejada_por_llm = False
                for k in ["historial_llm_reclamo", "datos_parciales_llm_reclamo", "esperando_info_llm_reclamo", "historial_conversacion_general_llm", "estado_conversacion"]:
                    if k == "estado_conversacion" and contexto_municipio_actual.get(k) in [ConversationState.ESPERANDO_INFO_RECLAMO_LLM, ConversationState.CONVERSACION_GENERAL_LLM]:
                        contexto_municipio_actual[k] = None
                    elif k != "estado_conversacion":
                        contexto_municipio_actual.pop(k, None)
    
    # --- Image Analysis & Web Analysis Check (POST-LLM or if LLM not used) ---
    # This block runs if LLM didn't handle the response, or to supplement LLM context
    # by performing image analysis if new media is present and not yet analyzed by LLM.
    # It sets context flags like 'es_foto', 'foto_url', 'archivo_id_para_asociar'
    # and can pre-fill 'categoria_reclamo', 'descripcion_reclamo' from image analysis.
    # It also loads results from asynchronous web image analysis if 'web_analisis_listo' is present.

    # Note: The LLM block above might have already used context["es_foto"] and context["foto_url"]
    # if the "EARLY_IMG_PROC" block (which is this one, now strategically placed) ran before it
    # in a conceptual sense for setting up the 'context' dict.
    # The key is that this block *also* runs if LLM is disabled or doesn't handle the response,
    # ensuring image data is always processed for traditional handlers.

    uploaded_file_info_for_analysis = received_payload.get("uploaded_file_info") or \
                                      received_payload.get("uploaded_file_info_whatsapp")

    # 1. Basic media info setting (es_foto, foto_url, archivo_id_para_asociar)
    # This part ensures these general context keys are set if there's media,
    # regardless of whether LLM used them or if analysis will be performed now.
    if uploaded_file_info_for_analysis and isinstance(uploaded_file_info_for_analysis, dict):
        mime_type = uploaded_file_info_for_analysis.get("mime_type", "")
        if mime_type.startswith("image/"): # Assuming we only care about images for this advanced processing
            if not context.get("es_foto"): # Only set if not already set (e.g. by an earlier phase if structure changes)
                context["es_foto"] = True
                logger_actual.info(f"[MEDIA_CONTEXT_SETUP] context['es_foto'] set to True.")
            if not context.get("foto_url") and uploaded_file_info_for_analysis.get("url"):
                context["foto_url"] = uploaded_file_info_for_analysis.get("url")
                logger_actual.info(f"[MEDIA_CONTEXT_SETUP] context['foto_url'] set to {context['foto_url']}.")
            if not context.get("archivo_id_para_asociar") and \
               uploaded_file_info_for_analysis.get("id") and \
               uploaded_file_info_for_analysis.get("source") != "whatsapp":
                context["archivo_id_para_asociar"] = uploaded_file_info_for_analysis.get("id")
                logger_actual.info(f"[MEDIA_CONTEXT_SETUP] context['archivo_id_para_asociar'] set to {context['archivo_id_para_asociar']}.")

    # 2. Load completed web analysis results if 'web_analisis_listo' is in context
    # This is for images uploaded via web and processed asynchronously.
    web_analisis_info = contexto_municipio_actual.pop("web_analisis_listo", None) # Consume the flag
    if web_analisis_info and isinstance(web_analisis_info, dict) and web_analisis_info.get("archivo_id"):
        archivo_id_analizado = web_analisis_info["archivo_id"]
        logger_actual.info(f"[MEDIA_ANALYSIS] 'web_analisis_listo' para archivo ID: {archivo_id_analizado}. Cargando análisis.")
        try:
            from models import ArchivoAdjunto, AnalisisArchivo # Ensure models are imported
            archivo_obj = db.session.get(ArchivoAdjunto, archivo_id_analizado)
            if archivo_obj and archivo_obj.analisis and archivo_obj.analisis.estado_analisis == "completado":
                analisis_obj = archivo_obj.analisis
                datos_estructurados_analisis = analisis_obj.datos_estructurados if isinstance(analisis_obj.datos_estructurados, dict) else {}
                
                categoria_sugerida_web = datos_estructurados_analisis.get("categoria_sugerida_final", datos_estructurados_analisis.get("vision_inferred_category"))
                descripcion_sugerida_web = datos_estructurados_analisis.get("descripcion_sugerida_final", "Descripción basada en imagen adjunta.")

                # Store structured analysis in 'analisis_imagen_reclamo_auto_raw' for consistency
                contexto_municipio_actual["analisis_imagen_reclamo_auto_raw"] = {
                    "es_reclamo": True, 
                    "categoria_sugerida": categoria_sugerida_web,
                    "descripcion_sugerida": descripcion_sugerida_web,
                    "texto_ocr": analisis_obj.texto_extraido or "",
                    "mime_type": archivo_obj.mime,
                    "raw_analysis": { "vision_api_raw": datos_estructurados_analisis.get("vision_api_raw", {}), "extracted_ocr_text": analisis_obj.texto_extraido or "", "llm_complaint_extraction_from_image": datos_estructurados_analisis.get("llm_complaint_extraction_from_image", {}) },
                    "analisis_id": analisis_obj.id, 
                    "source": "web_async_analysis"
                }
                logger_actual.info(f"Análisis de archivo web ID {archivo_id_analizado} (AnalisisID: {analisis_obj.id}) cargado en 'analisis_imagen_reclamo_auto_raw'.")

                if categoria_sugerida_web and (not contexto_municipio_actual.get("categoria_reclamo") or contexto_municipio_actual.get("categoria_reclamo") == "otro motivo"):
                    contexto_municipio_actual["categoria_reclamo"] = categoria_sugerida_web
                if descripcion_sugerida_web and (not contexto_municipio_actual.get("descripcion_reclamo") or len(contexto_municipio_actual.get("descripcion_reclamo", "")) < 20):
                    contexto_municipio_actual["descripcion_reclamo"] = descripcion_sugerida_web
                
                if not respuesta_manejada_por_llm and not pregunta_str.strip() and not context.get("intencion"):
                    context["intencion"] = "iniciar_reclamo"
                    logger_actual.info(f"Intención fijada a 'iniciar_reclamo' por análisis web completado (sin texto/intención previa y LLM no manejó).")
            else: 
                logger_actual.warning(f"Archivo ID {archivo_id_analizado} o su análisis completado no encontrado. 'web_analisis_listo' ignorado.")
        except Exception as e_load_web_analisis:
            logger_actual.error(f"Error cargando datos de análisis web para archivo ID {web_analisis_info.get('archivo_id')}: {e_load_web_analisis}", exc_info=True)

    # 3. Direct analysis for new WhatsApp images if not already analyzed (e.g., by LLM or previous turn)
    if not contexto_municipio_actual.get("analisis_imagen_reclamo_auto_raw") and \
       uploaded_file_info_for_analysis and \
       uploaded_file_info_for_analysis.get("source") == "whatsapp" and \
       context.get("es_foto"): # es_foto should be set by now if it's an image
        logger_actual.info("[MEDIA_ANALYSIS] Procesando imagen WhatsApp directamente (no 'analisis_imagen_reclamo_auto_raw' previo).")
        try:
            from services.interpretacion_imagen_service import interpretar_imagen_para_chat
            analisis_resultado_whatsapp = interpretar_imagen_para_chat(
                archivo_adjunto=uploaded_file_info_for_analysis, 
                tipo_interpretacion="reclamo_auto_descripcion_categoria"
            )
            logger_actual.info(f"Resultado análisis directo WhatsApp: {analisis_resultado_whatsapp}")
            if analisis_resultado_whatsapp and not analisis_resultado_whatsapp.get("error"):
                contexto_municipio_actual["analisis_imagen_reclamo_auto_raw"] = analisis_resultado_whatsapp
                cat_sug_wp = analisis_resultado_whatsapp.get("categoria_sugerida")
                desc_sug_wp = analisis_resultado_whatsapp.get("descripcion_sugerida")
                if cat_sug_wp and (not contexto_municipio_actual.get("categoria_reclamo") or contexto_municipio_actual.get("categoria_reclamo") == "otro motivo"):
                    contexto_municipio_actual["categoria_reclamo"] = cat_sug_wp
                if desc_sug_wp and (not contexto_municipio_actual.get("descripcion_reclamo") or len(contexto_municipio_actual.get("descripcion_reclamo", "")) < 20):
                    contexto_municipio_actual["descripcion_reclamo"] = desc_sug_wp
                
                if not respuesta_manejada_por_llm and not pregunta_str.strip() and not context.get("intencion") and analisis_resultado_whatsapp.get('es_reclamo'):
                    context["intencion"] = "iniciar_reclamo"
                    logger_actual.info(f"Intención fijada a 'iniciar_reclamo' por análisis WhatsApp (sin texto/intención previa y LLM no manejó).")
        except Exception as e_img_direct_wp:
            logger_actual.error(f"Error en análisis directo de imagen WhatsApp: {e_img_direct_wp}", exc_info=True)
    
    # --- End of Image Analysis & Web Analysis Check ---

    # --- Image Analysis & Web Analysis Check (POST-LLM or if LLM not used) ---
    # This block runs if LLM didn't handle the response, or to supplement LLM context
    # by performing image analysis if new media is present and not yet analyzed by LLM.
    # It sets context flags like 'es_foto', 'foto_url', 'archivo_id_para_asociar'
    # and can pre-fill 'categoria_reclamo', 'descripcion_reclamo' from image analysis.
    # It also loads results from asynchronous web image analysis if 'web_analisis_listo' is present.

    # Note: The LLM block above might have already used context["es_foto"] and context["foto_url"]
    # if the "EARLY_IMG_PROC" block (which is this one, now strategically placed) ran before it
    # in a conceptual sense for setting up the 'context' dict.
    # The key is that this block *also* runs if LLM is disabled or doesn't handle the response,
    # ensuring image data is always processed for traditional handlers.

    uploaded_file_info_for_analysis = received_payload.get("uploaded_file_info") or \
                                      received_payload.get("uploaded_file_info_whatsapp")

    # 1. Basic media info setting (es_foto, foto_url, archivo_id_para_asociar)
    # This part ensures these general context keys are set if there's media,
    # regardless of whether LLM used them or if analysis will be performed now.
    if uploaded_file_info_for_analysis and isinstance(uploaded_file_info_for_analysis, dict):
        mime_type = uploaded_file_info_for_analysis.get("mime_type", "")
        if mime_type.startswith("image/"): # Assuming we only care about images for this advanced processing
            if not context.get("es_foto"): # Only set if not already set (e.g. by an earlier phase if structure changes)
                context["es_foto"] = True
                logger_actual.info(f"[MEDIA_CONTEXT_SETUP] context['es_foto'] set to True.")
            if not context.get("foto_url") and uploaded_file_info_for_analysis.get("url"):
                context["foto_url"] = uploaded_file_info_for_analysis.get("url")
                logger_actual.info(f"[MEDIA_CONTEXT_SETUP] context['foto_url'] set to {context['foto_url']}.")
            if not context.get("archivo_id_para_asociar") and \
               uploaded_file_info_for_analysis.get("id") and \
               uploaded_file_info_for_analysis.get("source") != "whatsapp":
                context["archivo_id_para_asociar"] = uploaded_file_info_for_analysis.get("id")
                logger_actual.info(f"[MEDIA_CONTEXT_SETUP] context['archivo_id_para_asociar'] set to {context['archivo_id_para_asociar']}.")

    # 2. Load completed web analysis results if 'web_analisis_listo' is in context
    # This is for images uploaded via web and processed asynchronously.
    web_analisis_info = contexto_municipio_actual.pop("web_analisis_listo", None) # Consume the flag
    if web_analisis_info and isinstance(web_analisis_info, dict) and web_analisis_info.get("archivo_id"):
        archivo_id_analizado = web_analisis_info["archivo_id"]
        logger_actual.info(f"[MEDIA_ANALYSIS] 'web_analisis_listo' para archivo ID: {archivo_id_analizado}. Cargando análisis.")
        try:
            from models import ArchivoAdjunto, AnalisisArchivo # Ensure models are imported
            archivo_obj = db.session.get(ArchivoAdjunto, archivo_id_analizado)
            if archivo_obj and archivo_obj.analisis and archivo_obj.analisis.estado_analisis == "completado":
                analisis_obj = archivo_obj.analisis
                datos_estructurados_analisis = analisis_obj.datos_estructurados if isinstance(analisis_obj.datos_estructurados, dict) else {}
                
                categoria_sugerida_web = datos_estructurados_analisis.get("categoria_sugerida_final", datos_estructurados_analisis.get("vision_inferred_category"))
                descripcion_sugerida_web = datos_estructurados_analisis.get("descripcion_sugerida_final", "Descripción basada en imagen adjunta.")

                # Store structured analysis in 'analisis_imagen_reclamo_auto_raw' for consistency
                contexto_municipio_actual["analisis_imagen_reclamo_auto_raw"] = {
                    "es_reclamo": True, 
                    "categoria_sugerida": categoria_sugerida_web,
                    "descripcion_sugerida": descripcion_sugerida_web,
                    "texto_ocr": analisis_obj.texto_extraido or "",
                    "mime_type": archivo_obj.mime,
                    "raw_analysis": { "vision_api_raw": datos_estructurados_analisis.get("vision_api_raw", {}), "extracted_ocr_text": analisis_obj.texto_extraido or "", "llm_complaint_extraction_from_image": datos_estructurados_analisis.get("llm_complaint_extraction_from_image", {}) },
                    "analisis_id": analisis_obj.id, 
                    "source": "web_async_analysis"
                }
                logger_actual.info(f"Análisis de archivo web ID {archivo_id_analizado} (AnalisisID: {analisis_obj.id}) cargado en 'analisis_imagen_reclamo_auto_raw'.")

                if categoria_sugerida_web and (not contexto_municipio_actual.get("categoria_reclamo") or contexto_municipio_actual.get("categoria_reclamo") == "otro motivo"):
                    contexto_municipio_actual["categoria_reclamo"] = categoria_sugerida_web
                if descripcion_sugerida_web and (not contexto_municipio_actual.get("descripcion_reclamo") or len(contexto_municipio_actual.get("descripcion_reclamo", "")) < 20):
                    contexto_municipio_actual["descripcion_reclamo"] = descripcion_sugerida_web
                
                if not respuesta_manejada_por_llm and not pregunta_str.strip() and not context.get("intencion"):
                    context["intencion"] = "iniciar_reclamo"
                    logger_actual.info(f"Intención fijada a 'iniciar_reclamo' por análisis web completado (sin texto/intención previa y LLM no manejó).")
            else: 
                logger_actual.warning(f"Archivo ID {archivo_id_analizado} o su análisis completado no encontrado. 'web_analisis_listo' ignorado.")
        except Exception as e_load_web_analisis:
            logger_actual.error(f"Error cargando datos de análisis web para archivo ID {web_analisis_info.get('archivo_id')}: {e_load_web_analisis}", exc_info=True)

    # 3. Direct analysis for new WhatsApp images if not already analyzed (e.g., by LLM or previous turn)
    if not contexto_municipio_actual.get("analisis_imagen_reclamo_auto_raw") and \
       uploaded_file_info_for_analysis and \
       uploaded_file_info_for_analysis.get("source") == "whatsapp" and \
       context.get("es_foto"): # es_foto should be set by now if it's an image
        logger_actual.info("[MEDIA_ANALYSIS] Procesando imagen WhatsApp directamente (no 'analisis_imagen_reclamo_auto_raw' previo).")
        try:
            from services.interpretacion_imagen_service import interpretar_imagen_para_chat
            analisis_resultado_whatsapp = interpretar_imagen_para_chat(
                archivo_adjunto=uploaded_file_info_for_analysis, 
                tipo_interpretacion="reclamo_auto_descripcion_categoria"
            )
            logger_actual.info(f"Resultado análisis directo WhatsApp: {analisis_resultado_whatsapp}")
            if analisis_resultado_whatsapp and not analisis_resultado_whatsapp.get("error"):
                contexto_municipio_actual["analisis_imagen_reclamo_auto_raw"] = analisis_resultado_whatsapp
                cat_sug_wp = analisis_resultado_whatsapp.get("categoria_sugerida")
                desc_sug_wp = analisis_resultado_whatsapp.get("descripcion_sugerida")
                if cat_sug_wp and (not contexto_municipio_actual.get("categoria_reclamo") or contexto_municipio_actual.get("categoria_reclamo") == "otro motivo"):
                    contexto_municipio_actual["categoria_reclamo"] = cat_sug_wp
                if desc_sug_wp and (not contexto_municipio_actual.get("descripcion_reclamo") or len(contexto_municipio_actual.get("descripcion_reclamo", "")) < 20):
                    contexto_municipio_actual["descripcion_reclamo"] = desc_sug_wp
                
                if not respuesta_manejada_por_llm and not pregunta_str.strip() and not context.get("intencion") and analisis_resultado_whatsapp.get('es_reclamo'):
                    context["intencion"] = "iniciar_reclamo"
                    logger_actual.info(f"Intención fijada a 'iniciar_reclamo' por análisis WhatsApp (sin texto/intención previa y LLM no manejó).")
        except Exception as e_img_direct_wp:
            logger_actual.error(f"Error en análisis directo de imagen WhatsApp: {e_img_direct_wp}", exc_info=True)
    
    # --- End of Image Analysis & Web Analysis Check ---

    estado_guardado_raw = contexto_municipio_actual.get("estado_conversacion")
    logger_actual.info(
        f"[CONTEXTO_MUNICIPIO_LOAD_STATE_RAW] 'estado_conversacion' crudo extraído del contexto_municipio_actual: '{estado_guardado_raw}' (Tipo: {type(estado_guardado_raw)})"
    )

    # --- Corrected State Loading Logic ---
    if estado_guardado_raw is None:
        logger_actual.info(
            "[CONTEXTO_MUNICIPIO_LOAD_STATE] 'estado_conversacion' es None. Se mantiene como None."
        )
        contexto_municipio_actual["estado_conversacion"] = None
    elif isinstance(estado_guardado_raw, ConversationState): # If it's an Enum instance
        logger_actual.info(
            f"[CONTEXTO_MUNICIPIO_LOAD_STATE] 'estado_conversacion' es Enum ({estado_guardado_raw}). Convirtiendo a string: '{estado_guardado_raw.name}'."
        )
        contexto_municipio_actual["estado_conversacion"] = estado_guardado_raw.name
    elif isinstance(estado_guardado_raw, str):
        # If it's already a string, try to validate if it's a valid Enum name.
        # This helps catch cases where a non-Enum string might have been saved.
        try:
            ConversationState[estado_guardado_raw] # Validate if it's a known state name
            logger_actual.info(
                f"[CONTEXTO_MUNICIPIO_LOAD_STATE] 'estado_conversacion' es string válido de Enum: '{estado_guardado_raw}'."
            )
            # contexto_municipio_actual["estado_conversacion"] is already estado_guardado_raw (string)
        except KeyError:
            logger_actual.error(
                f"[CONTEXTO_MUNICIPIO_LOAD_STATE] 'estado_conversacion' es string ('{estado_guardado_raw}') pero no es un nombre válido de ConversationState. Se establece a None."
            )
            contexto_municipio_actual["estado_conversacion"] = None
    else: # Other unexpected types
        logger_actual.error(
            f"[CONTEXTO_MUNICIPIO_LOAD_STATE] Tipo inesperado para 'estado_conversacion' ({type(estado_guardado_raw)}): '{estado_guardado_raw}'. Se establece a None."
        )
        contexto_municipio_actual["estado_conversacion"] = None

    final_loaded_state_str = contexto_municipio_actual.get("estado_conversacion") # Should be string (valid Enum name) or None now
    logger_actual.info(
        f"[CONTEXTO_MUNICIPIO_LOAD_FINAL] 'estado_conversacion' para esta petición (string o None): '{final_loaded_state_str}'"
    )

    # --- Construcción del Contexto Global para Orchestrator y Handlers ---
    # Este es el 'global_context' que recibirá el ChatOrchestrator
    # y que luego se pasará a cada ActionHandler.

    # Cargar config específica del municipio (si existe)
    final_municipio_config = CONFIG_MUNICIPIO # Default global
    if owner_user and hasattr(owner_user, 'municipio_id') and owner_user.municipio_id:
        owner_user_municipio_id_str = str(owner_user.municipio_id)
        loaded_specific_config = cargar_configuracion_municipio(owner_user_municipio_id_str, "config.json")
        if loaded_specific_config:
            final_municipio_config = loaded_specific_config

    # (contexto_municipio_actual ya está definido y es el que se usa para el sub-contexto)

    # Construir 'usuario_info_for_gemini' para la llamada a Gemini
    usuario_info_for_gemini = {
        "nombre": getattr(viewer_user, "name", None) or getattr(viewer_user, "nombre", None) or contexto_municipio_actual.get("nombre_vecino") or "Vecino/a",
        "tipo_entidad": "municipio",
        "municipio_config": { # Pasar datos relevantes de la config del municipio al LLM
            "nombre_municipio": final_municipio_config.get("nombre_display", MUNICIPIO_ID.title()),
            "servicios_principales": final_municipio_config.get("servicios_principales_chatbot", ["reclamos", "trámites", "consultas generales"])
        }
    }
    # Añadir ubicación si se conoce (del perfil del usuario o del contexto del reclamo)
    loc_usuario_texto = getattr(viewer_user, "direccion", None) or contexto_municipio_actual.get("direccion_reclamo")
    if loc_usuario_texto: usuario_info_for_gemini["ubicacion_conocida"] = loc_usuario_texto

    # --- LLAMADA PRINCIPAL A GEMINI ---
    historial_chat_para_gemini = chat_db_context_live_data.get("mensajes_previos_gemini_formato", [])

    # La pregunta_str ya tiene el texto del usuario.
    # Si hay una imagen, el prompt de Gemini debe ser instruido para considerarla.
    # JULES_SYSTEM_PROMPT ya tiene instrucciones generales.
    # Aquí podríamos añadir un prefijo al mensaje si hay una imagen:
    mensaje_para_gemini = pregunta_str
    if context.get("es_foto") and context.get("foto_url"):
        # El LLM no puede ver la URL directamente. El JULES_SYSTEM_PROMPT debe guiarlo
        # para que, si el usuario menciona una foto o el sistema indica que hay una,
        # actúe en consecuencia (ej. pidiendo descripción o asumiendo que es para un reclamo).
        # Aquí, informamos al LLM que hay una foto adjunta.
        mensaje_para_gemini = f"[Sistema: El usuario ha adjuntado una imagen. URL para referencia interna: {context.get('foto_url')}] {pregunta_str}".strip()
        # El análisis de imagen (Vision API) se haría en un ActionHandler si el LLM decide que es necesario.
        # O, si la política es analizar siempre, se haría antes y los resultados se pasarían a Gemini.
        # Por ahora, el flujo es: Gemini decide -> Orchestrator -> ActionHandler (que podría usar Vision).

    try:
        llm_response_structured = llamar_gemini(
            mensaje_usuario=mensaje_para_gemini,
            usuario=usuario_info_for_gemini,
            historial=historial_chat_para_gemini
        )
    except Exception as e:
        logger_actual.error(f"[RESPONDER_MUNICIPIO_LLM_ERROR] Error general en la llamada a Gemini: {e}", exc_info=True)
        # Fallback a una respuesta de error segura si la llamada a LLM falla
        llm_response_structured = {
            "respuesta_usuario": "Lo siento, estoy teniendo problemas para conectarme con el asistente inteligente. Un agente humano revisará tu consulta.",
            "accion_backend": "derivar_humano",
            "datos_estructura": {"target": "municipio", "error_llm": True, "detalle_error": str(e)},
            "pedir_info": None,
            "botones": []
        }

    # Actualizar el historial de chat_db_context con este turno (pregunta y respuesta_usuario del LLM)
    # Esto es para que la próxima llamada a Gemini tenga este contexto.
    # (Asegurarse que el formato sea el esperado por llamar_gemini)
    if "mensajes_previos_gemini_formato" not in chat_db_context_live_data:
        chat_db_context_live_data["mensajes_previos_gemini_formato"] = []
    chat_db_context_live_data["mensajes_previos_gemini_formato"].append({"role": "user", "parts": [{"text": mensaje_para_gemini}]})
    # La respuesta del modelo se añadirá después de que el ActionHandler la confirme/modifique.

    # --- Preparar CONTEXTO GLOBAL para ChatOrchestrator y Action Handlers ---
    # Este es el 'global_context' que se pasa.
    # `contexto_municipio_actual` es el sub-diccionario específico del flujo de municipio.

    global_context_for_orchestrator = {
        CONTEXTO_MUNICIPIO: contexto_municipio_actual, # El estado actual del flujo municipal
        "user_obj": owner_user, # El User object del Bot (Municipio)
        "viewer_user_obj": viewer_user, # El User object del ciudadano (puede ser None)
        "cliente_id": getattr(viewer_user, "id", None),
        "anon_id": anon_id,
        "rubro_obj": rubro_obj, # Objeto Rubro del Bot
        "channel": channel,
        "municipio_config_actual": final_municipio_config, # Config específica del municipio
        "chat_session_uuid": kwargs.get("chat_session_uuid"),
        "chat_db_context_data": chat_db_context_live_data, # El dict vivo de context_data

        # Datos del turno actual que pueden ser útiles para los handlers:
        "pregunta_actual_usuario": pregunta_str, # Texto original del usuario para este turno
        "ubicacion_actual_payload": received_payload.get("ubicacion_usuario"), # Si el usuario compartió GPS en este turno
        "es_foto_actual_payload": context.get("es_foto", False), # Si este turno incluyó una foto
        "foto_url_actual_payload": context.get("foto_url"),
        "archivo_id_para_asociar": context.get("archivo_id_para_asociar"), # Si es un archivo web con ID
        "action_button_payload": received_payload.get("action"), # Si fue un click de botón
        "target_entity_type": "municipio" # Para que DerivarHumanoAction sepa a qué pool notificar
    }

    # --- EJECUTAR ACCIÓN VIA ChatOrchestrator ---
    from .chat_orchestrator import ChatOrchestrator # Importar aquí para evitar problemas de importación circular a nivel de módulo
    orchestrator = ChatOrchestrator(global_context=global_context_for_orchestrator)
    action_handler_result = orchestrator.execute_action(llm_response_structured)

    # --- PROCESAR RESULTADO DEL ACTION HANDLER ---
    respuesta_final_texto = action_handler_result.get("message_to_user")
    if not respuesta_final_texto: # Si el handler no dio un mensaje, usar el del LLM
        respuesta_final_texto = llm_response_structured.get("respuesta_usuario", "No entendí, ¿podrías repetirlo?")

    # Tomar botones del LLM original, a menos que el handler los haya modificado (no implementado aún)
    opciones_finales = llm_response_structured.get("botones", [])

    # Determinar 'pedir_info' final: priorizar el del action_handler si existe, sino el del LLM
    pedir_info_final = action_handler_result.get("pedir_info") or llm_response_structured.get("pedir_info")

    # --- Actualizar estado de conversación en contexto_municipio_actual ---
    # Esto es crucial. Si `pedir_info_final` está seteado, el estado debe reflejar qué se está esperando.
    # Esta lógica necesita mapear `pedir_info_final` a un `ConversationState`.
    # Ejemplo: if pedir_info_final == "ubicacion": contexto_municipio_actual["estado_conversacion"] = ConversationState.ESPERANDO_DIRECCION_RECLAMO.name
    # Por ahora, si hay pedir_info, asumimos que el estado se maneja dentro del flujo conversacional que pediría ese dato.
    # Si la acción fue exitosa y no hay pedir_info, generalmente se limpia el estado.

    if action_handler_result.get("success") and not pedir_info_final:
        # Si la acción fue exitosa y no se pide más info, limpiar estado específico del flujo.
        # Esto es una simplificación. Algunos flujos podrían querer mantenerse en un estado de "resumen" o similar.
        # El `action_handler_result` podría devolver un `nuevo_estado_conversacion` si quisiera ser explícito.
        if contexto_municipio_actual.get("estado_conversacion") not in [None, ConversationState.IDLE.name if hasattr(ConversationState, 'IDLE') else None]: # Evitar limpiar si ya estaba idle/None
            logger.info(f"Acción '{llm_response_structured.get('accion_backend')}' exitosa y sin pedir_info. Limpiando estado de conversación municipal.")
            # Guardar interacciones anon si existen antes de limpiar
            interacciones_anon_actual = contexto_municipio_actual.get("interacciones_anon_sesion")
            contexto_municipio_actual.clear() # Limpia el sub-diccionario
            if interacciones_anon_actual is not None:
                contexto_municipio_actual["interacciones_anon_sesion"] = interacciones_anon_actual
            # No se setea estado_conversacion a None aquí, clear() lo elimina. Se re-evaluará al final.
    elif pedir_info_final:
        # Mapear pedir_info_final a un ConversationState y guardarlo
        # Esta es la parte que necesita una lógica de mapeo robusta.
        # Ejemplo simplificado:
        estado_objetivo_str = None
        if pedir_info_final == "ubicacion": estado_objetivo_str = ConversationState.ESPERANDO_DIRECCION_RECLAMO.name
        elif pedir_info_final == "categoria": estado_objetivo_str = ConversationState.ESPERANDO_CATEGORIA_RECLAMO.name
        elif pedir_info_final == "nombre_completo": estado_objetivo_str = ConversationState.ESPERANDO_NOMBRE_VECINO.name
        # ... más mapeos ...

        if estado_objetivo_str:
            contexto_municipio_actual["estado_conversacion"] = estado_objetivo_str
            logger.info(f"Actualizando estado de conversación a: {estado_objetivo_str} debido a pedir_info: '{pedir_info_final}'")
        else:
            logger.warning(f"No se pudo mapear pedir_info '{pedir_info_final}' a un ConversationState. El estado no se actualizará explícitamente aquí.")
            # El estado actual (si lo había) se mantendrá o se limpiará si la acción fue un éxito sin pedir_info.

    # --- Guardar el historial de chat_db_context con la respuesta final del CHATBOT ---
    chat_db_context_live_data["mensajes_previos_gemini_formato"].append({"role": "model", "parts": [{"text": respuesta_final_texto}]})
    # Limitar historial si es necesario
    if len(chat_db_context_live_data["mensajes_previos_gemini_formato"]) > 20: # Ejemplo de límite
        chat_db_context_live_data["mensajes_previos_gemini_formato"] = chat_db_context_live_data["mensajes_previos_gemini_formato"][-20:]


    # --- Serializar y guardar contexto final ---
    # (La lógica de serialización y guardado de contexto_municipio_actual y flag_modified permanece igual que al final del original)
    # ... (código de serialización y guardado) ...

    # ---- INICIO: Lógica de sugerencia de registro PROACTIVA (adaptada) ----
    # Esta lógica ahora se ejecuta DESPUÉS de la lógica principal del handler y ANTES de formatear la respuesta final,
    # solo si la respuesta principal no fue ya una sugerencia de registro.
    # Y solo si el usuario es anónimo.

    respuesta_principal_ya_generada = True # Asumimos que respuesta_final_texto ya tiene algo

    if not viewer_user and anon_id and has_app_context() and respuesta_principal_ya_generada and \
       action_handler_result.get("fuente","") != "sugerencia_registro_municipio_v2": # No sugerir si ya se está sugiriendo

        estado_actual_enum_sug = None
        estado_actual_str_sug = contexto_municipio_actual.get("estado_conversacion") # string o None
        if estado_actual_str_sug:
            try: estado_actual_enum_sug = ConversationState[estado_actual_str_sug]
            except KeyError: pass

        estados_a_evitar_sugerencia_para_anon = [
            ConversationState.ESPERANDO_DIRECCION_RECLAMO, ConversationState.ESPERANDO_NOMBRE_VECINO,
            ConversationState.ESPERANDO_TELEFONO_VECINO, ConversationState.ESPERANDO_EMAIL_VECINO,
            ConversationState.ESPERANDO_DESCRIPCION_RECLAMO, ConversationState.ESPERANDO_ADJUNTOS_RECLAMO,
            ConversationState.ESPERANDO_CONFIRMACION_RECLAMO, ConversationState.ESPERANDO_UBICACION_PANICO,
        ]
        if not estado_actual_enum_sug or estado_actual_enum_sug not in estados_a_evitar_sugerencia_para_anon:
            interacciones_anon_sesion = contexto_municipio_actual.get("interacciones_anon_sesion", 0)
            if len(pregunta_str.split()) > 1 or pregunta_str.lower() not in ["si", "no", "ok", "dale", "bueno"]:
                interacciones_anon_sesion += 1
            contexto_municipio_actual["interacciones_anon_sesion"] = interacciones_anon_sesion

            umbral_sugerencia = current_app.config.get("MUNICIPIO_UMBRAL_SUGERENCIA_REGISTRO", 3)
            if umbral_sugerencia > 0 and interacciones_anon_sesion >= umbral_sugerencia and \
               not contexto_municipio_actual.get("sugerencia_registro_emitida_ronda", False):

                logger_actual.info(f"Anon {anon_id} alcanzó umbral. Añadiendo sugerencia de registro a la respuesta principal.")
                contexto_municipio_actual["sugerencia_registro_emitida_ronda"] = True

                sug_obj = construir_respuesta_sugerir_registro("Para una mejor experiencia y seguimiento.", "municipio", channel)
                # Anexar la sugerencia a la respuesta principal o modificarla
                respuesta_final_texto += f"\n\n{sug_obj['respuesta']}" # Añadir al cuerpo
                opciones_finales.extend(sug_obj.get('botones',[])) # Añadir botones de login/registro
                # Podríamos también cambiar la 'fuente' si la sugerencia domina la respuesta.
    # ---- FIN: Lógica de sugerencia de registro PROACTIVA ----


    # --- Serializar y guardar contexto final ---
    estado_final_para_guardar_str = contexto_municipio_actual.get("estado_conversacion") # Debería ser string o None
    if isinstance(estado_final_para_guardar_str, ConversationState): # Por si acaso no se convirtió a string
        logger_actual.warning(f"Estado {estado_final_para_guardar_str} era Enum antes de serializar. Convirtiendo.")
        contexto_municipio_actual["estado_conversacion"] = estado_final_para_guardar_str.name
    elif estado_final_para_guardar_str is None:
        contexto_municipio_actual.pop("estado_conversacion", None)

    contexto_municipio_serializado_para_db = serializar_enum(contexto_municipio_actual)
    chat_db_context.context_data[CONTEXTO_MUNICIPIO] = contexto_municipio_serializado_para_db
    if chat_db_context:
        flag_modified(chat_db_context, "context_data")

    # --- Formatear respuesta final ---
    message_type_final = "text"
    if opciones_finales:
        num_options = len(opciones_finales)
        if 0 < num_options <= 3: message_type_final = "interactive_buttons"
        elif num_options > 3: message_type_final = "interactive_list"

    final_response_dict = {
        "message_body": respuesta_final_texto,
        "options_list": opciones_finales,
        "message_type": message_type_final,
        "contexto_actualizado": {CONTEXTO_MUNICIPIO: contexto_municipio_serializado_para_db},
        "ticket_id": action_handler_result.get("data", {}).get("ticket_id") or action_handler_result.get("data", {}).get("sugerencia_id"), # Tomar de data si existe
        "fuente": action_handler_result.get("fuente") or llm_response_structured.get("accion_backend", "municipio_general_v4"),
        # Otros campos como media_url, location_data, adjuntos se manejarían si son parte de la respuesta
    }

    # Log de conversación para anónimos
    if anon_id and not viewer_user:
        try:
            db.session.add(Conversacion(
                session_id=kwargs.get("chat_session_uuid") or anon_id, pregunta=pregunta_str,
                respuesta=final_response_dict["message_body"], fuente=final_response_dict["fuente"],
                rubro=getattr(rubro_obj, "nombre", "municipio_general"), user_id=None,
            ))
            db.session.commit()
        except Exception as e_conv_muni_final:
            logger_actual.error(f"Error guardando Conversacion final (municipio): {e_conv_muni_final}", exc_info=True)
            db.session.rollback()

    logger_actual.info(f"[RESPONDER_MUNICIPIO_END_V4] Respuesta: '{final_response_dict['message_body'][:100]}...', Fuente: {final_response_dict['fuente']}")
    return final_response_dict