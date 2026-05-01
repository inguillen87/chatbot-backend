import os
import unittest
import copy
from datetime import datetime, timedelta, timezone

import jwt

os.environ.setdefault("FLASK_SKIP_GLOBAL_APP", "1")

from app import create_app, db
from config import Config
from models import TenantProfile, TenantTicket, User
from services.v2.sla_service import is_ticket_overdue


class V2TicketsTestConfig(Config):
    TESTING = True
    ENABLE_DEMO_MODE = True
    SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"
    SQLALCHEMY_ENGINE_OPTIONS = {"connect_args": {"check_same_thread": False}}
    ENABLE_RUNTIME_SCHEMA_SYNC = False
    ENABLE_RUNTIME_TENANT_INIT = False


class V2TicketsApiTest(unittest.TestCase):
    def setUp(self):
        self.app = create_app(V2TicketsTestConfig)
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()
        self.client = self.app.test_client()

        self.admin = self._create_user("admin@t1.test", "admin", tenant_slug="tenant-1", tenant_id=None)
        self.tenant_1 = TenantProfile(slug="tenant-1", nombre="Tenant 1", tipo="pyme", pyme_id=self.admin.id)
        db.session.add(self.tenant_1)
        db.session.commit()
        self.admin.tenant_id = self.tenant_1.id
        db.session.add(self.admin)

        self.employee = self._create_user("empleado@t1.test", "empleado", tenant_slug="tenant-1", tenant_id=self.tenant_1.id)
        self.end_user = self._create_user("usuario@t1.test", "usuario", tenant_slug="tenant-1", tenant_id=self.tenant_1.id)

        self.admin_2 = self._create_user("admin@t2.test", "admin", tenant_slug="tenant-2", tenant_id=None)
        self.tenant_2 = TenantProfile(slug="tenant-2", nombre="Tenant 2", tipo="pyme", pyme_id=self.admin_2.id)
        db.session.add(self.tenant_2)
        db.session.commit()
        self.admin_2.tenant_id = self.tenant_2.id
        db.session.add(self.admin_2)
        db.session.commit()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def _create_user(self, email: str, rol: str, tenant_slug: str | None, tenant_id: int | None):
        user = User(name=email.split("@")[0], email=email, rol=rol, tenant_slug=tenant_slug, tenant_id=tenant_id)
        user.set_password("secret123")
        db.session.add(user)
        db.session.flush()
        return user

    def _auth_header(self, user: User):
        token = jwt.encode(
            {
                "user_id": user.id,
                "rol": user.rol,
                "tenant_slug": user.tenant_slug,
                "exp": datetime.utcnow() + timedelta(hours=1),
            },
            self.app.config["SECRET_KEY"],
            algorithm="HS256",
        )
        return {"Authorization": f"Bearer {token}"}

    def test_create_ticket_with_valid_tenant(self):
        headers = {**self._auth_header(self.employee), "X-Tenant-Slug": "tenant-1"}
        resp = self.client.post(
            "/api/v2/tickets",
            json={"title": "Luz", "description": "Farola rota", "priority": "high", "channel": "web"},
            headers=headers,
        )
        self.assertEqual(resp.status_code, 201)
        payload = resp.get_json()
        self.assertEqual(payload.get("tenant_id"), self.tenant_1.id)

    def test_create_ticket_without_tenant_fails(self):
        headers = self._auth_header(self.employee)
        resp = self.client.post(
            "/api/v2/tickets",
            json={"title": "Luz", "description": "Farola rota"},
            headers=headers,
        )
        self.assertEqual(resp.status_code, 400)

    def test_list_does_not_mix_tenants(self):
        headers_t1 = {**self._auth_header(self.employee), "X-Tenant-Slug": "tenant-1"}
        headers_t2 = {**self._auth_header(self.admin_2), "X-Tenant-Slug": "tenant-2"}

        self.client.post(
            "/api/v2/tickets",
            json={"title": "A", "description": "A", "assignee_id": self.employee.id},
            headers=headers_t1,
        )
        self.client.post("/api/v2/tickets", json={"title": "B", "description": "B"}, headers=headers_t2)

        resp = self.client.get("/api/v2/tickets", headers={**headers_t1, "X-Request-Id": "tickets-contract-1"})
        self.assertEqual(resp.status_code, 200)
        payload = resp.get_json() or {}
        items = payload.get("items") or []
        self.assertEqual(payload.get("contract_version"), "tickets.v2.list")
        self.assertEqual(payload.get("request_id"), "tickets-contract-1")
        self.assertEqual(resp.headers.get("X-Request-Id"), "tickets-contract-1")
        self.assertTrue(all(item.get("tenant_id") == self.tenant_1.id for item in items))
        self.assertTrue(all("sla_status" in item for item in items))
        self.assertTrue(all("sla_state" in item for item in items))
        self.assertTrue(any((item.get("assignee") or {}).get("id") == self.employee.id for item in items))
        self.assertTrue(any(item.get("assignee_name") == self.employee.name for item in items))

    def test_patch_status_generates_event(self):
        headers = {**self._auth_header(self.employee), "X-Tenant-Slug": "tenant-1"}
        created = self.client.post("/api/v2/tickets", json={"title": "S", "description": "S"}, headers=headers).get_json()
        ticket_id = created["id"]

        patch = self.client.patch(f"/api/v2/tickets/{ticket_id}", json={"status": "in_progress"}, headers=headers)
        self.assertEqual(patch.status_code, 200)

        events_resp = self.client.get(f"/api/v2/tickets/{ticket_id}/events", headers=headers)
        events = (events_resp.get_json() or {}).get("items") or []
        types = [e.get("event_type") for e in events]
        self.assertIn("ticket.status_changed", types)

    def test_internal_comment_hidden_for_end_user(self):
        admin_headers = {**self._auth_header(self.admin), "X-Tenant-Slug": "tenant-1"}
        user_headers = {**self._auth_header(self.end_user), "X-Tenant-Slug": "tenant-1"}
        created = self.client.post("/api/v2/tickets", json={"title": "C", "description": "C"}, headers=user_headers).get_json()
        ticket_id = created["id"]

        comment_resp = self.client.post(
            f"/api/v2/tickets/{ticket_id}/comments",
            json={"body": "Nota interna", "visibility": "internal"},
            headers=admin_headers,
        )
        self.assertEqual(comment_resp.status_code, 201)

        listed = self.client.get("/api/v2/tickets", headers=user_headers).get_json()
        items = listed.get("items") or []
        target = next(item for item in items if item["id"] == ticket_id)
        self.assertEqual(target.get("comments"), [])

    def test_sla_due_date_is_computed(self):
        headers = {**self._auth_header(self.employee), "X-Tenant-Slug": "tenant-1"}
        created = self.client.post(
            "/api/v2/tickets",
            json={"title": "SLA", "description": "SLA", "priority": "medium"},
            headers=headers,
        )
        payload = created.get_json()
        sla = payload.get("sla") or {}
        self.assertTrue(sla.get("resolution_due_at"))

    def test_breach_detection_finds_overdue_ticket(self):
        headers = {**self._auth_header(self.employee), "X-Tenant-Slug": "tenant-1"}
        created = self.client.post("/api/v2/tickets", json={"title": "B", "description": "B"}, headers=headers).get_json()
        ticket = TenantTicket.query.get(created["id"])
        extra = copy.deepcopy(ticket.datos_extra or {})
        extra.setdefault("sla", {})
        past_due = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
        extra["sla"]["resolution_due_at"] = past_due
        extra["sla"]["next_update_due_at"] = past_due
        ticket.datos_extra = extra
        ticket.estado = "nuevo"
        db.session.add(ticket)
        db.session.commit()
        ticket = TenantTicket.query.get(ticket.id)
        self.assertTrue(is_ticket_overdue(ticket))

        resp = self.client.get("/api/v2/sla/breaches", headers=headers)
        self.assertEqual(resp.status_code, 200)
        items = (resp.get_json() or {}).get("items") or []
        self.assertTrue(any(item.get("ticket_id") == ticket.id for item in items))


if __name__ == "__main__":
    unittest.main()
