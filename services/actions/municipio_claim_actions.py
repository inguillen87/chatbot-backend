# services/actions/municipio_claim_actions.py
import logging
from typing import Dict, Any
from .base_action_handler import BaseActionHandler
from services.ticket_service import servicio_tickets # Assuming this service exists and is robust
from models import ArchivoAdjunto, AnalisisArchivo, db as global_db, User # Assuming User model for fetching details

logger = logging.getLogger(__name__)

class CrearReclamoAction(BaseActionHandler):
    def execute(self, action_data: Dict[str, Any]) -> Dict[str, Any]:
        logger.info(f"Executing CrearReclamoAction with data: {action_data}")

        required_fields = ["categoria", "descripcion", "ubicacion_original_reclamo"] # ubicacion_original_reclamo is what LLM provides
        if not self.validate_data(action_data, required_fields):
            return {
                "success": False,
                "message_to_user": "Faltan datos esenciales para crear el reclamo (categoría, descripción o ubicación).",
                "error_details": "Missing required fields for CrearReclamoAction."
            }

        # Extract and process data
        categoria = action_data.get("categoria", "Reclamo General")
        descripcion = action_data.get("descripcion")
        ubicacion_original_reclamo = action_data.get("ubicacion_original_reclamo") # Textual location from LLM

        # Attempt to parse structured address if LLM provides components, otherwise use textual ubicacion
        direccion_estructurada = action_data.get("direccion_estructurada") # e.g., {"calle": "San Martin", "numero": "123", "localidad": "Centro"}
        direccion_final_para_ticket = ubicacion_original_reclamo # Default to textual

        if isinstance(direccion_estructurada, dict) and direccion_estructurada.get("calle"):
            partes_dir = [direccion_estructurada["calle"]]
            if direccion_estructurada.get("numero"): partes_dir.append(direccion_estructurada["numero"])
            if direccion_estructurada.get("localidad"): partes_dir.append(direccion_estructurada["localidad"])
            direccion_final_para_ticket = ", ".join(filter(None, partes_dir))

        coordenadas = action_data.get("coordenadas") # e.g., {"lat": -32.88, "lon": -68.84}
        latitud = coordenadas.get("lat") if isinstance(coordenadas, dict) else None
        longitud = coordenadas.get("lon") if isinstance(coordenadas, dict) else None

        # User details (can be from LLM or context)
        nombre_vecino = action_data.get("nombre_vecino") or self.context.get("nombre_usuario_contexto")
        telefono_vecino = action_data.get("telefono_vecino") or self.context.get("telefono_usuario_contexto")
        email_vecino = action_data.get("email_vecino") or self.context.get("email_usuario_contexto")

        # Context details
        user_id_db = self.context.get("cliente_id") # ID of the ChatUser/User final
        anon_id_db = self.context.get("anon_id") if not user_id_db else None
        owner_user_obj = self.context.get("user_obj") # The User object of the bot instance (municipality)
        municipio_db_id_para_ticket = getattr(owner_user_obj, "municipio_id", None)

        # File attachment details from context (if an image/doc was processed)
        archivo_id_para_asociar = self.context.get("archivo_id_para_asociar") # ID of ArchivoAdjunto
        foto_url_directa_contexto = self.context.get("foto_url") # URL of image if directly available (e.g. WhatsApp)

        # If a file was processed, its analysis might provide better category/description
        # This logic might be enhanced if document_processor provides structured output.
        # For now, we assume LLM's action_data is the primary source after any pre-processing.

        ticket_payload = {
            "asunto": f"Reclamo ({categoria})",
            "categoria": categoria,
            "detalles": descripcion,
            "direccion": direccion_final_para_ticket,
            "latitud": latitud,
            "longitud": longitud,
            "nombre_vecino": nombre_vecino,
            "telefono_vecino": telefono_vecino, # servicio_tickets should normalize/validate
            "email_vecino": email_vecino,     # servicio_tickets should normalize/validate
            "estado": "nuevo",
            "user_id": user_id_db,
            "anon_id": anon_id_db,
            "municipio_id": municipio_db_id_para_ticket,
            "origen_reclamo": self.context.get("channel", "web") # web, whatsapp, etc.
        }

        if foto_url_directa_contexto and not archivo_id_para_asociar:
            ticket_payload["foto_url_directa"] = foto_url_directa_contexto
            logger.info(f"Including foto_url_directa from context: {foto_url_directa_contexto}")
        elif action_data.get("foto_url_adjunta"): # If LLM provided a URL for a photo it was shown
             ticket_payload["foto_url_directa"] = action_data.get("foto_url_adjunta")


        # Remove None values before sending to service
        ticket_data_cleaned = {k: v for k, v in ticket_payload.items() if v is not None}
        logger.info(f"Data for servicio_tickets.crear_nuevo_ticket: {ticket_data_cleaned}")

        try:
            ticket_creado = servicio_tickets.crear_nuevo_ticket(tipo_ticket="municipio", ticket_data=ticket_data_cleaned)
            if not ticket_creado:
                logger.error("servicio_tickets.crear_nuevo_ticket returned None.")
                return {"success": False, "message_to_user": "No se pudo registrar el reclamo en este momento."}

            nro_ticket_str = f"M-{ticket_creado.nro_ticket}"
            logger.info(f"Reclamo creado exitosamente: {nro_ticket_str}")

            # Associate ArchivoAdjunto if archivo_id_para_asociar exists
            if archivo_id_para_asociar:
                from services.archivo_service import archivo_service # Local import to avoid circularity at module level
                asociacion_exitosa = archivo_service.asociar_archivos_a_ticket(
                    ticket_id=ticket_creado.id,
                    tipo_ticket="municipio",
                    ids_archivos=[archivo_id_para_asociar]
                )
                if asociacion_exitosa:
                    logger.info(f"Archivo ID {archivo_id_para_asociar} asociado a ticket {nro_ticket_str}.")
                    # Clear from context after successful association
                    if "archivo_id_para_asociar" in self.context:
                         del self.context["archivo_id_para_asociar"]
                else:
                    logger.warning(f"No se pudo asociar archivo ID {archivo_id_para_asociar} a ticket {nro_ticket_str}.")

            # TODO: Send notifications (email, WhatsApp) - this might be handled by ticket_service or here

            return {
                "success": True,
                "message_to_user": f"Tu reclamo sobre '{categoria}' ha sido registrado con el número {nro_ticket_str}. Gracias.",
                "data": {"ticket_id": ticket_creado.id, "nro_ticket": nro_ticket_str}
            }

        except Exception as e:
            logger.error(f"Error en CrearReclamoAction: {e}", exc_info=True)
            # global_db.session.rollback() # Ensure rollback if ticket_service doesn't handle it
            return {
                "success": False,
                "message_to_user": "Hubo un error técnico al registrar tu reclamo. Por favor, intenta más tarde.",
                "error_details": str(e)
            }

