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
    construir_respuesta_sugerir_registro
)
from .llm_utils import extract_complaint_details_llm, extract_multiple_contact_details_llm
import math
from services.tasks import process_image_for_chat_task

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
    ESPERANDO_CONFIRMACION_INICIAR_RECLAMO = auto()
    ESPERANDO_CREACION_TICKET = auto()
    ESPERANDO_CONFIRMACION_UBICACION = auto()
    ESPERANDO_CONSULTA_GENERAL = auto()
    ESPERANDO_SELECCION_MENU_PRINCIPAL = auto()
    ESPERANDO_SELECCION_MENU_RECLAMOS = auto()
    ESPERANDO_UBICACION_GENERAL = auto()

# Palabras clave sencillas para detectar consultas generales de servicios
GENERAL_QUERY_KEYWORDS = [
    "veterinaria", "veterinarias", "farmacia", "supermercado", "negocio",
    "servicio", "buscar", "comercio", "local"
]

# --- Paged Menu Content and Mappings (as per user spec) ---
MENU_CONTENT = {
    "main": {
        1: "Municipalidad de Junín — Menú principal (pág 1)\n1. Iniciar un Reclamo\n2. Realizar una Denuncia\n3. Licencia de Conducir\n4. Pagar Tasas\n5. Consultar otros trámites\n6. Veterinaria y Bromatología\n\nEscribí el número o una palabra clave. Botones: Ver opciones / Siguiente / Agente",
        2: "Municipalidad de Junín — Menú principal (pág 2)\n7. Solicitar Turnos\n8. Agenda Cultural y Turística\n9. Últimas Novedades\n10. Defensa del Consumidor\n\nEscribí el número o una palabra clave. Para volver a la pág 1, tocá Ver opciones."
    },
    "reclamos": {
        1: "Seleccioná el tipo de reclamo (podés adjuntar foto o ubicación):\n1. 💡 Luminaria\n2. 🌳 Arbolado\n3. 🧹 Limpieza y riego\n4. 🚧 Arreglo de calle\n5. 💧 Pérdida de agua\n6. 📋 Otros"
    }
}

MENU_MAPPING = {
    "main": {
        "1": "mostrar_menu_reclamos",
        "2": "denuncias",
        "3": "licencia_de_conducir",
        "4": "pago_de_tasas_vigentes",
        "5": "consultar_otros_tramites",
        "6": "veterinaria_y_bromatologia",
        "7": "solicitar_turnos",
        "8": "agenda_cultural_y_turistica",
        "9": "ultimas_novedades",
        "10": "defensa_del_consumidor"
    },
    "reclamos": {
        "1": "reclamo_luminaria",
        "2": "reclamo_arbolado",
        "3": "reclamo_limpieza_riego",
        "4": "reclamo_arreglo_calle",
        "5": "reclamo_perdida_agua",
        "6": "reclamo_otros"
    }
}

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


class BaseMunicipioHandler:
    def __init__(self, context):
        self.context = context

    def handle(self, payload: dict) -> dict | None:
        raise NotImplementedError

from services.google_search import google_search

class GreetingHandler(BaseMunicipioHandler):
    def handle(self, payload: dict) -> dict | None:
        # This handler is now simplified. It resets the context and prepares the response
        # for the first page of the main menu. The actual sending is done by the caller.
        contexto_municipio_actual = self.context.get(CONTEXTO_MUNICIPIO, {})

        # Define keys to clear for a full conversation reset
        keys_to_clear = [
            "historial_llm_reclamo", "datos_parciales_llm_reclamo", "esperando_info_llm_reclamo",
            "estado_conversacion", "accion_pendiente_post_login", "estado_conversacion_pre_login",
            "last_search", "last_search_page", "mensaje_previo_llm_para_escalamiento",
            "current_menu", "menu_page" # Also clear menu state
        ]

        for key in keys_to_clear:
            contexto_municipio_actual.pop(key, None)

        # Also clear the general LLM history from the main context
        if self.context.get("chat_db_context_data"):
            self.context["chat_db_context_data"].pop("historial_conversacion_general_llm", None)

        logger.info("[GreetingHandler] Conversation context has been reset.")

        # Set the state to main menu navigation
        contexto_municipio_actual['estado_conversacion'] = ConversationState.ESPERANDO_SELECCION_MENU_PRINCIPAL.name
        contexto_municipio_actual['current_menu'] = 'main'
        contexto_municipio_actual['menu_page'] = 1

        logger.info(f"[GreetingHandler] Set estado_conversacion to {contexto_municipio_actual['estado_conversacion']}")

        # The message body is now just the content for the template's {{1}} variable
        message_body = MENU_CONTENT["main"][1]

        return {
            "message_body": message_body,
            "fuente": "greeting_paged_menu_v1"
            # No options_list or message_type needed, as the template has fixed buttons.
        }

