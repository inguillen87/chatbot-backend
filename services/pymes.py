# services/pymes.py

import logging
import re
import random
import datetime
from flask import session as flask_session

# --- Importaciones de la Base de Datos y Servicios ---
from models import Conversacion, PymeTicket, TicketComentario, db
from services.utils_placeholders import reemplazar_placeholders
from services.utils import sugerencias_por_rubro
from services.cohere_ai import get_cohere_response
from services.vector_search import buscar_item_vectorizado
from services.faq_matcher_spacy import buscar_en_faq_spacy
from services.intent_matcher import buscar_en_intents
# ¡Importamos el servicio de tickets centralizado!
from services.ticket_service import servicio_tickets

logger = logging.getLogger(__name__)

# --- Constantes de Sesión ---
NOMBRE_HISTORIAL_SESION = "historial_chat_cliente"
CONTEXTO_PYME_SESION = "contexto_pyme"
MAX_HISTORIAL_CHAT = 14

# --- NUEVA FUNCIÓN AUXILIAR: Generador de Asuntos ---
def _generar_asunto_con_llm(pregunta: str) -> str:
    """Usa el LLM para generar un asunto de ticket conciso y claro."""
    try:
        prompt = f"Resume la siguiente consulta de un cliente en un título breve de 4 a 8 palabras para un ticket de soporte. La consulta es: '{pregunta}'"
        # Esta es una llamada más pequeña y enfocada, muy eficiente.
        asunto = get_cohere_response(message=prompt, chat_history=[], preamble="Eres un experto en resumir consultas de clientes.")
        return asunto.strip().replace('"', '')
    except Exception as e:
        logger.error(f"[PYME] Error generando asunto con LLM: {e}")
        # Si el LLM falla, usamos un trozo de la pregunta como fallback.
        return (pregunta[:75] + '...') if len(pregunta) > 75 else pregunta


# --- PATRÓN DE DISEÑO: ORQUESTADOR CON MANEJADORES (REFINADO) ---

class BrokenProductHandler(BaseHandler):
    """
    Handler especialista para reclamos de productos rotos o dañados.
    Ofrece una solución proactiva inmediata.
    """
    def handle(self, pregunta: str) -> dict | None:
        palabras_clave = ["botella rota", "llegó roto", "producto dañado", "está roto", "vino roto"]
        if any(keyword in pregunta.lower() for keyword in palabras_clave):
            
            # 1. Generamos asunto y creamos el ticket con una categoría específica
            asunto = _generar_asunto_con_llm(pregunta)
            ticket_id = servicio_tickets.crear_nuevo_ticket(
                tipo_ticket="pyme",
                ticket_data={
                    "pregunta": pregunta,
                    "user_id": self.context['user_id'],
                    "comentario": pregunta,
                    "asunto": asunto,
                    "categoria": "Reclamo - Producto Dañado" # Categoría más específica
                }
            )

            # 2. Guardamos un contexto específico en la sesión para el seguimiento
            self.context['session'][CONTEXTO_PYME_SESION] = {'esperando_datos_reclamo_roto': ticket_id}
            self.context['session'].modified = True
            
            # 3. Formulamos la respuesta ideal que definimos
            nombre_empresa = self.context.get('nombre_pyme', 'nuestra bodega')
            respuesta = (
                        f"Lamento muchísimo escuchar eso. En {nombre_empresa} nos aseguramos de que recibas todo en perfectas condiciones.\n\n"
                        "No te preocupes, te enviaremos una nueva botella sin ningún costo adicional.\n\n"
                        "Para gestionar el nuevo envío, por favor, indícame en tu próximo mensaje el **número del pedido original** (el número de tu compra). Si puedes adjuntar una foto del daño, nos sería de gran ayuda para documentar el incidente."
                        )

            return {"respuesta": respuesta, "fuente": "handler_producto_dañado"}
            
        return None

class BaseHandler:
    """Clase base para todos los manejadores. Define la interfaz."""
    def __init__(self, context):
        self.context = context

    def handle(self, pregunta: str) -> dict | None:
        """Si el manejador puede responder, devuelve un dict. Si no, devuelve None."""
        raise NotImplementedError

