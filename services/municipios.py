import logging
import re
import json
import os
from enum import Enum, auto
import unicodedata
import difflib
from models import MunicipioTicket, TicketComentario, db, SitioWebInfo
from services.cohere_ai import get_cohere_response
from services.ticket_service import servicio_tickets
from .logic import (
    _clasificar_intencion_con_llm,
    detectar_small_talk_con_llm,
    generar_respuesta_small_talk,
)
from twilio.rest import Client
from services.utils_placeholders import (
    reemplazar_placeholders,
    obtener_respuesta_municipio,
)
from services.config_loader import cargar_configuracion_municipio
from .herramientas_municipio import (
    consultar_recoleccion_por_direccion,
    categorizar_reclamo_por_palabra_clave,
    sugerir_categorias_relevantes,
    normalizar_texto,
    direccion_es_valida,
    TOOL_REGISTRY,
    KEYWORD_TO_CATEGORY_MAP,
)
from .common_utils import ( # Changed from services.utils to .common_utils
    validar_email,
    validar_telefono,
    formatear_telefono_e164,
)
import math

# --- Configuración de Logging (Asegúrate de que esto esté al inicio de tu aplicación o en un archivo de configuración de logging) ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(name)s - %(message)s')
logger = logging.getLogger(__name__)
# ---------------------------------------------------------------------------------------------------

CONTEXTO_MUNICIPIO = "contexto_municipio"

# Regex para detectar URLs en texto
URL_REGEX = re.compile(r"https?://\S+")


def agregar_botones_para_links(texto: str, botones: list) -> list:
    """Agrega botones para cualquier enlace presente en el texto."""
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
TWILIO_WHATSAPP_NUMBER = "whatsapp:+14155238886" # Este debería ser tu número de Twilio WhatsApp
TWILIO_WHATSAPP_CONTENT_SID = os.environ.get("TWILIO_WHATSAPP_CONTENT_SID") # SID de tu plantilla aprobada

MUNICIPIO_ID = os.environ.get("MUNICIPIO_ID", "default")
CONFIG_MUNICIPIO = cargar_configuracion_municipio(MUNICIPIO_ID, "config.json")

TODAS_LAS_CATEGORIAS_UNICAS = sorted(list(set(KEYWORD_TO_CATEGORY_MAP.values())))
BOTONES_TODAS_CATEGORIAS = [{"texto": cat} for cat in TODAS_LAS_CATEGORIAS_UNICAS]

MINI_FAQ_TRAMITES = cargar_configuracion_municipio(
    MUNICIPIO_ID, "mini_faq_tramites.json"
)

# --- Trámites municipales ---
_TRAMITES_CACHE = None


def cargar_tramites_info():
    """Carga la descripción de los trámites desde el JSON del municipio."""
    global _TRAMITES_CACHE
    if _TRAMITES_CACHE is None:
        _TRAMITES_CACHE = cargar_configuracion_municipio(MUNICIPIO_ID, "tramites.json")
    return _TRAMITES_CACHE


TRAMITES_INFO = cargar_tramites_info()

# URL por defecto para los trámites del municipio
DEFAULT_TRAMITES_WEB_URL = CONFIG_MUNICIPIO.get(
    "tramites_web_url", "https://www.ejemplo.gob.ar/tramites/"
)

# Dirección del municipio utilizada en respuestas
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
    ESPERANDO_TEXTO_SUGERENCIA = auto() # Nuevo estado para sugerencias

    # Estados para Ventas y Carrito
    ESPERANDO_PRODUCTO_PARA_CONSULTA = auto()
    MOSTRANDO_PRODUCTOS = auto()
    ESPERANDO_CONFIRMACION_AGREGAR_CARRITO = auto()
    ESPERANDO_OPCION_CARRITO = auto()
    ESPERANDO_DETALLES_CHECKOUT = auto() # Para pedir dirección, etc.
    ESPERANDO_CONFIRMACION_PEDIDO = auto()

    # Estado para Pánico
    ESPERANDO_UBICACION_PANICO = auto()


# --- Carga del Catálogo de Productos ---
_PRODUCT_CATALOG_CACHE = None

def cargar_catalogo_productos():
    """Carga el catálogo de productos desde el JSON."""
    global _PRODUCT_CATALOG_CACHE
    if _PRODUCT_CATALOG_CACHE is None:
        try:
            catalog_file_path = os.path.join(os.path.dirname(__file__), "..", "data", "product_catalog.json")
            with open(catalog_file_path, "r", encoding="utf-8") as f:
                _PRODUCT_CATALOG_CACHE = json.load(f)
            logger.info(f"✅ Catálogo de productos cargado desde {catalog_file_path}")
        except Exception as e:
            logger.error(f"❌ No se pudo cargar product_catalog.json: {e}", exc_info=True)
            _PRODUCT_CATALOG_CACHE = [] # Devolver lista vacía en caso de error
    return _PRODUCT_CATALOG_CACHE

PRODUCT_CATALOG = cargar_catalogo_productos()
# --------------------------------------

# --- Carga de Ubicaciones de Comercios ---
_COMMERCE_LOCATIONS_CACHE = None

def cargar_ubicaciones_comercios():
    """Carga las ubicaciones de los comercios desde el JSON."""
    global _COMMERCE_LOCATIONS_CACHE
    if _COMMERCE_LOCATIONS_CACHE is None:
        try:
            loc_file_path = os.path.join(os.path.dirname(__file__), "..", "data", "commerce_locations.json")
            with open(loc_file_path, "r", encoding="utf-8") as f:
                _COMMERCE_LOCATIONS_CACHE = json.load(f)
            logger.info(f"✅ Ubicaciones de comercios cargadas desde {loc_file_path}")
        except Exception as e:
            logger.error(f"❌ No se pudo cargar commerce_locations.json: {e}", exc_info=True)
            _COMMERCE_LOCATIONS_CACHE = []
    return _COMMERCE_LOCATIONS_CACHE

COMMERCE_LOCATIONS = cargar_ubicaciones_comercios()
# -----------------------------------------

def enviar_notificacion_sms(numero_destino: str, mensaje: str):
    """
    Envía un SMS real usando Twilio (no WhatsApp). Requiere credenciales válidas.
    """
    if not all([TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_PHONE_NUMBER]):
        print("[NOTIFICACION SMS] Faltan credenciales de Twilio SMS.")
        return
    try:
        client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
        message = client.messages.create(
            body=mensaje, from_=TWILIO_PHONE_NUMBER, to=numero_destino
        )
        print(f"[NOTIFICACION SMS] SMS enviado SID: {message.sid}")
    except Exception as e:
        print(f"[NOTIFICACION SMS] Error al enviar SMS: {e}")


def enviar_notificacion_whatsapp_con_plantilla(
    numero_destino: str, nombre: str, nro_ticket: str, categoria: str
):
    if not all(
        [
            TWILIO_ACCOUNT_SID,
            TWILIO_AUTH_TOKEN,
            TWILIO_WHATSAPP_NUMBER,
            TWILIO_WHATSAPP_CONTENT_SID,
        ]
    ):
        logger.error("[NOTIFICACION WHATSAPP] Faltan credenciales de Twilio WhatsApp (SID/Token/Number/Content_SID).")
        return
    destinatario_whatsapp = f"whatsapp:{numero_destino}"
    try:
        client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
        variables_plantilla = {"1": nombre, "2": f"M-{nro_ticket}", "3": categoria}
        message = client.messages.create(
            from_=TWILIO_WHATSAPP_NUMBER,
            to=destinatario_whatsapp,
            content_sid=TWILIO_WHATSAPP_CONTENT_SID,
            content_variables=json.dumps(variables_plantilla),
        )
        logger.info(f"[NOTIFICACION WHATSAPP] Plantilla enviada, SID: {message.sid}")
    except Exception as e:
        logger.error(f"[NOTIFICACION WHATSAPP] Error al enviar plantilla: {e}", exc_info=True)


def es_pregunta_nueva(texto_usuario: str, tipo_esperado: str, categorias_validas=None) -> bool:
    """Determina si el usuario cambió de tema o respondió lo esperado.

    Se intenta primero con heurísticas sencillas para evitar falsos positivos
    cuando el usuario envía datos como números de ticket o teléfonos. Si las
    heurísticas no aplican, se consulta al modelo de lenguaje.
    """

    texto = texto_usuario.strip().lower()

    # Respuestas de cortesía o pedidos de agente se consideran cambio de tema
    if texto in {"ok", "gracias"}:
        return True
    if any(kw in texto for kw in {"agente", "asesor", "humano", "operador", "persona"}):
        return True

    if tipo_esperado == "una confirmación (sí o no)":
        if texto in {"si", "sí", "no"}:
            return False

    if tipo_esperado == "una calificación del 1 al 5":
        if re.fullmatch(r"[1-5]", texto):
            return False

    if tipo_esperado == "un número de ticket":
        if re.fullmatch(r"\d{5,}", texto):
            return False

    if tipo_esperado in {"el dato solicitado", "una dirección"}:
        if re.search(r"\d", texto):
            # Si contiene dígitos asumimos que puede ser una dirección o número
            return False

    prompt = f"""
    Analiza la RESPUESTA DEL USUARIO. El chatbot esperaba algo relacionado a: '{tipo_esperado}'.
    RESPUESTA DEL USUARIO: "{texto_usuario}"
    Si responde lo que esperabas, contestá 'RESPUESTA_VALIDA'.
    Si cambia de tema, contestá 'PREGUNTA_NUEVA'.
    """

    try:
        decision = get_cohere_response(
            message=prompt,
            preamble="Sos un clasificador. Solo respondé 'RESPUESTA_VALIDA' o 'PREGUNTA_NUEVA'.",
        )
        logger.info(f"[Guardián de Flujo] Decisión: {decision.strip()}")
        return "PREGUNTA_NUEVA" in decision
    except Exception as e:
        logger.error(f"[Guardián de Flujo] Error al clasificar pregunta nueva: {e}", exc_info=True)
        # En caso de error, asumir que no es una pregunta nueva para no romper el flujo
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
    def __init__(self, context):
        self.context = context
    # MODIFICADO: Ahora acepta un 'payload' completo como diccionario
    def handle(self, payload: dict) -> dict | None:
        raise NotImplementedError

    def build_detalles_memoria(self, memoria: dict) -> str:
        """Arma un pequeño resumen de los datos del reclamo."""
        partes = []
        if memoria.get("categoria_reclamo"):
            partes.append(f"Categoría: {memoria['categoria_reclamo']}")
        if memoria.get("direccion_reclamo"):
            partes.append(f"Dirección: {memoria['direccion_reclamo']}")
        if memoria.get("nombre_vecino"):
            partes.append(f"Nombre: {memoria['nombre_vecino']}")
        if memoria.get("telefono_vecino"):
            partes.append(f"Teléfono: {memoria['telefono_vecino']}")
        if memoria.get("email_vecino"):
            partes.append(f"Email: {memoria['email_vecino']}")
        if memoria.get("descripcion_reclamo"):
            partes.append(f"Descripción: {memoria['descripcion_reclamo']}")
        if memoria.get("ubicacion_gps"):
            lat = memoria['ubicacion_gps'].get('lat', 'N/A')
            lon = memoria['ubicacion_gps'].get('lon', 'N/A')
            partes.append(f"Ubicación GPS: Lat {lat}, Lon {lon}")
        if memoria.get("foto_url"):
            partes.append("Foto adjunta: Sí")
        return "\n".join(partes)


class GreetingHandler(BaseMunicipioHandler):
    # MODIFICADO: acepta payload
    def handle(self, payload: dict) -> dict | None:
        pregunta_str = payload.get("pregunta", "") # Extrae el string de la pregunta
        memoria = self.context.get("contexto_municipio", {})
        texto = normalizar_texto(pregunta_str.strip("!.,?"))
        saludos = [
            "hola",
            "buenos dias",
            "buenas tardes",
            "buenas noches",
            "hey",
            "que tal",
            "buenas",
        ]
        tokens = re.sub(r"[!.,?]", "", texto).split()
        set_saludo = {
            "hola",
            "buenos",
            "dias",
            "buenas",
            "tardes",
            "noches",
            "hey",
            "que",
            "tal",
        }

        # 1. Si es solo un saludo, responde amigable.
        if texto in saludos or (
            0 < len(tokens) <= 3 and all(t in set_saludo for t in tokens)
        ):
            memoria.clear()
            return {
                "respuesta": (
                    "¡Hola! 👋 Soy tu asistente digital del Municipio. Estoy aquí para ayudarte. "
                    "Podés consultarme sobre trámites, hacer un reclamo, dejar una sugerencia o resolver alguna duda que tengas. ¡Contame en qué te puedo colaborar hoy!"
                ),
                "botones": [ # Agregar botones de opciones comunes al saludo inicial
                    {"texto": "Hacer un reclamo"},
                    {"texto": "Dejar una sugerencia"},
                    {"texto": "Consultar un trámite"},
                    {"texto": "Estado de mi ticket"},
                ]
            }

        # 2. Si detecta saludo mezclado con consulta, deja que los otros handlers respondan pero mete saludo en la respuesta.
        for saludo in saludos:
            if (
                texto.startswith(saludo + " ")
                or texto.startswith(saludo + ",")
                or texto.startswith(saludo + ".")
            ):
                memoria["saludo_detectado"] = True
                break
        return None

class SugerenciasVecinoHandler(BaseMunicipioHandler):
    def handle(self, payload: dict) -> dict | None:
        pregunta_str = payload.get("pregunta", "").strip()
        memoria = self.context.get("contexto_municipio", {})
        estado_conversacion = memoria.get("estado_conversacion")
        intencion = self.context.get("intencion")
        sugerencia_texto_directo = ""

        if intencion == "hacer_sugerencia":
            # Intentar extraer el texto de la sugerencia si vino junto con la intención
            # (ej: "sugiero que pongan más bancos")
            # Simple heurística: si la pregunta es más larga que la palabra clave "sugerencia" + algo de margen.
            palabras_clave_sugerencia = ["sugerencia", "sugerencias", "idea", "propuesta", "proponer", "mejorar"]
            texto_limpio_de_keywords = pregunta_str
            for kw in palabras_clave_sugerencia:
                if texto_limpio_de_keywords.lower().startswith(kw):
                    texto_limpio_de_keywords = texto_limpio_de_keywords[len(kw):].strip()
            
            if texto_limpio_de_keywords and len(texto_limpio_de_keywords) > 5: # Tiene que haber algo de sustancia
                sugerencia_texto_directo = texto_limpio_de_keywords
            
            if not sugerencia_texto_directo and estado_conversacion != ConversationState.ESPERANDO_TEXTO_SUGERENCIA:
                memoria["estado_conversacion"] = ConversationState.ESPERANDO_TEXTO_SUGERENCIA
                return {
                    "respuesta": "¡Genial! Nos interesa mucho tu opinión. Por favor, contanos tu sugerencia o idea para mejorar:",
                    "botones": [{"texto": "Cancelar sugerencia"}]
                }

        if estado_conversacion == ConversationState.ESPERANDO_TEXTO_SUGERENCIA or sugerencia_texto_directo:
            sugerencia_final = sugerencia_texto_directo if sugerencia_texto_directo else pregunta_str
            
            if not sugerencia_final or len(sugerencia_final) < 5: # Validación mínima
                return {
                    "respuesta": "Por favor, ingresá el texto de tu sugerencia. Tiene que ser un poco más descriptiva para que podamos entenderla bien.",
                    "botones": [{"texto": "Cancelar sugerencia"}]
                }
            
            try:
                ticket_data = {
                    "asunto": "Nueva Sugerencia/Mejora del Vecino",
                    "categoria": "Sugerencia",
                    "detalles": sugerencia_final,
                    "pregunta": sugerencia_final, 
                    "estado": "nueva_sugerencia",
                    "user_id": self.context.get("cliente_id"),
                    "anon_id": self.context.get("anon_id") if not self.context.get("cliente_id") else None,
                    "municipio_id": getattr(self.context.get("user_obj"), "municipio_id", None)
                }
                
                tipo_ticket_para_sugerencia = "municipio" # Default
                # Podría ajustarse según el 'rubro_obj' si este handler se vuelve más genérico
                # if hasattr(self.context.get("rubro_obj"), "id"):
                # tipo_ticket_para_sugerencia = "pyme" 

                ticket = servicio_tickets.crear_nuevo_ticket(
                    tipo_ticket=tipo_ticket_para_sugerencia, 
                    ticket_data=ticket_data
                )

                if ticket:
                    nro_ticket_str = f"M-{ticket.nro_ticket}" if tipo_ticket_para_sugerencia == "municipio" else str(ticket.nro_ticket)
                    logger.info(f"Sugerencia registrada como ticket {nro_ticket_str}.")
                    memoria.clear() 
                    return {
                        "respuesta": (
                            "¡Muchas gracias por tu sugerencia! La hemos registrado y será revisada por nuestro equipo. "
                            f"Tu número de registro es {nro_ticket_str}. Valoramos mucho tu aporte."
                        ),
                        "botones": [
                            {"texto": "Hacer otra consulta"},
                            {"texto": "Volver al inicio"}
                        ]
                    }
                else:
                    raise Exception("La creación del ticket de sugerencia retornó None.")

            except Exception as e:
                logger.error(f"[SugerenciasVecinoHandler] Error al guardar sugerencia como ticket: {e}", exc_info=True)
                return {
                    "respuesta": "Hubo un problema al registrar tu sugerencia. Por favor, intentá de nuevo en un momento."
                }
        return None

class CancelHandler(BaseMunicipioHandler):
    """Permite cancelar el flujo actual si el usuario lo solicita."""

    CANCEL_KEYWORDS = [
        "cancelar",
        "olvidalo",
        "deja",
        "no importa",
        "volver",
        "salir",
        "cancelar reclamo",
        "no quiero continuar",
        "parar",
        "detener",
    ]

    # MODIFICADO: acepta payload
    def handle(self, payload: dict) -> dict | None:
        pregunta_str = payload.get("pregunta", "") # Extrae el string de la pregunta
        texto = normalizar_texto(pregunta_str)
        if any(kw in texto for kw in self.CANCEL_KEYWORDS) or payload.get("action") == "cancelar": # Si la acción es cancelar
            self.context.get("contexto_municipio", {}).clear()
            return {
                "respuesta": "Operación cancelada. ¿Necesitás ayuda con otro trámite o reclamo?",
                "botones": [
                    {"texto": "Hacer un reclamo"},
                    {"texto": "Consultar estado de ticket"},
                    {"texto": "Hablar con un agente"},
                ],
            }
        return None