class ConsultarEstadoReclamoAction(BaseActionHandler):
    def execute(self, action_data: Dict[str, Any]) -> Dict[str, Any]:
        logger.info(f"Executing ConsultarEstadoReclamoAction with data: {action_data}")

        nro_ticket_str = action_data.get("nro_ticket")
        if not nro_ticket_str:
            return {"success": False, "message_to_user": "Necesito el número de ticket para consultar su estado."}

        # Clean up "M-" prefix if present
        if isinstance(nro_ticket_str, str) and nro_ticket_str.upper().startswith("M-"):
            nro_ticket_str = nro_ticket_str[2:]

        try:
            nro_ticket = int(nro_ticket_str)
        except ValueError:
            return {"success": False, "message_to_user": f"El número de ticket '{nro_ticket_str}' no parece válido."}

        # Assuming MunicipioTicket model is accessible
        from models import MunicipioTicket, TicketComentario

        # owner_user_obj = self.context.get("user_obj")
        # municipio_id_context = getattr(owner_user_obj, "municipio_id", None)
        # if not municipio_id_context:
        #     logger.warning("ConsultarEstadoReclamoAction: No municipio_id in context. Cannot filter by municipality.")
        #     # Decide if you want to allow search across all municipalities or restrict
        #     # For now, let's assume tickets are unique enough or this is for a specific muni context

        ticket = MunicipioTicket.query.filter_by(nro_ticket=nro_ticket).first()

        if not ticket:
            return {"success": False, "message_to_user": f"No se encontró el ticket M-{nro_ticket}."}

        # Optional: Check if the current user (viewer_user) has permission to view this ticket
        # viewer_user_id = self.context.get("cliente_id")
        # anon_id_context = self.context.get("anon_id")
        # if ticket.user_id and viewer_user_id and ticket.user_id != viewer_user_id:
        #     # Potentially allow if admin/employee of the municipality
        #     # For now, simple check. This logic can be enhanced.
        #     return {"success": False, "message_to_user": "No tienes permiso para ver este ticket."}
        # elif ticket.anon_id and anon_id_context and ticket.anon_id != anon_id_context:
        #     return {"success": False, "message_to_user": "No tienes permiso para ver este ticket (anónimo)."}


        respuesta = f"El ticket **M-{ticket.nro_ticket}** (Asunto: '{ticket.asunto}') se encuentra en estado: **{ticket.estado.replace('_', ' ').title()}**."

        ultimo_comentario_admin = TicketComentario.query.filter(
            TicketComentario.municipio_ticket_id == ticket.id,
            TicketComentario.es_admin == True
        ).order_by(TicketComentario.fecha.desc()).first()

        if ultimo_comentario_admin:
            respuesta += f"\nÚltima actualización del municipio: *{ultimo_comentario_admin.comentario}* (el {ultimo_comentario_admin.fecha.strftime('%d/%m/%Y')})."
        elif ticket.detalles:
             respuesta += f"\nDetalles originales: {ticket.detalles[:100]}{'...' if len(ticket.detalles) > 100 else ''}"


        return {
            "success": True,
            "message_to_user": respuesta,
            "data": {
                "ticket_id": ticket.id,
                "nro_ticket": ticket.nro_ticket,
                "estado": ticket.estado,
                "asunto": ticket.asunto,
                "categoria": ticket.categoria,
                "fecha_creacion": ticket.fecha.isoformat(),
                "ultima_actualizacion_admin": ultimo_comentario_admin.fecha.isoformat() if ultimo_comentario_admin else None,
                "comentario_admin": ultimo_comentario_admin.comentario if ultimo_comentario_admin else None
            }
        }

# Add more municipio specific actions here
# e.g., ModificarReclamoAction, CancelarReclamoAction, AdjuntarFotoReclamoAction
