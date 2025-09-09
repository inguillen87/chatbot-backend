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
from cachetools import TTLCache
from models import MunicipioTicket, TicketComentario, db, SitioWebInfo, Conversacion
from services.ticket_service import servicio_tickets
from utils.db_utils import safe_flag_modified
# Compatibilidad hacia atrás para pruebas que parchean `flag_modified`
flag_modified = safe_flag_modified

logger = logging.getLogger(__name__)
from twilio.rest import Client
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
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
from .points_of_interest_handler import PointsOfInterestHandler
from .categorias_municipio import CATEGORIAS_RECLAMO, categorias_normalizadas
from .common_utils import (
    validar_email,
    validar_telefono,
    formatear_telefono_e164,
    construir_respuesta_sugerir_registro,
)
from .llm_utils import extract_complaint_details_llm, extract_multiple_contact_details_llm
import math
from services.tasks import process_image_for_chat_task
from services.intent_classifier import IntentClassifier
from services.multimodal_analyzer import analizar_imagen_con_fallback
import json
from services.ticket_utils import formatear_ticket_respuesta

ARG_TZ = ZoneInfo("America/Argentina/Buenos_Aires")

class ReclamoState(Enum):
    ESPERANDO_CATEGORIA = auto()
    ESPERANDO_DIRECCION = auto()
    ESPERANDO_DESCRIPCION = auto()
    ESPERANDO_FOTO = auto()
    ESPERANDO_DATOS_CONTACTO = auto()
    ESPERANDO_CONFIRMACION = auto()
    ESPERANDO_MENU_EDICION = auto()


CANCEL_KEYWORDS = {
    normalizar_texto(k)
    for k in [
        "cancelar",
        "salir",
        "volver",
        "menu",
        "menú principal",
        "menu principal",
        "terminar",
        "basta",
        "reiniciar",
        "resetear",
        "empezar de nuevo",
        "volver a empezar",
        "empezar de cero",
    ]
}

# Simple cache to avoid recomputing responses for repeated municipal queries
MUNICIPIO_RESPONSE_CACHE = TTLCache(maxsize=256, ttl=3600)

def clear_municipio_cache() -> None:
    """Utility mainly for tests to clear the local municipio response cache."""
    MUNICIPIO_RESPONSE_CACHE.clear()

# --- NUEVO: parsing compacto de datos de contacto ---
CONTACT_FIELDS = ("nombre", "email", "telefono", "dni", "direccion_contacto")


