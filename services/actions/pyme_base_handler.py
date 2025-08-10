import logging
from .base_action_handler import BaseActionHandler
from services import cart as cart_service
from services.pymes import CONTEXTO_PYME

logger = logging.getLogger(__name__)

class BasePymeHandler(BaseActionHandler):
    def __init__(self, context):
        super().__init__(context)
        self.pyme_ctx = self.context.get(CONTEXTO_PYME, {})
        self.pyme_id_actual = self.context.get("user_id")
        self.cliente_id_actual = self.context.get("cliente_id")
        self.chat_session_uuid_actual = self.context.get("chat_session_uuid")
        chat_db_context_data = self.context.get("chat_db_context_data", {})
        self.pyme_carts_data = chat_db_context_data.get(cart_service.SESSION_CARTS_KEY, {})

    def _guardar_contexto_pyme(self):
        if self.context.get("chat_db_context_data"):
            self.context["chat_db_context_data"][CONTEXTO_PYME] = self.pyme_ctx
            self.context["chat_db_context_data"][cart_service.SESSION_CARTS_KEY] = self.pyme_carts_data
        else:
            logger.error("[BasePymeHandler._guardar_contexto_pyme] chat_db_context_data no encontrado en self.context.")

    def execute(self, action_data):
        raise NotImplementedError
