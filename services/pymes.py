import logging
import re
import random
import json
from enum import Enum, auto
from flask import session as flask_session

from services.cohere_ai import robust_chat
from models import Conversacion, db, ArchivoAdjunto
from services.qdrant_search import (
    buscar_catalogo_qdrant,
    armar_respuesta_legible,
    formatear_tabla_catalogo,
)
from services.faq_matcher_spacy import buscar_en_faq_spacy
from services.utils_placeholders import reemplazar_placeholders
from services.utils import sugerencias_por_rubro
from services.logic import detectar_small_talk_con_llm, generar_respuesta_small_talk
from services.ticket_service import servicio_tickets
from services.webinfo import obtener_info_web
from services.preferences import add_preference

logger = logging.getLogger(__name__)

CONTEXTO_PYME = "contexto_pyme"
NOMBRE_HISTORIAL_SESION = "historial_chat_cliente_pyme"
MAX_HISTORIAL_CHAT = 30
CANCEL_KEYWORDS = {"cancel", "cancelar", "cancelalo", "anular", "borrar", "no gracias"}

# Prompt y helper para validar que el usuario realmente ingrese un producto
PROMPT_VALIDAR_PRODUCTO = """
¿El USUARIO menciona un producto o código y una cantidad para comprar? Responde
solo con SI o NO.

MENSAJE DEL USUARIO: "{texto}"
"""


def es_producto_valido_llm(texto: str) -> bool:
    """Utiliza el LLM para determinar si la entrada parece un producto."""
    try:
        decision = robust_chat(message=PROMPT_VALIDAR_PRODUCTO.format(texto=texto))
        if decision:
            return decision.strip().upper().startswith("SI")
    except Exception as e:
        logger.error(f"[PYME] Error validando producto con LLM: {e}")
    # Por defecto asumimos que es válido para no interrumpir el flujo
    return True


def tiene_archivo_catalogo(user_id: int) -> bool:
    """Verifica si el usuario tiene un catálogo cargado."""
    if not user_id:
        return False
    try:
        return (
            ArchivoAdjunto.query.filter_by(user_id=user_id, tipo="catalogo").first()
            is not None
        )
    except Exception:
        return False


def url_descargar_catalogo() -> str:
    """Construye el enlace absoluto al endpoint de descarga."""
    from flask import request

    base = request.url_root.rstrip("/")
    return f"{base}/catalogo/descargar"


def extraer_productos_llm(texto: str) -> list[dict]:
    """Utiliza el LLM para extraer productos y cantidades del texto."""
    prompt = (
        "Extrae producto y cantidad del MENSAJE y responde solo con JSON "
        "como [{'nombre': '...', 'cantidad': 1}].\n"
        f"MENSAJE: '{texto}'"
    )
    try:
        resp = robust_chat(message=prompt)
        datos = json.loads(resp or "[]")
        items: list[dict] = []
        if isinstance(datos, list):
            for it in datos:
                nombre = str(it.get("nombre", "")).strip()
                try:
                    cantidad = int(it.get("cantidad", 1))
                except (TypeError, ValueError):
                    continue
                if nombre:
                    items.append({"nombre": nombre, "cantidad": cantidad})
        return items
    except Exception:
        logger.exception("[PYME] Error usando LLM para extraer productos")
    return []


def extraer_productos(texto: str) -> list[dict]:
    """Intenta extraer pares cantidad/nombre de un texto simple."""
    partes = re.split(r",| y ", texto)
    items: list[dict] = []
    for p in partes:
        m = re.search(r"(\d+)[^a-zA-Z0-9]*(.+)", p.strip())
        if m:
            try:
                cantidad = int(m.group(1))
            except ValueError:
                continue
            nombre = m.group(2).strip()
            if nombre:
                items.append({"nombre": nombre, "cantidad": cantidad})
    if not items:
        items = extraer_productos_llm(texto)
    return items


def formatear_carrito(carrito: list[dict]) -> str:
    if not carrito:
        return "(vacío)"
    return "\n".join(f"{it['cantidad']} x {it['nombre']}" for it in carrito)


class PymeConversationState(Enum):
    IDLE = auto()
    ESPERANDO_PRODUCTO = auto()
    CONFIRMANDO_PEDIDO = auto()
    PEDIDO_FINALIZADO = auto()
    ESPERANDO_CONTACTO = auto()
    ESPERANDO_NUMERO_TICKET = auto()
    ESPERANDO_CONFIRMACION_CIERRE = auto()
    ESPERANDO_CALIFICACION = auto()


