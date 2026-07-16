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

    def test_explicit_tenant_id_wins_over_conflicting_referrer(self):
        response = self.client.get(
            f"/api/pwa/public/cart/url?tenant_id={self.tenant.id}",
            headers={"Referer": "https://www.chatboc.ar/t/otro-tenant/market"},
        )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["tenant_slug"], self.tenant.slug)

    def test_widget_token_requires_an_exact_match_and_does_not_mutate_tenant(self):
        self.tenant.configuracion = {"widget_tokens": ["demo-anon-production"]}
        db.session.commit()

        response = self.client.get("/api/pwa/public/cart/url?widget_token=demo-anon")

        self.assertEqual(response.status_code, 404)
        db.session.refresh(self.tenant)
        self.assertEqual(self.tenant.configuracion["widget_tokens"], ["demo-anon-production"])

    def test_invalid_explicit_slug_returns_actionable_json(self):
        response = self.client.get("/api/pwa/public/cart/url?tenant=slug-inexistente")
        self.assertEqual(response.status_code, 404)
        payload = response.get_json()
        self.assertEqual(payload["contract_version"], "pwa.public_tenant_resolution.v1")
        self.assertEqual(payload["reason_code"], "tenant_resolution_failed")
        self.assertEqual(payload["status_code"], 404)
        self.assertFalse(payload["retryable"])
        self.assertIn("query_params", payload["hints"])
        self.assertIn("accepted_query_params", payload["hints"])

    def test_canonical_tenant_info_returns_contract(self):
        response = self.client.get(
            f"/api/pwa/public/tenant-info?tenant={self.tenant.slug}",
            headers={"X-Request-Id": "tenant-info-contract-1"},
        )
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["contract_version"], "public.tenant_profile.v1")
        self.assertEqual(payload["request_id"], "tenant-info-contract-1")
        self.assertEqual(payload["tenant"]["slug"], self.tenant.slug)
        self.assertIn("public_cart_url", payload["tenant"])

    def test_public_tenant_widget_config_returns_frontend_contract(self):
        response = self.client.get(f"/api/public/tenants/{self.tenant.slug}/widget-config")
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["contract_version"], "public.widget_config.v1")
        self.assertEqual(payload["tenant"]["slug"], self.tenant.slug)
        self.assertIsInstance(payload["widget"], dict)
        self.assertIsInstance(payload["builder_config"], dict)
        self.assertIsInstance(payload["quick_menu"], list)
        self.assertIn("quick_menu", payload["builder_config"])
        self.assertFalse(payload["suppress_global_widget"])
        self.assertFalse(payload["integration_preview"])


if __name__ == "__main__":
    unittest.main()