class NewsHandler(BaseMunicipioHandler):
    def handle(self, payload: dict) -> dict | None:
        municipio_config = self.context.get('municipio_config_actual', {})
        municipio_name = municipio_config.get('nombre_display', 'del municipio')
        municipio_website = municipio_config.get('website')

        if municipio_website:
            query = f"site:{municipio_website} noticias de {municipio_name}"
        else:
            query = f"noticias de {municipio_name}"

        search_results = google_search(query, days=1)

        if not search_results:
            return {
                "message_body": "No se encontraron noticias recientes.",
                "options_list": [],
                "message_type": "text",
                "fuente": "news_handler_no_results"
            }

        options = []
        for result in search_results[:5]:
            options.append({
                "id": f"news_{result.get('link')}",
                "texto": result.get('title'),
                "url": result.get('link'),
                "type": "url"
            })

        return {
            "message_body": "Aquí están las últimas noticias:",
            "options_list": options,
            "message_type": "interactive_list",
            "fuente": "news_handler_with_results"
        }

class PointsOfInterestHandler(BaseMunicipioHandler):
    def handle(self, payload: dict) -> dict | None:
        query = payload.get("pregunta", "")
        location = payload.get("location")

        if not query:
            return {
                "message_body": "Por favor, decime qué punto de interés estás buscando.",
                "options_list": [],
                "message_type": "text",
                "fuente": "poi_handler_no_query"
            }

        if location:
            search_query = f"{query} cerca de {location}"
        else:
            search_query = f"{query} en {self.context.get('municipio_config_actual', {}).get('nombre_display', 'el municipio')}"

        search_results = google_search(search_query)

        if not search_results:
            return {
                "message_body": f"No se encontraron resultados para '{query}'.",
                "options_list": [],
                "message_type": "text",
                "fuente": "poi_handler_no_results"
            }

        poi_items = []
        for result in search_results[:3]:
            poi_items.append(f"- {result.get('title')}\n{result.get('snippet')}\n[Ver más]({result.get('link')})")

        return {
            "message_body": f"Aquí hay algunos resultados para '{query}':\n" + "\n\n".join(poi_items),
            "options_list": [],
            "message_type": "text",
            "fuente": "poi_handler_with_results"
        }

def handle_main_menu_action(action_id: str) -> dict:
    """
    Handles actions from the new categorized main menu.
    """
    if action_id == "mostrar_menu_reclamos":
        return _get_reclamos_menu()

    action_responses = {
        "licencia_de_conducir": "Para requisitos y turnos de licencia de conducir visitá:\nhttps://www.juninmendoza.gov.ar/licencia-de-conducir-junin/",
        "pago_tasas_vigentes": "Para pagar o descargar boletos vigentes, dirigite a:\nhttps://epagos.juninmendoza.gov.ar/jrentas/",
        "defensa_del_consumidor": "Para asesoramiento, escribí a:\ndefensadelconsumidorjuninmza@gmail.com",
        "veterinaria_y_bromatologia": "Para información vinculada a veterinaria y bromatología municipal escribí al WhatsApp:\n+54 9 2634 52-1563",
    }

    if action_id in action_responses:
        return {
            "message_body": action_responses[action_id],
            "options_list": [],
            "message_type": "text",
            "fuente": f"info_request_{action_id}"
        }

    # Placeholder for actions without a defined response yet
    if action_id == "ultimas_novedades":
        return NewsHandler(context={}).handle({})

    unimplemented_actions = [
        "consultar_otros_tramites", "denuncias", "solicitar_turnos",
        "agenda_cultural_y_turistica"
    ]
    if action_id in unimplemented_actions:
        return {
            "message_body": "Esta función aún no está implementada.",
            "options_list": [],
            "message_type": "text",
            "fuente": f"unimplemented_{action_id}"
        }

    return None # Return None if the action is not recognized by this handler


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