class PoliteHandler(BaseMunicipioHandler):
    """Responde brevemente ante agradecimientos u otras expresiones corteses."""

    KEYWORDS = {"gracias", "ok", "ok gracias", "muchas gracias", "dale", "perfecto", "genial"}

    # MODIFICADO: acepta payload
    def handle(self, payload: dict) -> dict | None:
        pregunta_str = payload.get("pregunta", "") # Extrae el string de la pregunta
        texto = normalizar_texto(pregunta_str)
        if texto in self.KEYWORDS:
            # No necesariamente limpiar toda la memoria, si está en medio de un flujo.
            # Solo si es un agradecimiento simple fuera de un flujo directo.
            if not self.context.get("contexto_municipio", {}).get("estado_conversacion"):
                self.context.get("contexto_municipio", {}).clear()
            return {
                "respuesta": "¡De nada! ¿Necesitás ayuda con algo más?",
                "botones": [
                    {"texto": "Hacer un reclamo"},
                    {"texto": "Consultar estado de un trámite"},
                    {"texto": "Hablar con un agente"},
                ],
            }
        return None


class SmallTalkHandler(BaseMunicipioHandler):
    """Responde cordialmente a consultas de small talk usando un LLM."""

    # MODIFICADO: acepta payload
    def handle(self, payload: dict) -> dict | None:
        pregunta_str = payload.get("pregunta", "") # Extrae el string de la pregunta
        if detectar_small_talk_con_llm(pregunta_str):
            respuesta = generar_respuesta_small_talk(pregunta_str)
            return {
                "respuesta": respuesta,
                "fuente": "smalltalk_municipio_llm",
            }
        return None


class RecoleccionHandler(BaseMunicipioHandler):
    """Atiende consultas sobre recolección de residuos en cualquier momento."""

    KEYWORDS = ["basura", "recoleccion", "residuos", "basurero"]

    # MODIFICADO: acepta payload
    def handle(self, payload: dict) -> dict | None:
        pregunta_str = payload.get("pregunta", "") # Extrae el string de la pregunta
        memoria = self.context.get("contexto_municipio", {})
        estado = memoria.get("estado_conversacion")
        texto = normalizar_texto(pregunta_str)

        # Si estamos esperando una dirección para recolección
        if estado == ConversationState.ESPERANDO_PARAM_RECOLECCION:
            if es_pregunta_nueva(pregunta_str, "una dirección"):
                memoria.clear()
                return None
            
            resultado = consultar_recoleccion_por_direccion(direccion=pregunta_str)
            memoria.clear() # Limpiar memoria después de completar la consulta
            if not resultado or "No" in resultado:
                return {
                    "respuesta": "No encontré información de recolección para esa dirección. Podés verificar en la web municipal o intentar con otra dirección.",
                    "botones": [
                        {"texto": "Consultar otra dirección"},
                        {"texto": "Hablar con un agente"},
                    ],
                }
            return {
                "respuesta": f"{resultado}\n¿Consultás otra dirección o hacés otro trámite?",
                "botones": [
                    {"texto": "Consultar otra dirección"},
                    {"texto": "Hacer un reclamo"},
                ],
            }

        # Si el usuario pregunta por recolección y no hay un flujo activo
        if any(kw in texto for kw in self.KEYWORDS) and not estado:
            # Si la pregunta ya contiene una dirección válida
            if direccion_es_valida(pregunta_str):
                resultado = consultar_recoleccion_por_direccion(direccion=pregunta_str)
                if not resultado or "No" in resultado:
                    return {
                        "respuesta": "No encontré información de recolección para esa dirección. Revisá si está bien escrita, o consultá al municipio.",
                        "botones": [
                            {"texto": "Reintentar"},
                            {"texto": "Hablar con un agente"},
                        ],
                    }
                return {
                    "respuesta": f"{resultado}\n¿Consultás otra dirección o hacés otro trámite?",
                    "botones": [
                        {"texto": "Consultar otra dirección"},
                        {"texto": "Hacer un reclamo"},
                    ],
                }
            else:
                # Si no tiene dirección, la pide
                memoria["estado_conversacion"] = ConversationState.ESPERANDO_PARAM_RECOLECCION
                return {
                    "respuesta": f"¿La dirección para consultar el horario de recolección?\nPor ejemplo: {EJEMPLO_DIRECCION}",
                }
        return None


class IntentClassifierHandler(BaseMunicipioHandler):
    KEYWORDS_AGENTE = [
        "agente", "humano", "persona", "representante", "operador", "empleado", "atención",
        "real", "chat real", "soporte", "ayuda humana", "hablar con alguien", "asesor",
        "consultor", "soporte técnico", "atender", "personal", "comunicarme",
        "llamar", "contacto", "quiero hablar", "hablame con"
    ]
    KEYWORDS_RECLAMO = [
        "reclamo", "reclamos", "queja", "quejas", "problema", "problemas",
        "denuncia", "denuncias", "reportar", "arbol caido", "árbol caído", "arbol", "caido"
    ]
    KEYWORDS_TRAMITE = [
        "trámite", "tramite", "trámites", "tramites", "gestión", "gestiones",
        "consulta de trámite", "turno", "certificado", "licencia", "renovar", "sacar"
    ]
    KEYWORDS_TICKET_STATUS = ["ticket", "estado", "seguimiento", "número de ticket"]
    KEYWORDS_SUGERENCIA = ["sugerencia", "sugerencias", "idea", "propuesta", "proponer", "mejorar"]

    # Keywords para ventas y comercio
    KEYWORDS_INICIAR_COMPRA = ["comprar", "compra", "pedido", "productos", "catálogo", "catalogo", "tienda", "venden"]
    KEYWORDS_VER_CARRITO = ["carrito", "bolsa", "mi compra", "mi pedido"]
    KEYWORDS_PAGAR = ["pagar", "checkout", "finalizar compra", "cobrar"]
    KEYWORDS_UBICACION_TIENDA = ["ubicación", "dirección", "local", "tienda física", "sucursal", "mapa"]
    KEYWORDS_RECLAMO_PEDIDO = ["pedido mal", "problema compra", "producto roto", "pedido incorrecto"]
    
    # Keywords para Pánico
    KEYWORDS_PANICO = ["ayuda urgente", "emergencia", "sos", "necesito ayuda inmediata", "panico", "pánico", "boton de panico", "botón de pánico", "peligro"]


    # MODIFICADO: acepta payload
    def handle(self, payload: dict) -> dict | None:
        pregunta_str = payload.get("pregunta", "") # Extrae el string de la pregunta
        logger.info(f"[INTENT] Analizando intención para: {pregunta_str}")
        memoria = self.context.get("contexto_municipio", {}) # Usaremos el mismo contexto por ahora
        texto_normalizado = normalizar_texto(pregunta_str)
        # tokens = texto_normalizado.split() # No se usa tokens directamente, se usa 'in'

        # Si ya hay un estado de conversación activo, no re-clasificamos la intención principal con keywords.
        if memoria.get("estado_conversacion"):
            # Excepción: si el usuario explícitamente quiere hablar con un agente, eso tiene prioridad.
            if any(kw in texto_normalizado for kw in self.KEYWORDS_AGENTE):
                self.context["intencion"] = "hablar_con_agente"
                memoria.clear() 
                logger.info(f"[MUNICIPIO] Intención: hablar_con_agente (por keyword, interrumpe flujo)")
                return None
            
            # Excepción: si el usuario explícitamente activa pánico, eso tiene máxima prioridad.
            if any(kw in texto_normalizado for kw in self.KEYWORDS_PANICO):
                self.context["intencion"] = "activar_panico"
                memoria.clear() # Limpiar cualquier estado previo, pánico es absoluto
                logger.info(f"[MUNICIPIO] Intención: activar_panico (por keyword, interrumpe flujo)")
                return None

            self.context["intencion"] = "continuar_flujo"
            logger.info(f"[MUNICIPIO] Intención: continuar_flujo (estado activo: {memoria.get('estado_conversacion')})")
            return None

        # Priorizar PÁNICO
        for kw in self.KEYWORDS_PANICO:
            if kw in texto_normalizado:
                self.context["intencion"] = "activar_panico"
                memoria.clear()
                logger.info(f"[MUNICIPIO] Intención: activar_panico (por keyword '{kw}')")
                return None

        # Luego, "hablar con agente"
        for kw in self.KEYWORDS_AGENTE:
            if kw in texto_normalizado:
                self.context["intencion"] = "hablar_con_agente"
                memoria.clear() 
                logger.info(f"[MUNICIPIO] Intención: hablar_con_agente (por keyword '{kw}')")
                return None

        # Nuevas intenciones de comercio
        for kw in self.KEYWORDS_INICIAR_COMPRA:
            if kw in texto_normalizado:
                self.context["intencion"] = "iniciar_compra"
                logger.info(f"[COMERCIO] Intención: iniciar_compra (por keyword '{kw}')")
                return None
        
        for kw in self.KEYWORDS_VER_CARRITO:
            if kw in texto_normalizado:
                self.context["intencion"] = "ver_carrito"
                logger.info(f"[COMERCIO] Intención: ver_carrito (por keyword '{kw}')")
                return None

        for kw in self.KEYWORDS_PAGAR:
            if kw in texto_normalizado:
                self.context["intencion"] = "proceder_al_pago"
                logger.info(f"[COMERCIO] Intención: proceder_al_pago (por keyword '{kw}')")
                return None

        for kw in self.KEYWORDS_UBICACION_TIENDA:
            if kw in texto_normalizado:
                self.context["intencion"] = "solicitar_ubicacion_tienda"
                logger.info(f"[COMERCIO] Intención: solicitar_ubicacion_tienda (por keyword '{kw}')")
                return None
        
        for kw in self.KEYWORDS_RECLAMO_PEDIDO:
            if kw in texto_normalizado:
                self.context["intencion"] = "reclamo_pedido"
                logger.info(f"[COMERCIO] Intención: reclamo_pedido (por keyword '{kw}')")
                return None
        
        # (La intención "consultar_producto" y "agregar_al_carrito" son más difíciles de capturar solo con keywords
        # ya que dependen mucho del contexto o de entidades. Se manejarán mejor con LLM o lógica de estado)

        # Intenciones existentes de municipio
        for kw in self.KEYWORDS_TICKET_STATUS:
            if kw in texto_normalizado:
                self.context["intencion"] = "consultar_estado_ticket"
                logger.info(f"[MUNICIPIO] Intención: consultar_estado_ticket (por keyword '{kw}')")
                return None

        for kw in self.KEYWORDS_RECLAMO: # Reclamo general municipal
            if kw in texto_normalizado:
                self.context["intencion"] = "iniciar_reclamo"
                logger.info(f"[MUNICIPIO] Intención: iniciar_reclamo (por palabra clave '{kw}')")
                return None

        for kw in self.KEYWORDS_TRAMITE:
            if kw in texto_normalizado:
                self.context["intencion"] = "consultar_tramite"
                logger.info(f"[MUNICIPIO] Intención: consultar_tramite (por palabra clave '{kw}')")
                return None

        for kw in self.KEYWORDS_SUGERENCIA:
            if kw in texto_normalizado:
                self.context["intencion"] = "hacer_sugerencia"
                logger.info(f"[MUNICIPIO] Intención: hacer_sugerencia (por palabra clave '{kw}')")
                return None

        # Finalmente, clasificación con LLM si no hubo match con keywords y no hay estado activo
        # Aquí se podría pasar un contexto de "rubro" al LLM si el bot maneja múltiples rubros (ej: municipio vs comercio)
        # Por ahora, se asume que _clasificar_intencion_con_llm puede manejarlo o se adaptará.
        intencion_llm = _clasificar_intencion_con_llm(pregunta_str) 
        self.context["intencion"] = intencion_llm

        logger.info(f"[MUNICIPIO] Intención (final): {self.context.get('intencion')}")
        return None # Siempre devuelve None, lo cual es correcto para este handler


class TicketStatusHandler(BaseMunicipioHandler):
    # MODIFICADO: acepta payload
    def handle(self, payload: dict) -> dict | None:
        pregunta_str = payload.get("pregunta", "") # Extrae el string de la pregunta
        memoria = self.context.get("contexto_municipio", {})
        estado_conversacion = memoria.get("estado_conversacion")

        # No interferir si estamos en un flujo de reclamo activo (ESPERANDO_..._RECLAMO)
        if estado_conversacion and estado_conversacion in RECLAMO_STATES:
            return None

        # Flujo de confirmación de cierre
        if estado_conversacion == ConversationState.ESPERANDO_CONFIRMACION_CIERRE:
            if es_pregunta_nueva(pregunta_str, "una confirmación (sí o no)"):
                memoria.clear()
                return None
            
            ticket_id = memoria.get("ticket_id_activo")
            ticket = db.session.get(MunicipioTicket, ticket_id)
            if not ticket: # Si el ticket no se encuentra, limpiar y salir.
                memoria.clear()
                return {"respuesta": "No pude encontrar el ticket activo. ¿Necesitás ayuda con algo más?"}

            if "si" in normalizar_texto(pregunta_str):
                ticket.estado = "resuelto"
                db.session.commit()
                memoria["estado_conversacion"] = ConversationState.ESPERANDO_CALIFICACION
                return {
                    "respuesta": "¡Excelente! ¿Podés calificar la atención recibida del 1 al 5?"
                }
            else:
                memoria.clear()
                return {
                    "respuesta": "Dejamos el ticket abierto para seguimiento del equipo. ¿Necesitás algo más?"
                }
        
        # Flujo de calificación
        elif estado_conversacion == ConversationState.ESPERANDO_CALIFICACION:
            if es_pregunta_nueva(pregunta_str, "una calificación del 1 al 5"):
                memoria.clear()
                return None
            
            calificacion_match = re.fullmatch(r"[1-5]", pregunta_str.strip())
            if not calificacion_match:
                return {"respuesta": "Por favor, ingresa una calificación del 1 al 5."}

            ticket_id = memoria.get("ticket_id_activo")
            if ticket_id:
                servicio_tickets.crear_comentario(
                    ticket_id=ticket_id,
                    tipo_ticket="municipio",
                    comentario_data={
                        "comentario": f"Calificación: {pregunta_str}",
                        "es_admin": False,
                        "anon_id": self.context.get("anon_id"),
                    },
                )
            memoria.clear()
            return {
                "respuesta": "¡Gracias por tu calificación! ¿Te ayudo con otro trámite o reclamo?",
                "botones": [
                    {"texto": "Nuevo reclamo"},
                    {"texto": "Consultar otro ticket"},
                    {"texto": "Hablar con un agente"},
                ],
            }
        
        # Flujo de espera de número de ticket
        elif estado_conversacion == ConversationState.ESPERANDO_NUMERO_TICKET:
            if es_pregunta_nueva(pregunta_str, "un número de ticket"):
                memoria.clear()
                return None
            
            match = re.search(r"\d{5,}", pregunta_str)
            if not match:
                return {
                    "respuesta": "No entendí el número de ticket. ¿Podés repetirlo? Debe ser un número de al menos 5 dígitos."
                }
            
            numero = int(match.group(0))
            ticket = MunicipioTicket.query.filter_by(nro_ticket=numero).first()
            memoria.pop("estado_conversacion", None) # Limpiar estado después de obtener el número

            if not ticket:
                return {"respuesta": f"No encontré ticket M-{match.group(0)}. Por favor, verificá el número."}
            
            respuesta = f"El ticket **M-{ticket.nro_ticket}** sobre '{ticket.asunto}' está en estado: **{ticket.estado.replace('_', ' ').title()}**."
            
            ultimo_comentario = (
                TicketComentario.query.filter_by(
                    municipio_ticket_id=ticket.id, es_admin=True
                )
                .order_by(TicketComentario.fecha.desc())
                .first()
            )
            if ultimo_comentario:
                respuesta += f"\nÚltima actualización: *{ultimo_comentario.comentario}*"
            
            if ticket.estado == "en_proceso": # O el estado que uses para indicar que puede ser resuelto
                memoria["estado_conversacion"] = ConversationState.ESPERANDO_CONFIRMACION_CIERRE
                memoria["ticket_id_activo"] = ticket.id
                respuesta += "\n¿Se resolvió tu problema?"
                return {
                    "respuesta": respuesta,
                    "botones": [
                        {"texto": "Sí, solucionado"},
                        {"texto": "No, aún no"},
                    ],
                }
            return {"respuesta": respuesta}
        
        # Inicio de consulta de ticket por intención clasificada
        if self.context.get("intencion") == "consultar_estado_ticket":
            match = re.search(r"\d{5,}", pregunta_str)
            if not match:
                memoria["estado_conversacion"] = ConversationState.ESPERANDO_NUMERO_TICKET
                return {"respuesta": "Para consultar el estado de un ticket, decime el número de ticket por favor."}
            
            numero = int(match.group(0))
            ticket = MunicipioTicket.query.filter_by(nro_ticket=numero).first()

            if not ticket:
                return {"respuesta": f"No encontré ticket M-{match.group(0)}. Por favor, verificá el número."}
            
            respuesta = f"El ticket **M-{ticket.nro_ticket}** sobre '{ticket.asunto}' está en estado: **{ticket.estado.replace('_', ' ').title()}**."
            
            ultimo_comentario = (
                TicketComentario.query.filter_by(
                    municipio_ticket_id=ticket.id, es_admin=True
                )
                .order_by(TicketComentario.fecha.desc())
                .first()
            )
            if ultimo_comentario:
                respuesta += f"\nÚltima actualización: *{ultimo_comentario.comentario}*"
            
            if ticket.estado == "en_proceso":
                memoria["estado_conversacion"] = ConversationState.ESPERANDO_CONFIRMACION_CIERRE
                memoria["ticket_id_activo"] = ticket.id
                respuesta += "\n¿Se resolvió tu problema?"
                return {
                    "respuesta": respuesta,
                    "botones": [
                        {"texto": "Sí, solucionado"},
                        {"texto": "No, aún no"},
                    ],
                }
            return {"respuesta": respuesta}
        
        return None


