# services/actions/municipio_claim_actions.py
import logging
from typing import Dict, Any
from .base_action_handler import BaseActionHandler
from services.ticket_service import servicio_tickets
from models import ArchivoAdjunto, AnalisisArchivo, db as global_db, User, MunicipioTicket, TicketComentario
# Import common utils if needed for validation, e.g.
# from services.common_utils import validar_telefono, formatear_telefono_e164

logger = logging.getLogger(__name__)

class CrearReclamoAction(BaseActionHandler):
    def execute(self, action_data: Dict[str, Any]) -> Dict[str, Any]:
        logger.info(f"Executing CrearReclamoAction with data: {action_data}")

        categoria = action_data.get("categoria")
        descripcion = action_data.get("descripcion")
        ubicacion = action_data.get("ubicacion")

        if not all([categoria, descripcion, ubicacion]):
            missing_fields = [f for f, v in {"categoría": categoria, "descripción": descripcion, "ubicación": ubicacion}.items() if not v]
            return {
                "success": False,
                "message_to_user": f"Faltan datos para crear el reclamo: {', '.join(missing_fields)}. Por favor, intenta de nuevo.",
                "pedir_info": missing_fields[0] if missing_fields else "datos_reclamo_faltantes",
                "error_details": "Missing required fields for CrearReclamoAction."
            }

        try:
            ticket_creado = servicio_tickets.crear_nuevo_ticket(
                tipo_ticket="municipio",
                ticket_data={
                    "asunto": f"Reclamo ({categoria})",
                    "categoria": categoria,
                    "detalles": descripcion,
                    "direccion": ubicacion,
                    "estado": "nuevo",
                    "user_id": self.context.get("cliente_id"),
                    "anon_id": self.context.get("anon_id") if not self.context.get("cliente_id") else None,
                    "municipio_id": getattr(self.context.get("user_obj"), "municipio_id", None),
                    "origen_reclamo": self.context.get("channel", "web")
                }
            )
            if not ticket_creado:
                raise Exception("servicio_tickets.crear_nuevo_ticket returned None")

            nro_ticket_str = f"M-{ticket_creado.nro_ticket}"
            logger.info(f"Reclamo creado exitosamente: {nro_ticket_str}")

            return {
                "success": True,
                "message_to_user": f"Tu reclamo sobre '{categoria}' ha sido registrado con el número {nro_ticket_str}. Gracias por tu colaboración.",
                "data": {"ticket_id": ticket_creado.id, "nro_ticket": nro_ticket_str, "status_reclamo": "registrado"}
            }
        except Exception as e:
            logger.error(f"Error en CrearReclamoAction: {e}", exc_info=True)
            return {
                "success": False,
                "message_to_user": "Hubo un error técnico al registrar tu reclamo. Por favor, intenta más tarde.",
                "error_details": str(e)
            }

