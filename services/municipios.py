import logging
import re
import json
import os
from enum import Enum, auto
import unicodedata
import difflib
from flask import current_app, has_app_context # Ensure current_app is imported directly
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
from .llm_utils import extract_complaint_details_llm
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
    if texto in {"ok", "gracias"}: return True # Consider "ok" and "gracias" as not new if they are short and simple.
    texto_norm = normalizar_texto(texto_usuario)

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
        return True # Usually a sign of ending or changing topic unless very specific context.

    # Specific rules to pass through valid-looking inputs to the handler
    if tipo_esperado == "una confirmación (sí o no)":
        if texto_norm in {"si", "sí", "no", "afirmativo", "negativo"}: return False
    if tipo_esperado == "una calificación del 1 al 5":
        if re.fullmatch(r"[1-5]", texto_norm): return False
    if tipo_esperado == "un número de ticket":
        if re.fullmatch(r"m?\-?\d{4,}", texto_norm) or (tipo_esperado == "un número de ticket" and texto_norm.isdigit()):
             return False

    # If it's not a clear interruption or a direct answer to a simple expected type, use LLM.
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
        if len(texto_norm.split()) <= 2 and texto_norm not in {"si", "sí", "no"}:
            if tipo_esperado == "una calificación del 1 al 5" and texto_norm.isdigit() and re.fullmatch(r"[1-5]", texto_norm):
                return False
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
            message_type = 'interactive_buttons' if len(options) <= 3 else 'interactive_list'
            if len(options) > 3 and len(options) > 10:
                logger.warning("GreetingHandler: Too many options for a single WhatsApp list. Truncating or consider sub-menus.")
            
            return {
                "message_body": greeting_body,
                "options_list": options,
                "message_type": message_type,
                "fuente": "saludo_municipio_interactivo_v2"
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
                return { "message_body": body, "options_list": options, "message_type": 'interactive_buttons', "fuente": "sugerencia_pedir_texto_v2" }
        if estado_conversacion == ConversationState.ESPERANDO_TEXTO_SUGERENCIA or sugerencia_texto_directo:
            if pregunta_str and not sugerencia_texto_directo and es_pregunta_nueva(pregunta_str, "el texto de tu sugerencia"): # quitamos 'memoria' de es_pregunta_nueva
                logger.info(f"[SugerenciasVecinoHandler] '{pregunta_str}' detectada como pregunta nueva. Limpiando.")
                memoria.clear(); self.context["intencion"] = None
                return None
            sugerencia_final = sugerencia_texto_directo if sugerencia_texto_directo else pregunta_str
            if not sugerencia_final or len(sugerencia_final) < 5:
                # ... (repregunta por texto corto)
                return { "message_body": "Por favor, ingresá el texto de tu sugerencia...", "options_list": [{"id": "cancelar_sugerencia_corta", "texto": "Cancelar sugerencia"}], "message_type": 'interactive_buttons', "fuente": "sugerencia_texto_corto_v2"}
            try:
                # ... (creación de ticket)
                ticket_data = {"asunto": "Nueva Sugerencia/Mejora del Vecino", "categoria": "Sugerencia", "detalles": sugerencia_final, "pregunta": sugerencia_final, "estado": "nueva_sugerencia", "user_id": self.context.get("cliente_id"), "anon_id": self.context.get("anon_id") if not self.context.get("cliente_id") else None, "municipio_id": getattr(self.context.get("user_obj"), "municipio_id", None)}
                ticket = servicio_tickets.crear_nuevo_ticket(tipo_ticket="municipio", ticket_data=ticket_data)
                if ticket:
                    # ... (mensaje de éxito)
                    nro_ticket_str = f"M-{ticket.nro_ticket}"
                    body_exito = f"¡Muchas gracias por tu sugerencia! Registrada como {nro_ticket_str}."
                    options_exito = [{"id": "hacer_otra_consulta_sug", "texto": "Hacer otra consulta"}, {"id": "volver_inicio_sug", "texto": "Volver al inicio"}]
                    return {"message_body": body_exito, "options_list": options_exito, "message_type": 'interactive_buttons', "fuente": "sugerencia_registrada_exito_v2"}
                else: raise Exception("Creación de ticket de sugerencia retornó None.")
            except Exception as e: 
                # ... (mensaje de error)
                return {"message_body": "Hubo un problema al registrar tu sugerencia...", "options_list": [], "message_type": "text", "fuente": "sugerencia_error_registro_v2"}
        return None

class CancelHandler(BaseMunicipioHandler):
    CANCEL_KEYWORDS = ["cancelar", "olvidalo", "no importa", "volver", "salir", "cancelar reclamo", "no quiero continuar", "parar", "detener"]
    def handle(self, payload: dict) -> dict | None:
        pregunta_str = payload.get("pregunta", ""); texto = normalizar_texto(pregunta_str)
        if any(kw in texto for kw in self.CANCEL_KEYWORDS) or payload.get("action") == "cancelar":
            self.context[CONTEXTO_MUNICIPIO].clear()
            body = "Operación cancelada. ¿Necesitás ayuda con otro trámite o reclamo?"
            options = [ {"id": "iniciar_reclamo", "texto": "Hacer un reclamo"}, {"id": "consultar_estado_ticket", "texto": "Consultar estado de ticket"}, {"id": "hablar_con_agente", "texto": "Hablar con un agente"} ]
            return { "message_body": body, "options_list": options, "message_type": 'interactive_buttons', "fuente": "cancel_handler_interactivo_v2"}
        return None

class PoliteHandler(BaseMunicipioHandler):
    KEYWORDS = {"gracias", "ok", "ok gracias", "muchas gracias", "dale", "perfecto", "genial"}
    def handle(self, payload: dict) -> dict | None:
        pregunta_str = payload.get("pregunta", ""); texto = normalizar_texto(pregunta_str)
        if texto in self.KEYWORDS:
            if not self.context[CONTEXTO_MUNICIPIO].get("estado_conversacion"): self.context[CONTEXTO_MUNICIPIO].clear()
            body = "¡De nada! ¿Necesitás ayuda con algo más?"
            options = [ {"id": "iniciar_reclamo", "texto": "Hacer un reclamo"}, {"id": "consultar_tramite", "texto": "Consultar estado de un trámite"}, {"id": "hablar_con_agente", "texto": "Hablar con un agente"} ]
            return { "message_body": body, "options_list": options, "message_type": 'interactive_buttons', "fuente": "polite_handler_interactivo_v2"}
        return None

class SmallTalkHandler(BaseMunicipioHandler):
    def handle(self, payload: dict) -> dict | None:
        pregunta_str = payload.get("pregunta", "")
        if detectar_small_talk_con_llm(pregunta_str):
            respuesta_text = generar_respuesta_small_talk(pregunta_str)
            return { "message_body": respuesta_text, "options_list": [], "message_type": "text", "fuente": "smalltalk_municipio_llm_v2"}
        return None

class RecoleccionHandler(BaseMunicipioHandler):
    KEYWORDS = ["basura", "recoleccion", "residuos", "basurero"]
    def handle(self, payload: dict) -> dict | None:
        pregunta_str = payload.get("pregunta", ""); memoria = self.context[CONTEXTO_MUNICIPIO]
        estado_str = memoria.get("estado_conversacion"); estado = ConversationState[estado_str] if isinstance(estado_str, str) else estado_str
        texto = normalizar_texto(pregunta_str)
        if estado == ConversationState.ESPERANDO_PARAM_RECOLECCION:
            if es_pregunta_nueva(pregunta_str, "una dirección"): memoria.clear(); return None # quitamos 'memoria'
            # ... (resto de la lógica de RecoleccionHandler)
            if not direccion_es_valida(pregunta_str): 
                return {"message_body": f"La dirección '{pregunta_str}' no parece completa...", "options_list": [], "message_type": "text", "fuente": "recoleccion_direccion_invalida_v2"}
            resultado = consultar_recoleccion_por_direccion(direccion=pregunta_str); memoria.clear()
            if not resultado or "No" in resultado:
                # ...
                return { "message_body": "No encontré información de recolección...", "options_list": [{"id": "consultar_otra_direccion_recoleccion", "texto": "Consultar otra"}, {"id": "hablar_con_agente", "texto": "Hablar con agente"}], "message_type": 'interactive_buttons', "fuente": "recoleccion_sin_resultado_opciones_v2"}
            # ...
            return { "message_body": f"{resultado}\n¿Consultás otra dirección...?", "options_list": [{"id": "consultar_otra_direccion_recoleccion", "texto": "Consultar otra"}, {"id": "iniciar_reclamo", "texto": "Hacer un reclamo"}], "message_type": 'interactive_buttons', "fuente": "recoleccion_resultado_opciones_v2"}
        if any(kw in texto for kw in self.KEYWORDS) and not estado:
            # ... (lógica similar)
            if direccion_es_valida(pregunta_str):
                # ...
                return {"message_body": "Resultado...", "options_list": [], "message_type": 'text'}
            else:
                memoria["estado_conversacion"] = ConversationState.ESPERANDO_PARAM_RECOLECCION.name
                return {"message_body": f"¿La dirección para consultar...?\nPor ejemplo: {EJEMPLO_DIRECCION}", "options_list": [], "message_type": "text", "fuente": "recoleccion_pedir_direccion_v2"}
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
                pass

        if current_state_enum and current_state_enum in RECLAMO_STATES:
            active_state_is_reclamo = True

        if active_state_is_reclamo:
            logger.info(f"[INTENT_CLASSIFIER] Reclamo en curso (estado: {current_state_enum.name}). Cediendo control.")
            if any(kw in texto_normalizado for kw in self.KEYWORDS_AGENTE):
                self.context["intencion"] = "hablar_con_agente"; memoria.clear(); return None
            if any(kw in texto_normalizado for kw in self.KEYWORDS_PANICO):
                self.context["intencion"] = "activar_panico"; memoria.clear(); return None
            return None

        if memoria.get("estado_conversacion"):
            if any(kw in texto_normalizado for kw in self.KEYWORDS_AGENTE): self.context["intencion"] = "hablar_con_agente"; memoria.clear(); return None
            if any(kw in texto_normalizado for kw in self.KEYWORDS_PANICO): self.context["intencion"] = "activar_panico"; memoria.clear(); return None
            self.context["intencion"] = "continuar_flujo";
            return None

        for kw in self.KEYWORDS_PANICO: # ... (resto de la lógica de keywords)
            if kw in texto_normalizado: self.context["intencion"] = "activar_panico"; memoria.clear(); return None
        for kw in self.KEYWORDS_AGENTE:
            if kw in texto_normalizado: self.context["intencion"] = "hablar_con_agente"; memoria.clear(); return None
        # ... (más keywords)
        if any(kw in texto_normalizado for kw in self.KEYWORDS_RECLAMO):
            self.context["intencion"] = "iniciar_reclamo"; return None
        # ...

        intencion_llm = _clasificar_intencion_con_llm(pregunta_str) 
        self.context["intencion"] = intencion_llm
        logger.info(f"[MUNICIPIO] Intención (LLM): {self.context.get('intencion')}"); return None

class TicketStatusHandler(BaseMunicipioHandler):
    def handle(self, payload: dict) -> dict | None:
        pregunta_str = payload.get("pregunta", ""); memoria = self.context[CONTEXTO_MUNICIPIO]; estado_conversacion_str = memoria.get("estado_conversacion"); estado_conversacion = ConversationState[estado_conversacion_str] if isinstance(estado_conversacion_str, str) else estado_conversacion_str;
        if estado_conversacion and estado_conversacion in RECLAMO_STATES: return None
        if estado_conversacion == ConversationState.ESPERANDO_CONFIRMACION_CIERRE:
            if es_pregunta_nueva(pregunta_str, "una confirmación (sí o no)"): memoria.clear(); return None # quitamos 'memoria'
            # ... (resto de la lógica)
        elif estado_conversacion == ConversationState.ESPERANDO_CALIFICACION:
            if es_pregunta_nueva(pregunta_str, "una calificación del 1 al 5"): memoria.clear(); return None # quitamos 'memoria'
            # ... (resto de la lógica)
        elif estado_conversacion == ConversationState.ESPERANDO_NUMERO_TICKET:
            if es_pregunta_nueva(pregunta_str, "un número de ticket"): memoria.clear(); return None # quitamos 'memoria'
            # ... (resto de la lógica)
        if self.context.get("intencion") == "consultar_estado_ticket":
            # ... (resto de la lógica)
            return {"message_body": "Respuesta sobre estado de ticket...", "options_list": [], "message_type": "text"} # Ejemplo
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

        if current_state_obj and current_state_obj in RECLAMO_STATES: return None # Cede a ReclamoHandler si ya está en flujo

        if self.context.get("intencion") != "iniciar_reclamo": return None

        if not memoria.get("categoria_reclamo") and not memoria.get("direccion_reclamo"):
            # ... (lógica original de extracción con LLM) ...
            # Si extrae datos y puede iniciar el flujo paso a paso:
            if any(memoria.get(f"{c}_reclamo") or memoria.get(f"{c}_vecino") for c in self.CAMPOS_RECLAMO):
                 if all(memoria.get(f"{c}_reclamo" if c not in ["nombre", "telefono", "email"] else f"{c}_vecino") for c in self.CAMPOS_RECLAMO):
                    # ... (confirmar reclamo completo)
                    return {"message_body": "Confirmar reclamo inteligente...", "options_list": [], "message_type": "text"}
                 else: # Iniciar paso a paso
                    memoria["estado_conversacion"] = ConversationState.ESPERANDO_CATEGORIA_RECLAMO.name
                    # ... (generar pregunta de categoría)
                    return {"message_body": "Pregunta de categoría...", "options_list": [], "message_type": "text"}
        return None
