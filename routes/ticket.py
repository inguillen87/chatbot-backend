# Reemplaza todo tu archivo de tickets con este código.

from models import MunicipioTicket, PymeTicket, TicketComentario, db
from datetime import datetime
import random
import logging
from typing import Dict, Any, Literal
from sqlalchemy.exc import SQLAlchemyError

# Logger para este módulo
logger = logging.getLogger(__name__)

# --- Clases de Estrategia (La lógica "pro" vive aquí, de forma interna) ---
# Estas clases no necesitan ser llamadas desde fuera, son ayudantes de nuestra función principal.

class TicketCreator:
    """Clase base para los creadores de tickets."""
    def create(self, ticket_data: Dict[str, Any]) -> PymeTicket | MunicipioTicket:
        raise NotImplementedError

class MunicipioTicketCreator(TicketCreator):
    """Crea tickets de tipo Municipio."""
    def create(self, ticket_data: Dict[str, Any]) -> MunicipioTicket:
        return MunicipioTicket(
            pregunta=ticket_data.get("pregunta"),
            user_id=ticket_data.get("user_id"),
            estado="nuevo",
            nro_ticket=ticket_data.get("nro_ticket"),
            fecha=ticket_data.get("fecha"),
            archivo_url=ticket_data.get("archivo_url")
        )

class PymeTicketCreator(TicketCreator):
    """Crea tickets de tipo Pyme."""
    def create(self, ticket_data: Dict[str, Any]) -> PymeTicket:
        return PymeTicket(
            pregunta=ticket_data.get("pregunta"),
            user_id=ticket_data.get("user_id"),
            estado="nuevo",
            nro_ticket=ticket_data.get("nro_ticket"),
            fecha=ticket_data.get("fecha"),
            rubro_id=ticket_data.get("rubro_id"),
            archivo_url=ticket_data.get("archivo_url"),
            telefono=ticket_data.get("telefono"),
            email=ticket_data.get("email"),
            dni=ticket_data.get("dni"),
            estado_cliente=ticket_data.get("estado_cliente", "no_definido")
        )

# --- TU FUNCIÓN ORIGINAL, AHORA CON EL MOTOR MEJORADO ---
def crear_ticket_universal(tipo: Literal["municipio", "pyme"], pregunta: str, user_id=None, rubro_id=None, 
                           telefono=None, email=None, dni=None, estado_cliente="no_definido", 
                           archivo_url=None, comentario=None) -> int:
    """
    Crea un ticket universal (municipio o pyme) y su comentario inicial
    en una única transacción atómica y segura.
    Mantiene la firma original para no romper el resto del código.
    """
    creators = {
        "municipio": MunicipioTicketCreator(),
        "pyme": PymeTicketCreator()
    }

    creator = creators.get(tipo)
    if not creator:
        logger.error(f"Intento de crear ticket con tipo inválido: {tipo}")
        raise ValueError("Tipo de ticket inválido. Debe ser 'municipio' o 'pyme'.")

    # Empaquetamos todos los parámetros en un diccionario limpio
    ticket_data = {
        "pregunta": pregunta,
        "user_id": user_id,
        "rubro_id": rubro_id,
        "telefono": telefono,
        "email": email,
        "dni": dni,
        "estado_cliente": estado_cliente,
        "archivo_url": archivo_url,
        "comentario": comentario,
        # Datos que generamos internamente
        "nro_ticket": random.randint(10000, 99999),
        "fecha": datetime.now()
    }

    try:
        # --- INICIO DE TRANSACCIÓN ATÓMICA ---
        # 1. Crear el objeto ticket usando la estrategia correcta
        ticket = creator.create(ticket_data)
        db.session.add(ticket)
        # Obtenemos el ID del ticket para usarlo en el comentario sin hacer commit
        db.session.flush()

        # 2. Si hay un comentario, lo creamos y lo asociamos
        if comentario:
            ticket_comment = TicketComentario(
                comentario=comentario,
                fecha=ticket_data["fecha"],
                user_id=user_id
            )
            # Asociamos el comentario al ticket correcto para evitar problemas de polimorfismo
            if tipo == "municipio":
                ticket_comment.municipio_ticket = ticket
            else: # tipo == "pyme"
                ticket_comment.pyme_ticket = ticket
            
            db.session.add(ticket_comment)

        # 3. Hacemos commit una sola vez al final
        db.session.commit()
        
        logger.info(f"Ticket #{ticket.nro_ticket} (tipo: {tipo}) y comentario inicial creados.")
        return ticket.nro_ticket

    except SQLAlchemyError as e:
        # Si cualquier paso falla, revertimos toda la transacción
        db.session.rollback()
        logger.error(f"Error de base de datos en crear_ticket_universal (tipo: {tipo}): {e}", exc_info=True)
        # Relanzamos el error para que la capa superior sepa que algo salió mal
        raise e

# --- TU OTRA FUNCIÓN, TAMBIÉN MEJORADA ---
def crear_comentario_ticket(ticket_id: int, tipo_ticket: Literal["municipio", "pyme"], 
                            comentario: str, user_id=None, telefono=None, email=None, 
                            dni=None, estado_cliente=None) -> TicketComentario:
    """
    Crea un nuevo comentario para un ticket existente de forma segura.
    Mantiene la firma original.
    """
    # Primero, verificamos que el ticket al que se quiere comentar realmente existe.
    if tipo_ticket == "municipio":
        ticket = db.session.get(MunicipioTicket, ticket_id)
    elif tipo_ticket == "pyme":
        ticket = db.session.get(PymeTicket, ticket_id)
    else:
        raise ValueError("Tipo de ticket inválido.")
        
    if not ticket:
        raise ValueError(f"No se encontró un ticket de tipo '{tipo_ticket}' con ID {ticket_id}.")

    try:
        nuevo_comentario = TicketComentario(
            comentario=comentario,
            user_id=user_id,
            telefono=telefono,
            email=email,
            dni=dni,
            estado_cliente=estado_cliente or "no_definido",
            fecha=datetime.now()
        )
        
        # Asociamos el comentario al ticket correcto
        if tipo_ticket == "municipio":
            nuevo_comentario.municipio_ticket = ticket
        else: # tipo == "pyme"
            nuevo_comentario.pyme_ticket = ticket

        db.session.add(nuevo_comentario)
        db.session.commit()

        logger.info(f"Comentario añadido al ticket #{ticket.nro_ticket} (tipo: {tipo_ticket}).")
        return nuevo_comentario

    except SQLAlchemyError as e:
        db.session.rollback()
        logger.error(f"Error de DB al crear comentario para ticket ID {ticket_id} (tipo: {tipo_ticket}): {e}", exc_info=True)
        raise e