def serialize_state(state):
    return state.name if state else None


def deserialize_state(value):
    if not value:
        return None
    try:
        return PymeConversationState[value]
    except KeyError:
        return None


PROMPT_CLASIFICAR_INTENCION = """
Sos el cerebro comercial de un chatbot para una pyme. Analizá la PREGUNTA DEL USUARIO y respondé sólo con una de estas intenciones:
- saludo
- ver_catalogo
- consultar_ofertas
- iniciar_pedido
- pregunta_faq
- hablar_con_agente
- continuar_flujo
- consultar_estado_ticket
- pregunta_ambigua

PREGUNTA DEL USUARIO: "{pregunta_usuario}"
INTENCIÓN:
"""


def clasificar_intencion_llm(pregunta):
    prompt = PROMPT_CLASIFICAR_INTENCION.format(pregunta_usuario=pregunta)
    try:
        res = robust_chat(message=prompt)
        return res.strip().lower()
    except Exception as e:
        logger.error(f"[PYME] Error clasificando intención: {e}")
        return "pregunta_ambigua"


def _clasificar_intencion_pyme_con_llm(pregunta: str) -> str:
    """Versión explícita para compatibilidad con tests."""
    return clasificar_intencion_llm(pregunta)


def es_pregunta_nueva(texto_usuario: str, tipo_esperado: str) -> bool:
    """Determina con heurísticas simples y LLM si el usuario cambió de tema."""
    texto = texto_usuario.strip().lower()
    if texto in {"ok", "gracias"}:
        return True
    if any(kw in texto for kw in {"agente", "humano", "persona", "operador"}):
        return True
    if re.search(r"\d", texto) and tipo_esperado in {
        "el dato solicitado",
        "una dirección",
    }:
        return False
    prompt = (
        f"Analiza la RESPUESTA DEL USUARIO. El chatbot esperaba algo relacionado a: '{tipo_esperado}'.\n"
        f'RESPUESTA DEL USUARIO: "{texto_usuario}"\n'
        "Si responde lo esperado contestá 'RESPUESTA_VALIDA'. Si cambia de tema contestá 'PREGUNTA_NUEVA'."
    )
    try:
        decision = robust_chat(message=prompt)
        return "PREGUNTA_NUEVA" in decision
    except Exception as e:
        logger.error(f"[PYME] Error clasificando pregunta nueva: {e}")
        return False


def analizar_sentimiento_llm(texto: str) -> str:
    """Devuelve 'positivo', 'negativo' o 'neutral'"""
    prompt = (
        "Analiza el sentimiento del siguiente texto y responde solo 'positivo', 'negativo' o 'neutral'.\n"
        f"TEXTO: '{texto}'\nSENTIMIENTO:"
    )
    try:
        res = robust_chat(message=prompt)
        sentimiento = res.strip().lower()
        if sentimiento in {"positivo", "negativo"}:
            return sentimiento
        return "neutral"
    except Exception as e:
        logger.error(f"[PYME] Error analizando sentimiento: {e}")
        return "neutral"


# --- HANDLERS ---
class BaseHandler:
    def __init__(self, context):
        self.context = context

    def handle(self, pregunta):
        raise NotImplementedError


class SaludoHandler(BaseHandler):
    def handle(self, pregunta):
        nombre = self.context.get("nombre_pyme", "la empresa")
        return {
            "respuesta": f"¡Hola! Soy tu asistente para {nombre}. ¿En qué puedo ayudarte hoy?",
            "fuente": "saludo",
            "botones": [
                {"texto": "Ver catálogo", "action": "ver_catalogo"},
                {"texto": "Ver ofertas", "action": "ver_ofertas"},
            ],
        }