class ConsultarEstadoReclamoAction(BaseActionHandler):
    def execute(self, action_data: Dict[str, Any]) -> Dict[str, Any]:
        logger.info(f"Executing ConsultarEstadoReclamoAction with data: {action_data}")

        nro_ticket_llm = action_data.get("id_ticket_mencionado") # LLM should provide this
        if not nro_ticket_llm:
            return {"success": False, "message_to_user": "Necesito el número de ticket para consultar su estado.", "pedir_info": "id_ticket_mencionado"}

        nro_ticket_str = str(nro_ticket_llm) # Ensure it's a string for processing
        if nro_ticket_str.upper().startswith("M-"):
            nro_ticket_str = nro_ticket_str[2:] # Remove "M-" prefix

        try:
            nro_ticket_int = int(nro_ticket_str)
        except ValueError:
            return {"success": False, "message_to_user": f"El número de ticket '{nro_ticket_llm}' no parece válido."}

        ticket = MunicipioTicket.query.filter_by(nro_ticket=nro_ticket_int).first()

        if not ticket:
            return {"success": False, "message_to_user": f"No se encontró el ticket M-{nro_ticket_int}."}

        # Basic permission check (can be expanded)
        # viewer_user_id = self.context.get("cliente_id")
        # if ticket.user_id and viewer_user_id and ticket.user_id != viewer_user_id:
        #     # Add logic for admin/employee override if necessary
        #     return {"success": False, "message_to_user": "No tienes permiso para ver este ticket."}

        respuesta = f"El ticket **M-{ticket.nro_ticket}** (Asunto: '{ticket.asunto}') se encuentra en estado: **{ticket.estado.replace('_', ' ').title()}**."
        ultimo_comentario_admin = TicketComentario.query.filter(
            TicketComentario.municipio_ticket_id == ticket.id, TicketComentario.es_admin == True
        ).order_by(TicketComentario.fecha.desc()).first()

        if ultimo_comentario_admin:
            respuesta += f"\nÚltima actualización del municipio: *{ultimo_comentario_admin.comentario}* (el {ultimo_comentario_admin.fecha.strftime('%d/%m/%Y')})."
        elif ticket.detalles: # Fallback to original details if no admin comment
             respuesta += f"\nDetalles originales: {ticket.detalles[:100]}{'...' if len(ticket.detalles) > 100 else ''}"

        return {
            "success": True,
            "message_to_user": respuesta,
            "data": {
                "ticket_id": ticket.id, "nro_ticket": ticket.nro_ticket, "estado": ticket.estado,
                "asunto": ticket.asunto, "categoria": ticket.categoria,
                "fecha_creacion": ticket.fecha.isoformat(),
                "ultima_actualizacion_admin": ultimo_comentario_admin.fecha.isoformat() if ultimo_comentario_admin else None,
                "comentario_admin": ultimo_comentario_admin.comentario if ultimo_comentario_admin else None
            }
        }

# Placeholder for other Municipio Action Handlers
# These should be implemented based on the ACTION_HANDLER_MAP in __init__.py
# Ensure they align with the expected 'action_data' from the LLM and 'context' provided by the orchestrator.

class ConsultarInfoTramiteAction(BaseActionHandler):
    def execute(self, action_data: Dict[str, Any]) -> Dict[str, Any]:
        logger.info(f"Executing ConsultarInfoTramiteAction with data: {action_data}")
        tramite_nombre = action_data.get("nombre_tramite") or action_data.get("categoria") # LLM might use 'categoria' for a general topic
        if not tramite_nombre:
            return {"success": False, "message_to_user": "¿Sobre qué trámite necesitas información?", "pedir_info": "nombre_tramite"}

        # Simulate fetching tramite info - replace with actual logic from services.municipios.get_tramites_info() or similar
        # from services.municipios import get_tramites_info # Assuming this function exists
        # tramites_db = get_tramites_info()
        # info_tramite_raw = tramites_db.get(tramite_nombre.lower()) or tramites_db.get(tramite_nombre) # Case-insensitive lookup

        simulated_info = f"Información sobre el trámite '{tramite_nombre}': [Detalles del trámite, requisitos, costos, horarios, etc.]."
        # if info_tramite_raw and isinstance(info_tramite_raw, dict):
        #    simulated_info = info_tramite_raw.get("descripcion", "No hay descripción detallada.")
        #    if info_tramite_raw.get("link"): simulated_info += f" Más info en: {info_tramite_raw['link']}"
        #    if info_tramite_raw.get("botones"): # LLM might format these as text or suggest actions
        #        simulated_info += " Opciones adicionales: " + ", ".join([b['texto'] for b in info_tramite_raw['botones']])


        return {"success": True, "message_to_user": simulated_info, "data": {"tramite_consultado": tramite_nombre}}