class ReclamoInteligenteMunicipioHandler(BaseMunicipioHandler):
    """
    Handler de reclamos municipales inteligente:
    Interpreta mensajes completos usando LLM, extrae datos clave,
    pide solo lo que falta y genera el ticket de una.
    """
    CAMPOS_RECLAMO = ["categoria", "direccion", "nombre", "telefono", "email", "descripcion"]

    # MODIFICADO: acepta payload
    def handle(self, payload: dict) -> dict | None:
        pregunta_str = payload.get("pregunta", "") # Extrae el string de la pregunta
        memoria = self.context.get("contexto_municipio", {})

        # Solo si NO hay un flujo de reclamo ya iniciado (estado_conversacion de reclamo)
        # y la intención es iniciar un reclamo. Esto evita que se active en medio de un reclamo paso a paso.
        # IMPORTANTE: ReclamoInteligenteMunicipioHandler NO debe tener un 'estado' que lo haga "dueño" del flujo
        # si ya se inició un reclamo paso a paso por ReclamoHandler. Si su handle responde, lo pasa.
        if memoria.get("estado_conversacion") and memoria["estado_conversacion"] in RECLAMO_STATES:
            return None # Si ya estamos en un estado específico de reclamo, no intentar el inteligente de nuevo
        
        if self.context.get("intencion") != "iniciar_reclamo":
            return None

        # Si no hay datos iniciales de reclamo en memoria, intentar la extracción inteligente.
        # Esto previene que se re-ejecute el LLM en cada paso del reclamo.
        if not memoria.get("categoria_reclamo") and not memoria.get("direccion_reclamo"):
            prompt = f"""
            Extraé del siguiente mensaje los siguientes datos si están presentes, si algún campo no está presente, simplemente omitilo:
            - categoria (motivo del reclamo, ej: basura, agua, semáforo, etc. Usá las categorías: {", ".join(CATEGORIAS_RECLAMO)})
            - direccion (ej: Av. San Martín 123)
            - nombre (nombre y apellido del reclamante)
            - telefono (número de teléfono con código de área)
            - email (dirección de correo electrónico)
            - descripcion (detalle del problema)

            Mensaje: "{pregunta_str}"

            Devolvé solo JSON con esos campos. Ejemplo:
            {{
              "categoria": "luminaria",
              "direccion": "Av. San Martín 500",
              "nombre": "Luis Pérez",
              "telefono": "2613334444",
              "email": "luis@gmail.com",
              "descripcion": "La luz del poste está apagada hace días."
            }}
            """
            try:
                resp = get_cohere_response(message=prompt, preamble="Extraé los campos y devolvé solo JSON.")
                datos = json.loads(resp) if resp else {}
                logger.info(f"[ReclamoInteligenteHandler] Datos extraídos por LLM: {datos}")
            except Exception as e:
                logger.error(f"[ReclamoInteligenteMunicipioHandler] Error Cohere/JSON: {e}", exc_info=True)
                datos = {}
            
            # Limpiar memoria al iniciar un reclamo inteligente para asegurar que empezamos de cero
            memoria.clear() 
            for campo in self.CAMPOS_RECLAMO:
                if datos.get(campo):
                    # Validar y asignar campos. Para categoría, intentar un fuzzy match.
                    if campo == "categoria":
                        matched_category = next((c for c in CATEGORIAS_RECLAMO if normalizar_texto(c) == normalizar_texto(datos[campo])), None)
                        if matched_category:
                            memoria["categoria_reclamo"] = matched_category
                        else: # Si no matchea, se considerará faltante
                            logger.warning(f"Categoría '{datos[campo]}' no válida. Se pedirá.")
                            pass
                    elif campo == "direccion":
                        if direccion_es_valida(datos[campo]):
                            memoria["direccion_reclamo"] = datos[campo].strip()
                        else:
                            logger.warning(f"Dirección '{datos[campo]}' no válida. Se pedirá.")
                            pass # No se asigna si no es válida, se pedirá más adelante
                    elif campo == "telefono":
                        if validar_telefono(datos[campo]):
                            memoria["telefono_vecino"] = datos[campo].strip()
                        else:
                            logger.warning(f"Teléfono '{datos[campo]}' no válido. Se pedirá.")
                            pass
                    elif campo == "email":
                        if validar_email(datos[campo]):
                            memoria["email_vecino"] = datos[campo].strip()
                        else:
                            logger.warning(f"Email '{datos[campo]}' no válido. Se pedirá.")
                            pass
                    else:
                        # Asegurarse de que el campo se guarda con el nombre correcto en memoria
                        if campo == "nombre":
                            memoria["nombre_vecino"] = datos[campo].strip()
                        elif campo == "descripcion":
                            memoria["descripcion_reclamo"] = datos[campo].strip()
                        else:
                             memoria[campo] = datos[campo].strip() # Para otros campos si los hubiera
            
            # Si al menos se obtuvo una categoría o descripción inicial, o cualquier dato de reclamo,
            # forzamos el inicio del flujo paso a paso si no se pudo completar de una.
            # Esto evita que ReclamoInteligenteIntente cada vez.
            if any(memoria.get(f"{c}_reclamo") or memoria.get(f"{c}_vecino") for c in self.CAMPOS_RECLAMO):
                # Si tenemos todos los datos, procedemos a la confirmación final para crear el ticket
                if all(memoria.get(f"{c}_reclamo" if c not in ["nombre", "telefono", "email"] else f"{c}_vecino") for c in self.CAMPOS_RECLAMO):
                    memoria["estado_conversacion"] = ConversationState.ESPERANDO_CONFIRMACION_RECLAMO
                    resumen = self.build_detalles_memoria(memoria)
                    return {
                        "respuesta": f"Parece que tenemos todos los datos. ¿Confirmás el reclamo con estos datos?\n{resumen}",
                        "botones": [
                            {"texto": "Confirmar reclamo", "action": "confirmar_reclamo"},
                            {"texto": "Editar datos", "action": "editar_reclamo"}
                        ]
                    }
                else:
                    # Si faltan datos, transferir el control al ReclamoHandler paso a paso
                    # Se iniciará en ESPERANDO_CATEGORIA_RECLAMO y el ReclamoHandler se encargará.
                    memoria["estado_conversacion"] = ConversationState.ESPERANDO_CATEGORIA_RECLAMO
                    # No devolver una respuesta aquí, dejar que el ReclamoHandler se ejecute inmediatamente después.
                    return None 

        return None # Este handler solo responde si completó o inició el reclamo.


class ReclamoHandler(BaseMunicipioHandler):
    """
    Maneja el flujo paso a paso del reclamo, aceptando tanto botones como texto libre,
    y utilizando Cohere/LLM para interpretar intención cuando no hay coincidencia clara.
    """
    EDIT_KEYWORDS = [
        "editar", "cambiar", "corregir", "modificar", 
        "no era asi", "me equivoque", "error"
    ] # Definido a nivel de clase

    def handle(self, payload: dict) -> dict | None:
        pregunta_str = payload.get("pregunta", "") or ""
        memoria = self.context.get("contexto_municipio", {})
        estado = memoria.get("estado_conversacion")
        intencion = self.context.get("intencion")

        # Si es inicio explícito de reclamo
        if intencion == "iniciar_reclamo" and not estado:
            memoria.clear()
            memoria["estado_conversacion"] = ConversationState.ESPERANDO_CATEGORIA_RECLAMO
            sugeridas = sugerir_categorias_relevantes(pregunta_str)
            botones = [{"texto": c.title()} for c in (sugeridas if sugeridas else CATEGORIAS_RECLAMO)]
            texto_respuesta = "Elegí la categoría del reclamo" if sugeridas else "¿Sobre qué categoría es tu reclamo?"
            return {
                "respuesta": texto_respuesta,
                "botones": botones
            }

        if estado not in RECLAMO_STATES:
            return None

        # Paso 1: Categoría
        if estado == ConversationState.ESPERANDO_CATEGORIA_RECLAMO:
            texto_normalizado = normalizar_texto(pregunta_str)
            categoria_final = None
            if texto_normalizado in categorias_normalizadas:
                idx = categorias_normalizadas.index(texto_normalizado)
                categoria_final = CATEGORIAS_RECLAMO[idx]
            else:
                from difflib import get_close_matches
                matches = get_close_matches(texto_normalizado, categorias_normalizadas, n=1, cutoff=0.7)
                if matches:
                    idx = categorias_normalizadas.index(matches[0])
                    categoria_final = CATEGORIAS_RECLAMO[idx]

            if not categoria_final:
                # Intenta entender la categoría con LLM si no la encuentra
                try:
                    respuesta_llm = _clasificar_intencion_con_llm(
                        pregunta_str, opciones=CATEGORIAS_RECLAMO, tipo="categoría"
                    )
                    if respuesta_llm:
                        categoria_final = respuesta_llm
                except Exception:
                    categoria_final = None

            if categoria_final:
                memoria["categoria_reclamo"] = categoria_final
                memoria["estado_conversacion"] = ConversationState.ESPERANDO_DIRECCION_RECLAMO
                return {
                    "respuesta": (
                        f"Perfecto, categoría: **{categoria_final.title()}**. ¿La **dirección exacta**?\n{EJEMPLO_DIRECCION}"
                    )
                }
            else:
                sugeridas = sugerir_categorias_relevantes(pregunta_str)
                botones = [{"texto": c.title()} for c in (sugeridas if sugeridas else CATEGORIAS_RECLAMO)]
                respuesta_texto = (
                    "¡Ups! No encontré esa categoría, pero estas opciones podrían ayudarte. "
                    "Si no te sirve ninguna, contame un poco más y lo intento de nuevo."
                )
                if sugeridas:
                    respuesta_texto += "\nOpciones sugeridas:"
                return {
                    "respuesta": respuesta_texto,
                    "botones": botones
                }

        # Paso 2: Dirección
        if estado == ConversationState.ESPERANDO_DIRECCION_RECLAMO:
            if payload.get("es_foto") or payload.get("es_ubicacion"):
                return {
                    "respuesta": "Entendido. Para poder asociar tu foto o ubicación, primero necesito la dirección escrita del problema (por ejemplo, 'Av. San Martín 123, Junín'). Una vez que la tenga, podrás adjuntar los archivos. ¿Me decís la dirección, por favor?",
                }
            if not direccion_es_valida(pregunta_str):
                return {
                    "respuesta": (
                        f"La dirección que ingresaste no parece completa o válida. ¿Podrías verificarla e ingresarla nuevamente? "
                        f"Necesito algo como: '{EJEMPLO_DIRECCION}'. ¡Gracias!"
                    )
                }
            memoria["direccion_reclamo"] = pregunta_str.strip()
            memoria["estado_conversacion"] = ConversationState.ESPERANDO_NOMBRE_VECINO
            return {"respuesta": "¡Perfecto! Ya tengo la dirección. Ahora, ¿podrías decirme tu **nombre completo** para registrar el reclamo?"}

        # Paso 3: Nombre
        if estado == ConversationState.ESPERANDO_NOMBRE_VECINO:
            nombre = pregunta_str.strip()
            if not nombre or len(nombre.split()) < 2: # Simple check for at least two words
                return {"respuesta": "Para continuar, necesitaría tu nombre y apellido. ¿Podrías ingresarlos, por favor?"}
            memoria["nombre_vecino"] = nombre
            memoria["estado_conversacion"] = ConversationState.ESPERANDO_TELEFONO_VECINO
            return {
                "respuesta": f"¡Gracias, {nombre}! Ahora, si fueras tan amable, ¿me podrías pasar tu **número de teléfono con código de área**? Así podremos contactarte si es necesario."
            }

        # Paso 4: Teléfono
        if estado == ConversationState.ESPERANDO_TELEFONO_VECINO:
            telefono = pregunta_str.strip()
            if not validar_telefono(telefono):
                return {"respuesta": "El número de teléfono que ingresaste no parece válido. ¿Podrías revisarlo e ingresarlo de nuevo, solo números incluyendo el código de área? Por ejemplo: 2615551234."}
            memoria["telefono_vecino"] = telefono
            memoria["estado_conversacion"] = ConversationState.ESPERANDO_EMAIL_VECINO
            return {"respuesta": "¡Excelente! Ya casi terminamos. ¿Cuál es tu **dirección de correo electrónico**? Te enviaremos las novedades del reclamo por ahí."}

        # Paso 5: Email
        if estado == ConversationState.ESPERANDO_EMAIL_VECINO:
            email = pregunta_str.strip()
            if not validar_email(email):
                return {"respuesta": "El email que ingresaste no parece tener el formato correcto. ¿Podrías revisarlo? Por ejemplo, debería ser algo como 'nombre@ejemplo.com'."}
            memoria["email_vecino"] = email
            memoria["estado_conversacion"] = ConversationState.ESPERANDO_DESCRIPCION_RECLAMO
            return {"respuesta": "¡Bárbaro! Ahora, por favor, contame con un poco más de detalle **cuál es el problema**. Si querés, después de esto podrás adjuntar una foto o compartir tu ubicación GPS."}

        # Paso 6: Descripción
        if estado == ConversationState.ESPERANDO_DESCRIPCION_RECLAMO:
            descripcion = pregunta_str.strip()
            if not descripcion or len(descripcion) < 10: # Simple check for some detail
                return {"respuesta": "Para entender mejor la situación, necesitaría una breve descripción del problema. ¿Podrías contarme un poco más?"}
            memoria["descripcion_reclamo"] = descripcion
            memoria["estado_conversacion"] = ConversationState.ESPERANDO_ADJUNTOS_RECLAMO
            return {
                "respuesta": "¡Gracias por la descripción! ¿Querés **adjuntar una foto del problema o compartir tu ubicación GPS** para que tengamos más detalles? Esto es opcional.",
                "botones": [
                    {"texto": "Adjuntar foto", "action": "adjuntar_foto"},
                    {"texto": "Compartir ubicación", "action": "compartir_ubicacion"},
                    {"texto": "No, continuar", "action": "sin_adjuntos"},
                    {"texto": "Completar reclamo", "action": "sin_adjuntos"}
                ]
            }

        # Paso 7: Adjuntos (acepta acción por botón o texto)
        if estado == ConversationState.ESPERANDO_ADJUNTOS_RECLAMO:
            accion = payload.get("action", "").lower() or normalizar_texto(pregunta_str)

            SIN_ADJUNTOS_KEYWORDS = [
                "sin_adjuntos", "no, continuar", "no", "no gracias", "no, gracias",
                "completar", "completar reclamo", "completar el reclamo",
                "terminar", "terminar reclamo", "quiero completar", "quiero terminar",
            ]

            if any(kw in accion for kw in SIN_ADJUNTOS_KEYWORDS):
                memoria["estado_conversacion"] = ConversationState.ESPERANDO_CONFIRMACION_RECLAMO
                resumen = self.build_detalles_memoria(memoria)
                return {
                    "respuesta": f"Perfecto, continuamos sin adjuntos. Por favor, revisá si todos los datos son correctos antes de confirmar:\n\n{resumen}\n\n¿Está todo bien para generar el reclamo?",
                    "botones": [
                        {"texto": "Sí, confirmar reclamo", "action": "confirmar_reclamo"},
                        {"texto": "No, quiero editar algo", "action": "editar_reclamo"}
                    ]
                }

            # Si botón de adjuntar
            if accion == "adjuntar_foto":
                if self.context.get("anon_id") and not self.context.get("user_id"):
                    return {
                        "respuesta": "Para adjuntar una foto, necesitás iniciar sesión o registrarte. ¿Te gustaría hacerlo ahora?",
                        "botones": [
                            {"texto": "Iniciar Sesión", "action": "login"},
                            {"texto": "Registrarme Gratis", "action": "register"},
                            {"texto": "Continuar sin adjuntar", "action": "sin_adjuntos"}
                        ]
                    }
                return {
                    "respuesta": "¡Entendido! Podés enviarme la foto ahora. Cuando la vea, la adjuntaré al reclamo. Si preferís no adjuntar nada, simplemente decime 'continuar'.",
                    "botones": [
                        {"texto": "No adjuntar y continuar", "action": "sin_adjuntos"}
                    ]
                }
            if accion == "compartir_ubicacion":
                if self.context.get("anon_id") and not self.context.get("user_id"):
                    return {
                        "respuesta": "Para compartir tu ubicación GPS de forma precisa para el reclamo, necesitás iniciar sesión o registrarte. ¿Te gustaría hacerlo ahora?",
                        "botones": [
                            {"texto": "Iniciar Sesión", "action": "login"},
                            {"texto": "Registrarme Gratis", "action": "register"},
                            {"texto": "Continuar sin compartir ubicación", "action": "sin_adjuntos"}
                        ]
                    }
                return {
                    "respuesta": "¡Claro! Podés compartir tu ubicación actual usando el botón del clip 📎 en tu WhatsApp o la opción de compartir ubicación de la web. Si preferís no hacerlo, solo decime 'continuar'.",
                    "botones": [
                        {"texto": "No compartir y continuar", "action": "sin_adjuntos"}
                    ]
                }

            # Si recibe el archivo o ubicación real (esto es manejado por el payload que llega a responder_municipio)
            adjunto_recibido_msg = ""
            if payload.get("es_foto") and payload.get("archivo_url"):
                memoria["foto_url"] = payload.get("archivo_url")
                adjunto_recibido_msg = "¡Foto recibida y adjuntada!"
            if payload.get("es_ubicacion") and payload.get("ubicacion_usuario"):
                memoria["ubicacion_gps"] = payload.get("ubicacion_usuario")
                adjunto_recibido_msg = "¡Ubicación GPS recibida y adjuntada!" if not adjunto_recibido_msg else "¡Foto y ubicación GPS recibidas y adjuntadas!"


            if memoria.get("foto_url") or memoria.get("ubicacion_gps"):
                memoria["estado_conversacion"] = ConversationState.ESPERANDO_CONFIRMACION_RECLAMO
                resumen = self.build_detalles_memoria(memoria)
                return {
                    "respuesta": f"{adjunto_recibido_msg}\n\nExcelente. Ahora, por favor, revisá si todos los datos son correctos antes de confirmar:\n\n{resumen}\n\n¿Está todo bien para generar el reclamo?",
                    "botones": [
                        {"texto": "Sí, confirmar reclamo", "action": "confirmar_reclamo"},
                        {"texto": "No, quiero editar algo", "action": "editar_reclamo"}
                    ]
                }
            # Si no reconoce, intenta entender con LLM (por si usuario escribe raro)
            try:
                respuesta_llm = _clasificar_intencion_con_llm(
                    pregunta_str,
                    opciones=["adjuntar foto", "compartir ubicacion", "completar", "ninguno"],
                    tipo="adjunto"
                )
                if respuesta_llm and "completar" in respuesta_llm.lower(): # Si el LLM interpreta que quiere continuar sin adjuntos
                    memoria["estado_conversacion"] = ConversationState.ESPERANDO_CONFIRMACION_RECLAMO
                    resumen = self.build_detalles_memoria(memoria)
                    return {
                        "respuesta": f"Entendido, continuamos sin adjuntos. Por favor, revisá si todos los datos son correctos antes de confirmar:\n\n{resumen}\n\n¿Está todo bien para generar el reclamo?",
                        "botones": [
                            {"texto": "Sí, confirmar reclamo", "action": "confirmar_reclamo"},
                            {"texto": "No, quiero editar algo", "action": "editar_reclamo"}
                        ]
                    }
            except Exception:
                pass # Si el LLM falla, no hacemos nada y caemos al mensaje de abajo

            return {
                "respuesta": "No estoy seguro de haber recibido un adjunto. ¿Querés intentar adjuntar una foto o compartir tu ubicación? También podés elegir continuar sin adjuntos.",
                "botones": [
                    {"texto": "Adjuntar foto", "action": "adjuntar_foto"},
                    {"texto": "Compartir ubicación", "action": "compartir_ubicacion"},
                    {"texto": "No, continuar", "action": "sin_adjuntos"},
                    {"texto": "Completar reclamo", "action": "sin_adjuntos"}
                ]
            }

        # Paso 8: Confirmación y creación de ticket
        if estado == ConversationState.ESPERANDO_CONFIRMACION_RECLAMO:
            # Verificar si es anónimo y ya excedió el límite de tickets
            if self.context.get("anon_id") and not self.context.get("user_id"): # Es anónimo
                from flask import current_app # Acceder a config
                from datetime import datetime, timedelta # Asegurar imports
                max_tickets_anon = current_app.config.get("ANONYMOUS_MAX_TICKETS_PER_SESSION", 1)
                session_timeout_minutes_config = current_app.config.get("ANONYMOUS_SESSION_TIMEOUT_MINUTES", 15)

                # Contar tickets existentes para este anon_id DENTRO de la ventana de sesión actual
                anon_tickets_count = MunicipioTicket.query\
                    .filter_by(anon_id=self.context["anon_id"])\
                    .filter(MunicipioTicket.fecha >= datetime.utcnow() - timedelta(minutes=session_timeout_minutes_config))\
                    .count()

                current_app.logger.info(f"Usuario anónimo {self.context['anon_id']} (Municipio): {anon_tickets_count} tickets en la sesión actual (límite: {max_tickets_anon}).")

                if anon_tickets_count >= max_tickets_anon:
                    memoria.clear() # Limpiar el flujo de reclamo
                    return {
                        "respuesta": "Alcanzaste el límite de reclamos para usuarios invitados en esta sesión. Para continuar, por favor inicia sesión o regístrate.",
                        "botones": [
                            {"texto": "Iniciar Sesión", "action": "login"},
                            {"texto": "Registrarme Gratis", "action": "register"}
                        ]
                    }

            texto_normalizado = normalizar_texto(pregunta_str)
            accion = payload.get("action", "").lower() or texto_normalizado

            if accion in [
                "confirmar_reclamo",
                "confirmar",
                "confirmar reclamo",
                "confirmo",
                "confirmado",
                "si confirmo",
                "sí confirmo",
                "finalizar",
                "finalizar reclamo",
                "si",
                "sí",
            ]:
                # Armar bien los detalles y crear el ticket
                # Asegurarse de que los datos estén presentes antes de crear
                if not all(memoria.get(f"{campo}_reclamo" if campo not in ["nombre", "telefono", "email"] else f"{campo}_vecino") for campo in ["categoria", "direccion", "nombre", "telefono", "email", "descripcion"]):
                     logger.error("[ReclamoHandler] Faltan datos críticos para la creación del ticket.")
                     memoria.clear()
                     return {
                        "respuesta": "Hubo un problema al recopilar toda la información necesaria. Por favor, intentemos de nuevo. ¿Querés hacer un reclamo?",
                        "botones": [{"texto": "Hacer un reclamo"}]
                    }
                try:
                    categoria = memoria.get("categoria_reclamo", "General")
                    nombre = memoria.get("nombre_vecino", "")
                    telefono_raw = memoria.get("telefono_vecino", "")
                    email = memoria.get("email_vecino", "")
                    ticket_data = {
                        "asunto": f"Reclamo de {categoria}",
                        "categoria": categoria,
                        "detalles": memoria.get("descripcion_reclamo", ""),
                        "direccion": memoria.get("direccion_reclamo", ""),
                        "nombre_vecino": nombre,
                        "telefono_vecino": telefono_raw,
                        "email": email,
                        "estado": "nuevo",
                        "user_id": self.context.get("user_id"),
                        "municipio_id": getattr(self.context.get("user_obj"), "municipio_id", None),
                        "ubicacion": memoria.get("ubicacion_gps"),
                        "foto_url": memoria.get("foto_url"),
                    }
                    ticket = servicio_tickets.crear_nuevo_ticket(
                        tipo_ticket="municipio",
                        ticket_data=ticket_data,
                    )
                    # Envío notificaciones si corresponde
                    telefono_e164 = formatear_telefono_e164(telefono_raw)
                    if telefono_e164:
                        try:
                            enviar_notificacion_whatsapp_con_plantilla(
                                telefono_e164, nombre, ticket.nro_ticket, categoria
                            )
                        except Exception:
                            pass
                        try:
                            enviar_notificacion_sms(
                                telefono_e164,
                                f"Hola {nombre}! Tu reclamo M-{ticket.nro_ticket} ({categoria}) fue generado."
                            )
                        except Exception:
                            pass
                    memoria.clear()
                    return {
                        "respuesta": (
                            f"¡Excelente! Tu reclamo ha sido registrado con el número de ticket: **M-{ticket.nro_ticket}**. "
                            "Guardalo para futuras consultas. Te mantendremos informado sobre su progreso por email o WhatsApp. "
                            "¡Gracias por ayudarnos a mejorar nuestro municipio!"
                        ),
                        "botones": [
                            {"texto": "Hacer un nuevo reclamo"},
                            {"texto": "Consultar estado de un ticket"},
                            {"texto": "Volver al inicio"},
                        ],
                        "ticket_id": ticket.id
                    }
                except Exception as e:
                    logger.error(f"[ReclamoHandler] Error al crear ticket: {e}", exc_info=True)
                    memoria.clear()
                    return {
                        "respuesta": (
                            "¡Oh, parece que tuvimos un pequeño problema técnico al registrar tu reclamo! Lamento mucho las molestias. "
                            "¿Podrías intentarlo de nuevo en unos minutos? Si el problema continúa, el equipo del municipio estará contento de ayudarte por otros medios."
                        ),
                        "botones": [{"texto": "Intentar de nuevo"}, {"texto": "Hablar con un agente"}]
                    }
            # Si el texto coincide con editar
            elif any(kw in accion for kw in EDIT_KEYWORDS) or "editar" in accion or "cambiar" in accion:
                # Reiniciar el flujo desde la categoría, pero manteniendo los datos en memoria
                # para que el usuario no tenga que ingresarlos todos de nuevo.
                # O mejor, preguntar qué campo específico quiere editar.
                memoria["estado_conversacion"] = ConversationState.ESPERANDO_CATEGORIA_RECLAMO # Volvemos al inicio del flujo de reclamo
                # Podríamos agregar un mensaje más específico para edición aquí si quisiéramos.
                # Por ahora, simplemente se reinicia el flujo y el ReclamoHandler se encargará de pedir los datos.
                # El usuario verá el primer paso (categoría) y podrá ir confirmando o cambiando datos.
                # Una mejora futura sería preguntar específicamente qué campo editar.
                return {
                    "respuesta": (
                        "Entendido. Vamos a revisar los datos desde el principio para que puedas corregir lo que necesites. "
                        "Empecemos de nuevo con la categoría. ¿Cuál sería la categoría correcta para tu reclamo?"
                    ),
                    "botones": [{"texto": cat.title()} for cat in CATEGORIAS_RECLAMO] # Mostrar todas las categorías
                }
            # Si no reconoce, preguntale al LLM como último recurso
            else:
                try:
                    respuesta_llm = _clasificar_intencion_con_llm(
                        pregunta_str, opciones=["confirmar", "editar"], tipo="confirmacion"
                    )
                    if respuesta_llm and "confirm" in respuesta_llm.lower():
                        payload2 = payload.copy()
                        payload2["action"] = "confirmar_reclamo" # Forzar la acción de confirmación
                        return self.handle(payload2) # Volver a procesar con la acción forzada
                    elif respuesta_llm and "edit" in respuesta_llm.lower():
                        payload2 = payload.copy()
                        payload2["action"] = "editar_reclamo" # Forzar la acción de edición
                        return self.handle(payload2) # Volver a procesar con la acción forzada
                except Exception:
                    pass # Si el LLM falla, se muestra el mensaje de abajo

                resumen = self.build_detalles_memoria(memoria) # Volver a mostrar el resumen
                return {
                    "respuesta": f"No estoy seguro de qué quisiste decir. Por favor, confirmá si los datos son correctos o si querés editar algo:\n\n{resumen}\n\n¿Confirmamos o editamos?",
                    "botones": [
                        {"texto": "Sí, confirmar reclamo", "action": "confirmar_reclamo"},
                        {"texto": "No, quiero editar algo", "action": "editar_reclamo"}
                    ]
                }
        return None

