import os
from flask import jsonify
import pandas as pd
from geopy.geocoders import GoogleV3
from geopy.exc import GeocoderTimedOut, GeocoderServiceError
from dotenv import load_dotenv
import sys
import logging
import re
import json
from enum import Enum, auto
import unicodedata
import difflib
from flask import current_app, has_app_context, session as flask_session
from sqlalchemy.orm.attributes import flag_modified
from models import MunicipioTicket, TicketComentario, db, SitioWebInfo, Conversacion
from services.ticket_service import servicio_tickets
from twilio.rest import Client
from datetime import datetime, timedelta
from services.utils_placeholders import (
    reemplazar_placeholders,
    obtener_respuesta_municipio,
)
from services.config_loader import cargar_configuracion_municipio
from .actions.municipio_actions import (
    CrearReclamoActionHandler,
)
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
from .categorias_municipio import CATEGORIAS_RECLAMO, categorias_normalizadas
from .common_utils import (
    validar_email,
    validar_telefono,
    formatear_telefono_e164,
    construir_respuesta_sugerir_registro,
    extract_multiple_contact_details_regex
)
from .llm_utils import extract_complaint_details_llm, extract_multiple_contact_details_llm
import math
from services.tasks import process_image_for_chat_task
from services.intent_classifier import IntentClassifier

# Initialize the classifier globally
INTENTS_FILE_PATH = os.path.join(os.path.dirname(__file__), '..', 'data', 'intents.json')
intent_classifier = IntentClassifier(intents_file_path=INTENTS_FILE_PATH)

load_dotenv()

# Placeholder for API key management
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY")

def configure_geolocator():
    """Configura y retorna un geolocalizador con la API key."""
    if not GOOGLE_API_KEY:
        raise ValueError("La API key de Google Maps no está configurada.")
    return GoogleV3(api_key=GOOGLE_API_KEY)

def obtener_municipios_cercanos(latitud, longitud, radio_km=5):
    """
    Encuentra municipios cercanos a una latitud y longitud dadas.
    """
    geolocator = configure_geolocator()
    try:
        # Intenta obtener la dirección (localidad) desde las coordenadas
        location = geolocator.reverse((latitud, longitud), exactly_one=True)
        address = location.raw.get('address_components', [])

        # Busca el componente de la dirección que corresponde a la localidad
        municipio_actual = None
        for component in address:
            if 'locality' in component.get('types', []):
                municipio_actual = component.get('long_name')
                break

        if not municipio_actual:
            return jsonify({"error": "No se pudo determinar la localidad desde las coordenadas proporcionadas."}), 404

        # Simulación de búsqueda en un radio (esto debería ser más complejo en una app real)
        # Aquí simplemente devolvemos la localidad encontrada como ejemplo
        municipios_encontrados = [municipio_actual]

        return jsonify({"municipios_cercanos": municipios_encontrados})

    except (GeocoderTimedOut, GeocoderServiceError) as e:
        return jsonify({"error": f"Error en el servicio de geolocalización: {e}"}), 500
    except Exception as e:
        return jsonify({"error": f"Error inesperado: {e}"}), 500

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

def _super_normalize(s: str) -> str:
    """More aggressive normalization for matching, removes all non-alphanumeric chars."""
    s = normalizar_texto(s)
    return re.sub(r'[^a-z0-9]', '', s)


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


def _remove_redundant_urls_from_message(message_body, options_list):
    """
    Removes URLs from the message body if they are already present in the buttons.
    """
    if not message_body or not options_list:
        return message_body

    for option in options_list:
        if isinstance(option, dict) and 'url' in option and option['url'] in message_body:
            message_body = message_body.replace(option['url'], '')

    # Clean up common leftover phrases and extra spaces
    # Using regex to be more robust and case-insensitive
    message_body = re.sub(r'por favor\s+ingresá\s+al\s+siguiente\s+enlace\s*:?', '', message_body, flags=re.IGNORECASE).strip()
    message_body = re.sub(r'ingresá\s+al\s+siguiente\s+enlace\s*:?', '', message_body, flags=re.IGNORECASE).strip()
    message_body = re.sub(r'enlace\s*:?', '', message_body, flags=re.IGNORECASE).strip()

    # Replace multiple spaces with a single space and clean up punctuation
    message_body = re.sub(r'\s{2,}', ' ', message_body).strip()
    message_body = message_body.replace(' .', '.').strip()
    # Remove hanging colons or commas before a period.
    message_body = re.sub(r'[,:]\s*\.', '.', message_body)
    # If the message is just a colon now, clear it.
    if message_body == ':':
        message_body = ''

    return message_body


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
TWILIO_WHATSAPP_NUMBER = os.environ.get(
    "TWILIO_WHATSAPP_NUMBER", "whatsapp:+17432643718"
)
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

def obtener_info_tramite_web(tramite_nombre: str) -> dict:
    """
    Busca información sobre un trámite en la web del municipio.
    """
    from services.scraper_avanzado import extraer_contenido_general

    tramites_links = cargar_configuracion_municipio(MUNICIPIO_ID, "tramites_links.json")
    if not tramites_links:
        return {"error": "No se encontraron links de trámites."}

    for nombre, url in tramites_links.items():
        if tramite_nombre.lower() in nombre.lower():
            return extraer_contenido_general(url)

    return {"error": "No se encontró información sobre el trámite."}

DEFAULT_TRAMITES_WEB_URL = CONFIG_MUNICIPIO.get(
    "tramites_web_url", "https://www.ejemplo.gob.ar/tramites/"
)
MUNICIPIO_DIRECCION = CONFIG_MUNICIPIO.get("direccion", "Dirección del municipio")
EJEMPLO_DIRECCION = CONFIG_MUNICIPIO.get("ejemplo_direccion", "Avenida Siempreviva 123")

from services.conversation_state import ConversationState

# Palabras clave sencillas para detectar consultas generales de servicios
GENERAL_QUERY_KEYWORDS = [
    "veterinaria", "veterinarias", "farmacia", "supermercado", "negocio",
    "servicio", "buscar", "comercio", "local"
]

def es_consulta_general(texto: str) -> bool:
    """Detecta si el texto parece una consulta de servicios generales."""
    texto_norm = normalizar_texto(texto or "")
    return any(k in texto_norm for k in GENERAL_QUERY_KEYWORDS)

# --- Mapping pedir_info -> ConversationState ---
def normalizar_str(s: str) -> str:
    """Normaliza cadenas a minusculas sin tildes ni espacios extra."""
    return unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode("ascii").lower().strip()

