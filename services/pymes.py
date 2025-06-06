# services/pymes.py

import logging
import re
import random
import datetime
from flask import session as flask_session
from models import Conversacion, PymeTicket, TicketComentario, db
from services.utils_placeholders import reemplazar_placeholders
from services.utils import sugerencias_por_rubro
from services.cohere_ai import get_cohere_response
from services.vector_search import buscar_item_vectorizado
from services.faq_matcher_spacy import buscar_en_faq_spacy
from services.intent_matcher import buscar_en_intents

NOMBRE_HISTORIAL_SESION = "historial_chat_cliente"
MAX_HISTORIAL_CHAT = 14

def limpiar_historial_sesion(nombre_historial=NOMBRE_HISTORIAL_SESION):
    try:
        if len(flask_session[nombre_historial]) > MAX_HISTORIAL_CHAT:
            flask_session[nombre_historial] = flask_session[nombre_historial][-MAX_HISTORIAL_CHAT:]
            flask_session.modified = True
    except Exception as e:
        logging.warning(f"[PYMES] Error limpiando historial: {e}")

def crear_ticket_pyme(pregunta, user_id, estado="nuevo", producto=None, cantidad=None, comentario=None):
    nro_ticket = random.randint(10000, 99999)
    ticket = PymeTicket(
        pregunta=pregunta,
        user_id=user_id,
        estado=estado,
        nro_ticket=nro_ticket,
        fecha=datetime.datetime.utcnow(),
        # producto y cantidad los podés agregar si querés, pero tu modelo PymeTicket NO tiene esos campos ahora
    )
    db.session.add(ticket)
    db.session.commit()
    if comentario:
        guardar_comentario_pyme(ticket.id, user_id, comentario)
    return ticket

def guardar_comentario_pyme(ticket_id, user_id, comentario):
    comentario_obj = TicketComentario(
        ticket_id=ticket_id,
        user_id=user_id,
        comentario=comentario,
        fecha=datetime.datetime.utcnow()
    )
    db.session.add(comentario_obj)
    db.session.commit()

def buscar_ticket_activo_pyme(user_id):
    return PymeTicket.query.filter(
        PymeTicket.user_id == user_id,
        PymeTicket.estado.in_(["nuevo", "en curso"])
    ).order_by(PymeTicket.fecha.desc()).first()

def buscar_ticket_por_nro_pyme(nro_ticket, user_id=None):
    q = PymeTicket.query.filter_by(nro_ticket=int(nro_ticket))
    if user_id:
        q = q.filter_by(user_id=user_id)
    return q.first()

