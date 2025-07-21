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

class TestMunicipios(unittest.TestCase):

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

    def test_greeting_handler(self):
        handler = GreetingHandler(self.context)
        payload = {"pregunta": "hola"}
        response = handler.handle(payload)
        self.assertIsNotNone(response)
        self.assertIn("¡Hola! 👋 Soy tu asistente digital del Municipio.", response["message_body"])

    def test_reclamo_handler_inicio(self):
        handler = ReclamoHandler(self.context)
        self.context["intencion"] = "iniciar_reclamo"
        payload = {"pregunta": "quiero hacer un reclamo"}
        response = handler.handle(payload)
        self.assertIsNotNone(response)
        self.assertIn("Lamento que estés teniendo un problema.", response["message_body"])
        self.assertEqual(self.context["contexto_municipio_v2"]["estado_conversacion"], "ESPERANDO_CONFIRMACION_INICIAR_RECLAMO")

    @patch('services.municipios.llamar_gemini')
    def test_responder_municipio_imagen(self, mock_llamar_gemini):
        mock_llamar_gemini.return_value = {
            "respuesta_usuario": "He recibido tu imagen. ¿Podrías decirme la dirección del problema?",
            "accion_backend": "crear_reclamo",
            "datos_estructura": {
                "target": "municipio",
                "categoria": "Alumbrado",
                "descripcion": "Poste de luz roto",
            },
            "pedir_info": "direccion",
            "botones": [],
        }

        pregunta_original = {
            "pregunta": "",
            "es_foto": True,
            "foto_url": "http://example.com/imagen.jpg",
            "uploaded_file_info_whatsapp": {
                "url": "http://example.com/imagen.jpg",
                "mime_type": "image/jpeg",
                "source": "whatsapp",
            },
        }

        with patch('services.municipios.db.session.get', return_value=None), \
             patch('services.municipios.db.session.add', return_value=None), \
             patch('services.municipios.db.session.commit', return_value=None), \
             patch('services.municipios.flag_modified', return_value=None):

            response = responder_municipio(
                pregunta_original,
                self.owner_user,
                self.rubro_obj,
                self.viewer_user,
                self.chat_db_context,
                "test_anon_id",
            )
            self.assertIsNotNone(response)
            self.assertIn("He recibido tu imagen.", response["message_body"])

if __name__ == '__main__':
    unittest.main()