def _parse_contact_compact_text(texto: str) -> dict:
    """Parsea datos de contacto en una sola línea separados por comas o espacios."""
    data = {k: None for k in CONTACT_FIELDS}
    t = " ".join([p.strip() for p in re.split(r"[,\n]+", texto) if p.strip()])

    m = re.search(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", t, re.I)
    if m:
        data["email"] = m.group(0)
        t = t.replace(m.group(0), " ")

    m = re.search(r"\b\+?\d{8,15}\b", t)
    if m:
        data["telefono"] = m.group(0)
        t = t.replace(m.group(0), " ")

    m = re.search(r"\b\d{7,9}\b", t)
    if m:
        data["dni"] = m.group(0)
        t = t.replace(m.group(0), " ")

    chunks = [c for c in re.split(r"\s{2,}|\s-\s", t) if c.strip()]
    rem = " ".join(chunks).strip()
    tokens = rem.split()
    for i, tok in enumerate(tokens):
        if tok.isdigit():
            if i >= 2:
                data["direccion_contacto"] = " ".join(tokens[i-2:]).strip()
                nombre_candidato = " ".join(tokens[: i - 2]).strip()
            else:
                data["direccion_contacto"] = " ".join(tokens[i-1:]).strip()
                nombre_candidato = " ".join(tokens[: i - 1]).strip()
            if nombre_candidato:
                data["nombre"] = nombre_candidato
            break
    else:
        if rem:
            data["nombre"] = rem

    if data["email"] and not validar_email(data["email"]):
        data["email"] = None
    if data["telefono"] and not validar_telefono(data["telefono"]):
        data["telefono"] = None

    return data


def _need_any_contact(datos: dict) -> bool:
    return any(not datos.get(k) for k in CONTACT_FIELDS)


def _merge_contact(base: dict, nuevo: dict) -> dict:
    out = dict(base or {})
    for k in CONTACT_FIELDS:
        if not out.get(k) and nuevo.get(k):
            out[k] = nuevo[k]
    return out


def _format_contact_summary(datos: dict) -> str:
    return (
        f"Nombre: {datos.get('nombre') or '-'}\n"
        f"Email: {datos.get('email') or '-'}\n"
        f"Teléfono: {datos.get('telefono') or '-'}\n"
        f"DNI: {datos.get('dni') or '-'}\n"
        f"Dirección contacto: {datos.get('direccion_contacto') or '-'}"
    )


def pedir_datos_contacto_compacto():
    return {
        "message_body": (
            "Para seguir, enviá *en una sola línea* tus datos separados por comas o espacios, "
            "en cualquier orden: *Nombre completo, Email, Teléfono, DNI, Dirección de contacto*.\n\n"
            "Ejemplo: *Juan Perez, juan@mail.com, 2615551234, 30123456, Don Bosco 55 Junín*"
        ),
        "message_type": "text",
    }


def procesar_datos_contacto_compacto(texto: str, datos_existentes: dict) -> dict:
    parsed = _parse_contact_compact_text(texto)
    datos = _merge_contact(datos_existentes, parsed)
    if _need_any_contact(datos):
        try:
            llm = extract_multiple_contact_details_llm(texto)
            datos = _merge_contact(
                datos,
                {
                    "nombre": llm.get("nombre"),
                    "email": llm.get("email"),
                    "telefono": llm.get("telefono"),
                    "dni": llm.get("dni"),
                    "direccion_contacto": llm.get("direccion"),
                },
            )
        except Exception:
            pass
    return datos

class ReclamoFlowHandler:
    def __init__(self, context, chat_db_context):
        self.context = context
        self.chat_db_context = chat_db_context
        # Ensure the flow data lives inside the main municipio context so it
        # survives across turns just like other conversation state.
        municipal_ctx = context.get("chat_db_context_data", {}).setdefault(
            CONTEXTO_MUNICIPIO, {}
        )
        self.municipal_ctx = municipal_ctx
        self.flow_context = municipal_ctx.setdefault("reclamo_flow_v2", {})
        self.greeting_handler = GreetingHandler(context)


    def check_for_cancel(self, user_input, payload):
        normalized_input = normalizar_texto(user_input)
        action = (payload.get("action_id") or payload.get("action") or "").lower()
        if (
            normalized_input in CANCEL_KEYWORDS
            or action in {"cancelar", "menu_principal"}
        ):
            return self.end_flow(
                "Proceso de reclamo cancelado. ¿En qué más te puedo ayudar?",
                show_menu=True,
            )
        return None

    def handle(self, user_input, payload):
        cancel_response = self.check_for_cancel(user_input, payload)
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
        elif state == ReclamoState.ESPERANDO_MENU_EDICION:
            return self.handle_menu_edicion(user_input, payload)
        else:
            logger.error(f"ReclamoFlowHandler: Estado desconocido o no manejado: {state_name}")
            return self.end_flow("Hubo un error en el proceso, por favor intentá de nuevo.", show_menu=True)

    def start_flow(self, datos_iniciales=None, categoria_inicial=None):
        logger.info("Iniciando flujo de reclamo v2.")
        self.flow_context.clear()
        self.flow_context['datos_reclamo'] = datos_iniciales or {}

        # Si la conversación comenzó con una foto (context['foto_url']) pero
        # aún no se reflejó en los datos del reclamo, la agregamos para evitar
        # que se le vuelva a solicitar al usuario.
        if (
            self.context.get("foto_url")
            and not self.flow_context['datos_reclamo'].get('foto_url')
        ):
            self.flow_context['datos_reclamo']['foto_url'] = self.context.get("foto_url")

        # Pre-fill contact details from the viewer if available so we do not
        # ask the user for information we already have.
        viewer = self.context.get("viewer_user_obj")
        if viewer:
            datos = self.flow_context['datos_reclamo']
            # Some viewer objects store attributes with different names. Fall back
            # to common alternatives to avoid asking for data we already have.
            datos.setdefault(
                'nombre',
                getattr(viewer, 'name', None)
                or getattr(viewer, 'nombre', None)
                or getattr(viewer, 'nombre_vecino', None),
            )
            datos.setdefault(
                'email',
                getattr(viewer, 'email', None)
                or getattr(viewer, 'email_vecino', None),
            )
            datos.setdefault(
                'telefono',
                getattr(viewer, 'telefono', None)
                or getattr(viewer, 'telefono_vecino', None),
            )
            datos.setdefault(
                'dni',
                getattr(viewer, 'dni', None)
                or getattr(viewer, 'dni_vecino', None)
                or getattr(viewer, 'documento', None),
            )

        # Reuse previously provided contact info stored in municipal context
        contacto_prev = self.municipal_ctx.get('contacto_usuario', {})
        if contacto_prev:
            datos = self.flow_context['datos_reclamo']
            for campo in ['nombre', 'email', 'telefono', 'dni']:
                datos.setdefault(campo, contacto_prev.get(campo))

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
        self.flow_context['state'] = ReclamoState.ESPERANDO_DESCRIPCION.name
        return {
            "message_body": f"Perfecto. Iniciemos tu reclamo por *{user_input}*.\n\nPor favor, describí brevemente el problema."
        }

    def handle_direccion(self, user_input, payload):
        datos = self.flow_context.setdefault('datos_reclamo', {})
        ubic_ctx = self.municipal_ctx.get("ubicacion_contextual") or {}
        coords = datos.get("coordenadas") or {}

        if payload.get("coordenadas"):
            coords = payload["coordenadas"]
        elif self.municipal_ctx.get("datos_parciales_llm_reclamo", {}).get("coordenadas"):
            coords = self.municipal_ctx["datos_parciales_llm_reclamo"]["coordenadas"]

        if coords:
            datos["coordenadas"] = coords

        direccion_display = (
            ubic_ctx.get("address")
            or self.municipal_ctx.get("datos_parciales_llm_reclamo", {}).get("ubicacion")
            or user_input.strip()
        )

        if not direccion_display:
            return {"message_body": "La dirección parece muy corta. Por favor, ingresá una dirección más completa (calle y número)."}

        datos["direccion"] = direccion_display

        if datos.get('foto_url') or self.context.get('foto_url'):
            datos.setdefault('foto_url', self.context.get('foto_url'))
            return self.ask_for_contact_details()

        self.flow_context['state'] = ReclamoState.ESPERANDO_FOTO.name
        return {
            "message_body": "¿Querés agregar una foto? Esto ayuda mucho a resolver el problema.",
            "options_list": [
                {"texto": "Sí, agregar foto", "action_id": "reclamo_adjuntar_foto_si"},
                {"texto": "No, omitir foto", "action_id": "reclamo_adjuntar_foto_no"},
            ],
            "message_type": "interactive_buttons",
        }

    def handle_descripcion(self, user_input):
        if len(user_input) < 10:
            return {"message_body": "Por favor, dame una descripción un poco más detallada del problema."}
        self.flow_context['datos_reclamo']['descripcion'] = user_input
        if not self.flow_context['datos_reclamo'].get('direccion'):
            self.flow_context['state'] = ReclamoState.ESPERANDO_DIRECCION.name
            return {"message_body": "Gracias. ¿Cuál es la dirección exacta del problema (calle y número)?"}
        else:
            # If a photo was already provided earlier or is present in the
            # context, do not ask for another one.
            if self.flow_context['datos_reclamo'].get('foto_url') or self.context.get('foto_url'):
                self.flow_context['datos_reclamo'].setdefault('foto_url', self.context.get('foto_url'))
                return self.ask_for_contact_details()
            self.flow_context['state'] = ReclamoState.ESPERANDO_FOTO.name
            return {
                "message_body": "¿Querés agregar una foto? Esto ayuda mucho a resolver el problema.",
                "options_list": [{"texto": "Sí, agregar foto", "action_id": "reclamo_adjuntar_foto_si"}, {"texto": "No, omitir foto", "action_id": "reclamo_adjuntar_foto_no"}],
                "message_type": "interactive_buttons"
            }
    def handle_foto(self, user_input, payload):
        action = payload.get("action")
        normalized = user_input.lower()

        # Accept the photo if either the payload or the outer context indicates
        # that an image was provided. This covers the case where the user sends
        # a picture directly without first pressing "Sí, agregar foto".
        foto_url = payload.get("foto_url") or self.context.get("foto_url")
        es_foto = payload.get("es_foto") or self.context.get("es_foto")
        if es_foto and foto_url:
            self.flow_context['datos_reclamo']['foto_url'] = foto_url
            self.context['foto_url'] = foto_url
            return self.ask_for_contact_details()

        no_words = {"no", "omitir", "omitilo", "sin foto", "ninguna"}
        yes_words = {"si", "sí", "enviar", "adjunto", "mandar"}

        if any(w in normalized for w in no_words) or action == "reclamo_adjuntar_foto_no":
            self.flow_context['datos_reclamo']['foto_url'] = None
            return self.ask_for_contact_details()
        elif any(w in normalized for w in yes_words) or action == "reclamo_adjuntar_foto_si":
            return {"message_body": "Por favor, enviá la foto ahora."}
        else:
            return {
                "message_body": "No entendí tu respuesta. Por favor, enviá una foto o elegí una de las opciones.",
                "options_list": [{"texto": "Omitir foto", "action_id": "reclamo_adjuntar_foto_no"}],
            }

    def ask_for_contact_details(self, force_prompt: bool = False):
        datos = self.flow_context.setdefault('datos_reclamo', {})

        if not datos.get('email') or not datos.get('nombre'):
            force_prompt = True

        if not force_prompt and not _need_any_contact(datos):
            self.flow_context['state'] = ReclamoState.ESPERANDO_CONFIRMACION.name
            return self.get_confirmation_message()

        self.flow_context['state'] = ReclamoState.ESPERANDO_DATOS_CONTACTO.name
        return pedir_datos_contacto_compacto()

    def handle_datos_contacto(self, user_input):
        datos_reclamo = self.flow_context.setdefault('datos_reclamo', {})
        nuevos = procesar_datos_contacto_compacto(user_input, datos_reclamo)
        self.flow_context['datos_reclamo'] = nuevos
        resumen = _format_contact_summary(nuevos)
        self.flow_context['state'] = ReclamoState.ESPERANDO_CONFIRMACION.name
        return {
            "message_body": f"Perfecto, tomé estos datos:\n{resumen}\n\n¿Confirmás?",
            "options_list": [
                {"texto": "1. Confirmar", "action_id": "reclamo_confirmar_si"},
                {"texto": "2. Editar", "action_id": "reclamo_confirmar_no"},
                {"texto": "3. Cancelar", "action_id": "cancelar"},
            ],
            "message_type": "interactive_buttons",
        }

    def get_confirmation_message(self):
        datos = self.flow_context.get('datos_reclamo', {})
        mensaje = "Por favor, confirmá que los datos de tu reclamo son correctos:\n\nDatos del reclamo:\n"
        mensaje += f"- Categoría: {datos.get('categoria', 'No especificada')}\n"
        mensaje += f"- Dirección: {datos.get('direccion', 'No especificada')}\n"
        mensaje += f"- Descripción: {datos.get('descripcion', 'No especificada')}\n\n"
        mensaje += "Datos personales:\n"
        mensaje += f"- Nombre: {datos.get('nombre', 'No especificado')}\n"
        mensaje += f"- DNI: {datos.get('dni', 'No especificado')}\n"
        mensaje += f"- Email: {datos.get('email', 'No especificado')}\n"
        mensaje += f"- Teléfono: {datos.get('telefono', 'No especificado')}\n"
        mensaje += f"- Foto adjunta: {'Sí' if datos.get('foto_url') else 'No'}\n"
        return {
            "message_body": mensaje,
            "options_list": [{"texto": "✅ Confirmar", "action_id": "reclamo_confirmar_si"}, {"texto": "✏️ Editar datos", "action_id": "reclamo_confirmar_no"}, {"texto": "❌ Cancelar", "action_id": "reclamo_cancelar"}],
            "message_type": "interactive_buttons"
        }

    def handle_confirmacion(self, user_input, payload):
        action = payload.get("action")
        normalized = user_input.lower()
        affirmatives = {"si", "sí", "confirmo", "confirmar", "ok", "okay", "acepto", "aceptar", "dale"}
        negatives = {"no", "editar", "modificar", "cambiar"}
        if any(word in normalized for word in affirmatives) or action == "reclamo_confirmar_si":
            datos = self.flow_context.get('datos_reclamo', {})
            action_data = {
                "categoria": datos.get("categoria"),
                "descripcion": datos.get("descripcion"),
                "ubicacion": datos.get("direccion"),
                "usuario": datos.get("nombre"),
                "dni": datos.get("dni"),
                "email": datos.get("email"),
                "telefono": datos.get("telefono"),
                "foto_url_adjunta": datos.get("foto_url"),
            }
            handler = CrearReclamoActionHandler(self.context)
            result = handler.execute(action_data)
            if result.get("success"):
                nro_ticket = result.get("data", {}).get("nro_ticket")
                message = result.get(
                    "message_to_user",
                    f"¡Tu reclamo fue creado con éxito! ✅\n\nEl número de seguimiento es *{nro_ticket}*. Te mantendremos informado sobre el estado del mismo por este medio.",
                )
                message += (
                    "\n\n¿Sabías que estamos trabajando para una Junín más limpia?\n"
                    "Planta de recolección, reciclaje y elaboración de productos sustentables.\n"
                    "Ladrillos, tejas, postes, mangueras, impresión 3D, luminarias LED y paneles solares.\n"
                    "Más info: https://www.juninmendoza.gov.ar/punto-limpio/"
                )
                punto_limpio_logo = "https://www.juninmendoza.gov.ar/wp-content/uploads/logo-junin-punto-limpio-1024x472.png"
                return self.end_flow(message, show_menu=True, image_url=punto_limpio_logo)
            error_message = result.get(
                "message_to_user",
                "Hubo un problema al registrar tu reclamo. Por favor, intentá de nuevo más tarde.",
            )
            return self.end_flow(error_message, show_menu=True)
        elif any(word in normalized for word in negatives) or action == "reclamo_confirmar_no":
            self.flow_context['state'] = ReclamoState.ESPERANDO_MENU_EDICION.name
            return self.get_edit_menu()
        else:  # Cancel or any other input
            cancel_msg = "Proceso de reclamo cancelado. ¿En qué más te puedo ayudar?"
            return self.end_flow(cancel_msg, show_menu=True)

    def get_edit_menu(self):
        options = [
            {"texto": "Dirección", "action_id": "edit_dir"},
            {"texto": "Descripción", "action_id": "edit_desc"},
            {"texto": "Foto", "action_id": "edit_foto"},
            {"texto": "Contacto", "action_id": "edit_contacto"},
        ]
        return {
            "message_body": "¿Qué querés editar?",
            "options_list": options,
            "message_type": "interactive_buttons",
        }

    def handle_menu_edicion(self, user_input, payload):
        action = payload.get("action")
        text = user_input.strip().lower()
        mapping = {
            "1": "edit_dir",
            "direccion": "edit_dir",
            "2": "edit_desc",
            "descripcion": "edit_desc",
            "3": "edit_foto",
            "foto": "edit_foto",
            "4": "edit_contacto",
            "contacto": "edit_contacto",
        }
        selected = action or mapping.get(text)
        if selected == "edit_dir":
            self.flow_context['state'] = ReclamoState.ESPERANDO_DIRECCION.name
            return {"message_body": "Indica la dirección corregida."}
        if selected == "edit_desc":
            self.flow_context['state'] = ReclamoState.ESPERANDO_DESCRIPCION.name
            return {"message_body": "Escribí la descripción actualizada."}
        if selected == "edit_foto":
            self.flow_context['state'] = ReclamoState.ESPERANDO_FOTO.name
            return {"message_body": "Enviá la nueva foto."}
        if selected == "edit_contacto":
            self.flow_context['state'] = ReclamoState.ESPERANDO_DATOS_CONTACTO.name
            return {"message_body": "Actualizá tus datos de contacto."}
        return self.get_edit_menu()

    def end_flow(self, message, show_menu=False, image_url=None):
        self.flow_context.clear()
        # Remove flow data from municipio context so subsequent turns don't
        # enter this handler unintentionally.
        self.municipal_ctx.pop("reclamo_flow_v2", None)

        payload = {"message_body": message, "message_type": "text"}
        if image_url:
            payload["image_url"] = image_url

        if show_menu:
            menu_payload = GreetingHandler(self.context).handle({})
            payload["delayed_payload"] = menu_payload
            payload["delay_seconds"] = 20

        return payload
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
    ESPERANDO_DATOS_CONTACTO_SUGERENCIA = auto()
    ESPERANDO_CONFIRMACION_SUGERENCIA = auto()
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
    "farmacia", "supermercado", "negocio", "servicio", "buscar",
    "comercio", "local"
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
    owner_user = context.get("user_obj")

    user_name = None
    if isinstance(profile_name, str) and profile_name.strip():
        owner_name = None
        if owner_user:
            owner_name = getattr(owner_user, "nombre", None) or getattr(owner_user, "name", None)
        # Avoid greeting with the admin/owner name when the session is anonymous
        if not owner_name or profile_name.strip().lower() != str(owner_name).strip().lower():
            user_name = profile_name.strip()
    if not user_name and viewer_user:
        user_name = getattr(viewer_user, "nombre", None) or getattr(viewer_user, "name", None)

    if welcome_message_override:
        welcome_message = welcome_message_override
    elif user_name:
        welcome_message = (
            f"¡Hola, {user_name}! 👋 Soy JUNI, tu Asistente Virtual de la Municipalidad de Junín.\n\n"
            "Podés compartir tu ubicación, enviarnos fotos o mandarnos una nota de voz con lo que necesitás y te ofreceremos opciones para trámites, reclamos y más.\n\n"
            "¿Cómo te puedo ayudar hoy?"
        )
    else:
        welcome_message = (
            "¡Hola! 👋 Soy JUNI, tu Asistente Virtual de la Municipalidad de Junín.\n\n"
            "Podés compartir tu ubicación, enviarnos fotos o mandarnos una nota de voz con lo que necesitás y te ofreceremos opciones para trámites, reclamos y más.\n\n"
            "¿Cómo te puedo ayudar hoy?"
        )

    channel = context.get("channel", "web")
    if channel == "whatsapp":
        # Simplified menu for WhatsApp: only top-level categories
        categorias = [{
            "titulo": "Categorías",
            "botones": [
                {"texto": "🗣️ Reclamos y Consultas", "action_id": "mostrar_menu_reclamos"},
                {"texto": "🚗 Trámites y Turnos", "action_id": "mostrar_menu_tramites"},
                {"texto": "📰 Información del Municipio", "action_id": "mostrar_menu_informacion"},
                {"texto": "🅿️ Estacionamiento", "action_id": "mostrar_menu_estacionamiento"},
            ]
        }]

        flat_buttons = []
        for boton in categorias[0].get('botones', []):
            new_boton = boton.copy()
            new_boton['id'] = new_boton.get('action_id', new_boton['texto'])
            flat_buttons.append(new_boton)
    else:
        # Full accordion-style menu for web/widget channels
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
                {"texto": "🏗️ Obras", "action_id": "obras"},
                {"texto": "♻️ Punto Limpio", "action_id": "punto_limpio"},
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

    response = {
        "message_body": welcome_message,
        "options_list": flat_buttons,
        "message_type": "interactive_list",
        "accion_backend": "responder_directamente",
        "fuente": "greeting_handler_structured_menu_v2",
        "categorias": categorias,
        "generar_audio": True
    }
    config = context.get("municipio_config_actual", {})
    image_url = config.get("welcome_image_url")
    if image_url:
        response["image_url"] = image_url
    return response


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
            contacto_prev = chat_db_context_data.get(CONTEXTO_MUNICIPIO, {}).get('contacto_usuario', {})
            profile_name = chat_db_context_data.get('profile_name')

            # Clear the entire context to prevent stale data from any flow
            chat_db_context_data.clear()

            # Restore essential info into a fresh context
            contexto_municipio_nuevo = chat_db_context_data.setdefault(CONTEXTO_MUNICIPIO, {})
            if contacto_prev:
                contexto_municipio_nuevo['contacto_usuario'] = contacto_prev
            if user_info:
                contexto_municipio_nuevo['user'] = user_info
            if profile_name:
                chat_db_context_data['profile_name'] = profile_name

            contexto_municipio_actual = contexto_municipio_nuevo

        # Establecer el estado para esperar una selección del menú principal en el próximo turno.
        contexto_municipio_actual['estado_conversacion'] = ConversationState.ESPERANDO_SELECCION_MENU_PRINCIPAL.name
        logger.info(f"[GreetingHandler] Nuevo estado de conversación: {contexto_municipio_actual['estado_conversacion']}")

        # Usar la función centralizada para obtener el payload del menú.
        return _get_main_menu_payload(self.context)



def _message_with_menu(message, context):
    menu_payload = GreetingHandler(context).handle({})
    if message:
        menu_payload["message_body"] = f"{message}\n\n{menu_payload['message_body']}"
    return menu_payload
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

    def _format_fecha(fecha_str: str) -> str:
        try:
            if not fecha_str:
                return ""
            fecha_str = fecha_str.rstrip("Z")
            dt = datetime.fromisoformat(fecha_str)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=ARG_TZ)
            else:
                dt = dt.astimezone(ARG_TZ)
            fecha_formateada = dt.strftime("%d/%m/%Y")
            if dt.time() != datetime.min.time():
                fecha_formateada += f" {dt.strftime('%H:%M')} hs"
            return fecha_formateada
        except Exception:
            return fecha_str

    title = post.get("titulo", "Sin título")
    subtitle = post.get("subtitulo")
    desc = post.get("descripcion", "Sin descripción.")
    link = post.get("enlace") or post.get("link") or post.get("url")
    imagen = post.get("imagen_url")

    fecha_inicio = post.get("fecha_evento_inicio") or post.get("fecha_inicio")
    fecha_fin = post.get("fecha_evento_fin")
    if fecha_inicio and fecha_fin and fecha_fin != fecha_inicio:
        fecha = f"{_format_fecha(fecha_inicio)} - {_format_fecha(fecha_fin)}"
    else:
        fecha = _format_fecha(fecha_inicio or fecha_fin or post.get("fecha_publicacion", "")) if (fecha_inicio or fecha_fin or post.get("fecha_publicacion")) else None

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
            lines.extend(["", desc])
        if imagen:
            lines.extend(["", imagen])
        if link:
            lines.extend(["", f"🔗 {link}"])
        return "\n".join(lines)

    if channel == "web":
        lines = [title]
        if subtitle:
            lines.append(subtitle)
        if fecha:
            lines.append(f"📅 {fecha}")
        if ubicacion:
            lines.append(f"📍 {ubicacion}")
        if desc:
            lines.extend(["", desc])
        if imagen:
            lines.extend(["", imagen])
        if link:
            lines.extend(["", f"🔗 {link}"])
        return "\n".join(lines)

    # Default to HTML formatting for other channels
    parts = [f"<strong>{title}</strong>"]
    if subtitle:
        parts.append(f"<em>{subtitle}</em>")
    if fecha:
        parts.append(f"📅 {fecha}")
    if ubicacion:
        parts.append(f"📍 {ubicacion}")
    if imagen:
        parts.append(f'<img src="{imagen}" alt="flyer" style="max-width:100%;height:auto;">')
    if desc:
        parts.append(desc)
    if link:
        parts.append(f'<a href="{link}" target="_blank">Ver más</a>')
    return "<br>".join(parts)


