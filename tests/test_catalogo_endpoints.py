import sys
import os

# Add project root to sys.path for this test file
project_root_catalogo = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if project_root_catalogo not in sys.path:
    sys.path.insert(0, project_root_catalogo)

import unittest
from types import SimpleNamespace, ModuleType
from unittest.mock import patch, MagicMock # Added MagicMock

# -- Stub heavy dependencies before importing the module under test --
# sys.modules.setdefault('cohere', ModuleType('cohere')) # Already done globally or not needed for this test


models_stub = ModuleType('models')
models_stub.CatalogoItem = type('CatalogoItem', (), {'query': None})
models_stub.QA = type('QA', (), {'query': None})
models_stub.ArchivoAdjunto = type('ArchivoAdjunto', (), {'query': None})
models_stub.User = type('User', (), {})
models_stub.Rubro = type('Rubro', (), {})
models_stub.MunicipioTicket = type('MunicipioTicket', (), {})
models_stub.PymeTicket = type('PymeTicket', (), {})
models_stub.TicketComentario = type('TicketComentario', (), {})
models_stub.TicketSatisfaccion = type('TicketSatisfaccion', (), {})
models_stub.db = SimpleNamespace(session=None) # This will be part of the models_stub
# sys.modules.setdefault('models', models_stub) # Moved to setUp/tearDown
sys.modules.setdefault('qdrant_client', ModuleType('qdrant_client'))
qdrant_stub = sys.modules['qdrant_client']
qdrant_stub.QdrantClient = object
qdrant_stub.models = ModuleType('qdrant_client.models')
sys.modules.setdefault('qdrant_client.http', ModuleType('qdrant_client.http'))
models_mod = ModuleType('qdrant_client.http.models')
models_mod.ScoredPoint = object
sys.modules.setdefault('qdrant_client.http.models', models_mod)

# Stub pandas to avoid heavy import
pandas_stub = ModuleType('pandas')
pandas_stub.DataFrame = object
pandas_stub.Series = object
sys.modules.setdefault('pandas', pandas_stub)

# Defer import of routes.catalogo until models stub is in place
# from routes.catalogo import faq_texto, textos_perfil, resumen_catalogo

class DummyQuery(list):
    def filter_by(self, **kwargs):
        # A slightly more realistic filter_by for simple cases if needed
        key, value = list(kwargs.items())[0]
        return DummyQuery([item for item in self if getattr(item, key, None) == value])
    def all(self):
        return list(self)
    def first(self):
        return self if self else None # For .first() returning a list if that's the mock data
    def get(self, ident): # Mock for session.get or query.get
        return next((item for item in self if getattr(item, 'id', None) == ident), None)

class CatalogoEndpointsTests(unittest.TestCase):
    def setUp(self):
        self.original_models_module = sys.modules.get('models')
        sys.modules['models'] = models_stub # Apply the stub

        # Now import the module that depends on the stubbed 'models'
        import routes.catalogo
        import importlib
        importlib.reload(routes.catalogo) # Reload to use the stub
        self.catalogo_routes = routes.catalogo

        # Mock the query attributes on the stubbed models directly if needed by routes
        models_stub.CatalogoItem.query = DummyQuery()
        models_stub.QA.query = DummyQuery()
        # ... any other models used by routes.catalogo ...

        # Mock db.session if routes.catalogo uses it directly
        self.mock_db_session = MagicMock()
        models_stub.db.session = self.mock_db_session


    def tearDown(self):
        if self.original_models_module:
            sys.modules['models'] = self.original_models_module
        else:
            if 'models' in sys.modules:
                del sys.modules['models']

        # Reload routes.catalogo again to restore its state with original models
        import routes.catalogo
        import importlib
        importlib.reload(routes.catalogo)

    def test_faq_texto_returns_clean_texts(self):
        user = SimpleNamespace(rubro_id=1)
        faqs_data = [SimpleNamespace(question='Q1', answer='A1'), SimpleNamespace(question='Q2', answer='A2')]

        # Configure the mock query on the stubbed QA model for this test
        models_stub.QA.query = DummyQuery(faqs_data)

        with patch('routes.catalogo.limpiar_texto_base', lambda t: t.lower()):
            with patch('routes.catalogo.jsonify', lambda x: x):
                resp = self.catalogo_routes.faq_texto.__wrapped__(user)
        self.assertEqual(resp, ['q1 a1', 'q2 a2'])

    def test_textos_perfil_returns_clean_texts(self):
        user = SimpleNamespace(id=42)
        items_data = [SimpleNamespace(texto='Uno'), SimpleNamespace(texto='Dos')]
        models_stub.CatalogoItem.query = DummyQuery(items_data) # Configure mock for this test

        with patch('routes.catalogo.limpiar_texto_base', lambda t: t.lower()):
            with patch('routes.catalogo.jsonify', lambda x: x):
                resp = self.catalogo_routes.textos_perfil.__wrapped__(user)
        self.assertEqual(resp, ['uno', 'dos'])

    def test_resumen_catalogo_counts_by_category(self):
        user = SimpleNamespace(id=7)
        items_data = [
            SimpleNamespace(categoria='vino'),
            SimpleNamespace(categoria='vino'),
            SimpleNamespace(categoria='cerveza'),
        ]
        models_stub.CatalogoItem.query = DummyQuery(items_data) # Configure mock

        with patch('routes.catalogo.jsonify', lambda x: x):
            resp = self.catalogo_routes.resumen_catalogo.__wrapped__(user)
        self.assertEqual(resp['total'], 3)
        self.assertIn({'nombre': 'vino', 'cantidad': 2}, resp['categorias'])
        self.assertIn({'nombre': 'cerveza', 'cantidad': 1}, resp['categorias'])

if __name__ == '__main__':
    unittest.main()