class CatalogoHandler(BaseHandler):
    def handle(self, pregunta):
        user_id = self.context.get("user_id")
        if not user_id:
            return {
                "respuesta": "Iniciá sesión para ver el catálogo.",
                "fuente": "catalogo_sin_login",
            }
        # Se busca SIEMPRE aunque la pregunta sea vaga
        resultados = buscar_catalogo_qdrant(
            user_id=user_id,
            pregunta=pregunta,
            categoria=self.context.get("rubro_nombre"),
            limite=50,
        )
        add_preference("busquedas", pregunta)
        botones_base = []
        if resultados:
            tabla = formatear_tabla_catalogo(resultados)
            mensaje = (
                "Estos son todos los productos disponibles en catálogo:\n\n"
                f"{tabla}\n\nSi querés ver otro producto o descargar el catálogo completo, avisame."
            )
            fuente = "catalogo_vector"
            botones_base = [
                {"texto": "Hacer un pedido", "action": "iniciar_pedido"},
                {"texto": "Ver catálogo completo", "action": "ver_catalogo"},
            ]
        else:
            mensaje = "No encontré productos que coincidan exactamente con tu búsqueda, pero mirá estas sugerencias:"
            fuente = "catalogo_vacio"
            botones_base = [
                {"texto": "Ver catálogo completo", "action": "ver_catalogo"},
                {"texto": "Hablar con un agente", "action": "hablar_con_agente"},
            ]

        if tiene_archivo_catalogo(user_id) and any(
            k in pregunta.lower() for k in ["descargar", "pdf"]
        ):
            botones_base.append(
                {"texto": "Descargar catálogo", "action": "descargar_catalogo"}
            )
            mensaje += (
                f"\n\nDescargá el catálogo completo aquí: {url_descargar_catalogo()}"
            )

        return {"respuesta": mensaje, "fuente": fuente, "botones": botones_base}


class OfertasHandler(BaseHandler):
    def handle(self, pregunta):
        return {
            "respuesta": "Ofertas de la semana: Combo Malbec 15% OFF, 2x1 en Espumante, Envío gratis desde $50.000.",
            "fuente": "ofertas",
            "botones": [
                {"texto": "Quiero el combo Malbec", "action": "iniciar_pedido"},
                {"texto": "Ver catálogo", "action": "ver_catalogo"},
            ],
        }


class SmallTalkHandler(BaseHandler):
    def handle(self, pregunta):
        respuesta = generar_respuesta_small_talk(pregunta)
        return {
            "respuesta": respuesta,
            "fuente": "smalltalk_pyme_llm",
        }


class SentimentHandler(BaseHandler):
    def __init__(self, context, sentimiento):
        super().__init__(context)
        self.sentimiento = sentimiento

    def handle(self, pregunta):
        if self.sentimiento == "negativo":
            return {
                "respuesta": "Lamentamos la experiencia. Te contactaré con un agente para ayudarte.",
                "fuente": "sentimiento_negativo",
                "botones": [
                    {"texto": "Hablar con un agente", "action": "hablar_con_agente"}
                ],
            }
        return {
            "respuesta": "¡Gracias por tu comentario!",
            "fuente": "sentimiento_positivo",
            "botones": [{"texto": "Ver ofertas", "action": "ver_ofertas"}],
        }


