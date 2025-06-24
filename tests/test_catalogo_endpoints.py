import unittest
from types import SimpleNamespace, ModuleType
from unittest.mock import patch
import sys

# -- Stub heavy dependencies before importing the module under test --
sys.modules.setdefault('cohere', ModuleType('cohere'))
pandas_stub = ModuleType('pandas')
pandas_stub.Series = object
pandas_stub.DataFrame = object
sys.modules.setdefault('pandas', pandas_stub)

models_stub = ModuleType('models')
models_stub.CatalogoItem = type('CatalogoItem', (), {'query': None})
models_stub.QA = type('QA', (), {'query': None})
models_stub.User = type('User', (), {})
models_stub.Rubro = type('Rubro', (), {})
models_stub.MunicipioTicket = type('MunicipioTicket', (), {})
models_stub.PymeTicket = type('PymeTicket', (), {})
models_stub.TicketComentario = type('TicketComentario', (), {})
models_stub.TicketSatisfaccion = type('TicketSatisfaccion', (), {})
models_stub.db = SimpleNamespace(session=None)
sys.modules['models'] = models_stub
sys.modules.setdefault('qdrant_client', ModuleType('qdrant_client'))
qdrant_stub = sys.modules['qdrant_client']
qdrant_stub.QdrantClient = object
qdrant_stub.models = ModuleType('qdrant_client.models')
sys.modules.setdefault('qdrant_client.http', ModuleType('qdrant_client.http'))
models_mod = ModuleType('qdrant_client.http.models')
models_mod.ScoredPoint = object
sys.modules.setdefault('qdrant_client.http.models', models_mod)
upload_stub = ModuleType('services.upload_processor')
upload_stub.subir_catalogo = lambda: None
sys.modules.setdefault('services.upload_processor', upload_stub)

from routes.catalogo import faq_texto, textos_perfil

class DummyQuery(list):
    def filter_by(self, **kwargs):
        return self
    def all(self):
        return list(self)

class CatalogoEndpointsTests(unittest.TestCase):
    def test_faq_texto_returns_clean_texts(self):
        user = SimpleNamespace(rubro_id=1)
        faqs = [SimpleNamespace(question='Q1', answer='A1'), SimpleNamespace(question='Q2', answer='A2')]
        with patch('routes.catalogo.QA', SimpleNamespace(query=DummyQuery(faqs))):
            with patch('routes.catalogo.limpiar_texto_base', lambda t: t.lower()):
                with patch('routes.catalogo.jsonify', lambda x: x):
                    resp = faq_texto.__wrapped__(user)
        self.assertEqual(resp, ['q1 a1', 'q2 a2'])

    def test_textos_perfil_returns_clean_texts(self):
        user = SimpleNamespace(id=42)
        items = [SimpleNamespace(texto='Uno'), SimpleNamespace(texto='Dos')]
        with patch('routes.catalogo.CatalogoItem', SimpleNamespace(query=DummyQuery(items))):
            with patch('routes.catalogo.limpiar_texto_base', lambda t: t.lower()):
                with patch('routes.catalogo.jsonify', lambda x: x):
                    resp = textos_perfil.__wrapped__(user)
        self.assertEqual(resp, ['uno', 'dos'])

if __name__ == '__main__':
    unittest.main()
