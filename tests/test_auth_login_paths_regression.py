import os
import unittest

os.environ.setdefault("FLASK_SKIP_GLOBAL_APP", "1")

from app import create_app, db
from config import Config
from models import Rubro, TenantProfile, User
from utils.auth_helpers import generar_token


class TestConfig(Config):
    TESTING = True
    SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"
    WTF_CSRF_ENABLED = False
    SQLALCHEMY_ENGINE_OPTIONS = {"connect_args": {"check_same_thread": False}}


class AuthLoginPathRegressionTest(unittest.TestCase):
    def setUp(self):
        self.app = create_app(TestConfig)
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()

        rubro = Rubro(clave="servicios", nombre="Servicios")
        db.session.add(rubro)
        db.session.flush()

        self.owner = User(
            name="Owner Municipio",
            email="owner-muni@test.com",
            rol="admin",
            tipo_chat="municipio",
            token="owner-public-token",
            rubro_id=rubro.id,
        )
        self.owner.set_password("secret")
        db.session.add(self.owner)
        db.session.flush()

        self.tenant = TenantProfile(
            slug="muni-demo",
            nombre="Municipio Demo",
            tipo="municipio",
            municipio_id=self.owner.id,
        )
        db.session.add(self.tenant)
        db.session.flush()

        self.user = User(
            name="Operador",
            email="operator@test.com",
            rol="usuario",
            tipo_chat="municipio",
            empresa_id=self.owner.id,
            rubro_id=rubro.id,
        )
        self.user.set_password("password123")
        db.session.add(self.user)
        db.session.commit()

        self.owner_jwt = generar_token(
            self.owner.id,
            self.owner.rol,
            self.owner.tipo_chat,
            getattr(self.owner, "municipio_id", None),
            getattr(self.owner, "pyme_id", None),
        )

        self.client = self.app.test_client()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def test_widget_login_uses_owner_tenant_for_municipio_scope(self):
        resp = self.client.post(
            "/auth/widget/login",
            json={"email": self.user.email, "password": "password123"},
            headers={"Authorization": f"Bearer {self.owner_jwt}"},
        )
        self.assertEqual(resp.status_code, 200)
        payload = resp.get_json()
        self.assertIn("token", payload)

    def test_chatuser_login_panel_uses_owner_tenant_for_municipio_scope(self):
        resp = self.client.post(
            "/auth/chatuserloginpanel",
            json={
                "empresa_token": self.owner.token,
                "email": self.user.email,
                "password": "password123",
            },
        )
        self.assertEqual(resp.status_code, 200)
        payload = resp.get_json()
        self.assertEqual(payload.get("tenant_slug"), self.tenant.slug)
        self.assertIn("token", payload)


if __name__ == "__main__":
    unittest.main()
