import os
import unittest
from unittest.mock import patch

os.environ.setdefault("FLASK_SKIP_GLOBAL_APP", "1")

from app import create_app, db
from config import Config
from models import TenantProfile, User
from routes import productos
from utils.auth_helpers import generar_token


class TestConfig(Config):
    TESTING = True
    SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"
    WTF_CSRF_ENABLED = False
    SQLALCHEMY_ENGINE_OPTIONS = {"connect_args": {"check_same_thread": False}}


class ProductosTenantResolutionTest(unittest.TestCase):
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

    def test_resolve_public_owner_from_auth_token_when_no_hints(self):
        """Debe resolver tenant usando el JWT aun sin headers de tenant."""

        with self.app.test_request_context(
            "/productos",
            headers={"Authorization": f"Bearer {self.auth_token}"},
        ):
            tenant, owner = productos._resolve_public_owner(require_explicit=True)

        self.assertIsNotNone(tenant)
        self.assertIsNotNone(owner)
        self.assertEqual(tenant.id, self.tenant.id)
        self.assertEqual(owner.id, self.owner.id)

    def test_resolve_public_owner_requires_hint_when_anonymous(self):
        """Sin token ni hints no debe devolver un tenant por defecto."""

        with self.app.test_request_context("/productos"):
            tenant, owner = productos._resolve_public_owner(require_explicit=True)

        self.assertIsNone(tenant)
        self.assertIsNone(owner)

    def test_resolve_public_owner_from_user_tenant_slug(self):
        """Debe usar tenant_slug del usuario cuando no tiene tenant_profile asociado."""

        citizen = User(
            name="Ciudadano", email="ciudadano@example.com", password_hash="hash", rol="usuario"
        )
        citizen.tenant_slug = self.tenant.slug
        db.session.add(citizen)
        db.session.commit()

        token = generar_token(
            citizen.id, citizen.rol, citizen.tipo_chat, getattr(citizen, "municipio_id", None), getattr(citizen, "pyme_id", None)
        )

        with self.app.test_request_context(
            "/productos",
            headers={"Authorization": f"Bearer {token}"},
        ):
            tenant, owner = productos._resolve_public_owner(require_explicit=True)

        self.assertIsNotNone(tenant)
        self.assertIsNotNone(owner)
        self.assertEqual(tenant.id, self.tenant.id)
        self.assertEqual(owner.id, self.owner.id)

    def test_resolve_public_owner_falls_back_to_resolver_when_anonymous(self):
        """Debe degradar al resolver común aun sin hints cuando se permite público."""

        with patch("routes.productos.resolve_tenant_only") as mock_resolver:
            mock_resolver.return_value = self.tenant

            with self.app.test_request_context("/productos"):
                tenant, owner = productos._resolve_public_owner(require_explicit=False)

        mock_resolver.assert_called_once()
        self.assertIsNotNone(tenant)
        self.assertIsNotNone(owner)
        self.assertEqual(tenant.id, self.tenant.id)
        self.assertEqual(owner.id, self.owner.id)

    def test_resolve_public_owner_falls_back_when_resolver_returns_ownerless(self):
        """Si el resolver devuelve un tenant sin owner, debe degradar a uno válido."""

        ownerless_tenant = TenantProfile(slug="ownerless", nombre="Sin owner", tipo="municipio")

        with patch("routes.productos.resolve_tenant_only") as mock_resolver:
            mock_resolver.return_value = ownerless_tenant

            with self.app.test_request_context("/productos"):
                tenant, owner = productos._resolve_public_owner(require_explicit=False)

        mock_resolver.assert_called_once()
        self.assertIsNotNone(tenant)
        self.assertIsNotNone(owner)
        self.assertEqual(tenant.id, self.tenant.id)
        self.assertEqual(owner.id, self.owner.id)

    def test_resolve_public_owner_from_market_referrer(self):
        """Debe extraer el slug desde rutas tipo /market/<slug>/... del referer."""

        with self.app.test_request_context(
            "/productos",
            headers={"Referer": "https://www.chatboc.ar/market/municipalidad-de-junin/cart"},
        ):
            tenant, owner = productos._resolve_public_owner(require_explicit=False)

        self.assertIsNotNone(tenant)
        self.assertIsNotNone(owner)
        self.assertEqual(tenant.id, self.tenant.id)
        self.assertEqual(owner.id, self.owner.id)


if __name__ == "__main__":
    unittest.main()