class PedidoHandler(BaseHandler):
    def handle(self, pregunta):
        ctx = self.context.setdefault(
            CONTEXTO_PYME, flask_session.get(CONTEXTO_PYME, {})
        )
        estado = (
            deserialize_state(ctx.get("estado_conversacion"))
            or PymeConversationState.IDLE
        )
        texto = pregunta.lower()
        intentos = ctx.get("reintentos", 0)
        carrito = ctx.setdefault("carrito", [])

        if estado == PymeConversationState.ESPERANDO_PRODUCTO:
            if any(k in texto for k in CANCEL_KEYWORDS):
                ctx.clear()
                ctx["estado_conversacion"] = serialize_state(PymeConversationState.IDLE)
                ctx["reintentos"] = 0
                flask_session[CONTEXTO_PYME] = ctx
                return {
                    "respuesta": "Pedido cancelado. ¿Necesitás otra cosa?",
                    "fuente": "pedido_cancelado",
                    "botones": [
                        {"texto": "Ver catálogo", "action": "ver_catalogo"},
                        {
                            "texto": "Hablar con un agente",
                            "action": "hablar_con_agente",
                        },
                    ],
                }
            if any(k in texto for k in ["mostrar", "carrito", "pedido"]):
                resumen = formatear_carrito(carrito)
                flask_session[CONTEXTO_PYME] = ctx
                return {
                    "respuesta": f"Tu pedido actual:\n{resumen}",
                    "fuente": "mostrar_pedido",
                    "botones": [
                        {"texto": "Finalizar pedido", "action": "finalizar_pedido"},
                    ],
                }
            if any(k in texto for k in ["sacar", "quitar", "eliminar"]):
                eliminado = False
                for item in list(carrito):
                    if item["nombre"].lower() in texto:
                        carrito.remove(item)
                        eliminado = True
                flask_session[CONTEXTO_PYME] = ctx
                if eliminado:
                    resumen = formatear_carrito(carrito)
                    return {
                        "respuesta": f"Producto eliminado. Carrito:\n{resumen}",
                        "fuente": "producto_eliminado",
                    }
                return {
                    "respuesta": "No identifiqué el producto a quitar.",
                    "fuente": "producto_no_encontrado",
                }
            if "cambiar" in texto:
                items = extraer_productos(texto)
                actualizado = False
                for it in items:
                    for c in carrito:
                        if c["nombre"].lower() in it["nombre"].lower():
                            c["cantidad"] = it["cantidad"]
                            actualizado = True
                flask_session[CONTEXTO_PYME] = ctx
                if actualizado:
                    return {
                        "respuesta": f"Actualizado. Carrito:\n{formatear_carrito(carrito)}",
                        "fuente": "producto_actualizado",
                    }
                return {
                    "respuesta": "No encontré el producto para cambiar.",
                    "fuente": "producto_no_encontrado",
                }
            if "finalizar" in texto:
                ctx["estado_conversacion"] = serialize_state(
                    PymeConversationState.CONFIRMANDO_PEDIDO
                )
                ctx["reintentos"] = 0
                flask_session[CONTEXTO_PYME] = ctx
                resumen = formatear_carrito(carrito)
                return {
                    "respuesta": f"Vas a confirmar este pedido:\n{resumen}\n¿Confirmás?",
                    "fuente": "confirmando_pedido",
                    "botones": [
                        {"texto": "Sí, confirmar", "action": "confirmar_pedido"},
                        {"texto": "Cancelar", "action": "cancelar_pedido"},
                    ],
                }
            if not es_producto_valido_llm(texto):
                intentos += 1
                ctx["reintentos"] = intentos
                flask_session[CONTEXTO_PYME] = ctx
                if intentos >= 3:
                    ctx.clear()
                    ctx["estado_conversacion"] = serialize_state(
                        PymeConversationState.IDLE
                    )
                    ctx["reintentos"] = 0
                    flask_session[CONTEXTO_PYME] = ctx
                    return {
                        "respuesta": "No pude entender los productos. Cancelé el pedido para empezar de nuevo.",
                        "fuente": "pedido_cancelado",
                        "botones": [
                            {"texto": "Ver catálogo", "action": "ver_catalogo"},
                            {
                                "texto": "Hablar con un agente",
                                "action": "hablar_con_agente",
                            },
                        ],
                    }
                return {
                    "respuesta": "No entendí el producto. Indicá nombre o código. Si querés cancelar, escribí 'cancelar'.",
                    "fuente": "producto_no_reconocido",
                }
            items = extraer_productos(texto)
            if items:
                carrito.extend(items)
                for it in items:
                    add_preference("productos", it.get("nombre", ""))
                ctx["reintentos"] = 0
                flask_session[CONTEXTO_PYME] = ctx
                return {
                    "respuesta": f"Agregado. Carrito:\n{formatear_carrito(carrito)}",
                    "fuente": "pedido_en_progreso",
                    "estado_respuesta": "pyme_pregunta_pedido",
                    "botones": [
                        {"texto": "Finalizar pedido", "action": "finalizar_pedido"},
                    ],
                }
            ctx["reintentos"] = 0
            flask_session[CONTEXTO_PYME] = ctx
            return {
                "respuesta": "No entendí el producto. Si querés cancelar, escribí 'cancelar'.",
                "fuente": "producto_no_reconocido",
            }

        if estado == PymeConversationState.CONFIRMANDO_PEDIDO:
            if any(k in texto for k in CANCEL_KEYWORDS):
                ctx.clear()
                ctx["estado_conversacion"] = serialize_state(PymeConversationState.IDLE)
                ctx["reintentos"] = 0
                flask_session[CONTEXTO_PYME] = ctx
                return {
                    "respuesta": "Pedido cancelado. ¿Necesitás otra cosa?",
                    "fuente": "pedido_cancelado",
                    "botones": [
                        {"texto": "Ver catálogo", "action": "ver_catalogo"},
                        {
                            "texto": "Hablar con un agente",
                            "action": "hablar_con_agente",
                        },
                    ],
                }
            if texto.strip() in {"si", "sí", "confirmo", "confirmar"}:
                ctx["estado_conversacion"] = serialize_state(
                    PymeConversationState.PEDIDO_FINALIZADO
                )
                ctx["reintentos"] = 0
                flask_session[CONTEXTO_PYME] = ctx
                return {
                    "respuesta": "¡Listo! Tu pedido fue registrado.",
                    "fuente": "pedido_finalizado",
                }
            intentos += 1
            ctx["reintentos"] = intentos
            flask_session[CONTEXTO_PYME] = ctx
            if intentos >= 3:
                ctx.clear()
                ctx["estado_conversacion"] = serialize_state(PymeConversationState.IDLE)
                ctx["reintentos"] = 0
                flask_session[CONTEXTO_PYME] = ctx
                return {
                    "respuesta": "No pudimos confirmar tu pedido y fue cancelado.",
                    "fuente": "pedido_cancelado",
                    "botones": [
                        {"texto": "Ver catálogo", "action": "ver_catalogo"},
                        {
                            "texto": "Hablar con un agente",
                            "action": "hablar_con_agente",
                        },
                    ],
                }
            return {
                "respuesta": "¿Confirmás tu pedido? Escribí 'sí' para confirmar o 'cancelar' para anular.",
                "fuente": "confirmando_pedido",
            }

        if estado == PymeConversationState.PEDIDO_FINALIZADO:
            ctx["estado_conversacion"] = serialize_state(PymeConversationState.IDLE)
            ctx["reintentos"] = 0
            flask_session[CONTEXTO_PYME] = ctx
            return {
                "respuesta": "Tu pedido ya fue finalizado. ¿Necesitás algo más?",
                "fuente": "pedido_finalizado",
            }

        ctx["estado_conversacion"] = serialize_state(
            PymeConversationState.ESPERANDO_PRODUCTO
        )
        ctx["reintentos"] = 0
        flask_session[CONTEXTO_PYME] = ctx
        return {
            "respuesta": "¿Qué producto y cuántas unidades querés pedir? Decime el nombre o el código. Cuando termines, escribí 'finalizar pedido'.",
            "fuente": "pedido",
            "estado_respuesta": "pyme_pregunta_pedido",
            "botones": [
                {"texto": "Agregar más productos", "action": "ver_catalogo"},
                {"texto": "Finalizar pedido", "action": "finalizar_pedido"},
            ],
        }


