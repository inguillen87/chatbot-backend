import os
import unittest
from datetime import datetime, timedelta, timezone

import jwt

os.environ.setdefault("FLASK_SKIP_GLOBAL_APP", "1")

from app import create_app, db
from config import Config
from models import (
    CatalogoItem,
    EncEncuesta,
    EncRespuesta,
    MunicipioPost,
    Notification,
    NotificationTemplate,
    PedidoConversacional,
    Promocion,
    TenantConfig,
    TenantProfile,
    TenantTicket,
    User,
    WhatsAppContactState,
    WhatsAppEnterpriseRule,
)


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
            vertical="educacion",
            subvertical="colegio",
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
                "assignee_name": self.employee.name,
                "assignee_email": self.employee.email,
                "conversation_id": "conv-beca-1",
                "demo_session_id": "demo-beca-1",
                "widget_id": "landing-widget",
                "contact_key": "whatsapp:+5491111111111",
                "intent": "consulta_beca",
                "contact": {"name": "Familia Gomez", "phone": "+5491111111111"},
                "zone": "centro",
                "channel": "whatsapp",
                "attachments": [
                    {
                        "id": "att-1",
                        "name": "comprobante.jpg",
                        "url": "https://cdn.example.com/comprobante.jpg",
                        "mimeType": "image/jpeg",
                    }
                ],
                "comments": [{"id": 1, "body": "Hola", "visibility": "public", "created_at": "2026-05-01T12:00:00Z"}],
            },
            latitud=-34.6,
            longitud=-58.4,
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
        db.session.add(
            CatalogoItem(
                user_id=self.owner.id,
                tenant_id=self.tenant.id,
                nombre="Uniforme escolar",
                descripcion="Chomba institucional",
                categoria="indumentaria",
                imagen_url="https://cdn.example.com/uniforme.jpg",
                promocion_info="10% off familia",
            )
        )
        db.session.add(
            PedidoConversacional(
                tenant_id=self.tenant.id,
                user_id=self.owner.id,
                estado="preparando",
                origen="whatsapp",
                monto_monetario=1500,
                items=[{"nombre": "Uniforme escolar", "cantidad": 1}],
            )
        )
        db.session.add(
            MunicipioPost(
                municipio_id=self.owner.id,
                tipo_post="noticia",
                titulo="Reunion de familias",
                descripcion="Comunicado para familias del colegio.",
            )
        )
        db.session.add(
            Promocion(
                pyme_user_id=self.owner.id,
                nombre_promocion="Promo vuelta a clases",
                descripcion_publica="Descuento en uniformes.",
                tipo_promocion="TOTAL_CARRITO_DESCUENTO_PORCENTAJE",
                valor_descuento=10,
            )
        )
        db.session.add(
            TenantConfig(
                tenant_id=self.tenant.id,
                key="links",
                channel="whatsapp",
                json_value={"links": [{"label": "Portal familias", "url": "https://demo.chatboc.ar/familias"}]},
            )
        )
        db.session.add(
            WhatsAppEnterpriseRule(
                tenant_id=self.tenant.id,
                enforce_template_outside_24h=True,
                max_outbound_per_hour=200,
                quiet_hours_start=22,
                quiet_hours_end=7,
                blocked_keywords=["spam"],
            )
        )
        db.session.add(
            WhatsAppContactState(
                tenant_id=self.tenant.id,
                recipient="whatsapp:+5491111111111",
                last_inbound_at=datetime.now(timezone.utc),
            )
        )
        encuesta = EncEncuesta(
            tenant_id=self.tenant.id,
            slug="voto-saas",
            titulo="Votacion comedor",
            tipo="votacion",
            estado="publicada",
            es_votacion_envivo=True,
            mostrar_resultados_envivo=True,
        )
        db.session.add(encuesta)
        db.session.flush()
        db.session.add(EncRespuesta(encuesta_id=encuesta.id, tenant_id=self.tenant.id, canal="widget"))

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

    def test_employee_routing_contract_scope_update_and_auto_assign(self):
        unassigned = TenantTicket(
            tenant_id=self.tenant.id,
            user_id=self.owner.id,
            categoria="educacion",
            descripcion="Necesito retirar documentacion",
            estado="nuevo",
            origen="whatsapp",
            datos_extra={"title": "Retiro de documentacion", "zone": "centro", "channel": "whatsapp"},
        )
        db.session.add(unassigned)
        db.session.commit()

        response = self.client.get(
            "/api/v2/employee-routing",
            headers={**self._auth(self.owner), "X-Request-Id": "routing-1"},
        )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload.get("contract_version"), "employee.routing.v1")
        self.assertEqual(payload.get("request_id"), "routing-1")
        self.assertGreaterEqual(payload["queues"]["unassigned_count"], 1)
        recommendation = next(item for item in payload["recommendations"] if item["ticket"]["id"] == unassigned.id)
        self.assertEqual(recommendation["suggested_assignee"]["id"], self.employee.id)
        self.assertIn("category_match", recommendation["reasons"])

        scope_response = self.client.patch(
            f"/api/v2/employees/{self.employee.id}/routing-scope",
            json={
                "categorias": ["educacion", "pagos"],
                "zonas": ["centro", "norte"],
                "channels": ["whatsapp", "widget"],
                "permisos": ["tickets_assign", "orders_assign"],
            },
            headers=self._auth(self.owner),
        )

        self.assertEqual(scope_response.status_code, 200)
        scope_payload = scope_response.get_json()
        self.assertEqual(scope_payload.get("contract_version"), "employee.routing_scope.v1")
        self.assertIn("pagos", scope_payload["employee"]["scope"]["categorias"])
        self.assertIn("widget", scope_payload["employee"]["scope"]["channels"])

        assign_response = self.client.post(
            "/api/v2/employee-routing/auto-assign",
            json={
                "dry_run": False,
                "tickets": [{"source_model": "TenantTicket", "id": unassigned.id}],
            },
            headers={**self._auth(self.owner), "X-Request-Id": "routing-assign-1"},
        )

        self.assertEqual(assign_response.status_code, 200)
        assign_payload = assign_response.get_json()
        self.assertEqual(assign_payload.get("contract_version"), "employee.routing.auto_assign.v1")
        self.assertEqual(assign_payload.get("request_id"), "routing-assign-1")
        self.assertEqual(assign_payload["applied_count"], 1)
        refreshed = db.session.get(TenantTicket, unassigned.id)
        self.assertEqual(refreshed.datos_extra["assignee_id"], self.employee.id)

    def test_tenant_health_contract(self):
        response = self.client.get("/api/v2/tenant-health", headers=self._auth(self.owner))

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload.get("contract_version"), "tenant.health.v1")
        self.assertIn(payload["health"]["status"], {"healthy", "warning", "critical"})
        self.assertIn("integrations", payload)
        self.assertIn("queues", payload)
        self.assertIn("recommended_actions", payload)

    def test_catalog_quality_contract_surfaces_image_price_and_stock_gaps(self):
        db.session.add(
            CatalogoItem(
                user_id=self.owner.id,
                tenant_id=self.tenant.id,
                nombre="Remera sin foto",
                descripcion="Producto para completar desde marketplace.",
                categoria="indumentaria",
                precio="2500",
                cantidad="12",
                disponible=True,
            )
        )
        db.session.add(
            CatalogoItem(
                user_id=self.owner.id,
                tenant_id=self.tenant.id,
                nombre="Cuaderno sin precio",
                descripcion="Falta precio para poder vender.",
                categoria="libreria",
                imagen_url="https://cdn.example.com/cuaderno.jpg",
                cantidad="20",
                disponible=True,
            )
        )
        db.session.commit()

        response = self.client.get(
            "/api/v2/catalog/quality",
            headers={**self._auth(self.owner), "X-Request-Id": "catalog-quality-1"},
        )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload.get("contract_version"), "catalog.quality.v1")
        self.assertEqual(payload.get("request_id"), "catalog-quality-1")
        self.assertEqual(payload["tenant"]["slug"], self.tenant.slug)
        self.assertGreaterEqual(payload["summary"]["products"], 3)
        self.assertGreaterEqual(payload["summary"]["missing_images"], 1)
        self.assertGreaterEqual(payload["summary"]["missing_price"], 1)
        self.assertTrue(payload["queues"]["missing_images"])
        self.assertTrue(payload["queues"]["missing_price"])
        self.assertEqual(payload["frontend_contract"]["render_as"], "catalog_quality_command_center")

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
        item = payload["items"][0]
        self.assertEqual(item["channel"], "whatsapp")
        self.assertTrue(item["timeline"])
        self.assertEqual(item["conversation_id"], "conv-beca-1")
        self.assertTrue(item["attachments"])
        self.assertIn("sla", item)
        self.assertIn("allowed_actions", item)
        self.assertIn("next_steps", item)
        self.assertEqual(item["source_metadata"]["demo_session_id"], "demo-beca-1")
        self.assertTrue(item["map"]["can_render"])
        self.assertEqual(item["frontend_contract"]["render_as"], "inbox_360_drawer")
        self.assertEqual(payload["frontend_contract"]["drawer_contract"], "inbox.omnichannel.detail.v1")

    def test_omnichannel_inbox_detail_contract_for_drawer_360(self):
        response = self.client.get(
            f"/api/v2/inbox/omnichannel/{self.ticket.id}",
            headers={**self._auth(self.owner), "X-Request-Id": "inbox-detail-1"},
        )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload.get("contract_version"), "inbox.omnichannel.detail.v1")
        self.assertEqual(payload.get("request_id"), "inbox-detail-1")
        item = payload["item"]
        self.assertEqual(item["ticket_id"], self.ticket.id)
        self.assertTrue(item["allowed_actions"])
        self.assertTrue(any(action["id"] == "reply" for action in item["allowed_actions"]))
        self.assertEqual(item["source_metadata"]["contact_key"], "whatsapp:+5491111111111")
        self.assertEqual(item["sla"]["priority"], "high")

    def test_production_smoke_contract_checks_critical_runtime_surfaces(self):
        response = self.client.get(
            "/api/v2/platform/production-smoke",
            headers={**self._auth(self.super_admin), "X-Request-Id": "smoke-1"},
        )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload.get("contract_version"), "platform.production_smoke.v1")
        self.assertEqual(payload.get("request_id"), "smoke-1")
        self.assertIn(payload["status"], {"pass", "warning", "fail"})
        check_ids = {item["id"] for item in payload["checks"]}
        self.assertIn("widget_platform_onboarding", check_ids)
        self.assertIn("socket_disabled_for_landing", check_ids)
        self.assertIn("tenant_admin_experience", check_ids)
        self.assertIn("inbox_360", check_ids)
        self.assertEqual(payload["frontend_contract"]["render_as"], "production_smoke_report")

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

    def test_tenant_admin_experience_contract_unifies_profile_operations_and_modules(self):
        response = self.client.get(
            "/api/v2/tenant/admin-experience",
            headers={**self._auth(self.owner), "X-Request-Id": "admin-exp-1"},
        )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload.get("contract_version"), "tenant.admin_experience.v1")
        self.assertEqual(payload.get("request_id"), "admin-exp-1")
        self.assertEqual(payload["tenant"]["slug"], self.tenant.slug)
        self.assertEqual(payload["profile"]["vertical"], "educacion")
        self.assertGreaterEqual(payload["profile"]["readiness"]["score"], 0)
        self.assertEqual(payload["operations"]["dashboard"]["contract_version"], "operations.dashboard.v1")
        self.assertEqual(payload["operations"]["freshness"]["contract_version"], "operations.freshness.v1")
        self.assertIsInstance(payload["operations"]["freshness"]["summary"]["can_render_heatmap"], bool)
        self.assertEqual(payload["lead_capture"]["summary"]["open"], 1)
        lead_item = payload["lead_capture"]["items"][0]
        self.assertIn("ticket_id", lead_item)
        self.assertIn("intent", lead_item)
        self.assertIn("next_action", lead_item)
        self.assertEqual(payload["surveys_votings"]["summary"]["live_votes"], 1)
        self.assertEqual(payload["marketplace"]["summary"]["products"], 1)
        self.assertIn("with_images", payload["marketplace"]["summary"])
        self.assertIn("missing_images", payload["marketplace"]["summary"])
        self.assertIn("products_without_image", payload["marketplace"]["summary"])
        self.assertIn("bulk_import_status", payload["marketplace"]["summary"])
        self.assertEqual(payload["marketplace"]["quality"]["contract_version"], "catalog.quality.v1")
        self.assertEqual(payload["marketplace"]["quality"]["summary"]["products"], 1)
        self.assertEqual(payload["whatsapp"]["contract_version"], "whatsapp.experience.v1")
        self.assertTrue(payload["whatsapp"]["channel"]["enabled"])
        self.assertTrue(payload["whatsapp"]["conversation_intelligence"]["inputs"]["image"]["enabled"])
        self.assertIn("route_progress", payload["whatsapp"]["tracking"]["courier_style_map"]["render_contract"]["animations"])
        self.assertEqual(payload["education"]["profile"]["is_education"], True)
        module_ids = {item["id"] for item in payload["modules"]}
        self.assertIn("inbox", module_ids)
        self.assertIn("analytics", module_ids)
        self.assertIn("surveys_votings", module_ids)
        self.assertIn("marketplace", module_ids)
        self.assertIn("education", module_ids)
        whatsapp_module = next(item for item in payload["modules"] if item["id"] == "widget_whatsapp")
        self.assertEqual(whatsapp_module["label"], "Widget/WhatsApp/Voz")
        self.assertEqual(whatsapp_module["endpoint"], "/api/v2/whatsapp/experience")
        for module in payload["modules"]:
            self.assertIn("secondary_endpoints", module)
            self.assertIn("widgets", module)
        for section in payload["education"]["admin_menu"]["panel_sections"]:
            self.assertIn("endpoint", section)
            self.assertIn("route", section)
            self.assertIn("widgets", section)
            self.assertIn("secondary_endpoints", section)

    def test_whatsapp_experience_contract_connects_channel_content_tracking_and_admin_panel(self):
        response = self.client.get(
            "/api/v2/whatsapp/experience",
            headers={**self._auth(self.owner), "X-Request-Id": "whatsapp-exp-1"},
        )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload.get("contract_version"), "whatsapp.experience.v1")
        self.assertEqual(payload.get("request_id"), "whatsapp-exp-1")
        self.assertEqual(response.headers.get("X-Request-Id"), "whatsapp-exp-1")
        self.assertEqual(payload["tenant"]["slug"], self.tenant.slug)
        self.assertTrue(payload["channel"]["enabled"])
        self.assertEqual(payload["enterprise_rules"]["max_outbound_per_hour"], 200)
        self.assertEqual(payload["contact_window"]["active_24h"], 1)
        self.assertTrue(payload["conversation_intelligence"]["inputs"]["emoji"]["enabled"])
        self.assertTrue(payload["conversation_intelligence"]["inputs"]["location"]["enabled"])
        self.assertTrue(payload["conversation_intelligence"]["inputs"]["audio_note"]["enabled"])
        self.assertTrue(payload["conversation_intelligence"]["inputs"]["video"]["enabled"])
        self.assertFalse(payload["conversation_intelligence"]["inputs"]["video"]["analysis_ready"])
        self.assertTrue(payload["conversation_intelligence"]["voice_calls"]["enabled"])
        self.assertTrue(payload["conversation_intelligence"]["voice_calls"]["capabilities"]["native_speech_to_speech"])
        self.assertEqual(payload["content_modules"]["catalog"]["items"], 1)
        self.assertEqual(payload["content_modules"]["catalog"]["items_with_images"], 1)
        self.assertGreaterEqual(payload["content_modules"]["surveys_votings"]["responses"], 1)
        self.assertEqual(payload["content_modules"]["news_events"]["by_type"]["noticia"], 1)
        self.assertTrue(payload["content_modules"]["promotions"]["enabled"])
        self.assertEqual(payload["content_modules"]["links"]["tenant_config_links"], 1)
        self.assertEqual(payload["tracking"]["claims"]["open"], 1)
        self.assertEqual(payload["tracking"]["orders"]["total"], 1)
        self.assertEqual(payload["tracking"]["claims"]["experience_endpoint"], "/api/public/tracking/experience?kind=claim&code={code}&pin={pin}")
        self.assertEqual(payload["tracking"]["orders"]["experience_endpoint"], "/api/public/tracking/experience?kind=order&code={code}")
        self.assertEqual(payload["tracking"]["courier_style_map"]["render_contract"]["fallback_when_no_coordinates"], "timeline_only")
        self.assertIn("route_progress", payload["tracking"]["courier_style_map"]["render_contract"]["animations"])
        self.assertEqual(payload["admin_panel"]["inbox"], "/api/v2/inbox/omnichannel")
        self.assertTrue(payload["education"]["enabled"])
        self.assertEqual(payload["frontend_contract"]["render_as"], "whatsapp_operations_hub")

        alias_response = self.client.get(
            f"/api/v2/tenants/{self.tenant.slug}/whatsapp/experience",
            headers={**self._auth(self.super_admin), "X-Request-Id": "whatsapp-exp-alias-1"},
        )
        self.assertEqual(alias_response.status_code, 200)
        alias_payload = alias_response.get_json()
        self.assertEqual(alias_payload.get("contract_version"), "whatsapp.experience.v1")
        self.assertEqual(alias_payload.get("request_id"), "whatsapp-exp-alias-1")
        self.assertEqual(alias_payload["tenant"]["slug"], self.tenant.slug)

    def test_superadmin_command_center_contract(self):
        response = self.client.get(
            "/api/v2/superadmin/command-center",
            headers={**self._auth(self.super_admin), "X-Request-Id": "command-center-1"},
        )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload.get("contract_version"), "superadmin.command_center.v1")
        self.assertEqual(payload.get("request_id"), "command-center-1")
        self.assertEqual(payload["summary"]["tenants"], 1)
        self.assertIn("tenant_creation", payload)
        self.assertEqual(payload["tenant_creation"]["endpoint"], "/api/admin/tenants")
        self.assertTrue(payload["tenants"]["items"])
        tenant_item = payload["tenants"]["items"][0]
        self.assertEqual(tenant_item["tenant"]["slug"], self.tenant.slug)
        self.assertEqual(tenant_item["tenant_slug"], self.tenant.slug)
        self.assertEqual(tenant_item["display_name"], self.tenant.nombre)
        self.assertIn("health_score", tenant_item)
        self.assertIn("status", tenant_item)
        self.assertIn("risk_reason", tenant_item)
        if payload["tenants"]["top_risky"]:
            risky_item = payload["tenants"]["top_risky"][0]
            self.assertIn("tenant_slug", risky_item)
            self.assertIn("risk_reason", risky_item)
        self.assertIn("drilldown_endpoint_template", payload["frontend_contract"])


if __name__ == "__main__":
    unittest.main()
