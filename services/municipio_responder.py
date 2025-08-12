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
    ESPERANDO_SELECCION_DE_LISTA = auto()
    ESPERANDO_UBICACION_GENERAL = auto()

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


class BaseMunicipioHandler:
    def __init__(self, context):
        self.context = context

    def handle(self, payload: dict) -> dict | None:
        raise NotImplementedError

from services.google_search import google_search

class GreetingHandler(BaseMunicipioHandler):
    def handle(self, payload: dict) -> dict | None:
        # When a greeting is triggered, we perform a full reset of the conversation context.
        chat_db_context_data = self.context.get("chat_db_context_data")

        if not chat_db_context_data:
            logger.warning("[GreetingHandler] chat_db_context_data not found in context. Cannot perform a full reset.")
            contexto_municipio_actual = {}
        else:
            # Preserve essential user info if it exists from the old context
            user_info = chat_db_context_data.get(CONTEXTO_MUNICIPIO, {}).get('user', {})
            
            # Create a completely new, clean context dictionary
            contexto_municipio_nuevo = {}
            
            # Restore essential info if it existed
            if user_info:
                contexto_municipio_nuevo['user'] = user_info
            
            # Replace the old context dictionary with the new one
            chat_db_context_data[CONTEXTO_MUNICIPIO] = contexto_municipio_nuevo
            
            # Also clear any other top-level keys that should not persist across sessions
            chat_db_context_data.pop("historial_conversacion_general_llm", None)
            
            contexto_municipio_actual = contexto_municipio_nuevo

        logger.info("[GreetingHandler] Conversation context has been reset.")

        # Set the state to wait for a menu selection in the new context
        contexto_municipio_actual['estado_conversacion'] = ConversationState.ESPERANDO_SELECCION_MENU_PRINCIPAL.name
        logger.info(f"[GreetingHandler] Set estado_conversacion to {contexto_municipio_actual['estado_conversacion']}")

        viewer_user = self.context.get("viewer_user_obj")
        profile_name = self.context.get("profile_name")
        user_name = getattr(viewer_user, "nombre", None) or getattr(viewer_user, "name", None) or profile_name

        if user_name:
            welcome_message = (
                f"¡Hola, {user_name}! 👋 Soy JUNI, tu Asistente Virtual de la Municipalidad de Junín. "
                "Estoy aquí para ayudarte de una forma más inteligente. Podés consultarme sobre trámites, "
                "reclamos, turnos, noticias y mucho más.\n\n"
                "¿Cómo te puedo ayudar hoy? Elegí una opción o escribí una palabra clave:"
            )
        else:
            welcome_message = (
                "¡Hola! 👋 Soy JUNI, tu Asistente Virtual de la Municipalidad de Junín. "
                "Estoy aquí para ayudarte de una forma más inteligente. Podés consultarme sobre trámites, "
                "reclamos, turnos, noticias y mucho más. Para empezar, podés escribirme, enviarme un audio, "
                "una foto de un problema o compartir tu ubicación.\n\n"
                "¿Cómo te puedo ayudar hoy? Elegí una opción o respondé con el número, la primera letra o una palabra clave:"
            )

        categorias = [
            {"titulo": "Reclamos y Denuncias 🛠️", "botones": [
                {"texto": "🛠️ Iniciar un Reclamo", "action_id": "mostrar_menu_reclamos"},
                {"texto": "⚖️ Realizar una Denuncia", "action_id": "denuncias"}
            ]},
            {"titulo": "Trámites y Consultas 📄", "botones": [
                {"texto": "🚗 Licencia de Conducir", "action_id": "licencia_de_conducir"},
                {"texto": "💵 Pagar Tasas", "action_id": "pago_de_tasas_vigentes"},
                {"texto": "📋 Consultar otros trámites", "action_id": "consultar_otros_tramites"}
            ]},
            {"titulo": "Servicios y Turnos 📅", "botones": [
                {"texto": "🐾 Veterinaria y Bromatología", "action_id": "veterinaria_y_bromatologia"},
                {"texto": "📅 Solicitar Turnos", "action_id": "solicitar_turnos"}
            ]},
            {"titulo": "Información y Novedades 📰", "botones": [
                {"texto": "🎭 Agenda Cultural y Turística", "action_id": "agenda_cultural_y_turistica"},
                {"texto": "📰 Últimas Novedades", "action_id": "ultimas_novedades"},
                {"texto": "🛒 Defensa del Consumidor", "action_id": "defensa_del_consumidor"}
            ]}
        ]

        flat_buttons = []
        for categoria in categorias:
            for boton in categoria.get('botones', []):
                new_boton = boton.copy()
                new_boton['id'] = new_boton.get('action_id', new_boton['texto'])
                flat_buttons.append(new_boton)

        return {
            "message_body": welcome_message,
            "options_list": flat_buttons,
            "message_type": "interactive_list",
            "accion_backend": "responder_directamente",
            "fuente": "greeting_handler_universal_v5",
            "categorias": categorias, # Keep original structure for channels that might support it
            "generar_audio_bienvenida": True
        }