class FaqHandler(BaseHandler):
    def handle(self, pregunta):
        respuesta_faq = buscar_en_faq_spacy(pregunta, self.context.get("user_id"))
        if respuesta_faq:
            return {"respuesta": respuesta_faq, "fuente": "faq"}
        return None


class HumanHandler(BaseHandler):
    """Escala la conversación a un agente humano creando un ticket."""

    def handle(self, pregunta):
        # Requiere que el cliente esté autenticado para poder contactarlo luego
        if not self.context.get("cliente_id"):
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

        ticket_data = {
            "asunto": "Solicitud de Chat en Vivo",
            "categoria": "Atención en Vivo",
            "detalles": f"El cliente solicitó chat en vivo con la pregunta: '{pregunta}'",
            "user_id": self.context.get("cliente_id"),
            "rubro_id": self.context.get("rubro_id"),
            "anon_id": self.context.get("anon_id"),
            "estado": "esperando_agente_en_vivo",
        }

        try:
            sala_de_chat = servicio_tickets.crear_nuevo_ticket(
                tipo_ticket="pyme", ticket_data=ticket_data
            )
            if not sala_de_chat:
                raise Exception("No se pudo crear el ticket de sala de chat.")

            servicio_tickets.crear_comentario(
                ticket_id=sala_de_chat.id,
                tipo_ticket="pyme",
                comentario_data={
                    "comentario": pregunta,
                    "es_admin": False,
                    "user_id": self.context.get("cliente_id"),
                    "anon_id": self.context.get("anon_id"),
                },
            )

            self.context.get(CONTEXTO_PYME, {}).clear()
            return {
                "respuesta": (
                    f"¡Listo! Abrimos una sala de chat directa con el equipo.\n"
                    f"Tu número de chat es **P-{sala_de_chat.nro_ticket}**. Esperá, un agente se conecta en breve."
                ),
                "ticket_id": sala_de_chat.id,
                "fuente": "escalamiento_humano",
            }
        except Exception as e:
            logger.error(f"[HumanHandler] Error al escalar a agente: {e}", exc_info=True)
            return {
                "respuesta": "No pudimos conectar con un agente en este momento. Probá más tarde o llamá a la empresa.",
                "botones": [{"texto": "Hablar con un agente"}],
            }


