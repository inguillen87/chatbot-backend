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

    def test_missing_tenant_aborts_with_json_payload(self):
        with self.app.test_request_context("/api/pwa/public/unknown/encuestas"):
            with self.assertRaises(HTTPException) as ctx:
                require_tenant()

            self.assertEqual(ctx.exception.code, 404)
            self.assertEqual(ctx.exception.response.get_json(), {"error": "Tenant no especificado o no encontrado"})

    def test_resolves_tenant_from_view_args(self):
        with self.app.test_request_context("/api/demo/productos") as ctx:
            ctx.request.view_args = {"tenant_slug": "demo"}
            tenant = require_tenant()

            self.assertEqual(tenant.slug, "demo")


if __name__ == "__main__":
    unittest.main()
