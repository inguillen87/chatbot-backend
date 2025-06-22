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
models_stub.TicketSatisfaccion = _DummyModel
models_stub.SitioWebInfo = _DummyModel
models_stub.db = SimpleNamespace(session=_DummySession())
sys.modules['models'] = models_stub

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

    @patch('services.municipios.get_cohere_response', return_value='RESPUESTA_VALIDA')
    def test_es_pregunta_nueva_agente(self, mock_llm):
        self.assertTrue(
            municipios.es_pregunta_nueva('Quiero hablar con un asesor', 'una dirección')
        )

    @patch('services.municipios.get_cohere_response', return_value='')
    @patch('services.municipios.servicio_tickets')
    @patch('services.municipios._clasificar_intencion_con_llm', return_value='hablar_con_agente')
    def test_human_escalation(self, mock_clf, mock_servicio, mock_llm):
        mock_servicio.crear_nuevo_ticket.return_value = DummyTicket()
        mock_servicio.crear_comentario.return_value = None
        user = DummyUser()
        resp = municipios.responder_municipio('Hablar con un agente', user, None, viewer_user=user)
        self.assertIn('chat directa', resp['respuesta'])

    @patch('services.municipios.get_cohere_response', return_value='')
    @patch('services.municipios.servicio_tickets')
    @patch('services.municipios._clasificar_intencion_con_llm', return_value='hablar_con_agente')
    def test_human_escalation_anonymous_requires_login(self, mock_clf, mock_servicio, mock_llm):
        mock_servicio.crear_nuevo_ticket.return_value = DummyTicket()
        resp = municipios.responder_municipio('Hablar con un agente', DummyUser(), None, viewer_user=None)
        self.assertIn('iniciar sesión', resp['respuesta'])

    def test_greeting_variation(self):
        user = DummyUser()
        resp = municipios.responder_municipio('hola buenos noches', user, None, viewer_user=user)
        self.assertIn('tu asistente digital del Municipio', resp['respuesta'])

    def test_small_talk_municipio(self):
        user = DummyUser()
        with patch('services.logic.get_cohere_response', side_effect=['SI', '¡Hola! ¿Todo bien!']) as mock_llm:
            resp = municipios.responder_municipio('¿Cómo te va?', user, None, viewer_user=user)
            self.assertIn('Hola', resp['respuesta'])
            self.assertEqual(mock_llm.call_count, 2)

    def test_tramite_selection_returns_string(self):
        """El texto de respuesta para un trámite debe ser una cadena."""
        user = DummyUser()
        # Primer paso: iniciar el flujo de trámites
        resp1 = municipios.responder_municipio('Quiero hacer un tramite', user, None, viewer_user=user)
        contexto = resp1.get('contexto_actualizado')
        self.assertIn('trámite', resp1['respuesta'].lower())
        # Seleccionamos un trámite específico
        resp2 = municipios.responder_municipio('Rentas', user, None, viewer_user=user, contexto_previo=contexto)
        self.assertIsInstance(resp2['respuesta'], str)
        self.assertIn('https://www.juninmendoza.gov.ar/vencimientos/', resp2['respuesta'])
        self.assertTrue(
            any(b.get('url') == 'https://www.juninmendoza.gov.ar/vencimientos/' for b in resp2.get('botones', []))
        )



if __name__ == '__main__':
    unittest.main()
