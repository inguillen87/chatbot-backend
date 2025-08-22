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
from .llm_utils import extract_complaint_details_llm
import math
from services.tasks import process_image_for_chat_task
from services.intent_classifier import IntentClassifier
from services.conversation_state import ConversationState
from services.handlers.base_handler import BaseMunicipioHandler
from services.handlers.greeting_handler import GreetingHandler, _get_main_menu_payload
from services.handlers.news_handler import NewsHandler
from services.handlers.poi_handler import PointsOfInterestHandler
from services.handlers.menu_handler import handle_main_menu_action, handle_contact_category_selection, MENU_KEYWORDS
from services.llm_interaction_handler import handle_llm_interaction
from services.flows.reclamos import ReclamoFlowHandler
from fuzzywuzzy import process


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
        location = geolocator.reverse((latitud, longitud), exactly_one=True)
        address = location.raw.get('address_components', [])
        municipio_actual = None
        for component in address:
            if 'locality' in component.get('types', []):
                municipio_actual = component.get('long_name')
                break
        if not municipio_actual:
            return jsonify({"error": "No se pudo determinar la localidad desde las coordenadas proporcionadas."}), 404
        municipios_encontrados = [municipio_actual]
        return jsonify({"municipios_cercanos": municipios_encontrados})
    except (GeocoderTimedOut, GeocoderServiceError) as e:
        return jsonify({"error": f"Error en el servicio de geolocalización: {e}"}), 500
    except Exception as e:
        return jsonify({"error": f"Error inesperado: {e}"}), 500

logger = logging.getLogger(__name__)

CONTEXTO_MUNICIPIO = "contexto_municipio_v2"

PALABRAS_CLAVE_CONFIRMACION = {
    "confirmar_reclamo", "confirmar", "confirmo", "confirmado",
    "si", "sí", "afirmativo", "dale", "ok", "proceder", "aceptar",
    "confirmar_reclamo_final", "si, confirmar reclamo", "sí, confirmar reclamo",
    "yes"
}

EDIT_KEYWORDS = {
    "editar", "cambiar", "corregir", "modificar",
    "no era asi", "me equivoque", "error", "equivocado",
    "editar datos", "editar_reclamo_datos", "quiero editar", "necesito cambiar"
}

def _super_normalize(s: str) -> str:
    s = normalizar_texto(s)
    return re.sub(r'[^a-z0-9]', '', s)


def extract_description_and_check_confirmation(text: str, confirmation_keywords: set) -> tuple[str | None, bool]:
    if not text:
        return None, False
    normalized_text = normalizar_texto(text.strip())
    sorted_confirmation_keywords = sorted(list(confirmation_keywords), key=len, reverse=True)
    extracted_description = normalized_text
    has_confirmation_intent = False
    for keyword in sorted_confirmation_keywords:
        if " " in keyword:
            if normalized_text.endswith(keyword):
                extracted_description = normalized_text[:-len(keyword)].strip(" .,")
                if not extracted_description:
                    extracted_description = None
                has_confirmation_intent = True
                break
        else:
            if normalized_text == keyword:
                extracted_description = None
                has_confirmation_intent = True
                break
            if normalized_text.endswith(f" {keyword}"):
                potential_description = normalized_text[:-(len(keyword) + 1)].strip()
                if len(potential_description.split()) <= 4 or not potential_description:
                    extracted_description = potential_description if potential_description else None
                    has_confirmation_intent = True
                    break
            for separator in [",", "."]:
                if normalized_text.endswith(f"{separator}{keyword}"):
                    potential_description = normalized_text[:-(len(keyword) + 1)].strip()
                    if len(potential_description.split()) <= 4 or not potential_description:
                        extracted_description = potential_description if potential_description else None
                        has_confirmation_intent = True
                        break
            if has_confirmation_intent:
                break
    if not has_confirmation_intent:
        extracted_description = text.strip()
    return extracted_description, has_confirmation_intent

URL_REGEX = re.compile(r"https?://\S+")

def _remove_redundant_urls_from_message(message_body, options_list):
    if not message_body or not options_list:
        return message_body
    for option in options_list:
        if isinstance(option, dict) and 'url' in option and option['url'] in message_body:
            message_body = message_body.replace(option['url'], '')
    message_body = re.sub(r'por favor\s+ingresá\s+al\s+siguiente\s+enlace\s*:?', '', message_body, flags=re.IGNORECASE).strip()
    message_body = re.sub(r'ingresá\s+al\s+siguiente\s+enlace\s*:?', '', message_body, flags=re.IGNORECASE).strip()
    message_body = re.sub(r'enlace\s*:?', '', message_body, flags=re.IGNORECASE).strip()
    message_body = re.sub(r'\s{2,}', ' ', message_body).strip()
    message_body = message_body.replace(' .', '.').strip()
    message_body = re.sub(r'[,:]\s*\.', '.', message_body)
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

def get_tramites_info() -> dict:
    from services.municipio_responder import _TRAMITES_CACHE, _TRAMITES_MTIME, cargar_tramites_info
    return cargar_tramites_info()

def serializar_enum(obj):
    if isinstance(obj, Enum): return obj.name
    elif isinstance(obj, dict): return {k: serializar_enum(v) for k, v in obj.items()}
    elif isinstance(obj, list): return [serializar_enum(v) for v in obj]
    else: return obj

def handle_location_update(data):
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

def _handle_ticket_creation(contexto_municipio_actual, context, datos_estructura_llm):
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
    app = current_app._get_current_object()
    final_municipio_config = cargar_configuracion_municipio(str(owner_user.municipio_id) if owner_user and hasattr(owner_user, 'municipio_id') and owner_user.municipio_id else "default", "config.json")

    received_payload = {}
    if isinstance(pregunta_original, dict):
        received_payload = pregunta_original
        pregunta_str = received_payload.get("pregunta", "")
    else:
        pregunta_str = str(pregunta_original)
        received_payload["pregunta"] = pregunta_str

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
    }

    # Logic from the original function...

    return {"message_body": "Fallback response", "message_type": "text"}
