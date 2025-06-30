import unittest
from types import SimpleNamespace, ModuleType
from unittest.mock import patch
import sys

# -- Stub heavy dependencies before importing the module under test --
twilio_rest_stub = ModuleType('twilio.rest')
twilio_rest_stub.Client = object
sys.modules.setdefault('twilio.rest', twilio_rest_stub)
sys.modules.setdefault('twilio', ModuleType('twilio'))
sys.modules.setdefault('cohere', ModuleType('cohere'))
flask_stub = ModuleType('flask')
class _DummyBP:
    def __init__(self, *a, **k):
        pass
    def route(self, *a, **k):
        def decorator(f):
            return f
        return decorator
flask_stub.Blueprint = _DummyBP
def abort(code):
    raise Exception(f"abort {code}")
flask_stub.abort = abort
class _Req: headers = {}
flask_stub.request = _Req()
flask_stub.current_app = SimpleNamespace()
flask_stub.g = SimpleNamespace()
def _jsonify(x):
    return x
flask_stub.jsonify = _jsonify
sys.modules.setdefault('flask', flask_stub)
sqlalchemy_stub = ModuleType('sqlalchemy')
sqlalchemy_stub.func = SimpleNamespace()
sys.modules.setdefault('sqlalchemy', sqlalchemy_stub)
sqlalchemy_exc_stub = ModuleType('sqlalchemy.exc')
sqlalchemy_exc_stub.SQLAlchemyError = type('SQLAlchemyError', (Exception,), {})
sys.modules.setdefault('sqlalchemy.exc', sqlalchemy_exc_stub)
sys.modules.setdefault('requests', ModuleType('requests'))
flask_sqlalchemy_stub = ModuleType('flask_sqlalchemy')
flask_sqlalchemy_stub.SQLAlchemy = object
sys.modules.setdefault('flask_sqlalchemy', flask_sqlalchemy_stub)
flask_migrate_stub = ModuleType('flask_migrate')
flask_migrate_stub.Migrate = object
sys.modules.setdefault('flask_migrate', flask_migrate_stub)
flask_login_stub = ModuleType('flask_login')
flask_login_stub.LoginManager = object
sys.modules.setdefault('flask_login', flask_login_stub)
models_stub = ModuleType('models')
models_stub.User = type('User', (), {})
models_stub.Rubro = type('Rubro', (), {})
models_stub.MunicipioTicket = type('MunicipioTicket', (), {})
models_stub.PymeTicket = type('PymeTicket', (), {})
models_stub.TicketComentario = type('TicketComentario', (), {})
models_stub.TicketSatisfaccion = type('TicketSatisfaccion', (), {})
models_stub.SitioWebInfo = type('SitioWebInfo', (), {})
models_stub.db = SimpleNamespace(session=None)
sys.modules.setdefault('models', models_stub)

from routes.categorias import obtener_categorias

class CategoriasEndpointTest(unittest.TestCase):
    def test_returns_category_list(self):
        user = SimpleNamespace(rol='admin')
        with patch('routes.categorias.jsonify', lambda x: x):
            resp = obtener_categorias.__wrapped__(user)
        self.assertIsInstance(resp, dict)
        self.assertIn('categorias', resp)
        self.assertIn('Luminaria', resp['categorias'])

if __name__ == '__main__':
    unittest.main()
