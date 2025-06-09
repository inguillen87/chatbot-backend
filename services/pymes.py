# services/pymes.py

import logging
import re
import random
import json
import datetime
from flask import session as flask_session

from models import Conversacion, PymeTicket, TicketComentario, PymePedido, db
from services.utils_placeholders import reemplazar_placeholders
from services.utils import sugerencias_por_rubro
from services.cohere_ai import get_cohere_response
from services.vector_search import buscar_item_vectorizado
from services.faq_matcher_spacy import buscar_en_faq_spacy
from services.intent_matcher import buscar_en_intents
from services.ticket_service import servicio_tickets
from services.webinfo import obtener_info_web

logger = logging.getLogger(__name__)

# --- Constantes de Sesión ---
NOMBRE_HISTORIAL_SESION = "historial_chat_cliente"
CONTEXTO_PYME_SESION = "contexto_pyme" # Nombre de la clave en la "mochila"
MAX_HISTORIAL_CHAT = 14

# --- Funciones Auxiliares (sin cambios) ---
def _generar_asunto_con_llm(pregunta: str) -> str:
    try:
        prompt = f"Resume la siguiente consulta de un cliente en un título breve de 4 a 8 palabras para un ticket de soporte. La consulta es: '{pregunta}'"
        asunto = get_cohere_response(message=prompt, chat_history=[], preamble="Eres un experto en resumir consultas de clientes.")
        return asunto.strip().replace('"', '')
    except Exception as e:
        logger.error(f"[PYME] Error generando asunto con LLM: {e}")
        return (pregunta[:75] + '...') if len(pregunta) > 75 else pregunta

def _extraer_cantidades_con_llm(pregunta_cliente: str, productos_disponibles: list) -> list:
    nombres_productos = [p.get('nombre', '') for p in productos_disponibles]
    prompt = f"""
    Tu tarea es analizar la respuesta de un cliente y extraer los productos y cantidades que solicita, basándote en una lista de productos válidos.
    Tu respuesta DEBE SER ÚNICAMENTE un objeto JSON en formato de lista. Cada objeto debe tener "producto", "cantidad" y "unidad".
    Si no se especifica unidad (como 'caja'), usa 'unidad'.
    Asocia lo que pide el cliente con un producto de la lista de productos válidos.
    **Productos Válidos:** {nombres_productos}
    **Respuesta del Cliente:** "{pregunta_cliente}"
    **JSON de Salida:**
    """
    try:
        respuesta_llm = get_cohere_response(message=prompt, chat_history=[], preamble="Eres un asistente experto en procesar pedidos en formato JSON.")
        json_limpio = respuesta_llm.strip().replace("```json", "").replace("```", "")
        return json.loads(json_limpio)
    except Exception as e:
        logger.error(f"[PYMES] Error al extraer cantidades con LLM: {e}")
        return [{"error": "No se pudo procesar la solicitud", "texto_original": pregunta_cliente}]

# --- PATRÓN DE HANDLERS (ADAPTADO A MEMORIA MANUAL) ---

class BaseHandler:
    def __init__(self, context):
        self.context = context
    def handle(self, pregunta: str) -> dict | None:
        raise NotImplementedError

class LimitHandler(BaseHandler):
    def handle(self, pregunta: str) -> dict | None:
        if self.context['preguntas_usadas'] >= self.context['limite_preguntas']:
            return {"respuesta": "🔒 Límite de preguntas alcanzado. Actualizá tu plan para continuar.", "fuente": "sistema_limite"}
        return None

class FollowUpHandler(BaseHandler):
    def handle(self, pregunta: str) -> dict | None:
        contexto_pyme = self.context['contexto_pyme'] # <-- USA LA MOCHILA
        if 'esperando_detalles_reclamo' in contexto_pyme:
            ticket_id = contexto_pyme.pop('esperando_detalles_reclamo') # <-- Limpia la mochila
            servicio_tickets.crear_comentario(ticket_id=ticket_id, tipo_ticket="pyme", comentario_data={"comentario": pregunta, "user_id": self.context['user_id']})
            return {"respuesta": "Perfecto, he añadido tus comentarios al reclamo.", "fuente": "detalle_reclamo_agregado"}
        elif 'esperando_datos_reclamo_roto' in contexto_pyme:
            ticket_id = contexto_pyme.pop('esperando_datos_reclamo_roto') # <-- Limpia la mochila
            servicio_tickets.crear_comentario(ticket_id=ticket_id, tipo_ticket="pyme", comentario_data={"comentario": f"Info adicional del cliente: {pregunta}", "user_id": self.context['user_id']})
            return {"respuesta": "Recibido. Gracias por la información. Ya estamos procesando el envío de tu reemplazo.", "fuente": "datos_reemplazo_recibidos"}
        return None

