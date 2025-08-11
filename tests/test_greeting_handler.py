import unittest
from app import create_app, db
from services.municipio_responder import GreetingHandler, CONTEXTO_MUNICIPIO
from models import User
from config import TestConfig

class TestGreetingHandler(unittest.TestCase):
    def setUp(self):
        self.app = create_app(TestConfig)
        self.app_context = self.app.app_context()
        self.app_context.push()
        db.create_all()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.app_context.pop()

    def test_greeting_with_profile_name(self):
        """
        Tests that the greeting message is personalized when profile_name is available.
        """
        context = {
            "profile_name": "Marcelo",
            "viewer_user_obj": None,
            "chat_db_context_data": {
                CONTEXTO_MUNICIPIO: {}
            }
        }
        handler = GreetingHandler(context)
        result = handler.handle({})
        self.assertIn("¡Hola, Marcelo!", result["message_body"])

    def test_greeting_with_user_name(self):
        """
        Tests that the greeting message is personalized when a user object with a name is available.
        """
        user = User(name="Test User", email="test@example.com", password_hash="hash")
        context = {
            "profile_name": None,
            "viewer_user_obj": user,
            "chat_db_context_data": {
                CONTEXTO_MUNICIPIO: {}
            }
        }
        handler = GreetingHandler(context)
        result = handler.handle({})
        self.assertIn("¡Hola, Test User!", result["message_body"])

    def test_greeting_without_name(self):
        """
        Tests that a generic greeting is returned when no name is available.
        """
        context = {
            "profile_name": None,
            "viewer_user_obj": None,
            "chat_db_context_data": {
                CONTEXTO_MUNICIPIO: {}
            }
        }
        handler = GreetingHandler(context)
        result = handler.handle({})
        self.assertNotIn("¡Hola, ", result["message_body"])
        self.assertIn("¡Hola! 👋", result["message_body"])

if __name__ == '__main__':
    unittest.main()
