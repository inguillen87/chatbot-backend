import unittest
from unittest.mock import patch, MagicMock, AsyncMock

from app import create_app, db
from services.pymes import responder_pyme_v4
from config import Config

class TestConfig(Config):
    TESTING = True
    SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"
    WTF_CSRF_ENABLED = False

class PymeSmokeTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app(TestConfig)
        self.app_context = self.app.app_context()
        self.app_context.push()
        db.create_all()
        self.loop = self.app.loop

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.app_context.pop()

    @patch("services.pymes.ChatOrchestrator.execute_action", new_callable=AsyncMock)
    async def run_keyword_test(self, keyword, expected_action, mock_response, mock_execute_action):
        """Helper function to run a keyword-based test."""
        mock_execute_action.return_value = mock_response

        response = await responder_pyme_v4(
            pyme_id=1,
            message=keyword,
            normalized_phone="+1234567890",
            profile_name="Test User",
            chat_context_data={},
        )

        mock_execute_action.assert_called_with(expected_action, {'target': 'pyme'}, unittest.mock.ANY)
        self.assertIn(mock_response["message_body"], response["message_body"])
        self.assertEqual(mock_response["fuente"], response["fuente"])

    def test_pyme_menu_keyword(self):
        mock_response = {
            "message_body": "¡Hola! Soy tu asistente virtual...",
            "fuente": "pyme_menu_principal_handler"
        }
        with self.app.test_request_context():
            self.loop.run_until_complete(
                self.run_keyword_test("menu", "pyme_menu_principal", mock_response)
            )

    def test_pyme_catalog_keyword(self):
        mock_response = {
            "message_body": "Aquí está nuestro catálogo de productos.",
            "fuente": "pyme_catalogo_handler"
        }
        with self.app.test_request_context():
            self.loop.run_until_complete(
                self.run_keyword_test("catalogo", "pyme_productos_stock", mock_response)
            )

    def test_pyme_ver_carrito_keyword(self):
        mock_response = {
            "message_body": "Este es el contenido de tu carrito.",
            "fuente": "pyme_ver_carrito_handler"
        }
        with self.app.test_request_context():
            self.loop.run_until_complete(
                self.run_keyword_test("carrito", "pyme_ver_carrito", mock_response)
            )

if __name__ == "__main__":
    unittest.main()