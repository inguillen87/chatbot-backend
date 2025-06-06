import re
from models import TicketComentario, MunicipioTicket
from extensions import db  # O tu módulo db real
from services.municipios import guardar_comentario
from services.municipios import buscar_ticket_por_nro

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
        ticket = buscar_ticket_por_nro(nro, user_id)
        if ticket:
            # Agregar comentario si viene con texto nuevo
            if len(pregunta.strip()) > len(nro) + 10:
                guardar_comentario(ticket.id, user_id, pregunta)
            msg = f"El ticket #{nro} está en estado: '{ticket.estado}'."
            if ticket.comentarios.count():
                ult_com = ticket.comentarios.order_by(TicketComentario.fecha.desc()).first()
                msg += f" Último comentario: {ult_com.comentario}"
            return {"respuesta": msg, "fuente": "consulta_estado_ticket", "ticket_obj": ticket}
        else:
            return {"respuesta": f"No existe el ticket #{nro}.", "fuente": "ticket_no_encontrado", "ticket_obj": None}
