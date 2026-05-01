import os
import unittest
from datetime import datetime, timedelta

import jwt

os.environ.setdefault("FLASK_SKIP_GLOBAL_APP", "1")

from app import create_app, db
from config import Config
from models import Notification, NotificationTemplate, TenantProfile, TenantTicket, User


class V2SaasTestConfig(Config):
    TESTING = True
    ENABLE_DEMO_MODE = True
    SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"
    SQLALCHEMY_ENGINE_OPTIONS = {"connect_args": {"check_same_thread": False}}
    ENABLE_RUNTIME_SCHEMA_SYNC = False
    ENABLE_RUNTIME_TENANT_INIT = False


class V2SaasContractsTest(unittest.TestCase):
    def setUp(self):
        self.app = create_app(V2SaasTestConfig)
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()
        self.client = self.app.test_client()

        self.owner = User(name="Owner SaaS", email="owner-saas@test.com", rol="admin", tenant_slug="saas-tenant")
        self.owner.set_password("secret123")
        db.session.add(self.owner)
        db.session.flush()

        self.tenant = TenantProfile(
            slug="saas-tenant",
            nombre="SaaS Tenant",
            tipo="pyme",
            pyme_id=self.owner.id,
            configuracion={"widget_tokens": ["widget-saas"], "mercadopago_access_token": "mp-token"},
            whatsapp_sender_id="whatsapp:+100",
        )
        db.session.add(self.tenant)
        db.session.commit()
        self.owner.tenant_id = self.tenant.id
        db.session.add(self.owner)

        self.employee = User(
            name="Mesa de entrada",
            email="mesa@test.com",
            rol="empleado",
            tenant_slug=self.tenant.slug,
            tenant_id=self.tenant.id,
            es_empleado=True,
            accesibilidad={
                "employee_scope": {
                    "categorias": ["educacion"],
                    "zonas": ["centro"],
                    "permisos": ["tickets_assign"],
                    "channels": ["whatsapp"],
                }
            },
        )
        self.employee.set_password("secret123")
        db.session.add(self.employee)
        db.session.flush()

        self.ticket = TenantTicket(
            tenant_id=self.tenant.id,
            user_id=self.owner.id,
            categoria="educacion",
            descripcion="Consulta por beca",
            estado="nuevo",
            origen="whatsapp",
            datos_extra={
                "title": "Consulta por beca",
                "priority": "high",
                "assignee_id": self.employee.id,
                "zone": "centro",
                "channel": "whatsapp",
                "comments": [{"id": 1, "body": "Hola", "visibility": "public", "created_at": "2026-05-01T12:00:00Z"}],
            },
        )
        db.session.add(self.ticket)

        template = NotificationTemplate(
            tenant_id=self.tenant.id,
            key="ticket_created",
            channel="email",
            subject_template="Nuevo ticket",
            body_template="Ticket {{id}}",
        )
        db.session.add(template)
        db.session.add(
            Notification(
                tenant_id=self.tenant.id,
                user_id=self.owner.id,
                template_id=template.id,
                channel="email",
                recipient="ops@test.com",
                subject="Nuevo ticket",
                body="Ticket creado",
                status="sent",
                idempotency_key="notif-1",
            )
        )

        self.super_admin = User(name="Super", email="super@test.com", rol="super_admin")
        self.super_admin.set_password("secret123")
        db.session.add(self.super_admin)
        db.session.commit()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def _auth(self, user: User):
        token = jwt.encode(
            {
                "user_id": user.id,
                "rol": user.rol,
                "tenant_slug": getattr(user, "tenant_slug", None),
                "exp": datetime.utcnow() + timedelta(hours=1),
            },
            self.app.config["SECRET_KEY"],
            algorithm="HS256",
        )
        return {"Authorization": f"Bearer {token}", "X-Tenant-Slug": self.tenant.slug}

    def test_employee_coverage_contract(self):
        response = self.client.get(
            "/api/v2/employee-coverage",
            headers={**self._auth(self.owner), "X-Request-Id": "coverage-1"},
        )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload.get("contract_version"), "employee.coverage.v1")
        self.assertEqual(payload.get("request_id"), "coverage-1")
        self.assertEqual(payload["tenant"]["slug"], self.tenant.slug)
        self.assertGreaterEqual(payload["summary"]["coverage_rate"], 0)
        self.assertTrue(payload["employees"])
        self.assertIn("educacion", payload["coverage"]["categorias"])

    def test_tenant_health_contract(self):
        response = self.client.get("/api/v2/tenant-health", headers=self._auth(self.owner))

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload.get("contract_version"), "tenant.health.v1")
        self.assertIn(payload["health"]["status"], {"healthy", "warning", "critical"})
        self.assertIn("integrations", payload)
        self.assertIn("queues", payload)
        self.assertIn("recommended_actions", payload)

    def test_superadmin_executive_summary_contract(self):
        response = self.client.get(
            "/api/v2/superadmin/executive-summary",
            headers=self._auth(self.super_admin),
        )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload.get("contract_version"), "superadmin.executive_summary.v1")
        self.assertEqual(payload["summary"]["tenants"], 1)
        self.assertIn("tenant_health", payload)

    def test_notification_hooks_contract_and_update(self):
        response = self.client.post(
            "/api/v2/notifications/hooks",
            json={
                "preferences": {"email": True, "whatsapp": False},
                "triggers": [{"event": "ticket.created", "channel": "email"}],
                "delivery": {"quiet_hours": {"start": 22, "end": 7}},
            },
            headers=self._auth(self.owner),
        )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload.get("contract_version"), "notifications.hooks.v1")
        self.assertTrue(payload["preferences"]["email"])
        self.assertTrue(payload["templates"])
        self.assertEqual(payload["delivery_status"]["totals"]["sent"], 1)

    def test_omnichannel_inbox_contract(self):
        response = self.client.get("/api/v2/inbox/omnichannel", headers=self._auth(self.owner))

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload.get("contract_version"), "inbox.omnichannel.v1")
        self.assertEqual(payload["summary"]["total"], 1)
        self.assertEqual(payload["items"][0]["channel"], "whatsapp")
        self.assertTrue(payload["items"][0]["timeline"])

    def test_omnichannel_inbox_action_updates_ticket(self):
        response = self.client.post(
            f"/api/v2/inbox/omnichannel/{self.ticket.id}/actions",
            json={"action": "reply", "body": "Estamos revisando tu caso.", "visibility": "public"},
            headers={**self._auth(self.owner), "X-Request-Id": "inbox-action-1"},
        )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload.get("contract_version"), "inbox.omnichannel.action.v1")
        self.assertEqual(payload.get("request_id"), "inbox-action-1")
        self.assertTrue(payload["ticket"]["timeline"])
        self.assertTrue(any(item.get("body") == "Estamos revisando tu caso." for item in payload["ticket"]["timeline"]))


if __name__ == "__main__":
    unittest.main()
