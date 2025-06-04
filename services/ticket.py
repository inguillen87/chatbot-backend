from models import MunicipioTicket, PymeTicket, TicketComentario, db
from datetime import datetime
import random

def crear_ticket_universal(tipo, pregunta, user_id=None, rubro_id=None, 
                           telefono=None, email=None, dni=None, estado_cliente="no_definido", 
                           archivo_url=None, comentario=None):
    """
    Crea un ticket universal, para municipio o pyme.
    """
    nro_ticket = random.randint(10000, 99999)
    fecha = datetime.now()

    if tipo == "municipio":
        ticket = MunicipioTicket(
            pregunta=pregunta,
            user_id=user_id,
            estado="nuevo",
            nro_ticket=nro_ticket,
            fecha=fecha,
            archivo_url=archivo_url
        )
        db.session.add(ticket)
        db.session.commit()  # Necesario para tener ticket.id disponible

    elif tipo == "pyme":
        ticket = PymeTicket(
            pregunta=pregunta,
            user_id=user_id,
            estado="nuevo",
            nro_ticket=nro_ticket,
            fecha=fecha,
            rubro_id=rubro_id,
            archivo_url=archivo_url,
            telefono=telefono,
            email=email,
            dni=dni,
            estado_cliente=estado_cliente
        )
        db.session.add(ticket)
        db.session.commit()
    else:
        raise ValueError("Tipo de ticket inválido. Debe ser 'municipio' o 'pyme'.")

    # Si hay comentario o datos de cliente, lo guardamos como comentario relacionado al ticket
    if comentario or telefono or email or dni or (estado_cliente and estado_cliente != "no_definido"):
        ticket_comment = TicketComentario(
            ticket_id=ticket.id,
            tipo_ticket=tipo,  # nombre del campo según tu modelo
            comentario=comentario or "",
            fecha=fecha,
            user_id=user_id,
            telefono=telefono,
            email=email,
            dni=dni,
            estado_cliente=estado_cliente or "no_definido"
        )
        db.session.add(ticket_comment)
        db.session.commit()

    return ticket.nro_ticket

def crear_comentario_ticket(ticket_id, tipo_ticket, comentario, user_id=None, telefono=None, email=None, dni=None, estado_cliente=None):
    nuevo_comentario = TicketComentario(
        ticket_id=ticket_id,
        tipo_ticket=tipo_ticket,  # nombre del campo según tu modelo
        comentario=comentario,
        user_id=user_id,
        telefono=telefono,
        email=email,
        dni=dni,
        estado_cliente=estado_cliente or "no_definido"
    )
    db.session.add(nuevo_comentario)
    db.session.commit()
    return nuevo_comentario
