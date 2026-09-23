import os
import unittest
from unittest.mock import patch

os.environ.setdefault("FLASK_SKIP_GLOBAL_APP", "1")

from app import create_app, db
from config import Config
from models import MunicipioTicket, TenantProfile, TicketComentario, User
from tests.junin_product_flow_support import (
    JUNIN_QA_ADDRESS,
    JUNIN_QA_LAT,
    JUNIN_QA_LNG,
)


class ProductFlowClaimConfig(Config):
    TESTING = True
    ENABLE_DEMO_MODE = True
    SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"
    SQLALCHEMY_ENGINE_OPTIONS = {"connect_args": {"check_same_thread": False}}
    ENABLE_RUNTIME_SCHEMA_SYNC = False
    ENABLE_RUNTIME_TENANT_INIT = False


class ProductFlowMunicipioClaimTrackingChatTest(unittest.TestCase):
    def setUp(self):
        self.app = create_app(ProductFlowClaimConfig)
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()
        self.client = self.app.test_client()

        self.owner = User(
            name="Municipalidad de Junín QA",
            email="municipio-flow@test.com",
            rol="admin",
            tipo_chat="municipio",
            tenant_slug="junin",
        )
        self.owner.set_password("secret123")
        db.session.add(self.owner)
        db.session.flush()

        self.tenant = TenantProfile(
            slug="junin",
            nombre="Municipalidad de Junín QA",
            tipo="municipio",
            municipio_id=self.owner.id,
            configuracion={
                "live_chat_schedule": {
                    "enabled": True,
                    "days": [0, 1, 2, 3, 4],
                    "start_time": "09:00",
                    "end_time": "13:00",
                    "timezone": "America/Argentina/Buenos_Aires",
                }
            },
        )
        db.session.add(self.tenant)
        db.session.flush()

        self.ticket = MunicipioTicket(
            tenant_id=self.tenant.id,
            municipio_id=self.owner.id,
            nro_ticket="123456",
            consulta_pin="654321",
            asunto="Alumbrado publico",
            categoria="alumbrado",
            pregunta="Luz apagada",
            estado="en_proceso",
            canal_ingreso="web",
            direccion=JUNIN_QA_ADDRESS,
            latitud=JUNIN_QA_LAT,
            longitud=JUNIN_QA_LNG,
            nombre_vecino="Ana",
        )
        db.session.add(self.ticket)
        db.session.flush()
        db.session.add(
            TicketComentario(
                municipio_ticket_id=self.ticket.id,
                comentario="Cuadrilla asignada.",
                es_admin=True,
                origen="admin_panel",
            )
        )
        db.session.commit()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def _offline_status(self, tenant):
        return {
            "contract_version": "live_chat.schedule.v1",
            "enabled": True,
            "available": False,
            "mode": "offline",
            "description": "lunes a viernes de 09:00 a 13:00 hs",
            "source": "tenant_config",
            "offline_message_enabled": True,
            "timezone": "America/Argentina/Buenos_Aires",
            "socket_room": f"municipio_{self.owner.id}",
        }

    def test_claim_tracking_public_message_updates_support_flow(self):
        with patch("routes.tracking_ui.emit_new_chat_message") as emit_chat, patch(
            "routes.tracking_ui.emit_ticket_unread_changed"
        ) as emit_unread, patch(
            "services.tracking_experience.build_tenant_live_chat_status",
            side_effect=self._offline_status,
        ):
            response = self.client.get(
                "/api/public/tracking/experience?kind=claim&code=M-123456&pin=654321"
            )
            self.assertEqual(response.status_code, 200, response.get_json())
            payload = response.get_json()
            self.assertEqual(payload["contract_version"], "tracking.experience.v1")
            self.assertEqual(payload["resource"]["code"], "M-123456")
            self.assertEqual(payload["support"]["mode"], "offline")
            self.assertEqual(payload["support"]["conversation"]["message_count"], 1)
            self.assertEqual(payload["support"]["admin_response_surface"]["id"], "tenant_claims_inbox")
            self.assertIn(f"ticket_id={self.ticket.id}", payload["support"]["admin_response_surface"]["route"])
            self.assertIn("offline_message", payload["support"]["admin_response_surface"]["route"])

            message = self.client.post(
                f"/api/public/tracking/claims/{self.ticket.id}/messages?pin=654321",
                json={"mensaje": "Sigue sin luz"},
                headers={"X-Request-Id": "claim-flow-1"},
            )

        self.assertEqual(message.status_code, 201, message.get_json())
        message_payload = message.get_json()
        self.assertEqual(message_payload["contract_version"], "tracking.support_message.v1")
        self.assertEqual(message_payload["request_id"], "claim-flow-1")
        self.assertEqual(message_payload["comment"]["source"], "public_tracking")
        self.assertTrue(message_payload["comment"]["unread_for_team"])
        self.assertEqual(message_payload["delivery"]["admin_surface"], "tenant_claims_inbox")
        self.assertIn(f"ticket_id={self.ticket.id}", message_payload["delivery"]["admin_route"])
        self.assertTrue(message_payload["delivery"]["admin_unread"])
        self.assertTrue(message_payload["delivery"]["timeline_updated"])
        self.assertEqual(message_payload["delivery"]["reply_status"], "queued_for_agent")
        self.assertEqual(message_payload["crm_writeback"]["thread_binding"], "municipio_ticket_id")
        self.assertIn(f"ticket_id={self.ticket.id}", message_payload["crm_writeback"]["frontend_path"])
        self.assertTrue(message_payload["crm_writeback"]["requires_admin_response"])
        self.assertIn(
            "admin_inbox_unread_incremented",
            message_payload["crm_writeback"]["writebacks"],
        )
        self.assertEqual(message_payload["tracking"]["support"]["conversation"]["message_count"], 2)
        emit_chat.assert_called_once()
        emitted_payload = emit_chat.call_args.args[0]
        self.assertEqual(emitted_payload["tenant_type"], "municipio")
        self.assertEqual(emitted_payload["tenant_id"], self.tenant.id)
        self.assertEqual(emitted_payload["municipio_id"], self.owner.id)
        self.assertEqual(emitted_payload["ticket_id"], self.ticket.id)
        self.assertEqual(emitted_payload["comment_id"], message_payload["comment"]["id"])
        self.assertTrue(emitted_payload["requires_response"])
        self.assertTrue(emitted_payload["admin_unread"])
        self.assertIn(f"ticket_id={self.ticket.id}", emitted_payload["admin_route"])
        emit_unread.assert_called_once()
        unread_payload = emit_unread.call_args.args[0]
        self.assertEqual(unread_payload["ticket_id"], self.ticket.id)
        self.assertEqual(unread_payload["comment_id"], message_payload["comment"]["id"])
        self.assertTrue(unread_payload["has_unread"])
        self.assertTrue(unread_payload["requires_response"])
        self.assertEqual(unread_payload["source"], "public_tracking")

        persisted = TicketComentario.query.filter_by(
            municipio_ticket_id=self.ticket.id,
            comentario="Sigue sin luz",
            es_admin=False,
            origen="public_tracking",
        ).first()
        self.assertIsNotNone(persisted)

        timeline = self.client.get(f"/tickets/municipio/{self.ticket.id}/timeline?pin=654321")
        self.assertEqual(timeline.status_code, 200, timeline.get_json())
        body = timeline.get_json()
        serialized = str(body)
        self.assertIn("Sigue sin luz", serialized)

    def test_demo_claim_creation_exposes_public_tracking_action_from_http_payload(self):
        from routes.v2.tenants import create_demo_session_token

        demo_session_id = create_demo_session_token(tenant_slug=self.tenant.slug, sector="gobierno", rubro="gobierno")
        response = self.client.post(
            f"/api/ask/municipio?tenant_slug={self.tenant.slug}&demo_session_id={demo_session_id}",
            json={
                "pregunta": "Hay un bache peligroso frente a la plaza",
                "demo_mode": True,
                "tenant_slug": self.tenant.slug,
                "location": {
                    "lat": JUNIN_QA_LAT,
                    "lng": JUNIN_QA_LNG,
                    "address": JUNIN_QA_ADDRESS,
                },
            },
            headers={
                "Origin": "https://www.chatboc.ar",
                "X-Request-Id": "claim-public-tracking-action-1",
                "X-Chat-Session-Id": "sid_claim_public_tracking_action",
            },
        )

        self.assertEqual(response.status_code, 200, response.get_json())
        payload = response.get_json()
        ticket_payload = payload.get("ticket") or {}
        ticket_code = str(ticket_payload.get("nro_ticket") or "")
        if ticket_code.upper().startswith(("M-", "S-")):
            ticket_code = ticket_code[2:]
        pin = str(ticket_payload.get("consulta_pin") or "")
        expected_url = f"/tracking/claim/{ticket_code}#pin={pin}"

        self.assertTrue(ticket_code)
        self.assertTrue(pin)
        self.assertEqual(ticket_payload.get("detail_endpoint"), expected_url)
        self.assertEqual((payload.get("lead") or {}).get("detail_endpoint"), expected_url)
        tracking_action = next(
            (action for action in payload.get("next_actions") or [] if action.get("id") == "track_claim"),
            None,
        )
        self.assertIsNotNone(tracking_action)
        self.assertEqual(tracking_action.get("label"), "Ver seguimiento")
        self.assertEqual(tracking_action.get("endpoint"), expected_url)
        self.assertNotIn("/api/v2/inbox/omnichannel", str(payload))

    def test_conversational_claim_intake_opens_tracking_and_public_thread(self):
        from routes.v2.tenants import create_demo_session_token

        demo_session_id = create_demo_session_token(tenant_slug=self.tenant.slug, sector="gobierno", rubro="gobierno")
        intake = self.client.post(
            f"/api/ask/municipio?tenant_slug={self.tenant.slug}&demo_session_id={demo_session_id}",
            json={
                "pregunta": "Donde reporto baches con ubicacion?",
                "demo_mode": True,
                "tenant_slug": self.tenant.slug,
                "location": {
                    "lat": JUNIN_QA_LAT,
                    "lng": JUNIN_QA_LNG,
                    "address": JUNIN_QA_ADDRESS,
                },
            },
            headers={
                "Origin": "https://www.chatboc.ar",
                "X-Request-Id": "claim-intake-tracking-1",
                "X-Chat-Session-Id": "sid_claim_intake_tracking",
            },
        )

        self.assertEqual(intake.status_code, 200, intake.get_json())
        intake_payload = intake.get_json()
        self.assertEqual(intake_payload.get("fuente"), "demo_municipio_runtime")
        self.assertEqual((intake_payload.get("ticket") or {}).get("category"), "Baches y calzada")

        ticket = (
            MunicipioTicket.query.filter(MunicipioTicket.id != self.ticket.id)
            .order_by(MunicipioTicket.id.desc())
            .first()
        )
        self.assertIsNotNone(ticket)
        self.assertEqual(ticket.categoria, "Baches y calzada")
        self.assertEqual(ticket.direccion, JUNIN_QA_ADDRESS)
        self.assertTrue(ticket.consulta_pin)

        with patch("routes.tracking_ui.emit_new_chat_message") as emit_chat, patch(
            "routes.tracking_ui.emit_ticket_unread_changed"
        ) as emit_unread, patch(
            "services.tracking_experience.build_tenant_live_chat_status",
            side_effect=self._offline_status,
        ):
            tracking = self.client.get(
                f"/api/public/tracking/experience?kind=claim&code=M-{ticket.nro_ticket}&pin={ticket.consulta_pin}",
                headers={"X-Request-Id": "claim-intake-tracking-view-1"},
            )
            self.assertEqual(tracking.status_code, 200, tracking.get_json())
            tracking_payload = tracking.get_json()
            self.assertEqual(tracking_payload["contract_version"], "tracking.experience.v1")
            self.assertEqual(tracking_payload["resource"]["code"], f"M-{ticket.nro_ticket}")
            self.assertEqual(tracking_payload["support"]["mode"], "offline")

            public_message = self.client.post(
                f"/api/public/tracking/claims/{ticket.id}/messages?pin={ticket.consulta_pin}",
                json={"mensaje": "Necesito saber cuando viene la cuadrilla"},
                headers={"X-Request-Id": "claim-intake-public-message-1"},
            )

        self.assertEqual(public_message.status_code, 201, public_message.get_json())
        message_payload = public_message.get_json()
        self.assertEqual(message_payload["contract_version"], "tracking.support_message.v1")
        self.assertEqual(message_payload["request_id"], "claim-intake-public-message-1")
        self.assertIn(f"ticket_id={ticket.id}", message_payload["delivery"]["admin_route"])
        self.assertTrue(message_payload["delivery"]["admin_unread"])
        self.assertEqual(message_payload["crm_writeback"]["thread_binding"], "municipio_ticket_id")
        emit_chat.assert_called_once()
        emit_unread.assert_called_once()

        timeline = self.client.get(f"/tickets/municipio/{ticket.id}/timeline?pin={ticket.consulta_pin}")
        self.assertEqual(timeline.status_code, 200, timeline.get_json())
        self.assertIn("Necesito saber cuando viene la cuadrilla", str(timeline.get_json()))