def responder_pyme(pregunta, user_obj, rubro_obj, session_obj=None, **kwargs):
    session = session_obj if session_obj is not None else flask_session
    session.setdefault(NOMBRE_HISTORIAL_SESION, [])
    mensajes_previos = session[NOMBRE_HISTORIAL_SESION][-12:]
    user_id = getattr(user_obj, "id", None)
    nombre_pyme = getattr(user_obj, "nombre_empresa", "la empresa")
    telefono = getattr(user_obj, "telefono", "")
    direccion = getattr(user_obj, "direccion", "")
    email = getattr(user_obj, "email", "")
    plan = getattr(user_obj, "plan", "anonimo")
    preguntas_usadas = getattr(user_obj, "preguntas_usadas", 0)
    limite_preguntas = getattr(user_obj, "limite_preguntas", 10)
    rubro_nombre = getattr(rubro_obj, "nombre", "empresa")

    if preguntas_usadas >= limite_preguntas:
        return {"respuesta": "🔒 Límite de preguntas alcanzado en tu plan. Registrate o actualizá para más.", "fuente": "sistema_limite"}

    # --- 1. Consulta estado de ticket/pedido/reclamo
    ticket_match = re.search(r"(pedido|ticket|reclamo)[\s#]*([0-9]{4,7})", pregunta, re.IGNORECASE)
    if ticket_match:
        nro = ticket_match.group(2)
        ticket = buscar_ticket_por_nro_pyme(nro, user_id)
        if ticket:
            msg_estado = f"El ticket #{nro} está en estado: '{ticket.estado}'."
            if ticket.comentarios.count() > 0:
                ult_com = ticket.comentarios.order_by(TicketComentario.fecha.desc()).first()
                msg_estado += f" Último comentario: {ult_com.comentario}"
            return {"respuesta": msg_estado, "fuente": "consulta_estado_ticket"}
        else:
            return {"respuesta": f"No se encontró el pedido/reclamo #{nro}.", "fuente": "ticket_no_encontrado"}

    # --- 2. Reclamos
    palabras_reclamo = ["mal servicio", "problema", "no llegó", "demora", "defectuoso", "reclamo", "falló", "devolución", "cancelar"]
    if any(w in pregunta.lower() for w in palabras_reclamo):
        ticket = crear_ticket_pyme(pregunta, user_id, estado="nuevo", comentario=pregunta)
        respuesta = (
            f"Tu reclamo fue registrado con el número #{ticket.nro_ticket}. "
            "Vas a recibir novedades por este chat. ¿Querés agregar más detalles?"
        )
        session[NOMBRE_HISTORIAL_SESION].append({"role": "assistant", "content": respuesta})
        limpiar_historial_sesion()
        return {"respuesta": respuesta, "fuente": "registro_reclamo"}

    # --- 3. Pedido de productos/ofertas
    palabras_pedido = ["comprar", "precio", "pedido", "cotización", "presupuesto", "oferta", "disponible", "stock", "quiero"]
    if any(w in pregunta.lower() for w in palabras_pedido):
        prod_match = re.search(r"(?:vino|producto|caja|botella|pack|varietal|etiqueta)\s+([a-zA-Z0-9 ]+)", pregunta, re.IGNORECASE)
        cantidad_match = re.search(r"(\d+)\s*(cajas|botellas|unidades|packs)", pregunta)
        producto = prod_match.group(1).strip() if prod_match else None
        cantidad = cantidad_match.group(1) if cantidad_match else None

        respuesta_vector = ""
        try:
            respuesta_vector = buscar_item_vectorizado(pregunta, user_obj)
        except Exception as e:
            logging.warning(f"[PYMES] Error búsqueda vectorial: {e}")

        if respuesta_vector:
            respuesta = f"{respuesta_vector}\n¿Querés hacer un pedido? Te genero el pedido ahora mismo."
            if "quiero comprar" in pregunta.lower() or "hacer pedido" in pregunta.lower():
                ticket = crear_ticket_pyme(pregunta, user_id, estado="nuevo")
                respuesta += f"\nPedido generado. Tu número de pedido es: #{ticket.nro_ticket}"
            session[NOMBRE_HISTORIAL_SESION].append({"role": "assistant", "content": respuesta})
            limpiar_historial_sesion()
            return {"respuesta": respuesta, "fuente": "catalogo_vector"}

    # --- 4. FAQ matcher
    try:
        faq = buscar_en_faq_spacy(pregunta, getattr(rubro_obj, "id", 1))
        if faq and faq.answer:
            respuesta = reemplazar_placeholders(faq.answer, user_obj)
            session[NOMBRE_HISTORIAL_SESION].append({"role": "assistant", "content": respuesta})
            limpiar_historial_sesion()
            return {"respuesta": respuesta, "fuente": "faq"}
    except Exception as e:
        logging.warning(f"[PYMES] Error FAQ: {e}")

    # --- 5. Intents matcher
    try:
        intent_resp = buscar_en_intents(pregunta, getattr(rubro_obj, "nombre", "general"))
        if intent_resp:
            respuesta = reemplazar_placeholders(intent_resp, user_obj)
            session[NOMBRE_HISTORIAL_SESION].append({"role": "assistant", "content": respuesta})
            limpiar_historial_sesion()
            return {"respuesta": respuesta, "fuente": "intent"}
    except Exception as e:
        logging.warning(f"[PYMES] Error Intent: {e}")

    # --- 6. PROMPT EXTENDIDO LLM SOLO COMO ÚLTIMO RECURSO ---
    respuesta_llm = ""
    try:
        # **PROMPT EXTENDIDO: 30+ líneas de contexto realista**
        prompt_pyme = f"""
Chatboc es el agente digital oficial de ventas y atención de {nombre_pyme}, especializado en responder rápido, cerrar ventas, generar pedidos y resolver reclamos o consultas.
Nunca te presentes como bot ni IA. Siempre actuá como vendedor humano real.
1. Respondé de forma clara, persuasiva y amable, orientado a ventas.
2. Si consultan por un producto que existe, brindá precio, descripción y stock.
3. Si piden varios productos, preguntá cantidades y datos de entrega.
4. Si no hay stock, sugerí alternativas reales o toma el pedido como reservado.
5. Si consultan por promociones, ofertas o descuentos, informá si existen o explicá cómo acceder.
6. Registrá cada pedido con número y estado.
7. Para reclamos, creá ticket y brindá seguimiento. Preguntá detalles para resolver más rápido.
8. Si el cliente consulta por formas de pago, informá tarjetas, transferencia, links y efectivo.
9. Si piden factura A/B, confirmá si la empresa emite y cuáles son los datos requeridos.
10. Ante dudas o info faltante, sugerí contactar por WhatsApp o dejar datos para que un representante llame.
11. Siempre mencioná los canales de contacto: teléfono {telefono or 'no informado'}, email {email or 'no informado'}, dirección {direccion or 'no informada'}.
12. Si hay catálogo cargado, usá sus datos reales. Si no, sugerí navegar la web o pedir asesoramiento personalizado.
13. Si el cliente quiere una compra grande o especial, ofrecé beneficios por volumen y pedí un contacto directo.
14. Nunca inventes datos, pero sí podés dar ejemplos o precios aproximados si el catálogo no cubre la consulta.
15. Siempre recordá el historial reciente del chat para evitar respuestas repetidas y mostrar continuidad.
16. Nunca respondas “no sé” sin antes ofrecer alternativas reales o un canal de consulta humana.
17. Si el usuario pide hablar con humano, indicá que será derivado a un asesor comercial y registrá el ticket.
18. Respondé en argentino neutral, simple, directo y sin tecnicismos.
19. Recordá mostrar empatía y voluntad de solucionar cada consulta.
20. Si el cliente pide ayuda fuera de horario, sugerí canales asincrónicos (WhatsApp, email, formulario web).
21. En cada respuesta buscá avanzar el proceso de compra o cierre del reclamo.
22. Asegurate de ser breve, efectivo y útil, no repitas datos innecesarios.
23. Nunca digas “soy un asistente” ni “soy IA”.
24. Siempre terminá con CTA: ¿Querés que te ayude a concretar tu compra/pedido/reclamo?
25. En promociones, aclarar condiciones y fechas de vigencia.
26. En precios, aclarar si son finales, con o sin IVA, y mínimos de compra.
27. Ante info incompleta, ofrecé armar un presupuesto personalizado.
28. Para entregas, detallá días, zonas y costos si están cargados.
29. Si cliente pide visitar el local, pasá dirección y horario.
30. TODO pedido, consulta o reclamo se responde con seguimiento y número si aplica.
Historial reciente:
"""
        for msg in mensajes_previos:
            prompt_pyme += f"\n- {msg.get('role', 'user')}: {msg.get('content','')}"
        prompt_pyme += f"\n- Cliente: {pregunta}\n- Agente:"

        respuesta_llm = get_cohere_response(
            message=pregunta,
            chat_history=[{"role": m.get("role", "user"), "message": m.get("content", "")} for m in mensajes_previos],
            preamble=prompt_pyme,
            rubro_id=getattr(rubro_obj, "id", 1),
            user_context={
                "nombre_empresa": nombre_pyme,
                "telefono": telefono,
                "direccion": direccion,
                "email": email
            }
        )
        respuesta_llm = reemplazar_placeholders(respuesta_llm, user_obj)
    except Exception as e:
        logging.warning(f"[PYMES] Error LLM: {e}")
        respuesta_llm = ""

    if not respuesta_llm:
        sugs = sugerencias_por_rubro(getattr(rubro_obj, "nombre", "bodega"))
        respuesta_llm = "No encontré una respuesta directa. Probá con: " + " · ".join(f"“{s}”" for s in sugs if s)

    session[NOMBRE_HISTORIAL_SESION].extend([
        {"role": "user", "content": pregunta},
        {"role": "assistant", "content": respuesta_llm}
    ])
    limpiar_historial_sesion()
    session.modified = True

    db.session.add(Conversacion(
        user_id=user_id,
        pregunta=pregunta,
        respuesta=respuesta_llm,
        fuente="llm" if respuesta_llm else "sugerencia",
        rubro=rubro_nombre
    ))
    db.session.commit()

    return {
        "respuesta": respuesta_llm,
        "fuente": "llm" if respuesta_llm else "sugerencia"
    }
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
# Asumimos que estos servicios existen y funcionan como hemos hablado
from services.vector_search import buscar_item_vectorizado
from services.faq_matcher_spacy import buscar_en_faq_spacy
from services.intent_matcher import buscar_en_intents
from services.webinfo import obtener_info_web

