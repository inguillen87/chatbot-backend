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
from services.intent_classifier import IntentClassifier
from services.multimodal_analyzer import analizar_imagen_con_fallback
import json

class ReclamoState(Enum):
    ESPERANDO_CATEGORIA = auto()
    ESPERANDO_DIRECCION = auto()
    ESPERANDO_DESCRIPCION = auto()
    ESPERANDO_FOTO = auto()
    ESPERANDO_DATOS_CONTACTO = auto()
    ESPERANDO_CONFIRMACION = auto()

def extract_multiple_contact_details_regex(text: str) -> dict:
    details = {}
    email_match = re.search(r'[\w\.-]+@[\w\.-]+\.\w+', text)
    if email_match:
        details['email'] = email_match.group(0)
        text = text.replace(email_match.group(0), '')
    dni_match = re.search(r'\b\d{7,8}\b', text)
    if dni_match:
        details['dni'] = dni_match.group(0)
        text = text.replace(dni_match.group(0), '')
    phone_match = re.search(r'(?:\+54\s?)?(?:9\s?)?(\d{2,4})\s?(\d{6,8})', text)
    if phone_match:
        details['telefono'] = f"{phone_match.group(1)}{phone_match.group(2)}"
        text = text.replace(phone_match.group(0), '')
    name_candidate = text.strip(' .,-_/\\')
    if len(name_candidate.split()) >= 2 and len(name_candidate.split()) <= 4:
        if 'mi nombre es' in name_candidate.lower():
            name_candidate = name_candidate.lower().replace('mi nombre es', '').strip()
        details['nombre'] = ' '.join([word.capitalize() for word in name_candidate.split()])
    return details

CANCEL_KEYWORDS = {"cancelar", "salir", "volver", "menu", "menú principal", "terminar", "basta"}

class ReclamoFlowHandler:
    def __init__(self, context, chat_db_context):
        self.context = context
        self.chat_db_context = chat_db_context
        self.flow_context = context.get("chat_db_context_data", {}).setdefault("reclamo_flow_v2", {})
        self.greeting_handler = GreetingHandler(context)


    def check_for_cancel(self, user_input):
        if normalizar_texto(user_input) in CANCEL_KEYWORDS:
            return self.end_flow("Proceso de reclamo cancelado. ¿En qué más te puedo ayudar?", show_menu=True)
        return None

    def handle(self, user_input, payload):
        cancel_response = self.check_for_cancel(user_input)
        if cancel_response:
            return cancel_response

        state_name = self.flow_context.get("state")
        state = ReclamoState[state_name] if state_name else None

        if state == ReclamoState.ESPERANDO_CATEGORIA:
            return self.handle_categoria(user_input)
        elif state == ReclamoState.ESPERANDO_DIRECCION:
            return self.handle_direccion(user_input, payload)
        elif state == ReclamoState.ESPERANDO_DESCRIPCION:
            return self.handle_descripcion(user_input)
        elif state == ReclamoState.ESPERANDO_FOTO:
            return self.handle_foto(user_input, payload)
        elif state == ReclamoState.ESPERANDO_DATOS_CONTACTO:
            return self.handle_datos_contacto(user_input)
        elif state == ReclamoState.ESPERANDO_CONFIRMACION:
            return self.handle_confirmacion(user_input, payload)
        else:
            logger.error(f"ReclamoFlowHandler: Estado desconocido o no manejado: {state_name}")
            return self.end_flow("Hubo un error en el proceso, por favor intentá de nuevo.", show_menu=True)

    def start_flow(self, datos_iniciales=None, categoria_inicial=None):
        logger.info("Iniciando flujo de reclamo v2.")
        self.flow_context.clear()
        self.flow_context['datos_reclamo'] = datos_iniciales or {}
        if categoria_inicial and not self.flow_context['datos_reclamo'].get('categoria'):
            self.flow_context['datos_reclamo']['categoria'] = categoria_inicial

        # Check what data is missing and transition to the correct state
        if not self.flow_context['datos_reclamo'].get('categoria'):
            self.flow_context['state'] = ReclamoState.ESPERANDO_CATEGORIA.name
            return _get_reclamos_menu()
        elif not self.flow_context['datos_reclamo'].get('descripcion'):
            # This case is less likely if categoria is present, but good to have
            self.flow_context['state'] = ReclamoState.ESPERANDO_DESCRIPCION.name
            categoria = self.flow_context['datos_reclamo']['categoria']
            return {"message_body": f"Entendido, el reclamo es por *{categoria}*. Ahora, por favor, describí brevemente el problema."}
        elif not self.flow_context['datos_reclamo'].get('direccion'):
            self.flow_context['state'] = ReclamoState.ESPERANDO_DIRECCION.name
            # Construct a message confirming the data we have
            categoria = self.flow_context['datos_reclamo']['categoria']
            descripcion = self.flow_context['datos_reclamo'].get('descripcion', 'No especificada')

            # If the description came from an image, it might be generic.
            # We can tailor the message.
            if self.flow_context['datos_reclamo'].get('origen_descripcion') == 'imagen':
                 return {"message_body": f"Gracias a tu imagen, entiendo que el reclamo es por *{categoria}* (problema similar a: '{descripcion}').\n\nPara continuar, por favor, indicame la dirección exacta del problema."}
            else:
                 return {"message_body": f"Reclamo por *{categoria}*.\n\nPara continuar, por favor, indicame la dirección exacta del problema."}
        else:
            # All initial data is present, move to confirmation or next step
            return self.ask_for_contact_details()

    def handle_categoria(self, user_input):
        self.flow_context['datos_reclamo']['categoria'] = user_input
        self.flow_context['state'] = ReclamoState.ESPERANDO_DIRECCION.name
        return {"message_body": f"Perfecto. Iniciemos tu reclamo por *{user_input}*.\n\nPor favor, indicame la dirección exacta del problema (calle y número). O podés escribir 'cancelar' para volver al menú."}

    def handle_direccion(self, user_input, payload):
        if payload.get("es_ubicacion") and payload.get("ubicacion_usuario"):
            location_data = payload.get("ubicacion_usuario")
            address = location_data.get("address")
            self.flow_context['datos_reclamo']['direccion'] = address if address else f"Lat: {location_data.get('latitude')}, Lon: {location_data.get('longitude')}"
        elif len(user_input) < 5:
             return {"message_body": "La dirección parece muy corta. Por favor, ingresá una dirección más completa (calle y número)."}
        else:
            self.flow_context['datos_reclamo']['direccion'] = user_input

        self.flow_context['state'] = ReclamoState.ESPERANDO_DESCRIPCION.name
        return {"message_body": "Gracias. Ahora, por favor, describí brevemente el problema."}

    def handle_descripcion(self, user_input):
        if len(user_input) < 10:
            return {"message_body": "Por favor, dame una descripción un poco más detallada del problema."}
        self.flow_context['datos_reclamo']['descripcion'] = user_input
        self.flow_context['state'] = ReclamoState.ESPERANDO_FOTO.name
        return {
            "message_body": "¿Querés agregar una foto? Esto ayuda mucho a resolver el problema.",
            "options_list": [{"texto": "Sí, agregar foto", "action_id": "reclamo_adjuntar_foto_si"}, {"texto": "No, omitir foto", "action_id": "reclamo_adjuntar_foto_no"}],
            "message_type": "interactive_buttons"
        }

    def handle_foto(self, user_input, payload):
        action = payload.get("action")
        if payload.get("es_foto") and payload.get("foto_url"):
            self.flow_context['datos_reclamo']['foto_url'] = payload.get("foto_url")
            return self.ask_for_contact_details()

        if "no" in user_input.lower() or action == "reclamo_adjuntar_foto_no":
            self.flow_context['datos_reclamo']['foto_url'] = None
            return self.ask_for_contact_details()
        elif "si" in user_input.lower() or action == "reclamo_adjuntar_foto_si":
            return {"message_body": "Por favor, enviá la foto ahora."}
        else:
            return {"message_body": "No entendí tu respuesta. Por favor, enviá una foto o elegí una de las opciones.", "options_list": [{"texto": "Omitir foto", "action_id": "reclamo_adjuntar_foto_no"}]}

    def ask_for_contact_details(self):
        self.flow_context['state'] = ReclamoState.ESPERANDO_DATOS_CONTACTO.name
        return {"message_body": "Ya casi terminamos. Por favor, decime tu nombre completo, DNI, email y teléfono. Podés escribir todo en un solo mensaje."}

    def handle_datos_contacto(self, user_input):
        contact_details = extract_multiple_contact_details_regex(user_input)
        if not contact_details:
            return {"message_body": "No pude identificar tus datos. Por favor, intentá de nuevo incluyendo nombre, DNI, email y teléfono."}

        self.flow_context['datos_reclamo'].update(contact_details)
        self.flow_context['state'] = ReclamoState.ESPERANDO_CONFIRMACION.name
        return self.get_confirmation_message()

    def get_confirmation_message(self):
        datos = self.flow_context.get('datos_reclamo', {})
        mensaje = "Por favor, confirmá que los datos de tu reclamo son correctos:\n\n"
        mensaje += f"🏷️ *Categoría:* {datos.get('categoria', 'No especificada')}\n"
        mensaje += f"📍 *Dirección:* {datos.get('direccion', 'No especificada')}\n"
        mensaje += f"📝 *Descripción:* {datos.get('descripcion', 'No especificada')}\n"
        mensaje += f"👤 *Nombre:* {datos.get('nombre', 'No especificado')}\n"
        mensaje += f"🆔 *DNI:* {datos.get('dni', 'No especificado')}\n"
        mensaje += f"📧 *Email:* {datos.get('email', 'No especificado')}\n"
        mensaje += f"📱 *Teléfono:* {datos.get('telefono', 'No especificado')}\n"
        mensaje += f"📷 *Foto adjunta:* {'Sí' if datos.get('foto_url') else 'No'}\n"
        return {
            "message_body": mensaje,
            "options_list": [{"texto": "✅ Confirmar", "action_id": "reclamo_confirmar_si"}, {"texto": "✏️ Editar datos", "action_id": "reclamo_confirmar_no"}, {"texto": "❌ Cancelar", "action_id": "reclamo_cancelar"}],
            "message_type": "interactive_buttons"
        }

    def handle_confirmacion(self, user_input, payload):
        action = payload.get("action")
        if "si" in user_input.lower() or action == "reclamo_confirmar_si":
            datos_reclamo = self.flow_context.get('datos_reclamo', {})
            ticket_id = "R" + str(random.randint(1000, 9999))
            success_message = f"¡Tu reclamo fue creado con éxito! ✅\n\nEl número de seguimiento es *{ticket_id}*. Te mantendremos informado sobre el estado del mismo por este medio."
            return self.end_flow(success_message)
        elif "no" in user_input.lower() or action == "reclamo_confirmar_no":
            return self.ask_for_contact_details()
        else: # Cancel
            return self.end_flow("Proceso de reclamo cancelado. ¿En qué más te puedo ayudar?", show_menu=True)

    def end_flow(self, message, show_menu=False):
        self.flow_context.clear()
        if "reclamo_flow_v2" in self.context.get("chat_db_context_data", {}):
            del self.context["chat_db_context_data"]["reclamo_flow_v2"]

        if show_menu:
            return self.greeting_handler.handle({})
        else:
            return {"message_body": message, "message_type": "text"}

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
# Configuración base (se puede sobrescribir por municipio en cada request)
CONFIG_MUNICIPIO = cargar_configuracion_municipio("default", "config.json")