def _format_contact(contact: dict, channel: str) -> str:
    """Formatea un contacto individual según el canal."""
    nombre = contact.get("nombre", "Sin nombre")
    if "_" in nombre:
        nombre = nombre.replace("_", " ").title()
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


def handle_contactos_utiles_mostrar_categoria(context, chat_db_context, selected):
    """Muestra los contactos de una categoría manteniendo el estado para nuevas consultas."""
    channel = context.get("channel", "web")
    contexto_municipio_actual = context.get("chat_db_context_data", {}).setdefault(CONTEXTO_MUNICIPIO, {})
    categorias_map = contexto_municipio_actual.get('contactos_categorias', {})

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

    buttons = [{"texto": v["nombre"], "action_id": f"select_contact_category_{k}"} for k, v in categorias_map.items()]

    contexto_municipio_actual['estado_conversacion'] = ConversationState.ESPERANDO_SELECCION_CONTACTO_CATEGORIA.name
    if chat_db_context:
        flag_modified(chat_db_context, "context_data")

    return {
        "message_body": message_body.strip(),
        "options_list": buttons,
        "message_type": "interactive_buttons",
        "fuente": "contactos_utiles_show_contacts",
    }


def _get_posts_from_json(content_type: str, channel: str, municipio_id: str) -> tuple[str, str | None]:
    """Helper to get formatted posts of a specific type from the JSON file.

    Returns a tuple with the formatted text and the first image URL found
    for the requested posts. The image is returned separately to allow the
    caller to adjuntar a media message.
    """

    all_posts_data = cargar_agenda_cultural(municipio_id)
    all_posts = all_posts_data.get("eventos", [])

    if not all_posts:
        return "", None

    posts = [p for p in all_posts if p.get("tipo_post") == content_type]

    if not posts:
        return "", None

    limit = 6

    def _parse_date(date_str: str):
        if not date_str:
            return None
        try:
            date_str = date_str.rstrip("Z")
            dt = datetime.fromisoformat(date_str)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=ARG_TZ)
            else:
                dt = dt.astimezone(ARG_TZ)
            return dt
        except Exception:
            return None

    if content_type == "evento":
        for p in posts:
            p["_start"] = _parse_date(
                p.get("fecha_evento_inicio")
                or p.get("fecha_inicio")
                or p.get("fecha_publicacion")
            )
        future_posts = [p for p in posts if p.get("_start") and p["_start"] >= datetime.now(ARG_TZ)]
        posts = future_posts or posts
        posts.sort(key=lambda x: x.get("_start") or datetime.max)
    else:
        posts.sort(key=lambda x: x.get("fecha_publicacion", ""), reverse=True)

    first_image = None
    formatted: list[str] = []
    for p in posts[:limit]:
        if not first_image:
            first_image = p.get("imagen_url")
        formatted.append(_format_post(p, channel))

    if channel == "whatsapp":
        emoji = "📰" if content_type == "noticia" else "🎭"
        formatted = [f"{emoji} {item}".rstrip() for item in formatted]
        return "\n\n".join(formatted) + "\n", first_image
    if channel == "web":
        return "\n\n".join(formatted) + "\n", first_image
    return "<hr>".join(formatted), first_image

def handle_main_menu_action(action_id: str, context: dict, chat_db_context) -> dict:
    """
    Handles actions from the new categorized main menu.
    """
    # --- Aliases for new action_ids to reuse existing logic ---
    if action_id in {"veterinaria_bromatologia", "bromatologia"}:
        action_id = "zoonosis"  # Re-route to existing logic
    if action_id == "buscar_estacionamiento":
        action_id = "estacionamiento" # Re-route to existing logic

    # --- Handlers for New/Modified Menu Options ---
    if action_id == "contactos_utiles":
        return handle_contactos_utiles_inicio(context, chat_db_context)

    if action_id == "menu_principal":
        contexto_municipio_actual = context.get("chat_db_context_data", {}).setdefault(CONTEXTO_MUNICIPIO, {})
        contexto_municipio_actual['estado_conversacion'] = ConversationState.ESPERANDO_SELECCION_MENU_PRINCIPAL.name
        contexto_municipio_actual.pop('menu_opciones', None)
        if chat_db_context:
            flag_modified(chat_db_context, "context_data")
        return _get_main_menu_payload(context)

    if action_id in {"limpiar_contexto", "cancelar"}:
        contexto_municipio_actual = context.get("chat_db_context_data", {}).setdefault(CONTEXTO_MUNICIPIO, {})
        contacto_prev = contexto_municipio_actual.get("contacto_usuario")
        contexto_municipio_actual.clear()
        if contacto_prev:
            contexto_municipio_actual["contacto_usuario"] = contacto_prev
        contexto_municipio_actual["estado_conversacion"] = ConversationState.ESPERANDO_SELECCION_MENU_PRINCIPAL.name
        if chat_db_context:
            flag_modified(chat_db_context, "context_data")
        return _get_main_menu_payload(
            context,
            welcome_message_override="¡Listo! Empezamos de nuevo. ¿En qué te puedo ayudar?",
        )

    if action_id == "mostrar_menu_reclamos":
        submenu = _get_reclamos_consultas_menu()
        contexto_municipio_actual = context.get("chat_db_context_data", {}).setdefault(CONTEXTO_MUNICIPIO, {})
        contexto_municipio_actual["estado_conversacion"] = ConversationState.ESPERANDO_SELECCION_DE_LISTA.name
        contexto_municipio_actual["menu_opciones"] = submenu.get("options_list", [])
        if chat_db_context:
            flag_modified(chat_db_context, "context_data")
        return submenu

    if action_id == "iniciar_reclamo":
        contexto_municipio_actual = context.get("chat_db_context_data", {}).setdefault(CONTEXTO_MUNICIPIO, {})
        logger.info("[MENU_ACTION] Clearing previous claim context for new claim.")
        contexto_municipio_actual.pop("datos_parciales_llm_reclamo", None)
        contexto_municipio_actual.pop("historial_llm_reclamo", None)
        contexto_municipio_actual["estado_conversacion"] = ConversationState.ESPERANDO_SELECCION_MENU_RECLAMOS.name

        user_input = context.get("user_input_raw", "")
        reclamo_opts = _get_reclamos_menu().get("options_list", [])
        details = extract_reclamo_details_from_text(user_input, reclamo_opts)
        detected_category = details.pop("categoria_sugerida", None)
        handler = ReclamoFlowHandler(context, chat_db_context)
        if detected_category:
            logger.info(
                f"[MENU_ACTION] Auto-detected category '{detected_category}' from input."
            )
        # Map remaining suggested fields into initial data
        datos_iniciales = {}
        if details.get("descripcion_sugerida"):
            datos_iniciales["descripcion"] = details["descripcion_sugerida"]
        if details.get("direccion_sugerida"):
            datos_iniciales["direccion"] = details["direccion_sugerida"]
        response_dict = handler.start_flow(
            datos_iniciales=datos_iniciales or None,
            categoria_inicial=detected_category,
        )
        if chat_db_context:
            flag_modified(chat_db_context, "context_data")
        return response_dict

    if action_id == "mostrar_menu_tramites":
        submenu = _get_tramites_menu()
        contexto_municipio_actual = context.get("chat_db_context_data", {}).setdefault(CONTEXTO_MUNICIPIO, {})
        contexto_municipio_actual["estado_conversacion"] = ConversationState.ESPERANDO_SELECCION_DE_LISTA.name
        contexto_municipio_actual["menu_opciones"] = submenu.get("options_list", [])
        if chat_db_context:
            flag_modified(chat_db_context, "context_data")
        return submenu

    if action_id == "mostrar_menu_informacion":
        submenu = _get_informacion_menu()
        contexto_municipio_actual = context.get("chat_db_context_data", {}).setdefault(CONTEXTO_MUNICIPIO, {})
        contexto_municipio_actual["estado_conversacion"] = ConversationState.ESPERANDO_SELECCION_DE_LISTA.name
        contexto_municipio_actual["menu_opciones"] = submenu.get("options_list", [])
        if chat_db_context:
            flag_modified(chat_db_context, "context_data")
        return submenu

    if action_id == "mostrar_menu_estacionamiento":
        submenu = _get_estacionamiento_menu()
        contexto_municipio_actual = context.get("chat_db_context_data", {}).setdefault(CONTEXTO_MUNICIPIO, {})
        contexto_municipio_actual["estado_conversacion"] = ConversationState.ESPERANDO_SELECCION_DE_LISTA.name
        contexto_municipio_actual["menu_opciones"] = submenu.get("options_list", [])
        if chat_db_context:
            flag_modified(chat_db_context, "context_data")
        return submenu

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
        noticias_body, noticias_img = _get_posts_from_json("noticia", channel, municipio_id)
        eventos_body, eventos_img = _get_posts_from_json("evento", channel, municipio_id)

        full_body = ""
        if noticias_body:
            if channel == "whatsapp":
                full_body += "*🗞️ Noticias Recientes*\n\n" + noticias_body
            elif channel == "web":
                full_body += "🗞️ Noticias Recientes\n\n" + noticias_body
            else:
                full_body += "<h3>🗞️ Noticias Recientes</h3>" + noticias_body
        if eventos_body:
            if channel == "whatsapp":
                if full_body:
                    full_body += "\n\n"
                full_body += "*🎭 Próximos Eventos*\n\n" + eventos_body
            elif channel == "web":
                if full_body:
                    full_body += "\n\n"
                full_body += "🎭 Próximos Eventos\n\n" + eventos_body
            else:
                full_body += "<h3>🎭 Próximos Eventos</h3>" + eventos_body

        if not full_body:
            full_body = "No hay noticias ni eventos para mostrar en este momento."
            social_buttons = []
            first_image = None
        else:
            config_links = context.get("municipio_config_actual", {}).get("social_links", [])
            if channel == "web":
                social_body = "<hr>Seguinos en nuestras redes:"
            else:
                social_body = "\n---\nSeguinos en nuestras redes:"
            full_body += social_body
            social_buttons = [
                {
                    "texto": link.get("name"),
                    "url": link.get("url"),
                    "type": "url",
                    "image_url": link.get("logo_url"),
                }
                for link in config_links
            ]
            first_image = eventos_img or noticias_img

        response = {
            "message_body": full_body.strip(),
            "message_type": "interactive_buttons" if social_buttons else "text",
            "fuente": "handler_agenda_y_noticias",
        }
        if social_buttons:
            response["options_list"] = social_buttons
        if first_image:
            response["image_url"] = first_image
        return response

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
        body = data.get("descripcion", "")
        config_links = context.get("municipio_config_actual", {}).get("social_links", [])
        social_buttons = []
        if action_id in {"obras", "punto_limpio"} and config_links:
            body += "\n---\nSeguinos en nuestras redes:"
            social_buttons = [
                {
                    "texto": link.get("name"),
                    "url": link.get("url"),
                    "type": "url",
                    "image_url": link.get("logo_url"),
                }
                for link in config_links
            ]
        response = {
            "message_body": body,
            "options_list": botones + social_buttons,
            "message_type": "interactive_buttons" if botones or social_buttons else "text",
            "fuente": f"info_{action_id}_json",
        }
        image = data.get("image_url")
        if image:
            response["image_url"] = image
        return response

    if action_id == "compartir_ubicacion":
        contexto_municipio_actual = context.get("chat_db_context_data", {}).setdefault(CONTEXTO_MUNICIPIO, {})
        if (
            contexto_municipio_actual.get("estado_conversacion") is None
            and contexto_municipio_actual.get("ultima_consulta_poi")
        ):
            contexto_municipio_actual["estado_conversacion"] = ConversationState.ESPERANDO_UBICACION_GENERAL.name
            contexto_municipio_actual["consulta_pendiente_ubicacion"] = contexto_municipio_actual.get("ultima_consulta_poi")
            if chat_db_context:
                flag_modified(chat_db_context, "context_data")
            return {
                "message_body": "Para buscar estacionamientos necesito tu ubicación.",
                "options_list": [
                    {"texto": "Compartir ubicación", "action": "compartir_ubicacion"},
                    {"texto": "Cancelar", "action": "cancelar"},
                ],
                "message_type": "interactive_buttons",
                "fuente": "pedir_ubicacion_estacionamiento",
            }

    if action_id == "estacionamiento":
        contexto_municipio_actual = context.get("chat_db_context_data", {}).setdefault(CONTEXTO_MUNICIPIO, {})
        contexto_municipio_actual['estado_conversacion'] = ConversationState.ESPERANDO_UBICACION_GENERAL.name
        contexto_municipio_actual['consulta_pendiente_ubicacion'] = 'estacionamiento'
        contexto_municipio_actual['ultima_consulta_poi'] = 'estacionamiento'
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
            "options_list": [{"texto": "Solicitar Turno", "url": "https://tlc.mendoza.gov.ar/turnos", "type": "url"}],
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