# --- Constantes de Sesión ---
NOMBRE_HISTORIAL_SESION = "historial_chat_cliente"
CONTEXTO_PYME_SESION = "contexto_pyme" # Para manejar conversaciones de varios pasos
MAX_HISTORIAL_CHAT = 14

# --- PATRÓN DE DISEÑO: ORQUESTADOR CON MANEJADORES ---
# Cada "Handler" es un especialista en un tipo de pregunta.

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
            return {"respuesta": "🔒 Límite de preguntas alcanzado en tu plan. Registrate o actualizá para más.", "fuente": "sistema_limite"}
        return None

class FollowUpHandler(BaseHandler):
    """Manejador de seguimiento para conversaciones en curso (ej. agregar detalles a un reclamo)."""
    def handle(self, pregunta: str) -> dict | None:
        contexto_pyme = self.context['session'].get(CONTEXTO_PYME_SESION, {})
        if contexto_pyme.get('esperando_detalles_reclamo'):
            ticket_id = contexto_pyme['esperando_detalles_reclamo']
            guardar_comentario_pyme(ticket_id, self.context['user_id'], pregunta)
            self.context['session'][CONTEXTO_PYME_SESION] = {} # Limpiar contexto
            return {
                "respuesta": "Perfecto, he añadido tus comentarios al reclamo. Nuestro equipo lo revisará a la brevedad. ¿Puedo ayudarte con algo más?",
                "fuente": "detalle_reclamo_agregado"
            }
        return None

