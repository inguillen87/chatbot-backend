import unittest
from unittest.mock import patch, MagicMock
import os
import sys
import json

# Add project root to system path
project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from app import create_app, db
from config import Config
from services.municipios import ReclamoHandler, ConversationState, es_pregunta_nueva

class TestConfig(Config):
    TESTING = True
    SQLALCHEMY_DATABASE_URI = 'sqlite:///:memory:'
    WTF_CSRF_ENABLED = False

class TestClaimFlow(unittest.TestCase):

    def setUp(self):
        self.app = create_app(TestConfig)
        self.app_context = self.app.app_context()
        self.app_context.push()
        db.create_all()
        self.client = self.app.test_client()

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

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.app_context.pop()

    def test_es_pregunta_nueva_with_address(self):
        """
        Tests that a valid address is not considered a new question.
        """
        # Simulate the state where the bot is waiting for an address
        memoria = {"estado_conversacion": "ESPERANDO_DIRECCION_RECLAMO"}
        self.assertFalse(es_pregunta_nueva("Don Bosco 55, Junín, Mendoza", "una dirección", memoria))

    @patch('services.herramientas_municipio.get_cohere_response')
    def test_reclamo_handler_address_input(self, mock_get_cohere_response):
        """
        Simulates the user providing an address after being prompted.
        """
        mock_get_cohere_response.return_value = json.dumps({
            "calle": "Don Bosco",
            "numero": "55",
            "localidad": "Junín",
            "provincia": "Mendoza"
        })
        handler = ReclamoHandler(self.context)
        self.context["contexto_municipio_v2"]["estado_conversacion"] = "ESPERANDO_DIRECCION_RECLAMO"
        self.context["contexto_municipio_v2"]["categoria_reclamo"] = "Bacheo"

        payload = {"pregunta": "Don Bosco 55, Junín, Mendoza"}
        with self.app.app_context():
            response = handler.handle(payload)

        # The handler should now be waiting for the user's name
        self.assertEqual(self.context["contexto_municipio_v2"]["estado_conversacion"], "ESPERANDO_NOMBRE_VECINO")
        self.assertIn("nombre completo", response["message_body"])
        self.assertEqual(self.context["contexto_municipio_v2"]["direccion_reclamo"], "Don Bosco 55, Junín")

if __name__ == '__main__':
    unittest.main()
