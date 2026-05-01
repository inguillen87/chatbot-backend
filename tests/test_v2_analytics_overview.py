import os
import unittest
from datetime import datetime, timedelta

import jwt

os.environ.setdefault("FLASK_SKIP_GLOBAL_APP", "1")

from app import create_app, db
from config import Config
from models import TenantProfile, TenantTicket, User


class V2AnalyticsTestConfig(Config):
    TESTING = True
    ENABLE_DEMO_MODE = True
    SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"
    SQLALCHEMY_ENGINE_OPTIONS = {"connect_args": {"check_same_thread": False}}
    ENABLE_RUNTIME_SCHEMA_SYNC = False
    ENABLE_RUNTIME_TENANT_INIT = False


class V2AnalyticsOverviewTest(unittest.TestCase):
    def setUp(self):
        self.app = create_app(V2AnalyticsTestConfig)
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()
        self.client = self.app.test_client()

        self.admin = User(name="analytics-admin", email="analytics@test.com", rol="admin", tenant_slug="analytics-tenant")
        self.admin.set_password("secret123")
        db.session.add(self.admin)
        db.session.flush()
        self.tenant = TenantProfile(slug="analytics-tenant", nombre="Analytics Tenant", tipo="pyme", pyme_id=self.admin.id)
        db.session.add(self.tenant)
        db.session.commit()
        self.admin.tenant_id = self.tenant.id
        db.session.add(self.admin)
        db.session.add(
            TenantTicket(
                tenant_id=self.tenant.id,
                user_id=self.admin.id,
                categoria="soporte",
                descripcion="Ticket abierto",
                estado="nuevo",
                origen="web",
                datos_extra={"title": "Ticket abierto", "priority": "high"},
            )
        )
        db.session.commit()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def _auth(self):
        token = jwt.encode(
            {
                "user_id": self.admin.id,
                "rol": self.admin.rol,
                "tenant_slug": self.admin.tenant_slug,
                "exp": datetime.utcnow() + timedelta(hours=1),
            },
            self.app.config["SECRET_KEY"],
            algorithm="HS256",
        )
        return {"Authorization": f"Bearer {token}"}

    def test_overview_returns_summary_contract(self):
        headers = {**self._auth(), "X-Tenant-Slug": self.tenant.slug, "X-Request-Id": "analytics-contract-1"}
        response = self.client.get("/api/v2/analytics/overview", headers=headers)

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        summary = payload.get("summary") or {}
        self.assertEqual(payload.get("contract_version"), "analytics.overview.v2")
        self.assertEqual(payload.get("request_id"), "analytics-contract-1")
        self.assertEqual(response.headers.get("X-Request-Id"), "analytics-contract-1")
        self.assertIn("conversations", summary)
        self.assertEqual(summary.get("open_tickets"), 1)
        self.assertEqual(summary.get("nps"), 0)
        self.assertEqual(summary.get("csat"), 0)
        self.assertIn("handoff_rate", summary)


if __name__ == "__main__":
    unittest.main()
