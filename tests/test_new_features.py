import unittest
from unittest.mock import patch, MagicMock
import os
import sys

# Asegúrate de que el directorio raíz del proyecto esté en el sys.path
project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from services.ticket_utils import formatear_ticket_respuesta
from services.municipios import GreetingHandler
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
        self.assertIn("Juan Obras", respuesta)
        self.assertIn("[+5491122334455](https://wa.me/5491122334455", respuesta)

    def test_greeting_handler_enhanced_message(self):
        """
        Verifica que el GreetingHandler devuelve el nuevo menú categorizado.
        """
        handler = GreetingHandler(context={})
        respuesta = handler.handle(payload={})

        self.assertIn("¡Hola! Soy JUNI, el asistente virtual de la Municipalidad de Junín.", respuesta["respuesta_usuario"])
        self.assertIn("categorias", respuesta)
        self.assertEqual(len(respuesta["categorias"]), 4)
        self.assertEqual(respuesta["categorias"][0]["titulo"], "Trámites y Consultas")
        self.assertEqual(respuesta["fuente"], "greeting_handler_categorized_v1")

if __name__ == '__main__':
    unittest.main()