BOTONES_COMANDOS_MUNICIPIO = {
    "Hacer un reclamo": "iniciar_reclamo",
    "Consultar estado de un trámite": "consultar_estado_ticket",
    "Consultar estado de ticket": "consultar_estado_ticket",
    "Consultar otro ticket": "consultar_estado_ticket",
    "Hablar con un agente": "hablar_con_agente",
    "Nuevo reclamo": "iniciar_reclamo",
    "Adjuntar foto": "adjuntar_foto",
    "Compartir ubicación": "compartir_ubicacion",
    "Foto": "adjuntar_foto",
    "Ubicación": "compartir_ubicacion",
    "No, continuar": "sin_adjuntos",
    "Completar reclamo": "sin_adjuntos",
    "Sí, confirmar reclamo": "confirmar_reclamo",
    "Si, confirmar reclamo": "confirmar_reclamo",
    "Confirmar reclamo": "confirmar_reclamo",
    "Finalizar": "confirmar_reclamo",
    "Finalizar reclamo": "confirmar_reclamo",
    "Confirmar": "confirmar_reclamo",
    "Confirmado": "confirmar_reclamo",
    "Si confirmo": "confirmar_reclamo",
    "Sí confirmo": "confirmar_reclamo",
    "Editar datos": "editar_reclamo",
    "Sí, solucionado": "confirmar_cierre_ticket",
    "No, aún no": "no_cerrar_ticket",
    "Volver al inicio": "menu_principal",
    "Cancelar": "cancelar",
    "Empezar de nuevo": "limpiar_contexto",
}

# Utiliza el orquestador de LLMs que intenta OpenAI y Cohere.
from services.llm_orchestrator import llamar_llm_con_fallback

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

    # --- Heuristic extraction before invoking the LLM ---
    reclamo_opts = _get_reclamos_menu().get("options_list", []) if '_get_reclamos_menu' in globals() else []
    detalles_rapidos = extract_reclamo_details_from_text(pregunta_str, reclamo_opts, use_llm=False)
    if detalles_rapidos:
        datos_reclamo = contexto_municipio_actual.setdefault("datos_parciales_llm_reclamo", {})
        if detalles_rapidos.get("categoria_sugerida") and not datos_reclamo.get("categoria"):
            datos_reclamo["categoria"] = detalles_rapidos["categoria_sugerida"]
        if detalles_rapidos.get("descripcion_sugerida") and not datos_reclamo.get("descripcion"):
            datos_reclamo["descripcion"] = detalles_rapidos["descripcion_sugerida"]
        if detalles_rapidos.get("direccion_sugerida") and not datos_reclamo.get("ubicacion"):
            datos_reclamo["ubicacion"] = detalles_rapidos["direccion_sugerida"]
        if detalles_rapidos.get("distrito_sugerido") and not datos_reclamo.get("distrito"):
            datos_reclamo["distrito"] = detalles_rapidos["distrito_sugerido"]

        tiene_categoria = bool(datos_reclamo.get("categoria"))
        tiene_ubicacion = bool(datos_reclamo.get("ubicacion"))

        if tiene_categoria and tiene_ubicacion:
            if not datos_reclamo.get("descripcion"):
                datos_reclamo["descripcion"] = detalles_rapidos.get("descripcion_sugerida", pregunta_str)
            if chat_db_context:
                flag_modified(chat_db_context, "context_data")
            handler = CrearReclamoActionHandler(context)
            return handler.execute(datos_reclamo), contexto_municipio_actual
        if tiene_categoria and not tiene_ubicacion:
            contexto_municipio_actual["estado_conversacion"] = ConversationState.ESPERANDO_DIRECCION_RECLAMO.name
            if chat_db_context:
                flag_modified(chat_db_context, "context_data")
            return {
                "message_body": (
                    "Para avanzar necesito la ubicación exacta del problema: calle, número y barrio o distrito; "
                    "si es en una esquina, indicá las calles aledañas."
                ),
                "options_list": [],
                "message_type": "text",
            }, contexto_municipio_actual
        if tiene_ubicacion and not tiene_categoria:
            contexto_municipio_actual["estado_conversacion"] = ConversationState.ESPERANDO_CATEGORIA_RECLAMO.name
            if chat_db_context:
                flag_modified(chat_db_context, "context_data")
            return {
                "message_body": "¿Qué tipo de problema es? (por ejemplo arbolado, luminaria, limpieza)",
                "options_list": [],
                "message_type": "text",
            }, contexto_municipio_actual

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

    historial_formateado = []
    if (
        historial_para_llm
        and isinstance(historial_para_llm, list)
        and historial_para_llm
        and "pregunta_usuario" in historial_para_llm[0]
    ):
        for turno in historial_para_llm:
            pregunta = turno.get("pregunta_usuario")
            respuesta = turno.get("respuesta_ia")
            if pregunta:
                historial_formateado.append({"role": "user", "parts": [{"text": pregunta}]})
            if respuesta:
                historial_formateado.append({"role": "model", "parts": [{"text": respuesta}]})
    else:
        historial_formateado = historial_para_llm or []

    try:
        # FIX: Pre-process expected data to prevent state loss if LLM fails to return it
        campo_esperado = contexto_municipio_actual.get("esperando_info_llm_reclamo")
        if context.get("es_ubicacion") and context.get("ubicacion_usuario"):
             campo_esperado = "ubicacion"

        if isinstance(campo_esperado, str) and (pregunta_str or context.get("es_ubicacion")):
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
            mensaje_para_llm = json.dumps(mensaje_completo_para_llm)
            respuesta_llm_dict, context_dict = llamar_llm_con_fallback(
                app=app,
                mensaje_usuario=mensaje_para_llm,
                usuario=usuario_info_llm,
                historial=historial_formateado,
                chat_session_id=context.get("chat_session_uuid")
            )
            logger.info(f"[HANDLE_LLM] Respuesta LLM: {respuesta_llm_dict}")
            if isinstance(context_dict, dict) and chat_db_context:
                chat_db_context.context_data.update(context_dict)
            logger_actual.info(f"[HANDLE_LLM] Accion backend LLM: {respuesta_llm_dict.get('accion_backend')}")
        except Exception as e:
            logger.error(
                f"[RESPONDER_MUNICIPIO_LLM_ERROR] Error general en la llamada al LLM: {e}",
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
        if isinstance(pedir_info_llm, str) and "," in pedir_info_llm:
            pedir_info_llm = [p.strip() for p in pedir_info_llm.split(",") if p.strip()]
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
    "mostrar_menu_reclamos": [
        "reclamo",
        "reclamos",
        "denuncia",
        "problema",
        "queja",
        "reportar",
        "averia",
        "averias",
        "incidente",
        "consulta",
        "consultas",
        "pregunta",
        "preguntas",
    ],
    "iniciar_reclamo": ["iniciar reclamo", "hacer reclamo", "nuevo reclamo", "realizar reclamo", "presentar reclamo", "registrar queja"],
    "solicitar_turnos": ["turnos", "turno", "solicitar turno", "pedir turno", "turnos online", "reservar turno", "agendar turno"],
    "licencia_de_conducir": ["licencia", "conducir", "carnet", "registro", "renovar licencia", "sacar licencia", "tramitar licencia", "registro de conducir"],
    "enviar_sugerencia": [
        "sugerencia",
        "sugerir",
        "propuesta",
        "pedido",
        "pedir algo",
        "comentario",
        "feedback",
        "opinion",
        "opinión",
        "hacer una sugerencia",
        "quiero hacer una sugerencia",
        "queria hacer una sugerencia",
        "quisiera hacer una sugerencia",
        "tengo una sugerencia",
        "tengo un comentario",
        "me gustaria hacer una sugerencia",
    ],
    "limpiar_contexto": [
        "cancelar",
        "volver al inicio",
        "empezar de nuevo",
        "reiniciar",
        "resetear",
        "limpiar chat",
        "borrar conversacion",
        "nuevo tema",
        "volver a empezar",
        "borrar historial",
        "limpiar memoria",
        "arrancar de cero",
    ],
    "consultar_estado_reclamo": [
        "consultar reclamo",
        "estado reclamo",
        "seguimiento",
        "ver reclamo",
        "consultar estado de reclamo",
        "consultar estado del reclamo",
        "estado de reclamo",
        "estado del reclamo",
    ],

    # Información útil
    "contactos_utiles": ["contactos", "contacto", "telefonos", "telefono", "utiles", "directorio", "llamar", "medios de contacto", "telefonos utiles"],
    "agenda_y_noticias": ["agenda", "cultural", "eventos", "noticias", "novedades", "informacion", "actividades", "eventos culturales"],
    "veterinaria_bromatologia": [
        "veterinaria", "bromatologia", "zoonosis", "animales", "animal",
        "perro", "perros", "gato", "gatos", "mascota", "mascotas",
        "vacuna", "vacunas", "vacunacion", "antirrabica", "antirrábica",
        "rabia", "perrera", "sanidad animal", "sanidad_animal"
    ],
    "defensa_del_consumidor": ["defensa del consumidor", "consumidor", "consumo", "proteccion al consumidor", "atencion al consumidor"],
    "obras": [
        "obras", "obra", "cuadrillas", "cloacas", "pavimento",
        "pavimentacion", "asfalto", "trabajos"
    ],
    "punto_limpio": [
        "punto limpio", "reciclaje", "reciclar", "planta de reciclaje",
        "punto verde", "residuos secos", "sustentable"
    ],

    # Tasas y Servicios
    "pago_de_tasas_vigentes": ["pagar", "pago", "tasas", "tasa", "boleta", "impuestos", "municipal", "tributo", "tributos", "arancel", "aranceles", "impuesto municipal", "impuestos municipales"],
    "buscar_estacionamiento": ["estacionamiento", "estacionar", "aparcamiento", "parking", "estacionar auto", "donde estacionar", "lugar para estacionar"],
    "recoleccion_residuos": ["recoleccion", "residuos", "basura", "basurero", "cuando pasa el camion", "recolector", "recogida", "recoleccion de basura"]
}

from fuzzywuzzy import process

