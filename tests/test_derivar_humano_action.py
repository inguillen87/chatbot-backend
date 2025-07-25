import unittest
from unittest.mock import patch, MagicMock
from types import SimpleNamespace
import sys
import os

# Añadir el directorio raíz al path para importar módulos de la app
project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from app import create_app, db
from models import User, Rubro
from config import TestConfig
from services.actions.common_actions import DerivarHumanoAction
from services.chat_orchestrator import ChatOrchestrator
from services.actions.municipio_actions import DerivarHumanoActionHandler
from services.actions.pyme_actions import DerivarHumanoActionHandlerPyme


class DerivarHumanoActionHandlerTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app(TestConfig)
        self.app_context = self.app.app_context()
        self.app_context.push()
        db.create_all()
        self.client = self.app.test_client()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.app_context.pop()

    @patch('services.actions.municipio_actions.servicio_tickets')
    def test_crea_ticket_municipio(self, mock_service):
        mock_service.crear_nuevo_ticket.return_value = SimpleNamespace(id=1, nro_ticket=123456)
        mock_service.crear_comentario.return_value = None
        context = {
            'viewer_user_obj': SimpleNamespace(name='Juan', telefono='123', email='a@b.com'),
            'user_obj': SimpleNamespace(municipio_id=10),
            'cliente_id': 5,
            'anon_id': None,
            'target_entity_type': 'municipio'
        }
        handler = DerivarHumanoActionHandler(context)
        result = handler.execute({'motivo_derivacion': 'prueba'})
        mock_service.crear_nuevo_ticket.assert_called_once()
        args, kwargs = mock_service.crear_nuevo_ticket.call_args
        self.assertEqual(kwargs['tipo_ticket'], 'municipio')
        ticket_data = kwargs['ticket_data']
        self.assertIn('Solicitud de Chat en Vivo', ticket_data['asunto'])
        self.assertTrue(result['success'])
        self.assertIn('M-', result['data']['chat_id'])

    @patch('services.actions.pyme_actions.servicio_tickets')
    def test_crea_ticket_pyme(self, mock_service):
        mock_service.crear_nuevo_ticket.return_value = SimpleNamespace(id=1, nro_ticket=222222)
        mock_service.crear_comentario.return_value = None
        context = {
            'viewer_user_obj': SimpleNamespace(name='Ana', telefono='456', email='x@y.com'),
            'user_id': 2,
            'rubro_id': 99,
            'cliente_id': 9,
            'anon_id': None,
            'target_entity_type': 'pyme'
        }
        handler = DerivarHumanoActionHandlerPyme(context)
        result = handler.execute({'motivo_derivacion': 'test'})
        mock_service.crear_nuevo_ticket.assert_called_once()
        args, kwargs = mock_service.crear_nuevo_ticket.call_args
        self.assertEqual(kwargs['tipo_ticket'], 'pyme')
        ticket_data = kwargs['ticket_data']
        self.assertIn('Chat en Vivo', ticket_data['asunto'])
        self.assertTrue(result['success'])
        self.assertIn('P-', result['data']['chat_id'])

    @patch('services.actions.pyme_actions.servicio_tickets')
    def test_orchestrator_routes_to_pyme_handler(self, mock_service):
        mock_service.crear_nuevo_ticket.return_value = SimpleNamespace(id=1, nro_ticket=333333)
        mock_service.crear_comentario.return_value = None
        context = {
            'viewer_user_obj': SimpleNamespace(name='Ana', telefono='456', email='x@y.com'),
            'user_id': 2,
            'rubro_id': 99,
            'cliente_id': 9,
            'anon_id': None,
            'target_entity_type': 'pyme'
        }
        orchestrator = ChatOrchestrator(global_context=context)
        result = orchestrator.execute_action({'accion_backend': 'derivar_humano', 'datos_estructura': {'motivo_derivacion': 'test'}})
        mock_service.crear_nuevo_ticket.assert_called_once()
        self.assertEqual(result['executed_action_handler'], 'DerivarHumanoActionHandlerPyme')
        self.assertTrue(result['success'])
        self.assertIn('P-', result['data']['chat_id'])

if __name__ == '__main__':
    unittest.main(verbosity=2)
