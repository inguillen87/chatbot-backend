# services/actions/general_actions.py
import logging
from .base_action_handler import BaseActionHandler
from typing import Dict, Any

logger = logging.getLogger(__name__)

class NoActionHandler(BaseActionHandler):
    def execute(self, action_data: Dict[str, Any]) -> Dict[str, Any]:
        logger.info(f"Executing NoActionHandler with data: {action_data}")
        # This handler typically does nothing on the backend but acknowledges the LLM's decision.
        # The LLM should have already provided a suitable "respuesta_usuario".
        return {
            "success": True,
            "message_to_user": action_data.get("respuesta_usuario_original_llm", "Entendido."), # Fallback message
            "data": {"action_taken": "none"}
        }

class SmallTalkActionHandler(BaseActionHandler):
    def execute(self, action_data: Dict[str, Any]) -> Dict[str, Any]:
        logger.info(f"Executing SmallTalkActionHandler with data: {action_data}")
        # Similar to NoActionHandler, the primary response comes from the LLM.
        # This handler might log the small talk or perform other minor backend tasks if needed.
        return {
            "success": True,
            "message_to_user": action_data.get("respuesta_usuario_original_llm", "¡Entendido! ¿En qué más puedo ayudarte?"), # Fallback
            "data": {"action_taken": "small_talk_acknowledged"}
        }

class DerivarHumanoAction(BaseActionHandler):
    def execute(self, action_data: Dict[str, Any]) -> Dict[str, Any]:
        logger.info(f"Executing DerivarHumanoAction with data: {action_data}")

        reason = action_data.get("razon_derivacion", "El usuario solicitó hablar con un humano.")
        target_entity_type = self.context.get("target_entity_type", "general") # 'municipio' or 'pyme'
        user_identifier = self.context.get("cliente_id") or self.context.get("anon_id")

        # In a real system, this would trigger a notification to a human agent pool
        # specific to the target_entity_type.
        # e.g., create_live_chat_ticket(user_identifier, reason, target_entity_type, chat_history_summary)

        logger.info(f"Derivación a humano ({target_entity_type}) solicitada para {user_identifier}. Razón: {reason}.")

        return {
            "success": True,
            "message_to_user": "Entendido. He notificado a un agente para que te asista. Se pondrán en contacto contigo a la brevedad.",
            "data": {"status": "derivacion_iniciada", "target_type": target_entity_type}
        }

class ProcesarAdjuntoAction(BaseActionHandler):
    def execute(self, action_data: Dict[str, Any]) -> Dict[str, Any]:
        logger.info(f"Executing ProcesarAdjuntoAction with data: {action_data}")
        # This is a generic placeholder. Specific logic would be in municipio/pyme versions
        # or this would call a more detailed document processing service.

        file_url = action_data.get("file_url")
        if not file_url:
            return {"success": False, "message_to_user": "No se detectó ningún archivo adjunto."}

        return {
            "success": True,
            "message_to_user": f"Recibí el archivo {file_url} y lo estoy procesando. Te avisaré cuando termine.",
            "data": {"adjunto_recibido": True, "status_procesamiento": "iniciado"}
        }

class InformarUsuarioAction(BaseActionHandler):
    def execute(self, action_data: Dict[str, Any]) -> Dict[str, Any]:
        logger.info(f"Executing InformarUsuarioAction with data: {action_data}")
        message = action_data.get("mensaje_para_mostrar", "Información procesada.")

        return {
            "success": True,
            "message_to_user": message,
            "data": {}
        }

# More general handlers can be added here if they are truly common across municipio and pyme.
# Otherwise, they should go into their specific action files.
