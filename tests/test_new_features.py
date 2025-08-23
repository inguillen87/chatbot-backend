import unittest
from unittest.mock import patch, MagicMock
import os
import sys

# Asegúrate de que el directorio raíz del proyecto esté en el sys.path
project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from services.ticket_utils import formatear_ticket_respuesta
from services.municipio_responder import GreetingHandler
from services.municipio_responder import responder_municipio
from config import TestConfig
from app import create_app
from models import db

class TestNewFeatures(unittest.TestCase):

    def setUp(self):
        """Configura un entorno de prueba básico."""
        self.app = create_app(TestConfig)
        self.app_context = self.app.app_context()
        self.app_context.push()
        db.create_all()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.app_context.pop()

    def test_formatear_ticket_respuesta_con_contacto_especializado(self):
        """
        Verifica que la respuesta del ticket incluya la información del contacto especializado.
        """
        contacto = {
            "nombre": "Juan Obras",
            "titulo": "Jefe de Bacheo",
            "telefono": "+5491122334455"
        }
        respuesta = formatear_ticket_respuesta(
            "reclamo", "Marcelo", "Bache en la calle", "Bacheo", "M-12345", contacto
        )
        message_body, buttons = respuesta
        self.assertIn("Juan Obras", message_body)
        self.assertEqual(len(buttons), 0)

    def test_greeting_handler_final_menu(self):
        """
        Verifica que el GreetingHandler devuelve el menú principal final (v5).
        """
        handler = GreetingHandler(context={'profile_name': 'Tester'})
        respuesta = handler.handle(payload={})
        self.assertIn("¡Hola, Tester!", respuesta["message_body"])
        self.assertIn("Soy JUNI", respuesta["message_body"])
        self.assertIn("options_list", respuesta)
        # 3 (Reclamos) + 3 (Trámites) + 3 (Info) + 1 (Estacionamiento) = 10
        self.assertEqual(len(respuesta["options_list"]), 10)
        self.assertEqual(respuesta["options_list"][0]["texto"], "📝 Iniciar un Reclamo")
        self.assertEqual(respuesta.get("fuente"), "greeting_handler_structured_menu_v2")
        self.assertEqual(len(respuesta.get("categorias", [])), 4)

    @patch('services.municipio_responder.llamar_gemini')
    def test_llm_mostrar_menu_returns_full_menu(self, mock_llamar_gemini):
        """Verifica que la acción "mostrar_menu" del LLM devuelve el menú completo."""
        mock_llamar_gemini.return_value = (
            {
                "message_body": "Partial menu",  # Should be replaced by local menu
                "accion_backend": "mostrar_menu",
                "datos_estructura": {"target": "municipio", "nombre_menu": "principal"},
                "botones": [{"texto": "🛠️ Iniciar un Reclamo", "action_id": "mostrar_menu_reclamos"}],
            },
            {}
        )

        response = responder_municipio(
            pregunta_original="otra consulta",
            owner_user=MagicMock(id=1),
            rubro_obj=MagicMock(nombre='municipio'),
            chat_db_context=MagicMock(context_data={}),
        )

        self.assertIn("¿Cómo te puedo ayudar hoy?", response.get("message_body", ""))
        # 3 (Reclamos) + 3 (Trámites) + 3 (Info) + 1 (Estacionamiento) = 10
        self.assertEqual(len(response.get("options_list", [])), 10)
        self.assertTrue(
            any(opt.get("texto") == "📝 Iniciar un Reclamo" for opt in response.get("options_list", []))
        )

if __name__ == '__main__':
    unittest.main()
