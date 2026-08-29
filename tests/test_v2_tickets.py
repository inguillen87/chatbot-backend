import os
import unittest
import copy
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import jwt

os.environ.setdefault("FLASK_SKIP_GLOBAL_APP", "1")

from app import create_app, db
from config import Config
from models import AnalyticsEventV2, TenantProfile, TenantTicket, User
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
        self.employee.ticket_categorias = "general"
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
        self.assertEqual(payload.get("category"), "general")

    def test_create_ticket_cannot_skip_the_initial_workflow_state(self):
        cases = [
            (self.end_user, "cerrado"),
            (self.admin, "en_vivo"),
            (self.employee, "en_proceso"),
        ]
        for index, (actor, status) in enumerate(cases, start=1):
            with self.subTest(role=actor.rol, status=status):
                response = self.client.post(
                    "/api/v2/tickets",
                    json={
                        "title": f"Salto inicial {index}",
                        "description": "Debe comenzar como nuevo",
                        "category": "general",
                        "status": status,
                    },
                    headers={
                        **self._auth_header(actor),
                        "X-Tenant-Slug": "tenant-1",
                    },
                )

                self.assertEqual(response.status_code, 422, response.get_json())
                payload = response.get_json() or {}
                self.assertEqual(payload.get("reason_code"), "ticket_initial_state_not_allowed")
                self.assertEqual(payload.get("action_hint"), "create_ticket_as_new")

        malformed = self.client.post(
            "/api/v2/tickets",
            json={
                "title": "Estado vacío",
                "description": "No debe interpretarse como omisión",
                "status": "",
            },
            headers={
                **self._auth_header(self.end_user),
                "X-Tenant-Slug": "tenant-1",
            },
        )
        self.assertEqual(malformed.status_code, 422, malformed.get_json())
        self.assertEqual(
            malformed.get_json()["reason_code"],
            "ticket_initial_state_invalid",
        )
        self.assertEqual(TenantTicket.query.filter_by(tenant_id=self.tenant_1.id).count(), 0)

    def test_create_ticket_without_tenant_fails(self):
        headers = self._auth_header(self.employee)
        resp = self.client.post(
            "/api/v2/tickets",
            json={"title": "Luz", "description": "Farola rota"},
            headers=headers,
        )
        self.assertEqual(resp.status_code, 400)
        payload = resp.get_json() or {}
        self.assertEqual(payload.get("contract_version"), "shared.error.v1")
        self.assertEqual(payload.get("reason_code"), "missing_tenant")
        self.assertTrue(payload.get("request_id"))

    def test_list_tickets_requires_authenticated_tenant_member(self):
        no_token = self.client.get("/api/v2/tickets", headers={"X-Tenant-Slug": "tenant-1"})
        self.assertEqual(no_token.status_code, 401)
        self.assertEqual((no_token.get_json() or {}).get("reason_code"), "auth_required")

        cross_tenant = self.client.get(
            "/api/v2/tickets",
            headers={**self._auth_header(self.employee), "X-Tenant-Slug": "tenant-2"},
        )
        self.assertEqual(cross_tenant.status_code, 403)
        self.assertEqual((cross_tenant.get_json() or {}).get("reason_code"), "tenant_access_denied")

    def test_create_ticket_rejects_cross_tenant_assignee_with_json_contract(self):
        headers = {**self._auth_header(self.employee), "X-Tenant-Slug": "tenant-1", "X-Request-Id": "ticket-assignee-1"}
        other_employee = self._create_user("empleado@t2.test", "empleado", tenant_slug="tenant-2", tenant_id=self.tenant_2.id)
        db.session.commit()

        resp = self.client.post(
            "/api/v2/tickets",
            json={"title": "Asignacion", "description": "No debe cruzar tenant", "assignee_id": other_employee.id},
            headers=headers,
        )

        self.assertEqual(resp.status_code, 400)
        payload = resp.get_json() or {}
        self.assertEqual(payload.get("contract_version"), "shared.error.v1")
        self.assertEqual(payload.get("reason_code"), "validation_failed")
        self.assertEqual(payload.get("request_id"), "ticket-assignee-1")

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
        created = self.client.post(
            "/api/v2/tickets",
            json={"title": "S", "description": "S", "assignee_id": self.employee.id},
            headers=headers,
        ).get_json()
        ticket_id = created["id"]

        patch = self.client.patch(f"/api/v2/tickets/{ticket_id}", json={"status": "in_progress"}, headers=headers)
        self.assertEqual(patch.status_code, 200)

        events_resp = self.client.get(f"/api/v2/tickets/{ticket_id}/events", headers=headers)
        events = (events_resp.get_json() or {}).get("items") or []
        types = [e.get("event_type") for e in events]
        self.assertIn("ticket.status_changed", types)

    def test_patch_status_rejects_impossible_transition_with_workflow_contract(self):
        headers = {**self._auth_header(self.admin), "X-Tenant-Slug": "tenant-1"}
        created = self.client.post(
            "/api/v2/tickets",
            json={"title": "Salto", "description": "No debe saltar estados"},
            headers=headers,
        ).get_json()
        ticket_id = created["id"]

        rejected = self.client.patch(
            f"/api/v2/tickets/{ticket_id}",
            json={"status": "en_vivo", "expected_status": "nuevo"},
            headers=headers,
        )
        self.assertEqual(rejected.status_code, 409, rejected.get_json())
        payload = rejected.get_json() or {}
        self.assertEqual(payload.get("contract_version"), "tickets.workflow_transition.v2")
        self.assertEqual(payload.get("reason_code"), "ticket_transition_not_allowed")
        self.assertEqual(payload.get("current_state"), "nuevo")
        self.assertEqual(payload.get("next_states"), ["en_proceso", "cerrado"])
        self.assertEqual(db.session.get(TenantTicket, ticket_id).estado, "nuevo")

    def test_employee_needs_assignment_and_receives_no_impossible_next_states(self):
        headers = {**self._auth_header(self.employee), "X-Tenant-Slug": "tenant-1"}
        created = self.client.post(
            "/api/v2/tickets",
            json={"title": "Sin asignar", "description": "Debe tomarse primero"},
            headers=headers,
        ).get_json()
        ticket_id = created["id"]

        detail = self.client.get(f"/api/v2/tickets/{ticket_id}", headers=headers)
        self.assertEqual(detail.status_code, 200, detail.get_json())
        detail_ticket = (detail.get_json() or {}).get("ticket") or {}
        self.assertEqual(detail_ticket.get("next_states"), [])
        self.assertEqual((detail_ticket.get("workflow") or {}).get("blocked_reason"), "ticket_assignment_required")

        rejected = self.client.patch(
            f"/api/v2/tickets/{ticket_id}",
            json={"status": "en_proceso", "expected_status": "nuevo"},
            headers=headers,
        )
        self.assertEqual(rejected.status_code, 409, rejected.get_json())
        self.assertEqual((rejected.get_json() or {}).get("reason_code"), "ticket_assignment_required")
        self.assertEqual(db.session.get(TenantTicket, ticket_id).estado, "nuevo")

    def test_detail_endpoint_returns_tenant_ticket_contract(self):
        self.employee.ticket_categorias = "general"
        db.session.commit()
        headers = {**self._auth_header(self.employee), "X-Tenant-Slug": "tenant-1"}
        created = self.client.post(
            "/api/v2/tickets",
            json={
                "title": "Detalle",
                "description": "Contrato estable",
                "channel": "widget",
                "category": "general",
            },
            headers=headers,
        ).get_json()
        ticket_id = created["id"]

        detail = self.client.get(f"/api/v2/tickets/{ticket_id}", headers=headers)
        self.assertEqual(detail.status_code, 200)
        payload = detail.get_json() or {}
        self.assertEqual(payload.get("contract_version"), "tickets.v2.detail")
        self.assertEqual(payload.get("source_model"), "TenantTicket")
        self.assertEqual(payload.get("ticket_type"), "tenant_ticket")
        self.assertEqual(payload.get("detail_endpoint"), f"/api/v2/tickets/{ticket_id}")
        self.assertEqual(payload.get("messages_endpoint"), f"/api/v2/tickets/{ticket_id}/messages")
        self.assertEqual(payload.get("timeline_endpoint"), f"/api/v2/tickets/{ticket_id}/timeline")
        self.assertEqual((payload.get("ticket") or {}).get("id"), ticket_id)

        cross_tenant = self.client.get(
            f"/api/v2/tickets/{ticket_id}",
            headers={**self._auth_header(self.admin_2), "X-Tenant-Slug": "tenant-2"},
        )
        self.assertEqual(cross_tenant.status_code, 404)
        self.assertEqual((cross_tenant.get_json() or {}).get("reason_code"), "ticket_not_found")

    def test_assisted_marketplace_ticket_promotes_operational_contract(self):
        self.employee.ticket_categorias = "luminaria"
        db.session.commit()
        headers = {**self._auth_header(self.employee), "X-Tenant-Slug": "tenant-1"}
        ticket = TenantTicket(
            tenant_id=self.tenant_1.id,
            user_id=self.end_user.id,
            categoria="Luminaria",
            descripcion="Luminaria quemada en Don Bosco 55",
            estado="nuevo",
            origen="marketplace",
            datos_extra={
                "title": "Luminaria quemada",
                "contract_version": "marketplace.commerce_intake_ticket.v1",
                "request_kind": "service_request",
                "request_kind_label": "reclamo o solicitud vecinal",
                "channel": "marketplace",
                "contact": {"name": "Marcelo", "phone": "+5492613168608"},
                "address": "Don Bosco 55, Junin",
                "operator_intake_summary": {
                    "contract_version": "marketplace.operator_intake_summary.v1",
                    "target_module": "municipal_claims",
                    "recommended_next_step": "resolver_faltantes_y_responder",
                    "summary": "Vecino informa luminaria quemada con direccion suficiente.",
                    "missing_fields": ["foto_opcional"],
                },
                "crm_handoff": {
                    "target_module": "municipal_claims",
                    "operator_next_steps": [
                        {
                            "id": "assign_area",
                            "label": "Asignar area responsable",
                            "description": "Derivar a alumbrado publico.",
                        }
                    ],
                },
                "operator_pack": {
                    "priority": "high",
                    "suggested_reply": "Recibimos el reclamo y lo derivamos al area responsable.",
                    "contact_links": [
                        {
                            "type": "whatsapp",
                            "label": "Responder por WhatsApp",
                            "href": "https://wa.me/5492613168608",
                        }
                    ],
                },
                "public_follow_up": {
                    "contract_version": "marketplace.assisted_followup.v1",
                    "tracking": {
                        "kind": "claim",
                        "code": "M-123456",
                        "path": "/tracking/claim/M-123456?pin=900144",
                        "label": "Seguimiento de reclamo",
                    },
                    "channels": [
                        {
                            "id": "tracking_page",
                            "label": "Ver reclamo",
                            "type": "link",
                            "href": "/tracking/claim/M-123456?pin=900144",
                        }
                    ],
                },
                "attachments": [
                    {
                        "id": "att-claim-1",
                        "name": "foto.jpg",
                        "url": "https://cdn.example.com/foto.jpg",
                        "mimeType": "image/jpeg",
                    }
                ],
            },
        )
        db.session.add(ticket)
        db.session.commit()

        listed = self.client.get("/api/v2/tickets", headers=headers)
        self.assertEqual(listed.status_code, 200)
        item = next(entry for entry in (listed.get_json() or {}).get("items", []) if entry["id"] == ticket.id)
        self.assertEqual(item["recommended_next_action"], "Resolver faltantes y responder")
        self.assertEqual(item["assisted_request"]["target_module"], "municipal_claims")
        self.assertEqual(item["assisted_request"]["operator_intake_summary"]["recommended_next_step"], "resolver_faltantes_y_responder")
        self.assertEqual(item["public_follow_up"]["tracking"]["code"], "M-123456")
        self.assertEqual(item["ai_operator_brief"]["recommended_next_action"], "Resolver faltantes y responder")
        self.assertEqual(item["ai_operator_brief"]["summary"], "Vecino informa luminaria quemada con direccion suficiente.")
        self.assertTrue(any(step["id"] == "assign_area" for step in item["next_steps"]))
        self.assertTrue(any(action["id"] == "open_public_follow_up" for action in item["allowed_actions"]))
        self.assertTrue(any(action["id"] == "whatsapp" for action in item["allowed_actions"]))
        self.assertTrue(any(action["id"] == "reply" for action in item["allowed_actions"]))
        self.assertEqual(item["attachments"][0]["id"], "att-claim-1")

        detail = self.client.get(f"/api/v2/tickets/{ticket.id}", headers=headers)
        self.assertEqual(detail.status_code, 200)
        detail_ticket = (detail.get_json() or {}).get("ticket") or {}
        self.assertEqual(detail_ticket["assisted_request"]["source_contract_version"], "marketplace.commerce_intake_ticket.v1")
        self.assertEqual(detail_ticket["recommended_next_action"], "Resolver faltantes y responder")

    def test_detail_messages_and_timeline_expose_source_attachments(self):
        self.employee.ticket_categorias = "marketplace_assisted_order"
        db.session.commit()
        headers = {**self._auth_header(self.employee), "X-Tenant-Slug": "tenant-1"}
        attachment = {
            "id": 77,
            "url": "https://cdn.example.com/pedido.jpg",
            "name": "pedido.jpg",
            "mime_type": "image/jpeg",
        }
        ticket = TenantTicket(
            tenant_id=self.tenant_1.id,
            user_id=self.end_user.id,
            categoria="marketplace_assisted_order",
            descripcion="Pedido por foto",
            estado="nuevo",
            origen="whatsapp",
            datos_extra={
                "title": "Pedido asistido",
                "attachments": [attachment],
                "attachmentInfo": attachment,
                "comments": [],
            },
        )
        db.session.add(ticket)
        db.session.commit()

        detail = self.client.get(f"/api/v2/tickets/{ticket.id}", headers=headers)
        self.assertEqual(detail.status_code, 200, detail.get_json())
        detail_payload = detail.get_json() or {}
        self.assertEqual((detail_payload.get("ticket") or {}).get("attachmentInfo", {}).get("url"), attachment["url"])
        self.assertEqual((detail_payload.get("ticket") or {}).get("attachments", [])[0]["name"], "pedido.jpg")

        messages = self.client.get(f"/api/v2/tickets/{ticket.id}/messages", headers=headers)
        self.assertEqual(messages.status_code, 200, messages.get_json())
        message_payload = messages.get_json() or {}
        self.assertEqual((message_payload.get("messages") or [])[0]["attachmentInfo"]["url"], attachment["url"])
        self.assertIn("Adjunto recibido", (message_payload.get("messages") or [])[0]["body"])

        timeline = self.client.get(f"/api/v2/tickets/{ticket.id}/timeline", headers=headers)
        self.assertEqual(timeline.status_code, 200, timeline.get_json())
        timeline_items = (timeline.get_json() or {}).get("timeline") or []
        attachment_events = [item for item in timeline_items if item.get("event_type") == "ticket.attachment_received"]
        self.assertTrue(attachment_events)
        self.assertEqual(attachment_events[0]["attachmentInfo"]["url"], attachment["url"])

    def test_timeline_cursor_pages_more_than_sixty_messages_without_duplicates(self):
        self.employee.ticket_categorias = "general"
        db.session.commit()
        headers = {**self._auth_header(self.employee), "X-Tenant-Slug": "tenant-1"}
        base_time = datetime.now(timezone.utc) + timedelta(minutes=1)
        comments = [
            {
                "id": index,
                "body": f"Mensaje histórico {index:02d}",
                "visibility": "public",
                "author_user_id": self.end_user.id if index % 2 else self.employee.id,
                "created_at": (base_time + timedelta(seconds=index)).isoformat(),
            }
            for index in range(1, 66)
        ]
        ticket = TenantTicket(
            tenant_id=self.tenant_1.id,
            user_id=self.end_user.id,
            categoria="general",
            descripcion="Historial enterprise",
            estado="nuevo",
            origen="whatsapp",
            datos_extra={"title": "Historial enterprise", "comments": comments},
        )
        db.session.add(ticket)
        db.session.commit()

        first = self.client.get(
            f"/api/v2/tickets/{ticket.id}/timeline",
            query_string={"limit": 25},
            headers=headers,
        )
        self.assertEqual(first.status_code, 200, first.get_json())
        first_payload = first.get_json() or {}
        first_items = first_payload.get("unified_conversation_stream") or []
        self.assertEqual(len(first_items), 25)
        self.assertTrue(first_payload.get("has_more"))
        self.assertTrue(first_payload.get("next_cursor"))
        self.assertEqual((first_payload.get("pagination") or {}).get("returned_count"), 25)

        second = self.client.get(
            f"/api/v2/tickets/{ticket.id}/timeline",
            query_string={"limit": 25, "cursor": first_payload["next_cursor"]},
            headers=headers,
        )
        self.assertEqual(second.status_code, 200, second.get_json())
        second_payload = second.get_json() or {}
        second_items = second_payload.get("unified_conversation_stream") or []
        self.assertEqual(len(second_items), 25)
        first_ids = {item["id"] for item in first_items}
        second_ids = {item["id"] for item in second_items}
        self.assertFalse(first_ids & second_ids)
        self.assertLess(second_items[-1]["timestamp"], first_items[0]["timestamp"])
        self.assertEqual(len(first_payload.get("historial_chat") or []), 25)
        self.assertEqual(len(second_payload.get("historial_chat") or []), 25)

        cross_tenant = self.client.get(
            f"/api/v2/tickets/{ticket.id}/timeline",
            query_string={"limit": 25, "cursor": first_payload["next_cursor"]},
            headers={**self._auth_header(self.admin_2), "X-Tenant-Slug": "tenant-2"},
        )
        self.assertEqual(cross_tenant.status_code, 404)
        self.assertEqual((cross_tenant.get_json() or {}).get("reason_code"), "ticket_not_found")

        cross_tenant_invalid_cursor = self.client.get(
            f"/api/v2/tickets/{ticket.id}/timeline",
            query_string={"cursor": "cursor-invalido"},
            headers={**self._auth_header(self.admin_2), "X-Tenant-Slug": "tenant-2"},
        )
        self.assertEqual(cross_tenant_invalid_cursor.status_code, 404)
        self.assertEqual(
            (cross_tenant_invalid_cursor.get_json() or {}).get("reason_code"),
            "ticket_not_found",
        )

        authorized_invalid_cursor = self.client.get(
            f"/api/v2/tickets/{ticket.id}/timeline",
            query_string={"cursor": "cursor-invalido"},
            headers=headers,
        )
        self.assertEqual(authorized_invalid_cursor.status_code, 400)
        self.assertEqual(
            (authorized_invalid_cursor.get_json() or {}).get("reason_code"),
            "invalid_history_pagination",
        )

    def test_customer_cannot_patch_or_read_events(self):
        headers = {**self._auth_header(self.end_user), "X-Tenant-Slug": "tenant-1"}
        created = self.client.post("/api/v2/tickets", json={"title": "Vecino", "description": "Caso"}, headers=headers).get_json()
        ticket_id = created["id"]

        patch = self.client.patch(f"/api/v2/tickets/{ticket_id}", json={"status": "in_progress"}, headers=headers)
        self.assertEqual(patch.status_code, 403)
        self.assertEqual((patch.get_json() or {}).get("reason_code"), "operator_required")

        events = self.client.get(f"/api/v2/tickets/{ticket_id}/events", headers=headers)
        self.assertEqual(events.status_code, 403)
        self.assertEqual((events.get_json() or {}).get("reason_code"), "operator_required")

    def test_patch_assigns_employee_and_blocks_cross_tenant_employee(self):
        headers = {**self._auth_header(self.employee), "X-Tenant-Slug": "tenant-1"}
        created = self.client.post("/api/v2/tickets", json={"title": "A", "description": "A"}, headers=headers).get_json()
        ticket_id = created["id"]

        patch = self.client.patch(f"/api/v2/tickets/{ticket_id}", json={"assignee_id": self.employee.id}, headers=headers)
        self.assertEqual(patch.status_code, 200)
        patched_payload = patch.get_json() or {}
        self.assertEqual((patched_payload.get("ticket") or {}).get("assignee_id"), self.employee.id)

        other_employee = self._create_user("empleado-patch@t2.test", "empleado", tenant_slug="tenant-2", tenant_id=self.tenant_2.id)
        db.session.commit()
        blocked = self.client.patch(f"/api/v2/tickets/{ticket_id}", json={"assignee_id": other_employee.id}, headers=headers)
        self.assertEqual(blocked.status_code, 404)
        self.assertEqual((blocked.get_json() or {}).get("reason_code"), "assignee_not_found")

    def test_patch_location_updates_heatmap_ready_coordinates_and_audit_event(self):
        headers = {**self._auth_header(self.employee), "X-Tenant-Slug": "tenant-1"}
        created = self.client.post(
            "/api/v2/tickets",
            json={
                "title": "Direccion sin coordenadas",
                "description": "Caso que debe salir en cola de geocodificacion",
                "location": {"address": "Don Bosco 55, Junin, Mendoza, AR"},
            },
            headers=headers,
        ).get_json()
        ticket_id = created["id"]

        patch = self.client.patch(
            f"/api/v2/tickets/{ticket_id}",
            json={"location": {"lat": "-34.5852", "lng": "-60.9441", "address": "Don Bosco 55, Junin, Mendoza, AR"}},
            headers=headers,
        )

        self.assertEqual(patch.status_code, 200)
        ticket_payload = (patch.get_json() or {}).get("ticket") or {}
        self.assertEqual(ticket_payload.get("location"), {
            "address": "Don Bosco 55, Junin, Mendoza, AR",
            "lat": -34.5852,
            "lng": -60.9441,
        })

        refreshed = TenantTicket.query.get(ticket_id)
        self.assertEqual(refreshed.latitud, -34.5852)
        self.assertEqual(refreshed.longitud, -60.9441)
        self.assertEqual((refreshed.datos_extra or {}).get("address"), "Don Bosco 55, Junin, Mendoza, AR")

        events_resp = self.client.get(f"/api/v2/tickets/{ticket_id}/events", headers=headers)
        events = (events_resp.get_json() or {}).get("items") or []
        location_event = next((event for event in events if event.get("event_type") == "ticket.location_updated"), None)
        self.assertIsNotNone(location_event)
        self.assertEqual(((location_event or {}).get("details") or {}).get("source"), "api_v2_patch_ticket")

    def test_patch_location_rejects_invalid_coordinates(self):
        headers = {**self._auth_header(self.employee), "X-Tenant-Slug": "tenant-1"}
        created = self.client.post("/api/v2/tickets", json={"title": "Geo", "description": "Geo"}, headers=headers).get_json()
        ticket_id = created["id"]

        patch = self.client.patch(
            f"/api/v2/tickets/{ticket_id}",
            json={"location": {"lat": "120", "lng": "-60.9441"}},
            headers=headers,
        )

        self.assertEqual(patch.status_code, 400)
        payload = patch.get_json() or {}
        self.assertEqual(payload.get("contract_version"), "shared.error.v1")
        self.assertEqual(payload.get("reason_code"), "validation_failed")
        self.assertIn("location.lat", (payload.get("message") or ""))

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

    def test_messages_and_timeline_filter_internal_comments_for_customer(self):
        admin_headers = {**self._auth_header(self.admin), "X-Tenant-Slug": "tenant-1"}
        user_headers = {**self._auth_header(self.end_user), "X-Tenant-Slug": "tenant-1"}
        created = self.client.post(
            "/api/v2/tickets",
            json={"title": "Conversacion", "description": "Vecino"},
            headers=user_headers,
        ).get_json()
        ticket_id = created["id"]

        public_comment = self.client.post(
            f"/api/v2/tickets/{ticket_id}/comments",
            json={"body": "Respuesta publica", "visibility": "public"},
            headers=admin_headers,
        )
        self.assertEqual(public_comment.status_code, 201)
        internal_comment = self.client.post(
            f"/api/v2/tickets/{ticket_id}/comments",
            json={"body": "Nota interna de equipo", "visibility": "internal"},
            headers=admin_headers,
        )
        self.assertEqual(internal_comment.status_code, 201)

        user_messages = self.client.get(f"/api/v2/tickets/{ticket_id}/messages", headers=user_headers)
        self.assertEqual(user_messages.status_code, 200)
        user_payload = user_messages.get_json() or {}
        user_texts = [item.get("body") for item in user_payload.get("messages") or []]
        self.assertEqual(user_texts, ["Respuesta publica"])

        admin_messages = self.client.get(f"/api/v2/tickets/{ticket_id}/messages", headers=admin_headers)
        self.assertEqual(admin_messages.status_code, 200)
        admin_texts = [item.get("body") for item in (admin_messages.get_json() or {}).get("messages") or []]
        self.assertIn("Respuesta publica", admin_texts)
        self.assertIn("Nota interna de equipo", admin_texts)

        user_timeline = self.client.get(f"/api/v2/tickets/{ticket_id}/timeline", headers=user_headers)
        self.assertEqual(user_timeline.status_code, 200)
        timeline_text = " ".join(
            str(item.get("preview_text") or "")
            for item in (user_timeline.get_json() or {}).get("unified_conversation_stream") or []
        )
        self.assertIn("Respuesta publica", timeline_text)
        self.assertNotIn("Nota interna de equipo", timeline_text)

    def test_v2_ai_enrichment_is_advisory_for_tenant_ticket(self):
        admin_headers = {**self._auth_header(self.admin), "X-Tenant-Slug": "tenant-1"}
        created = self.client.post(
            "/api/v2/tickets",
            json={"title": "Pedido", "description": "Cliente pregunta por stock"},
            headers=admin_headers,
        ).get_json()
        ticket_id = created["id"]
        self.client.post(
            f"/api/v2/tickets/{ticket_id}/comments",
            json={"body": "Quiere sumar dos unidades", "visibility": "public"},
            headers=admin_headers,
        )

        seen = {}

        def fake_enrichment(ticket, scope, comments=None, tenant=None):
            seen["ticket_id"] = ticket.id
            seen["scope"] = scope
            seen["comments"] = list(comments or [])
            seen["tenant_id"] = tenant.id if tenant else None
            return {
                "contract_version": "ticket.ai_enrichment.v1",
                "ticket_id": ticket.id,
                "tenant_id": tenant.id if tenant else ticket.tenant_id,
                "crm_hints": {
                    "suggested_queue": "crear_pedido",
                    "requires_human_attention": True,
                    "recommended_actions": [{"id": "preparar_pedido"}, {"id": "confirmar_stock"}],
                },
                "huggingface": {
                    "provider_family": "huggingface",
                    "mode": "advisory",
                    "intent": {"provider": "deterministic_local_fallback"},
                },
                "state_mutation": {"applied": False},
                "persisted": False,
            }

        with patch("services.ticket_ai_enrichment.build_ticket_ai_enrichment", side_effect=fake_enrichment):
            response = self.client.post(
                f"/api/v2/tickets/{ticket_id}/ai-enrichment",
                json={"comments_limit": 10},
                headers=admin_headers,
            )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json() or {}
        self.assertEqual(payload.get("contract_version"), "ticket.ai_enrichment.v1")
        self.assertEqual(payload.get("ticket_type"), "tenant")
        self.assertEqual(payload.get("source_model"), "TenantTicket")
        self.assertEqual(payload.get("domain_scope"), "pyme")
        self.assertEqual(seen["ticket_id"], ticket_id)
        self.assertEqual(seen["scope"], "pyme")
        self.assertEqual(len(seen["comments"]), 1)

        events = AnalyticsEventV2.query.filter_by(
            tenant_id=self.tenant_1.id,
            event_name="ticket_ai_enrichment_generated",
            entity_ref=f"ticket:{ticket_id}",
        ).all()
        self.assertEqual(len(events), 1)
        event_metadata = events[0].metadata_payload or {}
        self.assertTrue(event_metadata.get("advisory_only"))
        self.assertEqual(event_metadata.get("ticket_id"), ticket_id)
        self.assertEqual(event_metadata.get("source_model"), "TenantTicket")
        self.assertEqual(event_metadata.get("domain_scope"), "pyme")
        self.assertEqual(event_metadata.get("provider_family"), "huggingface")
        self.assertEqual(event_metadata.get("provider"), "deterministic_local_fallback")
        self.assertEqual(event_metadata.get("suggested_queue"), "crear_pedido")
        self.assertTrue(event_metadata.get("requires_human_attention"))
        self.assertEqual(event_metadata.get("recommended_action_ids"), ["preparar_pedido", "confirmar_stock"])
        self.assertEqual(event_metadata.get("comments_count"), 1)
        self.assertNotIn("Quiere sumar dos unidades", str(event_metadata))

        rejected = self.client.post(
            f"/api/v2/tickets/{ticket_id}/ai-enrichment",
            json={"apply": True, "estado": "resuelto"},
            headers=admin_headers,
        )
        self.assertEqual(rejected.status_code, 400)
        self.assertEqual((rejected.get_json() or {}).get("reason_code"), "ai_enrichment_mutation_rejected")
        self.assertEqual(
            AnalyticsEventV2.query.filter_by(
                tenant_id=self.tenant_1.id,
                event_name="ticket_ai_enrichment_generated",
                entity_ref=f"ticket:{ticket_id}",
            ).count(),
            1,
        )

    def test_customer_cannot_create_internal_comment(self):
        headers = {**self._auth_header(self.end_user), "X-Tenant-Slug": "tenant-1"}
        created = self.client.post("/api/v2/tickets", json={"title": "C", "description": "C"}, headers=headers).get_json()
        ticket_id = created["id"]

        comment_resp = self.client.post(
            f"/api/v2/tickets/{ticket_id}/comments",
            json={"body": "Nota privada falsa", "visibility": "internal"},
            headers=headers,
        )
        self.assertEqual(comment_resp.status_code, 403)
        self.assertEqual((comment_resp.get_json() or {}).get("reason_code"), "operator_required")

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
        self.assertTrue((resp.get_json() or {}).get("read_only"))

    def test_breach_detection_is_category_scoped_and_read_only_for_employee(self):
        headers = {**self._auth_header(self.employee), "X-Tenant-Slug": "tenant-1"}
        past_due = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
        restricted = TenantTicket(
            tenant_id=self.tenant_1.id,
            user_id=self.admin.id,
            categoria="restricted",
            descripcion="SLA fuera del alcance",
            estado="nuevo",
            origen="web",
            datos_extra={
                "title": "SLA restringido",
                "priority": "urgent",
                "sla": {
                    "resolution_due_at": past_due,
                    "next_update_due_at": past_due,
                },
            },
        )
        db.session.add(restricted)
        db.session.commit()
        before = copy.deepcopy(restricted.datos_extra)

        response = self.client.get("/api/v2/sla/breaches", headers=headers)

        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertNotIn(
            restricted.id,
            {item.get("ticket_id") for item in (response.get_json() or {}).get("items", [])},
        )
        db.session.expire_all()
        refreshed = db.session.get(TenantTicket, restricted.id)
        self.assertEqual(refreshed.datos_extra, before)
        self.assertNotIn("sla_breach_event_emitted_at", refreshed.datos_extra or {})

    def test_sla_endpoints_require_operator_in_tenant(self):
        no_token = self.client.get("/api/v2/sla/policies", headers={"X-Tenant-Slug": "tenant-1"})
        self.assertEqual(no_token.status_code, 401)
        self.assertEqual((no_token.get_json() or {}).get("reason_code"), "auth_required")

        cross_tenant = self.client.get(
            "/api/v2/sla/policies",
            headers={**self._auth_header(self.employee), "X-Tenant-Slug": "tenant-2"},
        )
        self.assertEqual(cross_tenant.status_code, 403)
        self.assertEqual((cross_tenant.get_json() or {}).get("reason_code"), "tenant_access_denied")

        customer = self.client.get(
            "/api/v2/sla/policies",
            headers={**self._auth_header(self.end_user), "X-Tenant-Slug": "tenant-1"},
        )
        self.assertEqual(customer.status_code, 403)
        self.assertEqual((customer.get_json() or {}).get("reason_code"), "operator_required")


if __name__ == "__main__":
    unittest.main()