# Placeholder para la función buscar_en_faqs
def buscar_en_faqs(pregunta: str, contexto_faq: str) -> dict | None:
    """
    Busca una pregunta en una base de conocimiento de FAQs.
    DEBE SER IMPLEMENTADA.
    """
    logger.info(f"[buscar_en_faqs] Buscando '{pregunta}' en el contexto '{contexto_faq}'. Implementación pendiente.")
    # Ejemplo de lo que podría devolver si encuentra algo:
    # if "costo" in pregunta.lower() and contexto_faq == "licencia_de_conducir":
    #     return {"p": pregunta, "a": "El costo del curso de licencia es de $5000.", "botones": [{"texto": "Más info"}]}
    return None


class TramitesHandler(BaseMunicipioHandler):
    # MODIFICADO: acepta payload
    def handle(self, payload: dict) -> dict | None:
        pregunta_str = payload.get("pregunta", "") # Extrae el string de la pregunta
        memoria = self.context.get("contexto_municipio", {})
        estado = memoria.get("estado_conversacion")
        
        # BLOQUEO si hay flujo de reclamo en curso
        if estado and estado in RECLAMO_STATES:
            return None
        
        intencion = self.context.get("intencion")

        # Si la intención es consultar_tramite y no hay estado activo (inicio de flujo)
        if intencion == "consultar_tramite" and not estado:
            memoria.clear() # Limpiar memoria para un nuevo flujo de trámite
            memoria["estado_conversacion"] = ConversationState.ESPERANDO_SELECCION_TRAMITE
            opciones = [{"texto": t.title()} for t in TRAMITES_INFO.keys()]
            return {
                "respuesta": "¿Sobre qué trámite necesitás información?",
                "botones": opciones,
            }

        # Si estamos esperando la selección de un trámite
        if estado == ConversationState.ESPERANDO_SELECCION_TRAMITE:
            from .sinonimos import aplicar_sinonimos, TRAMITE_SYNONYMS, fuzzy_match

            texto = normalizar_texto(pregunta_str)
            texto_con_sinonimos = aplicar_sinonimos(texto, TRAMITE_SYNONYMS)

            clave_tramite = next(
                (k for k in TRAMITES_INFO.keys() if normalizar_texto(k) == texto_con_sinonimos),
                None,
            )

            # Intenta un fuzzy match con los nombres de los trámites y sus sinónimos
            if not clave_tramite:
                all_tramite_names = list(TRAMITES_INFO.keys()) + list(TRAMITE_SYNONYMS.keys())
                best_match_key = fuzzy_match(all_tramite_names, texto)
                if best_match_key and best_match_key in TRAMITES_INFO: # Asegurar que el match es una clave de trámite real
                    clave_tramite = best_match_key
                elif best_match_key and best_match_key in TRAMITE_SYNONYMS: # Si el match es un sinónimo, obtener la clave real
                    clave_tramite = TRAMITE_SYNONYMS[best_match_key]


            if clave_tramite:
                memoria.clear() # Limpiar memoria después de encontrar el trámite
                info = TRAMITES_INFO[clave_tramite]
                user_obj = self.context.get("user_obj")
                link_web = (
                    getattr(user_obj, "link_web", None) or DEFAULT_TRAMITES_WEB_URL
                )
                direccion = getattr(user_obj, "direccion", None) or MUNICIPIO_DIRECCION
                data = {"linkWeb": link_web, "direccion": direccion}
                descripcion = reemplazar_placeholders(info.get("descripcion", ""), data)
                botones = info.get("botones", []).copy()
                botones = agregar_botones_para_links(descripcion, botones)
                return {
                    "respuesta": descripcion,
                    "botones": botones,
                }

            # Si no se encontró ningún trámite por nombre o fuzzy match, pero la pregunta es sobre licencia de conducir
            if "conducir" in texto and ("licencia" in texto or "carnet" in texto):
                memoria["estado_conversacion"] = (
                    ConversationState.ESPERANDO_PREGUNTA_CURSO_LICENCIA
                )
                return {
                    "respuesta": obtener_respuesta_municipio("curso_licencia_info"),
                    "botones": [
                        {
                            "texto": "Sacar Turno",
                            "url": "https://tlc.mendoza.gov.ar/turnos",
                        },
                        {"texto": "¿Dónde hacer el curso?"},
                    ],
                }

            memoria.clear() # Limpiar memoria si no se encontró el trámite y no es un flujo específico
            opciones = [{"texto": t.title()} for t in TRAMITES_INFO.keys()]
            return {
                "respuesta": obtener_respuesta_municipio("tramite_no_encontrado"),
                "botones": opciones,
            }
        
        # Si estamos esperando una pregunta sobre el curso de licencia
        elif estado == ConversationState.ESPERANDO_PREGUNTA_CURSO_LICENCIA:
            if es_pregunta_nueva(pregunta_str, "una pregunta sobre el curso de licencia"):
                memoria.clear()
                return None # Dejar que otro handler lo procese o caiga al general
            
            respuesta_faq = buscar_en_faqs(pregunta_str, "licencia_de_conducir")
            if respuesta_faq:
                memoria.clear()
                respuesta_dict = {"respuesta": respuesta_faq["a"]}
                if "botones" in respuesta_faq:
                    respuesta_dict["botones"] = respuesta_faq["botones"]
                return respuesta_dict
            
            # Si no se encuentra en la FAQ específica
            memoria.clear()
            return {"respuesta": obtener_respuesta_municipio("curso_licencia_info")}

        return None

# --- Handlers de Ventas ---
class ProductCatalogHandler(BaseMunicipioHandler):
    def handle(self, payload: dict) -> dict | None:
        pregunta_str = payload.get("pregunta", "").strip()
        memoria = self.context.get("contexto_municipio", {})
        intencion = self.context.get("intencion")

        if intencion != "iniciar_compra":
            return None

        if not PRODUCT_CATALOG:
            memoria.clear()
            return {"respuesta": "Lo sentimos, nuestro catálogo de productos no está disponible en este momento."}

        # Obtener categorías únicas
        categories = sorted(list(set(p.get("category", "Otros") for p in PRODUCT_CATALOG)))
        
        botones_categorias = []
        for cat in categories:
            botones_categorias.append({"texto": cat})

        respuesta_texto = "¡Excelente! Tenemos varios productos que podrían interesarte. ¿Qué tipo de producto estás buscando? Aquí tienes nuestras categorías:"
        
        # Podríamos mostrar algunos productos destacados también
        # destacados = [p['name'] for p in PRODUCT_CATALOG if p.get('featured')] # Asumiendo un campo 'featured'
        # if destacados:
        # respuesta_texto += "\nAlgunos destacados: " + ", ".join(destacados[:3])
            
        memoria["estado_conversacion"] = ConversationState.ESPERANDO_PRODUCTO_PARA_CONSULTA
        memoria.pop("last_found_products", None)
        memoria.pop("last_discussed_product", None)
        
        return {
            "respuesta": respuesta_texto,
            "botones": botones_categorias
        }

