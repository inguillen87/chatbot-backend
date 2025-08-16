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
    ESPERANDO_DISTRITO_RECLAMO = auto()
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

# Palabras clave sencillas para detectar consultas generales de servicios
GENERAL_QUERY_KEYWORDS = [
    "veterinaria", "veterinarias", "farmacia", "supermercado", "negocio",
    "servicio", "buscar", "comercio", "local"
]

def es_consulta_general(texto: str) -> bool:
    """Detecta si el texto parece una consulta de servicios generales."""
    texto_norm = normalizar_texto(texto or "")
    return any(k in texto_norm for k in GENERAL_QUERY_KEYWORDS)

PALABRAS_CLAVE_REINICIO = {
    "reclamo", "reclamar", "denuncia", "denunciar",
    "menu", "menú", "inicio", "empezar", "ayuda",
    "quiero hacer un reclamo", "iniciar reclamo",
    "consultar tramite", "ver tramites", "otro tramite",
    "empezar de nuevo", "volver al inicio"
}

def detectar_intencion_de_reinicio(texto: str) -> bool:
    """Detecta si el texto del usuario indica una intención de reiniciar la conversación."""
    if not texto:
        return False

    texto_norm = normalizar_texto(texto.strip())

    # Check for exact matches of longer phrases first
    for frase in sorted([k for k in PALABRAS_CLAVE_REINICIO if " " in k], key=len, reverse=True):
        if frase in texto_norm:
            return True

    # Check for single keywords in short sentences (e.g., <= 3 words)
    palabras = texto_norm.split()
    if len(palabras) <= 3:
        for palabra in palabras:
            if palabra in PALABRAS_CLAVE_REINICIO:
                return True

    return False

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
            catalog_file_path = os.path.join(os.path.dirname(__file__), "..", "data", "municipios", MUNICIPIO_ID, "product_catalog.json")
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
            loc_file_path = os.path.join(os.path.dirname(__file__), "..", "data", "municipios", MUNICIPIO_ID, "commerce_locations.json")
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

class GreetingHandler(BaseMunicipioHandler):
    def _get_user_display_name(self) -> str:
        """Return a friendly name for the user based on context info."""
        name = self.context.get("nombre_usuario_contexto")
        if name:
            return name

        phone = self.context.get("telefono_usuario_contexto") or self.context.get("telefono_usuario")
        if phone:
            return f"Usuario de WhatsApp {phone[-4:]}"

        return "Vecino/a"

    def handle(self, payload: dict) -> dict | None:
        user_name = self._get_user_display_name()
        welcome_message = (
            f"¡Hola, {user_name}! 👋 Soy JUNI, tu Asistente Virtual de la Municipalidad de Junín. "
            "Estoy aquí para ayudarte de una forma más inteligente. Podés consultarme sobre trámites, "
            "reclamos, turnos, noticias y mucho más.\n\n"
            "¿Cómo te puedo ayudar hoy? Elegí una opción o escribí una palabra clave:"
        )

        options_list = [
            {"texto": "🛠️ Iniciar un Reclamo", "action_id": "mostrar_menu_reclamos", "id": "mostrar_menu_reclamos"},
            {"texto": "⚖️ Realizar una Denuncia", "action_id": "denuncias", "id": "denuncias"},
            {"texto": "🚗 Licencia de Conducir", "action_id": "licencia_de_conducir", "id": "licencia_de_conducir"},
            {"texto": "💵 Pagar Tasas", "action_id": "pago_de_tasas_vigentes", "id": "pago_de_tasas_vigentes"},
            {"texto": "📋 Consultar otros trámites", "action_id": "consultar_otros_tramites", "id": "consultar_otros_tramites"},
            {"texto": "🐾 Veterinaria y Bromatología", "action_id": "veterinaria_y_bromatologia", "id": "veterinaria_y_bromatologia"},
            {"texto": "📅 Solicitar Turnos", "action_id": "solicitar_turnos", "id": "solicitar_turnos"},
            {"texto": "🎭 Agenda Cultural y Turística", "action_id": "agenda_cultural_y_turistica", "id": "agenda_cultural_y_turistica"},
            {"texto": "📰 Últimas Novedades", "action_id": "ultimas_novedades", "id": "ultimas_novedades"},
            {"texto": "🛒 Defensa del Consumidor", "action_id": "defensa_del_consumidor", "id": "defensa_del_consumidor"},
        ]

        categorias = [
            {
                "titulo": "Reclamos y Denuncias 🛠️",
                "botones": [
                    {"texto": "🛠️ Iniciar un Reclamo", "action_id": "mostrar_menu_reclamos"},
                    {"texto": "⚖️ Realizar una Denuncia", "action_id": "denuncias"},
                ],
            },
            {
                "titulo": "Trámites y Consultas 📄",
                "botones": [
                    {"texto": "🚗 Licencia de Conducir", "action_id": "licencia_de_conducir"},
                    {"texto": "💵 Pagar Tasas", "action_id": "pago_de_tasas_vigentes"},
                    {"texto": "📋 Consultar otros trámites", "action_id": "consultar_otros_tramites"},
                ],
            },
            {
                "titulo": "Servicios y Turnos 📅",
                "botones": [
                    {"texto": "🐾 Veterinaria y Bromatología", "action_id": "veterinaria_y_bromatologia"},
                    {"texto": "📅 Solicitar Turnos", "action_id": "solicitar_turnos"},
                ],
            },
            {
                "titulo": "Información y Novedades 📰",
                "botones": [
                    {"texto": "🎭 Agenda Cultural y Turística", "action_id": "agenda_cultural_y_turistica"},
                    {"texto": "📰 Últimas Novedades", "action_id": "ultimas_novedades"},
                    {"texto": "🛒 Defensa del Consumidor", "action_id": "defensa_del_consumidor"},
                ],
            },
        ]

        return {
            "message_body": welcome_message,
            "options_list": options_list,
            "message_type": "interactive_list",
            "fuente": "greeting_handler_categorized_v2",
            "categorias": categorias,
            "generar_audio": True,
        }

