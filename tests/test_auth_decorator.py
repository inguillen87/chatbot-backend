import unittest
from types import SimpleNamespace, ModuleType
import sys

# --- stub minimal flask features required by routes.auth ---
flask_stub = ModuleType('flask')
request_obj = SimpleNamespace(headers={}, args={}, form={}, _json=None)

def get_json(silent=False):
    return request_obj._json

request_obj.get_json = get_json
request_obj.__dict__['is_json'] = property(lambda self: self._json is not None)
flask_stub.request = request_obj
flask_stub.jsonify = lambda d: d
class DummyBlueprint:
    def __init__(self, *a, **k):
        pass
    def route(self, *a, **k):
        def decorator(f):
            return f
        return decorator

flask_stub.Blueprint = DummyBlueprint
flask_stub.current_app = SimpleNamespace(logger=SimpleNamespace(info=lambda *a, **k: None, warning=lambda *a, **k: None, error=lambda *a, **k: None))
flask_stub.g = SimpleNamespace()

sys.modules['flask'] = flask_stub

# Minimal stubs for extensions and dependencies
sqlalchemy_stub = ModuleType('sqlalchemy')
sqlalchemy_exc_stub = ModuleType('sqlalchemy.exc')
class _SAError(Exception):
    pass
sqlalchemy_exc_stub.SQLAlchemyError = _SAError
sqlalchemy_stub.exc = sqlalchemy_exc_stub
sqlalchemy_stub.func = lambda *a, **k: None
sys.modules['sqlalchemy'] = sqlalchemy_stub
sys.modules['sqlalchemy.exc'] = sqlalchemy_exc_stub

werkzeug_security_stub = ModuleType('werkzeug.security')
werkzeug_security_stub.check_password_hash = lambda p, h: True
sys.modules['werkzeug.security'] = werkzeug_security_stub
sys.modules['flask_sqlalchemy'] = ModuleType('flask_sqlalchemy')
sys.modules['flask_sqlalchemy'].SQLAlchemy = object
sys.modules['flask_migrate'] = ModuleType('flask_migrate')
sys.modules['flask_migrate'].Migrate = object
sys.modules['flask_login'] = ModuleType('flask_login')
sys.modules['flask_login'].LoginManager = object

extensions_stub = ModuleType('extensions')
extensions_stub.db = SimpleNamespace()
extensions_stub.migrate = SimpleNamespace()
sys.modules['extensions'] = extensions_stub

# stub models
models_stub = ModuleType('models')
class _DummyModel(SimpleNamespace):
    pass
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
class DummyQuery:
    def __init__(self):
        self._token = None
    def filter_by(self, token=None, **_):
        self._token = token
        return self
    def first(self):
        if self._token == 'abc123':
            return SimpleNamespace(id=1, token='abc123', rubro=None, nombre_empresa='Demo')
        return None

models_stub.Conversacion = _DummyModel
models_stub.PymeTicket = _DummyModel
models_stub.MunicipioTicket = _DummyModel
models_stub.TicketComentario = _DummyModel
models_stub.PymePedido = _DummyModel
models_stub.Rubro = _DummyModel
models_stub.SitioWebInfo = _DummyModel
models_stub.User = _DummyModel
models_stub.User.query = DummyQuery()
models_stub.CatalogoItem = _DummyModel
models_stub.CatalogoEmbedding = _DummyModel
models_stub.QA = _DummyModel
models_stub.db = SimpleNamespace(session=_DummySession())
sys.modules['models'] = models_stub

from routes import auth

class TokenDecoratorTests(unittest.TestCase):
    def setUp(self):
        auth.User.query = DummyQuery()

    def call(self, headers=None, query=None, json_body=None, form=None):
        request_obj.headers = headers or {}
        request_obj.args = query or {}
        request_obj.form = form or {}
        request_obj._json = json_body

        @auth.token_requerido
        def dummy(user):
            return {"id": user.id}, 200

        result = dummy()
        return result

    def test_token_from_header(self):
        resp, code = self.call(headers={"Authorization": "Bearer abc123"})
        self.assertEqual(code, 200)

    def test_token_from_query(self):
        resp, code = self.call(query={"token": "abc123"})
        self.assertEqual(code, 200)

    def test_missing_token(self):
        resp, code = self.call()
        self.assertEqual(code, 401)

if __name__ == '__main__':
    unittest.main()