class TicketStatusHandler(BaseHandler):
    def handle(self, pregunta):
        ctx = self.context.setdefault(
            CONTEXTO_PYME, flask_session.get(CONTEXTO_PYME, {})
        )
        estado = deserialize_state(ctx.get("estado_conversacion"))
        texto = pregunta.lower()

        if estado == PymeConversationState.ESPERANDO_CONFIRMACION_CIERRE:
            if texto in {"si", "sí", "yes", "y"}:
                ticket_id = ctx.get("ticket_id_activo")
                ticket = db.session.get(PymeTicket, ticket_id)
                if ticket:
                    ticket.estado = "resuelto"
                    db.session.commit()
                ctx["estado_conversacion"] = serialize_state(
                    PymeConversationState.ESPERANDO_CALIFICACION
                )
                return {"respuesta": "¡Excelente! ¿Podés calificar la atención recibida del 1 al 5?"}
            ctx.clear()
            return {"respuesta": "Dejamos el ticket abierto para seguimiento del equipo. ¿Necesitás algo más?"}

        if estado == PymeConversationState.ESPERANDO_CALIFICACION:
            if not re.fullmatch(r"[1-5]", texto.strip()):
                return {"respuesta": "Por favor, ingresa una calificación del 1 al 5."}
            ticket_id = ctx.get("ticket_id_activo")
            if ticket_id:
                servicio_tickets.crear_comentario(
                    ticket_id=ticket_id,
                    tipo_ticket="pyme",
                    comentario_data={
                        "comentario": f"Calificación: {texto}",
                        "es_admin": False,
                        "anon_id": self.context.get("anon_id"),
                    },
                )
            ctx.clear()
            return {
                "respuesta": "¡Gracias por tu calificación! ¿Te ayudo con algo más?",
                "botones": [{"texto": "Hablar con un agente", "action": "hablar_con_agente"}],
            }

        if estado == PymeConversationState.ESPERANDO_NUMERO_TICKET:
            match = re.search(r"\d{5,}", texto)
            if not match:
                return {"respuesta": "No entendí el número de ticket. ¿Podés repetirlo? Debe ser de al menos 5 dígitos."}
            numero = int(match.group(0))
            ticket = PymeTicket.query.filter_by(nro_ticket=numero).first()
            ctx.pop("estado_conversacion", None)
            if not ticket:
                return {"respuesta": f"No encontré ticket P-{numero}. Por favor verificá el número."}
            respuesta = (
                f"El ticket **P-{ticket.nro_ticket}** sobre '{getattr(ticket,'asunto','')}' "
                f"está en estado: **{ticket.estado.replace('_',' ').title()}**."
            )
            ultimo = (
                TicketComentario.query.filter_by(pyme_ticket_id=ticket.id, es_admin=True)
                .order_by(TicketComentario.fecha.desc())
                .first()
            )
            if ultimo:
                respuesta += f"\nÚltima actualización: *{ultimo.comentario}*"
            if ticket.estado == "en_proceso":
                ctx["estado_conversacion"] = serialize_state(
                    PymeConversationState.ESPERANDO_CONFIRMACION_CIERRE
                )
                ctx["ticket_id_activo"] = ticket.id
                return {
                    "respuesta": respuesta + "\n¿Se resolvió tu problema?",
                    "botones": [{"texto": "Sí, solucionado"}, {"texto": "No, aún no"}],
                }
            return {"respuesta": respuesta}

        if self.context.get("intencion") == "consultar_estado_ticket":
            match = re.search(r"\d{5,}", texto)
            if not match:
                ctx["estado_conversacion"] = serialize_state(
                    PymeConversationState.ESPERANDO_NUMERO_TICKET
                )
                return {"respuesta": "Para consultar el estado de un ticket, decime el número de ticket por favor."}
            numero = int(match.group(0))
            ticket = PymeTicket.query.filter_by(nro_ticket=numero).first()
            if not ticket:
                return {"respuesta": f"No encontré ticket P-{numero}. Por favor verificá el número."}
            respuesta = (
                f"El ticket **P-{ticket.nro_ticket}** sobre '{getattr(ticket,'asunto','')}' "
                f"está en estado: **{ticket.estado.replace('_',' ').title()}**."
            )
            ultimo = (
                TicketComentario.query.filter_by(pyme_ticket_id=ticket.id, es_admin=True)
                .order_by(TicketComentario.fecha.desc())
                .first()
            )
            if ultimo:
                respuesta += f"\nÚltima actualización: *{ultimo.comentario}*"
            if ticket.estado == "en_proceso":
                ctx["estado_conversacion"] = serialize_state(
                    PymeConversationState.ESPERANDO_CONFIRMACION_CIERRE
                )
                ctx["ticket_id_activo"] = ticket.id
                return {
                    "respuesta": respuesta + "\n¿Se resolvió tu problema?",
                    "botones": [{"texto": "Sí, solucionado"}, {"texto": "No, aún no"}],
                }
            return {"respuesta": respuesta}

        return None