class TicketStatusHandler(BaseHandler):
    """Busca el estado de un ticket existente."""
    def handle(self, pregunta: str) -> dict | None:
        ticket_match = re.search(r"(pedido|ticket|reclamo|consulta)[\s#]*([0-9]{4,7})", pregunta, re.IGNORECASE)
        if ticket_match:
            nro = ticket_match.group(2)
            ticket = buscar_ticket_por_nro_pyme(nro, self.context['user_id'])
            if ticket:
                msg_estado = f"El ticket #{nro} está en estado: '{ticket.estado}'."
                if ticket.comentarios.count() > 0:
                    ult_com = ticket.comentarios.order_by(TicketComentario.fecha.desc()).first()
                    msg_estado += f" Último comentario: \"{ult_com.comentario}\""
                return {"respuesta": msg_estado, "fuente": "consulta_estado_ticket"}
            else:
                return {"respuesta": f"No se encontró ningún ticket o reclamo con el número #{nro}.", "fuente": "ticket_no_encontrado"}
        return None

class DetailedContactHandler(BaseHandler):
    """Responde preguntas detalladas de contacto usando la info completa del scraper."""
    def handle(self, pregunta: str) -> dict | None:
        palabras_clave = ["whatsapp", "instagram", "facebook", "redes sociales", "todos los teléfonos"]
        if any(palabra in pregunta.lower() for palabra in palabras_clave):
            info_completa = obtener_info_web(self.context['user_id'], self.context['link_web'])
            if info_completa:
                respuesta = "¡Claro! Aquí tienes la información de contacto que encontré:\n"
                redes = info_completa.get("links_redes", {})
                telefonos = info_completa.get("telefonos", [])
                
                encontramos_algo = False
                if redes:
                    respuesta += "".join([f"- {red.capitalize()}: {link}\n" for red, link in redes.items()])
                    encontramos_algo = True
                if len(telefonos) > 1:
                    respuesta += f"- Teléfonos: {', '.join(telefonos)}\n"
                    encontramos_algo = True

                if encontramos_algo:
                    return {"respuesta": respuesta, "fuente": "info_web_detallada"}
        return None

class ClaimHandler(BaseHandler):
    """Detecta y registra nuevos reclamos."""
    def handle(self, pregunta: str) -> dict | None:
        palabras_reclamo = ["mal servicio", "problema", "no llegó", "demora", "defectuoso", "reclamo", "falló", "devolución", "cancelar", "inconveniente", "queja"]
        if any(w in pregunta.lower() for w in palabras_reclamo):
            ticket = crear_ticket_pyme(pregunta, self.context['user_id'], estado="nuevo", tipo="reclamo", comentario=pregunta)
            respuesta = (
                f"Lamento mucho el inconveniente. He generado un reclamo con el número de ticket #{ticket.nro_ticket}. "
                "Para poder ayudarte mejor, ¿podrías darme más detalles sobre lo que ocurrió? Tu próximo mensaje se agregará al reclamo."
            )
            self.context['session'][CONTEXTO_PYME_SESION] = {'esperando_detalles_reclamo': ticket.id}
            return {"respuesta": respuesta, "fuente": "registro_reclamo"}
        return None

class VectorCatalogHandler(BaseHandler):
    """Busca en el catálogo de productos usando búsqueda vectorial."""
    def handle(self, pregunta: str) -> dict | None:
        palabras_pedido = ["comprar", "precio", "pedido", "cotización", "presupuesto", "oferta", "disponible", "stock", "quiero", "cuánto sale", "tenés"]
        if any(w in pregunta.lower() for w in palabras_pedido):
            try:
                respuesta_vector = buscar_item_vectorizado(pregunta, self.context['user_obj'])
                if respuesta_vector:
                    respuesta = f"{respuesta_vector}\n¿Querés que te genere un pedido con esto?"
                    # Aquí podrías añadir una lógica de confirmación en un segundo paso.
                    return {"respuesta": respuesta, "fuente": "catalogo_vector"}
            except Exception as e:
                logging.warning(f"[PYMES] Error en VectorCatalogHandler: {e}")
        return None
        