def handle_location_for_reclamo(context, viewer_user, owner_user, chat_db_context, contexto_municipio_actual, location_data):
    """Attach a received location to an ongoing claim and create the ticket if possible."""
    datos_reclamo = contexto_municipio_actual.setdefault("datos_parciales_llm_reclamo", {})
    lat = location_data.get("latitude")
    lon = location_data.get("longitude")
    address = location_data.get("address") or f"Lat: {lat}, Lon: {lon}"
    datos_reclamo["coordenadas"] = {"lat": lat, "lon": lon}
    datos_reclamo.setdefault("ubicacion", address)

    tiene_categoria = bool(datos_reclamo.get("categoria"))
    tiene_descripcion = bool(datos_reclamo.get("descripcion"))

    if tiene_categoria:
        if not tiene_descripcion:
            datos_reclamo["descripcion"] = context.get("pregunta_actual_usuario", "") or ""
        handler = CrearReclamoActionHandler(context)
        respuesta = handler.execute(datos_reclamo)
        contexto_municipio_actual["estado_conversacion"] = ConversationState.CONVERSACION_GENERAL_LLM.name
    else:
        contexto_municipio_actual["estado_conversacion"] = ConversationState.ESPERANDO_CATEGORIA_RECLAMO.name
        respuesta = {
            "message_body": "¿Qué tipo de problema es? (por ejemplo arbolado, luminaria, limpieza)",
            "options_list": [],
            "message_type": "text",
        }
    if chat_db_context:
        flag_modified(chat_db_context, "context_data")
    return respuesta, contexto_municipio_actual

def find_menu_action_by_input(user_input: str, menu_buttons: list) -> str | None:
    """
    Finds a menu action based on user input, checking for exact match, number, first letter, or keywords.
    """
    if not user_input or not menu_buttons:
        return None

    # 0. Direct action_id match to support clients sending the action identifier
    normalized_action = normalizar_texto(user_input.strip())
    for button in menu_buttons:
        action_id_norm = normalizar_texto(button.get("action_id", ""))
        if action_id_norm and action_id_norm == normalized_action:
            logger.info(
                f"DEBUG: Direct action_id match found for '{user_input}'. Action: {button.get('action_id')}"
            )
            return button.get("action_id")

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

    # 4. For longer free-form phrases, skip fuzzy matching to avoid
    # misclassifying natural sentences as menu keywords. Let higher-level
    # NLU or LLM logic handle these cases instead.
    if len(normalized_input.split()) > 7:
        logger.info(
            f"DEBUG: Skipping fuzzy match for long input: '{normalized_input}'"
        )
        logger.warning(
            f"DEBUG: No menu action found for input: '{user_input}' (normalized: '{normalized_input}')"
        )
        return None

    # 5. Check for keyword match (fuzzy matching for natural language)
    local_keywords = {}
    for button in menu_buttons:
        action_id = button.get('action_id')
        if action_id in MENU_KEYWORDS:
            for keyword in MENU_KEYWORDS[action_id]:
                local_keywords[keyword] = action_id

    if local_keywords:
        best_match, score = process.extractOne(
            normalized_input, local_keywords.keys()
        )

        if score > 80:
            # Added log for debugging
            logger.info(
                f"DEBUG: Fuzzy match found for '{normalized_input}' with keyword '{best_match}' (score: {score}). Action: {local_keywords[best_match]}"
            )
            return local_keywords[best_match]

    # Added log for debugging
    logger.warning(
        f"DEBUG: No menu action found for input: '{user_input}' (normalized: '{normalized_input}')"
    )
    return None

def find_global_menu_action(user_input: str) -> str | None:
    """Attempts to resolve a menu action purely by keywords, ignoring menu context."""
    global_buttons = [{"texto": aid, "action_id": aid} for aid in MENU_KEYWORDS.keys()]
    return find_menu_action_by_input(user_input, global_buttons)


def _detect_reclamo_during_sugerencia(pregunta_str: str, contexto_municipio_actual: dict, context: dict, chat_db_context) -> dict | None:
    """If the user mentions starting a complaint while in the suggestion flow,
    abort the suggestion workflow and start the regular complaint flow."""
    normalized = normalizar_texto(pregunta_str or "")
    if "reclamo" in normalized:
        contexto_municipio_actual.pop('datos_sugerencia', None)
        contexto_municipio_actual.pop('ubicacion_contextual_sugerencia', None)
        contexto_municipio_actual.pop('estado_conversacion', None)
        handler = ReclamoFlowHandler(context, chat_db_context)
        response = handler.start_flow(datos_iniciales={"descripcion": pregunta_str})
        if chat_db_context:
            flag_modified(chat_db_context, "context_data")
        return response
    return None

RECLAMO_KEYWORDS = {
    "Luminaria": [
        "luminaria",
        "luz",
        "poste",
        "foco",
        "farol",
        "farola",
        "iluminacion",
        "lampara",
        "poste caido",
        "poste caído",
        "alumbrado",
        "luz quemada",
        "foco quemado",
    ],
    "Arbolado": [
        "arbolado",
        "arbol",
        "arboles",
        "rama",
        "ramas",
        "arbol caido",
        "árbol caído",
        "tronco",
        "gajo",
        "poda",
        "poda de arbol",
        "arbol seco",
        "raiz",
        "raíz",
        "raices",
        "raíces",
        "planta",
        "plantas",
        "arbusto",
        "arbol en medianera",
        "arbol invade",
    ],
    "Limpieza y riego": [
        "limpieza",
        "riego",
        "basura",
        "basural",
        "contenedor",
        "escombros",
        "mugre",
        "pasto",
        "yuyos",
        "maleza",
        "desmalezado",
        "baldio",
        "residuos",
        "suciedad",
        "carton",
        "cartón",
        "plastico",
        "plástico",
        "hojas",
    ],
    "Arreglo de calle": [
        "calle",
        "bache",
        "pozo",
        "asfalto",
        "vereda",
        "agujero",
        "hueco",
        "pavimento",
        "calzada",
        "calle rota",
        "pavimento levantado",
        "asfalto roto",
        "pozo en la calle",
        "vereda rota",
    ],
    "Pérdida de agua": [
        "agua",
        "perdida",
        "caño",
        "cañeria",
        "fuga",
        "rotura",
        "tuberia",
        "agua servida",
        "canilla rota",
        "cisterna",
    ],
    "Otros": ["otros", "otro", "varios"],
}

def find_reclamo_category_by_input(user_input: str, reclamo_options: list) -> str | None:
    """
    Finds a reclamo category based on user input, checking for number, first letter, or keywords.
    """
    if not user_input:
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


def extract_reclamo_details_from_text(user_input: str, reclamo_options: list, use_llm: bool = True) -> dict:
    """Attempt to extract category, description and address from a user message.

    Heuristic keyword checks are applied first so simple claims can be resolved
    without calling the LLM. If essential data remains missing the LLM is
    invoked to complete the extraction.
    """
    details: dict[str, str] = {}
    if not user_input:
        return details

    # --- Heuristic extraction (cheap) ---
    category = find_reclamo_category_by_input(user_input, reclamo_options)
    if category:
        details["categoria_sugerida"] = category
        details.setdefault("descripcion_sugerida", user_input)

    import re
    normalized = user_input.strip()
    if re.search(r"\besquina\b", normalized, re.IGNORECASE):
        before, after = re.split(r"\besquina", normalized, maxsplit=1, flags=re.IGNORECASE)
        addr_match = re.search(r"([A-Za-zÀ-ÿ'\s]+?)\s+(\d{1,5})", before)
        if addr_match:
            street = addr_match.group(1).strip()
            number = addr_match.group(2)
            parts = after.strip().split()
            cross = ""
            district = None
            if len(parts) >= 4:
                cross = " ".join(parts[:-2])
                district = " ".join(parts[-2:])
            elif len(parts) == 3:
                cross = " ".join(parts[:-1])
                district = parts[-1]
            elif len(parts) == 2:
                cross = parts[0]
                district = parts[1]
            elif parts:
                cross = parts[0]
            direccion = f"{street} {number}"
            if cross:
                direccion += f" esquina {cross}"
            details["direccion_sugerida"] = direccion
            if district:
                details["distrito_sugerido"] = district
    if "direccion_sugerida" not in details:
        match = re.search(r"([A-Za-zÀ-ÿ'\s]+?)\s+(\d{1,5})", normalized)
        if match:
            details["direccion_sugerida"] = f"{match.group(1).strip()} {match.group(2)}"

    # --- LLM extraction only if heuristics incomplete ---
    need_llm = "categoria_sugerida" not in details
    if need_llm and use_llm:
        llm_details = extract_complaint_details_llm(user_input) or {}
        if llm_details.get("tipo_problema") and "categoria_sugerida" not in details:
            mapped = find_reclamo_category_by_input(
                llm_details["tipo_problema"], reclamo_options
            )
            if mapped:
                details["categoria_sugerida"] = mapped
        if llm_details.get("descripcion_problema") and "descripcion_sugerida" not in details:
            details["descripcion_sugerida"] = llm_details["descripcion_problema"]
        if llm_details.get("ubicacion_problema") and "direccion_sugerida" not in details:
            details["direccion_sugerida"] = llm_details["ubicacion_problema"]
        if llm_details.get("distrito_problema") and "distrito_sugerido" not in details:
            details["distrito_sugerido"] = llm_details["distrito_problema"]
        if llm_details.get("nombre_usuario"):
            details["nombre_sugerido"] = llm_details["nombre_usuario"]
        if llm_details.get("telefono_usuario"):
            details["telefono_sugerido"] = llm_details["telefono_usuario"]
        if llm_details.get("email_usuario"):
            details["email_sugerido"] = llm_details["email_usuario"]
        if llm_details.get("dni_usuario"):
            details["dni_sugerido"] = llm_details["dni_usuario"]

    # --- Contact extraction (may still use LLM) ---
    if use_llm:
        missing_contact_fields = []
        if "nombre_sugerido" not in details:
            missing_contact_fields.append("nombre_cliente")
        if "telefono_sugerido" not in details:
            missing_contact_fields.append("telefono_cliente")
        if "email_sugerido" not in details:
            missing_contact_fields.append("email_cliente")
        if "dni_sugerido" not in details:
            missing_contact_fields.append("dni_cliente")

        if missing_contact_fields:
            contact_details = extract_multiple_contact_details_llm(
                user_input, missing_contact_fields
            ) or {}
            if contact_details.get("nombre_cliente"):
                details["nombre_sugerido"] = contact_details["nombre_cliente"]
            if contact_details.get("telefono_cliente"):
                details["telefono_sugerido"] = contact_details["telefono_cliente"]
            if contact_details.get("email_cliente"):
                details["email_sugerido"] = contact_details["email_cliente"]
            if contact_details.get("dni_cliente"):
                details["dni_sugerido"] = contact_details["dni_cliente"]
            if contact_details.get("direccion_cliente") and "direccion_sugerida" not in details:
                details["direccion_sugerida"] = contact_details["direccion_cliente"]

    # --- Regex fallback for location if still missing ---
    if "direccion_sugerida" not in details:
        import re
        match = re.search(r"en\s+([A-Za-zÀ-ÿ'\s]+?)\s+(\d{1,5})", user_input, re.IGNORECASE)
        if match:
            details["direccion_sugerida"] = f"{match.group(1).strip()} {match.group(2)}"

    return details


def _get_reclamos_consultas_menu():
    opciones = [
        {"texto": "📝 Iniciar un Reclamo", "action_id": "iniciar_reclamo"},
        {"texto": "💡 Enviar una Sugerencia", "action_id": "enviar_sugerencia"},
        {"texto": "🤔 Consultar Estado de Reclamo", "action_id": "consultar_estado_reclamo"},
        {"texto": "📞 Contactos Útiles", "action_id": "contactos_utiles"},
        {"texto": "*Volver al inicio*", "action_id": "menu_principal"},
        {"texto": "Cancelar", "action_id": "cancelar"},
    ]
    return {
        "message_body": "Elegí una opción para tu consulta:",
        "message_type": "interactive_buttons",
        "options_list": opciones,
        "fuente": "submenu_reclamos_consultas_v1",
        "generar_audio": True,
    }


def _get_tramites_menu():
    opciones = [
        {"texto": "*Volver al inicio*", "action_id": "menu_principal"},
        {"texto": "🚗 Licencia de Conducir", "action_id": "licencia_de_conducir"},
        {"texto": "🗓️ Solicitar Otros Turnos", "action_id": "solicitar_turnos"},
        {"texto": "💵 Pagar Tasas Municipales", "action_id": "pago_de_tasas_vigentes"},
    ]
    return {
        "message_body": "Elegí una opción:",
        "message_type": "interactive_buttons",
        "options_list": opciones,
        "fuente": "submenu_tramites_v1",
        "generar_audio": True,
    }


def _get_informacion_menu():
    opciones = [
        {"texto": "*Volver al inicio*", "action_id": "menu_principal"},
        {"texto": "🎭 Agenda Cultural y Noticias", "action_id": "agenda_y_noticias"},
        {"texto": "🐾 Veterinaria y Bromatología", "action_id": "veterinaria_bromatologia"},
        {"texto": "🏗️ Obras", "action_id": "obras"},
        {"texto": "♻️ Punto Limpio", "action_id": "punto_limpio"},
    ]
    return {
        "message_body": "Seleccioná una opción:",
        "message_type": "interactive_buttons",
        "options_list": opciones,
        "fuente": "submenu_informacion_v1",
        "generar_audio": True,
    }


