# services/ticket_service.py

import random
from datetime import datetime
from typing import Dict, Any, Literal, Union
import logging

from models import MunicipioTicket, PymeTicket, TicketComentario, db
from sqlalchemy.exc import SQLAlchemyError

logger = logging.getLogger(__name__)

# --- Interfaces y Estrategias de Creación ---

class TicketCreator:
    """Clase base abstracta para los creadores de tickets."""
    def create(self, ticket_data: Dict[str, Any]) -> Union[PymeTicket, MunicipioTicket]:
        raise NotImplementedError

class MunicipioTicketCreator(TicketCreator):
    """Estrategia para crear tickets de Municipio, ahora con campos enriquecidos."""
    def create(self, ticket_data: Dict[str, Any]) -> MunicipioTicket:
        return MunicipioTicket(
            user_id=ticket_data.get("user_id"),
            asunto=ticket_data.get("asunto", "Ticket sin asunto"),
            categoria=ticket_data.get("categoria", "General"),
            detalles=ticket_data.get("detalles", ticket_data.get("pregunta")), # Usa 'detalles', o 'pregunta' como fallback
            estado="nuevo",
            nro_ticket=ticket_data.get("nro_ticket"),
            fecha=datetime.now()
        )

class PymeTicketCreator(TicketCreator):
    """Estrategia para crear tickets de Pyme (también enriquecida)."""
    def create(self, ticket_data: Dict[str, Any]) -> PymeTicket:
        return PymeTicket(
            user_id=ticket_data.get("user_id"),
            asunto=ticket_data.get("asunto", "Ticket sin asunto"),
            categoria=ticket_data.get("categoria", "General"),
            pregunta=ticket_data.get("pregunta"), # Mantenemos pregunta por compatibilidad
            estado="nuevo",
            nro_ticket=ticket_data.get("nro_ticket"),
            fecha=datetime.now(),
            rubro_id=ticket_data.get("rubro_id")
        )

# --- Clase de Servicio Principal ---

class ServicioTickets:
    def __init__(self):
        self.creators: Dict[str, TicketCreator] = {
            "municipio": MunicipioTicketCreator(),
            "pyme": PymeTicketCreator()
        }

    def crear_nuevo_ticket(self, tipo_ticket: Literal["municipio", "pyme"], ticket_data: Dict[str, Any]) -> Union[PymeTicket, MunicipioTicket, None]:
        """
        Crea un nuevo ticket y su primer comentario en una transacción atómica.
        DEVUELVE EL OBJETO TICKET COMPLETO O NONE SI FALLA.
        """
        creator = self.creators.get(tipo_ticket)
        if not creator:
            raise ValueError(f"Tipo de ticket inválido: '{tipo_ticket}'.")

        ticket_data["nro_ticket"] = random.randint(100000, 999999)
        
        try:
            ticket = creator.create(ticket_data)
            db.session.add(ticket)
            db.session.flush() # Para obtener el ID del ticket antes del commit

            if ticket_data.get("comentario"):
                comentario = TicketComentario(
                    comentario=ticket_data.get("comentario"),
                    user_id=ticket_data.get("user_id"),
                    es_agente=False # Los comentarios iniciales son siempre del usuario/vecino
                )
                if tipo_ticket == "municipio":
                    comentario.municipio_ticket = ticket
                else:
                    comentario.pyme_ticket = ticket
                db.session.add(comentario)

            db.session.commit()
            logger.info(f"Ticket #{ticket.nro_ticket} (tipo: {tipo_ticket}) creado exitosamente.")
            return ticket # <-- CORRECCIÓN CLAVE: Devuelve el objeto completo
            
        except SQLAlchemyError as e:
            db.session.rollback()
            logger.error(f"Error de DB al crear ticket tipo '{tipo_ticket}': {e}", exc_info=True)
            return None # Devuelve None en caso de error

    def crear_comentario(self, ticket_id: int, tipo_ticket: Literal["municipio", "pyme"], comentario_data: Dict[str, Any]) -> TicketComentario | None:
        """
        Crea un nuevo comentario para un ticket existente, diferenciando si es de un agente.
        """
        TicketModel = MunicipioTicket if tipo_ticket == "municipio" else PymeTicket
        ticket = db.session.get(TicketModel, ticket_id)
        if not ticket:
            logger.error(f"Intento de añadir comentario a ticket inexistente ID {ticket_id} (tipo: {tipo_ticket})")
            return None

        try:
            nuevo_comentario = TicketComentario(
                comentario=comentario_data.get("comentario"),
                user_id=comentario_data.get("user_id"), # El ID del vecino o del agente logueado en el panel
                es_agente=comentario_data.get("es_agente", False) # <-- Nuevo campo para diferenciar
            )
            
            if tipo_ticket == "municipio":
                nuevo_comentario.municipio_ticket = ticket
            else:
                nuevo_comentario.pyme_ticket = ticket

            db.session.add(nuevo_comentario)
            db.session.commit()
            
            logger.info(f"Comentario añadido al ticket_id {ticket_id} (tipo: {tipo_ticket}).")
            return nuevo_comentario
        except SQLAlchemyError as e:
            db.session.rollback()
            logger.error(f"Error de DB al crear comentario para ticket ID {ticket_id}: {e}", exc_info=True)
            return None

# --- Instancia única del servicio ---
servicio_tickets = ServicioTickets()