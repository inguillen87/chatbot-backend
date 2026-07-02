import json
import os
import unittest
from unittest.mock import patch

os.environ.setdefault("FLASK_SKIP_GLOBAL_APP", "1")

from app import create_app, db
from config import Config
from models import MunicipioTicket, PedidoConversacional, PymePedido, TenantProfile, TicketComentario, User
from services.tracking_experience import TRACKING_EXPERIENCE_CONTRACT_VERSION


class TrackingExperienceTestConfig(Config):
    TESTING = True
    ENABLE_DEMO_MODE = True
    SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"
    SQLALCHEMY_ENGINE_OPTIONS = {"connect_args": {"check_same_thread": False}}
    ENABLE_RUNTIME_SCHEMA_SYNC = False
    ENABLE_RUNTIME_TENANT_INIT = False


class TrackingExperienceContractTest(unittest.TestCase):
    def setUp(self):
        self.app = create_app(TrackingExperienceTestConfig)
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()
        self.client = self.app.test_client()

        self.owner = User(name="Municipio Demo", email="muni-track@test.com", rol="admin", tipo_chat="municipio")
        self.owner.set_password("secret123")
        db.session.add(self.owner)
        db.session.flush()

        self.tenant = TenantProfile(
            slug="muni-track",
            nombre="Municipio Track",
            tipo="municipio",
            vertical="gobierno",
            municipio_id=self.owner.id,
            configuracion={
                "widget_tokens": ["track-token"],
                "live_chat_schedule": {
                    "enabled": True,
                    "days": [0, 1, 2, 3, 4],
                    "start_time": "09:00",
                    "end_time": "13:00",
                    "timezone": "America/Argentina/Buenos_Aires",
                },
            },
        )
        db.session.add(self.tenant)
        db.session.flush()

        self.claim = MunicipioTicket(
            tenant_id=self.tenant.id,
            municipio_id=self.owner.id,
            nro_ticket="123456",
            consulta_pin="654321",
            asunto="Alumbrado publico",
            categoria="alumbrado",
            pregunta="Luz apagada en la esquina",
            estado="en_proceso",
            canal_ingreso="whatsapp",
            direccion="Av. Siempre Viva 123",
            latitud=-34.6,
            longitud=-58.4,
            nombre_vecino="Ana",
        )
        db.session.add(self.claim)
        db.session.flush()
        db.session.add(
            TicketComentario(
                municipio_ticket_id=self.claim.id,
                comentario="Cuadrilla asignada.",
                es_admin=True,
                origen="admin_panel",
            )
        )

        self.order = PymePedido(
            pyme_id=self.owner.id,
            tenant_id=self.tenant.id,
            asunto="Pedido ferreteria",
            detalles=json.dumps([{"nombre": "Taladro", "cantidad": 1, "precio": 50000}]),
            monto_total=50000,
            nombre_cliente="Juan",
            telefono_cliente="+5491111111111",
            direccion="San Martin 100",
            latitud=-34.61,
            longitud=-58.41,
        )
        self.order.nro_pedido = "PED-100"
        self.order.estado = "preparando"
        db.session.add(self.order)
        db.session.commit()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def test_public_claim_tracking_experience_returns_courier_contract(self):
        response = self.client.get(
            "/api/public/tracking/experience?kind=claim&code=M-123456&pin=654321",
            headers={"X-Request-Id": "track-claim-1"},
        )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["contract_version"], "tracking.experience.v1")
        self.assertEqual(payload["request_id"], "track-claim-1")
        self.assertEqual(response.headers.get("X-Request-Id"), "track-claim-1")
        self.assertEqual(payload["kind"], "claim")
        self.assertEqual(payload["tenant"]["slug"], self.tenant.slug)
        self.assertEqual(payload["resource"]["code"], "M-123456")
        self.assertEqual(payload["status"]["current_stage"], "en_proceso")
        self.assertTrue(payload["map"]["has_coordinates"])
        self.assertIn("route_progress", payload["map"]["animations"])
        self.assertEqual(payload["map"]["fallback_when_no_coordinates"], "timeline_only")
        self.assertTrue(any(item["type"] == "comment" for item in payload["timeline"]))
        self.assertEqual(payload["frontend_contract"]["render_as"], "tracking_map_timeline_helpdesk")
        self.assertEqual(payload["support"]["contract_version"], "tracking.support.v1")
        self.assertEqual(payload["support"]["ticket"]["id"], self.claim.id)
        self.assertEqual(payload["support"]["ticket"]["requires_pin"], True)
        self.assertEqual(payload["support"]["endpoints"]["send_message"], f"/api/public/tracking/claims/{self.claim.id}/messages")
        self.assertEqual(payload["support"]["live_chat"]["contract_version"], "live_chat.schedule.v1")
        self.assertEqual(payload["support"]["live_chat"]["source"], "tenant_config")
        self.assertIn(payload["support"]["live_chat"]["mode"], {"live", "offline"})
        self.assertTrue(payload["support"]["live_chat"]["offline_message_enabled"])
        self.assertEqual(payload["support"]["conversation"]["message_count"], 1)
        self.assertFalse(payload["support"]["conversation"]["unread_for_team"])
        self.assertIn("admin_inbox_unread_incremented", payload["support"]["conversation"]["writebacks"])
        self.assertEqual(payload["support"]["service_window"]["tenant_schedule_source"], "tenant_config")
        self.assertTrue(payload["support"]["service_window"]["accepts_messages"])
        self.assertEqual(payload["support"]["service_window"]["outside_hours_mode"], "offline_message")
        self.assertEqual(payload["support"]["socket"]["fallback_transport"], "http_polling")
        self.assertEqual(payload["support"]["polling"]["endpoint"], f"/tickets/municipio/{self.claim.id}/timeline")
        self.assertTrue(payload["support"]["webview_policy"]["stay_inside_tracking"])
        self.assertFalse(payload["support"]["webview_policy"]["external_redirect_required"])
        self.assertEqual(payload["support"]["admin_response_surface"]["id"], "tenant_claims_inbox")
        self.assertEqual(payload["support"]["admin_response_surface"]["thread_binding"], "municipio_ticket_id")
        self.assertEqual(payload["support"]["admin_response_surface"]["route"], "/perfil?tab=tickets")
        self.assertTrue(payload["support"]["operator_queue"]["unread_on_customer_message"])
        self.assertTrue(payload["support"]["operator_queue"]["requires_admin_response"])
        action_by_id = {item["id"]: item for item in payload["actions"]}
        self.assertEqual(
            action_by_id["send_message"]["endpoint"],
            f"/api/public/tracking/claims/{self.claim.id}/messages",
        )
        self.assertEqual(
            action_by_id["open_tracking_page"]["url"],
            "/tracking/claim/123456?pin=654321",
        )
        self.assertTrue(action_by_id["open_tracking_page"]["requires_pin"])

    def test_public_claim_tracking_support_cta_differs_by_live_mode(self):
        base_status = {
            "contract_version": "live_chat.schedule.v1",
            "enabled": True,
            "description": "lunes a viernes de 09:00 a 13:00 hs",
            "source": "tenant_config",
            "timezone": "America/Argentina/Buenos_Aires",
            "offline_message_enabled": True,
        }

        with patch(
            "services.tracking_experience.build_tenant_live_chat_status",
            return_value={**base_status, "available": False, "mode": "offline"},
        ):
            response = self.client.get(
                "/api/public/tracking/experience?kind=claim&code=M-123456&pin=654321"
            )

        self.assertEqual(response.status_code, 200)
        offline_payload = response.get_json()
        offline_support = offline_payload["support"]
        self.assertEqual(offline_support["mode"], "offline")
        self.assertEqual(offline_support["availability"]["state"], "offline_accepting_messages")
        self.assertEqual(offline_support["cta"]["primary"]["label"], "Dejar mensaje para el equipo")
        self.assertEqual(offline_support["cta"]["primary"]["action"], "queue_ticket_comment")
        self.assertEqual(offline_support["ui"]["primary_cta"], "Dejar mensaje para el equipo")
        offline_action = {item["id"]: item for item in offline_payload["actions"]}["send_message"]
        self.assertEqual(offline_action["action"], "queue_ticket_comment")
        self.assertTrue(offline_action["safe_for_offline"])
        self.assertFalse(offline_support["webview_policy"]["external_redirect_required"])

        with patch(
            "services.tracking_experience.build_tenant_live_chat_status",
            return_value={**base_status, "available": True, "mode": "live"},
        ):
            response = self.client.get(
                "/api/public/tracking/experience?kind=claim&code=M-123456&pin=654321"
            )

        self.assertEqual(response.status_code, 200)
        live_payload = response.get_json()
        live_support = live_payload["support"]
        self.assertEqual(live_support["mode"], "live")
        self.assertEqual(live_support["availability"]["state"], "online")
        self.assertEqual(live_support["cta"]["primary"]["label"], "Chatear con un agente")
        self.assertEqual(live_support["cta"]["primary"]["action"], "socket_live_message")
        live_action = {item["id"]: item for item in live_payload["actions"]}["send_message"]
        self.assertEqual(live_action["action"], "socket_live_message")

    def test_public_claim_support_message_endpoint_requires_pin_and_updates_tracking(self):
        rejected = self.client.post(
            f"/api/public/tracking/claims/{self.claim.id}/messages",
            json={"mensaje": "hola seguimiento"},
        )
        self.assertEqual(rejected.status_code, 400)
        rejected_payload = rejected.get_json()
        self.assertEqual(rejected_payload["reason_code"], "tracking_pin_required")
        self.assertEqual(rejected_payload["contract_version"], TRACKING_EXPERIENCE_CONTRACT_VERSION)

        with patch(
            "services.tracking_experience.build_tenant_live_chat_status",
            return_value={
                "contract_version": "live_chat.schedule.v1",
                "enabled": True,
                "available": False,
                "mode": "offline",
                "description": "lunes a viernes de 09:00 a 13:00 hs",
                "source": "tenant_config",
                "timezone": "America/Argentina/Buenos_Aires",
                "offline_message_enabled": True,
            },
        ):
            accepted = self.client.post(
                f"/api/public/tracking/claims/{self.claim.id}/messages?pin=654321",
                json={"mensaje": "hola seguimiento"},
                headers={"X-Request-Id": "track-message-1"},
            )
        self.assertEqual(accepted.status_code, 201)
        payload = accepted.get_json()
        self.assertEqual(payload["contract_version"], "tracking.support_message.v1")
        self.assertEqual(payload["request_id"], "track-message-1")
        self.assertEqual(accepted.headers.get("X-Request-Id"), "track-message-1")
        self.assertEqual(payload["ticket_id"], self.claim.id)
        self.assertEqual(payload["comment"]["message"], "hola seguimiento")
        self.assertEqual(payload["comment"]["author"], "customer")
        self.assertEqual(payload["comment"]["source"], "public_tracking")
        self.assertEqual(payload["delivery"]["channel"], "ticket_bound_helpdesk")
        self.assertEqual(payload["delivery"]["admin_surface"], "tenant_claims_inbox")
        self.assertTrue(payload["delivery"]["admin_unread"])
        self.assertTrue(payload["delivery"]["timeline_updated"])
        self.assertEqual(payload["delivery"]["reply_status"], "queued_for_agent")
        self.assertEqual(payload["crm_writeback"]["admin_surface"], "tenant_claims_inbox")
        self.assertEqual(payload["crm_writeback"]["route"], "/perfil?tab=tickets")
        self.assertEqual(payload["crm_writeback"]["thread_binding"], "municipio_ticket_id")
        self.assertTrue(payload["crm_writeback"]["unread_for_team"])
        self.assertTrue(payload["crm_writeback"]["requires_admin_response"])
        self.assertIn("admin_inbox_unread_incremented", payload["crm_writeback"]["writebacks"])
        self.assertEqual(payload["unread_event"]["ticket_id"], self.claim.id)
        self.assertEqual(payload["unread_event"]["comment_id"], payload["comment"]["id"])
        self.assertTrue(payload["unread_event"]["has_unread"])
        self.assertTrue(payload["unread_event"]["requires_response"])
        self.assertEqual(payload["timeline_endpoint"], f"/tickets/municipio/{self.claim.id}/timeline")
        self.assertEqual(payload["tracking"]["support"]["conversation"]["message_count"], 2)
        self.assertTrue(payload["tracking"]["support"]["conversation"]["unread_for_team"])
        self.assertEqual(
            payload["tracking"]["support"]["endpoints"]["send_message"],
            f"/api/public/tracking/claims/{self.claim.id}/messages",
        )

        persisted = TicketComentario.query.filter_by(
            municipio_ticket_id=self.claim.id,
            comentario="hola seguimiento",
            origen="public_tracking",
            es_admin=False,
        ).first()
        self.assertIsNotNone(persisted)

    def test_public_order_tracking_experience_returns_items_and_progress(self):
        response = self.client.get(
            "/api/public/tracking/experience?kind=order&code=PED-100",
            headers={"X-Request-Id": "track-order-1"},
        )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["contract_version"], "tracking.experience.v1")
        self.assertEqual(payload["request_id"], "track-order-1")
        self.assertEqual(payload["kind"], "order")
        self.assertEqual(payload["resource"]["code"], "PED-100")
        self.assertEqual(payload["status"]["current_stage"], "preparando")
        self.assertEqual(payload["items"][0]["title"], "Taladro")
        self.assertTrue(payload["map"]["has_coordinates"])
        self.assertEqual(payload["map"]["fallback_when_no_coordinates"], "timeline_only")
        self.assertEqual(payload["frontend_contract"]["render_as"], "tracking_map_timeline")

    def test_public_assisted_order_tracking_sanitizes_raw_payload(self):
        assisted = PedidoConversacional(
            tenant_id=self.tenant.id,
            user_id=self.owner.id,
            tipo="solicitud_vecinal_desde_archivo",
            estado="confirmado",
            origen="marketplace",
            items=[
                {
                    "texto_original": "Reclamo privado con telefono y direccion completa",
                    "items_detectados": [],
                    "no_encontrados_labels": ["1 Luminaria quemada en Don Bosco 55"],
                    "operator_pack": {"suggested_reply": "mensaje interno"},
                }
            ],
            metadata_payload={
                "contract_version": "marketplace.assisted_request.v1",
                "request_kind": "service_request",
                "request_kind_label": "reclamo o solicitud vecinal",
                "contact": {"name": "Marcelo", "phone": "+5492613168608", "email": "marcelo@example.com"},
            },
            monto_monetario=0,
            monto_puntos=0,
        )
        db.session.add(assisted)
        db.session.commit()

        response = self.client.get(f"/api/public/tracking/experience?kind=order&code=pc-{assisted.id}")

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["kind"], "order")
        self.assertEqual(payload["customer"]["name"], "Marcelo")
        self.assertEqual(payload["customer"]["phone"], "+5492613168608")
        self.assertEqual(payload["items"][0]["title"], "1 Luminaria quemada en Don Bosco 55")
        self.assertEqual(payload["items"][0]["status"], "operator_review")
        serialized_payload = json.dumps(payload, ensure_ascii=False)
        self.assertNotIn("texto_original", serialized_payload)
        self.assertNotIn("operator_pack", serialized_payload)
        self.assertNotIn("mensaje interno", serialized_payload)

    def test_claim_tracking_requires_pin(self):
        response = self.client.get("/api/public/tracking/experience?kind=claim&code=M-123456")

        self.assertEqual(response.status_code, 400)
        payload = response.get_json()
        self.assertEqual(payload["reason_code"], "tracking_pin_required")
        self.assertEqual(payload["contract_version"], "tracking.experience.v1")
        self.assertIn("request_id", payload)

    def test_legacy_claim_tracking_page_requires_pin_before_rendering_private_data(self):
        rejected = self.client.get("/tracking/claim/123456")

        self.assertEqual(rejected.status_code, 403)
        self.assertNotIn(b"Cuadrilla asignada", rejected.data)

        accepted = self.client.get("/tracking/claim/123456?pin=654321")
        self.assertEqual(accepted.status_code, 200)
        self.assertIn(b"Cuadrilla asignada", accepted.data)
        self.assertIn(b'pin: "654321"', accepted.data)

    def test_legacy_claim_message_endpoint_requires_pin_when_ticket_has_pin(self):
        rejected = self.client.post(
            "/tracking/api/send-claim-message",
            json={"nro_ticket": "123456", "mensaje": "hola"},
        )
        self.assertEqual(rejected.status_code, 403)

        accepted = self.client.post(
            "/tracking/api/send-claim-message?pin=654321",
            json={"nro_ticket": "123456", "mensaje": "sumo informacion"},
        )
        self.assertEqual(accepted.status_code, 200)
        self.assertIsNotNone(
            TicketComentario.query.filter_by(
                municipio_ticket_id=self.claim.id,
                comentario="sumo informacion",
            ).first()
        )


if __name__ == "__main__":
    unittest.main()