class FaqHandler(BaseHandler):
    """Busca en las Preguntas Frecuentes (FAQs)."""
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
    """El último recurso: llama al Large Language Model (Cohere)."""
    def handle(self, pregunta: str) -> dict | None:
        try:
            prompt_pyme = f"""
            Eres "Chatboc", el agente de ventas y atención al cliente de {self.context['nombre_pyme']}. Tu objetivo es vender, resolver consultas y ser extremadamente eficiente.
            
            **Tus Datos de Contacto (¡Úsalos siempre!):**
            - Teléfono: {self.context['telefono'] or "No informado. Pide al cliente que aguarde para que un representante lo contacte."}
            - Email: {self.context['email'] or "No informado."}
            - Dirección física: {self.context['direccion'] or "No informada. Menciona que operamos principalmente online o con envío."}

            **Tus Reglas de Oro:**
            1. Actúa como un humano, no como un bot. Usa un tono amable, profesional y argentino.
            2. Sé proactivo. Si un producto está disponible, ofrece generar el pedido. Si hay un reclamo, pide detalles.
            3. Usa tus datos. Menciona siempre los canales de contacto cuando sea relevante.
            4. Nunca inventes. Si no sabes algo, ofrece buscarlo o derivar la consulta. Di "Déjame que lo verifico" en lugar de "No sé".
            5. Crea tickets. Todo reclamo o pedido importante debe generar un ticket. Informa siempre el número.
            6. Sé un vendedor. Facilita la compra, describe productos, informa precios del catálogo.
            7. Continúa la conversación. Revisa el historial reciente para no repetir preguntas.

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
        
        # Fallback definitivo si el LLM falla o no responde
        sugs = sugerencias_por_rubro(self.context['rubro_nombre'])
        respuesta_fallback = "No encontré una respuesta directa. Probá con: " + " · ".join(f"“{s}”" for s in sugs if s)
        return {"respuesta": respuesta_fallback, "fuente": "sugerencia_fallback"}

# --- FUNCIÓN PRINCIPAL: EL ORQUESTADOR ---

def responder_pyme(pregunta, user_obj, rubro_obj, session_obj=None, **kwargs):
    session = session_obj if session_obj is not None else flask_session
    
    # 1. Preparamos el contexto que usarán todos los manejadores
    context = {
        "session": session,
        "user_obj": user_obj,
        "rubro_obj": rubro_obj,
        "user_id": getattr(user_obj, "id", None),
        "nombre_pyme": getattr(user_obj, "nombre_empresa", "la empresa"),
        "telefono": getattr(user_obj, "telefono", ""),
        "direccion": getattr(user_obj, "direccion", ""),
        "email": getattr(user_obj, "email", ""),
        "link_web": getattr(user_obj, "link_web", ""),
        "plan": getattr(user_obj, "plan", "anonimo"),
        "preguntas_usadas": getattr(user_obj, "preguntas_usadas", 0),
        "limite_preguntas": getattr(user_obj, "limite_preguntas", 10),
        "rubro_nombre": getattr(rubro_obj, "nombre", "empresa"),
        "mensajes_previos": session.setdefault(NOMBRE_HISTORIAL_SESION, [])[-12:]
    }
    
    # 2. Definimos la cadena de especialistas (manejadores) en orden de prioridad
    # Este orden es crucial y ahora es muy fácil de cambiar o ampliar.
    handler_chain = [
        LimitHandler,
        FollowUpHandler,      # Importante: chequear si estamos en medio de una charla primero.
        TicketStatusHandler,
        DetailedContactHandler, # Responder preguntas específicas de contacto antes de reclamos generales.
        ClaimHandler,
        VectorCatalogHandler, # Búsqueda de productos es una de las acciones principales.
        FaqHandler,
        IntentHandler,
        LLMHandler            # El LLM es siempre el último recurso.
    ]

    # 3. El Orquestador recorre la cadena
    respuesta_final = None
    for handler_class in handler_chain:
        handler_instance = handler_class(context)
        respuesta_final = handler_instance.handle(pregunta)
        if respuesta_final:
            break  # El primer manejador que da una respuesta, gana.

    # 4. Guardamos la conversación y actualizamos la sesión
    historial = session[NOMBRE_HISTORIAL_SESION]
    historial.extend([
        {"role": "user", "content": pregunta},
        {"role": "assistant", "content": respuesta_final['respuesta']}
    ])
    if len(historial) > MAX_HISTORIAL_CHAT:
        session[NOMBRE_HISTORIAL_SESION] = historial[-MAX_HISTORIAL_CHAT:]
    session.modified = True
    
    try:
        db.session.add(Conversacion(
            user_id=context['user_id'],
            pregunta=pregunta,
            respuesta=respuesta_final['respuesta'],
            fuente=respuesta_final.get('fuente', 'desconocida'),
            rubro=context['rubro_nombre']
        ))
        db.session.commit()
    except Exception as e:
        logging.error(f"[PYMES] Error guardando conversación en DB: {e}")
        db.session.rollback()

    return respuesta_final


# --- FUNCIONES AUXILIARES (SE MANTIENEN IGUAL) ---

def crear_ticket_pyme(pregunta, user_id, estado="nuevo", tipo="indefinido", comentario=None):
    nro_ticket = random.randint(10000, 99999)
    ticket = PymeTicket(
        pregunta=pregunta,
        user_id=user_id,
        estado=estado,
        nro_ticket=nro_ticket,
        tipo=tipo,
        fecha=datetime.datetime.utcnow(),
    )
    db.session.add(ticket)
    db.session.commit()
    if comentario:
        guardar_comentario_pyme(ticket.id, user_id, comentario)
    return ticket

def guardar_comentario_pyme(ticket_id, user_id, comentario):
    comentario_obj = TicketComentario(
        ticket_id=ticket_id,
        user_id=user_id,
        comentario=comentario,
        fecha=datetime.datetime.utcnow()
    )
    db.session.add(comentario_obj)
    db.session.commit()

def buscar_ticket_por_nro_pyme(nro_ticket, user_id=None):
    try:
        q = PymeTicket.query.filter_by(nro_ticket=int(nro_ticket))
        if user_id:
            q = q.filter_by(user_id=user_id)
        return q.first()
    except (ValueError, TypeError):
        return None# services/pymes.py

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
# Asumimos que estos servicios existen y funcionan como hemos hablado
from services.vector_search import buscar_item_vectorizado
from services.faq_matcher_spacy import buscar_en_faq_spacy
from services.intent_matcher import buscar_en_intents
# NUEVO: podrías tener un servicio que extrae info detallada de la web del cliente
# from services.webinfo import obtener_info_web 

# --- Constantes de Sesión ---
NOMBRE_HISTORIAL_SESION = "historial_chat_cliente"
CONTEXTO_PYME_SESION = "contexto_pyme" # Para manejar conversaciones de varios pasos
MAX_HISTORIAL_CHAT = 14

# --- PATRÓN DE DISEÑO: ORQUESTADOR CON MANEJADORES ---
# Cada "Handler" es un especialista en un tipo de pregunta.

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
            return {"respuesta": "🔒 Límite de preguntas alcanzado en tu plan. Registrate o actualizá para más.", "fuente": "sistema_limite"}
        return None

class FollowUpHandler(BaseHandler):
    """
    ¡NUEVA CAPACIDAD! Manejador de seguimiento para conversaciones en curso.
    Ej: si el bot acaba de pedir más detalles de un reclamo, este handler captura la siguiente respuesta.
    """
    def handle(self, pregunta: str) -> dict | None:
        contexto_pyme = self.context['session'].get(CONTEXTO_PYME_SESION, {})
        if contexto_pyme.get('esperando_detalles_reclamo'):
            ticket_id = contexto_pyme['esperando_detalles_reclamo']
            guardar_comentario_pyme(ticket_id, self.context['user_id'], pregunta)
            self.context['session'][CONTEXTO_PYME_SESION] = {} # Limpiar contexto
            self.context['session'].modified = True
            return {
                "respuesta": "Perfecto, he añadido tus comentarios al reclamo. Nuestro equipo lo revisará a la brevedad. ¿Puedo ayudarte con algo más?",
                "fuente": "detalle_reclamo_agregado"
            }
        # Aquí podrías agregar más lógicas de seguimiento, ej: 'esperando_confirmacion_pedido'
        return None

class TicketStatusHandler(BaseHandler):
    """Busca el estado de un ticket existente."""
    def handle(self, pregunta: str) -> dict | None:
        ticket_match = re.search(r"(pedido|ticket|reclamo|consulta)[\s#]*([0-9]{4,7})", pregunta, re.IGNORECASE)
        if ticket_match:
            nro = ticket_match.group(2)
            ticket = buscar_ticket_por_nro_pyme(nro, self.context['user_id'])
            if ticket:
                msg_estado = f"El ticket #{nro} (tipo: {ticket.tipo}) está en estado: '{ticket.estado}'."
                if ticket.comentarios.count() > 0:
                    ult_com = ticket.comentarios.order_by(TicketComentario.fecha.desc()).first()
                    msg_estado += f" Último comentario: \"{ult_com.comentario}\""
                return {"respuesta": msg_estado, "fuente": "consulta_estado_ticket"}
            else:
                return {"respuesta": f"No se encontró ningún ticket o reclamo con el número #{nro}.", "fuente": "ticket_no_encontrado"}
        return None

class ClaimHandler(BaseHandler):
    """Detecta y registra nuevos reclamos, iniciando una conversación de seguimiento."""
    def handle(self, pregunta: str) -> dict | None:
        palabras_reclamo = ["mal servicio", "problema", "no llegó", "demora", "defectuoso", "reclamo", "falló", "devolución", "cancelar", "inconveniente", "queja"]
        if any(w in pregunta.lower() for w in palabras_reclamo):
            ticket = crear_ticket_pyme(pregunta, self.context['user_id'], estado="nuevo", tipo="reclamo", comentario=pregunta)
            respuesta = (
                f"Lamento mucho el inconveniente. He generado un reclamo con el número de ticket #{ticket.nro_ticket}. "
                "Para poder ayudarte mejor, ¿podrías darme más detalles sobre lo que ocurrió? Tu próximo mensaje se agregará al reclamo."
            )
            # Guardamos en sesión que estamos esperando una respuesta
            self.context['session'][CONTEXTO_PYME_SESION] = {'esperando_detalles_reclamo': ticket.id}
            self.context['session'].modified = True
            return {"respuesta": respuesta, "fuente": "registro_reclamo"}
        return None

class VectorCatalogHandler(BaseHandler):
    """Busca en el catálogo de productos usando búsqueda vectorial."""
    def handle(self, pregunta: str) -> dict | None:
        palabras_pedido = ["comprar", "precio", "pedido", "cotización", "presupuesto", "oferta", "disponible", "stock", "quiero", "cuánto sale", "tenés", "vino", "caja", "botella"]
        # Usamos un umbral para que no se active con cualquier palabra suelta.
        if sum(1 for w in palabras_pedido if w in pregunta.lower().split()) >= 1:
            try:
                # La función de búsqueda debe devolver un objeto estructurado, no solo texto.
                # Por ejemplo: [{"nombre": "Malbec Reserva", "precio": "5000"}, ...]
                resultados = buscar_item_vectorizado(pregunta, self.context['user_id']) 
                if resultados:
                    # Formateamos la respuesta para que sea clara y accionable.
                    respuesta_texto = "¡Claro! Encontré esto en nuestro catálogo:\n"
                    respuesta_texto += "\n".join([f"- **{item.payload['nombre']}**: ${item.payload['precio_str']}" for item in resultados])
                    respuesta_texto += "\n\n¿Te gustaría que genere un pedido con alguno de estos productos?"
                    # Idealmente, aquí devolverías una estructura con botones para el frontend.
                    return {"respuesta": respuesta_texto, "fuente": "catalogo_vector"}
            except Exception as e:
                logging.error(f"[PYMES] Error fatal en VectorCatalogHandler: {e}", exc_info=True)
        return None

class FaqHandler(BaseHandler):
    """Busca en las Preguntas Frecuentes (FAQs)."""
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
    """El último recurso: llama al Large Language Model (Cohere) con un prompt optimizado."""
    def handle(self, pregunta: str) -> dict | None:
        try:
            # PROMPT MÁS LIMPIO Y DIRECTO
            prompt_pyme = f"""
