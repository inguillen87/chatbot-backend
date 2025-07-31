# services/ticket_service.py
import random
from datetime import datetime, timedelta
from typing import Dict, Any, Literal, Union
import logging

from models import (
    MunicipioTicket,
    PymeTicket,
    TicketComentario,
    TicketSatisfaccion,
    db,
)
from sqlalchemy.exc import SQLAlchemyError
from .integracion_municipal import enviar_ticket_a_sigem # SIGEM Integration

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
            municipio_id=ticket_data.get("municipio_id"),
            anon_id=ticket_data.get("anon_id"),
            asunto=ticket_data.get("asunto", "Sin Asunto"),
            categoria=ticket_data.get("categoria", "General"),
            pregunta=ticket_data.get("pregunta", ""),    # Reclamo original
            detalles=ticket_data.get("detalles", ""),    # Dirección, nombre, tel, etc.
            nro_ticket=ticket_data.get("nro_ticket"),
            direccion=ticket_data.get("direccion"),
            latitud=lat,
            longitud=lon,
            # Campos adicionales para información del vecino/contacto
            nombre_vecino=ticket_data.get("nombre_vecino"),
            telefono_vecino=ticket_data.get("telefono_vecino"),
            email_vecino=ticket_data.get("email_vecino"),
            foto_url_directa=ticket_data.get("foto_url_directa") # Para la foto inicial del reclamo
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
            longitud=lon,
            telefono=ticket_data.get("telefono_vecino") or ticket_data.get("telefono"),
            email=ticket_data.get("email_vecino") or ticket_data.get("email"),
            dni=ticket_data.get("dni")
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
            logger.info(f"Ticket #{ticket.nro_ticket} (ID: {ticket.id}) ({tipo_ticket}) creado localmente. Datos: {ticket.__dict__}")

            # Integración con SIGEM para tickets municipales
            if tipo_ticket == "municipio" and isinstance(ticket, MunicipioTicket):
                try:
                    sigem_success = enviar_ticket_a_sigem(ticket)
                    if sigem_success:
                        logger.info(f"Ticket #{ticket.nro_ticket} enviado a SIGEM exitosamente.")
                    else:
                        logger.warning(f"Ticket #{ticket.nro_ticket} NO pudo ser enviado a SIGEM (función devolvió False).")
                except Exception as e_sigem:
                    # Loggear el error pero no revertir la creación local del ticket.
                    # La integración externa no debe impedir el funcionamiento primario.
                    logger.error(f"Error durante el envío del Ticket #{ticket.nro_ticket} a SIGEM: {e_sigem}", exc_info=True)

            # Notificar panel en tiempo real
            try:
                from socket_service import emit_ticket_update
                data = {
                    "message": f"Nuevo ticket creado: #{ticket.nro_ticket}",
                    "ticket_id": ticket.id,
                    "tipo": tipo_ticket,
                    "nuevo_estado": ticket.estado,
                    "asunto": getattr(ticket, "asunto", ""),
                    "categoria": getattr(ticket, "categoria", None),
                    "fecha": ticket.fecha.isoformat() if ticket.fecha else None,
                    "nro_ticket": ticket.nro_ticket
                }
                emit_ticket_update(data)
            except Exception as e_notify:
                logger.error(f"Error enviando notificación en tiempo real para ticket #{ticket.nro_ticket}: {e_notify}", exc_info=True)

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
            try:
                from services.email_service import (
                    enviar_email_ticket_novedad,
                    enviar_sms_ticket_novedad,
                    enviar_whatsapp_ticket_novedad, # <--- IMPORTAR NUEVA FUNCIÓN
                    enviar_email_ticket_admin,
                )
                mensaje_notificacion = f"Nuevo comentario en tu ticket #{ticket.nro_ticket}: {comentario_data.get('comentario', '')[:50]}..."
                if nuevo_comentario.es_admin: # Notificar al usuario/cliente
                    enviar_email_ticket_novedad(ticket, mensaje_notificacion)
                    enviar_sms_ticket_novedad(ticket, mensaje_notificacion)
                    if tipo_ticket == "municipio": # Por ahora, WhatsApp solo para municipio
                        enviar_whatsapp_ticket_novedad(ticket, mensaje_notificacion)
                else: # Notificar al admin/empleado
                    enviar_email_ticket_admin(ticket) # Email al admin es suficiente por ahora
            except Exception as e:  # pragma: no cover - not essential for tests
                logger.error(f"Error enviando notificaciones tras crear comentario para ticket {ticket.id if ticket else 'N/A'}: {e}", exc_info=True)
            return nuevo_comentario
        except SQLAlchemyError as e:
            db.session.rollback()
            logger.error(f"Error de DB al crear comentario: {e}", exc_info=True)
            return None

    def guardar_encuesta(
        self,
        ticket_id: int,
        tipo_ticket: Literal["municipio", "pyme"],
        puntuacion: int,
        comentario: str | None = None,
    ) -> Union[TicketSatisfaccion, None]:
        try:
            encuesta = TicketSatisfaccion(
                ticket_id=ticket_id,
                tipo=tipo_ticket,
                puntuacion=puntuacion,
                comentario=comentario,
            )
            db.session.add(encuesta)
            db.session.commit()
            return encuesta
        except SQLAlchemyError as e:
            db.session.rollback()
            logger.error(
                f"Error de DB al guardar encuesta: {e}", exc_info=True
            )
            return None

    def obtener_tickets_con_ubicacion_para_mapa( # Nombre modificado
        self,
        tipo_ticket: Literal["municipio", "pyme"],
        *,
        municipio_id: int | None = None,
        rubro_id: int | None = None,
        fecha_inicio: str | None = None,
        fecha_fin: str | None = None,
        categoria: str | None = None,
        estado: str | None = None, # Nuevo parámetro de estado
    ) -> list[dict]:
        """
        Devuelve los tickets con ubicación, opcionalmente filtrados por estado,
        agrupados por ubicación y con un peso (cantidad de tickets).
        Permite filtrar por municipio/rubro, rango de fechas y categoría.
        """
        Model = MunicipioTicket if tipo_ticket == "municipio" else PymeTicket
        try:
            query = Model.query().filter(Model.latitud.isnot(None), Model.longitud.isnot(None))

            # Filtrar por estado si se proporciona
            if estado:
                query = query.filter(Model.estado == estado)
            # else: # Comportamiento por defecto si no se especifica estado (ej: no cerrados)
            #     query = query.filter(Model.estado != "cerrado") # Opcional: mantener un filtro por defecto

            if tipo_ticket == "municipio" and municipio_id is not None:
                query = query.filter_by(municipio_id=municipio_id)
            if tipo_ticket == "pyme" and rubro_id is not None:
                query = query.filter_by(rubro_id=rubro_id)

            if fecha_inicio:
                try:
                    query = query.filter(Model.fecha >= datetime.fromisoformat(fecha_inicio))
                except ValueError:
                    logger.warning(f"Formato de fecha_inicio inválido: {fecha_inicio}")
            if fecha_fin:
                try:
                    # Añadimos un día para incluir todo el día de fecha_fin
                    fecha_fin_dt = datetime.fromisoformat(fecha_fin) + timedelta(days=1) # Ajustar para incluir el día completo
                    query = query.filter(Model.fecha < fecha_fin_dt)
                except ValueError:
                    logger.warning(f"Formato de fecha_fin inválido: {fecha_fin}")

            if categoria and hasattr(Model, 'categoria'):
                query = query.filter(Model.categoria == categoria)

            tickets = query.all()

            # El agrupamiento por ubicación y el cálculo de 'weight' permanecen igual.
            # Si se desea devolver todos los puntos individualmente para que el frontend agrupe/clusterice:
            # return [
            #     {
            #         "id": t.id, "lat": t.latitud, "lng": t.longitud, "estado": t.estado,
            #         "asunto": t.asunto, "nro_ticket": t.nro_ticket, "categoria": t.categoria
            #     } for t in tickets
            # ]
            # Por ahora, mantendremos la agrupación existente que devuelve 'weight'.

            ubicaciones_agrupadas = {} # (lat, lng) -> count

            for t in tickets:
                # Redondear lat/lng a un número de decimales para agrupar puntos cercanos.
                # Ajustar el número de decimales según la precisión deseada.
                # 5 decimales dan una precisión de ~1.1 metros.
                # 4 decimales dan una precisión de ~11 metros.
                # 3 decimales dan una precisión de ~110 metros.
                # Consideremos 4 decimales para agrupar problemáticas en una misma "zona pequeña".
                lat_lng_key = (round(t.latitud, 4), round(t.longitud, 4))
                if lat_lng_key not in ubicaciones_agrupadas:
                    ubicaciones_agrupadas[lat_lng_key] = 0
                ubicaciones_agrupadas[lat_lng_key] += 1

            resultado_heatmap = []
            for (lat, lng), weight in ubicaciones_agrupadas.items():
                resultado_heatmap.append({
                    "location": {"lat": lat, "lng": lng},
                    "weight": weight
                })

            return resultado_heatmap
        except SQLAlchemyError as e:
            logger.error(
                f"Error de DB al obtener tickets para mapa de calor: {e}", exc_info=True
            )
            return []

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
