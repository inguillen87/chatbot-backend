import json
import os
import unittest

os.environ.setdefault("FLASK_SKIP_GLOBAL_APP", "1")

from app import create_app, db
from config import Config
from models import MunicipioTicket, PymePedido, TenantProfile, TicketComentario, User


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
            configuracion={"widget_tokens": ["track-token"]},
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

    def test_claim_tracking_requires_pin(self):
        response = self.client.get("/api/public/tracking/experience?kind=claim&code=M-123456")

        self.assertEqual(response.status_code, 400)
        payload = response.get_json()
        self.assertEqual(payload["reason_code"], "tracking_pin_required")
        self.assertEqual(payload["contract_version"], "tracking.experience.v1")
        self.assertIn("request_id", payload)


if __name__ == "__main__":
    unittest.main()
