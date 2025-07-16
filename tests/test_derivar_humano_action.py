import unittest
from unittest.mock import patch
from types import SimpleNamespace, ModuleType
import sys

# Minimal stubs so importing ticket_service doesn't require full SQLAlchemy setup
models_stub = ModuleType('models')
models_stub.MunicipioTicket = type('MunicipioTicket', (), {})
models_stub.PymeTicket = type('PymeTicket', (), {})
models_stub.TicketComentario = type('TicketComentario', (), {})
models_stub.TicketSatisfaccion = type('TicketSatisfaccion', (), {})
models_stub.db = SimpleNamespace(session=SimpleNamespace(add=lambda *a, **k: None,
                                                         commit=lambda: None,
                                                         flush=lambda: None))
sys.modules.setdefault('models', models_stub)
sqlalchemy_stub = ModuleType('sqlalchemy')
sqlalchemy_exc_stub = ModuleType('sqlalchemy.exc')
class _DummySAError(Exception):
    pass
sqlalchemy_exc_stub.SQLAlchemyError = _DummySAError
sys.modules.setdefault('sqlalchemy', sqlalchemy_stub)
sys.modules.setdefault('sqlalchemy.exc', sqlalchemy_exc_stub)
sys.modules.setdefault('services.integracion_municipal', ModuleType('services.integracion_municipal'))
sys.modules['services.integracion_municipal'].enviar_ticket_a_sigem = lambda *a, **k: True
municipios_stub = ModuleType('services.municipios')
municipios_stub.enviar_notificacion_whatsapp_con_plantilla = lambda *a, **k: None
municipios_stub.enviar_notificacion_sms = lambda *a, **k: None
sys.modules.setdefault('services.municipios', municipios_stub)
sys.modules.setdefault('requests', ModuleType('requests'))
pandas_stub = ModuleType('pandas')
pandas_stub.DataFrame = object
sys.modules.setdefault('pandas', pandas_stub)

from services.actions.municipio_actions import DerivarHumanoActionHandler
from services.chat_orchestrator import ChatOrchestrator

class DummyTicket(SimpleNamespace):
    def __init__(self, id=1, nro_ticket=123456):
        super().__init__(id=id, nro_ticket=nro_ticket)

class DerivarHumanoActionHandlerTests(unittest.TestCase):
    @patch('services.actions.municipio_actions.servicio_tickets')
    def test_crea_ticket_municipio(self, mock_service):
        mock_service.crear_nuevo_ticket.return_value = DummyTicket()
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
        self.assertEqual(args[0], 'municipio')
        ticket_data = args[1]
        self.assertIn('Solicitud de Chat en Vivo', ticket_data['asunto'])
        self.assertTrue(result['success'])
        self.assertIn('M-', result['data']['chat_id'])

    @patch('services.actions.municipio_actions.servicio_tickets')
    def test_crea_ticket_pyme(self, mock_service):
        mock_service.crear_nuevo_ticket.return_value = DummyTicket(nro_ticket=222222)
        mock_service.crear_comentario.return_value = None
        context = {
            'viewer_user_obj': SimpleNamespace(name='Ana', telefono='456', email='x@y.com'),
            'user_id': 2,
            'rubro_id': 99,
            'cliente_id': 9,
            'anon_id': None,
            'target_entity_type': 'pyme'
        }
        handler = DerivarHumanoActionHandler(context)
        result = handler.execute({'motivo_derivacion': 'test'})
        mock_service.crear_nuevo_ticket.assert_called_once()
        args, kwargs = mock_service.crear_nuevo_ticket.call_args
        self.assertEqual(args[0], 'pyme')
        ticket_data = args[1]
        self.assertIn('Chat en Vivo', ticket_data['asunto'])
        self.assertTrue(result['success'])
        self.assertIn('P-', result['data']['chat_id'])

    @patch('services.actions.pyme_actions.servicio_tickets')
    def test_orchestrator_routes_to_pyme_handler(self, mock_service):
        mock_service.crear_nuevo_ticket.return_value = DummyTicket(nro_ticket=333333)
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
