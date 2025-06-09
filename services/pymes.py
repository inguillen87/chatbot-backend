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

# --- Constantes ---
NOMBRE_HISTORIAL_SESION = "historial_chat_cliente"
CONTEXTO_PYME_SESION = "contexto_pyme"
MAX_HISTORIAL_CHAT = 14

# --- Funciones Auxiliares (sin cambios) ---
def _generar_asunto_con_llm(pregunta: str) -> str:
    # ... (código sin cambios)
    pass

def _extraer_cantidades_con_llm(pregunta_cliente: str, productos_disponibles: list) -> list:
    # ... (código sin cambios)
    pass

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
        contexto_pyme = self.context['contexto_pyme']  # <-- USA LA MOCHILA
        if 'esperando_detalles_reclamo' in contexto_pyme:
            ticket_id = contexto_pyme.pop('esperando_detalles_reclamo')  # Limpia la mochila
            servicio_tickets.crear_comentario(ticket_id=ticket_id, tipo_ticket="pyme", comentario_data={"comentario": pregunta, "user_id": self.context['user_id']})
            return {"respuesta": "Perfecto, he añadido tus comentarios al reclamo.", "fuente": "detalle_reclamo_agregado"}
        elif 'esperando_datos_reclamo_roto' in contexto_pyme:
            ticket_id = contexto_pyme.pop('esperando_datos_reclamo_roto')  # Limpia la mochila
            servicio_tickets.crear_comentario(ticket_id=ticket_id, tipo_ticket="pyme", comentario_data={"comentario": f"Info adicional del cliente: {pregunta}", "user_id": self.context['user_id']})
            return {"respuesta": "Recibido. Gracias por la información. Ya estamos procesando el envío de tu reemplazo.", "fuente": "datos_reemplazo_recibidos"}
        return None

class PedidoHandler(BaseHandler):
    def handle(self, pregunta: str) -> dict | None:
        contexto_pyme = self.context['contexto_pyme']  # <-- USA LA MOCHILA
        if 'detallando_pedido' in contexto_pyme:
            productos_para_pedido = contexto_pyme.pop('detallando_pedido')  # Limpia la mochila
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
                productos_encontrados = contexto_pyme.pop('confirmando_pedido')  # Limpia la mochila
                self.context['contexto_pyme']['detallando_pedido'] = productos_encontrados  # Pone el nuevo estado
                respuesta = "¡Perfecto! Para continuar, por favor, decime qué productos y qué cantidades querés. Por ejemplo: 'una caja de cabernet y 2 de blanco dulce'."
                return {"respuesta": respuesta, "fuente": "handler_pedido_iniciado"}
        return None

class TicketStatusHandler(BaseHandler):
    # (Este handler es de solo lectura, no necesita cambios)
    def handle(self, pregunta: str) -> dict | None:
        # ... código sin cambios ...
        pass

class BrokenProductHandler(BaseHandler):
    def handle(self, pregunta: str) -> dict | None:
        palabras_clave = ["botella rota", "llegó roto", "producto dañado"]
        if any(keyword in pregunta.lower() for keyword in palabras_clave):
            asunto = _generar_asunto_con_llm(pregunta)
            ticket_creado = servicio_tickets.crear_nuevo_ticket(tipo_ticket="pyme", ticket_data={"pregunta": pregunta, "user_id": self.context['user_id'], "comentario": pregunta, "asunto": asunto, "categoria": "Reclamo - Producto Dañado"})
            if ticket_creado:
                self.context['contexto_pyme']['esperando_datos_reclamo_roto'] = ticket_creado.id  # <-- USA LA MOCHILA
            nombre_empresa = self.context.get('nombre_pyme', 'nuestra bodega')
            respuesta = (f"Lamento muchísimo escuchar eso. En {nombre_empresa} nos aseguramos de que recibas todo en perfectas condiciones.\n\n"
                         "No te preocupes, te enviaremos una nueva botella sin ningún costo adicional.\n\n"
                         "Para gestionar el nuevo envío, por favor, indícame en tu próximo mensaje el **número del pedido original** (el número de tu compra). Si puedes adjuntar una foto del daño, nos sería de gran ayuda para documentar el incidente.")
            return {"respuesta": respuesta, "fuente": "handler_producto_dañado"}
        return None