class FallbackHandler(BaseHandler):
    def handle(self, pregunta):
        user_id = self.context.get("user_id")
        rubro = self.context.get("rubro_nombre")
        resultados = buscar_catalogo_qdrant(
            user_id=user_id, pregunta=pregunta, categoria=rubro
        )
        if resultados:
            respuesta_legible = armar_respuesta_legible(resultados, max_items=3)
            botones = [
                {"texto": "Ver catálogo completo", "action": "ver_catalogo"},
                {"texto": "Hablar con un agente", "action": "hablar_con_agente"},
            ]
            if tiene_archivo_catalogo(user_id) and any(
                k in pregunta.lower() for k in ["descargar", "pdf"]
            ):
                botones.append(
                    {"texto": "Descargar catálogo", "action": "descargar_catalogo"}
                )
                respuesta_legible += (
                    f"\n\nDescargá el catálogo aquí: {url_descargar_catalogo()}"
                )
            return {
                "respuesta": f"No estoy seguro de haber entendido, pero mirá estos productos recomendados:\n{respuesta_legible}\n¿Te interesa alguno?",
                "fuente": "fallback_catalogo",
                "botones": botones,
            }
        sugerencias = sugerencias_por_rubro(rubro)
        if sugerencias:
            return {
                "respuesta": f"{random.choice(sugerencias)} ¿Querés una oferta personalizada o ayuda para comprar?",
                "fuente": "fallback",
                "botones": [
                    {"texto": "Ver ofertas", "action": "ver_ofertas"},
                    {"texto": "Hablar con un agente", "action": "hablar_con_agente"},
                ],
            }
        info_web = (
            obtener_info_web(user_id, self.context.get("nombre_pyme"))
            if user_id
            else {}
        )
        if info_web:
            mensaje = ", ".join(f"{k}: {v}" for k, v in info_web.items())
            return {
                "respuesta": mensaje,
                "fuente": "llm_contextual_pyme",
            }
        return {
            "respuesta": "No entendí tu consulta. ¿Querés ver el catálogo o hablar con un agente?",
            "fuente": "fallback_generico",
            "botones": [
                {"texto": "Ver catálogo", "action": "ver_catalogo"},
                {"texto": "Hablar con un agente", "action": "hablar_con_agente"},
            ],
        }


