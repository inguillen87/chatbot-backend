import unittest
from types import SimpleNamespace
from unittest.mock import patch
from app import create_app, db
from config import Config
from models import User

from routes.auth import actualizar_me

class TestConfig(Config):
    TESTING = True
    SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"
    WTF_CSRF_ENABLED = False

class DummySession:
    def commit(self):
        pass
    def rollback(self):
        pass

def make_user():
    return User(
        rol='usuario',
        empresa_id=None,
        name='Juan',
        email='juan@test.com',
        password_hash='somehash'
    )

class ProfileUpdateSecurityTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app(TestConfig)
        self.app_context = self.app.app_context()
        self.app_context.push()
        db.create_all()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.app_context.pop()

    def test_cannot_change_role_or_empresa(self):
        user = make_user()
        db.session.add(user)
        db.session.commit()

        data = {
            'rol': 'empleado',
            'role': 'empleado',
            'empresa_id': 123,
            'name': 'Nuevo'
        }
        with self.app.test_request_context(json=data, method='POST'), patch('routes.auth.db', SimpleNamespace(session=db.session)):
            resp = actualizar_me(user)
        self.assertEqual(resp.get_json()["mensaje"], "Perfil actualizado correctamente.")
        self.assertEqual(user.rol, 'usuario')
        self.assertEqual(user.empresa_id, None)
        self.assertEqual(user.name, 'Nuevo')

if __name__ == '__main__':
    unittest.main()