class ProductInquiryHandler(BaseMunicipioHandler):
    def _buscar_productos(self, texto_busqueda: str) -> list:
        if not texto_busqueda:
            return []
        
        texto_busqueda_norm = normalizar_texto(texto_busqueda)
        palabras_busqueda = set(texto_busqueda_norm.split())
        
        productos_encontrados = []
        for prod in PRODUCT_CATALOG:
            nombre_norm = normalizar_texto(prod.get("name", ""))
            desc_norm = normalizar_texto(prod.get("description", ""))
            cat_norm = normalizar_texto(prod.get("category", ""))

            # Prioridad 1: Coincidencia exacta o casi exacta en nombre
            if texto_busqueda_norm in nombre_norm:
                productos_encontrados.append({"producto": prod, "score": 10})
                continue

            # Prioridad 2: Todas las palabras de búsqueda en el nombre
            if palabras_busqueda.issubset(nombre_norm.split()):
                productos_encontrados.append({"producto": prod, "score": 8})
                continue
            
            # Prioridad 3: Palabras de búsqueda en categoría + nombre/descripción
            score = 0
            if any(palabra in cat_norm for palabra in palabras_busqueda):
                score +=3
            
            palabras_en_nombre = sum(1 for palabra in palabras_busqueda if palabra in nombre_norm)
            palabras_en_desc = sum(1 for palabra in palabras_busqueda if palabra in desc_norm)
            
            score += palabras_en_nombre * 2 # Más peso a las palabras en el nombre
            score += palabras_en_desc * 1

            if score > 2: # Umbral mínimo para considerar relevante
                 productos_encontrados.append({"producto": prod, "score": score})

        # Ordenar por score descendente
        productos_encontrados.sort(key=lambda x: x["score"], reverse=True)
        
        # Devolver solo los productos, no el score, y eliminar duplicados por ID
        # Y aplicar un filtro final de score si es necesario
        final_list_with_scores = []
        seen_ids = set()
        for item in productos_encontrados:
            if item["producto"]["id"] not in seen_ids:
                # Podríamos aplicar un umbral de score mínimo aquí si es necesario
                # if item["score"] > MIN_RELEVANCE_SCORE:
                final_list_with_scores.append(item)
                seen_ids.add(item["producto"]["id"])
        
        # Si no hay coincidencias fuertes, intentar fuzzy matching como último recurso
        # Esto es más útil si las palabras clave no dieron buenos resultados o para typos.
        if not final_list_with_scores or final_list_with_scores[0]["score"] < 5: # Si el mejor score es bajo
            all_product_names = {prod.get("id"): normalizar_texto(prod.get("name", "")) for prod in PRODUCT_CATALOG}
            # Usar texto_busqueda_norm que es la pregunta del usuario normalizada
            fuzzy_matches_names = difflib.get_close_matches(texto_busqueda_norm, all_product_names.values(), n=3, cutoff=0.7) # cutoff más alto para más precisión
            
            if fuzzy_matches_names:
                logger.info(f"[ProductInquiryHandler] Fuzzy matches encontrados: {fuzzy_matches_names}")
                for name_match in fuzzy_matches_names:
                    for prod_id, norm_name in all_product_names.items():
                        if norm_name == name_match:
                            # Encontrar el producto original
                            original_prod = next((p for p in PRODUCT_CATALOG if p["id"] == prod_id), None)
                            if original_prod and original_prod["id"] not in seen_ids:
                                # Añadir con un score indicativo de fuzzy match, o simplemente añadirlo
                                # Damos un score más bajo para que no supere a los keyword matches fuertes si los hubo
                                final_list_with_scores.append({"producto": original_prod, "score": 2}) 
                                seen_ids.add(original_prod["id"])
                                break # Pasar al siguiente nombre de fuzzy_match

        # Re-ordenar por si se añadieron fuzzy matches y para asegurar unicidad final
        final_list_with_scores.sort(key=lambda x: x["score"], reverse=True)
        
        # Extraer solo los productos finales
        final_products_list = []
        final_seen_ids = set()
        for item in final_list_with_scores:
            if item["producto"]["id"] not in final_seen_ids:
                final_products_list.append(item["producto"])
                final_seen_ids.add(item["producto"]["id"])
        
        return final_products_list

    def handle(self, payload: dict) -> dict | None:
        pregunta_str = payload.get("pregunta", "").strip()
        memoria = self.context.get("contexto_municipio", {})
        estado = memoria.get("estado_conversacion")
        intencion = self.context.get("intencion")

        # Si el usuario seleccionó una categoría del ProductCatalogHandler
        # o si la intención es consultar_producto
        if not (estado == ConversationState.ESPERANDO_PRODUCTO_PARA_CONSULTA or \
                (intencion == "consultar_producto" and estado != ConversationState.ESPERANDO_CONFIRMACION_AGREGAR_CARRITO) ):
            return None

        if not PRODUCT_CATALOG:
             return {"respuesta": "Nuestro catálogo de productos no está disponible en este momento. Intenta más tarde, por favor."}

        logger.info(f"[ProductInquiryHandler] Procesando consulta de producto: '{pregunta_str}'")
        
        # --- Placeholder para NLU avanzada con LLM para extraer entidades ---
        # Ejemplo de cómo se podría integrar:
        # if _deberia_usar_llm_para_entidades(pregunta_str): # Función heurística para decidir si usar LLM
        #     extracted_entities = _extract_product_entities_with_llm(pregunta_str, PRODUCT_CATALOG)
        #     if extracted_entities:
        #         # Lógica para manejar múltiples productos extraídos, cantidades, etc.
        #         # Esto podría implicar agregar varios al carrito o pedir confirmación para cada uno.
        #         # Por ahora, este es un punto de extensión.
        #         logger.info(f"[ProductInquiryHandler] Entidades extraídas por LLM: {extracted_entities}")
        #         # Ejemplo simplificado: si extrajo un solo producto y quiere agregarlo
        #         if len(extracted_entities) == 1 and extracted_entities[0].get('accion') == 'agregar':
        #                # ... buscar producto en catálogo, si existe y hay stock ...
        #                # memoria['last_discussed_product'] = found_product_from_llm
        #                # return CartHandler(self.context).handle(payload_con_producto_y_accion_agregar)
        #                pass # No implementado completamente aquí

        # Continuar con la búsqueda basada en keywords/fuzzy si LLM no se usó o no dio resultado concluyente.
        productos = self._buscar_productos(pregunta_str)
        
        memoria.pop("last_discussed_product", None) # Limpiar producto anterior

        if not productos:
            # Si la búsqueda por nombre/descripción no da nada, intentar buscar por categoría si la pregunta es solo una categoría
            # (esto ya estaba, se mantiene)
            categoria_match = next((cat for cat in set(p.get("category") for p in PRODUCT_CATALOG) if normalizar_texto(pregunta_str) == normalizar_texto(cat)), None)
            if categoria_match:
                productos_categoria = [p for p in PRODUCT_CATALOG if p.get("category") == categoria_match]
                if productos_categoria:
                    memoria["last_found_products"] = productos_categoria
                    memoria["estado_conversacion"] = ConversationState.MOSTRANDO_PRODUCTOS
                    nombres_productos = [f"{p['name']} (${p['price']:.2f})" for p in productos_categoria[:5]] # Mostrar hasta 5
                    respuesta_str = f"Encontré estos productos en la categoría '{categoria_match}':\n" + "\n".join(f"- {nombre}" for nombre in nombres_productos)
                    if len(productos_categoria) > 5:
                        respuesta_str += f"\n... y {len(productos_categoria) - 5} más."
                    respuesta_str += "\n¿Te interesa alguno en particular?"
                    return {"respuesta": respuesta_str, "botones": [{"texto": p['name']} for p in productos_categoria[:3]]} # Botones para los primeros 3

            return {
                "respuesta": "No encontré productos que coincidan con tu búsqueda. ¿Querés intentar con otras palabras o ver nuestras categorías?",
                "botones": [{"texto": "Ver categorías"}, {"texto": "Cancelar compra"}] 
            }

        if len(productos) == 1:
            prod = productos[0]
            memoria["last_discussed_product"] = prod
            memoria["estado_conversacion"] = ConversationState.ESPERANDO_CONFIRMACION_AGREGAR_CARRITO
            respuesta = (
                f"Encontré esto: **{prod['name']}**\n"
                f"{prod['description']}\nPrecio: ${prod['price']:.2f}\n"
                f"¿Te gustaría agregarlo al carrito?"
            )
            return {
                "respuesta": respuesta,
                "botones": [
                    {"texto": "Sí, agregar al carrito"},
                    {"texto": "No, gracias"},
                    {"texto": "Buscar otro producto"}
                ]
            }
        else: # Múltiples productos encontrados
            memoria["last_found_products"] = productos 
            memoria["estado_conversacion"] = ConversationState.MOSTRANDO_PRODUCTOS
            
            nombres_productos = [f"{p['name']} (${p['price']:.2f})" for p in productos[:5]] # Mostrar hasta 5
            respuesta_str = "Encontré varios productos que podrían interesarte:\n" + "\n".join(f"- {nombre}" for nombre in nombres_productos)
            if len(productos) > 5:
                respuesta_str += f"\n... y {len(productos) - 5} más."
            respuesta_str += "\n¿Cuál de estos te interesa? O puedes refinar tu búsqueda."

            botones = [{"texto": p['name']} for p in productos[:3]] # Botones para los primeros 3
            botones.append({"texto": "Buscar de nuevo"})
            return {
                "respuesta": respuesta_str,
                "botones": botones
            }

class CartHandler(BaseMunicipioHandler):
    def _initialize_cart(self, memoria: dict):
        memoria.setdefault('shopping_cart', [])

    def _add_to_cart(self, memoria: dict, product_to_add: dict, quantity: int = 1) -> bool:
        self._initialize_cart(memoria)
        cart = memoria['shopping_cart']
        
        # Check stock (basic)
        if product_to_add.get('stock', float('inf')) < quantity:
            return False # Not enough stock

        for item in cart:
            if item['id'] == product_to_add['id']:
                item['quantity'] += quantity
                # item['stock'] -= quantity # Deduct stock if managing here
                return True
        
        cart.append({
            'id': product_to_add['id'],
            'name': product_to_add['name'],
            'price': product_to_add['price'],
            'quantity': quantity,
            # 'stock': product_to_add.get('stock', float('inf')) - quantity 
        })
        return True

    def _remove_from_cart(self, memoria: dict, product_id_to_remove: str) -> bool:
        self._initialize_cart(memoria)
        cart = memoria['shopping_cart']
        original_length = len(cart)
        memoria['shopping_cart'] = [item for item in cart if item['id'] != product_id_to_remove]
        return len(memoria['shopping_cart']) < original_length
    
    def _format_cart_view(self, memoria: dict) -> str:
        self._initialize_cart(memoria)
        cart = memoria['shopping_cart']
        if not cart:
            return "Tu carrito de compras está vacío."

        respuesta = "🛒 **Tu Carrito de Compras:**\n"
        total_general = 0.0
        for i, item in enumerate(cart):
            subtotal = item['price'] * item['quantity']
            respuesta += f"{i+1}. **{item['name']}**\n"
            respuesta += f"   Cantidad: {item['quantity']} x ${item['price']:.2f} c/u = ${subtotal:.2f}\n"
            total_general += subtotal
        
        respuesta += f"\n✨ **Total General: ${total_general:.2f}**"
        memoria['last_cart_total'] = total_general # Guardar para posible checkout
        return respuesta

    def handle(self, payload: dict) -> dict | None:
        pregunta_str = payload.get("pregunta", "").strip()
        action = payload.get("action", normalizar_texto(pregunta_str)) # Considerar acción de botón
        memoria = self.context.get("contexto_municipio", {})
        estado = memoria.get("estado_conversacion")
        intencion = self.context.get("intencion")
        
        self._initialize_cart(memoria)

        # --- AGREGAR AL CARRITO ---
        if (intencion == "agregar_al_carrito" or \
            (estado == ConversationState.ESPERANDO_CONFIRMACION_AGREGAR_CARRITO and \
             any(kw in action for kw in ["si", "sí", "agregar", "dale", "quiero"]))):
            
            product_to_add = memoria.get("last_discussed_product")
            if not product_to_add:
                return {"respuesta": "No estoy seguro de qué producto querés agregar. ¿Podrías mostrarme de nuevo?"}

            if self._add_to_cart(memoria, product_to_add):
                memoria.pop("last_discussed_product", None)
                memoria.pop("last_found_products", None)
                memoria["estado_conversacion"] = ConversationState.ESPERANDO_OPCION_CARRITO 
                
                cart_summary = self._format_cart_view(memoria)
                num_items = sum(item['quantity'] for item in memoria['shopping_cart'])
                
                return {
                    "respuesta": f"✅ ¡{product_to_add['name']} agregado al carrito!\n\n{cart_summary}",
                    "botones": [
                        {"texto": "Seguir comprando"},
                        {"texto": "Finalizar Compra"},
                        {"texto": "Quitar un producto"}
                    ]
                }
            else: # Sin stock
                 return {
                    "respuesta": f"Lo siento, parece que no tenemos suficiente stock de {product_to_add['name']} en este momento.",
                    "botones": [{"texto": "Buscar otro producto"}, {"texto": "Ver carrito"}]
                }
        
        if estado == ConversationState.ESPERANDO_CONFIRMACION_AGREGAR_CARRITO and \
           any(kw in action for kw in ["no", "cancelar"]):
            memoria.pop("last_discussed_product", None)
            memoria.pop("estado_conversacion", None) # Volver al estado neutro o MOSTRANDO_PRODUCTOS si venía de ahí
            return {"respuesta": "Entendido. ¿Querés buscar otro producto o ver nuestras categorías?", "botones": [{"texto":"Buscar otro producto"}, {"texto": "Ver categorías"}]}


        # --- VER CARRITO ---
        if intencion == "ver_carrito" or action == "ver carrito":
            cart_view = self._format_cart_view(memoria)
            botones = []
            if memoria['shopping_cart']: # Solo mostrar botones de acción si hay algo en el carrito
                 botones = [
                    {"texto": "Finalizar Compra"},
                    {"texto": "Seguir comprando"},
                    {"texto": "Quitar un producto"}
                ]
            else:
                botones = [{"texto": "Ver productos"}]
            
            memoria["estado_conversacion"] = ConversationState.ESPERANDO_OPCION_CARRITO if memoria['shopping_cart'] else None
            return {
                "respuesta": cart_view,
                "botones": botones
            }

        # --- QUITAR DEL CARRITO (Básico) ---
        if intencion == "eliminar_del_carrito" or \
           (estado == ConversationState.ESPERANDO_OPCION_CARRITO and "quitar" in action):
            
            if not memoria['shopping_cart']:
                return {"respuesta": "Tu carrito ya está vacío.", "botones": [{"texto": "Ver productos"}]}

            # Por ahora, una forma simple: quitar el último agregado o preguntar cuál.
            # Para una mejor UX, se necesitaría identificar el producto a quitar por nombre o índice.
            # Esta es una simplificación para el primer paso.
            
            # Ejemplo: si el usuario dice "quitar Malbec Clásico"
            producto_a_quitar_nombre = None
            if "quitar" in pregunta_str: # Asume formato "quitar NOMBRE_PRODUCTO"
                partes = pregunta_str.split("quitar", 1)
                if len(partes) > 1 and partes[1].strip():
                    producto_a_quitar_nombre = normalizar_texto(partes[1].strip())
            
            if producto_a_quitar_nombre:
                item_id_to_remove = None
                for item in memoria['shopping_cart']:
                    if normalizar_texto(item['name']) == producto_a_quitar_nombre:
                        item_id_to_remove = item['id']
                        break
                if item_id_to_remove:
                    self._remove_from_cart(memoria, item_id_to_remove)
                    cart_view = self._format_cart_view(memoria)
                    respuesta_msg = f"'{producto_a_quitar_nombre.title()}' eliminado del carrito.\n\n{cart_view}"
                    if not memoria['shopping_cart']: memoria.pop("estado_conversacion", None)
                    return {"respuesta": respuesta_msg, "botones": [{"texto": "Seguir comprando"}, {"texto": "Finalizar Compra"}]}
                else:
                    return {"respuesta": f"No encontré '{producto_a_quitar_nombre.title()}' en tu carrito. ¿Querés ver el carrito para verificar?", "botones": [{"texto":"Ver carrito"}]}
            else: # No se especificó producto, o no se pudo extraer. Pedir que elija.
                memoria["estado_conversacion"] = ConversationState.ESPERANDO_OPCION_CARRITO # O un nuevo estado ESPERANDO_PRODUCTO_A_QUITAR
                botones_productos_carrito = [{"texto": f"Quitar: {item['name']}"} for item in memoria['shopping_cart'][:3]] # Mostrar hasta 3 para quitar
                return {
                    "respuesta": "OK. ¿Qué producto te gustaría quitar de tu carrito?",
                    "botones": botones_productos_carrito + [{"texto": "Ver carrito completo"}, {"texto": "Cancelar"}]
                }
        
        # Si la acción es quitar un producto específico (ej: por botón "Quitar: Malbec Clásico")
        if action.startswith("quitar:"):
            nombre_a_quitar = action.split("quitar:", 1)[1].strip()
            item_id_to_remove = None
            for item in memoria['shopping_cart']:
                if item['name'] == nombre_a_quitar: # Comparación exacta por nombre del botón
                    item_id_to_remove = item['id']
                    break
            if item_id_to_remove:
                self._remove_from_cart(memoria, item_id_to_remove)
                cart_view = self._format_cart_view(memoria)
                respuesta_msg = f"'{nombre_a_quitar}' eliminado del carrito.\n\n{cart_view}"
                if not memoria['shopping_cart']: memoria.pop("estado_conversacion", None)
                return {"respuesta": respuesta_msg, "botones": [{"texto": "Seguir comprando"}, {"texto": "Finalizar Compra"}]}


        # --- SEGUIR COMPRANDO (desde el carrito) ---
        if estado == ConversationState.ESPERANDO_OPCION_CARRITO and "seguir comprando" in action:
            memoria.pop("estado_conversacion", None) # Limpiar estado para que pueda buscar productos
            # Re-llamar a ProductCatalogHandler o similar para mostrar opciones de compra
            return ProductCatalogHandler(self.context).handle({"pregunta": "ver productos", "action":"ver productos", "intencion": "iniciar_compra"})


        return None

