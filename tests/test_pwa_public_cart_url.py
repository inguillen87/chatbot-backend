import unittest

from app import create_app, db
from config import Config
from models import TenantProfile, User


class TestConfig(Config):
    TESTING = True
    SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"
    WTF_CSRF_ENABLED = False
    SQLALCHEMY_ENGINE_OPTIONS = {"connect_args": {"check_same_thread": False}}


class PublicCartUrlTest(unittest.TestCase):
    def setUp(self):
        self.app = create_app(TestConfig)
        self.app_context = self.app.app_context()
        self.app_context.push()
        db.create_all()

        self.owner = User(
            name="Municipalidad de Junín",
            email="junin@example.com",
            password_hash="hash",
            tipo_chat="municipio",
        )
        db.session.add(self.owner)
        db.session.commit()

        self.tenant = TenantProfile(
            slug="municipalidad-de-junin",
            nombre="Municipalidad de Junín",
            tipo="municipio",
            municipio_id=self.owner.id,
        )
        db.session.add(self.tenant)
        db.session.commit()

        self.client = self.app.test_client()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.app_context.pop()

    def test_default_cart_url_uses_slug_and_localhost(self):
        response = self.client.get(f"/api/pwa/public/cart/url?tenant_id={self.tenant.id}")
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["path"], f"m/{self.tenant.slug}")
        self.assertEqual(payload["cart_url"], f"http://localhost/m/{self.tenant.slug}")

    def test_cart_url_respects_custom_domain_and_path(self):
        self.tenant.dominio = "munijunin.gob.ar"
        self.tenant.configuracion = {"public_cart_path": "beneficios/junin"}
        db.session.commit()

        response = self.client.get(f"/api/pwa/public/cart/url?tenant_id={self.tenant.id}")
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["path"], "beneficios/junin")
        self.assertEqual(payload["cart_url"], "http://munijunin.gob.ar/beneficios/junin")

    def test_cart_url_allows_full_override(self):
        self.tenant.configuracion = {"public_cart_url": "https://white.label/app"}
        db.session.commit()

        response = self.client.get(f"/api/pwa/public/cart/url?tenant_id={self.tenant.id}")
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["path"], "override")
        self.assertEqual(payload["cart_url"], "https://white.label/app")

    def test_cart_url_resolves_via_widget_token(self):
        self.tenant.configuracion = {"widget_tokens": ["demo-anon"]}
        db.session.commit()

        response = self.client.get("/api/pwa/public/cart/url?widget_token=demo-anon")
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["tenant_slug"], self.tenant.slug)
        self.assertIn(self.tenant.slug, payload["cart_url"])


if __name__ == "__main__":
    unittest.main()
