import os
import unittest

os.environ.setdefault("FLASK_SKIP_GLOBAL_APP", "1")

from app import create_app, db
from config import Config


class V2BaseTestConfig(Config):
    TESTING = True
    ENABLE_DEMO_MODE = True
    SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"
    SQLALCHEMY_ENGINE_OPTIONS = {"connect_args": {"check_same_thread": False}}
    ENABLE_RUNTIME_SCHEMA_SYNC = False
    ENABLE_RUNTIME_TENANT_INIT = False


class BadProdConfig(V2BaseTestConfig):
    ENV = "prod"
    SECRET_KEY = "una-llave-secreta-muy-segura-para-desarrollo-local"
    DEBUG = False


class ApiV2FoundationTest(unittest.TestCase):
    def setUp(self):
        self.app = create_app(V2BaseTestConfig)
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()
        self.client = self.app.test_client()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def test_v2_health_ok(self):
        resp = self.client.get("/api/v2/health")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json(), {"ok": True, "version": "v2"})

    def test_v2_demo_catalog_does_not_expose_sensitive_keys(self):
        resp = self.client.get("/api/v2/demo/catalog")
        self.assertEqual(resp.status_code, 200)
        payload = resp.get_json()
        self.assertIn("sectors", payload)

        flattened = str(payload).lower()
        self.assertNotIn("password", flattened)
        self.assertNotIn("token", flattened)
        self.assertNotIn("secret", flattened)

    def test_v2_tenant_route_requires_explicit_tenant_context(self):
        resp = self.client.get("/api/v2/tenants/current")
        self.assertEqual(resp.status_code, 400)

    def test_v2_login_invalid_credentials_returns_401(self):
        resp = self.client.post(
            "/api/v2/auth/login",
            json={"email": "nadie@example.com", "password": "incorrecta"},
        )
        self.assertEqual(resp.status_code, 401)

    def test_prod_with_default_secret_fails_fast(self):
        with self.assertRaises(RuntimeError):
            create_app(BadProdConfig)


if __name__ == "__main__":
    unittest.main()