Eres "Chatboc", el agente de ventas y atención al cliente de {self.context['nombre_pyme']}. Tu objetivo es vender, resolver consultas y ser extremadamente eficiente.

**Tus Datos de Contacto (¡Úsalos siempre!):**
- Teléfono: {self.context['telefono'] or "No informado. Pide al cliente que aguarde para que un representante lo contacte."}
- Email: {self.context['email'] or "No informado."}
- Dirección física: {self.context['direccion'] or "No informada. Menciona que operamos principalmente online o con envío."}

**Tus Reglas de Oro:**
1.  **Actúa como un humano**, no como un bot. Usa un tono amable, profesional y argentino.
2.  **Sé proactivo.** Si un producto está disponible, ofrece generar el pedido. Si hay un reclamo, pide detalles.
3.  **Usa tus datos.** Menciona siempre los canales de contacto cuando sea relevante.
4.  **Nunca inventes.** Si no sabes algo, ofrece buscarlo o derivar la consulta. Di "Déjame que lo verifico" en lugar de "No sé".
5.  **Crea tickets.** Todo reclamo o pedido importante debe generar un ticket. Informa siempre el número.
6.  **Sé un vendedor.** Facilita la compra, describe productos, informa precios del catálogo.
7.  **Continúa la conversación.** Revisa el historial reciente para no repetir preguntas.

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
        
        # Fallback definitivo si el LLM falla o no responde
        sugs = sugerencias_por_rubro(self.context['rubro_nombre'])
        respuesta_fallback = "No encontré una respuesta directa. Probá con: " + " · ".join(f"“{s}”" for s in sugs if s)
        return {"respuesta": respuesta_fallback, "fuente": "sugerencia_fallback"}