PEDIR_INFO_TO_STATE = {
    "ubicacion": ConversationState.ESPERANDO_DIRECCION_RECLAMO,
    "direccion": ConversationState.ESPERANDO_DIRECCION_RECLAMO,
    "categoria": ConversationState.ESPERANDO_CATEGORIA_RECLAMO,
    "descripcion": ConversationState.ESPERANDO_DESCRIPCION_RECLAMO,
    "descripcion_mas_detallada": ConversationState.ESPERANDO_DESCRIPCION_RECLAMO,
    "nombre_completo": ConversationState.ESPERANDO_NOMBRE_VECINO,
    "nombre": ConversationState.ESPERANDO_NOMBRE_VECINO,
    "telefono": ConversationState.ESPERANDO_TELEFONO_VECINO,
    "email": ConversationState.ESPERANDO_EMAIL_VECINO,
    "id_reclamo": ConversationState.ESPERANDO_NUMERO_TICKET,
    "id_ticket": ConversationState.ESPERANDO_NUMERO_TICKET,
    "confirmacion": ConversationState.ESPERANDO_CONFIRMACION_RECLAMO,
    "adjuntos": ConversationState.ESPERANDO_ADJUNTOS_RECLAMO,
}

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




PROMPT_MUNICIPIO_CON_CONTEXTO = """
Sos el asistente digital del municipio. Respondé la PREGUNTA DEL USUARIO usando solo la INFORMACIÓN DE CONTEXTO.
Si no tenés info suficiente, decilo y sugerí contactar al municipio.
--- CONTEXTO ---
{contexto_scraped}
-----------------
PREGUNTA: "{pregunta_usuario}"
Respuesta:
"""
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


from services.handlers.base_handler import BaseMunicipioHandler

from services.google_search import google_search

from services.handlers.greeting_handler import GreetingHandler, _get_main_menu_payload

from services.handlers.news_handler import NewsHandler
from services.handlers.poi_handler import PointsOfInterestHandler

from services.handlers.menu_handler import handle_main_menu_action, handle_contact_category_selection, MENU_KEYWORDS

def handle_info_requests(action_id: str) -> dict:
    """
    Handles simple informational requests based on action IDs from buttons.
    """
    tramites_info = get_tramites_info()
    contactos_info = cargar_configuracion_municipio(MUNICIPIO_ID, "contactos_especializados.json")

    info_map = {
        "info_licencia_conducir": "licencia_de_conducir",
        "info_pago_tasas": "pago_de_tasas_vigentes",
        "info_defensa_consumidor": "defensa_del_consumidor",
    }

    if action_id in info_map:
        tramite_key = info_map[action_id]
        if tramite_key in tramites_info:
            tramite_data = tramites_info[tramite_key]
            return {
                "message_body": tramite_data["descripcion"],
                "options_list": tramite_data["botones"],
                "message_type": "interactive_buttons" if tramite_data["botones"] else "text",
                "fuente": f"info_request_{tramite_key}"
            }
    elif action_id == "info_veterinaria":
        contacto_data = contactos_info.get("Veterinaria y Bromatologia")
        if contacto_data:
            return {
                "message_body": f"Para información vinculada a veterinaria y bromatología municipal escribí al WhatsApp: {contacto_data['telefono']}",
                "options_list": [],
                "message_type": "text",
                "fuente": "info_request_veterinaria"
            }

    return {
        "message_body": "No encontré la información solicitada. Por favor, intentá de nuevo.",
        "options_list": [],
        "message_type": "text",
        "fuente": "info_request_not_found"
    }


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


def serializar_enum(obj):
    if isinstance(obj, Enum): return obj.name
    elif isinstance(obj, dict): return {k: serializar_enum(v) for k, v in obj.items()}
    elif isinstance(obj, list): return [serializar_enum(v) for v in obj]
    else: return obj

def handle_location_update(data):
    """
    Handles a location update from the client.
    It receives latitude and longitude, gets the address,
    and stores it in the user's session.
    """
    from .herramientas_municipio import obtener_direccion_de_coordenadas
    from flask import session

    lat = data.get("lat")
    lon = data.get("lon")

    if not lat or not lon:
        return {"respuesta": "No se pudo obtener la ubicación."}

    direccion_info = obtener_direccion_de_coordenadas(lat, lon)

    if not direccion_info:
        return {"respuesta": "No se pudo obtener la dirección desde las coordenadas."}

    session["user_location"] = direccion_info
    session.modified = True

    return {
        "respuesta": f"Ubicación actualizada a: {direccion_info.get('formatted_address')}"
    }

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




def accion_crear_reclamo_municipio(datos_reclamo, context):
    """Wrapper que delega la creación de reclamos al ActionHandler dedicado."""
    handler = CrearReclamoActionHandler(context=context)
    return handler.execute(datos_reclamo)
def _handle_ticket_creation(contexto_municipio_actual, context, datos_estructura_llm):
    """
    Prepares the confirmation message for the user before creating a ticket.
    It does not create the ticket itself but sets the stage for the final confirmation.
    """
    datos_reclamo = contexto_municipio_actual.get("datos_parciales_llm_reclamo", {})
    datos_reclamo.update(datos_estructura_llm)

    categoria = datos_reclamo.get("categoria")
    descripcion = datos_reclamo.get("descripcion")
    ubicacion = datos_reclamo.get("ubicacion")
    nombre_usuario = datos_reclamo.get("nombre_usuario_detectado")
    telefono_usuario = datos_reclamo.get("telefono_detectado")
    email_usuario = datos_reclamo.get("email_detectado")

    campos_faltantes = [campo for campo, valor in {
        "categoría": categoria, "descripción": descripcion, "ubicación": ubicacion,
        "nombre": nombre_usuario, "teléfono": telefono_usuario, "email": email_usuario
    }.items() if not valor]

    if campos_faltantes:
        return {
            "message_body": f"Para continuar, aún necesito estos datos: {', '.join(campos_faltantes)}.",
            "fuente": "error_crear_reclamo_faltan_datos_previo"
        }, contexto_municipio_actual

    contexto_municipio_actual["estado_conversacion"] = ConversationState.ESPERANDO_CONFIRMACION_DATOS_RECLAMO.name
    contexto_municipio_actual["datos_a_confirmar"] = datos_reclamo.copy()

    mensaje_confirmacion = (
        f"Por favor, confirmá si los datos para tu reclamo son correctos:\n"
        f"- **Categoría**: {categoria}\n"
        f"- **Descripción**: {descripcion}\n"
        f"- **Ubicación**: {ubicacion}\n"
        f"- **Nombre**: {nombre_usuario}\n"
        f"- **Teléfono**: {telefono_usuario}\n"
        f"- **Email**: {email_usuario}"
    )

    botones = [
        {"texto": "Sí, crear reclamo", "action_id": "confirmar_reclamo_si"},
        {"texto": "No, quiero editar", "action_id": "confirmar_reclamo_no"},
    ]

    return {
        "message_body": mensaje_confirmacion,
        "options_list": botones,
        "message_type": "interactive_buttons"
    }, contexto_municipio_actual


from services.llm_interaction_handler import handle_llm_interaction

from fuzzywuzzy import process
from services.flows.reclamo_handler import ReclamoFlowHandler



