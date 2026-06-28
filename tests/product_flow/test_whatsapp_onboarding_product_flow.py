import os
import unittest
from datetime import datetime, timedelta

import jwt

os.environ.setdefault("FLASK_SKIP_GLOBAL_APP", "1")

from app import create_app, db
from config import Config
from models import TenantProfile, User


class ProductFlowWhatsappConfig(Config):
    TESTING = True
    ENABLE_DEMO_MODE = True
    SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"
    SQLALCHEMY_ENGINE_OPTIONS = {"connect_args": {"check_same_thread": False}}
    ENABLE_RUNTIME_SCHEMA_SYNC = False
    ENABLE_RUNTIME_TENANT_INIT = False
    TWILIO_TECH_PROVIDER_LIVE_ENABLED = False


class ProductFlowWhatsappOnboardingTest(unittest.TestCase):
    def setUp(self):
        self.app = create_app(ProductFlowWhatsappConfig)
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()
        self.client = self.app.test_client()

        self.admin = User(
            name="Product Flow",
            email="product-flow@test.com",
            rol="admin",
            tenant_slug="product-flow",
            tipo_chat="pyme",
        )
        self.admin.set_password("secret123")
        db.session.add(self.admin)
        db.session.flush()

        self.tenant = TenantProfile(
            slug="product-flow",
            nombre="Product Flow",
            tipo="pyme",
            pyme_id=self.admin.id,
            plan="full",
        )
        db.session.add(self.tenant)
        db.session.commit()
        self.admin.tenant_id = self.tenant.id
        db.session.add(self.admin)
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
                "tenant_slug": self.tenant.slug,
                "exp": datetime.utcnow() + timedelta(hours=1),
            },
            self.app.config["SECRET_KEY"],
            algorithm="HS256",
        )
        return {"Authorization": f"Bearer {token}", "X-Tenant-Slug": self.tenant.slug}

    def test_provider_status_smoke_uses_readiness_checks(self):
        contract = self.client.get(
            f"/api/v2/tenants/{self.tenant.slug}/whatsapp/tech-provider",
            headers=self._auth(),
        )
        self.assertEqual(contract.status_code, 200, contract.get_json())
        contract_payload = contract.get_json()
        self.assertEqual(contract_payload["contract_version"], "twilio.tech_provider.v1")
        self.assertEqual(contract_payload["provider"], "twilio_tech_provider")
        self.assertFalse(contract_payload["automation"]["customer_sees_twilio_console"])
        self.assertFalse(contract_payload["frontend_contract"]["show_manual_console_steps"])
        self.assertTrue(contract_payload["smoke_playbook"]["safe_by_default"])

        status = self.client.get(
            f"/api/v2/tenants/{self.tenant.slug}/integrations/whatsapp/status",
            headers=self._auth(),
        )
        self.assertEqual(status.status_code, 200, status.get_json())
        status_payload = status.get_json()
        self.assertEqual(status_payload["contract_version"], "provider.platform_status.v1")
        self.assertEqual(status_payload["frontend_contract"]["render_as"], "whatsapp_provider_status")
        readiness_checks = status_payload["readiness_checks"]
        self.assertGreater(len(readiness_checks), 0)
        self.assertIn("platform_credentials", {item["id"] for item in readiness_checks})

        smoke = self.client.post(
            f"/api/v2/tenants/{self.tenant.slug}/whatsapp/tech-provider/smoke-test/provider_status",
            json={},
            headers=self._auth(),
        )
        self.assertEqual(smoke.status_code, 200, smoke.get_json())
        smoke_payload = smoke.get_json()
        self.assertEqual(smoke_payload["contract_version"], "twilio.tech_provider.smoke_execution.v1")
        self.assertEqual(smoke_payload["details"]["checks_total"], len(readiness_checks))
        self.assertGreater(smoke_payload["details"]["checks_total"], 0)
        self.assertEqual(
            smoke_payload["details"]["readiness_check_ids"],
            [item["id"] for item in readiness_checks if item.get("id")],
        )