# --- FUNCIÓN PRINCIPAL: EL ORQUESTADOR ---

def responder_pyme(pregunta, user_obj, rubro_obj, session_obj=None, **kwargs):
    session = session_obj if session_obj is not None else flask_session
    
    # 1. Preparamos el contexto que usarán todos los manejadores
    context = {
        "session": session,
        "user_obj": user_obj,
        "rubro_obj": rubro_obj,
        "user_id": getattr(user_obj, "id", None),
        "nombre_pyme": getattr(user_obj, "nombre_empresa", "la empresa"),
        "telefono": getattr(user_obj, "telefono", ""),
        "direccion": getattr(user_obj, "direccion", ""),
        "email": getattr(user_obj, "email", ""),
        "plan": getattr(user_obj, "plan", "anonimo"),
        "preguntas_usadas": getattr(user_obj, "preguntas_usadas", 0),
        "limite_preguntas": getattr(user_obj, "limite_preguntas", 10),
        "rubro_nombre": getattr(rubro_obj, "nombre", "empresa"),
        "mensajes_previos": session.setdefault(NOMBRE_HISTORIAL_SESION, [])[-12:]
    }
    
    # 2. Definimos la cadena de especialistas (manejadores) en orden de prioridad
    # ¡Este orden es crucial y ahora es muy fácil de cambiar o ampliar!
    handler_chain = [
        LimitHandler,
        FollowUpHandler,          # Importante: chequear si estamos en medio de una charla primero.
        TicketStatusHandler,
        ClaimHandler,
        VectorCatalogHandler,     # Búsqueda de productos es una de las acciones principales.
        FaqHandler,
        IntentHandler,
        LLMHandler                # El LLM es siempre el último recurso.
    ]

    # 3. El Orquestador recorre la cadena
    respuesta_final = None
    for handler_class in handler_chain:
        handler_instance = handler_class(context)
        respuesta_final = handler_instance.handle(pregunta)
        if respuesta_final:
            break  # El primer manejador que da una respuesta, gana.

    # 4. Guardamos la conversación y actualizamos la sesión
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
            user_id=context['user_id'],
            pregunta=pregunta,
            respuesta=respuesta_final['respuesta'],
            fuente=respuesta_final.get('fuente', 'desconocida'),
            rubro=context['rubro_nombre']
        ))
        db.session.commit()
    except Exception as e:
        logging.error(f"[PYMES] Error guardando conversación en DB: {e}")
        db.session.rollback()

    return respuesta_final


