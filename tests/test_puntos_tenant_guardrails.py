import os
import unittest

os.environ.setdefault("FLASK_SKIP_GLOBAL_APP", "1")

from app import create_app, db
from config import Config
from models import TenantProfile, User


class TestConfig(Config):
    TESTING = True
    SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"
    WTF_CSRF_ENABLED = False
    SQLALCHEMY_ENGINE_OPTIONS = {"connect_args": {"check_same_thread": False}}


class PuntosTenantGuardrailsTest(unittest.TestCase):
    def setUp(self):
        self.app = create_app(TestConfig)
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()

        self.owner = User(
            name="Demo Municipio",
            email="demo-municipio@example.com",
            password_hash="hash",
            tipo_chat="municipio",
            rol="admin",
        )
        db.session.add(self.owner)
        db.session.commit()

        self.tenant = TenantProfile(
            slug="demo-municipio",
            nombre="Demo Municipio",
            tipo="municipio",
            municipio_id=self.owner.id,
        )
        db.session.add(self.tenant)
        db.session.commit()

        self.client = self.app.test_client()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def test_saldo_requires_resolvable_tenant(self):
        resp = self.client.get("/api/puntos/saldo")
        self.assertEqual(resp.status_code, 404)
        payload = resp.get_json()
        self.assertIn("error", payload)

    def test_historial_requires_resolvable_tenant(self):
        resp = self.client.get("/api/puntos/historial")
        self.assertEqual(resp.status_code, 404)
        payload = resp.get_json()
        self.assertIn("error", payload)

    def test_saldo_with_valid_tenant_slug_works(self):
        resp = self.client.get("/api/puntos/saldo", query_string={"tenant": self.tenant.slug})
        self.assertEqual(resp.status_code, 200)
        payload = resp.get_json()
        self.assertEqual(payload["tenant_id"], self.tenant.id)
        self.assertEqual(payload["saldo"], 0)


if __name__ == "__main__":
    unittest.main()
