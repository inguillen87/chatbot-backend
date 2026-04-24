import os
import unittest
from unittest.mock import patch

os.environ.setdefault("FLASK_SKIP_GLOBAL_APP", "1")

from app import create_app, db
from config import Config
from models import TenantProfile, User
from routes import carrito
from utils.auth_helpers import generar_token


class TestConfig(Config):
    TESTING = True
    SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"
    WTF_CSRF_ENABLED = False
    SQLALCHEMY_ENGINE_OPTIONS = {"connect_args": {"check_same_thread": False}}


class CarritoTenantResolutionTest(unittest.TestCase):
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

    def test_resolve_owner_from_user_tenant_slug(self):
        """El carrito debe respetar tenant_slug del usuario autenticado."""

        citizen = User(
            name="Ciudadano",
            email="ciudadano@example.com",
            password_hash="hash",
            rol="usuario",
            tenant_slug=self.tenant.slug,
        )
        db.session.add(citizen)
        db.session.commit()

        token = generar_token(
            citizen.id,
            citizen.rol,
            citizen.tipo_chat,
            getattr(citizen, "municipio_id", None),
            getattr(citizen, "pyme_id", None),
        )

        with self.app.test_request_context(
            "/carrito",
            headers={"Authorization": f"Bearer {token}"},
        ):
            tenant, owner = carrito._resolve_owner_and_seed()

        self.assertIsNotNone(tenant)
        self.assertIsNotNone(owner)
        self.assertEqual(tenant.id, self.tenant.id)
        self.assertEqual(owner.id, self.owner.id)

    def test_get_or_create_cart_fallback_insert_works_without_channel_column(self):
        with self.app.test_request_context("/carrito", headers={"X-Sales-Channel": "whatsapp"}):
            with patch.object(carrito, "_market_cart_has_channel_column", return_value=False):
                cart = carrito._get_or_create_db_cart(self.tenant, None)

        self.assertIsNotNone(cart)
        self.assertEqual(cart.tenant_id, self.tenant.id)
        self.assertEqual(cart.status, "open")


if __name__ == "__main__":
    unittest.main()
