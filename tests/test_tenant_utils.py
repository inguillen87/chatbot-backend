import os
import unittest
from unittest.mock import patch

os.environ.setdefault("FLASK_SKIP_GLOBAL_APP", "1")

from app import create_app, db
from config import Config
from models import TenantProfile, User
from services.tenant_resolver import TenantResolutionError
from utils import tenant as tenant_utils


class TestConfig(Config):
    TESTING = True
    SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"
    WTF_CSRF_ENABLED = False
    SQLALCHEMY_ENGINE_OPTIONS = {"connect_args": {"check_same_thread": False}}


class TenantUtilsFallbackTest(unittest.TestCase):
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
            rol="admin",
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

    def test_get_current_tenant_falls_back_to_first_when_resolver_fails(self):
        """Debe devolver algún tenant aun si el resolver principal falla."""

        self.app.config["PUBLIC_CATALOG_DEFAULT_TENANT"] = None
        with patch(
            "services.tenant_resolver.resolve_tenant_only",
            side_effect=TenantResolutionError("sin tenant"),
        ) as mock_resolver:
            with self.app.test_request_context("/productos"):
                tenant = tenant_utils.get_current_tenant_profile()

        mock_resolver.assert_called_once()
        self.assertIsNotNone(tenant)

    def test_get_current_tenant_can_disable_global_fallback(self):
        """Los modulos operativos pueden rechazar tenants ambiguos."""

        self.app.config["PUBLIC_CATALOG_DEFAULT_TENANT"] = None
        with patch(
            "services.tenant_resolver.resolve_tenant_only",
            side_effect=TenantResolutionError("sin tenant"),
        ) as mock_resolver:
            with self.app.test_request_context("/productos"):
                tenant = tenant_utils.get_current_tenant_profile(allow_fallback=False)

        mock_resolver.assert_called_once()
        self.assertIsNone(tenant)


if __name__ == "__main__":
    unittest.main()
