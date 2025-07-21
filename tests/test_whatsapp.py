import unittest
from unittest.mock import patch, MagicMock
import os
import sys

# Add project root to system path
project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, project_root)

from services.municipios import (
    GreetingHandler,
    ReclamoHandler,
    TicketStatusHandler,
    ConversationState,
    responder_municipio,
)

class TestWhatsApp(unittest.TestCase):

    def setUp(self):
        from app import create_app
        self.app = create_app()
        self.app_context = self.app.app_context()
        self.app_context.push()

        self.context = {
            "contexto_municipio_v2": {},
            "user_obj": MagicMock(),
            "viewer_user_obj": None,
            "cliente_id": None,
            "anon_id": "test_anon_id",
            "rubro_obj": None,
            "channel": "whatsapp",
            "municipio_config_actual": {},
            "chat_session_uuid": "test_session_uuid",
            "chat_db_context_data": {},
        }
        self.owner_user = MagicMock()
        self.owner_user.id = 1
        self.rubro_obj = None
        self.viewer_user = None
        self.chat_db_context = MagicMock()
        self.chat_db_context.context_data = {}

    def tearDown(self):
        self.app_context.pop()

    def test_reclamo_handler_categoria_buttons(self):
        handler = ReclamoHandler(self.context)
        self.context["intencion"] = "iniciar_reclamo"
        self.context["contexto_municipio_v2"]["estado_conversacion"] = "ESPERANDO_CATEGORIA_RECLAMO"
        payload = {"pregunta": ""}
        response = handler.handle(payload)
        self.assertIsNotNone(response)
        self.assertEqual(response["message_type"], "interactive_list")
        self.assertGreater(len(response["options_list"]), 0)

    def test_reclamo_handler_share_location_button(self):
        handler = ReclamoHandler(self.context)
        self.context["intencion"] = "iniciar_reclamo"
        self.context["contexto_municipio_v2"]["estado_conversacion"] = "ESPERANDO_DIRECCION_RECLAMO"
        payload = {"pregunta": ""}
        response = handler.handle(payload)
        self.assertIsNotNone(response)
        self.assertEqual(response["message_type"], "text")
        self.assertNotIn("📍 Enviar mi ubicación actual", response.get("options_list", []))

    def test_ticket_status_handler_ticket_number_shortcut(self):
        handler = TicketStatusHandler(self.context)
        self.context["intencion"] = "consultar_estado_ticket"
        payload = {"pregunta": "quiero saber el estado de mi ticket 12345"}
        with patch('services.municipios.MunicipioTicket.query') as mock_query:
            mock_ticket = MagicMock()
            mock_ticket.id = 1
            mock_ticket.nro_ticket = 12345
            mock_ticket.asunto = "Test"
            mock_ticket.estado = "en_proceso"
            mock_query.filter_by.return_value.first.return_value = mock_ticket
            with patch('services.municipios.TicketComentario.query') as mock_comment_query:
                mock_comment_query.filter_by.return_value.order_by.return_value.first.return_value = None
                response = handler.handle(payload)
                self.assertIsNotNone(response)
                mock_query.filter_by.assert_called_with(nro_ticket=12345)
                self.assertIn("El ticket **M-12345** sobre 'Test' se encuentra en estado: **En Proceso**.", response["message_body"])

if __name__ == '__main__':
    unittest.main()
