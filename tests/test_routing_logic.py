import unittest
from unittest.mock import patch, MagicMock
import os
import sys

# Add project root to sys.path
project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from app import create_app

class RoutingLogicTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app('testing')
        self.app_context = self.app.app_context()
        self.app_context.push()

    def tearDown(self):
        self.app_context.pop()

    @patch('services.municipio_responder.responder_municipio')
    @patch('services.pymes.responder_pyme')
    def test_municipio_routing(self, mock_responder_pyme, mock_responder_municipio):
        """Test that 'municipio' type chats are routed to the municipio responder."""
        from services.logic import responder_chatboc
        rubro_obj = MagicMock()
        rubro_obj.nombre = "municipio"
        with self.app.test_request_context():
            responder_chatboc('hola', tipo_chat='municipio', rubro_obj=rubro_obj)
        mock_responder_municipio.assert_called_once()
        mock_responder_pyme.assert_not_called()

    @patch('services.municipio_responder.responder_municipio')
    @patch('services.pymes.responder_pyme')
    def test_pyme_routing(self, mock_responder_pyme, mock_responder_municipio):
        """Test that 'pyme' type chats are routed to the pyme responder."""
        from services.logic import responder_chatboc
        rubro_obj = MagicMock()
        rubro_obj.nombre = "pyme"
        with self.app.test_request_context():
            responder_chatboc('hola', tipo_chat='pyme', rubro_obj=rubro_obj)
        mock_responder_pyme.assert_called_once()
        mock_responder_municipio.assert_not_called()

    def test_tipo_chat_required(self):
        """Test that an error is raised if tipo_chat is invalid."""
        from services.logic import responder_chatboc
        with self.assertRaises(ValueError):
            responder_chatboc('hola', tipo_chat='invalido')

    @patch('services.municipio_responder.responder_municipio')
    @patch('services.pymes.responder_pyme')
    def test_rubro_corrige_a_municipio(self, mock_responder_pyme, mock_responder_municipio):
        """Si el rubro es de pyme pero viene tipo_chat municipio se corrige."""
        from services.logic import responder_chatboc
        rubro_obj = MagicMock()
        rubro_obj.nombre = "pyme"
        with self.app.test_request_context():
            responder_chatboc('hola', tipo_chat='municipio', rubro_obj=rubro_obj)
        mock_responder_pyme.assert_called_once()
        mock_responder_municipio.assert_not_called()

    @patch('services.municipio_responder.responder_municipio')
    @patch('services.pymes.responder_pyme')
    def test_rubro_sin_nombre_usa_clave(self, mock_responder_pyme, mock_responder_municipio):
        """Debe usar la clave del rubro cuando no hay nombre."""
        from services.logic import responder_chatboc
        rubro_obj = MagicMock()
        rubro_obj.nombre = None
        rubro_obj.clave = "municipio"
        with self.app.test_request_context():
            responder_chatboc('hola', tipo_chat='municipio', rubro_obj=rubro_obj)
        mock_responder_municipio.assert_called_once()
        mock_responder_pyme.assert_not_called()

    @patch('services.municipio_responder.responder_municipio')
    @patch('services.pymes.responder_pyme')
    def test_rubro_corrige_a_pyme(self, mock_responder_pyme, mock_responder_municipio):
        """Si el rubro es municipal pero viene tipo_chat pyme se corrige."""
        from services.logic import responder_chatboc
        rubro_obj = MagicMock()
        rubro_obj.nombre = "municipio"
        with self.app.test_request_context():
            responder_chatboc('hola', tipo_chat='pyme', rubro_obj=rubro_obj)
        mock_responder_municipio.assert_called_once()
        mock_responder_pyme.assert_not_called()

    def test_es_rubro_publico_normaliza(self):
        from services.logic import es_rubro_publico
        self.assertTrue(es_rubro_publico("  Municipio  "))
        self.assertTrue(es_rubro_publico("GOBIERNO"))
        self.assertFalse(es_rubro_publico("  Pyme  "))

if __name__ == '__main__':
    unittest.main()