# --- ROUTER PRINCIPAL ---
def responder_pyme(
    pregunta, owner_user, rubro_obj, viewer_user=None, anon_id=None, **kwargs
):
    """Procesa un mensaje del flujo pyme.

    Si ``owner_user`` es ``None`` pero ``viewer_user`` pertenece a una empresa,
    se utilizará ``viewer_user.empresa_id`` para acceder al catálogo. De esta
    forma, administradores y empleados pueden probar el chat sin que el bot les
    solicite iniciar sesión nuevamente.
    """

    if owner_user:
        user_id = getattr(owner_user, "id", None)
        nombre_pyme = getattr(owner_user, "nombre_empresa", "la empresa")
    elif viewer_user:
        user_id = getattr(viewer_user, "empresa_id", None) or getattr(
            viewer_user, "id", None
        )
        nombre_pyme = getattr(viewer_user, "nombre_empresa", "la empresa")
    else:
        user_id = None
        nombre_pyme = "la empresa"

    context = {
        "user_id": user_id,
        "nombre_pyme": nombre_pyme,
        "rubro_nombre": (
            getattr(rubro_obj, "nombre", "empresa").lower()
            if rubro_obj
            else "desconocido"
        ),
        "mensajes_previos": flask_session.get(NOMBRE_HISTORIAL_SESION, []),
        CONTEXTO_PYME: flask_session.get(CONTEXTO_PYME, {}),
        "cliente_id": getattr(viewer_user, "id", None),
        "anon_id": anon_id,
        "rubro_id": getattr(rubro_obj, "id", None) if rubro_obj else None,
    }

    intencion = _clasificar_intencion_pyme_con_llm(pregunta)
    logger.info(f"[PYME] Intención detectada: {intencion}")

    # Mapeo profesional de intents. Si la pregunta parece compra, SIEMPRE busca catálogo.
    INTENT_MAP = {
        "saludo": SaludoHandler,
        "ver_catalogo": CatalogoHandler,
        "consultar_ofertas": OfertasHandler,
        "iniciar_pedido": PedidoHandler,
        "continuar_flujo": PedidoHandler,
        "pregunta_faq": FaqHandler,
        "hablar_con_agente": HumanHandler,
        "hablar_con_agente_pyme": HumanHandler,
        "consultar_estado_ticket": TicketStatusHandler,
    }

    if detectar_small_talk_con_llm(pregunta):
        handler = SmallTalkHandler(context)
    else:
        palabras_compra = [
            "comprar",
            "vender",
            "precio",
            "tenés",
            "hay",
            "malbec",
            "oferta",
            "promo",
            "descuento",
            "unidades",
            "sku",
            "stock",
            "vino",
            "caja",
        ]
        es_pregunta_compra = any(pal in pregunta.lower() for pal in palabras_compra)

        if (
            intencion
            in {
                "ver_catalogo",
                "consultar_ofertas",
                "iniciar_pedido",
                "continuar_flujo",
                "consultar_estado_ticket",
            }
            or es_pregunta_compra
        ):
            if intencion == "pregunta_ambigua" and es_pregunta_compra:
                handler_cls = CatalogoHandler
            else:
                handler_cls = INTENT_MAP.get(intencion, FallbackHandler)
            handler = handler_cls(context)
        else:
            sentimiento = analizar_sentimiento_llm(pregunta)
            if sentimiento in {"positivo", "negativo"}:
                handler = SentimentHandler(context, sentimiento)
            else:
                handler_cls = INTENT_MAP.get(intencion, FallbackHandler)
                handler = handler_cls(context)

    respuesta_final = handler.handle(pregunta) or FallbackHandler(context).handle(
        pregunta
    )

    # Guardar historial sesión
    historial = flask_session.get(NOMBRE_HISTORIAL_SESION, [])
    historial.append({"role": "user", "content": pregunta})
    historial.append(
        {"role": "assistant", "content": respuesta_final.get("respuesta", "")}
    )
    flask_session[NOMBRE_HISTORIAL_SESION] = historial[-MAX_HISTORIAL_CHAT:]

    # Guardar conversación en DB (si hay user)
    try:
        if context["user_id"]:
            db.session.add(
                Conversacion(
                    user_id=context["user_id"],
                    pregunta=pregunta,
                    respuesta=respuesta_final.get("respuesta", ""),
                    fuente=respuesta_final.get("fuente", "desconocida"),
                    rubro=context["rubro_nombre"],
                )
            )
            db.session.commit()
    except Exception as e:
        logger.error(f"[PYMES] Error guardando conversación en DB: {e}")
        db.session.rollback()

    flask_session[CONTEXTO_PYME] = context.get(CONTEXTO_PYME, {})

    return {
        "respuesta": respuesta_final.get("respuesta", "Ocurrió un error."),
        "fuente": respuesta_final.get("fuente", "desconocida"),
        "botones": respuesta_final.get("botones", []),
        "estado_respuesta": respuesta_final.get("estado_respuesta"),
        "contexto_actualizado": {CONTEXTO_PYME: context.get(CONTEXTO_PYME, {})},
    }
