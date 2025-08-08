import unittest
from unittest.mock import patch, MagicMock
from app import create_app, db
from config import TestConfig
from models import User
from services.pymes import responder_pyme

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

    @patch('services.pymes.llamar_gemini')
    @patch('services.pymes.google_search')
    def test_fallback_handler(self, mock_google_search, mock_llamar_gemini):
        mock_llamar_gemini.return_value = {"accion_backend": "fallback", "datos_estructura": {"pregunta": "unhandled query"}}
        mock_google_search.return_value = [
            {"title": "Test Search Result", "link": "http://example.com/search", "snippet": "This is a test search result."}
        ]

        owner_user = MagicMock()
        owner_user.id = 1
        owner_user.rubro.nombre = "general"

        response = responder_pyme("unhandled query", owner_user, None, chat_db_context=MagicMock())

        self.assertIn("encontré esto en la web", response["message_body"])
        self.assertIn("Test Search Result", response["message_body"])
        self.assertEqual(response["fuente"], "pyme_fallback_google_search")
        mock_google_search.assert_called_with("unhandled query")

if __name__ == '__main__':
    unittest.main()