def _get_estacionamiento_menu():
    opciones = [
        {"texto": "*Volver al inicio*", "action_id": "menu_principal"},
        {"texto": "🅿️ Buscar Estacionamiento Libre", "action_id": "buscar_estacionamiento"},
    ]
    return {
        "message_body": "Opciones de estacionamiento:",
        "message_type": "interactive_buttons",
        "options_list": opciones,
        "fuente": "submenu_estacionamiento_v1",
        "generar_audio": True,
    }

def _get_reclamos_menu():
    """Devuelve la estructura del menú de reclamos estandarizado, con íconos y negritas."""
    opciones = [
        {"texto": "*Volver al inicio*", "action_id": "0", "category_name": "Volver al inicio"},
        {"texto": "💡 *Luminaria*", "action_id": "1", "category_name": "Luminaria"},
        {"texto": "🌳 *Arbolado*", "action_id": "2", "category_name": "Arbolado"},
        {"texto": "🗑️ *Limpieza y riego*", "action_id": "3", "category_name": "Limpieza y riego"},
        {"texto": "🚧 *Arreglo de calle*", "action_id": "4", "category_name": "Arreglo de calle"},
        {"texto": "💧 *Pérdida de agua*", "action_id": "5", "category_name": "Pérdida de agua"},
        {"texto": "⚫ *Otros*", "action_id": "6", "category_name": "Otros"},
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

    normalized_question = None

    def _finalize_response(response):
        """Return the response unchanged; also store it in cache for repeated queries."""
        if normalized_question:
            MUNICIPIO_RESPONSE_CACHE[normalized_question] = response
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

    normalized_question = normalizar_texto(pregunta_str)
    if normalized_question in MUNICIPIO_RESPONSE_CACHE:
        logger_actual.info("responder_municipio: returning cached response")
        return MUNICIPIO_RESPONSE_CACHE[normalized_question]

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

    # --- Manejo rápido de reclamos detectados vía imagen ---
    datos_interpretados_archivo = context.get("datos_interpretados_archivo")
    flujo_activo = (
        context.get("chat_db_context_data", {})
        .get(CONTEXTO_MUNICIPIO, {})
        .get("reclamo_flow_v2", {})
        .get("state")
    )
    if (
        datos_interpretados_archivo
        and isinstance(datos_interpretados_archivo, dict)
        and datos_interpretados_archivo.get("es_reclamo")
    ):
        handler = ReclamoFlowHandler(context, chat_db_context)
        if flujo_activo:
            # Merge suggested data into the existing flow and continue
            flow_data = (
                context["chat_db_context_data"][CONTEXTO_MUNICIPIO]
                .setdefault("reclamo_flow_v2", {})
                .setdefault("datos_reclamo", {})
            )
            flow_data.setdefault(
                "categoria", datos_interpretados_archivo.get("categoria_sugerida")
            )
            flow_data.setdefault(
                "descripcion", datos_interpretados_archivo.get("descripcion_sugerida")
            )
            flow_data["foto_url"] = received_payload.get("foto_url")
            response = handler.handle("", received_payload)
            if chat_db_context:
                flag_modified(chat_db_context, "context_data")
            return _finalize_response(response)
        else:
            logger_actual.info("Auto-starting claim flow from image analysis")
            datos_iniciales = {
                "categoria": datos_interpretados_archivo.get("categoria_sugerida"),
                "descripcion": datos_interpretados_archivo.get("descripcion_sugerida"),
                "origen_descripcion": "imagen",
                "foto_url": received_payload.get("foto_url"),
            }
            response = handler.start_flow(datos_iniciales=datos_iniciales)
            if chat_db_context:
                flag_modified(chat_db_context, "context_data")
            return _finalize_response(response)

    # --- INICIO: Análisis de Imágenes Multimodal ---
    if received_payload.get("es_foto") and received_payload.get("foto_url"):
        if flujo_activo:
            handler = ReclamoFlowHandler(context, chat_db_context)
            response_dict = handler.handle("", received_payload)
            if chat_db_context:
                flag_modified(chat_db_context, "context_data")
            return _finalize_response(response_dict)

        logger_actual.info(
            f"Image received. Starting multimodal analysis for URL: {received_payload.get('foto_url')}"
        )

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

        analysis_result = analizar_imagen_con_fallback(
            received_payload.get("foto_url"), vision_prompt
        )

        if analysis_result and analysis_result.get("raw_response"):
            try:
                parsed_response = json.loads(analysis_result.get("raw_response"))
                if parsed_response.get("intent") == "crear_reclamo":
                    logger_actual.info(
                        f"Multimodal analysis successful. Intent: 'crear_reclamo'. Data: {parsed_response.get('data')}"
                    )

                    datos_iniciales = parsed_response.get("data", {})
                    datos_iniciales['origen_descripcion'] = 'imagen'
                    datos_iniciales['foto_url'] = received_payload.get("foto_url")

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
        contexto_municipio_actual = context.get("chat_db_context_data", {}).setdefault(CONTEXTO_MUNICIPIO, {})

        if contexto_municipio_actual.get("reclamo_flow_v2", {}).get("state") == ReclamoState.ESPERANDO_DIRECCION.name:
            handler = ReclamoFlowHandler(context, chat_db_context)
            response_dict = handler.handle("", received_payload)
            if chat_db_context:
                flag_modified(chat_db_context, "context_data")
            return _finalize_response(response_dict)

        estado = contexto_municipio_actual.get("estado_conversacion")
        if (
            estado in {
                ConversationState.ESPERANDO_DIRECCION_RECLAMO.name,
                ConversationState.ESPERANDO_CONFIRMACION_UBICACION.name,
            }
            or contexto_municipio_actual.get("datos_parciales_llm_reclamo")
        ):
            loc = received_payload.get("ubicacion_usuario", {})
            lat = loc.get("latitude")
            lon = loc.get("longitude")
            address = loc.get("address") or f"Lat: {lat}, Lon: {lon}"
            datos = contexto_municipio_actual.setdefault(
                "datos_parciales_llm_reclamo", {}
            )
            datos["coordenadas"] = {"lat": lat, "lon": lon}
            datos["ubicacion"] = address
            contexto_municipio_actual[
                "estado_conversacion"
            ] = ConversationState.ESPERANDO_CONFIRMACION_UBICACION.name
            opciones = [
                {"texto": "Confirmar", "action_id": "confirmar_ubicacion"},
                {"texto": "Editar", "action_id": "editar_ubicacion"},
            ]
            if chat_db_context:
                flag_modified(chat_db_context, "context_data")
            return (
                _finalize_response(
                    {
                        "message_body": f"¿Es esta tu dirección: {address}?",
                        "options_list": opciones,
                        "message_type": "interactive_buttons",
                        "fuente": "confirmar_ubicacion",
                    }
                ),
                contexto_municipio_actual,
            )

        # When already waiting for a location to answer a pending query (e.g., estacionamiento),
        # skip proactive handling so that the dedicated state logic can process it.
        if contexto_municipio_actual.get("estado_conversacion") != ConversationState.ESPERANDO_UBICACION_GENERAL.name:
            ultima_consulta = contexto_municipio_actual.get("ultima_consulta_poi")
            if ultima_consulta:
                logger_actual.info(
                    f"Location received for last POI query '{ultima_consulta}'."
                )
                return _finalize_response(
                    PointsOfInterestHandler(context={}).handle(
                        {
                            "pregunta": ultima_consulta,
                            "location": received_payload.get("ubicacion_usuario"),
                        }
                    )
                )

            logger_actual.info(
                "Location received without text. Starting proactive location handling."
            )

            address = received_payload.get("ubicacion_usuario", {}).get("address", "la ubicación que compartiste")

            contexto_municipio_actual['estado_conversacion'] = ConversationState.ESPERANDO_INTENCION_UBICACION.name
            opciones_proactivas = [
                {"texto": "Iniciar un Reclamo", "action_id": "iniciar_reclamo_con_ubicacion"},
                {"texto": "Enviar una Sugerencia", "action_id": "enviar_sugerencia_con_ubicacion"},
                {"texto": "Cancelar", "action_id": "cancelar"},
            ]
            contexto_municipio_actual['ubicacion_contextual'] = received_payload.get("ubicacion_usuario")
            contexto_municipio_actual['menu_opciones'] = opciones_proactivas
            if chat_db_context:
                flag_modified(chat_db_context, "context_data")

            return _finalize_response({
                "message_body": f"Recibí tu ubicación en *{address}*. ¿Qué te gustaría hacer?",
                "options_list": opciones_proactivas,
                "message_type": "interactive_buttons",
                "fuente": "proactive_location_handler",
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
    context["user_input_raw"] = pregunta_str
    normalized_input_menu = normalizar_texto(pregunta_str or "")
    action_id = received_payload.get("action_id") or received_payload.get("action")
    if normalized_input_menu in {"menu", "menu principal"} or action_id == "menu_principal":
        contexto_municipio_actual.pop("reclamo_flow_v2", None)
        handler = GreetingHandler(context)
        response = handler.handle(received_payload)
        if chat_db_context:
            flag_modified(chat_db_context, "context_data")
        return _finalize_response(response)
    # --- INICIO: Manejo del Flujo de Reclamos Activo ---
    if "reclamo_flow_v2" in contexto_municipio_actual and contexto_municipio_actual["reclamo_flow_v2"].get("state"):
        logger_actual.info(f"Reclamo flow is active. State: {contexto_municipio_actual['reclamo_flow_v2'].get('state')}. Handing off to ReclamoFlowHandler.")
        handler = ReclamoFlowHandler(context, chat_db_context)
        response = handler.handle(pregunta_str, received_payload)
        safe_flag_modified(chat_db_context, "context_data")
        return _finalize_response(response)
    # --- FIN: Manejo del Flujo de Reclamos Activo ---

    # --- START GLOBAL MENU SHORTCUTS ---
    if not contexto_municipio_actual.get("estado_conversacion") and pregunta_str:
        inferred_action = find_global_menu_action(pregunta_str)
        if inferred_action:
            contexto_municipio_actual['estado_conversacion'] = None
            if chat_db_context:
                flag_modified(chat_db_context, "context_data")
            response = handle_main_menu_action(inferred_action, context, chat_db_context)
            if response:
                return _finalize_response(response)
    # --- END GLOBAL MENU SHORTCUTS ---

    # --- START DIRECT RECLAMO DETECTION FOR TEXT OR AUDIO ---
    if not contexto_municipio_actual.get("estado_conversacion"):
        reclamo_options = _get_reclamos_menu().get("options_list", [])
        plain_text_options = [{"texto": opt.get("category_name")} for opt in reclamo_options]
        details = extract_reclamo_details_from_text(pregunta_str, plain_text_options)
        detected_category = details.pop("categoria_sugerida", None)
        if detected_category:
            handler = ReclamoFlowHandler(context, chat_db_context)
            datos_iniciales = {}
            if details.get("descripcion_sugerida"):
                datos_iniciales["descripcion"] = details["descripcion_sugerida"]
            if details.get("direccion_sugerida"):
                datos_iniciales["direccion"] = details["direccion_sugerida"]
            response_dict = handler.start_flow(
                datos_iniciales=datos_iniciales or None,
                categoria_inicial=detected_category,
            )
            if chat_db_context:
                flag_modified(chat_db_context, "context_data")
            return _finalize_response(response_dict)
    # --- END DIRECT RECLAMO DETECTION FOR TEXT OR AUDIO ---

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

    # If the user is asking for a general point of interest (e.g., farmacias,
    # veterinarias) handle it with the PointsOfInterestHandler. This needs to
    # happen before fuzzy matching to menu keywords to avoid misclassifications
    # such as interpreting "farmacias de turno" as a request for appointments.
    if es_consulta_general(pregunta_str):
        poi_handler = PointsOfInterestHandler(context)
        loc_data = None
        location = received_payload.get("location")
        if isinstance(location, dict):
            loc_data = location
        elif isinstance(location, str):
            loc_data = location
        contexto_municipio_actual = context.get("chat_db_context_data", {}).setdefault(CONTEXTO_MUNICIPIO, {})
        # Clear any pending claim-related context since the user switched topics
        for campo in [
            "historial_llm_reclamo",
            "esperando_info_llm_reclamo",
            "esperando_info_llm",
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
        # Ensure datos_parciales_llm_reclamo exists as empty dict
        contexto_municipio_actual["datos_parciales_llm_reclamo"] = {}
        contexto_municipio_actual["estado_conversacion"] = ConversationState.CONVERSACION_GENERAL_LLM.name
        if chat_db_context:
            flag_modified(chat_db_context, "context_data")
        return _finalize_response(poi_handler.handle({"pregunta": pregunta_str, "location": loc_data}))


    # El manejo de reseteo por palabra clave ahora es manejado por el LLM
    # que debe devolver accion_backend: "saludar".

    # --- RESTRUCTURED LOGIC ---
    # Obtener el estado actual de la conversación antes de evaluar acciones
    estado_conversacion = contexto_municipio_actual.get("estado_conversacion")
    action = received_payload.get("action")

    # If the client sends an explicit action (e.g., button press) and there is
    # no active conversation state, handle it immediately via the main menu
    # dispatcher. This allows frontend buttons to work even when they send an
    # action identifier instead of free-form text.
    if action and not estado_conversacion:
        response = handle_main_menu_action(action, context, chat_db_context)
        if response and not response.get("fuente", "").startswith("unimplemented_"):
            return _finalize_response(response)

    # Allow keyword shortcuts even when a conversation state is active,
    # but avoid treating numeric replies or explicit action IDs as global
    # menu shortcuts so that selections like "3" or "mostrar_menu_*" are
    # handled within their local context.
    main_actions = {
        normalizar_texto(btn.get("action_id", ""))
        for btn in _get_main_menu_payload(context).get("options_list", [])
    }
    if (
        not action
        and pregunta_str
        and not pregunta_str.strip().isdigit()
        and normalizar_texto(pregunta_str) not in main_actions
        and estado_conversacion != ConversationState.ESPERANDO_INTENCION_UBICACION.name
        and estado_conversacion != ConversationState.ESPERANDO_CONFIRMACION_SUGERENCIA.name
        and estado_conversacion != ConversationState.ESPERANDO_TEXTO_SUGERENCIA.name
        and estado_conversacion != ConversationState.ESPERANDO_DATOS_CONTACTO_SUGERENCIA.name
    ):
        inferred_action = find_global_menu_action(pregunta_str)
        if inferred_action:
            contexto_municipio_actual['estado_conversacion'] = None
            if chat_db_context:
                flag_modified(chat_db_context, "context_data")
            response = handle_main_menu_action(inferred_action, context, chat_db_context)
            if response:
                return _finalize_response(response)

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
            if not selected_action:
                selected_action = find_global_menu_action(pregunta_str_menu)

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
                        if option.get("action_id") == pregunta_str_reclamo:
                            selected_category_name = option.get("category_name")
                            break
                if not selected_category_name:
                    plain_text_options = [{"texto": opt.get("category_name")} for opt in reclamo_options]
                    details = extract_reclamo_details_from_text(pregunta_str_reclamo, plain_text_options)
                    selected_category_name = details.pop("categoria_sugerida", None)

            if selected_category_name:
                handler = ReclamoFlowHandler(context, chat_db_context)
                datos_iniciales = {}
                if details.get("descripcion_sugerida"):
                    datos_iniciales["descripcion"] = details["descripcion_sugerida"]
                if details.get("direccion_sugerida"):
                    datos_iniciales["direccion"] = details["direccion_sugerida"]
                response_dict = handler.start_flow(
                    datos_iniciales=datos_iniciales or None,
                    categoria_inicial=selected_category_name,
                )
                if chat_db_context:
                    flag_modified(chat_db_context, "context_data")
                return _finalize_response(response_dict)
            else:
                return _finalize_response(_get_reclamos_menu())

        elif estado_conversacion == ConversationState.ESPERANDO_INTENCION_UBICACION.name:
            ubicacion_contextual = contexto_municipio_actual.pop('ubicacion_contextual', None)
            address = ubicacion_contextual.get('address', 'la ubicación proporcionada') if ubicacion_contextual else 'la ubicación proporcionada'

            if not action:
                pregunta_menu = ""
                if isinstance(pregunta_original, str):
                    pregunta_menu = pregunta_original
                elif isinstance(pregunta_original, dict):
                    pregunta_menu = pregunta_original.get("pregunta", "")
                opciones = [
                    {"texto": "Iniciar un Reclamo", "action_id": "iniciar_reclamo_con_ubicacion"},
                    {"texto": "Enviar una Sugerencia", "action_id": "enviar_sugerencia_con_ubicacion"},
                    {"texto": "Cancelar", "action_id": "cancelar"},
                ]
                action = find_menu_action_by_input(pregunta_menu, opciones)

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
                # If the user response doesn't match any option, keep the flow active
                # and re-send the proactive menu instead of resetting the conversation.
                contexto_municipio_actual['estado_conversacion'] = ConversationState.ESPERANDO_INTENCION_UBICACION.name
                if chat_db_context:
                    flag_modified(chat_db_context, "context_data")
                return _finalize_response(
                    {
                        "message_body": f"No entendí la opción. ¿Qué te gustaría hacer en *{address}*?",
                        "options_list": [
                            {"texto": "Iniciar un Reclamo", "action_id": "iniciar_reclamo_con_ubicacion"},
                            {"texto": "Enviar una Sugerencia", "action_id": "enviar_sugerencia_con_ubicacion"},
                            {"texto": "Cancelar", "action_id": "cancelar"},
                            {"texto": "Menú", "action_id": "menu_principal"},
                        ],
                        "fuente": "proactive_location_handler",
                    }
                )


        elif estado_conversacion == ConversationState.ESPERANDO_NUMERO_TICKET.name:
            numero_guardado = contexto_municipio_actual.get('numero_ticket_consulta')
            if not numero_guardado:
                numero_ticket = ''.join(filter(str.isdigit, pregunta_str or ''))
                if not numero_ticket:
                    return _finalize_response({
                        "message_body": "Por favor, ingresá un número de reclamo válido.",
                        "fuente": "handler_consultar_reclamo"
                    })
                contexto_municipio_actual['numero_ticket_consulta'] = numero_ticket
                if chat_db_context: flag_modified(chat_db_context, "context_data")
                return _finalize_response({
                    "message_body": "Ingresá el PIN de 6 dígitos asociado al ticket.",
                    "fuente": "handler_consultar_reclamo"
                })

            pin = ''.join(filter(str.isdigit, pregunta_str or ''))
            if len(pin) != 6:
                return _finalize_response({
                    "message_body": "El PIN debe tener 6 dígitos.",
                    "fuente": "handler_consultar_reclamo"
                })

            municipio_id = context.get("municipio_id", MUNICIPIO_ID)
            ticket_query = MunicipioTicket.query.filter_by(nro_ticket=numero_guardado, consulta_pin=pin)
            try:
                ticket_query = ticket_query.filter_by(municipio_id=int(municipio_id))
            except (TypeError, ValueError):
                pass
            ticket = ticket_query.first()

            contexto_municipio_actual.pop('numero_ticket_consulta', None)
            contexto_municipio_actual['estado_conversacion'] = None
            if chat_db_context: flag_modified(chat_db_context, "context_data")


            botones = []
            if ticket:
                contactos = cargar_configuracion_municipio(municipio_id, "contactos_especializados.json")
                contacto_especializado = contactos.get(ticket.categoria, contactos.get("default", {})) if isinstance(contactos, dict) else {}
                municipio_config = context.get("municipio_config_actual", {})
                base_chat_url = municipio_config.get("base_chat_url", "https://www.chatboc.ar/chat")
                mensaje, botones = formatear_ticket_respuesta(
                    "reclamo",
                    ticket.nombre_vecino or "Vecino/a",
                    ticket.detalles or ticket.pregunta or "",
                    ticket.categoria,
                    f"M-{ticket.nro_ticket}",
                    contacto_especializado,
                    base_chat_url,
                    consulta_pin=ticket.consulta_pin,
                )
                mensaje += f"\n\n🔔 *Estado actual:* {ticket.estado}"
            else:
                mensaje = (
                    "No encontramos un ticket con ese número y PIN. Por favor, verifica los datos e intenta nuevamente."
                )
            final_payload = _message_with_menu(mensaje, context)
            final_payload['fuente'] = 'handler_consultar_reclamo'
            if botones:
                final_payload['options_list'] = botones + final_payload.get('options_list', [])
                final_payload['message_type'] = 'interactive_buttons'
            return _finalize_response(final_payload)
        elif estado_conversacion == ConversationState.ESPERANDO_TEXTO_SUGERENCIA.name:
            switch_response = _detect_reclamo_during_sugerencia(pregunta_str, contexto_municipio_actual, context, chat_db_context)
            if switch_response:
                return _finalize_response(switch_response)
            sugerencia_texto = pregunta_str
            if len(sugerencia_texto) < 10:
                return _finalize_response({"message_body": "Tu sugerencia parece un poco corta. ¿Podrías darme un poco más de detalle?", "fuente": "sugerencia_muy_corta"})

            ubicacion_sugerencia = contexto_municipio_actual.pop('ubicacion_contextual_sugerencia', 'N/A')
            viewer_user_obj = context.get("viewer_user_obj")
            contacto_prev = contexto_municipio_actual.get('contacto_usuario', {})
            datos_sugerencia = {
                "categoria": "Sugerencia",
                "descripcion": sugerencia_texto,
                "ubicacion": ubicacion_sugerencia,
                "nombre": (
                    getattr(viewer_user_obj, "name", None)
                    or getattr(viewer_user_obj, "nombre", None)
                    or contacto_prev.get("nombre")
                ),
                "dni": getattr(viewer_user_obj, "dni", None) or contacto_prev.get("dni"),
                "email": getattr(viewer_user_obj, "email", None) or contacto_prev.get("email"),
                "direccion": getattr(viewer_user_obj, "direccion", None) or contacto_prev.get("direccion"),
                "telefono": getattr(viewer_user_obj, "telefono", None) or contacto_prev.get("telefono"),
            }

            campos_faltantes = [c for c in ["nombre", "dni", "email", "direccion"] if not datos_sugerencia.get(c)]
            contexto_municipio_actual['datos_sugerencia'] = datos_sugerencia
            contexto_municipio_actual['contacto_usuario'] = {
                k: datos_sugerencia.get(k)
                for k in ["nombre", "dni", "email", "direccion", "telefono"]
                if datos_sugerencia.get(k)
            }
            if campos_faltantes:
                contexto_municipio_actual['estado_conversacion'] = ConversationState.ESPERANDO_DATOS_CONTACTO_SUGERENCIA.name
                if chat_db_context:
                    flag_modified(chat_db_context, "context_data")
                campos_texto = ', '.join(campos_faltantes)
                return _finalize_response({
                    "message_body": f"Para registrar tu sugerencia necesito: {campos_texto}. Podés escribir todo en un solo mensaje.",
                    "fuente": "pide_datos_contacto_sugerencia"
                })

            mensaje_confirmacion = (
                "Por favor, confirmá si los datos para tu sugerencia son correctos:\n"
                f"- **Nombre**: {datos_sugerencia.get('nombre')}\n"
                f"- **DNI**: {datos_sugerencia.get('dni')}\n"
                f"- **Email**: {datos_sugerencia.get('email')}\n"
                f"- **Dirección**: {datos_sugerencia.get('direccion')}\n"
                f"- **Sugerencia**: {sugerencia_texto}"
            )
            botones = [
                {"texto": "Sí, enviar sugerencia", "action_id": "confirmar_sugerencia_si"},
                {"texto": "No, corregir", "action_id": "confirmar_sugerencia_no"},
            ]
            contexto_municipio_actual['estado_conversacion'] = ConversationState.ESPERANDO_CONFIRMACION_SUGERENCIA.name
            if chat_db_context: flag_modified(chat_db_context, "context_data")
            return _finalize_response({
                "message_body": mensaje_confirmacion,
                "options_list": botones,
                "message_type": "interactive_buttons",
                "fuente": "pide_confirmacion_sugerencia"
            })

        elif estado_conversacion == ConversationState.ESPERANDO_DATOS_CONTACTO_SUGERENCIA.name:
            switch_response = _detect_reclamo_during_sugerencia(pregunta_str, contexto_municipio_actual, context, chat_db_context)
            if switch_response:
                return _finalize_response(switch_response)
            datos_guardados = contexto_municipio_actual.get('datos_sugerencia', {})
            campos_requeridos = ["nombre", "dni", "email", "direccion"]

            # Primero intentamos extraer con regex para los campos aún faltantes.
            nuevos_datos = extract_multiple_contact_details_regex(
                pregunta_str, campos_requeridos + ["telefono"]
            )
            for campo in ["nombre", "dni", "email", "direccion", "telefono"]:
                if nuevos_datos.get(campo):
                    datos_guardados[campo] = nuevos_datos[campo]

            campos_faltantes = [c for c in campos_requeridos if not datos_guardados.get(c)]

            # Utilizar el LLM solo si todavía faltan campos
            if campos_faltantes:
                try:
                    llm_datos = extract_multiple_contact_details_llm(
                        pregunta_str, campos_requeridos + ["telefono"]
                    )
                    if llm_datos:
                        for campo, valor in llm_datos.items():
                            if valor and campo in ["nombre", "dni", "email", "direccion", "telefono"]:
                                datos_guardados[campo] = valor
                except Exception as e:
                    logger.error("[DATOS_SUGERENCIA] LLM fallback failed: %s", e)
                campos_faltantes = [c for c in campos_requeridos if not datos_guardados.get(c)]

            contexto_municipio_actual['datos_sugerencia'] = datos_guardados
            # Persist contact info for future interactions
            contexto_municipio_actual['contacto_usuario'] = {
                k: datos_guardados.get(k)
                for k in ["nombre", "dni", "email", "direccion", "telefono"]
                if datos_guardados.get(k)
            }
            if campos_faltantes:
                if chat_db_context:
                    flag_modified(chat_db_context, "context_data")
                return _finalize_response({
                    "message_body": f"Aún necesito: {', '.join(campos_faltantes)}. Podés enviarlos todos juntos.",
                    "fuente": "datos_contacto_sugerencia_incompletos"
                })

            mensaje_confirmacion = (
                "Por favor, confirmá si los datos para tu sugerencia son correctos:\n"
                f"- **Nombre**: {datos_guardados.get('nombre')}\n"
                f"- **DNI**: {datos_guardados.get('dni')}\n"
                f"- **Email**: {datos_guardados.get('email')}\n"
                f"- **Dirección**: {datos_guardados.get('direccion')}\n"
                f"- **Sugerencia**: {datos_guardados.get('descripcion')}"
            )
            botones = [
                {"texto": "Sí, enviar sugerencia", "action_id": "confirmar_sugerencia_si"},
                {"texto": "No, corregir", "action_id": "confirmar_sugerencia_no"},
            ]
            contexto_municipio_actual['estado_conversacion'] = ConversationState.ESPERANDO_CONFIRMACION_SUGERENCIA.name
            if chat_db_context: flag_modified(chat_db_context, "context_data")
            return _finalize_response({
                "message_body": mensaje_confirmacion,
                "options_list": botones,
                "message_type": "interactive_buttons",
                "fuente": "pide_confirmacion_sugerencia"
            })

        elif estado_conversacion == ConversationState.ESPERANDO_CONFIRMACION_SUGERENCIA.name:
            switch_response = _detect_reclamo_during_sugerencia(pregunta_str, contexto_municipio_actual, context, chat_db_context)
            if switch_response:
                return _finalize_response(switch_response)
            texto_normalizado = normalizar_texto(pregunta_str)
            afirmativos = ["si", "enviar", "guardar", "guarda", "ok", "dale", "confirmar"]
            if (
                action == "confirmar_sugerencia_si"
                or texto_normalizado.strip() == "1"
                or any(a in texto_normalizado for a in afirmativos)
            ):
                datos_confirmados = contexto_municipio_actual.pop('datos_sugerencia', {})
                handler = CrearReclamoActionHandler(context)
                response = handler.execute(datos_confirmados)
                if response.get("success"):
                    response["message_to_user"] = f"✅ ¡Hemos recibido tu sugerencia! Muchas gracias por tu aporte. Lo hemos registrado con el número de ticket `{response.get('data', {}).get('nro_ticket', 'N/A')}` para su seguimiento."
                    contexto_municipio_actual['estado_conversacion'] = None
                    if chat_db_context: flag_modified(chat_db_context, "context_data")
                    final_payload = _message_with_menu(response["message_to_user"], context)
                    final_payload['success'] = True
                    return _finalize_response(final_payload)
                if chat_db_context: flag_modified(chat_db_context, "context_data")
                return _finalize_response(response)
            else:
                contexto_municipio_actual['estado_conversacion'] = ConversationState.ESPERANDO_DATOS_CONTACTO_SUGERENCIA.name
                if chat_db_context: flag_modified(chat_db_context, "context_data")
                return _finalize_response({
                    "message_body": "Entendido. Por favor, enviá los datos correctos en un solo mensaje.",
                    "fuente": "pide_correccion_sugerencia"
                })

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
        if not inferred_action:
            inferred_action = find_global_menu_action(pregunta_str or "")
        if inferred_action:
            response = handle_main_menu_action(inferred_action, context, chat_db_context)
            if response:
                return _finalize_response(response)


    USAR_LLM_PARA_RECLAMOS = True  # Habilita el flujo con LLM para reclamos
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
    if not pregunta_str.strip():  # Solo actuar si no hay texto del usuario
        synthetic_prompt = None
        datos_interpretados = context.get("datos_interpretados_archivo") or kwargs.get("datos_interpretados_archivo")

        if datos_interpretados and isinstance(datos_interpretados, dict):
            # Si el análisis automático ya determinó que es un reclamo, iniciar el flujo directamente
            if datos_interpretados.get("es_reclamo"):
                logger_actual.info("Iniciando flujo de reclamo desde imagen interpretada")
                handler = ReclamoFlowHandler(context, chat_db_context)
                datos_iniciales = {
                    "categoria": datos_interpretados.get("categoria_sugerida"),
                    "descripcion": datos_interpretados.get("descripcion_sugerida"),
                    "origen_descripcion": "imagen",
                }
                return _finalize_response(handler.start_flow(datos_iniciales=datos_iniciales))

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
        payload_action = None
        if isinstance(pregunta_original, str):
            pregunta_str_menu = pregunta_original
        elif isinstance(pregunta_original, dict):
            pregunta_str_menu = pregunta_original.get("pregunta", "")
            payload_action = pregunta_original.get("action")

        logger_actual.info(
            f"Handling input in ESPERANDO_SELECCION_MENU_PRINCIPAL state. Input: '{pregunta_str_menu}', Payload action: '{payload_action}'"
        )

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

        selected_action = payload_action or find_menu_action_by_input(pregunta_str_menu, buttons_for_finder)
        if not selected_action:
            selected_action = find_global_menu_action(pregunta_str_menu)

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

    # --- INICIO: Manejo genérico de selección de submenús ---
    elif estado_conversacion == ConversationState.ESPERANDO_SELECCION_DE_LISTA.name:
        pregunta_str_menu = ""
        action_payload = None
        if isinstance(pregunta_original, str):
            pregunta_str_menu = pregunta_original
        elif isinstance(pregunta_original, dict):
            pregunta_str_menu = pregunta_original.get("pregunta", "")
            action_payload = pregunta_original.get("action")

        menu_opciones = contexto_municipio_actual.get("menu_opciones", [])
        selected_action = action_payload or find_menu_action_by_input(pregunta_str_menu, menu_opciones)
        if not selected_action:
            selected_action = find_global_menu_action(pregunta_str_menu)

        if selected_action:
            contexto_municipio_actual['estado_conversacion'] = None
            contexto_municipio_actual.pop('menu_opciones', None)
            if chat_db_context:
                flag_modified(chat_db_context, "context_data")
            response = handle_main_menu_action(selected_action, context, chat_db_context)
            if response:
                new_state = contexto_municipio_actual.get("estado_conversacion")
                if not new_state:
                    response['message_body'] = response.get('message_body', '').strip() + "\n\n¿En qué más puedo ayudarte?"
                return _finalize_response(response)
        else:
            # Reenviar el mismo submenú si la opción no es válida
            return _finalize_response({
                "message_body": "No reconocí esa opción. Por favor, elegí una opción del menú.",
                "message_type": "interactive_buttons",
                "options_list": menu_opciones,
                "fuente": "submenu_opcion_invalida",
            })
    # --- FIN: Manejo genérico de selección de submenús ---

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

        if pregunta_str_reclamo in {"0", "1"} or normalized_input in RETURN_TO_MAIN_MENU:
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

        # El menú se muestra numerado a partir de 1, mientras que los action_id
        # comienzan en 0. Convertimos la elección del usuario a action_id.
        reclamo_options = _get_reclamos_menu().get("options_list", [])
        selected_category_name = None

        if pregunta_str_reclamo.isdigit():
            expected_id = str(int(pregunta_str_reclamo) - 1)
            for option in reclamo_options:
                if option.get("action_id") == expected_id:
                    selected_category_name = option.get("category_name")
                    break

        # Si no es un número o no corresponde, intentar matchear por texto y extraer más datos.
        details = {}
        if not selected_category_name:
            plain_text_options = [{"texto": opt.get("category_name")} for opt in reclamo_options]
            details = extract_reclamo_details_from_text(pregunta_str_reclamo, plain_text_options)
            selected_category_name = details.pop("categoria_sugerida", None)

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
            datos_iniciales = {}
            if details.get("descripcion_sugerida"):
                datos_iniciales["descripcion"] = details["descripcion_sugerida"]
            if details.get("direccion_sugerida"):
                datos_iniciales["direccion"] = details["direccion_sugerida"]
            response_dict = handler.start_flow(
                datos_iniciales=datos_iniciales or None,
                categoria_inicial=selected_category_name,
            )
            if chat_db_context:
                flag_modified(chat_db_context, "context_data")
            return _finalize_response(response_dict)
            # --- FIN: Integración del nuevo ReclamoFlowHandler ---
        else:
            logger_actual.warning(f"Input '{pregunta_str_reclamo}' no coincide con ninguna categoría. Mostrando menú de nuevo.")
            return _finalize_response(_get_reclamos_menu())
    # --- FIN: Manejo de selección de menú de reclamos ---


    elif estado_conversacion == ConversationState.ESPERANDO_SELECCION_CONTACTO_CATEGORIA.name:
        selected_category_action = received_payload.get('action')
        pregunta_str_norm = normalizar_texto(pregunta_str or '')

        categorias_map = contexto_municipio_actual.get('contactos_categorias', {})
        if selected_category_action and selected_category_action.startswith('select_contact_category_'):
            slug = selected_category_action.replace('select_contact_category_', '')
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
                'message_body': 'Por favor, seleccioná una categoría de la lista.',
                'fuente': 'contactos_utiles_invalid_category_selection'
            })

        return _finalize_response(
            handle_contactos_utiles_mostrar_categoria(context, chat_db_context, selected)
        )
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
            if consulta_guardada:
                contexto_municipio_actual['ultima_consulta_poi'] = consulta_guardada
            contexto_municipio_actual['estado_conversacion'] = None  # Clear state
            if chat_db_context:
                flag_modified(chat_db_context, "context_data")

            if consulta_guardada:
                logger_actual.info(f"Received location, processing saved query: '{consulta_guardada}'")
                return _finalize_response(PointsOfInterestHandler(context={}).handle({"pregunta": consulta_guardada, "location": location}))
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
                    if consulta_guardada:
                        contexto_municipio_actual['ultima_consulta_poi'] = consulta_guardada
                    contexto_municipio_actual['estado_conversacion'] = None  # Clear state
                    if chat_db_context:
                        flag_modified(chat_db_context, "context_data")

                    if consulta_guardada:
                        logger_actual.info(f"Geocoded address successfully. Processing saved query: '{consulta_guardada}'")
                        loc_payload = {
                            "address": geocoded_location.get("formatted_address"),
                            "lat": geocoded_location.get("lat"),
                            "lon": geocoded_location.get("lng"),
                        }
                        return _finalize_response(PointsOfInterestHandler(context={}).handle({"pregunta": consulta_guardada, "location": loc_payload}))
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
        if not action:
            pregunta_menu = ""
            if isinstance(pregunta_original, str):
                pregunta_menu = pregunta_original
            elif isinstance(pregunta_original, dict):
                pregunta_menu = pregunta_original.get("pregunta", "")
            opciones = [
                {"texto": "Iniciar un Reclamo", "action_id": "iniciar_reclamo_con_ubicacion"},
                {"texto": "Enviar una Sugerencia", "action_id": "enviar_sugerencia_con_ubicacion"},
                {"texto": "Cancelar", "action_id": "cancelar"},
            ]
            action = find_menu_action_by_input(pregunta_menu, opciones)

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
        ubicacion_display = (
            contexto_municipio_actual.get("datos_parciales_llm_reclamo", {})
            .get("ubicacion", "")
        )
        normalized = pregunta_str.strip().lower()
        if normalized in {"1", "confirmar", "si", "sí"}:
            contexto_municipio_actual["ubicacion_confirmada"] = True
            contexto_municipio_actual["estado_conversacion"] = None
        elif normalized in {"2", "editar", "no"}:
            contexto_municipio_actual["ubicacion_confirmada"] = False
            contexto_municipio_actual["estado_conversacion"] = ConversationState.ESPERANDO_DIRECCION_RECLAMO.name
            return _finalize_response({
                "message_body": "Por favor, decime la nueva ubicación.",
                "options_list": [],
                "message_type": "text",
                "fuente": "pedir_nueva_ubicacion",
            })
        else:
            opciones = [
                {"texto": "Confirmar", "action_id": "confirmar_ubicacion"},
                {"texto": "Editar", "action_id": "editar_ubicacion"},
            ]
            return _finalize_response({
                "message_body": f"¿Es esta tu dirección: {ubicacion_display}?",
                "options_list": opciones,
                "message_type": "interactive_buttons",
                "fuente": "confirmar_ubicacion",
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
        contacto_prev = contexto_municipio_actual.get('contacto_usuario', {})
        if contacto_prev:
            datos_sugerencia.update({
                k: contacto_prev.get(k)
                for k in ["nombre", "dni", "email", "direccion", "telefono"]
                if contacto_prev.get(k)
            })

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
        datos_interpretados = context.get("datos_interpretados_archivo") or kwargs.get("datos_interpretados_archivo")
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