TODAS_LAS_CATEGORIAS_UNICAS = sorted(list(set(KEYWORD_TO_CATEGORY_MAP.values())))
BOTONES_TODAS_CATEGORIAS = [{"texto": cat} for cat in TODAS_LAS_CATEGORIAS_UNICAS]

def cargar_tramites_info(municipio_id: str = MUNICIPIO_ID):
    return cargar_configuracion_municipio(municipio_id, "tramites.json")

def get_tramites_info(municipio_id: str = MUNICIPIO_ID) -> dict:
    return cargar_tramites_info(municipio_id)

def cargar_contactos_utiles(municipio_id: str = MUNICIPIO_ID):
    return cargar_configuracion_municipio(municipio_id, "contactos_utiles.json")

def cargar_agenda_cultural(municipio_id: str = MUNICIPIO_ID):
    return cargar_configuracion_municipio(municipio_id, "agenda_cultural.json")

def obtener_info_tramite_web(tramite_nombre: str, municipio_id: str = MUNICIPIO_ID) -> dict:
    """Obtiene la descripción y enlaces de un trámite desde ``tramites.json``.

    El archivo ``tramites_links.json`` ya no se utiliza. En su lugar, la información
    de cada trámite (incluyendo enlaces asociados) se mantiene dentro de
    ``tramites.json`` para cada municipio.

    Args:
        tramite_nombre: Nombre del trámite buscado.
        municipio_id: Identificador del municipio.

    Returns:
        dict: Un diccionario con las claves ``contenido`` y ``botones`` si el
        trámite se encuentra. Si no existe, se devuelve ``{"error": ...}``.
    """

    tramites = cargar_tramites_info(municipio_id)
    if not tramites:
        return {"error": "No se encontraron trámites configurados."}

    nombre_norm = normalizar_texto(tramite_nombre)

    for key, info in tramites.items():
        if nombre_norm in normalizar_texto(key):
            return {
                "contenido": info.get("descripcion", ""),
                "botones": info.get("botones", []),
            }
        for boton in info.get("botones", []):
            if nombre_norm in normalizar_texto(boton.get("texto", "")):
                return {
                    "contenido": info.get("descripcion", ""),
                    "botones": info.get("botones", []),
                }

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
    ESPERANDO_NUEVO_DATO_USUARIO = auto()
    ESPERANDO_CONFIRMACION_DATOS_RECLAMO = auto()
    ESPERANDO_CORRECCION_DATOS_RECLAMO = auto()
    ESPERANDO_SELECCION_CONTACTO_CATEGORIA = auto()
    ESPERANDO_INTENCION_UBICACION = auto()

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

