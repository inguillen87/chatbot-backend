# services/ticket_service.py
import random
from datetime import datetime
from typing import Dict, Any, Literal, Union
import logging

from models import MunicipioTicket, PymeTicket, TicketComentario, db
from sqlalchemy.exc import SQLAlchemyError

logger = logging.getLogger(__name__)

class TicketCreator:
    def create(self, ticket_data: Dict[str, Any]) -> Union[PymeTicket, MunicipioTicket]:
        raise NotImplementedError

class MunicipioTicketCreator(TicketCreator):
    """Estrategia para crear tickets de Municipio usando los campos del modelo."""
    def create(self, ticket_data: Dict[str, Any]) -> MunicipioTicket:
        return MunicipioTicket(
            user_id=ticket_data.get("user_id"),
            asunto=ticket_data.get("asunto", "Sin Asunto"),
            categoria=ticket_data.get("categoria", "General"),
            # CORRECCIÓN: El modelo usa 'pregunta' para los detalles, no 'detalles'.
            pregunta=ticket_data.get("detalles", ""), 
            nro_ticket=ticket_data.get("nro_ticket")
        )

class PymeTicketCreator(TicketCreator):
    """Estrategia para crear tickets de Pyme."""
    def create(self, ticket_data: Dict[str, Any]) -> PymeTicket:
        # Esta versión ya era correcta, la mantenemos.
        return PymeTicket(
            user_id=ticket_data.get("user_id"),
            asunto=ticket_data.get("asunto", "Sin Asunto"),
            categoria=ticket_data.get("categoria", "General"),
            pregunta=ticket_data.get("pregunta"),
            nro_ticket=ticket_data.get("nro_ticket"),
            rubro_id=ticket_data.get("rubro_id")
        )

class ServicioTickets:
    def __init__(self):
        self.creators: Dict[str, TicketCreator] = {
            "municipio": MunicipioTicketCreator(),
            "pyme": PymeTicketCreator()
        }

    def crear_nuevo_ticket(self, tipo_ticket: Literal["municipio", "pyme"], ticket_data: Dict[str, Any]) -> Union[PymeTicket, MunicipioTicket, None]:
        creator = self.creators.get(tipo_ticket)
        if not creator: raise ValueError(f"Tipo de ticket inválido: '{tipo_ticket}'.")
        
        ticket_data["nro_ticket"] = random.randint(100000, 999999)
        
        try:
            ticket = creator.create(ticket_data)
            db.session.add(ticket)
            db.session.flush()

            if ticket_data.get("comentario"):
                comentario = TicketComentario(
                    comentario=ticket_data.get("comentario"),
                    user_id=ticket_data.get("user_id"),
                    es_agente=False
                )
                # MEJORA: Usar la relación directa es más limpio.
                if tipo_ticket == "municipio":
                    comentario.municipio_ticket = ticket
                else:
                    comentario.pyme_ticket = ticket
                db.session.add(comentario)

            db.session.commit()
            logger.info(f"Ticket #{ticket.nro_ticket} creado.")
            return ticket
        except SQLAlchemyError as e:
            db.session.rollback()
            logger.error(f"Error de DB al crear ticket: {e}", exc_info=True)
            return None

    def crear_comentario(self, ticket_id: int, tipo_ticket: Literal["municipio", "pyme"], comentario_data: Dict[str, Any]) -> TicketComentario | None:
        TicketModel = MunicipioTicket if tipo_ticket == "municipio" else PymeTicket
        ticket = db.session.get(TicketModel, ticket_id)
        if not ticket: return None
        try:
            nuevo_comentario = TicketComentario(
                comentario=comentario_data.get("comentario"),
                user_id=comentario_data.get("user_id"),
                es_agente=comentario_data.get("es_agente", False)
            )
            # MEJORA: Usar la relación directa es más limpio.
            if tipo_ticket == "municipio":
                nuevo_comentario.municipio_ticket = ticket
            else:
                nuevo_comentario.pyme_ticket = ticket
            db.session.add(nuevo_comentario)
            db.session.commit()
            return nuevo_comentario
        except SQLAlchemyError as e:
            db.session.rollback()
            logger.error(f"Error de DB al crear comentario: {e}", exc_info=True)
            return None

servicio_tickets = ServicioTickets()