class CheckoutHandler(BaseMunicipioHandler):
    def handle(self, payload: dict) -> dict | None:
        pregunta_str = payload.get("pregunta", "").strip()
        action = payload.get("action", normalizar_texto(pregunta_str))
        memoria = self.context.get("contexto_municipio", {})
        estado = memoria.get("estado_conversacion")
        intencion = self.context.get("intencion")

        cart_handler_instance = CartHandler(self.context) # Para reutilizar _format_cart_view

        # --- INICIAR CHECKOUT ---
        if intencion == "proceder_al_pago" or \
           (estado == ConversationState.ESPERANDO_OPCION_CARRITO and "finalizar compra" in action):
            
            cart_handler_instance._initialize_cart(memoria) # Asegurar que el carrito existe
            if not memoria.get('shopping_cart'):
                return {
                    "respuesta": "Tu carrito está vacío. ¿Querés ver nuestros productos para agregar algo?",
                    "botones": [{"texto": "Ver productos"}]
                }
            
            cart_view = cart_handler_instance._format_cart_view(memoria)
            memoria["estado_conversacion"] = ConversationState.ESPERANDO_CONFIRMACION_PEDIDO
            return {
                "respuesta": f"Estás por finalizar tu compra. Por favor, revisá tu pedido:\n\n{cart_view}\n\n¿Confirmás este pedido?",
                "botones": [
                    {"texto": "Sí, confirmar pedido"},
                    {"texto": "Modificar carrito"},
                    {"texto": "Cancelar"}
                ]
            }

        # --- CONFIRMAR PEDIDO ---
        if estado == ConversationState.ESPERANDO_CONFIRMACION_PEDIDO:
            if any(kw in action for kw in ["si", "sí", "confirmar", "confirmar pedido"]):
                cart_handler_instance._initialize_cart(memoria) # Asegurar que el carrito existe
                shopping_cart = memoria.get('shopping_cart', [])
                if not shopping_cart: # Doble chequeo por si acaso
                    memoria.clear() # Limpiar por si hubo error
                    return {"respuesta": "Parece que tu carrito está vacío. Volvamos a empezar.", "botones": [{"texto": "Ver productos"}]}

                # Crear el ticket de pedido
                try:
                    user_name = getattr(self.context.get("user_obj"), "nombre", "Cliente Chat") or \
                                getattr(self.context.get("viewer_user"), "nombre", "Cliente Chat")
                    
                    # Formatear detalles del pedido para el ticket
                    detalles_pedido_str = "Productos:\n"
                    for item in shopping_cart:
                        detalles_pedido_str += f"- {item['name']} (x{item['quantity']}) - ${item['price']:.2f} c/u\n"
                    detalles_pedido_str += f"\nTotal: ${memoria.get('last_cart_total', 0.0):.2f}"
                    
                    # (Opcional) Recopilar más info como dirección de envío aquí si es necesario,
                    # por ahora se simplifica y se asume que se coordina post-confirmación.

                    ticket_data = {
                        "asunto": f"Nuevo Pedido Web/Chat - {user_name}",
                        "categoria": "Pedido Online",
                        "detalles": detalles_pedido_str,
                        "pregunta": f"Pedido confirmado por {user_name}.", # O un resumen
                        "estado": "pedido_confirmado", # O "pendiente_procesamiento"
                        "user_id": self.context.get("cliente_id"),
                        "anon_id": self.context.get("anon_id") if not self.context.get("cliente_id") else None,
                        "municipio_id": getattr(self.context.get("user_obj"), "municipio_id", None),
                        # Podríamos añadir campos específicos para pedidos si el modelo Ticket lo permite
                        # ej: 'datos_pedido_json': json.dumps(shopping_cart)
                    }
                    
                    # Asumimos tipo "pyme" para pedidos, o hacerlo configurable
                    # Si el bot es solo para una tienda, "pyme" tiene sentido.
                    # Si es un bot multipropósito (municipio y ventas), esto necesita más lógica.
                    # Por ahora, si 'rubro_obj' existe y tiene un 'id', lo usamos para PymeTicket. Sino, MunicipioTicket.
                    tipo_ticket_pedido = "pyme" if hasattr(self.context.get("rubro_obj"), "id") else "municipio"


                    pedido_ticket = servicio_tickets.crear_nuevo_ticket(
                        tipo_ticket=tipo_ticket_pedido, 
                        ticket_data=ticket_data
                    )

                    if pedido_ticket:
                        logger.info(f"Pedido confirmado y registrado como ticket #{pedido_ticket.nro_ticket}.")
                        memoria.pop('shopping_cart', None)
                        memoria.pop('last_cart_total', None)
                        memoria.pop('last_discussed_product', None)
                        memoria.pop('last_found_products', None)
                        memoria.pop("estado_conversacion", None)
                        
                        # Notificar al admin (ya sucede si crear_comentario se llama dentro de crear_nuevo_ticket o si se agrega un comentario post-creación)
                        # Si no, llamar explícitamente a email_service.enviar_email_pedido_admin(pedido_ticket) si esa función existe y está adaptada
                        
                        return {
                            "respuesta": (
                                f"¡Excelente! Tu pedido ha sido confirmado con el número de referencia: **{pedido_ticket.nro_ticket}**. "
                                "Nos pondremos en contacto contigo a la brevedad para coordinar los detalles del pago y la entrega. ¡Gracias por tu compra!"
                            ),
                            "botones": [{"texto": "Ver más productos"}, {"texto": "Necesito ayuda"}]
                        }
                    else:
                        raise Exception("La creación del ticket de pedido retornó None.")

                except Exception as e:
                    logger.error(f"[CheckoutHandler] Error al finalizar pedido y crear ticket: {e}", exc_info=True)
                    # No limpiar el carrito en caso de error para que el usuario pueda reintentar
                    memoria["estado_conversacion"] = ConversationState.ESPERANDO_OPCION_CARRITO # Volver al carrito
                    return {
                        "respuesta": "Hubo un problema al procesar tu pedido. Por favor, intentá confirmar nuevamente en unos momentos. Tu carrito sigue guardado.",
                        "botones": [{"texto": "Reintentar confirmar"}, {"texto": "Ver carrito"}]
                    }
            elif any(kw in action for kw in ["modificar", "modificar carrito"]):
                memoria["estado_conversacion"] = ConversationState.ESPERANDO_OPCION_CARRITO
                cart_view = cart_handler_instance._format_cart_view(memoria) # Reutilizar vista del carrito
                return {
                    "respuesta": "Ok, volvemos a tu carrito para que puedas modificarlo:\n\n" + cart_view,
                     "botones": [
                        {"texto": "Finalizar Compra"},
                        {"texto": "Seguir comprando"},
                        {"texto": "Quitar un producto"}
                    ]
                }
            elif any(kw in action for kw in ["cancelar", "no"]):
                 memoria.pop("estado_conversacion", None)
                 return {
                     "respuesta": "Pedido cancelado. ¿Querés seguir viendo productos o necesitas ayuda con algo más?",
                     "botones": [{"texto": "Seguir comprando"}, {"texto": "Ver carrito"}, {"texto": "Hablar con un agente"}]
                 }
            else: # Respuesta no clara
                cart_view = cart_handler_instance._format_cart_view(memoria)
                return {
                    "respuesta": f"No entendí tu respuesta. Por favor, confirmá si querés finalizar este pedido:\n\n{cart_view}\n\n",
                    "botones": [
                        {"texto": "Sí, confirmar pedido"},
                        {"texto": "Modificar carrito"},
                        {"texto": "Cancelar"}
                    ]
                }
        return None

class StoreLocationHandler(BaseMunicipioHandler):
    def _calculate_distance_sq(self, lat1, lon1, lat2, lon2):
        """Calculates squared Euclidean distance. Faster than true distance for sorting."""
        # Consider using haversine for real-world distances if precision matters beyond sorting.
        return (lat1 - lat2)**2 + (lon1 - lon2)**2

    def handle(self, payload: dict) -> dict | None:
        pregunta_str = payload.get("pregunta", "").strip()
        memoria = self.context.get("contexto_municipio", {})
        intencion = self.context.get("intencion")
        user_location = self.context.get("ubicacion_usuario") # Expected format: {'lat': float, 'lon': float}

        if intencion != "solicitar_ubicacion_tienda":
            return None

        if not COMMERCE_LOCATIONS:
            return {"respuesta": "Lo siento, no tengo información sobre la ubicación de nuestras tiendas o sucursales en este momento."}

        if not user_location:
            # Si el usuario no ha compartido ubicación, pedirla.
            # Guardar la intención para que después de compartir ubicación, se vuelva a este handler.
            memoria["estado_conversacion"] = "ESPERANDO_UBICACION_PARA_TIENDAS" # Podría ser un Enum
            memoria["intencion_pendiente_ubicacion"] = "solicitar_ubicacion_tienda"
            return {
                "respuesta": "Para encontrar las sucursales más cercanas, necesito tu ubicación. ¿Podrías compartirla?",
                "botones": [
                    {"texto": "Compartir mi ubicación", "action": "compartir_ubicacion_para_tiendas"}, # Frontend debe manejar esta acción
                    {"texto": "No, gracias"}
                ]
            }
        
        # Si llegamos aquí, tenemos la ubicación del usuario.
        # Limpiar estado de espera de ubicación si existiera.
        if memoria.get("estado_conversacion") == "ESPERANDO_UBICACION_PARA_TIENDAS":
            memoria.pop("estado_conversacion", None)
            memoria.pop("intencion_pendiente_ubicacion", None)

        # Calcular distancias y ordenar
        locations_with_distance = []
        for loc in COMMERCE_LOCATIONS:
            if loc.get("latitude") is not None and loc.get("longitude") is not None:
                dist_sq = self._calculate_distance_sq(
                    user_location['lat'], user_location['lon'],
                    loc['latitude'], loc['longitude']
                )
                locations_with_distance.append({**loc, "distance_sq": dist_sq})
        
        locations_with_distance.sort(key=lambda x: x["distance_sq"])
        
        nearest_locations = locations_with_distance[:3] # Mostrar las 3 más cercanas

        if not nearest_locations:
            return {"respuesta": "No encontré tiendas o sucursales cercanas a tu ubicación actual."}

        respuesta_str = "Aquí están las tiendas/sucursales más cercanas que encontré:\n"
        botones = []
        for i, loc in enumerate(nearest_locations):
            respuesta_str += (
                f"\n{i+1}. **{loc['name']}**\n"
                f"   Dirección: {loc['address']}\n"
            )
            if loc.get('hours'):
                respuesta_str += f"   Horario: {loc['hours']}\n"
            if loc.get('phone'):
                 respuesta_str += f"   Teléfono: {loc['phone']}\n"
            
            # Botón para ver en mapa (requiere que el frontend lo maneje o genere un link de Google Maps)
            map_url = f"https://www.google.com/maps/search/?api=1&query={loc['latitude']},{loc['longitude']}"
            botones.append({"texto": f"Ver mapa: {loc['name']}", "url": map_url})
        
        respuesta_str += "\nEspero que esta información te sea útil."
        # Limpiar el contexto de ventas por si acaso
        memoria.pop("last_found_products", None)
        memoria.pop("last_discussed_product", None)
        memoria.pop("shopping_cart", None)


        return {
            "respuesta": respuesta_str,
            "botones": botones
        }

# --- Fin Handlers de Ventas ---

# --- Handler de Pánico ---
class PanicButtonHandler(BaseMunicipioHandler):
    def handle(self, payload: dict) -> dict | None:
        pregunta_str = payload.get("pregunta", "").strip() # Puede ser útil para loguear el trigger inicial
        memoria = self.context.get("contexto_municipio", {})
        estado = memoria.get("estado_conversacion")
        intencion = self.context.get("intencion")
        user_location = self.context.get("ubicacion_usuario")

        if not (intencion == "activar_panico" or estado == ConversationState.ESPERANDO_UBICACION_PANICO):
            return None

        logger.warning(f"[PANIC_HANDLER] Pánico activado. Intención: {intencion}, Estado: {estado}, Ubicación: {user_location}")
        
        # Si no tenemos ubicación y no la estamos esperando explícitamente, la pedimos.
        # También verificar si es anónimo, ya que compartir ubicación podría ser una función restringida.
        if self.context.get("anon_id") and not self.context.get("user_id"):
            # Para Pánico, la restricción es más laxa, pero igual se informa.
             return {
                "respuesta": (
                    "🚨 **EMERGENCIA DETECTADA** 🚨\nPara enviar ayuda de forma efectiva, necesitamos tu ubicación. "
                    "Compartir tu ubicación precisa requiere que inicies sesión o te registres. "
                    "**Si estás en peligro inmediato y no puedes/quieres registrarte, llamá directamente al 911 o al número de emergencia local.**\n\n"
                    "Si deseas continuar por aquí y compartir tu ubicación (requiere registro/login):"
                ),
                "botones": [
                    {"texto": "Iniciar Sesión para Emergencia", "action": "login"},
                    {"texto": "Registrarme para Emergencia", "action": "register"},
                    {"texto": "Cancelar Alerta (error mío)"} # Opción para cancelar si fue un error
                ]
            }

        if not user_location and estado != ConversationState.ESPERANDO_UBICACION_PANICO:
            memoria["estado_conversacion"] = ConversationState.ESPERANDO_UBICACION_PANICO
            memoria["intencion_pendiente_ubicacion"] = "activar_panico" # Para el callback de ubicación
            # Guardar el mensaje original que disparó el pánico si es la primera vez.
            if intencion == "activar_panico": # Solo guardar si es el inicio del flujo de pánico
                 memoria["mensaje_original_panico"] = pregunta_str

            return {
                "respuesta": (
                    "¡EMERGENCIA! Para ayudarte de inmediato, COMPARTÍ TU UBICACIÓN AHORA. Es crucial para enviar ayuda.\n"
                    "Si no puedes compartirla, intentaremos ayudarte igualmente, pero la ubicación acelera la respuesta."
                ),
                "botones": [
                    {"texto": "🚨 COMPARTIR UBICACIÓN URGENTE", "action": "compartir_ubicacion_urgente"},
                    {"texto": "No puedo compartir ubicación"} # Usuario puede confirmar pánico sin ubicación
                ]
            }

        # Si el usuario presiona "No puedo compartir ubicación" o si ya teníamos la ubicación
        # o si la ubicación se acaba de recibir (user_location ya estaría en context).
        
        # Limpiar estados de espera de ubicación si ya la tenemos o si el usuario decidió no compartirla
        if memoria.get("estado_conversacion") == ConversationState.ESPERANDO_UBICACION_PANICO:
            memoria.pop("estado_conversacion", None)
            memoria.pop("intencion_pendiente_ubicacion", None)
        
        mensaje_original_guardado = memoria.pop("mensaje_original_panico", pregunta_str) # Usar el guardado o el actual

        try:
            detalles_alerta = f"Botón de pánico activado por el usuario. Mensaje original: '{mensaje_original_guardado}'."
            if user_location:
                detalles_alerta += f" Ubicación compartida: Lat {user_location.get('lat')}, Lon {user_location.get('lon')}."
            else:
                detalles_alerta += " Ubicación NO compartida por el usuario."

            ticket_data = {
                "asunto": "¡¡¡ALERTA DE PÁNICO ACTIVADA!!!",
                "categoria": "Emergencia Pánico", # Categoría bien distintiva
                "detalles": detalles_alerta,
                "pregunta": mensaje_original_guardado, # El mensaje que disparó el pánico
                "estado": "ALERTA_PANICO_ACTIVA", # Un estado específico y urgente
                "user_id": self.context.get("cliente_id"),
                "anon_id": self.context.get("anon_id") if not self.context.get("cliente_id") else None,
                "municipio_id": getattr(self.context.get("user_obj"), "municipio_id", None),
                "latitud": user_location.get("lat") if user_location else None,
                "longitud": user_location.get("lon") if user_location else None,
            }

            # Siempre tipo "municipio" para pánico, ya que es un servicio ciudadano.
            ticket_panico = servicio_tickets.crear_nuevo_ticket(
                tipo_ticket="municipio", 
                ticket_data=ticket_data
            )

            if not ticket_panico:
                raise Exception("La creación del ticket de pánico retornó None.")

            logger.critical(f"[PANIC_HANDLER] Ticket de pánico M-{ticket_panico.nro_ticket} CREADO. {detalles_alerta}")
            
            # TODO: Considerar notificación SMS/WhatsApp directa a un número de emergencia configurado,
            # además del email que enviará `servicio_tickets` al ADMIN_EMAIL.
            # Ejemplo: enviar_sms_emergencia(numero_emergencia, f"ALERTA PANICO M-{ticket_panico.nro_ticket} en Lat:{lat} Lon:{lon}")

            respuesta_usuario = ""
            if user_location:
                respuesta_usuario = (
                    "Tu ALERTA DE PÁNICO y ubicación han sido ENVIADAS a los servicios de emergencia. "
                    "La ayuda está en camino. Mantené la calma y seguí las instrucciones de las autoridades si te contactan."
                )
            else:
                respuesta_usuario = (
                    "Tu ALERTA DE PÁNICO ha sido ENVIADA. No se pudo obtener tu ubicación. "
                    "Si es posible, informala cuando te contacten. Mantené la calma."
                )
            
            # Limpiar contexto sensible o innecesario. No limpiar todo por si hay info de usuario útil.
            memoria.pop("shopping_cart", None) 
            memoria.pop("last_discussed_product", None)
            # No limpiar 'estado_conversacion' aquí, ya se hizo o no aplica.

            return {"respuesta": respuesta_usuario}

        except Exception as e:
            logger.error(f"[PanicButtonHandler] Error crítico al procesar pánico: {e}", exc_info=True)
            # Mensaje de fallback genérico pero que indique que algo se intentó
            return {
                "respuesta": (
                    "Estamos intentando procesar tu alerta de emergencia. Si estás en peligro inmediato, por favor contacta "
                    "directamente a los servicios de emergencia locales (ej: 911)."
                )
            }
# --- Fin Handler de Pánico ---


class ImpuestosHandler(BaseMunicipioHandler):
    # MODIFICADO: acepta payload
    def handle(self, payload: dict) -> dict | None:
        pregunta_str = payload.get("pregunta", "") # Extrae el string de la pregunta
        memoria = self.context.get("contexto_municipio", {})
        estado = memoria.get("estado_conversacion")
        if estado and estado in RECLAMO_STATES:
            return None
        intencion = self.context.get("intencion")
        if intencion == "consultar_impuestos":
            self.context.get("contexto_municipio", {}).clear()
            return {
                "respuesta": obtener_respuesta_municipio("impuestos_info"),
                "botones": obtener_respuesta_municipio("impuestos_botones"),
            }
        return None


class GeneralHandler(BaseMunicipioHandler):
    # MODIFICADO: acepta payload
    def handle(self, payload: dict) -> dict | None:
        pregunta_str = payload.get("pregunta", "") # Extrae el string de la pregunta
        memoria = self.context.get("contexto_municipio", {})
        estado = memoria.get("estado_conversacion")
        if estado and estado in RECLAMO_STATES:
            return None
        
        logger.info("[GeneralHandler] Consulta general con contexto de DB.")
        user_obj = self.context.get("user_obj")

        # Si el usuario está en el flujo de licencia de conducir y hace una pregunta general
        if estado == ConversationState.ESPERANDO_PREGUNTA_CURSO_LICENCIA:
            respuesta_faq = buscar_en_faqs(pregunta_str, "licencia_de_conducir")
            if respuesta_faq:
                memoria.clear()
                respuesta = {"respuesta": respuesta_faq["a"]}
                if "botones" in respuesta_faq:
                    respuesta["botones"] = respuesta_faq["botones"]
                return respuesta
            memoria.clear() # Limpiar si no se encuentra en la FAQ específica
            return {"respuesta": "¿Sobre qué más te puedo ayudar?"}

        if not user_obj: # Solo si no es un usuario logueado o con datos.
            return None

        contexto_scraped = ""
        try:
            # Filtrar por municipio_id si user_obj tiene uno, para no traer info de otros municipios
            query_filter = {"user_id": user_obj.id}
            if hasattr(user_obj, "municipio_id") and user_obj.municipio_id:
                query_filter["municipio_id"] = user_obj.municipio_id

            contenidos = SitioWebInfo.query.filter_by(**query_filter).all()
            
            textos_relevantes = [
                json.loads(item.datos_json).get("contenido", "")
                for item in contenidos
                if json.loads(item.datos_json).get("tipo") == "contenido_general"
            ]
            contexto_scraped = " ".join(filter(None, textos_relevantes))
            
            if not contexto_scraped:
                contexto_scraped = "No hay información disponible para esta consulta general."
        except Exception as e:
            logger.error(f"[GeneralHandler] Error al obtener contenido SitioWebInfo: {e}", exc_info=True)
            contexto_scraped = "Hubo un error al cargar la información general."

        prompt_final = PROMPT_MUNICIPIO_CON_CONTEXTO.format(
            contexto_scraped=contexto_scraped, pregunta_usuario=pregunta_str
        )
        
        respuesta_llm = safe_llm_call(
            prompt=prompt_final,
            preamble="Sos un asistente municipal que responde basado en info oficial.",
            fallback="No encontré información específica para esa consulta. Te puedo ayudar con reclamos, trámites, o conectar con un agente."
        )

        if "no tengo información específica" in respuesta_llm.lower() or \
           "no pude encontrar la respuesta a tu pregunta" in respuesta_llm.lower() or \
           "no encontré respuesta exacta" in respuesta_llm.lower():
            
            return {
                "respuesta": (
                    "No encontré respuesta exacta a tu pregunta. Pero te puedo ayudar con estas opciones:"
                ),
                "botones": [
                    {"texto": "Hacer un reclamo"},
                    {"texto": "Consultar estado de ticket"},
                    {"texto": "Hablar con un agente"},
                ],
            }
        return {"respuesta": respuesta_llm}


