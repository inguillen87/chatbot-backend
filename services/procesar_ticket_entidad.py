import re
from models import TicketComentario, MunicipioTicket
from extensions import db  # O tu módulo db real
import datetime
from models import TicketComentario, MunicipioTicket, db

def guardar_comentario(ticket_id, user_id, comentario):
    comentario_obj = TicketComentario(
        ticket_id=ticket_id,
        user_id=user_id,
        comentario=comentario,
        fecha=datetime.datetime.utcnow()
    )
    db.session.add(comentario_obj)
    db.session.commit()

def buscar_ticket_por_nro(nro_ticket, consulta_pin, user_id=None):
    q = MunicipioTicket.query.filter_by(nro_ticket=int(nro_ticket), consulta_pin=consulta_pin)
    if user_id:
        q = q.filter_by(user_id=user_id)
    return q.first()

def procesar_ticket_entidad(pregunta, user_obj):
    """
    - Detecta si es seguimiento (ticket existente), reclamo nuevo o comentario adicional.
    - Actualiza o responde según corresponda.
    - Retorna dict: {respuesta, fuente, ticket_obj}
    """
    user_id = user_obj.id
    # Detectar referencia a ticket/reclamo existente
    ticket_match = re.search(r"(ticket|reclamo)[\s#]*([0-9]{4,7})", pregunta, re.IGNORECASE)
    if ticket_match:
        nro = ticket_match.group(2)
        pin_match = re.search(r"pin[\s#]*([0-9]{6})", pregunta, re.IGNORECASE)
        if not pin_match:
            return {"respuesta": "Para consultar el estado necesito el PIN del ticket.", "fuente": "pin_requerido", "ticket_obj": None}
        pin = pin_match.group(1)
        ticket = buscar_ticket_por_nro(nro, pin, user_id)
        if ticket:
            if len(pregunta.strip()) > len(nro) + 10:
                guardar_comentario(ticket.id, user_id, pregunta)
            msg = f"El ticket #{nro} está en estado: '{ticket.estado}'."
            if ticket.comentarios.count():
                ult_com = ticket.comentarios.order_by(TicketComentario.fecha.desc()).first()
                msg += f" Último comentario: {ult_com.comentario}"
            return {"respuesta": msg, "fuente": "consulta_estado_ticket", "ticket_obj": ticket}
        else:
            return {"respuesta": f"No existe el ticket #{nro} o el PIN es incorrecto.", "fuente": "ticket_no_encontrado", "ticket_obj": None}