class ReclamosMenuHandler(BaseMunicipioHandler):
    def handle(self, payload: dict) -> dict | None:
        return {
            "message_body": "Por favor, seleccioná el tipo de reclamo:",
            "options_list": [
                {"id": "reclamo_luminaria", "texto": "Luminaria"},
                {"id": "reclamo_arbolado", "texto": "Arbolado"},
                {"id": "reclamo_limpieza", "texto": "Limpieza y riego"},
                {"id": "reclamo_calle", "texto": "Arreglo de calle"},
                {"id": "reclamo_perdida_agua", "texto": "Pérdida de agua"},
                {"id": "reclamo_otros", "texto": "Otros"},
            ],
            "message_type": "interactive_list",
            "fuente": "reclamos_menu_handler"
        }

class LicenciaConducirHandler(BaseMunicipioHandler):
    def handle(self, payload: dict) -> dict | None:
        message_body = "Para requisitos y turnos de licencia de conducir visitá:\nhttps://www.juninmendoza.gov.ar/licencia-de-conducir-junin/"
        botones = agregar_botones_para_links(message_body, [])
        return {
            "message_body": message_body,
            "options_list": botones,
            "fuente": "licencia_conducir_handler"
        }

class PagoTasasHandler(BaseMunicipioHandler):
    def handle(self, payload: dict) -> dict | None:
        message_body = "Para pagar o descargar boletos vigentes, dirigite a:\nhttps://epagos.juninmendoza.gov.ar/jrentas/"
        botones = agregar_botones_para_links(message_body, [])
        return {
            "message_body": message_body,
            "options_list": botones,
            "fuente": "pago_tasas_handler"
        }

class DefensaConsumidorHandler(BaseMunicipioHandler):
    def handle(self, payload: dict) -> dict | None:
        return {
            "message_body": "Para asesoramiento, escribí a:\ndefensadelconsumidorjuninmza@gmail.com",
            "fuente": "defensa_consumidor_handler"
        }

class VeterinariaBromatologiaHandler(BaseMunicipioHandler):
    def handle(self, payload: dict) -> dict | None:
        return {
            "message_body": "Para información vinculada a veterinaria y bromatología municipal escribí al WhatsApp:\n+54 9 2634 52-1563",
            "fuente": "veterinaria_bromatologia_handler"
        }

