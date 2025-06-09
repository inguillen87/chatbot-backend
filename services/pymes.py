# services/pymes.py

import logging
import re
import random
import json
import datetime
from flask import session as flask_session

# --- Importaciones ---
from models import Conversacion, PymeTicket, TicketComentario, PymePedido, Rubro, db
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
NOMBRE_HISTORIAL_SESION = "historial_chat_cliente_pyme" # Usamos un nombre único para la sesión de historial
CONTEXTO_PYME_SESION = "contexto_pyme"
MAX_HISTORIAL_CHAT = 14

# --- Funciones Auxiliares ---
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

# --- ARQUITECTURA DE HANDLERS (COMPLETA Y FUNCIONAL) ---

class BaseHandler:
    def __init__(self, context):
        self.context = context
    def handle(self, pregunta: str) -> dict | None:
        raise NotImplementedError

class LimitHandler(BaseHandler):
    def handle(self, pregunta: str) -> dict | None:
        if self.context.get('preguntas_usadas', 0) >= self.context.get('limite_preguntas', 50):
            return {"respuesta": "🔒 Límite de preguntas alcanzado. Actualizá tu plan para continuar.", "fuente": "sistema_limite"}
        return None

class FollowUpHandler(BaseHandler):
    def handle(self, pregunta: str) -> dict | None:
        contexto_pyme = self.context.get('contexto_pyme', {})
        if 'esperando_detalles_reclamo' in contexto_pyme:
            ticket_id = contexto_pyme.pop('esperando_detalles_reclamo')
            ticket = db.session.get(PymeTicket, ticket_id)
            if ticket:
                servicio_tickets.crear_comentario(ticket_id=ticket.id, tipo_ticket="pyme", comentario_data={"comentario": pregunta, "user_id": self.context['user_id']})
                return {"respuesta": "Perfecto, he añadido tus comentarios al reclamo.", "fuente": "detalle_reclamo_agregado"}
        elif 'esperando_datos_reclamo_roto' in contexto_pyme:
            ticket_id = contexto_pyme.pop('esperando_datos_reclamo_roto')
            ticket = db.session.get(PymeTicket, ticket_id)
            if ticket:
                servicio_tickets.crear_comentario(ticket_id=ticket.id, tipo_ticket="pyme", comentario_data={"comentario": f"Info adicional del cliente: {pregunta}", "user_id": self.context['user_id']})
                return {"respuesta": "Recibido. Gracias por la información. Ya estamos procesando el envío de tu reemplazo.", "fuente": "datos_reemplazo_recibidos"}
        return None

class PedidoHandler(BaseHandler):
    def handle(self, pregunta: str) -> dict | None:
        contexto_pyme = self.context.get('contexto_pyme', {})
        if 'detallando_pedido' in contexto_pyme:
            productos_para_pedido = contexto_pyme.pop('detallando_pedido')
            detalles_estructurados = _extraer_cantidades_con_llm(pregunta, productos_para_pedido)
            try:
                nro_pedido = f"P-{random.randint(10000, 99999)}"
                nuevo_pedido = PymePedido(user_id=self.context['user_id'], nro_pedido=nro_pedido, detalles=json.dumps(detalles_estructurados, indent=2, ensure_ascii=False), estado="pendiente")
                db.session.add(nuevo_pedido)
                db.session.commit()
                respuesta = (f"¡Pedido recibido! He generado tu orden con el número **{nro_pedido}**.\nUn representante de ventas se pondrá en contacto contigo. ¡Muchas gracias!")
                return {"respuesta": respuesta, "fuente": "handler_pedido_confirmado_ia"}
            except Exception as e:
                db.session.rollback()
                logging.error(f"[PYMES] Error fatal guardando pedido: {e}", exc_info=True)
                return {"respuesta": "Hubo un problema al guardar tu pedido. Un representante te contactará.", "fuente": "error_handler_pedido"}
        elif 'confirmando_pedido' in contexto_pyme:
            palabras_confirmacion = ["si", "sí", "dale", "quiero", "generar", "confirmar", "ok", "me gustaria"]
            if any(pregunta.lower().strip().startswith(palabra) for palabra in palabras_confirmacion):
                productos_encontrados = contexto_pyme.pop('confirmando_pedido')
                self.context['contexto_pyme']['detallando_pedido'] = productos_encontrados
                return {"respuesta": "¡Perfecto! Para continuar, decime qué productos y qué cantidades querés.", "fuente": "handler_pedido_iniciado"}
        return None

class TicketStatusHandler(BaseHandler):
    def handle(self, pregunta: str) -> dict | None:
        ticket_match = re.search(r"(ticket|reclamo|consulta)\s*#?\s*([0-9]{5,})", pregunta, re.IGNORECASE)
        if ticket_match:
            nro = ticket_match.group(2)
            ticket = PymeTicket.query.filter_by(nro_ticket=int(nro), user_id=self.context.get('user_id')).first()
            if ticket:
                msg = f"El ticket de reclamo #{nro} (Asunto: '{ticket.asunto}') está en estado: '{ticket.estado}'."
                # Aquí podrías añadir una lógica para mostrar el último comentario si quisieras
                return {"respuesta": msg, "fuente": "consulta_estado_ticket"}
            else:
                return {"respuesta": f"No se encontró ningún ticket con el número #{nro}.", "fuente": "ticket_no_encontrado"}
        return None