class EngancheAnonimoMunicipioHandler(BaseMunicipioHandler):
    # MODIFICADO: acepta payload
    def handle(self, payload: dict) -> dict | None:
        # pregunta_str = payload.get("pregunta", "") # No se usa directamente aquí, solo la presencia de user_id/intencion
        
        # Si ya hay un user_id, no se aplica este enganche
        if self.context.get("user_id"):
            return None

        # Si el usuario hace una pregunta trivial (saludo, small talk, etc.),
        # dejamos que los handlers correspondientes respondan primero.
        # Estas llamadas a handle son con el payload completo ahora
        if GreetingHandler(self.context).handle(payload) or \
           PoliteHandler(self.context).handle(payload) or \
           SmallTalkHandler(self.context).handle(payload):
            # Estos handlers ya devuelven una respuesta o None. Si devuelven respuesta, se usa.
            # Si devuelven None, la cadena de handlers continúa.
            # No necesitamos hacer nada especial aquí para EngancheAnonimo si estos ya respondieron.
            pass # La respuesta de estos handlers (si la hay) se propagará.
        
        # Si la intención es claramente hacer un reclamo o hablar con agente, o consultar ticket,
        # y el usuario es anónimo (verificado por la ausencia de user_id/cliente_id y presencia de anon_id)
        # entonces el enganche debe ser específico para esas acciones que requieren registro.
        intencion = self.context.get("intencion")
        es_anonimo_real = self.context.get("anon_id") and not self.context.get("user_id") and not self.context.get("cliente_id")

        if es_anonimo_real:
            if intencion in ["iniciar_reclamo", "hablar_con_agente", "consultar_estado_ticket", "activar_panico",
                             "iniciar_compra", "proceder_al_pago"]: # Agregadas intenciones de compra/pánico
                # Mensaje específico para funciones que requieren login
                return {
                    "respuesta": (
                        "Para esta acción (como registrar reclamos, chatear con un agente, activar alertas, o realizar compras) "
                        "necesitás iniciar sesión o registrarte. ¿Te gustaría hacerlo ahora?"
                    ),
                    "botones": [
                        {"texto": "Iniciar Sesión", "action": "login"},
                        {"texto": "Registrarme Gratis", "action": "register"},
                        {"texto": "No, gracias (info general)"}, # Opción para seguir como invitado si no quiere
                    ],
                }

            # Para cualquier otra consulta general de un usuario anónimo, ofrecer registro de forma más suave.
            # Esto solo se ejecuta si los handlers de saludo/cortesía no respondieron.
            return {
                "respuesta": (
                    "¡Hola! Soy tu asistente digital. Para darte una atención más completa y personalizada, "
                    "te recomiendo registrarte o iniciar sesión. "
                    "¿Querés continuar como invitado y solo consultar información general por ahora?"
                ),
                "botones": [
                    {"texto": "Iniciar Sesión", "action": "login"},
                    {"texto": "Registrarme Gratis", "action": "register"},
                    {"texto": "Continuar como invitado"},
                ],
            }

        return None # Si no es anónimo o ya fue manejado, no hace nada.


def crear_prompt_decision_herramienta(pregunta_usuario: str) -> str:
    descripcion_herramientas_json = {}
    for nombre, detalles in TOOL_REGISTRY.items():
        descripcion_herramientas_json[nombre] = {
            "descripcion": detalles["descripcion"],
            "parametros": detalles["parametros"],
        }
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
    # MODIFICADO: acepta payload
    def handle(self, payload: dict) -> dict | None:
        pregunta_str = payload.get("pregunta", "") # Extrae el string de la pregunta
        memoria = self.context.get("contexto_municipio", {})
        estado = memoria.get("estado_conversacion")
        
        # Bloquear si hay un flujo de reclamo en curso
        if estado and estado in RECLAMO_STATES:
            return None

        # Si el estado actual indica que estamos esperando un parámetro para una herramienta específica (ej: recolección)
        # Esto debería ser manejado por RecoleccionHandler.
        if estado == ConversationState.ESPERANDO_PARAM_RECOLECCION:
            return RecoleccionHandler(self.context).handle(payload) # Pasa el payload completo

        # Si no hay un estado activo, intentar clasificar con herramientas.
        prompt = crear_prompt_decision_herramienta(pregunta_str) # Pasa el string de la pregunta
        try:
            respuesta_llm_str = get_cohere_response(
                message=prompt,
                preamble="Sos experto en decidir si una pregunta requiere una herramienta. Respondé JSON o 'null'.",
            )
            logger.info(f"[ToolHandler] Decisión LLM Herramienta: {respuesta_llm_str.strip()}")

            if not respuesta_llm_str or respuesta_llm_str.strip().lower() == "null":
                return None # Ninguna herramienta aplica, dejar que otros handlers sigan

            decision = json.loads(respuesta_llm_str)
            nombre_herramienta = decision.get("herramienta")
            
            if not nombre_herramienta or nombre_herramienta not in TOOL_REGISTRY:
                return None # Herramienta no reconocida o no válida

            if "faltan_parametros" in decision:
                param_faltante = decision["faltan_parametros"][0]
                # Por ahora, solo manejamos "direccion" específicamente para recolección
                if param_faltante == "direccion" and nombre_herramienta == "consultar_recoleccion_por_direccion":
                    memoria["estado_conversacion"] = ConversationState.ESPERANDO_PARAM_RECOLECCION
                    return {
                        "respuesta": (
                            "¡Perfecto! Decime la dirección completa donde querés consultar el servicio municipal.\n"
                            f"{EJEMPLO_DIRECCION}"
                        )
                    }
                else:
                    # Para otros parámetros faltantes, podríamos pedirlo de forma más genérica
                    return {
                        "respuesta": f"Necesito más información para usar la herramienta de {nombre_herramienta.replace('_', ' ')}. ¿Podrías proveer el dato: {param_faltante}?",
                    }
            elif "parametros" in decision:
                parametros = decision["parametros"]
                funcion_a_ejecutar = TOOL_REGISTRY[nombre_herramienta]["funcion"]
                logger.info(
                    f"[ToolHandler] Ejecutando herramienta '{nombre_herramienta}' con parámetros: {parametros}"
                )
                resultado = funcion_a_ejecutar(**parametros)
                try:
                    resultado_dict = json.loads(resultado)
                    return resultado_dict
                except (json.JSONDecodeError, TypeError):
                    return {"respuesta": resultado}
        except json.JSONDecodeError:
            logger.error(f"[ToolHandler] Error al parsear JSON de respuesta LLM: {respuesta_llm_str}", exc_info=True)
            return None # Dejar que otro handler intente
        except Exception as e:
            logger.error(f"[ToolHandler] Error general en ToolHandler: {e}", exc_info=True)
            return {
                "respuesta": "Hubo un error técnico al intentar usar una herramienta. Por favor, probá de nuevo o comunicate con el municipio.",
                "botones": [{"texto": "Hablar con un agente"}],
            }
        return None


class HumanEscalationHandler(BaseMunicipioHandler):
    # MODIFICADO: acepta payload
    def handle(self, payload: dict) -> dict | None:
        pregunta_str = payload.get("pregunta", "") # Extrae el string de la pregunta
        # Este handler solo actúa si la intención es "hablar_con_agente" o si el LLM lo indica directamente.
        if self.context.get("intencion") != "hablar_con_agente":
            return None
            
        # Si el usuario es anónimo, pedir que se registre/inicie sesión
        if self.context.get("anon_id") and not self.context.get("cliente_id"): # Es anónimo
            return {
                "respuesta": (
                    "Para hablar con un agente y que podamos dar seguimiento personalizado a tu consulta, "
                    "necesitás iniciar sesión o registrarte. ¿Te gustaría hacerlo ahora?"
                ),
                "botones": [
                    {"texto": "Iniciar Sesión", "action": "login"},
                    {"texto": "Registrarme Gratis", "action": "register"},
                    {"texto": "No, gracias (continuar como invitado)"}
                ],
            }
        elif not self.context.get("cliente_id"): # No es anónimo (no tiene anon_id) pero tampoco tiene cliente_id (caso raro, podría ser owner_user sin ser viewer)
             return { # Fallback por si acaso, aunque anon_o_token_requerido debería manejar esto.
                "respuesta": "Para hablar con un agente, por favor inicia sesión.",
                "botones": [{"texto": "Iniciar Sesión", "action": "login"}]
            }


        logger.info(
            f"[HumanEscalationHandler] Usuario {self.context.get('cliente_id') or self.context.get('anon_id')} pide agente."
        )
        
        # Crear el ticket de escalación
        ticket_data = {
            "asunto": "Solicitud de Chat en Vivo",
            "categoria": "Atención en Vivo",
            "detalles": f"El vecino solicitó chat en vivo con la pregunta: '{pregunta_str}'",
            "user_id": self.context.get("cliente_id"),
            "municipio_id": getattr(self.context.get("user_obj"), "municipio_id", None),
            "estado": "esperando_agente_en_vivo",
            "ubicacion": self.context.get("ubicacion_usuario"),
        }
        
        try:
            sala_de_chat = servicio_tickets.crear_nuevo_ticket(
                tipo_ticket="municipio", ticket_data=ticket_data
            )
            
            if not sala_de_chat:
                raise Exception("No se pudo crear el ticket de sala de chat.")
            
            # Agregar el mensaje original del usuario como comentario en el ticket
            servicio_tickets.crear_comentario(
                ticket_id=sala_de_chat.id,
                tipo_ticket="municipio",
                comentario_data={
                    "comentario": pregunta_str, # Usa pregunta_str
                    "es_admin": False,
                    "user_id": self.context.get("cliente_id"),
                    "anon_id": self.context.get("anon_id"),
                },
            )
            
            logger.info(
                f"[HumanEscalationHandler] Sala de chat #{sala_de_chat.nro_ticket} creada."
            )
            
            self.context.get("contexto_municipio", {}).clear() # Limpiar contexto al escalar a un agente
            
            # Notificar al ADMIN_EMAIL ya se hace automáticamente cuando se crea el comentario del ticket.
            # El email que recibe el admin dirá:
            # Asunto: Nuevo ticket M-XXXXXX
            # Asunto (en cuerpo): Solicitud de Chat en Vivo
            # Categoría (en cuerpo): Atención en Vivo
            # Pregunta (en cuerpo): <El mensaje original del usuario pidiendo agente>

            return {
                "respuesta": (
                    f"Hemos recibido tu solicitud para hablar con un agente. Estamos notificando al equipo. "
                    f"Tu número de chat es **M-{sala_de_chat.nro_ticket}**. Un agente se unirá tan pronto como esté disponible. "
                    "Si la espera se prolonga, puedes intentar nuevamente o dejarnos un mensaje más detallado sobre tu consulta."
                ),
                "ticket_id": sala_de_chat.id,
                "botones": [ # Opcional: ofrecer botones para acciones mientras espera
                    {"texto": "Dejar un mensaje detallado"},
                    {"texto": "Ver estado de mi solicitud (M-" + str(sala_de_chat.nro_ticket) + ")"} 
                ]
            }
        except Exception as e:
            logger.error(f"[HumanEscalationHandler] Error al escalar a agente: {e}", exc_info=True)
            # Mensaje de fallback mejorado
            return {
                "respuesta": (
                    "Tuvimos un inconveniente al intentar conectar con un agente en este momento. "
                    "Por favor, ¿podrías intentar nuevamente en unos minutos? "
                    "Si prefieres, puedes dejarnos un mensaje con tu consulta y te contactaremos a la brevedad, "
                    "o llamar directamente al [telefono_municipio_o_empresa]." # TODO: Configurar este número de teléfono
                ),
                "botones": [
                    {"texto": "Intentar de nuevo"},
                    {"texto": "Dejar un mensaje"}
                    # Considerar añadir un botón para "Llamar ahora" si se tiene el número
                ],
            }
        return None

class VectorMunicipioCatalogHandler(BaseMunicipioHandler):
    # MODIFICADO: acepta payload
    def handle(self, payload: dict) -> dict | None:
        pregunta_str = payload.get("pregunta", "") # Extrae el string de la pregunta
        memoria = self.context.get("contexto_municipio", {})
        estado = memoria.get("estado_conversacion")
        if estado and estado in RECLAMO_STATES:
            return None
        
        user_obj = self.context.get("user_obj")
        if not user_obj:
            return None

        keywords_catalogo = [
            "oficina", "dependencia", "servicio", "centro", "hospital", "salud", "atención",
            "punto", "ubicación", "dónde queda", "cómo llego", "mapa", "dirección", "municipalidad", "delegación",
            "horario", "contacto"
        ]
        
        # Solo activa si hay palabras clave de catálogo y no hay un flujo activo
        if not any(kw in normalizar_texto(pregunta_str) for kw in keywords_catalogo) and not estado:
            return None

        try:
            query_filter = {"user_id": user_obj.id}
            if hasattr(user_obj, "municipio_id") and user_obj.municipio_id:
                query_filter["municipio_id"] = user_obj.municipio_id
                
            contenidos = SitioWebInfo.query.filter_by(**query_filter).all()
            
            dependencias = []
            for item in contenidos:
                datos = json.loads(item.datos_json)
                if datos.get("tipo") in ("dependencias", "oficinas", "servicios", "puntos_atencion"):
                    dependencias.extend(datos.get("items", []))
            
            if not dependencias:
                return None

            ubicacion_usuario = self.context.get("ubicacion_usuario")
            if ubicacion_usuario and isinstance(ubicacion_usuario, dict) and 'lat' in ubicacion_usuario and 'lon' in ubicacion_usuario:
                def distancia(dep):
                    lat, lon = dep.get("lat"), dep.get("lon")
                    if lat is not None and lon is not None:
                        # Usar la fórmula de distancia euclidiana para ordenar rápidamente
                        return (lat - ubicacion_usuario["lat"])**2 + (lon - ubicacion_usuario["lon"])**2
                    return float("inf")
                dependencias.sort(key=distancia)
            else:
                dependencias.sort(key=lambda d: d.get("nombre", ""))

            agrupadas = {}
            for dep in dependencias:
                cat = dep.get("categoria") or dep.get("tipo") or "Otros"
                agrupadas.setdefault(cat, []).append(dep)

            respuesta = ""
            for cat, deps in agrupadas.items():
                respuesta += f"\n🏢 **{cat.title()}**\n"
                for i, d in enumerate(deps):
                    if i >= 5: # Limitar a 5 por categoría para no saturar la respuesta inicial
                        break
                    nombre = d.get("nombre", "Dependencia sin nombre")
                    direccion = d.get("direccion", "Dirección no informada")
                    tel = d.get("telefono", "")
                    horario = d.get("horario", "")
                    ubicacion_coords = f"({d.get('lat', '')}, {d.get('lon', '')})" if d.get("lat") and d.get("lon") else ""
                    
                    respuesta += f"- **{nombre}** — {direccion} {ubicacion_coords}\n"
                    if tel:
                        respuesta += f"  Tel: {tel}\n"
                    if horario:
                        respuesta += f"  Horario: {horario}\n"
                if len(deps) > 5:
                    respuesta += f"  ...y {len(deps)-5} más en esta categoría. Podés preguntar por ellos.\n"

            botones = []
            if ubicacion_usuario:
                botones.append({"texto": "Ver en mapa", "action": "abrir_mapa"})
            botones.append({"texto": "Contactar municipio"})

            return {
                "respuesta": respuesta.strip() + "\n\n¿Necesitás más detalles o ver esto en un mapa?",
                "fuente": "catalogo_dependencias",
                "estado_respuesta": "mostrar_dependencias",
                "botones": botones
            }
        except Exception as e:
            logger.error(f"[VectorMunicipioCatalogHandler] Error: {e}", exc_info=True)
            return None