class PedidoHandler(BaseHandler):
    def handle(self, pregunta: str) -> dict | None:
        contexto_pyme = self.context['contexto_pyme'] # <-- USA LA MOCHILA
        if 'detallando_pedido' in contexto_pyme:
            productos_para_pedido = contexto_pyme.pop('detallando_pedido') # <-- Limpia la mochila
            detalles_estructurados = _extraer_cantidades_con_llm(pregunta, productos_para_pedido)
            try:
                nro_pedido = f"P-{random.randint(10000, 99999)}"
                nuevo_pedido = PymePedido(user_id=self.context['user_id'], nro_pedido=nro_pedido, detalles=json.dumps(detalles_estructurados, indent=2, ensure_ascii=False), estado="pendiente")
                db.session.add(nuevo_pedido)
                db.session.commit()
                respuesta = (f"¡Pedido recibido! He generado tu orden con el número **{nro_pedido}** con los detalles que me indicaste.\nUn representante de ventas se pondrá en contacto contigo. ¡Muchas gracias!")
                return {"respuesta": respuesta, "fuente": "handler_pedido_confirmado_ia"}
            except Exception as e:
                logging.error(f"[PYMES] Error fatal guardando pedido estructurado: {e}", exc_info=True)
                db.session.rollback()
                return {"respuesta": "Hubo un problema al guardar tu pedido. Un representante te contactará.", "fuente": "error_handler_pedido"}
        elif 'confirmando_pedido' in contexto_pyme:
            palabras_confirmacion = ["si", "sí", "dale", "quiero", "generar", "confirmar", "ok", "me gustaria"]
            pregunta_limpia = pregunta.lower().strip()
            if any(pregunta_limpia.startswith(palabra) for palabra in palabras_confirmacion):
                productos_encontrados = contexto_pyme.pop('confirmando_pedido') # <-- Limpia la mochila
                self.context['contexto_pyme']['detallando_pedido'] = productos_encontrados # <-- Pone el nuevo estado
                respuesta = "¡Perfecto! Para continuar, por favor, decime qué productos y qué cantidades querés. Por ejemplo: 'una caja de cabernet y 2 de blanco dulce'."
                return {"respuesta": respuesta, "fuente": "handler_pedido_iniciado"}
        return None
        
# ... (El resto de los Handlers: TicketStatus, BrokenProduct, Claim, VectorCatalog, SalesEngage, Faq, Intent, LLM, todos adaptados de la misma forma)
# Por brevedad, se muestra la adaptación en el handler más complejo. El principio es el mismo para los demás.

# --- FUNCIÓN PRINCIPAL ORQUESTADORA (ADAPTADA A MEMORIA MANUAL) ---

def responder_pyme(pregunta, user_obj, rubro_obj, **kwargs):
    # 1. RECIBE LA "MOCHILA" DEL PASADO
    contexto_previo = kwargs.get('contexto_previo', {})
    contexto_pyme = contexto_previo.get(CONTEXTO_PYME_SESION, {})

    # 2. PREPARA EL CONTEXTO PARA ESTA EJECUCIÓN
    context = {
        "contexto_pyme": contexto_pyme,  # Esta es nuestra "memoria" de trabajo
        "user_obj": user_obj, "rubro_obj": rubro_obj,
        "user_id": getattr(user_obj, "id", None),
        "nombre_pyme": getattr(user_obj, "nombre_empresa", "la empresa"),
        "telefono": getattr(user_obj, "telefono", ""), "direccion": getattr(user_obj, "direccion", ""),
        "email": getattr(user_obj, "email", ""), "plan": getattr(user_obj, "plan", "anonimo"),
        "preguntas_usadas": getattr(user_obj, "preguntas_usadas", 0),
        "limite_preguntas": getattr(user_obj, "limite_preguntas", 10),
        "rubro_nombre": getattr(rubro_obj, "nombre", "empresa"),
        "mensajes_previos": flask_session.get(NOMBRE_HISTORIAL_SESION, [])[-12:]
    }
    
    handler_chain = [
        LimitHandler, FollowUpHandler, PedidoHandler,
        TicketStatusHandler, BrokenProductHandler, ClaimHandler,
        VectorCatalogHandler, SalesEngageHandler, FaqHandler, 
        IntentHandler, LLMHandler
    ]

    respuesta_final = None
    for handler_class in handler_chain:
        handler_instance = handler_class(context)
        respuesta_final = handler_instance.handle(pregunta)
        if respuesta_final:
            break

    if not respuesta_final:
        respuesta_final = {"respuesta": "Disculpa, no pude procesar tu solicitud.", "fuente": "error_no_handler"}
    
    # 3. GUARDAR HISTORIAL (esto puede seguir usando la sesión de Flask, no es crítico)
    historial = flask_session.get(NOMBRE_HISTORIAL_SESION, [])
    historial.extend([
        {"role": "user", "content": pregunta},
        {"role": "assistant", "content": respuesta_final['respuesta']}
    ])
    if len(historial) > MAX_HISTORIAL_CHAT:
        flask_session[NOMBRE_HISTORIAL_SESION] = historial[-MAX_HISTORIAL_CHAT:]
    else:
        flask_session[NOMBRE_HISTORIAL_SESION] = historial
    flask_session.modified = True
    
    # 4. DEVOLVEMOS LA "MOCHILA" ACTUALIZADA AL FRONTEND
    return {
        "respuesta": respuesta_final['respuesta'],
        "fuente": respuesta_final.get('fuente', 'desconocida'),
        "contexto_actualizado": {CONTEXTO_PYME_SESION: contexto_pyme} 
    }