class LimitHandler(BaseHandler):
    """Verifica si el usuario ha alcanzado el límite de preguntas de su plan."""
    def handle(self, pregunta: str) -> dict | None:
        if self.context['preguntas_usadas'] >= self.context['limite_preguntas']:
            return {"respuesta": "🔒 Límite de preguntas alcanzado. Actualizá tu plan para continuar.", "fuente": "sistema_limite"}
        return None

class FollowUpHandler(BaseHandler):
    """Manejador de seguimiento para conversaciones en curso."""
    def handle(self, pregunta: str) -> dict | None:
        contexto_pyme = self.context['session'].get(CONTEXTO_PYME_SESION, {})
        
        # Flujo para reclamos generales
        if contexto_pyme.get('esperando_detalles_reclamo'):
            ticket_id = contexto_pyme['esperando_detalles_reclamo']
            servicio_tickets.crear_comentario(
                ticket_id=ticket_id, tipo_ticket="pyme",
                comentario_data={"comentario": pregunta, "user_id": self.context['user_id']}
            )
            self.context['session'][CONTEXTO_PYME_SESION] = {} # Limpiar contexto
            self.context['session'].modified = True
            return {
                "respuesta": "Perfecto, he añadido tus comentarios al reclamo. Nuestro equipo lo revisará a la brevedad.",
                "fuente": "detalle_reclamo_agregado"
            }
            
        # NUEVO FLUJO: Para cuando pedimos datos de un producto roto
        elif contexto_pyme.get('esperando_datos_reclamo_roto'):
            ticket_id = contexto_pyme['esperando_datos_reclamo_roto']
            servicio_tickets.crear_comentario(
                ticket_id=ticket_id, tipo_ticket="pyme",
                comentario_data={"comentario": f"Info adicional del cliente: {pregunta}", "user_id": self.context['user_id']}
            )
            self.context['session'][CONTEXTO_PYME_SESION] = {} # Limpiar contexto
            self.context['session'].modified = True
            return {
                "respuesta": "Recibido. Gracias por la información. Ya estamos procesando el envío de tu reemplazo.",
                "fuente": "datos_reemplazo_recibidos"
            }
            
        return None

class TicketStatusHandler(BaseHandler):
    """Busca el estado de un ticket existente."""
    def handle(self, pregunta: str) -> dict | None:
        ticket_match = re.search(r"(ticket|reclamo|consulta)\s*#?\s*([0-9]{4,7})", pregunta, re.IGNORECASE)
        if ticket_match:
            nro = ticket_match.group(2)
            # Aquí podrías usar un método del servicio de tickets si la búsqueda se vuelve más compleja
            ticket = PymeTicket.query.filter_by(nro_ticket=int(nro), user_id=self.context['user_id']).first()
            if ticket:
                msg_estado = f"El ticket #{nro} (Asunto: '{ticket.asunto}') está en estado: '{ticket.estado}'."
                if ticket.comentarios.count() > 0:
                    ult_com = ticket.comentarios.order_by(TicketComentario.fecha.desc()).first()
                    msg_estado += f" Último comentario: \"{ult_com.comentario}\""
                return {"respuesta": msg_estado, "fuente": "consulta_estado_ticket"}
            else:
                return {"respuesta": f"No se encontró ningún ticket con el número #{nro}.", "fuente": "ticket_no_encontrado"}
        
        # Si no hubo match en la expresion regular, devuelve None para pasar al siguiente handler
        return None
    
