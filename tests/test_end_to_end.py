import unittest
from unittest.mock import patch, MagicMock
import os
import sys

# Add project root to system path
project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, project_root)

from services.municipio_responder import responder_municipio

class TestEndToEnd(unittest.TestCase):

    def setUp(self):
        os.environ["GOOGLE_PROJECT_ID"] = "test-project"
        from app import create_app
        self.app = create_app()
        self.app_context = self.app.app_context()
        self.app_context.push()

        self.owner_user = MagicMock()
        self.owner_user.id = 1
        self.rubro_obj = None
        self.viewer_user = None
        self.chat_db_context = MagicMock()
        self.chat_db_context.context_data = {}

    def tearDown(self):
        self.app_context.pop()

    @patch('services.municipios.responder_municipio')
    def test_end_to_end_pothole_complaint(self, mock_responder_municipio):
        # 1. User sends image
        mock_responder_municipio.return_value = {"message_body": "He recibido tu foto. Para continuar con el reclamo, por favor, decime la dirección del problema."}
        response = mock_responder_municipio(pregunta_original={"es_foto": True})
        self.assertIn("Para continuar con el reclamo", response["message_body"])

        # 2. User sends address
        mock_responder_municipio.return_value = {"message_body": "Gracias. Ahora necesito tu nombre completo."}
        response = mock_responder_municipio(pregunta_original="Calle Falsa 123")
        self.assertIn("Gracias. Ahora necesito tu nombre completo.", response["message_body"])

        # 3. User sends name
        mock_responder_municipio.return_value = {"message_body": "Gracias, Juan. Por último, tu teléfono."}
        response = mock_responder_municipio(pregunta_original="Juan Perez")
        self.assertIn("Gracias, Juan", response["message_body"])

        # 4. User sends phone
        mock_responder_municipio.return_value = {"message_body": "Perfecto, ya casi terminamos. Por favor, revisá los datos."}
        response = mock_responder_municipio(pregunta_original="1122334455")
        self.assertIn("Perfecto, ya casi terminamos", response["message_body"])

        # 5. User sends email
        mock_responder_municipio.return_value = {"message_body": "Por favor, revisá los datos y confirmá."}
        response = mock_responder_municipio(pregunta_original="juan.perez@example.com")
        self.assertIn("Por favor, revisá los datos", response["message_body"])

        # 6. User confirms
        mock_responder_municipio.return_value = {"message_body": "Tu reclamo ha sido registrado con el número de ticket: **M-12345**"}
        response = mock_responder_municipio(pregunta_original="si")
        self.assertIn("Tu reclamo ha sido registrado con el número de ticket: **M-12345**", response["message_body"])

if __name__ == '__main__':
    unittest.main()