class BrokenProductHandler(BaseHandler):
    def handle(self, pregunta: str) -> dict | None:
        if any(keyword in pregunta.lower() for keyword in ["botella rota", "llegó roto", "producto dañado"]):
            asunto = _generar_asunto_con_llm(pregunta)
            ticket_creado = servicio_tickets.crear_nuevo_ticket(tipo_ticket="pyme", ticket_data={"pregunta": pregunta, "user_id": self.context['user_id'], "asunto": asunto, "categoria": "Reclamo - Producto Dañado"})
            if ticket_creado:
                self.context['contexto_pyme']['esperando_datos_reclamo_roto'] = ticket_creado.id
            respuesta = (f"Lamento muchísimo escuchar eso. En {self.context.get('nombre_pyme')} nos aseguramos de que recibas todo en perfectas condiciones.\n\nPara gestionar el nuevo envío, por favor, indícame en tu próximo mensaje el número del pedido original.")
            return {"respuesta": respuesta, "fuente": "handler_producto_dañado"}
        return None

class ClaimHandler(BaseHandler):
    def handle(self, pregunta: str) -> dict | None:
        if any(w in pregunta.lower() for w in ["mal servicio", "problema", "no llegó", "demora", "reclamo", "queja"]):
            asunto = _generar_asunto_con_llm(pregunta)
            ticket_creado = servicio_tickets.crear_nuevo_ticket(tipo_ticket="pyme", ticket_data={"pregunta": pregunta, "user_id": self.context['user_id'],"asunto": asunto, "categoria": "Reclamo"})
            if ticket_creado:
                respuesta = (f"Lamento mucho el inconveniente. He generado un reclamo con el ticket #{ticket_creado.nro_ticket}.\nPara poder ayudarte mejor, ¿podrías darme más detalles?")
                self.context['contexto_pyme']['esperando_detalles_reclamo'] = ticket_creado.id
                return {"respuesta": respuesta, "fuente": "registro_reclamo"}
        return None

class VectorCatalogHandler(BaseHandler):
    def handle(self, pregunta: str) -> dict | None:
        if any(w in pregunta.lower() for w in ["comprar", "precio", "pedido", "catalogo", "stock", "quiero", "vinos"]):
            try:
                resultados = buscar_item_vectorizado(pregunta, self.context['user_id'])
                if resultados:
                    respuesta_texto = "¡Claro! En nuestro catálogo detallado encontré esto:\n"
                    items_payload = [item.payload for item in resultados]
                    for payload in items_payload:
                        respuesta_texto += f"- **{payload.get('nombre', '')}**: ${payload.get('precio_str', 'Consultar')}\n"
                    respuesta_texto += "\n¿Te gustaría que genere un pedido con alguno de estos productos?"
                    self.context['contexto_pyme']['confirmando_pedido'] = items_payload
                    return {"respuesta": respuesta_texto, "fuente": "catalogo_qdrant"}
            except Exception as e:
                logging.warning(f"[PYMES] Error buscando en Qdrant: {e}")
        return None

class SalesEngageHandler(BaseHandler):
    def handle(self, pregunta: str) -> dict | None:
        contexto_pyme = self.context.get('contexto_pyme', {})
        if contexto_pyme.get('aviso_sin_catalogo_dado'): return None
        palabras_venta = ["comprar", "precio", "producto", "catalogo", "stock", "vinos"]
        if any(palabra in pregunta.lower() for palabra in palabras_venta):
            nombre_pyme = self.context.get('nombre_pyme', 'nuestra empresa')
            respuesta = (f"Veo que te interesa consultar sobre nuestros productos en {nombre_pyme}, ¡qué bueno!\n\n"
                         "En este momento no encuentro información detallada en mi sistema para responderte.\n\n"
                         "¿Te gustaría que tome nota de tu consulta y tus datos para que un representante comercial se ponga en contacto contigo a la brevedad?")
            contexto_pyme['aviso_sin_catalogo_dado'] = True
            return {"respuesta": respuesta, "fuente": "handler_sin_catalogo"}
        return None

class FaqHandler(BaseHandler):
    def handle(self, pregunta: str) -> dict | None:
        try:
            # Esta lógica viene de tu archivo original
            faq = buscar_en_faq_spacy(pregunta, self.context['rubro_obj'].id if self.context.get('rubro_obj') else 1)
            if faq and faq.answer:
                return {"respuesta": reemplazar_placeholders(faq.answer, self.context['user_obj']), "fuente": "faq"}
        except Exception as e:
            logging.warning(f"[PYMES] Error en FaqHandler: {e}")
        return None

class IntentHandler(BaseHandler):
    def handle(self, pregunta: str) -> dict | None:
        try:
            # Esta lógica viene de tu archivo original
            intent_resp = buscar_en_intents(pregunta, self.context.get('rubro_nombre', 'general'))
            if intent_resp:
                return {"respuesta": reemplazar_placeholders(intent_resp, self.context['user_obj']), "fuente": "intent"}
        except Exception as e:
            logging.warning(f"[PYMES] Error en IntentHandler: {e}")
        return None