class HacerSugerenciaAction(BaseActionHandler):
    def execute(self, action_data: Dict[str, Any]) -> Dict[str, Any]:
        logger.info(f"Executing HacerSugerenciaAction with data: {action_data}")
        descripcion_sugerencia = action_data.get("descripcion")
        if not descripcion_sugerencia:
            return {"success": False, "message_to_user": "Claro, ¿cuál es tu sugerencia?", "pedir_info": "descripcion_sugerencia"}

        # Simulate saving suggestion (e.g., as a specific type of ticket)
        # ticket_sugerencia_payload = {
        #     "asunto": "Sugerencia del Vecino", "categoria": "Sugerencia", "detalles": descripcion_sugerencia,
        #     "user_id": self.context.get("cliente_id"), "anon_id": self.context.get("anon_id"),
        #     "municipio_id": getattr(self.context.get("user_obj"), "municipio_id", None), "estado": "nueva_sugerencia"
        # }
        # ticket_sug = servicio_tickets.crear_nuevo_ticket(tipo_ticket="municipio", ticket_data=ticket_sugerencia_payload)
        simulated_sug_id = "SUG-SIM" + str(action_data.get("id_simulacion", "001")) # Placeholder
        user_message = f"¡Muchas gracias por tu sugerencia! ('{descripcion_sugerencia[:30]}...') La hemos registrado con el ID {simulated_sug_id} y será revisada por nuestro equipo."
        return {"success": True, "message_to_user": user_message, "data": {"sugerencia_id": simulated_sug_id, "status_sugerencia": "registrada"}}

class EjecutarHerramientaAction(BaseActionHandler):
    def execute(self, action_data: Dict[str, Any]) -> Dict[str, Any]:
        logger.info(f"Executing EjecutarHerramientaAction with data: {action_data}")
        nombre_herramienta = action_data.get("nombre_herramienta")
        parametros = action_data.get("parametros_herramienta", {})

        if not nombre_herramienta:
            return {"success": False, "message_to_user": "No pude identificar la herramienta a ejecutar."}

        # from services.herramientas_municipio import TOOL_REGISTRY # Assuming TOOL_REGISTRY exists
        # if nombre_herramienta not in TOOL_REGISTRY:
        #     return {"success": False, "message_to_user": f"Herramienta '{nombre_herramienta}' no reconocida."}
        # funcion_a_ejecutar = TOOL_REGISTRY[nombre_herramienta]["funcion"]
        # try:
        #     resultado = funcion_a_ejecutar(**parametros) # Ensure params match function signature
        #     return {"success": True, "message_to_user": str(resultado), "data": {"resultado_herramienta": resultado}}
        # except Exception as e_tool:
        #     logger.error(f"Error ejecutando herramienta '{nombre_herramienta}': {e_tool}", exc_info=True)
        #     return {"success": False, "message_to_user": "Hubo un error al usar la herramienta solicitada."}

        # Placeholder simulation
        resultado_simulado = f"Resultado simulado de la herramienta '{nombre_herramienta}' con parámetros {parametros}."
        if nombre_herramienta == "consultar_recoleccion_por_direccion" and parametros.get("direccion"):
            resultado_simulado = f"Según mis registros, la recolección en '{parametros.get('direccion')}' es los Lunes, Miércoles y Viernes por la mañana."
        return {"success": True, "message_to_user": resultado_simulado, "data": {"resultado_herramienta": "simulado", "herramienta_usada": nombre_herramienta}}


class ActivarPanicoAction(BaseActionHandler):
    def execute(self, action_data: Dict[str, Any]) -> Dict[str, Any]:
        logger.critical(f"Executing ActivarPanicoAction with data: {action_data}")
        # Critical action: create high-priority ticket, notify emergency contacts, etc.
        # ticket_panico_payload = {
        #     "asunto": "!!! ALERTA DE PÁNICO !!!", "categoria": "Emergencia", "estado": "ALERTA_PANICO_ACTIVA",
        #     "descripcion": action_data.get("descripcion_situacion", "Activación de botón de pánico"),
        #     "ubicacion": action_data.get("ubicacion", "No especificada"),
        #     "coordenadas": action_data.get("coordenadas"),
        #     "user_id": self.context.get("cliente_id"), "anon_id": self.context.get("anon_id"),
        #     "municipio_id": getattr(self.context.get("user_obj"), "municipio_id", None)
        # }
        # ticket_panico = servicio_tickets.crear_nuevo_ticket(tipo_ticket="municipio", ticket_data=ticket_panico_payload)
        user_message = "🚨 ALERTA DE PÁNICO RECIBIDA. Hemos notificado a los servicios de emergencia con tu ubicación. Mantené la calma, la ayuda está en camino."
        if not action_data.get("coordenadas") and not action_data.get("ubicacion"): # Check if location info was provided by LLM
            user_message = "🚨 ALERTA DE PÁNICO RECIBIDA. No pudimos obtener tu ubicación precisa. Por favor, si es posible, indicala a los servicios de emergencia cuando te contacten. Mantené la calma."

        return {"success": True, "message_to_user": user_message, "data": {"alerta_status": "enviada_simulada"}}

