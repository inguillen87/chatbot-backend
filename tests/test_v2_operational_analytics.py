import json
import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import jwt

os.environ.setdefault("FLASK_SKIP_GLOBAL_APP", "1")

from app import create_app, db
from config import Config
import config.feature_flags as feature_flags
from models import (
    AnalyticsEventV2,
    ChatSessionContext,
    EncEncuesta,
    EncLink,
    EncRespuesta,
    MarketOrder,
    MunicipioTicket,
    Order,
    PedidoConversacional,
    PymePedido,
    PymeTicket,
    TenantProfile,
    TenantTicket,
    TicketRealtimeState,
    User,
)


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
        self.hf_zero_shot_patch = patch("services.huggingface_ai_insights.classify_zero_shot", return_value=None)
        self.hf_zero_shot_patch.start()

        self.admin = User(name="Mauricio Junin", email="mauricio@junin.com", rol="admin", tenant_slug="junin")
        self.admin.set_password("123456")
        db.session.add(self.admin)
        db.session.flush()

        self.tenant = TenantProfile(slug="junin", nombre="Municipalidad de Junin", tipo="municipio", municipio_id=self.admin.id, plan="full")
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
                "genero": "femenino",
                "edad": 67,
            },
        )
        db.session.add(self.ticket)
        db.session.flush()
        db.session.add(
            MunicipioTicket(
                municipio_id=self.admin.id,
                tenant_id=None,
                pregunta="Arbol caido sobre calle municipal",
                asunto="Arbol caido",
                categoria="arbolado",
                estado="nuevo",
                canal_ingreso="whatsapp",
                distrito="Centro",
                latitud=-34.6034,
                longitud=-58.3812,
                detalles='{"genero": "no_binario", "edad": 45}',
                fecha=now,
            )
        )
        db.session.add(
            MunicipioTicket(
                municipio_id=self.admin.id,
                tenant_id=None,
                pregunta="Reclamo historico con ubicacion",
                asunto="Reclamo historico",
                categoria="historico",
                estado="cerrado",
                canal_ingreso="web",
                distrito="La Colonia",
                latitud=-34.61,
                longitud=-58.39,
                detalles='{"genero": "femenino", "edad": 39}',
                fecha=now - timedelta(days=800),
            )
        )

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
        db.session.add(EncLink(encuesta_id=encuesta.id, slug_publico="voto-plaza-publica", canal="whatsapp"))
        db.session.add(
            EncRespuesta(
                encuesta_id=encuesta.id,
                tenant_id=self.tenant.id,
                huella_unica="resp-1",
                lat=-34.604,
                lng=-58.382,
                canal="widget",
                barrio="Centro",
                genero="masculino",
                edad=34,
                metadata_payload={"categoria": "votacion_plaza"},
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
                metadata_payload={"categoria": "consulta", "gender": "femenino", "age": 22},
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
        self.assisted_order = PedidoConversacional(
            tenant_id=self.tenant.id,
            user_id=self.admin.id,
            estado="pendiente_revision",
            tipo="compra",
            origen="marketplace_upload",
            items=[],
            metadata_payload={
                "needs_operator_review": True,
                "match_summary": {
                    "matched": 1,
                    "unmatched": 2,
                    "detected": 3,
                    "needs_operator_review": True,
                },
                "customer": {
                    "name": "Cliente sensible",
                    "email": "cliente.sensible@example.com",
                    "phone": "+5492610000000",
                },
            },
        )
        db.session.add(self.assisted_order)
        db.session.commit()

    def tearDown(self):
        self.hf_zero_shot_patch.stop()
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
        self.assertEqual((payload.get("summary") or {}).get("open_tickets"), 2)
        self.assertEqual((payload.get("summary") or {}).get("overdue_tickets"), 1)
        self.assertEqual((payload.get("summary") or {}).get("assisted_orders"), 1)
        self.assertEqual((payload.get("summary") or {}).get("orders_needing_review"), 1)
        self.assertEqual((payload.get("summary") or {}).get("unmatched_order_items"), 2)
        self.assertEqual((payload.get("surveys") or {}).get("summary", {}).get("votaciones_live"), 1)
        live_control = (payload.get("surveys") or {}).get("live_control_room") or {}
        self.assertEqual(live_control.get("contract_version"), "operations.survey_live_control_room.v1")
        self.assertEqual((live_control.get("summary") or {}).get("live_surveys"), 1)
        self.assertEqual((live_control.get("summary") or {}).get("responses_with_geo"), 1)
        self.assertEqual((live_control.get("realtime") or {}).get("refresh_seconds"), 10)
        self.assertIn("survey.vote.created", (live_control.get("realtime") or {}).get("socket_events") or [])
        monitor = (live_control.get("monitors") or [])[0]
        self.assertEqual(monitor.get("slug"), "voto-plaza")
        self.assertEqual(monitor.get("public_token"), "voto-plaza-publica")
        self.assertEqual(monitor.get("public_url"), "/e/voto-plaza-publica")
        self.assertEqual(monitor.get("admin_url"), "/admin/encuestas/1/analytics?focus=live")
        self.assertEqual(monitor.get("live_results_endpoint"), "/api/v2/public/surveys/voto-plaza-publica/live-results")
        self.assertEqual(monitor.get("whatsapp_template_id"), "gov_survey_invite")
        live_response = self.client.get(f"{monitor.get('live_results_endpoint')}?include_heatmap=0")
        self.assertEqual(live_response.status_code, 200)
        self.assertEqual(live_response.get_json().get("contract_version"), "surveys.live_results.v2")
        self.assertTrue(any(action.get("id") == "sync_whatsapp_survey_template" for action in live_control.get("actions") or []))
        self.assertEqual((payload.get("chats") or {}).get("summary", {}).get("whatsapp_messages"), 1)
        commerce = payload.get("commerce") or {}
        self.assertEqual(commerce.get("contract_version"), "operations.commerce.v1")
        self.assertEqual((commerce.get("summary") or {}).get("orders"), 1)
        self.assertEqual((commerce.get("summary") or {}).get("assisted_orders"), 1)
        self.assertEqual((commerce.get("summary") or {}).get("orders_needing_review"), 1)
        self.assertEqual((commerce.get("summary") or {}).get("unmatched_items"), 2)
        self.assertEqual((commerce.get("frontend_contract") or {}).get("render_as"), "commerce_assisted_ops")
        review_item = (commerce.get("review_items") or [])[0]
        self.assertEqual(review_item.get("ui_hint"), "open_assisted_order_review")
        self.assertEqual(
            review_item.get("endpoint"),
            f"/api/admin/tenants/{self.tenant.slug}/orders/conversational:{self.assisted_order.id}",
        )
        self.assertIn("/perfil?tab=pedidos", review_item.get("frontend_path") or "")
        self.assertIn("focus=assisted_order_queue", review_item.get("frontend_path") or "")
        self.assertTrue((review_item.get("pii") or {}).get("redacted"))
        self.assertEqual((payload.get("employees") or {}).get("summary", {}).get("employees"), 1)
        self.assertTrue((payload.get("maps") or {}).get("heatmap", {}).get("hotspots"))
        self.assertEqual((payload.get("trends") or {}).get("contract_version"), "operations.trends.v1")
        self.assertTrue(payload.get("next_best_actions"))
        self.assertTrue(any(action.get("id") == "review_assisted_orders" for action in payload.get("next_best_actions") or []))
        for action in payload.get("next_best_actions") or []:
            self.assertTrue(action.get("href"))
            self.assertEqual(action.get("frontend_path"), action.get("href"))
            self.assertFalse(str(action.get("href")).startswith("/api/"))
        actions_by_id = {action.get("id"): action for action in payload.get("next_best_actions") or []}
        self.assertIn("/perfil?tab=tickets", actions_by_id.get("review_overdue_tickets", {}).get("href") or "")
        self.assertIn("/perfil?tab=pedidos", actions_by_id.get("review_assisted_orders", {}).get("href") or "")
        self.assertEqual((payload.get("ai_brief") or {}).get("contract_version"), "operations.ai_brief.v1")
        self.assertEqual((payload.get("ai_brief") or {}).get("severity"), "high")
        self.assertTrue((payload.get("ai_brief") or {}).get("focus_items"))
        focus_ids = {item.get("id") for item in (payload.get("ai_brief") or {}).get("focus_items") or []}
        self.assertIn("orders_needing_review", focus_ids)
        self.assertEqual(((payload.get("ai_brief") or {}).get("signals") or {}).get("orders_needing_review"), 1)
        self.assertIn(
            "ai_brief",
            ((payload.get("frontend_contract") or {}).get("exports") or {}),
        )
        self.assertTrue(any(alert.get("reason_code") == "tickets_overdue" for alert in payload.get("alerts") or []))
        self.assertTrue(any(alert.get("reason_code") == "assisted_orders_need_review" for alert in payload.get("alerts") or []))

    def test_queue_truth_keeps_full_backlog_and_reports_sla_unknowns(self):
        now = datetime.now(timezone.utc)
        old_open = TenantTicket(
            tenant_id=self.tenant.id,
            user_id=self.admin.id,
            categoria="alumbrado",
            descripcion="Reclamo abierto anterior a la ventana del dashboard",
            estado="nuevo",
            origen="whatsapp",
            datos_extra={},
            created_at=now - timedelta(days=30),
            updated_at=now - timedelta(days=30),
        )
        expired_due = TenantTicket(
            tenant_id=self.tenant.id,
            user_id=self.admin.id,
            categoria="agua",
            descripcion="SLA vencido respaldado solo por due_at",
            estado="nuevo",
            origen="web",
            datos_extra={
                "sla": {
                    "resolution_due_at": (now - timedelta(hours=2)).isoformat(),
                }
            },
        )
        foreign_owner = User(
            name="Operador ajeno",
            email="foreign-queue-truth@example.com",
            rol="admin",
            tenant_slug="foreign-queue-truth",
        )
        foreign_owner.set_password("123456")
        db.session.add(foreign_owner)
        db.session.flush()
        foreign_tenant = TenantProfile(
            slug="foreign-queue-truth",
            nombre="Tenant ajeno a la cola",
            tipo="pyme",
            pyme_id=foreign_owner.id,
            plan="full",
        )
        db.session.add_all([old_open, expired_due, foreign_tenant])
        db.session.flush()
        db.session.add(
            TenantTicket(
                tenant_id=foreign_tenant.id,
                categoria="privado",
                descripcion="foreign queue truth secret",
                estado="nuevo",
                origen="web",
                datos_extra={"sla_status": "breached"},
            )
        )
        db.session.commit()

        import routes.v2.analytics as analytics_routes

        analytics_routes._clear_operations_dashboard_cache_for_tests()
        response = self.client.get(
            "/api/v2/analytics/operations/dashboard?days=7",
            headers=self._auth(),
        )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        queue_truth = payload.get("queue_truth") or {}
        self.assertEqual(queue_truth.get("contract_version"), "operations.queue_truth.v1")
        self.assertEqual(queue_truth.get("grain"), "one_current_open_ticket")
        self.assertTrue(queue_truth.get("as_of"))
        self.assertEqual(
            queue_truth.get("source_models"),
            ["TenantTicket", "MunicipioTicket", "PymeTicket"],
        )

        snapshot = queue_truth.get("queue_snapshot") or {}
        snapshot_summary = snapshot.get("summary") or {}
        self.assertEqual(snapshot_summary.get("open_total"), 4)
        self.assertEqual(snapshot_summary.get("sla_breached"), 2)
        self.assertEqual(snapshot_summary.get("sla_unknown"), 2)
        self.assertEqual(snapshot_summary.get("unassigned"), 3)

        sla = snapshot.get("sla") or {}
        self.assertEqual(sla.get("eligible"), 4)
        self.assertEqual(sla.get("known"), 2)
        self.assertEqual(sla.get("unknown"), 2)
        self.assertEqual(sla.get("breached"), 2)
        self.assertEqual(sla.get("numerator"), 2)
        self.assertEqual(sla.get("denominator"), 2)
        self.assertEqual(sla.get("breach_rate_pct"), 100.0)

        age_buckets = {item.get("key"): item for item in snapshot.get("age_buckets") or []}
        self.assertEqual((age_buckets.get("gte_7d") or {}).get("count"), 1)
        self.assertIn("/perfil?tab=tickets", (age_buckets.get("gte_7d") or {}).get("href") or "")
        self.assertFalse((age_buckets.get("gte_7d") or {}).get("exact_filter"))
        ownership = snapshot.get("ownership") or {}
        self.assertIn("agent=unassigned", ownership.get("unassigned_href") or "")
        by_owner = ownership.get("by_owner") or []
        self.assertTrue(by_owner)
        self.assertTrue(all(item.get("link_semantics") == "navigation_only" for item in by_owner))
        self.assertTrue(all(item.get("exact_filter") is False for item in by_owner))
        self.assertNotIn("sla=", ((snapshot.get("links") or {}).get("sla_breached") or ""))
        link_contract = snapshot.get("link_contract") or {}
        self.assertFalse(((link_contract.get("sla_breached") or {}).get("exact_filter")))
        self.assertEqual(
            link_contract.get("unassigned"),
            {"semantics": "navigation_only", "exact_filter": False},
        )
        self.assertEqual(
            link_contract.get("ownership_by_owner"),
            {"semantics": "navigation_only", "exact_filter": False},
        )
        self.assertEqual(
            link_contract.get("reason_code"),
            "operational_queue_v1_not_yet_bound_to_queue_truth_snapshot",
        )
        self.assertIn("drilldowns exactos", link_contract.get("notice") or "")

        period_flow = queue_truth.get("period_flow") or {}
        self.assertEqual(period_flow.get("grain"), "one_ticket_created_in_period")
        self.assertEqual((period_flow.get("summary") or {}).get("created_total"), 3)
        self.assertIn("historical_backlog_snapshot", period_flow.get("does_not_measure") or [])
        self.assertEqual((payload.get("summary") or {}).get("open_tickets"), 4)
        self.assertEqual((payload.get("summary") or {}).get("overdue_tickets"), 2)
        unavailable_trends = {item.get("key") for item in (payload.get("trends") or {}).get("unavailable") or []}
        self.assertEqual(unavailable_trends, {"open_tickets", "overdue_tickets"})
        self.assertNotIn("foreign queue truth secret", json.dumps(payload).lower())

    def test_queue_truth_applies_as_of_membership_and_quarantines_future_dates(self):
        now = datetime.now(timezone.utc)
        future_at = now + timedelta(days=30)
        future_tenant = TenantTicket(
            tenant_id=self.tenant.id,
            user_id=self.admin.id,
            categoria="futuro",
            descripcion="No debe entrar al corte operativo",
            estado="nuevo",
            origen="web",
            created_at=future_at,
            updated_at=future_at,
        )
        future_municipio = MunicipioTicket(
            tenant_id=self.tenant.id,
            municipio_id=self.admin.id,
            pregunta="Registro municipal con fecha futura",
            asunto="Futuro municipio",
            categoria="futuro",
            estado="nuevo",
            canal_ingreso="web",
            fecha=future_at,
        )
        future_pyme = PymeTicket(
            tenant_id=self.tenant.id,
            pregunta="Registro pyme con fecha futura",
            asunto="Futuro pyme",
            categoria="futuro",
            estado="nuevo",
            nro_ticket=987654320,
            fecha=future_at,
        )
        null_created_at = PymeTicket(
            tenant_id=self.tenant.id,
            pregunta="Registro legado sin fecha",
            asunto="Legado sin fecha",
            categoria="legado",
            estado="nuevo",
            nro_ticket=987654321,
            fecha=now,
        )
        db.session.add_all(
            [future_tenant, future_municipio, future_pyme, null_created_at]
        )
        db.session.flush()
        null_created_at.fecha = None
        db.session.commit()

        import routes.v2.analytics as analytics_routes

        analytics_routes._clear_operations_dashboard_cache_for_tests()
        response = self.client.get(
            "/api/v2/analytics/operations/dashboard?days=7",
            headers=self._auth(),
        )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        queue_truth = payload.get("queue_truth") or {}
        self.assertEqual((queue_truth.get("queue_snapshot") or {}).get("summary", {}).get("open_total"), 3)
        quality = queue_truth.get("membership_quality") or {}
        self.assertEqual(
            quality.get("creation_membership"),
            "created_at_null_or_lte_as_of",
        )
        self.assertEqual(
            (quality.get("null_created_at") or {}).get("included_records"),
            1,
        )
        future_quality = quality.get("future_created_at") or {}
        self.assertEqual(future_quality.get("state"), "quarantined")
        self.assertEqual(future_quality.get("excluded_records"), 3)
        self.assertEqual(
            {
                item.get("source_model"): item.get("excluded_records")
                for item in future_quality.get("by_source_model") or []
            },
            {"TenantTicket": 1, "MunicipioTicket": 1, "PymeTicket": 1},
        )

    def test_operations_commerce_unifies_normal_orders_and_maps_only_privacy_safe_locations(self):
        legacy = PymePedido(
            pyme_id=self.admin.id,
            tenant_id=self.tenant.id,
            asunto="Pedido ferreteria",
            detalles='[{"nombre": "Clavos", "cantidad": 2}]',
            monto_total=1500,
            nombre_cliente="Cliente privado legacy",
            email_cliente="legacy-private@example.com",
            direccion="Direccion legacy secreta 123",
            latitud=-34.605,
            longitud=-58.383,
        )
        canonical = Order(
            tenant_id=self.tenant.id,
            buyer_name="Cliente privado canonical",
            buyer_email="canonical-private@example.com",
            buyer_phone="+5492611111111",
            status="created",
            channel="web_widget",
            total=2500,
            delivery_address={
                "address": "Direccion canonical secreta 456",
                "coordinates": {"lat": -34.606, "lng": -58.384},
            },
        )
        mirror = MarketOrder(
            tenant_id=self.tenant.id,
            status="pending",
            channel="marketplace",
            external_provider="pedido_conversacional",
            external_order_id=str(self.assisted_order.id),
            total_monetary=0,
            currency="ARS",
        )
        foreign_tenant = TenantProfile(
            slug="foreign-commerce",
            nombre="Foreign Commerce",
            tipo="pyme",
            pyme_id=self.admin.id,
            plan="full",
        )
        db.session.add(foreign_tenant)
        db.session.flush()
        foreign_legacy = PymePedido(
            pyme_id=self.admin.id,
            tenant_id=foreign_tenant.id,
            asunto="Pedido de otro tenant",
            detalles="[]",
            monto_total=9999,
            latitud=-34.607,
            longitud=-58.385,
        )
        db.session.add_all([legacy, canonical, mirror, foreign_legacy])
        db.session.commit()

        dashboard_response = self.client.get("/api/v2/analytics/operations/dashboard", headers=self._auth())
        self.assertEqual(dashboard_response.status_code, 200)
        commerce = dashboard_response.get_json().get("commerce") or {}
        summary = commerce.get("summary") or {}
        self.assertEqual(summary.get("source_records"), 4)
        self.assertEqual(summary.get("orders"), 3)
        self.assertEqual(summary.get("deduplicated_mirrors"), 1)
        self.assertEqual(summary.get("assisted_orders"), 1)
        self.assertEqual(summary.get("orders_needing_review"), 1)
        self.assertEqual(summary.get("total_monetary"), 4000.0)
        self.assertEqual(summary.get("currency"), "ARS")
        self.assertEqual(commerce.get("totals_by_currency"), [
            {"key": "ARS", "label": "ARS", "currency": "ARS", "amount": 4000.0, "count": 3},
        ])
        source_counts = {item.get("key"): item.get("count") for item in commerce.get("by_source_model") or []}
        self.assertEqual(source_counts.get("MarketOrder"), 1)
        self.assertEqual(source_counts.get("PymePedido"), 1)
        self.assertEqual(source_counts.get("Order"), 1)

        heatmap_response = self.client.get(
            "/api/v2/analytics/operations/heatmap?source=orders&include_ai=0",
            headers=self._auth(),
        )
        self.assertEqual(heatmap_response.status_code, 200)
        heatmap = heatmap_response.get_json()
        self.assertEqual((heatmap.get("summary") or {}).get("commerce_points"), 2)
        self.assertEqual((heatmap.get("summary") or {}).get("points"), 2)
        self.assertIn("commerce_activity", (heatmap.get("render_contract") or {}).get("layers") or [])
        self.assertIn(
            "commerce_activity",
            {item.get("id") for item in (heatmap.get("map_layers") or {}).get("layers") or []},
        )
        commerce_quality = ((heatmap.get("source_quality") or {}).get("sources") or {}).get("commerce") or {}
        self.assertEqual(commerce_quality.get("records"), 3)
        self.assertEqual(commerce_quality.get("points"), 2)
        self.assertEqual(commerce_quality.get("privacy_mode"), "coordinates_without_customer_pii")
        for point in heatmap.get("points") or []:
            self.assertEqual(point.get("source"), "commerce")
            self.assertTrue((point.get("privacy") or {}).get("customer_pii_redacted"))

        serialized_heatmap = json.dumps(heatmap).lower()
        for secret in (
            "cliente privado",
            "legacy-private@example.com",
            "canonical-private@example.com",
            "+5492611111111",
            "direccion legacy secreta",
            "direccion canonical secreta",
        ):
            self.assertNotIn(secret, serialized_heatmap)

        detail_response = self.client.get(
            f"/api/admin/tenants/{self.tenant.slug}/orders/order:{canonical.id}",
            headers=self._auth(),
        )
        self.assertEqual(detail_response.status_code, 200)
        self.assertEqual(detail_response.get_json().get("source_model"), "Order")

    def test_operations_excludes_explicit_foreign_municipio_ticket_with_shared_owner(self):
        foreign_tenant = TenantProfile(
            slug="foreign-municipio",
            nombre="Municipio ajeno",
            tipo="municipio",
            municipio_id=self.admin.id,
            plan="full",
        )
        db.session.add(foreign_tenant)
        db.session.flush()
        db.session.add(
            MunicipioTicket(
                municipio_id=self.admin.id,
                tenant_id=foreign_tenant.id,
                pregunta="Dato territorial de otro tenant",
                asunto="No debe aparecer",
                categoria="privado",
                estado="nuevo",
                canal_ingreso="whatsapp",
                latitud=-34.7,
                longitud=-58.5,
                fecha=datetime.now(timezone.utc),
            )
        )
        db.session.commit()

        response = self.client.get("/api/v2/analytics/operations/dashboard", headers=self._auth())

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        # Once the legacy owner maps to two profiles, its tenant_id=NULL row
        # is intentionally quarantined; only the exact TenantTicket remains.
        self.assertEqual((payload.get("summary") or {}).get("open_tickets"), 1)
        self.assertNotIn("dato territorial de otro tenant", json.dumps(payload).lower())

    def test_commerce_dedupe_inherits_amount_and_currency_from_complete_source(self):
        from services.operational_intelligence import _collect_commerce_records

        source_order = PedidoConversacional(
            tenant_id=self.tenant.id,
            user_id=self.admin.id,
            estado="pendiente_revision",
            tipo="order_note",
            origen="marketplace_upload",
            monto_monetario=125,
            items=[],
            metadata_payload={"currency": "USD", "request_kind": "order_note"},
        )
        db.session.add(source_order)
        db.session.flush()
        mirror = MarketOrder(
            tenant_id=self.tenant.id,
            status="pending",
            channel="marketplace",
            external_provider="pedido_conversacional",
            external_order_id=str(source_order.id),
            total_monetary=0,
            currency="ARS",
        )
        db.session.add(mirror)
        db.session.commit()

        now = datetime.now(timezone.utc)
        records, raw_count = _collect_commerce_records(
            self.tenant,
            now - timedelta(days=1),
            now + timedelta(days=1),
        )
        merged = next(
            item
            for item in records
            if item.get("source_id") == mirror.id and item.get("source_model") == "MarketOrder"
        )

        self.assertGreaterEqual(raw_count, 3)
        self.assertEqual(merged.get("total"), 125.0)
        self.assertEqual(merged.get("currency"), "USD")
        self.assertEqual(merged.get("request_kind"), "order_note")

    def test_commerce_request_kinds_preserve_supported_document_classes(self):
        from services.operational_intelligence import _commerce_request_kind

        supported = {
            "order_note",
            "handwritten_order",
            "quote_request",
            "receipt",
            "tax_bill",
            "certificate",
            "service_request",
            "other",
        }

        self.assertEqual({_commerce_request_kind(value) for value in supported}, supported)

    def test_operations_heatmap_returns_points_cells_and_layers(self):
        response = self.client.get("/api/v2/analytics/operations/heatmap", headers=self._auth())

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload.get("contract_version"), "operations.heatmap.v1")
        self.assertEqual((payload.get("summary") or {}).get("ticket_points"), 2)
        self.assertGreaterEqual((payload.get("summary") or {}).get("points"), 4)
        self.assertTrue(payload.get("cells"))
        self.assertIn("tickets", (payload.get("render_contract") or {}).get("layers") or [])
        self.assertIn("surveys", (payload.get("render_contract") or {}).get("layers") or [])
        self.assertIn("analytics_events", (payload.get("render_contract") or {}).get("layers") or [])
        self.assertIn("ai_risk", (payload.get("render_contract") or {}).get("layers") or [])
        declared_layers = set((payload.get("render_contract") or {}).get("layers") or [])
        point_layers = {point.get("layer") for point in payload.get("points") or [] if point.get("layer")}
        self.assertTrue({"tickets", "surveys", "analytics_events"}.issubset(point_layers))
        self.assertTrue(point_layers.issubset(declared_layers))
        self.assertIn("interactive_globe", (payload.get("render_contract") or {}).get("recommended_views") or [])
        self.assertTrue(payload.get("category_layers"))
        self.assertEqual((payload.get("ai_insights") or {}).get("contract_version"), "huggingface.ai_insights.v1")
        self.assertEqual((payload.get("ai_layers") or {}).get("contract_version"), "huggingface.map_ai_layers.v1")
        self.assertEqual((payload.get("map_experience") or {}).get("preferred_visualization"), "interactive_globe_heatmap")
        self.assertIn("deckgl", ((payload.get("ai_layers") or {}).get("frontend_contract") or {}).get("map_engines") or [])
        self.assertEqual((payload.get("map_narrative") or {}).get("contract_version"), "operations.heatmap_narrative.v1")
        self.assertEqual(((payload.get("map_narrative") or {}).get("primary_metric") or {}).get("value"), (payload.get("summary") or {}).get("points"))
        self.assertEqual((payload.get("viewport_presets") or {}).get("contract_version"), "operations.heatmap_viewport_presets.v1")
        self.assertEqual((payload.get("viewport_presets") or {}).get("default_preset_id"), "fit_operational_bounds")
        viewport_ids = {item.get("id") for item in (payload.get("viewport_presets") or {}).get("presets") or []}
        self.assertIn("top_hotspot", viewport_ids)
        self.assertEqual((payload.get("layer_style_contract") or {}).get("contract_version"), "operations.heatmap_layer_styles.v1")
        layer_ids = {item.get("id") for item in (payload.get("layer_style_contract") or {}).get("layers") or []}
        self.assertIn("base_heatmap", layer_ids)
        self.assertIn("ai_risk_layers", layer_ids)
        self.assertIn("geocoding_queue", layer_ids)
        self.assertEqual((payload.get("hotspot_actions") or {}).get("contract_version"), "operations.heatmap_hotspot_actions.v1")
        action_ids = {item.get("id") for item in (payload.get("hotspot_actions") or {}).get("actions") or []}
        self.assertIn("inspect_hotspot_1", action_ids)
        playbook_ids = {item.get("id") for item in (payload.get("hotspot_actions") or {}).get("playbook") or []}
        self.assertIn("triage_high_density_zone", playbook_ids)
        operational_hotspots = payload.get("operational_hotspots") or []
        self.assertTrue(operational_hotspots)
        self.assertEqual((payload.get("summary") or {}).get("operational_hotspots"), len(operational_hotspots))
        self.assertIn("operational_hotspots", (payload.get("render_contract") or {}).get("premium_metadata") or [])
        first_operational_hotspot = operational_hotspots[0]
        self.assertGreater(first_operational_hotspot.get("operational_score") or 0, 0)
        self.assertEqual(first_operational_hotspot.get("rank_reason"), "sla_breached")
        self.assertGreaterEqual((first_operational_hotspot.get("signals") or {}).get("breached_sla") or 0, 1)
        self.assertGreaterEqual((first_operational_hotspot.get("signals") or {}).get("tickets") or 0, 1)
        self.assertGreaterEqual((first_operational_hotspot.get("signals") or {}).get("recent_24h") or 0, 1)
        self.assertEqual((first_operational_hotspot.get("recommended_action") or {}).get("ui_hint"), "focus_map_cell_and_filter_tickets")
        self.assertEqual((payload.get("ai_status") or {}).get("contract_version"), "operations.heatmap_ai_status.v1")
        self.assertIn((payload.get("ai_status") or {}).get("status"), {"hf_active", "local_fallback"})
        self.assertTrue((payload.get("ai_status") or {}).get("safe_to_render_without_hf_token"))
        self.assertTrue((payload.get("ai_status") or {}).get("ai_layers_ready"))
        self.assertIn("map_narrative", (payload.get("render_contract") or {}).get("premium_metadata") or [])
        self.assertEqual((payload.get("map_experience") or {}).get("viewport_contract"), "operations.heatmap_viewport_presets.v1")
        self.assertEqual((payload.get("quality") or {}).get("contract_version"), "operations.heatmap_quality.v1")
        self.assertTrue((payload.get("quality") or {}).get("can_render_heatmap"))
        self.assertIn((payload.get("quality") or {}).get("state"), {"ready", "partial"})
        self.assertEqual((payload.get("realtime") or {}).get("contract_version"), "operations.heatmap_realtime.v1")
        self.assertIn("whatsapp.message.created", (payload.get("realtime") or {}).get("socket_events") or [])
        self.assertEqual((payload.get("legend") or {}).get("mode"), "category_source_quality")
        self.assertIn("geo_layers", (payload.get("render_contract") or {}).get("premium_metadata") or [])
        self.assertIn("map_layers", (payload.get("render_contract") or {}).get("premium_metadata") or [])
        self.assertIn("source_quality", (payload.get("render_contract") or {}).get("premium_metadata") or [])
        self.assertEqual((payload.get("geo_layers") or {}).get("contract_version"), "operations.heatmap_geo_layers.v1")
        self.assertEqual((payload.get("map_layers") or {}).get("contract_version"), "operations.heatmap_map_layers.v1")
        self.assertEqual((payload.get("source_quality") or {}).get("contract_version"), "operations.heatmap_source_quality.v1")
        geo_points = (((payload.get("geo_layers") or {}).get("points") or {}).get("features") or [])
        self.assertEqual(len(geo_points), (payload.get("summary") or {}).get("points"))
        self.assertEqual(((payload.get("map_layers") or {}).get("telemetry") or {}).get("event_endpoint"), "/api/analytics/event")
        self.assertEqual(
            (((payload.get("source_quality") or {}).get("sources") or {}).get("ticket") or {}).get("points"),
            (payload.get("summary") or {}).get("ticket_points"),
        )
        self.assertEqual((payload.get("demographics") or {}).get("source"), "real_metadata_only")
        self.assertGreaterEqual((payload.get("summary") or {}).get("points_with_gender"), 3)
        self.assertGreaterEqual((payload.get("summary") or {}).get("points_with_age"), 3)
        render_filters = set((payload.get("render_contract") or {}).get("segment_filters") or [])
        self.assertIn("estado", render_filters)
        self.assertIn("zona", render_filters)
        self.assertIn("sla_state", render_filters)
        self.assertIn("assignee_id", render_filters)

        ticket_point = next(
            point for point in payload.get("points") or []
            if point.get("id") == f"tenant_ticket:{self.ticket.id}"
        )
        self.assertEqual(ticket_point.get("sla_state"), "breached")
        self.assertTrue(ticket_point.get("overdue"))
        self.assertEqual(ticket_point.get("zone"), "centro")
        self.assertEqual(ticket_point.get("assignee_id"), self.employee.id)
        ticket_actions = {item.get("id"): item for item in ticket_point.get("actions") or []}
        self.assertEqual(ticket_actions.get("open_record", {}).get("endpoint"), f"/api/v2/tickets/{self.ticket.id}")
        self.assertEqual(ticket_actions.get("open_record", {}).get("frontend_path"), ticket_actions.get("open_record", {}).get("href"))
        self.assertIn("/perfil?tab=tickets", ticket_actions.get("open_record", {}).get("href") or "")
        self.assertIn(f"ticket_id={self.ticket.id}", ticket_actions.get("open_record", {}).get("href") or "")
        self.assertEqual(ticket_actions.get("update_location", {}).get("method"), "PATCH")
        self.assertEqual(ticket_actions.get("update_location", {}).get("endpoint"), f"/api/v2/tickets/{self.ticket.id}")
        self.assertIn("focus=open_geocoding_queue", ticket_actions.get("update_location", {}).get("href") or "")

        categories = {item.get("key") for item in (payload.get("segments") or {}).get("category") or []}
        genders = {item.get("key") for item in (payload.get("segments") or {}).get("gender") or []}
        age_ranges = {item.get("key") for item in (payload.get("segments") or {}).get("age_range") or []}
        statuses = {item.get("key") for item in (payload.get("segments") or {}).get("status") or []}
        zones = {item.get("key") for item in (payload.get("segments") or {}).get("zone") or []}
        sla_states = {item.get("key") for item in (payload.get("segments") or {}).get("sla_state") or []}
        self.assertIn("reclamos", categories)
        self.assertIn("arbolado", categories)
        self.assertIn("votacion_plaza", categories)
        self.assertIn("consulta", categories)
        self.assertIn("nuevo", statuses)
        self.assertIn("centro", zones)
        self.assertIn("breached", sla_states)
        self.assertIn("femenino", genders)
        self.assertIn("masculino", genders)
        self.assertIn("no_binario", genders)
        self.assertIn("18_24", age_ranges)
        self.assertIn("25_34", age_ranges)
        self.assertIn("45_59", age_ranges)
        self.assertIn("60_plus", age_ranges)

    def test_operations_heatmap_supports_limit_and_bbox(self):
        limited_response = self.client.get(
            "/api/v2/analytics/operations/heatmap?include_ai=0&limit=2",
            headers=self._auth(),
        )
        self.assertEqual(limited_response.status_code, 200)
        limited_payload = limited_response.get_json()
        self.assertEqual((limited_payload.get("quality") or {}).get("max_points"), 2)
        self.assertLessEqual((limited_payload.get("summary") or {}).get("points"), 2)
        self.assertEqual(
            len((((limited_payload.get("geo_layers") or {}).get("points") or {}).get("features") or [])),
            (limited_payload.get("summary") or {}).get("points"),
        )

        bbox_response = self.client.get(
            "/api/v2/analytics/operations/heatmap?include_ai=0&bbox=-58.38165,-34.60375,-58.38155,-34.60365",
            headers=self._auth(),
        )
        self.assertEqual(bbox_response.status_code, 200)
        bbox_payload = bbox_response.get_json()
        self.assertTrue((bbox_payload.get("spatial_filter") or {}).get("applied"))
        self.assertEqual((bbox_payload.get("summary") or {}).get("points"), 1)
        point = (bbox_payload.get("points") or [])[0]
        self.assertEqual(point.get("id"), f"tenant_ticket:{self.ticket.id}")

    def test_operations_heatmap_can_skip_synchronous_ai_for_operational_load(self):
        response = self.client.get("/api/v2/analytics/operations/heatmap?include_ai=0", headers=self._auth())

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload.get("contract_version"), "operations.heatmap.v1")
        self.assertEqual((payload.get("ai_insights") or {}).get("mode"), "deterministic_lightweight_dashboard")
        self.assertEqual(
            ((payload.get("ai_insights") or {}).get("hf_status") or {}).get("fallback_reason"),
            "lightweight_dashboard_mode",
        )
        self.assertEqual((payload.get("ai_status") or {}).get("status"), "local_fallback")
        self.assertTrue((payload.get("ai_status") or {}).get("safe_to_render_without_hf_token"))
        self.assertTrue((payload.get("ai_layers") or {}).get("layers"))

    def test_operations_heatmap_filters_by_demographics(self):
        response = self.client.get(
            "/api/v2/analytics/operations/heatmap?genero=femenino&edad=22",
            headers=self._auth(),
        )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual((payload.get("summary") or {}).get("points"), 1)
        self.assertTrue((payload.get("summary") or {}).get("filtered"))
        self.assertEqual((payload.get("applied_filters") or {}).get("gender"), ["femenino"])
        self.assertEqual((payload.get("applied_filters") or {}).get("age_range"), ["18_24"])
        point = (payload.get("points") or [])[0]
        self.assertEqual(point.get("category"), "consulta")
        self.assertEqual(point.get("gender"), "femenino")
        self.assertEqual(point.get("age_range"), "18_24")
        self.assertNotIn("age", point)

    def test_operations_heatmap_filters_by_category_and_source_alias(self):
        response = self.client.get(
            "/api/v2/analytics/operations/heatmap?categoria=reclamos&source=tickets",
            headers=self._auth(),
        )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual((payload.get("summary") or {}).get("points"), 1)
        point = (payload.get("points") or [])[0]
        self.assertEqual(point.get("category"), "reclamos")
        self.assertEqual(point.get("source"), "ticket")
        self.assertEqual((payload.get("applied_filters") or {}).get("category"), ["reclamos"])
        self.assertEqual((payload.get("applied_filters") or {}).get("source"), ["tickets"])

        operational_response = self.client.get(
            f"/api/v2/analytics/operations/heatmap?source=tickets&estado=nuevo&zona=centro&sla=breached&assignee_id={self.employee.id}",
            headers=self._auth(),
        )
        self.assertEqual(operational_response.status_code, 200)
        operational_payload = operational_response.get_json()
        self.assertEqual((operational_payload.get("summary") or {}).get("points"), 1)
        operational_point = (operational_payload.get("points") or [])[0]
        self.assertEqual(operational_point.get("id"), f"tenant_ticket:{self.ticket.id}")
        self.assertEqual((operational_payload.get("applied_filters") or {}).get("status"), ["nuevo"])
        self.assertEqual((operational_payload.get("applied_filters") or {}).get("zone"), ["centro"])
        self.assertEqual((operational_payload.get("applied_filters") or {}).get("sla_state"), ["breached"])
        self.assertEqual((operational_payload.get("applied_filters") or {}).get("assignee_id"), [str(self.employee.id)])

    def test_operations_heatmap_includes_legacy_municipio_owner_tickets(self):
        response = self.client.get(
            "/api/v2/analytics/operations/heatmap?categoria=arbolado&source=tickets",
            headers=self._auth(),
        )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual((payload.get("summary") or {}).get("points"), 1)
        point = (payload.get("points") or [])[0]
        self.assertEqual(point.get("record_source"), "municipio_ticket")
        self.assertEqual(point.get("category"), "arbolado")
        self.assertEqual(point.get("gender"), "no_binario")
        self.assertEqual(point.get("age_range"), "45_59")

    def test_operations_heatmap_can_use_full_historical_range(self):
        default_response = self.client.get(
            "/api/v2/analytics/operations/heatmap?categoria=historico&source=tickets",
            headers=self._auth(),
        )
        self.assertEqual(default_response.status_code, 200)
        self.assertEqual((default_response.get_json().get("summary") or {}).get("points"), 0)

        response = self.client.get(
            "/api/v2/analytics/operations/heatmap?range=all&categoria=historico&source=tickets",
            headers=self._auth(),
        )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual((payload.get("summary") or {}).get("points"), 1)
        point = (payload.get("points") or [])[0]
        self.assertEqual(point.get("category"), "historico")
        self.assertEqual(point.get("age_range"), "35_44")

    def test_operations_heatmap_empty_without_real_coordinates(self):
        empty_admin = User(name="empty-admin", email="empty-admin@test.com", rol="admin", tenant_slug="empty-tenant")
        empty_admin.set_password("secret123")
        db.session.add(empty_admin)
        db.session.flush()
        empty_tenant = TenantProfile(slug="empty-tenant", nombre="Empty Tenant", tipo="municipio", municipio_id=empty_admin.id, plan="full")
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
        self.assertEqual(payload.get("operational_hotspots"), [])
        self.assertEqual((payload.get("summary") or {}).get("operational_hotspots"), 0)
        self.assertFalse((payload.get("render_contract") or {}).get("can_render_heatmap"))
        self.assertEqual((payload.get("render_contract") or {}).get("state"), "empty")
        self.assertEqual((payload.get("quality") or {}).get("state"), "blocked")
        self.assertEqual((payload.get("quality") or {}).get("reason_code"), "missing_coordinates")
        self.assertEqual((payload.get("quality") or {}).get("visible_points"), 0)
        self.assertEqual((payload.get("map_narrative") or {}).get("state"), "blocked")
        self.assertEqual((payload.get("viewport_presets") or {}).get("default_preset_id"), "tenant_region_empty")
        self.assertEqual((payload.get("ai_status") or {}).get("contract_version"), "operations.heatmap_ai_status.v1")

    def test_operations_heatmap_surfaces_addresses_pending_geocode(self):
        db.session.add(
            TenantTicket(
                tenant_id=self.tenant.id,
                user_id=self.admin.id,
                categoria="limpieza",
                descripcion="Residuo voluminoso informado con direccion pero sin GPS",
                estado="cerrado",
                origen="web",
                latitud=None,
                longitud=None,
                datos_extra={
                    "title": "Residuo en vereda",
                    "address": "Av. San Martin 123, Junin",
                    "channel": "web",
                    "genero": "femenino",
                    "edad": 31,
                },
            )
        )
        db.session.commit()

        response = self.client.get(
            "/api/v2/analytics/operations/heatmap?categoria=limpieza&source=tickets",
            headers=self._auth(),
        )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual((payload.get("summary") or {}).get("points"), 0)
        self.assertEqual((payload.get("summary") or {}).get("pending_geocode"), 1)
        self.assertEqual((payload.get("geocoding") or {}).get("candidate_count"), 1)
        candidate = ((payload.get("geocoding") or {}).get("candidates") or [])[0]
        self.assertEqual(candidate.get("address"), "Av. San Martin 123, Junin")
        self.assertEqual(candidate.get("reason_code"), "address_without_coordinates")
        candidate_actions = {item.get("id"): item for item in candidate.get("actions") or []}
        self.assertEqual(candidate_actions.get("open_record", {}).get("endpoint"), f"/api/v2/tickets/{candidate.get('record_id')}")
        self.assertEqual(candidate_actions.get("open_record", {}).get("frontend_path"), candidate_actions.get("open_record", {}).get("href"))
        self.assertIn("/perfil?tab=tickets", candidate_actions.get("open_record", {}).get("href") or "")
        self.assertEqual(candidate_actions.get("update_location", {}).get("method"), "PATCH")
        self.assertEqual(candidate_actions.get("update_location", {}).get("endpoint"), f"/api/v2/tickets/{candidate.get('record_id')}")
        self.assertIn("focus=open_geocoding_queue", candidate_actions.get("update_location", {}).get("href") or "")
        self.assertTrue(candidate_actions.get("update_location", {}).get("writes_enabled"))
        self.assertIn("location.lat", candidate_actions.get("update_location", {}).get("requires") or [])
        self.assertTrue((payload.get("render_contract") or {}).get("address_geocoding"))
        self.assertEqual((payload.get("quality") or {}).get("state"), "pending_geocode")
        self.assertEqual((payload.get("quality") or {}).get("pending_geocode"), 1)
        self.assertEqual(((payload.get("geocoding") or {}).get("guidance") or {}).get("contract_version"), "operations.heatmap_geocoding_guidance.v1")
        self.assertEqual(((payload.get("geocoding") or {}).get("guidance") or {}).get("state"), "pending")
        self.assertEqual(((payload.get("geocoding") or {}).get("guidance") or {}).get("backend_external_calls"), "none")
        self.assertEqual(((payload.get("geocoding") or {}).get("recommended_action") or {}).get("method"), "dynamic")
        self.assertIn("candidates[]", ((payload.get("geocoding") or {}).get("recommended_action") or {}).get("endpoint_template") or "")
        guidance_action = {
            item.get("id"): item
            for item in ((payload.get("geocoding") or {}).get("guidance") or {}).get("recommended_actions") or []
        }.get("open_geocoding_queue") or {}
        self.assertIn("/perfil?tab=tickets", guidance_action.get("href") or "")
        self.assertEqual(guidance_action.get("frontend_path"), guidance_action.get("href"))
        viewport_ids = {item.get("id") for item in (payload.get("viewport_presets") or {}).get("presets") or []}
        self.assertIn("geocoding_queue", viewport_ids)
        self.assertEqual(((payload.get("map_narrative") or {}).get("empty_state") or {}).get("recommended_view"), "geocoding_queue")
        action_ids = {item.get("id") for item in (payload.get("hotspot_actions") or {}).get("actions") or []}
        self.assertIn("open_geocoding_queue", action_ids)
        open_queue_action = {
            item.get("id"): item for item in (payload.get("hotspot_actions") or {}).get("actions") or []
        }.get("open_geocoding_queue") or {}
        self.assertIn("/perfil?tab=tickets", open_queue_action.get("href") or "")
        self.assertEqual(open_queue_action.get("frontend_path"), open_queue_action.get("href"))
        geocode_playbook = {
            item.get("id"): item
            for item in (payload.get("hotspot_actions") or {}).get("playbook") or []
        }
        self.assertTrue((geocode_playbook.get("recover_missing_geolocation") or {}).get("enabled"))
        self.assertIn(
            "use_update_location_action",
            (geocode_playbook.get("recover_missing_geolocation") or {}).get("steps") or [],
        )

    def test_operations_executive_summary_returns_ai_contract(self):
        with patch(
            "routes.v2.analytics.generate_analytics_report",
            return_value={"summary": "Hay actividad operativa real.", "opportunities": [], "threats": [], "tone": "Civic"},
        ) as mocked_report:
            response = self.client.get(
                "/api/v2/analytics/operations/executive-summary",
                headers=self._auth(),
            )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload.get("contract_version"), "operations.executive_summary.v1")
        self.assertEqual((payload.get("ai") or {}).get("summary"), "Hay actividad operativa real.")
        self.assertEqual((payload.get("model_policy") or {}).get("contract_version"), "llm.task_policy.v1")
        self.assertEqual((payload.get("model_policy") or {}).get("task_type"), "analytics")
        self.assertTrue((payload.get("model_policy") or {}).get("backoffice_optimized"))
        self.assertEqual((payload.get("frontend_contract") or {}).get("render_as"), "operations_ai_executive_summary")
        mocked_report.assert_called_once()

    def test_operations_export_pdf_returns_downloadable_dashboard_report(self):
        response = self.client.get(
            "/api/v2/analytics/operations/export.pdf",
            headers={**self._auth(), "X-Request-Id": "ops-pdf-1"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, "application/pdf")
        self.assertEqual(response.headers.get("X-Request-Id"), "ops-pdf-1")
        body = response.data.decode("latin-1", errors="ignore")
        self.assertIn("Reporte operativo Chatboc", body)
        self.assertIn("startxref", body)

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

    def test_operations_ai_brief_returns_priority_contract_and_model_policy(self):
        response = self.client.get(
            "/api/v2/analytics/operations/ai-brief",
            headers={**self._auth(), "X-Request-Id": "ops-ai-brief-1"},
        )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload.get("contract_version"), "operations.ai_brief.v1")
        self.assertEqual(payload.get("request_id"), "ops-ai-brief-1")
        self.assertEqual(response.headers.get("X-Request-Id"), "ops-ai-brief-1")
        self.assertEqual(payload.get("severity"), "high")
        self.assertEqual(payload.get("source_contract"), "operations.dashboard.v1")
        self.assertEqual((payload.get("tenant") or {}).get("slug"), "junin")
        self.assertTrue(payload.get("focus_items"))
        self.assertTrue(payload.get("top_action"))
        self.assertEqual((payload.get("model_policy") or {}).get("contract_version"), "llm.task_policy.v1")
        self.assertEqual((payload.get("model_policy") or {}).get("task_type"), "analytics")
        self.assertEqual((payload.get("frontend_contract") or {}).get("render_as"), "operations_ai_brief")
        signal_keys = (payload.get("signals") or {}).keys()
        self.assertIn("hf_configured", signal_keys)
        self.assertIn("hf_mode", signal_keys)

    def test_operations_ai_provider_status_returns_tenant_safe_contract(self):
        with patch.dict(
            os.environ,
            {
                "OPENAI_API_KEY": "sk-ops-secret",
                "GEMINI_API_KEY": "gemini-ops-secret",
                "HUGGINGFACE_API_TOKEN": "hf_ops_secret",
                "LLM_PROVIDER_ORDER": "gemini,openai",
                "HUGGINGFACE_ENABLED": "true",
                "HUGGINGFACE_ZERO_SHOT_ENABLED": "true",
            },
            clear=False,
        ):
            response = self.client.get(
                "/api/v2/analytics/operations/ai-provider-status",
                headers={**self._auth(), "X-Request-Id": "ops-ai-provider-status-1"},
            )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        serialized = json.dumps(payload)
        self.assertEqual(payload.get("contract_version"), "ai.provider_status_public.v1")
        self.assertEqual(payload.get("request_id"), "ops-ai-provider-status-1")
        self.assertEqual((payload.get("tenant") or {}).get("slug"), "junin")
        self.assertFalse(payload.get("secret_values_exposed"))
        frontend_contract = payload.get("frontend_contract") or {}
        self.assertEqual(frontend_contract.get("render_as"), "operations_ai_provider_status")
        self.assertTrue(frontend_contract.get("access_tenant_scoped"))
        self.assertEqual(frontend_contract.get("configuration_scope"), "platform_runtime")
        self.assertNotIn("tenant_scoped", frontend_contract)
        self.assertEqual((payload.get("model_policy") or {}).get("contract_version"), "llm.task_policy.v1")
        self.assertEqual((payload.get("model_policy") or {}).get("task_type"), "analytics")
        self.assertTrue(((payload.get("providers") or {}).get("gemini") or {}).get("configured"))
        self.assertTrue(((payload.get("providers") or {}).get("huggingface") or {}).get("configured"))
        self.assertFalse((payload.get("readiness") or {}).get("chat_ready"))
        self.assertFalse((payload.get("readiness") or {}).get("specialized_ai_ready"))
        self.assertNotIn("sk-ops-secret", serialized)
        self.assertNotIn("gemini-ops-secret", serialized)
        self.assertNotIn("hf_ops_secret", serialized)

    def test_operations_dashboard_cache_reuses_refresh_burst(self):
        from routes.v2 import analytics as analytics_routes

        self.app.config["ENABLE_OPERATIONS_DASHBOARD_CACHE_FOR_TESTS"] = True
        analytics_routes._clear_operations_dashboard_cache_for_tests()
        real_builder = analytics_routes.build_operational_dashboard

        try:
            with patch("routes.v2.analytics.build_operational_dashboard", wraps=real_builder) as mocked_builder:
                responses = [
                    self.client.get("/api/v2/analytics/operations/dashboard", headers=self._auth()),
                    self.client.get("/api/v2/analytics/operations/action-center", headers=self._auth()),
                    self.client.get("/api/v2/analytics/operations/ai-brief", headers=self._auth()),
                ]

            self.assertTrue(all(response.status_code == 200 for response in responses))
            self.assertEqual(mocked_builder.call_count, 1)
            self.assertEqual(responses[0].get_json().get("contract_version"), "operations.dashboard.v1")
            self.assertEqual(responses[1].get_json().get("contract_version"), "operations.action_center.v1")
            self.assertEqual(responses[2].get_json().get("contract_version"), "operations.ai_brief.v1")
        finally:
            analytics_routes._clear_operations_dashboard_cache_for_tests()
            self.app.config["ENABLE_OPERATIONS_DASHBOARD_CACHE_FOR_TESTS"] = False

    def test_operations_ai_ops_queue_disabled_by_feature_flag(self):
        with patch.object(feature_flags, "FEATURE_AI_OPS_QUEUE", False):
            response = self.client.get(
                "/api/v2/analytics/operations/ai-ops-queue",
                headers={**self._auth(), "X-Request-Id": "ops-ai-queue-disabled"},
            )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload.get("contract_version"), "operations.ai_ops_queue.v1")
        self.assertFalse(payload.get("enabled"))
        self.assertEqual(payload.get("reason_code"), "feature_disabled")
        self.assertEqual(payload.get("request_id"), "ops-ai-queue-disabled")
        self.assertEqual(payload.get("items"), [])
        self.assertTrue((payload.get("advisory_policy") or {}).get("advisory_only"))
        self.assertFalse((payload.get("advisory_policy") or {}).get("mutates_operational_state"))
        self.assertEqual((payload.get("frontend_contract") or {}).get("state"), "disabled_by_feature_flag")

    def test_operations_ai_ops_queue_prioritizes_without_mutating_or_leaking_pii(self):
        before_ticket_state = self.ticket.estado
        before_order_state = self.assisted_order.estado

        with patch.object(feature_flags, "FEATURE_AI_OPS_QUEUE", True):
            response = self.client.get(
                "/api/v2/analytics/operations/ai-ops-queue?limit=10",
                headers={**self._auth(), "X-Request-Id": "ops-ai-queue-1"},
            )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload.get("contract_version"), "operations.ai_ops_queue.v1")
        self.assertTrue(payload.get("enabled"))
        self.assertEqual(payload.get("agent_display_name"), "Valeria IA-Analytics")
        self.assertEqual(payload.get("request_id"), "ops-ai-queue-1")
        self.assertEqual((payload.get("tenant") or {}).get("slug"), "junin")
        self.assertEqual((payload.get("frontend_contract") or {}).get("render_as"), "ai_ops_queue")
        self.assertEqual((payload.get("model_policy") or {}).get("contract_version"), "llm.task_policy.v1")
        self.assertTrue((payload.get("summary") or {}).get("total") >= 3)
        self.assertGreaterEqual((payload.get("summary") or {}).get("high"), 1)
        self.assertTrue((payload.get("summary") or {}).get("advisory_only"))

        policy = payload.get("advisory_policy") or {}
        self.assertTrue(policy.get("advisory_only"))
        self.assertFalse(policy.get("mutates_operational_state"))
        self.assertTrue(policy.get("requires_operator_confirmation"))
        self.assertFalse(policy.get("external_ai_required"))

        items = payload.get("items") or []
        sources = {item.get("source") for item in items}
        self.assertIn("ticket", sources)
        self.assertIn("order", sources)
        self.assertIn("survey", sources)
        for item in items:
            self.assertTrue((item.get("pii") or {}).get("redacted"))
            action = item.get("recommended_action") or {}
            self.assertEqual(action.get("method"), "GET")
            self.assertTrue(action.get("endpoint"))
            self.assertTrue(action.get("href"))
            self.assertEqual(action.get("frontend_path"), action.get("href"))
            self.assertFalse(str(action.get("href")).startswith("/api/"))

        actions_by_source = {item.get("source"): item.get("recommended_action") or {} for item in items}
        self.assertIn("/tickets", actions_by_source.get("ticket", {}).get("href") or "")
        self.assertEqual(
            actions_by_source.get("order", {}).get("endpoint"),
            f"/api/admin/tenants/{self.tenant.slug}/orders/conversational:{self.assisted_order.id}",
        )
        self.assertIn("/pedidos/", actions_by_source.get("order", {}).get("href") or "")
        self.assertIn("/admin/encuestas", actions_by_source.get("survey", {}).get("href") or "")
        self.assertIn("focus=live", actions_by_source.get("survey", {}).get("href") or "")

        encoded = str(payload).lower()
        self.assertNotIn("cliente.sensible@example.com", encoded)
        self.assertNotIn("+5492610000000", encoded)
        self.assertNotIn("cliente sensible", encoded)
        self.assertNotIn("mauricio@junin.com", encoded)
        self.assertNotIn("operador@test.com", encoded)

        db.session.refresh(self.ticket)
        db.session.refresh(self.assisted_order)
        self.assertEqual(self.ticket.estado, before_ticket_state)
        self.assertEqual(self.assisted_order.estado, before_order_state)

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
        self.assertIn("commerce", sources)
        self.assertIn("heatmap", sources)
        self.assertEqual(sources["tickets"].get("period_count"), 2)
        self.assertEqual(sources["commerce"].get("period_count"), 1)
        self.assertGreaterEqual(sources["heatmap"].get("period_count"), 1)
        self.assertEqual((payload.get("frontend_contract") or {}).get("render_as"), "analytics_freshness")


if __name__ == "__main__":
    unittest.main()
