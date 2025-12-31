# services/ticket_service.py
import os
import random
from datetime import datetime, timedelta
from typing import Dict, Any, Literal, Union, Iterable, Optional
import logging

from models import (
    MunicipioTicket,
    PymeTicket,
    TicketComentario,
    TicketSatisfaccion,
    Conversacion,
    User,
    db,
)
from utils.ticket_utils import normalize_category
from utils.time_utils import datetime_to_iso_utc, get_local_now
from sqlalchemy import func
from sqlalchemy.exc import SQLAlchemyError
from .integracion_municipal import enviar_ticket_a_sigem # SIGEM Integration
from utils.heatmap import enrich_heatmap_points

logger = logging.getLogger(__name__)

_CLOSED_STATES = {"cerrado"}


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
            consulta_pin=ticket_data.get("consulta_pin"),
            direccion=ticket_data.get("direccion"),
            latitud=lat,
            longitud=lon,
            # Campos adicionales para información del vecino/contacto
            nombre_vecino=ticket_data.get("nombre_vecino"),
            telefono_vecino=ticket_data.get("telefono_vecino"),
            email_vecino=ticket_data.get("email_vecino"),
            dni_vecino=ticket_data.get("dni") or ticket_data.get("dni_vecino"),
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
        telefono_contacto = (
            ticket_data.get("telefono_cliente")
            or ticket_data.get("telefono_vecino")
            or ticket_data.get("telefono")
        )
        email_contacto = (
            ticket_data.get("email_cliente")
            or ticket_data.get("email_vecino")
            or ticket_data.get("email")
        )
        return PymeTicket(
            user_id=ticket_data.get("user_id"),
            tenant_id=ticket_data.get("tenant_id"),
            anon_id=ticket_data.get("anon_id"),
            asunto=ticket_data.get("asunto", "Sin Asunto"),
            categoria=ticket_data.get("categoria", "General"),
            pregunta=ticket_data.get("pregunta"),
            nro_ticket=ticket_data.get("nro_ticket"),
            rubro_id=ticket_data.get("rubro_id"),
            direccion=ticket_data.get("direccion"),
            latitud=lat,
            longitud=lon,
            telefono=telefono_contacto,
            email=email_contacto,
            dni=ticket_data.get("dni"),
            estado=ticket_data.get("estado", "nuevo"),
            estado_cliente=ticket_data.get("estado", "nuevo")
        )

