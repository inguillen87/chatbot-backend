import os
import unittest

from flask import g
from werkzeug.exceptions import HTTPException

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("CORS_ALLOWED_ORIGINS", "https://example.com")

from app import create_app
from config import TestingConfig
from middleware import require_tenant
from models import TenantProfile, User, db
from utils.errors import ApiError


@unittest.skipIf(create_app is None, "Flask not available")
class TenantContextResolutionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = create_app(TestingConfig)
        cls.ctx = cls.app.app_context()
        cls.ctx.push()
        db.create_all()

        admin = User(
            name="Admin",
            email="admin@example.com",
            password_hash="hash",
            rol="admin",
        )
        db.session.add(admin)
        db.session.flush()

        cls.tenant = TenantProfile(
            slug="demo",
            nombre="Demo Tenant",
            tipo="municipio",
            municipio_id=admin.id,
        )
        db.session.add(cls.tenant)
        db.session.commit()

    @classmethod
    def tearDownClass(cls):
        db.session.remove()
        db.drop_all()
        cls.ctx.pop()

    def setUp(self):
        # The class intentionally reuses one app context; clear request-local
        # tenant state so one resolution assertion cannot satisfy the next.
        for key in (
            "tenant_profile",
            "tenant_profile_slug",
            "current_tenant",
            "current_tenant_slug",
            "tenant_slug",
            "tenant",
        ):
            g.pop(key, None)

    def test_resolves_tenant_from_header_slug(self):
        with self.app.test_request_context(
            "/api/pwa/public/catalogo", headers={"X-Tenant": "demo"}
        ):
            tenant = require_tenant()
            slug = getattr(g, "tenant_profile_slug", None)

            self.assertEqual(tenant.id, self.tenant.id)
            self.assertEqual(slug, "demo")

    def test_resolves_tenant_from_public_api_path(self):
        with self.app.test_request_context("/api/pwa/public/demo/productos"):
            tenant = require_tenant()
            self.assertEqual(tenant.slug, "demo")

    def test_resolves_tenant_from_short_alias_path(self):
        with self.app.test_request_context("/m/demo/encuestas"):
            tenant = require_tenant()
            self.assertEqual(tenant.slug, "demo")

    def test_resolves_tenant_from_marketplace_path(self):
        with self.app.test_request_context("/market/demo/catalog"):
            tenant = require_tenant()
            self.assertEqual(tenant.slug, "demo")

    def test_resolves_tenant_from_forwarded_host_mapping(self):
        self.app.config["TENANT_DOMAIN_MAP"] = {"custom.chatboc.ar": "demo"}
        with self.app.test_request_context("/", headers={"X-Forwarded-Host": "custom.chatboc.ar"}):
            tenant = require_tenant()

        self.assertEqual(tenant.slug, "demo")

    def test_unknown_tenant_slug_fails_closed(self):
        with self.app.test_request_context("/api/pwa/public/unknown/encuestas"):
            with self.assertRaises(ApiError):
                require_tenant()

    def test_unknown_custom_host_does_not_fall_back_to_first_available(self):
        with self.app.test_request_context("https://unknown.customer.example/"):
            with self.assertRaises(ApiError):
                require_tenant()

    def test_fallback_to_first_tenant_when_no_hints(self):
        with self.app.test_request_context("/"):
            tenant = require_tenant()

        self.assertEqual(tenant.slug, self.tenant.slug)

    def test_resolves_tenant_from_view_args(self):
        with self.app.test_request_context("/api/demo/productos") as ctx:
            ctx.request.view_args = {"tenant_slug": "demo"}
            tenant = require_tenant()

            self.assertEqual(tenant.slug, "demo")

    def test_resolves_tenant_from_query_params(self):
        with self.app.test_request_context("/productos?tenant=demo"):
            tenant = require_tenant()

            self.assertEqual(tenant.slug, "demo")

    def test_pwa_endpoint_uses_query_params_not_path_segment(self):
        with self.app.test_request_context(
            "/api/pwa/anon-id?tenant_slug=demo&tenant=demo"
        ):
            tenant = require_tenant()

            self.assertEqual(tenant.slug, "demo")


if __name__ == "__main__":
    unittest.main()
