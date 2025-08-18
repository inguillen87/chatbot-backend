import logging
from typing import List, Optional
from extensions import db
from werkzeug.datastructures import FileStorage
# from models import ArchivoAdjunto, MunicipioTicket, PymeTicket, User # Movido para evitar importación circular
from datetime import datetime, timedelta # Para posible filtro de tiempo
from services.attachment_service import create_attachment_with_thumbnail

logger = logging.getLogger(__name__)


def guardar_archivo_adjunto_ticket(file: FileStorage, user_id: int, ticket_id: int, tipo_ticket: str) -> Optional['ArchivoAdjunto']:
    """
    Guarda un archivo, genera su thumbnail, y crea los registros en la BD asociados a un ticket.
    La sesión de la base de datos no se commitea aquí, se debe hacer en la función que llama.
    """
    from models import ArchivoAdjunto # Importación local para evitar ciclos

    try:
        # 1. Usar el nuevo servicio que maneja todo (subida, thumbnail, db records)
        nuevo_adjunto = create_attachment_with_thumbnail(file, user_id=user_id)

        if not nuevo_adjunto:
            logger.error(f"Fallo al procesar archivo con attachment_service para el ticket {tipo_ticket} {ticket_id}.")
            return None

        # 2. Asociar el adjunto al ticket correspondiente
        if tipo_ticket == 'municipio':
            nuevo_adjunto.municipio_ticket_id = ticket_id
        elif tipo_ticket == 'pyme':
            nuevo_adjunto.pyme_ticket_id = ticket_id
        else:
            logger.error(f"Tipo de ticket '{tipo_ticket}' no válido al guardar adjunto.")
            # La lógica de borrado de huérfanos estaría en el attachment_service si esto falla.
            return None

        # El objeto ya está en la sesión por create_attachment_with_thumbnail,
        # solo lo modificamos. La función que llama hará el commit.

        logger.info(f"ArchivoAdjunto ID {nuevo_adjunto.id} preparado para ticket {tipo_ticket} {ticket_id}.")
        return nuevo_adjunto

    except Exception as e:
        logger.error(f"Error en guardar_archivo_adjunto_ticket: {e}", exc_info=True)
        # La función que llama debe manejar el rollback.
        return None


