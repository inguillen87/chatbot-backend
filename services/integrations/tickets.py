"""
Integration with the ticketing system (SIGEM or internal).
"""
from services.ticket_service import servicio_tickets
from models import MunicipioTicket, TicketComentario, db
import logging

logger = logging.getLogger(__name__)

def create(data: dict) -> dict:
    """
    Creates a new ticket.
    `data` should be a dictionary with the fields required by `servicio_tickets.crear_nuevo_ticket`.
    """
    try:
        # We assume the ticket type is 'municipio' as per the context of this project.
        ticket = servicio_tickets.crear_nuevo_ticket(tipo_ticket="municipio", ticket_data=data)
        if ticket:
            # The spec requires a status_url. We'll construct a placeholder for now.
            status_url = f"https://example.com/ticket/{ticket.nro_ticket}"
            return {
                "id": f"M-{ticket.nro_ticket}",
                "status_url": status_url
            }
        else:
            return None
    except Exception as e:
        logger.error(f"Error creating ticket: {e}", exc_info=True)
        return None


def status(ticket_id: str) -> dict:
    """
    Checks the status of an existing ticket.
    `ticket_id` is the ticket number (e.g., "M-123456").
    """
    if not ticket_id or not ticket_id.upper().startswith('M-'):
        return None

    nro_ticket = ticket_id.split('-')[1]

    try:
        ticket = MunicipioTicket.query.filter_by(nro_ticket=nro_ticket).first()
        if not ticket:
            return None

        comentarios = TicketComentario.query.filter_by(municipio_ticket_id=ticket.id).order_by(TicketComentario.fecha.asc()).all()

        history = [
            {
                "date": c.fecha.isoformat(),
                "comment": c.comentario,
                "is_admin": c.es_admin
            } for c in comentarios
        ]

        return {
            "state": ticket.estado,
            "history": history
        }
    except Exception as e:
        logger.error(f"Error getting ticket status for {ticket_id}: {e}", exc_info=True)
        return None
