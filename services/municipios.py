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
    ESPERANDO_CONFIRMACION_INICIAR_RECLAMO = auto()

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
    def handle(self, payload: dict) -> dict | None:
        # This is a simplified greeting handler.
        # It could be expanded to include the user's name, etc.
        return {
            "message_body": "¡Hola! ¿En qué puedo ayudarte?",
            "options_list": [
                {"id": "iniciar_reclamo", "texto": "Hacer un reclamo"},
                {"id": "consultar_estado_ticket", "texto": "Consultar estado de un trámite"},
            ],
            "message_type": "interactive_buttons",
            "fuente": "greeting_handler_v2"
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


from services.actions.municipio_actions import CrearReclamoActionHandler


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

def handle_llm_interaction(pregunta_str, context, viewer_user, owner_user, chat_db_context):
    logger_actual = current_app.logger if has_app_context() else logging.getLogger(__name__)
    contexto_municipio_actual = context.get(CONTEXTO_MUNICIPIO, {})

    estado_conversacion_para_llm = contexto_municipio_actual.get("estado_conversacion")
    invocar_llm = False

    if estado_conversacion_para_llm in [ConversationState.ESPERANDO_INFO_RECLAMO_LLM.name, ConversationState.CONVERSACION_GENERAL_LLM.name]:
        invocar_llm = True
    elif not estado_conversacion_para_llm or contexto_municipio_actual.get("saludo_detectado_en_largo_mensaje"):
        if len(pregunta_str.strip().split()) > 1 or (context.get("es_foto") and not pregunta_str.strip()):
            invocar_llm = True

    if invocar_llm:
        logger.info(f"[HANDLE_LLM] Invocando LLM. Estado: {estado_conversacion_para_llm}")

    usuario_info_llm = {
        "nombre": getattr(viewer_user, "nombre", "Vecino/a") if viewer_user else "Vecino/a",
        "tipo_entidad": "municipio",
        "ubicacion": getattr(viewer_user, "direccion", None) if viewer_user else None,
        "contacto": {
            "telefono": getattr(viewer_user, "telefono", None) if viewer_user else None,
            "email": getattr(viewer_user, "email", None) if viewer_user else None
        }
    }

    historial_para_llm = []
    if estado_conversacion_para_llm == ConversationState.ESPERANDO_INFO_RECLAMO_LLM.name:
        historial_para_llm = contexto_municipio_actual.get("historial_llm_reclamo", [])
    elif estado_conversacion_para_llm == ConversationState.CONVERSACION_GENERAL_LLM.name:
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
        except Exception as e:
            logger.error(f"[RESPONDER_MUNICIPIO_LLM_ERROR] Error general en la llamada a Gemini: {e}", exc_info=True)
            return None

        respuesta_usuario_llm = respuesta_llm_dict.get("respuesta_usuario")
        accion_backend_llm = respuesta_llm_dict.get("accion_backend")
        datos_estructura_llm = respuesta_llm_dict.get("datos_estructura")
        pedir_info_llm = respuesta_llm_dict.get("pedir_info")
        botones_llm = respuesta_llm_dict.get("botones", [])

        if not respuesta_usuario_llm:
            return None

        nuevo_turno_historial = {"pregunta_usuario": pregunta_str, "respuesta_ia": respuesta_usuario_llm}

        if accion_backend_llm == "crear_reclamo" and datos_estructura_llm and datos_estructura_llm.get("target") == "municipio":
            contexto_municipio_actual.setdefault("historial_llm_reclamo", []).append(nuevo_turno_historial)
            if not pedir_info_llm:
                respuesta_accion = accion_crear_reclamo_municipio(datos_estructura_llm, context)
                for k in ["historial_llm_reclamo", "datos_parciales_llm_reclamo", "esperando_info_llm_reclamo"]:
                    contexto_municipio_actual.pop(k, None)
                contexto_municipio_actual["estado_conversacion"] = None
                return respuesta_accion
            else:
                contexto_municipio_actual["datos_parciales_llm_reclamo"] = datos_estructura_llm
                contexto_municipio_actual["estado_conversacion"] = ConversationState.ESPERANDO_INFO_RECLAMO_LLM.name
                contexto_municipio_actual["esperando_info_llm_reclamo"] = pedir_info_llm
                return {"message_body": respuesta_usuario_llm, "options_list": botones_llm, "message_type": "interactive_buttons" if botones_llm else "text", "fuente": "llm_pide_info_reclamo"}

        elif accion_backend_llm == "derivar_humano":
            context["intencion"] = "hablar_con_agente"
            contexto_municipio_actual["mensaje_previo_llm_para_escalamiento"] = respuesta_usuario_llm
            logger.info("[HANDLE_LLM] LLM derivó a humano.")
            return None

        else: # Respuesta general
            contexto_municipio_actual.setdefault("historial_conversacion_general_llm", []).append(nuevo_turno_historial)
            contexto_municipio_actual["estado_conversacion"] = ConversationState.CONVERSACION_GENERAL_LLM.name
            if pedir_info_llm:
                contexto_municipio_actual["esperando_info_general_llm"] = pedir_info_llm
            else:
                contexto_municipio_actual.pop("esperando_info_general_llm", None)
            return {"message_body": respuesta_usuario_llm, "options_list": botones_llm, "message_type": "interactive_buttons" if botones_llm else "text", "fuente": "llm_respuesta_general"}

    except Exception as e_llm:
        logger.error(f"[HANDLE_LLM] Error: {e_llm}", exc_info=True)
        for k in ["historial_llm_reclamo", "datos_parciales_llm_reclamo", "esperando_info_llm_reclamo", "historial_conversacion_general_llm", "estado_conversacion"]:
            if k == "estado_conversacion" and contexto_municipio_actual.get(k) in [ConversationState.ESPERANDO_INFO_RECLAMO_LLM.name, ConversationState.CONVERSACION_GENERAL_LLM.name]:
                contexto_municipio_actual[k] = None
            elif k != "estado_conversacion":
                contexto_municipio_actual.pop(k, None)
        return None

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

    # LLM-first approach. The main Gemini call will now be the primary driver.
    # The old handlers will be retired.
    # respuesta_manejada_por_llm = handle_llm_interaction(pregunta_str, context, viewer_user, owner_user, chat_db_context)
    #
    # if respuesta_manejada_por_llm:
    #     contexto_municipio_serializado_para_db = serializar_enum(context.get(CONTEXTO_MUNICIPIO, {}))
    #     if chat_db_context and hasattr(chat_db_context, 'context_data') and chat_db_context.context_data is not None:
    #         chat_db_context.context_data[CONTEXTO_MUNICIPIO] = contexto_municipio_serializado_para_db
    #         flag_modified(chat_db_context, "context_data")
    #     return respuesta_manejada_por_llm

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
        from services.tasks import process_image_for_chat_task
        process_image_for_chat_task.delay(
            user_phone_number=anon_id,
            client_user_id=owner_user.id,
            uploaded_file_info_whatsapp=uploaded_file_info_for_analysis,
            chat_session_id=kwargs.get("chat_session_uuid")
        )
        return {
            "message_body": "He recibido tu imagen y la estoy analizando. Te enviaré un mensaje cuando termine.",
            "options_list": [],
            "message_type": "text",
            "fuente": "analisis_imagen_async"
        }
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
    # Esto es crucial. Si `pedir_info_final` está seteado, el estado debe reflejar qué se está esperando.
    # Esta lógica necesita mapear `pedir_info_final` a un `ConversationState`.
    # Ejemplo: if pedir_info_final == "ubicacion": contexto_municipio_actual["estado_conversacion"] = ConversationState.ESPERANDO_DIRECCION_RECLAMO.name
    # Por ahora, si hay pedir_info, asumimos que el estado se maneja dentro del flujo conversacional que pediría ese dato.
    # Si la acción fue exitosa y no hay pedir_info, generalmente se limpia el estado.

    if action_handler_result.get("success") and not pedir_info_final:
        if contexto_municipio_actual.get("estado_conversacion") not in [None, ConversationState.IDLE.name if hasattr(ConversationState, 'IDLE') else None]:
            logger.info(f"Acción '{llm_response_structured.get('accion_backend')}' exitosa y sin pedir_info. Limpiando estado de conversación municipal.")
            interacciones_anon_actual = contexto_municipio_actual.get("interacciones_anon_sesion")
            contexto_municipio_actual.clear()
            if interacciones_anon_actual is not None:
                contexto_municipio_actual["interacciones_anon_sesion"] = interacciones_anon_actual
    elif pedir_info_final:
        # FIX: No actualizar estado si la acción fue `crear_reclamo` y tuvo éxito aparente (sin pedir_info).
        # El estado se debe determinar por el resultado del ActionHandler, no por el `pedir_info` del LLM inicial.
        accion_llm = llm_response_structured.get('accion_backend')
        if accion_llm == 'crear_reclamo' and not pedir_info_final:
            logger.info(f"Acción es '{accion_llm}' y no hay 'pedir_info_final'. Se omite la actualización de estado basada en el 'pedir_info' del LLM.")
        else:
            if isinstance(pedir_info_final, list):
                pedir_info_str = ", ".join(pedir_info_final)
            else:
                pedir_info_str = str(pedir_info_final)

            logger.info(f"[PEDIR_INFO_MAP] Valor recibido: '{pedir_info_str}'")
            pedir_info_norm = normalizar_str(pedir_info_str)
            logger.info(f"[PEDIR_INFO_MAP] Normalizado: '{pedir_info_norm}'")

            estado_objetivo = None
            for key, state in PEDIR_INFO_TO_STATE.items():
                if key in pedir_info_norm:
                    estado_objetivo = state
                    break

            if estado_objetivo:
                contexto_municipio_actual["estado_conversacion"] = estado_objetivo.name
                logger.info(
                    f"Actualizando estado de conversación a: {estado_objetivo.name} debido a pedir_info normalizado: '{pedir_info_norm}'"
                )
            else:
                logger.warning(
                    f"No se pudo mapear pedir_info '{pedir_info_final}' normalizado '{pedir_info_norm}' a un ConversationState. Reiniciando flujo."
                )
                respuesta_final_texto = (
                    "Perdón, tuve un problema para continuar con el reclamo. ¿Podés intentar de nuevo desde el inicio?"
                )
                interacciones_anon_actual = contexto_municipio_actual.get("interacciones_anon_sesion")
                contexto_municipio_actual.clear()
                if interacciones_anon_actual is not None:
                    contexto_municipio_actual["interacciones_anon_sesion"] = interacciones_anon_actual
                contexto_municipio_actual["estado_conversacion"] = None

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