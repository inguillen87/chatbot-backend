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

def crear_ticket_pyme(tipo, pregunta, user_id, estado="nuevo", producto=None, cantidad=None, comentario=None):
    nro_ticket = random.randint(10000, 99999)
    ticket = PymeTicket(
        tipo=tipo,
        pregunta=pregunta,
        user_id=user_id,
        estado=estado,
        nro_ticket=nro_ticket,
        fecha=datetime.datetime.utcnow(),
        producto=producto,
        cantidad=cantidad,
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

def buscar_ticket_activo_pyme(user_id, tipo="pedido"):
    return PymeTicket.query.filter(
        PymeTicket.user_id == user_id,
        PymeTicket.tipo == tipo,
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
            msg_estado = f"El {ticket.tipo} #{nro} está en estado: '{ticket.estado}'."
            if ticket.comentarios.count() > 0:
                ult_com = ticket.comentarios.order_by(TicketComentario.fecha.desc()).first()
                msg_estado += f" Último comentario: {ult_com.comentario}"
            return {"respuesta": msg_estado, "fuente": "consulta_estado_ticket"}
        else:
            return {"respuesta": f"No se encontró el pedido/reclamo #{nro}.", "fuente": "ticket_no_encontrado"}

    # --- 2. Reclamos
    palabras_reclamo = ["mal servicio", "problema", "no llegó", "demora", "defectuoso", "reclamo", "falló", "devolución", "cancelar"]
    if any(w in pregunta.lower() for w in palabras_reclamo):
        ticket = crear_ticket_pyme("reclamo", pregunta, user_id, estado="nuevo", comentario=pregunta)
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
                ticket = crear_ticket_pyme("pedido", pregunta, user_id, estado="nuevo", producto=producto, cantidad=cantidad)
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
