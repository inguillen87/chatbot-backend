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


class PublicResolverTest(unittest.TestCase):
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
        self.owner.token = "owner-latest-token"
        db.session.add(self.owner)
        db.session.commit()

        self.tenant = TenantProfile(
            slug="municipalidad-de-junin",
            nombre="Municipalidad de Junín",
            tipo="municipio",
            municipio_id=self.owner.id,
            logo_url="https://example.com/logo.png",
            configuracion={"widget_tokens": "demo-token"},
        )
        db.session.add(self.tenant)
        db.session.commit()

        self.client = self.app.test_client()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.app_context.pop()

    def test_public_tenant_profile_by_slug(self):
        response = self.client.get(f"/api/public/tenant-profile?tenant={self.tenant.slug}")
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["tenant"]["slug"], self.tenant.slug)
        self.assertEqual(payload["tenant"]["logo_url"], self.tenant.logo_url)
        self.assertIn("config", payload["tenant"])

    def test_public_tenant_profile_by_widget_token(self):
        response = self.client.get(
            "/api/public/tenant-profile",
            headers={"X-Widget-Token": "demo-token"},
        )
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["tenant"]["slug"], self.tenant.slug)

    def test_public_tenant_profile_prefers_widget_token_over_host(self):
        other_owner = User(
            name="Otra Empresa",
            email="otra@example.com",
            password_hash="hash",
            tipo_chat="pyme",
        )
        db.session.add(other_owner)
        db.session.commit()

        other_tenant = TenantProfile(
            slug="catalogo-demo",
            nombre="Catálogo Demo",
            tipo="pyme",
            pyme_id=other_owner.id,
            dominio="otra-empresa.com",
            configuracion={"widget_tokens": ["otro-token"]},
        )
        db.session.add(other_tenant)
        db.session.commit()

        response = self.client.get(
            "/api/public/tenant-profile?widget_token=otro-token",
            base_url="http://externo.example.com",
        )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["tenant"]["slug"], other_tenant.slug)

    def test_public_tenant_profile_falls_back_to_widget_token_when_slug_invalid(self):
        response = self.client.get(
            "/api/public/tenant-profile?tenant=inexistente",
            headers={"X-Widget-Token": "demo-token"},
        )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["tenant"]["slug"], self.tenant.slug)
        self.assertIn("warning", payload)

    def test_public_tenant_profile_accepts_owner_token_header(self):
        response = self.client.get(
            "/api/public/tenant-profile?tenant=inexistente",
            headers={"X-Owner-Token": "demo-token"},
        )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["tenant"]["slug"], self.tenant.slug)

    def test_public_tenant_profile_uses_widget_cookie(self):
        self.client.set_cookie("widget_token", "demo-token")

        response = self.client.get("/api/public/tenant-profile?tenant=inexistente")

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["tenant"]["slug"], self.tenant.slug)
        self.assertIn("warning", payload)

    def test_public_tenant_profile_accepts_bearer_owner_token(self):
        response = self.client.get(
            "/api/public/tenant-profile?tenant=inexistente",
            headers={"Authorization": "Bearer demo-token"},
        )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["tenant"]["slug"], self.tenant.slug)

    def test_public_tenant_profile_reserved_slug(self):
        response = self.client.get("/api/public/tenant-profile?tenant=iframe")
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["tenant"]["slug"], self.tenant.slug)
        self.assertIn("warning", payload)
        self.assertIn("reserved", payload["warning"].get("message", ""))

    def test_widget_token_registers_on_resolution(self):
        """Ensure new widget tokens get persisted in tenant configuration."""

        self.tenant.configuracion = {}
        db.session.commit()

        response = self.client.post(
            "/api/public/resolve-tenant",
            json={"tenant_slug": self.tenant.slug, "widget_token": "fresh-token"},
        )

        self.assertEqual(response.status_code, 200)
        db.session.refresh(self.tenant)

        tokens = self.tenant.configuracion.get("widget_tokens")
        if isinstance(tokens, list):
            self.assertIn("fresh-token", tokens)
        else:
            self.assertEqual(tokens, "fresh-token")

    def test_public_tenant_profile_not_found(self):
        response = self.client.get("/api/public/tenant-profile?tenant=desconocido")
        self.assertEqual(response.status_code, 404)
        payload = response.get_json()
        self.assertIn("error", payload)

    def test_tenant_profile_returns_canonical_token_and_cookie(self):
        response = self.client.get(
            f"/api/public/tenant-profile?tenant={self.tenant.slug}"
        )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload.get("widget_token"), self.owner.token)

        widget_cookie_name = self.app.config.get("WIDGET_TOKEN_COOKIE_NAME", "widget_token")
        cookie_headers = response.headers.getlist("Set-Cookie")
        self.assertTrue(
            any(
                header.startswith(f"{widget_cookie_name}={self.owner.token}")
                for header in cookie_headers
            )
        )

    def test_tenant_profile_replaces_outdated_widget_token(self):
        # Simular una rotación: el widget envía un token viejo que ya no está en la config
        self.tenant.configuracion = {"widget_tokens": ["rotated-token"]}
        db.session.commit()

        response = self.client.get(
            "/api/public/tenant-profile",
            headers={"X-Widget-Token": "demo-token"},
        )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload.get("widget_token"), self.owner.token)


if __name__ == "__main__":
    unittest.main()