RECLAMO_STATES = [ConversationState.ESPERANDO_CATEGORIA_RECLAMO, ConversationState.ESPERANDO_DIRECCION_RECLAMO, ConversationState.ESPERANDO_NOMBRE_VECINO, ConversationState.ESPERANDO_TELEFONO_VECINO, ConversationState.ESPERANDO_EMAIL_VECINO, ConversationState.ESPERANDO_DESCRIPCION_RECLAMO, ConversationState.ESPERANDO_ADJUNTOS_RECLAMO, ConversationState.ESPERANDO_CONFIRMACION_RECLAMO]

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
    Handles the ticket creation process.
    """
    # Combina los datos parciales con los nuevos datos recibidos
    datos_reclamo = contexto_municipio_actual.get("datos_parciales_llm_reclamo", {})
    datos_reclamo.update(datos_estructura_llm)

    # Llama a la acción para crear el reclamo
    respuesta_accion = accion_crear_reclamo_municipio(datos_reclamo, context)

    # Limpia el contexto del reclamo en el municipio, preservando datos del usuario
    keys_to_clear_after_claim = [
        "historial_llm_reclamo", "datos_parciales_llm_reclamo", "esperando_info_llm_reclamo",
        "estado_conversacion", "categoria_reclamo", "descripcion_reclamo", "direccion_reclamo",
        "coordenadas_reclamo", "foto_url", "mensaje_previo_llm_para_escalamiento"
    ]
    for key in keys_to_clear_after_claim:
        contexto_municipio_actual.pop(key, None)

    # Si la creación del ticket fue exitosa, prepara una respuesta de confirmación
    if respuesta_accion and respuesta_accion.get("success"):
        logger.info(f"Ticket creado con ID: {respuesta_accion.get('data', {}).get('ticket_id')}")
        # Usar el mensaje ya formateado por el ActionHandler
        message_body = respuesta_accion.get("message_to_user")

        # Opcional: añadir botones si el handler no los proveyó
        options_list = respuesta_accion.get("options_list", [])
        logger.info(f"Options list from action handler: {options_list}")
        if not options_list:
            logger.info("Action handler did not provide options, adding default buttons.")
            options_list.extend([
                {"texto": "Ver estado de mi reclamo", "id_accion": "consultar_estado_ticket"},
                {"texto": "Hacer otro reclamo", "id_accion": "iniciar_reclamo"}
            ])

        # Create a combined message
        final_message = f"{message_body}\n\n¿Hay algo más en lo que pueda ayudarte?"

        return {
            "message_body": final_message,
            "options_list": options_list,
            "message_type": "interactive_buttons",
            "fuente": "ticket_creado_v2"
        }, contexto_municipio_actual
    else:
        # En caso de fallo, simplemente devuelve la respuesta de error del handler.
        return respuesta_accion, contexto_municipio_actual


def handle_llm_interaction(pregunta_str, context, viewer_user, owner_user, chat_db_context, contexto_municipio_actual):
    logger_actual = current_app.logger if has_app_context() else logging.getLogger(__name__)

    logger_actual.info(
        f"[HANDLE_LLM_START] pregunta='{pregunta_str}' estado_previo='{contexto_municipio_actual.get('estado_conversacion')}' ubicacion='{contexto_municipio_actual.get('datos_parciales_llm_reclamo', {}).get('ubicacion')}'"
    )

    estado_conversacion_para_llm = contexto_municipio_actual.get("estado_conversacion")
    invocar_llm = False

    # Si se está esperando info de un reclamo pero el usuario consulta un servicio
    if (
        estado_conversacion_para_llm == ConversationState.ESPERANDO_INFO_RECLAMO_LLM.name
        and es_consulta_general(pregunta_str)
    ):
        logger_actual.info(
            "[HANDLE_LLM] Cambio de tema detectado durante flujo de reclamo. Reseteando contexto a conversacion general."
        )
        # Clear all claim-related context
        contexto_municipio_actual["historial_llm_reclamo"] = []
        contexto_municipio_actual["datos_parciales_llm_reclamo"] = {}
        contexto_municipio_actual.pop("esperando_info_llm_reclamo", None)
        contexto_municipio_actual.pop("esperando_info_llm", None)
        # Limpia datos residuales del reclamo previo
        for campo in [
            "categoria_reclamo",
            "descripcion_reclamo",
            "direccion_reclamo",
            "coordenadas_reclamo",
            "nombre_vecino",
            "telefono_vecino",
            "email_vecino",
            "foto_url",
        ]:
            contexto_municipio_actual.pop(campo, None)

        contexto_municipio_actual["estado_conversacion"] = ConversationState.CONVERSACION_GENERAL_LLM.name
        estado_conversacion_para_llm = ConversationState.CONVERSACION_GENERAL_LLM.name

    if estado_conversacion_para_llm in [ConversationState.ESPERANDO_INFO_RECLAMO_LLM.name, ConversationState.CONVERSACION_GENERAL_LLM.name]:
        invocar_llm = True
    elif not estado_conversacion_para_llm or contexto_municipio_actual.get("saludo_detectado_en_largo_mensaje"):
        if len(pregunta_str.strip().split()) > 1 or (context.get("es_foto") and not pregunta_str.strip()):
            invocar_llm = True

    if invocar_llm:
        logger.info(f"[HANDLE_LLM] Invocando LLM. Estado: {estado_conversacion_para_llm}")

    datos_reclamo = contexto_municipio_actual.get("datos_parciales_llm_reclamo", {})
    usuario_info_llm = {
        "nombre": datos_reclamo.get("nombre_usuario_detectado") or getattr(viewer_user, "nombre", "Vecino/a") if viewer_user else "Vecino/a",
        "tipo_entidad": "municipio",
        "ubicacion": datos_reclamo.get("ubicacion") or getattr(viewer_user, "direccion", None) if viewer_user else None,
        "contacto": {
            "telefono": datos_reclamo.get("telefono_detectado") or getattr(viewer_user, "telefono", None) if viewer_user else None,
            "email": datos_reclamo.get("email_detectado") or getattr(viewer_user, "email", None) if viewer_user else None
        },
        "datos_reclamo_actuales": datos_reclamo
    }

    historial_para_llm = []
    if estado_conversacion_para_llm == ConversationState.ESPERANDO_INFO_RECLAMO_LLM.name:
        historial_para_llm = contexto_municipio_actual.get("historial_llm_reclamo", [])
    else:
        historial_para_llm = contexto_municipio_actual.get("historial_conversacion_general_llm", [])

    try:
        mensaje_completo_para_llm = {"texto": pregunta_str}
        if context.get("es_foto") and context.get("foto_url"):
            mensaje_completo_para_llm["imagen_url"] = context.get("foto_url")
            if contexto_municipio_actual.get("analisis_imagen_reclamo_auto_raw"):
                analisis_previo = contexto_municipio_actual.get("analisis_imagen_reclamo_auto_raw")
                if isinstance(analisis_previo, dict):
                    resumen_analisis = {k: analisis_previo.get(k) for k in ["categoria_sugerida", "descripcion_sugerida", "texto_ocr"] if analisis_previo.get(k)}
                    if resumen_analisis:
                        mensaje_completo_para_llm["analisis_previo_imagen"] = resumen_analisis

        try:
            mensaje_para_gemini = json.dumps(mensaje_completo_para_llm)
            respuesta_llm_dict = llamar_gemini(mensaje_usuario=mensaje_para_gemini, usuario=usuario_info_llm, historial=historial_para_llm)
            logger.info(f"[HANDLE_LLM] Respuesta LLM: {respuesta_llm_dict}")
            logger_actual.info(f"[HANDLE_LLM] Accion backend LLM: {respuesta_llm_dict.get('accion_backend')}")
        except Exception as e:
            logger.error(f"[RESPONDER_MUNICIPIO_LLM_ERROR] Error general en la llamada a Gemini: {e}", exc_info=True)
            return {
                "message_body": "Error de configuración del servicio de IA (entorno). Por favor, contacta al administrador.",
                "options_list": [],
                "message_type": "text",
                "fuente": "error"
            }, contexto_municipio_actual

        respuesta_usuario_llm = respuesta_llm_dict.get("message_body")
        accion_backend_llm = respuesta_llm_dict.get("accion_backend")
        datos_estructura_llm = respuesta_llm_dict.get("datos_estructura")
        pedir_info_llm = respuesta_llm_dict.get("pedir_info")
        botones_llm = respuesta_llm_dict.get("botones", [])

        if not respuesta_usuario_llm and accion_backend_llm not in ["crear_reclamo"]:
             logger_actual.warning("[HANDLE_LLM] LLM response did not contain a 'message_body' and was not a parameterless action. Returning None to trigger fallback.")
             return None, contexto_municipio_actual

        nuevo_turno_historial = {"pregunta_usuario": pregunta_str, "respuesta_ia": respuesta_usuario_llm}

        if accion_backend_llm == "saludar":
            logger.info("LLM detectó un saludo. Invocando GreetingHandler.")
            # The 'context' dict passed to handle_llm_interaction has the necessary nested structure.
            handler = GreetingHandler(context)
            response = handler.handle({})  # Pass empty payload
            if chat_db_context:
                flag_modified(chat_db_context, "context_data")
            # The handler's response is the full dict ready to be returned by responder_municipio
            return response, contexto_municipio_actual

        if accion_backend_llm in ["crear_reclamo", "iniciar_reclamo"] and datos_estructura_llm and datos_estructura_llm.get("target") == "municipio":
            # Si es el inicio de un nuevo reclamo, limpiar el contexto anterior
            if contexto_municipio_actual.get("estado_conversacion") != ConversationState.ESPERANDO_INFO_RECLAMO_LLM.name:
                contexto_municipio_actual["datos_parciales_llm_reclamo"] = {}
                contexto_municipio_actual["historial_llm_reclamo"] = []

            # Asegurarse de que datos_parciales_llm_reclamo exista si no fue creado arriba
            contexto_municipio_actual.setdefault("datos_parciales_llm_reclamo", {})

            contexto_municipio_actual.setdefault("historial_llm_reclamo", []).append(nuevo_turno_historial)

            # Actualizar con los nuevos datos, priorizando los que no son None
            if not isinstance(contexto_municipio_actual.get("datos_parciales_llm_reclamo"), dict):
                contexto_municipio_actual["datos_parciales_llm_reclamo"] = {}

            # Combinar datos antiguos y nuevos
            datos_actuales = contexto_municipio_actual.get("datos_parciales_llm_reclamo", {})
            nuevos_datos = {k: v for k, v in datos_estructura_llm.items() if v is not None}
            datos_actuales.update(nuevos_datos)
            contexto_municipio_actual["datos_parciales_llm_reclamo"] = datos_actuales

            if not pedir_info_llm:
                # _handle_ticket_creation returns the tuple (response, context), which is what this function should return.
                return _handle_ticket_creation(contexto_municipio_actual, context, datos_actuales)
            else:
                contexto_municipio_actual["estado_conversacion"] = ConversationState.ESPERANDO_INFO_RECLAMO_LLM.name
                contexto_municipio_actual["esperando_info_llm_reclamo"] = pedir_info_llm
                # Update the context that will be passed to the next turn
                if chat_db_context and hasattr(chat_db_context, 'context_data'):
                    chat_db_context.context_data[CONTEXTO_MUNICIPIO] = contexto_municipio_actual
                    flag_modified(chat_db_context, "context_data")
                return {"message_body": respuesta_usuario_llm, "options_list": botones_llm, "message_type": "interactive_buttons" if botones_llm else "text", "fuente": "llm_pide_info_reclamo"}, contexto_municipio_actual
        elif accion_backend_llm == "mostrar_menu_reclamos":
            logger.info("[HANDLE_LLM] LLM solicitó mostrar el menú de reclamos.")
            # La función _get_reclamos_menu ya devuelve el diccionario de respuesta formateado.
            # Lo devolvemos junto con el contexto actual.
            return _get_reclamos_menu(), contexto_municipio_actual
        elif accion_backend_llm == "derivar_humano":
            context["intencion"] = "hablar_con_agente"
            contexto_municipio_actual["mensaje_previo_llm_para_escalamiento"] = respuesta_usuario_llm
            logger.info("[HANDLE_LLM] LLM derivó a humano.")
            return None, contexto_municipio_actual
        elif accion_backend_llm == "finalizar_tramite":
            logger.info(
                "[HANDLE_LLM] LLM finalizó el trámite. Reseteando contexto de reclamo."
            )
            # Clear all claim-related context
            contexto_municipio_actual["historial_llm_reclamo"] = []
            contexto_municipio_actual["datos_parciales_llm_reclamo"] = {}
            contexto_municipio_actual.pop("esperando_info_llm_reclamo", None)
            contexto_municipio_actual.pop("esperando_info_llm", None)
            contexto_municipio_actual["estado_conversacion"] = ConversationState.CONVERSACION_GENERAL_LLM.name

            # Also add the final user-facing message to the general history
            contexto_municipio_actual.setdefault("historial_conversacion_general_llm", []).append(nuevo_turno_historial)

            return {
                "message_body": respuesta_usuario_llm,
                "options_list": botones_llm,
                "message_type": "interactive_buttons" if botones_llm else "text",
                "fuente": "llm_finalizar_tramite"
            }, contexto_municipio_actual
        elif accion_backend_llm == "ejecutar_herramienta":
            nombre_herramienta = datos_estructura_llm.get("nombre_herramienta")
            parametros_herramienta = datos_estructura_llm.get("parametros_herramienta", {})

            # Unificado: chequear si el LLM está pidiendo más información.
            info_faltante = pedir_info_llm or datos_estructura_llm.get("faltan_parametros_herramienta")

            if nombre_herramienta and nombre_herramienta in TOOL_REGISTRY:
                # Si se necesita más información, no ejecutar la herramienta.
                # Simplemente preguntar al usuario y guardar el estado para el próximo turno.
                if info_faltante:
                    logger.info(f"[HERRAMIENTA] LLM pide más información ('{info_faltante}') antes de ejecutar '{nombre_herramienta}'. No se ejecutará la herramienta.")
                    contexto_municipio_actual["estado_conversacion"] = ConversationState.ESPERANDO_INFO_RECLAMO_LLM.name
                    contexto_municipio_actual["esperando_info_llm_reclamo"] = info_faltante[0] if isinstance(info_faltante, list) else info_faltante
                    contexto_municipio_actual["datos_parciales_llm_reclamo"] = {
                        "nombre_herramienta": nombre_herramienta,
                        "parametros_herramienta": parametros_herramienta
                    }
                    # Devolver la pregunta del LLM al usuario
                    return {"message_body": respuesta_usuario_llm, "options_list": botones_llm, "message_type": "interactive_buttons" if botones_llm else "text", "fuente": "llm_pide_info_herramienta"}, contexto_municipio_actual

                # Si no falta información, proceder a ejecutar la herramienta.
                herramienta = TOOL_REGISTRY[nombre_herramienta]
                funcion_herramienta = herramienta["funcion"]

                try:
                    logger.info(f"[HERRAMIENTA] Intentando ejecutar: {nombre_herramienta} con params: {parametros_herramienta}")
                    resultado_herramienta = funcion_herramienta(**parametros_herramienta)
                    logger.info(f"[HERRAMIENTA] Resultado de {nombre_herramienta}: {str(resultado_herramienta)[:200]}...")

                    # Combinar la respuesta del LLM con el resultado de la herramienta
                    respuesta_final = f"{respuesta_usuario_llm}\n\n{resultado_herramienta}"

                    contexto_municipio_actual.setdefault("historial_conversacion_general_llm", []).append({
                        "pregunta_usuario": pregunta_str,
                        "respuesta_ia": respuesta_final
                    })

                    return {
                        "message_body": respuesta_final,
                        "options_list": botones_llm,
                        "message_type": "text",
                        "fuente": f"herramienta_{nombre_herramienta}"
                    }, contexto_municipio_actual

                except Exception as e:
                    logger.error(f"Error ejecutando la herramienta '{nombre_herramienta}': {e}", exc_info=True)
                    return {
                        "message_body": "Hubo un error al intentar usar la herramienta. Por favor, intenta de nuevo.",
                        "options_list": [],
                        "message_type": "text",
                        "fuente": "error_herramienta"
                    }, contexto_municipio_actual
            else:
                # Este caso se da si el LLM pide ejecutar una herramienta que no existe en TOOL_REGISTRY
                logger.warning(f"Se intentó ejecutar una herramienta no registrada: '{nombre_herramienta}'")
                return {
                    "message_body": "No se encontró la herramienta solicitada. Por favor, reformula tu pregunta.",
                    "options_list": [],
                    "message_type": "text",
                    "fuente": "herramienta_no_encontrada"
                }, contexto_municipio_actual

        elif accion_backend_llm == "derivar_humano":
            context["intencion"] = "hablar_con_agente"
            contexto_municipio_actual["mensaje_previo_llm_para_escalamiento"] = respuesta_usuario_llm
            logger.info("[HANDLE_LLM] LLM derivó a humano.")
            return None, contexto_municipio_actual

        elif accion_backend_llm == "responder_directamente":
            logger.info("[HANDLE_LLM] LLM solicitó responder directamente.")
            contexto_municipio_actual.setdefault("historial_conversacion_general_llm", []).append(nuevo_turno_historial)
            contexto_municipio_actual["estado_conversacion"] = ConversationState.CONVERSACION_GENERAL_LLM.name

            # Devolvemos un diccionario que se asemeja más a la respuesta original del LLM
            # para que el frontend pueda procesarlo directamente.
            return {
                "message_body": respuesta_usuario_llm,
                "botones": botones_llm,
                "accion_backend": accion_backend_llm,
                "datos_estructura": datos_estructura_llm,
                "pedir_info": pedir_info_llm,
                "fuente": "llm_respuesta_directa"
            }, contexto_municipio_actual

        else: # Respuesta general o continuación de un flujo
            # Si estábamos esperando info para un reclamo y el LLM no generó una acción concreta
            # pero sí extrajo datos, los fusionamos con los datos parciales.
            if estado_conversacion_para_llm == ConversationState.ESPERANDO_INFO_RECLAMO_LLM.name and datos_estructura_llm:
                contexto_municipio_actual.setdefault("historial_llm_reclamo", []).append(nuevo_turno_historial)

                # Fusionar datos nuevos con los existentes
                datos_actuales = contexto_municipio_actual.get("datos_parciales_llm_reclamo", {})
                nuevos_datos = {k: v for k, v in datos_estructura_llm.items() if v is not None}
                datos_actuales.update(nuevos_datos)
                contexto_municipio_actual["datos_parciales_llm_reclamo"] = datos_actuales

                # El LLM puede pedir más info o haber terminado de recopilar
                if pedir_info_llm:
                    contexto_municipio_actual["estado_conversacion"] = ConversationState.ESPERANDO_INFO_RECLAMO_LLM.name
                    contexto_municipio_actual["esperando_info_llm_reclamo"] = pedir_info_llm
                else:
                    # Si no pide más info, podría ser momento de confirmar o crear el reclamo
                    # (Esta lógica podría necesitar más refinamiento, pero por ahora es una respuesta general)
                    contexto_municipio_actual["estado_conversacion"] = ConversationState.CONVERSACION_GENERAL_LLM.name
                    contexto_municipio_actual.pop("esperando_info_llm_reclamo", None)

            else: # Conversación general que no es parte de un flujo de reclamo activo
                contexto_municipio_actual.setdefault("historial_conversacion_general_llm", []).append(nuevo_turno_historial)
                if pedir_info_llm:
                    # Esto podría iniciar un nuevo flujo si el LLM lo decide
                    contexto_municipio_actual["estado_conversacion"] = ConversationState.ESPERANDO_INFO_RECLAMO_LLM.name
                    contexto_municipio_actual["esperando_info_llm_reclamo"] = pedir_info_llm
                else:
                    contexto_municipio_actual["estado_conversacion"] = ConversationState.CONVERSACION_GENERAL_LLM.name
                    contexto_municipio_actual.pop("esperando_info_llm_reclamo", None) # Limpiar por si acaso

            return {"message_body": respuesta_usuario_llm, "options_list": botones_llm, "message_type": "interactive_buttons" if botones_llm else "text", "fuente": "llm_respuesta_general_v2"}, contexto_municipio_actual

    except Exception as e_llm:
        logger.error(f"[HANDLE_LLM] Error: {e_llm}", exc_info=True)
        for k in ["historial_llm_reclamo", "datos_parciales_llm_reclamo", "esperando_info_llm_reclamo", "historial_conversacion_general_llm", "estado_conversacion"]:
            if k == "estado_conversacion" and contexto_municipio_actual.get(k) in [ConversationState.ESPERANDO_INFO_RECLAMO_LLM.name, ConversationState.CONVERSACION_GENERAL_LLM.name]:
                contexto_municipio_actual[k] = None
            elif k != "estado_conversacion":
                contexto_municipio_actual.pop(k, None)
        return None, contexto_municipio_actual

MENU_KEYWORDS = {
    "mostrar_menu_reclamos": ["reclamo", "reclamos", "iniciar", "problema"],
    "denuncias": ["denuncia", "denuncias"],
    "licencia_de_conducir": ["licencia", "conducir", "licencias", "carnet"],
    "pago_de_tasas_vigentes": ["pagar", "pago", "tasas", "tasa", "boleta", "boletas"],
    "consultar_otros_tramites": ["consultar", "consulta", "tramites", "tramite", "otros"],
    "veterinaria_y_bromatologia": ["veterinaria", "animales", "perro", "gato", "mascotas", "bromatologia"],
    "solicitar_turnos": ["turnos", "turno", "solicitar"],
    "agenda_cultural_y_turistica": ["agenda", "cultural", "turistica", "turismo", "eventos"],
    "ultimas_novedades": ["novedades", "noticias", "ultimas"],
    "defensa_del_consumidor": ["consumidor", "defensa"]
}

def find_menu_action_by_input(user_input: str, menu_buttons: list) -> str | None:
    """
    Finds a menu action based on user input, checking for number, first letter, or keywords.
    """
    if not user_input or not menu_buttons:
        return None

    normalized_input = normalizar_texto(user_input.strip())

    # 1. Check for numeric selection
    try:
        selection_index = int(normalized_input) - 1
        if 0 <= selection_index < len(menu_buttons):
            return menu_buttons[selection_index].get('action_id')
    except (ValueError, IndexError):
        pass  # Not a valid number or index, proceed to other checks

    # 2. Check for first letter match (only if input is a single character)
    if len(normalized_input) == 1:
        for button in menu_buttons:
            button_text_norm = normalizar_texto(button.get("texto", ""))
            if button_text_norm.startswith(normalized_input):
                return button.get("action_id")

    # 3. Check for keyword match
    for action_id, keywords in MENU_KEYWORDS.items():
        for keyword in keywords:
            if keyword in normalized_input:
                # Ensure this action_id is actually in the current menu to avoid ambiguity
                if any(btn.get('action_id') == action_id for btn in menu_buttons):
                    return action_id

    return None

RECLAMO_KEYWORDS = {
    "Luminaria": ["luminaria", "luz", "poste", "foco"],
    "Arbolado": ["arbolado", "arbol", "arboles", "rama", "ramas"],
    "Limpieza y riego": ["limpieza", "riego", "basura", "basural", "contenedor"],
    "Arreglo de calle": ["calle", "bache", "pozo", "asfalto", "vereda"],
    "Pérdida de agua": ["agua", "perdida", "caño", "cañeria"],
    "Otros": ["otros", "otro", "varios"]
}

def find_reclamo_category_by_input(user_input: str, reclamo_options: list) -> str | None:
    """
    Finds a reclamo category based on user input, checking for number, first letter, or keywords.
    """
    if not user_input or not reclamo_options:
        return None

    normalized_input = normalizar_texto(user_input.strip())

    # 1. Check for numeric selection
    try:
        selection_index = int(normalized_input) - 1
        if 0 <= selection_index < len(reclamo_options):
            return reclamo_options[selection_index].get('texto')
    except (ValueError, IndexError):
        pass

    # 2. Check for first letter match
    if len(normalized_input) == 1:
        for option in reclamo_options:
            if normalizar_texto(option.get("texto", "")).startswith(normalized_input):
                return option.get("texto")

    # 3. Check for keyword match
    for category, keywords in RECLAMO_KEYWORDS.items():
        for keyword in keywords:
            if keyword in normalized_input:
                return category

    return None

def _get_reclamos_menu():
    """Devuelve la estructura del menú de reclamos estandarizado."""
    return {
        "message_body": MENU_CONTENT["reclamos"][1],
        "fuente": "submenu_reclamos_estandar_v2"
    }


SIMPLE_GREETINGS = {"hola", "buenos dias", "buenas tardes", "buenas noches", "hey", "hi", "hello", "menu", "menú"}

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
    logger_actual.info(f"[RESPONDER_MUNICIPIO_START_V3] ==================================================")
    logger_actual.info(f"[RESPONDER_MUNICIPIO_START_V3] Pregunta: '{pregunta_original}', User: {getattr(viewer_user, 'id', anon_id)}, Channel: {channel}")

    contexto_municipio_actual = chat_db_context.context_data.setdefault(CONTEXTO_MUNICIPIO, {})

    # This context object is passed to handlers
    context = {
        CONTEXTO_MUNICIPIO: contexto_municipio_actual,
        "user_obj": owner_user,
        "viewer_user_obj": viewer_user,
        "cliente_id": getattr(viewer_user, "id", None),
        "anon_id": anon_id,
        "rubro_obj": rubro_obj,
        "channel": channel,
        "municipio_config_actual": CONFIG_MUNICIPIO,
        "chat_session_uuid": kwargs.get("chat_session_uuid"),
        "chat_db_context_data": chat_db_context.context_data,
        "pregunta_actual_usuario": pregunta_original,
    }

    pregunta_str = ""
    if isinstance(pregunta_original, dict):
        pregunta_str = pregunta_original.get("pregunta", "")
    elif isinstance(pregunta_original, str):
        pregunta_str = pregunta_original

    normalized_input = normalizar_texto(pregunta_str)

    # --- Menu Navigation Logic ---
    # Always check for menu commands first
    if normalized_input in SIMPLE_GREETINGS or normalized_input == "ver opciones":
        handler = GreetingHandler(context)
        return handler.handle({})

    if normalized_input == "agente":
        from .actions.municipio_actions import DerivarHumanoActionHandler
        return DerivarHumanoActionHandler(context=context).execute({})

    # Check for menu state
    estado_conversacion = contexto_municipio_actual.get("estado_conversacion")
    if estado_conversacion == ConversationState.ESPERANDO_SELECCION_MENU_PRINCIPAL.name or \
       estado_conversacion == ConversationState.ESPERANDO_SELECCION_MENU_RECLAMOS.name:

        current_menu = contexto_municipio_actual.get("current_menu", "main")
        current_page = contexto_municipio_actual.get("menu_page", 1)

        if normalized_input == "siguiente":
            next_page = current_page + 1
            if next_page in MENU_CONTENT.get(current_menu, {}):
                contexto_municipio_actual["menu_page"] = next_page
                return {"message_body": MENU_CONTENT[current_menu][next_page], "fuente": f"menu_nav_siguiente_p{next_page}"}
            else:
                contexto_municipio_actual["menu_page"] = 1
                return {"message_body": MENU_CONTENT[current_menu][1], "fuente": "menu_nav_siguiente_fin"}

        elif normalized_input.isdigit():
            action_id = MENU_MAPPING.get(current_menu, {}).get(normalized_input)
            if not action_id:
                # Fallback to LLM if number is not in the current menu mapping
                logger_actual.warning(f"Input '{normalized_input}' is not a valid option in menu '{current_menu}'. Passing to LLM.")
                pass
            elif action_id == "mostrar_menu_reclamos":
                contexto_municipio_actual.update({"current_menu": "reclamos", "menu_page": 1, "estado_conversacion": ConversationState.ESPERANDO_SELECCION_MENU_RECLAMOS.name})
                return _get_reclamos_menu()
            else:
                action_response = handle_main_menu_action(action_id)
                contexto_municipio_actual.clear()
                handler = GreetingHandler(context)
                greeting_response = handler.handle({})
                greeting_response["message_body"] = f"{action_response['message_body']}\n\n{MENU_CONTENT['main'][1]}"
                return greeting_response

    # Fallback to LLM for any other input
    logger_actual.info(f"Input '{pregunta_str}' no es un comando de menú, derivando a LLM.")
    return handle_llm_interaction(pregunta_str, context, viewer_user, owner_user, chat_db_context, contexto_municipio_actual)
