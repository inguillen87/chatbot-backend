import unittest
from unittest.mock import patch
from types import SimpleNamespace
import os
import sys

# Add project root to sys.path
project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from app import create_app
from config import TestConfig
from services.logic import responder_chatboc, es_rubro_publico

class RoutingLogicTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app(TestConfig)
        self.app_context = self.app.app_context()
        self.app_context.push()
        # Mock context object that the responders expect
        self.mock_chat_db_context = SimpleNamespace(context_data={})

    def tearDown(self):
        self.app_context.pop()

    @patch('services.municipio_responder.responder_municipio')
    @patch('services.pymes.responder_pyme')
    def test_municipio_routing(self, mock_responder_pyme, mock_responder_municipio):
        """Test that 'municipio' type chats are routed to the municipio responder."""
        rubro_obj = SimpleNamespace(nombre="municipio")
        responder_chatboc('hola', tipo_chat='municipio', rubro_obj=rubro_obj, chat_db_context=self.mock_chat_db_context)
        mock_responder_municipio.assert_called_once()
        mock_responder_pyme.assert_not_called()

    @patch('services.municipio_responder.responder_municipio')
    @patch('services.pymes.responder_pyme')
    def test_pyme_routing(self, mock_responder_pyme, mock_responder_municipio):
        """Test that 'pyme' type chats are routed to the pyme responder."""
        rubro_obj = SimpleNamespace(nombre="pyme")
        responder_chatboc('hola', tipo_chat='pyme', rubro_obj=rubro_obj, chat_db_context=self.mock_chat_db_context)
        mock_responder_pyme.assert_called_once()
        mock_responder_municipio.assert_not_called()

    def test_tipo_chat_required(self):
        """Test that an error is raised if tipo_chat is invalid."""
        with self.assertRaises(ValueError):
            responder_chatboc('hola', tipo_chat='invalido', chat_db_context=self.mock_chat_db_context)

    @patch('services.municipio_responder.responder_municipio')
    @patch('services.pymes.responder_pyme')
    def test_rubro_overrides_tipo_chat_to_pyme(self, mock_responder_pyme, mock_responder_municipio):
        """Tests that if a rubro is 'pyme', it routes to pyme_responder even if tipo_chat says 'municipio'."""
        rubro_obj = SimpleNamespace(nombre="pyme")
        responder_chatboc('hola', tipo_chat='municipio', rubro_obj=rubro_obj, chat_db_context=self.mock_chat_db_context)
        mock_responder_pyme.assert_called_once()
        mock_responder_municipio.assert_not_called()

    @patch('services.municipio_responder.responder_municipio')
    @patch('services.pymes.responder_pyme')
    def test_rubro_overrides_tipo_chat_to_municipio(self, mock_responder_pyme, mock_responder_municipio):
        """Tests that if a rubro is public, it routes to municipio_responder even if tipo_chat says 'pyme'."""
        rubro_obj = SimpleNamespace(nombre="gobierno") # 'gobierno' is in RUBROS_PUBLICOS
        responder_chatboc('hola', tipo_chat='pyme', rubro_obj=rubro_obj, chat_db_context=self.mock_chat_db_context)
        mock_responder_municipio.assert_called_once()
        mock_responder_pyme.assert_not_called()

    @patch('services.municipio_responder.responder_municipio')
    @patch('services.pymes.responder_pyme')
    def test_rubro_sin_nombre_usa_clave(self, mock_responder_pyme, mock_responder_municipio):
        """Debe usar la clave del rubro cuando no hay nombre."""
        rubro_obj = SimpleNamespace(nombre=None, clave="municipio")
        responder_chatboc('hola', tipo_chat='pyme', rubro_obj=rubro_obj, chat_db_context=self.mock_chat_db_context)
        mock_responder_municipio.assert_called_once()
        mock_responder_pyme.assert_not_called()

    def test_es_rubro_publico_normaliza(self):
        self.assertTrue(es_rubro_publico("  Municipio  "))
        self.assertTrue(es_rubro_publico("GOBIERNO"))
        self.assertFalse(es_rubro_publico("  Pyme  "))

if __name__ == '__main__':
    unittest.main()
