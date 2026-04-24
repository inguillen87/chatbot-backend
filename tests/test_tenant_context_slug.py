import os
import unittest

os.environ.setdefault("FLASK_SKIP_GLOBAL_APP", "1")

from flask import request

from app import create_app
from config import Config
from middleware.tenant_context import _tenant_slug_from_path


class TestConfig(Config):
    TESTING = True
    SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"
    WTF_CSRF_ENABLED = False
    SQLALCHEMY_ENGINE_OPTIONS = {"connect_args": {"check_same_thread": False}}


class TenantContextSlugExtractionTest(unittest.TestCase):
    def setUp(self):
        self.app = create_app(TestConfig)
        self.app_context = self.app.app_context()
        self.app_context.push()

    def tearDown(self):
        self.app_context.pop()

    def test_api_municipal_prefix_does_not_resolve_as_slug(self):
        with self.app.test_request_context("/api/municipal/usuarios"):
            slug = _tenant_slug_from_path(request.path)

        self.assertIsNone(slug)

    def test_api_slug_resolution_still_works_for_dynamic_slugs(self):
        with self.app.test_request_context("/api/ciudad-junin/productos"):
            slug = _tenant_slug_from_path(request.path)

        self.assertEqual(slug, "ciudad-junin")


if __name__ == "__main__":
    unittest.main()