def _get_main_menu_payload(context: dict, welcome_message_override: str = None) -> dict:
    """
    Generates the main menu payload with the new, structured layout.
    """
    viewer_user = context.get("viewer_user_obj")
    profile_name = context.get("profile_name")

    user_name = None
    if isinstance(profile_name, str) and profile_name.strip():
        user_name = profile_name.strip()
    elif viewer_user:
        user_name = getattr(viewer_user, "nombre", None) or getattr(viewer_user, "name", None)

    if welcome_message_override:
        welcome_message = welcome_message_override
    elif user_name:
        welcome_message = (
            f"¡Hola, {user_name}! 👋 Soy JUNI, tu Asistente Virtual de la Municipalidad de Junín.\n\n"
            "¿Cómo te puedo ayudar hoy?"
        )
    else:
        welcome_message = (
            "¡Hola! 👋 Soy JUNI, tu Asistente Virtual de la Municipalidad de Junín.\n\n"
            "¿Cómo te puedo ayudar hoy?"
        )

    # Final Menu Structure (v5)
    categorias = [
        {"titulo": "🗣️ Reclamos y Consultas", "botones": [
            {"texto": "📝 Iniciar un Reclamo", "action_id": "mostrar_menu_reclamos"},
            {"texto": "💡 Enviar una Sugerencia", "action_id": "enviar_sugerencia"},
            {"texto": "🤔 Consultar Estado de Reclamo", "action_id": "consultar_estado_reclamo"},
            {"texto": "📞 Contactos Útiles", "action_id": "contactos_utiles"},
        ]},
        {"titulo": "🚗 Trámites y Turnos", "botones": [
            {"texto": "🚗 Licencia de Conducir", "action_id": "licencia_de_conducir"},
            {"texto": "🗓️ Solicitar Otros Turnos", "action_id": "solicitar_turnos"},
            {"texto": "💵 Pagar Tasas Municipales", "action_id": "pago_de_tasas_vigentes"},
        ]},
        {"titulo": "📰 Información del Municipio", "botones": [
            {"texto": "🎭 Agenda Cultural y Noticias", "action_id": "agenda_y_noticias"},
            {"texto": "🐾 Veterinaria y Bromatología", "action_id": "veterinaria_bromatologia"},
        ]},
        {"titulo": "🅿️ Estacionamiento", "botones": [
             {"texto": "🅿️ Buscar Estacionamiento Libre", "action_id": "buscar_estacionamiento"},
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
        "fuente": "greeting_handler_structured_menu_v2",
        "categorias": categorias,
        "generar_audio": True
    }


class GreetingHandler(BaseMunicipioHandler):
    def handle(self, payload: dict) -> dict | None:
        chat_db_context_data = self.context.get("chat_db_context_data")

        if not chat_db_context_data:
            logger.warning("[GreetingHandler] chat_db_context_data no encontrado. No se puede hacer un reseteo completo.")
            contexto_municipio_actual = {}
        else:
            logger.info("[GreetingHandler] Saludo detectado. Realizando reseteo completo del contexto.")

            # Preserve essential info if it exists
            user_info = chat_db_context_data.get(CONTEXTO_MUNICIPIO, {}).get('user', {})

            # Clear the entire context to prevent stale data from any flow
            chat_db_context_data.clear()

            # Restore essential info into a fresh context
            contexto_municipio_nuevo = chat_db_context_data.setdefault(CONTEXTO_MUNICIPIO, {})
            if user_info:
                contexto_municipio_nuevo['user'] = user_info

            contexto_municipio_actual = contexto_municipio_nuevo

        # Establecer el estado para esperar una selección del menú principal en el próximo turno.
        contexto_municipio_actual['estado_conversacion'] = ConversationState.ESPERANDO_SELECCION_MENU_PRINCIPAL.name
        logger.info(f"[GreetingHandler] Nuevo estado de conversación: {contexto_municipio_actual['estado_conversacion']}")

        # Usar la función centralizada para obtener el payload del menú.
        return _get_main_menu_payload(self.context)


def handle_contactos_utiles_inicio(context, chat_db_context):
    """Handles the initial request for 'Contactos Útiles'."""
    municipio_id = context.get("municipio_id", MUNICIPIO_ID)
    contactos_data = cargar_contactos_utiles(municipio_id)
    categorias = contactos_data.get("categorias", []) if isinstance(contactos_data, dict) else []
    if not categorias:
        return {
            "message_body": "No se encontró información de contactos útiles en este momento.",
            "message_type": "text"
        }

    # Build buttons and map slug -> contactos
    categorias_map = {}
    buttons = []
    for cat in categorias:
        nombre = cat.get("nombre_categoria", "Sin categoría")
        slug = normalizar_texto(nombre).replace(" ", "_")
        categorias_map[slug] = {
            "nombre": nombre,
            "contactos": cat.get("contactos", [])
        }
        buttons.append({"texto": nombre, "action_id": f"select_contact_category_{slug}"})

    contexto_municipio_actual = context.get("chat_db_context_data", {}).setdefault(CONTEXTO_MUNICIPIO, {})
    contexto_municipio_actual['estado_conversacion'] = ConversationState.ESPERANDO_SELECCION_CONTACTO_CATEGORIA.name
    contexto_municipio_actual['contactos_categorias'] = categorias_map
    if chat_db_context:
        flag_modified(chat_db_context, "context_data")

    return {
        "message_body": "Seleccioná una categoría para ver los contactos:",
        "options_list": buttons,
        "message_type": "interactive_buttons",
        "fuente": "contactos_utiles_show_categories"
    }

def _format_post(post: dict, channel: str) -> str:
    """Return a formatted string for a single news/event entry."""
    title = post.get("titulo", "Sin título")
    subtitle = post.get("subtitulo")
    desc = post.get("descripcion", "Sin descripción.")
    link = post.get("enlace") or post.get("url")
    fecha = (
        post.get("fecha_evento")
        or post.get("fecha_inicio")
        or post.get("fecha_publicacion")
    )
    ubicacion = post.get("ubicacion")

    if channel == "whatsapp":
        lines = [f"*{title}*"]
        if subtitle:
            lines.append(f"_{subtitle}_")
        if fecha:
            lines.append(f"📅 {fecha}")
        if ubicacion:
            lines.append(f"📍 {ubicacion}")
        if desc:
            lines.append(desc)
        if link:
            lines.append(link)
        return "\n".join(lines) + "\n\n"

    # Default to web/HTML formatting
    parts = [f"<strong>{title}</strong>"]
    if subtitle:
        parts.append(f"<em>{subtitle}</em>")
    if fecha:
        parts.append(f"📅 {fecha}")
    if ubicacion:
        parts.append(f"📍 {ubicacion}")
    if desc:
        parts.append(desc)
    if link:
        parts.append(f'<a href="{link}" target="_blank">{link}</a>')
    return "<br>".join(parts) + "<br><br>"


def _format_contact(contact: dict, channel: str) -> str:
    """Formatea un contacto individual según el canal."""
    nombre = contact.get("nombre", "Sin nombre")
    descripcion = contact.get("descripcion")
    telefono = contact.get("telefono")
    url = contact.get("url")
    horario = contact.get("horario")
    lines = []
    if channel == "whatsapp":
        lines.append(f"*{nombre}*")
        if descripcion:
            lines.append(descripcion)
        if horario:
            lines.append(f"🕑 {horario}")
        if telefono:
            digits = re.sub(r"\D", "", telefono)
            lines.append(f"📞 {telefono}")
            lines.append(f"👉 https://wa.me/{digits}")
        if url:
            lines.append(f"🔗 {url}")
        lines.append("")
        return "\n".join(lines)
    else:
        lines.append(f"<strong>{nombre}</strong>")
        if descripcion:
            lines.append(descripcion)
        if horario:
            lines.append(f"<em>{horario}</em>")
        if telefono:
            digits = re.sub(r"\D", "", telefono)
            lines.append(f'Tel: <a href="tel:{digits}">{telefono}</a>')
            lines.append(f'WhatsApp: <a href="https://wa.me/{digits}" target="_blank">{telefono}</a>')
        if url:
            lines.append(f'<a href="{url}" target="_blank">Más información</a>')
        lines.append("<br>")
        return "<br>".join(lines)


def _get_posts_from_json(content_type: str, channel: str, municipio_id: str) -> str:
    """Helper to get formatted posts of a specific type from the JSON file."""
    all_posts_data = cargar_agenda_cultural(municipio_id)
    all_posts = all_posts_data.get("eventos", [])

    if not all_posts:
        return ""

    posts = [p for p in all_posts if p.get("tipo_post") == content_type]
    posts.sort(key=lambda x: x.get("fecha_publicacion", ""), reverse=True)

    if not posts:
        return ""

    message_body = "".join(_format_post(p, channel) for p in posts[:3])
    return message_body

def handle_main_menu_action(action_id: str, context: dict, chat_db_context) -> dict:
    """
    Handles actions from the new categorized main menu.
    """
    # --- Aliases for new action_ids to reuse existing logic ---
    if action_id == "veterinaria_bromatologia":
        action_id = "zoonosis" # Re-route to existing logic
    if action_id == "buscar_estacionamiento":
        action_id = "estacionamiento" # Re-route to existing logic

    # --- Handlers for New/Modified Menu Options ---
    if action_id == "contactos_utiles":
        return handle_contactos_utiles_inicio(context, chat_db_context)

    if action_id == "mostrar_menu_reclamos":
        contexto_municipio_actual = context.get("chat_db_context_data", {}).setdefault(CONTEXTO_MUNICIPIO, {})
        logger.info("[MENU_ACTION] Clearing previous claim context for new claim.")
        contexto_municipio_actual.pop("datos_parciales_llm_reclamo", None)
        contexto_municipio_actual.pop("historial_llm_reclamo", None)
        return _get_reclamos_menu()

    if action_id == "consultar_estado_reclamo":
        contexto_municipio_actual = context.get("chat_db_context_data", {}).setdefault(CONTEXTO_MUNICIPIO, {})
        contexto_municipio_actual['estado_conversacion'] = ConversationState.ESPERANDO_NUMERO_TICKET.name
        if chat_db_context:
            flag_modified(chat_db_context, "context_data")
        return {
            "message_body": "Por favor, ingresá el número de tu reclamo para consultar el estado.",
            "message_type": "text",
            "fuente": "handler_consultar_reclamo"
        }

    if action_id == "enviar_sugerencia":
        contexto_municipio_actual = context.get("chat_db_context_data", {}).setdefault(CONTEXTO_MUNICIPIO, {})
        contexto_municipio_actual['estado_conversacion'] = ConversationState.ESPERANDO_TEXTO_SUGERENCIA.name
        if chat_db_context:
            flag_modified(chat_db_context, "context_data")
        return {
            "message_body": "¡Gracias por tu iniciativa! Por favor, escribí tu sugerencia o propuesta a continuación.",
            "message_type": "text",
            "fuente": "handler_enviar_sugerencia"
        }

    if action_id == "agenda_y_noticias":
        channel = context.get("channel", "web")
        municipio_id = context.get("municipio_id", MUNICIPIO_ID)
        noticias_body = _get_posts_from_json("noticia", channel, municipio_id)
        eventos_body = _get_posts_from_json("evento", channel, municipio_id)

        full_body = ""
        if noticias_body:
            if channel == "whatsapp":
                full_body += "*🗞️ Noticias Recientes*\n" + noticias_body
            else:
                full_body += "<h3>🗞️ Noticias Recientes</h3>" + noticias_body
        if eventos_body:
            if channel == "whatsapp":
                full_body += "*🎭 Próximos Eventos*\n" + eventos_body
            else:
                full_body += "<h3>🎭 Próximos Eventos</h3>" + eventos_body

        if not full_body:
            full_body = "No hay noticias ni eventos para mostrar en este momento."
        else:
            if channel == "whatsapp":
                social_links = (
                    "\n---\n"
                    "Seguinos en nuestras redes:\n"
                    "📘 Facebook: https://www.facebook.com/municipalidaddejunin\n"
                    "📸 Instagram: https://www.instagram.com/municipalidaddejunin"
                )
            else:
                social_links = (
                    "<hr>Seguinos en nuestras redes:<br>"
                    '<a href="https://www.facebook.com/municipalidaddejunin" target="_blank">📘 Facebook</a><br>'
                    '<a href="https://www.instagram.com/municipalidaddejunin" target="_blank">📸 Instagram</a>'
                )
            full_body += social_links

        return {
            "message_body": full_body.strip(),
            "message_type": "text",
            "fuente": "handler_agenda_y_noticias",
        }

    if action_id == "web_municipio":
        website_url = context.get("municipio_config_actual", {}).get("website_url", "https://www.juninmendoza.gov.ar/")
        return {
            "message_body": f"Podés encontrar toda la información oficial en nuestro sitio web.",
            "options_list": [{"texto": "Visitar Sitio Web", "url": website_url, "type": "url"}],
            "message_type": "interactive_buttons",
            "fuente": "handler_web_municipio"
        }

    # --- Handlers for existing options that are kept ---
    tramites_info = get_tramites_info(context.get("municipio_id", MUNICIPIO_ID))
    if action_id in tramites_info:
        data = tramites_info[action_id] or {}
        botones = data.get("botones", [])
        for btn in botones:
            if btn.get("url") and not btn.get("type"):
                btn["type"] = "url"
        return {
            "message_body": data.get("descripcion", ""),
            "options_list": botones,
            "message_type": "interactive_buttons" if botones else "text",
            "fuente": f"info_{action_id}_json",
        }

    if action_id == "estacionamiento":
        contexto_municipio_actual = context.get("chat_db_context_data", {}).setdefault(CONTEXTO_MUNICIPIO, {})
        contexto_municipio_actual['estado_conversacion'] = ConversationState.ESPERANDO_UBICACION_GENERAL.name
        contexto_municipio_actual['consulta_pendiente_ubicacion'] = 'estacionamiento'
        if chat_db_context:
            flag_modified(chat_db_context, "context_data")
        return {
            "message_body": "Para encontrar estacionamiento libre, por favor compartí tu ubicación o escribí una dirección.",
            "options_list": [{"texto": "Compartir ubicación", "action": "compartir_ubicacion"}, {"texto": "Cancelar", "action": "cancelar"}],
            "message_type": "interactive_buttons",
            "fuente": "pedir_ubicacion_estacionamiento"
        }

    if action_id == "solicitar_turnos":
        return {
            "message_body": "📅 Para solicitar turnos online, por favor ingresá al siguiente enlace:",
            "options_list": [{"texto": "Solicitar Turno", "url": "https://www.juninmendoza.gov.ar/turnos-online/", "type": "url"}],
            "message_type": "interactive_buttons",
            "fuente": "info_solicitar_turnos_direct_link"
        }

    if action_id == "zoonosis": # Handles the 'veterinaria_bromatologia' alias
        contactos_info = cargar_configuracion_municipio(context.get("municipio_id", MUNICIPIO_ID), "contactos_especializados.json")
        contacto_data = contactos_info.get("Veterinaria y Bromatologia", {})
        if not contacto_data:
            return {"message_body": "No se encontró la información de contacto en este momento.", "message_type": "text"}

        nombre = contacto_data.get("nombre")
        telefono = contacto_data.get("telefono")
        horario = contacto_data.get("horario")

        message_body = f"🐾 *Información de Veterinaria y Bromatología*\n\n"
        if nombre:
            message_body += f"Encargado/a: *{nombre}*\n"
        if telefono:
            link_whatsapp = f"https://wa.me/{''.join(filter(str.isdigit, telefono))}"
            message_body += f"Teléfono: *{telefono}* (WhatsApp: {link_whatsapp})\n"
        if horario:
            message_body += f"Horario de atención: *{horario}*\n"

        botones = []
        if telefono:
            link_whatsapp = f"https://wa.me/{''.join(filter(str.isdigit, telefono))}"
            botones.append({"texto": "Contactar por WhatsApp", "url": link_whatsapp, "type": "url"})

        return {
            "message_body": message_body.strip(),
            "options_list": botones,
            "message_type": "interactive_buttons" if botones else "text",
            "fuente": "info_veterinaria_json"
        }

    # Fallback for any other action that is not explicitly handled above
    return {
        "message_body": "Esta función no está implementada en este momento. Por favor, intentá con otra opción.",
        "options_list": [],
        "message_type": "text",
        "fuente": f"unimplemented_{action_id}"
    }


def handle_info_requests(action_id: str) -> dict:
    """
    Handles simple informational requests based on action IDs from buttons.
    """
    tramites_info = get_tramites_info(context.get("municipio_id", MUNICIPIO_ID))
    contactos_info = cargar_configuracion_municipio(context.get("municipio_id", MUNICIPIO_ID), "contactos_especializados.json")

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


def handle_llm_interaction(app, pregunta_str, context, viewer_user, owner_user, chat_db_context, contexto_municipio_actual):
    logger_actual = app.logger if app else (current_app.logger if has_app_context() else logging.getLogger(__name__))
    datos_actuales = {} # Initialize to prevent UnboundLocalError

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

    if estado_conversacion_para_llm in [
        ConversationState.ESPERANDO_INFO_RECLAMO_LLM.name,
        ConversationState.CONVERSACION_GENERAL_LLM.name,
        ConversationState.ESPERANDO_CONFIRMACION_RECLAMO.name # Add this state to the LLM-handled states
    ]:
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
        # FIX: Pre-process expected data to prevent state loss if LLM fails to return it
        campo_esperado = contexto_municipio_actual.get("esperando_info_llm_reclamo")
        if context.get("es_ubicacion") and context.get("ubicacion_usuario"):
             campo_esperado = "ubicacion"

        if campo_esperado and (pregunta_str or context.get("es_ubicacion")):
            logger_actual.info(f"Guardando dato esperado '{campo_esperado}' en el contexto antes de llamar al LLM.")
            datos_parciales = contexto_municipio_actual.setdefault("datos_parciales_llm_reclamo", {})

            valor_a_guardar = None
            if campo_esperado == "ubicacion":
                if context.get("ubicacion_usuario"):
                    lat = context["ubicacion_usuario"].get("latitude")
                    lon = context["ubicacion_usuario"].get("longitude")
                    address = context["ubicacion_usuario"].get("address")
                    valor_a_guardar = address if address else f"Lat: {lat}, Lon: {lon}"
                else:
                    valor_a_guardar = pregunta_str
            else:
                valor_a_guardar = pregunta_str

            datos_parciales[campo_esperado] = valor_a_guardar
            logger_actual.info(f"Datos parciales actualizados: {datos_parciales}")
            contexto_municipio_actual.pop("esperando_info_llm_reclamo", None)


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
            respuesta_llm_dict, context_dict = llamar_gemini(
                app=app,
                mensaje_usuario=mensaje_para_gemini,
                usuario=usuario_info_llm,
                historial=historial_para_llm,
                chat_session_id=context.get("chat_session_uuid")
            )
            logger.info(f"[HANDLE_LLM] Respuesta LLM: {respuesta_llm_dict}")
            if isinstance(context_dict, dict) and chat_db_context:
                chat_db_context.context_data.update(context_dict)
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

        if not respuesta_usuario_llm and accion_backend_llm not in ["crear_reclamo", "ejecutar_herramienta"]:
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
            # Si es el inicio de un nuevo reclamo, limpiar el contexto anterior para evitar "context bleed".
            if contexto_municipio_actual.get("estado_conversacion") != ConversationState.ESPERANDO_INFO_RECLAMO_LLM.name:
                logger_actual.info("[CONTEXT_RESET] Nuevo reclamo detectado. Limpiando historiales de conversación.")
                # Eliminar el historial de la conversación general anterior.
                contexto_municipio_actual.pop("historial_conversacion_general_llm", None)
                # Reiniciar el contexto específico del reclamo.
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
                # If the LLM thinks it has all the data, call the handler to validate.
                # The handler is the source of truth. Its response will be used.
                # This prevents a premature confirmation from the LLM being shown
                # if the handler then asks for more information.
                logger_actual.info("[HANDLE_LLM] LLM provided all data. Executing CrearReclamoActionHandler for validation and creation.")
                handler = CrearReclamoActionHandler(context)
                handler_response = handler.execute(datos_actuales)

                # The handler's response is the final one, whether it's a success message
                # or a request for more info. We return it directly, ignoring the LLM's
                # potentially premature confirmation message.
                return handler_response, contexto_municipio_actual
            else:
                contexto_municipio_actual["estado_conversacion"] = ConversationState.ESPERANDO_INFO_RECLAMO_LLM.name
                contexto_municipio_actual["esperando_info_llm_reclamo"] = pedir_info_llm
                # Update the context that will be passed to the next turn
                if chat_db_context and hasattr(chat_db_context, 'context_data'):
                    chat_db_context.context_data[CONTEXTO_MUNICIPIO] = contexto_municipio_actual
                    flag_modified(chat_db_context, "context_data")
                return {"message_body": respuesta_usuario_llm, "options_list": botones_llm, "message_type": "interactive_buttons" if botones_llm else "text", "fuente": "llm_pide_info_reclamo"}, contexto_municipio_actual
        elif accion_backend_llm == "mostrar_menu":
            logger.info("[HANDLE_LLM] LLM solicitó mostrar el menú principal.")
            contexto_municipio_actual["estado_conversacion"] = ConversationState.ESPERANDO_SELECCION_MENU_PRINCIPAL.name
            if chat_db_context and hasattr(chat_db_context, "context_data"):
                chat_db_context.context_data[CONTEXTO_MUNICIPIO] = contexto_municipio_actual
                flag_modified(chat_db_context, "context_data")
            return _get_main_menu_payload(context), contexto_municipio_actual
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

        elif accion_backend_llm == "pedir_dato_usuario":
            campo_a_pedir = pedir_info_llm[0] if isinstance(pedir_info_llm, list) and pedir_info_llm else None
            if not campo_a_pedir:
                logger.warning("[HANDLE_LLM] 'pedir_dato_usuario' action received without 'pedir_info'.")
                return None, contexto_municipio_actual

            contexto_municipio_actual['estado_conversacion'] = ConversationState.ESPERANDO_NUEVO_DATO_USUARIO.name
            contexto_municipio_actual['campo_a_actualizar'] = campo_a_pedir

            # Guarda la pregunta original del usuario que inició este flujo para poder reanudarla.
            if historial_para_llm:
                # El historial se pasa como ['pregunta_usuario', 'respuesta_ia', ...], tomamos la última pregunta.
                contexto_municipio_actual['accion_original_para_reintentar'] = historial_para_llm[-2] if len(historial_para_llm) > 1 else pregunta_str

            return {
                "message_body": respuesta_usuario_llm,
                "options_list": botones_llm,
                "message_type": "interactive_buttons" if botones_llm else "text",
                "fuente": "llm_pide_dato_usuario"
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
                    # Friendly message for the user, more specific than a generic error.
                    user_friendly_tool_name = nombre_herramienta.replace("_", " ").replace("consultar", "la consulta de").replace("buscar", "la búsqueda de")

                    return {
                        "message_body": f"Lo siento, tuve un problema con {user_friendly_tool_name}. Por favor, intenta de nuevo en unos momentos.",
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

            # FIX: Si el LLM extrae datos (ej. ubicación), guardarlos en el contexto del reclamo
            # aunque la acción principal sea solo responder. Esto evita perder información.
            if datos_estructura_llm:
                datos_actuales = contexto_municipio_actual.setdefault("datos_parciales_llm_reclamo", {})
                nuevos_datos = {k: v for k, v in datos_estructura_llm.items() if v is not None}
                if nuevos_datos:
                    datos_actuales.update(nuevos_datos)
                    logger_actual.info(f"[CONTEXT_MERGE] Datos parciales de reclamo actualizados en flujo 'responder_directamente': {datos_actuales}")

            contexto_municipio_actual.setdefault("historial_conversacion_general_llm", []).append(nuevo_turno_historial)
            contexto_municipio_actual["estado_conversacion"] = ConversationState.CONVERSACION_GENERAL_LLM.name
            response_payload = {
                "message_body": respuesta_usuario_llm,
                "options_list": botones_llm,
                "message_type": "interactive_buttons" if botones_llm else "text",
                "accion_backend": accion_backend_llm,
                "datos_estructura": datos_estructura_llm,
                "pedir_info": pedir_info_llm,
                "fuente": "llm_respuesta_directa"
            }
            if respuesta_llm_dict.get("generar_audio"):
                response_payload["generar_audio"] = True
            return response_payload, contexto_municipio_actual

        elif estado_conversacion_para_llm == ConversationState.ESPERANDO_CONFIRMACION_RECLAMO.name:
            # User is at the confirmation step. Their response is either a "yes" or a correction.
            _, es_confirmacion = extract_description_and_check_confirmation(pregunta_str, PALABRAS_CLAVE_CONFIRMACION)

            if es_confirmacion:
                # User confirmed. Proceed to create the ticket.
                logger_actual.info("[HANDLE_LLM_CONFIRM] Confirmación detectada. Procediendo a crear ticket.")
                return _handle_ticket_creation(contexto_municipio_actual, context, {})
            else:
                # User sent a correction. Extract new data, merge, and re-confirm.
                logger_actual.info("[HANDLE_LLM_CONFIRM] No es confirmación, asumiendo corrección.")
                datos_nuevos = extract_multiple_contact_details_llm(pregunta_str)
                datos_actuales = contexto_municipio_actual.get("datos_parciales_llm_reclamo", {})
                datos_actuales.update(datos_nuevos)
                contexto_municipio_actual["datos_parciales_llm_reclamo"] = datos_actuales

                # Re-prompt for confirmation with updated data
                # This part needs to be improved to show the data again. For now, a generic message.
                # A better implementation would call a function to format the confirmation message.
                return {"message_body": "OK, he actualizado tus datos. ¿Son correctos ahora?", "options_list": botones_llm, "message_type": "text", "fuente": "llm_re_pide_confirmacion"}, contexto_municipio_actual

        else: # Generic flow continuation
            if estado_conversacion_para_llm == ConversationState.ESPERANDO_INFO_RECLAMO_LLM.name and datos_estructura_llm:
                contexto_municipio_actual.setdefault("historial_llm_reclamo", []).append(nuevo_turno_historial)
                datos_actuales = contexto_municipio_actual.get("datos_parciales_llm_reclamo", {})
                nuevos_datos = {k: v for k, v in datos_estructura_llm.items() if v is not None}
                datos_actuales.update(nuevos_datos)
                contexto_municipio_actual["datos_parciales_llm_reclamo"] = datos_actuales
            else:
                contexto_municipio_actual.setdefault("historial_conversacion_general_llm", []).append(nuevo_turno_historial)

            # State transition logic based on 'pedir_info'
            if pedir_info_llm:
                next_state_obj = PEDIR_INFO_TO_STATE.get(pedir_info_llm)
                if next_state_obj:
                    contexto_municipio_actual["estado_conversacion"] = next_state_obj.name
                else:
                    # Fallback if a new 'pedir_info' value isn't in our map
                    contexto_municipio_actual["estado_conversacion"] = ConversationState.ESPERANDO_INFO_RECLAMO_LLM.name
                contexto_municipio_actual["esperando_info_llm"] = pedir_info_llm
            else:
                # If no more info is needed, decide what to do
                if estado_conversacion_para_llm == ConversationState.ESPERANDO_INFO_RECLAMO_LLM.name:
                    # If we were in a claim flow, it's time to create the ticket
                    return _handle_ticket_creation(contexto_municipio_actual, context, datos_actuales)
                else:
                    contexto_municipio_actual["estado_conversacion"] = ConversationState.CONVERSACION_GENERAL_LLM.name
                    contexto_municipio_actual.pop("esperando_info_llm", None)

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
    # Reclamos, Trámites y Turnos
    "mostrar_menu_reclamos": ["reclamo", "reclamos", "iniciar reclamo", "denuncia", "problema", "queja", "reportar"],
    "solicitar_turnos": ["turnos", "turno", "solicitar turno", "pedir turno", "turnos online"],
    "licencia_de_conducir": ["licencia", "conducir", "carnet", "registro", "renovar licencia", "sacar licencia"],
    "enviar_sugerencia": ["sugerencia", "sugerir", "propuesta", "pedido", "pedir algo"],
    "consultar_estado_reclamo": ["consultar reclamo", "estado reclamo", "seguimiento", "ver reclamo"],

    # Información útil
    "contactos_utiles": ["contactos", "contacto", "telefonos", "telefono", "utiles", "directorio", "llamar"],
    "agenda_y_noticias": ["agenda", "cultural", "eventos", "noticias", "novedades", "informacion", "actividades"],
    "veterinaria_bromatologia": ["veterinaria", "bromatologia", "zoonosis", "animales", "perro", "gato", "mascotas"],
    "defensa_del_consumidor": ["defensa del consumidor", "consumidor", "consumo", "proteccion al consumidor"],

    # Tasas y Servicios
    "pago_de_tasas_vigentes": ["pagar", "pago", "tasas", "tasa", "boleta", "impuestos", "municipal"],
    "buscar_estacionamiento": ["estacionamiento", "estacionar", "aparcamiento", "parking", "estacionar auto"],
    "recoleccion_residuos": ["recoleccion", "residuos", "basura", "basurero", "cuando pasa el camion", "recolector"]
}

from fuzzywuzzy import process

def find_menu_action_by_input(user_input: str, menu_buttons: list) -> str | None:
    """
    Finds a menu action based on user input, checking for exact match, number, first letter, or keywords.
    """
    if not user_input or not menu_buttons:
        return None

    # Use a more aggressive normalization for matching to handle emojis, etc.
    normalized_input = _super_normalize(user_input)

    # 1. Check for exact match on super-normalized button text
    for button in menu_buttons:
        button_text_super_norm = _super_normalize(button.get("texto", ""))
        if button_text_super_norm and button_text_super_norm == normalized_input:
            logger.info(f"DEBUG: Super-normalized exact match found for '{normalized_input}'. Action: {button.get('action_id')}")
            return button.get('action_id')

    # Fallback to standard normalization if super-norm fails (e.g. numeric input)
    normalized_input = normalizar_texto(user_input.strip())

    # 2. Check for numeric selection
    try:
        selection_index = int(normalized_input) - 1
        if 0 <= selection_index < len(menu_buttons):
            return menu_buttons[selection_index].get('action_id')
    except (ValueError, IndexError):
        pass  # Not a valid number or index, proceed to other checks

    # 3. Check for first letter match (only if input is a single character)
    if len(normalized_input) == 1:
        for button in menu_buttons:
            button_text_norm = normalizar_texto(button.get("texto", ""))
            if button_text_norm.startswith(normalized_input):
                return button.get("action_id")

    # 4. Check for keyword match (fuzzy matching for natural language)
    local_keywords = {}
    for button in menu_buttons:
        action_id = button.get('action_id')
        if action_id in MENU_KEYWORDS:
            for keyword in MENU_KEYWORDS[action_id]:
                local_keywords[keyword] = action_id

    if local_keywords:
        best_match, score = process.extractOne(normalized_input, local_keywords.keys())

        if score > 80:
            # Added log for debugging
            logger.info(f"DEBUG: Fuzzy match found for '{normalized_input}' with keyword '{best_match}' (score: {score}). Action: {local_keywords[best_match]}")
            return local_keywords[best_match]

    # Added log for debugging
    logger.warning(f"DEBUG: No menu action found for input: '{user_input}' (normalized: '{normalized_input}')")
    return None

RECLAMO_KEYWORDS = {
    "Luminaria": ["luminaria", "luz", "poste", "foco"],
    "Arbolado": ["arbolado", "arbol", "arboles", "rama", "ramas"],
    "Limpieza y riego": ["limpieza", "riego", "basura", "basural", "contenedor"],
    "Arreglo de calle": ["calle", "bache", "pozo", "asfalto", "vereda", "agujero", "hueco"],
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

    # 3. Check for keyword match within the input
    for category, keywords in RECLAMO_KEYWORDS.items():
        for keyword in keywords:
            if keyword in normalized_input:
                return category

    # 4. Fallback to fuzzy matching if no direct keyword was found
    all_keywords = {
        keyword: category
        for category, keywords in RECLAMO_KEYWORDS.items()
        for keyword in keywords
    }
    best_match, score = process.extractOne(normalized_input, all_keywords.keys())
    if score > 80:
        return all_keywords[best_match]

    return None

def _get_reclamos_menu():
    """Devuelve la estructura del menú de reclamos estandarizado, con íconos y negritas."""
    opciones = [
        {"texto": "*Volver al inicio*", "id_accion": "0", "category_name": "Volver al inicio"},
        {"texto": "💡 *Luminaria*", "id_accion": "1", "category_name": "Luminaria"},
        {"texto": "🌳 *Arbolado*", "id_accion": "2", "category_name": "Arbolado"},
        {"texto": "🗑️ *Limpieza y riego*", "id_accion": "3", "category_name": "Limpieza y riego"},
        {"texto": "🚧 *Arreglo de calle*", "id_accion": "4", "category_name": "Arreglo de calle"},
        {"texto": "💧 *Pérdida de agua*", "id_accion": "5", "category_name": "Pérdida de agua"},
        {"texto": "⚫ *Otros*", "id_accion": "6", "category_name": "Otros"},
    ]
    # El cuerpo del mensaje ahora instruye al usuario que puede responder con un número o seleccionar una opción.
    return {
        "message_body": "Elegí una opción para tu reclamo:",
        "message_type": "interactive_buttons",
        "options_list": opciones,
        "fuente": "submenu_reclamos_estandar_v4",
        "generar_audio": True
    }


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
    final_municipio_config = CONFIG_MUNICIPIO  # Default global
    owner_user_municipio_id_str = str(owner_user.municipio_id) if owner_user and hasattr(owner_user, 'municipio_id') and owner_user.municipio_id else MUNICIPIO_ID
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

    chat_db_context_live_data = {}
    if chat_db_context and chat_db_context.context_data is not None:
        chat_db_context_live_data = chat_db_context.context_data

    # Crear el diccionario de contexto principal una sola vez
    context = {
        "user_obj": owner_user,
        "viewer_user_obj": viewer_user,
        "cliente_id": getattr(viewer_user, "id", None),
        "anon_id": anon_id,
        "rubro_obj": rubro_obj,
        "channel": channel,
        "municipio_config_actual": final_municipio_config,
        "municipio_id": owner_user_municipio_id_str,
        "chat_session_uuid": kwargs.get("chat_session_uuid"),
        "chat_db_context_data": chat_db_context_live_data, # Usar el dict vivo
        "intencion": kwargs.get("intencion"),
        "ubicacion_usuario": location or received_payload.get("ubicacion_usuario"),
        "es_foto": received_payload.get("es_foto", False),
        "foto_url": received_payload.get("foto_url"),
        "es_ubicacion": received_payload.get("es_ubicacion", False),
        "es_archivo": received_payload.get("es_archivo", False),
        "action": received_payload.get("action"),
        "datos_interpretados_archivo": kwargs.get("datos_interpretados_archivo"),
        "archivo_id_para_asociar": kwargs.get("archivo_id_para_asociar"),
    }
    # --- FIN REFACTOR ---

    # --- INICIO: Análisis de Imágenes Multimodal ---
    if received_payload.get("es_foto") and received_payload.get("foto_url"):
        logger_actual.info(f"Image received. Starting multimodal analysis for URL: {received_payload.get('foto_url')}")

        # Define a detailed prompt for the vision model
        vision_prompt = """
        Analiza la siguiente imagen desde la perspectiva de un asistente municipal. Tu objetivo es identificar el problema principal y clasificarlo.
        Responde SÓLO con un objeto JSON con la siguiente estructura:
        {
          "intent": "crear_reclamo",
          "data": {
            "categoria": "Una de las siguientes: Luminaria, Arbolado, Limpieza y riego, Arreglo de calle, Pérdida de agua, Otros",
            "descripcion": "Una descripción breve y clara del problema que se ve en la imagen."
          }
        }
        Si no puedes identificar un problema claro o la imagen no es relevante para un reclamo municipal, devuelve un JSON con "intent": "invalido".
        """

        analysis_result = analizar_imagen_con_fallback(received_payload.get("foto_url"), vision_prompt)

        if analysis_result and analysis_result.get("raw_response"):
            try:
                parsed_response = json.loads(analysis_result.get("raw_response"))
                if parsed_response.get("intent") == "crear_reclamo":
                    logger_actual.info(f"Multimodal analysis successful. Intent: 'crear_reclamo'. Data: {parsed_response.get('data')}")

                    datos_iniciales = parsed_response.get("data", {})
                    datos_iniciales['origen_descripcion'] = 'imagen' # Add origin marker

                    handler = ReclamoFlowHandler(context, chat_db_context)
                    response_dict = handler.start_flow(datos_iniciales=datos_iniciales)

                    if chat_db_context:
                        flag_modified(chat_db_context, "context_data")

                    return _finalize_response(response_dict)

            except json.JSONDecodeError:
                logger_actual.error(f"Failed to parse JSON from vision model response: {analysis_result.get('raw_response')}")
    # --- FIN: Análisis de Imágenes Multimodal ---

    # --- INICIO: Manejo Proactivo de Ubicación ---
    if received_payload.get("es_ubicacion") and not pregunta_str.strip():
        logger_actual.info(f"Location received without text. Starting proactive location handling.")

        address = received_payload.get("ubicacion_usuario", {}).get("address", "la ubicación que compartiste")

        contexto_municipio_actual = context.get("chat_db_context_data", {}).setdefault(CONTEXTO_MUNICIPIO, {})
        contexto_municipio_actual['estado_conversacion'] = ConversationState.ESPERANDO_INTENCION_UBICACION.name
        contexto_municipio_actual['ubicacion_contextual'] = received_payload.get("ubicacion_usuario")
        if chat_db_context:
            flag_modified(chat_db_context, "context_data")

        return _finalize_response({
            "message_body": f"Recibí tu ubicación en *{address}*. ¿Qué te gustaría hacer?",
            "options_list": [
                {"texto": "Iniciar un Reclamo", "action_id": "iniciar_reclamo_con_ubicacion"},
                {"texto": "Enviar una Sugerencia", "action_id": "enviar_sugerencia_con_ubicacion"},
                {"texto": "Cancelar", "action_id": "cancelar"}
            ],
            "message_type": "interactive_buttons",
            "fuente": "proactive_location_handler"
        })
    # --- FIN: Manejo Proactivo de Ubicación ---


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
    # --- INICIO: Manejo del Flujo de Reclamos Activo ---
    if "reclamo_flow_v2" in contexto_municipio_actual and contexto_municipio_actual["reclamo_flow_v2"].get("state"):
        logger_actual.info(f"Reclamo flow is active. State: {contexto_municipio_actual['reclamo_flow_v2'].get('state')}. Handing off to ReclamoFlowHandler.")
        handler = ReclamoFlowHandler(context, chat_db_context)
        response = handler.handle(pregunta_str, received_payload)
        if chat_db_context:
            flag_modified(chat_db_context, "context_data")
        return _finalize_response(response)
    # --- FIN: Manejo del Flujo de Reclamos Activo ---

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
            logger_actual.info("Claim initiation intent detected. Bypassing LLM and showing reclamos menu.")
            contexto_municipio_actual = context.get("chat_db_context_data", {}).setdefault(CONTEXTO_MUNICIPIO, {})
            contexto_municipio_actual['estado_conversacion'] = ConversationState.ESPERANDO_SELECCION_MENU_RECLAMOS.name
            if chat_db_context:
                flag_modified(chat_db_context, "context_data")
            return _finalize_response(_get_reclamos_menu())

        if intent == "consultar_reclamo":
            logger_actual.info("Claim status check intent detected. Bypassing LLM.")
            return _finalize_response({
                "message_body": "Para consultar el estado de tu reclamo, por favor ingresá el número de ticket.",
                "fuente": "intent_consultar_reclamo"
            })
        # --- END INTENT CLASSIFICATION ---


    # El manejo de reseteo por palabra clave ahora es manejado por el LLM
    # que debe devolver accion_backend: "saludar".

    # --- RESTRUCTURED LOGIC ---
    # Obtener el estado actual de la conversación antes de evaluar acciones
    estado_conversacion = contexto_municipio_actual.get("estado_conversacion")
    action = received_payload.get("action")

    # 1. Handle active conversation states first.
    if estado_conversacion:
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

        elif estado_conversacion == ConversationState.ESPERANDO_SELECCION_MENU_PRINCIPAL.name:
            pregunta_str_menu = ""
            if isinstance(pregunta_original, str):
                pregunta_str_menu = pregunta_original
            elif isinstance(pregunta_original, dict) and "pregunta" in pregunta_original:
                pregunta_str_menu = pregunta_original["pregunta"]

            selected_action = action or find_menu_action_by_input(pregunta_str_menu, _get_main_menu_payload(context).get('options_list', []))

            if selected_action:
                contexto_municipio_actual['estado_conversacion'] = None
                if chat_db_context: flag_modified(chat_db_context, "context_data")
                response = handle_main_menu_action(selected_action, context, chat_db_context)
                if response:
                    return _finalize_response(response)
            else:
                logger_actual.info(f"Input '{pregunta_str_menu}' is not a menu option. Treating as a general query.")
                contexto_municipio_actual['estado_conversacion'] = None
                if chat_db_context: flag_modified(chat_db_context, "context_data")

        elif estado_conversacion == ConversationState.ESPERANDO_SELECCION_MENU_RECLAMOS.name:
            pregunta_str_reclamo = ""
            if isinstance(pregunta_original, str):
                pregunta_str_reclamo = pregunta_original
            elif isinstance(pregunta_original, dict) and "pregunta" in pregunta_original:
                pregunta_str_reclamo = pregunta_original["pregunta"]

            logger_actual.info(f"Handling input in ESPERANDO_SELECCION_MENU_RECLAMOS state. Input: '{pregunta_str_reclamo}', Action: '{action}'")

            reclamo_categories = {
                "reclamo_luminaria": "Luminaria", "reclamo_arbolado": "Arbolado",
                "reclamo_limpieza_riego": "Limpieza y riego", "reclamo_arreglo_calle": "Arreglo de calle",
                "reclamo_otros": "Otros"
            }

            selected_category_name = None
            if action in reclamo_categories:
                selected_category_name = reclamo_categories[action]
            else:
                normalized_input = normalizar_texto(pregunta_str_reclamo or "")
                if pregunta_str_reclamo == "0" or normalized_input in RETURN_TO_MAIN_MENU:
                    return _finalize_response(GreetingHandler(context).handle({}))

                reclamo_options = _get_reclamos_menu().get("options_list", [])
                if pregunta_str_reclamo.isdigit():
                    for option in reclamo_options:
                        if option.get("id_accion") == pregunta_str_reclamo:
                            selected_category_name = option.get("category_name")
                            break
                if not selected_category_name:
                    plain_text_options = [{"texto": opt.get("category_name")} for opt in reclamo_options]
                    selected_category_name = find_reclamo_category_by_input(pregunta_str_reclamo, plain_text_options)

            if selected_category_name:
                handler = ReclamoFlowHandler(context, chat_db_context)
                response_dict = handler.start_flow(categoria_inicial=selected_category_name)
                if chat_db_context: flag_modified(chat_db_context, "context_data")
                return _finalize_response(response_dict)
            else:
                return _finalize_response(_get_reclamos_menu())

        elif estado_conversacion == ConversationState.ESPERANDO_INTENCION_UBICACION.name:
            ubicacion_contextual = contexto_municipio_actual.pop('ubicacion_contextual', None)
            address = ubicacion_contextual.get('address', 'la ubicación proporcionada') if ubicacion_contextual else 'la ubicación proporcionada'

            if action == "iniciar_reclamo_con_ubicacion":
                handler = ReclamoFlowHandler(context, chat_db_context)
                datos_iniciales = {"direccion": address}
                if ubicacion_contextual:
                    datos_iniciales['coordenadas'] = {"lat": ubicacion_contextual.get("latitude"), "lon": ubicacion_contextual.get("longitude")}
                response_dict = handler.start_flow(datos_iniciales=datos_iniciales)
                if chat_db_context: flag_modified(chat_db_context, "context_data")
                return _finalize_response(response_dict)
            elif action == "enviar_sugerencia_con_ubicacion":
                contexto_municipio_actual['estado_conversacion'] = ConversationState.ESPERANDO_TEXTO_SUGERENCIA.name
                contexto_municipio_actual['ubicacion_contextual_sugerencia'] = address
                if chat_db_context: flag_modified(chat_db_context, "context_data")
                return _finalize_response({"message_body": f"Excelente. Por favor, escribí tu sugerencia relacionada con la ubicación: *{address}*.", "fuente": "handler_enviar_sugerencia_con_ubicacion"})
            else:
                contexto_municipio_actual['estado_conversacion'] = None
                if chat_db_context: flag_modified(chat_db_context, "context_data")
                return GreetingHandler(context).handle({})

        elif estado_conversacion == ConversationState.ESPERANDO_TEXTO_SUGERENCIA.name:
            sugerencia_texto = pregunta_str
            if len(sugerencia_texto) < 10:
                return _finalize_response({"message_body": "Tu sugerencia parece un poco corta. ¿Podrías darme un poco más de detalle?", "fuente": "sugerencia_muy_corta"})

            ubicacion_sugerencia = contexto_municipio_actual.pop('ubicacion_contextual_sugerencia', 'N/A')
            datos_sugerencia = {"categoria": "Sugerencia", "descripcion": sugerencia_texto, "ubicacion": ubicacion_sugerencia}

            handler = CrearReclamoActionHandler(context)
            response = handler.execute(datos_sugerencia)

            if response.get("success"):
                response["message_to_user"] = f"✅ ¡Hemos recibido tu sugerencia! Muchas gracias por tu aporte. Lo hemos registrado con el número de ticket `{response.get('data', {}).get('nro_ticket', 'N/A')}` para su seguimiento."

            contexto_municipio_actual['estado_conversacion'] = None
            if chat_db_context: flag_modified(chat_db_context, "context_data")
            return _finalize_response(response)

    # 2. If no state is active, then handle actions that start new flows.
    elif action:
        response = handle_main_menu_action(action, context, chat_db_context)
        if response:
            return _finalize_response(response)
    else:
        menu_payload = _get_main_menu_payload(context)
        buttons_for_finder = [
            {"texto": btn.get("texto"), "action_id": btn.get("id")}
            for btn in menu_payload.get("options_list", [])
        ]
        inferred_action = find_menu_action_by_input(pregunta_str or "", buttons_for_finder)
        if inferred_action:
            response = handle_main_menu_action(inferred_action, context, chat_db_context)
            if response:
                return _finalize_response(response)


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

            # The state should be cleared so we don't get stuck here.
            # The handler itself will set a new state if it needs to continue a flow.
            contexto_municipio_actual['estado_conversacion'] = None
            if chat_db_context:
                flag_modified(chat_db_context, "context_data")

            response = handle_main_menu_action(selected_action, context, chat_db_context)
            if response:
                # After the action, ask a generic follow-up question unless the handler
                # wants to take control of the conversation (e.g., by setting a new state).
                # We check if a new state was set by the handler.
                new_state = contexto_municipio_actual.get("estado_conversacion")
                if not new_state:
                    # Append a follow-up question and show the main menu again.
                    # This creates a clear "turn" and returns control to the user.
                    follow_up_message = "\n\n¿En qué más puedo ayudarte?"
                    response['message_body'] = response.get('message_body', '').strip() + follow_up_message

                    # We will not send the full menu again here.
                    # We will send a simpler prompt.
                    # A better approach would be to have a "back to menu" button.
                    # For now, we just add the text.

                return _finalize_response(response)
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

    # --- INICIO: Manejo de selección de menú de reclamos ---
    elif estado_conversacion == ConversationState.ESPERANDO_SELECCION_MENU_RECLAMOS.name:
        pregunta_str_reclamo = ""
        if isinstance(pregunta_original, str):
            pregunta_str_reclamo = pregunta_original
        elif isinstance(pregunta_original, dict) and "pregunta" in pregunta_original:
            pregunta_str_reclamo = pregunta_original["pregunta"]

        logger_actual.info(f"Handling input in ESPERANDO_SELECCION_MENU_RECLAMOS state. Input: '{pregunta_str_reclamo}'")

        normalized_input = normalizar_texto(pregunta_str_reclamo or "")

        if pregunta_str_reclamo == "0" or normalized_input in RETURN_TO_MAIN_MENU:
            logger_actual.info("User requested to return to main menu from reclamos menu.")
            handler = GreetingHandler(context)
            response = handler.handle({})
            if chat_db_context:
                flag_modified(chat_db_context, "context_data")
            return _finalize_response(response)

        repeat_commands = {
            "show_reclamos_menu",
            "mostrar_menu_reclamos",
            "hacer un reclamo",
            "iniciar reclamo",
            "reclamo",
            "reclamos",
        }
        if not pregunta_str_reclamo or normalized_input in repeat_commands:
            logger_actual.info("Input requests reclamos menu again. Returning submenu.")
            return _finalize_response(_get_reclamos_menu())

        # El menú ahora tiene id_accion numéricos.
        # Primero, intentar matchear el input numérico con el id_accion.
        reclamo_options = _get_reclamos_menu().get("options_list", [])
        selected_category_name = None

        if pregunta_str_reclamo.isdigit():
            for option in reclamo_options:
                if option.get("id_accion") == pregunta_str_reclamo:
                    selected_category_name = option.get("category_name")
                    break

        # Si no es un número, o el número no corresponde a una opción, intentar matchear por texto.
        if not selected_category_name:
            # Usar la función existente que busca por keywords.
            # Le pasamos una lista de dicts con la clave "texto" que espera la función.
            # Usamos el 'category_name' limpio que agregamos.
            plain_text_options = [{"texto": opt.get("category_name")} for opt in reclamo_options]
            selected_category_name = find_reclamo_category_by_input(pregunta_str_reclamo, plain_text_options)

        if selected_category_name:
            if selected_category_name == "Pérdida de agua":
                contexto_municipio_actual['estado_conversacion'] = None
                if chat_db_context: flag_modified(chat_db_context, "context_data")
                return _finalize_response({
                    "message_body": "Para pérdida de agua, dirigite a la página de Aysam:\nhttps://www.aysam.com.ar/",
                    "options_list": [], "message_type": "text", "fuente": "info_perdida_agua"
                })

            logger_actual.info(f"Categoría de reclamo seleccionada: '{selected_category_name}'. Limpiando contexto anterior.")

            # FIX: Limpiar explícitamente el contexto del reclamo anterior para evitar el "estado atascado".
            contexto_municipio_actual.pop("datos_parciales_llm_reclamo", None)
            contexto_municipio_actual.pop("historial_llm_reclamo", None)

            constructed_prompt = f"Quiero iniciar un reclamo de {selected_category_name}"

            # --- INICIO: Integración del nuevo ReclamoFlowHandler ---
            handler = ReclamoFlowHandler(context, chat_db_context)
            response_dict = handler.start_flow(categoria_inicial=selected_category_name)
            if chat_db_context:
                flag_modified(chat_db_context, "context_data")
            return _finalize_response(response_dict)
            # --- FIN: Integración del nuevo ReclamoFlowHandler ---
        else:
            logger_actual.warning(f"Input '{pregunta_str_reclamo}' no coincide con ninguna categoría. Mostrando menú de nuevo.")
            return _finalize_response(_get_reclamos_menu())
    # --- FIN: Manejo de selección de menú de reclamos ---

    elif estado_conversacion == ConversationState.ESPERANDO_SELECCION_CONTACTO_CATEGORIA.name:
        selected_category_action = received_payload.get("action")
        pregunta_str_norm = normalizar_texto(pregunta_str or "")

        # Find the category either by action_id or by text matching
        selected_category = None
        categorias_map = contexto_municipio_actual.get('contactos_categorias', {})
        if selected_category_action and selected_category_action.startswith("select_contact_category_"):
            slug = selected_category_action.replace("select_contact_category_", "")
            selected = categorias_map.get(slug)
        elif pregunta_str_norm and categorias_map:
            from fuzzywuzzy import process
            name_map = {v['nombre']: k for k, v in categorias_map.items()}
            match, score = process.extractOne(pregunta_str_norm, list(name_map.keys()))
            selected = categorias_map.get(name_map[match]) if score > 80 else None
        else:
            selected = None

        if not selected:
            return _finalize_response({
                "message_body": "Por favor, seleccioná una categoría de la lista.",
                "fuente": "contactos_utiles_invalid_category_selection"
            })

        channel = context.get("channel", "web")
        contactos = selected.get("contactos", [])
        nombre_categoria = selected.get("nombre", "")
        if not contactos:
            message_body = f"No se encontraron contactos para la categoría '{nombre_categoria}'."
        else:
            if channel == "whatsapp":
                message_body = f"📞 *Contactos para {nombre_categoria}:*\n\n"
                for c in contactos:
                    message_body += _format_contact(c, channel) + "\n"
            else:
                message_body = f"<h4>📞 Contactos para {nombre_categoria}</h4>"
                for c in contactos:
                    message_body += _format_contact(c, channel)

        # Reset state
        contexto_municipio_actual['estado_conversacion'] = None
        if chat_db_context:
            flag_modified(chat_db_context, "context_data")

        return _finalize_response({
            "message_body": message_body.strip(),
            "message_type": "text",
            "fuente": "contactos_utiles_show_contacts"
        })

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

    elif estado_conversacion == ConversationState.ESPERANDO_INTENCION_UBICACION.name:
        ubicacion_contextual = contexto_municipio_actual.pop('ubicacion_contextual', None)
        address = ubicacion_contextual.get('address', 'la ubicación proporcionada') if ubicacion_contextual else 'la ubicación proporcionada'

        if action == "iniciar_reclamo_con_ubicacion":
            handler = ReclamoFlowHandler(context, chat_db_context)
            datos_iniciales = {"direccion": address}
            if ubicacion_contextual:
                datos_iniciales['coordenadas'] = {
                    "lat": ubicacion_contextual.get("latitude"),
                    "lon": ubicacion_contextual.get("longitude")
                }
            # The original implementation was missing the 'categoria_inicial' argument for start_flow
            response_dict = handler.start_flow(datos_iniciales=datos_iniciales)
            if chat_db_context:
                flag_modified(chat_db_context, "context_data")
            return _finalize_response(response_dict)

        elif action == "enviar_sugerencia_con_ubicacion":
            contexto_municipio_actual['estado_conversacion'] = ConversationState.ESPERANDO_TEXTO_SUGERENCIA.name
            contexto_municipio_actual['ubicacion_contextual_sugerencia'] = address
            if chat_db_context:
                flag_modified(chat_db_context, "context_data")
            return _finalize_response({
                "message_body": f"Excelente. Por favor, escribí tu sugerencia relacionada con la ubicación: *{address}*.",
                "fuente": "handler_enviar_sugerencia_con_ubicacion"
            })

        else: # Cancelar o no se entiende
            contexto_municipio_actual['estado_conversacion'] = None
            if chat_db_context:
                flag_modified(chat_db_context, "context_data")
            return GreetingHandler(context).handle({})


    elif estado_conversacion == ConversationState.ESPERANDO_CORRECCION_DATOS_RECLAMO.name:
        logger_actual.info(f"Handling input in ESPERANDO_CORRECCION_DATOS_RECLAMO state. Input: '{pregunta_str}'")

        datos_nuevos = extract_multiple_contact_details_llm(pregunta_str, ["nombre", "email", "telefono", "ubicacion", "descripcion"])
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

    elif estado_conversacion == ConversationState.ESPERANDO_TEXTO_SUGERENCIA.name:
        sugerencia_texto = pregunta_str
        if len(sugerencia_texto) < 10:
            return _finalize_response({
                "message_body": "Tu sugerencia parece un poco corta. ¿Podrías darme un poco más de detalle?",
                "fuente": "sugerencia_muy_corta"
            })

        ubicacion_sugerencia = contexto_municipio_actual.pop('ubicacion_contextual_sugerencia', 'N/A')

        # Crear ticket para la sugerencia
        datos_sugerencia = {
            "categoria": "Sugerencia",
            "descripcion": sugerencia_texto,
            "ubicacion": ubicacion_sugerencia,
        }

        handler = CrearReclamoActionHandler(context)
        response = handler.execute(datos_sugerencia)

        # Modificar el mensaje de éxito para que sea específico para sugerencias
        if response.get("success"):
            response["message_body"] = f"✅ ¡Hemos recibido tu sugerencia! Muchas gracias por tu aporte. Lo hemos registrado con el número de ticket `{response.get('ticket_nro', 'N/A')}` para su seguimiento."

        # Limpiar el estado de la conversación
        contexto_municipio_actual['estado_conversacion'] = None
        if chat_db_context:
            flag_modified(chat_db_context, "context_data")

        return _finalize_response(response)

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