class CorregirDatosReclamoAction(BaseActionHandler):
     def execute(self, action_data: Dict[str, Any]) -> Dict[str, Any]:
        logger.info(f"Executing CorregirDatosReclamoAction with data: {action_data}")
        campo_a_corregir = action_data.get("campo_a_corregir") # e.g., "ubicacion_reclamo", "descripcion_reclamo"
        nuevo_valor = action_data.get("nuevo_valor")
        # id_reclamo_contexto = action_data.get("id_reclamo_contexto") # If LLM can identify which claim is being corrected

        if not campo_a_corregir or nuevo_valor is None: # nuevo_valor can be an empty string if user wants to clear a field
            return {
                "success": False,
                "message_to_user": "No indicaste qué dato corregir o cuál es el nuevo valor.",
                "pedir_info": "detalle_correccion" # Ask user to clarify
            }

        # This action implies the correction should be applied to a claim currently being built/edited in the session.
        # The actual update to `self.context[CONTEXTO_MUNICIPIO]` would happen in the main `responder_municipio` flow
        # based on this action's result, or the orchestrator updates the shared context.
        # For now, this handler just confirms the intent to correct.

        return {
            "success": True,
            "message_to_user": f"Entendido. He tomado nota para corregir '{campo_a_corregir}' a '{nuevo_valor}'. ¿Deseas confirmar estos cambios y el reclamo, o necesitas ajustar algo más?",
            "data": {
                "correccion_solicitada": True,
                "campo_corregido_propuesto": campo_a_corregir,
                "nuevo_valor_propuesto": nuevo_valor
            },
            "pedir_info": "confirmacion_final_o_mas_cambios" # Signal to ask user how to proceed after correction
        }

class ProcesarAdjuntoReclamoAction(BaseActionHandler):
    def execute(self, action_data: Dict[str, Any]) -> Dict[str, Any]:
        logger.info(f"Executing ProcesarAdjuntoReclamoAction with data: {action_data}")
        # This action is triggered when the LLM identifies that an attachment (already uploaded and info available in context)
        # should be specifically processed for a municipio claim.

        # The actual file processing (Vision API, DocAI) should have been done by a common "ProcesarAdjuntoAction"
        # or an async task, and its results placed into `self.context` or `action_data`.
        # This handler's job is to interpret those results in the context of a municipio claim.

        archivo_info_llm = action_data.get("archivo_adjunto_info") # Expects dict from LLM: {'url', 'mime_type', 'analisis_id', 'datos_extraidos_del_analisis'}

        if not archivo_info_llm or not archivo_info_llm.get("url"): # Or 'analisis_id' if that's the primary key
            return {"success": False, "message_to_user": "No se especificó qué adjunto procesar para el reclamo."}

        # Example: Use extracted data from the attachment to pre-fill claim details
        datos_extraidos = archivo_info_llm.get("datos_extraidos_del_analisis", {})
        categoria_sugerida_adj = datos_extraidos.get("categoria_sugerida")
        descripcion_sugerida_adj = datos_extraidos.get("descripcion_sugerida")

        # This handler would update the claim-in-progress in the main context,
        # which is then used by CrearReclamoAction or the conversational flow.
        # For now, just acknowledge.

        user_message = f"He procesado la información del adjunto ({archivo_info_llm.get('url')}). "
        if categoria_sugerida_adj:
            user_message += f"Sugiere que la categoría podría ser '{categoria_sugerida_adj}'. "
        if descripcion_sugerida_adj:
            user_message += f"Y la descripción: '{descripcion_sugerida_adj[:50]}...'. "
        user_message += "¿Cómo deseas continuar con el reclamo?"

        return {
            "success": True,
            "message_to_user": user_message,
            "data": {
                "adjunto_procesado_para_reclamo": True,
                "datos_para_prellenar_reclamo": datos_extraidos # This data should be merged into the claim context by orchestrator
            }
        }
