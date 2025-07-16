# services/actions/general_actions.py
import logging
from .base_action_handler import BaseActionHandler
from typing import Dict, Any
from models import User, db
from sqlalchemy import func
from services.logic import es_rubro_publico
from services.common_utils import validar_email
import uuid
from datetime import datetime

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


class RegistrarUsuarioActionHandler(BaseActionHandler):
    def execute(self, action_data: Dict[str, Any]) -> Dict[str, Any]:
        logger.info(f"Executing RegistrarUsuarioActionHandler with data: {action_data}")

        name = action_data.get("name")
        email = action_data.get("email")
        password = action_data.get("password")
        empresa_token = action_data.get("empresa_token")

        if not all([name, email, password, empresa_token]):
            return {
                "success": False,
                "message_to_user": "Faltan datos para registrarte (nombre, email, contraseña o token).",
                "pedir_info": "datos_registro"
            }

        if not validar_email(email):
            return {
                "success": False,
                "message_to_user": "El email proporcionado no parece válido.",
                "pedir_info": "email"
            }

        owner_user = User.query.filter_by(token=empresa_token.strip()).first()
        if not owner_user:
            return {
                "success": False,
                "message_to_user": "Token de entidad inválido o no encontrado.",
            }

        existing = User.query.filter(func.lower(User.email) == func.lower(email.strip())).first()
        if existing:
            return {
                "success": False,
                "message_to_user": "El email ya está registrado.",
                "data": {"email_registrado": True}
            }

        nuevo = User(
            name=name.strip(),
            email=email.strip().lower(),
            token=str(uuid.uuid4()),
            rubro_id=owner_user.rubro_id,
            empresa_id=owner_user.id,
            plan="gratis",
            rol="usuario",
            tipo_chat=getattr(owner_user, "tipo_chat", None) or ("municipio" if es_rubro_publico(owner_user.rubro) else "pyme"),
            acepto_terminos=True,
            fecha_aceptacion_terminos=datetime.utcnow(),
        )
        nuevo.set_password(password)

        try:
            db.session.add(nuevo)
            db.session.commit()
            return {
                "success": True,
                "message_to_user": f"Usuario '{nuevo.email}' registrado exitosamente.",
                "data": {"user_id": nuevo.id, "token": nuevo.token}
            }
        except Exception as e:
            db.session.rollback()
            logger.error(f"Error en RegistrarUsuarioActionHandler: {e}", exc_info=True)
            return {
                "success": False,
                "message_to_user": "Error interno al registrar el usuario.",
            }

# More general handlers can be added here if they are truly common across municipio and pyme.
# Otherwise, they should go into their specific action files.
