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
from services.utils import (
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
                    "¡Hola! 👋 Soy Chatboc, tu asistente digital del Municipio. "
                    "¿Querés hacer un reclamo, consultar un trámite o resolver una duda? ¡Contame en qué te ayudo!"
                )
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


    # MODIFICADO: acepta payload
    def handle(self, payload: dict) -> dict | None:
        pregunta_str = payload.get("pregunta", "") # Extrae el string de la pregunta
        logger.info(f"[INTENT] Analizando intención para: {pregunta_str}")
        memoria = self.context.get("contexto_municipio", {})
        texto_normalizado = normalizar_texto(pregunta_str)
        tokens = texto_normalizado.split()

        # Si ya hay un estado de conversación activo (ej: esperando una dirección para un reclamo),
        # no re-clasificamos la intención principal con keywords, porque el LLM ya lo hará si aplica.
        if memoria.get("estado_conversacion"):
            self.context["intencion"] = "continuar_flujo"
            logger.info(f"[MUNICIPIO] Intención: continuar_flujo (estado activo)")
            return None

        # Priorizar "hablar con agente"
        for kw in self.KEYWORDS_AGENTE:
            if kw in texto_normalizado:
                self.context["intencion"] = "hablar_con_agente"
                memoria.clear() # Limpiar memoria si el usuario quiere hablar con agente
                logger.info(f"[MUNICIPIO] Intención: hablar_con_agente (por keyword '{kw}')")
                return None

        # Priorizar "consultar estado de ticket"
        for kw in self.KEYWORDS_TICKET_STATUS:
            if kw in texto_normalizado:
                self.context["intencion"] = "consultar_estado_ticket"
                logger.info(f"[MUNICIPIO] Intención: consultar_estado_ticket (por keyword '{kw}')")
                return None


        # Luego, reclamo
        for kw in self.KEYWORDS_RECLAMO:
            if kw in texto_normalizado:
                self.context["intencion"] = "iniciar_reclamo"
                logger.info(f"[MUNICIPIO] Intención: iniciar_reclamo (por palabra clave '{kw}')")
                return None

        # Luego, trámite
        for kw in self.KEYWORDS_TRAMITE:
            if kw in texto_normalizado:
                self.context["intencion"] = "consultar_tramite"
                logger.info(f"[MUNICIPIO] Intención: consultar_tramite (por palabra clave '{kw}')")
                return None

        # Finalmente, clasificación con LLM si no hubo match con keywords y no hay estado activo
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
    Este handler maneja el flujo paso a paso de un reclamo.
    Solo se activará si el `ReclamoInteligenteMunicipioHandler` no pudo completar el proceso de una sola vez
    o si la intención inicial es explícitamente "iniciar_reclamo" y no se detectó suficiente información.
    """
    # MODIFICADO: acepta payload
    def handle(self, payload: dict) -> dict | None:
        pregunta_str = payload.get("pregunta", "") # Extrae el string de la pregunta
        memoria = self.context.get("contexto_municipio", {})
        estado = memoria.get("estado_conversacion")
        intencion = self.context.get("intencion")

        # SOLO activar este handler si la intención es "iniciar_reclamo"
        # y el estado actual es uno de los pasos del reclamo.
        # Si la intención es "iniciar_reclamo" y NO hay un estado de reclamo activo,
        # lo inicializamos en ESPERANDO_CATEGORIA_RECLAMO.
        if intencion == "iniciar_reclamo" and not estado:
            memoria.clear() # Limpiar memoria para un nuevo reclamo
            memoria["estado_conversacion"] = ConversationState.ESPERANDO_CATEGORIA_RECLAMO
            sugeridas = sugerir_categorias_relevantes(pregunta_str)
            
            botones = [{"texto": c.title()} for c in (sugeridas if sugeridas else CATEGORIAS_RECLAMO)]
            texto_respuesta = "Elegí la categoría del reclamo" if sugeridas else "¿Sobre qué categoría es tu reclamo?"
            
            return {
                "respuesta": texto_respuesta,
                "botones": botones
            }
        
        # Si el estado de conversación actual NO es uno de los estados de reclamo,
        # este handler no debe procesar, a menos que la intención sea iniciar reclamo
        # y no se haya inicializado el flujo aún (caso de arriba).
        if estado not in RECLAMO_STATES:
            return None


        # 1. Selección de categoría
        if estado == ConversationState.ESPERANDO_CATEGORIA_RECLAMO:
            texto_normalizado = normalizar_texto(pregunta_str)
            categoria_final = None

            # Intentar un match exacto con las categorías normalizadas
            if texto_normalizado in categorias_normalizadas:
                idx = categorias_normalizadas.index(texto_normalizado)
                categoria_final = CATEGORIAS_RECLAMO[idx]
            else:
                # Si no hay match exacto, intentar con fuzzy matching
                from difflib import get_close_matches
                matches = get_close_matches(texto_normalizado, categorias_normalizadas, n=1, cutoff=0.7)
                if matches:
                    idx = categorias_normalizadas.index(matches[0])
                    categoria_final = CATEGORIAS_RECLAMO[idx]

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
                respuesta_texto = "Esa categoría no es válida o no la entendí. Por favor, seleccioná una de las opciones o escribí una similar."
                if sugeridas:
                    respuesta_texto += "\nOpciones sugeridas:"
                return {
                    "respuesta": respuesta_texto,
                    "botones": botones
                }

        # 2. Dirección
        if estado == ConversationState.ESPERANDO_DIRECCION_RECLAMO:
            # Ahora kwargs ya incluye es_foto/es_ubicacion de payload
            if payload.get("es_foto") or payload.get("es_ubicacion"): # Acceder a payload
                return {
                    "respuesta": "Primero necesito la dirección exacta del problema (ejemplo: San Martín 123, Junín). Después vas a poder adjuntar foto o ubicación.",
                }
            if not direccion_es_valida(pregunta_str):
                return {
                    "respuesta": f"No pude identificar una dirección válida. Por favor, ingresa una dirección como: {EJEMPLO_DIRECCION}"
                }
            memoria["direccion_reclamo"] = pregunta_str.strip()
            memoria["estado_conversacion"] = ConversationState.ESPERANDO_NOMBRE_VECINO
            return {"respuesta": "¡Gracias! Ahora tu **nombre completo**."}

        # 3. Nombre
        if estado == ConversationState.ESPERANDO_NOMBRE_VECINO:
            nombre = pregunta_str.strip()
            if not nombre:
                return {"respuesta": "Por favor, ingresa tu nombre completo."}
            memoria["nombre_vecino"] = nombre
            memoria["estado_conversacion"] = ConversationState.ESPERANDO_TELEFONO_VECINO
            return {
                "respuesta": f"Gracias, {nombre}. ¿Me pasás tu **teléfono con código de área**?"
            }

        # 4. Teléfono
        if estado == ConversationState.ESPERANDO_TELEFONO_VECINO:
            telefono = pregunta_str.strip()
            if not validar_telefono(telefono):
                return {"respuesta": "El teléfono ingresado no parece válido. Ingresalo de nuevo (solo números)."}
            memoria["telefono_vecino"] = telefono
            memoria["estado_conversacion"] = ConversationState.ESPERANDO_EMAIL_VECINO
            return {"respuesta": "¿Cuál es tu **email**? (Te notificaremos el estado del reclamo)"}

        # 5. Email
        if estado == ConversationState.ESPERANDO_EMAIL_VECINO:
            email = pregunta_str.strip()
            if not validar_email(email):
                return {"respuesta": "El email ingresado no parece válido. Ingresalo nuevamente."}
            memoria["email_vecino"] = email
            memoria["estado_conversacion"] = ConversationState.ESPERANDO_DESCRIPCION_RECLAMO
            return {"respuesta": "Contame brevemente el **problema**. Podés adjuntar una foto o ubicación después."}

        # 6. Descripción
        if estado == ConversationState.ESPERANDO_DESCRIPCION_RECLAMO:
            descripcion = pregunta_str.strip()
            if not descripcion:
                return {"respuesta": "Por favor, describe brevemente el problema."}
            memoria["descripcion_reclamo"] = descripcion
            memoria["estado_conversacion"] = ConversationState.ESPERANDO_ADJUNTOS_RECLAMO
            return {
                "respuesta": "¿Querés **adjuntar una foto o compartir tu ubicación**?",
                "botones": [
                    {"texto": "Adjuntar foto", "action": "adjuntar_foto"},
                    {"texto": "Compartir ubicación", "action": "compartir_ubicacion"},
                    {"texto": "No, continuar", "action": "sin_adjuntos"}
                ]
            }

        # 7. Adjuntos (solo en este paso podés aceptar foto/ubicación)
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
                    "respuesta": f"¿Confirmás el reclamo con estos datos?\n{resumen}",
                    "botones": [
                        {"texto": "Confirmar reclamo", "action": "confirmar_reclamo"},
                        {"texto": "Editar datos", "action": "editar_reclamo"}
                    ]
                }

            if accion == "adjuntar_foto":
                return {
                    "respuesta": "Enviá la imagen ahora y la adjuntaré al reclamo.",
                    "botones": [
                        {"texto": "No, continuar", "action": "sin_adjuntos"}
                    ]
                }

            if accion == "compartir_ubicacion":
                return {
                    "respuesta": "Compartí tu ubicación actual desde el dispositivo.",
                    "botones": [
                        {"texto": "No, continuar", "action": "sin_adjuntos"}
                    ]
                }
            
            # Procesar adjuntos si el usuario los envía directamente o presiona los botones
            if payload.get("es_foto") and payload.get("archivo_url"): # Acceder a payload
                memoria["foto_url"] = payload.get("archivo_url")
                logger.info(f"Foto adjunta: {memoria['foto_url']}")
            
            if payload.get("es_ubicacion") and payload.get("ubicacion_usuario"): # Acceder a payload
                memoria["ubicacion_gps"] = payload.get("ubicacion_usuario")
                logger.info(f"Ubicación adjunta: {memoria['ubicacion_gps']}")
            
            # Si ya hay adjuntos, avanza a confirmación
            if memoria.get("foto_url") or memoria.get("ubicacion_gps"):
                memoria["estado_conversacion"] = ConversationState.ESPERANDO_CONFIRMACION_RECLAMO
                resumen = self.build_detalles_memoria(memoria)
                return {
                    "respuesta": f"¡Adjuntos recibidos! ¿Confirmás el reclamo con estos datos?\n{resumen}",
                    "botones": [
                        {"texto": "Confirmar reclamo", "action": "confirmar_reclamo"},
                        {"texto": "Editar datos", "action": "editar_reclamo"}
                    ]
                }
            
            # Si no es "no continuar" y no hay adjunto, volver a pedir
            return {
                "respuesta": "No recibí ningún adjunto válido. ¿Querés adjuntar una foto o compartir tu ubicación?",
                "botones": [
                    {"texto": "Adjuntar foto", "action": "adjuntar_foto"},
                    {"texto": "Compartir ubicación", "action": "compartir_ubicacion"},
                    {"texto": "No, continuar", "action": "sin_adjuntos"}
                ]
            }

        # 8. Confirmación y creación de ticket
        if estado == ConversationState.ESPERANDO_CONFIRMACION_RECLAMO:
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
                    
                    if ticket:
                        telefono_e164 = formatear_telefono_e164(telefono_raw)
                        if telefono_e164: # Solo enviar si el teléfono es válido para E.164
                            enviar_notificacion_whatsapp_con_plantilla(
                                telefono_e164,
                                nombre,
                                ticket.nro_ticket,
                                categoria,
                            )
                            enviar_notificacion_sms(
                                telefono_e164,
                                f"Hola {nombre}! Tu reclamo M-{ticket.nro_ticket} ({categoria}) fue generado."
                            )
                        
                        memoria.clear() # Limpiar memoria después de crear el ticket
                        return {
                            "respuesta": (
                                f"¡Listo! Tu reclamo fue registrado con éxito. Número de ticket: **M-{ticket.nro_ticket}**. "
                                "Vas a recibir novedades por email o WhatsApp."
                            ),
                            "botones": [
                                {"texto": "Nuevo reclamo"},
                                {"texto": "Consultar estado de ticket"},
                            ],
                            "ticket_id": ticket.id
                        }
                    else:
                        raise Exception("Fallo la creación del ticket")

                except Exception as e:
                    logger.error(f"[ReclamoHandler] Error al guardar ticket: {e}", exc_info=True)
                    memoria.clear() # Limpiar memoria en caso de error para evitar bucles
                    return {
                        "respuesta": "Hubo un error técnico al registrar tu reclamo. Por favor, probá más tarde o comunicate con el municipio.",
                        "botones": [{"texto": "Hablar con un agente"}]
                    }

            elif accion in ["editar_datos", "editar", "no"]: # "no" como "no quiero confirmar, quiero editar"
                # Volver a pedir la descripción como punto de entrada para editar
                memoria["estado_conversacion"] = ConversationState.ESPERANDO_DESCRIPCION_RECLAMO
                return {
                    "respuesta": "¿Qué dato querés editar? Podés decirme 'quiero cambiar la dirección' o volver a ingresar la descripción si querés corregirla.",
                }
            else:
                return {
                    "respuesta": "Por favor, confirmá el reclamo o elegí editar los datos.",
                    "botones": [
                        {"texto": "Confirmar reclamo", "action": "confirmar_reclamo"},
                        {"texto": "Editar datos", "action": "editar_reclamo"}
                    ]
                }
        return None


def buscar_en_faqs(pregunta, tramite):
    if tramite not in MINI_FAQ_TRAMITES:
        return None
    pregunta_norm = normalizar_texto(pregunta)
    for item in MINI_FAQ_TRAMITES[tramite]:
        item_norm = normalizar_texto(item["q"])
        # Usa difflib.SequenceMatcher para un match más flexible
        if difflib.SequenceMatcher(None, pregunta_norm, item_norm).ratio() > 0.7:
             return item
        # Intenta también si todas las palabras clave de la pregunta normalizada están en el item
        if all(token in pregunta_norm for token in item_norm.split()):
            return item
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
            return None
        
        # Si la intención es claramente hacer un reclamo o hablar con agente, o consultar ticket,
        # entonces el enganche debe ser específico para esas acciones que requieren registro.
        intencion = self.context.get("intencion")
        if intencion in ["iniciar_reclamo", "hablar_con_agente", "consultar_estado_ticket"]:
            return {
                "respuesta": (
                    "Para poder asistirte con eso (registrar reclamos, chatear con un agente o consultar tickets), \n"
                    "necesitás iniciar sesión o registrarte. ¿Te gustaría hacerlo ahora?"
                ),
                "botones": [
                    {"texto": "Iniciar sesión", "action": "login"},
                    {"texto": "Registrarme Gratis", "action": "register"},
                    {"texto": "Consultar info general (como invitado)"},
                ],
            }

        # Para cualquier otra consulta general de un usuario anónimo, ofrecer registro.
        return {
            "respuesta": (
                "¡Hola! Soy tu asistente municipal. Para darte una atención completa, "
                "especialmente para reclamos o gestiones personalizadas, te recomiendo registrarte o iniciar sesión. "
                "¿Querés continuar como invitado y solo consultar información general?"
            ),
            "botones": [
                {"texto": "Iniciar sesión", "action": "login"},
                {"texto": "Registrarme Gratis", "action": "register"},
                {"texto": "Consultar info general"},
            ],
        }


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
        if not self.context.get("cliente_id"): # cliente_id es viewer_user.id
            return {
                "respuesta": (
                    "Para hablar con un agente y que podamos dar seguimiento a tu consulta, \n"
                    "necesitás iniciar sesión o registrarte. ¿Te gustaría hacerlo?"
                ),
                "botones": [
                    {"texto": "Iniciar sesión", "action": "login"},
                    {"texto": "Registrarme Gratis", "action": "register"},
                ],
            }

        logger.info(
            f"[HumanEscalationHandler] Usuario {self.context.get('cliente_id')} pide agente."
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
            return {
                "respuesta": (
                    f"¡Listo! Abrimos una sala de chat directa con el equipo.\n"
                    f"Tu número de chat es **M-{sala_de_chat.nro_ticket}**. Esperá, un agente se conecta en breve."
                ),
                "ticket_id": sala_de_chat.id,
            }
        except Exception as e:
            logger.error(f"[HumanEscalationHandler] Error al escalar a agente: {e}", exc_info=True)
            return {
                "respuesta": "No pudimos conectar con un agente en este momento. Probá más tarde o llamá al municipio.",
                "botones": [{"texto": "Hablar con un agente"}],
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
    if isinstance(pregunta_original, dict): # Si el frontend envió un objeto JSON completo en el campo 'pregunta'
        received_payload = pregunta_original
        pregunta_str = received_payload.get("pregunta", "") # El texto principal del mensaje
    else: # Si el frontend envió un string simple, la 'pregunta' es ese string
        pregunta_str = pregunta_original
        received_payload["pregunta"] = pregunta_original # Aseguramos que el texto principal esté en el payload
    
    # Combinar los kwargs adicionales de la llamada API con el payload recibido
    # (kwargs de la llamada a responder_municipio tienen prioridad sobre los del payload dentro del campo 'pregunta')
    for key, value in kwargs.items():
        received_payload[key] = value

    # Obtener el contexto de la sesión actual
    # Mover la inicialización de contexto_previo y contexto_municipio para que estén disponibles
    contexto_previo = received_payload.get("contexto_previo", {})
    contexto_municipio = contexto_previo.get(CONTEXTO_MUNICIPIO, {})

    estado_guardado_str = contexto_municipio.get("estado_conversacion")
    if estado_guardado_str and isinstance(estado_guardado_str, str):
        try:
            contexto_municipio["estado_conversacion"] = ConversationState[estado_guardado_str]
        except KeyError:
            logger.warning(f"[CONTEXTO] Estado inválido en el contexto: {estado_guardado_str}. Se reseteará.")
            contexto_municipio["estado_conversacion"] = None
    elif not isinstance(estado_guardado_str, ConversationState):
        contexto_municipio["estado_conversacion"] = None

    # Inicializar el diccionario 'context' CON TODOS los datos necesarios desde el principio
    context = {
        "contexto_municipio": contexto_municipio,
        "user_obj": owner_user,
        "user_id": getattr(owner_user, "id", None),
        "cliente_id": getattr(viewer_user, "id", None),
        "anon_id": anon_id,
        "intencion": None, # Esto se clasificará más adelante o se usará de la memoria
        
        # Datos de adjuntos y acciones extraídos directamente del received_payload
        "ubicacion_usuario": received_payload.get("ubicacion_usuario"),
        "foto_url": received_payload.get("archivo_url") if received_payload.get("es_foto") else None,
        "es_foto": received_payload.get("es_foto", False),
        "es_ubicacion": received_payload.get("es_ubicacion", False),
        "es_archivo": received_payload.get("es_archivo", False), # Assuming es_archivo comes with es_foto/es_ubicacion
        "action": received_payload.get("action"),
    }
    # --- FIN MODIFICACIÓN CRÍTICA ---

    # Si hay una acción de botón detectada por texto, sobreescribe si 'action' no vino en payload.
    comando_from_text = BOTONES_COMANDOS_MUNICIPIO.get(pregunta_str.strip())
    if comando_from_text and not context.get("action"):
        context["action"] = comando_from_text
        received_payload["action"] = comando_from_text
        logger.info(
            f"[BOTON] Comando detectado: '{comando_from_text}' (desde texto del botón)"
        )
    elif context.get("action"): # Si la acción ya vino en el payload
        logger.info(f"[BOTON] Comando detectado: '{context['action']}' (desde payload.action)")
    elif context.get("es_foto") or context.get("es_ubicacion"):
        logger.info(f"[ADJUNTO] Adjunto detectado: es_foto={context['es_foto']}, es_ubicacion={context['es_ubicacion']}")


    estado_antes = context["contexto_municipio"].get("estado_conversacion")
    logger.info(f"[CONTEXTO] Estado previo: {estado_antes.name if estado_antes else 'None'}")
    
    handler_chain = [
        CancelHandler, 
        PoliteHandler, 
        SmallTalkHandler, 
        IntentClassifierHandler, 
        
        HumanEscalationHandler, 
        TicketStatusHandler, 
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
            
            # Los handlers de cortesía y cancelación siempre se evalúan primero
            if handler_class in [CancelHandler, PoliteHandler, SmallTalkHandler, GreetingHandler]:
                # Pasamos el 'received_payload' completo
                respuesta_parcial = handler_instance.handle(received_payload) 
                if respuesta_parcial:
                    respuesta_final = respuesta_parcial
                    break
                continue # Continúa con el siguiente handler si no responde

            if current_state_in_context:
                is_current_handler_owner = \
                    (isinstance(handler_instance, ReclamoHandler) and current_state_in_context in RECLAMO_STATES) or \
                    (isinstance(handler_instance, TicketStatusHandler) and current_state_in_context.name.startswith("ESPERANDO_") and "TICKET" in current_state_in_context.name) or \
                    (isinstance(handler_instance, RecoleccionHandler) and current_state_in_context == ConversationState.ESPERANDO_PARAM_RECOLECCION) or \
                    (isinstance(handler_instance, TramitesHandler) and current_state_in_context in [ConversationState.ESPERANDO_SELECCION_TRAMITE, ConversationState.ESPERANDO_PREGUNTA_CURSO_LICENCIA])
                
                if is_current_handler_owner:
                    logger.info(f"[HANDLER] Procesando con handler de estado activo: {handler_class.__name__} (Estado: {current_state_in_context.name})")
                    
                    # Pasamos el 'received_payload' completo
                    respuesta_parcial = handler_instance.handle(received_payload) 
                    
                    if respuesta_parcial:
                        respuesta_final = respuesta_parcial
                        break
                    else: 
                        logger.warning(f"[HANDLER] Handler {handler_class.__name__} (estado activo) no respondió. Posible pregunta nueva.")
                        
                        # Si no fue un adjunto/acción explícita, entonces sí verificamos si es una pregunta nueva.
                        if not received_payload.get("es_foto") and not received_payload.get("es_ubicacion") and not received_payload.get("action"):
                            if es_pregunta_nueva(pregunta_str, "el dato solicitado"): 
                                logger.info("[GUARDIAN] Detectada PREGUNTA_NUEVA. Limpiando estado y re-evaluando intención.")
                                context["contexto_municipio"].clear()
                                context["intencion"] = None
                                respuesta_final = None
                                break 
                        continue
                else: 
                    logger.info(f"[HANDLER] Saltando {handler_class.__name__} (estado activo {current_state_in_context.name} no le corresponde).")
                    continue
            
            logger.info(f"[HANDLER] Procesando con {handler_class.__name__} (sin estado activo o es de inicio).")
            # Pasamos el 'received_payload' completo
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
        logger.info("[RESPUESTA] No se encontró respuesta. Usando fallback.")
        respuesta_final = {
            "respuesta": "Disculpa, no entendí tu consulta. Por favor, reformulá la pregunta o elegí una opción de las siguientes.",
            "botones": [
                {"texto": "Hacer un reclamo"},
                {"texto": "Consultar estado de un trámite"},
                {"texto": "Hablar con un agente"},
            ]
        }

    contexto_para_guardar = serializar_enum(context["contexto_municipio"])

    media_url_to_send = contexto_municipio.get("foto_url")
    location_data_to_send = contexto_municipio.get("ubicacion_gps")

    logger.info(f"[FIN] Respuesta final: '{respuesta_final.get('respuesta')}'")
    return {
        "respuesta": respuesta_final.get("respuesta"),
        "botones": respuesta_final.get("botones", []),
        "contexto_actualizado": {CONTEXTO_MUNICIPIO: contexto_para_guardar},
        "ticket_id": respuesta_final.get("ticket_id", None),
        "media_url": media_url_to_send,        
        "location_data": location_data_to_send 
    }
