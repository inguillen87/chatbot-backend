import logging
import re
import random
import json
from enum import Enum, auto
from flask import session as flask_session

from services.cohere_ai import robust_chat
from models import Conversacion, db, ArchivoAdjunto
from services.qdrant_search import buscar_catalogo_qdrant, armar_respuesta_legible
from services.faq_matcher_spacy import buscar_en_faq_spacy
from services.utils_placeholders import reemplazar_placeholders
from services.utils import sugerencias_por_rubro
from services.logic import detectar_small_talk_con_llm, generar_respuesta_small_talk
from services.ticket_service import servicio_tickets
from services.webinfo import obtener_info_web

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


class PymeConversationState(Enum):
    IDLE = auto()
    ESPERANDO_PRODUCTO = auto()
    CONFIRMANDO_PEDIDO = auto()
    PEDIDO_FINALIZADO = auto()
    ESPERANDO_CONTACTO = auto()


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
        )
        botones_base = []
        if resultados:
            respuesta_legible = armar_respuesta_legible(resultados, max_items=5)
            mensaje = f"Estos son algunos productos que tenemos:\n{respuesta_legible}\n¿Te interesa alguno o querés ver más opciones?"
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
        estado = deserialize_state(ctx.get("estado_conversacion")) or PymeConversationState.IDLE
        texto = pregunta.lower()
        intentos = ctx.get("reintentos", 0)

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
                        {"texto": "Hablar con un agente", "action": "hablar_con_agente"},
                    ],
                }
            if "finalizar" in texto:
                ctx["estado_conversacion"] = serialize_state(PymeConversationState.CONFIRMANDO_PEDIDO)
                ctx["reintentos"] = 0
                flask_session[CONTEXTO_PYME] = ctx
                return {
                    "respuesta": "¿Confirmás tu pedido?",
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
                    ctx["estado_conversacion"] = serialize_state(PymeConversationState.IDLE)
                    ctx["reintentos"] = 0
                    flask_session[CONTEXTO_PYME] = ctx
                    return {
                        "respuesta": "No pude entender los productos. Cancelé el pedido para empezar de nuevo.",
                        "fuente": "pedido_cancelado",
                        "botones": [
                            {"texto": "Ver catálogo", "action": "ver_catalogo"},
                            {"texto": "Hablar con un agente", "action": "hablar_con_agente"},
                        ],
                    }
                return {
                    "respuesta": "No entendí el producto. Indicá nombre o código. Si querés cancelar, escribí 'cancelar'.",
                    "fuente": "producto_no_reconocido",
                }
            ctx["reintentos"] = 0
            flask_session[CONTEXTO_PYME] = ctx
            return {
                "respuesta": "Producto agregado. Indicá otro o escribí 'finalizar pedido'.",
                "fuente": "pedido_en_progreso",
                "estado_respuesta": "pyme_pregunta_pedido",
                "botones": [
                    {"texto": "Finalizar pedido", "action": "finalizar_pedido"},
                ],
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
                        {"texto": "Hablar con un agente", "action": "hablar_con_agente"},
                    ],
                }
            if texto.strip() in {"si", "sí", "confirmo", "confirmar"}:
                ctx["estado_conversacion"] = serialize_state(PymeConversationState.PEDIDO_FINALIZADO)
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
                        {"texto": "Hablar con un agente", "action": "hablar_con_agente"},
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

        ctx["estado_conversacion"] = serialize_state(PymeConversationState.ESPERANDO_PRODUCTO)
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
    def handle(self, pregunta):
        return {
            "respuesta": "Te paso con un agente humano. Aguardá un momento.",
            "fuente": "escalamiento_humano",
        }


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
    }

    intencion = _clasificar_intencion_pyme_con_llm(pregunta)
    logger.info(f"[PYME] Intención detectada: {intencion}")

    # Mapeo profesional de intents. Si la pregunta parece compra, SIEMPRE busca catálogo.
    INTENT_MAP = {
        "saludo": SaludoHandler,
        "ver_catalogo": CatalogoHandler,
        "consultar_ofertas": OfertasHandler,
        "iniciar_pedido": PedidoHandler,
        "pregunta_faq": FaqHandler,
        "hablar_con_agente": HumanHandler,
    }

    if detectar_small_talk_con_llm(pregunta):
        handler = SmallTalkHandler(context)
    else:
        sentimiento = analizar_sentimiento_llm(pregunta)
        if sentimiento in {"positivo", "negativo"}:
            handler = SentimentHandler(context, sentimiento)
        else:
            # Extra: si la intención es ambigua pero la pregunta contiene palabras de compra, forzá búsqueda en catálogo.
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

            if intencion == "pregunta_ambigua" and es_pregunta_compra:
                handler_cls = CatalogoHandler
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
        "contexto_actualizado": {CONTEXTO_PYME: context.get(CONTEXTO_PYME, {})},
    }
