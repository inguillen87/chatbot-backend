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
models_stub.TicketComentario = _DummyModel
models_stub.PymePedido = _DummyModel
models_stub.Rubro = _DummyModel
models_stub.SitioWebInfo = _DummyModel
models_stub.User = _DummyModel
models_stub.CatalogoItem = _DummyModel
models_stub.CatalogoEmbedding = _DummyModel
models_stub.QA = _DummyModel
models_stub.db = SimpleNamespace(session=_DummySession())
sys.modules['models'] = models_stub

flask_stub = ModuleType('flask')
flask_stub.session = {}
class _DummyBlueprint:
    def __init__(self, *a, **k):
        pass
    def route(self, *a, **k):
        def decorator(func):
            return func
        return decorator
flask_stub.Blueprint = _DummyBlueprint
flask_stub.request = SimpleNamespace(files={}, headers={})
flask_stub.jsonify = lambda *a, **k: {}
sys.modules['flask'] = flask_stub

werkzeug_stub = ModuleType('werkzeug.utils')
werkzeug_stub.secure_filename = lambda name: name
sys.modules['werkzeug.utils'] = werkzeug_stub

fsql_stub = ModuleType('flask_sqlalchemy')
class _DummySQLAlchemy:
    def __init__(self, *a, **k):
        self.session = SimpleNamespace(bulk_save_objects=lambda *a, **k: None, commit=lambda: None, rollback=lambda: None)
fsql_stub.SQLAlchemy = _DummySQLAlchemy
sys.modules['flask_sqlalchemy'] = fsql_stub

flask_migrate_stub = ModuleType('flask_migrate')
flask_migrate_stub.Migrate = lambda *a, **k: None
sys.modules['flask_migrate'] = flask_migrate_stub

flask_login_stub = ModuleType('flask_login')
flask_login_stub.LoginManager = lambda *a, **k: None
sys.modules['flask_login'] = flask_login_stub

extensions_stub = ModuleType('extensions')
extensions_stub.db = models_stub.db
extensions_stub.migrate = SimpleNamespace()
sys.modules['extensions'] = extensions_stub

google_cloud_stub = ModuleType('google.cloud')
documentai_stub = ModuleType('google.cloud.documentai')
class _DummyDoc:
    class TextAnchor:
        def __init__(self):
            self.text_segments = []
    class Page:
        class Table:
            pass
    def __init__(self):
        self.pages = []
documentai_stub.Document = _DummyDoc
google_cloud_stub.documentai = documentai_stub
sys.modules['google'] = ModuleType('google')
sys.modules['google.cloud'] = google_cloud_stub
sys.modules['google.cloud.documentai'] = documentai_stub
google_oauth_stub = ModuleType('google.oauth2')
service_account_stub = ModuleType('google.oauth2.service_account')
service_account_stub.Credentials = SimpleNamespace(from_service_account_info=lambda info: None)
google_oauth_stub.service_account = service_account_stub
sys.modules['google.oauth2'] = google_oauth_stub
sys.modules['google.oauth2.service_account'] = service_account_stub

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

from services import pymes
from services import upload_processor

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

    @patch('services.pymes.get_cohere_response', return_value='PREGUNTA_NUEVA')
    def test_heuristics_ticket(self, mock_llm):
        self.assertFalse(pymes.es_pregunta_nueva('123456', 'un número de ticket'))
        mock_llm.assert_not_called()

    @patch('services.pymes.get_cohere_response', return_value='RESPUESTA_VALIDA')
    def test_heuristics_gracias(self, mock_llm):
        self.assertTrue(pymes.es_pregunta_nueva('gracias', 'el dato solicitado'))

    @patch('services.pymes.servicio_tickets')
    @patch('services.pymes._clasificar_intencion_con_llm', return_value='hablar_con_agente_pyme')
    def test_human_escalation_pyme(self, mock_clf, mock_servicio):
        mock_servicio.crear_nuevo_ticket.return_value = DummyTicket()
        mock_servicio.crear_comentario.return_value = None
        user = DummyUser()
        resp = pymes.responder_pyme('Necesito hablar con un agente', user, None)
        self.assertIn('sala de chat', resp['respuesta'])


class UploadProcessorTests(unittest.TestCase):
    @patch('services.upload_processor.CatalogoItem')
    @patch('services.upload_processor.db')
    @patch('services.upload_processor.guardar_en_qdrant')
    @patch('services.upload_processor.embed_textos', return_value=[[0.1, 0.2]])
    @patch('services.upload_processor.procesar_catalogo_pdf_google', return_value=[{'nombre': 'vino', 'descripcion': 'tinto', 'precio_str': '10', 'texto_para_embedding': 'vino'}])
    def test_procesar_pdf_ruta(self, mock_proc, mock_embed, mock_qdrant, mock_db, mock_model):
        mock_db.session = SimpleNamespace(bulk_save_objects=lambda objs: None, commit=lambda: None, rollback=lambda: None)
        mock_model.query = SimpleNamespace(filter_by=lambda **k: SimpleNamespace(delete=lambda: None))
        res = upload_processor.procesar_y_embedear_catalogo('archivo.pdf', 1, 'vino')
        self.assertEqual(res, 1)
        mock_proc.assert_called_once_with('archivo.pdf', 1, 'vino')

    def test_extension_invalida(self):
        with self.assertRaises(ValueError):
            upload_processor.procesar_y_embedear_catalogo('archivo.docx', 1, 'vino')


if __name__ == '__main__':
    unittest.main()