class NewsHandler(BaseMunicipioHandler):
    def handle(self, payload: dict) -> dict | None:
        contexto_municipio_actual = self.context.get(CONTEXTO_MUNICIPIO, {})
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
        
        # Set context for the next turn
        contexto_municipio_actual['estado_conversacion'] = ConversationState.ESPERANDO_SELECCION_DE_LISTA.name
        contexto_municipio_actual['opciones_en_pantalla'] = options


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

def handle_main_menu_action(action_id: str, context: dict) -> dict:
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
        return NewsHandler(context=context).handle({})
    
    if action_id == "consultar_otros_tramites":
        from .actions.municipio_actions import ConsultarInfoTramiteActionHandler
        return ConsultarInfoTramiteActionHandler(context).execute({})

    unimplemented_actions = [
        "denuncias", "solicitar_turnos",
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
        options_list = (respuesta_accion.get("options_list") or [])
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
            logger.error(
                f"[RESPONDER_MUNICIPIO_LLM_ERROR] Error general en la llamada a Gemini: {e}",
                exc_info=True,
            )
            return (
                {
                    "message_body": "Error de configuración del servicio de IA (entorno). Por favor, contacta al administrador.",
                    "options_list": [],
                    "message_type": "text",
                    "fuente": "error",
                },
                contexto_municipio_actual,
            )

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
            # Setear estado y menú para que el siguiente click se procese como selección
            contexto_municipio_actual["estado_conversacion"] = ConversationState.ESPERANDO_SELECCION_MENU_RECLAMOS.name
            contexto_municipio_actual["current_menu"] = "reclamos"
            contexto_municipio_actual["menu_page"] = 1
            if chat_db_context and hasattr(chat_db_context, "context_data"):
                chat_db_context.context_data[CONTEXTO_MUNICIPIO] = contexto_municipio_actual
                flag_modified(chat_db_context, "context_data")

            # Devolver menú con botones
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
                        "message_type": "interactive_buttons" if botones_llm else "text",
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
                "options_list": botones_llm,
                "message_type": "interactive_buttons" if botones_llm else "text",
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
    opciones = [
        {"texto": "1. 💡 Luminaria",        "id_accion": "1"},
        {"texto": "2. 🌳 Arbolado",         "id_accion": "2"},
        {"texto": "3. 🧹 Limpieza y riego", "id_accion": "3"},
        {"texto": "4. 🚧 Arreglo de calle", "id_accion": "4"},
        {"texto": "5. 💧 Pérdida de agua",  "id_accion": "5"},
        {"texto": "6. 📋 Otros",            "id_accion": "6"},
    ]
    return {
        "message_body": "Elegí una opción para tu reclamo:",
        "message_type": "interactive_buttons",
        "options_list": opciones,
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
    logger_actual.info(
        f"[RESPONDER_MUNICIPIO_START] =================================================="
    )
    logger_actual.info(
        f"[RESPONDER_MUNICIPIO_START] Pregunta: '{pregunta_original}', UserMunicipio: {getattr(owner_user, 'id', 'N/A')}, ViewerCiudadano: {getattr(viewer_user, 'id', 'N/A')}, Anon: {anon_id}, Channel: {channel}, ChatSessionUUID: {kwargs.get('chat_session_uuid')}"
    )

    # --- INICIO REFACTOR: Inicialización de 'context' y 'received_payload' al principio ---
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
    if normalizar_texto(pregunta_str) in SIMPLE_GREETINGS:
        logger_actual.info(f"Simple greeting '{pregunta_str}' detected. Bypassing LLM and showing main menu.")
        # Pass the full context to the handler
        handler = GreetingHandler(context)
        response = handler.handle(received_payload)
        if chat_db_context:
            flag_modified(chat_db_context, "context_data")
        return response


    # El manejo de reseteo por palabra clave ahora es manejado por el LLM
    # que debe devolver accion_backend: "saludar".

    action = received_payload.get("action")

    # New main menu handler
    if action == "mostrar_menu_reclamos":
        contexto_municipio_actual = chat_db_context.context_data.setdefault(CONTEXTO_MUNICIPIO, {})
        contexto_municipio_actual['estado_conversacion'] = ConversationState.ESPERANDO_SELECCION_MENU_RECLAMOS.name
        if chat_db_context:
            flag_modified(chat_db_context, "context_data")
        return _get_reclamos_menu()
    elif action:
        response = handle_main_menu_action(action, context)
        if response:
            return response

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
            return {
                "message_body": f"Aquí tienes el enlace que pediste: {seleccion.get('url')}",
                "options_list": [{"texto": "Menú Principal", "action_id": "saludar"}],
                "message_type": "interactive_buttons",
                "fuente": "seleccion_lista_dinamica"
            }
        else:
            # Si no se pudo procesar la selección, volver al menú principal
            return GreetingHandler(context).handle({})


    if action == "iniciar_reclamo": # Kept for backward compatibility or other flows
        return _get_reclamos_menu()

    if action == "reclamo_perdida_agua":
        return {
            "message_body": "Para pérdida de agua, dirigite a la página de Aysam:\nhttps://www.aysam.com.ar/",
            "options_list": [],
            "message_type": "text",
            "fuente": "info_perdida_agua"
        }

    if es_consulta_general(pregunta_str_for_check):
        current_location = location or flask_session.get("user_location")
        if current_location:
            # The location object might be a dict from session or a direct payload
            address = current_location.get("formatted_address") or current_location.get("address")
            return PointsOfInterestHandler(context={}).handle({"pregunta": pregunta_original, "location": address})
        else:
            contexto_municipio_actual = chat_db_context.context_data.setdefault(CONTEXTO_MUNICIPIO, {})
            contexto_municipio_actual['estado_conversacion'] = ConversationState.ESPERANDO_UBICACION_GENERAL.name
            contexto_municipio_actual['consulta_pendiente_ubicacion'] = pregunta_original # Save the original query
            if chat_db_context: flag_modified(chat_db_context, "context_data")
            return {
                "message_body": "Para poder ayudarte mejor, necesito tu ubicación. ¿Podrías compartirla?",
                "options_list": [{"texto": "Compartir ubicación", "action": "compartir_ubicacion"}, {"texto": "No, gracias", "action": "cancelar"}],
                "message_type": "interactive_buttons",
                "fuente": "solicitar_ubicacion"
            }

    reclamo_categories = {
        "reclamo_luminaria": "Luminaria",
        "reclamo_arbolado": "Arbolado",
        "reclamo_limpieza_riego": "Limpieza y riego",
        "reclamo_arreglo_calle": "Arreglo de calle",
        "reclamo_otros": "Otros",
    }
    if action in reclamo_categories:
        contexto_municipio_actual = chat_db_context.context_data.setdefault(CONTEXTO_MUNICIPIO, {})
        contexto_municipio_actual['categoria_reclamo'] = reclamo_categories[action]
        contexto_municipio_actual['estado_conversacion'] = ConversationState.ESPERANDO_DESCRIPCION_RECLAMO.name
        return {
            "message_body": f"Entendido, iniciaste un reclamo por **{reclamo_categories[action]}**. Por favor, describí la incidencia.",
            "options_list": [],
            "message_type": "text",
            "fuente": "inicio_flujo_reclamo_categorizado"
        }

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

        # Reconstruct the button list to be used for matching
        categorias_menu = [
            {"titulo": "Reclamos y Denuncias 🛠️", "botones": [{"texto": "Iniciar un Reclamo", "action_id": "mostrar_menu_reclamos"}, {"texto": "Realizar una Denuncia", "action_id": "denuncias"}]},
            {"titulo": "Trámites y Consultas 📄", "botones": [{"texto": "Licencia de Conducir", "action_id": "licencia_de_conducir"}, {"texto": "Pagar Tasas", "action_id": "pago_de_tasas_vigentes"}, {"texto": "Consultar otros trámites", "action_id": "consultar_otros_tramites"}]},
            {"titulo": "Servicios y Turnos 📅", "botones": [{"texto": "Veterinaria y Bromatología", "action_id": "veterinaria_y_bromatologia"}, {"texto": "Solicitar Turnos", "action_id": "solicitar_turnos"}]},
            {"titulo": "Información y Novedades 📰", "botones": [{"texto": "Agenda Cultural y Turística", "action_id": "agenda_cultural_y_turistica"}, {"texto": "Últimas Novedades", "action_id": "ultimas_novedades"}, {"texto": "Defensa del Consumidor", "action_id": "defensa_del_consumidor"}]}
        ]
        flat_buttons = [boton for categoria in categorias_menu for boton in categoria.get('botones', [])]

        selected_action = find_menu_action_by_input(pregunta_str_menu, flat_buttons)

        if selected_action:
            logger_actual.info(f"User input '{pregunta_str_menu}' matched to action: '{selected_action}'")

            # Special handling for actions that lead to a sub-menu
            if selected_action == "mostrar_menu_reclamos":
                contexto_municipio_actual['estado_conversacion'] = ConversationState.ESPERANDO_SELECCION_MENU_RECLAMOS.name
                if chat_db_context: flag_modified(chat_db_context, "context_data")
                return _get_reclamos_menu()

            # For all other actions, clear state and handle them
            contexto_municipio_actual['estado_conversacion'] = None
            if chat_db_context: flag_modified(chat_db_context, "context_data")
            response = handle_main_menu_action(selected_action)
            if response:
                return response
        else:
            # If no match, clear state and let LLM handle it
            logger_actual.info(f"Input '{pregunta_str_menu}' did not match any menu option. Passing to LLM.")
            contexto_municipio_actual['estado_conversacion'] = None
            if chat_db_context: flag_modified(chat_db_context, "context_data")
    # --- FIN: Manejo de selección de menú principal ---

    # --- INICIO: Manejo de selección de menú de reclamos ---
    elif estado_conversacion == ConversationState.ESPERANDO_SELECCION_MENU_RECLAMOS.name:
        pregunta_str_reclamo = ""
        if isinstance(pregunta_original, str):
            pregunta_str_reclamo = pregunta_original
        elif isinstance(pregunta_original, dict) and "pregunta" in pregunta_original:
            pregunta_str_reclamo = pregunta_original["pregunta"]

        logger_actual.info(f"Handling input in ESPERANDO_SELECCION_MENU_RECLAMOS state. Input: '{pregunta_str_reclamo}'")

        # El menú ahora tiene id_accion numéricos.
        # Primero, intentar matchear el input numérico con el id_accion.
        reclamo_options = _get_reclamos_menu().get("options_list", [])
        selected_category_name = None

        if pregunta_str_reclamo.isdigit():
            for option in reclamo_options:
                if option.get("id_accion") == pregunta_str_reclamo:
                    # Extraer el nombre de la categoría del texto del botón, ej "💡 Luminaria" -> "Luminaria"
                    selected_category_name = re.sub(r'^\d+\.\s*💡?\s*', '', option.get("texto", "")).strip()
                    break

        # Si no es un número, o el número no corresponde a una opción, intentar matchear por texto.
        if not selected_category_name:
            # Usar la función existente que busca por keywords.
            # Le pasamos una lista de dicts con la clave "texto" que espera.
            plain_text_options = [{"texto": re.sub(r'^\d+\.\s*💡?\s*', '', opt.get("texto", "")).strip()} for opt in reclamo_options]
            selected_category_name = find_reclamo_category_by_input(pregunta_str_reclamo, plain_text_options)

        if selected_category_name:
            if selected_category_name == "Pérdida de agua":
                contexto_municipio_actual['estado_conversacion'] = None
                if chat_db_context: flag_modified(chat_db_context, "context_data")
                return {
                    "message_body": "Para pérdida de agua, dirigite a la página de Aysam:\nhttps://www.aysam.com.ar/",
                    "options_list": [], "message_type": "text", "fuente": "info_perdida_agua"
                }

            logger_actual.info(f"Categoría de reclamo seleccionada: '{selected_category_name}'")
            constructed_prompt = f"Quiero iniciar un reclamo de {selected_category_name}"

            contexto_municipio_actual["estado_conversacion"] = ConversationState.ESPERANDO_INFO_RECLAMO_LLM.name
            contexto_municipio_actual["datos_parciales_llm_reclamo"] = {"categoria": selected_category_name}
            contexto_municipio_actual["historial_llm_reclamo"] = []
            contexto_municipio_actual.pop("current_menu", None)
            contexto_municipio_actual.pop("menu_page", None)

            # --- INICIO REFACTOR: Usar el 'context' principal en lugar de crear uno nuevo ---
            # El diccionario 'context' ya se inicializó al principio de la función
            # y contiene toda la información necesaria.
            response_dict, _ = handle_llm_interaction(
                constructed_prompt, context, viewer_user, owner_user, chat_db_context, contexto_municipio_actual
            )
            # --- FIN REFACTOR ---

            if response_dict is None:
                return {"message_body": "No pude procesar la selección. Probá de nuevo.",
                        "message_type": "text", "options_list": [], "fuente": "error_category_selection"}

            response_dict.setdefault("message_type", "text")
            response_dict.setdefault("options_list", [])
            return response_dict
        else:
            logger_actual.warning(f"Input '{pregunta_str_reclamo}' no coincide con ninguna categoría. Mostrando menú de nuevo.")
            return _get_reclamos_menu()
    # --- FIN: Manejo de selección de menú de reclamos ---

    # --- INICIO: Manejo de recepción de ubicación para consulta general ---
    elif estado_conversacion == ConversationState.ESPERANDO_UBICACION_GENERAL.name:
        if location:
            consulta_guardada = contexto_municipio_actual.pop('consulta_pendiente_ubicacion', None)
            contexto_municipio_actual['estado_conversacion'] = None # Clear state
            if chat_db_context: flag_modified(chat_db_context, "context_data")

            if consulta_guardada:
                logger_actual.info(f"Received location, processing saved query: '{consulta_guardada}'")
                return PointsOfInterestHandler(context={}).handle({"pregunta": consulta_guardada, "location": location.get("address")})
            else:
                logger_actual.warning("In ESPERANDO_UBICACION_GENERAL state but no saved query found.")
                return {"message_body": "Recibí tu ubicación, pero no recuerdo qué estabas buscando. ¿Podrías decírmelo de nuevo?", "options_list": [], "message_type": "text", "fuente": "error_no_saved_query"}
        else:
            # User sent something other than a location
            contexto_municipio_actual['estado_conversacion'] = None # Reset state
            if chat_db_context: flag_modified(chat_db_context, "context_data")
            return {"message_body": "No recibí una ubicación. Si cambiaste de opinión, no hay problema. ¿En qué te puedo ayudar?", "options_list": [], "message_type": "text", "fuente": "no_location_received"}
    # --- FIN: Manejo de recepción de ubicación ---

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
            return {
                "message_body": "Por favor, decime la nueva ubicación.",
                "options_list": [],
                "message_type": "text",
                "fuente": "pedir_nueva_ubicacion"
            }, contexto_municipio_actual

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
        respuesta_manejada_por_llm, contexto_municipio_actual = handle_llm_interaction(pregunta_str, context, viewer_user, owner_user, chat_db_context, contexto_municipio_actual)
        logger_actual.info(f"[AFTER_HANDLE_LLM] Contexto: {contexto_municipio_actual}")
        if respuesta_manejada_por_llm:
            if not isinstance(respuesta_manejada_por_llm, dict):
                respuesta_manejada_por_llm = {"message_body": str(respuesta_manejada_por_llm)}
            respuesta_manejada_por_llm.setdefault("message_type", "text")
            respuesta_manejada_por_llm.setdefault("options_list", [])
            return respuesta_manejada_por_llm

        # Si la intención se estableció en derivar a un agente, significa que el flujo del LLM
        # ya manejó la lógica y no debemos continuar con el flujo antiguo.
        if context.get("intencion") == "hablar_con_agente":
            mensaje_para_escalar = contexto_municipio_actual.get("mensaje_previo_llm_para_escalamiento", "Un agente se pondrá en contacto contigo en breve.")
            # Ensure the context is saved before returning
            if chat_db_context:
                flag_modified(chat_db_context, "context_data")
            return {
                "message_body": mensaje_para_escalar,
                "options_list": [],
                "message_type": "text",
                "fuente": "llm_derivar_humano_v2"
            }


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

    if location:
        contexto_municipio_actual["ubicacion_usuario"] = location

    # Construir 'usuario_info_for_gemini' para la llamada a Gemini
    datos_reclamo = contexto_municipio_actual.get("datos_parciales_llm_reclamo", {})
    usuario_info_for_gemini = {
        "nombre": getattr(viewer_user, "name", None) or getattr(viewer_user, "nombre", None) or datos_reclamo.get("nombre_usuario_detectado") or "Vecino/a",
        "tipo_entidad": "municipio",
        "municipio_config": { # Pasar datos relevantes de la config del municipio al LLM
            "nombre_municipio": final_municipio_config.get("nombre_display", MUNICIPIO_ID.title()),
            "servicios_principales": final_municipio_config.get("servicios_principales_chatbot", ["reclamos", "trámites", "consultas generales"])
        },
        "contacto": {
            "telefono": datos_reclamo.get("telefono_detectado"),
            "email": datos_reclamo.get("email_detectado")
        }
    }
    # Añadir ubicación si se conoce (del perfil del usuario o del contexto del reclamo)
    loc_usuario_texto = getattr(viewer_user, "direccion", None) or contexto_municipio_actual.get("direccion_reclamo")
    if loc_usuario_texto:
        usuario_info_for_gemini["ubicacion_conocida"] = loc_usuario_texto
        # Ask for confirmation
        if not contexto_municipio_actual.get("ubicacion_confirmada"):
            contexto_municipio_actual["estado_conversacion"] = ConversationState.ESPERANDO_CONFIRMACION_UBICACION.name
            return {
                "message_body": f"Veo que tu ubicación registrada es {loc_usuario_texto}. ¿Querés que busque cerca de ahí?",
                "options_list": [{"texto": "Sí"}, {"texto": "No, usar otra ubicación"}],
                "message_type": "interactive_buttons",
                "fuente": "confirmacion_ubicacion"
            }, contexto_municipio_actual

    # --- LLAMADA PRINCIPAL A GEMINI ---
    if "user_location" in flask_session:
        contexto_municipio_actual["ubicacion_usuario"] = flask_session["user_location"]
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
        if llm_response_structured.get("accion_backend") == "saludar":
            logger_actual.info("LLM detectó un saludo. Invocando GreetingHandler.")
            context_for_handler = {
                CONTEXTO_MUNICIPIO: contexto_municipio_actual,
                "chat_db_context_data": chat_db_context_live_data
            }
            handler = GreetingHandler(context_for_handler)
            response = handler.handle({})
            if chat_db_context:
                flag_modified(chat_db_context, "context_data")
            return response
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
    if llm_response_structured.get("accion_backend") == "error":
        return {
            "message_body": "Hubo un problema al procesar tu solicitud (acción desconocida).",
            "options_list": [],
            "message_type": "text",
            "fuente": "error",
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
        "empresa_token": getattr(owner_user, "token", None),

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

    if llm_response_structured.get("accion_backend") == "saludar":
        handler = GreetingHandler(global_context_for_orchestrator)
        action_handler_result = handler.handle(received_payload)
    else:
        orchestrator = ChatOrchestrator(global_context=global_context_for_orchestrator)
        action_handler_result = orchestrator.execute_action(llm_response_structured)

    # Ensure we always have a dictionary to avoid AttributeError when handlers return None
    if action_handler_result is None:
        logger.warning("Action handler returned None; defaulting to empty result dictionary")
        action_handler_result = {}

    # --- PROCESAR RESULTADO DEL ACTION HANDLER ---
    respuesta_final_texto = action_handler_result.get("message_to_user")
    if not respuesta_final_texto: # Si el handler no dio un mensaje, usar el del LLM
        respuesta_final_texto = llm_response_structured.get("respuesta_usuario", "No entendí, ¿podrías repetirlo?")

    # Tomar botones del LLM original, a menos que el handler los haya modificado (no implementado aún)
    opciones_finales = llm_response_structured.get("botones", [])

    # Determinar 'pedir_info' final: priorizar el del action_handler si existe, sino el del LLM
    pedir_info_final = action_handler_result.get("pedir_info") or llm_response_structured.get("pedir_info")

    # --- Actualizar estado de conversación en contexto_municipio_actual ---
    # (Esta sección se ha movido y mejorado)
    estado_conversacion_actual_str = contexto_municipio_actual.get("estado_conversacion")

    # Si el LLM pide info, la guardamos para el siguiente turno.
    if pedir_info_final:
        contexto_municipio_actual["esperando_info_llm"] = pedir_info_final
        # Mapear 'pedir_info' a un estado de conversación más granular si es posible
        pedir_info_norm = normalizar_str(str(pedir_info_final))
        estado_objetivo = None
        for key, state in PEDIR_INFO_TO_STATE.items():
            if key in pedir_info_norm:
                estado_objetivo = state
                break
        if estado_objetivo:
            contexto_municipio_actual["estado_conversacion"] = estado_objetivo.name
            logger_actual.info(f"Estado de conversación actualizado a: {estado_objetivo.name} por 'pedir_info'")
        else:
            # Si no hay un estado específico, pero se pide info, nos ponemos en un estado de espera genérico.
            contexto_municipio_actual["estado_conversacion"] = ConversationState.ESPERANDO_INFO_RECLAMO_LLM.name
            logger_actual.info(f"Estado de conversación actualizado a ESPERANDO_INFO_RECLAMO_LLM por 'pedir_info' no mapeado.")

    # Si el estado actual es de espera de datos, y el LLM no está pidiendo más,
    # significa que los datos se proporcionaron. Limpiamos el estado de espera.
    elif estado_conversacion_actual_str and estado_conversacion_actual_str.startswith("ESPERANDO_") and not pedir_info_final:
         # Si la acción fue exitosa, limpiamos el estado.
        if action_handler_result.get("success"):
            logger_actual.info(f"Acción exitosa sin 'pedir_info' adicional. Limpiando estado de conversación '{estado_conversacion_actual_str}'.")
            contexto_municipio_actual.pop("estado_conversacion", None)
            contexto_municipio_actual.pop("esperando_info_llm", None)

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

    # ---- FIN: Lógica de sugerencia de registro PROACTIVA ----


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


    # --- Formatear respuesta final ---
    message_type_final = "text"
    if opciones_finales:
        num_options = len(opciones_finales)
        if 0 < num_options <= 3:
            message_type_final = "interactive_buttons"
        elif num_options > 3:
            message_type_final = "interactive_list"


    final_response_dict = {
        "message_body": respuesta_final_texto,
        "options_list": opciones_finales,
        "message_type": message_type_final,
        "estado": contexto_municipio_serializado_para_db.get("estado_conversacion"),
        "contexto_actualizado": {CONTEXTO_MUNICIPIO: contexto_municipio_serializado_para_db},
        "ticket_id": action_handler_result.get("data", {}).get("ticket_id") or action_handler_result.get("data", {}).get("sugerencia_id"), # Tomar de data si existe
        "fuente": action_handler_result.get("fuente") or llm_response_structured.get("accion_backend", "municipio_general_v4"),
        # Otros campos como media_url, location_data, adjuntos se manejarían si son parte de la respuesta
    }

    # --- Google Search Fallback ---
    generic_fuentes = ["municipio_general_v4", "fallback_final", "llm_respuesta_general_v2"]
    if final_response_dict.get("fuente") in generic_fuentes and not final_response_dict.get("options_list"):
        logger_actual.info(f"Respuesta genérica (fuente: {final_response_dict.get('fuente')}). Intentando fallback con Google Search.")
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
    return final_response_dict
