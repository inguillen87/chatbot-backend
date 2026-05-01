import os
import unittest

os.environ.setdefault("FLASK_SKIP_GLOBAL_APP", "1")

from app import create_app, db
from config import Config
from models import TenantProfile, User


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
        self.assertEqual(payload.get("contract_version"), "demo.catalog.v2")
        self.assertEqual(payload.get("sectors"), ["gobierno", "empresas"])
        self.assertIn("rubros", payload)
        self.assertIn("sector_groups", payload)

        flattened = str(payload).lower()
        self.assertNotIn("password", flattened)
        self.assertNotIn("token", flattened)
        self.assertNotIn("secret", flattened)

    def test_v2_demo_session_returns_workspace_contract(self):
        owner = User(name="Demo Pyme", email="demo-pyme@test.com", password_hash="hash", tipo_chat="pyme")
        db.session.add(owner)
        db.session.flush()
        tenant = TenantProfile(slug="demo-pyme", nombre="Demo Pyme", tipo="pyme", pyme_id=owner.id)
        db.session.add(tenant)
        db.session.commit()

        resp = self.client.post(
            "/api/v2/demo/session",
            json={"sector": "empresas", "tenant_slug": tenant.slug},
            headers={"X-Request-Id": "demo-contract-1"},
        )

        self.assertEqual(resp.status_code, 200)
        payload = resp.get_json()
        workspace = payload.get("workspace") or {}
        self.assertEqual(payload.get("contract_version"), "demo.session.v2")
        self.assertEqual(payload.get("tenant_slug"), tenant.slug)
        self.assertEqual(payload.get("request_id"), "demo-contract-1")
        self.assertEqual(resp.headers.get("X-Request-Id"), "demo-contract-1")
        self.assertEqual(workspace.get("title"), tenant.nombre)
        self.assertIsInstance(workspace.get("quick_replies"), list)
        self.assertTrue(all(isinstance(item, dict) and item.get("label") for item in workspace.get("quick_replies")))
        self.assertIsInstance(workspace.get("value_cards"), list)
        self.assertIsInstance(workspace.get("handoff_labels"), dict)
        self.assertIn("createTicket", workspace.get("handoff_labels"))

    def test_v2_tenant_route_requires_explicit_tenant_context(self):
        resp = self.client.get("/api/v2/tenants/current")
        self.assertEqual(resp.status_code, 400)

    def test_v2_login_invalid_credentials_returns_401(self):
        resp = self.client.post(
            "/api/v2/auth/login",
            json={"email": "nadie@example.com", "password": "incorrecta"},
        )
        self.assertEqual(resp.status_code, 401)

    def test_v2_refresh_invalid_token_returns_401(self):
        resp = self.client.post("/api/v2/auth/refresh", json={"token": "invalid-token"})
        self.assertEqual(resp.status_code, 401)

    def test_prod_with_default_secret_fails_fast(self):
        with self.assertRaises(RuntimeError):
            create_app(BadProdConfig)


if __name__ == "__main__":
    unittest.main()
