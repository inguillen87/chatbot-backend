import unittest
from unittest.mock import patch, MagicMock
from app import create_app, db
from config import TestConfig
from models import User
from services.pymes import responder_pyme, CONTEXTO_PYME

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

    @patch('services.google_search.google_search')
    @patch('services.pymes.llamar_gemini')
    def test_fallback_handler(self, mock_llamar_gemini, mock_google_search):
        mock_llamar_gemini.return_value = {"accion_backend": "fallback", "datos_estructura": {"pregunta": "unhandled query"}}
        mock_google_search.return_value = [
            {"title": "Test Search Result", "link": "http://example.com/search", "snippet": "This is a test search result."}
        ]

        owner_user = MagicMock()
        owner_user.id = 1
        owner_user.rubro.nombre = "general"

        chat_db_context = MagicMock()
        chat_db_context.context_data = {CONTEXTO_PYME: {}}

        response = responder_pyme(pregunta_original="unhandled query", owner_user=owner_user, rubro_obj=owner_user.rubro, viewer_user=None, chat_db_context=chat_db_context)

        self.assertIn("No estoy seguro de cómo proceder", response["message_body"])
        mock_google_search.assert_not_called()

if __name__ == '__main__':
    unittest.main()
