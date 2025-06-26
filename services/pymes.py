import logging
import re
import random
import json
from enum import Enum, auto
from flask import session as flask_session

from services.cohere_ai import robust_chat
from models import Conversacion, db
from services.qdrant_search import buscar_catalogo_qdrant, armar_respuesta_legible
from services.faq_matcher_spacy import buscar_en_faq_spacy
from services.utils_placeholders import reemplazar_placeholders
from services.utils import sugerencias_por_rubro

logger = logging.getLogger(__name__)

# --- CONSTANTES Y SESIONES ---
NOMBRE_HISTORIAL_SESION = "historial_chat_cliente_pyme"
CONTEXTO_PYME_SESION = "contexto_pyme"
MAX_HISTORIAL_CHAT = 30

# --- ENUM DE ESTADOS (modificá o ampliá lo que realmente uses) ---
class PymeConversationState(Enum):
    SIN_ESTADO = auto()
    CONFIRMANDO_PEDIDO = auto()
    ESPERANDO_CONTACTO = auto()

def serialize_state(state: PymeConversationState | None) -> str | None:
    return state.name if state else None

def deserialize_state(value: str | None) -> PymeConversationState | None:
    if not value:
        return None
    try:
        return PymeConversationState[value]
    except KeyError:
        return None

# --- PROMPT DE CLASIFICACIÓN DE INTENCIÓN ---
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

def clasificar_intencion_llm(pregunta: str) -> str:
    prompt = PROMPT_CLASIFICAR_INTENCION.format(pregunta_usuario=pregunta)
    try:
        res = robust_chat(message=prompt)
        return res.strip().lower()
    except Exception as e:
        logger.error(f"[PYME] Error clasificando intención: {e}")
        return "pregunta_ambigua"

# --- HANDLERS ---

class BaseHandler:
    def __init__(self, context): self.context = context
    def handle(self, pregunta: str): raise NotImplementedError

class SaludoHandler(BaseHandler):
    def handle(self, pregunta):
        nombre = self.context.get('nombre_pyme', 'la empresa')
        return {
            "respuesta": f"¡Hola! Soy tu asistente para {nombre}. ¿En qué puedo ayudarte hoy?",
            "fuente": "saludo",
            "botones": [
                {"texto": "Ver catálogo", "action": "ver_catalogo"},
                {"texto": "Ver ofertas", "action": "ver_ofertas"},
            ]
        }

class CatalogoHandler(BaseHandler):
    def handle(self, pregunta):
        user_id = self.context.get('user_id')
        if not user_id:
            return {"respuesta": "Iniciá sesión para ver el catálogo.", "fuente": "catalogo_sin_login"}
        resultados = buscar_catalogo_qdrant(user_id=user_id, pregunta=pregunta, categoria=self.context.get('rubro_nombre'))
        if not resultados:
            return {"respuesta": "No encontré productos que coincidan con tu búsqueda.", "fuente": "catalogo_vacio"}
        respuesta_legible = armar_respuesta_legible(resultados)
        return {
            "respuesta": f"Aquí tenés los productos:\n{respuesta_legible}\n¿Querés iniciar un pedido?",
            "fuente": "catalogo_vector",
            "botones": [
                {"texto": "Hacer un pedido", "action": "iniciar_pedido"},
                {"texto": "Hablar con un agente", "action": "hablar_con_agente"},
            ]
        }

class OfertasHandler(BaseHandler):
    def handle(self, pregunta):
        return {
            "respuesta": "Estas son las ofertas de la semana: Combo Malbec 15% OFF, 2x1 en Espumante, Envío gratis desde $50.000.",
            "fuente": "ofertas",
            "botones": [
                {"texto": "Quiero el combo Malbec", "action": "iniciar_pedido"},
                {"texto": "Ver catálogo", "action": "ver_catalogo"},
            ]
        }

