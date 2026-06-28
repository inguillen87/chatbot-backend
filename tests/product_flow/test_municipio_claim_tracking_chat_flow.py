import os
import unittest
from unittest.mock import patch

os.environ.setdefault("FLASK_SKIP_GLOBAL_APP", "1")

from app import create_app, db
from config import Config
from models import MunicipioTicket, TenantProfile, TicketComentario, User


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
            name="Municipio Flow",
            email="municipio-flow@test.com",
            rol="admin",
            tipo_chat="municipio",
        )
        self.owner.set_password("secret123")
        db.session.add(self.owner)
        db.session.flush()

        self.tenant = TenantProfile(
            slug="muni-flow",
            nombre="Municipio Flow",
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
            direccion="Av. Siempre Viva 123",
            latitud=-34.6,
            longitud=-58.4,
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
        self.assertEqual(message_payload["delivery"]["admin_surface"], "tenant_claims_inbox")
        self.assertEqual(message_payload["tracking"]["support"]["conversation"]["message_count"], 2)
        emit_chat.assert_called_once()
        emitted_payload = emit_chat.call_args.args[0]
        self.assertEqual(emitted_payload["tenant_type"], "municipio")
        self.assertEqual(emitted_payload["tenant_id"], self.tenant.id)
        self.assertEqual(emitted_payload["municipio_id"], self.owner.id)
        self.assertEqual(emitted_payload["ticket_id"], self.ticket.id)

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
