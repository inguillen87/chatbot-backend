import unittest
from unittest.mock import patch, MagicMock
from app import create_app, db
from config import TestConfig
from models import User
from services.pymes import responder_pyme, CONTEXTO_PYME, SaludoHandler

class PymesTestCase(unittest.TestCase):
    def setUp(self):
        self.app = create_app(TestConfig)
        self.app_context = self.app.app_context()
        self.app_context.push()
        db.create_all()
        self.client = self.app.test_client()

        # Create a test user
        self.user = User(
            name="Test User",
            email="test@example.com",
            pyme_id=1
        )
        self.user.set_password("password")
        db.session.add(self.user)
        db.session.commit()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.app_context.pop()

    @patch('services.pymes.google_search')
    def test_fallback_handler(self, mock_google_search):
        mock_google_search.return_value = [{'link': 'http://example.com/search', 'title': 'Test Search Result', 'snippet': 'This is a test search result.'}]

        with patch('services.pymes.llamar_llm_con_fallback') as mock_llamar_fallback:
            mock_llamar_fallback.return_value = ({"accion_backend": "fallback", "datos_estructura": {"pregunta": "unhandled query"}}, None)

            owner_user = MagicMock()
            owner_user.id = 1
            owner_user.rubro.nombre = "general"
            owner_user.rubro.slug = "general"

            chat_db_context = MagicMock()
            chat_db_context.context_data = {CONTEXTO_PYME: {}}

            response = responder_pyme(pregunta_original="unhandled query", owner_user=owner_user, rubro_obj=owner_user.rubro, viewer_user=None, chat_db_context=chat_db_context)

            mock_google_search.assert_called_once_with("unhandled query")
            self.assertEqual(mock_llamar_fallback.call_args.kwargs.get("task_type"), "whatsapp_realtime")
            self.assertIn("message_body", response)
            self.assertIn("encontré esto en la web", response["message_body"])
            self.assertIn("Test Search Result", response["message_body"])

    def test_saludo_handler_adds_whatsapp_template_pre_message(self):
        chat_db_context_data = {CONTEXTO_PYME: {}}
        context = {
            CONTEXTO_PYME: {"nombre_cliente": "Marcelo"},
            "user_id": self.user.id,
            "tenant_id": 77,
            "channel": "whatsapp",
            "rubro_nombre": "bodega",
            "chat_db_context_data": chat_db_context_data,
        }
        welcome_config = {
            "nombre_pyme": "Bodega Test",
            "whatsapp": {"numero": "+5492613168608"},
            "welcome": {
                "template_sid": "saludo_inicial_cfincas",
                "template_variables": {"1": "{{user_name}}", "2": "{{pyme_whatsapp}}"},
            },
        }
        menu_payload = {
            "message_body": "Menu de la tienda",
            "options_list": [{"texto": "Catalogo", "action_id": "pyme_productos_stock"}],
        }

        with patch("services.pymes.cargar_configuracion_pyme", return_value=welcome_config), patch(
            "services.pymes.get_pyme_menu_payload",
            return_value=menu_payload,
        ):
            response = SaludoHandler(context).execute({})

        pre_messages = response.get("_twilio_pre_messages") or []
        self.assertEqual(len(pre_messages), 1)
        template = pre_messages[0]
        self.assertEqual(template.get("template_name"), "saludo_inicial_cfincas")
        self.assertEqual(template.get("tenant_id"), 77)
        self.assertEqual(template.get("content_variables"), {"1": "Marcelo", "2": "+5492613168608"})
        self.assertIn("Respond", response.get("message_body", ""))
        self.assertEqual(response.get("message_type"), "text")

if __name__ == '__main__':
    unittest.main()