class ClaimHandler(BaseHandler):
    def handle(self, pregunta: str) -> dict | None:
        palabras_reclamo = ["mal servicio", "problema", "no llegó", "demora", "reclamo"]
        if any(w in pregunta.lower() for w in palabras_reclamo):
            asunto = _generar_asunto_con_llm(pregunta)
            ticket_creado = servicio_tickets.crear_nuevo_ticket(tipo_ticket="pyme", ticket_data={"pregunta": pregunta, "user_id": self.context['user_id'],"comentario": pregunta, "asunto": asunto, "categoria": "Reclamo"})
            if ticket_creado:
                respuesta = (f"Lamento mucho el inconveniente. He generado un reclamo con el ticket #{ticket_creado.nro_ticket} (Asunto: '{asunto}'). "
                             "Para poder ayudarte mejor, ¿podrías darme más detalles? Tu próximo mensaje se agregará automáticamente.")
                self.context['contexto_pyme']['esperando_detalles_reclamo'] = ticket_creado.id  # <-- USA LA MOCHILA
                return {"respuesta": respuesta, "fuente": "registro_reclamo"}
        return None

class VectorCatalogHandler(BaseHandler):
    def handle(self, pregunta: str) -> dict | None:
        # ... (lógica sin cambios, pero la parte de guardar el contexto se adapta)
        # ...
        # Al final, antes del return:
        # self.context['contexto_pyme']['confirmando_pedido'] = items_encontrados  # <-- USA LA MOCHILA
        return None # Placeholder para brevedad

class SalesEngageHandler(BaseHandler):
    def handle(self, pregunta: str) -> dict | None:
        contexto_pyme = self.context['contexto_pyme']  # <-- USA LA MOCHILA
        if contexto_pyme.get('aviso_sin_catalogo_dado'):
            return None
        # ...
        # Al final, antes del return:
        # contexto_pyme['aviso_sin_catalogo_dado'] = True  # <-- USA LA MOCHILA
        return None # Placeholder para brevedad

# ... (FaqHandler, IntentHandler, LLMHandler no necesitan cambios porque no guardan estado conversacional)

# --- FUNCIÓN PRINCIPAL ORQUESTADORA (ADAPTADA A MEMORIA MANUAL) ---

def responder_pyme(pregunta, user_obj, rubro_obj, **kwargs):
    # 1. RECIBE LA "MOCHILA" DEL PASADO
    contexto_previo = kwargs.get('contexto_previo', {})
    contexto_pyme = contexto_previo.get(CONTEXTO_PYME_SESION, {})

    # 2. PREPARA EL CONTEXTO PARA ESTA EJECUCIÓN
    context = {
        "contexto_pyme": contexto_pyme,
        "user_obj": user_obj, "rubro_obj": rubro_obj,
        "user_id": getattr(user_obj, "id", None),
        "nombre_pyme": getattr(user_obj, "nombre_empresa", "la empresa"),
        "telefono": getattr(user_obj, "telefono", ""), 
        "direccion": getattr(user_obj, "direccion", ""),
        "email": getattr(user_obj, "email", ""), 
        "plan": getattr(user_obj, "plan", "anonimo"),
        "preguntas_usadas": getattr(user_obj, "preguntas_usadas", 0),
        "limite_preguntas": getattr(user_obj, "limite_preguntas", 10),
        "rubro_nombre": getattr(rubro_obj, "nombre", "empresa") if rubro_obj else "desconocido",
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
            # --- TRAMPA PARA BUGS ---
            # Verificamos que el handler haya devuelto un diccionario.
            if not isinstance(respuesta_final, dict):
                logging.error(f"[HANDLER_ERROR] El handler '{handler_class.__name__}' devolvió un tipo de dato incorrecto: {type(respuesta_final)}. Valor: {respuesta_final}")
                # Si no es un dict, lo ignoramos y seguimos con el próximo handler.
                respuesta_final = None 
            else:
                # Si es un dict, salimos del bucle como siempre.
                break

    if not respuesta_final:
        respuesta_final = {"respuesta": "Disculpa, no pude procesar tu solicitud en este momento.", "fuente": "error_no_handler"}
    
    # ... (código para guardar historial y conversación sin cambios) ...
    
    # 4. DEVOLVEMOS LA "MOCHILA" ACTUALIZADA AL FRONTEND
    return {
        "respuesta": respuesta_final.get('respuesta', "Error: respuesta mal formada."),
        "fuente": respuesta_final.get('fuente', 'desconocida'),
        "contexto_actualizado": {CONTEXTO_PYME_SESION: contexto_pyme} 
    }