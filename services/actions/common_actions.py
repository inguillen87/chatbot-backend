# services/actions/common_actions.py
import logging
from typing import Dict, Any
from .base_action_handler import BaseActionHandler
# Import necessary services like document_processor, notification_service, etc.
# from services.document_processor import process_document_for_claim, process_document_for_order
# from services.notification_service import send_notification_to_human_agent

logger = logging.getLogger(__name__)

class DerivarHumanoAction(BaseActionHandler):
    def execute(self, action_data: Dict[str, Any]) -> Dict[str, Any]:
        logger.info(f"Executing DerivarHumanoAction with data: {action_data}")

        reason = action_data.get("razon_derivacion", "El usuario solicitó hablar con un humano.")
        user_identifier = self.context.get("cliente_id") or self.context.get("anon_id")
        chat_history_summary = "Últimos mensajes: " + str(self.context.get("mensajes_previos", [])[-3:]) # Example summary

        # In a real system, this would trigger a notification to a human agent pool
        # e.g., via a message queue, email, or a live chat system API.
        # send_notification_to_human_agent(user_identifier, reason, chat_history_summary)

        logger.info(f"Derivación a humano solicitada para {user_identifier}. Razón: {reason}. Historial: {chat_history_summary}")

        return {
            "success": True,
            "message_to_user": "Entendido. He notificado a un agente humano para que te asista. Se pondrán en contacto contigo a la brevedad.",
            "data": {"status": "derivacion_iniciada"}
        }

class ProcesarAdjuntoAction(BaseActionHandler):
    """
    Action to initiate processing of an uploaded attachment (image, document).
    This action itself might trigger an async task and update the context
    for the orchestrator to check for results later.
    """
    def execute(self, action_data: Dict[str, Any]) -> Dict[str, Any]:
        logger.info(f"Executing ProcesarAdjuntoAction with data: {action_data}")

        file_url = action_data.get("file_url")
        mime_type = action_data.get("mime_type")
        context_tipo = action_data.get("contexto_procesamiento") # e.g., "reclamo_municipal", "pedido_pyme_lista"
        archivo_id_db = self.context.get("archivo_id_para_asociar") # ID if file already saved in ArchivoAdjunto

        if not (file_url or archivo_id_db) or not mime_type:
            return {
                "success": False,
                "message_to_user": "No se proporcionó suficiente información del archivo para procesar.",
                "error_details": "Missing file_url/archivo_id_db or mime_type for ProcesarAdjuntoAction."
            }

        # Import the document_processor service (assuming it's created)
        try:
            from services.document_processor import DocumentProcessorService # Ensure this service is created
            doc_processor = DocumentProcessorService() # Or get instance if it's a singleton

            # This call might be asynchronous in a real system
            # For now, let's assume it's synchronous for simplicity of this action handler,
            # or that document_processor itself handles async and returns an immediate status.

            processing_result = {}
            if archivo_id_db: # If we have a DB record, pass its ID
                logger.info(f"Procesando adjunto (ID DB: {archivo_id_db}) con mime_type: {mime_type}, contexto: {context_tipo}")
                # The document_processor should be able to fetch the file from DB using its ID
                # or expect a URL even if an ID is provided.
                # Let's assume it can use archivo_id_db to get the file.
                # This might involve calling a method like:
                # processing_result = doc_processor.process_existing_attachment(archivo_id_db, context_tipo)
                # For now, placeholder:
                processing_result = {"status": "pending_analysis_for_db_id", "analisis_id": None, "message": "Análisis iniciado para archivo existente."}
                # Actual implementation would call Vision/DocAI and store results in AnalisisArchivo

            elif file_url: # If it's a direct URL (e.g. from WhatsApp not yet in DB)
                logger.info(f"Procesando adjunto desde URL: {file_url}, mime_type: {mime_type}, contexto: {context_tipo}")
                # processing_result = doc_processor.process_new_attachment_from_url(file_url, mime_type, context_tipo,
                #                                                                  user_id=self.context.get("cliente_id"),
                #                                                                  pyme_id=self.context.get("user_id"))
                # For now, placeholder:
                processing_result = {"status": "pending_analysis_for_url", "analisis_id": None, "message": "Análisis iniciado para archivo desde URL."}
                # Actual implementation would save to ArchivoAdjunto, then call Vision/DocAI, then save AnalisisArchivo.

            if processing_result.get("status") == "pending_analysis_for_db_id" or \
               processing_result.get("status") == "pending_analysis_for_url":

                # The orchestrator should now know to periodically check the status of this analysis_id
                # or wait for a webhook/callback if the processing is truly async.
                # For a synchronous LLM flow, the LLM might be re-invoked with "analysis_pending"
                # and the user asked to wait or describe the issue while it processes.

                # This action handler's response should inform the orchestrator that processing has started.
                # The LLM (via orchestrator) will then decide the next conversational step.
                return {
                    "success": True,
                    "message_to_user": "He comenzado a procesar el archivo que enviaste. Te avisaré cuando esté listo.", # LLM might override this
                    "data": {
                        "analysis_status": processing_result.get("status"),
                        "analisis_id": processing_result.get("analisis_id"), # ID of the AnalisisArchivo record if created
                        "next_action_suggestion": "poll_analysis_status" # Hint for orchestrator or LLM
                    }
                }
            elif processing_result.get("status") == "completed":
                 return {
                    "success": True,
                    "message_to_user": "El archivo ha sido procesado.", # LLM will use extracted data
                    "data": {
                        "analysis_status": "completed",
                        "analisis_id": processing_result.get("analisis_id"),
                        "extracted_data": processing_result.get("extracted_data") # Pass to LLM
                    }
                }
            else: # Error or other status
                logger.error(f"Procesamiento de adjunto falló o estado desconocido: {processing_result}")
                return {
                    "success": False,
                    "message_to_user": "Hubo un problema al procesar tu archivo. Por favor, intenta de nuevo o descríbelo manualmente.",
                    "error_details": processing_result.get("error", "Error desconocido durante procesamiento de adjunto")
                }

        except ImportError:
            logger.error("Servicio 'document_processor' no encontrado. El procesamiento de adjuntos no está disponible.")
            return {"success": False, "message_to_user": "El sistema no está configurado para procesar archivos en este momento."}
        except Exception as e:
            logger.error(f"Error en ProcesarAdjuntoAction: {e}", exc_info=True)
            return {
                "success": False,
                "message_to_user": "Ocurrió un error técnico al intentar procesar tu archivo.",
                "error_details": str(e)
            }

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