class PerdidaDeAguaHandler(BaseMunicipioHandler):
    def handle(self, payload: dict) -> dict | None:
        message_body = "Para pérdida de agua, dirigite a la página de Aysam:\nhttps://www.aysam.com.ar/"
        botones = agregar_botones_para_links(message_body, [])
        return {
            "message_body": message_body,
            "options_list": botones,
            "fuente": "perdida_de_agua_handler"
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
    resultado = handler.execute(datos_reclamo)

    if resultado.get("success"):
        return {
            "message_body": resultado.get("message_to_user", "Reclamo generado."),
            "options_list": resultado.get("botones", []),
            "fuente": "accion_crear_reclamo_llm_exito",
            "ticket_id": resultado.get("data", {}).get("ticket_id"),
        }

    return {
        "message_body": resultado.get(
            "message_to_user",
            "Hubo un problema al intentar registrar tu reclamo. Por favor, intenta de nuevo.",
        ),
        "options_list": [],
        "fuente": "accion_crear_reclamo_llm_error",
    }
def _handle_ticket_creation(contexto_municipio_actual, context, datos_estructura_llm):
    """
    Handles the ticket creation process.
    """
    # Combina los datos parciales con los nuevos datos recibidos
    datos_reclamo = contexto_municipio_actual.get("datos_parciales_llm_reclamo", {})
    datos_reclamo.update(datos_estructura_llm)

    # Validar datos
    nombre = datos_reclamo.get("nombre_usuario_detectado") or datos_reclamo.get("usuario")
    telefono = datos_reclamo.get("telefono_detectado") or datos_reclamo.get("telefono")
    email = datos_reclamo.get("email_detectado") or datos_reclamo.get("email")
    ubicacion = datos_reclamo.get("ubicacion")

    if not all([nombre, telefono, email, ubicacion]):
        campos_faltantes = []
        if not nombre:
            campos_faltantes.append("nombre")
        if not telefono:
            campos_faltantes.append("teléfono")
        if not email:
            campos_faltantes.append("email")
        if not ubicacion:
            campos_faltantes.append("ubicación")

        return {
            "message_body": f"Faltan los siguientes datos para poder crear el reclamo: {', '.join(campos_faltantes)}. Por favor, proporciónalos para continuar.",
            "options_list": [],
            "message_type": "text",
            "fuente": "datos_incompletos"
        }, contexto_municipio_actual

    # Llama a la acción para crear el reclamo
    respuesta_accion = accion_crear_reclamo_municipio(datos_reclamo, context)

    # Limpia el contexto del reclamo en el municipio
    contexto_municipio_actual.clear()

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
            "[HANDLE_LLM] Cambio de tema detectado durante flujo de reclamo. Reseteando contexto TOTAL a conversacion general."
        )
        # BUG FIX: El contexto anterior era parcial y dejaba estados viejos.
        # Se limpia COMPLETAMENTE el contexto del municipio para evitar que el fallback
        # a la lógica antigua recoja estados residuales de un reclamo anterior.
        contexto_municipio_actual.clear()

        # Se restablece el estado únicamente al modo de conversación general con el LLM,
        # inicializando las claves esperadas para evitar KeyErrors.
        contexto_municipio_actual["estado_conversacion"] = ConversationState.CONVERSACION_GENERAL_LLM.name
        contexto_municipio_actual["datos_parciales_llm_reclamo"] = {}
        contexto_municipio_actual["historial_llm_reclamo"] = []
        contexto_municipio_actual["historial_conversacion_general_llm"] = []

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
        }
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

        respuesta_usuario_llm = respuesta_llm_dict.get("respuesta_usuario")
        accion_backend_llm = respuesta_llm_dict.get("accion_backend")
        datos_estructura_llm = respuesta_llm_dict.get("datos_estructura")
        pedir_info_llm = respuesta_llm_dict.get("pedir_info")
        botones_llm = respuesta_llm_dict.get("botones", [])

        if not respuesta_usuario_llm:
            return None, None

        nuevo_turno_historial = {"pregunta_usuario": pregunta_str, "respuesta_ia": respuesta_usuario_llm}

        if accion_backend_llm == "crear_reclamo" and datos_estructura_llm and datos_estructura_llm.get("target") == "municipio":
            # Si es el inicio de un nuevo reclamo, limpiar el contexto anterior
            if contexto_municipio_actual.get("estado_conversacion") != ConversationState.ESPERANDO_INFO_RECLAMO_LLM.name:
                contexto_municipio_actual["datos_parciales_llm_reclamo"] = {}
                contexto_municipio_actual["historial_llm_reclamo"] = []

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
        elif accion_backend_llm == "derivar_humano":
            context["intencion"] = "hablar_con_agente"
            contexto_municipio_actual["mensaje_previo_llm_para_escalamiento"] = respuesta_usuario_llm
            logger.info("[HANDLE_LLM] LLM derivó a humano, retornando mensaje para el usuario.")
            # Devolver el mensaje del LLM directamente en lugar de depender del fallback.
            return {"message_body": respuesta_usuario_llm, "options_list": botones_llm, "fuente": "llm_deriva_humano"}, contexto_municipio_actual
        elif accion_backend_llm == "ejecutar_herramienta":
            nombre_herramienta = datos_estructura_llm.get("nombre_herramienta")
            parametros_herramienta = datos_estructura_llm.get("parametros_herramienta", {})
            parametros_faltantes = datos_estructura_llm.get("faltan_parametros_herramienta", [])

            if nombre_herramienta and nombre_herramienta in TOOL_REGISTRY:
                if parametros_faltantes:
                    # Guardar el estado actual y pedir al usuario la información que falta
                    contexto_municipio_actual["estado_conversacion"] = ConversationState.ESPERANDO_INFO_RECLAMO_LLM.name
                    contexto_municipio_actual["esperando_info_llm_reclamo"] = parametros_faltantes[0] # Pedir un parámetro a la vez
                    contexto_municipio_actual["datos_parciales_llm_reclamo"] = {
                        "nombre_herramienta": nombre_herramienta,
                        "parametros_herramienta": parametros_herramienta
                    }
                    return {"message_body": respuesta_usuario_llm, "options_list": botones_llm, "message_type": "text", "fuente": "llm_pide_info_herramienta"}, contexto_municipio_actual

                herramienta = TOOL_REGISTRY[nombre_herramienta]
                funcion_herramienta = herramienta["funcion"]

                try:
                    logger.info(f"[HERRAMIENTA] Intentando ejecutar: {nombre_herramienta} con params: {parametros_herramienta}")
                    resultado_herramienta = funcion_herramienta(**parametros_herramienta)
                    logger.info(f"[HERRAMIENTA] Resultado de {nombre_herramienta}: {resultado_herramienta[:200] if resultado_herramienta else ''}...")

                    response_dict = {
                        "message_body": respuesta_usuario_llm,
                        "options_list": botones_llm,
                        "message_type": "text",
                        "fuente": f"herramienta_{nombre_herramienta}"
                    }

                    if nombre_herramienta == "generar_respuesta_audio":
                        response_dict["audio_url"] = resultado_herramienta
                    else:
                        response_dict["message_body"] = f"{respuesta_usuario_llm}\n\n{resultado_herramienta}"

                    contexto_municipio_actual.setdefault("historial_conversacion_general_llm", []).append({
                        "pregunta_usuario": pregunta_str,
                        "respuesta_ia": response_dict["message_body"]
                    })

                    return response_dict, contexto_municipio_actual

                except Exception as e:
                    logger.error(f"Error ejecutando la herramienta '{nombre_herramienta}': {e}", exc_info=True)
                    return {
                        "message_body": "Hubo un error al intentar usar la herramienta. Por favor, intenta de nuevo.",
                        "options_list": [],
                        "message_type": "text",
                        "fuente": "error_herramienta"
                    }, contexto_municipio_actual
            else:
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

        error_response = {
            "message_body": "Lo siento, ocurrió un error inesperado al procesar tu mensaje. Por favor, intenta de nuevo más tarde.",
            "options_list": [],
            "message_type": "text",
            "fuente": "error_handle_llm_interaction"
        }
        return error_response, contexto_municipio_actual

