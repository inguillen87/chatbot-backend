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
import math

logger = logging.getLogger(__name__)
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
TWILIO_WHATSAPP_NUMBER = "whatsapp:+14155238886"
TWILIO_WHATSAPP_CONTENT_SID = os.environ.get("TWILIO_WHATSAPP_CONTENT_SID")

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
        logger.error("[NOTIFICACION WHATSAPP] Faltan credenciales de Twilio.")
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
        logger.error(f"[NOTIFICACION WHATSAPP] Error: {e}", exc_info=True)


def es_pregunta_nueva(texto_usuario: str, tipo_esperado: str) -> bool:
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
        logger.error(f"[Guardián de Flujo] Error: {e}")
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

    def handle(self, pregunta: str) -> dict | None:
        raise NotImplementedError


class GreetingHandler(BaseMunicipioHandler):
    def handle(self, pregunta: str) -> dict | None:
        memoria = self.context.get("contexto_municipio", {})
        texto = normalizar_texto(pregunta.strip("!.,?"))
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
    ]

    def handle(self, pregunta: str) -> dict | None:
        texto = normalizar_texto(pregunta)
        if any(kw in texto for kw in self.CANCEL_KEYWORDS):
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

    KEYWORDS = {"gracias", "ok", "ok gracias", "muchas gracias"}

    def handle(self, pregunta: str) -> dict | None:
        texto = normalizar_texto(pregunta)
        if texto in self.KEYWORDS:
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

    def handle(self, pregunta: str) -> dict | None:
        if detectar_small_talk_con_llm(pregunta):
            respuesta = generar_respuesta_small_talk(pregunta)
            return {
                "respuesta": respuesta,
                "fuente": "smalltalk_municipio_llm",
            }
        return None