# --- FUNCIONES AUXILIARES (Refactorizadas para mayor claridad) ---

def crear_ticket_pyme(pregunta: str, user_id: int, estado: str = "nuevo", tipo: str = "indefinido", comentario: str = None) -> PymeTicket:
    """Crea un nuevo ticket en la base de datos con un tipo específico."""
    nro_ticket = random.randint(10000, 99999)
    ticket = PymeTicket(
        pregunta=pregunta,
        user_id=user_id,
        estado=estado,
        nro_ticket=nro_ticket,
        tipo=tipo,  # Campo importante para clasificar (venta, reclamo, consulta)
        fecha=datetime.datetime.utcnow(),
    )
    db.session.add(ticket)
    db.session.commit()
    if comentario:
        guardar_comentario_pyme(ticket.id, user_id, comentario)
    return ticket

def guardar_comentario_pyme(ticket_id: int, user_id: int, comentario: str):
    """Guarda un comentario asociado a un ticket."""
    comentario_obj = TicketComentario(
        ticket_id=ticket_id,
        user_id=user_id,
        comentario=comentario,
        fecha=datetime.datetime.utcnow()
    )
    db.session.add(comentario_obj)
    db.session.commit()

def buscar_ticket_por_nro_pyme(nro_ticket: str, user_id: int = None) -> PymeTicket | None:
    """Busca un ticket por su número, manejando posibles errores de conversión."""
    try:
        q = PymeTicket.query.filter_by(nro_ticket=int(nro_ticket))
        if user_id:
            q = q.filter_by(user_id=user_id)
        return q.first()
    except (ValueError, TypeError):
        return None