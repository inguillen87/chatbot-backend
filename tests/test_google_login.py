import unittest
from types import SimpleNamespace
from unittest.mock import patch, MagicMock

import sys
from types import ModuleType
import importlib


# Crear stubs mínimos de models y sqlalchemy antes de importar
models_stub = ModuleType('models')
class DummyUser(SimpleNamespace):
    def set_password(self, *a, **k):
        pass
models_stub.User = DummyUser
models_stub.db = SimpleNamespace(session=SimpleNamespace(add=lambda *a, **k: None, commit=lambda: None))

sqlalchemy_stub = ModuleType('sqlalchemy')
sqlalchemy_exc_stub = ModuleType('sqlalchemy.exc')
sqlalchemy_stub.exc = sqlalchemy_exc_stub

with patch.dict(sys.modules, {'models': models_stub, 'sqlalchemy': sqlalchemy_stub, 'sqlalchemy.exc': sqlalchemy_exc_stub}):

    import services.google_auth as gauth
    importlib.reload(gauth)

class GoogleLoginTests(unittest.TestCase):
    @patch.object(gauth.id_token, 'verify_oauth2_token')
    def test_crea_usuario_nuevo(self, mock_verify):


        dummy_user = SimpleNamespace(id=1, email='new@example.com', name='Nuevo', token='tok')
        query = MagicMock()
        query.first.return_value = None
        UserMock = MagicMock()
        UserMock.query.filter_by.return_value = query
        UserMock.return_value = dummy_user
        dummy_user.set_password = lambda *a, **k: None

        session = MagicMock()
        with patch.object(gauth, 'User', UserMock), patch.object(gauth, 'db', SimpleNamespace(session=session)):
            user = gauth.login_o_crear_usuario('idtoken')

        self.assertEqual(user, dummy_user)
        session.add.assert_called_once_with(dummy_user)
        session.commit.assert_called_once()

    @patch.object(gauth.id_token, 'verify_oauth2_token')
    def test_usa_usuario_existente(self, mock_verify):


        existing = SimpleNamespace(id=2, email='exist@example.com', name='Exist', token='tok2')
        query = MagicMock()
        query.first.return_value = existing
        UserMock = MagicMock()
        UserMock.query.filter_by.return_value = query

        session = MagicMock()
        with patch.object(gauth, 'User', UserMock), patch.object(gauth, 'db', SimpleNamespace(session=session)):
            user = gauth.login_o_crear_usuario('idtoken')

        self.assertEqual(user, existing)
        session.add.assert_not_called()
        session.commit.assert_not_called()


if __name__ == '__main__':
    unittest.main()
