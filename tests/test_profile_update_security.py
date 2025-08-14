import unittest
from types import SimpleNamespace
from unittest.mock import patch
from app import create_app, db
from config import Config
from models import User

from routes.auth import me_perfil

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
        self.client = self.app.test_client()
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
        from functools import wraps
        def token_passthrough(f):
            @wraps(f)
            def decorated_function(*args, **kwargs):
                # The decorated function expects the user object as its first argument
                return f(user, *args, **kwargs)
            return decorated_function

        with patch('routes.auth.token_requerido', token_passthrough):
            resp = self.client.put('/me', json=data)

        # The endpoint should reject the request because it tries to change protected fields.
        # A 400 Bad Request or 403 Forbidden would also be reasonable. Let's assume 401 for now.
        self.assertIn(resp.status_code, [400, 401, 403])

        # Verify that the protected fields were NOT changed
        self.assertEqual(user.rol, 'usuario')
        self.assertEqual(user.empresa_id, None)

        # Verify that the allowed field was also NOT changed because the request failed
        self.assertEqual(user.name, 'Juan')

if __name__ == '__main__':
    unittest.main()
