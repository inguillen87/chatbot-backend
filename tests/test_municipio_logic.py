import unittest
from types import SimpleNamespace, ModuleType
from unittest.mock import patch
import sys

# -- Crear stubs mínimos para dependencias pesadas --
models_stub = ModuleType('models')
class _DummyModel: pass
class _DummySession:
    def get(self, *a, **k):
        return None
    def add(self, *a, **k):
        pass
    def commit(self):
        pass
    def flush(self):
        pass
    def rollback(self):
        pass
models_stub.MunicipioTicket = _DummyModel
models_stub.PymeTicket = _DummyModel
models_stub.TicketComentario = _DummyModel
models_stub.SitioWebInfo = _DummyModel
models_stub.db = SimpleNamespace(session=_DummySession())
sys.modules.setdefault('models', models_stub)

twilio_rest_stub = ModuleType('twilio.rest')
class _DummyClient:
    class messages:
        @staticmethod
        def create(*a, **k):
            return SimpleNamespace(sid='dummy')
twilio_rest_stub.Client = _DummyClient
sys.modules.setdefault('twilio.rest', twilio_rest_stub)
sys.modules.setdefault('twilio', ModuleType('twilio'))
sys.modules.setdefault('cohere', ModuleType('cohere'))
sqlalchemy_stub = ModuleType('sqlalchemy')
sqlalchemy_exc_stub = ModuleType('sqlalchemy.exc')
class _SAError(Exception):
    pass
sqlalchemy_exc_stub.SQLAlchemyError = _SAError
sqlalchemy_stub.exc = sqlalchemy_exc_stub
sys.modules.setdefault('sqlalchemy', sqlalchemy_stub)
sys.modules.setdefault('sqlalchemy.exc', sqlalchemy_exc_stub)
sys.modules.setdefault('requests', ModuleType('requests'))

from services import municipios

class DummyTicket:
    def __init__(self, id=1, nro_ticket=123456):
        self.id = id
        self.nro_ticket = nro_ticket

class DummyUser(SimpleNamespace):
    def __init__(self):
        super().__init__(
            id=1,
            rubro=SimpleNamespace(nombre='municipio'),
            plan='full',
            preguntas_usadas=0,
            limite_preguntas=100,
            link_web='https://example.com',
            direccion='Avenida Siempreviva 123'
        )

class MunicipioLogicTests(unittest.TestCase):
    @patch('services.municipios.get_cohere_response', return_value='RESPUESTA_VALIDA')
    def test_es_pregunta_nueva_acknowledge(self, mock_llm):
        self.assertTrue(municipios.es_pregunta_nueva('ok', 'un número de ticket'))
        self.assertTrue(municipios.es_pregunta_nueva('gracias', 'una dirección'))

    @patch('services.municipios.get_cohere_response', return_value='')
    @patch('services.municipios.servicio_tickets')
    @patch('services.municipios._clasificar_intencion_con_llm', return_value='hablar_con_agente')
    def test_human_escalation(self, mock_clf, mock_servicio, mock_llm):
        mock_servicio.crear_nuevo_ticket.return_value = DummyTicket()
        mock_servicio.crear_comentario.return_value = None
        user = DummyUser()
        resp = municipios.responder_municipio('Hablar con un agente', user, None)
        self.assertIn('chat directa', resp['respuesta'])



if __name__ == '__main__':
    unittest.main()