class ClaimHandler(BaseHandler):
    """Detecta, clasifica, genera asunto y registra nuevos reclamos."""
    def handle(self, pregunta: str) -> dict | None:
        palabras_reclamo = ["mal servicio", "problema", "no llegó", "demora", "defectuoso", "reclamo", "falló", "devolución", "cancelar", "queja"]
        if any(w in pregunta.lower() for w in palabras_reclamo):
            
            # 1. Generamos el asunto automáticamente
            asunto = _generar_asunto_con_llm(pregunta)
            
            # 2. Creamos el ticket usando el servicio centralizado
            nro_ticket = servicio_tickets.crear_nuevo_ticket(
                tipo_ticket="pyme",
                ticket_data={
                    "pregunta": pregunta,
                    "user_id": self.context['user_id'],
                    "comentario": pregunta, # El primer comentario es la propia pregunta
                    "asunto": asunto,
                    "categoria": "Reclamo" # 3. Asignamos la categoría
                }
            )
            
            respuesta = (
                f"Lamento mucho el inconveniente. He generado un reclamo con el ticket #{nro_ticket} (Asunto: '{asunto}'). "
                "Para poder ayudarte mejor, ¿podrías darme más detalles? Tu próximo mensaje se agregará automáticamente."
            )
            
            # 4. Guardamos en sesión que estamos esperando una respuesta
            self.context['session'][CONTEXTO_PYME_SESION] = {'esperando_detalles_reclamo': nro_ticket}
            self.context['session'].modified = True
            
            return {"respuesta": respuesta, "fuente": "registro_reclamo"}
        return None

# Puedes seguir este patrón para otros handlers que creen tickets (ej. PedidoHandler, SoporteHandler)

class VectorCatalogHandler(BaseHandler):
    """Busca en el catálogo de productos y ofrece crear un pedido."""
 def handle(self, pregunta: str) -> dict | None:
        palabras_pedido = ["comprar", "precio", "pedido", "cotización", "oferta", "disponible", "stock", "quiero"]
        if any(w in pregunta.lower() for w in palabras_pedido):
            try:
                # Asumimos que buscar_item_vectorizado devuelve una lista de resultados
                resultados = buscar_item_vectorizado(pregunta, self.context['user_id'])
                if resultados:
                    # Formateamos la respuesta para que sea clara
                    respuesta_texto = "¡Claro! Encontré esto en nuestro catálogo:\n"
                    # Suponemos que cada 'item' tiene .payload con 'nombre' y 'precio'
                    items_encontrados = []
                    for item in resultados:
                        nombre = item.payload.get('nombre', 'Producto sin nombre')
                        precio = item.payload.get('precio_str', 'Consultar precio')
                        respuesta_texto += f"- **{nombre}**: ${precio}\n"
                        items_encontrados.append(item.payload)
                    
                    respuesta_texto += "\n¿Te gustaría que genere un pedido con alguno de estos productos?"
                    
                    # ¡LA MODIFICACIÓN CLAVE! Guardamos los productos en la sesión
                    self.context['session'][CONTEXTO_PYME_SESION] = {'confirmando_pedido': items_encontrados}
                    self.context['session'].modified = True

                    return {"respuesta": respuesta_texto, "fuente": "catalogo_vector"}
            except Exception as e:
                logging.warning(f"[PYMES] Error en VectorCatalogHandler: {e}")
        return None
# En pymes.py
import json # Asegúrate de tener esta importación al principio del archivo

class VectorCatalogHandler(BaseHandler):
    """Busca en el catálogo de productos y ofrece crear un pedido."""
    def handle(self, pregunta: str) -> dict | None:
        palabras_pedido = ["comprar", "precio", "pedido", "cotización", "oferta", "disponible", "stock", "quiero"]
        if any(w in pregunta.lower() for w in palabras_pedido):
            try:
                # Asumimos que buscar_item_vectorizado devuelve una lista de resultados
                resultados = buscar_item_vectorizado(pregunta, self.context['user_id'])
                if resultados:
                    # Formateamos la respuesta para que sea clara
                    respuesta_texto = "¡Claro! Encontré esto en nuestro catálogo:\n"
                    # Suponemos que cada 'item' tiene .payload con 'nombre' y 'precio'
                    items_encontrados = []
                    for item in resultados:
                        nombre = item.payload.get('nombre', 'Producto sin nombre')
                        precio = item.payload.get('precio_str', 'Consultar precio')
                        respuesta_texto += f"- **{nombre}**: ${precio}\n"
                        items_encontrados.append(item.payload)
                    
                    respuesta_texto += "\n¿Te gustaría que genere un pedido con alguno de estos productos?"
                    
                    # ¡LA MODIFICACIÓN CLAVE! Guardamos los productos en la sesión
                    self.context['session'][CONTEXTO_PYME_SESION] = {'confirmando_pedido': items_encontrados}
                    self.context['session'].modified = True

                    return {"respuesta": respuesta_texto, "fuente": "catalogo_vector"}
            except Exception as e:
                logging.warning(f"[PYMES] Error en VectorCatalogHandler: {e}")
        return None