SIMPLE_GREETINGS = {"hola", "buenos dias", "buenas tardes", "buenas noches", "menu", "hola buenos dias", "hola buenas tardes", "hola buenas noches", "buenas"}
RETURN_TO_MAIN_MENU = {"volver al inicio", "volver al menu", "inicio", "menu", "menú principal"}

def responder_municipio(
    pregunta_original,
    owner_user,
    rubro_obj,
    viewer_user=None,
    chat_db_context=None,
    anon_id=None,
    channel: str = "web",
    location=None,
    **kwargs
):
    logger_actual = current_app.logger if has_app_context() else logger

    # --- START DEBUG LOG ---
    if chat_db_context and chat_db_context.context_data:
        estado_conversacion_debug = chat_db_context.context_data.get(CONTEXTO_MUNICIPIO, {}).get("estado_conversacion")
        logger_actual.info(f"DEBUG: [START] responder_municipio called for session {chat_db_context.chat_session_id}. Initial state: {estado_conversacion_debug}")
    # --- END DEBUG LOG ---

    def _finalize_response(response):
        """Return the response unchanged; audio is handled upstream."""
        return response

    logger_actual.info(
        f"[RESPONDER_MUNICIPIO_START] =================================================="
    )
    logger_actual.info(
        f"[RESPONDER_MUNICIPIO_START] Pregunta: '{pregunta_original}', UserMunicipio: {getattr(owner_user, 'id', 'N/A')}, ViewerCiudadano: {getattr(viewer_user, 'id', 'N/A')}, Anon: {anon_id}, Channel: {channel}, ChatSessionUUID: {kwargs.get('chat_session_uuid')}"
    )

    # --- INICIO REFACTOR: Inicialización de 'context' y 'received_payload' al principio ---
    # Obtener la app actual
    app = current_app._get_current_object()

    # Cargar config específica del municipio (si existe)
    final_municipio_config = CONFIG_MUNICIPIO # Default global
    if owner_user and hasattr(owner_user, 'municipio_id') and owner_user.municipio_id:
        owner_user_municipio_id_str = str(owner_user.municipio_id)
        loaded_specific_config = cargar_configuracion_municipio(owner_user_municipio_id_str, "config.json")
        if loaded_specific_config:
            final_municipio_config = loaded_specific_config

    # Poblar el payload con los datos de la solicitud
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

    # Crear el diccionario de contexto principal una sola vez
    context = {
        "user_obj": owner_user,
        "viewer_user_obj": viewer_user,
        "cliente_id": getattr(viewer_user, "id", None),
        "anon_id": anon_id,
        "rubro_obj": rubro_obj,
        "channel": channel,
        "municipio_config_actual": final_municipio_config,
        "chat_session_uuid": kwargs.get("chat_session_uuid"),
        "chat_db_context_data": {}, # Se poblará después de cargar desde la DB
        "intencion": kwargs.get("intencion"),
        "ubicacion_usuario": location or received_payload.get("ubicacion_usuario"),
        "es_foto": False, "foto_url": None,
        "es_ubicacion": received_payload.get("es_ubicacion", False),
        "es_archivo": received_payload.get("es_archivo", False),
        "action": received_payload.get("action"),
        "datos_interpretados_archivo": kwargs.get("datos_interpretados_archivo"),
        "archivo_id_para_asociar": kwargs.get("archivo_id_para_asociar"),
    }
    # --- FIN REFACTOR ---

    pregunta_str_for_check = pregunta_str

    # --- CONTEXT INITIALIZATION ---
    # This is now at the top to ensure all parts of the function have access to the full context.
    final_municipio_config = CONFIG_MUNICIPIO
    if owner_user and hasattr(owner_user, 'municipio_id') and owner_user.municipio_id:
        owner_user_municipio_id_str = str(owner_user.municipio_id)
        loaded_specific_config = cargar_configuracion_municipio(owner_user_municipio_id_str, "config.json")
        if loaded_specific_config:
            final_municipio_config = loaded_specific_config

    received_payload = {}
    pregunta_str = ""
    if isinstance(pregunta_original, dict):
        received_payload = pregunta_original
        pregunta_str = received_payload.get("pregunta", "")
    elif isinstance(pregunta_original, str):
        pregunta_str = pregunta_original
        received_payload["pregunta"] = pregunta_original
    else:
        pregunta_str = ""
        received_payload["pregunta"] = ""

    if kwargs:
        received_payload.update(kwargs)

    chat_db_context_live_data = {}
    if chat_db_context and chat_db_context.context_data is not None:
        chat_db_context_live_data = chat_db_context.context_data
    
    contexto_municipio_actual = chat_db_context_live_data.setdefault(CONTEXTO_MUNICIPIO, {})

    context = {
        CONTEXTO_MUNICIPIO: contexto_municipio_actual,
        "user_obj": owner_user,
        "viewer_user_obj": viewer_user,
        "cliente_id": getattr(viewer_user, "id", None),
        "anon_id": anon_id,
        "rubro_obj": rubro_obj,
        "channel": channel,
        "municipio_config_actual": final_municipio_config,
        "chat_session_uuid": kwargs.get("chat_session_uuid"),
        "chat_db_context_data": chat_db_context_live_data,
        "profile_name": kwargs.get("profile_name"),
        # Other kwargs will be in received_payload
    }
    # --- END CONTEXT INITIALIZATION ---

    # For simple greetings, bypass LLM and show the main menu directly.
    # --- Audio Processing Logic ---
    is_from_audio = False
    if isinstance(pregunta_original, dict) and "media_url" in pregunta_original:
        from services.audio_transcription_service import transcribe_audio_from_url
        is_from_audio = True

        # Auto-learn prefers_audio
        if viewer_user:
            audio_message_count = contexto_municipio_actual.get('audio_message_count', 0) + 1
            contexto_municipio_actual['audio_message_count'] = audio_message_count
            if audio_message_count >= 2 and not viewer_user.prefers_audio:
                viewer_user.prefers_audio = True
                db.session.add(viewer_user)
                db.session.commit()
                logger_actual.info(f"User {viewer_user.id} prefers audio after {audio_message_count} audio messages.")

        transcription_result = transcribe_audio_from_url(pregunta_original["media_url"])
        if transcription_result:
            transcript = transcription_result.get("transcript")
            confidence = transcription_result.get("confidence", 1.0)

            if confidence < 0.8: # Low confidence threshold
                contexto_municipio_actual['estado_conversacion'] = 'ESPERANDO_CONFIRMACION_STT'
                contexto_municipio_actual['stt_transcript_pendiente'] = transcript
                return _finalize_response({
                    "message_body": f"Escuché: \"{transcript}\". ¿Es correcto?",
                    "options_list": [
                        {"texto": "Sí, es correcto", "action_id": "confirmar_stt_si"},
                        {"texto": "No, intentar de nuevo", "action_id": "confirmar_stt_no"}
                    ],
                    "message_type": "interactive_buttons"
                })
            else:
                pregunta_str = transcript # Use high-confidence transcript as the new question
                # Update the payload so subsequent logic sees the transcribed text
                if "pregunta" in received_payload:
                    received_payload["pregunta"] = pregunta_str


    # --- INICIO FIX: Manejo explícito de solicitud de menú principal ---
    # Si el usuario pide explícitamente el menú, lo mostramos directamente sin pasar por el LLM.
    if not is_from_audio:
        # --- START INTENT CLASSIFICATION ---
        # FIX: First, check for simple keywords and __INIT__ to be more robust and cost-effective
        normalized_input_for_greeting = normalizar_texto(pregunta_str or "").strip()
        if normalized_input_for_greeting in SIMPLE_GREETINGS or pregunta_str == "__INIT__":
            logger_actual.info(f"Simple greeting or __INIT__ keyword detected ('{pregunta_str}'). Bypassing LLM and showing main menu.")
            handler = GreetingHandler(context)
            response = handler.handle(received_payload)
            if chat_db_context:
                flag_modified(chat_db_context, "context_data")
            return _finalize_response(response)

        # If it's not a simple greeting, proceed with intent classification
        intent, intent_payload = intent_classifier.classify(pregunta_str)
        logger_actual.info(f"[IntentClassifier] Classified intent: {intent} with payload: {intent_payload}")

        if intent == "saludar":
            logger_actual.info("Greeting intent detected. Bypassing LLM and showing main menu.")
            handler = GreetingHandler(context)
            response = handler.handle(received_payload)
            if chat_db_context:
                flag_modified(chat_db_context, "context_data")
            return _finalize_response(response)

        if intent == "iniciar_reclamo":
            logger_actual.info("Claim initiation intent detected. Starting ReclamoFlowHandler.")
            reclamo_handler = ReclamoFlowHandler(context)
            return _finalize_response(reclamo_handler.start_flow())

        if intent == "consultar_reclamo":
            logger_actual.info("Claim status check intent detected. Bypassing LLM.")
            return _finalize_response({
                "message_body": "Para consultar el estado de tu reclamo, por favor ingresá el número de ticket.",
                "fuente": "intent_consultar_reclamo"
            })
        # --- END INTENT CLASSIFICATION ---


    # El manejo de reseteo por palabra clave ahora es manejado por el LLM
    # que debe devolver accion_backend: "saludar".

    # Obtener el estado actual de la conversación antes de evaluar acciones
    estado_conversacion = contexto_municipio_actual.get("estado_conversacion")
    action = received_payload.get("action")

    if estado_conversacion == 'ESPERANDO_CONFIRMACION_STT':
        transcript_pendiente = contexto_municipio_actual.get('stt_transcript_pendiente')
        contexto_municipio_actual['estado_conversacion'] = None
        contexto_municipio_actual.pop('stt_transcript_pendiente', None)

        if "si" in normalizar_texto(pregunta_str) or (action and "si" in action):
            pregunta_str = transcript_pendiente
            if "pregunta" in received_payload:
                received_payload["pregunta"] = pregunta_str
        else:
            return _finalize_response({
                "message_body": "Entendido. Por favor, intentá de nuevo o escribí tu consulta.",
                "options_list": []
            })

    # New main menu handler
    # FIX: Reordered logic. First, check for specific reclamo actions that set state.
    # Then, handle generic main menu actions that return immediately.
    reclamo_categories = {
        "reclamo_luminaria": "Luminaria",
        "reclamo_arbolado": "Arbolado",
        "reclamo_limpieza_riego": "Limpieza y riego",
        "reclamo_arreglo_calle": "Arreglo de calle",
        "reclamo_otros": "Otros",
    }

    if action in reclamo_categories:
        contexto_municipio_actual = chat_db_context.context_data.setdefault(CONTEXTO_MUNICIPIO, {})

        logger_actual.info(f"[ACTION_RECLAMO] New claim started via action '{action}'. Clearing previous claim context.")
        contexto_municipio_actual.pop("datos_parciales_llm_reclamo", None)
        contexto_municipio_actual.pop("historial_llm_reclamo", None)

        contexto_municipio_actual.setdefault('datos_parciales_llm_reclamo', {})['categoria'] = reclamo_categories[action]
        contexto_municipio_actual['estado_conversacion'] = ConversationState.ESPERANDO_INFO_RECLAMO_LLM.name
        # After setting state, we let the execution fall through to the LLM handler
    elif action == "mostrar_menu_reclamos":
        logger_actual.info("Claim initiation action detected. Starting ReclamoFlowHandler.")
        reclamo_handler = ReclamoFlowHandler(context)
        response = reclamo_handler.start_flow()
        if chat_db_context:
            flag_modified(chat_db_context, "context_data")
        return _finalize_response(response)
    elif action:
        response = handle_main_menu_action(action, context, chat_db_context)
        if response:
            return _finalize_response(response)


    # --- INICIO: Manejo de selección de lista dinámica (Noticias, etc.) ---
    elif estado_conversacion == ConversationState.ESPERANDO_SELECCION_DE_LISTA.name:
        opciones_guardadas = contexto_municipio_actual.get('opciones_en_pantalla', [])
        seleccion = None
        if pregunta_str.isdigit():
            try:
                indice = int(pregunta_str) - 1
                if 0 <= indice < len(opciones_guardadas):
                    seleccion = opciones_guardadas[indice]
            except (ValueError, IndexError):
                pass
        
        # Limpiar contexto para el siguiente turno
        contexto_municipio_actual['estado_conversacion'] = None
        contexto_municipio_actual.pop('opciones_en_pantalla', None)

        if seleccion and seleccion.get('url'):
            return _finalize_response({
                "message_body": f"Aquí tienes el enlace que pediste: {seleccion.get('url')}",
                "options_list": [{"texto": "Menú Principal", "action_id": "saludar"}],
                "message_type": "interactive_buttons",
                "fuente": "seleccion_lista_dinamica"
            })
        else:
            # Si no se pudo procesar la selección, volver al menú principal
            return _finalize_response(GreetingHandler(context).handle({}))


    if action == "iniciar_reclamo": # Kept for backward compatibility or other flows
        return _finalize_response(_get_reclamos_menu())

    if action == "reclamo_perdida_agua":
        return _finalize_response({
            "message_body": "Para pérdida de agua, dirigite a la página de Aysam:\nhttps://www.aysam.com.ar/",
            "options_list": [],
            "message_type": "text",
            "fuente": "info_perdida_agua"
        })

    USAR_LLM_PARA_RECLAMOS = True # Feature flag para la nueva lógica LLM
    respuesta_manejada_por_llm = False # Flag para indicar si el LLM ya manejó la respuesta

    # >>> INICIO FIX: Si la pregunta está vacía pero se recibió una ubicación, crear una pregunta para el LLM
    if not pregunta_str.strip() and location:
        lat = location.get('latitude')
        lon = location.get('longitude')
        address = location.get('address', f"coordenadas {lat}, {lon}")

        pregunta_str = (
            f"El usuario ha compartido una ubicación sin texto adicional. "
            f"La ubicación es: {address}. "
            f"Es muy probable que quiera reportar un problema en este lugar. "
            f"Por favor, actúa proactivamente: confirma la ubicación con el usuario y pregúntale "
            f"directamente qué problema o reclamo quiere reportar en esa dirección."
        )
        received_payload['pregunta'] = pregunta_str
        logger_actual.info(f"Pregunta generada a partir de ubicación: '{pregunta_str}'")
    # <<< FIN FIX

    # --- INICIO: Manejo proactivo de multimedia y ubicación ---
    # Si el usuario envía solo una imagen o ubicación, el bot debe actuar proactivamente.
    if not pregunta_str.strip(): # Solo actuar si no hay texto del usuario
        synthetic_prompt = None
        datos_interpretados = kwargs.get("datos_interpretados_archivo")

        if datos_interpretados and isinstance(datos_interpretados, dict):
            logger_actual.info(f"Manejando proactivamente un archivo interpretado: {datos_interpretados}")
            categoria = datos_interpretados.get("categoria_sugerida", "No especificada")
            descripcion = datos_interpretados.get("descripcion_sugerida", "No especificada")
            synthetic_prompt = (
                f"El usuario ha enviado una imagen para iniciar un reclamo. "
                f"El análisis automático sugiere: Categoría='{categoria}', Descripción='{descripcion}'. "
                f"Inicia el proceso de reclamo confirmando estos datos con el usuario y pide la información que falte (ej. ubicación)."
            )
        elif location:
            logger_actual.info(f"Manejando proactivamente una ubicación: {location}")
            address = location.get("address", f"coordenadas {location.get('latitude')}, {location.get('longitude')}")
            synthetic_prompt = (
                f"El usuario ha compartido la ubicación '{address}' sin texto adicional. "
                f"Actúa proactivamente: confirma la ubicación con el usuario y pregúntale qué problema quiere reportar en esa dirección."
            )

        if synthetic_prompt:
            logger_actual.info(f"Pregunta sintética generada para manejo proactivo: '{synthetic_prompt}'")
            # Forzar el estado a conversación general para que el LLM tome el control
            contexto_municipio_actual = chat_db_context.context_data.setdefault(CONTEXTO_MUNICIPIO, {})
            contexto_municipio_actual['estado_conversacion'] = ConversationState.CONVERSACION_GENERAL_LLM.name

            response_dict, _ = handle_llm_interaction(
                app, synthetic_prompt, context, viewer_user, owner_user, chat_db_context, contexto_municipio_actual
            )
            if response_dict:
                return _finalize_response(response_dict)
    # --- FIN: Manejo proactivo ---

    contexto_municipio_data_from_db = {}
    chat_db_context_live_data = {}

    if chat_db_context:
        if chat_db_context.context_data is None:
            chat_db_context.context_data = {}
        chat_db_context_live_data = chat_db_context.context_data # Reference to the live dict
        context["chat_db_context_data"] = chat_db_context_live_data # Update main context with live data
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

    # Log the current state of the conversation
    estado_conversacion = contexto_municipio_data_from_db.get("estado_conversacion")
    logger_actual.info(f"[CONTEXTO_MUNICIPIO] Estado de conversacion actual: {estado_conversacion}")

    # Directly use the dictionary from the live context data.
    # This ensures that modifications are made to the original object.
    contexto_municipio_actual = chat_db_context_live_data.setdefault(CONTEXTO_MUNICIPIO, {})
    context[CONTEXTO_MUNICIPIO] = contexto_municipio_actual # Ensure main context points to this sub-context

    # --- INICIO: Manejo de selección de menú principal por número, letra o keyword ---
    if estado_conversacion == ConversationState.ESPERANDO_SELECCION_MENU_PRINCIPAL.name:
        pregunta_str_menu = ""
        if isinstance(pregunta_original, str):
            pregunta_str_menu = pregunta_original
        elif isinstance(pregunta_original, dict) and "pregunta" in pregunta_original:
            pregunta_str_menu = pregunta_original["pregunta"]

        logger_actual.info(f"Handling input in ESPERANDO_SELECCION_MENU_PRINCIPAL state. Input: '{pregunta_str_menu}'")

        # Get the definitive menu structure from the payload generator
        # This ensures that the menu we check against is the same one the user saw.
        menu_payload = _get_main_menu_payload(context)
        # The payload has 'options_list' which is the flat list of buttons with 'id' and 'texto'
        flat_buttons = menu_payload.get('options_list', [])

        # We need to adapt the list for find_menu_action_by_input, which expects 'action_id'
        buttons_for_finder = []
        for btn in flat_buttons:
            buttons_for_finder.append({
                "texto": btn.get("texto"),
                "action_id": btn.get("id") # The 'id' key holds the action_id
            })

        selected_action = find_menu_action_by_input(pregunta_str_menu, buttons_for_finder)

        if selected_action:
            logger_actual.info(f"User input '{pregunta_str_menu}' matched to action: '{selected_action}'")

            # Special handling for actions that lead to a sub-menu
            if selected_action == "mostrar_menu_reclamos":
                contexto_municipio_actual['estado_conversacion'] = ConversationState.ESPERANDO_SELECCION_MENU_RECLAMOS.name
                if chat_db_context: flag_modified(chat_db_context, "context_data")
                return _finalize_response(_get_reclamos_menu())

            # For informational actions, handle them and then return to the main menu prompt.
            response = handle_main_menu_action(selected_action, context, chat_db_context)
            if response:
                contexto_municipio_actual['estado_conversacion'] = ConversationState.ESPERANDO_SELECCION_MENU_PRINCIPAL.name

                # Add a follow-up message.
                follow_up_message = "\n\n¿Puedo ayudarte con algo más?"
                augmented_message = response.get('message_body', '').strip() + follow_up_message

                # Get the main menu to show after the informational message.
                main_menu_payload = _get_main_menu_payload(context, welcome_message_override=augmented_message)

                # The main_menu_payload now contains the combined message and the full menu structure.
                # We can use it directly as the new response.

                if chat_db_context:
                    flag_modified(chat_db_context, "context_data")

                return _finalize_response(main_menu_payload)
        else:
            # If the input doesn't match a menu option, treat it as a general query.
            # Clear the state so it falls through to the main LLM handler.
            logger_actual.info(f"Input '{pregunta_str_menu}' is not a menu option. Treating as a general query and falling through to LLM.")
            contexto_municipio_actual['estado_conversacion'] = None
            if chat_db_context:
                flag_modified(chat_db_context, "context_data")
    # --- FIN: Manejo de selección de menú principal ---

    # --- INICIO: Manejo de la espera por nombre de trámite ---
    elif estado_conversacion == ConversationState.ESPERANDO_SELECCION_TRAMITE.name:
        logger_actual.info(f"Handling input in ESPERANDO_SELECCION_TRAMITE state. Input: '{pregunta_str}'")
        from .actions.municipio_actions import ConsultarInfoTramiteActionHandler

        # The handler expects the data in the payload dict
        received_payload['nombre_tramite'] = pregunta_str

        handler = ConsultarInfoTramiteActionHandler(context)
        handler_response = handler.execute(received_payload)

        # The handler should now succeed and return a standard response with the info.
        # We need to clear the state after this.
        contexto_municipio_actual['estado_conversacion'] = None
        if chat_db_context:
            flag_modified(chat_db_context, "context_data")
        return _finalize_response(handler_response)
    # --- FIN: Manejo de la espera por nombre de trámite ---

    # --- INICIO: Manejo de flujo de reclamo ---
    elif estado_conversacion == "RECLAMO_EN_PROGRESO":
        logger_actual.info(f"Continuing with ReclamoFlowHandler. Input: '{pregunta_str}'")
        reclamo_handler = ReclamoFlowHandler(context)
        response = reclamo_handler.handle(pregunta_original)
        if chat_db_context:
            flag_modified(chat_db_context, "context_data")
        return _finalize_response(response)
    # --- FIN: Manejo de flujo de reclamo ---

    elif estado_conversacion == ConversationState.ESPERANDO_SELECCION_CATEGORIA_CONTACTO.name:
        return _finalize_response(handle_contact_category_selection(pregunta_str, context, chat_db_context))

    # --- INICIO: Manejo de actualización de datos de usuario ---
    elif estado_conversacion == ConversationState.ESPERANDO_NUEVO_DATO_USUARIO.name:
        campo_a_actualizar = contexto_municipio_actual.get('campo_a_actualizar')
        nuevo_valor = pregunta_str.strip()

        if not campo_a_actualizar:
            logger_actual.error("[UPDATE_USER_DATA] In ESPERANDO_NUEVO_DATO_USUARIO state but no 'campo_a_actualizar' in context. Resetting.")
            contexto_municipio_actual['estado_conversacion'] = None
        elif not viewer_user:
            logger_actual.error("[UPDATE_USER_DATA] Cannot update data for a non-logged-in user. Resetting.")
            contexto_municipio_actual['estado_conversacion'] = None
            # Limpiar contexto para no quedar en un bucle
            contexto_municipio_actual.pop('campo_a_actualizar', None)
            contexto_municipio_actual.pop('accion_original_para_reintentar', None)
            return _finalize_response({"message_body": "Para actualizar tus datos, primero necesitás iniciar sesión. ¿Querés que te ayude con eso?", "fuente": "update_data_login_required"})
        else:
            from services.user_service import actualizar_perfil_usuario
            resultado_actualizacion = actualizar_perfil_usuario(
                user_id=viewer_user.id,
                datos_actualizacion={campo_a_actualizar: nuevo_valor}
            )

            if resultado_actualizacion.get('status') == 'success':
                contexto_municipio_actual.pop('campo_a_actualizar', None)
                contexto_municipio_actual['estado_conversacion'] = None # Reset state to re-evaluate

                accion_original = contexto_municipio_actual.pop('accion_original_para_reintentar', "continuar con el reclamo")

                pregunta_str = (
                    f"Acabo de actualizar el/la {campo_a_actualizar} del usuario a '{nuevo_valor}'. "
                    f"Confirma al usuario que el dato fue actualizado correctamente. "
                    f"Ahora, por favor, continúa con su pedido original, que era: '{accion_original}'"
                )

                logger_actual.info(f"Generated synthetic prompt to resume flow: {pregunta_str}")
                # La ejecución continuará y llamará a handle_llm_interaction con la nueva pregunta_str
            else:
                contexto_municipio_actual['estado_conversacion'] = None # Reset state
                error_message = resultado_actualizacion.get('message', f"Hubo un error al actualizar tu {campo_a_actualizar}.")
                return _finalize_response({
                    "message_body": error_message,
                    "options_list": [],
                    "message_type": "text",
                    "fuente": "error_actualizacion_dato_usuario"
                })
    # --- FIN: Manejo de actualización de datos de usuario ---

    # --- INICIO: Manejo de recepción de ubicación para consulta general ---
    elif estado_conversacion == ConversationState.ESPERANDO_UBICACION_GENERAL.name:
        if location:
            consulta_guardada = contexto_municipio_actual.pop('consulta_pendiente_ubicacion', None)
            contexto_municipio_actual['estado_conversacion'] = None # Clear state
            if chat_db_context: flag_modified(chat_db_context, "context_data")

            if consulta_guardada:
                logger_actual.info(f"Received location, processing saved query: '{consulta_guardada}'")
                return _finalize_response(PointsOfInterestHandler(context={}).handle({"pregunta": consulta_guardada, "location": location.get("address")}))
            else:
                logger_actual.warning("In ESPERANDO_UBICACION_GENERAL state but no saved query found.")
                return _finalize_response({"message_body": "Recibí tu ubicación, pero no recuerdo qué estabas buscando. ¿Podrías decírmelo de nuevo?", "options_list": [], "message_type": "text", "fuente": "error_no_saved_query"})
        else:
            # If no location object was sent, check if the user typed an address
            if pregunta_str and len(pregunta_str) > 5: # Basic check to see if it's a potential address
                from .herramientas_municipio import validar_y_formatear_direccion
                logger_actual.info(f"Attempting to geocode textual address: '{pregunta_str}'")

                # We can use the simpler geocoding tool here
                geocoded_location = validar_y_formatear_direccion(pregunta_str)

                if geocoded_location:
                    # Address was valid, proceed with the original query
                    consulta_guardada = contexto_municipio_actual.pop('consulta_pendiente_ubicacion', None)
                    contexto_municipio_actual['estado_conversacion'] = None # Clear state
                    if chat_db_context: flag_modified(chat_db_context, "context_data")

                    if consulta_guardada:
                        logger_actual.info(f"Geocoded address successfully. Processing saved query: '{consulta_guardada}'")
                        # The handler expects the address string in the 'location' key
                        return _finalize_response(PointsOfInterestHandler(context={}).handle({"pregunta": consulta_guardada, "location": geocoded_location.get("formatted_address")}))
                    else:
                        # This case is unlikely but handled for safety
                        logger_actual.warning("Geocoded address but no saved query found.")
                        return _finalize_response({"message_body": f"OK, entiendo que estás en {geocoded_location.get('formatted_address')}. ¿Qué necesitabas buscar?", "options_list": [], "message_type": "text", "fuente": "geocoded_but_no_query"})
                else:
                    # Geocoding failed, stay in the same state and re-prompt.
                    if chat_db_context: flag_modified(chat_db_context, "context_data")
                    return _finalize_response({
                        "message_body": "No pude entender esa dirección. Por favor, intentá de nuevo con más detalles, usá el botón para compartir tu ubicación, o escribí 'cancelar' para salir.",
                        "options_list": [{"texto": "Compartir ubicación", "action": "compartir_ubicacion"}, {"texto": "Cancelar", "action": "cancelar"}],
                        "message_type": "interactive_buttons",
                        "fuente": "geocoding_failed_reprompt"
                    })
            else:
                # User sent something that is not a location and not a potential address.
                # Check for cancellation.
                cancel_keywords = {"cancelar", "no", "salir", "basta", "terminar"}
                if normalizar_texto(pregunta_str) in cancel_keywords or action == "cancelar":
                    contexto_municipio_actual['estado_conversacion'] = None # Reset state
                    contexto_municipio_actual.pop('consulta_pendiente_ubicacion', None)
                    if chat_db_context: flag_modified(chat_db_context, "context_data")
                    return _finalize_response({"message_body": "Ok, cancelado. ¿En qué otra cosa te puedo ayudar?", "options_list": [], "message_type": "text", "fuente": "ubicacion_cancelled"})
                else:
                    # Not a location, not an address, not a cancellation. Re-prompt.
                    if chat_db_context: flag_modified(chat_db_context, "context_data")
                    return _finalize_response({
                        "message_body": "No recibí una ubicación. Por favor, compartí tu ubicación, escribí una dirección, o escribí 'cancelar' para salir.",
                        "options_list": [{"texto": "Compartir ubicación", "action": "compartir_ubicacion"}, {"texto": "Cancelar", "action": "cancelar"}],
                        "message_type": "interactive_buttons",
                        "fuente": "no_location_reprompt"
                    })
    # --- FIN: Manejo de recepción de ubicación ---

    elif estado_conversacion == ConversationState.ESPERANDO_CONFIRMACION_DATOS_RECLAMO.name:
        if "si" in normalizar_texto(pregunta_str) or action == "confirmar_reclamo_si":
            datos_confirmados = contexto_municipio_actual.pop("datos_a_confirmar", {})

            # Llamar a la acción de creación de reclamo
            handler = CrearReclamoActionHandler(context)
            response = handler.execute(datos_confirmados)

            # Limpiar el estado de la conversación solo si la creación fue exitosa
            if response.get("success"):
                contexto_municipio_actual['estado_conversacion'] = None

            if chat_db_context:
                flag_modified(chat_db_context, "context_data")

            return _finalize_response(response)
        else: # User wants to edit
            contexto_municipio_actual['estado_conversacion'] = ConversationState.ESPERANDO_CORRECCION_DATOS_RECLAMO.name
            if chat_db_context:
                flag_modified(chat_db_context, "context_data")
            return _finalize_response({
                "message_body": "Entendido. ¿Qué dato te gustaría corregir o agregar? Por favor, decímelo y lo corrijo.",
                "options_list": [],
                "message_type": "text",
                "fuente": "pide_correccion_reclamo"
            })

    elif estado_conversacion == ConversationState.ESPERANDO_CORRECCION_DATOS_RECLAMO.name:
        logger_actual.info(f"Handling input in ESPERANDO_CORRECCION_DATOS_RECLAMO state. Input: '{pregunta_str}'")

        datos_nuevos = extract_multiple_contact_details_regex(pregunta_str, ["nombre", "email", "telefono", "ubicacion", "descripcion"])
        datos_pendientes = contexto_municipio_actual.get("datos_a_confirmar", {})

        # Mapeo de claves para actualizar correctamente
        if datos_nuevos.get("nombre"): datos_pendientes["nombre_usuario_detectado"] = datos_nuevos["nombre"]
        if datos_nuevos.get("email"): datos_pendientes["email_detectado"] = datos_nuevos["email"]
        if datos_nuevos.get("telefono"): datos_pendientes["telefono_detectado"] = datos_nuevos["telefono"]
        if datos_nuevos.get("ubicacion"): datos_pendientes["ubicacion"] = datos_nuevos["ubicacion"]
        if datos_nuevos.get("descripcion"): datos_pendientes["descripcion"] = datos_nuevos["descripcion"]

        contexto_municipio_actual["datos_a_confirmar"] = datos_pendientes
        contexto_municipio_actual['estado_conversacion'] = ConversationState.ESPERANDO_CONFIRMACION_DATOS_RECLAMO.name

        mensaje_confirmacion = (
            f"""Perfecto, he actualizado los datos. Por favor, confirmá si ahora son correctos:
*Categoría:* {datos_pendientes.get('categoria', 'No especificada')}
*Descripción:* {datos_pendientes.get('descripcion', 'No especificada')}
*Ubicación:* {datos_pendientes.get('ubicacion', 'No especificada')}
*Nombre:* {datos_pendientes.get('nombre_usuario_detectado', 'No especificado')}
*Teléfono:* {datos_pendientes.get('telefono_detectado', 'No especificado')}
*Email:* {datos_pendientes.get('email_detectado', 'No especificado')}
"""
        )

        botones = [
            {"texto": "Sí, crear reclamo", "action_id": "confirmar_reclamo_si"},
            {"texto": "No, seguir editando", "action_id": "confirmar_reclamo_no"},
        ]

        return _finalize_response({
            "message_body": mensaje_confirmacion,
            "options_list": botones,
            "message_type": "interactive_buttons",
            "fuente": "re_pide_confirmacion_reclamo"
        })


    # Initialize the context if it's empty
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
        "ubicacion_usuario": location or received_payload.get("ubicacion_usuario"),
        "es_foto": False, "foto_url": None, # Defaults, will be updated after inspecting payload
        "es_ubicacion": received_payload.get("es_ubicacion", False),
        "es_archivo": received_payload.get("es_archivo", False),
        "action": received_payload.get("action"), # From button clicks, etc.
        "datos_interpretados_archivo": kwargs.get("datos_interpretados_archivo"),
        "archivo_id_para_asociar": kwargs.get("archivo_id_para_asociar"),
    }
    if not (chat_db_context and hasattr(chat_db_context, 'context_data')):
        logger_actual.critical("chat_db_context.context_data no disponible al inicializar 'context'. Usando dict vacío. Esto es problemático.")

    # Check for completed analysis in the context
    if chat_db_context_live_data.get("web_analisis_listo"):
        analisis_info = chat_db_context_live_data.pop("web_analisis_listo")
        from models import AnalisisArchivo
        analisis_obj = db.session.get(AnalisisArchivo, analisis_info.get("archivo_id"))
        if analisis_obj and analisis_obj.texto_extraido:
            pregunta_str = analisis_obj.texto_extraido
            logger_actual.info(f"Usando texto de análisis de archivo como pregunta: '{pregunta_str}'")

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

    if contexto_municipio_actual.get("estado_conversacion") == ConversationState.ESPERANDO_CONFIRMACION_UBICACION.name:
        if "si" in pregunta_str.lower():
            contexto_municipio_actual["ubicacion_confirmada"] = True
            contexto_municipio_actual["estado_conversacion"] = None
        else:
            contexto_municipio_actual["ubicacion_confirmada"] = False
            contexto_municipio_actual["estado_conversacion"] = ConversationState.ESPERANDO_DIRECCION_RECLAMO.name
            return _finalize_response({
                "message_body": "Por favor, decime la nueva ubicación.",
                "options_list": [],
                "message_type": "text",
                "fuente": "pedir_nueva_ubicacion"
            })

    if USAR_LLM_PARA_RECLAMOS:
        # --- INICIO FIX: Resetear contexto de reclamo si llega una nueva imagen analizada ---
        datos_interpretados = kwargs.get("datos_interpretados_archivo")
        if datos_interpretados and isinstance(datos_interpretados, dict):
            logger_actual.info("[CONTEXT_RESET] Se detectaron datos de archivo interpretados. Forzando reseteo de contexto de reclamo.")

            # Guardar datos de contacto antes de limpiar
            datos_parciales_existentes = contexto_municipio_actual.get("datos_parciales_llm_reclamo", {})
            datos_de_contacto_a_preservar = {
                "nombre_usuario_detectado": datos_parciales_existentes.get("nombre_usuario_detectado"),
                "telefono_detectado": datos_parciales_existentes.get("telefono_detectado"),
                "email_detectado": datos_parciales_existentes.get("email_detectado"),
            }

            # Limpiar contexto de reclamo anterior
            contexto_municipio_actual["historial_llm_reclamo"] = []
            contexto_municipio_actual["datos_parciales_llm_reclamo"] = {}

            # Repoblar con la nueva información del análisis de imagen
            if datos_interpretados.get("categoria_sugerida"):
                contexto_municipio_actual["datos_parciales_llm_reclamo"]["categoria"] = datos_interpretados["categoria_sugerida"]
            if datos_interpretados.get("descripcion_sugerida"):
                contexto_municipio_actual["datos_parciales_llm_reclamo"]["descripcion"] = datos_interpretados["descripcion_sugerida"]

            # Restaurar datos de contacto si existían
            contexto_municipio_actual["datos_parciales_llm_reclamo"].update({k: v for k, v in datos_de_contacto_a_preservar.items() if v})

            # Establecer el estado para que el LLM sepa que está en un flujo de reclamo
            contexto_municipio_actual["estado_conversacion"] = ConversationState.ESPERANDO_INFO_RECLAMO_LLM.name

            # >>> INICIO FIX: Si la pregunta está vacía pero la imagen se interpretó como reclamo, crear una pregunta para el LLM
            if not pregunta_str.strip() and datos_interpretados.get("es_reclamo"):
                categoria = datos_interpretados.get("categoria_sugerida", "No especificada")
                descripcion = datos_interpretados.get("descripcion_sugerida", "No especificada")

                pregunta_str = (
                    f"El usuario ha enviado una imagen para iniciar un reclamo. "
                    f"El análisis automático de la imagen sugiere la siguiente información: "
                    f"Categoría: '{categoria}', Descripción: '{descripcion}'. "
                    f"Por favor, inicia el proceso de reclamo confirmando estos datos con el usuario y "
                    f"solicita la información que falte, como la ubicación."
                )
                logger_actual.info(f"Pregunta generada a partir de imagen: '{pregunta_str}'")
            # <<< FIN FIX


        logger_actual.info(f"[BEFORE_HANDLE_LLM] Contexto: {contexto_municipio_actual}")
        respuesta_manejada_por_llm, contexto_municipio_actual = handle_llm_interaction(app, pregunta_str, context, viewer_user, owner_user, chat_db_context, contexto_municipio_actual)
        logger_actual.info(f"[AFTER_HANDLE_LLM] Contexto: {contexto_municipio_actual}")
        if respuesta_manejada_por_llm:
            if not isinstance(respuesta_manejada_por_llm, dict):
                respuesta_manejada_por_llm = {"message_body": str(respuesta_manejada_por_llm)}

            # Clean the message body of redundant URLs that are in buttons
            if 'message_body' in respuesta_manejada_por_llm:
                respuesta_manejada_por_llm['message_body'] = _remove_redundant_urls_from_message(
                    respuesta_manejada_por_llm.get('message_body'),
                    respuesta_manejada_por_llm.get('options_list', [])
                )

            respuesta_manejada_por_llm.setdefault("message_type", "text")
            respuesta_manejada_por_llm.setdefault("options_list", [])
            return _finalize_response(respuesta_manejada_por_llm)

        # Si la intención se estableció en derivar a un agente, significa que el flujo del LLM
        # ya manejó la lógica y no debemos continuar con el flujo antiguo.
        if context.get("intencion") == "hablar_con_agente":
            mensaje_para_escalar = contexto_municipio_actual.get("mensaje_previo_llm_para_escalamiento", "Un agente se pondrá en contacto contigo en breve.")
            # Ensure the context is saved before returning
            if chat_db_context:
                flag_modified(chat_db_context, "context_data")
            return _finalize_response({
                "message_body": mensaje_para_escalar,
                "options_list": [],
                "message_type": "text",
                "fuente": "llm_derivar_humano_v2"
            })




    # --- Serializar y guardar contexto final ---
    contexto_municipio_serializado_para_db = serializar_enum(contexto_municipio_actual)
    estado_final_para_guardar_str = contexto_municipio_actual.get("estado_conversacion") # Debería ser string o None
    if isinstance(estado_final_para_guardar_str, ConversationState): # Por si acaso no se convirtió a string
        logger_actual.warning(f"Estado {estado_final_para_guardar_str} era Enum antes de serializar. Convirtiendo.")
        contexto_municipio_actual["estado_conversacion"] = estado_final_para_guardar_str.name
    elif estado_final_para_guardar_str is None:
        contexto_municipio_actual.pop("estado_conversacion", None)

    if chat_db_context:
        # Explicitly re-assign the dictionary to ensure SQLAlchemy detects the change.
        # This is a more robust way to handle mutable JSONB fields.
        chat_db_context.context_data = chat_db_context_live_data
        flag_modified(chat_db_context, "context_data")
        logger_actual.info(f"[CONTEXT_SAVE_FINAL] Final context data being flagged for save: {chat_db_context.context_data}")


    # --- Fallback logic ---
    logger_actual.info(f"LLM no manejó la respuesta. Intentando fallback con Google Search.")
    search_results = google_search(pregunta_str)
    if search_results:
        search_items = []
        for result in search_results[:3]:
            search_items.append(f"- [{result.get('title')}]({result.get('link')})\n{result.get('snippet')}")

        final_response_dict = {
            "message_body": "No estoy seguro de cómo ayudarte con eso, pero encontré esto en la web:\n\n" + "\n\n".join(search_items),
            "options_list": [],
            "message_type": "text",
            "fuente": "municipio_fallback_google_search"
        }
    else:
        final_response_dict = {
            "message_body": "Lo siento, no pude entender tu consulta. ¿Podrías intentar reformularla?",
            "options_list": [],
            "message_type": "text",
            "fuente": "fallback_final"
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

    logger_actual.info(f"[RESPONDER_MUNICIPIO_END_V4] Respuesta: '{final_response_dict.get('message_body', '')[:100]}...', Fuente: {final_response_dict.get('fuente', 'N/A')}")
    return _finalize_response(final_response_dict)
