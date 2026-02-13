import json
import os
import unittest

os.environ.setdefault("FLASK_SKIP_GLOBAL_APP", "1")

from app import create_app, db
from config import Config
from models import TenantProfile, User
from utils.auth_helpers import generar_token


class TestConfig(Config):
    TESTING = True
    SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"
    WTF_CSRF_ENABLED = False
    SQLALCHEMY_ENGINE_OPTIONS = {"connect_args": {"check_same_thread": False}}


class AdminMercadoPagoIntegrationTest(unittest.TestCase):
    def setUp(self):
        self.app = create_app(TestConfig)
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()

        self.user = User(
            name="Admin Tenant",
            email="admin-tenant@example.com",
            password_hash="hash",
            rol="admin",
            tipo_chat="pyme",
        )
        db.session.add(self.user)
        db.session.commit()

        self.tenant = TenantProfile(
            slug="tenant-mp",
            nombre="Tenant MP",
            tipo="pyme",
            pyme_id=self.user.id,
            plan="full",
        )
        db.session.add(self.tenant)
        db.session.commit()

        self.user.tenant_id = self.tenant.id
        db.session.commit()

        self.token = generar_token(self.user.id, self.user.rol, self.user.tipo_chat, self.user.municipio_id, self.user.pyme_id)
        self.client = self.app.test_client()
        self.headers = {"Authorization": f"Bearer {self.token}"}

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def test_set_and_get_mercadopago_credentials(self):
        save_resp = self.client.post(
            f"/api/admin/tenants/{self.tenant.slug}/integrations/mercadopago",
            data=json.dumps({"access_token": "APP_USR-tenant-secret-token"}),
            content_type="application/json",
            headers=self.headers,
        )
        self.assertEqual(save_resp.status_code, 200)
        save_payload = save_resp.get_json()
        self.assertTrue(save_payload["configured"])
        self.assertIn("...", save_payload["access_token_masked"])

        get_resp = self.client.get(
            f"/api/admin/tenants/{self.tenant.slug}/integrations/mercadopago",
            headers=self.headers,
        )
        self.assertEqual(get_resp.status_code, 200)
        get_payload = get_resp.get_json()
        self.assertTrue(get_payload["configured"])
        self.assertNotIn("tenant-secret-token", str(get_payload))

    def test_test_connection_updates_status(self):
        self.tenant.configuracion = {"mercadopago_access_token": "APP_USR-tenant-secret-token"}
        db.session.commit()

        def fake_get(url, headers=None, timeout=None):  # noqa: ARG001
            class DummyResponse:
                ok = True

                def json(self):
                    return {"id": 123, "site_id": "MLA", "email": "owner@example.com"}

            return DummyResponse()

        import routes.admin_tenant as admin_tenant_module

        original_get = admin_tenant_module.requests.get
        admin_tenant_module.requests.get = fake_get
        try:
            resp = self.client.post(
                f"/api/admin/tenants/{self.tenant.slug}/integrations/mercadopago/test",
                headers=self.headers,
            )
        finally:
            admin_tenant_module.requests.get = original_get

        self.assertEqual(resp.status_code, 200)
        payload = resp.get_json()
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["status"], "ok")


if __name__ == "__main__":
    unittest.main()