class ServicioTickets:
    def __init__(self):
        self.creators: Dict[str, TicketCreator] = {
            "municipio": MunicipioTicketCreator(),
            "pyme": PymeTicketCreator()
        }
        self.auto_assign_enabled = (
            str(os.getenv("AUTO_ASSIGN_TICKETS", "false")).strip().lower()
            in {"1", "true", "yes"}
        )

    def _empleados_para_ticket_municipal(self, ticket: MunicipioTicket) -> list[User]:
        if not ticket.municipio_id:
            return []

        query = User.query.filter(
            User.empresa_id == ticket.municipio_id,
            User.rol == "empleado",
            User.tipo_chat == "municipio",
        )

        categoria_normalizada = (ticket.categoria or "").strip().lower()
        if categoria_normalizada:
            query = query.join(User.categorias).filter(func.lower(Categoria.nombre) == categoria_normalizada)

        return query.order_by(User.id.asc()).all()

    def _calcular_carga_empleado_municipal(self, empleado: User, municipio_id: int) -> int:
        return (
            MunicipioTicket.query.filter(
                MunicipioTicket.municipio_id == municipio_id,
                MunicipioTicket.asignado_a_id == empleado.id,
                ~MunicipioTicket.estado.in_(list(_CLOSED_STATES)),
            )
            .with_entities(func.count(MunicipioTicket.id))
            .scalar()
            or 0
        )

    def asignar_ticket_municipal(
        self,
        ticket: MunicipioTicket,
        empleado_id: Optional[int] = None,
        *,
        auto: bool = False,
        actor_id: Optional[int] = None,
    ) -> Optional[User]:
        """Asigna el ticket a un empleado compatible con la categoría y municipio."""

        if not ticket:
            return None

        if empleado_id:
            empleado = User.query.filter(
                User.id == empleado_id,
                User.empresa_id == ticket.municipio_id,
                User.rol.in_(["empleado", "admin"]),
            ).first()
        else:
            candidatos = self._empleados_para_ticket_municipal(ticket)
            if not candidatos or (not auto and not self.auto_assign_enabled):
                return None
            empleado = min(
                candidatos,
                key=lambda emp: self._calcular_carga_empleado_municipal(emp, ticket.municipio_id),
            )

        if not empleado:
            return None

        if ticket.asignado_a_id == empleado.id:
            return empleado

        ticket.asignado_a = empleado
        ticket.asignado_en = get_local_now()
        if hasattr(ticket, "ultima_actividad"):
            ticket.ultima_actividad = get_local_now()

        comentario = TicketComentario(
            municipio_ticket_id=ticket.id,
            comentario=f"Ticket asignado a {empleado.name}",
            user_id=actor_id,
            es_admin=True,
            origen="sistema",
        )
        db.session.add(comentario)

        return empleado

    def _resolve_pyme_owner_id(self, ticket: PymeTicket, actor_id: Optional[int]) -> Optional[int]:
        if actor_id:
            actor = db.session.get(User, actor_id)
            if actor and actor.tipo_chat == "pyme":
                return actor.id if actor.rol == "admin" else actor.empresa_id

        admin_for_rubro = (
            User.query.filter(
                User.rubro_id == ticket.rubro_id,
                User.tipo_chat == "pyme",
                User.rol == "admin",
            )
            .order_by(User.id.asc())
            .first()
        )
        return admin_for_rubro.id if admin_for_rubro else None

    def _empleados_para_ticket_pyme(self, ticket: PymeTicket, actor_id: Optional[int]) -> list[User]:
        owner_id = self._resolve_pyme_owner_id(ticket, actor_id)
        if not owner_id:
            return []

        candidatos = (
            User.query.filter(
                User.empresa_id == owner_id,
                User.rol.in_(["empleado", "admin"]),
                User.tipo_chat == "pyme",
            )
            .order_by(User.id.asc())
            .all()
        )

        categoria_normalizada = (ticket.categoria or "").strip().lower()
        if not categoria_normalizada:
            return candidatos

        filtrados = []
        for empleado in candidatos:
            categorias_emp = [
                c.strip().lower()
                for c in (empleado.ticket_categorias or "").split(",")
                if c.strip()
            ]
            if not categorias_emp or categoria_normalizada in categorias_emp:
                filtrados.append(empleado)

        return filtrados

    def _calcular_carga_empleado_pyme(self, empleado: User, rubro_id: int) -> int:
        return (
            PymeTicket.query.filter(
                PymeTicket.rubro_id == rubro_id,
                PymeTicket.asignado_a_id == empleado.id,
                ~PymeTicket.estado.in_(list(_CLOSED_STATES)),
            )
            .with_entities(func.count(PymeTicket.id))
            .scalar()
            or 0
        )

    def asignar_ticket_pyme(
        self,
        ticket: PymeTicket,
        empleado_id: Optional[int] = None,
        *,
        auto: bool = False,
        actor_id: Optional[int] = None,
    ) -> Optional[User]:
        """Asigna el ticket de pyme a un empleado compatible."""

        if not ticket:
            return None

        if empleado_id:
            owner_id = self._resolve_pyme_owner_id(ticket, actor_id)
            empleado = User.query.filter(
                User.id == empleado_id,
                User.tipo_chat == "pyme",
                User.rol.in_(["empleado", "admin"]),
                User.empresa_id == owner_id,
            ).first()
        else:
            candidatos = self._empleados_para_ticket_pyme(ticket, actor_id)
            if not candidatos or (not auto and not self.auto_assign_enabled):
                return None
            empleado = min(
                candidatos,
                key=lambda emp: self._calcular_carga_empleado_pyme(emp, ticket.rubro_id),
            )

        if not empleado:
            return None

        if ticket.asignado_a_id == empleado.id:
            return empleado

        ticket.asignado_a = empleado
        ticket.asignado_en = get_local_now()
        if hasattr(ticket, "ultima_actividad"):
            ticket.ultima_actividad = get_local_now()

        comentario = TicketComentario(
            pyme_ticket_id=ticket.id,
            comentario=f"Ticket asignado a {empleado.name}",
            user_id=actor_id,
            es_admin=True,
            origen="sistema",
        )
        db.session.add(comentario)

        return empleado

    def crear_nuevo_ticket(
        self,
        tipo_ticket: Literal["municipio", "pyme"],
        ticket_data: Dict[str, Any],
        *,
        return_object: bool = False,
    ) -> Union[PymeTicket, MunicipioTicket, None, dict]:
        creator = self.creators.get(tipo_ticket)
        if not creator:
            raise ValueError(f"Tipo de ticket inválido: '{tipo_ticket}'.")

        # Inferir categoría a partir de campos alternativos si no fue provista
        if not ticket_data.get("categoria"):
            tipo_general = (
                ticket_data.get("tipo")
                or ticket_data.get("tipo_ticket")
                or ticket_data.get("tipo_reclamo")
            )
            if tipo_general:
                ticket_data["categoria"] = tipo_general

        # Normalizar categoría para evitar duplicados como variantes de 'luminarias'
        if "categoria" in ticket_data:
            ticket_data["categoria"] = normalize_category(ticket_data.get("categoria"))

        ticket_data["nro_ticket"] = random.randint(100000, 999999)
        try:
            # Eliminar el prefijo "Reclamo (LLM):" del asunto si existe
            if ticket_data.get("asunto", "").startswith("Reclamo (LLM):"):
                ticket_data["asunto"] = ticket_data["asunto"].replace("Reclamo (LLM):", "").strip()

            ticket = creator.create(ticket_data)
            db.session.add(ticket)
            db.session.flush() # flush para obtener el ID del ticket para el comentario

            if (
                self.auto_assign_enabled
                and tipo_ticket == "municipio"
                and isinstance(ticket, MunicipioTicket)
            ):
                try:
                    self.asignar_ticket_municipal(ticket, auto=True)
                except Exception:
                    logger.exception("No se pudo asignar automáticamente el ticket municipal")

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

            # Notificaciones por email (admin y cliente)
            self._notificar_ticket_por_email(ticket, tipo_ticket, ticket_data)

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

            if return_object:
                return ticket
            return ticket_dict
        except SQLAlchemyError as e:
            db.session.rollback()
            logger.error(f"Error de DB al crear ticket: {e}", exc_info=True)
            return None

    def _notificar_ticket_por_email(self, ticket, tipo_ticket: str, ticket_data: Dict[str, Any]) -> None:
        """Envía notificaciones por email al administrador y al cliente si corresponde."""
        if not ticket:
            return

        try:
            from services.email_service import (
                enviar_email_ticket_admin,
                enviar_email_ticket_cliente,
            )

            admin_user = None
            owner_id = None

            if tipo_ticket == "municipio":
                owner_id = ticket_data.get("municipio_id") or getattr(ticket, "municipio_id", None)
            elif tipo_ticket == "pyme":
                owner_id = ticket_data.get("pyme_id")

            if owner_id:
                try:
                    admin_user = db.session.get(User, owner_id)
                except Exception:  # pragma: no cover - defensive, should not happen in tests
                    admin_user = None

            if (
                not admin_user
                and tipo_ticket == "pyme"
                and getattr(ticket, "rubro_id", None)
                and hasattr(User, "query")
            ):
                try:
                    admin_user = User.query.filter_by(rubro_id=ticket.rubro_id).first()
                except Exception:  # pragma: no cover - defensive fallback
                    admin_user = None

            enviar_email_ticket_admin(
                ticket,
                admin_user=admin_user,
                tipo_ticket=tipo_ticket,
                ticket_data=ticket_data,
            )
            enviar_email_ticket_cliente(
                ticket,
                tipo_ticket=tipo_ticket,
                admin_user=admin_user,
                ticket_data=ticket_data,
            )
        except Exception as e:  # pragma: no cover - logging only
            logger.error(
                f"Error enviando notificaciones por email para ticket {getattr(ticket, 'id', 'N/A')}: {e}",
                exc_info=True,
            )

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
            if hasattr(ticket, "ultima_actividad"):
                ticket.ultima_actividad = get_local_now()
            # db.session.commit() # <<< ELIMINADO
            try:
                from services.email_service import (
                    enviar_email_ticket_novedad,
                    enviar_sms_ticket_novedad,
                    enviar_whatsapp_ticket_novedad, # <--- IMPORTAR NUEVA FUNCIÓN
                    enviar_email_ticket_admin,
                )
                comentario_texto = comentario_data.get('comentario', '') or ''
                mensaje_notificacion = f"Nuevo comentario en tu ticket #{ticket.nro_ticket}: {comentario_texto[:50]}..."
                mensaje_completo = comentario_texto.strip() or mensaje_notificacion
                if nuevo_comentario.es_admin: # Notificar al usuario/cliente
                    enviar_email_ticket_novedad(
                        ticket,
                        mensaje_completo,
                        comentario_reciente=nuevo_comentario,
                    )
                    enviar_sms_ticket_novedad(ticket, mensaje_notificacion)
                    if tipo_ticket == "municipio": # Por ahora, WhatsApp solo para municipio
                        enviar_whatsapp_ticket_novedad(ticket, mensaje_notificacion)
                else: # Notificar al admin/empleado
                    enviar_email_ticket_admin(
                        ticket,
                        tipo_ticket=tipo_ticket,
                        comentario_reciente=nuevo_comentario,
                        mensaje_resumen=mensaje_completo,
                    )
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
        categoria: str | Iterable[str] | None = None,
        distrito: str | None = None,
        estado: str | Iterable[str] | None = None, # Nuevo parámetro de estado
        satisfactorio: bool | None = None,
    ) -> list[dict]:
        """
        Devuelve los tickets con ubicación, opcionalmente filtrados por estado,
        agrupados por ubicación y con un peso (cantidad de tickets).
        Permite filtrar por municipio/rubro, rango de fechas y categoría.
        """
        Model = MunicipioTicket if tipo_ticket == "municipio" else PymeTicket
        try:
            logger.info(
                "[TICKET_SERVICE_MAPA] tipo=%s municipio_id=%s rubro_id=%s fecha_inicio=%s fecha_fin=%s categoria=%s distrito=%s estado=%s",
                tipo_ticket,
                municipio_id,
                rubro_id,
                fecha_inicio,
                fecha_fin,
                categoria,
                distrito,
                estado,
            )

            query = Model.query.filter(Model.latitud.isnot(None), Model.longitud.isnot(None))

            distrito_filtrado = distrito.strip() if isinstance(distrito, str) else None
            if distrito_filtrado and hasattr(Model, "distrito"):
                query = query.filter(Model.distrito == distrito_filtrado)

            estados_filtrar_set: set[str] | None = None
            # Filtrar por estado si se proporciona. Si el estado solicitado es
            # "resuelto", también incluimos aquellos marcados como "cerrado" para
            # que el frontend pueda tratarlos como reclamos resueltos.
            if estado:
                if isinstance(estado, str):
                    estados_solicitados = [estado.strip()] if estado.strip() else []
                else:
                    estados_solicitados = [
                        valor.strip()
                        for valor in estado
                        if isinstance(valor, str) and valor.strip()
                    ]

                if estados_solicitados:
                    estados_expandidos: list[str] = []
                    for estado_solicitado in estados_solicitados:
                        if estado_solicitado == "resuelto":
                            estados_expandidos.extend(["resuelto", "cerrado"])
                        else:
                            estados_expandidos.append(estado_solicitado)

                    # Quitar duplicados preservando el orden
                    estados_unicos: list[str] = []
                    vistos_estados: set[str] = set()
                    for estado_unico in estados_expandidos:
                        if estado_unico not in vistos_estados:
                            estados_unicos.append(estado_unico)
                            vistos_estados.add(estado_unico)

                    if estados_unicos:
                        estados_filtrar_set = set(estados_unicos)
                        query = query.filter(Model.estado.in_(estados_unicos))
            # else: # Comportamiento por defecto si no se especifica estado (ej: no cerrados)
            #     query = query.filter(Model.estado != "cerrado") # Opcional: mantener un filtro por defecto

            # Si se solicita solo tickets satisfactorios, unimos con
            # TicketSatisfaccion para asegurar que exista una encuesta asociada.
            if satisfactorio:
                query = query.join(
                    TicketSatisfaccion,
                    (TicketSatisfaccion.ticket_id == Model.id)
                    & (TicketSatisfaccion.tipo == tipo_ticket),
                )

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

            categorias_filtrar_lower: list[str] = []
            # Check if model has a 'categoria' column before filtering by it
            has_categoria_column = hasattr(Model, 'categoria')

            # PymeTicket typically stores category in 'categoria' (String) as per schema,
            # but log error 'column pyme_ticket.categoria_id does not exist' suggests
            # something else might have been trying to join or filter by ID.
            # The code block below filters by `Model.categoria` string column.

            if categoria and has_categoria_column:
                if isinstance(categoria, str):
                    raw_values = [categoria]
                else:
                    raw_values = list(categoria)

                vistos: set[str] = set()
                for raw in raw_values:
                    if not isinstance(raw, str):
                        continue
                    texto = raw.strip()
                    if not texto:
                        continue
                    clave = texto.lower()
                    if clave in vistos:
                        continue
                    vistos.add(clave)
                    categorias_filtrar_lower.append(clave)

                if categorias_filtrar_lower:
                    query = query.filter(
                        db.func.lower(Model.categoria).in_(categorias_filtrar_lower)
                    )

            tickets = query.all()
            logger.info(
                "[TICKET_SERVICE_MAPA] tickets_raw=%s",
                len(tickets),
            )

            if distrito_filtrado and hasattr(Model, "distrito"):
                tickets = [
                    t for t in tickets if getattr(t, "distrito", None) == distrito_filtrado
                ]

            if estados_filtrar_set:
                tickets = [
                    t for t in tickets if getattr(t, "estado", None) in estados_filtrar_set
                ]

            if categorias_filtrar_lower and hasattr(Model, "categoria"):
                tickets = [
                    t
                    for t in tickets
                    if isinstance(getattr(t, "categoria", None), str)
                    and getattr(t, "categoria").strip().lower() in categorias_filtrar_lower
                ]

            # El agrupamiento por ubicación y el cálculo de 'weight' permanecen igual.
            # Si se desea devolver todos los puntos individualmente para que el frontend agrupe/clusterice:
            # return [
            #     {
            #         "id": t.id, "lat": t.latitud, "lng": t.longitud, "estado": t.estado,
            #         "asunto": t.asunto, "nro_ticket": t.nro_ticket, "categoria": t.categoria
            #     } for t in tickets
            # ]
            # Por ahora, mantendremos la agrupación existente que devuelve 'weight'.

            ubicaciones_agrupadas = {}  # (lat, lng, categoria) -> count

            for t in tickets:
                # Redondear lat/lng a un número de decimales para agrupar puntos cercanos.
                # Ajustar el número de decimales según la precisión deseada.
                # 5 decimales dan una precisión de ~1.1 metros.
                # 4 decimales dan una precisión de ~11 metros.
                # 3 decimales dan una precisión de ~110 metros.
                # Consideremos 4 decimales para agrupar problemáticas en una misma "zona pequeña".
                lat_lng_key = (
                    round(t.latitud, 4),
                    round(t.longitud, 4),
                    getattr(t, "categoria", None),
                )
                if lat_lng_key not in ubicaciones_agrupadas:
                    ubicaciones_agrupadas[lat_lng_key] = 0
                ubicaciones_agrupadas[lat_lng_key] += 1

            resultado_heatmap = []
            for (lat, lng, cat), weight in ubicaciones_agrupadas.items():
                resultado_heatmap.append(
                    {
                        "location": {"lat": lat, "lng": lng},
                        "lat": lat,
                        "lng": lng,
                        "weight": weight,
                        "categoria": cat,
                    }
                )
            enrich_heatmap_points(
                resultado_heatmap,
                property_keys=("categoria", "estado", "barrio", "fuente"),
            )
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

    def obtener_historial_chat(self, ticket: Union[MunicipioTicket, PymeTicket]) -> list[dict]:
        """Devuelve el historial completo de conversación para un ticket.

        Combina el historial previo almacenado en ``Conversacion`` (pregunta/
        respuesta del bot) con los comentarios posteriores guardados en
        ``TicketComentario``. Cada entrada se normaliza con metadatos de autor
        para que el frontend pueda distinguir entre mensajes del municipio y
        del vecino.
        """
        mensajes: list[dict] = []

        # --- Conversaciones previas al ticket (chatbot) ---
        if getattr(ticket, "anon_id", None):
            try:
                conversaciones = (
                    Conversacion.query.filter_by(session_id=ticket.anon_id)
                    .order_by(Conversacion.timestamp.asc())
                    .all()
                )
            except Exception:
                conversaciones = []

            nombre_vecino = (
                getattr(ticket, "nombre_vecino", None)
                or getattr(ticket, "nombre_cliente", None)
                or "Vecino/a"
            )

            for conv in conversaciones:
                if conv.pregunta:
                    mensajes.append(
                        {
                            "texto": conv.pregunta,
                            "fecha": datetime_to_iso_utc(conv.timestamp),
                            "autor": "vecino",
                            "autor_nombre": nombre_vecino,
                            "es_admin": False,
                        }
                    )
                if conv.respuesta:
                    # Añadir un pequeño delta para conservar el orden pregunta-respuesta
                    respuesta_fecha = datetime_to_iso_utc(
                        conv.timestamp + timedelta(milliseconds=1)
                    )
                    mensajes.append(
                        {
                            "texto": conv.respuesta,
                            "fecha": respuesta_fecha,
                            "autor": "municipio",
                            "autor_nombre": "Chatbot",
                            "es_admin": True,
                        }
                    )

        # --- Comentarios del ticket (posteriores) ---
        try:
            comentarios = ticket.comentarios.order_by(TicketComentario.fecha.asc()).all()
        except Exception:
            comentarios = []

        for c in comentarios:
            data = c.to_dict()
            data["texto"] = data.pop("comentario")
            data["fecha"] = datetime_to_iso_utc(c.fecha)
            mensajes.append(data)

        # Orden cronológico por fecha
        mensajes.sort(key=lambda x: x["fecha"])
        return mensajes

    def obtener_timeline_ticket(self, ticket: Union[MunicipioTicket, PymeTicket]) -> list[dict]:
        """Construye la línea de tiempo de un ticket con comentarios y cambios de estado."""
        try:
            comentarios = ticket.comentarios.order_by(TicketComentario.fecha.asc()).all()
        except Exception:
            comentarios = []

        def _estado_publico(estado: str) -> str:
            """Normaliza estados internos para mostrarlos al público."""
            return "resuelto" if estado == "cerrado" else estado

        timeline = [
            {
                "tipo": "ticket_creado",
                "estado": "nuevo",
                "fecha": datetime_to_iso_utc(ticket.fecha),
            }
        ]

        for c in comentarios:
            if c.estado_ticket:
                timeline.append(
                    {
                        "tipo": "estado",
                        "estado": _estado_publico(c.estado_ticket),
                        "fecha": datetime_to_iso_utc(c.fecha),
                    }
                )
            else:
                autor_tipo = "municipio" if c.es_admin else "vecino"
                if c.es_admin:
                    nombre_autor = "Municipio"
                    if c.user_id:
                        usuario = db.session.get(User, c.user_id)
                        if usuario and usuario.name:
                            nombre_autor = usuario.name
                else:
                    nombre_autor = None
                    if c.municipio_ticket and getattr(c.municipio_ticket, "nombre_vecino", None):
                        nombre_autor = c.municipio_ticket.nombre_vecino
                    elif c.pyme_ticket and getattr(c.pyme_ticket, "nombre_cliente", None):
                        nombre_autor = c.pyme_ticket.nombre_cliente
                    if not nombre_autor:
                        nombre_autor = "Vecino/a"
                timeline.append(
                    {
                        "tipo": "comentario",
                        "texto": c.comentario,
                        "fecha": datetime_to_iso_utc(c.fecha),
                        "es_admin": c.es_admin,
                        "user_id": c.user_id,
                        "autor": autor_tipo,
                        "autor_nombre": nombre_autor,
                    }
                )

        estado_actual = _estado_publico(getattr(ticket, "estado", None))
        if estado_actual:
            estado_ya_registrado = any(
                evento.get("tipo") == "estado" and evento.get("estado") == estado_actual
                for evento in timeline
            )
            if not estado_ya_registrado:
                fecha_estado = getattr(ticket, "ultima_actividad", None) or ticket.fecha
                timeline.append(
                    {
                        "tipo": "estado",
                        "estado": estado_actual,
                        "fecha": datetime_to_iso_utc(fecha_estado),
                    }
                )

        timeline.sort(key=lambda evento: evento.get("fecha") or "")

        return timeline

    def obtener_estado_progreso(self, ticket: Union[MunicipioTicket, PymeTicket]) -> list[dict]:
        """Genera una lista ordenada con los estados principales del ticket."""
        timeline = self.obtener_timeline_ticket(ticket)

        estados = {
            "nuevo": {"completado": False, "fecha": None},
            "en_proceso": {"completado": False, "fecha": None},
            "completado": {"completado": False, "fecha": None},
            "resuelto": {"completado": False, "fecha": None},
        }

        for evento in timeline:
            if evento.get("tipo") == "ticket_creado":
                estados["nuevo"] = {"completado": True, "fecha": evento.get("fecha")}
            elif evento.get("tipo") == "estado":
                nombre_estado = evento.get("estado")
                if nombre_estado in ("en_progreso", "en progreso", "en_proceso"):
                    estados["en_proceso"] = {"completado": True, "fecha": evento.get("fecha")}
                elif nombre_estado == "completado":
                    estados["completado"] = {"completado": True, "fecha": evento.get("fecha")}
                elif nombre_estado in ("resuelto", "cerrado"):
                    estados["completado"] = {"completado": True, "fecha": evento.get("fecha")}
                    estados["resuelto"] = {"completado": True, "fecha": evento.get("fecha")}

        return [
            {"estado": "nuevo", **estados["nuevo"]},
            {"estado": "en_proceso", **estados["en_proceso"]},
            {"estado": "completado", **estados["completado"]},
            {"estado": "resuelto", **estados["resuelto"]},
        ]

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
