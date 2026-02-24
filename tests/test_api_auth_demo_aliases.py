import os
import unittest

os.environ.setdefault("FLASK_SKIP_GLOBAL_APP", "1")

from app import create_app, db
from config import Config


class TestConfig(Config):
    TESTING = True
    SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"
    WTF_CSRF_ENABLED = False
    SQLALCHEMY_ENGINE_OPTIONS = {"connect_args": {"check_same_thread": False}}


class ApiAuthDemoAliasesTest(unittest.TestCase):
    def setUp(self):
        self.app = create_app(TestConfig)
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()
        self.client = self.app.test_client()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def test_api_auth_demo_catalog_alias(self):
        resp = self.client.get('/api/auth/demo/catalog?tenant_slug=junin-1&tenant=junin-1')
        self.assertEqual(resp.status_code, 200)
        payload = resp.get_json()
        self.assertTrue(payload.get('demo_login_enabled'))
        self.assertEqual(payload.get('demo_login_endpoint'), '/auth/demo')

    def test_api_auth_demo_login_alias(self):
        resp = self.client.post('/api/auth/demo', json={'rubro': 'municipio'})
        self.assertEqual(resp.status_code, 200)
        payload = resp.get_json()
        self.assertTrue(payload.get('demo_mode'))
        self.assertIn('tenant_slug', payload)


if __name__ == '__main__':
    unittest.main()
