# services/actions/common_actions.py
import logging
from typing import Dict, Any
from .base_action_handler import BaseActionHandler
from services.ticket_service import servicio_tickets
from services.ticket_utils import formatear_ticket_respuesta

logger = logging.getLogger(__name__)

class DerivarHumanoAction(BaseActionHandler):
    def execute(self, action_data: Dict[str, Any]) -> Dict[str, Any]:
        """Crea un ticket real de chat en vivo y devuelve su identificador."""
        logger.info(f"Executing DerivarHumanoAction with data: {action_data}")

        try:
            viewer_user = self.context.get("viewer_user_obj")
            owner_user = self.context.get("user_obj")
            pregunta_original = self.context.get("pregunta_actual_usuario", "")
            target_entity_type = self.context.get("target_entity_type", "general")

            nombre = (getattr(viewer_user, "name", None) or action_data.get("nombre"))
            telefono = (getattr(viewer_user, "telefono", None) or action_data.get("telefono"))
            email = (getattr(viewer_user, "email", None) or action_data.get("email"))

            ticket_data = {
                "asunto": f"Solicitud de Chat en Vivo por: {nombre or 'Usuario'}",
                "categoria": "Atención en Vivo",
                "pregunta": pregunta_original,
                "detalles": action_data.get("motivo_derivacion", "Solicitud de agente"),
                "user_id": self.context.get("cliente_id"),
                "anon_id": self.context.get("anon_id") if not self.context.get("cliente_id") else None,
                "estado": "esperando_agente_en_vivo",
                "nombre_vecino": nombre, # Usado por municipio
                "telefono_vecino": telefono, # Usado por municipio
                "email_vecino": email, # Usado por municipio
                "nombre_cliente": nombre, # Usado por pyme
                "telefono_cliente": telefono, # Usado por pyme
                "email_cliente": email, # Usado por pyme
            }

            if target_entity_type == "municipio":
                ticket_data["municipio_id"] = getattr(owner_user, "municipio_id", None)
            else: # pyme
                ticket_data["pyme_id"] = getattr(owner_user, "id", None)


            ticket_data_cleaned = {k: v for k, v in ticket_data.items() if v is not None}
            ticket_data_cleaned['tipo_ticket'] = target_entity_type

            sala = servicio_tickets.crear_nuevo_ticket(tipo_ticket=target_entity_type, ticket_data=ticket_data_cleaned)
            if not sala:
                raise Exception("crear_nuevo_ticket devolvió None")

            servicio_tickets.crear_comentario(
                ticket_id=sala.id,
                tipo_ticket=target_entity_type,
                comentario_data={
                    "comentario": pregunta_original,
                    "user_id": self.context.get("cliente_id"),
                    "anon_id": self.context.get("anon_id"),
                    "es_admin": False,
                },
            )

            # Formatear el prefijo del ID de chat según el tipo de entidad
            chat_id_prefix = "M" if target_entity_type == "municipio" else "P"
            chat_id = f"{chat_id_prefix}-{sala.nro_ticket}"

            user_message = formatear_ticket_respuesta("chat", nombre, pregunta_original, "Atención en Vivo", chat_id)
            return {
                "success": True,
                "message_to_user": user_message,
                "data": {"ticket_id": sala.id, "chat_id": chat_id, "status": "esperando_agente_en_vivo"},
            }
        except Exception as e:
            logger.error(f"Error en DerivarHumanoAction: {e}", exc_info=True)
            return {
                "success": False,
                "message_to_user": "Ocurrió un problema al crear el chat en vivo. ¿Podés intentar de nuevo más tarde?",
                "error_details": str(e),
            }

from services.document_processing_service import DocumentProcessingService

class ProcesarAdjuntoAction(BaseActionHandler):
    def execute(self, action_data: Dict[str, Any]) -> Dict[str, Any]:
        logger.info(f"Executing ProcesarAdjuntoAction with data: {action_data}")

        archivo_id = self.context.get("archivo_id_para_asociar")
        if not archivo_id:
            return {"success": False, "message_to_user": "No se encontró un archivo para procesar."}

        try:
            processing_service = DocumentProcessingService()
            analysis_result = processing_service.process_document_by_id(archivo_id)

            if not analysis_result or not analysis_result.get("success"):
                error_detail = analysis_result.get("error", "Error desconocido en el procesamiento del documento.")
                logger.error(f"Fallo el procesamiento del documento para el archivo ID {archivo_id}: {error_detail}")
                return {"success": False, "message_to_user": f"No se pudo procesar el archivo. Detalle: {error_detail}"}

            extracted_data = analysis_result.get("extracted_data", {})
            user_message = "He procesado el archivo. "
            if extracted_data.get("es_catalogo"):
                user_message += f"Detecté que es un catálogo con {extracted_data.get('numero_productos', 0)} productos."
            elif extracted_data.get("es_reclamo_con_imagen"):
                user_message += f"Gracias por la imagen. La he asociado a tu reclamo sobre: {extracted_data.get('categoria_sugerida', 'asunto no identificado')}."

            return {
                "success": True,
                "message_to_user": user_message,
                "data": {
                    "analysis_status": "completed",
                    "extracted_data": extracted_data
                }
            }

        except Exception as e:
            logger.error(f"Error en ProcesarAdjuntoAction: {e}", exc_info=True)
            return {"success": False, "message_to_user": "Ocurrió un error técnico al procesar el archivo."}

# Example of another common action
class InformarUsuarioAction(BaseActionHandler):
    def execute(self, action_data: Dict[str, Any]) -> Dict[str, Any]:
        """
        A simple action that just passes a message through, possibly after some formatting or logging.
        The LLM would use this if it wants to convey information without a specific backend DB change.
        """
        logger.info(f"Executing InformarUsuarioAction with data: {action_data}")

        message = action_data.get("mensaje_para_mostrar")
        if not message:
            return {"success": False, "message_to_user": "No hay mensaje para informar."} # Should not happen

        return {
            "success": True,
            "message_to_user": message, # This message comes directly from LLM's "respuesta_usuario" for this action
            "data": {}
        }

class DescargarArchivoActionHandler(BaseActionHandler):
    def execute(self, action_data: Dict[str, Any]) -> Dict[str, Any]:
        logger.info(f"Executing DescargarArchivoActionHandler with data: {action_data}")
        nombre_archivo = action_data.get("nombre_archivo")
        if not nombre_archivo:
            return {"success": False, "message_to_user": "No se especificó qué archivo descargar."}

        # In a real implementation, you would generate a secure, temporary download link.
        # For now, we'll construct a direct link to the /archivos/ endpoint.
        # This assumes the 'archivos_bp' blueprint is registered at '/archivos'.
        from flask import url_for
        try:
            # This requires an app context to work.
            download_url = url_for('archivos.download_file', filename=nombre_archivo, _external=True)
            user_message = f"Puedes descargar el archivo '{nombre_archivo}' desde el siguiente enlace: {download_url}"
            return {"success": True, "message_to_user": user_message, "data": {"download_url": download_url}}
        except RuntimeError:
            # Fallback for when url_for is not available (e.g., outside of a request context)
            logger.warning("Could not generate download URL using url_for due to no app context.")
            # Provide a relative path as a fallback
            download_url = f"/archivos/{nombre_archivo}"
            user_message = f"Puedes descargar el archivo '{nombre_archivo}' desde el siguiente enlace: {download_url}"
            return {"success": True, "message_to_user": user_message, "data": {"download_url": download_url}}