class ArchivoService:

    def asociar_archivos_a_ticket(
        self,
        ticket_id: int,
        tipo_ticket: str, # "municipio" o "pyme"
        ids_archivos: List[int] = None,
        session_id: str = None,
        user_id: int = None # Para archivos subidos por un usuario autenticado pero sin session_id claro
    ) -> bool:
        """
        Asocia archivos adjuntos a un ticket recién creado.
        Los archivos pueden ser identificados por una lista de sus IDs,
        o por un session_id (para archivos subidos anónimamente o antes del login),
        o por user_id (para archivos subidos por un usuario pero aún no asociados).

        Se da prioridad a ids_archivos, luego a session_id (con user_id opcional),
        y finalmente solo a user_id (con un filtro de tiempo para relevancia).

        Retorna True si se asoció al menos un archivo, False en caso contrario o error.
        """
        from models import ArchivoAdjunto, MunicipioTicket, PymeTicket
        if not ticket_id or not tipo_ticket:
            logger.error("Ticket ID o tipo_ticket no proporcionados para asociar archivos.")
            return False

        if not ids_archivos and not session_id and not user_id:
            logger.warning(f"No se proporcionaron ids_archivos, session_id ni user_id para asociar al ticket {tipo_ticket} {ticket_id}.")
            return False

        archivos_a_asociar_query = ArchivoAdjunto.query.filter(
            ArchivoAdjunto.pyme_ticket_id.is_(None),
            ArchivoAdjunto.municipio_ticket_id.is_(None)
        )

        criterio_usado = "ninguno"

        if ids_archivos:
            criterio_usado = f"ids_archivos: {ids_archivos}"
            archivos_a_asociar_query = archivos_a_asociar_query.filter(ArchivoAdjunto.id.in_(ids_archivos))
        elif session_id:
            criterio_usado = f"session_id: {session_id}"
            archivos_a_asociar_query = archivos_a_asociar_query.filter_by(session_id=session_id)
            if user_id: # Refinar por user_id si está disponible con session_id
                criterio_usado += f", user_id: {user_id}"
                archivos_a_asociar_query = archivos_a_asociar_query.filter_by(user_id=user_id)
        elif user_id: # Solo user_id, como última opción
            criterio_usado = f"user_id: {user_id} (con filtro de tiempo)"
            archivos_a_asociar_query = archivos_a_asociar_query.filter(
                ArchivoAdjunto.user_id == user_id,
                # Considerar solo archivos recientes para evitar asociar archivos muy antiguos
                # si un usuario tiene muchos archivos no asociados por alguna razón.
                ArchivoAdjunto.fecha >= datetime.utcnow() - timedelta(hours=1)
            )

        logger.info(f"Buscando archivos por criterio: [{criterio_usado}] para ticket {tipo_ticket} {ticket_id}")
        archivos_a_asociar = archivos_a_asociar_query.all()

        if not archivos_a_asociar:
            logger.info(f"No se encontraron archivos pendientes para asociar al ticket {tipo_ticket} {ticket_id} con criterio [{criterio_usado}].")
            return False

        TicketModel = None
        ticket_attr_name = None
        if tipo_ticket == "municipio":
            TicketModel = MunicipioTicket
            ticket_attr_name = "municipio_ticket_id"
        elif tipo_ticket == "pyme":
            TicketModel = PymeTicket
            ticket_attr_name = "pyme_ticket_id"
        else:
            logger.error(f"Tipo de ticket desconocido: {tipo_ticket}")
            return False

        session = db.session
        try:
            # Es importante obtener el ticket dentro de la misma sesión si se van a modificar sus relaciones
            # o si se va a verificar su existencia.
            # ticket = session.get(TicketModel, ticket_id) # Usar session.get si está disponible y es preferido
            ticket = TicketModel.query.with_session(session).get(ticket_id)

            if not ticket:
                logger.error(f"No se encontró el ticket {tipo_ticket} con ID {ticket_id} para asociar archivos.")
                return False

            archivos_asociados_count = 0
            for archivo in archivos_a_asociar:
                setattr(archivo, ticket_attr_name, ticket.id)
                if archivo not in session: # Asegurarse que el objeto está en la sesión si fue cargado por una query diferente
                    session.add(archivo)
                logger.info(f"Asociando Archivo ID {archivo.id} ({archivo.nombre_original}) a Ticket {tipo_ticket} ID {ticket.id}")
                archivos_asociados_count += 1

            session.commit()
            logger.info(f"{archivos_asociados_count} archivos asociados exitosamente al ticket {tipo_ticket} {ticket.id}.")
            return archivos_asociados_count > 0

        except Exception as e:
            session.rollback()
            logger.error(f"Error al asociar archivos al ticket {tipo_ticket} {ticket_id}: {e}", exc_info=True)
            return False

# Instancia del servicio para ser importada y usada
archivo_service = ArchivoService()

# Ejemplo de uso que iría en services/logic.py (o donde se cree el ticket):
# from .ticket_service import servicio_tickets
# from .archivo_service import archivo_service
#
# def alguna_funcion_que_crea_ticket(datos_usuario, datos_reclamo, session_id_actual, ids_archivos_seleccionados=None):
#     # ... lógica para preparar ticket_data ...
#     nuevo_ticket = servicio_tickets.crear_nuevo_ticket("municipio", ticket_data)
#     if nuevo_ticket:
#         # Intentar asociar archivos
#         # Priorizar lista explícita de IDs, luego session_id
#         if ids_archivos_seleccionados:
#             archivo_service.asociar_archivos_a_ticket(
#                 ticket_id=nuevo_ticket.id,
#                 tipo_ticket="municipio",
#                 ids_archivos=ids_archivos_seleccionados
#             )
#         elif session_id_actual: # Para archivos subidos antes de crear el ticket en la misma sesión
#             archivo_service.asociar_archivos_a_ticket(
#                 ticket_id=nuevo_ticket.id,
#                 tipo_ticket="municipio",
#                 session_id=session_id_actual,
#                 user_id=getattr(nuevo_ticket, 'user_id', None) # Pasar user_id si el ticket ya lo tiene
#             )
#     return nuevo_ticket
