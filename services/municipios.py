import logging
import re
import json
import os
from enum import Enum, auto
import unicodedata
import difflib
from flask import current_app, has_app_context  # Ensure current_app is imported directly
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
        if texto in saludos or (0 < len(tokens) <= 3 and all(t in set_saludo for t in tokens)):
            memoria.clear()
            greeting_body = "¡Hola! 👋 Soy tu asistente digital del Municipio. Estoy aquí para ayudarte. Podés consultarme sobre trámites, hacer un reclamo, dejar una sugerencia o resolver alguna duda que tengas. ¡Contame en qué te puedo colaborar hoy!"
            options = [
                {"id": "iniciar_reclamo", "texto": "Hacer un reclamo"},
                {"id": "hacer_sugerencia", "texto": "Dejar una sugerencia"},
                {"id": "consultar_tramite", "texto": "Consultar un trámite"},
                {"id": "consultar_estado_ticket", "texto": "Estado de mi ticket"}
            ]
            # Determine message_type based on number of options
            message_type = 'interactive_buttons' if len(options) <= 3 else 'interactive_list'
            if len(options) > 3 and len(options) > 10: # WhatsApp list limit
                logger.warning("GreetingHandler: Too many options for a single WhatsApp list. Truncating or consider sub-menus.")
                # For now, it will be handled by formatter, but good to be aware.
            
            return {
                "message_body": greeting_body,
                "options_list": options,
                "message_type": message_type,
                "fuente": "saludo_municipio_interactivo_v2"
                # contexto_actualizado will be handled by responder_municipio
            }
        for saludo in saludos:
            if (texto.startswith(saludo + " ") or texto.startswith(saludo + ",") or texto.startswith(saludo + ".")): memoria["saludo_detectado"] = True; break
        return None

