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
            foto_url_directa=ticket_data.get("foto_url_directa"), # Para la foto inicial del reclamo
            canal_ingreso=ticket_data.get("canal_ingreso"),
            estado=ticket_data.get("estado", "nuevo")
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
            dni=ticket_data.get("dni"),
            estado=ticket_data.get("estado", "nuevo")
        )

class ServicioTickets:
    def __init__(self):
        self.creators: Dict[str, TicketCreator] = {
            "municipio": MunicipioTicketCreator(),
            "pyme": PymeTicketCreator()
        }

    def crear_nuevo_ticket(self, tipo_ticket: Literal["municipio", "pyme"], ticket_data: Dict[str, Any]) -> Union[PymeTicket, MunicipioTicket, None, dict]:
        from models import User  # Import User model here to avoid circular import at module level
        creator = self.creators.get(tipo_ticket)
        if not creator:
            raise ValueError(f"Tipo de ticket inválido: '{tipo_ticket}'.")

        # --- Lógica de manejo de usuario mejorada ---
        email_llm = ticket_data.get("email_vecino")
        user_existente = None

        if email_llm:
            # Buscar usuario existente por email, ignorando mayúsculas/minúsculas.
            user_existente = User.query.filter(db.func.lower(User.email) == db.func.lower(email_llm)).first()

        if user_existente:
            logger.info(f"Usuario existente encontrado por email '{email_llm}'. ID: {user_existente.id}. Asociando ticket.")
            # Si encontramos un usuario por email, ESE es el usuario correcto para el ticket.
            ticket_data["user_id"] = user_existente.id
            ticket_data["anon_id"] = None # Nos aseguramos de que no quede como anónimo

            # Actualizar datos del usuario existente solo si los campos están vacíos
            nombre_llm = ticket_data.get("nombre_vecino")
            telefono_llm = ticket_data.get("telefono_vecino")
            if nombre_llm and not user_existente.name:
                user_existente.name = nombre_llm
            if telefono_llm and not user_existente.telefono:
                user_existente.telefono = telefono_llm

            # Usar los datos del perfil (potencialmente actualizados) para el ticket
            ticket_data['nombre_vecino'] = user_existente.name
            ticket_data['email_vecino'] = user_existente.email
            ticket_data['telefono_vecino'] = user_existente.telefono

        elif ticket_data.get("user_id"):
            # Si no se encontró por email pero se pasó un user_id (ej. usuario logueado anónimo), usamos ese.
            user_actual = db.session.get(User, ticket_data.get("user_id"))
            if user_actual:
                logger.info(f"Actualizando perfil para usuario ID: {user_actual.id} con datos del reclamo.")
                # Actualizar el perfil del usuario actual si se proporcionaron datos nuevos
                nombre_llm = ticket_data.get("nombre_vecino")
                email_llm = ticket_data.get("email_vecino")
                telefono_llm = ticket_data.get("telefono_vecino")

                if nombre_llm and nombre_llm != user_actual.name:
                    user_actual.name = nombre_llm
                # Solo intentar actualizar email si es diferente y no nulo
                if email_llm and email_llm != user_actual.email:
                    # Este es el punto que puede causar el error si el email ya existe en otro usuario.
                    # La lógica anterior ya debería haber capturado este caso, pero es una salvaguarda.
                    user_actual.email = email_llm
                if telefono_llm and telefono_llm != user_actual.telefono:
                    user_actual.telefono = telefono_llm
            else:
                 logger.warning(f"Se proveyó un user_id ({ticket_data.get('user_id')}) para crear un ticket, pero el usuario no fue encontrado.")

        # El commit se movió al final de la transacción en routes/chat.py
        # para evitar detached instances.
        # --- Fin de la lógica de manejo de usuario ---

        ticket_data["nro_ticket"] = random.randint(100000, 999999)
        try:
            # Eliminar el prefijo "Reclamo (LLM):" del asunto si existe
            if ticket_data.get("asunto", "").startswith("Reclamo (LLM):"):
                ticket_data["asunto"] = ticket_data["asunto"].replace("Reclamo (LLM):", "").strip()

            ticket = creator.create(ticket_data)
            db.session.add(ticket)
            db.session.flush() # flush para obtener el ID del ticket para el comentario

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

            # db.session.commit() # <<< ELIMINADO
            logger.info(f"Ticket #{ticket.nro_ticket} (ID: {ticket.id}) ({tipo_ticket}) creado localmente. Municipio ID: {getattr(ticket, 'municipio_id', 'N/A')}. Datos: {ticket.__dict__}")

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
            # This logic was moved to the action handlers to avoid circular imports
            # try:
            #     from routes.ticket import serialize_ticket_to_json # Importar la nueva función
            #
            #     # Serializar el ticket completo para la notificación
            #     ticket_json = serialize_ticket_to_json(ticket, tipo_ticket)
            #
            #     # El evento 'ticket_update' ahora enviará el objeto de ticket completo
            #     emit_ticket_update(ticket_json)
            #
            # except Exception as e_notify:
            #     logger.error(f"Error enviando notificación en tiempo real para ticket #{ticket.nro_ticket}: {e_notify}", exc_info=True)

            # Convertir el objeto ticket a un diccionario para un retorno consistente
            ticket_dict = {
                "id": ticket.id,
                "nro_ticket": ticket.nro_ticket,
                "asunto": ticket.asunto,
                "categoria": ticket.categoria,
                "estado": ticket.estado,
                "direccion": ticket.direccion,
                "user_id": ticket.user_id,
                "anon_id": ticket.anon_id
            }
            if tipo_ticket == "municipio":
                ticket_dict["detalles"] = ticket.detalles
                ticket_dict["nombre_vecino"] = getattr(ticket, 'nombre_vecino', None)
                ticket_dict["telefono_vecino"] = getattr(ticket, 'telefono_vecino', None)
                ticket_dict["email_vecino"] = getattr(ticket, 'email_vecino', None)
                ticket_dict["municipio_id"] = getattr(ticket, 'municipio_id', None)
            elif tipo_ticket == "pyme":
                ticket_dict["detalles"] = ticket.pregunta # PymeTicket uses 'pregunta'
                ticket_dict["rubro_id"] = getattr(ticket, 'rubro_id', None)

            return ticket_dict
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
                es_admin=comentario_data.get("es_admin", False),
                archivo_adjunto_id=comentario_data.get("archivo_adjunto_id"), # Add the new field
                origen=comentario_data.get("origen", "chat") # Guardar el origen
            )
            if tipo_ticket == "municipio":
                nuevo_comentario.municipio_ticket = ticket
            else:
                nuevo_comentario.pyme_ticket = ticket
            db.session.add(nuevo_comentario)
            # db.session.commit() # <<< ELIMINADO
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
            # db.session.commit() # <<< ELIMINADO
            return encuesta
        except SQLAlchemyError as e:
            db.session.rollback()
            logger.error(
                f"Error de DB al guardar encuesta: {e}", exc_info=True
            )
            return None

    def obtener_locations_de_tickets(
        self,
        *,
        municipio_id: int,
    ) -> list[dict]:
        """
        Devuelve una lista de coordenadas de todos los tickets para un municipio
        que tengan ubicación registrada. Formato: [{ "lat": lat, "lng": lng }]
        """
        try:
            tickets = (
                MunicipioTicket.query
                .filter(MunicipioTicket.municipio_id == municipio_id)
                .filter(MunicipioTicket.latitud.isnot(None), MunicipioTicket.longitud.isnot(None))
                .all()
            )
            return [{"lat": t.latitud, "lng": t.longitud} for t in tickets]
        except SQLAlchemyError as e:
            logger.error(
                f"Error de DB al obtener locations de tickets para municipio {municipio_id}: {e}", exc_info=True
            )
            return []


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
            logger.info(
                "[TICKET_SERVICE_MAPA] tipo=%s municipio_id=%s rubro_id=%s fecha_inicio=%s fecha_fin=%s categoria=%s estado=%s",
                tipo_ticket,
                municipio_id,
                rubro_id,
                fecha_inicio,
                fecha_fin,
                categoria,
                estado,
            )

            query = Model.query.filter(Model.latitud.isnot(None), Model.longitud.isnot(None))

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
            logger.info(
                "[TICKET_SERVICE_MAPA] tickets_raw=%s",
                len(tickets),
            )

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
            logger.info(
                "[TICKET_SERVICE_MAPA] puntos_heatmap=%s ejemplo=%s",
                len(resultado_heatmap),
                resultado_heatmap[:3] if resultado_heatmap else [],
            )
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
            # db.session.commit() # <<< ELIMINADO
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
