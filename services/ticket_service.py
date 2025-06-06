# services/ticket_service.py

import random
from datetime import datetime
from typing import Dict, Any, Literal
import logging

from models import MunicipioTicket, PymeTicket, TicketComentario, db
from sqlalchemy.exc import SQLAlchemyError

# Logger para este servicio
logger = logging.getLogger(__name__)

# --- Interfaces y Estrategias de Creación (Patrón Strategy) ---

class TicketCreator:
    """Clase base abstracta para los creadores de tickets."""
    def create(self, ticket_data: Dict[str, Any]) -> PymeTicket | MunicipioTicket:
        raise NotImplementedError("El método 'create' debe ser implementado por las subclases.")

class MunicipioTicketCreator(TicketCreator):
    """Estrategia concreta para crear tickets de tipo Municipio."""
    def create(self, ticket_data: Dict[str, Any]) -> MunicipioTicket:
        return MunicipioTicket(
            pregunta=ticket_data.get("pregunta"),
            user_id=ticket_data.get("user_id"),
            nro_ticket=ticket_data.get("nro_ticket"),
            fecha=ticket_data.get("fecha"),
            estado="nuevo",
            archivo_url=ticket_data.get("archivo_url")
        )

class PymeTicketCreator(TicketCreator):
    """Estrategia concreta para crear tickets de tipo Pyme."""
    def create(self, ticket_data: Dict[str, Any]) -> PymeTicket:
        return PymeTicket(
            pregunta=ticket_data.get("pregunta"),
            user_id=ticket_data.get("user_id"),
            nro_ticket=ticket_data.get("nro_ticket"),
            fecha=ticket_data.get("fecha"),
            estado="nuevo",
            rubro_id=ticket_data.get("rubro_id"),
            archivo_url=ticket_data.get("archivo_url"),
            telefono=ticket_data.get("telefono"),
            email=ticket_data.get("email"),
            dni=ticket_data.get("dni"),
            estado_cliente=ticket_data.get("estado_cliente", "no_definido")
        )

# --- Clase de Servicio Principal ---

class ServicioTickets:
    """
    Clase de servicio que encapsula toda la lógica de negocio para Tickets.
    Esta es la única clase que deberías usar desde tus rutas (endpoints).
    """
    def __init__(self):
        # Mapea un string de tipo a su estrategia de creación correspondiente
        self.creators: Dict[str, TicketCreator] = {
            "municipio": MunicipioTicketCreator(),
            "pyme": PymeTicketCreator()
        }

    def crear_nuevo_ticket(self, tipo_ticket: Literal["municipio", "pyme"], ticket_data: Dict[str, Any]) -> int:
        """
        Crea un nuevo ticket y su primer comentario en una única transacción atómica.

        Args:
            tipo_ticket: El tipo de ticket a crear ('municipio' o 'pyme').
            ticket_data: Un diccionario con todos los datos necesarios.
                         Campos requeridos: 'pregunta'.
                         Campos opcionales: 'user_id', 'rubro_id', 'comentario', etc.
        
        Returns:
            El número del ticket creado.
            
        Raises:
            ValueError: Si el tipo_ticket es inválido.
            SQLAlchemyError: Si ocurre un error de base de datos.
        """
        creator = self.creators.get(tipo_ticket)
        if not creator:
            raise ValueError(f"Tipo de ticket inválido: '{tipo_ticket}'. Debe ser 'municipio' o 'pyme'.")

        # Preparamos datos comunes
        ticket_data["nro_ticket"] = random.randint(10000, 99999)
        ticket_data["fecha"] = datetime.now()
        
        try:
            # --- INICIO DE TRANSACCIÓN ATÓMICA ---
            
            # 1. Crear el objeto ticket usando la estrategia correcta
            ticket = creator.create(ticket_data)
            db.session.add(ticket)
            # Usamos flush para obtener el ID del ticket sin hacer commit todavía
            db.session.flush()

            # 2. Si hay un comentario inicial, lo creamos y lo asociamos
            #    Esto resuelve la advertencia de polimorfismo que tenías.
            if ticket_data.get("comentario"):
                comentario = TicketComentario(
                    comentario=ticket_data.get("comentario"),
                    fecha=ticket_data.get("fecha"),
                    user_id=ticket_data.get("user_id")
                )
                # La forma correcta de asociar un comentario a un ticket polimórfico
                if tipo_ticket == "municipio":
                    comentario.municipio_ticket = ticket
                elif tipo_ticket == "pyme":
                    comentario.pyme_ticket = ticket
                
                db.session.add(comentario)

            # 3. Solo al final, si todo salió bien, hacemos commit.
            db.session.commit()
            
            logger.info(f"Ticket #{ticket.nro_ticket} (tipo: {tipo_ticket}) creado exitosamente.")
            return ticket.nro_ticket
            
            # --- FIN DE TRANSACCIÓN ATÓMICA ---

        except SQLAlchemyError as e:
            # Si algo falla, revertimos TODOS los cambios de esta transacción
            db.session.rollback()
            logger.error(f"Error de base de datos al crear ticket tipo '{tipo_ticket}': {e}", exc_info=True)
            # Re-lanzamos la excepción para que la capa superior la maneje si es necesario
            raise

    def crear_comentario(self, ticket_id: int, tipo_ticket: Literal["municipio", "pyme"], comentario_data: Dict[str, Any]) -> TicketComentario:
        """
        Crea un nuevo comentario para un ticket existente.
        """
        # Aquí buscarías el ticket para asegurar que existe antes de comentar
        # ticket = ...
        
        try:
            nuevo_comentario = TicketComentario(
                comentario=comentario_data.get("comentario"),
                user_id=comentario_data.get("user_id"),
                fecha=datetime.now()
                # ... otros campos si los necesitas ...
            )
            
            # Asociar correctamente según el tipo
            if tipo_ticket == "municipio":
                nuevo_comentario.ticket_id_municipio = ticket_id # Asumo nombres de FK
            elif tipo_ticket == "pyme":
                nuevo_comentario.ticket_id_pyme = ticket_id

            db.session.add(nuevo_comentario)
            db.session.commit()
            
            logger.info(f"Comentario añadido al ticket_id {ticket_id} (tipo: {tipo_ticket}).")
            return nuevo_comentario
            
        except SQLAlchemyError as e:
            db.session.rollback()
            logger.error(f"Error de base de datos al crear comentario para ticket_id {ticket_id}: {e}", exc_info=True)
            raise

# --- Instancia única del servicio para usar en toda la aplicación ---
servicio_tickets = ServicioTickets()