class SugerenciasVecinoHandler(BaseMunicipioHandler):
    def handle(self, payload: dict) -> dict | None:
        pregunta_str = payload.get("pregunta", "").strip(); memoria = self.context[CONTEXTO_MUNICIPIO]; estado_conversacion_str = memoria.get("estado_conversacion"); estado_conversacion = ConversationState[estado_conversacion_str] if isinstance(estado_conversacion_str, str) else estado_conversacion_str; intencion = self.context.get("intencion"); sugerencia_texto_directo = ""
        if intencion == "hacer_sugerencia":
            palabras_clave_sugerencia = ["sugerencia", "sugerencias", "idea", "propuesta", "proponer", "mejorar"]; texto_limpio_de_keywords = pregunta_str
            for kw in palabras_clave_sugerencia:
                if texto_limpio_de_keywords.lower().startswith(kw): texto_limpio_de_keywords = texto_limpio_de_keywords[len(kw):].strip()
            if texto_limpio_de_keywords and len(texto_limpio_de_keywords) > 5: sugerencia_texto_directo = texto_limpio_de_keywords
            if not sugerencia_texto_directo and estado_conversacion != ConversationState.ESPERANDO_TEXTO_SUGERENCIA:
                memoria["estado_conversacion"] = ConversationState.ESPERANDO_TEXTO_SUGERENCIA.name
                body = "¡Genial! Nos interesa mucho tu opinión. Por favor, contanos tu sugerencia o idea para mejorar:"
                options = [{"id": "cancelar_sugerencia", "texto": "Cancelar sugerencia"}]
                return {
                    "message_body": body,
                    "options_list": options,
                    "message_type": 'interactive_buttons',
                    "fuente": "sugerencia_pedir_texto_v2"
                }
        if estado_conversacion == ConversationState.ESPERANDO_TEXTO_SUGERENCIA or sugerencia_texto_directo:
            # If there's direct input (pregunta_str) and it's not a carry-over (sugerencia_texto_directo is empty)
            if pregunta_str and not sugerencia_texto_directo and es_pregunta_nueva(pregunta_str, "el texto de tu sugerencia", memoria):
                logger.info(f"[SugerenciasVecinoHandler] '{pregunta_str}' detectada como pregunta nueva mientras se esperaba texto de sugerencia. Limpiando.")
                memoria.clear()
                self.context["intencion"] = None
                return None

            sugerencia_final = sugerencia_texto_directo if sugerencia_texto_directo else pregunta_str
            if not sugerencia_final or len(sugerencia_final) < 5:
                body_corto = "Por favor, ingresá el texto de tu sugerencia. Tiene que ser un poco más descriptiva para que podamos entenderla bien."
                options_corto = [{"id": "cancelar_sugerencia_corta", "texto": "Cancelar sugerencia"}]
                return {
                    "message_body": body_corto,
                    "options_list": options_corto,
                    "message_type": 'interactive_buttons',
                    "fuente": "sugerencia_texto_corto_v2"
                }
            try:
                ticket_data = {"asunto": "Nueva Sugerencia/Mejora del Vecino", "categoria": "Sugerencia", "detalles": sugerencia_final, "pregunta": sugerencia_final, "estado": "nueva_sugerencia", "user_id": self.context.get("cliente_id"), "anon_id": self.context.get("anon_id") if not self.context.get("cliente_id") else None, "municipio_id": getattr(self.context.get("user_obj"), "municipio_id", None)}
                tipo_ticket_para_sugerencia = "municipio"
                ticket = servicio_tickets.crear_nuevo_ticket(tipo_ticket=tipo_ticket_para_sugerencia, ticket_data=ticket_data)
                if ticket:
                    nro_ticket_str = f"M-{ticket.nro_ticket}" if tipo_ticket_para_sugerencia == "municipio" else str(ticket.nro_ticket)
                    logger.info(f"Sugerencia registrada como ticket {nro_ticket_str}."); memoria.clear()
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
        pregunta_str = payload.get("pregunta", ""); logger.info(f"[INTENT] Analizando intención para: {pregunta_str}"); memoria = self.context[CONTEXTO_MUNICIPIO]; texto_normalizado = normalizar_texto(pregunta_str)

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
            if es_pregunta_nueva(pregunta_str, "una confirmación (sí o no)"): memoria.clear(); return None
            ticket_id = memoria.get("ticket_id_activo"); ticket = db.session.get(MunicipioTicket, ticket_id)
            if not ticket: memoria.clear(); return {"respuesta": "No pude encontrar el ticket activo. ¿Necesitás ayuda con algo más?"}
            if "si" in normalizar_texto(pregunta_str):
                ticket.estado = "resuelto"; db.session.commit()
                memoria["estado_conversacion"] = ConversationState.ESPERANDO_CALIFICACION.name
                return {"respuesta": "¡Excelente! ¿Podés calificar la atención recibida del 1 al 5?"}
            else: memoria.clear(); return {"respuesta": "Dejamos el ticket abierto para seguimiento del equipo. ¿Necesitás algo más?"}
        elif estado_conversacion == ConversationState.ESPERANDO_CALIFICACION:
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
            for campo in self.CAMPOS_RECLAMO:
                if campo == "direccion": continue
                valor_campo = datos_extraidos_reclamo_inteligente.get(campo, "")
                if valor_campo:
                    if campo == "categoria":
                        matched_category = next((c for c in CATEGORIAS_RECLAMO if normalizar_texto(c) == normalizar_texto(valor_campo)), None)
                        if not matched_category:
                            from difflib import get_close_matches; close_matches = get_close_matches(normalizar_texto(valor_campo), categorias_normalizadas, n=1, cutoff=0.7)
                            if close_matches: idx = categorias_normalizadas.index(close_matches[0]); matched_category = CATEGORIAS_RECLAMO[idx]
                        if matched_category: memoria["categoria_reclamo"] = matched_category
                        else: logger.warning(f"Categoría '{valor_campo}' no válida o no reconocida. Se pedirá.")
                    elif campo == "telefono":
                        if validar_telefono(valor_campo): memoria["telefono_vecino"] = valor_campo.strip()
                        else: logger.warning(f"Teléfono '{valor_campo}' no válido. Se pedirá.")
                    elif campo == "email":
                        if validar_email(valor_campo): memoria["email_vecino"] = valor_campo.strip()
                        else: logger.warning(f"Email '{valor_campo}' no válido. Se pedirá.")
                    else:
                        if campo == "nombre": memoria["nombre_vecino"] = valor_campo.strip()
                        elif campo == "descripcion": memoria["descripcion_reclamo"] = datos_extraidos_reclamo_inteligente.get("descripcion", "").strip()
                        else: memoria[campo] = valor_campo.strip()

            if any(memoria.get(f"{c}_reclamo") or memoria.get(f"{c}_vecino") for c in self.CAMPOS_RECLAMO): # If any field was extracted
                if all(memoria.get(f"{c}_reclamo" if c not in ["nombre", "telefono", "email"] else f"{c}_vecino") for c in self.CAMPOS_RECLAMO): # If all fields extracted
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
                    # If not all data extracted, initiate step-by-step by asking the first question (category)
                    memoria["estado_conversacion"] = ConversationState.ESPERANDO_CATEGORIA_RECLAMO.name
                    # This handler will now respond to start the flow
                    sugeridas_data_intel = sugerir_categorias_relevantes(pregunta_str)
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
        logger.info(f"[ReclamoHandler.handle ENTRY] Pregunta: '{pregunta_str[:100]}...', Estado actual: {estado.name if estado else 'None'}, Intención: {intencion}")


        # If intent is to start a claim AND no specific state is yet set (or was cleared due to invalid string)
        if intencion == "iniciar_reclamo" and estado is None:
            logger.info(f"[ReclamoHandler] Intención 'iniciar_reclamo' y sin estado activo. Iniciando nuevo flujo de reclamo."); memoria.clear()
            datos_archivo = self.context.get("datos_interpretados_archivo")
            if datos_archivo and isinstance(datos_archivo, dict):
                logger.info(f"[ReclamoHandler] Pre-llenando memoria con datos de archivo: {datos_archivo}")
                cat_archivo = datos_archivo.get("tipo_problema") or datos_archivo.get("categoria")
                if cat_archivo:
                    matched_category = next((c for c in CATEGORIAS_RECLAMO if normalizar_texto(c) == normalizar_texto(cat_archivo)), None)
                    if not matched_category:
                        from difflib import get_close_matches; close_matches = get_close_matches(normalizar_texto(cat_archivo), categorias_normalizadas, n=1, cutoff=0.7)
                        if close_matches: idx = categorias_normalizadas.index(close_matches[0]); matched_category = CATEGORIAS_RECLAMO[idx]
                    if matched_category: memoria["categoria_reclamo"] = matched_category; logger.info(f"[ReclamoHandler] Pre-llenado categoria_reclamo: {matched_category}")
                dir_archivo = datos_archivo.get("direccion_problema") or datos_archivo.get("direccion")
                if dir_archivo and direccion_es_valida(dir_archivo): memoria["direccion_reclamo"] = dir_archivo.strip(); logger.info(f"[ReclamoHandler] Pre-llenado direccion_reclamo: {dir_archivo.strip()}")
                nombre_archivo = datos_archivo.get("nombre_ciudadano") or datos_archivo.get("nombre_cliente")
                if nombre_archivo and len(nombre_archivo.split()) >= 1: memoria["nombre_vecino"] = nombre_archivo.strip(); logger.info(f"[ReclamoHandler] Pre-llenado nombre_vecino: {nombre_archivo.strip()}")
                tel_archivo = datos_archivo.get("telefono_ciudadano") or datos_archivo.get("telefono_cliente")
                if tel_archivo and validar_telefono(tel_archivo): memoria["telefono_vecino"] = tel_archivo.strip(); logger.info(f"[ReclamoHandler] Pre-llenado telefono_vecino: {tel_archivo.strip()}")
                email_archivo = datos_archivo.get("email_ciudadano") or datos_archivo.get("email_cliente")
                if email_archivo and validar_email(email_archivo): memoria["email_vecino"] = email_archivo.strip(); logger.info(f"[ReclamoHandler] Pre-llenado email_vecino: {email_archivo.strip()}")
                desc_archivo = datos_archivo.get("descripcion_corta_problema") or datos_archivo.get("descripcion_problema") or datos_archivo.get("detalles_adicionales")
                if desc_archivo and len(desc_archivo) >= 10: memoria["descripcion_reclamo"] = desc_archivo.strip(); logger.info(f"[ReclamoHandler] Pre-llenado descripcion_reclamo: {desc_archivo.strip()}")

            memoria["estado_conversacion"] = ConversationState.ESPERANDO_CATEGORIA_RECLAMO.name
            estado = ConversationState.ESPERANDO_CATEGORIA_RECLAMO # Update local 'estado' for current pass

            # This block will now proceed to the while loop if categoria_reclamo was not pre-filled,
            # or it will ask for category directly if it was pre-filled and then ask for address.
            # The first question is now consistently handled by the while loop logic below.

        # Check if the current state is a valid reclamo state
        # This condition handles ongoing claims or claims initiated by ReclamoInteligente
        if estado not in RECLAMO_STATES:
            # If state is None but intent was not "iniciar_reclamo", this handler isn't responsible
            if estado is None and intencion != "iniciar_reclamo":
                return None
            # If state is not None but also not a RECLAMO_STATE, it's an invalid/unexpected state for this handler
            if estado is not None:
                 logger.info(f"[ReclamoHandler] Estado '{estado.name if isinstance(estado, Enum) else estado}' no es un estado de reclamo válido. Retornando None.")
                 return None
            # If estado is None and intencion was "iniciar_reclamo", the block above should have set it.
            # If it's still None here, it means the init block didn't return and something is amiss.
            if estado is None and intencion == "iniciar_reclamo":
                logger.warning("[ReclamoHandler] Estado es None y la intención es iniciar_reclamo, pero el bloque de inicialización no respondió. Forzando pregunta de categoría.")
                memoria["estado_conversacion"] = ConversationState.ESPERANDO_CATEGORIA_RECLAMO.name
                estado = ConversationState.ESPERANDO_CATEGORIA_RECLAMO # Ensure local 'estado' is updated
                # Fall through to the while loop to ask the category question

        # If we reach here, 'estado' must be a valid Enum member from RECLAMO_STATES
        if not isinstance(estado, ConversationState) or estado not in RECLAMO_STATES:
             logger.error(f"[ReclamoHandler] Critical error: Estado '{estado}' no es válido para el bucle de reclamos. Abortando.")
             memoria.clear() # Clear to prevent loops
             memoria["estado_conversacion"] = None
             return {"respuesta": "Hubo un error procesando tu reclamo. Por favor, intentá de nuevo."}


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
                    tel_val = validar_telefono(extracted_details['telefono_cliente']) # validar_telefono already strips
                    if tel_val:
                        memoria["telefono_vecino"] = tel_val; llm_updated_any_field_in_this_pass = True; logger.info(f"LLM set telefono_vecino: {tel_val}")

                if 'email_cliente' in extracted_details and extracted_details['email_cliente'] and not memoria.get("email_vecino"):
                    email_val = validar_email(extracted_details['email_cliente']) # validar_email already strips
                    if email_val:
                        memoria["email_vecino"] = email_val; llm_updated_any_field_in_this_pass = True; logger.info(f"LLM set email_vecino: {email_val}")

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
                if memoria.get("categoria_reclamo"):
                    logger.debug(f"[ReclamoHandler] Categoria ya en memoria: '{memoria['categoria_reclamo']}'. Avanzando.")
                    memoria["estado_conversacion"] = ConversationState.ESPERANDO_DIRECCION_RECLAMO.name
                    estado = ConversationState.ESPERANDO_DIRECCION_RECLAMO
                    if all(memoria.get(campo) for campo in ["direccion_reclamo", "nombre_vecino", "telefono_vecino", "email_vecino", "descripcion_reclamo"]): # If LLM filled everything else
                        logger.info("[ReclamoHandler] Categoria y todos los demás datos ya en memoria. Saltando a confirmación.")
                        memoria["estado_conversacion"] = ConversationState.ESPERANDO_CONFIRMACION_RECLAMO.name
                        estado = ConversationState.ESPERANDO_CONFIRMACION_RECLAMO
                    pregunta_str = "" # Clear pregunta_str so it doesn't get reprocessed for next field in same turn
                    continue

                # If categoria_reclamo is not in memoria, ask for it (or process current pregunta_str for it)
                texto_normalizado = normalizar_texto(pregunta_str); categoria_final = None
                logger.info(f"[ReclamoHandler] Estado: ESPERANDO_CATEGORIA_RECLAMO. Input: '{pregunta_str}'. Normalizado: '{texto_normalizado}'.")

                if texto_normalizado in categorias_normalizadas:
                    idx = categorias_normalizadas.index(texto_normalizado); categoria_final = CATEGORIAS_RECLAMO[idx]
                else:
                    from difflib import get_close_matches; matches = get_close_matches(texto_normalizado, categorias_normalizadas, n=1, cutoff=0.7)
                    if matches: idx = categorias_normalizadas.index(matches[0]); categoria_final = CATEGORIAS_RECLAMO[idx]

                if not categoria_final and pregunta_str: # Try LLM classification if not found by keyword/fuzzy
                    try:
                        respuesta_llm_cat = _clasificar_intencion_con_llm(pregunta_str, opciones=CATEGORIAS_RECLAMO, tipo="categoría")
                        if respuesta_llm_cat and respuesta_llm_cat in CATEGORIAS_RECLAMO: categoria_final = respuesta_llm_cat
                    except Exception: pass

                if categoria_final:
                    memoria["categoria_reclamo"] = categoria_final
                    logger.info(f"[ReclamoHandler] Categoría guardada: {categoria_final}.")
                    # Check if all data is now present to jump to confirmation
                    if all(memoria.get(campo) for campo in ["direccion_reclamo", "nombre_vecino", "telefono_vecino", "email_vecino", "descripcion_reclamo"]):
                        memoria["estado_conversacion"] = ConversationState.ESPERANDO_CONFIRMACION_RECLAMO.name
                        estado = ConversationState.ESPERANDO_CONFIRMACION_RECLAMO
                        logger.info(f"[ReclamoHandler] Todos los datos completos tras categoría. Avanzando a confirmación.")
                    else:
                        memoria["estado_conversacion"] = ConversationState.ESPERANDO_DIRECCION_RECLAMO.name
                        estado = ConversationState.ESPERANDO_DIRECCION_RECLAMO
                        logger.info(f"[ReclamoHandler] Avanzando a ESPERANDO_DIRECCION_RECLAMO.")

                    # Only return a response if this specific step (category) was fulfilled by the CURRENT user input.
                    # If category was pre-filled by LLM from a multi-data message, we just continue the loop.
                    if pregunta_str == payload.get("pregunta",""):
                         # Check if next field (direccion) is already filled to skip asking for it.
                        if memoria.get("direccion_reclamo"):
                            # If all subsequent fields are also filled, this will be caught by the all() check at the top of the next iteration
                            # or by the all() check when transitioning to ESPERANDO_CONFIRMACION_RECLAMO
                            pregunta_str = "" # Clear to avoid re-processing for next field in this turn
                            continue # Loop to ask for the next unfilled item
                        else:
                            return { "respuesta": f"Perfecto, categoría: **{categoria_final.title()}**. ¿La **dirección exacta** del problema?\nPor ejemplo: {EJEMPLO_DIRECCION}"}
                    pregunta_str = "" # Clear to avoid re-processing for next field in this turn
                    continue
                else: # Could not determine category from input
                    sugeridas_data = sugerir_categorias_relevantes(pregunta_str)
                    options_data = sugeridas_data if sugeridas_data else CATEGORIAS_RECLAMO
                    options = [{"id": normalizar_texto(c), "texto": c.title()} for c in options_data]
                    respuesta_texto = "¡Ups! No encontré esa categoría." if sugeridas_data else "No entendí la categoría."
                    respuesta_texto += " ¿Podrías elegir una de estas opciones o describirla mejor?"
                    message_type = 'interactive_list' if len(options) > 3 else 'interactive_buttons'
                    if len(options) > 10: logger.warning(f"ReclamoHandler Esperando Categoria: Too many options ({len(options)}) for WhatsApp list.")
                    return {"message_body": respuesta_texto, "options_list": options, "message_type": message_type, "fuente": "solicitud_categoria_reclamo_interactivo_v2"}

            # 2. ESPERANDO_DIRECCION_RECLAMO
            elif current_state_for_logic == ConversationState.ESPERANDO_DIRECCION_RECLAMO:
                if memoria.get("direccion_reclamo"):
                    logger.debug(f"[ReclamoHandler] Dirección ya en memoria: '{memoria['direccion_reclamo']}'. Avanzando.")
                    memoria["estado_conversacion"] = ConversationState.ESPERANDO_NOMBRE_VECINO.name; estado = ConversationState.ESPERANDO_NOMBRE_VECINO
                    if all(memoria.get(campo) for campo in ["nombre_vecino", "telefono_vecino", "email_vecino", "descripcion_reclamo"]):
                        logger.info("[ReclamoHandler] Dirección y todos los demás datos ya en memoria. Saltando a confirmación.")
                        memoria["estado_conversacion"] = ConversationState.ESPERANDO_CONFIRMACION_RECLAMO.name
                        estado = ConversationState.ESPERANDO_CONFIRMACION_RECLAMO
                    pregunta_str = ""
                    continue

                if pregunta_str: # Only process if there's new input for this field
                    if es_pregunta_nueva(pregunta_str, "una dirección", memoria):
                        logger.info(f"[ReclamoHandler] '{pregunta_str}' detectada como pregunta nueva. Limpiando reclamo.")
                        for key in list(memoria.keys()):
                            if key.endswith(('_reclamo', '_vecino')) or key in ['foto_url', 'ubicacion_gps', 'direccion_estructurada_reclamo']: memoria.pop(key, None)
                        memoria["estado_conversacion"] = None; self.context["intencion"] = None; return None
                
                if payload.get("es_foto") or payload.get("es_ubicacion"):
                    return {"respuesta": "Entendido. Para asociar tu foto/ubicación, primero necesito la dirección escrita del problema (ej. 'Av. San Martín 123'). ¿Me la decís?"}

                logger.info(f"[ReclamoHandler] Estado: ESPERANDO_DIRECCION_RECLAMO. Input: '{pregunta_str}'.")
                config_muni_parseo = self.context.get("municipio_config") or CONFIG_MUNICIPIO
                parsed_address = parse_direccion_completa(pregunta_str, config_muni_parseo)

                if parsed_address and parsed_address.get("calle") and parsed_address.get("localidad"):
                    memoria["direccion_estructurada_reclamo"] = parsed_address
                    dir_confirm_text = f"{parsed_address['calle']} {parsed_address.get('numero', '')}, {parsed_address['localidad']}".replace(" ,",",").strip()
                    memoria["direccion_reclamo"] = dir_confirm_text
                    logger.info(f"[ReclamoHandler] Dirección guardada: {dir_confirm_text}.")
                    if all(memoria.get(campo) for campo in ["nombre_vecino", "telefono_vecino", "email_vecino", "descripcion_reclamo"]):
                        memoria["estado_conversacion"] = ConversationState.ESPERANDO_CONFIRMACION_RECLAMO.name
                        estado = ConversationState.ESPERANDO_CONFIRMACION_RECLAMO
                    else:
                        memoria["estado_conversacion"] = ConversationState.ESPERANDO_NOMBRE_VECINO.name
                        estado = ConversationState.ESPERANDO_NOMBRE_VECINO

                    if pregunta_str == payload.get("pregunta",""):
                        if memoria.get("nombre_vecino"): # If next field already filled
                            pregunta_str = "" # Clear to avoid re-processing
                            continue # Loop to ask for the next unfilled item
                        else:
                            return {"respuesta": f"¡Perfecto! Dirección registrada como: **{memoria['direccion_reclamo']}**. Ahora, ¿podrías decirme tu **nombre completo**?"}
                    pregunta_str = ""
                    continue
                else: # Direccion no valida
                    respuesta_dir_inv = f"La dirección '{pregunta_str}' no parece completa o válida. ¿Podrías verificarla? Necesito algo como '{EJEMPLO_DIRECCION}, Localidad'."
                    # ... (logic for anon GPS suggestion remains the same)
                    if self.context.get("anon_id") and not self.context.get("cliente_id"):
                        allow_gps = False; # type: ignore
                        if has_app_context(): allow_gps = current_app.config.get("ALLOW_ANON_GPS", False) # type: ignore
                        if allow_gps: respuesta_dir_inv += ("\nSi tenés problemas, podés compartir tu ubicación GPS.")
                        else: respuesta_dir_inv += ("\nTras registrarte, podrás compartir tu ubicación GPS.")
                    return {"respuesta": respuesta_dir_inv}

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
                if parse_direccion_completa(nombre_input, config_muni_parseo_nombre) and len(nombre_input.split()) > 1:
                    return {"respuesta": "Estaba esperando tu nombre, pero eso parece una dirección. ¿Podrías ingresar tu nombre y apellido, por favor?"}

                # Si la entrada es un número y podría ser un teléfono, no interrumpir el flujo de reclamo.
                if validar_telefono(nombre_input):
                    logger.warning(f"[ReclamoHandler] Input '{nombre_input}' para NOMBRE parece un teléfono. Repreguntando nombre sin perder contexto.")
                    # No cambiar estado, no guardar el teléfono aquí, solo repreguntar el nombre.
                    return {"respuesta": "Estaba esperando tu nombre y apellido, pero eso parece un número de teléfono. ¿Podrías decírmelos, por favor?"}

                if not nombre_input or len(nombre_input.split()) < 1: # Allow single name, can be expanded by user if needed. Original was < 2
                    return {"respuesta": "Para continuar, necesitaría tu **nombre y apellido** (o al menos un nombre). ¿Podrías ingresarlos?"}

                memoria["nombre_vecino"] = nombre_input
                logger.info(f"[ReclamoHandler] Nombre guardado: '{nombre_input}'.")
                if all(memoria.get(campo) for campo in ["telefono_vecino", "email_vecino", "descripcion_reclamo"]):
                    memoria["estado_conversacion"] = ConversationState.ESPERANDO_CONFIRMACION_RECLAMO.name
                    estado = ConversationState.ESPERANDO_CONFIRMACION_RECLAMO
                else:
                    memoria["estado_conversacion"] = ConversationState.ESPERANDO_TELEFONO_VECINO.name
                    estado = ConversationState.ESPERANDO_TELEFONO_VECINO

                if pregunta_str == payload.get("pregunta",""):
                    if memoria.get("telefono_vecino"): # If next field already filled
                        pregunta_str = ""
                        continue
                    else:
                        return {"respuesta": f"¡Gracias, {nombre_input.split()[0]}! Ahora, ¿me pasarías tu **número de teléfono con código de área**?"}
                pregunta_str = ""
                continue

            # 4. ESPERANDO_TELEFONO_VECINO
            elif current_state_for_logic == ConversationState.ESPERANDO_TELEFONO_VECINO:
                if memoria.get("telefono_vecino"):
                    logger.debug(f"[ReclamoHandler] Teléfono ya en memoria: '{memoria['telefono_vecino']}'. Avanzando.")
                    memoria["estado_conversacion"] = ConversationState.ESPERANDO_EMAIL_VECINO.name; estado = ConversationState.ESPERANDO_EMAIL_VECINO
                    if all(memoria.get(campo) for campo in ["email_vecino", "descripcion_reclamo"]):
                        memoria["estado_conversacion"] = ConversationState.ESPERANDO_CONFIRMACION_RECLAMO.name
                        estado = ConversationState.ESPERANDO_CONFIRMACION_RECLAMO
                    pregunta_str = ""
                    continue

                if pregunta_str and es_pregunta_nueva(pregunta_str, "tu número de teléfono", memoria):
                    logger.info(f"[ReclamoHandler] '{pregunta_str}' detectada como pregunta nueva. Limpiando reclamo."); # ... (clear logic)
                    for key in list(memoria.keys()):
                        if key.endswith(('_reclamo', '_vecino')) or key in ['foto_url', 'ubicacion_gps', 'direccion_estructurada_reclamo']: memoria.pop(key, None)
                    memoria["estado_conversacion"] = None; self.context["intencion"] = None; return None

                telefono_input = pregunta_str.strip()
                logger.info(f"[ReclamoHandler] Estado: ESPERANDO_TELEFONO_VECINO. Input: '{telefono_input}'.")
                telefono_validado = validar_telefono(telefono_input)
                if telefono_validado:
                    memoria["telefono_vecino"] = telefono_validado; logger.info(f"[ReclamoHandler] Teléfono guardado: '{telefono_validado}'.")
                    if all(memoria.get(campo) for campo in ["email_vecino", "descripcion_reclamo"]):
                        memoria["estado_conversacion"] = ConversationState.ESPERANDO_CONFIRMACION_RECLAMO.name
                        estado = ConversationState.ESPERANDO_CONFIRMACION_RECLAMO
                    else:
                        memoria["estado_conversacion"] = ConversationState.ESPERANDO_EMAIL_VECINO.name
                        estado = ConversationState.ESPERANDO_EMAIL_VECINO

                    if pregunta_str == payload.get("pregunta",""):
                        if memoria.get("email_vecino"): # If next field already filled
                            pregunta_str = ""
                            continue
                        else:
                             return {"respuesta": "¡Excelente! Casi terminamos. ¿Cuál es tu **dirección de correo electrónico**?"}
                    pregunta_str = ""
                    continue
                else: # Telefono no valido
                    # ... (validations for address in phone field)
                    config_muni_parseo_tel = self.context.get("municipio_config") or CONFIG_MUNICIPIO
                    if parse_direccion_completa(telefono_input, config_muni_parseo_tel) and len(telefono_input.split()) > 1: return {"respuesta": "Estaba esperando un teléfono, pero eso parece una dirección. ¿Tu teléfono?"}
                    return {"respuesta": "El **teléfono** no parece válido. ¿Podrías revisarlo (solo números con código de área)?"}

            # 5. ESPERANDO_EMAIL_VECINO
            elif current_state_for_logic == ConversationState.ESPERANDO_EMAIL_VECINO:
                if memoria.get("email_vecino"):
                    logger.debug(f"[ReclamoHandler] Email ya en memoria: '{memoria['email_vecino']}'. Avanzando.")
                    memoria["estado_conversacion"] = ConversationState.ESPERANDO_DESCRIPCION_RECLAMO.name; estado = ConversationState.ESPERANDO_DESCRIPCION_RECLAMO
                    if memoria.get("descripcion_reclamo"): # If description also filled
                        memoria["estado_conversacion"] = ConversationState.ESPERANDO_CONFIRMACION_RECLAMO.name
                        estado = ConversationState.ESPERANDO_CONFIRMACION_RECLAMO
                    pregunta_str = ""
                    continue

                if pregunta_str and es_pregunta_nueva(pregunta_str, "tu correo electrónico", memoria):
                    logger.info(f"[ReclamoHandler] '{pregunta_str}' detectada como pregunta nueva. Limpiando reclamo."); # ... (clear logic)
                    for key in list(memoria.keys()):
                        if key.endswith(('_reclamo', '_vecino')) or key in ['foto_url', 'ubicacion_gps', 'direccion_estructurada_reclamo']: memoria.pop(key, None)
                    memoria["estado_conversacion"] = None; self.context["intencion"] = None; return None

                email_input = pregunta_str.strip()
                logger.info(f"[ReclamoHandler] Estado: ESPERANDO_EMAIL_VECINO. Input: '{email_input}'.")
                email_validado = validar_email(email_input)
                if email_validado:
                    memoria["email_vecino"] = email_validado; logger.info(f"[ReclamoHandler] Email guardado: '{email_validado}'.")
                    if memoria.get("descripcion_reclamo"): # If description also filled
                        memoria["estado_conversacion"] = ConversationState.ESPERANDO_CONFIRMACION_RECLAMO.name
                        estado = ConversationState.ESPERANDO_CONFIRMACION_RECLAMO
                    else:
                        memoria["estado_conversacion"] = ConversationState.ESPERANDO_DESCRIPCION_RECLAMO.name
                        estado = ConversationState.ESPERANDO_DESCRIPCION_RECLAMO

                    if pregunta_str == payload.get("pregunta",""):
                        if memoria.get("descripcion_reclamo"): # If next field already filled
                            pregunta_str = ""
                            continue
                        else:
                            return {"respuesta": "¡Bárbaro! Ahora, por favor, contame con un poco más de detalle **cuál es el problema**. Luego podrás adjuntar foto/ubicación si querés."}
                    pregunta_str = ""
                    continue
                else: # Email no valido
                    # ... (validations for address/phone in email field)
                    config_muni_parseo_email = self.context.get("municipio_config") or CONFIG_MUNICIPIO
                    if parse_direccion_completa(email_input, config_muni_parseo_email) and len(email_input.split()) > 1 : return {"respuesta": "Estaba esperando un email, pero eso parece una dirección. ¿Tu email?"}
                    if validar_telefono(email_input): return {"respuesta": "Estaba esperando un email, pero eso parece un teléfono. ¿Tu email?"}
                    return {"respuesta": "El **correo electrónico** no parece tener el formato correcto. ¿Podrías revisarlo?"}

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
                datos_sub_payload = payload.get("datos", {}); campo_descripcion = "descripcion_reclamo"
                if campo_descripcion in datos_sub_payload: descripcion_final = datos_sub_payload[campo_descripcion].strip()
                elif pregunta_str: descripcion_final = pregunta_str.strip()

                if not descripcion_final or len(descripcion_final) < 10:
                    return {"respuesta": "Para entender mejor, necesitaría una breve **descripción del problema**. ¿Podrías contarme más?"}

                memoria["descripcion_reclamo"] = descripcion_final
                logger.info(f"Descripción guardada: '{descripcion_final[:50]}...'.")
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
        # Ensure estado_after_loop is an Enum for comparison, or None
        if isinstance(estado_str_after_loop, str):
            try:
                estado_after_loop = ConversationState[estado_str_after_loop]
            except KeyError:
                logger.error(f"[ReclamoHandler] Estado inválido '{estado_str_after_loop}' en memoria tras bucle. Limpiando.")
                memoria.clear()
                return {"respuesta": "Hubo un error procesando tu reclamo. Por favor, intentá de nuevo."}
        elif not isinstance(estado_str_after_loop, ConversationState) and estado_str_after_loop is not None:
            logger.error(f"[ReclamoHandler] Tipo de estado inesperado '{type(estado_str_after_loop)}' en memoria tras bucle. Limpiando.")
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
            idempotency_key = payload.get("idempotency_key"); chat_session_uuid = self.context.get("chat_session_uuid")
            chat_session_data = self.context.get("chat_db_context_data")
            if idempotency_key and chat_session_uuid and chat_session_data is not None:
                processed_keys = chat_session_data.get("processed_idempotency_keys", {})
                if idempotency_key in processed_keys:
                    existing_ticket_nro = processed_keys[idempotency_key]
                    logger.info(f"[ReclamoHandler] Idempotency key '{idempotency_key}' ya procesada. Ticket existente: M-{existing_ticket_nro}.")
                    memoria.clear(); chat_session_data[CONTEXTO_MUNICIPIO] = memoria
                    # Original: return {"respuesta": (f"Este reclamo ya fue registrado..."), "botones": [...], "ticket_id": None}
                    body_idempotency = f"Este reclamo ya fue registrado anteriormente con el número de ticket: **M-{existing_ticket_nro}**. No se ha creado un nuevo ticket. ¡Gracias!"
                    options_idempotency = [
                        {"id": "iniciar_reclamo_nuevo", "texto": "Hacer un nuevo reclamo"},
                        {"id": "consultar_estado_ticket_existente", "texto": "Consultar estado de un ticket"}
                    ]
                    return {
                        "message_body": body_idempotency,
                        "options_list": options_idempotency,
                        "message_type": "interactive_buttons",
                        "fuente": "reclamo_idempotencia_detectada_v2",
                        "ticket_id": None
                    }
            elif idempotency_key and chat_session_data is None: logger.warning("[ReclamoHandler] chat_db_context_data no disponible para idempotencia.")

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
                    ticket_data = {"asunto": f"Reclamo de {categoria}", "categoria": categoria, "detalles": memoria.get("descripcion_reclamo", ""), "direccion": memoria.get("direccion_reclamo", ""), "nombre_vecino": nombre, "telefono_vecino": telefono_raw, "email": email, "estado": "nuevo", "user_id": self.context.get("cliente_id"), "anon_id": self.context.get("anon_id") if not self.context.get("cliente_id") else None, "municipio_id": getattr(self.context.get("user_obj"), "municipio_id", None), "ubicacion": memoria.get("ubicacion_gps"), "foto_url": memoria.get("foto_url")}
                    ticket = servicio_tickets.crear_nuevo_ticket(tipo_ticket="municipio", ticket_data=ticket_data)
                    # ... (idempotency and file association logic remains the same) ...
                    if ticket:
                        # ... (idempotency and file association logic as before) ...
                        if idempotency_key and chat_session_uuid and chat_session_data is not None: # Copied for completeness
                            processed_keys = chat_session_data.get("processed_idempotency_keys", {}); processed_keys[idempotency_key] = ticket.nro_ticket
                            chat_session_data["processed_idempotency_keys"] = processed_keys
                            logger.info(f"[ReclamoHandler] Idempotency key '{idempotency_key}' asociada al ticket M-{ticket.nro_ticket} y guardada en chat_db_context.context_data.")
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
                        try: enviar_notificacion_whatsapp_con_plantilla(telefono_e164, nombre, ticket.nro_ticket, categoria) # type: ignore
                        except Exception: pass
                        try: enviar_notificacion_sms(telefono_e164, f"Hola {nombre}! Tu reclamo M-{ticket.nro_ticket} ({categoria}) fue generado.") # type: ignore
                        except Exception: pass
                    memoria.clear()
                    # Original: return {"respuesta": (f"¡Excelente! Tu reclamo ha sido registrado..."), "botones": [...], "ticket_id": ticket.id}
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
            elif accion == "editar_reclamo_datos" or any(kw in accion for kw in EDIT_KEYWORDS) or "editar" in accion or "cambiar" in accion : # Added specific action ID
                memoria["estado_conversacion"] = ConversationState.ESPERANDO_CATEGORIA_RECLAMO.name
                # Original: return {"respuesta": ("Entendido. Vamos a revisar los datos..."), "botones": [{"texto": cat.title()} for cat in CATEGORIAS_RECLAMO]}
                body_editar = "Entendido. Vamos a revisar los datos desde el principio para que puedas corregir lo que necesites. Empecemos de nuevo con la categoría. ¿Cuál sería la categoría correcta para tu reclamo?"
                options_editar = [{"id": normalizar_texto(cat), "texto": cat.title()} for cat in CATEGORIAS_RECLAMO]
                return {
                    "message_body": body_editar,
                    "options_list": options_editar,
                    "message_type": 'interactive_list', # Categories can be many
                    "fuente": "reclamo_editar_datos_v2"
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
                body = "Para esta acción (como registrar reclamos, chatear con un agente, activar alertas, o realizar compras) necesitás iniciar sesión o registrarte. ¿Te gustaría hacerlo ahora?"
                options = [
                    {"id": "login_enganche_risky", "texto": "Iniciar Sesión"},
                    {"id": "register_enganche_risky", "texto": "Registrarme Gratis"},
                    {"id": "continuar_invitado_enganche_risky", "texto": "No, gracias"}
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
        ticket_data = {"asunto": "Solicitud de Chat en Vivo", "categoria": "Atención en Vivo", "detalles": f"El vecino solicitó chat en vivo con la pregunta: '{pregunta_str}'", "user_id": self.context.get("cliente_id"), "municipio_id": getattr(self.context.get("user_obj"), "municipio_id", None), "estado": "esperando_agente_en_vivo", "ubicacion": self.context.get("ubicacion_usuario")}
        try:
            sala_de_chat = servicio_tickets.crear_nuevo_ticket(tipo_ticket="municipio", ticket_data=ticket_data)
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

CATEGORIAS_RECLAMO = ["arbol caido", "arreglo de calle", "castracion de mascota", "falta de agua, rotura de caño", "fumigacion", "inspeccion de comercio", "limpieza", "luminaria", "riego de calle", "rotura de semaforo", "tramites de obras privadas", "otro motivo"]
categorias_normalizadas = [normalizar_texto(c) for c in CATEGORIAS_RECLAMO]
RECLAMO_STATES = [ConversationState.ESPERANDO_CATEGORIA_RECLAMO, ConversationState.ESPERANDO_DIRECCION_RECLAMO, ConversationState.ESPERANDO_NOMBRE_VECINO, ConversationState.ESPERANDO_TELEFONO_VECINO, ConversationState.ESPERANDO_EMAIL_VECINO, ConversationState.ESPERANDO_DESCRIPCION_RECLAMO, ConversationState.ESPERANDO_ADJUNTOS_RECLAMO, ConversationState.ESPERANDO_CONFIRMACION_RECLAMO]

def serializar_enum(obj):
    if isinstance(obj, Enum): return obj.name
    elif isinstance(obj, dict): return {k: serializar_enum(v) for k, v in obj.items()}
    elif isinstance(obj, list): return [serializar_enum(v) for v in obj]
    else: return obj

BOTONES_COMANDOS_MUNICIPIO = {"Hacer un reclamo": "iniciar_reclamo", "Consultar estado de un trámite": "consultar_estado_ticket", "Consultar estado de ticket": "consultar_estado_ticket", "Consultar otro ticket": "consultar_estado_ticket", "Hablar con un agente": "hablar_con_agente", "Nuevo reclamo": "iniciar_reclamo", "Adjuntar foto": "adjuntar_foto", "Compartir ubicación": "compartir_ubicacion", "Foto": "adjuntar_foto", "Ubicación": "compartir_ubicacion", "No, continuar": "sin_adjuntos", "Completar reclamo": "sin_adjuntos", "Sí, confirmar reclamo": "confirmar_reclamo", "Si, confirmar reclamo": "confirmar_reclamo", "Confirmar reclamo": "confirmar_reclamo", "Finalizar": "confirmar_reclamo", "Finalizar reclamo": "confirmar_reclamo", "Confirmar": "confirmar_reclamo", "Confirmado": "confirmar_reclamo", "Si confirmo": "confirmar_reclamo", "Sí confirmo": "confirmar_reclamo", "Editar datos": "editar_reclamo", "Sí, solucionado": "confirmar_cierre_ticket", "No, aún no": "no_cerrar_ticket"}
OWNER_HANDLERS_FOR_STATE = {ConversationState.ESPERANDO_CATEGORIA_RECLAMO: ReclamoHandler, ConversationState.ESPERANDO_DIRECCION_RECLAMO: ReclamoHandler, ConversationState.ESPERANDO_NOMBRE_VECINO: ReclamoHandler, ConversationState.ESPERANDO_TELEFONO_VECINO: ReclamoHandler, ConversationState.ESPERANDO_EMAIL_VECINO: ReclamoHandler, ConversationState.ESPERANDO_DESCRIPCION_RECLAMO: ReclamoHandler, ConversationState.ESPERANDO_ADJUNTOS_RECLAMO: ReclamoHandler, ConversationState.ESPERANDO_CONFIRMACION_RECLAMO: ReclamoHandler, ConversationState.ESPERANDO_NUMERO_TICKET: TicketStatusHandler, ConversationState.ESPERANDO_CONFIRMACION_CIERRE: TicketStatusHandler, ConversationState.ESPERANDO_CALIFICACION: TicketStatusHandler, ConversationState.ESPERANDO_PARAM_RECOLECCION: RecoleccionHandler, ConversationState.ESPERANDO_SELECCION_TRAMITE: TramitesHandler, ConversationState.ESPERANDO_PREGUNTA_CURSO_LICENCIA: TramitesHandler, ConversationState.ESPERANDO_TEXTO_SUGERENCIA: SugerenciasVecinoHandler, ConversationState.ESPERANDO_PRODUCTO_PARA_CONSULTA: ProductInquiryHandler, ConversationState.MOSTRANDO_PRODUCTOS: ProductInquiryHandler, ConversationState.ESPERANDO_CONFIRMACION_AGREGAR_CARRITO: ProductInquiryHandler, ConversationState.ESPERANDO_OPCION_CARRITO: CartHandler, ConversationState.ESPERANDO_DETALLES_CHECKOUT: CheckoutHandler, ConversationState.ESPERANDO_CONFIRMACION_PEDIDO: CheckoutHandler, ConversationState.ESPERANDO_UBICACION_PANICO: PanicButtonHandler}

def responder_municipio(pregunta_original, owner_user, rubro_obj, viewer_user=None, chat_db_context=None, anon_id=None, channel: str = "web", **kwargs):
    logger_actual = current_app.logger if has_app_context() else logger
    logger_actual.info(f"[RESPONDER_MUNICIPIO_START] Pregunta: '{pregunta_original}', UserMunicipio: {getattr(owner_user, 'id', 'N/A')}, ViewerCiudadano: {getattr(viewer_user, 'id', 'N/A')}, Anon: {anon_id}, Channel: {channel}, ChatSessionUUID: {kwargs.get('chat_session_uuid')}")
    received_payload = {}; pregunta_str = ""
    if isinstance(pregunta_original, dict): received_payload = pregunta_original; pregunta_str = received_payload.get("pregunta", "")
    elif isinstance(pregunta_original, str): pregunta_str = pregunta_original; received_payload["pregunta"] = pregunta_original
    else: logger_actual.warning(f"Tipo inesperado para pregunta_original: {type(pregunta_original)}. Contenido: {pregunta_original}"); pregunta_str = ""; received_payload["pregunta"] = ""
    if kwargs:
        for key, value in kwargs.items(): received_payload[key] = value
    if chat_db_context.context_data is None: chat_db_context.context_data = {}
    # Carga inicial del contexto específico del municipio
    contexto_municipio_data_from_db = chat_db_context.context_data.get(CONTEXTO_MUNICIPIO, {})
    logger_actual.info(f"[CONTEXTO_MUNICIPIO_LOAD_RAW] Contexto crudo para '{CONTEXTO_MUNICIPIO}' desde DB: {contexto_municipio_data_from_db}")

    # Crear una copia para modificar de forma segura para esta request.
    # Esto es importante si `contexto_municipio_data_from_db` es directamente el objeto que se guardará.
    # Si es una copia ya (ej. `dict(contexto_municipio_data_from_db)`), entonces no es estrictamente necesario, pero no hace daño.
    contexto_municipio_actual = dict(contexto_municipio_data_from_db)

    estado_guardado_raw = contexto_municipio_actual.get("estado_conversacion")
    logger_actual.info(f"[CONTEXTO_MUNICIPIO_LOAD_STATE_RAW] 'estado_conversacion' crudo extraído del contexto_municipio_actual: '{estado_guardado_raw}' (Tipo: {type(estado_guardado_raw)})")

    if estado_guardado_raw and isinstance(estado_guardado_raw, str):
        logger_actual.info(f"[CONTEXTO_MUNICIPIO_LOAD_STATE] Intentando convertir estado string '{estado_guardado_raw}' a Enum ConversationState.")
        try:
            estado_enum = ConversationState[estado_guardado_raw]
            contexto_municipio_actual["estado_conversacion"] = estado_enum
            logger_actual.info(f"[CONTEXTO_MUNICIPIO_LOAD_STATE] Éxito. 'estado_conversacion' ahora es Enum: {estado_enum}")
        except KeyError:
            logger_actual.error(f"[CONTEXTO_MUNICIPIO_LOAD_STATE] Falló conversión. String '{estado_guardado_raw}' no es un miembro válido de ConversationState. 'estado_conversacion' se establece a None.")
            contexto_municipio_actual["estado_conversacion"] = None
    elif estado_guardado_raw is None:
        logger_actual.info("[CONTEXTO_MUNICIPIO_LOAD_STATE] 'estado_conversacion' es None en los datos crudos. Se mantiene como None.")
        contexto_municipio_actual["estado_conversacion"] = None # Asegurar que sea None explícito
    elif isinstance(estado_guardado_raw, ConversationState):
        logger_actual.warning(f"[CONTEXTO_MUNICIPIO_LOAD_STATE] 'estado_conversacion' ya es un Enum ({estado_guardado_raw}) al cargar. Esto es inusual si se carga desde JSON/DB. Se usará tal cual.")
        contexto_municipio_actual["estado_conversacion"] = estado_guardado_raw # Mantener el Enum
    else: # Otros tipos inesperados
        logger_actual.error(f"[CONTEXTO_MUNICIPIO_LOAD_STATE] Tipo inesperado para 'estado_conversacion' ({type(estado_guardado_raw)}): '{estado_guardado_raw}'. Se establece a None.")
        contexto_municipio_actual["estado_conversacion"] = None

    # Log del estado final que se usará en esta petición
    final_loaded_state = contexto_municipio_actual.get("estado_conversacion")
    logger_actual.info(f"[CONTEXTO_MUNICIPIO_LOAD_FINAL] 'estado_conversacion' final para esta petición: '{final_loaded_state}' (Tipo: {type(final_loaded_state)})")

    context = {
        CONTEXTO_MUNICIPIO: contexto_municipio_actual, # Esta es la copia modificada
        "user_obj": owner_user, "user_id": getattr(owner_user, "id", None),
        "cliente_id": getattr(viewer_user, "id", None), "viewer_user_obj": viewer_user,
        "anon_id": anon_id, "intencion": None, "rubro_obj": rubro_obj,
        "channel": channel, # Pass channel into context for handlers
        "ubicacion_usuario": received_payload.get("ubicacion_usuario"),
        "foto_url": received_payload.get("archivo_url") if received_payload.get("es_foto") else None,
        "es_foto": received_payload.get("es_foto", False),
        "es_ubicacion": received_payload.get("es_ubicacion", False),
        "es_archivo": received_payload.get("es_archivo", False),
        "action": received_payload.get("action"),
        "datos_interpretados_archivo": kwargs.get("datos_interpretados_archivo"),
        "archivo_id_para_asociar": kwargs.get("archivo_id_para_asociar"),
        "chat_session_uuid": kwargs.get("chat_session_uuid"),
        "chat_db_context_data": chat_db_context.context_data 
    }

    # --- Logic for suggesting registration to anonymous users ---
    # This is the block that might need adjustment based on the error at line 2193
    estado_para_chequeo_sugerencia = contexto_municipio_actual.get("estado_conversacion") # Enum or None

    if not viewer_user and anon_id and has_app_context():
        estados_a_evitar_sugerencia_para_anon = [
            ConversationState.ESPERANDO_DIRECCION_RECLAMO, 
            ConversationState.ESPERANDO_NOMBRE_VECINO, 
            ConversationState.ESPERANDO_TELEFONO_VECINO, 
            ConversationState.ESPERANDO_EMAIL_VECINO, 
            ConversationState.ESPERANDO_DESCRIPCION_RECLAMO, 
            ConversationState.ESPERANDO_ADJUNTOS_RECLAMO, 
            ConversationState.ESPERANDO_CONFIRMACION_RECLAMO, 
            ConversationState.ESPERANDO_UBICACION_PANICO
        ]
        
        # This is where 'estado_actual_sugerencia' was used in the log (line 2193 refers to this condition)
        # We now use 'estado_para_chequeo_sugerencia' which is guaranteed to be defined.
        if estado_para_chequeo_sugerencia not in estados_a_evitar_sugerencia_para_anon:
            interacciones_anon_sesion = contexto_municipio_actual.get("interacciones_anon_sesion", 0)
            if len(pregunta_str.split()) > 1 or pregunta_str.lower() not in ["si", "no", "ok", "dale", "bueno"]:
                interacciones_anon_sesion += 1
            contexto_municipio_actual["interacciones_anon_sesion"] = interacciones_anon_sesion
            
            umbral_sugerencia = (current_app.config.get("MUNICIPIO_UMBRAL_SUGERENCIA_REGISTRO", 3) if has_app_context() else 3)
            
            if umbral_sugerencia and umbral_sugerencia > 0 and interacciones_anon_sesion >= umbral_sugerencia:
                if not contexto_municipio_actual.get("sugerencia_registro_emitida_ronda", False):
                    logger_actual.info(f"[RESPONDER_MUNICIPIO] Anon {anon_id} alcanzó umbral. Sugiriendo registro.")
                    contexto_municipio_actual["sugerencia_registro_emitida_ronda"] = True
                    
                    respuesta_sugerencia_obj = construir_respuesta_sugerir_registro(
                        mensaje_personalizado="Para ayudarte mejor con tus gestiones y reclamos.", 
                        tipo_entidad="municipio"
                    )
                    
                    sug_body = respuesta_sugerencia_obj.get("respuesta", "Te recomendamos registrarte para una mejor experiencia.")
                    sug_options_raw = respuesta_sugerencia_obj.get("botones", [])
                    sug_options_list = [{"id": btn.get("action", normalizar_texto(btn["texto"])), "texto": btn["texto"]} for btn in sug_options_raw]
                    sug_message_type = 'interactive_buttons' if sug_options_list else 'text'

                    chat_db_context.context_data[CONTEXTO_MUNICIPIO] = contexto_municipio_actual
                    if chat_db_context: # Ensure flag_modified is called if context is updated
                        flag_modified(chat_db_context, "context_data")

                    if anon_id and not viewer_user: # Log conversation for this early return
                        try:
                            db.session.add(Conversacion(session_id=context.get("chat_session_uuid") or anon_id, pregunta=pregunta_str, respuesta=sug_body, fuente=respuesta_sugerencia_obj.get("fuente", "sugerencia_registro_municipio"), rubro=getattr(context.get("rubro_obj"), "nombre", "municipio_general"), user_id=None))
                            db.session.commit()
                        except Exception as e_conv_sug_muni: 
                            logger_actual.error(f"Error guardando Conversacion (sugerencia MUNICIPIO): {e_conv_sug_muni}"); db.session.rollback()
                    
                    return { # Return the new structure
                        "message_body": sug_body,
                        "options_list": sug_options_list,
                        "message_type": sug_message_type,
                        "fuente": respuesta_sugerencia_obj.get("fuente", "sugerencia_registro_municipio_v2"),
                        "contexto_actualizado": {CONTEXTO_MUNICIPIO: serializar_enum(contexto_municipio_actual)}
                    }
            else: # Not reached umbral or umbral is 0/disabled
                contexto_municipio_actual.pop("sugerencia_registro_emitida_ronda", None)
                # Context will be saved at the end of responder_municipio if no early return.
    if context.get("datos_interpretados_archivo"): logger_actual.info(f"[MUNICIPIOS_HANDLER] Datos interpretados: {context['datos_interpretados_archivo']}")
    if context.get("archivo_id_para_asociar"): logger_actual.info(f"[MUNICIPIOS_HANDLER] Archivo ID para asociar: {context['archivo_id_para_asociar']}")
    comando_from_text = BOTONES_COMANDOS_MUNICIPIO.get(pregunta_str.strip())
    if comando_from_text and not context.get("action"): context["action"] = comando_from_text; received_payload["action"] = comando_from_text; logger_actual.info(f"[BOTON] Comando por texto: '{comando_from_text}'")
    elif context.get("action"): logger_actual.info(f"[BOTON] Comando por payload.action: '{context['action']}'")
    elif context.get("es_foto") or context.get("es_ubicacion"):
        logger_actual.info(f"[ADJUNTO] Detectado: foto={context['es_foto']}, ubicacion={context['es_ubicacion']}")
        if context.get("es_ubicacion"):
            if contexto_municipio_actual.get("intencion_pendiente_ubicacion") == "solicitar_ubicacion_tienda" and contexto_municipio_actual.get("estado_conversacion") == "ESPERANDO_UBICacion_PARA_TIENDAS": context["intencion"] = "solicitar_ubicacion_tienda"; logger_actual.info(f"[CONTEXTO] Ubicación para tiendas, re-evaluando con intención: {context['intencion']}")
            elif contexto_municipio_actual.get("intencion_pendiente_ubicacion") == "activar_panico" and contexto_municipio_actual.get("estado_conversacion") == ConversationState.ESPERANDO_UBICACION_PANICO: context["intencion"] = "activar_panico"; logger_actual.info(f"[CONTEXTO] Ubicación para PÁNICO, re-evaluando con intención: {context['intencion']}")

    estado_conversacion_actual = contexto_municipio_actual.get("estado_conversacion") # This is now an Enum or None
    active_state_log_name = estado_conversacion_actual.name if isinstance(estado_conversacion_actual, Enum) else str(estado_conversacion_actual)
    logger_actual.info(f"[HANDLER_CHAIN_START] Estado en memoria: {active_state_log_name}. Intención previa: {context.get('intencion')}")

    prioritized_handlers = [CancelHandler, PanicButtonHandler]
    if context.get('intencion') == 'hablar_con_agente': prioritized_handlers.append(HumanEscalationHandler)

    respuesta_final = None; dueño_handler_class = None
    for handler_class_iter in prioritized_handlers:
        handler_instance = handler_class_iter(context); respuesta_parcial = handler_instance.handle(received_payload)
        if respuesta_parcial: respuesta_final = respuesta_parcial; logger_actual.info(f"[HANDLER_CHAIN] Prioritized handler {handler_class_iter.__name__} respondió."); break

            if not respuesta_final and estado_conversacion_actual:
        dueño_handler_class_actual = OWNER_HANDLERS_FOR_STATE.get(estado_conversacion_actual)
        if dueño_handler_class_actual:
                    dueño_instance = dueño_handler_class_actual(context) # Create instance
            active_state_name_log = estado_conversacion_actual.name if isinstance(estado_conversacion_actual, Enum) else str(estado_conversacion_actual)
            logger_actual.info(f"[HANDLER_CHAIN] Estado activo '{active_state_name_log}'. Dando prioridad a {dueño_instance.__class__.__name__}")
            respuesta_parcial_dueño = dueño_instance.handle(received_payload)
                    if respuesta_parcial_dueño:
                        respuesta_final = respuesta_parcial_dueño
                        logger_actual.info(f"[HANDLER_CHAIN] Dueño del estado {dueño_instance.__class__.__name__} respondió.")
            else:
                logger_actual.info(f"[HANDLER_CHAIN] Dueño del estado ({dueño_instance.__class__.__name__}) no respondió. Re-evaluando intención.")
                IntentClassifierHandler(context).handle(received_payload) # Re-classify intent
                logger_actual.info(f"[HANDLER_CHAIN] Nueva intención post-dueño: {context.get('intencion')}")
                else: # No owner handler for the current state
            active_state_name_log_no_owner = estado_conversacion_actual.name if isinstance(estado_conversacion_actual, Enum) else str(estado_conversacion_actual)
                    logger_actual.warning(f"[HANDLER_CHAIN] Estado activo '{active_state_name_log_no_owner}' pero no se encontró handler dueño definido. Limpiando estado y re-clasificando.")
                    contexto_municipio_actual.pop("estado_conversacion", None) # Clear state
                    # No need to save chat_db_context here, will be saved at the end.
                    IntentClassifierHandler(context).handle(received_payload) # Re-classify intent
                    logger_actual.info(f"[HANDLER_CHAIN] Nueva intención post-limpieza de estado sin dueño: {context.get('intencion')}")

    if not respuesta_final: # If no prioritized handler or owner handler responded
        # Ensure intent is classified if not already set or if state was cleared
        if not context.get("intencion") and not contexto_municipio_actual.get("estado_conversacion"):
            logger_actual.info("[HANDLER_CHAIN] Ejecutando IntentClassifierHandler (sin estado activo, sin intención previa).")
            IntentClassifierHandler(context).handle(received_payload)
            logger_actual.info(f"[HANDLER_CHAIN] Intención post-clasificación inicial: {context.get('intencion')}")

        # Define the general sequence of handlers
        # EngancheAnonimoMunicipioHandler is now placed towards the end to allow other handlers to act first.
        # ReclamoInteligenteMunicipioHandler runs before ReclamoHandler.
        remaining_handlers = [
            GreetingHandler, PoliteHandler, SmallTalkHandler,
            HumanEscalationHandler,  # Already in prioritized, but check again if intent changed
            TicketStatusHandler, SugerenciasVecinoHandler, RecoleccionHandler,
            ReclamoInteligenteMunicipioHandler, ReclamoHandler, # ReclamoInteligente first
            TramitesHandler, TramiteInteligenteHandler, ImpuestosHandler,
            ProductCatalogHandler, ProductInquiryHandler, CartHandler, CheckoutHandler, StoreLocationHandler,
            ToolHandler, VectorMunicipioCatalogHandler,
            GeneralHandler, # General context-based answers
            EngancheAnonimoMunicipioHandler # Suggest login/register if still anonymous and no other handler took over
        ]

        for handler_class_iter_main in remaining_handlers:
            if respuesta_final: break # If a handler in this loop responds, exit

            # Skip if already run as prioritized and it's not HumanEscalation (which might run again if intent changes)
            if handler_class_iter_main in prioritized_handlers and handler_class_iter_main != HumanEscalationHandler:
                logger_actual.debug(f"[HANDLER_CHAIN] Saltando {handler_class_iter_main.__name__} (ya corrió como prioritario o no aplicó).")
                continue

            # Skip Enganche if user is logged in
            if handler_class_iter_main == EngancheAnonimoMunicipioHandler and context.get("cliente_id"):
                logger_actual.debug(f"[HANDLER_CHAIN] Saltando EngancheAnonimoMunicipioHandler (usuario logueado).")
                continue

            # Ensure `context[CONTEXTO_MUNICIPIO]["estado_conversacion"]` is an Enum for the handler
            # This conversion logic is important and should be robust.
            current_memoria_state_for_handler_raw = context[CONTEXTO_MUNICIPIO].get("estado_conversacion")
            current_memoria_state_for_handler_enum = None
            if isinstance(current_memoria_state_for_handler_raw, str):
                try: current_memoria_state_for_handler_enum = ConversationState[current_memoria_state_for_handler_raw]
                except KeyError: pass # Keep as None if invalid string
            elif isinstance(current_memoria_state_for_handler_raw, ConversationState):
                current_memoria_state_for_handler_enum = current_memoria_state_for_handler_raw

            original_state_in_context_before_handler = context[CONTEXTO_MUNICIPIO].get("estado_conversacion") # Store original (string or Enum or None)
            context[CONTEXTO_MUNICIPIO]["estado_conversacion"] = current_memoria_state_for_handler_enum # Set Enum for handler

            handler_instance = handler_class_iter_main(context)
            log_state_for_handler = current_memoria_state_for_handler_enum.name if current_memoria_state_for_handler_enum else 'None'
            logger_actual.info(f"[HANDLER_CHAIN] Intentando con handler: {handler_class_iter_main.__name__} (Intención: {context.get('intencion')}, Estado para Handler: {log_state_for_handler})")

            respuesta_parcial = handler_instance.handle(received_payload)

            # After handler execution, decide what state to persist.
            # If handler changed state to an Enum, convert to string for persistence.
            # If handler cleared state (set to None), persist None.
            # If handler set to a string (should not happen if handlers use Enums), persist that string.
            state_after_handler = context[CONTEXTO_MUNICIPIO].get("estado_conversacion")
            if isinstance(state_after_handler, ConversationState):
                context[CONTEXTO_MUNICIPIO]["estado_conversacion"] = state_after_handler.name # Persist as string
            elif state_after_handler is None:
                context[CONTEXTO_MUNICIPIO].pop("estado_conversacion", None) # Ensure it's None or key removed
            # else: it's already a string or some other type, persist as is.

            if respuesta_parcial:
                respuesta_final = respuesta_parcial
                logger_actual.info(f"[HANDLER_CHAIN] Handler {handler_class_iter_main.__name__} respondió.")
                break # Exit loop once a handler provides a response
            else:
                logger_actual.info(f"[HANDLER_CHAIN] Handler {handler_class_iter_main.__name__} no respondió.")
                # Restore the original state string if the handler didn't change it,
                # especially if it was temporarily converted from string to Enum for the handler.
                # This is tricky because the handler *might* have intentionally changed it.
                # The current logic (convert to string if Enum, else keep as is) after handler call handles most cases.
                # If handler returned None (no response), the `context[CONTEXTO_MUNICIPIO]["estado_conversacion"]`
                # reflects the state *after* the handler logic (which might have changed it).

    if not respuesta_final: # Fallback if no handler responded
        logger_actual.info("[HANDLER_CHAIN_FALLBACK] Ningún handler respondió. Usando fallback general.")
        # Ensure context state is cleared or reset if bot is confused
        current_fallback_state_raw = contexto_municipio_actual.get("estado_conversacion")
        current_fallback_state_enum = None
        if isinstance(current_fallback_state_raw, str):
            try: current_fallback_state_enum = ConversationState[current_fallback_state_raw]
            except KeyError: pass
        elif isinstance(current_fallback_state_raw, ConversationState): # Should be string by now
            current_fallback_state_enum = current_fallback_state_raw


        current_fallback_state = contexto_municipio_actual.get("estado_conversacion")

        options_fallback = [
            {"id": "iniciar_reclamo_fallback_main", "texto": "Hacer un reclamo"},
            {"id": "consultar_tramite_fallback_main", "texto": "Consultar un trámite"},
            {"id": "hablar_con_agente_fallback_main", "texto": "Hablar con un agente"}
        ]
        message_type_fallback = 'interactive_buttons'

        if current_fallback_state:
            estado_log_val = current_fallback_state
            if isinstance(current_fallback_state, Enum):
                estado_log_val = current_fallback_state.name
            logger_actual.error(
                f"[FALLBACK_ERROR] Fallback con estado activo no manejado: {estado_log_val}. Limpiando estado."
            )
            contexto_municipio_actual.clear()  # Clear context if bot got confused with active state
            body_fallback = (
                "¡Vaya! Parece que nos perdimos un poco. No te preocupes, empecemos de nuevo. ¿Cómo puedo ayudarte hoy?"
            )
        else:
            body_fallback = (
                "Disculpa, no estoy seguro de haber entendido bien tu consulta. ¿Podrías intentar reformular tu pregunta o elegir una de estas opciones?"
            )

        respuesta_final = {
            "message_body": body_fallback,
            "options_list": options_fallback,
            "message_type": message_type_fallback,
            "fuente": "municipio_fallback_general_v2",
        }

    # Ensure the state in `contexto_municipio_actual` is a string before assigning to `chat_db_context.context_data`
    # This was handled by the loop logic for `remaining_handlers`.
    # If a prioritized or owner handler was the last one, ensure its state is also stringified.
    estado_final_en_memoria = contexto_municipio_actual.get("estado_conversacion")
    if isinstance(estado_final_en_memoria, ConversationState):
        contexto_municipio_actual["estado_conversacion"] = estado_final_en_memoria.name
        logger_actual.info(f"[CONTEXTO_MUNICIPIO_PRE_SAVE] Estado Enum '{estado_final_en_memoria.name}' convertido a string para DB.")
    elif estado_final_en_memoria is None:
         contexto_municipio_actual.pop("estado_conversacion", None) # Ensure it's None or key removed
         logger_actual.info(f"[CONTEXTO_MUNICIPIO_PRE_SAVE] Estado es None. Se guardará como tal.")
    else: # Already a string or other type
        logger_actual.info(f"[CONTEXTO_MUNICIPIO_PRE_SAVE] Estado ya es string o tipo no-Enum: '{estado_final_en_memoria}'. Se guardará como tal.")

    chat_db_context.context_data[CONTEXTO_MUNICIPIO] = contexto_municipio_actual
    logger_actual.info(f"[CONTEXTO_MUNICIPIO_POST_SAVE_IN_DB_CONTEXT] Contexto municipio completo asignado a chat_db_context.data: {contexto_municipio_actual}")

    # Ensure SQLAlchemy detects changes to the JSON field
    from sqlalchemy.orm.attributes import flag_modified
    if chat_db_context: # Ensure chat_db_context exists
        flag_modified(chat_db_context, "context_data")

    # `serializar_enum` is for the HTTP response context, ensuring Enums are strings there too.
    # `contexto_municipio_actual` should already have its 'estado_conversacion' as a string if it was an Enum.
    contexto_serializado_para_respuesta_http = serializar_enum(contexto_municipio_actual)

    media_url_to_send = contexto_serializado_para_respuesta_http.get("foto_url")
    location_data_to_send = contexto_serializado_para_respuesta_http.get("ubicacion_gps")

    # Ensure message_body and options_list are correctly extracted from respuesta_final
    message_body_final = respuesta_final.get("message_body") or respuesta_final.get("respuesta", "") # Default to empty string
    options_list_final = respuesta_final.get("options_list") or respuesta_final.get("botones", []) # Default to empty list

    # Determine message_type for WhatsApp, defaulting to 'text'
    # This logic should align with how utils.whatsapp_utils.send_whatsapp_message formats messages
    message_type_final = "text" # Default
    if isinstance(options_list_final, list) and options_list_final:
        num_options = len(options_list_final)
        # Heuristic: if options are present, it's likely interactive.
        # Specific type ('interactive_buttons' vs 'interactive_list') depends on WhatsApp limits.
        # For simplicity, use 'interactive_buttons' if few, 'interactive_list' if many.
        # The actual formatting and limits are handled by the WhatsApp sending utility.
        # Here, we just hint at the intended type.
        if 0 < num_options <= 3:
            message_type_final = respuesta_final.get("message_type") or 'interactive_buttons'
        elif num_options > 3:
            message_type_final = respuesta_final.get("message_type") or 'interactive_list'
        # If message_type was explicitly set in respuesta_final, respect it.
        if "message_type" in respuesta_final and respuesta_final["message_type"]:
             message_type_final = respuesta_final["message_type"]

    final_response_dict = {
        "message_body": message_body_final,
        "options_list": options_list_final,
        "message_type": message_type_final, # This is now more dynamic
        "contexto_actualizado": {CONTEXTO_MUNICIPIO: contexto_serializado_para_respuesta_http},
        "ticket_id": respuesta_final.get("ticket_id", None),
        "media_url": media_url_to_send,
        "location_data": location_data_to_send,
        "adjuntos": [], # Populate this if needed
        "fuente": respuesta_final.get("fuente", "desconocida") # Add fuente for better tracking
    }

    uploaded_file_info = received_payload.get("uploaded_file_info")
    if uploaded_file_info and isinstance(uploaded_file_info, dict):
        if uploaded_file_info.get("url") and uploaded_file_info.get("name"):
            final_response_dict["adjuntos"].append({
                "nombre_original": uploaded_file_info["name"],
                "url_descarga": uploaded_file_info["url"],
                "tipo_mime": uploaded_file_info.get("type", 'application/octet-stream')
            })
            logger_actual.info(f"Adjuntando info de archivo subido: {uploaded_file_info['name']}")
    
    respuesta_log = (final_response_dict.get('message_body') or '')[:100]
    adjuntos_len = len(final_response_dict.get('adjuntos', []))
    logger_actual.info(f"[RESPONDER_MUNICIPIO_END] Respuesta: '{respuesta_log}...', Opciones: {len(options_list_final)}, TipoMsg: {message_type_final}, Fuente: {final_response_dict['fuente']}, Adjuntos: {adjuntos_len}")

    # Log conversation for anonymous users
    if anon_id and not viewer_user and respuesta_final and isinstance(respuesta_final, dict):
        try:
            db.session.add(Conversacion(
                session_id=kwargs.get("chat_session_uuid") or anon_id,
                pregunta=pregunta_str,
                respuesta=final_response_dict.get("message_body"),
                fuente=final_response_dict.get("fuente", "municipio_anon_respuesta"),
                rubro=getattr(context.get("rubro_obj"), "nombre", "municipio_general"),
                user_id=None
            ))
            db.session.commit()
            logger_actual.info(f"Conversación (municipio) para anon_id {anon_id}/session {kwargs.get('chat_session_uuid')} guardada.")
        except Exception as e_conv_muni:
            logger_actual.error(f"Error guardando conversación de municipio para anon_id {anon_id}/session {kwargs.get('chat_session_uuid')}: {e_conv_muni}", exc_info=True)
            db.session.rollback()

    return final_response_dict