class RecoleccionHandler(BaseMunicipioHandler):
    """Atiende consultas sobre recolección de residuos en cualquier momento."""

    KEYWORDS = ["basura", "recoleccion", "residuos", "basurero"]

    def handle(self, pregunta: str) -> dict | None:
        memoria = self.context.get("contexto_municipio", {})
        texto = normalizar_texto(pregunta)

        estado = memoria.get("estado_conversacion")
        if estado == ConversationState.ESPERANDO_PARAM_RECOLECCION:
            if es_pregunta_nueva(pregunta, "una dirección"):
                memoria.clear()
                return None
            memoria.clear()
            resultado = consultar_recoleccion_por_direccion(direccion=pregunta)
            if not resultado or "No" in resultado:
                return {
                    "respuesta": "No encontré información de recolección para esa dirección. Podés verificar en la web municipal.",
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

        if any(kw in texto for kw in self.KEYWORDS):
            memoria.clear()
            if direccion_es_valida(pregunta):
                resultado = consultar_recoleccion_por_direccion(direccion=pregunta)
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
            memoria["estado_conversacion"] = ConversationState.ESPERANDO_PARAM_RECOLECCION
            return {
                "respuesta": f"¿La dirección para consultar el horario de recolección?\n{EJEMPLO_DIRECCION}",
            }

        return None


class IntentClassifierHandler(BaseMunicipioHandler):
    KEYWORDS_AGENTE = [
        "agente",
        "humano",
        "persona",
        "representante",
        "operador",
        "empleado",
        "atención",
        "real",
        "chat real",
        "soporte",
        "ayuda humana",
        "hablar con alguien",
        "asesor",
        "consultor",
        "soporte técnico",
        "atender",
        "personal",
        "comunicarme", # Añadir sinónimos comunes
        "llamar",
        "contacto",
        "quiero hablar",
        "hablame con"
    ]
    # Otras palabras clave para reclamo, trámite, etc. si quieres un fallback rápido sin LLM
    KEYWORDS_RECLAMO = ["reclamo", "queja", "problema", "denuncia", "reportar"]
    KEYWORDS_TRAMITE = ["trámite", "tramite", "gestión", "consulta de trámite"]
    
    def handle(self, pregunta: str) -> dict | None:
        memoria = self.context.get("contexto_municipio", {})
        texto_normalizado = normalizar_texto(pregunta)

        # 1. Prioridad: AGENTE por keywords
        if any(kw in texto_normalizado for kw in self.KEYWORDS_AGENTE):
            self.context["intencion"] = "hablar_con_agente"
            memoria.clear()
            logger.info(f"[MUNICIPIO] Intención: hablar_con_agente (por palabra clave)")
            return None # <-- Aquí el handler devuelve None, para que el próximo handler lo procese

        # 2. Otras keywords (Reclamo, Trámite)
        if any(kw in texto_normalizado for kw in self.KEYWORDS_RECLAMO):
            self.context["intencion"] = "iniciar_reclamo"
            memoria.clear()
            logger.info(f"[MUNICIPIO] Intención: iniciar_reclamo (por palabra clave)")
            return None

        if any(kw in texto_normalizado for kw in self.KEYWORDS_TRAMITE):
            self.context["intencion"] = "consultar_tramite"
            memoria.clear()
            logger.info(f"[MUNICIPIO] Intención: consultar_tramite (por palabra clave)")
            return None

        # 3. Clasificación con LLM si no hubo match con keywords
        if not memoria.get("estado_conversacion"):
            intencion_llm = _clasificar_intencion_con_llm(pregunta)
            self.context["intencion"] = intencion_llm
        else:
            self.context["intencion"] = "continuar_flujo"

        logger.info(f"[MUNICIPIO] Intención (final): {self.context.get('intencion')}")
        return None # Siempre devuelve None, lo cual es correcto para este handler

class TicketStatusHandler(BaseMunicipioHandler):
    def handle(self, pregunta: str) -> dict | None:
        memoria = self.context.get("contexto_municipio", {})
        estado_conversacion = memoria.get("estado_conversacion")
        if estado_conversacion == ConversationState.ESPERANDO_CONFIRMACION_CIERRE:
            if es_pregunta_nueva(pregunta, "una confirmación (sí o no)"):
                memoria.clear()
                return None
            ticket_id = memoria.get("ticket_id_activo")
            ticket = db.session.get(MunicipioTicket, ticket_id)
            if "si" in normalizar_texto(pregunta):
                ticket.estado = "resuelto"
                db.session.commit()
                memoria["estado_conversacion"] = (
                    ConversationState.ESPERANDO_CALIFICACION
                )
                return {
                    "respuesta": "¡Excelente! ¿Podés calificar la atención recibida del 1 al 5?"
                }
            else:
                memoria.clear()
                return {
                    "respuesta": "Dejamos el ticket abierto para seguimiento del equipo. ¿Necesitás algo más?"
                }
        elif estado_conversacion == ConversationState.ESPERANDO_CALIFICACION:
            if es_pregunta_nueva(pregunta, "una calificación del 1 al 5"):
                memoria.clear()
                return None
            ticket_id = memoria.get("ticket_id_activo")
            servicio_tickets.crear_comentario(
                ticket_id=ticket_id,
                tipo_ticket="municipio",
                comentario_data={
                    "comentario": f"Calificación: {pregunta}",
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
        elif estado_conversacion == ConversationState.ESPERANDO_NUMERO_TICKET:
            if es_pregunta_nueva(pregunta, "un número de ticket"):
                memoria.clear()
                return None
            match = re.search(r"\d{5,}", pregunta)
            if not match:
                return {
                    "respuesta": "No entendí el número de ticket. ¿Podés repetirlo?"
                }
            numero = int(match.group(0))
            ticket = MunicipioTicket.query.filter_by(nro_ticket=numero).first()
            memoria.pop("estado_conversacion", None)
            if not ticket:
                return {"respuesta": f"No encontré ticket {match.group(0)}."}
            respuesta = f"El ticket **M-{ticket.nro_ticket}** sobre '{ticket.asunto}' está en estado: **{ticket.estado}**."
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
                    memoria["estado_conversacion"] = (
                        ConversationState.ESPERANDO_CONFIRMACION_CIERRE
                    )
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
        if self.context.get("intencion") == "consultar_estado_ticket":
            match = re.search(r"\d{5,}", pregunta)
            if not match:
                memoria["estado_conversacion"] = (
                    ConversationState.ESPERANDO_NUMERO_TICKET
                )
                return {"respuesta": "Decime el número de ticket que querés consultar."}
            ticket = MunicipioTicket.query.filter_by(
                nro_ticket=int(match.group(0))
            ).first()
            if not ticket:
                return {"respuesta": f"No encontré ticket {match.group(0)}."}
            respuesta = f"El ticket **M-{ticket.nro_ticket}** sobre '{ticket.asunto}' está en estado: **{ticket.estado}**."
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
                    memoria["estado_conversacion"] = (
                        ConversationState.ESPERANDO_CONFIRMACION_CIERRE
                    )
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


class ReclamoHandler(BaseMunicipioHandler):
    def build_detalles_memoria(self, memoria: dict) -> str:
        return (
            f"Consulta original: {memoria.get('pregunta_original', '')}\n"
            f"Categoría: {memoria.get('categoria_reclamo', '')}\n"
            f"Dirección: {memoria.get('direccion_reclamo', '')}\n"
            f"Nombre: {memoria.get('nombre_vecino', '')}\n"
            f"Teléfono: {memoria.get('telefono_vecino', '')}"
        )

    def handle(self, pregunta: str) -> dict | None:
        memoria = self.context.get("contexto_municipio", {})
        estado = memoria.get("estado_conversacion")

        if estado in [
            ConversationState.ESPERANDO_CATEGORIA_RECLAMO,
            ConversationState.ESPERANDO_DIRECCION_RECLAMO,
            ConversationState.ESPERANDO_NOMBRE_VECINO,
            ConversationState.ESPERANDO_TELEFONO_VECINO,
        ] and es_pregunta_nueva(pregunta, "el dato solicitado"):
            memoria.clear()
            return {
                "respuesta": (
                    "Veo que cambiaste de tema. No hay problema, "
                    "¿con qué otro trámite o consulta te ayudo?"
                ),
                "botones": [
                    {"texto": "Nuevo reclamo"},
                    {"texto": "Consultar estado de ticket"},
                    {"texto": "Hablar con un agente"},
                ],
            }

        if self.context.get("intencion") == "iniciar_reclamo" and not estado:
            memoria.clear()
            memoria["pregunta_original"] = pregunta
            categoria_adivinada = categorizar_reclamo_por_palabra_clave(pregunta)
            if categoria_adivinada != "Otros":
                memoria["estado_conversacion"] = (
                    ConversationState.ESPERANDO_DIRECCION_RECLAMO
                )
                memoria["categoria_reclamo"] = categoria_adivinada
                return {
                    "respuesta": (
                        f"Ok, el reclamo es sobre **{categoria_adivinada}**. ¿Me pasás la dirección exacta del problema?\n{EJEMPLO_DIRECCION}"
                    )
                }
            sugerencias = sugerir_categorias_relevantes(pregunta)
            if sugerencias:
                memoria["estado_conversacion"] = (
                    ConversationState.ESPERANDO_CATEGORIA_RECLAMO
                )
                return {
                    "respuesta": "¿Cuál opción representa mejor tu problema?",
                    "botones": [{"texto": s} for s in sugerencias]
                    + [{"texto": "Otro motivo"}],
                }
            memoria["estado_conversacion"] = (
                ConversationState.ESPERANDO_CATEGORIA_RECLAMO
            )
            return {
                "respuesta": "Seleccioná la categoría que mejor describa tu reclamo:",
                "botones": BOTONES_TODAS_CATEGORIAS + [{"texto": "Otro motivo"}],
            }

        if estado == ConversationState.ESPERANDO_CATEGORIA_RECLAMO:
            categoria_final = categorizar_reclamo_por_palabra_clave(pregunta)
            if categoria_final == "Otros":
                categoria_final = pregunta.strip().capitalize()
            memoria["categoria_reclamo"] = categoria_final
            memoria["estado_conversacion"] = (
                ConversationState.ESPERANDO_DIRECCION_RECLAMO
            )
            return {
                "respuesta": (
                    f"Perfecto, categoría: **{categoria_final}**. ¿La dirección exacta?\n{EJEMPLO_DIRECCION}"
                )
            }

        if estado == ConversationState.ESPERANDO_DIRECCION_RECLAMO:
            if not direccion_es_valida(pregunta):
                return {
                    "respuesta": f"No pude identificar una dirección válida. Ejemplo: {EJEMPLO_DIRECCION}"
                }
            memoria["direccion_reclamo"] = pregunta
            memoria["estado_conversacion"] = ConversationState.ESPERANDO_NOMBRE_VECINO
            return {"respuesta": "¡Gracias! Ahora tu nombre completo."}

        if estado == ConversationState.ESPERANDO_NOMBRE_VECINO:
            memoria["nombre_vecino"] = pregunta
            memoria["estado_conversacion"] = ConversationState.ESPERANDO_TELEFONO_VECINO
            return {
                "respuesta": f"Gracias, {pregunta}. ¿Me pasás tu teléfono con código de área?"
            }

        if estado == ConversationState.ESPERANDO_TELEFONO_VECINO:
            memoria["telefono_vecino"] = pregunta.strip()
            detalles = self.build_detalles_memoria(memoria)
            categoria = memoria.get("categoria_reclamo", "General")
            nombre = memoria.get("nombre_vecino", "")
            telefono_raw = memoria.get("telefono_vecino", "")
            telefono_limpio = re.sub(r"\D", "", telefono_raw)
            if not telefono_limpio.startswith("+") and len(telefono_limpio) > 8:
                telefono_e164 = "+549" + telefono_limpio
            else:
                telefono_e164 = telefono_limpio
            ticket = servicio_tickets.crear_nuevo_ticket(
                tipo_ticket="municipio",
                ticket_data={
                    "asunto": f"Reclamo de {categoria}",
                    "categoria": categoria,
                    "detalles": detalles,
                    "user_id": self.context.get("user_id"),
                },
            )
            memoria.clear()
            if ticket:
                enviar_notificacion_whatsapp_con_plantilla(
                    telefono_e164,
                    nombre,
                    ticket.nro_ticket,
                    categoria,
                )
                enviar_notificacion_sms(
                    telefono_e164,
                    f"Hola {nombre}! Tu reclamo M-{ticket.nro_ticket} ({categoria}) fue generado.",
                )
                return {
                    "respuesta": (
                        f"¡Listo! Tu reclamo fue generado con éxito. El número de ticket es **M-{ticket.nro_ticket}**. "
                        "Vas a recibir un mensaje con el detalle."
                    ),
                    "botones": [
                        {"texto": "Nuevo reclamo"},
                        {"texto": "Consultar estado de ticket"},
                        {"texto": "Hablar con un agente"},
                    ],
                    "ticket_id": ticket.id,
                }
            return {
                "respuesta": obtener_respuesta_municipio("reclamo_error"),
                "botones": [{"texto": "Hablar con un agente"}],
            }
        return None


def buscar_en_faqs(pregunta, tramite):
    if tramite not in MINI_FAQ_TRAMITES:
        return None
    pregunta_norm = normalizar_texto(pregunta)
    for item in MINI_FAQ_TRAMITES[tramite]:
        item_norm = normalizar_texto(item["q"])
        if all(token in pregunta_norm for token in item_norm.split()):
            return item
    return None


class TramitesHandler(BaseMunicipioHandler):
    def handle(self, pregunta: str) -> dict | None:
        memoria = self.context.get("contexto_municipio", {})
        estado = memoria.get("estado_conversacion")
        intencion = self.context.get("intencion")

        if intencion == "consultar_tramite" and not estado:
            memoria.clear()
            memoria["estado_conversacion"] = (
                ConversationState.ESPERANDO_SELECCION_TRAMITE
            )
            opciones = [{"texto": t.title()} for t in TRAMITES_INFO.keys()]
            return {
                "respuesta": "¿Sobre qué trámite necesitás información?",
                "botones": opciones,
            }

        if estado == ConversationState.ESPERANDO_SELECCION_TRAMITE:
            from .sinonimos import aplicar_sinonimos, TRAMITE_SYNONYMS, fuzzy_match

            texto = normalizar_texto(pregunta)
            texto = aplicar_sinonimos(texto, TRAMITE_SYNONYMS)

            clave_tramite = next(
                (k for k in TRAMITES_INFO.keys() if normalizar_texto(k) == texto),
                None,
            )

            if not clave_tramite:
                clave_tramite = fuzzy_match(list(TRAMITES_INFO.keys()) + list(TRAMITE_SYNONYMS.keys()), texto)
            if clave_tramite:
                memoria.clear()
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

            if re.search(r"(licencia|carnet).*conducir", texto):
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

            memoria.clear()
            opciones = [{"texto": t.title()} for t in TRAMITES_INFO.keys()]
            return {
                "respuesta": obtener_respuesta_municipio("tramite_no_encontrado"),
                "botones": opciones,
            }
        if estado == ConversationState.ESPERANDO_PREGUNTA_CURSO_LICENCIA:
            if es_pregunta_nueva(pregunta, "una pregunta sobre el curso de licencia"):
                memoria.clear()
                return {
                    "respuesta": "¿Sobre qué otro trámite querés info?",
                    "botones": [{"texto": "Licencia de Conducir"}],
                }
            respuesta_faq = buscar_en_faqs(pregunta, "licencia_de_conducir")
            if respuesta_faq:
                memoria.clear()
                respuesta = {"respuesta": respuesta_faq["a"]}
                if "botones" in respuesta_faq:
                    respuesta["botones"] = respuesta_faq["botones"]
                return respuesta
            memoria.clear()
            return {"respuesta": obtener_respuesta_municipio("curso_licencia_info")}

        return None


class ImpuestosHandler(BaseMunicipioHandler):
    def handle(self, pregunta: str) -> dict | None:
        intencion = self.context.get("intencion")
        if intencion == "consultar_impuestos":
            self.context.get("contexto_municipio", {}).clear()
            return {
                "respuesta": obtener_respuesta_municipio("impuestos_info"),
                "botones": obtener_respuesta_municipio("impuestos_botones"),
            }
        return None


class GeneralHandler(BaseMunicipioHandler):
    def handle(self, pregunta: str) -> dict | None:
        logger.info("[GeneralHandler] Consulta general con contexto de DB.")
        user_obj = self.context.get("user_obj")
        memoria = self.context.get("contexto_municipio", {})
        estado = memoria.get("estado_conversacion")

        if estado == ConversationState.ESPERANDO_PREGUNTA_CURSO_LICENCIA:
            respuesta_faq = buscar_en_faqs(pregunta, "licencia_de_conducir")
            if respuesta_faq:
                memoria.clear()
                respuesta = {"respuesta": respuesta_faq["a"]}
                if "botones" in respuesta_faq:
                    respuesta["botones"] = respuesta_faq["botones"]
                return respuesta
            memoria.clear()
            return {"respuesta": "¿Sobre qué más te puedo ayudar?"}

        if not user_obj:
            return None

        contexto_scraped = ""
        try:
            contenidos = SitioWebInfo.query.filter_by(user_id=user_obj.id).all()
            textos_relevantes = [
                json.loads(item.datos_json).get("contenido", "")
                for item in contenidos
                if json.loads(item.datos_json).get("tipo") == "contenido_general"
            ]
            contexto_scraped = " ".join(filter(None, textos_relevantes))
            if not contexto_scraped:
                contexto_scraped = "No hay información disponible para esta consulta."
        except Exception as e:
            logger.error(f"[GeneralHandler] Error: {e}")
            contexto_scraped = "Hubo un error al cargar la información."

        prompt_final = PROMPT_MUNICIPIO_CON_CONTEXTO.format(
            contexto_scraped=contexto_scraped, pregunta_usuario=pregunta
        )
        respuesta_llm = get_cohere_response(
            message=prompt_final,
            preamble="Sos un asistente municipal que responde basado en info oficial.",
        )

        if (
            not respuesta_llm
            or "no tengo información específica" in respuesta_llm.lower()
        ):
            return {
                "respuesta": (
                    "No encontré respuesta exacta, pero podés contactarnos por WhatsApp o hacer otra consulta."
                ),
                "botones": [
                    {"texto": "Ir a la Web", "url": "https://www.juninmendoza.gov.ar/"},
                    {"texto": "Enviar WhatsApp", "url": "https://wa.me/542634612777"},
                ],
            }
        return {"respuesta": respuesta_llm}


class EngancheAnonimoMunicipioHandler(BaseMunicipioHandler):
    def handle(self, pregunta: str) -> dict | None:
        if not self.context.get("user_id"):
            return {
                "respuesta": (
                    "¡Hola! Para poder darte atención completa (reclamos, seguimiento oficial), registrate o iniciá sesión. "
                    "¿Querés seguir como invitado? Solo podés consultar info general o iniciar sesión para más funciones."
                ),
                "botones": [
                    {"texto": "Iniciar sesión", "action": "login"},
                    {"texto": "Registrarme Gratis", "action": "register"},
                    {"texto": "Consultar info general"},
                ],
            }
        return None


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
    def handle(self, pregunta: str) -> dict | None:
        memoria = self.context.get("contexto_municipio", {})
        if (
            memoria.get("estado_conversacion")
            == ConversationState.ESPERANDO_PARAM_RECOLECCION
        ):
            if es_pregunta_nueva(pregunta, "una dirección"):
                memoria.clear()
                return {
                    "respuesta": (
                        "Noté que cambiaste de tema. Si querés volver a consultar la recolección, decime la dirección. "
                        "Si preferís hacer otra consulta, decime cómo te ayudo."
                    ),
                    "botones": [
                        {"texto": "Consultar recolección"},
                        {"texto": "Hacer un reclamo"},
                        {"texto": "Hablar con un agente"},
                    ],
                }
            memoria.clear()
            resultado = consultar_recoleccion_por_direccion(direccion=pregunta)
            if not resultado or "No encontrado" in resultado:
                return {
                    "respuesta": (
                        "No encontré información de recolección para esa dirección. Revisá si está bien escrita, o consultá directo al municipio."
                    ),
                    "botones": [
                        {"texto": "Volver a intentar"},
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
        if memoria.get("estado_conversacion"):
            return None

        prompt = crear_prompt_decision_herramienta(pregunta)
        try:
            respuesta_llm_str = get_cohere_response(
                message=prompt,
                preamble="Sos experto en decidir si una pregunta requiere una herramienta. Respondé JSON o 'null'.",
            )
            if not respuesta_llm_str or respuesta_llm_str.strip().lower() == "null":
                return {
                    "respuesta": (
                        "No tengo una herramienta directa para esa consulta, pero decime más detalles o elegí otra opción:"
                    ),
                    "botones": [
                        {"texto": "Hacer un reclamo"},
                        {"texto": "Consultar estado de un trámite"},
                        {"texto": "Hablar con un agente"},
                    ],
                }
            decision = json.loads(respuesta_llm_str)
            nombre_herramienta = decision.get("herramienta")
            if not nombre_herramienta or nombre_herramienta not in TOOL_REGISTRY:
                return None
            if "faltan_parametros" in decision:
                param_faltante = decision["faltan_parametros"][0]
                if param_faltante == "direccion":
                    memoria["estado_conversacion"] = (
                        ConversationState.ESPERANDO_PARAM_RECOLECCION
                    )
                    return {
                        "respuesta": (
                            "¡Perfecto! Decime la dirección completa donde querés consultar el servicio municipal.\n"
                            f"{EJEMPLO_DIRECCION}"
                        )
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
        except Exception as e:
            logger.error(f"[ToolHandler] Error: {e}", exc_info=True)
            return {
                "respuesta": "Hubo un error técnico. Probá de nuevo o comunicate con el municipio.",
                "botones": [{"texto": "Hablar con un agente"}],
            }
        palabras_clave_recoleccion = ["basurero", "recoleccion", "residuos", "basura"]
        if any(
            palabra in normalizar_texto(pregunta)
            for palabra in palabras_clave_recoleccion
        ):
            memoria["estado_conversacion"] = (
                ConversationState.ESPERANDO_PARAM_RECOLECCION
            )
            return {
                "respuesta": (
                    "¿La dirección para consultar el horario de recolección?\n"
                    f"{EJEMPLO_DIRECCION}"
                )
            }
        return None


class HumanEscalationHandler(BaseMunicipioHandler):
    def handle(self, pregunta: str) -> dict | None:
        if self.context.get("intencion") == "hablar_con_agente":
            # Si el usuario es anónimo, pedimos que se registre antes de
            # iniciar el chat en vivo. Esto asegura que podamos asociar el
            # ticket a un usuario válido y guardar su ubicación.
            if not self.context.get("cliente_id"):
                return {
                    "respuesta": (
                        "Para hablar con un agente y registrar tu reclamo, \n"
                        "necesitás iniciar sesión o registrarte."
                    ),
                    "botones": [
                        {"texto": "Iniciar sesión", "action": "login"},
                        {"texto": "Registrarme Gratis", "action": "register"},
                    ],
                }

            logger.info(
                f"[HumanEscalationHandler] Usuario {self.context.get('cliente_id')} pide agente."
            )
            ticket_data = {
                "asunto": "Solicitud de Chat en Vivo",
                "categoria": "Atención en Vivo",
                "detalles": f"El vecino solicitó chat en vivo: '{pregunta}'",
                "user_id": self.context.get("cliente_id"),
                "estado": "esperando_agente_en_vivo",
            }
            sala_de_chat = servicio_tickets.crear_nuevo_ticket(
                tipo_ticket="municipio", ticket_data=ticket_data
            )
            if not sala_de_chat:
                return {
                    "respuesta": "No pudimos conectar con un agente. Probá más tarde o llamá al municipio."
                }
            servicio_tickets.crear_comentario(
                ticket_id=sala_de_chat.id,
                tipo_ticket="municipio",
                comentario_data={
                    "comentario": pregunta,
                    "es_admin": False,
                    "user_id": self.context.get("user_id"),
                    "anon_id": self.context.get("anon_id"),   # <--- AGREGÁ ESTO

                },
            )
            logger.info(
                f"[HumanEscalationHandler] Sala de chat #{sala_de_chat.nro_ticket} creada."
            )
            self.context.get("contexto_municipio", {}).clear()
            return {
                "respuesta": (
                    f"¡Listo! Abrimos una sala de chat directa con el equipo.\n"
                    f"Tu número de chat es **M-{sala_de_chat.nro_ticket}**. Esperá, un agente se conecta en breve."
                ),
                "ticket_id": sala_de_chat.id,  # <---- Esto es FUNDAMENTAL para que el frontend lo siga!
            }
        return None

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
}
# ...existing imports...
import math

# ...existing code...

class VectorMunicipioCatalogHandler(BaseMunicipioHandler):
    """
    Muestra dependencias/oficinas/servicios municipales agrupados, ordenados y con botones de acción.
    Usa geolocalización si está disponible.
    """
    def handle(self, pregunta: str) -> dict | None:
        user_obj = self.context.get("user_obj")
        if not user_obj:
            return None

        keywords_catalogo = [
            "oficina", "dependencia", "servicio", "centro", "hospital", "salud", "atención",
            "punto", "ubicación", "dónde queda", "cómo llego", "mapa", "dirección", "municipalidad", "delegación"
        ]
        if not any(kw in pregunta.lower() for kw in keywords_catalogo):
            return None

        try:
            contenidos = SitioWebInfo.query.filter_by(user_id=user_obj.id).all()
            dependencias = []
            for item in contenidos:
                datos = json.loads(item.datos_json)
                if datos.get("tipo") in ("dependencias", "oficinas", "servicios"):
                    dependencias.extend(datos.get("items", []))
            if not dependencias:
                return None

            ubicacion_usuario = self.context.get("ubicacion_usuario")
            if ubicacion_usuario:
                def distancia(dep):
                    lat, lon = dep.get("lat"), dep.get("lon")
                    if lat is not None and lon is not None:
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
                for d in deps[:5]:
                    nombre = d.get("nombre", "Dependencia")
                    direccion = d.get("direccion", "Dirección no informada")
                    tel = d.get("telefono", "")
                    horario = d.get("horario", "")
                    ubicacion = f"({d.get('lat','')}, {d.get('lon','')})" if d.get("lat") and d.get("lon") else ""
                    respuesta += f"- **{nombre}** — {direccion} {ubicacion}\n"
                    if tel:
                        respuesta += f"  Tel: {tel}\n"
                    if horario:
                        respuesta += f"  Horario: {horario}\n"
                if len(deps) > 5:
                    respuesta += f"  ...y {len(deps)-5} más en esta categoría.\n"

            botones = [{"texto": "Ver en mapa", "action": "abrir_mapa"}]
            return {
                "respuesta": respuesta.strip() + "\n\n¿Querés ver la ubicación en el mapa o recibir indicaciones?",
                "fuente": "catalogo_dependencias",
                "estado_respuesta": "mostrar_dependencias",
                "botones": botones
            }
        except Exception as e:
            logger.error(f"[VectorMunicipioCatalogHandler] Error: {e}")
            return None

class TramiteInteligenteHandler(BaseMunicipioHandler):
    """
    Detecta trámites por scraping, fuzzy match, y muestra requisitos, pasos, costos, links y botones.
    """
    def handle(self, pregunta: str) -> dict | None:
        memoria = self.context.get("contexto_municipio", {})
        intencion = self.context.get("intencion")
        estado = memoria.get("estado_conversacion")
        keywords_tramite = [
            "requisito", "documento", "necesito", "cómo hago", "pasos", "turno", "costo", "precio",
            "arancel", "dónde", "lugar", "horario", "duración", "tramite", "trámite"
        ]
        if not any(kw in pregunta.lower() for kw in keywords_tramite) and intencion != "consultar_tramite":
            return None
        if estado:
            return None

        user_obj = self.context.get("user_obj")
        try:
            contenidos = SitioWebInfo.query.filter_by(user_id=user_obj.id).all()
            tramites = []
            for item in contenidos:
                datos = json.loads(item.datos_json)
                if datos.get("tipo") == "tramites":
                    tramites.extend(datos.get("tramites", []))
            if not tramites:
                return None

            import difflib
            pregunta_norm = pregunta.lower()
            nombres_tramites = [t.get("nombre", "").lower() for t in tramites]
            mejor_match = difflib.get_close_matches(pregunta_norm, nombres_tramites, n=1, cutoff=0.5)
            tramite = None
            if mejor_match:
                idx = nombres_tramites.index(mejor_match[0])
                tramite = tramites[idx]
            else:
                for t in tramites:
                    if any(kw in pregunta_norm for kw in t.get("nombre", "").lower().split()):
                        tramite = t
                        break
            if not tramite:
                return None

            nombre = tramite.get("nombre", "Trámite")
            requisitos = tramite.get("requisitos", "No informados")
            pasos = tramite.get("pasos", "")
            costo = tramite.get("costo", "Consultar")
            lugar = tramite.get("lugar", "")
            horario = tramite.get("horario", "")
            link = tramite.get("link", "")
            respuesta = f"**{nombre.title()}**\n"
            if requisitos:
                respuesta += f"**Requisitos:** {requisitos}\n"
            if pasos:
                respuesta += f"**Pasos:** {pasos}\n"
            if costo:
                respuesta += f"**Costo:** {costo}\n"
            if lugar:
                respuesta += f"**Lugar:** {lugar}\n"
            if horario:
                respuesta += f"**Horario:** {horario}\n"
            if link:
                respuesta += f"[Más información]({link})\n"

            botones = [{"texto": "Sacar turno", "action": "sacar_turno"}] if "turno" in requisitos.lower() or "turno" in pasos.lower() else []
            botones.append({"texto": "Ver todos los trámites", "action": "ver_tramites"})
            return {
                "respuesta": respuesta.strip(),
                "fuente": "tramite_inteligente",
                "estado_respuesta": "mostrar_tramite",
                "botones": botones
            }
        except Exception as e:
            logger.error(f"[TramiteInteligenteHandler] Error: {e}")
            return None

class ReclamoGeoHandler(BaseMunicipioHandler):
    """
    Permite reclamos con ubicación GPS y adjuntar fotos, priorizando la resolución.
    """
    def handle(self, pregunta: str) -> dict | None:
        memoria = self.context.get("contexto_municipio", {})
        estado = memoria.get("estado_conversacion")
        intencion = self.context.get("intencion")
        if intencion != "iniciar_reclamo" and not any(kw in pregunta.lower() for kw in ["reclamo", "denuncia", "problema", "reportar", "queja"]):
            return None
        if estado:
            return None

        ubicacion = self.context.get("ubicacion_usuario")
        foto_url = self.context.get("foto_url")
        detalles = f"Reclamo recibido: {pregunta}\n"
        if ubicacion:
            detalles += f"Ubicación GPS: {ubicacion.get('lat')}, {ubicacion.get('lon')}\n"
        if foto_url:
            detalles += f"Foto adjunta: {foto_url}\n"

        categoria = "General"
        nombre = self.context.get("nombre_usuario", "Vecino/a")
        telefono = self.context.get("telefono_usuario", "")
        ticket = servicio_tickets.crear_nuevo_ticket(
            tipo_ticket="municipio",
            ticket_data={
                "asunto": f"Reclamo ciudadano",
                "categoria": categoria,
                "detalles": detalles,
                "user_id": self.context.get("user_id"),
            },
        )
        if ticket:
            if telefono:
                enviar_notificacion_sms(telefono, f"Hola {nombre}! Tu reclamo M-{ticket.nro_ticket} fue generado.")
            return {
                "respuesta": (
                    f"¡Listo! Tu reclamo fue generado con éxito. El número de ticket es **M-{ticket.nro_ticket}**. "
                    "¿Querés adjuntar una foto o compartir tu ubicación para agilizar la resolución?"
                ),
                "botones": [
                    {"texto": "Adjuntar foto", "action": "adjuntar_foto"},
                    {"texto": "Compartir ubicación", "action": "compartir_ubicacion"},
                    {"texto": "Nuevo reclamo"},
                    {"texto": "Consultar estado de ticket"},
                ],
                "ticket_id": ticket.id,
            }
        return None

def safe_llm_call(prompt, preamble, fallback=None):
    try:
        resp = get_cohere_response(message=prompt, preamble=preamble)
        if not resp or "no tengo información" in resp.lower():
            raise ValueError("Respuesta vacía o genérica")
        return resp
    except Exception as e:
        logger.error(f"[LLM_FALLBACK] Error: {e}")
        return fallback or "No tengo información específica, pero podés consultar al municipio o elegir otra opción."

# Contenido COMPLETO y FINAL de la función responder_municipio con las mejoras.
# Asume que todas las clases Handler y funciones auxiliares (como serializar_enum,
# normalizar_texto, etc.) están definidas en el mismo archivo o importadas correctamente.

def responder_municipio(pregunta, owner_user, rubro_obj, viewer_user=None, anon_id=None, **kwargs):
    contexto_previo = kwargs.get("contexto_previo", {})
    contexto_municipio = contexto_previo.get(CONTEXTO_MUNICIPIO, {})
    estado_guardado = contexto_municipio.get("estado_conversacion")
    if estado_guardado and isinstance(estado_guardado, str):
        try:
            contexto_municipio["estado_conversacion"] = ConversationState[
                estado_guardado
            ]
        except KeyError:
            logger.warning(f"Estado inválido en el contexto: {estado_guardado}")
            contexto_municipio["estado_conversacion"] = None
    context = {
        "contexto_municipio": contexto_municipio,
        "user_obj": owner_user,
        "user_id": getattr(owner_user, "id", None),
        "cliente_id": getattr(viewer_user, "id", None),
        "intencion": None,
    }
    # --- INTERCEPTA COMANDOS DE BOTONES ---
    comando = BOTONES_COMANDOS_MUNICIPIO.get(pregunta.strip())
    if comando:
        context["intencion"] = comando
        if comando == "iniciar_reclamo":
            # Directamente llama al handler si es por botón
            return ReclamoHandler(context).handle("Quiero hacer un reclamo")
        elif comando == "consultar_estado_ticket":
            # Directamente llama al handler si es por botón
            return TicketStatusHandler(context).handle("Consultar estado de ticket")
        elif comando == "hablar_con_agente":
            # Directamente llama al HumanEscalationHandler si es por botón
            return HumanEscalationHandler(context).handle(pregunta)

    # --- SIGUE EL FLUJO NORMAL ---
    estado_antes = contexto_municipio.get("estado_conversacion")
    handler_chain = [
        GreetingHandler,
        CancelHandler,
        PoliteHandler,
        SmallTalkHandler,
        IntentClassifierHandler,
        HumanEscalationHandler,
        VectorMunicipioCatalogHandler,      # NUEVO: catálogo de dependencias/servicios
        TramiteInteligenteHandler,          # NUEVO: trámites inteligentes
        ReclamoGeoHandler,                  # NUEVO: reclamos con ubicación/foto
        RecoleccionHandler,
        TicketStatusHandler,
        ReclamoHandler,
        TramitesHandler,
        ImpuestosHandler,
        ToolHandler,
        GeneralHandler,
        EngancheAnonimoMunicipioHandler,
    ]
    respuesta_final = None
    for handler_class in handler_chain:
        try:
            handler_instance = handler_class(context)
            respuesta_parcial = handler_instance.handle(pregunta)
            if respuesta_parcial:
                if not isinstance(respuesta_parcial, dict):
                    logger.error(f"[HANDLER_ERROR] Handler '{handler_class.__name__}' devolvió tipo incorrecto: {type(respuesta_parcial)}. Pregunta: '{pregunta}'")
                    respuesta_parcial = None 
                if respuesta_parcial:
                    respuesta_final = respuesta_parcial
                    break
        except Exception as e:
            logger.error(
                f"Error en handler {handler_class.__name__}: {e}", exc_info=True
            )
    if not respuesta_final:
        if not context.get("user_id"):
            respuesta_final = EngancheAnonimoMunicipioHandler(context).handle(pregunta)
        else:
            respuesta_final = {
                "respuesta": "No entendí tu consulta. Reformulá la pregunta o elegí una opción."
            }
    estado_despues = contexto_municipio.get("estado_conversacion")
    texto_respuesta = respuesta_final.get("respuesta", "")
    FRASES_EXITO = [
        "Tu reclamo fue generado",
        "Reclamo registrado",
        "¡Gracias por tu calificación!",
        "Dejamos el ticket abierto",
        "El curso de seguridad vial es online",
        "Abrimos una sala de chat directa",
        "Tu número de chat es",
    ]
    es_cierre_flujo = any(frase in texto_respuesta for frase in FRASES_EXITO)
    if estado_antes and not estado_despues and texto_respuesta and not es_cierre_flujo:
        respuesta_final["respuesta"] = texto_respuesta
    texto_respuesta_procesada = reemplazar_placeholders(
        respuesta_final.get("respuesta", ""), context.get("user_obj")
    )
    texto_respuesta_procesada = reemplazar_placeholders(texto_respuesta_procesada, contexto_municipio)
    respuesta_final["respuesta"] = texto_respuesta_procesada
    contexto_para_guardar = serializar_enum(context["contexto_municipio"])
    return {
        "respuesta": respuesta_final.get("respuesta"),
        "botones": respuesta_final.get("botones", []),
        "contexto_actualizado": {CONTEXTO_MUNICIPIO: contexto_para_guardar},
        "ticket_id": respuesta_final.get("ticket_id", None)
    }