class TramiteInteligenteHandler(BaseMunicipioHandler):
    """
    Detecta trámites por scraping, fuzzy match, y muestra requisitos, pasos, costos, links y botones.
    """
    # MODIFICADO: acepta payload
    def handle(self, payload: dict) -> dict | None:
        pregunta_str = payload.get("pregunta", "") # Extrae el string de la pregunta
        memoria = self.context.get("contexto_municipio", {})
        estado = memoria.get("estado_conversacion")
        
        # Bloquear si hay un flujo de reclamo en curso
        if estado and estado in RECLAMO_STATES:
            return None
        
        intencion = self.context.get("intencion")
        keywords_tramite = [
            "requisito", "documento", "necesito", "cómo hago", "pasos", "turno", "costo", "precio",
            "arancel", "dónde", "lugar", "horario", "duración", "tramite", "trámite",
            "solicitar", "pedir", "obtener", "gestionar"
        ]
        
        # Activar si la intención es consultar_tramite o si la pregunta contiene palabras clave de trámite
        # y NO hay un estado de conversación activo (para evitar interferir con otros flujos)
        if not (intencion == "consultar_tramite" or any(kw in normalizar_texto(pregunta_str) for kw in keywords_tramite)) or estado:
            return None

        user_obj = self.context.get("user_obj")
        if not user_obj:
            return None

        try:
            query_filter = {"user_id": user_obj.id}
            if hasattr(user_obj, "municipio_id") and user_obj.municipio_id:
                query_filter["municipio_id"] = user_obj.municipio_id

            contenidos = SitioWebInfo.query.filter_by(**query_filter).all()
            
            tramites_scraped = []
            for item in contenidos:
                datos = json.loads(item.datos_json)
                if datos.get("tipo") == "tramites":
                    tramites_scraped.extend(datos.get("tramites", []))
            
            # Combinar con trámites cargados desde config.json si existen y no están duplicados
            for k, v in TRAMITES_INFO.items():
                if not any(t.get("nombre") == k for t in tramites_scraped):
                    tramites_scraped.append({"nombre": k, **v})

            if not tramites_scraped:
                return None

            pregunta_norm = normalizar_texto(pregunta_str)
            
            # Intentar un fuzzy match con todos los nombres de trámites disponibles
            nombres_tramites = [t.get("nombre", "").lower() for t in tramites_scraped]
            
            # Añadir sinónimos de trámites si existen
            from .sinonimos import TRAMITE_SYNONYMS
            for syn, real_name in TRAMITE_SYNONYMS.items():
                if real_name not in nombres_tramites: # Evitar duplicados
                    nombres_tramites.append(syn.lower())

            mejor_match = difflib.get_close_matches(pregunta_norm, nombres_tramites, n=1, cutoff=0.6) # Ajustar cutoff
            
            tramite_encontrado = None
            if mejor_match:
                matched_name = mejor_match[0]
                # Buscar el trámite original, ya sea por nombre directo o por su sinónimo
                for t in tramites_scraped:
                    if normalizar_texto(t.get("nombre", "")) == matched_name:
                        tramite_encontrado = t
                        break
                if not tramite_encontrado: # Si el match fue un sinónimo, buscar el trámite real
                    real_name = next((v for k, v in TRAMITE_SYNONYMS.items() if normalizar_texto(k) == matched_name), None)
                    if real_name:
                        for t in tramites_scraped:
                            if normalizar_texto(t.get("nombre", "")) == normalizar_texto(real_name):
                                tramite_encontrado = t
                                break

            # Fallback a búsqueda por palabras clave dentro del nombre del trámite si fuzzy match falla
            if not tramite_encontrado:
                for t in tramites_scraped:
                    if any(kw in normalizar_texto(t.get("nombre", "")) for kw in pregunta_norm.split() if len(kw) > 2):
                        tramite_encontrado = t
                        break
            
            if not tramite_encontrado:
                return None # No se encontró un trámite relevante

            nombre = tramite_encontrado.get("nombre", "Trámite")
            requisitos = tramite_encontrado.get("requisitos", "No informados")
            pasos = tramite_encontrado.get("pasos", "")
            costo = tramite_encontrado.get("costo", "Consultar")
            lugar = tramite_encontrado.get("lugar", "")
            horario = tramite_encontrado.get("horario", "")
            link = tramite_encontrado.get("link", "")
            
            respuesta = f"**Información sobre {nombre.title()}**\n"
            if requisitos:
                respuesta += f"\n**Requisitos:** {requisitos}\n"
            if pasos:
                respuesta += f"\n**Pasos a seguir:** {pasos}\n"
            if costo:
                respuesta += f"\n**Costo:** {costo}\n"
            if lugar:
                respuesta += f"\n**Lugar:** {lugar}\n"
            if horario:
                respuesta += f"\n**Horario:** {horario}\n"
            
            botones = []
            if link:
                botones.append({"texto": "Más información", "url": link})
            
            if "turno" in requisitos.lower() or "turno" in pasos.lower() or "turno" in pregunta_norm:
                 botones.append({"texto": "Sacar turno", "action": "sacar_turno"})
            
            botones.append({"texto": "Ver todos los trámites", "action": "ver_tramites"})
            
            return {
                "respuesta": respuesta.strip(),
                "fuente": "tramite_inteligente",
                "estado_respuesta": "mostrar_tramite",
                "botones": botones
            }
        except Exception as e:
            logger.error(f"[TramiteInteligenteHandler] Error: {e}", exc_info=True)
            return None


class ReclamoGeoHandler(BaseMunicipioHandler):
    """
    Este handler es auxiliar y solo procesa adjuntos (foto/ubicación) CUANDO el flujo
    ya está en ESPERANDO_ADJUNTOS_RECLAMO. La lógica principal de manejo de adjuntos
    se ha movido a `ReclamoHandler`.
    """
    # MODIFICADO: acepta payload
    def handle(self, payload: dict) -> dict | None:
        pregunta_str = payload.get("pregunta", "") # Extrae el string de la pregunta
        memoria = self.context.get("contexto_municipio", {})
        estado = memoria.get("estado_conversacion")
        
        # Solo se activa si el estado de conversación es el de espera de adjuntos
        if estado != ConversationState.ESPERANDO_ADJUNTOS_RECLAMO:
            return None
        
        # ReclamoHandler ya maneja esto. Este handler puede ser eliminado o fusionado.
        # Por robustez, lo dejamos para redirigir la llamada si es necesario.
        return ReclamoHandler(self.context).handle(payload) # Pasa el payload completo


def safe_llm_call(prompt, preamble, fallback=None):
    try:
        resp = get_cohere_response(message=prompt, preamble=preamble)
        if not resp or "no tengo información" in resp.lower() or "lo siento" in resp.lower():
            raise ValueError("Respuesta vacía o genérica del LLM")
        return resp
    except Exception as e:
        logger.error(f"[LLM_FALLBACK] Error en llamada a LLM: {e}", exc_info=True)
        return fallback or "No tengo información específica en este momento. ¿Te puedo ayudar con algo más?"

# --- Categorías válidas para reclamos ---
CATEGORIAS_RECLAMO = [
    "arbol caido",
    "arreglo de calle",
    "castracion de mascota",
    "falta de agua, rotura de caño",
    "fumigacion",
    "inspeccion de comercio",
    "limpieza",
    "luminaria",
    "riego de calle",
    "rotura de semaforo",
    "tramites de obras privadas",
    "otro motivo",
]
categorias_normalizadas = [normalizar_texto(c) for c in CATEGORIAS_RECLAMO]

# Definir los estados de reclamo para una mejor legibilidad y mantenimiento
RECLAMO_STATES = [
    ConversationState.ESPERANDO_CATEGORIA_RECLAMO,
    ConversationState.ESPERANDO_DIRECCION_RECLAMO,
    ConversationState.ESPERANDO_NOMBRE_VECINO,
    ConversationState.ESPERANDO_TELEFONO_VECINO,
    ConversationState.ESPERANDO_EMAIL_VECINO,
    ConversationState.ESPERANDO_DESCRIPCION_RECLAMO,
    ConversationState.ESPERANDO_ADJUNTOS_RECLAMO,
    ConversationState.ESPERANDO_CONFIRMACION_RECLAMO,
]


def serializar_enum(obj):
    if isinstance(obj, Enum):
        return obj.name
    elif isinstance(obj, dict):
        return {k: serializar_enum(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [serializar_enum(v) for v in obj]
    else:
        return obj


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
    "No, continuar": "sin_adjuntos",  # Renombrado para mayor claridad en el backend
    "Completar reclamo": "sin_adjuntos",
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
}


def responder_municipio(pregunta_original, owner_user, rubro_obj, viewer_user=None, anon_id=None, **kwargs):
    logger.info(f"[INICIO] Pregunta recibida: '{pregunta_original}'")
    
    # --- MODIFICACIÓN CRÍTICA AQUÍ ---
    # Combinar la pregunta original con los kwargs para formar un payload completo
    received_payload = {}
    if isinstance(pregunta_original, dict):  # Si el frontend envió un objeto JSON completo
        received_payload = pregunta_original
        pregunta_str = received_payload.get("pregunta", "")  # El texto principal del mensaje
    else:  # Si el frontend envió un string simple
        pregunta_str = pregunta_original
        received_payload["pregunta"] = pregunta_original  # Aseguramos que esté en el payload

    # Merge de kwargs (tienen prioridad)
    for key, value in kwargs.items():
        received_payload[key] = value

    # Recuperar contexto previo de sesión (si vino)
    contexto_previo = received_payload.get("contexto_previo", {})
    contexto_municipio = contexto_previo.get(CONTEXTO_MUNICIPIO, {})

    # Reconvertir string a Enum si hace falta
    estado_guardado_str = contexto_municipio.get("estado_conversacion")
    if estado_guardado_str and isinstance(estado_guardado_str, str):
        try:
            contexto_municipio["estado_conversacion"] = ConversationState[estado_guardado_str]
        except KeyError:
            logger.warning(f"[CONTEXTO] Estado inválido: {estado_guardado_str}. Se limpia.")
            contexto_municipio["estado_conversacion"] = None
    elif not isinstance(estado_guardado_str, ConversationState):
        contexto_municipio["estado_conversacion"] = None

    # Diccionario de contexto completo para handlers
    context = {
        "contexto_municipio": contexto_municipio,
        "user_obj": owner_user,
        "user_id": getattr(owner_user, "id", None),
        "cliente_id": getattr(viewer_user, "id", None),
        "anon_id": anon_id,
        "intencion": None,
        # Acciones y adjuntos
        "ubicacion_usuario": received_payload.get("ubicacion_usuario"),
        "foto_url": received_payload.get("archivo_url") if received_payload.get("es_foto") else None,
        "es_foto": received_payload.get("es_foto", False),
        "es_ubicacion": received_payload.get("es_ubicacion", False),
        "es_archivo": received_payload.get("es_archivo", False),
        "action": received_payload.get("action"),
    }

    # Detectar comando por texto del botón
    comando_from_text = BOTONES_COMANDOS_MUNICIPIO.get(pregunta_str.strip())
    if comando_from_text and not context.get("action"):
        context["action"] = comando_from_text
        received_payload["action"] = comando_from_text
        logger.info(f"[BOTON] Comando detectado: '{comando_from_text}' (desde texto del botón)")
    elif context.get("action"):
        logger.info(f"[BOTON] Comando detectado: '{context['action']}' (desde payload.action)")
    elif context.get("es_foto") or context.get("es_ubicacion"):
        logger.info(f"[ADJUNTO] Adjunto detectado: es_foto={context['es_foto']}, es_ubicacion={context['es_ubicacion']}")
        # Si se compartió ubicación específicamente para tiendas
        if context.get("es_ubicacion"):
            # Callback para ubicación de tiendas
            if memoria.get("intencion_pendiente_ubicacion") == "solicitar_ubicacion_tienda" and \
               memoria.get("estado_conversacion") == "ESPERANDO_UBICACION_PARA_TIENDAS":
                context["intencion"] = "solicitar_ubicacion_tienda"
                logger.info(f"[CONTEXTO] Ubicación recibida para tiendas, re-evaluando con intención: {context['intencion']}")
            # Callback para ubicación de pánico
            elif memoria.get("intencion_pendiente_ubicacion") == "activar_panico" and \
                 memoria.get("estado_conversacion") == ConversationState.ESPERANDO_UBICACION_PANICO: # Check against Enum member
                context["intencion"] = "activar_panico" # Forzar la intención para re-procesar con PanicButtonHandler
                logger.info(f"[CONTEXTO] Ubicación URGENTE recibida para PÁNICO, re-evaluando con intención: {context['intencion']}")


    estado_antes = context["contexto_municipio"].get("estado_conversacion")
    logger.info(f"[CONTEXTO] Estado previo: {estado_antes.name if estado_antes else 'None'}")

    handler_chain = [
        CancelHandler, 
        PoliteHandler, 
        SmallTalkHandler,
        PanicButtonHandler, # Added PanicButtonHandler with high priority
        IntentClassifierHandler,
        # Sales Handlers (New)
        ProductCatalogHandler,
        ProductInquiryHandler,
        CartHandler,
        CheckoutHandler,
        StoreLocationHandler, # Added StoreLocationHandler
        # End Sales Handlers
        HumanEscalationHandler, 
        TicketStatusHandler,
        SugerenciasVecinoHandler,
        RecoleccionHandler,
        ReclamoInteligenteMunicipioHandler,
        ReclamoHandler,
        TramitesHandler, 
        TramiteInteligenteHandler, 
        ImpuestosHandler, 
        ToolHandler, 
        VectorMunicipioCatalogHandler, 
        GeneralHandler, 
        EngancheAnonimoMunicipioHandler, 
        GreetingHandler, 
    ]

    respuesta_final = None

    for handler_class in handler_chain:
        try:
            handler_instance = handler_class(context)
            current_state_in_context = context["contexto_municipio"].get("estado_conversacion")

            # Los handlers de cortesía y cancelación se evalúan siempre primero
            if handler_class in [CancelHandler, PoliteHandler, SmallTalkHandler, GreetingHandler]:
                respuesta_parcial = handler_instance.handle(received_payload)
                if respuesta_parcial:
                    respuesta_final = respuesta_parcial
                    break
                continue

            # Si hay estado activo, solo responde el dueño del flujo
            if current_state_in_context:
                is_current_handler_owner = (
                    (isinstance(handler_instance, ReclamoHandler) and current_state_in_context in RECLAMO_STATES) or
                    (isinstance(handler_instance, TicketStatusHandler) and current_state_in_context.name.startswith("ESPERANDO_") and "TICKET" in current_state_in_context.name) or
                    (isinstance(handler_instance, RecoleccionHandler) and current_state_in_context == ConversationState.ESPERANDO_PARAM_RECOLECCION) or
                    (isinstance(handler_instance, TramitesHandler) and current_state_in_context in [ConversationState.ESPERANDO_SELECCION_TRAMITE, ConversationState.ESPERANDO_PREGUNTA_CURSO_LICENCIA])
                )
                if is_current_handler_owner:
                    logger.info(f"[HANDLER] Procesando con handler de estado activo: {handler_class.__name__} (Estado: {current_state_in_context.name})")
                    respuesta_parcial = handler_instance.handle(received_payload)
                    if respuesta_parcial:
                        respuesta_final = respuesta_parcial
                        break
                    else:
                        logger.warning(f"[HANDLER] Handler {handler_class.__name__} (estado activo) no respondió. Posible pregunta nueva.")
                        # Si no fue adjunto/acción explícita, chequeamos pregunta nueva
                        if not received_payload.get("es_foto") and not received_payload.get("es_ubicacion") and not received_payload.get("action"):
                            if es_pregunta_nueva(pregunta_str, "el dato solicitado"):
                                logger.info("[GUARDIAN] Pregunta nueva. Limpiando estado y re-evaluando intención.")
                                context["contexto_municipio"].clear()
                                context["intencion"] = None
                                respuesta_final = None
                                break
                        continue
                else:
                    logger.info(f"[HANDLER] Saltando {handler_class.__name__} (estado activo {current_state_in_context.name} no le corresponde).")
                    continue

            logger.info(f"[HANDLER] Procesando con {handler_class.__name__} (sin estado activo o es de inicio).")
            respuesta_parcial = handler_instance.handle(received_payload)
            if respuesta_parcial and isinstance(respuesta_parcial, dict):
                logger.info(f"[HANDLER] {handler_class.__name__} respondió correctamente.")
                respuesta_final = respuesta_parcial
                break
            else:
                logger.info(f"[HANDLER] {handler_class.__name__} no generó respuesta válida. Continuando.")
        except Exception as e:
            logger.error(f"[ERROR] Handler '{handler_class.__name__}' falló: {e}", exc_info=True)

    if not respuesta_final:
        logger.info("[RESPUESTA] No se encontró respuesta específica. Fallback general.")
        # Si hay estado de conversación activo y llega acá, limpiar todo y dar mensaje reinicio
        if contexto_municipio.get("estado_conversacion"):
            logger.error(f"[FALLBACK_ERROR] Fallback con estado activo: {contexto_municipio['estado_conversacion']}. Limpiando.")
            contexto_municipio.clear()
            respuesta_final = {
                "respuesta": (
                    "¡Vaya! Parece que nos perdimos un poco en la conversación. No te preocupes, empecemos de nuevo. "
                    "¿Cómo puedo ayudarte hoy? Aquí tienes algunas opciones comunes:"
                ),
                "botones": [
                    {"texto": "Hacer un reclamo"},
                    {"texto": "Dejar una sugerencia"},
                    {"texto": "Consultar estado de un ticket"},
                    {"texto": "Ver trámites disponibles"},
                    {"texto": "Hablar con un agente"},
                ],
            }
        else:
            respuesta_final = {
                "respuesta": (
                    "Disculpa, no estoy seguro de haber entendido bien tu consulta. A veces me cuesta un poquito. 😊\n"
                    "¿Podrías intentar reformular tu pregunta o elegir una de estas opciones para que pueda ayudarte mejor?"
                ),
                "botones": [
                    {"texto": "Hacer un reclamo"},
                    {"texto": "Dejar una sugerencia"},
                    {"texto": "Consultar estado de un ticket"},
                    {"texto": "Ver trámites disponibles"},
                    {"texto": "Hablar con un agente"},
                ],
            }

    # Serializar estado actualizado para frontend/session
    contexto_para_guardar = serializar_enum(context["contexto_municipio"])
    media_url_to_send = contexto_municipio.get("foto_url")
    location_data_to_send = contexto_municipio.get("ubicacion_gps")

    logger.info(f"[FIN] Respuesta final: '{respuesta_final.get('respuesta')}'")

    # Log anonymous conversation to Conversacion table
    if anon_id and not viewer_user and respuesta_final: # It's an anonymous user and we have a response
        try:
            # Log user's question part of the conversation
            db.session.add(Conversacion(
                session_id=anon_id, # anon_id is stored in session_id for anonymous
                pregunta=pregunta_str, # The original question string from the payload
                respuesta="", # Bot's response will be in the next entry
                fuente="municipio_anon_pregunta",
                rubro=context.get("rubro_obj").nombre if context.get("rubro_obj") else "municipio_general" # Get rubro name
            ))
            # Log bot's answer part of the conversation
            db.session.add(Conversacion(
                session_id=anon_id,
                pregunta=pregunta_str, # Repeat user question for context if desired, or keep it specific to bot's turn
                respuesta=respuesta_final.get("respuesta"),
                fuente=respuesta_final.get("fuente", "municipio_anon_respuesta"),
                rubro=context.get("rubro_obj").nombre if context.get("rubro_obj") else "municipio_general"
            ))
            db.session.commit()
            logger.info(f"Conversación anónima (municipio) para anon_id {anon_id} guardada.")
        except Exception as e_conv:
            logger.error(f"Error guardando conversación anónima de municipio para anon_id {anon_id}: {e_conv}", exc_info=True)
            db.session.rollback()

    return {
        "respuesta": respuesta_final.get("respuesta"),
        "botones": respuesta_final.get("botones", []),
        "contexto_actualizado": {CONTEXTO_MUNICIPIO: contexto_para_guardar},
        "ticket_id": respuesta_final.get("ticket_id", None),
        "media_url": media_url_to_send,
        "location_data": location_data_to_send
    }
