import os
import unittest
from datetime import datetime, timedelta, timezone

import jwt

os.environ.setdefault("FLASK_SKIP_GLOBAL_APP", "1")

from app import create_app, db
from config import Config
from models import AnalyticsEventV2, ChatSessionContext, EncEncuesta, EncRespuesta, TenantProfile, TenantTicket, TicketRealtimeState, User


class V2OperationalAnalyticsTestConfig(Config):
    TESTING = True
    ENABLE_DEMO_MODE = True
    SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"
    SQLALCHEMY_ENGINE_OPTIONS = {"connect_args": {"check_same_thread": False}}
    ENABLE_RUNTIME_SCHEMA_SYNC = False
    ENABLE_RUNTIME_TENANT_INIT = False


class V2OperationalAnalyticsTest(unittest.TestCase):
    def setUp(self):
        self.app = create_app(V2OperationalAnalyticsTestConfig)
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()
        self.client = self.app.test_client()

        self.admin = User(name="ops-admin", email="ops-admin@test.com", rol="admin", tenant_slug="ops-tenant")
        self.admin.set_password("secret123")
        db.session.add(self.admin)
        db.session.flush()

        self.tenant = TenantProfile(slug="ops-tenant", nombre="Ops Tenant", tipo="municipio", municipio_id=self.admin.id)
        db.session.add(self.tenant)
        db.session.flush()
        self.admin.tenant_id = self.tenant.id

        self.employee = User(
            name="Operador Centro",
            email="operador@test.com",
            password_hash="hash",
            rol="empleado",
            tenant_id=self.tenant.id,
            tenant_slug=self.tenant.slug,
            es_empleado=True,
            accesibilidad={"employee_scope": {"categorias": ["reclamos"], "channels": ["whatsapp"], "zonas": ["centro"]}},
        )
        db.session.add(self.employee)
        db.session.flush()

        now = datetime.now(timezone.utc)
        self.ticket = TenantTicket(
            tenant_id=self.tenant.id,
            user_id=self.admin.id,
            categoria="reclamos",
            descripcion="Bache con foto enviado por WhatsApp",
            estado="nuevo",
            origen="whatsapp",
            latitud=-34.6037,
            longitud=-58.3816,
            datos_extra={
                "title": "Bache en centro",
                "priority": "high",
                "channel": "whatsapp",
                "assignee_id": self.employee.id,
                "sla_status": "breached",
                "zone": "centro",
            },
        )
        db.session.add(self.ticket)
        db.session.flush()

        encuesta = EncEncuesta(
            tenant_id=self.tenant.id,
            slug="voto-plaza",
            titulo="Votacion plaza",
            tipo="votacion",
            estado="publicada",
            es_votacion_envivo=True,
            mostrar_resultados_envivo=True,
        )
        db.session.add(encuesta)
        db.session.flush()
        db.session.add(
            EncRespuesta(
                encuesta_id=encuesta.id,
                tenant_id=self.tenant.id,
                huella_unica="resp-1",
                lat=-34.604,
                lng=-58.382,
                canal="widget",
                barrio="Centro",
                submitted_at=now,
            )
        )
        db.session.add(
            AnalyticsEventV2(
                tenant_id=self.tenant.id,
                tenant_type="municipio",
                channel="whatsapp",
                event_name="message_in",
                session_id="session-ops",
                lat=-34.6038,
                lng=-58.3817,
                ts=now,
            )
        )
        db.session.add(
            ChatSessionContext(
                chat_session_id="session-ops",
                tenant_id=self.tenant.id,
                user_id=self.admin.id,
                context_data={"source": "whatsapp"},
                last_updated=now,
            )
        )
        db.session.add(
            TicketRealtimeState(
                ticket_type="tenant_ticket",
                ticket_id=self.ticket.id,
                viewer_key="employee:1",
                viewer_user_id=self.employee.id,
                viewer_role="empleado",
                active_session_id="session-ops",
                presence_status="active",
                last_presence_at=now,
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
        return {"Authorization": f"Bearer {token}", "X-Tenant-Slug": self.tenant.slug}

    def test_operations_dashboard_unifies_operational_metrics(self):
        response = self.client.get(
            "/api/v2/analytics/operations/dashboard",
            headers={**self._auth(), "X-Request-Id": "ops-dashboard-1"},
        )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload.get("contract_version"), "operations.dashboard.v1")
        self.assertEqual(payload.get("request_id"), "ops-dashboard-1")
        self.assertEqual(response.headers.get("X-Request-Id"), "ops-dashboard-1")
        self.assertEqual((payload.get("summary") or {}).get("open_tickets"), 1)
        self.assertEqual((payload.get("summary") or {}).get("overdue_tickets"), 1)
        self.assertEqual((payload.get("surveys") or {}).get("summary", {}).get("votaciones_live"), 1)
        self.assertEqual((payload.get("chats") or {}).get("summary", {}).get("whatsapp_messages"), 1)
        self.assertEqual((payload.get("employees") or {}).get("summary", {}).get("employees"), 1)
        self.assertTrue((payload.get("maps") or {}).get("heatmap", {}).get("hotspots"))
        self.assertEqual((payload.get("trends") or {}).get("contract_version"), "operations.trends.v1")
        self.assertTrue(payload.get("next_best_actions"))
        self.assertTrue(any(alert.get("reason_code") == "tickets_overdue" for alert in payload.get("alerts") or []))

    def test_operations_heatmap_returns_points_cells_and_layers(self):
        response = self.client.get("/api/v2/analytics/operations/heatmap", headers=self._auth())

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload.get("contract_version"), "operations.heatmap.v1")
        self.assertEqual((payload.get("summary") or {}).get("ticket_points"), 1)
        self.assertGreaterEqual((payload.get("summary") or {}).get("points"), 3)
        self.assertTrue(payload.get("cells"))
        self.assertIn("tickets", (payload.get("render_contract") or {}).get("layers") or [])
        self.assertIn("surveys", (payload.get("render_contract") or {}).get("layers") or [])
        self.assertIn("analytics_events", (payload.get("render_contract") or {}).get("layers") or [])

    def test_operations_heatmap_empty_without_real_coordinates(self):
        empty_admin = User(name="empty-admin", email="empty-admin@test.com", rol="admin", tenant_slug="empty-tenant")
        empty_admin.set_password("secret123")
        db.session.add(empty_admin)
        db.session.flush()
        empty_tenant = TenantProfile(slug="empty-tenant", nombre="Empty Tenant", tipo="municipio", municipio_id=empty_admin.id)
        db.session.add(empty_tenant)
        db.session.flush()
        empty_admin.tenant_id = empty_tenant.id
        db.session.add(
            TenantTicket(
                tenant_id=empty_tenant.id,
                user_id=empty_admin.id,
                categoria="reclamos",
                descripcion="Reclamo sin coordenadas",
                estado="nuevo",
                origen="web",
                datos_extra={"title": "Sin geo", "priority": "high", "channel": "web"},
            )
        )
        db.session.commit()

        token = jwt.encode(
            {
                "user_id": empty_admin.id,
                "rol": empty_admin.rol,
                "tenant_slug": empty_admin.tenant_slug,
                "exp": datetime.utcnow() + timedelta(hours=1),
            },
            self.app.config["SECRET_KEY"],
            algorithm="HS256",
        )
        response = self.client.get(
            "/api/v2/analytics/operations/heatmap",
            headers={"Authorization": f"Bearer {token}", "X-Tenant-Slug": empty_tenant.slug},
        )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload.get("contract_version"), "operations.heatmap.v1")
        self.assertEqual((payload.get("summary") or {}).get("points"), 0)
        self.assertEqual(payload.get("points"), [])
        self.assertEqual(payload.get("cells"), [])
        self.assertEqual(payload.get("hotspots"), [])
        self.assertFalse((payload.get("render_contract") or {}).get("can_render_heatmap"))
        self.assertEqual((payload.get("render_contract") or {}).get("state"), "empty")

    def test_operations_action_center_returns_prioritized_actions(self):
        response = self.client.get(
            "/api/v2/analytics/operations/action-center",
            headers={**self._auth(), "X-Request-Id": "ops-actions-1"},
        )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload.get("contract_version"), "operations.action_center.v1")
        self.assertEqual(payload.get("request_id"), "ops-actions-1")
        self.assertTrue(payload.get("items"))
        self.assertTrue(any(item.get("reason_code") == "tickets_overdue" for item in payload.get("items") or []))
        self.assertEqual((payload.get("frontend_contract") or {}).get("render_as"), "action_center")

    def test_operations_freshness_returns_source_diagnostics(self):
        response = self.client.get(
            "/api/v2/analytics/operations/freshness",
            headers={**self._auth(), "X-Request-Id": "ops-freshness-1"},
        )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload.get("contract_version"), "operations.freshness.v1")
        self.assertEqual(payload.get("request_id"), "ops-freshness-1")
        self.assertIn(payload.get("status"), {"fresh", "degraded", "empty"})
        self.assertTrue((payload.get("summary") or {}).get("has_operational_data"))
        self.assertTrue((payload.get("summary") or {}).get("can_render_dashboard"))
        sources = {item.get("key"): item for item in payload.get("sources") or []}
        self.assertIn("tickets", sources)
        self.assertIn("analytics_events", sources)
        self.assertIn("heatmap", sources)
        self.assertEqual(sources["tickets"].get("period_count"), 1)
        self.assertGreaterEqual(sources["heatmap"].get("period_count"), 1)
        self.assertEqual((payload.get("frontend_contract") or {}).get("render_as"), "analytics_freshness")


if __name__ == "__main__":
    unittest.main()
