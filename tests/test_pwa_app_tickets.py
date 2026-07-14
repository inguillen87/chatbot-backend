import unittest
import sys
import os
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from flask import g

project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from app import create_app, db  # noqa: E402
from config import Config  # noqa: E402
from models import TenantFollower, TenantProfile, TenantTicket, User  # noqa: E402
from routes.pwa_app import list_tickets  # noqa: E402
from utils.auth_helpers import generar_token  # noqa: E402


class TestConfig(Config):
    TESTING = True
    SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"
    WTF_CSRF_ENABLED = False


class PwaAppTicketsTest(unittest.TestCase):
    def setUp(self):
        self.app = create_app(TestConfig)
        self.app_context = self.app.app_context()
        self.app_context.push()
        db.create_all()

        self.owner = User(name="Owner", email="owner@example.com", password_hash="hash")
        self.viewer = User(name="Viewer", email="viewer@example.com", password_hash="hash")
        db.session.add_all([self.owner, self.viewer])
        db.session.commit()

        self.tenant = TenantProfile(
            slug="demo-tenant",
            nombre="Demo Tenant",
            tipo="municipio",
            municipio_id=self.owner.id,
        )
        db.session.add(self.tenant)
        db.session.commit()

        db.session.add(
            TenantFollower(
                user_id=self.viewer.id,
                tenant_id=self.tenant.id,
                notifications_enabled=True,
            )
        )
        db.session.commit()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.app_context.pop()

    def _create_ticket(self, **overrides):
        now = datetime.now(timezone.utc)
        ticket = TenantTicket(
            tenant_id=self.tenant.id,
            user_id=overrides.get("user_id", self.viewer.id),
            categoria=overrides.get("categoria"),
            descripcion=overrides.get("descripcion", "Descripción"),
            estado=overrides.get("estado", "nuevo"),
            origen=overrides.get("origen", "pwa"),
            latitud=overrides.get("latitud"),
            longitud=overrides.get("longitud"),
            datos_extra=overrides.get("datos_extra"),
            fingerprint=overrides.get("fingerprint"),
        )
        ticket.created_at = overrides.get("created_at", now)
        ticket.updated_at = overrides.get("updated_at", ticket.created_at)
        db.session.add(ticket)
        db.session.commit()
        return ticket

    def test_list_tickets_authenticated_returns_summary_and_pagination(self):
        base_time = datetime.now(timezone.utc)
        self._create_ticket(descripcion="Alumbrado", estado="nuevo", created_at=base_time - timedelta(days=2))
        self._create_ticket(descripcion="Bacheo", estado="cerrado", created_at=base_time - timedelta(days=1))
        self._create_ticket(descripcion="Residuos", estado="en_proceso", created_at=base_time)

        with self.app.test_request_context(
            f"/api/pwa/app/tickets?tenant_id={self.tenant.id}&per_page=2&page=1"
        ):
            g.tenant_profile = self.tenant
            g.viewer = self.viewer

            response = list_tickets()

        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertEqual(data["pagination"]["total_items"], 3)
        self.assertTrue(data["pagination"]["has_next"])
        self.assertFalse(data["pagination"]["has_prev"])
        self.assertEqual(len(data["tickets"]), 2)
        self.assertEqual(data["summary"]["nuevo"], 1)
        self.assertEqual(data["summary"]["resuelto"], 1)
        self.assertEqual(data["summary"]["en_proceso"], 1)
        self.assertEqual(data["summary"]["total"], 3)

    def test_list_tickets_uses_fingerprint_for_anonymous_users(self):
        fingerprint = "fp-test"
        self._create_ticket(
            descripcion="Recolección",
            estado="nuevo",
            fingerprint=fingerprint,
            user_id=None,
        )

        with patch("routes.pwa_app.hash_fingerprint", return_value=fingerprint):
            with self.app.test_request_context(f"/api/pwa/app/tickets?tenant_id={self.tenant.id}"):
                g.tenant_profile = self.tenant
                g.viewer = None

                response = list_tickets()

        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertEqual(data["summary"]["total"], 1)
        self.assertEqual(len(data["tickets"]), 1)
        self.assertEqual(data["tickets"][0]["descripcion"], "Recolección")

    def test_followed_tenants_accepts_first_party_panel_cookie(self):
        token = generar_token(
            self.viewer.id,
            self.viewer.rol,
            None,
            None,
            None,
        )
        client = self.app.test_client()
        client.set_cookie("auth_token", token)

        response = client.get(
            "/api/pwa/app/me/tenants",
            headers={"Origin": "https://www.chatboc.ar"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()[0]["slug"], self.tenant.slug)


if __name__ == "__main__":
    unittest.main()