class LLMHandler(BaseHandler):
    def handle(self, pregunta: str) -> dict | None:
        try:
            prompt_pyme = f"""
                "Chatboc", el agente de ventas y atención al cliente de {self.context.get('nombre_pyme', 'la empresa')}.
                Tus Datos de Contacto: Teléfono {self.context.get('telefono', 'no provisto')}, Email {self.context.get('email', 'no provisto')}.
                Regla de Oro: Si no sabes una respuesta sobre un producto, NO inventes. Ofrece amablemente los canales de contacto para que un humano pueda ayudar.
                Historial reciente:
                """
            for msg in self.context.get('mensajes_previos', []):
                prompt_pyme += f"\n- {msg.get('role', 'user')}: {msg.get('content','')}"
            prompt_pyme += f"\n- Cliente: {pregunta}\n- Chatboc:"
            
            respuesta_llm = get_cohere_response(message=pregunta, chat_history=self.context.get('mensajes_previos', []), preamble=prompt_pyme)
            if respuesta_llm:
                return {"respuesta": reemplazar_placeholders(respuesta_llm, self.context.get('user_obj')), "fuente": "llm"}
        except Exception as e:
            logging.error(f"[PYMES] Error fatal en LLMHandler: {e}", exc_info=True)
        
        sugs = sugerencias_por_rubro(self.context.get('rubro_nombre', 'empresa'))
        return {"respuesta": "No encontré una respuesta directa. Probá con: " + " · ".join(f"“{s}”" for s in sugs if s), "fuente": "sugerencia_fallback"}

# --- FUNCIÓN ORQUESTADORA PRINCIPAL ---

def responder_pyme(pregunta, user_obj, rubro_obj, **kwargs):
    contexto_previo = kwargs.get('contexto_previo', {})
    contexto_previo_valido = contexto_previo if contexto_previo is not None else {}
    contexto_pyme = contexto_previo_valido.get(CONTEXTO_PYME_SESION, {})

    context = {
        "contexto_pyme": contexto_pyme,
        "user_obj": user_obj, "rubro_obj": rubro_obj,
        "user_id": getattr(user_obj, "id", None),
        "nombre_pyme": getattr(user_obj, "nombre_empresa", "la empresa") if user_obj else "la empresa",
        "telefono": getattr(user_obj, "telefono", "") if user_obj else "",
        "direccion": getattr(user_obj, "direccion", "") if user_obj else "",
        "email": getattr(user_obj, "email", "") if user_obj else "",
        "plan": getattr(user_obj, "plan", "anonimo") if user_obj else "anonimo",
        "preguntas_usadas": getattr(user_obj, "preguntas_usadas", 0) if user_obj else 0,
        "limite_preguntas": getattr(user_obj, "limite_preguntas", 10) if user_obj else 10,
        "rubro_nombre": getattr(rubro_obj, "nombre", "empresa") if rubro_obj else "desconocido",
        "mensajes_previos": flask_session.get(NOMBRE_HISTORIAL_SESION, [])
    }
    
    handler_chain = [
        LimitHandler, FollowUpHandler, PedidoHandler, TicketStatusHandler, 
        BrokenProductHandler, ClaimHandler, VectorCatalogHandler, 
        SalesEngageHandler, FaqHandler, IntentHandler, LLMHandler
    ]

    respuesta_final = None
    for handler_class in handler_chain:
        handler_instance = handler_class(context)
        respuesta_final = handler_instance.handle(pregunta)
        if respuesta_final:
            if not isinstance(respuesta_final, dict):
                logging.error(f"[HANDLER_ERROR] Handler '{handler_class.__name__}' devolvió tipo incorrecto: {type(respuesta_final)}")
                respuesta_final = None
            else:
                break

    if not respuesta_final:
        respuesta_final = {"respuesta": "Disculpa, no pude procesar tu solicitud en este momento.", "fuente": "error_no_handler"}
    
    historial = flask_session.get(NOMBRE_HISTORIAL_SESION, [])
    historial.append({"role": "user", "content": pregunta})
    historial.append({"role": "assistant", "content": respuesta_final['respuesta']})
    flask_session[NOMBRE_HISTORIAL_SESION] = historial[-MAX_HISTORIAL_CHAT:]
    
    try:
        if context['user_id']:
            db.session.add(Conversacion(user_id=context['user_id'], pregunta=pregunta, respuesta=respuesta_final['respuesta'], fuente=respuesta_final.get('fuente', 'desconocida'), rubro=context['rubro_nombre']))
            db.session.commit()
    except Exception as e:
        logging.error(f"[PYMES] Error guardando conversación en DB: {e}")
        db.session.rollback()

    return {
        "respuesta": respuesta_final.get('respuesta', "Error: respuesta mal formada."),
        "fuente": respuesta_final.get('fuente', 'desconocida'),
        "contexto_actualizado": {CONTEXTO_PYME_SESION: contexto_pyme} 
    }