# En pymes.py

class PedidoHandler(BaseHandler):
    """
    Handler especialista que se activa después del VectorCatalogHandler
    para confirmar y crear un pedido en la base de datos.
    """
    def handle(self, pregunta: str) -> dict | None:
        contexto_pyme = self.context['session'].get(CONTEXTO_PYME_SESION, {})
        productos_a_confirmar = contexto_pyme.get('confirmando_pedido')

        # Se activa solo si hay un pedido por confirmar en la sesión
        if productos_a_confirmar:
            palabras_confirmacion = ["sí", "dale", "quiero", "generar pedido", "confirmar", "ok"]
            if any(palabra in pregunta.lower() for palabra in palabras_confirmacion):
                
                try:
                    # 1. Generar un número de pedido único
                    nro_pedido = f"P-{random.randint(10000, 99999)}"
                    
                    # 2. Crear el objeto PymePedido con los datos
                    nuevo_pedido = PymePedido(
                        user_id=self.context['user_id'],
                        nro_pedido=nro_pedido,
                        detalles=json.dumps(productos_a_confirmar), # Guardamos los detalles como JSON
                        estado="pendiente"
                    )
                    
                    # 3. Guardar en la base de datos
                    db.session.add(nuevo_pedido)
                    db.session.commit()
                    
                    # 4. Limpiar el contexto de la sesión
                    self.context['session'][CONTEXTO_PYME_SESION] = {}
                    self.context['session'].modified = True
                    
                    # 5. Responder al usuario con la confirmación
                    respuesta = (
                        f"¡Excelente! He generado tu pedido con el número **{nro_pedido}**.\n"
                        "Un representante de ventas se pondrá en contacto contigo a la brevedad para coordinar el pago y el envío. ¡Muchas gracias por tu compra!"
                    )
                    
                    return {"respuesta": respuesta, "fuente": "handler_pedido"}

                except Exception as e:
                    logging.error(f"[PYMES] Error fatal creando pedido: {e}")
                    db.session.rollback()
                    return {"respuesta": "Hubo un problema al generar tu pedido. Un representante te contactará para ayudarte.", "fuente": "error_handler_pedido"}
        
        return None
class FaqHandler(BaseHandler):
    """Busca en las Preguntas Frecuentes (FAQs)."""
    # (Sin cambios, se mantiene igual)
    def handle(self, pregunta: str) -> dict | None:
        try:
            faq = buscar_en_faq_spacy(pregunta, self.context['rubro_obj'].id)
            if faq and faq.answer:
                respuesta = reemplazar_placeholders(faq.answer, self.context['user_obj'])
                return {"respuesta": respuesta, "fuente": "faq"}
        except Exception as e:
            logging.warning(f"[PYMES] Error en FaqHandler: {e}")
        return None

class IntentHandler(BaseHandler):
    """Detecta intenciones generales (saludos, despedidas, etc.)."""
    # (Sin cambios, se mantiene igual)
    def handle(self, pregunta: str) -> dict | None:
        try:
            intent_resp = buscar_en_intents(pregunta, self.context['rubro_nombre'])
            if intent_resp:
                respuesta = reemplazar_placeholders(intent_resp, self.context['user_obj'])
                return {"respuesta": respuesta, "fuente": "intent"}
        except Exception as e:
            logging.warning(f"[PYMES] Error en IntentHandler: {e}")
        return None

