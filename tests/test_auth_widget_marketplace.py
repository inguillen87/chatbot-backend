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


class WidgetAuthMarketplaceTest(unittest.TestCase):
    def setUp(self):
        self.app = create_app(TestConfig)
        self.app_context = self.app.app_context()
        self.app_context.push()
        db.create_all()

        self.owner = User(
            name="Owner",
            email="owner@example.com",
            password_hash="hash",
            tipo_chat="pyme",
            rol="admin",
        )
        db.session.add(self.owner)
        db.session.commit()

        self.tenant = TenantProfile(
            slug="demo-tenant",
            nombre="Demo Tenant",
            tipo="pyme",
            pyme_id=self.owner.id,
            configuracion={"public_cart_prefix": "m"},
        )
        db.session.add(self.tenant)
        db.session.commit()

        self.login_user = User(
            name="Login User",
            email="login@example.com",
            empresa_id=self.owner.id,
            tipo_chat="pyme",
            rol="usuario",
        )
        self.login_user.set_password("password123")
        db.session.add(self.login_user)
        db.session.commit()

        self.auth_token = generar_token(
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
        self.app_context.pop()

    def test_widget_register_returns_marketplace_and_sets_tenant(self):
        payload = {"name": "Client", "email": "client@example.com", "password": "secret"}
        response = self.client.post(
            "/auth/widget/register",
            json=payload,
            headers={"Authorization": f"Bearer {self.auth_token}"},
        )

        self.assertEqual(response.status_code, 201)
        data = response.get_json()
        self.assertEqual(data.get("tenant_id"), self.tenant.id)
        self.assertEqual(data.get("tenant_slug"), self.tenant.slug)
        self.assertIn("marketplace", data)
        self.assertTrue(data["marketplace"].get("enabled"))
        self.assertIn(str(self.tenant.slug), data["marketplace"].get("public_cart_url", ""))

        created = User.query.filter_by(email="client@example.com").first()
        self.assertIsNotNone(created)
        self.assertEqual(getattr(created, "tenant_slug", None), self.tenant.slug)

    def test_widget_login_includes_marketplace_and_tenant(self):
        response = self.client.post(
            "/auth/widget/login",
            json={"email": self.login_user.email, "password": "password123"},
            headers={"Authorization": f"Bearer {self.auth_token}"},
        )

        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertEqual(data.get("tenant_id"), self.tenant.id)
        self.assertEqual(data.get("tenant_slug"), self.tenant.slug)
        self.assertIn("marketplace", data)
        self.assertTrue(data["marketplace"].get("enabled"))

        refreshed_user = User.query.get(self.login_user.id)
        self.assertEqual(getattr(refreshed_user, "tenant_slug", None), self.tenant.slug)


if __name__ == "__main__":
    unittest.main()