GREETING_KEYWORDS = {"hola", "buenos dias", "buenas tardes", "buenas noches", "hey", "hi", "hello"}

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
    
    opciones_finales = []
    respuesta_final_texto = ""
    action_handler_result = {}
    llm_response_structured = {}
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

    # Log the current state of the conversation
    estado_conversacion = contexto_municipio_data_from_db.get("estado_conversacion")
    logger_actual.info(f"[CONTEXTO_MUNICIPIO] Estado de conversacion actual: {estado_conversacion}")

    # Directly use the dictionary from the live context data.
    # This ensures that modifications are made to the original object.
    contexto_municipio_actual = chat_db_context_live_data.setdefault(CONTEXTO_MUNICIPIO, {})

    # --- Context Timeout Logic ---
    if chat_db_context and hasattr(chat_db_context, 'last_updated') and chat_db_context.last_updated:
        from datetime import datetime, timedelta
        if datetime.utcnow() - chat_db_context.last_updated > timedelta(minutes=15):
            logger_actual.info(f"Context for session {kwargs.get('chat_session_uuid')} timed out. Resetting.")
            contexto_municipio_actual.clear()
            # Also reset the higher-level context to ensure a clean slate
            chat_db_context_live_data.clear()
            # Re-initialize the essential structure
            chat_db_context_live_data[CONTEXTO_MUNICIPIO] = contexto_municipio_actual


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

    # --- Enhanced Greeting and Reset Logic ---
    estado_conversacion_actual = contexto_municipio_actual.get("estado_conversacion")
    if isinstance(estado_conversacion_actual, Enum):
        estado_conversacion_actual = estado_conversacion_actual.name

    is_in_flow = estado_conversacion_actual not in [None, ConversationState.CONVERSACION_GENERAL_LLM.name]
    is_greeting = GREETING_KEYWORDS.intersection(pregunta_str.lower().split())
    is_reset_intent = detectar_intencion_de_reinicio(pregunta_str)

    if is_greeting or (is_in_flow and is_reset_intent):
        if is_greeting:
            logger_actual.info("Greeting detected. Resetting context and showing welcome menu.")
        else:
            logger_actual.info(f"Reset intent detected ('{pregunta_str}'). Resetting context and showing welcome menu.")

        contexto_municipio_actual.clear()
        if chat_db_context and hasattr(chat_db_context, 'context_data'):
            chat_db_context.context_data[CONTEXTO_MUNICIPIO] = {}
            flag_modified(chat_db_context, "context_data")

        handler_context = {"user_obj": owner_user, "viewer_user_obj": viewer_user}
        handler = GreetingHandler(handler_context)
        return handler.handle(received_payload)

    # --- Simple Router for Main Menu Options ---
    pregunta_str_lower = pregunta_str.strip().lower()
    if pregunta_str_lower == "reclamos":
        return ReclamosMenuHandler(context).handle(received_payload)
    elif pregunta_str_lower == "licencia_conducir":
        return LicenciaConducirHandler(context).handle(received_payload)
    elif pregunta_str_lower == "pago_tasas":
        return PagoTasasHandler(context).handle(received_payload)
    elif pregunta_str_lower == "defensa_consumidor":
        return DefensaConsumidorHandler(context).handle(received_payload)
    elif pregunta_str_lower == "veterinaria_bromatologia":
        return VeterinariaBromatologiaHandler(context).handle(received_payload)
    elif pregunta_str_lower == "reclamo_perdida_agua":
        return PerdidaDeAguaHandler(context).handle(received_payload)
    elif pregunta_str_lower.startswith("reclamo_"):
        categoria = pregunta_str_lower.replace("reclamo_", "").replace("_", " ").title()
        contexto_municipio_actual['categoria_reclamo'] = categoria
        # Let it fall through to the LLM to ask for the claim details
        pregunta_str = f"Quiero hacer un reclamo de {categoria}."


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

    # --- LLM-first approach ---
    logger_actual.info(f"[BEFORE_HANDLE_LLM] Contexto: {contexto_municipio_actual}")
    respuesta_manejada_por_llm, _ = handle_llm_interaction(pregunta_str, context, viewer_user, owner_user, chat_db_context, contexto_municipio_actual)
    logger_actual.info(f"[AFTER_HANDLE_LLM] Contexto: {contexto_municipio_actual}")

    if respuesta_manejada_por_llm:
        return respuesta_manejada_por_llm

    # --- Fallback to old logic if LLM interaction returns None ---
    logger_actual.warning("[FALLBACK] LLM interaction did not handle the message. Proceeding with fallback logic.")


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