class PedidoHandler(BaseHandler):
    def handle(self, pregunta):
        return {
            "respuesta": "¿Qué producto y cuántas unidades querés pedir? Decime el nombre o el código. Cuando termines, escribí 'finalizar pedido'.",
            "fuente": "pedido",
            "botones": [
                {"texto": "Agregar más productos", "action": "ver_catalogo"},
                {"texto": "Finalizar pedido", "action": "finalizar_pedido"},
            ]
        }

class FaqHandler(BaseHandler):
    def handle(self, pregunta):
        respuesta_faq = buscar_en_faq_spacy(pregunta, self.context.get('user_id'))
        if respuesta_faq:
            return {"respuesta": respuesta_faq, "fuente": "faq"}
        return None

class HumanHandler(BaseHandler):
    def handle(self, pregunta):
        return {
            "respuesta": "Te paso con un agente humano. Aguardá un momento.",
            "fuente": "escalamiento_humano"
        }

class FallbackHandler(BaseHandler):
    def handle(self, pregunta):
        sugerencias = sugerencias_por_rubro(self.context.get('rubro_nombre'))
        if sugerencias:
            return {
                "respuesta": f"{random.choice(sugerencias)} ¿Querés una oferta personalizada o ayuda para comprar?",
                "fuente": "fallback",
                "botones": [
                    {"texto": "Ver ofertas", "action": "ver_ofertas"},
                    {"texto": "Hablar con un agente", "action": "hablar_con_agente"},
                ]
            }
        return {
            "respuesta": "No entendí tu consulta. ¿Querés ver el catálogo o hablar con un agente?",
            "fuente": "fallback_generico",
            "botones": [
                {"texto": "Ver catálogo", "action": "ver_catalogo"},
                {"texto": "Hablar con un agente", "action": "hablar_con_agente"},
            ]
        }

# --- ROUTER PRINCIPAL ---
def responder_pyme(pregunta, owner_user, rubro_obj, viewer_user=None, anon_id=None, **kwargs):
    context = {
        "user_id": getattr(owner_user, "id", None),
        "nombre_pyme": getattr(owner_user, "nombre_empresa", "la empresa"),
        "rubro_nombre": getattr(rubro_obj, "nombre", "empresa").lower() if rubro_obj else "desconocido",
        "mensajes_previos": flask_session.get(NOMBRE_HISTORIAL_SESION, [])
    }

    # Clasificá la intención
    intencion = clasificar_intencion_llm(pregunta)
    logger.info(f"[PYME] Intención detectada: {intencion}")

    # Mapear la intención a handler
    INTENT_MAP = {
        "saludo": SaludoHandler,
        "ver_catalogo": CatalogoHandler,
        "consultar_ofertas": OfertasHandler,
        "iniciar_pedido": PedidoHandler,
        "pregunta_faq": FaqHandler,
        "hablar_con_agente": HumanHandler,
    }
    handler_cls = INTENT_MAP.get(intencion, FallbackHandler)
    handler = handler_cls(context)
    respuesta_final = handler.handle(pregunta) or FallbackHandler(context).handle(pregunta)

    # Guardar historial sesión
    historial = flask_session.get(NOMBRE_HISTORIAL_SESION, [])
    historial.append({"role": "user", "content": pregunta})
    historial.append({"role": "assistant", "content": respuesta_final.get('respuesta','')})
    flask_session[NOMBRE_HISTORIAL_SESION] = historial[-MAX_HISTORIAL_CHAT:]

    # Guardar conversación en DB (si hay user)
    try:
        if context['user_id']:
            db.session.add(Conversacion(
                user_id=context['user_id'],
                pregunta=pregunta,
                respuesta=respuesta_final.get('respuesta', ''),
                fuente=respuesta_final.get('fuente', 'desconocida'),
                rubro=context['rubro_nombre']
            ))
            db.session.commit()
    except Exception as e:
        logger.error(f"[PYMES] Error guardando conversación en DB: {e}")
        db.session.rollback()

    return {
        "respuesta": respuesta_final.get('respuesta', "Ocurrió un error."),
        "fuente": respuesta_final.get('fuente', 'desconocida'),
        "botones": respuesta_final.get('botones', []),
        "contexto_actualizado": {},  # si querés pasar estado, agregalo acá
    }