class LLMHandler(BaseHandler):
    """El último recurso: llama al LLM con un prompt optimizado."""
    def handle(self, pregunta: str) -> dict | None:
        try:
            prompt_pyme = f"""
Eres "Chatboc", el agente de ventas y atención al cliente de {self.context['nombre_pyme']}. Tu objetivo es vender, resolver consultas y ser eficiente.

**Tus Datos Clave (Úsalos si es relevante):**
- Contacto: Teléfono {self.context['telefono'] or 'no provisto'}, Email {self.context['email'] or 'no provisto'}.
- Dirección: {self.context['direccion'] or 'operamos principalmente online'}.

**Reglas de Oro:**
1.  **Actúa como un humano experto**, no como un bot. Usa un tono amable, profesional y argentino.
2.  **Sé proactivo:** Si puedes resolver algo, ofrécelo. Si hay un reclamo, pide detalles.
3.  **Nunca inventes:** Si no sabes algo, di "Déjame que lo verifico con el equipo" en lugar de "No sé".
4.  **Sé un vendedor:** Facilita la compra, describe productos, informa sobre el catálogo.
5.  **Revisa el historial** para dar continuidad a la charla.

Historial reciente:
"""
            for msg in self.context['mensajes_previos']:
                prompt_pyme += f"\n- {msg.get('role', 'user')}: {msg.get('content','')}"
            prompt_pyme += f"\n- Cliente: {pregunta}\n- Chatboc:"

            respuesta_llm = get_cohere_response(
                message=pregunta,
                chat_history=[{"role": m.get("role", "user"), "message": m.get("content", "")} for m in self.context['mensajes_previos']],
                preamble=prompt_pyme
            )
            if respuesta_llm:
                return {"respuesta": reemplazar_placeholders(respuesta_llm, self.context['user_obj']), "fuente": "llm"}
        except Exception as e:
            logging.error(f"[PYMES] Error fatal en LLMHandler: {e}", exc_info=True)
        
        sugs = sugerencias_por_rubro(self.context['rubro_nombre'])
        return {"respuesta": "No encontré una respuesta directa. Probá con: " + " · ".join(f"“{s}”" for s in sugs if s), "fuente": "sugerencia_fallback"}

# --- FUNCIÓN PRINCIPAL: EL ORQUESTADOR ---
def responder_pyme(pregunta, user_obj, rubro_obj, session_obj=None, **kwargs):
    session = session_obj if session_obj is not None else flask_session
    
    context = {
        "session": session, "user_obj": user_obj, "rubro_obj": rubro_obj,
        "user_id": getattr(user_obj, "id", None),
        "nombre_pyme": getattr(user_obj, "nombre_empresa", "la empresa"),
        "telefono": getattr(user_obj, "telefono", ""), "direccion": getattr(user_obj, "direccion", ""),
        "email": getattr(user_obj, "email", ""), "plan": getattr(user_obj, "plan", "anonimo"),
        "preguntas_usadas": getattr(user_obj, "preguntas_usadas", 0),
        "limite_preguntas": getattr(user_obj, "limite_preguntas", 10),
        "rubro_nombre": getattr(rubro_obj, "nombre", "empresa"),
        "mensajes_previos": session.setdefault(NOMBRE_HISTORIAL_SESION, [])[-12:]
    }
    
    # El orden de la cadena es crucial para la eficiencia.
    handler_chain = [
        LimitHandler, FollowUpHandler, TicketStatusHandler, BrokenProductHandler, ClaimHandler,
        VectorCatalogHandler, FaqHandler, IntentHandler, LLMHandler
    ]

    respuesta_final = None
    for handler_class in handler_chain:
        handler_instance = handler_class(context)
        respuesta_final = handler_instance.handle(pregunta)
        if respuesta_final:
            break

    # Guardamos la conversación y actualizamos la sesión
    historial = session.setdefault(NOMBRE_HISTORIAL_SESION, [])
    historial.extend([
        {"role": "user", "content": pregunta},
        {"role": "assistant", "content": respuesta_final['respuesta']}
    ])
    if len(historial) > MAX_HISTORIAL_CHAT:
        session[NOMBRE_HISTORIAL_SESION] = historial[-MAX_HISTORIAL_CHAT:]
    session.modified = True
    
    try:
        db.session.add(Conversacion(
            user_id=context['user_id'], pregunta=pregunta,
            respuesta=respuesta_final['respuesta'],
            fuente=respuesta_final.get('fuente', 'desconocida'),
            rubro=context['rubro_nombre']
        ))
        db.session.commit()
    except Exception as e:
        logging.error(f"[PYMES] Error guardando conversación en DB: {e}")
        db.session.rollback()

    return respuesta_final