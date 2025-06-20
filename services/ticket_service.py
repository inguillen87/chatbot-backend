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
    def create(self, ticket_data: Dict[str, Any]) -> MunicipioTicket:
        lat = (
            ticket_data.get("latitud")
            or ticket_data.get("lat")
            or ticket_data.get("latitude")
        )
        lon = (
            ticket_data.get("longitud")
            or ticket_data.get("lon")
            or ticket_data.get("lng")
            or ticket_data.get("longitude")
        )
        return MunicipioTicket(
            user_id=ticket_data.get("user_id"),
            anon_id=ticket_data.get("anon_id"),
            asunto=ticket_data.get("asunto", "Sin Asunto"),
            categoria=ticket_data.get("categoria", "General"),
            pregunta=ticket_data.get("pregunta", ""),    # Reclamo original
            detalles=ticket_data.get("detalles", ""),    # Dirección, nombre, tel, etc.
            nro_ticket=ticket_data.get("nro_ticket"),
            direccion=ticket_data.get("direccion"),
            latitud=lat,
            longitud=lon
        )

class PymeTicketCreator(TicketCreator):
    def create(self, ticket_data: Dict[str, Any]) -> PymeTicket:
        lat = (
            ticket_data.get("latitud")
            or ticket_data.get("lat")
            or ticket_data.get("latitude")
        )
        lon = (
            ticket_data.get("longitud")
            or ticket_data.get("lon")
            or ticket_data.get("lng")
            or ticket_data.get("longitude")
        )
        return PymeTicket(
            user_id=ticket_data.get("user_id"),
            anon_id=ticket_data.get("anon_id"),
            asunto=ticket_data.get("asunto", "Sin Asunto"),
            categoria=ticket_data.get("categoria", "General"),
            pregunta=ticket_data.get("pregunta"),
            nro_ticket=ticket_data.get("nro_ticket"),
            rubro_id=ticket_data.get("rubro_id"),
            direccion=ticket_data.get("direccion"),
            latitud=lat,
            longitud=lon
        )

class ServicioTickets:
    def __init__(self):
        self.creators: Dict[str, TicketCreator] = {
            "municipio": MunicipioTicketCreator(),
            "pyme": PymeTicketCreator()
        }

    def crear_nuevo_ticket(self, tipo_ticket: Literal["municipio", "pyme"], ticket_data: Dict[str, Any]) -> Union[PymeTicket, MunicipioTicket, None]:
        creator = self.creators.get(tipo_ticket)
        if not creator:
            raise ValueError(f"Tipo de ticket inválido: '{tipo_ticket}'.")

        ticket_data["nro_ticket"] = random.randint(100000, 999999)
        try:
            ticket = creator.create(ticket_data)
            db.session.add(ticket)
            db.session.flush()

            # Si viene un comentario opcional, lo agregamos
            if ticket_data.get("comentario"):
                comentario = TicketComentario(
                    comentario=ticket_data.get("comentario"),
                    user_id=ticket_data.get("user_id"),
                    es_admin=False
                )
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

    def crear_comentario(self, ticket_id: int, tipo_ticket: Literal["municipio", "pyme"], comentario_data: Dict[str, Any]) -> Union[TicketComentario, None]:
        TicketModel = MunicipioTicket if tipo_ticket == "municipio" else PymeTicket
        ticket = db.session.get(TicketModel, ticket_id)
        if not ticket:
            return None
        try:
            nuevo_comentario = TicketComentario(
                comentario=comentario_data.get("comentario"),
                user_id=comentario_data.get("user_id"),
                anon_id=comentario_data.get("anon_id"),
                es_admin=comentario_data.get("es_admin", False)
            )
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

    def migrar_tickets_de_anonimo(self, anon_id: str, nuevo_user_id: int) -> int:
        """Asigna a ``nuevo_user_id`` todos los tickets y comentarios
        vinculados al ``anon_id`` proporcionado."""
        if not anon_id or not nuevo_user_id:
            logger.warning("migrar_tickets_de_anonimo llamado sin parametros validos")
            return 0

        try:
            muni_count = (
                MunicipioTicket.query.filter_by(anon_id=anon_id)
                .update({"user_id": nuevo_user_id})
            )
            pyme_count = (
                PymeTicket.query.filter_by(anon_id=anon_id)
                .update({"user_id": nuevo_user_id})
            )
            comentario_count = (
                TicketComentario.query.filter_by(anon_id=anon_id)
                .update({"user_id": nuevo_user_id})
            )
            db.session.commit()
            total = (muni_count or 0) + (pyme_count or 0) + (comentario_count or 0)
            logger.info(
                "Tickets migrados de anon_id %s a user_id %s: %s",
                anon_id,
                nuevo_user_id,
                total,
            )
            return total
        except SQLAlchemyError as e:
            db.session.rollback()
            logger.error(
                "Error de DB al migrar tickets de anonimo %s: %s",
                anon_id,
                e,
                exc_info=True,
            )
            return 0

servicio_tickets = ServicioTickets()
