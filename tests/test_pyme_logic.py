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
models_stub.Conversacion = _DummyModel
models_stub.PymeTicket = _DummyModel
models_stub.MunicipioTicket = _DummyModel
models_stub.TicketComentario = _DummyModel
models_stub.TicketSatisfaccion = _DummyModel
models_stub.PymePedido = _DummyModel
models_stub.Rubro = _DummyModel
models_stub.SitioWebInfo = _DummyModel
models_stub.User = _DummyModel
class _DummyQuery(list):
    def filter_by(self, **kwargs):
        return self
    def all(self):
        return []

class DummyCatalogoItem:
    query = _DummyQuery()

models_stub.CatalogoItem = DummyCatalogoItem
models_stub.CatalogoEmbedding = _DummyModel
models_stub.QA = _DummyModel
models_stub.db = SimpleNamespace(session=_DummySession())
sys.modules['models'] = models_stub
sys.modules.pop('services.catalogo_local', None)

flask_stub = ModuleType('flask')
flask_stub.session = {}
sys.modules['flask'] = flask_stub

sys.modules['cohere'] = ModuleType('cohere')
sqlalchemy_stub = ModuleType('sqlalchemy')
sqlalchemy_exc_stub = ModuleType('sqlalchemy.exc')
class _SAError(Exception):
    pass
sqlalchemy_exc_stub.SQLAlchemyError = _SAError
sqlalchemy_stub.exc = sqlalchemy_exc_stub
sys.modules['sqlalchemy'] = sqlalchemy_stub
sys.modules['sqlalchemy.exc'] = sqlalchemy_exc_stub
sys.modules['requests'] = ModuleType('requests')
pandas_stub = ModuleType('pandas')
class _DummyDF: pass
class _DummySeries: pass
pandas_stub.DataFrame = _DummyDF
pandas_stub.Series = _DummySeries
sys.modules['pandas'] = pandas_stub
sys.modules['spacy'] = ModuleType('spacy')
bs4_stub = ModuleType('bs4')
bs4_stub.BeautifulSoup = object
sys.modules['bs4'] = bs4_stub
qdrant_stub = ModuleType('qdrant_client')
qdrant_http_stub = ModuleType('qdrant_client.http')
qdrant_http_models_stub = ModuleType('qdrant_client.http.models')
qdrant_stub.QdrantClient = type('QdrantClient', (), {})
qdrant_stub.models = ModuleType('qdrant_client.models')
qdrant_stub.http = qdrant_http_stub
qdrant_http_stub.models = qdrant_http_models_stub
qdrant_http_models_stub.ScoredPoint = object
sys.modules['qdrant_client'] = qdrant_stub
sys.modules['qdrant_client.http'] = qdrant_http_stub
sys.modules['qdrant_client.http.models'] = qdrant_http_models_stub
twilio_rest_stub = ModuleType('twilio.rest')
class _DummyClient:
    class messages:
        @staticmethod
        def create(*a, **k):
            return SimpleNamespace(sid='dummy')
twilio_rest_stub.Client = _DummyClient
sys.modules.setdefault('twilio.rest', twilio_rest_stub)
sys.modules.setdefault('twilio', ModuleType('twilio'))

from services import pymes

class DummyTicket:
    def __init__(self, id=1, nro_ticket=654321):
        self.id = id
        self.nro_ticket = nro_ticket

class DummyUser(SimpleNamespace):
    def __init__(self):
        super().__init__(
            id=1,
            rubro=SimpleNamespace(nombre='vinoteca'),
            nombre_empresa='Bodega Ejemplo',
            telefono='123456',
            direccion='Calle Falsa 123',
            email='a@b.com',
            plan='full',
            preguntas_usadas=0,
            limite_preguntas=100,
        )

class PymeLogicTests(unittest.TestCase):
    @patch('services.pymes.get_cohere_response', return_value='PREGUNTA_NUEVA')
    def test_es_pregunta_nueva_llm(self, mock_llm):
        self.assertTrue(pymes.es_pregunta_nueva('hola', 'el dato solicitado'))
        mock_llm.return_value = 'RESPUESTA_VALIDA'
        self.assertFalse(pymes.es_pregunta_nueva('hola', 'el dato solicitado'))

    @patch('services.catalogo_local.buscar_catalogo_local', return_value=[])
    @patch('services.pymes.servicio_tickets')
    @patch('services.pymes._clasificar_intencion_pyme_con_llm', return_value='hablar_con_agente_pyme')
    def test_human_escalation_pyme(self, mock_clf, mock_servicio, mock_buscar):
        mock_servicio.crear_nuevo_ticket.return_value = DummyTicket()
        mock_servicio.crear_comentario.return_value = None
        user = DummyUser()
        resp = pymes.responder_pyme('Necesito hablar con un agente', user, None, viewer_user=user)
        self.assertIn('sala de chat', resp['respuesta'])

    @patch('services.catalogo_local.buscar_catalogo_local', return_value=[])
    @patch('services.pymes.servicio_tickets')
    @patch('services.pymes._clasificar_intencion_pyme_con_llm', return_value='hablar_con_agente_pyme')
    def test_human_escalation_pyme_anonymous_requires_login(self, mock_clf, mock_servicio, mock_buscar):
        mock_servicio.crear_nuevo_ticket.return_value = DummyTicket()
        resp = pymes.responder_pyme('Necesito hablar con un agente', DummyUser(), None, viewer_user=None)
        self.assertIn('iniciar sesión', resp['respuesta'])

    def test_greeting_with_typo(self):
        user = DummyUser()
        resp = pymes.responder_pyme('holaa buenos noxes', user, None, viewer_user=user)
        self.assertIn('¿En qué puedo ayudarte hoy', resp['respuesta'])

    def test_small_talk(self):
        user = DummyUser()
        with patch('services.logic.get_cohere_response', side_effect=['SI', '¡Hola! ¿Cómo estás?']) as mock_llm:
            resp = pymes.responder_pyme('¿Cómo andas?', user, None, viewer_user=user)
            self.assertEqual(resp['fuente'], 'smalltalk_pyme_llm')
            self.assertIn('Hola', resp['respuesta'])
            self.assertEqual(mock_llm.call_count, 2)

    def test_cancel_keywords(self):
        contexto = {
            'contexto_pyme': {
                'estado_conversacion': pymes.serialize_state(pymes.PymeConversationState.CONFIRMANDO_PEDIDO_TEMP),
                'productos_solicitados_temp': [{'nombre': 'vino', 'cantidad': 1}],
                'monto_total_temp': 100
            },
            'user_id': 1,
            'rubro_obj': SimpleNamespace(nombre='vinoteca'),
            'user_obj': DummyUser(),
            'rubro_nombre': 'vinoteca'
        }
        handler = pymes.PedidoHandler(contexto)
        resp = handler.handle('cancelalo por favor')
        self.assertEqual(resp['fuente'], 'pedido_cancelado')

    @patch('services.pymes._clasificar_intencion_pyme_con_llm', return_value='iniciar_pedido')
    def test_iniciar_pedido_flujo(self, mock_clf):
        user = DummyUser()
        resp = pymes.responder_pyme('quiero comprar', user, None, viewer_user=user)
        self.assertEqual(resp['estado_respuesta'], 'pyme_pregunta_pedido')
        self.assertIn('pedido', resp['respuesta'].lower())


if __name__ == '__main__':
    unittest.main()
